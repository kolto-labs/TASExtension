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
 * UpdateScreen message pump (0x401510) never sees real-time input. The one
 * exception is score input: an armed !altf4@save is served to that pump
 * inside a save (msg_save_pump). */
#include "shim.hpp"
#include "winhandles.hpp"
#include "physical_input.hpp"

/* Fixed C hook ABI and assembly-referenced symbol names. */
extern "C" {

int g_in_gate_pump;
u32 g_msg_int_n, g_msg_ext_n;
#define g_wndproc_orig (SH->wh.callbacks)
#define g_nslots (SH->wh.callback_count)
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

static int find_posted(HWND h, UINT m, WPARAM w, LPARAM l) {
    MSG message{}; message.hwnd=h;message.message=m;message.wParam=w;message.lParam=l;
    wh_msg_alias(&message); h=message.hwnd;w=message.wParam;l=message.lParam;
    for (u32 i = 0; i < g_posted_n; i++) {
        posted_t *p = &g_posted[i];
        if (p->h == h && p->m == m && p->w == w && p->l == l) return (int)i;
    }
    return -1;
}

static int take_posted(HWND h, UINT m, WPARAM w, LPARAM l) {
    int i = find_posted(h, m, w, l);
    if (i < 0) return 0;
    --g_posted_n;
    memmove(&g_posted[i], &g_posted[i + 1], (g_posted_n - (u32)i) * sizeof g_posted[0]);
    return 1;
}

/* Keyboard and mouse messages in the real queue come from the host (the game's
 * input is the virtual DirectInput device and the synthesized score queue). */
static int host_input(UINT m) {
    return (m >= WM_KEYFIRST && m <= WM_KEYLAST) || (m >= WM_MOUSEFIRST && m <= WM_MOUSELAST);
}
u32 g_msg_host_dropped;

/* The supported executable's WinMessageHandler (402800) has no WM_GETOBJECT
 * case or side effects before its DefWindowProc default. Cross-thread native
 * accessibility requests can arrive during any USER32 call, not only our pump.
 * InSendMessageEx is delivery provenance, NOT sender authentication. Any active
 * supported game SendMessage/Dispatch scope conservatively retains the game
 * route, including scopes on another real thread. */
static volatile LONG g_game_message_depth;
/* Main-thread stack of the messages the game itself is sending or
 * dispatching (its SendMessageA/DispatchMessageA, and the gate's dispatch of
 * its own posts). Only the innermost one is admitted past the host filter. */
static UINT g_game_message[32];
static u32 g_game_message_sp;
void msg_game_enter(UINT m) {
    InterlockedIncrement(&g_game_message_depth);
    if (cur_rtid() != g_main_rtid) return;
    if (g_game_message_sp < 32) g_game_message[g_game_message_sp] = m;
    g_game_message_sp++;
}
void msg_game_leave(void) {
    InterlockedDecrement(&g_game_message_depth);
    if (cur_rtid() == g_main_rtid && g_game_message_sp) g_game_message_sp--;
}
static int game_message(UINT m) {
    u32 sp = g_game_message_sp;
    return cur_rtid() == g_main_rtid && sp && sp <= 32 && g_game_message[sp - 1] == m;
}
static int msg_host_accessibility(WNDPROC proc,UINT m) {
    if(g_bnd_native || m!=WM_GETOBJECT || (uintptr_t)proc!=0x00402800u ||
       InterlockedCompareExchange(&g_game_message_depth,0,0)) return 0;
    u8 fp[512] __attribute__((aligned(16)));u32 flags;
    __asm__ volatile("fxsave %0; pushfl; popl %1":"=m"(fp),"=r"(flags)::"memory");
    DWORD error=GetLastError();
    DWORD sent=InSendMessageEx(NULL);
    SetLastError(error);
    __asm__ volatile("fxrstor %0; pushl %1; popfl"::"m"(fp),"r"(flags):"memory","cc");
    return (sent & ISMEX_SEND)!=0;
}

/* Messages only the host desktop originates: pointer, keyboard and IME input
 * and the pointer's activation/cursor queries. The game's input is the
 * virtual DirectInput device and the synthesized score queue, which reaches
 * the WndProc only through the game's own DispatchMessageA. */
static int host_input_class(UINT m) {
    return host_input(m) || m == WM_SETCURSOR || m == WM_MOUSEACTIVATE || m == WM_INPUT ||
           (m >= WM_NCMOUSEMOVE && m <= WM_NCXBUTTONDBLCLK) || (m >= 0x2a0 && m <= 0x2a3) ||
           (m >= WM_IME_STARTCOMPOSITION && m <= WM_IME_KEYLAST) ||
           (m >= WM_IME_CONTROL && m <= WM_IME_KEYUP);
}
/* Outside the gate pump (which already routes every unrequested message to
 * DefWindowProc), host desktop interaction must still never reach the game:
 * focus, activation, capture and cancel-mode messages are delivered only
 * inside a game window call that changes that class in the virtual model
 * (winhandles.c), or when the game itself sends or dispatches that message;
 * host input classes only in the latter case. Every boundary but native. */
static int host_message(UINT m) {
    if (g_bnd_native) return 0;
    u32 cls = wh_activation_class(m);
    if (cls) return !(wh_activation_admit() & cls) && !game_message(m);
    return host_input_class(m) && !game_message(m);
}
u32 g_msg_host_suppressed;
static u32 g_host_logged;

static LRESULT interp(u32 i, HWND h, UINT m, WPARAM w, LPARAM l) {
    if (wh_reconciling()) return DefWindowProcA(h,m,w,l);
    if (g_in_gate_pump || msg_host_accessibility(g_wndproc_orig[i],m)) {
        g_msg_ext_n++;
        if (g_ext_logged < 400) {
            g_ext_logged++;
            slog("ext msg tick=%u m=%x w=%x l=%x", (u32)g_gate.tick, m, (u32)w, (u32)l);
        }
        if (m == WM_CLOSE || m == WM_QUIT || m == WM_DESTROY) return 0;
        return DefWindowProcA(h, m, w, l);
    }
    if (host_message(m)) {
        /* Not hashed: arrival follows the host, not the game. */
        g_msg_ext_n++;
        g_msg_host_suppressed++;
        if (g_host_logged < 200) {
            g_host_logged++;
            slog("host msg suppressed tick=%u m=%x w=%x l=%x scope=%u admit=%x",
                 (u32)g_gate.tick, m, (u32)w, (u32)l, wh_activation_depth(), wh_activation_admit());
        }
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
    return wh_wndproc_call(g_wndproc_orig[i], h, m, w, l);
}
static LRESULT CALLBACK interp0(HWND h, UINT m, WPARAM w, LPARAM l) { return interp(0, h, m, w, l); }
static LRESULT CALLBACK interp1(HWND h, UINT m, WPARAM w, LPARAM l) { return interp(1, h, m, w, l); }
static LRESULT CALLBACK interp2(HWND h, UINT m, WPARAM w, LPARAM l) { return interp(2, h, m, w, l); }
static LRESULT CALLBACK interp3(HWND h, UINT m, WPARAM w, LPARAM l) { return interp(3, h, m, w, l); }
static WNDPROC g_interp[4] = {interp0, interp1, interp2, interp3};

WNDPROC msg_wrap_wndproc(WNDPROC proc) {
    u32 p=(u32)(uintptr_t)proc;
    if(p<EXE_TEXT_VA || p>=EXE_TEXT_VA+EXE_TEXT_SIZE) return proc;
    for(u32 i=0;i<g_nslots;i++) if(g_wndproc_orig[i]==proc) return g_interp[i];
    if(g_nslots==4) { slog("window callback slots exhausted");TerminateProcess(GetCurrentProcess(),6);return NULL; }
    g_wndproc_orig[g_nslots]=proc;return g_interp[g_nslots++];
}
WNDPROC msg_unwrap_wndproc(WNDPROC proc) {
    for(u32 i=0;i<g_nslots;i++) if(g_interp[i]==proc) return g_wndproc_orig[i];
    return proc;
}

static ATOM WINAPI hk_RegisterClassA(const WNDCLASSA *wc) {
    math_host_scope_t fp;math_scope_begin(&fp,MATH_HOST_FP|MATH_HOST_ID);
    WNDCLASSA translated = *wc;
    translated.hCursor=static_cast<HCURSOR>(wh_real(translated.hCursor));
    translated.hIcon=static_cast<HICON>(wh_real(translated.hIcon));
    translated.hbrBackground=static_cast<HBRUSH>(wh_real(translated.hbrBackground));
    translated.lpfnWndProc=msg_wrap_wndproc(wc->lpfnWndProc);
    if(translated.lpfnWndProc!=wc->lpfnWndProc)
        slog("RegisterClassA %s wndproc=%x -> interposer %x",
             ((uintptr_t)wc->lpszClassName > 0xffff) ? wc->lpszClassName : "#atom",
             (u32)(uintptr_t)wc->lpfnWndProc,(u32)(uintptr_t)translated.lpfnWndProc);
    ATOM result=r_RegisterClassA(&translated);math_scope_end(&fp);return result;
}

/* A registered worker may post to the main window while holding the serial
 * scheduler baton. K->cur alone is not provenance: an unrelated OS thread can
 * enter this IAT hook while a game slot is running. Verify its real thread
 * handle, slot liveness and destination queue before admitting a mirror entry.
 * Worker-local/foreign-window queues are outside the main queue restore model.
 * This helper changes neither SH nor LastError. */
static int post_from_game(HWND h) {
    u32 caller=cur_rtid();
    if(caller==g_main_rtid) return 1;
    if(!K || !SH) return 0;
    u32 slot=K->cur;
    if(!slot || slot>=KST_MAX_SLOTS || slot>=K->cfg.nslots ||
       !SH->slot[slot].used || SH->slot[slot].state!=W_RUNNING ||
       !K->real_thr[slot] || !h || h==HWND_BROADCAST || wh_reconciling()) return 0;
    DWORD error=GetLastError();
    DWORD owner=GetThreadId(K->real_thr[slot]);
    DWORD target=owner==caller ? GetWindowThreadProcessId(h,NULL) : 0;
    SetLastError(error);
    return owner==caller && target==g_main_rtid;
}

static BOOL WINAPI hk_PostMessageA(HWND h, UINT m, WPARAM w, LPARAM l) {
    /* Never deliver an untracked post: the mirror is part of the restore
     * state. A failed real post must not leave a phantom mirror entry. */
    int tracked = post_from_game(h);
    if (tracked && g_posted_n >= 64) {
        slog("message capture refused: posted queue capacity exhausted");
        TerminateProcess(GetCurrentProcess(),6);for(;;){}
    }
    posted_t pending;
    if (tracked) {
        MSG message{};message.hwnd=h;message.message=m;message.wParam=w;message.lParam=l;
        wh_msg_alias(&message);
        pending.h=message.hwnd;pending.m=m;pending.w=message.wParam;pending.l=message.lParam;
    }
    /* A worker retains the baton across this asynchronous post and mirror
     * append. The main game pump cannot run until it parks; failed OS posts
     * leave no entry. No lock or unsnapshotted queue is introduced. */
    BOOL ok = r_PostMessageA(h, m, w, l);
    if (ok && tracked) g_posted[g_posted_n++] = pending;
    return ok;
}

/* Discard the abandoned timeline's queued delivery before overwriting SH.
 * Only tracked game posts and host input are removed. Unrelated host messages
 * retain their normal external dispatch, so paint/window maintenance survives.
 * Called on the owning main thread with execution already poisoned. */
void msg_restore_prepare(void) {
    DWORD error=GetLastError();
    int was=g_in_gate_pump;g_in_gate_pump=1;
    /* The host already computed a minimal restore write set. Never mutate SH
     * here: an identical omitted page would retain this cleanup's changes. */
    posted_t pending[64];u32 count=g_posted_n;
    if(count>64){slog("invalid posted queue before restore");TerminateProcess(GetCurrentProcess(),6);for(;;){}}
    memcpy(pending,g_posted,count*sizeof pending[0]);
    for(u32 i=0;i<count;i++) {
        MSG real{};real.hwnd=pending[i].h;real.message=pending[i].m;
        real.wParam=pending[i].w;real.lParam=pending[i].l;
        wh_msg_real(&real); /* lookup only; aliasing unknown host handles allocates SH slots */
        pending[i].h=real.hwnd;pending[i].w=real.wParam;pending[i].l=real.lParam;
    }
    MSG m;
    while (PeekMessageA(&m,NULL,0,0,PM_REMOVE)) {
        int tracked=0;
        for(u32 i=0;i<count;i++) {
            posted_t *p=&pending[i];
            if(p->h==m.hwnd&&p->m==m.message&&p->w==m.wParam&&p->l==m.lParam) {
                --count;memmove(&pending[i],&pending[i+1],(count-i)*sizeof pending[0]);
                tracked=1;break;
            }
        }
        if (m.message==WM_QUIT || tracked || host_input(m.message)) continue;
        DispatchMessageA(&m);
    }
    g_in_gate_pump=was;SetLastError(error);
}

/* Memory restore supplies the target's ordered logical posts. Recreate their
 * host queue after windows have been rebound, without appending duplicates to
 * SH. Failure leaves the native restore poisoned; a later restore drains any
 * partial reconstruction before trying again. */
int msg_restore_finish(void) {
    if (g_posted_n>64) return 0;
    DWORD error=GetLastError();int was=g_in_gate_pump;g_in_gate_pump=1;
    int ok=1;
    for(u32 i=0;i<g_posted_n;i++) {
        posted_t *p=&g_posted[i];MSG m{};
        m.hwnd=p->h;m.message=p->m;m.wParam=p->w;m.lParam=p->l;
        wh_msg_real(&m);
        if(!PostMessageA(m.hwnd,m.message,m.wParam,m.lParam)){ok=0;break;}
    }
    g_in_gate_pump=was;SetLastError(error);return ok;
}

/* Synthesized score messages (contract 5.3 step 8): the gate PeekMessageA
 * returns the head without removing it, and WinMain's GetMessageA serves
 * the same message, which DispatchMessageA hands to the game WndProc. */

/* A message's pt is the cursor: a mouse message's own position, else the
 * last one a synthesized mouse message carried. */
static void synth_fill(MSG *q, HWND h, UINT m, WPARAM w, LPARAM l) {
    memset(q, 0, sizeof *q);
    q->hwnd = h;
    q->message = m;
    q->wParam = w;
    q->lParam = l;
    q->time = (DWORD)(clock_u() / 1000ull);
    if (m >= WM_MOUSEFIRST && m <= WM_MOUSELAST) {
        SH->synth_pt_x = (int)(l & 0xffff);
        SH->synth_pt_y = (int)((l >> 16) & 0xffff);
    }
    q->pt.x = SH->synth_pt_x;
    q->pt.y = SH->synth_pt_y;
}

int msg_synth_available(u32 count) {
    return g_synth_n <= 64 && count <= 64 - g_synth_n;
}

void msg_synth_push(UINT m, WPARAM w, LPARAM l) {
    if (g_synth_n >= 64) return;
    synth_fill(&g_synth[(g_synth_h + g_synth_n) % 64], *(HWND *)(uintptr_t)ADDR_RENDER_WINDOW, m, w, l);
    g_synth_n++;
}

/* A post: Windows hands posted messages out before input, so it goes first. */
void msg_synth_push_front(HWND h, UINT m, WPARAM w, LPARAM l) {
    if (g_synth_n >= 64) return;
    g_synth_h = (g_synth_h + 63) % 64;
    synth_fill(&g_synth[g_synth_h], static_cast<HWND>(wh_alias(h,WH_WINDOW)), m, w, l);
    g_synth_n++;
}

int msg_synth_peek(MSG *out) {
    if (!g_synth_n) return 0;
    *out = g_synth[g_synth_h];
    return 1;
}

/* Translate only the exact message just delivered from our captured queue.
 * Native TranslateMessage posts into USER32's real queue, where host-input
 * filtering would discard its character. Keep this causal delivery captured.
 * Return/Escape are layout-independent unmodified control characters. Ctrl/
 * Alt translations and general text/dead keys remain outside this cut. */
int msg_translate_synth(const MSG *message, BOOL *result) {
    if (cur_rtid() != g_main_rtid || !SH->synth_translate_pending) return 0;
    MSG *p = &SH->synth_translate;
    if (p->hwnd != message->hwnd || p->message != message->message ||
        p->wParam != message->wParam || p->lParam != message->lParam ||
        p->time != message->time || p->pt.x != message->pt.x || p->pt.y != message->pt.y)
        return 0;
    SH->synth_translate_pending = 0;
    UINT kind = message->message;
    *result = kind == WM_KEYDOWN || kind == WM_KEYUP ||
              kind == WM_SYSKEYDOWN || kind == WM_SYSKEYUP;
    if (kind == WM_KEYDOWN &&
        (message->wParam == VK_RETURN || message->wParam == VK_ESCAPE) &&
        !SH->keys[0x1d] && !SH->keys[0x9d] &&
        !SH->keys[0x38] && !SH->keys[0xb8]) {
        /* Append, as TranslateMessage posts after messages already queued.
         * Capacity was reserved before admission; never silently lose a char. */
        if (!msg_synth_available(1)) {
            slog("message translation refused: synthesized queue capacity exhausted");
            TerminateProcess(GetCurrentProcess(),6);for(;;){}
        }
        MSG character = *message;
        character.message = WM_CHAR;
        g_synth[(g_synth_h + g_synth_n) % 64] = character;
        g_synth_n++;
    }
    return 1;
}

/* The keyboard messages a real Alt+F4 sends, pressed and released together:
 * WM_SYSKEYDOWN VK_MENU, WM_SYSKEYDOWN VK_F4 (lParam bit 29: Alt is down),
 * WM_SYSKEYUP VK_F4, then WM_KEYUP VK_MENU (Alt released after another key
 * is a plain key-up). */
void msg_altf4_keys(void) {
    msg_synth_push(WM_SYSKEYDOWN, VK_MENU, 0x20380001);
    msg_synth_push(WM_SYSKEYDOWN, VK_F4, 0x203e0001);
    msg_synth_push(WM_SYSKEYUP, VK_F4, (LPARAM)0xe03e0001u);
    msg_synth_push(WM_KEYUP, VK_MENU, (LPARAM)0xc0380001u);
}

/* !altf4@save: Alt+F4 pressed while a save is being written.
 * Both save writers, StallEventSaveGame (0x4b3110) and DoPCAutosave
 * (0x4b8300), call UpdateScreen (0x401c10; first at 0x4b325a and 0x4b83c6),
 * and UpdateScreen always runs the message pump at 0x401510 (call at
 * 0x401cd6): PeekMessageA(PM_NOREMOVE) returning to 0x401529, then
 * GetMessageA 0x40154d, TranslateMessage 0x401558, DispatchMessageA 0x40155f,
 * and the re-peek returning to 0x40157c. On Windows that pump is where a key
 * pressed during the save reaches the game. Here every such peek returns
 * "no message", except while an armed Alt+F4 is live: the first in-save peek
 * stages the four key messages, and from then until the save returns the pump
 * is served the synthesized queue, as the gate does. That includes the
 * WM_SYSCOMMAND SC_CLOSE that hk_DefWindowProcA puts at its head, so it comes
 * back in the same pump loop, as a real post would. Neither the clock nor the
 * save length changes. With nothing armed this returns 0 and the pump sees
 * what it always saw. */
static int g_save_pump_live;
static u32 g_save_pump_logged;
int msg_save_pump(MSG *out, u32 ret) {
    if (ret != 0x00401529u && ret != 0x0040157cu) return 0;
    if (!g_save_depth) return 0;
    if (!g_save_pump_live && SH->altf4_save) {
        SH->altf4_save = 0;
        g_save_pump_live = 1;
        if (g_posted_n)
            slog("altf4@save: %u game posts still queued; a real queue would deliver them first", g_posted_n);
        msg_altf4_keys();
        ev_add(EV_ALTF4, 1);
        slog("altf4@save: delivered at tick %u, pump peek from %x, save depth %u",
             (u32)g_gate.tick, ret, g_save_depth);
    }
    if (!g_save_pump_live) return 0;
    if (g_save_pump_logged < 32) {
        g_save_pump_logged++;
        slog("altf4@save: in-save peek from %x tick %u queued %u", ret, (u32)g_gate.tick, g_synth_n);
    }
    return msg_synth_peek(out);
}

/* The outermost save writer returned. */
void msg_save_end(void) {
    if (g_save_pump_live && g_synth_n)
        slog("altf4@save: save ended with %u messages left for the gate", g_synth_n);
    g_save_pump_live = 0;
    if (SH->altf4_save)
        slog("altf4@save: save at tick %u ran no message pump; still armed", (u32)g_gate.tick);
}

static BOOL WINAPI hk_GetMessageA(LPMSG msg, HWND h, UINT a, UINT b) {
    if (cur_rtid() == g_main_rtid && g_synth_n) {
        *msg = g_synth[g_synth_h];
        SH->synth_translate = *msg;
        SH->synth_translate_pending = 1;
        g_synth_h = (g_synth_h + 1) % 64;
        g_synth_n--;
        return TRUE;
    }
    if (cur_rtid() == g_main_rtid) SH->synth_translate_pending = 0;
    if (cur_rtid() == g_main_rtid) slog("GetMessageA on main thread from %x", (u32)(uintptr_t)__builtin_return_address(0));
    return r_GetMessageA(msg, h, a, b);
}

/* DefWindowProc's half of Alt+F4. On Windows (and in Wine's DefWindowProc)
 * WM_SYSKEYDOWN VK_F4 with Alt down posts WM_SYSCOMMAND SC_CLOSE to the
 * top-level window unless its class has CS_NOCLOSE. A real post would wait
 * for the gate pump and count as external, so the post goes to the head of
 * the synthesized queue instead: the game's WndProc gets it in the same pump
 * pass, as on Windows, and its DefWindowProc call turns SC_CLOSE into
 * WM_CLOSE with a send on this thread. Every other message passes through. */
static LRESULT (WINAPI *r_DefWindowProcA)(HWND, UINT, WPARAM, LPARAM);
static LRESULT WINAPI hk_DefWindowProcA(HWND h, UINT m, WPARAM w, LPARAM l) {
    if (m == WM_SYSKEYDOWN && w == VK_F4 && (l & 0x20000000)) {
        HWND top = GetAncestor(h, GA_ROOT);
        if (!top) top = h;
        int noclose = (GetClassLongA(top, GCL_STYLE) & CS_NOCLOSE) != 0;
        if (!noclose) msg_synth_push_front(top, WM_SYSCOMMAND, SC_CLOSE, 0);
        slog("DefWindowProcA Alt+F4 tick=%u -> %s", (u32)g_gate.tick, noclose ? "CS_NOCLOSE, nothing" : "WM_SYSCOMMAND SC_CLOSE");
        return 0;
    }
    return r_DefWindowProcA(h, m, w, l);
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
    wire_physical_observe(NULL);
    MSG m;
    g_in_gate_pump = 1;
    while (PeekMessageA(&m, NULL, 0, 0, PM_REMOVE)) {
        if (m.message == WM_QUIT) { g_quit_seen = 1; SH->quit_status = (u32)m.wParam; continue; }
        if (take_posted(m.hwnd, m.message, m.wParam, m.lParam)) {
            g_in_gate_pump = 0;
            msg_game_enter(m.message);
            DispatchMessageA(&m);
            msg_game_leave();
            g_in_gate_pump = 1;
            continue;
        }
        /* host keys and mouse: removed, never translated or dispatched (not even to
         * DefWindowProc, whose Alt/F10 handling can start a modal menu loop) */
        if (host_input(m.message)) { wire_physical_observe(&m); g_msg_host_dropped++; continue; }
        DispatchMessageA(&m);
    }
    g_in_gate_pump = 0;
}

/* At a stopped gate (tool controls) nothing on the game thread reads the X
 * connection, so Wine never turns key presses into input and GetAsyncKeyState
 * stays where it was. Let the driver process its pending events now. Host
 * keyboard and mouse messages it queues are removed and dropped here, without
 * Translate or Dispatch. Messages the driver sends meanwhile are routed as
 * external (DefWindowProc), as in the gate pump. Everything else, the game's
 * own posts included, stays queued for the next gate pump: delivering it here
 * would change the game after gate n's state and journal were taken. */
u32 msg_host_events(void) {
    wire_physical_observe(NULL);
    int was = g_in_gate_pump;
    u32 n = 0;
    MSG m;
    g_in_gate_pump = 1;
    MsgWaitForMultipleObjectsEx(0, NULL, 0, QS_ALLINPUT, MWMO_INPUTAVAILABLE);
    static const UINT lo[2] = {WM_KEYFIRST, WM_MOUSEFIRST}, hi[2] = {WM_KEYLAST, WM_MOUSELAST};
    for (u32 r = 0; r < 2; r++)
        while (PeekMessageA(&m, NULL, lo[r], hi[r], PM_NOREMOVE | PM_NOYIELD)) {
            if (find_posted(m.hwnd, m.message, m.wParam, m.lParam) >= 0) break;   /* the game's: leave it */
            if (!PeekMessageA(&m, NULL, lo[r], hi[r], PM_REMOVE | PM_NOYIELD)) break;
            wire_physical_observe(&m);
            n++;
        }
    g_msg_host_dropped += n;
    g_in_gate_pump = was;
    return n;
}

void msg_install(void) {
    HMODULE exe = GetModuleHandleA(NULL);
    void *o;
    if ((o = iat_hook(exe, "USER32.dll", "RegisterClassA", (void *)hk_RegisterClassA))) { r_RegisterClassA = reinterpret_cast<decltype(r_RegisterClassA)>(o); hooks_note("exe:RegisterClassA"); }
    if ((o = iat_hook(exe, "USER32.dll", "PostMessageA", (void *)hk_PostMessageA))) { r_PostMessageA = reinterpret_cast<decltype(r_PostMessageA)>(o); hooks_note("exe:PostMessageA"); }
    if ((o = iat_hook(exe, "USER32.dll", "GetMessageA", (void *)hk_GetMessageA))) { r_GetMessageA = reinterpret_cast<decltype(r_GetMessageA)>(o); hooks_note("exe:GetMessageA"); }
    if ((o = iat_hook(exe, "USER32.dll", "DefWindowProcA", (void *)hk_DefWindowProcA))) {
        r_DefWindowProcA = reinterpret_cast<decltype(r_DefWindowProcA)>(o);
        hooks_note("exe:DefWindowProcA");
    }
    if ((o = iat_hook(exe, "USER32.dll", "SystemParametersInfoA", (void *)hk_SystemParametersInfoA))) {
        r_SystemParametersInfoA = reinterpret_cast<decltype(r_SystemParametersInfoA)>(o);
        hooks_note("exe:SystemParametersInfoA");
    }
    msg_tick_reset();
}

} /* extern "C" */
