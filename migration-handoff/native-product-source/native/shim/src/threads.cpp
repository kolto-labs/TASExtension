/* Thread slots and the serial scheduler (contract 3.2, sched.mode = "serial"),
 * keeping M1's join points.
 *
 * Every exe CreateThread gets a slot: an arena stack, a context record in
 * harness_game, a virtual TID and a virtual handle. Slot k is driven by one
 * real thread pre-created at the entry (slot 0: the process main thread), so
 * the game code of a slot always runs on the same Wine thread. Exactly one game
 * context runs at a time; the baton passes between real threads through real
 * events in harness_core.
 *
 * Join points are M1's: a ResumeThread or SetEvent from the main slot marks the
 * worker pending, and the main slot hands over the baton at its next
 * LeaveCriticalSection that brings its (exe-IAT) lock depth to zero, at a
 * CreateThread without CREATE_SUSPENDED, or at the gate. The worker runs until
 * it parks (SuspendThread on itself, a wait on an unsignaled event, or exit);
 * parking saves its context record and hands the baton back.
 *
 * Parked contexts, suspend counts, virtual events and TLS are all in
 * harness_game, so a restore rewinds the thread set with memory: a worker that
 * had not started at T starts again from its entry. */
#include "shim.hpp"

/* Fixed C hook ABI and assembly-referenced symbol names. */
extern "C" {

u32 g_main_rtid;
u32 g_hang_ms = 120000;
u32 g_exe_stack_reserve = 1u << 20;

static void (WINAPI *r_Enter)(LPCRITICAL_SECTION);
static void (WINAPI *r_Leave)(LPCRITICAL_SECTION);
static DWORD (WINAPI *r_Wait)(HANDLE, DWORD);
static BOOL (WINAPI *r_SetEvent)(HANDLE);
static DWORD (WINAPI *r_ResumeThread)(HANDLE);
static DWORD (WINAPI *r_SuspendThread)(HANDLE);
static void (WINAPI *r_ExitThread)(DWORD);
static BOOL (WINAPI *r_GetExitCodeThread)(HANDLE, LPDWORD);
static BOOL (WINAPI *r_SetThreadPriority)(HANDLE, int);
typedef BOOL (WINAPI *icsex_t)(LPCRITICAL_SECTION, DWORD, DWORD);
static icsex_t r_InitCSEx;
static u32 g_logs;

static inline vslot_t *cur_vs(void) { return &SH->slot[K->cur]; }

u32 vtid_current(void) { return SH->slot[K->cur].vtid; }

u32 threads_tls_main(u32 idx) { return idx < 64 ? SH->slot[0].tls[idx] : 0; }

/* ---------------------------------------------------------------- baton */

/* Harness side (the gate, or the harness loop after a hand-off): wait until the
 * running slot hands the baton back to slot 0. */
void threads_harness_wait_main(void) {
    for (;;) {
        DWORD r = WaitForSingleObject(K->real_ev[0], g_hang_ms);
        if (r == WAIT_OBJECT_0) break;
        SH->hang_trips++;
        slog("hang guard: slot %u has run %u ms without parking (tick %u)", K->cur, g_hang_ms, (u32)g_gate.tick);
    }
    K->cur = 0;
}

static void after_join(u32 k) {
    vslot_t *s = &SH->slot[k];
    SH->joins++;
    if (g_logs < 400) {
        g_logs++;
        slog("join slot %u vtid=%x start=%x state=%u tick=%u", k, s->vtid, s->start, s->state, (u32)g_gate.tick);
    }
    if (s->state == W_EXITED && s->used) {
        /* the stack is dead: free it from here, never from the slot itself */
        kst_vfree((void *)(uintptr_t)s->stack);
        s->used = 0;
        K->hg->slot[k].state = KST_SLOT_FREE;
    }
}

/* From the main slot's game context: run slot k until it parks. */
static void run_from_main(u32 k) {
    vslot_t *s = &SH->slot[k];
    s->pending = 0;
    s->state = W_RUNNING;
    K->next = k;
    K->reason[0] = R_HANDOFF;
    kst_switch(&K->hg->slot[0].ctx, &K->hctx[0]);
    after_join(k);
}

/* From the harness (gate, or game code running inside the gate pump). */
static void run_from_harness(u32 k) {
    vslot_t *s = &SH->slot[k];
    s->pending = 0;
    s->state = W_RUNNING;
    SetEvent(K->real_ev[k]);
    threads_harness_wait_main();
    after_join(k);
}

static void join_pending(int from_harness) {
    if (!SH->pending_any) return;
    SH->pending_any = 0;
    for (u32 k = 1; k < KST_MAX_SLOTS; k++) {
        vslot_t *s = &SH->slot[k];
        if (!s->used || !s->pending) continue;
        if (from_harness) run_from_harness(k);
        else run_from_main(k);
    }
}

void threads_join_pending(void) { join_pending(1); }

static void join_now(void) { join_pending(K->in_gate ? 1 : 0); }

/* A worker parks: save its context record, hand the baton back to slot 0. */
static void park(u32 how) {
    u32 k = K->cur;
    if (k == 0) { slog("park from slot 0 (how=%u): ignored", how); return; }
    SH->slot[k].state = how;
    K->next = 0;
    K->reason[k] = R_HANDOFF;
    kst_switch(&K->hg->slot[k].ctx, &K->hctx[k]);
    /* handed the baton again by a join */
    SH->slot[k].state = W_RUNNING;
}

static void thread_done(u32 k, DWORD code) {
    vslot_t *s = &SH->slot[k];
    s->exit_code = code;
    if (s->vh >= VH_BASE && s->vh < VH_BASE + 4 * NVH) {
        vh_t *v = &SH->vh[(s->vh - VH_BASE) >> 2];
        if (v->type == VH_THREAD && v->slot == k) { v->signaled = 1; v->exit_code = code; }
    }
    park(W_EXITED);
    slog("exited slot %u resumed", k);
    TerminateProcess(GetCurrentProcess(), 5);
}

static void slot_entry(void *arg) {
    u32 k = (u32)(uintptr_t)arg;
    vslot_t *s = &SH->slot[k];
    DWORD code = ((LPTHREAD_START_ROUTINE)(uintptr_t)s->start)((LPVOID)(uintptr_t)s->arg);
    thread_done(k, code);
}

/* ---------------------------------------------------------------- CreateThread and friends */

static HANDLE WINAPI hk_CreateThread(LPSECURITY_ATTRIBUTES sa, SIZE_T stack, LPTHREAD_START_ROUTINE fn,
                                     LPVOID param, DWORD flags, LPDWORD ptid) {
    (void)sa;
    u32 k = 0;
    for (u32 i = 1; i < KST_MAX_SLOTS; i++)
        if (!SH->slot[i].used) { k = i; break; }
    if (!k) { slog("CreateThread: no free slot"); SetLastError(ERROR_NOT_ENOUGH_MEMORY); return NULL; }
    u32 size = (u32)stack;
    if (size < g_exe_stack_reserve) size = g_exe_stack_reserve;
    size = (size + 0xffffu) & ~0xffffu;
    void *stk = kst_valloc(size);
    vslot_t *s = &SH->slot[k];
    memset(s, 0, sizeof *s);
    s->used = 1;
    s->vtid = 0x104 + 4 * SH->nthreads++;
    s->start = (u32)(uintptr_t)fn;
    s->arg = (u32)(uintptr_t)param;
    s->stack = (u32)(uintptr_t)stk;
    s->stack_size = size;
    s->suspend = 1;
    s->state = W_PARK_SUSPEND;
    u32 h = vh_alloc(VH_THREAD);
    if (!h) { slog("CreateThread: no free handle"); return NULL; }
    SH->vh[(h - VH_BASE) >> 2].slot = k;
    s->vh = h;
    kst_slot_init(k, stk, size, slot_entry, (void *)(uintptr_t)k, KST_SLOT_RUNNABLE);
    if (ptid) *ptid = s->vtid;
    slog("CreateThread slot=%u start=%x flags=%x vtid=%x vh=%x stack=%x+%x caller=%x", k, s->start, flags, s->vtid,
         h, s->stack, size, (u32)(uintptr_t)__builtin_return_address(0));
    if (!(flags & CREATE_SUSPENDED)) {
        s->suspend = 0;
        if (K->in_gate) run_from_harness(k);
        else run_from_main(k);
    }
    return (HANDLE)(uintptr_t)h;
}

static vslot_t *slot_of(HANDLE h, u32 *kout) {
    u32 v = (u32)(uintptr_t)h;
    if (h == (HANDLE)(intptr_t)-2) { *kout = K->cur; return cur_vs(); }
    if (v < VH_BASE || v >= VH_BASE + 4 * NVH || (v & 3)) return 0;
    vh_t *e = &SH->vh[(v - VH_BASE) >> 2];
    if (e->type != VH_THREAD) return 0;
    *kout = e->slot;
    return &SH->slot[e->slot];
}

static DWORD WINAPI hk_ResumeThread(HANDLE h) {
    u32 k;
    vslot_t *s = slot_of(h, &k);
    if (!s) return r_ResumeThread(h);
    DWORD prev = s->suspend;
    if (s->suspend) s->suspend--;
    if (g_logs < 400) {
        g_logs++;
        slog("ResumeThread slot=%u prev=%u state=%u from %x depth=%u tick=%u", k, prev, s->state,
             (u32)(uintptr_t)__builtin_return_address(0), SH->main_cs_depth, (u32)g_gate.tick);
    }
    if (prev == 1 && s->state == W_PARK_SUSPEND && s->used) {
        s->pending = 1;
        SH->pending_any = 1;
        if (K->cur == 0 && SH->main_cs_depth == 0) join_now();
    }
    return prev;
}

static DWORD WINAPI hk_SuspendThread(HANDLE h) {
    u32 k;
    vslot_t *s = slot_of(h, &k);
    if (!s) return r_SuspendThread(h);
    DWORD prev = s->suspend++;
    if (k == K->cur && k != 0) park(W_PARK_SUSPEND);
    return prev;
}

static void WINAPI hk_ExitThread(DWORD code) {
    u32 k = K->cur;
    if (k == 0) { slog("ExitThread on the main slot"); r_ExitThread(code); }
    thread_done(k, code);
}

static BOOL WINAPI hk_GetExitCodeThread(HANDLE h, LPDWORD code) {
    u32 v = (u32)(uintptr_t)h;
    if (v >= VH_BASE && v < VH_BASE + 4 * NVH) {
        vh_t *e = &SH->vh[(v - VH_BASE) >> 2];
        if (e->type == VH_THREAD) {
            if (code) *code = e->signaled ? e->exit_code : STILL_ACTIVE;
            return TRUE;
        }
    }
    return r_GetExitCodeThread(h, code);
}

static BOOL WINAPI hk_SetThreadPriority(HANDLE h, int p) {
    u32 k;
    if (slot_of(h, &k)) return TRUE;
    return r_SetThreadPriority(h, p);
}

/* ---------------------------------------------------------------- events and waits */

static vh_t *vh_of(HANDLE h) {
    u32 v = (u32)(uintptr_t)h;
    if (v < VH_BASE || v >= VH_BASE + 4 * NVH || (v & 3)) return 0;
    vh_t *e = &SH->vh[(v - VH_BASE) >> 2];
    return e->type ? e : 0;
}

static HANDLE WINAPI hk_CreateEventA(LPSECURITY_ATTRIBUTES sa, BOOL manual, BOOL initial, LPCSTR name) {
    (void)sa;
    u32 h = vh_alloc(VH_EVENT);
    if (!h) return NULL;
    vh_t *e = &SH->vh[(h - VH_BASE) >> 2];
    e->manual = manual ? 1 : 0;
    e->signaled = initial ? 1 : 0;
    slog("CreateEventA vh=%x manual=%d initial=%d name=%s", h, manual, initial, name ? name : "-");
    SetLastError(0);
    return (HANDLE)(uintptr_t)h;
}

static BOOL WINAPI hk_SetEvent(HANDLE h) {
    vh_t *e = vh_of(h);
    if (!e || e->type != VH_EVENT) return r_SetEvent(h);
    int woke = 0;
    for (u32 k = 1; k < KST_MAX_SLOTS; k++) {
        vslot_t *s = &SH->slot[k];
        if (!s->used || s->state != W_PARK_WAIT || s->wait_vh != (u32)(uintptr_t)h) continue;
        s->pending = 1;
        s->wait_vh = 0;
        SH->pending_any = 1;
        woke = 1;
        if (!e->manual) break;
    }
    if (e->manual || !woke) e->signaled = 1;
    if (woke && K->cur == 0 && SH->main_cs_depth == 0) join_now();
    if (woke && K->cur != 0) slog("SetEvent from slot %u woke a worker (joined by the main slot later)", K->cur);
    return TRUE;
}

HANDLE vh_real_mutex(u32 h);

static DWORD WINAPI hk_WaitForSingleObject(HANDLE h, DWORD ms) {
    vh_t *e = vh_of(h);
    if (!e) return r_Wait(h, ms);
    if (e->type == VH_EVENT) {
        if (e->signaled) {
            if (!e->manual) e->signaled = 0;
            return WAIT_OBJECT_0;
        }
        if (ms == 0) return WAIT_TIMEOUT;
        if (K->cur != 0) {
            cur_vs()->wait_vh = (u32)(uintptr_t)h;
            park(W_PARK_WAIT);
            return WAIT_OBJECT_0;   /* woken by SetEvent, which consumed an auto-reset signal */
        }
        /* the main slot waits: let pending workers run, then re-check */
        join_now();
        if (e->signaled) {
            if (!e->manual) e->signaled = 0;
            return WAIT_OBJECT_0;
        }
        slog("main slot waits on unsignaled event vh=%x ms=%u: WAIT_TIMEOUT", (u32)(uintptr_t)h, ms);
        return WAIT_TIMEOUT;
    }
    if (e->type == VH_THREAD) {
        if (!e->signaled && K->cur == 0) join_now();
        if (e->signaled) return WAIT_OBJECT_0;
        if (ms == 0) return WAIT_TIMEOUT;
        slog("wait on a parked thread vh=%x ms=%u: WAIT_TIMEOUT", (u32)(uintptr_t)h, ms);
        return WAIT_TIMEOUT;
    }
    if (e->type == VH_MUTEX) return r_Wait(vh_real_mutex((u32)(uintptr_t)h), ms);
    return WAIT_OBJECT_0;
}

/* ---------------------------------------------------------------- critical sections */

static void WINAPI hk_InitializeCriticalSection(LPCRITICAL_SECTION cs) {
    /* No DebugInfo: nothing is allocated in the process heap, so the section
     * holds no pointer into ambient memory (contract 3.4.3). Serial scheduling
     * means it is never contended, so no LockSemaphore is ever created. */
    if (r_InitCSEx && r_InitCSEx(cs, 0, 0x01000000u /* CRITICAL_SECTION_NO_DEBUG_INFO */)) return;
    InitializeCriticalSection(cs);
}

/* The CRT caches this GetProcAddress result in 0x833fcc. Serve the same
 * fixed shim entry on every runtime and keep DebugInfo out of game memory. */
BOOL WINAPI threads_init_cs_spin(LPCRITICAL_SECTION cs, DWORD spin) {
    if (r_InitCSEx) return r_InitCSEx(cs, spin, 0x01000000u);
    return InitializeCriticalSectionAndSpinCount(cs, spin);
}

static void WINAPI hk_Enter(LPCRITICAL_SECTION cs) {
    if (K->cur == 0) SH->main_cs_depth++;
    else cur_vs()->cs_depth++;
    r_Enter(cs);
}

static void WINAPI hk_Leave(LPCRITICAL_SECTION cs) {
    r_Leave(cs);
    if (K->cur == 0) {
        if (SH->main_cs_depth) SH->main_cs_depth--;
        if (!SH->main_cs_depth && SH->pending_any) join_now();
    } else {
        vslot_t *s = cur_vs();
        if (s->cs_depth) s->cs_depth--;
    }
}

/* ---------------------------------------------------------------- TLS and ids */

static DWORD WINAPI hk_TlsAlloc(void) {
    /* These indices address the captured per-slot table, never Wine's TLS.
     * Allocating a host index leaked DLL startup order into the CRT's globals
     * (21 on Wine 11, 3 on Wine 8) despite entirely virtual Get/SetValue. */
    for (DWORD i = 0; i < 64; i++) {
        u32 mask = 1u << (i & 31u);
        if (SH->tls_used[i >> 5] & mask) continue;
        SH->tls_used[i >> 5] |= mask;
        for (u32 k = 0; k < KST_MAX_SLOTS; k++) SH->slot[k].tls[i] = 0;
        return i;
    }
    SetLastError(ERROR_NOT_ENOUGH_MEMORY);
    return TLS_OUT_OF_INDEXES;
}
static BOOL WINAPI hk_TlsFree(DWORD i) {
    if (i >= 64 || !(SH->tls_used[i >> 5] & (1u << (i & 31u)))) {
        SetLastError(ERROR_INVALID_PARAMETER);
        return FALSE;
    }
    SH->tls_used[i >> 5] &= ~(1u << (i & 31u));
    for (u32 k = 0; k < KST_MAX_SLOTS; k++) SH->slot[k].tls[i] = 0;
    return TRUE;
}
static LPVOID WINAPI hk_TlsGetValue(DWORD i) {
    if (i >= 64) { SetLastError(ERROR_INVALID_PARAMETER); return NULL; }
    SetLastError(0);
    return (LPVOID)(uintptr_t)cur_vs()->tls[i];
}
static BOOL WINAPI hk_TlsSetValue(DWORD i, LPVOID v) {
    if (i >= 64) { SetLastError(ERROR_INVALID_PARAMETER); return FALSE; }
    cur_vs()->tls[i] = (u32)(uintptr_t)v;
    return TRUE;
}

static DWORD WINAPI hk_GetCurrentThreadId(void) { return vtid_current(); }

#define HOOK(fn, h, save)                                                        \
    do {                                                                         \
        void *o = iat_hook(exe, "KERNEL32.dll", fn, (void *)(h));                \
        if (o) { if (save) *(void **)(save) = o; hooks_note("exe:" fn); }        \
        else slog("iat miss exe %s", fn);                                        \
    } while (0)

void threads_install(void) {
    HMODULE exe = GetModuleHandleA(NULL);
    IMAGE_NT_HEADERS *nt = (IMAGE_NT_HEADERS *)((u8 *)exe + ((IMAGE_DOS_HEADER *)exe)->e_lfanew);
    g_exe_stack_reserve = nt->OptionalHeader.SizeOfStackReserve;
    if (g_exe_stack_reserve < (256u << 10)) g_exe_stack_reserve = 256u << 10;
    g_exe_stack_reserve = (g_exe_stack_reserve + 0xffffu) & ~0xffffu;
    r_InitCSEx = (icsex_t)(void *)GetProcAddress(GetModuleHandleA("kernel32.dll"), "InitializeCriticalSectionEx");
    SH->slot[0].used = 1;
    SH->slot[0].vtid = 0x100;
    SH->slot[0].state = W_RUNNING;
    void *none = 0;
    (void)none;
    HOOK("CreateThread", hk_CreateThread, 0);
    HOOK("ResumeThread", hk_ResumeThread, &r_ResumeThread);
    HOOK("SuspendThread", hk_SuspendThread, &r_SuspendThread);
    HOOK("ExitThread", hk_ExitThread, &r_ExitThread);
    HOOK("GetExitCodeThread", hk_GetExitCodeThread, &r_GetExitCodeThread);
    HOOK("SetThreadPriority", hk_SetThreadPriority, &r_SetThreadPriority);
    HOOK("WaitForSingleObject", hk_WaitForSingleObject, &r_Wait);
    HOOK("SetEvent", hk_SetEvent, &r_SetEvent);
    HOOK("CreateEventA", hk_CreateEventA, 0);
    HOOK("InitializeCriticalSection", hk_InitializeCriticalSection, 0);
    HOOK("EnterCriticalSection", hk_Enter, &r_Enter);
    HOOK("LeaveCriticalSection", hk_Leave, &r_Leave);
    HOOK("TlsAlloc", hk_TlsAlloc, 0);
    HOOK("TlsFree", hk_TlsFree, 0);
    HOOK("TlsGetValue", hk_TlsGetValue, 0);
    HOOK("TlsSetValue", hk_TlsSetValue, 0);
    HOOK("GetCurrentThreadId", hk_GetCurrentThreadId, 0);
    slog("threads: exe stack reserve %u, InitializeCriticalSectionEx %s", g_exe_stack_reserve,
         r_InitCSEx ? "found" : "missing");
}

} /* extern "C" */
