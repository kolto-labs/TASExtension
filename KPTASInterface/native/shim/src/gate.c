/* Tick gate (contract 5.2, 5.3), the main slot on an arena stack, and the
 * harness loop.
 *
 * The gate is the exe's PeekMessageA IAT hook. It fires when the return
 * address is WinMain's loop-head call site (0x404592, returning to 0x404598)
 * and UpdateScreen depth is 0. Every other exe PeekMessageA returns FALSE.
 * Tick n closes at gate n: journal, capture, wire, then V(n+1) and the input
 * for tick n+1 are staged and the game is released.
 *
 * M2: at the exe entry the process main thread becomes the harness. The CRT
 * and WinMain run as slot 0 on an arena stack whose context record lives in
 * harness_game, and the gate switches with that record (kst_switch). After a
 * restore the switch back loads T's record, so the main slot resumes where it
 * was at T. The Wine main stack is slot 0's harness stack.
 *
 * Counters come from detours: client MainLoop 0x602eb0 (one per tick),
 * server MainLoop 0x4babb0, UpdateScreen 0x401c10 (depth, via a
 * return-address swap), LoadModuleFinish 0x4c5880 (exit count), rand
 * 0x6fc447 and srand 0x6fc43a (draws per call site). */
#include "shim.h"

volatile u32 g_client_count, g_server_count, g_us_count, g_us_depth, g_load_done_count;
u32 g_us_shadow[256];
u32 g_us_sp;
u32 g_lf_ret;
void *t_client, *t_server, *t_us, *t_lf, *t_rand, *t_srand, *t_entry;
volatile LONG g_last_gate_ms;
volatile LONG g_in_gate;

static BOOL (WINAPI *r_PeekMessageA)(LPMSG, HWND, UINT, UINT, UINT);
static BOOL (WINAPI *r_SwapBuffers)(HDC);
u32 g_present_every;   /* 0 = skip every present ([replay] present = "skip"), 1 = draw all, N = every Nth tick */
u32 g_presents, g_presents_skipped;

/* Presents are output only: skipping them never changes game state. Draw
 * calls still run; llvmpipe discards an unflushed frame at the next clear. */
static BOOL WINAPI hk_SwapBuffers(HDC dc) {
    if (g_present_every && (g_present_every == 1 || g_gate.tick % g_present_every == 0)) {
        g_presents++;
        return r_SwapBuffers(dc);
    }
    g_presents_skipped++;
    return TRUE;
}

/* ---- detour stubs ---- */
__attribute__((naked, used)) void st_client(void) {
    __asm__ volatile("lock incl _g_client_count\n\t"
                     "jmp *_t_client\n\t");
}
__attribute__((naked, used)) void st_server(void) {
    __asm__ volatile("lock incl _g_server_count\n\t"
                     "jmp *_t_server\n\t");
}
/* UpdateScreen is cdecl; eax/ecx/edx are free at entry, eax is kept at exit. */
__attribute__((naked, used)) void st_us(void) {
    __asm__ volatile("incl _g_us_count\n\t"
                     "incl _g_us_depth\n\t"
                     "movl _g_us_sp, %eax\n\t"
                     "movl (%esp), %edx\n\t"
                     "movl %edx, _g_us_shadow(,%eax,4)\n\t"
                     "incl %eax\n\t"
                     "andl $255, %eax\n\t"
                     "movl %eax, _g_us_sp\n\t"
                     "movl $1f, (%esp)\n\t"
                     "jmp *_t_us\n"
                     "1:\n\t"
                     "pushl %eax\n\t"
                     "decl _g_us_depth\n\t"
                     "movl _g_us_sp, %eax\n\t"
                     "decl %eax\n\t"
                     "andl $255, %eax\n\t"
                     "movl %eax, _g_us_sp\n\t"
                     "movl _g_us_shadow(,%eax,4), %edx\n\t"
                     "popl %eax\n\t"
                     "jmp *%edx\n\t");
}
/* LoadModuleFinish is thiscall: ecx must survive; eax/edx are free at entry. */
__attribute__((naked, used)) void st_lf(void) {
    __asm__ volatile("movl (%esp), %eax\n\t"
                     "movl %eax, _g_lf_ret\n\t"
                     "movl $1f, (%esp)\n\t"
                     "jmp *_t_lf\n"
                     "1:\n\t"
                     "incl _g_load_done_count\n\t"
                     "jmp *_g_lf_ret\n\t");
}
__attribute__((naked, used)) void st_rand(void) {
    __asm__ volatile("pushl (%esp)\n\t"
                     "call _rand_count\n\t"
                     "addl $4, %esp\n\t"
                     "jmp *_t_rand\n\t");
}
__attribute__((naked, used)) void st_srand(void) {
    __asm__ volatile("pushl 4(%esp)\n\t"
                     "pushl 4(%esp)\n\t"
                     "call _srand_note\n\t"
                     "addl $8, %esp\n\t"
                     "jmp *_t_srand\n\t");
}
__attribute__((naked, used)) void st_entry(void) {
    __asm__ volatile("pushal\n\t"
                     "call _entry_work\n\t"
                     "popal\n\t"
                     "jmp *_t_entry\n\t");
}

/* ---- watchdog (diagnostics only; reads the main thread context on a stall) ---- */
u32 g_profile_ms;

/* Sampling profiler (diagnostics only): main-thread eip and the return
 * address chain head while the game runs a tick; logged in batches. */
static void profile_sample(HANDLE mt) {
    static u32 buf[24 * 3];
    static u32 n;
    if (!g_gate.started || g_in_gate) return;
    CONTEXT c;
    memset(&c, 0, sizeof c);
    c.ContextFlags = CONTEXT_CONTROL | CONTEXT_INTEGER;
    if (SuspendThread(mt) == (DWORD)-1) return;
    GetThreadContext(mt, &c);
    u32 r0 = 0, r1 = 0;
    if (!IsBadReadPtr((void *)(uintptr_t)c.Ebp, 8)) { r0 = ((u32 *)(uintptr_t)c.Ebp)[1]; u32 e2 = ((u32 *)(uintptr_t)c.Ebp)[0];
        if (!IsBadReadPtr((void *)(uintptr_t)e2, 8)) r1 = ((u32 *)(uintptr_t)e2)[1]; }
    ResumeThread(mt);
    buf[n * 3] = (u32)c.Eip; buf[n * 3 + 1] = r0; buf[n * 3 + 2] = r1;
    if (++n == 24) {
        char line[1100];
        buf_t b;
        b_init(&b, line, sizeof line);
        b_str(&b, "prof");
        for (u32 i = 0; i < 24; i++) {
            b_ch(&b, ' '); b_hexu32(&b, buf[i * 3]); b_ch(&b, '/'); b_hexu32(&b, buf[i * 3 + 1]); b_ch(&b, '/'); b_hexu32(&b, buf[i * 3 + 2]);
        }
        b_term(&b);
        slog("%s", line);
        n = 0;
    }
}

/* The watchdog never touches the main thread unless KPTAS_WATCHDOG_CTX=1:
 * a SuspendThread/GetThreadContext during boot changed .data in one cold run
 * (exitB). Its thread handle is opened on the main thread before the
 * watchdog starts, so the handle table grows at a fixed point. */
u32 g_watchdog_ctx;

static DWORD WINAPI watchdog(LPVOID arg) {
    HANDLE mt = (HANDLE)arg;
    u32 last_logged = 0;
    for (;;) {
        if (g_profile_ms) {
            u32 t = GetTickCount();
            while (GetTickCount() - t < 1000) { Sleep(g_profile_ms); if (mt) profile_sample(mt); }
        } else Sleep(1000);
        if (!g_gate.started || g_in_gate || !g_watchdog_s) continue;
        u32 now = GetTickCount();
        u32 since = now - (u32)g_last_gate_ms;
        if (since < g_watchdog_s * 1000u || now - last_logged < 15000u) continue;
        last_logged = now;
        if (!mt || !g_watchdog_ctx) {
            slog("watchdog: no gate for %u ms tick=%u us_depth=%u", since, (u32)g_gate.tick, g_us_depth);
            continue;
        }
        CONTEXT c;
        memset(&c, 0, sizeof c);
        c.ContextFlags = CONTEXT_CONTROL | CONTEXT_INTEGER;
        SuspendThread(mt);
        GetThreadContext(mt, &c);
        u32 st[8] = {0};
        if (!IsBadReadPtr((void *)(uintptr_t)c.Esp, 32)) memcpy(st, (void *)(uintptr_t)c.Esp, 32);
        ResumeThread(mt);
        slog("watchdog: no gate for %u ms tick=%u eip=%x esp=%x ebp=%x eax=%x ecx=%x us_depth=%u "
             "stk=%x %x %x %x %x %x %x %x",
             since, (u32)g_gate.tick, (u32)c.Eip, (u32)c.Esp, (u32)c.Ebp, (u32)c.Eax, (u32)c.Ecx,
             g_us_depth, st[0], st[1], st[2], st[3], st[4], st[5], st[6], st[7]);
    }
}

/* ---- entry and harness loop ---- */
static void gate_body(void);

/* Slot 0's first code on its arena stack: the original entry (CRT startup). */
static void main_slot_entry(void *arg) {
    (void)arg;
    ((void (*)(void))t_entry)();
    slog("CRT entry returned");
    wire_exit(0);
    TerminateProcess(GetCurrentProcess(), 0);
}

/* The game's own exit (WM_QUIT, then CRT exit): tell the driver first. */
static void (WINAPI *r_ExitProcess)(UINT);
static void WINAPI hk_ExitProcess(UINT code) {
    wire_exit(code);
    r_ExitProcess(code);
}

static void harness_loop(void) {
    for (;;) {
        K->cur = 0;
        K->reason[0] = R_NONE;
        kst_switch(&K->hctx[0], &K->hg->slot[0].ctx);
        u32 r = K->reason[0];
        if (r == R_GATE) gate_body();
        else if (r == R_HANDOFF) {
            SetEvent(K->real_ev[K->next]);
            threads_harness_wait_main();
        } else {
            slog("main slot switched out without a reason (%u)", r);
            TerminateProcess(GetCurrentProcess(), 4);
        }
    }
}

/* Runs once at the exe entry point, before CRT startup, outside the loader
 * lock, on the Wine main stack. When active it never returns: the Wine main
 * stack becomes the harness loop. */
void entry_work(void) {
    if (!g_active) return;
    g_last_gate_ms = (LONG)GetTickCount();
    kst_init_threads();
    u32 size = g_exe_stack_reserve;
    void *stk = kst_valloc(size);
    kst_slot_init(0, stk, size, main_slot_entry, 0, KST_SLOT_RUNNING);
    SH->slot[0].stack = (u32)(uintptr_t)stk;
    SH->slot[0].stack_size = size;
    slog("entry: main slot stack %x+%x, %u slot threads", (u32)(uintptr_t)stk, size, KST_MAX_SLOTS - 1);
    if (g_watchdog_s || g_profile_ms) {
        HANDLE mt = (g_watchdog_ctx || g_profile_ms)
                        ? OpenThread(THREAD_SUSPEND_RESUME | THREAD_GET_CONTEXT, FALSE, g_main_rtid) : NULL;
        CloseHandle(CreateThread(NULL, 65536, watchdog, mt, 0, NULL));
    }
    wire_service(0);          /* hello .. start, on the harness stack */
    slog("entry: released to CRT on the arena stack (started=%d)", g_gate.started);
    harness_loop();
}

/* ---- gate ---- */
static void gate_body(void) {
    g_in_gate = 1;
    kst_set_in_gate(1);
    threads_join_pending();
    msg_gate_pump();
    g_gate.client_passes = g_client_count;
    g_gate.server_passes = g_server_count;
    g_gate.subticks = g_us_count;
    if (g_gate.tick == 0 || g_gate.tick == 300) gl_log_resolved();
    wire_gate_hook();       /* journal, pushes, replies, then serve requests until released */
    /* release into tick n+1 */
    g_gate.tick++;
    clock_set_tick(g_gate.tick);
    clock_tick_reset();
    journal_tick_reset();
    msg_tick_reset();
    wire_tick_begin();
    g_client_count = g_server_count = g_us_count = 0;
    g_last_gate_ms = (LONG)GetTickCount();
    kst_set_in_gate(0);
    g_in_gate = 0;
}

static int g_serving_synth;

static BOOL WINAPI hk_PeekMessageA(LPMSG m, HWND h, UINT a, UINT b, UINT r) {
    u32 ret = (u32)(uintptr_t)__builtin_return_address(0);
    if (ret == GATE_RET && g_us_depth == 0 && g_gate.started && cur_rtid() == g_main_rtid && K->cur == 0 &&
        !g_in_gate) {
        DWORD le = GetLastError();
        if (!g_serving_synth) {
            K->reason[0] = R_GATE;
            kst_switch(&K->hg->slot[0].ctx, &K->hctx[0]);
        }
        SetLastError(le);
        /* staged score messages first; this pass only dispatches and is not a tick */
        if (msg_synth_peek(m)) { g_serving_synth = 1; return TRUE; }
        g_serving_synth = 0;
        if (g_quit_seen) {
            g_quit_seen = 0;
            PostQuitMessage(0);
            return r_PeekMessageA(m, h, a, b, r);
        }
        return FALSE;
    }
    return FALSE;
}

void gate_install(void) {
    HMODULE exe = GetModuleHandleA(NULL);
    void *o = iat_hook(exe, "USER32.dll", "PeekMessageA", (void *)hk_PeekMessageA);
    if (o) { r_PeekMessageA = o; hooks_note("exe:PeekMessageA(gate)"); }
    o = iat_hook(exe, "KERNEL32.dll", "ExitProcess", (void *)hk_ExitProcess);
    if (o) { r_ExitProcess = o; hooks_note("exe:ExitProcess"); }
    if (g_present_every != 1) {
        o = iat_hook(exe, "GDI32.dll", "SwapBuffers", (void *)hk_SwapBuffers);
        if (o) { r_SwapBuffers = o; hooks_note("exe:SwapBuffers(present)"); }
    }

    static const u8 e_entry[7] = {0x6a, 0x60, 0x68, 0x40, 0xa0, 0x75, 0x00};
    static const u8 l_entry[] = {2, 5, 0};
    static const u8 e_loop[6] = {0x55, 0x8b, 0xec, 0x83, 0xe4, 0xf8};
    static const u8 l_loop[] = {1, 2, 3, 0};
    static const u8 e_us[6] = {0x56, 0xe8, 0x7a, 0xd1, 0x04, 0x00};
    static const u8 l_us[] = {1, 5, 0};
    static const u8 e_lf[7] = {0x6a, 0xff, 0x68, 0x6b, 0x96, 0x71, 0x00};
    static const u8 l_lf[] = {2, 5, 0};
    static const u8 e_rand[5] = {0xe8, 0x93, 0x2a, 0x00, 0x00};
    static const u8 e_srand[5] = {0xe8, 0xa0, 0x2a, 0x00, 0x00};
    static const u8 l_call[] = {5, 0};
    static const u8 e_ghrt[6] = {0x83, 0xec, 0x08, 0x56, 0x8b, 0xf1};

    if (detour(EXE_ENTRY, e_entry, 7, l_entry, (void *)st_entry, &t_entry)) hooks_note("exe:entry");
    else slog("detour entry failed");
    if (detour(ADDR_CLIENT_LOOP, e_loop, 6, l_loop, (void *)st_client, &t_client)) hooks_note("exe:ClientMainLoop");
    else slog("detour client loop failed");
    if (detour(ADDR_SERVER_LOOP, e_loop, 6, l_loop, (void *)st_server, &t_server)) hooks_note("exe:ServerMainLoop");
    else slog("detour server loop failed");
    if (detour(ADDR_UPDATESCREEN, e_us, 6, l_us, (void *)st_us, &t_us)) hooks_note("exe:UpdateScreen");
    else slog("detour UpdateScreen failed");
    if (detour(ADDR_LOAD_FINISH, e_lf, 7, l_lf, (void *)st_lf, &t_lf)) hooks_note("exe:LoadModuleFinish");
    else slog("detour LoadModuleFinish failed");
    if (detour(ADDR_RAND, e_rand, 5, l_call, (void *)st_rand, &t_rand)) hooks_note("exe:rand");
    else slog("detour rand failed");
    if (detour(ADDR_SRAND, e_srand, 5, l_call, (void *)st_srand, &t_srand)) hooks_note("exe:srand");
    else slog("detour srand failed");
    if (g_clk.mode == 0) {
        if (memcmp((void *)(uintptr_t)ADDR_GHRT_INT, e_ghrt, 6) == 0 && patch_jmp(ADDR_GHRT_INT, (void *)hk_ghrt))
            hooks_note("exe:GetHighResolutionTimer");
        else slog("GetHighResolutionTimer patch failed");
    }
}
