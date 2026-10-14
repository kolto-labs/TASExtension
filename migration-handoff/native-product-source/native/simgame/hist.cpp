/* Reference page store for the standalone SimGame harness. It stands in for the
 * Python store: per-tick page-ref deltas in memory, page bytes content-addressed
 * in a session file, and restore sets W written as section 9 PAGE records.
 * Harness side only (runs at the gate on the harness stack). */
#include "../state/kst.h"

extern "C" {
#include "hist.hpp"

typedef struct href { u32 vaddr; u8 key[16]; u8 mclass, form; u16 prot; } href;

typedef struct hist {
    href *refs;
    u32 nrefs, cap, pending;
    u32 *toff, *tcnt;
    u32 maxticks;
    /* content store */
    HANDLE pages;
    u32 npages;
    u8 (*hkey)[16];
    u32 *hidx;
    u32 hmask;
    u32 *mapT;      /* scratch: latest ref index per vpn */
    u32 vlo, vhi;
    u64 bytes_written;
} hist;

KST_CORE static hist *S;

void hist_init(const WCHAR *pages_path, u32 maxticks) {
    S = static_cast<hist *>(kst_core_alloc(sizeof(hist)));
    S->cap = 1u << 21;
    S->refs = static_cast<href *>(kst_core_alloc(S->cap * sizeof(href)));
    S->maxticks = maxticks;
    S->toff = (u32 *)kst_core_alloc(maxticks * 4);
    S->tcnt = (u32 *)kst_core_alloc(maxticks * 4);
    S->hmask = (1u << 20) - 1;
    S->hkey = static_cast<u8 (*)[16]>(kst_core_alloc((S->hmask + 1) * 16));
    S->hidx = (u32 *)kst_core_alloc((S->hmask + 1) * 4);
    for (u32 i = 0; i <= S->hmask; i++) S->hidx[i] = 0xFFFFFFFFu;
    S->pages = CreateFileW(pages_path, GENERIC_READ | GENERIC_WRITE, FILE_SHARE_READ, 0, CREATE_ALWAYS,
                           FILE_ATTRIBUTE_NORMAL, 0);
    if (S->pages == INVALID_HANDLE_VALUE) kst_fatal("hist: cannot create the pages file");
    kst_vpn_span(&S->vlo, &S->vhi);
    S->mapT = (u32 *)kst_core_alloc((S->vhi - S->vlo) * 4);
}

static u32 slot_of(const u8 key[16]) {
    u32 h;
    memcpy(&h, key, 4);
    return h & S->hmask;
}

static u32 store_find(const u8 key[16]) {
    for (u32 i = slot_of(key);; i = (i + 1) & S->hmask) {
        if (S->hidx[i] == 0xFFFFFFFFu) return 0xFFFFFFFFu;
        if (!memcmp(S->hkey[i], key, 16)) return S->hidx[i];
    }
}

static void store_put(const u8 key[16], const void *data) {
    u32 i = slot_of(key);
    for (;; i = (i + 1) & S->hmask) {
        if (S->hidx[i] == 0xFFFFFFFFu) break;
        if (!memcmp(S->hkey[i], key, 16)) return;
    }
    if (S->npages * 2 > S->hmask) kst_fatal("hist: page index full");
    OVERLAPPED ov;
    memset(&ov, 0, sizeof ov);
    u64 off = (u64)S->npages * KST_PAGE;
    ov.Offset = (u32)off;
    ov.OffsetHigh = (u32)(off >> 32);
    DWORD w = 0;
    if (!WriteFile(S->pages, data, KST_PAGE, &w, &ov) || w != KST_PAGE) kst_fatal("hist: page write failed");
    memcpy(S->hkey[i], key, 16);
    S->hidx[i] = S->npages++;
    S->bytes_written += KST_PAGE;
}

static void store_get(const u8 key[16], void *out) {
    u32 idx = store_find(key);
    if (idx == 0xFFFFFFFFu) kst_fatal("hist: page missing from the store");
    OVERLAPPED ov;
    memset(&ov, 0, sizeof ov);
    u64 off = (u64)idx * KST_PAGE;
    ov.Offset = (u32)off;
    ov.OffsetHigh = (u32)(off >> 32);
    DWORD got = 0;
    if (!ReadFile(S->pages, out, KST_PAGE, &got, &ov) || got != KST_PAGE) kst_fatal("hist: page read failed");
}

void hist_page(u64 tick, u32 vaddr, u8 mclass, u16 prot, u16 form, const u8 key[16], const void *data) {
    (void)tick;
    form &= KST_F_DATA | KST_F_ZERO | KST_F_DECOMMIT;
    if (form == KST_F_DATA) store_put(key, data);
    if (S->nrefs >= S->cap) kst_fatal("hist: ref log full");
    href *r = &S->refs[S->nrefs++];
    r->vaddr = vaddr;
    memcpy(r->key, key, 16);
    r->mclass = mclass;
    r->form = (u8)form;
    r->prot = form == KST_F_DECOMMIT ? 0 : prot;
}

void hist_tick_end(u64 tick) {
    if (tick >= S->maxticks) kst_fatal("hist: tick beyond the table");
    S->toff[tick] = S->pending;
    S->tcnt[tick] = S->nrefs - S->pending;
    S->pending = S->nrefs;
}

void hist_truncate(u64 tick) {
    S->nrefs = S->toff[tick] + S->tcnt[tick];
    S->pending = S->nrefs;
}

u32 hist_npages(void) { return S->npages; }
u64 hist_bytes(void) { return S->bytes_written; }

/* W = { vpn : live map != map(T) }, written as PAGE records. Returns the count. */
u32 hist_write_w(u64 target, const WCHAR *path, u32 *data_pages) {
    u32 nv = S->vhi - S->vlo;
    for (u32 i = 0; i < nv; i++) S->mapT[i] = 0xFFFFFFFFu;
    for (u64 t = 0; t <= target; t++) {
        u32 a = S->toff[t], n = S->tcnt[t];
        for (u32 j = a; j < a + n; j++) S->mapT[(S->refs[j].vaddr >> 12) - S->vlo] = j;
    }
    /* pass 1: size */
    u32 nrec = 0, ndata = 0;
    u8 *buf = 0;
    for (int pass = 0; pass < 2; pass++) {
        u32 off = 0;
        if (pass == 1) {
            buf = (u8 *)VirtualAlloc(0, nrec * 40 + ndata * KST_PAGE + 8, MEM_RESERVE | MEM_COMMIT, PAGE_READWRITE);
            if (!buf) kst_fatal("hist: W buffer allocation failed");
        }
        for (u32 v = S->vlo; v < S->vhi; v++) {
            if (!kst_region_class(v)) continue;
            u8 lk[16], mc = 0;
            u16 lp = 0;
            int live = kst_page_get(v, lk, &lp, &mc);
            u32 r = S->mapT[v - S->vlo];
            const href *h = r == 0xFFFFFFFFu ? 0 : &S->refs[r];
            int tp = h && h->form != KST_F_DECOMMIT;
            if (!tp && !live) continue;
            if (tp && live && lp == h->prot && !memcmp(lk, h->key, 16)) continue;
            u16 form = !tp ? KST_F_DECOMMIT : h->form;
            if (pass == 0) {
                nrec++;
                if (form == KST_F_DATA) ndata++;
                continue;
            }
            u8 *p = buf + off;
            memset(p, 0, 40);
            p[0] = 'S';
            p[1] = 'R';
            p[2] = KST_REC_PAGE;
            p[3] = mc;
            memcpy(p + 4, &form, 2);
            u16 prot = tp ? h->prot : 0;
            memcpy(p + 6, &prot, 2);
            u32 va = v << 12, dlen = form == KST_F_DATA ? KST_PAGE : 0;
            memcpy(p + 8, &va, 4);
            memcpy(p + 12, &dlen, 4);
            memcpy(p + 16, &target, 8);
            if (tp) memcpy(p + 24, h->key, 16);
            off += 40;
            if (dlen) { store_get(h->key, buf + off); off += KST_PAGE; }
        }
        if (pass == 1) {
            if (!kst_write_file(path, buf, off)) kst_fatal("hist: cannot write the W file");
            VirtualFree(buf, 0, MEM_RELEASE);
        }
    }
    if (data_pages) *data_pages = ndata;
    return nrec;
}

} // extern "C"
