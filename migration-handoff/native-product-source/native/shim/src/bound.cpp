/* Virtual machine boundary (contract section 7): one fixed machine for the game.
 *
 * Nothing about the host, the install path, the user or the session reaches
 * game memory through the calls below. The exe sees:
 *   - a fixed install directory (C:\KotOR by default) in GetModuleFileNameA,
 *     GetCommandLineA, GetStartupInfoA, GetCurrentDirectoryA and
 *     GetFullPathNameA. File calls map a path under it back to the real working
 *     directory; a relative path is left alone, since the real working directory
 *     resolves it;
 *   - a fixed environment block (GetEnvironmentStrings, GetEnvironmentStringsW,
 *     GetEnvironmentVariableA/W), built only from the values below. The shim
 *     reads KPTAS_* from the real environment in DllMain; the game never sees it;
 *   - fixed SYSTEM_INFO, OSVERSIONINFO, code pages and locale, UTC time zone,
 *     computer and user name, and no standard handles (a GUI process);
 *   - fixed GL identity: glGetString vendor, renderer, version and extensions,
 *     the WGL extension string, and the GL limits that reach game memory
 *     (glGetIntegerv/glGetFloatv of the enums in gl_q).
 *
 * The values come from KPTAS_BOUNDARY, which the supervisor builds from the
 * portable profile: ';'-separated key=value pairs (see bound_parse). Unset means
 * the built-in baseline-1 values below. "native" means no boundary: the real
 * values pass through, as the contract's native profile says.
 *
 * Every string the game can keep a pointer to lives in one block in
 * harness_game at a fixed address, filled once in DllMain. The pointers the CRT
 * and the renderer store are then the same in every run, and a restore writes
 * back the same bytes. Nothing is allocated from the game heap, so the heap
 * layout does not depend on the boundary either.
 *
 * CExoRandInternal::GenSeed (0x5ed080) copies 128 bytes of uninitialised stack
 * into the engine's random pool before it adds QPC and SYSTEM_INFO. Those bytes
 * are whatever earlier calls left below the caller, Wine's own frames included.
 * The detour here zeroes the dead stack below GenSeed's entry, so the seed
 * depends only on the virtual clock and the virtual SYSTEM_INFO. It runs in
 * native mode too: it is determinism, not identity. */
#include "shim.hpp"

/* Fixed C hook ABI and assembly-referenced symbol names. */
extern "C" {

#define BND_OFF 0x003F0000u     /* harness_game offset of the block: the last 64 KB */
#define BND_MAGIC 0x444E4242u

typedef struct {
    u32 magic, version;
    char install[128];          /* virtual install directory, no trailing '\' */
    char module[160];           /* install + "\swkotor.exe" */
    char cmdline[400];
    WCHAR cmdline_w[400];
    char desktop[4];            /* "" */
    char computer[32], user[32];
    char gl_vendor[64], gl_renderer[96], gl_version[64];
    char gl_ext[2048];          /* space-separated, trailing space, as drivers do */
    char wgl_ext[1024];
    char env_a[2048];           /* NUL-separated, double NUL at the end */
    WCHAR env_w[2048];
} bnd_block;

/* shim_hg (from SHG_OFF) grows with each part of the tick it keeps; the block
 * sits above it and must never share a byte with it. */
static_assert(SHG_OFF + sizeof(shim_hg) <= BND_OFF, "shim_hg runs into the boundary block");
static_assert(sizeof(bnd_block) <= 0x10000u, "the boundary block outgrew its 64 KB");

/* baseline-1 (kptas.tier.envpin BUILTIN_PROFILES). The GL extensions are the ones
 * the K1 exe checks (GLRender::InitExtensions, InitializeAntiAliasing) that Mesa
 * llvmpipe supports, plus what WGL_ARB_render_texture needs. */
static constexpr char DEFAULT_BOUNDARY[] =
    "os=6.1.7601;lcid=0x409;acp=1252;oemcp=437;tz=UTC;computer=KPTAS;user=player;"
    "cpus=1;cpu_level=6;cpu_rev=0x0d06;install=C:\\KotOR;"
    "gl_vendor=KPTAS;gl_renderer=KPTAS baseline-1;gl_version=1.5.0;"
    "gl_ext=GL_ARB_depth_texture GL_ARB_multitexture GL_ARB_texture_compression GL_ARB_vertex_buffer_object "
    "GL_ARB_vertex_program GL_ATI_fragment_shader GL_EXT_bgra GL_EXT_compiled_vertex_array GL_EXT_fog_coord "
    "GL_EXT_secondary_color GL_EXT_stencil_two_side GL_EXT_texture_compression_s3tc GL_EXT_texture_cube_map "
    "GL_EXT_texture_env_combine GL_EXT_texture_filter_anisotropic GL_SGIS_generate_mipmap "
    "WGL_ARB_extensions_string WGL_ARB_pbuffer WGL_ARB_pixel_format WGL_ARB_render_texture "
    "WGL_EXT_extensions_string WGL_EXT_swap_control;"
    "gl_q=0x0d57:8,0x84ff:16,0x854d:";

bound_mode g_bnd_modes[16] = {{640,480,60}, {800,600,60}, {1024,768,60}};
u32 g_bnd_nmodes = 3, g_bnd_window_w = 800, g_bnd_window_h = 600, g_bnd_refresh = 60;
u32 g_bnd_total_mb = 4096, g_bnd_avail_mb = 2048, g_bnd_disk_mb = 20480;
int g_bnd_native;
u8 g_bnd_sha[32];
u32 g_bnd_gl_missing;
static bnd_block *B;
static u32 os_major = 6, os_minor = 1, os_build = 7601, lcid = 0x409, acp = 1252, oemcp = 437;
static u32 cpus = 1, cpu_level = 6, cpu_rev = 0x0d06;
static char g_real_root[MAX_PATH];   /* the real working directory the install directory maps to */
static u32 g_real_root_n;
static char g_bnd_text[4096];

/* GL queries served from the profile: an enum with no values is unsupported (params untouched) */
#define GLQ_MAX 16
typedef struct { u32 e, n, logged; i32 v[4]; } glq_t;
static glq_t g_glq[GLQ_MAX];
static u32 g_nglq;

/* ---------------------------------------------------------------- strings */

static void scopy(char *d, u32 cap, const char *s, u32 n) {
    if (n > cap - 1) n = cap - 1;
    memcpy(d, s, n);
    d[n] = 0;
}

static u32 widen(WCHAR *d, const char *s, u32 n) {
    for (u32 i = 0; i < n; i++) d[i] = (WCHAR)(u8)s[i];
    return n;
}

static char up(char c) { return (c >= 'a' && c <= 'z') ? (char)(c - 32) : c; }

/* n leading chars of a equal b's, ignoring case and '/' versus '\' */
static int prefix_ieq(const char *a, const char *b, u32 n) {
    for (u32 i = 0; i < n; i++) {
        char x = a[i] == '/' ? '\\' : up(a[i]), y = b[i] == '/' ? '\\' : up(b[i]);
        if (x != y || !x) return 0;
    }
    return 1;
}

/* A virtual path under the install directory as the real path, in out; any other path as given. */
const char *bound_v2r(const char *p, char *out, u32 cap) {
    if (g_bnd_native || !p || !B) return p;
    u32 n = (u32)strlen(B->install);
    if (!prefix_ieq(p, B->install, n) || (p[n] && p[n] != '\\' && p[n] != '/')) return p;
    u32 rest = (u32)strlen(p + n);
    if (g_real_root_n + rest + 1 > cap) return p;
    memcpy(out, g_real_root, g_real_root_n);
    memcpy(out + g_real_root_n, p + n, rest + 1);
    return out;
}

/* A real path under the real working directory, rewritten in place to the virtual one. */
static void r2v(char *p, u32 cap) {
    u32 n = g_real_root_n;
    if (!n || !prefix_ieq(p, g_real_root, n) || (p[n] && p[n] != '\\' && p[n] != '/')) return;
    u32 il = (u32)strlen(B->install), rest = (u32)strlen(p + n);
    if (il + rest + 1 > cap) return;
    memmove(p + il, p + n, rest + 1);
    memcpy(p, B->install, il);
}

/* Copy s to a caller buffer with the Vista semantics of the A path calls:
 * returns the length, or the size needed (with the NUL) when buf is too small. */
static DWORD put_path(const char *s, char *buf, DWORD cap) {
    u32 n = (u32)strlen(s);
    if (!buf || cap <= n) return n + 1;
    memcpy(buf, s, n + 1);
    return n;
}

/* ---------------------------------------------------------------- KPTAS_BOUNDARY */

static void set_kv(const char *k, u32 kl, const char *v, u32 vl) {
    char t[24];
    scopy(t, sizeof t, v, vl);
    u64 num = k_parse_u64(t, 0);
#define KEY(s) (kl == sizeof(s) - 1 && k_strneq(k, s, kl))
    if (KEY("os")) {
        const char *e;
        os_major = (u32)k_parse_u64(t, &e);
        if (*e == '.') os_minor = (u32)k_parse_u64(e + 1, &e);
        if (*e == '.') os_build = (u32)k_parse_u64(e + 1, &e);
    } else if (KEY("lcid")) lcid = (u32)num;
    else if (KEY("acp")) acp = (u32)num;
    else if (KEY("oemcp")) oemcp = (u32)num;
    else if (KEY("tz")) { if (!(vl == 3 && k_strneq(v, "UTC", 3))) slog("boundary: tz %s is not served; UTC", t); }
    else if (KEY("computer")) scopy(B->computer, sizeof B->computer, v, vl);
    else if (KEY("user")) scopy(B->user, sizeof B->user, v, vl);
    else if (KEY("cpus")) cpus = num >= 1 && num <= 32 ? (u32)num : 1;
    else if (KEY("cpu_level")) cpu_level = (u32)num;
    else if (KEY("cpu_rev")) cpu_rev = (u32)num;
    else if (KEY("install")) scopy(B->install, sizeof B->install, v, vl);
    else if (KEY("window_w")) g_bnd_window_w = (u32)num;
    else if (KEY("window_h")) g_bnd_window_h = (u32)num;
    else if (KEY("refresh_hz")) g_bnd_refresh = (u32)num;
    else if (KEY("total_phys_mb")) g_bnd_total_mb = (u32)num;
    else if (KEY("avail_phys_mb")) g_bnd_avail_mb = (u32)num;
    else if (KEY("disk_free_mb")) g_bnd_disk_mb = (u32)num;
    else if (KEY("modes")) {
        const char *q = v, *e = v + vl;
        g_bnd_nmodes = 0;
        while (q < e && g_bnd_nmodes < 16) {
            const char *next;
            bound_mode m;
            m.w = (u32)k_parse_u64(q, &next);
            if (next >= e || *next != 'x') break;
            m.h = (u32)k_parse_u64(next + 1, &next);
            if (next >= e || *next != '@') break;
            m.hz = (u32)k_parse_u64(next + 1, &next);
            if (!m.w || !m.h || !m.hz) break;
            g_bnd_modes[g_bnd_nmodes++] = m;
            if (next >= e || *next != ',') break;
            q = next + 1;
        }
    }
    else if (KEY("gl_vendor")) scopy(B->gl_vendor, sizeof B->gl_vendor, v, vl);
    else if (KEY("gl_renderer")) scopy(B->gl_renderer, sizeof B->gl_renderer, v, vl);
    else if (KEY("gl_version")) scopy(B->gl_version, sizeof B->gl_version, v, vl);
    else if (KEY("gl_ext")) {
        /* GL_ names go to GL_EXTENSIONS, WGL_ names to the WGL string */
        buf_t g, w;
        b_init(&g, B->gl_ext, sizeof B->gl_ext);
        b_init(&w, B->wgl_ext, sizeof B->wgl_ext);
        u32 i = 0;
        while (i < vl) {
            while (i < vl && v[i] == ' ') i++;
            u32 s = i;
            while (i < vl && v[i] != ' ') i++;
            if (i == s) break;
            buf_t *d = (i - s > 4 && k_strneq(v + s, "WGL_", 4)) ? &w : &g;
            b_raw(d, v + s, i - s);
            b_ch(d, ' ');
        }
        b_term(&g);
        b_term(&w);
    } else if (KEY("gl_q")) {
        /* enum:value[/value..],... */
        g_nglq = 0;
        const char *q = v, *e = v + vl;
        while (q < e && g_nglq < GLQ_MAX) {
            char tok[64];
            const char *c = q;
            while (c < e && *c != ',') c++;
            scopy(tok, sizeof tok, q, (u32)(c - q));
            const char *r;
            glq_t *g = &g_glq[g_nglq];
            memset(g, 0, sizeof *g);
            g->e = (u32)k_parse_u64(tok, &r);
            if (*r == ':') {
                r++;
                while (*r && g->n < 4) {
                    g->v[g->n++] = (i32)k_parse_u64(r, &r);
                    if (*r != '/') break;
                    r++;
                }
                g_nglq++;
            }
            q = c < e ? c + 1 : c;
        }
    } else {
        char kk[32];
        scopy(kk, sizeof kk, k, kl);
        slog("boundary: unknown key %s", kk);
    }
#undef KEY
}

static void bound_parse(const char *s) {
    while (*s) {
        const char *k = s;
        while (*s && *s != '=' && *s != ';') s++;
        u32 kl = (u32)(s - k);
        if (*s != '=') { if (*s) s++; continue; }
        const char *v = ++s;
        while (*s && *s != ';') s++;
        set_kv(k, kl, v, (u32)(s - v));
        if (*s) s++;
    }
}

/* The environment block: a fixed, documented set (contract section 7), sorted as Windows sorts it. */
static void build_env(void) {
    buf_t b;
    char n[16], lv[16], rv[8];
    const char *hx = "0123456789abcdef";
    b_init(&b, B->env_a, sizeof B->env_a - 1);
    const char *u = B->user;
#define VAR(s) do { b_str(&b, s); } while (0)
#define END() do { b_ch(&b, 0); } while (0)
    VAR("ALLUSERSPROFILE=C:\\ProgramData"); END();
    VAR("APPDATA=C:\\users\\"); VAR(u); VAR("\\AppData\\Roaming"); END();
    VAR("CommonProgramFiles=C:\\Program Files\\Common Files"); END();
    VAR("COMPUTERNAME="); VAR(B->computer); END();
    VAR("ComSpec=C:\\windows\\system32\\cmd.exe"); END();
    VAR("HOMEDRIVE=C:"); END();
    VAR("HOMEPATH=\\users\\"); VAR(u); END();
    VAR("LOCALAPPDATA=C:\\users\\"); VAR(u); VAR("\\AppData\\Local"); END();
    buf_t t;
    b_init(&t, n, sizeof n); b_u64(&t, cpus); b_term(&t);
    VAR("NUMBER_OF_PROCESSORS="); VAR(n); END();
    VAR("OS=Windows_NT"); END();
    VAR("PATH=C:\\windows\\system32;C:\\windows"); END();
    VAR("PATHEXT=.COM;.EXE;.BAT;.CMD"); END();
    VAR("PROCESSOR_ARCHITECTURE=x86"); END();
    b_init(&t, lv, sizeof lv); b_u64(&t, cpu_level); b_term(&t);
    VAR("PROCESSOR_IDENTIFIER=x86 Family "); VAR(lv); VAR(" Model ");
    b_u64(&b, cpu_rev >> 8); VAR(" Stepping "); b_u64(&b, cpu_rev & 0xff); VAR(", GenuineIntel"); END();
    VAR("PROCESSOR_LEVEL="); VAR(lv); END();
    for (int i = 0; i < 4; i++) rv[i] = hx[(cpu_rev >> (12 - 4 * i)) & 15];
    rv[4] = 0;
    VAR("PROCESSOR_REVISION="); VAR(rv); END();
    VAR("ProgramData=C:\\ProgramData"); END();
    VAR("ProgramFiles=C:\\Program Files"); END();
    VAR("PUBLIC=C:\\users\\Public"); END();
    VAR("SystemDrive=C:"); END();
    VAR("SystemRoot=C:\\windows"); END();
    VAR("TEMP=C:\\users\\"); VAR(u); VAR("\\AppData\\Local\\Temp"); END();
    VAR("TMP=C:\\users\\"); VAR(u); VAR("\\AppData\\Local\\Temp"); END();
    VAR("USERDOMAIN="); VAR(B->computer); END();
    VAR("USERNAME="); VAR(u); END();
    VAR("USERPROFILE=C:\\users\\"); VAR(u); END();
    VAR("windir=C:\\windows"); END();
#undef VAR
#undef END
    if (b.overflow) slog("boundary: environment block truncated");
    B->env_a[b.n] = 0;   /* the second NUL; the block was zeroed when committed */
    widen(B->env_w, B->env_a, b.n + 1);
}

/* GetCommandLineA: the virtual exe path, quoted, then whatever followed the exe in the real one. */
static void build_cmdline(void) {
    const char *r = GetCommandLineA();
    if (*r == '"') { r++; while (*r && *r != '"') r++; if (*r) r++; }
    else while (*r && *r != ' ' && *r != '\t') r++;
    while (*r == ' ' || *r == '\t') r++;
    buf_t b;
    b_init(&b, B->cmdline, sizeof B->cmdline);
    b_ch(&b, '"'); b_str(&b, B->module); b_ch(&b, '"');
    if (*r) { b_ch(&b, ' '); b_str(&b, r); }
    b_term(&b);
    widen(B->cmdline_w, B->cmdline, (u32)strlen(B->cmdline) + 1);
}

/* ---------------------------------------------------------------- process and machine */

static LPSTR WINAPI hk_GetCommandLineA(void) { return B->cmdline; }
static LPWSTR WINAPI hk_GetCommandLineW(void) { return B->cmdline_w; }
/* CRT startup and WinMain retain these imported procedure addresses in live
 * callee-saved registers. Keep their code identities in the fixed shim while
 * forwarding the actual operation unchanged. */
static HMODULE WINAPI hk_GetModuleHandleA(LPCSTR name) { return GetModuleHandleA(name); }
static BOOL WINAPI hk_SetPriorityClass(HANDLE process, DWORD priority) { return SetPriorityClass(process,priority); }
static HRESULT (WINAPI *r_CoInitialize)(LPVOID);
static HRESULT WINAPI hk_CoInitialize(LPVOID reserved) {
    math_host_scope_t fp;math_scope_begin(&fp,MATH_HOST_FP|MATH_HOST_ID);
    HRESULT result=r_CoInitialize(reserved);
    math_scope_end(&fp);
    return result;
}

static DWORD (WINAPI *r_GetModuleFileNameA)(HMODULE, LPSTR, DWORD);
static DWORD WINAPI hk_GetModuleFileNameA(HMODULE m, LPSTR buf, DWORD cap) {
    char t[MAX_PATH + 8];
    if (!m || m == GetModuleHandleA(NULL)) scopy(t, sizeof t, B->module, (u32)strlen(B->module));
    else {
        DWORD n = GetModuleFileNameA(m, t, MAX_PATH);
        if (!n) return 0;
        t[n < MAX_PATH ? n : MAX_PATH - 1] = 0;
        r2v(t, sizeof t);
    }
    if (!buf || !cap) { SetLastError(ERROR_INSUFFICIENT_BUFFER); return 0; }
    u32 n = (u32)strlen(t);
    if (n < cap) { memcpy(buf, t, n + 1); SetLastError(NO_ERROR); return n; }
    memcpy(buf, t, cap - 1);
    buf[cap - 1] = 0;
    SetLastError(ERROR_INSUFFICIENT_BUFFER);
    return cap;
}
static DWORD WINAPI hk_GetModuleFileNameW(HMODULE m, LPWSTR buf, DWORD cap) {
    char t[MAX_PATH + 8];
    DWORD n = hk_GetModuleFileNameA(m, t, sizeof t);
    if (!n) return 0;
    if (!buf || !cap) { SetLastError(ERROR_INSUFFICIENT_BUFFER); return 0; }
    if (n < cap) { widen(buf, t, n + 1); return n; }
    widen(buf, t, cap - 1);
    buf[cap - 1] = 0;
    SetLastError(ERROR_INSUFFICIENT_BUFFER);
    return cap;
}

static LPCH WINAPI hk_GetEnvironmentStrings(void) { return B->env_a; }
static LPWCH WINAPI hk_GetEnvironmentStringsW(void) { return B->env_w; }
static BOOL (WINAPI *r_FreeEnvironmentStringsA)(LPCH);
static BOOL (WINAPI *r_FreeEnvironmentStringsW)(LPWCH);
static BOOL WINAPI hk_FreeEnvironmentStringsA(LPCH p) { return p == B->env_a ? TRUE : r_FreeEnvironmentStringsA(p); }
static BOOL WINAPI hk_FreeEnvironmentStringsW(LPWCH p) { return p == B->env_w ? TRUE : r_FreeEnvironmentStringsW(p); }

/* the value of name in the virtual block, or 0 */
static const char *env_find(const char *name) {
    u32 nl = (u32)strlen(name);
    for (const char *e = B->env_a; *e; e += strlen(e) + 1)
        if (prefix_ieq(e, name, nl) && e[nl] == '=') return e + nl + 1;
    return 0;
}
static DWORD WINAPI hk_GetEnvironmentVariableA(LPCSTR name, LPSTR buf, DWORD cap) {
    const char *v = name ? env_find(name) : 0;
    if (!v) { SetLastError(ERROR_ENVVAR_NOT_FOUND); return 0; }
    u32 n = (u32)strlen(v);
    if (!buf || cap <= n) return n + 1;
    memcpy(buf, v, n + 1);
    return n;
}
static DWORD WINAPI hk_GetEnvironmentVariableW(LPCWSTR name, LPWSTR buf, DWORD cap) {
    char a[64];
    u32 i = 0;
    for (; name && name[i] && i < sizeof a - 1; i++) a[i] = name[i] < 0x80 ? (char)name[i] : '?';
    a[i] = 0;
    const char *v = name ? env_find(a) : 0;
    if (!v) { SetLastError(ERROR_ENVVAR_NOT_FOUND); return 0; }
    u32 n = (u32)strlen(v);
    if (!buf || cap <= n) return n + 1;
    widen(buf, v, n + 1);
    return n;
}

static void fill_sysinfo(LPSYSTEM_INFO si) {
    memset(si, 0, sizeof *si);
    si->wProcessorArchitecture = PROCESSOR_ARCHITECTURE_INTEL;
    si->dwPageSize = 4096;
    si->lpMinimumApplicationAddress = (LPVOID)0x00010000u;
    si->lpMaximumApplicationAddress = (LPVOID)0x7ffeffffu;
    si->dwActiveProcessorMask = cpus >= 32 ? 0xffffffffu : (1u << cpus) - 1;
    si->dwNumberOfProcessors = cpus;
    si->dwProcessorType = PROCESSOR_INTEL_PENTIUM;
    si->dwAllocationGranularity = 65536;
    si->wProcessorLevel = (WORD)cpu_level;
    si->wProcessorRevision = (WORD)cpu_rev;
}
static void WINAPI hk_GetSystemInfo(LPSYSTEM_INFO si) { fill_sysinfo(si); }

static BOOL WINAPI hk_GetVersionExA(LPOSVERSIONINFOA v) {
    if (!v || (v->dwOSVersionInfoSize != sizeof(OSVERSIONINFOA) && v->dwOSVersionInfoSize != sizeof(OSVERSIONINFOEXA))) {
        SetLastError(ERROR_INSUFFICIENT_BUFFER);
        return FALSE;
    }
    DWORD size = v->dwOSVersionInfoSize;
    memset(v, 0, size);
    v->dwOSVersionInfoSize = size;
    v->dwMajorVersion = os_major;
    v->dwMinorVersion = os_minor;
    v->dwBuildNumber = os_build;
    v->dwPlatformId = VER_PLATFORM_WIN32_NT;
    if (size == sizeof(OSVERSIONINFOEXA)) {
        OSVERSIONINFOEXA *x = (OSVERSIONINFOEXA *)v;
        x->wSuiteMask = VER_SUITE_SINGLEUSERTS;
        x->wProductType = VER_NT_WORKSTATION;
    }
    return TRUE;
}

/* A GUI process started from the shell: no inherited handles, no standard handles. */
static void WINAPI hk_GetStartupInfoA(LPSTARTUPINFOA s) {
    memset(s, 0, sizeof *s);
    s->cb = sizeof *s;
    s->lpDesktop = B->desktop;
    s->lpTitle = B->module;
    s->dwFlags = STARTF_USESHOWWINDOW;
    s->wShowWindow = SW_SHOWNORMAL;
    s->hStdInput = s->hStdOutput = s->hStdError = INVALID_HANDLE_VALUE;
    math_host_probe("GetStartupInfoA",0xffffffffu);
}
static HANDLE WINAPI hk_GetStdHandle(DWORD which) { (void)which; return NULL; }

static BOOL name_out(const char *v, LPSTR buf, LPDWORD cap) {
    u32 n = (u32)strlen(v);
    if (!buf || !cap || *cap <= n) { if (cap) *cap = n + 1; SetLastError(ERROR_BUFFER_OVERFLOW); return FALSE; }
    memcpy(buf, v, n + 1);
    *cap = n;
    return TRUE;
}
static BOOL name_out_w(const char *v, LPWSTR buf, LPDWORD cap) {
    u32 n = (u32)strlen(v);
    if (!buf || !cap || *cap <= n) { if (cap) *cap = n + 1; SetLastError(ERROR_BUFFER_OVERFLOW); return FALSE; }
    widen(buf, v, n + 1);
    *cap = n;
    return TRUE;
}
static BOOL WINAPI hk_GetComputerNameA(LPSTR b, LPDWORD n) { return name_out(B->computer, b, n); }
static BOOL WINAPI hk_GetComputerNameW(LPWSTR b, LPDWORD n) { return name_out_w(B->computer, b, n); }
/* GetUserName counts the NUL in *n */
static BOOL WINAPI hk_GetUserNameA(LPSTR b, LPDWORD n) { BOOL r = name_out(B->user, b, n); if (r) (*n)++; return r; }
static BOOL WINAPI hk_GetUserNameW(LPWSTR b, LPDWORD n) { BOOL r = name_out_w(B->user, b, n); if (r) (*n)++; return r; }

/* ---------------------------------------------------------------- time zone: UTC */

static DWORD WINAPI hk_GetTimeZoneInformation(LPTIME_ZONE_INFORMATION tz) {
    memset(tz, 0, sizeof *tz);
    return TIME_ZONE_ID_UNKNOWN;
}
static BOOL WINAPI hk_FileTimeToLocalFileTime(const FILETIME *in, LPFILETIME out) {
    *out = *in;
    return TRUE;
}
static void WINAPI hk_GetLocalTimeWall(LPSYSTEMTIME st) { GetSystemTime(st); }

/* ---------------------------------------------------------------- locale */

static LCID map_lcid(LCID l) { return (l == 0 || l == LOCALE_USER_DEFAULT || l == LOCALE_SYSTEM_DEFAULT) ? lcid : l; }
static UINT map_cp(UINT cp) { return (cp == CP_ACP || cp == CP_THREAD_ACP) ? acp : cp == CP_OEMCP ? oemcp : cp; }

static LCID WINAPI hk_GetUserDefaultLCID(void) { return lcid; }
static UINT WINAPI hk_GetACP(void) { return acp; }
static UINT WINAPI hk_GetOEMCP(void) { return oemcp; }
static BOOL (WINAPI *r_GetCPInfo)(UINT, LPCPINFO);
static BOOL WINAPI hk_GetCPInfo(UINT cp, LPCPINFO ci) { return r_GetCPInfo(map_cp(cp), ci); }
static int (WINAPI *r_GetLocaleInfoA)(LCID, LCTYPE, LPSTR, int);
static int WINAPI hk_GetLocaleInfoA(LCID l, LCTYPE t, LPSTR b, int n) { return r_GetLocaleInfoA(map_lcid(l), t, b, n); }
static int (WINAPI *r_GetLocaleInfoW)(LCID, LCTYPE, LPWSTR, int);
static int WINAPI hk_GetLocaleInfoW(LCID l, LCTYPE t, LPWSTR b, int n) { return r_GetLocaleInfoW(map_lcid(l), t, b, n); }
static int (WINAPI *r_LCMapStringA)(LCID, DWORD, LPCSTR, int, LPSTR, int);
static int WINAPI hk_LCMapStringA(LCID l, DWORD f, LPCSTR s, int sn, LPSTR d, int dn) {
    return r_LCMapStringA(map_lcid(l), f, s, sn, d, dn);
}
static int (WINAPI *r_LCMapStringW)(LCID, DWORD, LPCWSTR, int, LPWSTR, int);
static int WINAPI hk_LCMapStringW(LCID l, DWORD f, LPCWSTR s, int sn, LPWSTR d, int dn) {
    return r_LCMapStringW(map_lcid(l), f, s, sn, d, dn);
}
static int (WINAPI *r_CompareStringA)(LCID, DWORD, LPCSTR, int, LPCSTR, int);
static int WINAPI hk_CompareStringA(LCID l, DWORD f, LPCSTR a, int an, LPCSTR b, int bn) {
    return r_CompareStringA(map_lcid(l), f, a, an, b, bn);
}
static int (WINAPI *r_CompareStringW)(LCID, DWORD, LPCWSTR, int, LPCWSTR, int);
static int WINAPI hk_CompareStringW(LCID l, DWORD f, LPCWSTR a, int an, LPCWSTR b, int bn) {
    return r_CompareStringW(map_lcid(l), f, a, an, b, bn);
}
static BOOL (WINAPI *r_GetStringTypeA)(LCID, DWORD, LPCSTR, int, LPWORD);
static BOOL WINAPI hk_GetStringTypeA(LCID l, DWORD t, LPCSTR s, int n, LPWORD out) {
    return r_GetStringTypeA(map_lcid(l), t, s, n, out);
}

/* ---------------------------------------------------------------- paths */

static DWORD (WINAPI *r_GetCurrentDirectoryA)(DWORD, LPSTR);
static DWORD WINAPI hk_GetCurrentDirectoryA(DWORD cap, LPSTR buf) {
    char t[MAX_PATH + 8];
    DWORD n = r_GetCurrentDirectoryA(MAX_PATH, t);
    if (!n || n >= MAX_PATH) return n;
    r2v(t, sizeof t);
    return put_path(t, buf, cap);
}

static DWORD (WINAPI *r_GetFullPathNameA)(LPCSTR, DWORD, LPSTR, LPSTR *);
static DWORD WINAPI hk_GetFullPathNameA(LPCSTR in, DWORD cap, LPSTR buf, LPSTR *file) {
    char m[MAX_PATH + 8], t[MAX_PATH + 8];
    DWORD n = r_GetFullPathNameA(bound_v2r(in, m, sizeof m), MAX_PATH, t, NULL);
    if (!n || n >= MAX_PATH) return n;
    r2v(t, sizeof t);
    DWORD r = put_path(t, buf, cap);
    if (file && buf && r < cap) {
        char *f = 0;
        for (char *p = buf; *p; p++) if (*p == '\\' || *p == '/') f = p + 1;
        *file = (f && *f) ? f : NULL;
    }
    return r;
}

#define MAPA(p) bound_v2r((p), m_##p, sizeof m_##p)
static DWORD (WINAPI *r_GetFileAttributesA)(LPCSTR);
static DWORD WINAPI hk_GetFileAttributesA(LPCSTR p) { char m_p[MAX_PATH + 8]; return r_GetFileAttributesA(MAPA(p)); }
static BOOL (WINAPI *r_SetFileAttributesA)(LPCSTR, DWORD);
/* g_file_touch: these path calls can change the stage files (counted whether or not they succeed) */
static BOOL WINAPI hk_SetFileAttributesA(LPCSTR p, DWORD a) { char m_p[MAX_PATH + 8]; g_file_touch++; return r_SetFileAttributesA(MAPA(p), a); }
static BOOL (WINAPI *r_DeleteFileA)(LPCSTR);
static BOOL WINAPI hk_DeleteFileA(LPCSTR p) { char m_p[MAX_PATH + 8]; g_file_touch++; return r_DeleteFileA(MAPA(p)); }
static BOOL (WINAPI *r_CreateDirectoryA)(LPCSTR, LPSECURITY_ATTRIBUTES);
static BOOL WINAPI hk_CreateDirectoryA(LPCSTR p, LPSECURITY_ATTRIBUTES sa) { char m_p[MAX_PATH + 8]; g_file_touch++; return r_CreateDirectoryA(MAPA(p), sa); }
static BOOL (WINAPI *r_RemoveDirectoryA)(LPCSTR);
static BOOL WINAPI hk_RemoveDirectoryA(LPCSTR p) { char m_p[MAX_PATH + 8]; g_file_touch++; return r_RemoveDirectoryA(MAPA(p)); }
static BOOL (WINAPI *r_CopyFileA)(LPCSTR, LPCSTR, BOOL);
static BOOL WINAPI hk_CopyFileA(LPCSTR p, LPCSTR q, BOOL f) {
    char m_p[MAX_PATH + 8], m_q[MAX_PATH + 8];
    g_file_touch++;
    return r_CopyFileA(MAPA(p), MAPA(q), f);
}
static BOOL (WINAPI *r_MoveFileA)(LPCSTR, LPCSTR);
static BOOL WINAPI hk_MoveFileA(LPCSTR p, LPCSTR q) {
    char m_p[MAX_PATH + 8], m_q[MAX_PATH + 8];
    g_file_touch++;
    return r_MoveFileA(MAPA(p), MAPA(q));
}
/* version.dll: GetGameVersionInfo reads the exe's version resource through the module path */
static DWORD (WINAPI *r_GetFileVersionInfoSizeA)(LPCSTR, LPDWORD);
static DWORD WINAPI hk_GetFileVersionInfoSizeA(LPCSTR p, LPDWORD h) { char m_p[MAX_PATH + 8]; return r_GetFileVersionInfoSizeA(MAPA(p), h); }
static BOOL (WINAPI *r_GetFileVersionInfoA)(LPCSTR, DWORD, DWORD, LPVOID);
static BOOL WINAPI hk_GetFileVersionInfoA(LPCSTR p, DWORD h, DWORD n, LPVOID d) { char m_p[MAX_PATH + 8]; return r_GetFileVersionInfoA(MAPA(p), h, n, d); }
#undef MAPA

/* ---------------------------------------------------------------- GL identity */

#define GLAPI __attribute__((stdcall))
static const u8 *(GLAPI *r_glGetString)(u32);
static const char *(WINAPI *r_wglExtArb)(HDC);
static const char *(WINAPI *r_wglExtExt)(void);

/* Every token of want must be a token of have (the boot check of contract section 7). */
static void check_tokens(const char *what, const char *want, const char *have) {
    if (!have) { slog("boundary: %s: the driver gave no string", what); g_bnd_gl_missing++; return; }
    const char *w = want;
    while (*w) {
        while (*w == ' ') w++;
        const char *s = w;
        while (*w && *w != ' ') w++;
        u32 n = (u32)(w - s);
        if (!n) break;
        int found = 0;
        for (const char *h = have; *h && !found;) {
            while (*h == ' ') h++;
            const char *t = h;
            while (*h && *h != ' ') h++;
            if ((u32)(h - t) == n && k_strneq(t, s, n)) found = 1;
        }
        if (!found) {
            char tok[96];
            scopy(tok, sizeof tok, s, n);
            slog("boundary: %s %s is advertised but the driver lacks it", what, tok);
            g_bnd_gl_missing++;
        }
    }
}

static const u8 *GLAPI hk_glGetString(u32 name) {
    static int checked;
    switch (name) {
    case 0x1F00: return (const u8 *)B->gl_vendor;
    case 0x1F01: return (const u8 *)B->gl_renderer;
    case 0x1F02: return (const u8 *)B->gl_version;
    case 0x1F03:
        if (!checked) {
            checked = 1;
            u32 fp=math_host_begin();
            const u8 *real = r_glGetString(0x1F03);
            math_host_end("glGetString(extensions)",fp);
            slog("boundary: GL real vendor=%s renderer=%s version=%s", (const char *)r_glGetString(0x1F00),
                 (const char *)r_glGetString(0x1F01), (const char *)r_glGetString(0x1F02));
            check_tokens("GL", B->gl_ext, (const char *)real);
        }
        return (const u8 *)B->gl_ext;
    }
    return r_glGetString(name);
}

static void (GLAPI *r_glGetIntegerv)(u32, i32 *);
static void (GLAPI *r_glGetFloatv)(u32, float *);

static glq_t *glq_find(u32 e) {
    for (u32 i = 0; i < g_nglq; i++)
        if (g_glq[i].e == e) return &g_glq[i];
    return 0;
}
/* first use of a served limit: the real driver must back it */
static void glq_check(glq_t *g) {
    if (g->logged) return;
    g->logged = 1;
    if (!g->n) { slog("boundary: GL query %x served as unsupported", g->e); return; }
    i32 real[16];
    memset(real, 0, sizeof real);
    btrace_point("glq.driver.entry",40);
    r_glGetIntegerv(g->e, real);
    btrace_point("glq.driver.return",41);
    slog("boundary: GL query %x served %d, driver %d", g->e, g->v[0], real[0]);
    btrace_point("glq.log.done",42);
    for (u32 i = 0; i < g->n; i++)
        if (real[i] < g->v[i]) { slog("boundary: GL query %x: the driver cannot back %d", g->e, g->v[i]); g_bnd_gl_missing++; }
}
/* An unsupported enum leaves params alone and sets GL_INVALID_ENUM, on any driver: enum 0 is invalid everywhere. */
static void glq_unsupported(void) {
    i32 scratch[16];
    r_glGetIntegerv(0, scratch);
}
static void GLAPI hk_glGetIntegerv(u32 e, i32 *out) {
    glq_t *g = glq_find(e);
    if (!g) { r_glGetIntegerv(e, out); return; }
    glq_check(g);
    if (!g->n) { glq_unsupported(); return; }
    for (u32 i = 0; i < g->n; i++) out[i] = g->v[i];
}
static void GLAPI hk_glGetFloatv(u32 e, float *out) {
    /* Query values are returned through out; host FP/CPU-dispatch flags are
     * not query results. Preserve callbacks' deliberate environment changes. */
    glq_t *g = glq_find(e);
    if (!g) r_glGetFloatv(e, out);
    else {
        glq_check(g);
        if (!g->n) glq_unsupported();
        else for (u32 i = 0; i < g->n; i++) out[i] = (float)g->v[i];
    }
    btrace_point("glq.float.done",43);
}

static const char *WINAPI w_wglGetExtensionsStringARB(HDC dc) {
    static int checked;
    if (!checked && r_wglExtArb) { checked = 1; check_tokens("WGL", B->wgl_ext, r_wglExtArb(dc)); }
    return B->wgl_ext;
}
static const char *WINAPI w_wglGetExtensionsStringEXT(void) { return B->wgl_ext; }

/* gl.c's wglGetProcAddress hook asks here first: the WGL extension string is served. */
void *bound_gl_proc(const char *name, void *real) {
    if (g_bnd_native || !real || !B) return 0;
    if (k_streq(name, "wglGetExtensionsStringARB")) { r_wglExtArb = reinterpret_cast<decltype(r_wglExtArb)>(real); return (void *)w_wglGetExtensionsStringARB; }
    if (k_streq(name, "wglGetExtensionsStringEXT")) { r_wglExtExt = reinterpret_cast<decltype(r_wglExtExt)>(real); return (void *)w_wglGetExtensionsStringEXT; }
    return 0;
}

/* ---------------------------------------------------------------- GenSeed */

#define ADDR_GENSEED 0x005ed080u
void *t_genseed;
u8 g_genseed_seen[128];         /* the bytes GenSeed would have copied (diagnostics) */
u32 g_genseed_calls, g_gs_eax, g_gs_ecx, g_gs_edi, g_gs_esi;

/* At entry ESP points at the return address. GenSeed's local_80 is [esp-0x80, esp):
 * keep a copy for the log, then zero [esp-0x400, esp). No stack is used here. */
__attribute__((naked, used)) void st_genseed(void) {
    __asm__ volatile("movl %eax, _g_gs_eax\n\t"
                     "movl %ecx, _g_gs_ecx\n\t"
                     "movl %edi, _g_gs_edi\n\t"
                     "movl %esi, _g_gs_esi\n\t"
                     "cld\n\t"
                     "leal -0x80(%esp), %esi\n\t"
                     "movl $_g_genseed_seen, %edi\n\t"
                     "movl $0x20, %ecx\n\t"
                     "rep movsl\n\t"
                     "leal -0x400(%esp), %edi\n\t"
                     "movl $0x100, %ecx\n\t"
                     "xorl %eax, %eax\n\t"
                     "rep stosl\n\t"
                     "incl _g_genseed_calls\n\t"
                     "movl _g_gs_esi, %esi\n\t"
                     "movl _g_gs_edi, %edi\n\t"
                     "movl _g_gs_ecx, %ecx\n\t"
                     "movl _g_gs_eax, %eax\n\t"
                     "jmp *_t_genseed\n\t");
}

/* After the first gate: the residue GenSeed saw, for comparing runs. */
void bound_log_genseed(void) {
    static int done;
    if (done || !g_genseed_calls) return;
    done = 1;
    char line[400];
    buf_t b;
    b_init(&b, line, sizeof line);
    b_str(&b, "boundary: GenSeed calls=");
    b_u64(&b, g_genseed_calls);
    b_str(&b, " stack residue zeroed, was ");
    b_hex(&b, g_genseed_seen, sizeof g_genseed_seen);
    b_term(&b);
    slog("%s", line);
}

/* ---------------------------------------------------------------- install */

int bound_init_early(void) {
    static char real[sizeof g_bnd_text];
    const char *src = DEFAULT_BOUNDARY;
    if (GetEnvironmentVariableA("KPTAS_BOUNDARY", real, sizeof real)) src = real;
    scopy(g_bnd_text, sizeof g_bnd_text, src, (u32)strlen(src));
    sha256(g_bnd_text, (u32)strlen(g_bnd_text), g_bnd_sha);
    if (k_streq(g_bnd_text, "native")) {
        g_bnd_native = 1;
        slog("boundary: native (the real values pass through)");
        return 1;
    }
    u32 need = ((u32)sizeof(bnd_block) + 4095u) & ~4095u;
    u32 at = K->cfg.hg_base + BND_OFF;
    if (BND_OFF + need > K->cfg.hg_size) { slog("boundary block does not fit harness_game"); return 0; }
    if (VirtualAlloc((void *)(uintptr_t)at, need, MEM_COMMIT, PAGE_READWRITE) != (void *)(uintptr_t)at) {
        slog("boundary block commit failed");
        return 0;
    }
    B = (bnd_block *)(uintptr_t)at;
    B->magic = BND_MAGIC;
    B->version = 1;
    bound_parse(DEFAULT_BOUNDARY);   /* every key has a value even if the given string omits it */
    if (src != DEFAULT_BOUNDARY) bound_parse(src);
    u32 n = (u32)strlen(B->install);
    while (n > 3 && (B->install[n - 1] == '\\' || B->install[n - 1] == '/')) B->install[--n] = 0;
    buf_t b;
    b_init(&b, B->module, sizeof B->module);
    b_str(&b, B->install);
    b_str(&b, "\\swkotor.exe");
    b_term(&b);
    g_real_root_n = GetCurrentDirectoryA(sizeof g_real_root, g_real_root);
    if (g_real_root_n >= sizeof g_real_root) g_real_root_n = 0;
    while (g_real_root_n > 3 && g_real_root[g_real_root_n - 1] == '\\') g_real_root[--g_real_root_n] = 0;
    build_env();
    build_cmdline();

    /* the real values the boundary hides (diagnostics; the log is harness-side) */
    char t[MAX_PATH], u[64], c[64], z[64];
    SYSTEM_INFO si;
    TIME_ZONE_INFORMATION tz;
    GetModuleFileNameA(NULL, t, sizeof t);
    GetSystemInfo(&si);
    DWORD tzr = GetTimeZoneInformation(&tz);
    if (!GetEnvironmentVariableA("USER", u, sizeof u)) u[0] = 0;
    if (!GetEnvironmentVariableA("COMPUTERNAME", c, sizeof c)) c[0] = 0;
    if (!GetEnvironmentVariableA("TZ", z, sizeof z)) z[0] = 0;
    slog("boundary: real exe=%s cwd=%s user=%s computer=%s TZ=%s tz_bias=%d (%u) cpus=%u level=%u rev=%x cmdline=%s",
         t, g_real_root, u, c, z, (int)tz.Bias, tzr, si.dwNumberOfProcessors, si.wProcessorLevel,
         si.wProcessorRevision, GetCommandLineA());
    slog("boundary: served os=%u.%u.%u lcid=%x acp=%u oemcp=%u cpus=%u level=%u rev=%x install=%s user=%s computer=%s",
         os_major, os_minor, os_build, lcid, acp, oemcp, cpus, cpu_level, cpu_rev, B->install, B->user, B->computer);
    slog("boundary: block %x, cmdline %s", at, B->cmdline);
    return 1;
}

#define HOOK(dll, fn, h, save)                                               \
    do {                                                                     \
        void *o = iat_hook(exe, dll, fn, (void *)(h));                       \
        if (o) { if (save) *(void **)(save) = o; hooks_note("exe:" fn); }    \
        else slog("iat miss exe %s!%s", dll, fn);                            \
    } while (0)

void bound_install(void) {
    static const u8 e_gs[6] = {0x81, 0xec, 0xac, 0x00, 0x00, 0x00};
    static const u8 l_gs[] = {6, 0};
    if (detour(ADDR_GENSEED, e_gs, 6, l_gs, (void *)st_genseed, &t_genseed)) hooks_note("exe:GenSeed");
    else slog("detour GenSeed failed");
    if (g_bnd_native) return;
    HMODULE exe = GetModuleHandleA(NULL);
    const char *k = "KERNEL32.dll";
    HOOK(k, "GetCommandLineA", hk_GetCommandLineA, 0);
    HOOK(k, "GetModuleHandleA", hk_GetModuleHandleA, 0);
    HOOK(k, "SetPriorityClass", hk_SetPriorityClass, 0);
    HOOK("OLE32.dll", "CoInitialize", hk_CoInitialize, &r_CoInitialize);
    HOOK(k, "GetModuleFileNameA", hk_GetModuleFileNameA, &r_GetModuleFileNameA);
    HOOK(k, "GetEnvironmentStrings", hk_GetEnvironmentStrings, 0);
    HOOK(k, "GetEnvironmentStringsW", hk_GetEnvironmentStringsW, 0);
    HOOK(k, "FreeEnvironmentStringsA", hk_FreeEnvironmentStringsA, &r_FreeEnvironmentStringsA);
    HOOK(k, "FreeEnvironmentStringsW", hk_FreeEnvironmentStringsW, &r_FreeEnvironmentStringsW);
    HOOK(k, "GetSystemInfo", hk_GetSystemInfo, 0);
    HOOK(k, "GetVersionExA", hk_GetVersionExA, 0);
    HOOK(k, "GetStartupInfoA", hk_GetStartupInfoA, 0);
    HOOK(k, "GetStdHandle", hk_GetStdHandle, 0);
    HOOK(k, "GetTimeZoneInformation", hk_GetTimeZoneInformation, 0);
    HOOK(k, "FileTimeToLocalFileTime", hk_FileTimeToLocalFileTime, 0);
    if (g_clk.mode != 0) HOOK(k, "GetLocalTime", hk_GetLocalTimeWall, 0);   /* virtual mode: clock.c, also UTC */
    HOOK(k, "GetUserDefaultLCID", hk_GetUserDefaultLCID, 0);
    HOOK(k, "GetACP", hk_GetACP, 0);
    HOOK(k, "GetOEMCP", hk_GetOEMCP, 0);
    HOOK(k, "GetCPInfo", hk_GetCPInfo, &r_GetCPInfo);
    HOOK(k, "GetLocaleInfoA", hk_GetLocaleInfoA, &r_GetLocaleInfoA);
    HOOK(k, "GetLocaleInfoW", hk_GetLocaleInfoW, &r_GetLocaleInfoW);
    HOOK(k, "LCMapStringA", hk_LCMapStringA, &r_LCMapStringA);
    HOOK(k, "LCMapStringW", hk_LCMapStringW, &r_LCMapStringW);
    HOOK(k, "CompareStringA", hk_CompareStringA, &r_CompareStringA);
    HOOK(k, "CompareStringW", hk_CompareStringW, &r_CompareStringW);
    HOOK(k, "GetStringTypeA", hk_GetStringTypeA, &r_GetStringTypeA);
    HOOK(k, "GetCurrentDirectoryA", hk_GetCurrentDirectoryA, &r_GetCurrentDirectoryA);
    HOOK(k, "GetFullPathNameA", hk_GetFullPathNameA, &r_GetFullPathNameA);
    HOOK(k, "GetFileAttributesA", hk_GetFileAttributesA, &r_GetFileAttributesA);
    HOOK(k, "SetFileAttributesA", hk_SetFileAttributesA, &r_SetFileAttributesA);
    HOOK(k, "DeleteFileA", hk_DeleteFileA, &r_DeleteFileA);
    HOOK(k, "CreateDirectoryA", hk_CreateDirectoryA, &r_CreateDirectoryA);
    HOOK(k, "RemoveDirectoryA", hk_RemoveDirectoryA, &r_RemoveDirectoryA);
    HOOK(k, "CopyFileA", hk_CopyFileA, &r_CopyFileA);
    HOOK(k, "MoveFileA", hk_MoveFileA, &r_MoveFileA);
    HOOK("VERSION.dll", "GetFileVersionInfoSizeA", hk_GetFileVersionInfoSizeA, &r_GetFileVersionInfoSizeA);
    HOOK("VERSION.dll", "GetFileVersionInfoA", hk_GetFileVersionInfoA, &r_GetFileVersionInfoA);
    HOOK("OPENGL32.dll", "glGetString", hk_glGetString, &r_glGetString);
    HOOK("OPENGL32.dll", "glGetIntegerv", hk_glGetIntegerv, &r_glGetIntegerv);
    HOOK("OPENGL32.dll", "glGetFloatv", hk_glGetFloatv, &r_glGetFloatv);
}

/* GetProcAddress-resolved names (clock.c's hook asks here). */
FARPROC bound_proc(const char *name) {
    if (g_bnd_native || !B) return 0;
    static const struct { const char *n; void *f; } T[] = {
        {"GetCommandLineA", (void *)hk_GetCommandLineA}, {"GetCommandLineW", (void *)hk_GetCommandLineW},
        {"GetModuleHandleA", (void *)hk_GetModuleHandleA}, {"SetPriorityClass", (void *)hk_SetPriorityClass},
        {"GetModuleFileNameA", (void *)hk_GetModuleFileNameA}, {"GetModuleFileNameW", (void *)hk_GetModuleFileNameW},
        {"GetEnvironmentStrings", (void *)hk_GetEnvironmentStrings}, {"GetEnvironmentStringsA", (void *)hk_GetEnvironmentStrings},
        {"GetEnvironmentStringsW", (void *)hk_GetEnvironmentStringsW},
        {"GetEnvironmentVariableA", (void *)hk_GetEnvironmentVariableA},
        {"GetEnvironmentVariableW", (void *)hk_GetEnvironmentVariableW},
        {"GetSystemInfo", (void *)hk_GetSystemInfo}, {"GetNativeSystemInfo", (void *)hk_GetSystemInfo},
        {"GetVersionExA", (void *)hk_GetVersionExA}, {"GetStartupInfoA", (void *)hk_GetStartupInfoA},
        {"GetTimeZoneInformation", (void *)hk_GetTimeZoneInformation},
        {"GetComputerNameA", (void *)hk_GetComputerNameA}, {"GetComputerNameW", (void *)hk_GetComputerNameW},
        {"GetUserNameA", (void *)hk_GetUserNameA}, {"GetUserNameW", (void *)hk_GetUserNameW},
        {"GetUserDefaultLCID", (void *)hk_GetUserDefaultLCID}, {"GetACP", (void *)hk_GetACP},
        {"GetOEMCP", (void *)hk_GetOEMCP}, {0, 0},
    };
    for (u32 i = 0; T[i].n; i++)
        if (k_streq(name, T[i].n)) return (FARPROC)T[i].f;
    if (k_streq(name, "GetCurrentDirectoryA") && r_GetCurrentDirectoryA) return (FARPROC)hk_GetCurrentDirectoryA;
    if (k_streq(name, "GetFullPathNameA") && r_GetFullPathNameA) return (FARPROC)hk_GetFullPathNameA;
    return 0;
}

/* hello: {"mode":"profile"|"native","sha256":<sha256 of the KPTAS_BOUNDARY text served>,"gl_missing":n} */
void bound_hello_json(buf_t *b) {
    b_str(b, "{\"gl_missing\":");
    b_u64(b, g_bnd_gl_missing);
    b_str(b, ",\"mode\":\"");
    b_str(b, g_bnd_native ? "native" : "profile");
    b_str(b, "\",\"sha256\":\"");
    b_hex(b, g_bnd_sha, 32);
    b_str(b, "\"}");
}

} /* extern "C" */
