/* Sim-set layout: harness_game region, game arena with its placement allocator
 * (map inside the arena), heap-in-arena, image_data sections, live-map arrays. */
#include "kst_int.h"

/* The core struct sits in the image's excluded .kstcore section, at a
 * link-time address: engine code running on game stacks spills pointers into
 * it (kst_switch arguments, locals), and those must not depend on how much the
 * process allocated before. */
KST_CORE static kst_core k_core;
KST_CORE kst_core *K;

static u32 round_page(u32 n) { return (n + KST_PAGE - 1) & ~(KST_PAGE - 1); }

static void add_region(u32 base, u32 size, u32 mclass, u32 ww) {
    if (K->nreg >= 8) kst_fatal("too many sim regions");
    region *r = &K->reg[K->nreg++];
    r->base = base;
    r->size = size;
    r->mclass = mclass;
    r->ww = ww;
    r->shadow = 0;
}

/* The exe's .data and .bss. Every page is written once so that copy-on-write
 * pages report PAGE_READWRITE from tick 0 on. */
static void find_image_data(void) {
    u8 *img = (u8 *)GetModuleHandleW(0);
    IMAGE_DOS_HEADER *dos = (IMAGE_DOS_HEADER *)img;
    IMAGE_NT_HEADERS32 *nt = (IMAGE_NT_HEADERS32 *)(img + dos->e_lfanew);
    IMAGE_SECTION_HEADER *sec = IMAGE_FIRST_SECTION(nt);
    kst_buf b;
    char line[512];
    kb_init(&b, line, sizeof line);
    kb_str(&b, "kst: image sections:");
    for (u32 i = 0; i < nt->FileHeader.NumberOfSections; i++) {
        char name[9];
        memcpy(name, sec[i].Name, 8);
        name[8] = 0;
        kb_ch(&b, ' ');
        kb_str(&b, name);
        kb_ch(&b, '@');
        kb_hex32(&b, (u32)img + sec[i].VirtualAddress);
        kb_ch(&b, '+');
        kb_hex32(&b, sec[i].Misc.VirtualSize);
        if (!(sec[i].Characteristics & IMAGE_SCN_MEM_WRITE)) continue;
        int is_data = !memcmp(name, ".data", 6), is_bss = !memcmp(name, ".bss", 5);
        if (!is_data && !is_bss) continue;
        u32 base = (u32)img + sec[i].VirtualAddress;
        u32 size = round_page(sec[i].Misc.VirtualSize);
        if (!size) continue;
        for (u32 off = 0; off < size; off += KST_PAGE) {
            volatile u8 *p = (volatile u8 *)(base + off);
            *p = *p;
        }
        add_region(base, size, KST_IMAGE_DATA, 0);
        kb_str(&b, "[sim]");
    }
    kb_ch(&b, '\n');
    kst_out(b.p, b.n);
}

void kst__arena_init(void) {
    const kst_config *c = &K->cfg;
    /* harness_game */
    void *hg = VirtualAlloc((void *)c->hg_base, c->hg_size, MEM_RESERVE | MEM_WRITE_WATCH, PAGE_READWRITE);
    if (hg != (void *)c->hg_base) kst_fatal("harness_game fixed-base reservation failed");
    if (!VirtualAlloc(hg, round_page(sizeof(kst_hg)), MEM_COMMIT, PAGE_READWRITE))
        kst_fatal("harness_game commit failed");
    K->hg = (kst_hg *)hg;
    K->hg->magic = KST_HG_MAGIC;
    K->hg->version = 1;
    /* arena */
    void *a = VirtualAlloc((void *)c->arena_base, c->arena_size, MEM_RESERVE | MEM_WRITE_WATCH, PAGE_READWRITE);
    if (a != (void *)c->arena_base) kst_fatal("arena fixed-base reservation failed");
    u32 ngran = c->arena_size / KST_GRAN;
    u32 hdr_bytes = round_page(16 + ngran * 4);
    u32 hdr_gran = (hdr_bytes + KST_GRAN - 1) / KST_GRAN;
    if (!VirtualAlloc(a, hdr_bytes, MEM_COMMIT, PAGE_READWRITE)) kst_fatal("arena map commit failed");
    arena_hdr *ah = (arena_hdr *)a;
    ah->magic = KST_ARENA_MAGIC;
    ah->ngran = ngran;
    ah->map[0] = hdr_gran;
    for (u32 g = 1; g < hdr_gran; g++) ah->map[g] = KST_CONT;
    ah->hw_gran = hdr_gran;
    ah->nalloc = 1;
    K->ah = ah;

    find_image_data();
    add_region(c->hg_base, c->hg_size, KST_HARNESS_GAME, 1);
    add_region(c->arena_base, c->arena_size, KST_GAME_ARENA, 1);
    /* sort regions by base (insertion sort, tiny) */
    for (u32 i = 1; i < K->nreg; i++)
        for (u32 j = i; j && K->reg[j - 1].base > K->reg[j].base; j--) {
            region t = K->reg[j];
            K->reg[j] = K->reg[j - 1];
            K->reg[j - 1] = t;
        }
    for (u32 i = 1; i < K->nreg; i++)
        if (K->reg[i - 1].base + K->reg[i - 1].size > K->reg[i].base) kst_fatal("sim regions overlap");

    K->vpn_lo = K->reg[0].base >> 12;
    K->vpn_hi = (K->reg[K->nreg - 1].base + K->reg[K->nreg - 1].size) >> 12;
    u32 nvpn = K->vpn_hi - K->vpn_lo;
    K->map = (pent *)kst_core_alloc(nvpn * sizeof(pent));
    K->prot_now = (u16 *)kst_core_alloc(nvpn * 2);
    K->mark = (u32 *)kst_core_alloc(((nvpn + 31) / 32) * 4);
    K->mark_lo = KST_NONE;
    K->mark_hi = 0;
    for (u32 i = 0; i < K->nreg; i++) {
        region *r = &K->reg[i];
        for (u32 v = r->base >> 12; v < (r->base + r->size) >> 12; v++) kst__pent(v)->mclass = (u8)r->mclass;
        if (!r->ww) {
            r->shadow = (u8 *)kst_core_alloc(r->size);
            memcpy(r->shadow, (void *)r->base, r->size);
        }
    }
    K->leaf_lo = K->vpn_lo >> 10;
    K->nleaf = ((K->vpn_hi - 1) >> 10) - K->leaf_lo + 1;
    K->leaf_hash = (u8 (*)[32])kst_core_alloc(K->nleaf * 32);
    K->leaf_dirty = (u8 *)kst_core_alloc(K->nleaf);
    K->leaf_count = (u16 *)kst_core_alloc(K->nleaf * 2);
    K->wwcap = 65536;
    K->wwbuf = (void **)kst_core_alloc(K->wwcap * sizeof(void *));
    K->arena_span = hdr_gran * KST_GRAN;
    K->RtlCreateHeap = (PVOID(WINAPI *)(ULONG, PVOID, SIZE_T, SIZE_T, PVOID, PVOID))
        GetProcAddress(GetModuleHandleW(L"ntdll.dll"), "RtlCreateHeap");
}

/* Split init for hosts that must reserve the sim set early (a proxy DLL's
 * DllMain) and start threads and the ring later (outside the loader lock). */
int kst_init_mem(const kst_config *cfg, const kst_host *host) {
    K = &k_core;
    K->cfg = *cfg;
    if (host) K->host = *host;
    if (!K->cfg.arena_base) K->cfg.arena_base = 0x10000000u;
    if (!K->cfg.arena_size) K->cfg.arena_size = 1024u << 20;
    if (!K->cfg.hg_base) K->cfg.hg_base = 0x0E000000u;
    if (!K->cfg.hg_size) K->cfg.hg_size = 1u << 20;
    if (!K->cfg.ring_mb) K->cfg.ring_mb = 64;
    if (!K->cfg.nslots) K->cfg.nslots = 4;
    if (K->cfg.nslots > KST_MAX_SLOTS) K->cfg.nslots = KST_MAX_SLOTS;
    if (!K->cfg.main_stack) K->cfg.main_stack = 1u << 20;
    K->jcap = 256 * 1024;
    K->jbuf = (u8 *)kst_core_alloc(K->jcap);
    kst__arena_init();
    kst_b2s_init(&K->batch, 16);
    return 1;
}

int kst_init_threads(void) {
    kst__threads_init();
    return 1;
}

int kst_ring_attach(const WCHAR *path, u32 mb) {
    if (K->ring_base) return 1;
    K->cfg.ring_path = path;
    if (mb) K->cfg.ring_mb = mb;
    kst__ring_open();
    return K->ring_base != 0;
}

int kst_init(const kst_config *cfg, const kst_host *host) {
    kst_init_mem(cfg, host);
    kst__ring_open();
    kst__threads_init();
    return 1;
}

/* ---------------------------------------------------------------- placement allocator */

void *kst_vreserve(u32 size) {
    arena_hdr *ah = K->ah;
    u32 ng = (size + KST_GRAN - 1) / KST_GRAN;
    if (!ng) ng = 1;
    u32 g = 0;
    while (g + ng <= ah->ngran) {
        u32 e = ah->map[g];
        if (e) { g += e; continue; }
        u32 run = 1;
        while (run < ng && !ah->map[g + run]) run++;
        if (run == ng) {
            ah->map[g] = ng;
            for (u32 i = 1; i < ng; i++) ah->map[g + i] = KST_CONT;
            if (g + ng > ah->hw_gran) ah->hw_gran = g + ng;
            ah->nalloc++;
            u32 span = ah->hw_gran * KST_GRAN;
            if (span > K->arena_span) K->arena_span = span;
            return (void *)(K->cfg.arena_base + g * KST_GRAN);
        }
        g += run;
    }
    kst_fatal("arena exhausted");
    return 0;
}

typedef struct vcall { u32 op; void *p; u32 size; u32 ok; } vcall;

static void vcall_fn(void *a) {
    vcall *c = (vcall *)a;
    if (c->op == 0) c->ok = VirtualAlloc(c->p, c->size, MEM_COMMIT, PAGE_READWRITE) == c->p;
    else c->ok = VirtualFree(c->p, c->size, MEM_DECOMMIT) != 0;
}

static int vop(u32 op, void *p, u32 size) {
    vcall c = {op, p, size, 0};
    kst_hcall(vcall_fn, &c);
    return (int)c.ok;
}

int kst_vcommit(void *p, u32 size) { return vop(0, p, round_page(size)); }

int kst_vdecommit(void *p, u32 size) { return vop(1, p, round_page(size)); }

void *kst_valloc(u32 size) {
    void *p = kst_vreserve(size);
    if (!kst_vcommit(p, size)) kst_fatal("arena commit failed");
    return p;
}

int kst_vfree(void *p) {
    arena_hdr *ah = K->ah;
    u32 off = (u32)p - K->cfg.arena_base;
    if (off % KST_GRAN || off >= K->cfg.arena_size) return 0;
    u32 g = off / KST_GRAN, n = ah->map[g];
    if (!n || n == KST_CONT) return 0;
    vop(1, p, n * KST_GRAN);
    for (u32 i = 0; i < n; i++) ah->map[g + i] = 0;
    ah->nalloc--;
    if (g + n == ah->hw_gran) {
        u32 h = g;
        while (h && !ah->map[h - 1]) h--;
        ah->hw_gran = h;
    }
    return 1;
}

int kst_in_arena(const void *p) {
    return (u32)p - K->cfg.arena_base < K->cfg.arena_size;
}

/* Heap in the arena: a non-growable RtlCreateHeap over a reserved range whose
 * header is committed (M0: a reserved-only base faults). Runs on the harness
 * stack. Wine allocates the heap lock's DebugInfo from the process heap, a
 * pointer from sim memory into ambient memory whose value depends on everything
 * the process allocated before. The lock is found by the DebugInfo back-pointer
 * (RTL_CRITICAL_SECTION_DEBUG.CriticalSection == &lock) and switched to the
 * no-debug-info marker, as the contract does for the exe's own critical sections. */
typedef struct hcreate { u32 flags, reserve, commit; void *base; PVOID heap; u32 cs_off, list_off; } hcreate;

static void heap_create_fn(void *a) {
    hcreate *c = (hcreate *)a;
    c->heap = K->RtlCreateHeap(c->flags & ~HEAP_GROWABLE, c->base, c->reserve, c->commit, 0, 0);
    c->cs_off = KST_NONE;
    if (c->heap != c->base) return;
    u8 *hb = (u8 *)c->base;
    for (u32 o = 0; o + 24 <= 0x800; o += 4) {
        u32 *cs = (u32 *)(hb + o), p = cs[0];
        /* a free RTL_CRITICAL_SECTION: LockCount -1, RecursionCount 0, OwningThread 0 */
        if (cs[1] != 0xFFFFFFFFu || cs[2] != 0 || cs[3] != 0) continue;
        if (p < 0x10000 || p == 0xFFFFFFFFu || kst_in_arena((void *)p) || (p & 0xFFF) > 0xFF0) continue;
        MEMORY_BASIC_INFORMATION mbi;
        if (!VirtualQuery((void *)p, &mbi, sizeof mbi) || mbi.State != MEM_COMMIT) continue;
        if (!kst__readable((u16)mbi.Protect)) continue;
        /* RTL_CRITICAL_SECTION_DEBUG: Type (0) ... CriticalSection back-pointer */
        if (*(u16 *)p != 0 || *(u32 *)(p + 4) != (u32)cs) continue;
        *(u32 *)(hb + o) = 0xFFFFFFFFu;   /* RTL_CRITICAL_SECTION_NO_DEBUG_INFO marker */
        c->cs_off = o;
        break;
    }
    /* Unlink the heap from Wine's process heap list: a LIST_ENTRY in the header
     * whose neighbours live outside the arena. Otherwise a heap Wine creates later
     * writes our header (ambient into sim), and a restore would undo that write. */
    c->list_off = KST_NONE;
    HeapLock(GetProcessHeap());
    for (u32 o = 0; o + 8 <= 0x800; o += 4) {
        u32 n = *(u32 *)(hb + o), pv = *(u32 *)(hb + o + 4), self = (u32)(hb + o);
        if (n < 0x10000 || pv < 0x10000 || kst_in_arena((void *)n) || kst_in_arena((void *)pv)) continue;
        if ((n & 3) || (pv & 3) || (n & 0xFFF) > 0xFF8 || (pv & 0xFFF) > 0xFF8) continue;
        MEMORY_BASIC_INFORMATION m1, m2;
        if (!VirtualQuery((void *)n, &m1, sizeof m1) || m1.State != MEM_COMMIT || !kst__readable((u16)m1.Protect)) continue;
        if (!VirtualQuery((void *)pv, &m2, sizeof m2) || m2.State != MEM_COMMIT || !kst__readable((u16)m2.Protect)) continue;
        if (*(u32 *)(n + 4) != self || *(u32 *)pv != self) continue;
        *(u32 *)pv = n;          /* prev->next = next */
        *(u32 *)(n + 4) = pv;    /* next->prev = prev */
        *(u32 *)(hb + o) = self; /* self loop: list_remove stays harmless */
        *(u32 *)(hb + o + 4) = self;
        c->list_off = o;
        break;
    }
    HeapUnlock(GetProcessHeap());
}

HANDLE kst_heap_create(u32 reserve, u32 commit, u32 flags) {
    if (!K->RtlCreateHeap) kst_fatal("RtlCreateHeap unavailable");
    if (commit < KST_GRAN) commit = KST_GRAN;
    hcreate c = {flags, reserve, commit, kst_vreserve(reserve), 0, 0, 0};
    if (!kst_vcommit(c.base, commit)) kst_fatal("heap header commit failed");
    kst_hcall(heap_create_fn, &c);
    if (c.heap != c.base) kst_fatal("RtlCreateHeap did not use the arena base");
    if (c.cs_off == KST_NONE) kst_fatal("heap lock DebugInfo not found");
    if (c.list_off == KST_NONE) kst_fatal("heap list entry not found");
    K->heap_cs_off = c.cs_off;
    K->heap_list_off = c.list_off;
    return (HANDLE)c.heap;
}
