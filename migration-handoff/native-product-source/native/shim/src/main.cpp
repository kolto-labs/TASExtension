/* dinput8.dll proxy entry points (contract section 6, "Load").
 *
 * DllMain (under the loader lock) reads the environment, verifies the exe
 * (.text sha256 against the k1_gog_103 table), reserves harness_core,
 * patches the exe's IAT and code, and returns. It starts no threads,
 * no Winsock, and calls no LoadLibrary. On any mismatch the shim goes
 * passive: DirectInput8Create is forwarded to the system dinput8 and
 * nothing else is touched.
 *
 * KPM is not supported. KPTAS does not run with KPM (KotOR Patch
 * Manager) installed: a vulnerability was found in it. When KPM's loader is in the
 * process (KotorPatcher.dll, or binkw32Hooked.dll behind its KProxy), or a
 * foreign patch sits at the exe entry, an active shim logs "refuse: kpm
 * present: ..." and exits with KPTAS_EXIT_KPM. It only reads: it never
 * touches KPM's files or modules. A passive shim only logs it. */
#include "shim.hpp"
#include "exe_profile.hpp"

/* Fixed C hook ABI and assembly-referenced symbol names. */
extern "C" {

int g_active;
u8 g_text_sha[32];
char g_session_dir[260];
char g_modlist[2048];
HMODULE g_self;
u32 g_watchdog_s;

/* ---- KPM refusal ---- */
static volatile LONG g_entry_reached;   /* entry_work ran: the shim's exe entry detour is intact */

static void kpm_refuse(const char *why, const char *where, int refuse) {
    if (!refuse) {
        slog("kpm present, passive shim: %s (at %s)", why, where);
        return;
    }
    slog("refuse: kpm present: %s (at %s)", why, where);
    TerminateProcess(GetCurrentProcess(), KPTAS_EXIT_KPM);
    for (;;) {}
}

/* KPM's loader in the process: KotorPatcher.dll, or binkw32Hooked.dll (the
 * game's Bink, renamed, that KPM's KProxy forwards to). Read-only lookups. */
static void kpm_guard(const char *where, int refuse) {
    if (GetModuleHandleA("KotorPatcher.dll")) kpm_refuse("KotorPatcher.dll is loaded", where, refuse);
    else if (GetModuleHandleA("binkw32Hooked.dll"))
        kpm_refuse("binkw32Hooked.dll is loaded (KPM's KProxy is binkw32.dll)", where, refuse);
}

/* The exe entry, from entry_work: every DllMain has run, so a loader that
 * a KProxy started is in by now. */
void kpm_guard_entry(void) {
    InterlockedExchange(&g_entry_reached, 1);
    kpm_guard("exe entry", g_active);
}

/* Active, the game must not run past its entry without the harness. If the
 * entry detour never ran, a foreign patch (KPM's own entry DETOUR, written
 * after this DllMain) replaced it, and the IAT hooks are live with no gate. */
void kpm_require_entry(const char *who) {
    if (g_active && !g_entry_reached)
        kpm_refuse("the shim's exe entry hook never ran; a foreign patch may have replaced it", who, 1);
}

typedef HRESULT(WINAPI *di8c_t)(HINSTANCE, DWORD, REFIID, LPVOID *, void *);
static di8c_t g_real_di8c;

__declspec(dllexport) HRESULT WINAPI DirectInput8Create(HINSTANCE hinst, DWORD ver, REFIID riid, LPVOID *out,
                                                       void * outer) {
    if (g_active) {
        static int checked;
        if (!checked) { checked = 1; kpm_guard("DirectInput8Create", 1); }
        kpm_require_entry("DirectInput8Create");
        (void)hinst; (void)ver; (void)riid; (void)outer;
        if (!out) return E_POINTER;
        return di_create(out);
    }
    if (!g_real_di8c) {
        char path[MAX_PATH];
        UINT n = GetSystemDirectoryA(path, MAX_PATH);
        if (n && n < MAX_PATH - 16) {
            memcpy(path + n, "\\dinput8.dll", 13);
            HMODULE m = LoadLibraryA(path);
            if (m) g_real_di8c = (di8c_t)(void *)GetProcAddress(m, "DirectInput8Create");
        }
    }
    if (!g_real_di8c) return E_FAIL;
    return g_real_di8c(hinst, ver, riid, out, outer);
}

static void capture_modlist(void) {
    u32 peb;
    __asm__ volatile("movl %%fs:0x30,%0" : "=r"(peb));
    u32 ldr = *(u32 *)(uintptr_t)(peb + 0x0c);
    u32 head = ldr + 0x0c, cur = *(u32 *)(uintptr_t)head;
    u32 n = 0;
    for (int i = 0; cur != head && i < 64; i++) {
        u16 len = *(u16 *)(uintptr_t)(cur + 0x2c);
        u32 bufp = *(u32 *)(uintptr_t)(cur + 0x30);
        if (n && n + 1 < sizeof g_modlist) g_modlist[n++] = ',';
        for (u32 j = 0; bufp && j < len / 2u && n + 1 < sizeof g_modlist; j++)
            g_modlist[n++] = (char)((wchar_t *)(uintptr_t)bufp)[j];
        g_modlist[n] = 0;
        cur = *(u32 *)(uintptr_t)cur;
    }
}

/* The exe's NT headers when it is mapped at EXE_BASE, else NULL. */
static IMAGE_NT_HEADERS *exe_nt(void) {
    u8 *base = (u8 *)GetModuleHandleA(NULL);
    if ((u32)(uintptr_t)base != EXE_BASE) return 0;
    return (IMAGE_NT_HEADERS *)(base + ((IMAGE_DOS_HEADER *)base)->e_lfanew);
}

static const exe_profile_t *identify_exe(void) {
    u8 *base = (u8 *)GetModuleHandleA(NULL);
    IMAGE_NT_HEADERS *nt = exe_nt();
    if (!nt) return 0;
    IMAGE_SECTION_HEADER *sh = IMAGE_FIRST_SECTION(nt);
    for (u32 p = 0; p < sizeof EXE_PROFILES / sizeof EXE_PROFILES[0]; p++) {
        const exe_profile_t *profile = &EXE_PROFILES[p];
        if ((u32)(uintptr_t)base != profile->image_base ||
            nt->OptionalHeader.AddressOfEntryPoint != profile->entry - profile->image_base) continue;
        for (u32 i = 0; i < nt->FileHeader.NumberOfSections; i++) {
            if (!k_strneq((const char *)sh[i].Name, ".text", 5) ||
                sh[i].VirtualAddress != profile->text_va - profile->image_base ||
                sh[i].Misc.VirtualSize != profile->text_size) continue;
            sha256((const void *)(uintptr_t)profile->text_va, profile->text_size, g_text_sha);
            if (memcmp(g_text_sha, profile->text_sha256, 32) == 0) return profile;
        }
    }
    return NULL;
}

/* The K1 image (entry at EXE_ENTRY) whose entry bytes are not the original:
 * KPM's DETOUR site. Such an image would only fail the .text identity and go
 * passive; this names it instead. */
static int entry_patched_by_foreign(void) {
    const IMAGE_NT_HEADERS *nt = exe_nt();
    return nt && nt->OptionalHeader.AddressOfEntryPoint == EXE_ENTRY - EXE_BASE &&
           memcmp((const void *)(uintptr_t)EXE_ENTRY, EXE_ENTRY_BYTES, EXE_ENTRY_LEN) != 0;
}

static void attach(void) {
    char tmp[512];
    g_main_rtid = cur_rtid();
    if (GetEnvironmentVariableA("KPTAS_SESSION", g_session_dir, sizeof g_session_dir) == 0) g_session_dir[0] = 0;
    slog_init();
    slog("==== %s %s attach tid=%x ====", SHIM_NAME, SHIM_VERSION, g_main_rtid);
    capture_modlist();
    if (!GetEnvironmentVariableA("KPTAS_PORT", tmp, sizeof tmp)) {
        slog("KPTAS_PORT unset: passive");
        kpm_guard("dllmain, passive", 0);   /* passive: log only, never end a game KPTAS did not launch */
        return;
    }
    kpm_guard("dllmain", 1);                /* before any hook installs */
    if (entry_patched_by_foreign()) {
        slog("refuse: kpm present: a foreign patch is at the exe entry %x (at dllmain)", EXE_ENTRY);
        TerminateProcess(GetCurrentProcess(), KPTAS_EXIT_KPM);
        for (;;) {}
    }
    const exe_profile_t *profile = identify_exe();
    if (!profile) {
        slog("exe does not match a known native profile: passive");
        return;
    }
    if (!profile->hooks_admitted) {
        slog("recognized %s: passive (%s)", profile->name, profile->refusal);
        return;
    }
    /* Read and drop KPTAS_PORTABLE before the CRT copies the environment into
     * game memory. Python gives it one length in both math modes, so the Wine
     * heap below that copy is laid out the same way too. */
    if (GetEnvironmentVariableA("KPTAS_PORTABLE", tmp, sizeof tmp)) {
        math_parse_env(tmp);
        SetEnvironmentVariableA("KPTAS_PORTABLE", NULL);
    }
    /* Saved callback pointers and stack return addresses must name this same
     * image in every process. Never proceed with a relocated shim. */
    if ((u32)(uintptr_t)g_self != 0x10000000u) {
        slog("shim fixed image base unavailable (expected 10000000)");
        TerminateProcess(GetCurrentProcess(), 6);
        return;
    }
    if (!blake2s_selftest()) {
        slog("blake2s self-test failed: passive");
        return;
    }
    if (!core_init(32u << 20)) {
        slog("harness_core reserve failed: passive");
        return;
    }
    /* the sim set: arena, harness_game and image_data, reserved before the CRT runs */
    if (!state_init_early()) {
        slog("state init failed: passive");
        return;
    }
    /* the virtual machine boundary, before anything of the game reads the environment */
    if (!bound_init_early()) {
        slog("boundary init failed: passive");
        return;
    }
    if (GetEnvironmentVariableA("KPTAS_CLOCK", tmp, sizeof tmp)) clock_parse_env(tmp);
    else clock_parse_env(0);
    if (GetEnvironmentVariableA("KPTAS_WATCHDOG", tmp, sizeof tmp)) g_watchdog_s = (u32)k_parse_u64(tmp, 0);
    if (GetEnvironmentVariableA("KPTAS_WATCHDOG_CTX", tmp, sizeof tmp)) g_watchdog_ctx = (u32)k_parse_u64(tmp, 0);
    if (GetEnvironmentVariableA("KPTAS_HANG_MS", tmp, sizeof tmp)) g_hang_ms = (u32)k_parse_u64(tmp, 0);
    if (GetEnvironmentVariableA("KPTAS_PROFILE", tmp, sizeof tmp)) g_profile_ms = (u32)k_parse_u64(tmp, 0);
    audio_parse_env(GetEnvironmentVariableA("KPTAS_AUDIO", tmp, sizeof tmp) ? tmp : 0);
    if (GetEnvironmentVariableA("KPTAS_PRESENT", tmp, sizeof tmp))
        g_present_every = k_streq(tmp, "skip") ? 0 : k_streq(tmp, "draw") ? 1 : (u32)k_parse_u64(tmp, 0);
    /* Diagnostic only: check every texture-shadow hit against the driver and
     * die on a mismatch. Read, then dropped so the game never sees it. */
    if (GetEnvironmentVariableA("KPTAS_GL_SHADOW_VERIFY", tmp, sizeof tmp)) {
        g_gl_shadow_verify = k_streq(tmp, "1");
        SetEnvironmentVariableA("KPTAS_GL_SHADOW_VERIFY", NULL);
        if (g_gl_shadow_verify) slog("GL texture shadow verify mode");
    }
    g_active = 1;
    clock_install_exe();
    clock_install_module(GetModuleHandleA("mss32.dll"), "mss32:time");
    clock_install_module(GetModuleHandleA("binkw32.dll"), "binkw32:time");
    threads_install();
    vmem_install();
    vhandle_install();
    gl_install();
    msg_install();
    geo_install();
    gate_install();
    wh_install();
    save_install();
    audio_install();
    bound_install();
    gldraw_install();
    gllists_install();
    math_install();
    xtrace_install();
    glenv_install();
    btrace_install();
    ledger_seal();   /* after the last install */
    slog("attach done: clock mode=%d hz=%u qpf=%u uptime0=%u hooks=%u",
         g_clk.mode, g_clk.hz, (u32)g_clk.qpf, (u32)g_clk.uptime0_ms, g_hooks_n);
    slog("hooks: %s", g_hooks_list);
}

BOOL WINAPI DllMain(HINSTANCE h, DWORD reason, LPVOID reserved) {
    (void)reserved;
    if (reason == DLL_PROCESS_ATTACH) {
        g_self = h;
        DisableThreadLibraryCalls(h);
        attach();
    }
    return TRUE;
}

} /* extern "C" */
