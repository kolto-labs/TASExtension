/* Message interposition (contract 5.3 step 1, [messages] defaults):
 *   real_pump = "gate_drain": only the gate pumps the real queue;
 *   wndproc   = "interpose":  the exe's window classes get our WndProc;
 *   external  = "defwindowproc_journal": messages the gate pump delivers go
 *               to DefWindowProcA (logged, not journalled, since their
 *               arrival follows real time);
 *   internal  = "pass_journal": messages caused by the game's own calls
 *               (sends from its API calls, and its own posts) reach the game
 *               WndProc and are journalled.
 * Every exe PeekMessageA other than the gate returns "no message", so the
 * UpdateScreen message pump (0x401510) never sees real-time input. */
#include "shim.h"

int g_in_gate_pump;
u32 g_msg_int_n, g_msg_ext_n;
volatile int g_quit_seen;
WNDPROC g_wndproc_orig[4];
static u32 g_nslots;
static blake2s_t g_msg_h;
static u32 g_ext_logged;

static ATOM (WINAPI *r_RegisterClassA)(const WNDCLASSA *);
static BOOL (WINAPI *r_PostMessageA)(HWND, UINT, WPARAM, LPARAM);
static BOOL (WINAPI *r_GetMessageA)(LPMSG, HWND, UINT, UINT);

/* Messages the game posted itself; matched and passed through by the gate pump.
 * The list and the synthesized score queue live in harness_game (shim_hg). */
#define g_posted (SH->posted)
#define g_posted_n (SH->posted_n)
#define g_synth (SH->synth)
#define g_synth_h (SH->synth_h)
#define g_synth_n (SH->synth_n)

void msg_tick_reset(void) {
    blake2s_init(&g_msg_h, 16);
    g_msg_int_n = 0;
    g_msg_ext_n = 0;
}

void msg_tick_digest(u8 out[16]) {
    blake2s_t c = g_msg_h;
    blake2s_final(&c, out);
}

static int take_posted(HWND h, UINT m, WPARAM w, LPARAM l) {
    for (u32 i = 0; i < g_posted_n; i++) {
        posted_t *p = &g_posted[i];
        if (p->h == h && p->m == m && p->w == w && p->l == l) {
            g_posted[i] = g_posted[--g_posted_n];
            return 1;
        }
    }
    return 0;
}

static LRESULT interp(u32 i, HWND h, UINT m, WPARAM w, LPARAM l) {
    if (g_in_gate_pump) {
        g_msg_ext_n++;
        if (g_ext_logged < 400) {
            g_ext_logged++;
            slog("ext msg tick=%u m=%x w=%x l=%x", (u32)g_gate.tick, m, (u32)w, (u32)l);
        }
        if (m == WM_CLOSE || m == WM_QUIT || m == WM_DESTROY) return 0;
        return DefWindowProcA(h, m, w, l);
    }
    g_msg_int_n++;
    static u32 g_int_logged;
    if (g_int_logged < 300) {
        g_int_logged++;
        slog("int msg tick=%u m=%x w=%x l=%x", (u32)g_gate.tick, m, (u32)w, (u32)l);
    }
    u32 rec[2] = {m, g_gate.started ? 1u : 0u};
    blake2s_update(&g_msg_h, rec, 4);
    return g_wndproc_orig[i](h, m, w, l);
}
static LRESULT CALLBACK interp0(HWND h, UINT m, WPARAM w, LPARAM l) { return interp(0, h, m, w, l); }
static LRESULT CALLBACK interp1(HWND h, UINT m, WPARAM w, LPARAM l) { return interp(1, h, m, w, l); }
static LRESULT CALLBACK interp2(HWND h, UINT m, WPARAM w, LPARAM l) { return interp(2, h, m, w, l); }
static LRESULT CALLBACK interp3(HWND h, UINT m, WPARAM w, LPARAM l) { return interp(3, h, m, w, l); }
static WNDPROC g_interp[4] = {interp0, interp1, interp2, interp3};

static ATOM WINAPI hk_RegisterClassA(const WNDCLASSA *wc) {
    u32 p = (u32)(uintptr_t)wc->lpfnWndProc;
    if (p >= EXE_TEXT_VA && p < EXE_TEXT_VA + EXE_TEXT_SIZE) {
        u32 slot = g_nslots;
        for (u32 i = 0; i < g_nslots; i++) if (g_wndproc_orig[i] == wc->lpfnWndProc) slot = i;
        if (slot < 4) {
            if (slot == g_nslots) { g_wndproc_orig[slot] = wc->lpfnWndProc; g_nslots++; }
            WNDCLASSA c = *wc;
            c.lpfnWndProc = g_interp[slot];
            slog("RegisterClassA %s wndproc=%x -> interposer %u",
                 ((uintptr_t)wc->lpszClassName > 0xffff) ? wc->lpszClassName : "#atom", p, slot);
            return r_RegisterClassA(&c);
        }
    }
    return r_RegisterClassA(wc);
}

static BOOL WINAPI hk_PostMessageA(HWND h, UINT m, WPARAM w, LPARAM l) {
    if (cur_rtid() == g_main_rtid && g_posted_n < 64) {
        posted_t *p = &g_posted[g_posted_n++];
        p->h = h; p->m = m; p->w = w; p->l = l;
    }
    return r_PostMessageA(h, m, w, l);
}

/* Synthesized score messages (contract 5.3 step 8): the gate PeekMessageA
 * returns the head without removing it, and WinMain's GetMessageA serves
 * the same message, which DispatchMessageA hands to the game WndProc. */

int msg_synth_push(UINT m, WPARAM w, LPARAM l) {
    if (g_synth_n >= 64) return 0;
    MSG *q = &g_synth[(g_synth_h + g_synth_n) % 64];
    memset(q, 0, sizeof *q);
    q->hwnd = *(HWND *)(uintptr_t)0x007a39d8u;  /* renderWindow */
    q->message = m;
    q->wParam = w;
    q->lParam = l;
    q->time = (DWORD)(clock_u() / 1000ull);
    q->pt.x = (LONG)(l & 0xffff);
    q->pt.y = (LONG)((l >> 16) & 0xffff);
    g_synth_n++;
    return 1;
}

int msg_synth_peek(MSG *out) {
    if (!g_synth_n) return 0;
    *out = g_synth[g_synth_h];
    return 1;
}

static BOOL WINAPI hk_GetMessageA(LPMSG msg, HWND h, UINT a, UINT b) {
    if (cur_rtid() == g_main_rtid && g_synth_n) {
        *msg = g_synth[g_synth_h];
        g_synth_h = (g_synth_h + 1) % 64;
        g_synth_n--;
        return TRUE;
    }
    if (cur_rtid() == g_main_rtid) slog("GetMessageA on main thread from %x", (u32)(uintptr_t)__builtin_return_address(0));
    return r_GetMessageA(msg, h, a, b);
}

/* Pinned host facts (contract 7 item 4): the exe reads the screensaver and
 * mouse-trail settings into .data (0x7a3cac, 0x7a3ca8) on activation, and
 * writes them back on exit. The X server's value varied between cold runs, so
 * reads return the profile value 0 and writes never touch the host. */
static BOOL (WINAPI *r_SystemParametersInfoA)(UINT, UINT, PVOID, UINT);
static BOOL WINAPI hk_SystemParametersInfoA(UINT act, UINT ui, PVOID pv, UINT wini) {
    switch (act) {
    case SPI_GETSCREENSAVEACTIVE:
    case SPI_GETMOUSETRAILS:
        if (pv) *(u32 *)pv = 0;
        return TRUE;
    case SPI_SETSCREENSAVEACTIVE:
    case SPI_SETMOUSETRAILS:
        return TRUE;
    default:
        slog("SystemParametersInfoA(%x) passed through", act);
        return r_SystemParametersInfoA(act, ui, pv, wini);
    }
}

void msg_gate_pump(void) {
    MSG m;
    g_in_gate_pump = 1;
    while (PeekMessageA(&m, NULL, 0, 0, PM_REMOVE)) {
        if (m.message == WM_QUIT) { g_quit_seen = 1; continue; }
        if (take_posted(m.hwnd, m.message, m.wParam, m.lParam)) {
            g_in_gate_pump = 0;
            DispatchMessageA(&m);
            g_in_gate_pump = 1;
            continue;
        }
        DispatchMessageA(&m);
    }
    g_in_gate_pump = 0;
}

void msg_install(void) {
    HMODULE exe = GetModuleHandleA(NULL);
    void *o;
    if ((o = iat_hook(exe, "USER32.dll", "RegisterClassA", (void *)hk_RegisterClassA))) { r_RegisterClassA = o; hooks_note("exe:RegisterClassA"); }
    if ((o = iat_hook(exe, "USER32.dll", "PostMessageA", (void *)hk_PostMessageA))) { r_PostMessageA = o; hooks_note("exe:PostMessageA"); }
    if ((o = iat_hook(exe, "USER32.dll", "GetMessageA", (void *)hk_GetMessageA))) { r_GetMessageA = o; hooks_note("exe:GetMessageA"); }
    if ((o = iat_hook(exe, "USER32.dll", "SystemParametersInfoA", (void *)hk_SystemParametersInfoA))) {
        r_SystemParametersInfoA = o;
        hooks_note("exe:SystemParametersInfoA");
    }
    msg_tick_reset();
}
