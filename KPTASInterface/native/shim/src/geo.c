/* Display, window and cursor geometry the game reads from the host.
 *
 * The X screen size and window placement are host facts. The game reads
 * them through the display-mode list (EnumDisplaySettingsA), the desktop
 * rect (SetVideoMode 0x5f1830: fullscreen when the desktop is no larger than
 * the mode), the monitor rect, window rects and the cursor, and Wine sizes
 * CW_USEDEFAULT windows from the screen. On a 1280x1024 screen that gave a
 * 1280x1024 GUI and a different virtual cursor, so the first click missed.
 *
 * Here the game sees a virtual display instead: one 1024x768 desktop at the
 * origin with the modes 1024x768x32/16/8, the same values a 1024x768 X
 * screen gives. Window and cursor coordinates go through a virtual render
 * window whose client origin is fixed at the desktop origin; the real
 * cursor is never warped. ChangeDisplaySettingsA passes the host's own mode,
 * so the host display never changes and Wine still sends the game its
 * WM_DISPLAYCHANGE. The game's own SetWindowPos and MoveWindow calls pass
 * through: their arguments come from the pinned values.
 *
 * The wire verb log {"geo":1} logs each call with its arguments and results. */
#include "shim.h"

#define VW 1024
#define VH 768
#define VX 0                 /* render window client origin, screen coordinates */
#define VY 0
static const u32 VBPP[3] = {32, 16, 8};

static int g_geo_log;
static u32 g_geo_n[32];
#define GLOG(i, ...) do { if (g_geo_log && g_geo_n[i]++ < 60) slog(__VA_ARGS__); } while (0)

static BOOL (WINAPI *r_ClientToScreen)(HWND, LPPOINT);
static BOOL (WINAPI *r_ScreenToClient)(HWND, LPPOINT);
static BOOL (WINAPI *r_GetCursorInfo)(PCURSORINFO);
static BOOL (WINAPI *r_GetWindowRect)(HWND, LPRECT);
static BOOL (WINAPI *r_GetWindowInfo)(HWND, PWINDOWINFO);
static BOOL (WINAPI *r_GetMonitorInfoA)(HMONITOR, LPMONITORINFO);
static HMONITOR (WINAPI *r_MonitorFromWindow)(HWND, DWORD);
static BOOL (WINAPI *r_MoveWindow)(HWND, int, int, int, int, BOOL);
static BOOL (WINAPI *r_SetWindowPos)(HWND, HWND, int, int, int, int, UINT);
static HWND (WINAPI *r_CreateWindowExA)(DWORD, LPCSTR, LPCSTR, DWORD, int, int, int, int, HWND, HMENU, HINSTANCE, LPVOID);
static BOOL (WINAPI *r_AdjustWindowRect)(LPRECT, DWORD, BOOL);
static BOOL (WINAPI *r_EnumDisplaySettingsA)(LPCSTR, DWORD, DEVMODEA *);
static LONG (WINAPI *r_ChangeDisplaySettingsA)(DEVMODEA *, DWORD);
static HWND (WINAPI *r_GetDesktopWindow)(void);
static BOOL (WINAPI *r_ShowWindow)(HWND, int);
static int (WINAPI *r_ShowCursor)(BOOL);
static BOOL (WINAPI *r_SetCursorPos)(int, int);

static u32 T(void) { return (u32)g_gate.tick; }
static HWND render_window(void) { return *(HWND *)(uintptr_t)ADDR_RENDER_WINDOW; }

/* virtual minus real screen position of the render window's client origin */
static POINT delta(void) {
    POINT o = {0, 0}, d = {0, 0};
    HWND rw = render_window();
    if (rw && r_ClientToScreen(rw, &o)) { d.x = VX - o.x; d.y = VY - o.y; }
    return d;
}

static void hexdump(const char *what, const void *p, u32 n) {
    char line[800];
    buf_t b;
    b_init(&b, line, sizeof line);
    b_hex(&b, (const u8 *)p, n);
    b_term(&b);
    slog("geo   %s %s", what, line);
}

/* ---- coordinates ---- */
static BOOL WINAPI hk_ClientToScreen(HWND h, LPPOINT p) {
    int x = p->x, y = p->y;
    POINT d = delta();
    BOOL r = r_ClientToScreen(h, p);
    if (r) { p->x += d.x; p->y += d.y; }
    GLOG(0, "geo ClientToScreen t=%u h=%x (%d,%d) -> %d (%d,%d)", T(), (u32)(uintptr_t)h, x, y, r, p->x, p->y);
    return r;
}
static BOOL WINAPI hk_ScreenToClient(HWND h, LPPOINT p) {
    int x = p->x, y = p->y;
    POINT d = delta();
    p->x -= d.x;
    p->y -= d.y;
    BOOL r = r_ScreenToClient(h, p);
    GLOG(1, "geo ScreenToClient t=%u h=%x (%d,%d) -> %d (%d,%d)", T(), (u32)(uintptr_t)h, x, y, r, p->x, p->y);
    return r;
}
/* The game's cursor lives in the virtual DirectInput device (client
 * coordinates of the render window); the host cursor is never warped. */
static BOOL WINAPI hk_SetCursorPos(int x, int y) {
    di_set_cursor(x - VX, y - VY);
    GLOG(16, "geo SetCursorPos t=%u (%d,%d) -> client (%d,%d)", T(), x, y, x - VX, y - VY);
    return TRUE;
}
static BOOL WINAPI hk_GetCursorPos(LPPOINT p) {
    if (!p) return FALSE;
    p->x = SH->vcur_x + VX;
    p->y = SH->vcur_y + VY;
    GLOG(17, "geo GetCursorPos t=%u -> (%d,%d)", T(), p->x, p->y);
    return TRUE;
}
static BOOL WINAPI hk_GetCursorInfo(PCURSORINFO ci) {
    BOOL r = r_GetCursorInfo(ci);   /* flags and cursor handle follow the game's own ShowCursor/SetCursor */
    if (r) { ci->ptScreenPos.x = SH->vcur_x + VX; ci->ptScreenPos.y = SH->vcur_y + VY; }
    GLOG(2, "geo GetCursorInfo t=%u -> %d flags=%x cur=%x pt=(%d,%d)", T(), r, (u32)ci->flags,
         (u32)(uintptr_t)ci->hCursor, ci->ptScreenPos.x, ci->ptScreenPos.y);
    return r;
}
static BOOL WINAPI hk_ClipCursor(const RECT *rc) {
    GLOG(18, "geo ClipCursor t=%u %s", T(), rc ? "rect" : "none");
    return TRUE;                    /* no host effect */
}

/* ---- window and desktop rects ---- */
static void shift(RECT *rc, POINT d) { rc->left += d.x; rc->right += d.x; rc->top += d.y; rc->bottom += d.y; }

static BOOL WINAPI hk_GetWindowRect(HWND h, LPRECT rc) {
    BOOL r;
    if (h == r_GetDesktopWindow()) {
        rc->left = 0; rc->top = 0; rc->right = VW; rc->bottom = VH;
        r = TRUE;
    } else {
        POINT d = delta();
        r = r_GetWindowRect(h, rc);
        if (r) shift(rc, d);
    }
    GLOG(3, "geo GetWindowRect t=%u h=%x -> %d (%d,%d,%d,%d)", T(), (u32)(uintptr_t)h, r, rc->left, rc->top,
         rc->right, rc->bottom);
    return r;
}
static BOOL WINAPI hk_GetClientRect(HWND h, LPRECT rc) {
    BOOL r;
    if (h == r_GetDesktopWindow()) {
        rc->left = 0; rc->top = 0; rc->right = VW; rc->bottom = VH;
        r = TRUE;
    } else r = GetClientRect(h, rc);   /* client rects start at 0,0: no placement in them */
    GLOG(19, "geo GetClientRect t=%u h=%x -> %d (%d,%d)", T(), (u32)(uintptr_t)h, r, rc->right, rc->bottom);
    return r;
}
static BOOL WINAPI hk_GetWindowInfo(HWND h, PWINDOWINFO wi) {
    POINT d = delta();
    BOOL r = r_GetWindowInfo(h, wi);
    if (r) { shift(&wi->rcWindow, d); shift(&wi->rcClient, d); }
    GLOG(4, "geo GetWindowInfo t=%u h=%x -> %d win=(%d,%d,%d,%d) cli=(%d,%d,%d,%d) style=%x ex=%x", T(),
         (u32)(uintptr_t)h, r, wi->rcWindow.left, wi->rcWindow.top, wi->rcWindow.right, wi->rcWindow.bottom,
         wi->rcClient.left, wi->rcClient.top, wi->rcClient.right, wi->rcClient.bottom, (u32)wi->dwStyle,
         (u32)wi->dwExStyle);
    return r;
}
static BOOL WINAPI hk_GetMonitorInfoA(HMONITOR m, LPMONITORINFO mi) {
    BOOL r = r_GetMonitorInfoA(m, mi);
    if (r) {
        mi->rcMonitor.left = mi->rcWork.left = 0;
        mi->rcMonitor.top = mi->rcWork.top = 0;
        mi->rcMonitor.right = mi->rcWork.right = VW;
        mi->rcMonitor.bottom = mi->rcWork.bottom = VH;
        mi->dwFlags = MONITORINFOF_PRIMARY;
    }
    GLOG(5, "geo GetMonitorInfoA t=%u m=%x cb=%u -> %d mon=(%d,%d,%d,%d) work=(%d,%d,%d,%d) flags=%x", T(),
         (u32)(uintptr_t)m, (u32)mi->cbSize, r, mi->rcMonitor.left, mi->rcMonitor.top, mi->rcMonitor.right,
         mi->rcMonitor.bottom, mi->rcWork.left, mi->rcWork.top, mi->rcWork.right, mi->rcWork.bottom,
         (u32)mi->dwFlags);
    return r;
}
static int WINAPI hk_GetSystemMetrics(int i) {
    int r = i == SM_CXSCREEN ? VW : i == SM_CYSCREEN ? VH : GetSystemMetrics(i);
    GLOG(20, "geo GetSystemMetrics t=%u %d -> %d", T(), i, r);
    return r;
}
static HMONITOR WINAPI hk_MonitorFromWindow(HWND h, DWORD f) {
    HMONITOR r = r_MonitorFromWindow(h, f);
    GLOG(6, "geo MonitorFromWindow t=%u h=%x f=%x -> %x", T(), (u32)(uintptr_t)h, (u32)f, (u32)(uintptr_t)r);
    return r;
}

/* ---- window placement ---- */
static BOOL WINAPI hk_MoveWindow(HWND h, int x, int y, int w, int hh, BOOL rp) {
    BOOL r = r_MoveWindow(h, x, y, w, hh, rp);
    GLOG(7, "geo MoveWindow t=%u h=%x (%d,%d %dx%d) rp=%d -> %d", T(), (u32)(uintptr_t)h, x, y, w, hh, rp, r);
    return r;
}
static BOOL WINAPI hk_SetWindowPos(HWND h, HWND after, int x, int y, int w, int hh, UINT f) {
    BOOL r = r_SetWindowPos(h, after, x, y, w, hh, f);
    GLOG(8, "geo SetWindowPos t=%u h=%x after=%x (%d,%d %dx%d) f=%x -> %d", T(), (u32)(uintptr_t)h,
         (u32)(uintptr_t)after, x, y, w, hh, f, r);
    return r;
}
/* Wine sizes a CW_USEDEFAULT window at 3/4 of the screen: use the virtual screen. */
static HWND WINAPI hk_CreateWindowExA(DWORD ex, LPCSTR cls, LPCSTR name, DWORD style, int x, int y, int w, int hh,
                                      HWND parent, HMENU menu, HINSTANCE inst, LPVOID param) {
    if (!parent && x == (int)CW_USEDEFAULT) { x = 0; y = 0; }
    if (!parent && w == (int)CW_USEDEFAULT) { w = VW * 3 / 4; hh = VH * 3 / 4; }
    HWND r = r_CreateWindowExA(ex, cls, name, style, x, y, w, hh, parent, menu, inst, param);
    GLOG(9, "geo CreateWindowExA t=%u cls=%s ex=%x style=%x (%d,%d %dx%d) parent=%x -> %x", T(),
         ((uintptr_t)cls > 0xffff) ? cls : "#atom", (u32)ex, (u32)style, x, y, w, hh, (u32)(uintptr_t)parent,
         (u32)(uintptr_t)r);
    if (g_geo_log && r) {
        RECT wr, cr;
        r_GetWindowRect(r, &wr);
        GetClientRect(r, &cr);
        POINT o = {0, 0};
        r_ClientToScreen(r, &o);
        slog("geo   window %x real=(%d,%d,%d,%d) client=%dx%d origin=(%d,%d)", (u32)(uintptr_t)r, wr.left, wr.top,
             wr.right, wr.bottom, cr.right, cr.bottom, o.x, o.y);
    }
    return r;
}
static BOOL WINAPI hk_AdjustWindowRect(LPRECT rc, DWORD style, BOOL menu) {
    int l = rc->left, t = rc->top, r0 = rc->right, b = rc->bottom;
    BOOL r = r_AdjustWindowRect(rc, style, menu);
    GLOG(10, "geo AdjustWindowRect t=%u (%d,%d,%d,%d) style=%x -> %d (%d,%d,%d,%d)", T(), l, t, r0, b, (u32)style, r,
         rc->left, rc->top, rc->right, rc->bottom);
    return r;
}
static BOOL WINAPI hk_ShowWindow(HWND h, int c) {
    BOOL r = r_ShowWindow(h, c);
    GLOG(14, "geo ShowWindow t=%u h=%x cmd=%d -> %d", T(), (u32)(uintptr_t)h, c, r);
    return r;
}
static int WINAPI hk_ShowCursor(BOOL s) {
    int r = r_ShowCursor(s);
    GLOG(15, "geo ShowCursor t=%u %d -> %d", T(), s, r);
    return r;
}
static HWND WINAPI hk_GetDesktopWindow(void) {
    HWND r = r_GetDesktopWindow();
    GLOG(13, "geo GetDesktopWindow t=%u -> %x", T(), (u32)(uintptr_t)r);
    return r;
}

/* ---- display modes ----
 * The virtual list: VBPP[i] at VW x VH. Fields other than size and depth
 * come from the host's first mode (Wine's own values for its display). */
static BOOL host_mode(DEVMODEA *m) {
    memset(m, 0, sizeof *m);
    m->dmSize = sizeof *m;
    return r_EnumDisplaySettingsA(NULL, 0, m);
}

static BOOL virtual_mode(u32 i, DEVMODEA *dm) {
    DEVMODEA cur;
    if (!host_mode(&cur)) return FALSE;
    cur.dmPelsWidth = VW;
    cur.dmPelsHeight = VH;
    cur.dmBitsPerPel = VBPP[i];
    u32 n = dm->dmSize && dm->dmSize < sizeof cur ? dm->dmSize : sizeof cur;
    u16 extra = dm->dmDriverExtra;
    memcpy(dm, &cur, n);
    dm->dmSize = (u16)n;
    dm->dmDriverExtra = extra;
    return TRUE;
}

static BOOL WINAPI hk_EnumDisplaySettingsA(LPCSTR dev, DWORD i, DEVMODEA *dm) {
    BOOL r;
    if (dev) r = r_EnumDisplaySettingsA(dev, i, dm);
    else if (i == ENUM_CURRENT_SETTINGS || i == ENUM_REGISTRY_SETTINGS) r = virtual_mode(0, dm);
    else r = i < 3 ? virtual_mode(i, dm) : FALSE;
    GLOG(11, "geo EnumDisplaySettingsA t=%u dev=%s i=%d -> %d %ux%u bpp=%u hz=%u fields=%x", T(), dev ? dev : "-",
         (int)i, r, r ? (u32)dm->dmPelsWidth : 0, r ? (u32)dm->dmPelsHeight : 0, r ? (u32)dm->dmBitsPerPel : 0,
         r ? (u32)dm->dmDisplayFrequency : 0, r ? (u32)dm->dmFields : 0);
    if (g_geo_log && r && g_geo_n[11] <= 4) hexdump("devmode", dm, dm->dmSize);
    return r;
}
/* A mode from the virtual list is accepted; the host gets its own mode, so
 * it never changes. */
static LONG WINAPI hk_ChangeDisplaySettingsA(DEVMODEA *dm, DWORD f) {
    LONG r;
    if (!dm) r = r_ChangeDisplaySettingsA(dm, f);
    else {
        int ok = 0;
        for (u32 i = 0; i < 3; i++)
            if ((!(dm->dmFields & DM_PELSWIDTH) || dm->dmPelsWidth == VW) &&
                (!(dm->dmFields & DM_PELSHEIGHT) || dm->dmPelsHeight == VH) &&
                (!(dm->dmFields & DM_BITSPERPEL) || dm->dmBitsPerPel == VBPP[i]))
                ok = 1;
        if (!ok) r = DISP_CHANGE_BADMODE;
        else {
            DEVMODEA cur;
            r = host_mode(&cur) ? r_ChangeDisplaySettingsA(&cur, f) : DISP_CHANGE_FAILED;
        }
    }
    GLOG(12, "geo ChangeDisplaySettingsA t=%u dm=%x %ux%u bpp=%u fields=%x f=%x -> %d", T(), (u32)(uintptr_t)dm,
         dm ? (u32)dm->dmPelsWidth : 0, dm ? (u32)dm->dmPelsHeight : 0, dm ? (u32)dm->dmBitsPerPel : 0,
         dm ? (u32)dm->dmFields : 0, (u32)f, (int)r);
    return r;
}

/* GetProcAddress from the exe: every name is logged; geometry names are served here. */
FARPROC geo_proc(const char *name) {
    static u32 n;
    if (g_geo_log && n++ < 300) slog("geo GetProcAddress t=%u %s", T(), name);
    if (k_streq(name, "GetCursorPos")) return (FARPROC)hk_GetCursorPos;
    if (k_streq(name, "SetCursorPos")) return (FARPROC)hk_SetCursorPos;
    if (k_streq(name, "GetCursorInfo")) return (FARPROC)hk_GetCursorInfo;
    if (k_streq(name, "ClipCursor")) return (FARPROC)hk_ClipCursor;
    if (k_streq(name, "ClientToScreen")) return (FARPROC)hk_ClientToScreen;
    if (k_streq(name, "ScreenToClient")) return (FARPROC)hk_ScreenToClient;
    if (k_streq(name, "GetWindowRect")) return (FARPROC)hk_GetWindowRect;
    if (k_streq(name, "GetClientRect")) return (FARPROC)hk_GetClientRect;
    if (k_streq(name, "GetWindowInfo")) return (FARPROC)hk_GetWindowInfo;
    if (k_streq(name, "GetMonitorInfoA")) return (FARPROC)hk_GetMonitorInfoA;
    if (k_streq(name, "GetSystemMetrics")) return (FARPROC)hk_GetSystemMetrics;
    if (k_streq(name, "EnumDisplaySettingsA")) return (FARPROC)hk_EnumDisplaySettingsA;
    if (k_streq(name, "ChangeDisplaySettingsA")) return (FARPROC)hk_ChangeDisplaySettingsA;
    return 0;
}

void geo_set_log(int on) { g_geo_log = on; }

void geo_install(void) {
    HMODULE exe = GetModuleHandleA(NULL);
    void *o;
    /* the real calls, for the hooks the IAT walk does not set */
    r_GetDesktopWindow = GetDesktopWindow;
    r_ClientToScreen = ClientToScreen;
    r_ScreenToClient = ScreenToClient;
    r_GetWindowRect = GetWindowRect;
    r_GetWindowInfo = GetWindowInfo;
    r_GetMonitorInfoA = GetMonitorInfoA;
    r_GetCursorInfo = GetCursorInfo;
    r_EnumDisplaySettingsA = EnumDisplaySettingsA;
    r_ChangeDisplaySettingsA = ChangeDisplaySettingsA;
#define GHOOK(fn, h)                                                                     \
    do {                                                                                 \
        if ((o = iat_hook(exe, "USER32.dll", #fn, (void *)h))) { *(void **)&r_##fn = o; hooks_note("exe:" #fn); } \
        else slog("iat miss exe USER32.dll!%s", #fn);                                    \
    } while (0)
    GHOOK(ClientToScreen, hk_ClientToScreen);
    GHOOK(ScreenToClient, hk_ScreenToClient);
    GHOOK(GetCursorInfo, hk_GetCursorInfo);
    GHOOK(GetWindowRect, hk_GetWindowRect);
    GHOOK(GetWindowInfo, hk_GetWindowInfo);
    GHOOK(GetMonitorInfoA, hk_GetMonitorInfoA);
    GHOOK(MonitorFromWindow, hk_MonitorFromWindow);
    GHOOK(MoveWindow, hk_MoveWindow);
    GHOOK(SetWindowPos, hk_SetWindowPos);
    GHOOK(CreateWindowExA, hk_CreateWindowExA);
    GHOOK(AdjustWindowRect, hk_AdjustWindowRect);
    GHOOK(EnumDisplaySettingsA, hk_EnumDisplaySettingsA);
    GHOOK(ChangeDisplaySettingsA, hk_ChangeDisplaySettingsA);
    GHOOK(GetDesktopWindow, hk_GetDesktopWindow);
    GHOOK(ShowWindow, hk_ShowWindow);
    GHOOK(ShowCursor, hk_ShowCursor);
    GHOOK(SetCursorPos, hk_SetCursorPos);
#undef GHOOK
    (void)r_SetCursorPos;
}
