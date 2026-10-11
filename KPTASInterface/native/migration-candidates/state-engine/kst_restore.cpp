/* Restore to tick T at a gate: apply a record set (section 9 PAGE records, the
 * format Python writes for restore sets W) with per-page commit reconciliation,
 * skip pages already equal to the live map, verify, and emit the restore batch.
 * Registers come back through the context records in harness_game: the gate's
 * switch-back loads slot 0's record after these writes. */
#include "kst_int.hpp"

static int writable(u16 prot) {
    if (prot & PAGE_GUARD) return 0;
    switch (prot & 0xFF) {
    case PAGE_READWRITE: case PAGE_WRITECOPY: case PAGE_EXECUTE_READWRITE: case PAGE_EXECUTE_WRITECOPY:
        return 1;
    }
    return 0;
}

static inline void mark(u32 vpn) {
    u32 i = vpn - K->vpn_lo, w = i >> 5;
    K->mark[w] |= 1u << (i & 31);
    if (w < K->mark_lo) K->mark_lo = w;
    if (w > K->mark_hi) K->mark_hi = w;
}

static const char *apply_one(const kst_record_header *h, const u8 *data, kst_restore_stats *st) {
    u32 form = h->flags & (KST_F_DATA | KST_F_ZERO | KST_F_DECOMMIT);
    u32 vpn = h->vaddr >> 12, va = h->vaddr;
    if (h->vaddr & (KST_PAGE - 1)) return "unaligned vaddr";
    if (vpn < K->vpn_lo || vpn >= K->vpn_hi) return "vaddr outside the sim set";
    pent *e = kst__pent(vpn);
    if (!e->mclass || e->mclass != h->mclass) return "memory class does not match the region";
    if (form != KST_F_DATA && form != KST_F_ZERO && form != KST_F_DECOMMIT) return "PAGE needs one form";
    if (form == KST_F_DATA && h->dlen != KST_PAGE) return "DATA page without 4096 bytes";
    st->records++;
    if (form == KST_F_DECOMMIT) {
        if (!e->present) { st->skipped++; return 0; }
        if (e->mclass == KST_IMAGE_DATA) return "image_data pages cannot be decommitted";
        if (!VirtualFree(reinterpret_cast<void *>(static_cast<uintptr_t>(va)), KST_PAGE, MEM_DECOMMIT)) return "VirtualFree(MEM_DECOMMIT) failed";
        st->decommitted++;
        e->present = 0;
        e->prot = 0;
        memset(e->key, 0, 16);
        kst__leaf_touch(vpn);
        mark(vpn);
        return 0;
    }
    const u8 *key = form == KST_F_ZERO ? KST_ZERO_KEY : h->hash;
    if (e->present && e->prot == h->prot && !memcmp(e->key, key, 16)) { st->skipped++; return 0; }
    u16 cur = e->prot;
    if (!e->present) {
        if (VirtualAlloc(reinterpret_cast<void *>(static_cast<uintptr_t>(va)), KST_PAGE, MEM_COMMIT, PAGE_READWRITE) != reinterpret_cast<void *>(static_cast<uintptr_t>(va))) return "MEM_COMMIT failed";
        st->committed++;
        cur = PAGE_READWRITE;
    }
    DWORD old;
    if (!writable(cur)) {
        if (!VirtualProtect(reinterpret_cast<void *>(static_cast<uintptr_t>(va)), KST_PAGE, PAGE_READWRITE, &old)) return "VirtualProtect(RW) failed";
        cur = PAGE_READWRITE;
    }
    if (form == KST_F_DATA) memcpy(reinterpret_cast<void *>(static_cast<uintptr_t>(va)), data, KST_PAGE);
    else memset(reinterpret_cast<void *>(static_cast<uintptr_t>(va)), 0, KST_PAGE);
    st->written++;
    if (h->prot != cur) {
        if (!VirtualProtect(reinterpret_cast<void *>(static_cast<uintptr_t>(va)), KST_PAGE, h->prot, &old)) return "VirtualProtect(T) failed";
        st->prot_changes++;
    }
    e->present = 1;
    e->prot = h->prot;
    memcpy(e->key, key, 16);
    kst__leaf_touch(vpn);
    mark(vpn);
    return 0;
}

/* Apply the records in buf; returns the bytes consumed (whole records only). */
static u32 apply_buf(const u8 *buf, u32 len, kst_restore_stats *st, int final) {
    u32 off = 0;
    const char *err = 0;
    while (off < len && !err) {
        if (len - off < sizeof(kst_record_header)) { if (final) err = "truncated record header"; break; }
        kst_record_header h;
        memcpy(&h, buf + off, sizeof h);
        if (h.magic[0] != 'S' || h.magic[1] != 'R') { err = "bad record magic"; break; }
        u32 size = sizeof h + ((h.dlen + 7) & ~7u);
        if (size > len - off) { if (final) err = "truncated record data"; break; }
        if (h.kind == KST_REC_PAGE) err = apply_one(&h, buf + off + sizeof h, st);
        else if (h.kind != KST_REC_JOURNAL && h.kind != KST_REC_TICK_END && h.kind != KST_REC_WRAP)
            err = "unknown record kind";
        off += size;
    }
    if (err) kst_fatal(err);   /* memory is partly written: never continue */
    return off;
}

static int restore_finish(u64 target, const u8 expect_root[32], kst_restore_stats *st, u64 t0);

int kst_restore_records(const u8 *buf, u32 len, u64 target, const u8 expect_root[32], kst_restore_stats *st) {
    memset(st, 0, sizeof *st);
    st->target = target;
    if (!K->in_gate) kst_fatal("restore outside the gate");
    u64 t0 = kst_qpc();
    apply_buf(buf, len, st, 1);
    return restore_finish(target, expect_root, st, t0);
}

/* Streaming variant: the set is read through a fixed 1 MiB buffer, so a large
 * write set never needs one contiguous allocation in the 2 GB address space. */
int kst_restore_file(const WCHAR *path, u64 target, const u8 expect_root[32], kst_restore_stats *st) {
    memset(st, 0, sizeof *st);
    st->target = target;
    if (!K->in_gate) kst_fatal("restore outside the gate");
    u64 t0 = kst_qpc();
    HANDLE h = CreateFileW(path, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE, 0, OPEN_EXISTING,
                           FILE_ATTRIBUTE_NORMAL, 0);
    if (h == INVALID_HANDLE_VALUE) return -1;
    const u32 CAP = 1u << 20;
    static u8 *buf;
    if (!buf) buf = static_cast<u8 *>(kst_core_alloc(CAP));
    u32 have = 0;
    for (;;) {
        DWORD got = 0;
        if (!ReadFile(h, buf + have, CAP - have, &got, 0)) { CloseHandle(h); kst_fatal("restore set read failed"); }
        have += got;
        int final = got == 0;
        u32 used = apply_buf(buf, have, st, final);
        memmove(buf, buf + used, have - used);
        have -= used;
        if (final) break;
    }
    CloseHandle(h);
    return restore_finish(target, expect_root, st, t0);
}

static int restore_finish(u64 target, const u8 expect_root[32], kst_restore_stats *st, u64 t0) {
    /* image shadows follow the written bytes */
    for (u32 i = 0; i < K->nreg; i++) {
        region *r = &K->reg[i];
        if (!r->ww) memcpy(r->shadow, reinterpret_cast<void *>(static_cast<uintptr_t>(r->base)), r->size);
    }
    kst__ww_reset_all();
    u64 t1 = kst_qpc();
    /* writes landed: re-hash every changed page; then emit the restore batch */
    if (K->mark_lo != KST_NONE) {
        for (u32 w = K->mark_lo; w <= K->mark_hi; w++) {
            u32 bits = K->mark[w];
            K->mark[w] = 0;
            while (bits) {
                u32 b = static_cast<u32>(__builtin_ctz(bits));
                bits &= bits - 1;
                u32 vpn = K->vpn_lo + w * 32 + b, va = vpn << 12;
                pent *e = kst__pent(vpn);
                if (!e->present) {
                    kst__emit_page(target, va, e->mclass, 0, KST_F_DECOMMIT, e->key, 0, 1);
                    continue;
                }
                u8 key[16];
                kst_page_key(reinterpret_cast<void *>(static_cast<uintptr_t>(va)), key);
                if (memcmp(key, e->key, 16)) st->verify_bad++;
                int zero = !memcmp(e->key, KST_ZERO_KEY, 16);
                kst__emit_page(target, va, e->mclass, e->prot, zero ? KST_F_ZERO : KST_F_DATA, e->key,
                               zero ? 0 : reinterpret_cast<void *>(static_cast<uintptr_t>(va)), 1);
            }
        }
        K->mark_lo = KST_NONE;
        K->mark_hi = 0;
    }
    kst__emit_close(target, KST_F_RESTORE_POINT | KST_F_RESTORE);
    kst__compute_root();
    st->root_ok = expect_root ? !memcmp(K->root, expect_root, 32) : 1;
    kst_audit(&st->audit);
    u64 t2 = kst_qpc();
    st->us_apply = kst_us(t1 - t0);
    st->us_verify = kst_us(t2 - t1);
    return st->root_ok && !st->verify_bad && !(st->audit.extra | st->audit.missing | st->audit.key_mismatch |
                                               st->audit.prot_mismatch);
}
