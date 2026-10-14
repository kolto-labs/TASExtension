/* Per-tick capture: write-watch + commit walk + image compare -> changed pages,
 * BLAKE2s-128 page keys, staging-ring records (contract section 9), and the
 * section 8.6 state root over the live map. Also the full-sweep audit. */
#include "kst_int.hpp"
#include <emmintrin.h>

// Preserve the public native and assembly C symbol contract.
extern "C" {

/* ---------------------------------------------------------------- helpers */

int kst__readable(u16 prot) {
    if (prot & PAGE_GUARD) return 0;
    switch (prot & 0xFF) {
    case PAGE_READONLY: case PAGE_READWRITE: case PAGE_WRITECOPY:
    case PAGE_EXECUTE_READ: case PAGE_EXECUTE_READWRITE: case PAGE_EXECUTE_WRITECOPY:
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

/* 1 when the 4096-byte pages a and b are equal. Both are 16-byte aligned. */
static int page_equal(const void *a, const void *b) {
    const __m128i *x = static_cast<const __m128i *>(a);
    const __m128i *y = static_cast<const __m128i *>(b);
    for (u32 i = 0; i < KST_PAGE / 16; i += 8) {
        __m128i d = _mm_or_si128(_mm_or_si128(_mm_or_si128(_mm_xor_si128(_mm_load_si128(x + i), _mm_load_si128(y + i)),
                                                           _mm_xor_si128(_mm_load_si128(x + i + 1), _mm_load_si128(y + i + 1))),
                                              _mm_or_si128(_mm_xor_si128(_mm_load_si128(x + i + 2), _mm_load_si128(y + i + 2)),
                                                           _mm_xor_si128(_mm_load_si128(x + i + 3), _mm_load_si128(y + i + 3)))),
                                 _mm_or_si128(_mm_or_si128(_mm_xor_si128(_mm_load_si128(x + i + 4), _mm_load_si128(y + i + 4)),
                                                           _mm_xor_si128(_mm_load_si128(x + i + 5), _mm_load_si128(y + i + 5))),
                                              _mm_or_si128(_mm_xor_si128(_mm_load_si128(x + i + 6), _mm_load_si128(y + i + 6)),
                                                           _mm_xor_si128(_mm_load_si128(x + i + 7), _mm_load_si128(y + i + 7)))));
        if (_mm_movemask_epi8(_mm_cmpeq_epi8(d, _mm_setzero_si128())) != 0xffff) return 0;
    }
    return 1;
}

/* Key cache: a direct-mapped copy of the last bytes keyed for a page, with
 * their key. A hit needs all 4096 live bytes equal to the copy, and the copy's
 * key is the BLAKE2s-128 of the copy, so a hit's key is the key of the live
 * bytes: keys, records and roots are those of a full rehash. The cache is
 * harness_core: one private allocation at init (kst_init_mem, before the game
 * runs), never captured. It is bottom-up like the live-map arrays allocated
 * beside it, so the shim's top-down blocks (core, exec, import thunks) keep
 * their addresses. Plain statics would not do: a host that links kst
 * into its exe would put them in image_data. Decommits and restores drop
 * entries, though a hit never depends on that. Without the allocation every
 * page is hashed, as before. */
#define KC_BITS 11u
#define KC_SLOTS (1u << KC_BITS)
typedef struct kc_meta { u32 vpn; u8 key[16]; } kc_meta;   /* vpn 0: empty (no sim page has vpn 0) */
KST_CORE static u8 *kc_page;      /* KC_SLOTS pages */
KST_CORE static kc_meta *kc_ent;  /* KC_SLOTS entries */

static inline u32 kc_slot(u32 vpn) { return (vpn * 2654435761u) >> (32 - KC_BITS); }

void kst__keycache_init(void) {
    if (kc_page) return;
    u32 bytes = KC_SLOTS * KST_PAGE + KC_SLOTS * (u32)sizeof(kc_meta);
    u8 *p = static_cast<u8 *>(VirtualAlloc(0, bytes, MEM_RESERVE | MEM_COMMIT, PAGE_READWRITE));
    if (!p) { kst_outs("kst: key cache allocation failed, every page is hashed\n"); return; }
    kc_ent = reinterpret_cast<kc_meta *>(p + KC_SLOTS * KST_PAGE);
    kc_page = p;
}

static inline void kc_drop(u32 vpn) {
    if (!kc_page) return;
    kc_meta *m = &kc_ent[kc_slot(vpn)];
    if (m->vpn == vpn) m->vpn = 0;
}

void kst__leaf_touch(u32 vpn) { K->leaf_dirty[(vpn >> 10) - K->leaf_lo] = 1; }

static u32 region_span(const region *r) {
    if (r->mclass == KST_GAME_ARENA) {
        u32 offset = r->base - K->cfg.arena_base;
        if (K->arena_span <= offset) return 0;
        u32 span = K->arena_span - offset;
        return span < r->size ? span : r->size;
    }
    return r->size;
}

/* 64-bit stores/loads the Python consumer sees whole (SSE2 movq). */
static inline void st64(volatile u64 *p, u64 v) {
    __asm__ volatile("movq %1, %%xmm0\n\tmovq %%xmm0, %0" : "=m"(*p) : "m"(v) : "xmm0", "memory");
}
static inline u64 ld64(volatile u64 *p) {
    u64 v;
    __asm__ volatile("movq %1, %%xmm0\n\tmovq %%xmm0, %0" : "=m"(v) : "m"(*p) : "xmm0", "memory");
    return v;
}

/* ---------------------------------------------------------------- staging ring */

#define RH_WRITE 16
#define RH_READ 24
#define RH_LAST 32
#define RH_PPID 40
#define RH_CPID 44
#define RH_STATE 48

typedef struct __attribute__((packed)) rec_hdr {
    char magic[2];
    u8 kind, mclass;
    u16 flags, prot;
    u32 vaddr, dlen;
    u64 tick;
    u8 hash[16];
} rec_hdr;

int kst_ring_attached(void) { return K->ring_base != 0; }

void kst__ring_open(void) {
    if (!K->cfg.ring_path) return;
    HANDLE f = CreateFileW(K->cfg.ring_path, GENERIC_READ | GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE,
                           0, OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, 0);
    if (f == INVALID_HANDLE_VALUE) kst_fatal("staging ring: cannot open the file");
    DWORD hi = 0, size = GetFileSize(f, &hi);
    int fresh = size < 4096 + 3 * KST_PAGE;
    u32 cap = K->cfg.ring_mb << 20;
    if (fresh) {
        LONG h2 = 0;
        SetFilePointer(f, (LONG)(4096 + cap), &h2, FILE_BEGIN);
        if (!SetEndOfFile(f)) kst_fatal("staging ring: cannot size the file");
        size = 4096 + cap;
    }
    HANDLE m = CreateFileMappingW(f, 0, PAGE_READWRITE, 0, size, 0);
    if (!m) kst_fatal("staging ring: CreateFileMapping failed");
    u8 *base = static_cast<u8 *>(MapViewOfFile(m, FILE_MAP_ALL_ACCESS, 0, 0, size));
    if (!base) kst_fatal("staging ring: MapViewOfFile failed");
    if (fresh) {
        memset(base, 0, 4096);
        memcpy(base, "KPSR", 4);
        *(u16 *)(base + 4) = 1;
        *(u16 *)(base + 6) = 8;
        *(u32 *)(base + 8) = 4096;
        *(u32 *)(base + 12) = cap;
    } else {
        if (memcmp(base, "KPSR", 4) || *(u16 *)(base + 4) != 1 || *(u16 *)(base + 6) != 8 ||
            *(u32 *)(base + 8) != 4096)
            kst_fatal("staging ring: bad header");
        cap = *(u32 *)(base + 12);
        if (cap % KST_PAGE || 4096 + cap > size) kst_fatal("staging ring: bad capacity");
    }
    K->ring_base = base;
    K->ring_cap = cap;
    K->ring_wpos = ld64((volatile u64 *)(base + RH_WRITE));
    K->ring_off = (u32)(K->ring_wpos % cap);
    *(volatile u32 *)(base + RH_PPID) = GetCurrentProcessId();
    kst_buf b;
    char line[160];
    kb_init(&b, line, sizeof line);
    kb_str(&b, fresh ? "kst: ring created, capacity " : "kst: ring opened, capacity ");
    kb_u64(&b, cap);
    kb_str(&b, ", write_pos ");
    kb_u64(&b, K->ring_wpos);
    kb_ch(&b, '\n');
    kst_out(b.p, b.n);
}

/* Set by the shim. Asked when the consumer made no room for 60 s: nonzero (a
 * streamed step whose host is alive, only slow) waits another 60 s. */
int (*kst_ring_patience)(void);

static void ring_wait(u64 need_end) {
    u64 t0 = 0, since = 0;
    u32 spins = 0;
    for (;;) {
        u64 read = ld64((volatile u64 *)(K->ring_base + RH_READ));
        /* The consumer owns this cursor, but it must still describe a record
         * boundary inside the producer's published window. In particular,
         * subtracting a cursor ahead of ring_wpos would wrap u64 and make a
         * corrupt ring look perpetually full. */
        if (read & 7u) kst_fatal("staging ring: unaligned consumer cursor");
        if (read > K->ring_wpos) kst_fatal("staging ring: consumer cursor exceeds producer");
        if (K->ring_wpos - read > K->ring_cap) kst_fatal("staging ring: consumer cursor fell behind");
        if (need_end < read || need_end - read <= K->ring_cap) break;
        if (!t0) since = t0 = kst_qpc();
        Sleep(spins++ < 64 ? 0 : 1);
        if (kst_us(kst_qpc() - since) > 60000000ull) {
            if (!kst_ring_patience || !kst_ring_patience()) kst_fatal("staging ring: consumer stalled for 60 s");
            since = kst_qpc();
        }
    }
    if (t0) K->st.ring_wait_us += kst_us(kst_qpc() - t0);
}

static void ring_put(u8 kind, u8 mclass, u16 flags, u16 prot, u32 vaddr, u64 tick,
                     const u8 hash[16], const void *data, u32 dlen) {
    u32 cap = K->ring_cap, size = 40 + ((dlen + 7) & ~7u);
    u32 rem = cap - K->ring_off, skip = 0;
    int wrap = 0;
    if (rem < 40) skip = rem;
    else if (size > rem) { skip = rem; wrap = 1; }
    u64 advance = (u64)skip + size;
    if (K->ring_wpos > ~(u64)0 - advance) kst_fatal("staging ring: producer position overflow");
    ring_wait(K->ring_wpos + advance);
    u8 *d = K->ring_base + 4096;
    if (wrap) {
        memset(d + K->ring_off, 0, 40);
        d[K->ring_off] = 'S';
        d[K->ring_off + 1] = 'R';
        d[K->ring_off + 2] = KST_REC_WRAP;
    }
    if (skip) { K->ring_wpos += skip; K->ring_off = 0; }
    rec_hdr *h = reinterpret_cast<rec_hdr *>(d + K->ring_off);
    h->magic[0] = 'S';
    h->magic[1] = 'R';
    h->kind = kind;
    h->mclass = mclass;
    h->flags = flags;
    h->prot = prot;
    h->vaddr = vaddr;
    h->dlen = dlen;
    h->tick = tick;
    if (hash) memcpy(h->hash, hash, 16); else memset(h->hash, 0, 16);
    if (dlen) memcpy((u8 *)(h + 1), data, dlen);
    u32 pad = ((dlen + 7) & ~7u) - dlen;
    if (pad) memset((u8 *)(h + 1) + dlen, 0, pad);
    K->ring_wpos += size;
    K->ring_off += size;
    if (K->ring_off == cap) K->ring_off = 0;
    if (kind == KST_REC_TICK_END) st64((volatile u64 *)(K->ring_base + RH_LAST), tick);
    st64((volatile u64 *)(K->ring_base + RH_WRITE), K->ring_wpos);
}

static void batch_add(u8 kind, u32 vaddr, const u8 hash[16]) {
    u8 e[21];
    e[0] = kind;
    memcpy(e + 1, &vaddr, 4);
    memcpy(e + 5, hash, 16);
    kst_b2s_update(&K->batch, e, 21);
}

void kst__emit_page(u64 tick, u32 vaddr, u8 mclass, u16 prot, u16 flags, const u8 key[16],
                    const void *data, int restore) {
    static constexpr u8 nullk[16]{};
    const u8 *h = (flags & KST_F_DECOMMIT) ? nullk : key;
    if (flags & KST_F_DECOMMIT) kc_drop(vaddr >> 12);
    u32 dlen = (flags & KST_F_DATA) ? KST_PAGE : 0;
    u64 t0 = kst_qpc();
    if (K->ring_base) ring_put(KST_REC_PAGE, mclass, flags, (flags & KST_F_DECOMMIT) ? 0 : prot, vaddr, tick, h,
                               data, dlen);
    K->st.us_ring += kst_qpc() - t0;
    batch_add(KST_REC_PAGE, vaddr, h);
    u64 t1 = kst_qpc();
    if (K->host.on_page) K->host.on_page(K->host.ud, tick, vaddr, mclass, prot, flags, h, data, restore);
    K->st.us_host += kst_qpc() - t1;
}

void kst__emit_close(u64 tick, u16 tflags) {
    if ((tflags & KST_F_RESTORE) && kc_page) memset(kc_ent, 0, KC_SLOTS * sizeof(kc_meta));
    u64 th = kst_qpc();
    u32 jl = K->host.journal ? K->host.journal(K->host.ud, tick, K->jbuf, K->jcap) : 0;
    K->st.us_host += kst_qpc() - th;
    u8 jk[16], dg[16];
    kst_blake2s128(K->jbuf, jl, jk);
    batch_add(KST_REC_JOURNAL, 0, jk);
    kst_b2s_final(&K->batch, dg);
    kst_b2s_init(&K->batch, 16);
    u64 t0 = kst_qpc();
    if (K->ring_base) {
        ring_put(KST_REC_JOURNAL, 0, 0, 0, 0, tick, jk, K->jbuf, jl);
        ring_put(KST_REC_TICK_END, 0, tflags, 0, 0, tick, dg, 0, 0);
    }
    K->st.us_ring += kst_qpc() - t0;
}

/* ---------------------------------------------------------------- state root (8.6) */

void kst__compute_root(void) {
    for (u32 l = 0; l < K->nleaf; l++) {
        if (!K->leaf_dirty[l]) continue;
        K->leaf_dirty[l] = 0;
        u32 hi = K->leaf_lo + l, v0 = hi << 10, v1 = v0 + 1024, n = 0;
        if (v0 < K->vpn_lo) v0 = K->vpn_lo;
        if (v1 > K->vpn_hi) v1 = K->vpn_hi;
        kst_sha s;
        kst_sha_init(&s);
        u8 hd[6] = {'K', 'P', 'L', 'F', static_cast<u8>(hi), static_cast<u8>(hi >> 8)};
        kst_sha_update(&s, hd, 6);
        for (u32 v = v0; v < v1; v++) {
            pent *e = kst__pent(v);
            if (!e->present) continue;
            u8 ent[22];
            u16 lo = (u16)(v & 0x3FF);
            memcpy(ent, &lo, 2);
            memcpy(ent + 2, e->key, 16);
            ent[18] = e->mclass;
            ent[19] = 0;
            memcpy(ent + 20, &e->prot, 2);
            kst_sha_update(&s, ent, 22);
            n++;
        }
        K->leaf_count[l] = (u16)n;
        if (n) kst_sha_final(&s, K->leaf_hash[l]);
    }
    kst_sha s;
    kst_sha_init(&s);
    kst_sha_update(&s, "KPRT", 4);
    for (u32 l = 0; l < K->nleaf; l++) {
        if (!K->leaf_count[l]) continue;
        u16 hi = (u16)(K->leaf_lo + l);
        kst_sha_update(&s, &hi, 2);
        kst_sha_update(&s, K->leaf_hash[l], 32);
    }
    kst_sha_final(&s, K->root);
}

void kst_root(u8 out[32]) { memcpy(out, K->root, 32); }

/* ---------------------------------------------------------------- dirty sources */

static u32 ww_collect(u32 base, u32 span, int mark_them) {
    if (!span) return 0;
    u32 total = 0;
    for (;;) {
        ULONG_PTR n = K->wwcap;
        ULONG gran = 0;
        if (GetWriteWatch(WRITE_WATCH_FLAG_RESET, (void *)base, span, K->wwbuf, &n, &gran))
            kst_fatal("GetWriteWatch failed");
        if (gran != KST_PAGE) kst_fatal("write-watch granularity is not 4096");
        if (mark_them)
            for (ULONG_PTR i = 0; i < n; i++) mark((u32)K->wwbuf[i] >> 12);
        total += (u32)n;
        if (n < K->wwcap) break;
    }
    return total;
}

void kst__ww_reset_all(void) {
    for (u32 i = 0; i < K->nreg; i++)
        if (K->reg[i].ww) ww_collect(K->reg[i].base, region_span(&K->reg[i]), 0);
}

/* Commit state and protection of every page of [base, base+span) into prot_now;
 * mark pages whose state differs from the live map (or all committed pages). */
static u32 walk(u32 base, u32 span, int all) {
    u32 changed = 0, a = base, end = base + span;
    while (a < end) {
        MEMORY_BASIC_INFORMATION mbi;
        if (!VirtualQuery((void *)a, &mbi, sizeof mbi)) kst_fatal("VirtualQuery failed");
        u32 rb = (u32)mbi.BaseAddress, re = rb + (u32)mbi.RegionSize;
        if (re > end) re = end;
        u16 pn = mbi.State == MEM_COMMIT ? (u16)mbi.Protect : 0;
        if (pn == 0 && mbi.State == MEM_COMMIT) pn = PAGE_NOACCESS;
        for (u32 v = a >> 12; v < re >> 12; v++) {
            K->prot_now[v - K->vpn_lo] = pn;
            if (all) { if (pn) mark(v); continue; }
            pent *e = kst__pent(v);
            if ((pn != 0) != (e->present != 0) || (pn && pn != e->prot)) { mark(v); changed++; }
        }
        a = re;
    }
    return changed;
}

static u32 image_compare(region *r, int all) {
    u32 n = 0;
    for (u32 off = 0; off < r->size; off += KST_PAGE) {
        void *p = (void *)(r->base + off);
        if (all || !page_equal(p, r->shadow + off)) {
            memcpy(r->shadow + off, p, KST_PAGE);
            mark((r->base + off) >> 12);
            if (!all) n++;
        }
    }
    return n;
}

/* ---------------------------------------------------------------- capture */

static void process(u64 tick, u32 vpn, u16 sweep_flag) {
    pent *e = kst__pent(vpn);
    u16 pn = K->prot_now[vpn - K->vpn_lo];
    u32 va = vpn << 12;
    if (!pn) {
        if (e->present) {
            e->present = 0;
            e->prot = 0;
            memset(e->key, 0, 16);
            kst__leaf_touch(vpn);
            kst__emit_page(tick, va, e->mclass, 0, KST_F_DECOMMIT | sweep_flag, e->key, 0, 0);
            K->st.emitted++;
            K->st.decommits++;
        }
        return;
    }
    if (!kst__readable(pn)) kst_fatal("sim page is not readable (guard or no-access protection)");
    u8 key[16];
    if (kc_page) {
        u32 s = kc_slot(vpn);
        kc_meta *m = &kc_ent[s];
        u8 *copy = kc_page + s * KST_PAGE;
        if (m->vpn == vpn && page_equal((const void *)va, copy)) memcpy(key, m->key, 16);
        else {
            memcpy(copy, (const void *)va, KST_PAGE);
            kst_page_key(copy, key);
            memcpy(m->key, key, 16);
            m->vpn = vpn;
            K->st.hashed++;
        }
    } else {
        kst_page_key((void *)va, key);
        K->st.hashed++;
    }
    if (e->present && e->prot == pn && !memcmp(e->key, key, 16)) return;
    e->present = 1;
    e->prot = pn;
    memcpy(e->key, key, 16);
    kst__leaf_touch(vpn);
    int zero = !memcmp(key, KST_ZERO_KEY, 16);
    kst__emit_page(tick, va, e->mclass, pn, (zero ? KST_F_ZERO : KST_F_DATA) | sweep_flag, key,
                   zero ? 0 : (void *)va, 0);
    K->st.emitted++;
}

static void capture(u64 tick, int sweep) {
    kst_tick_stats *st = &K->st;
    memset(st, 0, sizeof *st);
    st->tick = tick;
    st->sweep = (u32)sweep;
    u64 t0 = kst_qpc();
    for (u32 i = 0; i < K->nreg; i++)
        if (K->reg[i].ww) st->ww_dirty += ww_collect(K->reg[i].base, region_span(&K->reg[i]), 1);
    u64 t1 = kst_qpc();
    for (u32 i = 0; i < K->nreg; i++) {
        region *r = &K->reg[i];
        if (r->ww) st->commit_changed += walk(r->base, region_span(r), sweep);
        else {
            walk(r->base, r->size, 0);
            st->img_dirty += image_compare(r, sweep);
        }
    }
    u64 t2 = kst_qpc();
    u16 sf = sweep ? KST_F_SWEEP : 0;
    if (K->mark_lo != KST_NONE) {
        for (u32 w = K->mark_lo; w <= K->mark_hi; w++) {
            u32 bits = K->mark[w];
            if (!bits) continue;
            K->mark[w] = 0;
            while (bits) {
                u32 b = (u32)__builtin_ctz(bits);
                bits &= bits - 1;
                process(tick, K->vpn_lo + w * 32 + b, sf);
            }
        }
        K->mark_lo = KST_NONE;
        K->mark_hi = 0;
    }
    kst__emit_close(tick, (u16)(KST_F_RESTORE_POINT | (sweep ? KST_F_FULL : 0)));
    u64 t3 = kst_qpc();
    kst__compute_root();
    u64 t4 = kst_qpc();
    u64 ring = st->us_ring, hostt = st->us_host;
    st->us_ring = kst_us(ring);
    st->us_host = kst_us(hostt);
    st->us_ww = kst_us(t1 - t0);
    st->us_walk = kst_us(t2 - t1);
    st->us_hash = kst_us(t3 - t2 - ring - hostt);
    st->us_root = kst_us(t4 - t3);
    st->us_total = kst_us(t4 - t0 - hostt);
}

void kst__gate(void) {
    kst_hg *hg = K->hg;
    /* every sim write of the gate happens before the scan */
    u64 t = hg->tick_next;
    hg->tick_cur = t;
    hg->tick_next = t + 1;
    capture(t, t == 0);
    if (K->host.on_tick) K->host.on_tick(K->host.ud, t, K->root, &K->st);
}

/* Host-numbered capture (the shim owns the tick counter): capture the sim set
 * as the state at the gate of `tick`. The first capture is a full sweep. */
void kst_capture_tick(u64 tick, int sweep) {
    kst_hg *hg = K->hg;
    if (!K->captured_once) sweep = 1;
    K->captured_once = 1;
    hg->tick_cur = tick;
    hg->tick_next = tick + 1;
    capture(tick, sweep);
    if (K->host.on_tick) K->host.on_tick(K->host.ud, tick, K->root, &K->st);
}

/* A gate between captures (capture cadence). The tick counters in harness_game
 * are written exactly as a capture writes them. Write-watch is collected with
 * RESET and its pages marked, and the commit walk marks pages whose commit state
 * or protection now differs from the map: the mark bitmap is only cleared by a
 * capture (or a restore), so the next capture processes the union of every page
 * written, committed, decommitted or reprotected since the last one. The walk
 * per gate keeps a decommit at one gate and a recommit at a later one (no write
 * in between) visible, as per-gate capture does. Image sections need nothing:
 * the capture compares them with their shadows. Resetting write-watch here keeps
 * the shim's dead-stack scrub (which reads it without RESET) per gate. */
void kst_skip_tick(u64 tick) {
    kst_hg *hg = K->hg;
    if (!K->captured_once) kst_fatal("the first gate must be captured");
    hg->tick_cur = tick;
    hg->tick_next = tick + 1;
    kst_tick_stats *st = &K->st;
    memset(st, 0, sizeof *st);
    st->tick = tick;
    u64 t0 = kst_qpc();
    for (u32 i = 0; i < K->nreg; i++)
        if (K->reg[i].ww) st->ww_dirty += ww_collect(K->reg[i].base, region_span(&K->reg[i]), 1);
    u64 t1 = kst_qpc();
    for (u32 i = 0; i < K->nreg; i++)
        if (K->reg[i].ww) st->commit_changed += walk(K->reg[i].base, region_span(&K->reg[i]), 0);
    u64 t2 = kst_qpc();
    kst__emit_close(tick, KST_F_UNCAPTURED);
    u64 t3 = kst_qpc();
    u64 ring = st->us_ring, hostt = st->us_host;
    st->us_ring = kst_us(ring);
    st->us_host = kst_us(hostt);
    st->us_ww = kst_us(t1 - t0);
    st->us_walk = kst_us(t2 - t1);
    st->us_total = kst_us(t3 - t0 - hostt);
}

void kst_set_in_gate(int on) { K->in_gate = on; }

u64 kst_tick(void) { return K->hg->tick_cur; }

/* ---------------------------------------------------------------- audit (full sweep) */

int kst_audit(kst_audit_result *r) {
    memset(r, 0, sizeof *r);
    r->first_bad_vaddr = 0;
    u64 t0 = kst_qpc();
    for (u32 i = 0; i < K->nreg; i++) {
        region *g = &K->reg[i];
        u32 span = g->ww ? region_span(g) : g->size, a = g->base, end = g->base + span;
        while (a < end) {
            MEMORY_BASIC_INFORMATION mbi;
            if (!VirtualQuery((void *)a, &mbi, sizeof mbi)) kst_fatal("VirtualQuery failed");
            u32 re = (u32)mbi.BaseAddress + (u32)mbi.RegionSize;
            if (re > end) re = end;
            u16 pn = mbi.State == MEM_COMMIT ? (u16)mbi.Protect : 0;
            for (u32 v = a >> 12; v < re >> 12; v++) {
                pent *e = kst__pent(v);
                int bad = 0;
                if (!pn) {
                    if (e->present) { r->extra++; bad = 1; }
                } else {
                    r->pages++;
                    u8 key[16];
                    if (!kst__readable(pn)) { r->prot_mismatch++; bad = 1; }
                    else {
                        kst_page_key((void *)(v << 12), key);
                        if (!e->present) { r->missing++; bad = 1; }
                        else {
                            if (memcmp(key, e->key, 16)) { r->key_mismatch++; bad = 1; }
                            if (pn != e->prot) { r->prot_mismatch++; bad = 1; }
                        }
                    }
                }
                if (bad && !r->first_bad_vaddr) r->first_bad_vaddr = v << 12;
            }
            a = re;
        }
    }
    r->us = kst_us(kst_qpc() - t0);
    return !(r->extra | r->missing | r->key_mismatch | r->prot_mismatch);
}

/* ---------------------------------------------------------------- map access */

int kst_page_get(u32 vpn, u8 key[16], u16 *prot, u8 *mclass) {
    if (vpn < K->vpn_lo || vpn >= K->vpn_hi) return 0;
    pent *e = kst__pent(vpn);
    if (mclass) *mclass = e->mclass;
    if (!e->present) return 0;
    if (key) memcpy(key, e->key, 16);
    if (prot) *prot = e->prot;
    return 1;
}

void kst_vpn_span(u32 *lo, u32 *hi) { *lo = K->vpn_lo; *hi = K->vpn_hi; }

u32 kst_region_class(u32 vpn) {
    if (vpn < K->vpn_lo || vpn >= K->vpn_hi) return 0;
    return kst__pent(vpn)->mclass;
}

} // extern "C"
