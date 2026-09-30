/* Virtual clock (contract D5, section 5.5) and pinned host facts (section 7 item 4).
 *
 * V(n) = V(s) + floor((n - s) * 1e6 / hz) us; V is frozen within a tick.
 * U = uptime0_ms * 1000 + V (+ budget-guard micro-advance).
 * QPC = floor(U * qpf / 1e6), QPF = qpf, GetTickCount = floor(U / 1000) mod 2^32,
 * GetSystemTimeAsFileTime = initial_ft + 10 * V.
 * Live from DllMain: the CRT cookie and the startup srand seed read it. */
#include "shim.h"

/* g_clk is SH->clk (harness_game): a restore rewinds the clock with memory,
 * including last_u, the monotonic clamp. */
u32 g_guard_trips, g_guard_site_reads = 100000, g_guard_step_us = 1, g_guard_first_site;

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
        u64 num = k_parse_u64(v, 0);
        if (kl == 4 && k_strneq(k, "mode", 4)) g_clk.mode = k_strneq(v, "wall", 4) ? 1 : 0;
        else if (kl == 2 && k_strneq(k, "hz", 2)) g_clk.hz = num ? (u32)num : 60;
        else if (kl == 6 && k_strneq(k, "qpf_hz", 6)) g_clk.qpf = num ? num : 10000000ull;
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

#define RETADDR() ((u32)(uintptr_t)__builtin_return_address(0))

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
    FileTimeToSystemTime(&ft, st);  /* profile time zone is UTC */
}
static DWORD WINAPI hk_GetTimeZoneInformation(LPTIME_ZONE_INFORMATION tz) {
    memset(tz, 0, sizeof *tz);
    return TIME_ZONE_ID_UNKNOWN;
}
static BOOL WINAPI hk_FileTimeToLocalFileTime(const FILETIME *in, LPFILETIME out) {
    *out = *in;
    return TRUE;
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
static void WINAPI hk_GlobalMemoryStatus(LPMEMORYSTATUS m) {
    m->dwLength = sizeof(MEMORYSTATUS);
    m->dwMemoryLoad = 50;
    m->dwTotalPhys = 0x7fffffffu;
    m->dwAvailPhys = 0x7fffffffu;
    m->dwTotalPageFile = 0x7fffffffu;
    m->dwAvailPageFile = 0x7fffffffu;
    m->dwTotalVirtual = 0x7ffe0000u;
    m->dwAvailVirtual = 0x70000000u;
}
static BOOL WINAPI hk_GetDiskFreeSpaceExA(LPCSTR dir, PULARGE_INTEGER avail, PULARGE_INTEGER total,
                                          PULARGE_INTEGER totfree) {
    (void)dir;
    if (avail) avail->QuadPart = 20480ull << 20;
    if (total) total->QuadPart = 102400ull << 20;
    if (totfree) totfree->QuadPart = 20480ull << 20;
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
    if ((uintptr_t)name > 0xffff) {
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
        HOOK("KERNEL32.dll", "GetTimeZoneInformation", hk_GetTimeZoneInformation, 0);
        HOOK("KERNEL32.dll", "FileTimeToLocalFileTime", hk_FileTimeToLocalFileTime, 0);
        HOOK("KERNEL32.dll", "Sleep", hk_Sleep, 0);
    }
    HOOK("KERNEL32.dll", "GetCurrentProcessId", hk_GetCurrentProcessId, 0);
    HOOK("KERNEL32.dll", "GlobalMemoryStatus", hk_GlobalMemoryStatus, 0);
    HOOK("KERNEL32.dll", "GetDiskFreeSpaceExA", hk_GetDiskFreeSpaceExA, 0);
    HOOK("KERNEL32.dll", "GetProcAddress", hk_GetProcAddress, &r_GetProcAddress);
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
