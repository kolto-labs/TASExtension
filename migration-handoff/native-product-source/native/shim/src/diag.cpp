/* Diagnostics: call-site traces for chasing state outside the sim set.
 *
 * The wire's log verb turns traces on and off at run time, so the environment
 * block (which the CRT copies into the arena) stays pinned. Nothing here writes
 * game-visible memory; a trace line costs a WriteFile on the game's stack,
 * below its live frames, which the gate scrubs.
 *
 * A backtrace is a scan of the current stack for exe return addresses: a value
 * inside .text that follows a call instruction. Frame pointers are not needed,
 * and a stale return address left in a live frame can show up as an extra line. */
#include "shim.hpp"

// Retain the native C ABI, including CRT and assembly-referenced symbols.
extern "C" {

u32 g_trace, g_trace_site, g_trace_from, g_trace_to;
u32 g_trace_lines;

int trace_on(u32 bit) {
    if (!(g_trace & bit) || g_trace_lines >= 20000u) return 0;
    u32 t = static_cast<u32>(g_gate.tick);
    return t >= g_trace_from && t <= g_trace_to;
}

static int after_call(u32 v) {
    if (v < EXE_TEXT_VA + 8 || v >= EXE_TEXT_VA + EXE_TEXT_SIZE) return 0;
    const u8 *p = reinterpret_cast<const u8 *>(reinterpret_cast<uintptr_t>(v));
    if (p[-5] == 0xE8) return 1;                                 /* call rel32 */
    if (p[-2] == 0xFF && (p[-1] & 0x38) == 0x10) return 1;       /* call reg / [reg] */
    if (p[-3] == 0xFF && (p[-2] & 0x38) == 0x10) return 1;       /* call [reg+disp8] */
    if (p[-6] == 0xFF && (p[-5] & 0x38) == 0x10) return 1;       /* call [abs32] / [reg+disp32] */
    if (p[-7] == 0xFF && (p[-6] & 0x38) == 0x10) return 1;       /* call [sib+disp32] */
    return 0;
}

void diag_bt(const char *tag, u32 a, u32 b) {
    char line[900];
    buf_t o;
    b_init(&o, line, sizeof line);
    u32 sp = static_cast<u32>(reinterpret_cast<uintptr_t>(&o));
    u32 top = sp + 0x10000u;
    u32 k = K ? K->cur : 0;
    if (K && k < KST_MAX_SLOTS && K->hg->slot[k].stack_hi > sp && K->hg->slot[k].stack_lo < sp)
        top = K->hg->slot[k].stack_hi;
    else {
        MEMORY_BASIC_INFORMATION m;
        if (VirtualQuery((void *)reinterpret_cast<uintptr_t>(sp), &m, sizeof m)) top = static_cast<u32>(reinterpret_cast<uintptr_t>(m.BaseAddress)) + static_cast<u32>(m.RegionSize);
    }
    u32 n = 0;
    for (u32 p = (sp + 3u) & ~3u; p + 4 <= top && n < 24; p += 4) {
        u32 v = *reinterpret_cast<const u32 *>(reinterpret_cast<uintptr_t>(p));
        if (after_call(v)) {
            b_ch(&o, ' ');
            b_hexu32(&o, v);
            n++;
        }
    }
    b_term(&o);
    g_trace_lines++;
    slog("trace %s %x %x tick=%u slot=%u bt:%s", tag, a, b, static_cast<u32>(g_gate.tick), k, line);
}

} // extern "C"
