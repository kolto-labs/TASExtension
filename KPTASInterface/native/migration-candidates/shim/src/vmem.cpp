/* Game memory into the arena (contract 3.4.2).
 *
 * HeapCreate from the exe (the static CRT's _heap_init) returns a
 * non-growable RtlCreateHeap placed inside the arena by the engine; HeapAlloc,
 * HeapFree, HeapReAlloc and HeapSize then run unchanged on that handle. The exe
 * imports neither GetProcessHeap nor GlobalAlloc/LocalAlloc (LocalFree only
 * frees FormatMessageA buffers).
 *
 * VirtualAlloc/VirtualFree from the exe (the CRT small-block heap and the
 * game's own buffers) go to the engine's first-fit placement allocator, whose
 * map sits at the arena base: after a restore the map is T's, so every later
 * allocation returns the recorded address. Commit and decommit are real. */
#include "shim.hpp"

u32 g_va_calls, g_va_reserved, g_heap_creates;
static LPVOID (WINAPI *r_VirtualAlloc)(LPVOID, SIZE_T, DWORD, DWORD);
static BOOL (WINAPI *r_VirtualFree)(LPVOID, SIZE_T, DWORD);
static SIZE_T (WINAPI *r_VirtualQuery)(LPCVOID, PMEMORY_BASIC_INFORMATION, SIZE_T);
static BOOL (WINAPI *r_VirtualProtect)(LPVOID, SIZE_T, DWORD, PDWORD);
static u32 g_logs, g_vq_logs;

#define HEAP_RESERVE (576u << 20)

struct vcall_t { u32 op; u32 p, size, prot; u32 ok; };

extern "C" void vcall_fn(void *a) {
    vcall_t *c = static_cast<vcall_t *>(a);
    void *address = reinterpret_cast<void *>(static_cast<uintptr_t>(c->p));
    if (c->op == 0) c->ok = VirtualAlloc(address, c->size, MEM_COMMIT, c->prot) == address;
    else c->ok = VirtualFree(address, c->size, MEM_DECOMMIT) != 0;
}

static int vop(u32 op, u32 p, u32 size, u32 prot) {
    vcall_t c = {op, p, size, prot, 0};
    kst_hcall(vcall_fn, &c);
    return (int)c.ok;
}

/* Large blocks. An arena heap is non-growable (a growable Wine heap puts
 * large blocks in NtAllocateVirtualMemory memory outside the arena), and a
 * non-growable heap refuses any block of HEAP_MIN_LARGE_BLOCK_SIZE (0x7f000)
 * or more, as Windows does. The exe's HeapAlloc/HeapReAlloc/HeapFree/HeapSize
 * on an arena heap therefore serve large requests from the placement
 * allocator: a 16-byte header {magic, size} at the start of their own 64 KiB
 * granule, the block right after it. */
#define LB_MIN 0x7e000u
#define LB_MAGIC 0x424C504Bu   /* "KPLB" */
static LPVOID (WINAPI *r_HeapAlloc)(HANDLE, DWORD, SIZE_T);
static LPVOID (WINAPI *r_HeapReAlloc)(HANDLE, DWORD, LPVOID, SIZE_T);
static BOOL (WINAPI *r_HeapFree)(HANDLE, DWORD, LPVOID);
static SIZE_T (WINAPI *r_HeapSize)(HANDLE, DWORD, LPCVOID);
static u32 g_arena_heaps[8];
static u32 g_n_arena_heaps;
u32 g_lb_live, g_lb_allocs;

static int arena_heap(HANDLE h) {
    for (u32 i = 0; i < g_n_arena_heaps; i++)
        if (g_arena_heaps[i] == shim_abi::address32(h)) return 1;
    return 0;
}

static int is_large(LPCVOID p) {
    u32 a = shim_abi::address32(p);
    if (!p || !kst_in_arena(p) || ((a - 16) & 0xffffu)) return 0;
    u32 g = (a - 16 - K->cfg.arena_base) >> 16;
    u32 e = K->ah->map[g];
    if (!e || e == 0xFFFFFFFFu) return 0;
    return *reinterpret_cast<const u32 *>(static_cast<uintptr_t>(a - 16)) == LB_MAGIC;
}

static void *lb_alloc(u32 n) {
    u32 total = n + 16;
    u32 p = shim_abi::address32(kst_vreserve(total));
    if (!vop(0, p, (total + 4095u) & ~4095u, PAGE_READWRITE)) {
        kst_vfree(reinterpret_cast<void *>(static_cast<uintptr_t>(p)));
        return nullptr;
    }
    *reinterpret_cast<u32 *>(static_cast<uintptr_t>(p)) = LB_MAGIC;
    *reinterpret_cast<u32 *>(static_cast<uintptr_t>(p + 4)) = n;
    g_lb_live++;
    g_lb_allocs++;
    return reinterpret_cast<void *>(static_cast<uintptr_t>(p + 16));
}

static void lb_free(LPVOID p) {
    u32 a = shim_abi::address32(p) - 16;
    *reinterpret_cast<u32 *>(static_cast<uintptr_t>(a)) = 0;
    kst_vfree(reinterpret_cast<void *>(static_cast<uintptr_t>(a)));
    g_lb_live--;
}

static u32 lb_size(LPCVOID p) { return *reinterpret_cast<const u32 *>(static_cast<const u8 *>(p) - 12); }

static LPVOID WINAPI hk_HeapAlloc(HANDLE h, DWORD flags, SIZE_T n) {
    if (n >= LB_MIN && arena_heap(h)) {
        void *p = lb_alloc((u32)n);
        if (!p) SetLastError(ERROR_NOT_ENOUGH_MEMORY);
        return p;
    }
    return r_HeapAlloc(h, flags, n);
}

static BOOL WINAPI hk_HeapFree(HANDLE h, DWORD flags, LPVOID p) {
    if (is_large(p) && arena_heap(h)) { lb_free(p); return TRUE; }
    return r_HeapFree(h, flags, p);
}

static SIZE_T WINAPI hk_HeapSize(HANDLE h, DWORD flags, LPCVOID p) {
    if (is_large(p) && arena_heap(h)) return lb_size(p);
    return r_HeapSize(h, flags, p);
}

static LPVOID WINAPI hk_HeapReAlloc(HANDLE h, DWORD flags, LPVOID p, SIZE_T n) {
    if (!arena_heap(h)) return r_HeapReAlloc(h, flags, p, n);
    int big_old = is_large(p), big_new = n >= LB_MIN;
    if (!big_old && !big_new) return r_HeapReAlloc(h, flags, p, n);
    if (flags & HEAP_REALLOC_IN_PLACE_ONLY) { SetLastError(ERROR_NOT_ENOUGH_MEMORY); return nullptr; }
    u32 old = big_old ? lb_size(p) : (u32)r_HeapSize(h, 0, p);
    void *q = big_new ? lb_alloc((u32)n) : r_HeapAlloc(h, flags & HEAP_ZERO_MEMORY, n);
    if (!q) { SetLastError(ERROR_NOT_ENOUGH_MEMORY); return nullptr; }
    memcpy(q, p, old < n ? old : (u32)n);
    if ((flags & HEAP_ZERO_MEMORY) && n > old && !big_new) memset(static_cast<u8 *>(q) + old, 0, (u32)n - old);
    if (big_old) lb_free(p);
    else r_HeapFree(h, 0, p);
    return q;
}

static HANDLE WINAPI hk_HeapCreate(DWORD flags, SIZE_T initial, SIZE_T max) {
    u32 reserve = max ? (u32)((max + 0xffffu) & ~0xffffu) : HEAP_RESERVE;
    if (reserve < (1u << 20)) reserve = 1u << 20;
    u32 commit = (u32)((initial + 0xffffu) & ~0xffffu);
    HANDLE h = kst_heap_create(reserve, commit, flags);
    g_heap_creates++;
    if (g_n_arena_heaps < 8) g_arena_heaps[g_n_arena_heaps++] = shim_abi::address32(h);
    slog("HeapCreate flags=%x initial=%x max=%x -> %x (reserve %u MB, lock at +%x, list at +%x) caller=%x",
         flags, (u32)initial, (u32)max, shim_abi::address32(h), reserve >> 20, K->heap_cs_off, K->heap_list_off,
         shim_abi::address32(__builtin_return_address(0)));
    return h;
}

static BOOL WINAPI hk_HeapDestroy(HANDLE h) {
    slog("HeapDestroy %x ignored (arena heaps live for the session)", shim_abi::address32(h));
    return TRUE;
}

static LPVOID WINAPI hk_VirtualAlloc(LPVOID a, SIZE_T sz, DWORD type, DWORD prot) {
    u32 ret = shim_abi::address32(__builtin_return_address(0));
    g_va_calls++;
    if (!a) {
        if (!(type & (MEM_RESERVE | MEM_COMMIT)) || (type & ~(MEM_RESERVE | MEM_COMMIT | MEM_TOP_DOWN))) {
            slog("VirtualAlloc(NULL, %x, %x, %x) passed through (caller %x)", (u32)sz, type, prot, ret);
            return r_VirtualAlloc(a, sz, type, prot);
        }
        u32 p = shim_abi::address32(kst_vreserve(static_cast<u32>(sz)));
        g_va_reserved += (u32)((sz + 0xffffu) & ~0xffffu);
        if (type & MEM_COMMIT) {
            u32 n = ((u32)sz + 4095u) & ~4095u;
            if (!vop(0, p, n, prot)) {
                slog("VirtualAlloc commit failed at %x+%x", p, n);
                kst_vfree(reinterpret_cast<void *>(static_cast<uintptr_t>(p)));
                SetLastError(ERROR_NOT_ENOUGH_MEMORY);
                return nullptr;
            }
        }
        if (g_logs < 300) {
            g_logs++;
            slog("VirtualAlloc(NULL, %x, %x, %x) -> %x caller=%x tick=%u", (u32)sz, type, prot, p, ret, (u32)g_gate.tick);
        }
        return reinterpret_cast<LPVOID>(static_cast<uintptr_t>(p));
    }
    if (kst_in_arena(a)) {
        u32 base = shim_abi::address32(a) & ~4095u;
        u32 end = (shim_abi::address32(a) + (u32)sz + 4095u) & ~4095u;
        if (type & MEM_COMMIT) {
            if (!vop(0, base, end - base, prot)) { SetLastError(ERROR_INVALID_ADDRESS); return nullptr; }
            return reinterpret_cast<LPVOID>(static_cast<uintptr_t>(base));
        }
        if (type == MEM_RESERVE) return a;
        slog("VirtualAlloc(%x, %x, %x, %x) in the arena passed through (caller %x)", shim_abi::address32(a), (u32)sz,
             type, prot, ret);
    }
    return r_VirtualAlloc(a, sz, type, prot);
}

static BOOL WINAPI hk_VirtualFree(LPVOID a, SIZE_T sz, DWORD type) {
    if (!kst_in_arena(a)) return r_VirtualFree(a, sz, type);
    if (type == MEM_RELEASE) {
        if (sz || !kst_vfree(a)) { SetLastError(ERROR_INVALID_PARAMETER); return FALSE; }
        return TRUE;
    }
    if (type == MEM_DECOMMIT) {
        u32 base = shim_abi::address32(a) & ~4095u;
        u32 end = (shim_abi::address32(a) + (u32)sz + 4095u) & ~4095u;
        if (!sz) { SetLastError(ERROR_INVALID_PARAMETER); return FALSE; }
        return vop(1, base, end - base, 0) ? TRUE : FALSE;
    }
    slog("VirtualFree(%x, %x, %x) in the arena passed through", shim_abi::address32(a), (u32)sz, type);
    return r_VirtualFree(a, sz, type);
}

static SIZE_T WINAPI hk_VirtualQuery(LPCVOID a, PMEMORY_BASIC_INFORMATION m, SIZE_T n) {
    SIZE_T r = r_VirtualQuery(a, m, n);
    if (kst_in_arena(a) && g_vq_logs < 50) {
        g_vq_logs++;
        slog("VirtualQuery(%x) in the arena from %x", shim_abi::address32(a), shim_abi::address32(__builtin_return_address(0)));
    }
    return r;
}

static BOOL WINAPI hk_VirtualProtect(LPVOID a, SIZE_T sz, DWORD prot, PDWORD old) {
    if (kst_in_arena(a) || (shim_abi::address32(a) >= EXE_DATA_VA && shim_abi::address32(a) < EXE_DATA_END))
        slog("VirtualProtect(%x, %x, %x) on sim memory from %x", shim_abi::address32(a), (u32)sz, prot,
             shim_abi::address32(__builtin_return_address(0)));
    return r_VirtualProtect(a, sz, prot, old);
}

#define HOOK(fn, h, save)                                                        \
    do {                                                                         \
        void *o = iat_hook(exe, "KERNEL32.dll", fn, shim_abi::erase_function(h));                \
        if (o) { shim_abi::save_function(save, o); hooks_note("exe:" fn); }        \
        else slog("iat miss exe %s", fn);                                        \
    } while (0)

void vmem_install(void) {
    HMODULE exe = GetModuleHandleA(nullptr);
    HOOK("HeapCreate", hk_HeapCreate, 0);
    HOOK("HeapDestroy", hk_HeapDestroy, 0);
    HOOK("HeapAlloc", hk_HeapAlloc, &r_HeapAlloc);
    HOOK("HeapReAlloc", hk_HeapReAlloc, &r_HeapReAlloc);
    HOOK("HeapFree", hk_HeapFree, &r_HeapFree);
    HOOK("HeapSize", hk_HeapSize, &r_HeapSize);
    HOOK("VirtualAlloc", hk_VirtualAlloc, &r_VirtualAlloc);
    HOOK("VirtualFree", hk_VirtualFree, &r_VirtualFree);
    HOOK("VirtualQuery", hk_VirtualQuery, &r_VirtualQuery);
    HOOK("VirtualProtect", hk_VirtualProtect, &r_VirtualProtect);
}
