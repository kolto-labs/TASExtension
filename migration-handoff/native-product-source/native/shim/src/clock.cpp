/* Virtual clock (contract D5, section 5.5) and pinned host facts (section 7 item 4).
 *
 * V(n) = V(s) + floor((n - s) * 1e6 / hz) us; V is frozen within a tick.
 * U = uptime0_ms * 1000 + V (+ budget-guard micro-advance).
 * QPC = floor(U * qpf / 1e6), QPF = qpf, GetTickCount = floor(U / 1000) mod 2^32,
 * GetSystemTimeAsFileTime = initial_ft + 10 * V.
 * Live from DllMain: the CRT cookie and the startup srand seed read it. */
#include "shim.hpp"

/* Fixed C hook ABI and assembly-referenced symbol names. */
extern "C" {

/* g_clk is SH->clk (harness_game): a restore rewinds the clock with memory,
 * including last_u, the monotonic clamp. */
u32 g_guard_site_reads = 100000, g_guard_step_us = 1;

/* 2004-02-12T00:00:00Z as FILETIME */
#define DEFAULT_FT 127210176000000000ull

static BOOL (WINAPI *r_QPC)(LARGE_INTEGER *);
static BOOL (WINAPI *r_QPF)(LARGE_INTEGER *);
static DWORD (WINAPI *r_GTC)(void);
static void (WINAPI *r_GSTAFT)(LPFILETIME);
static void (WINAPI *r_Sleep)(DWORD);
static FARPROC (WINAPI *r_GetProcAddress)(HMODULE, LPCSTR);

void clock_defaults(void) {
    memset(&g_clk, 0, sizeof g_clk);
    g_clk.mode = 0;
    g_clk.hz = 60;
    g_clk.qpf = 10000000ull;
    g_clk.uptime0_ms = 3600000ull;
}

int clock_parse_env(const char *s) {
    g_clk.initial_ft = DEFAULT_FT;
    if (!s) return 1;
    while (*s) {
        const char *k = s;
        while (*s && *s != '=' && *s != ';') s++;
        u32 kl = (u32)(s - k);
        if (*s != '=') { if (*s) s++; continue; }
        s++;
        const char *v = s;
        while (*s && *s != ';') s++;
        const char *value_end = nullptr;
        u64 num = k_parse_u64(v, &value_end);
        if (kl == 4 && k_strneq(k, "mode", 4)) g_clk.mode = k_strneq(v, "wall", 4) ? 1 : 0;
        else if (kl == 2 && k_strneq(k, "hz", 2)) {
            if (value_end != s || num < 1 || num > 1000) {
                /* Match the live clock command's range; truncating a u64 here can install hz=0. */
                g_clk.hz = 60;
                slog("clock: invalid KPTAS_CLOCK hz; using 60");
            } else {
                g_clk.hz = (u32)num;
            }
        } else if (kl == 6 && k_strneq(k, "qpf_hz", 6)) g_clk.qpf = num ? num : 10000000ull;
        else if (kl == 10 && k_strneq(k, "uptime0_ms", 10)) g_clk.uptime0_ms = num;
        else if (kl == 10 && k_strneq(k, "initial_ft", 10)) g_clk.initial_ft = num;
        if (*s) s++;
    }
    return 1;
}

void clock_set_tick(u64 tick) {
    g_clk.v = g_clk.seg_v + (tick - g_clk.seg_tick) * 1000000ull / g_clk.hz;
    g_clk.extra = 0;
}

u64 clock_u(void) {
    u64 u = g_clk.uptime0_ms * 1000ull + g_clk.v + g_clk.extra;
    if (u < g_clk.last_u) u = g_clk.last_u;
    g_clk.last_u = u;
    return u;
}

/* ---- budget guard: per call site read counts within one tick ---- */
#define GSLOTS 1024
static u32 g_gsite[GSLOTS], g_gcount[GSLOTS], g_gstamp[GSLOTS];
static u32 g_gstamp_now = 1;

void clock_tick_reset(void) {
    g_gstamp_now++;
    g_guard_trips = 0;
    g_guard_first_site = 0;
}

static void guard_read(u32 site) {
    if (!g_gate.started) return;
    u32 h = (site * 2654435761u) >> 22;
    for (u32 i = 0; i < GSLOTS; i++) {
        u32 k = (h + i) & (GSLOTS - 1);
        if (g_gstamp[k] != g_gstamp_now) {
            g_gstamp[k] = g_gstamp_now;
            g_gsite[k] = site;
            g_gcount[k] = 1;
            return;
        }
        if (g_gsite[k] == site) {
            if (++g_gcount[k] > g_guard_site_reads) {
                g_clk.extra += g_guard_step_us;
                g_guard_trips++;
                if (!g_guard_first_site) g_guard_first_site = site;
            }
            return;
        }
    }
}

#define RETADDR() (btrace_caller((u32)(uintptr_t)__builtin_return_address(0)))

/* ---- exe hooks (count toward the budget guard) ---- */
static BOOL WINAPI hk_QPC(LARGE_INTEGER *p) {
    guard_read(RETADDR());
    u64 u = clock_u();
    p->QuadPart = (LONGLONG)(u * g_clk.qpf / 1000000ull);
    return TRUE;
}
static BOOL WINAPI hk_QPF(LARGE_INTEGER *p) {
    p->QuadPart = (LONGLONG)g_clk.qpf;
    return TRUE;
}
static DWORD WINAPI hk_GTC(void) {
    guard_read(RETADDR());
    return (DWORD)(clock_u() / 1000ull);
}
static void WINAPI hk_GSTAFT(LPFILETIME ft) {
    guard_read(RETADDR());
    clock_u();
    u64 v = g_clk.initial_ft + 10ull * (g_clk.v + g_clk.extra);
    ft->dwLowDateTime = (DWORD)v;
    ft->dwHighDateTime = (DWORD)(v >> 32);
}
static void WINAPI hk_GetLocalTime(LPSYSTEMTIME st) {
    FILETIME ft;
    u64 v = g_clk.initial_ft + 10ull * (g_clk.v + g_clk.extra);
    ft.dwLowDateTime = (DWORD)v;
    ft.dwHighDateTime = (DWORD)(v >> 32);
    FileTimeToSystemTime(&ft, st);  /* the boundary's time zone is UTC (bound.c) */
}
/* Sleep yields real time without advancing virtual time. */
static void WINAPI hk_Sleep(DWORD ms) { r_Sleep(ms > 1 ? 1 : ms); }
static DWORD WINAPI hk_GetCurrentProcessId(void) { return 0x00000f00u; }

/* GetHighResolutionTimer replacement: microseconds, exact. thiscall, no stack args. */
u64 __attribute__((thiscall)) hk_ghrt(void *self) {
    (void)self;
    guard_read(RETADDR());
    return clock_u();
}

/* ---- pinned host facts ---- */
static DWORD legacy_mb(u32 mb) {
    u64 bytes = (u64)mb << 20;
    return bytes > 0xffffffffull ? 0xffffffffu : (DWORD)bytes;
}
static void WINAPI hk_GlobalMemoryStatus(LPMEMORYSTATUS m) {
    if (g_bnd_native) { GlobalMemoryStatus(m); return; }
    m->dwLength = sizeof(MEMORYSTATUS);
    m->dwMemoryLoad = g_bnd_total_mb ? 100u - (u32)((u64)g_bnd_avail_mb * 100 / g_bnd_total_mb) : 0;
    m->dwTotalPhys = legacy_mb(g_bnd_total_mb);
    m->dwAvailPhys = legacy_mb(g_bnd_avail_mb);
    m->dwTotalPageFile = 0x7fffffffu;
    m->dwAvailPageFile = 0x7fffffffu;
    m->dwTotalVirtual = 0x7ffe0000u;
    m->dwAvailVirtual = 0x70000000u;
}
static BOOL WINAPI hk_GetDiskFreeSpaceExA(LPCSTR dir, PULARGE_INTEGER avail, PULARGE_INTEGER total,
                                          PULARGE_INTEGER totfree) {
    if (g_bnd_native) return GetDiskFreeSpaceExA(dir, avail, total, totfree);
    if (avail) avail->QuadPart = (u64)g_bnd_disk_mb << 20;
    if (total) total->QuadPart = (u64)(g_bnd_disk_mb > 102400u ? g_bnd_disk_mb : 102400u) << 20;
    if (totfree) totfree->QuadPart = (u64)g_bnd_disk_mb << 20;
    return TRUE;
}

/* ---- module (mss32, binkw32) hooks: virtual time, not counted ---- */
static BOOL WINAPI hkm_QPC(LARGE_INTEGER *p) {
    p->QuadPart = (LONGLONG)(clock_u() * g_clk.qpf / 1000000ull);
    return TRUE;
}
static DWORD WINAPI hkm_GTC(void) { return (DWORD)(clock_u() / 1000ull); }

/* GetProcAddress-resolved time and host APIs go through the same hooks. */
static FARPROC WINAPI hk_GetProcAddress(HMODULE m, LPCSTR name) {
    if(m && m==GetModuleHandleA("opengl32.dll")) {
        FARPROC real=r_GetProcAddress(m,name);
        DWORD error=GetLastError();
        FARPROC result=(FARPROC)gl_export_proc(name,(void *)real);
        SetLastError(error);return result;
    }
    if ((uintptr_t)name > 0xffff) {
        FARPROC g = wh_proc(name);
        if (g) return g;
        g = geo_proc(name);
        if (g) return g;
        if ((g = bound_proc(name))) return g;
        if (k_streq(name, "InitializeCriticalSectionAndSpinCount")) return (FARPROC)threads_init_cs_spin;
        if (k_streq(name, "GetDiskFreeSpaceExA")) return (FARPROC)hk_GetDiskFreeSpaceExA;
        if (k_streq(name, "QueryPerformanceCounter")) return (FARPROC)hk_QPC;
        if (k_streq(name, "QueryPerformanceFrequency")) return (FARPROC)hk_QPF;
        if (k_streq(name, "GetTickCount")) return (FARPROC)hk_GTC;
        if (k_streq(name, "timeGetTime")) return (FARPROC)hk_GTC;
        if (k_streq(name, "GetSystemTimeAsFileTime")) return (FARPROC)hk_GSTAFT;
        if (k_streq(name, "GlobalMemoryStatus")) return (FARPROC)hk_GlobalMemoryStatus;
    }
    return r_GetProcAddress(m, name);
}

#define HOOK(dll, fn, h, save)                                               \
    do {                                                                     \
        void *o = iat_hook(exe, dll, fn, (void *)(h));                       \
        if (o) { if (save) *(void **)(save) = o; hooks_note("exe:" fn); }    \
        else slog("iat miss exe %s!%s", dll, fn);                            \
    } while (0)

void clock_install_exe(void) {
    HMODULE exe = GetModuleHandleA(NULL);
    r_Sleep = (void (WINAPI *)(DWORD))GetProcAddress(GetModuleHandleA("kernel32.dll"), "Sleep");
    if (g_clk.mode == 0) {
        HOOK("KERNEL32.dll", "QueryPerformanceCounter", hk_QPC, &r_QPC);
        HOOK("KERNEL32.dll", "QueryPerformanceFrequency", hk_QPF, &r_QPF);
        HOOK("KERNEL32.dll", "GetTickCount", hk_GTC, &r_GTC);
        HOOK("KERNEL32.dll", "GetSystemTimeAsFileTime", hk_GSTAFT, &r_GSTAFT);
        HOOK("KERNEL32.dll", "GetLocalTime", hk_GetLocalTime, 0);
        HOOK("KERNEL32.dll", "Sleep", hk_Sleep, 0);
    }
    HOOK("KERNEL32.dll", "GetCurrentProcessId", hk_GetCurrentProcessId, 0);
    HOOK("KERNEL32.dll", "GlobalMemoryStatus", hk_GlobalMemoryStatus, 0);
    HOOK("KERNEL32.dll", "GetDiskFreeSpaceExA", hk_GetDiskFreeSpaceExA, 0);
    HOOK("KERNEL32.dll", "GetProcAddress", hk_GetProcAddress, &r_GetProcAddress);
}

/* ---- save length ----
 * The game writes a save in one of two places, both on the main thread and
 * both blocking:
 * - StallEventSaveGame (0x4b3110, thiscall) writes the player's saves and the
 *   script autosave. CServerExoAppInternal::SaveGame (0x4b58a0) arms it: it
 *   creates the slot directory and takes the screenshot, and the next server
 *   MainLoop pass takes its stall-event branch (0x4bafb1), which skips
 *   SetSnapshotTime and calls StallEventSaveGame. The player's saves reach
 *   SaveGame through CServerExoApp::SaveGame (0x4ae6e0, from the client's
 *   module message); DoSinglePlayerAutoSave reaches it from the MainLoop
 *   (0x4bae4c, "AUTOSAVE" in slot 1). The SaveGame hook only notes who asked
 *   and for which slot.
 * - DoPCAutosave (0x4b8300, thiscall) writes the area-transition autosave
 *   ("AUTOSAVE", slot 1) itself. StartNewModule (0x4bab7b) calls it when
 *   modulesave.2da says the new area autosaves; it never reaches SaveGame.
 * The clock moves once per save, when the writer returns: V moves on by the
 * save length (SH->save_ms, or the one-shot SH->save_next_ms), so the next
 * tick's frame delta carries the whole save, screenshot included, as on a
 * real machine. The real duration of the save never reaches game time. */
void *t_save, *t_savegame, *t_pcauto;
u32 g_save_ret, g_save_depth, g_savegame_ret, g_pcauto_ret;
static u32 g_save_us, g_save_aux;

void savegame_enter(u32 ret, u32 slot) {
    SH->save_kind = ret == RET_AUTOSAVE ? SAVE_KIND_AUTO : SAVE_KIND_MANUAL;
    SH->save_slot = slot;
    slog("save: SaveGame slot %u from %x at tick %u", slot, ret, (u32)g_gate.tick);
}

/* SaveGame returns 0 when it arms nothing (no room on the disk) */
void savegame_leave(u32 armed) {
    if (!armed) {
        slog("save: SaveGame armed no save at tick %u", (u32)g_gate.tick);
        SH->save_kind = SAVE_KIND_NONE;
    }
}

static void save_open(u32 aux) {
    if (g_save_depth++) { slog("save: nested save writer at tick %u", (u32)g_gate.tick); return; }
    u32 ms = SH->save_next_set ? SH->save_next_ms : SH->save_ms;
    SH->save_next_set = 0;
    g_save_us = ms * 1000u;
    g_save_aux = aux;
    ev_add_aux(EV_SAVE_BEGIN, g_save_us, g_save_aux);
}

/* StallEventSaveGame: the save SaveGame armed */
void save_begin(void) {
    u32 aux = SAVE_AUX(SH->save_kind, SH->save_slot & 0xffffffu);
    SH->save_kind = SAVE_KIND_NONE;
    save_open(aux);
}

/* DoPCAutosave: the area-transition autosave, always slot 1 */
void pcauto_begin(void) { save_open(SAVE_AUX(SAVE_KIND_AUTO, 1u)); }

void save_done(void) {
    if (--g_save_depth) return;
    if (g_save_us) {
        g_clk.v += g_save_us;
        g_clk.seg_v = g_clk.v;
        g_clk.seg_tick = g_gate.tick;
    }
    ev_add_aux(EV_SAVE_DONE, g_save_us, g_save_aux);
    msg_save_end();
    slog("save: tick %u advanced V by %u us", (u32)g_gate.tick, g_save_us);
}

/* ecx (this) survives the entry call; eax (result) survives the exit call. */
__attribute__((naked, used)) void st_save(void) {
    __asm__ volatile("pushal\n\t"
                     "call _save_begin\n\t"
                     "popal\n\t"
                     "movl (%esp), %eax\n\t"
                     "movl %eax, _g_save_ret\n\t"
                     "movl $1f, (%esp)\n\t"
                     "jmp *_t_save\n"
                     "1:\n\t"
                     "pushal\n\t"
                     "call _save_done\n\t"
                     "popal\n\t"
                     "jmp *_g_save_ret\n\t");
}

/* DoPCAutosave(module, modulesave) pops its two arguments itself. */
__attribute__((naked, used)) void st_pcauto(void) {
    __asm__ volatile("pushal\n\t"
                     "call _pcauto_begin\n\t"
                     "popal\n\t"
                     "movl (%esp), %eax\n\t"
                     "movl %eax, _g_pcauto_ret\n\t"
                     "movl $1f, (%esp)\n\t"
                     "jmp *_t_pcauto\n"
                     "1:\n\t"
                     "pushal\n\t"
                     "call _save_done\n\t"
                     "popal\n\t"
                     "jmp *_g_pcauto_ret\n\t");
}

/* SaveGame(slot, name, player, flag): the caller's return address and the
 * slot go to savegame_enter; the callee pops its four arguments itself, so
 * the swapped return lands with the caller's stack as it expects. */
__attribute__((naked, used)) void st_savegame(void) {
    __asm__ volatile("pushal\n\t"
                     "pushl 36(%esp)\n\t"
                     "pushl 36(%esp)\n\t"
                     "call _savegame_enter\n\t"
                     "addl $8, %esp\n\t"
                     "popal\n\t"
                     "movl (%esp), %eax\n\t"
                     "movl %eax, _g_savegame_ret\n\t"
                     "movl $1f, (%esp)\n\t"
                     "jmp *_t_savegame\n"
                     "1:\n\t"
                     "pushal\n\t"
                     "pushl %eax\n\t"
                     "call _savegame_leave\n\t"
                     "addl $4, %esp\n\t"
                     "popal\n\t"
                     "jmp *_g_savegame_ret\n\t");
}

void save_install(void) {
    static const u8 e_save[6] = {0x55, 0x8b, 0xec, 0x83, 0xe4, 0xf8};
    static const u8 l_save[] = {1, 2, 3, 0};
    if (g_clk.mode != 0) return;
    if (detour(ADDR_STALL_SAVE, e_save, 6, l_save, (void *)st_save, &t_save)) hooks_note("exe:StallEventSaveGame");
    else slog("detour StallEventSaveGame failed");
    if (detour(ADDR_SAVE_GAME, e_save, 6, l_save, (void *)st_savegame, &t_savegame)) hooks_note("exe:SaveGame");
    else slog("detour SaveGame failed");
    if (detour(ADDR_PC_AUTOSAVE, e_save, 6, l_save, (void *)st_pcauto, &t_pcauto)) hooks_note("exe:DoPCAutosave");
    else slog("detour DoPCAutosave failed");
}

void clock_install_module(HMODULE m, const char *label) {
    if (!m || g_clk.mode != 0) return;
    static const char *dlls[] = {"KERNEL32.dll", "kernel32.dll", 0};
    for (int i = 0; dlls[i]; i++) {
        if (iat_hook(m, dlls[i], "QueryPerformanceCounter", (void *)hkm_QPC)) { hooks_note(label); }
        iat_hook(m, dlls[i], "QueryPerformanceFrequency", (void *)hk_QPF);
        iat_hook(m, dlls[i], "GetTickCount", (void *)hkm_GTC);
    }
    iat_hook(m, "WINMM.dll", "timeGetTime", (void *)hkm_GTC);
    iat_hook(m, "winmm.dll", "timeGetTime", (void *)hkm_GTC);
}

} /* extern "C" */
