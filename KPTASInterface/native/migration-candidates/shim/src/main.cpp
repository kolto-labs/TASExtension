/* dinput8.dll proxy entry points (contract section 6, "Load").
 *
 * DllMain (under the loader lock) reads the environment, verifies the exe
 * (.text sha256 against the k1_gog_103 table), reserves harness_core,
 * patches the exe's IAT and code, and returns. It starts no threads,
 * no Winsock, and calls no LoadLibrary. On any mismatch the shim goes
 * passive: DirectInput8Create is forwarded to the system dinput8 and
 * nothing else is touched. */
#include "shim.hpp"

int g_active;
u8 g_text_sha[32];
char g_session_dir[260];
char g_modlist[2048];
HMODULE g_self;
u32 g_watchdog_s;

/* k1_gog_103 .text sha256 (KotorPhasor data/addresses/k1_gog_103.json known_text) */
static const u8 K1_GOG_103_TEXT[32] = {
    0x7f, 0x3f, 0xba, 0x60, 0xec, 0xc6, 0x41, 0x34, 0x7e, 0x38, 0xde, 0x04, 0x6b, 0xe0, 0x47, 0x23,
    0x9d, 0xd8, 0xcc, 0xb4, 0x63, 0xff, 0xca, 0x5e, 0x45, 0xfe, 0x88, 0x9d, 0xd5, 0x6b, 0xa3, 0xd3,
};

using di8c_t = HRESULT (WINAPI *)(HINSTANCE, DWORD, REFIID, LPVOID *, void *);
static di8c_t g_real_di8c;

extern "C" __declspec(dllexport) HRESULT WINAPI DirectInput8Create(HINSTANCE hinst, DWORD ver, REFIID riid, LPVOID *out,
                                                       void * outer) {
    if (g_active) {
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
            if (m) g_real_di8c = reinterpret_cast<di8c_t>(GetProcAddress(m, "DirectInput8Create"));
        }
    }
    if (!g_real_di8c) return E_FAIL;
    return g_real_di8c(hinst, ver, riid, out, outer);
}

static void capture_modlist(void) {
    u32 peb;
    __asm__ volatile("movl %%fs:0x30,%0" : "=r"(peb));
    u32 ldr = *reinterpret_cast<u32 *>(static_cast<uintptr_t>(peb + 0x0c));
    u32 head = ldr + 0x0c, cur = *reinterpret_cast<u32 *>(static_cast<uintptr_t>(head));
    u32 n = 0;
    for (int i = 0; cur != head && i < 64; i++) {
        u16 len = *reinterpret_cast<u16 *>(static_cast<uintptr_t>(cur + 0x2c));
        u32 bufp = *reinterpret_cast<u32 *>(static_cast<uintptr_t>(cur + 0x30));
        if (n && n + 1 < sizeof g_modlist) g_modlist[n++] = ',';
        for (u32 j = 0; bufp && j < len / 2u && n + 1 < sizeof g_modlist; j++)
            g_modlist[n++] = static_cast<char>(reinterpret_cast<wchar_t *>(static_cast<uintptr_t>(bufp))[j]);
        g_modlist[n] = 0;
        cur = *reinterpret_cast<u32 *>(static_cast<uintptr_t>(cur));
    }
}

static int verify_exe(void) {
    u8 *base = reinterpret_cast<u8 *>(GetModuleHandleA(nullptr));
    if (shim_abi::address32(base) != EXE_BASE) return 0;
    IMAGE_DOS_HEADER *dos = reinterpret_cast<IMAGE_DOS_HEADER *>(base);
    IMAGE_NT_HEADERS *nt = reinterpret_cast<IMAGE_NT_HEADERS *>(base + dos->e_lfanew);
    if (nt->OptionalHeader.AddressOfEntryPoint != EXE_ENTRY - EXE_BASE) return 0;
    IMAGE_SECTION_HEADER *sh = IMAGE_FIRST_SECTION(nt);
    int found = 0;
    for (u32 i = 0; i < nt->FileHeader.NumberOfSections; i++)
        if (k_strneq(reinterpret_cast<const char *>(sh[i].Name), ".text", 5) && sh[i].VirtualAddress == EXE_TEXT_VA - EXE_BASE &&
            sh[i].Misc.VirtualSize == EXE_TEXT_SIZE)
            found = 1;
    if (!found) return 0;
    sha256(reinterpret_cast<const void *>(static_cast<uintptr_t>(EXE_TEXT_VA)), EXE_TEXT_SIZE, g_text_sha);
    return memcmp(g_text_sha, K1_GOG_103_TEXT, 32) == 0;
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
        return;
    }
    if (!verify_exe()) {
        slog("exe does not match k1_gog_103: passive");
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
    if (GetEnvironmentVariableA("KPTAS_CLOCK", tmp, sizeof tmp)) clock_parse_env(tmp);
    else clock_parse_env(0);
    if (GetEnvironmentVariableA("KPTAS_WATCHDOG", tmp, sizeof tmp)) g_watchdog_s = (u32)k_parse_u64(tmp, 0);
    if (GetEnvironmentVariableA("KPTAS_WATCHDOG_CTX", tmp, sizeof tmp)) g_watchdog_ctx = (u32)k_parse_u64(tmp, 0);
    if (GetEnvironmentVariableA("KPTAS_HANG_MS", tmp, sizeof tmp)) g_hang_ms = (u32)k_parse_u64(tmp, 0);
    if (GetEnvironmentVariableA("KPTAS_PROFILE", tmp, sizeof tmp)) g_profile_ms = (u32)k_parse_u64(tmp, 0);
    if (GetEnvironmentVariableA("KPTAS_PRESENT", tmp, sizeof tmp))
        g_present_every = k_streq(tmp, "skip") ? 0 : k_streq(tmp, "draw") ? 1 : (u32)k_parse_u64(tmp, 0);
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
    save_install();
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
