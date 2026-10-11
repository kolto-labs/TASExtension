/* harness_core: a private region reserved at boot (contract section 6).
 * The shim never allocates from the process heap or the game heap. */
#include "shim.hpp"

u8 *g_core_base;
u32 g_core_size;
static u32 g_core_used;
static u8 *g_exec_base;
static u32 g_exec_used;

int core_init(u32 bytes) {
    g_core_base = static_cast<u8 *>(VirtualAlloc(nullptr, bytes, MEM_RESERVE | MEM_COMMIT | MEM_TOP_DOWN, PAGE_READWRITE));
    if (!g_core_base) return 0;
    g_core_size = bytes;
    g_core_used = 0;
    g_exec_base = static_cast<u8 *>(VirtualAlloc(nullptr, 4096, MEM_RESERVE | MEM_COMMIT | MEM_TOP_DOWN,
                                                 PAGE_EXECUTE_READWRITE));
    g_exec_used = 0;
    return g_exec_base != nullptr;
}

void *core_alloc(u32 n) {
    n = (n + 15) & ~15u;
    if (g_core_used + n > g_core_size) return nullptr;
    void *p = g_core_base + g_core_used;
    g_core_used += n;
    return p;
}

void *core_exec_alloc(u32 n) {
    n = (n + 15) & ~15u;
    if (g_exec_used + n > 4096) return nullptr;
    void *p = g_exec_base + g_exec_used;
    g_exec_used += n;
    return p;
}
