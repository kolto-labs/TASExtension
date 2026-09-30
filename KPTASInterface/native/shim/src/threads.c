/* threads.model = "join" (contract C2, section 5.4), adapted to the locks the
 * exe holds across ResumeThread.
 *
 * Both gameplay ResumeThread sites hold a critical section across the call
 * (0x5ebc30 holds 0x8328b8, 0x40652c holds [resman+0x40]) and each worker's
 * first action is to enter that same lock. Joining inside ResumeThread would
 * deadlock. So a resume only marks the worker pending, and the main thread
 * joins at its next LeaveCriticalSection that brings its (exe-IAT) lock depth
 * to zero, or at the gate. The join waits until the worker parks again:
 * self-suspends, waits, or exits. A self-suspend is confirmed with a real
 * Suspend/Resume probe so a later ResumeThread is never lost. */
#include "shim.h"

#define MAXW 16
enum { W_RUNNING = 0, W_PARK_SUSPEND = 1, W_PARK_WAIT = 2, W_EXITED = 3 };
typedef struct {
    HANDLE h;      /* handle returned to the exe */
    HANDLE hdup;   /* our own handle, for waits and suspend probes */
    HANDLE park;   /* manual-reset event, set when the worker parks */
    HANDLE wait_on;
    u32 rtid, vtid, start;
    volatile LONG state;
    int pending;
} worker_t;

static worker_t g_w[MAXW];
u32 g_workers_n;
u32 g_joins, g_hang_trips;
u32 g_main_rtid;
static u32 g_cs_depth;
static int g_pending;
u32 g_hang_ms = 120000;

static HANDLE (WINAPI *r_CreateThread)(LPSECURITY_ATTRIBUTES, SIZE_T, LPTHREAD_START_ROUTINE, LPVOID, DWORD, LPDWORD);
static DWORD (WINAPI *r_ResumeThread)(HANDLE);
static DWORD (WINAPI *r_SuspendThread)(HANDLE);
static void (WINAPI *r_ExitThread)(DWORD);
static DWORD (WINAPI *r_Wait)(HANDLE, DWORD);
static void (WINAPI *r_Enter)(LPCRITICAL_SECTION);
static void (WINAPI *r_Leave)(LPCRITICAL_SECTION);
static BOOL (WINAPI *r_SetEvent)(HANDLE);

static worker_t *by_rtid(u32 t) {
    for (u32 i = 0; i < g_workers_n; i++) if (g_w[i].rtid == t) return &g_w[i];
    return 0;
}
static worker_t *by_handle(HANDLE h) {
    for (u32 i = 0; i < g_workers_n; i++) if (g_w[i].h == h) return &g_w[i];
    return 0;
}

u32 vtid_current(void) {
    u32 t = cur_rtid();
    if (t == g_main_rtid) return 0x100;
    worker_t *w = by_rtid(t);
    return w ? w->vtid : t;
}

static void join(worker_t *w) {
    HANDLE hs[2] = {w->park, w->hdup};
    DWORD t0 = GetTickCount();
    DWORD r = WaitForMultipleObjects(2, hs, FALSE, g_hang_ms);
    DWORD dt = GetTickCount() - t0;
    if (g_joins < 200 || dt > 1000)
        slog("join vtid=%x start=%x r=%u state=%d waited=%ums tick=%u", w->vtid, w->start, r, (int)w->state, dt, (u32)g_gate.tick);
    if (r == WAIT_TIMEOUT) {
        g_hang_trips++;
        slog("hang guard: worker vtid=%x start=%x state=%d", w->vtid, w->start, (int)w->state);
        w->pending = 0;
        return;
    }
    if (w->state == W_PARK_SUSPEND) {
        for (int i = 0; i < 1000000; i++) {
            DWORD c = SuspendThread(w->hdup);
            if (c == (DWORD)-1) break;
            ResumeThread(w->hdup);
            if (c >= 1) break;
            SwitchToThread();
        }
    }
    w->pending = 0;
    g_joins++;
}

void threads_join_pending(void) {
    if (!g_pending) return;
    g_pending = 0;
    for (u32 i = 0; i < g_workers_n; i++)
        if (g_w[i].pending) join(&g_w[i]);
}

static HANDLE WINAPI hk_CreateThread(LPSECURITY_ATTRIBUTES sa, SIZE_T stack, LPTHREAD_START_ROUTINE fn,
                                     LPVOID param, DWORD flags, LPDWORD ptid) {
    DWORD rtid = 0;
    HANDLE h = r_CreateThread(sa, stack, fn, param, flags | CREATE_SUSPENDED, &rtid);
    if (!h || g_workers_n >= MAXW) {
        if (ptid) *ptid = rtid;
        if (h && !(flags & CREATE_SUSPENDED)) r_ResumeThread(h);
        return h;
    }
    worker_t *w = &g_w[g_workers_n];
    memset(w, 0, sizeof *w);
    w->h = h;
    w->rtid = rtid;
    w->vtid = 0x104 + 4 * g_workers_n;
    w->start = (u32)(uintptr_t)fn;
    DuplicateHandle(GetCurrentProcess(), h, GetCurrentProcess(), &w->hdup, 0, FALSE, DUPLICATE_SAME_ACCESS);
    w->park = CreateEventA(NULL, TRUE, FALSE, NULL);
    w->state = W_PARK_SUSPEND;
    g_workers_n++;
    if (ptid) *ptid = w->vtid;
    slog("CreateThread start=%x flags=%x vtid=%x caller=%x", w->start, flags, w->vtid,
         (u32)(uintptr_t)__builtin_return_address(0));
    if (!(flags & CREATE_SUSPENDED)) {
        w->state = W_RUNNING;
        r_ResumeThread(h);
        join(w);
    }
    return h;
}

static DWORD WINAPI hk_ResumeThread(HANDLE h) {
    worker_t *w = by_handle(h);
    if (!w || cur_rtid() != g_main_rtid) return r_ResumeThread(h);
    if (g_joins < 200) slog("ResumeThread vtid=%x from %x depth=%u", w->vtid, (u32)(uintptr_t)__builtin_return_address(0), g_cs_depth);
    ResetEvent(w->park);
    InterlockedExchange(&w->state, W_RUNNING);
    DWORD r = r_ResumeThread(h);
    w->pending = 1;
    g_pending = 1;
    if (g_cs_depth == 0) threads_join_pending();
    return r;
}

static DWORD WINAPI hk_SuspendThread(HANDLE h) {
    worker_t *w = by_rtid(cur_rtid());
    if (w && (h == w->h || h == (HANDLE)(intptr_t)-2)) {
        InterlockedExchange(&w->state, W_PARK_SUSPEND);
        SetEvent(w->park);
    }
    return r_SuspendThread(h);
}

static void WINAPI hk_ExitThread(DWORD code) {
    worker_t *w = by_rtid(cur_rtid());
    if (w) {
        InterlockedExchange(&w->state, W_EXITED);
        SetEvent(w->park);
    }
    r_ExitThread(code);
}

static DWORD WINAPI hk_WaitForSingleObject(HANDLE h, DWORD ms) {
    worker_t *w = by_rtid(cur_rtid());
    if (!w) return r_Wait(h, ms);
    w->wait_on = h;
    InterlockedExchange(&w->state, W_PARK_WAIT);
    SetEvent(w->park);
    DWORD r = r_Wait(h, ms);
    InterlockedExchange(&w->state, W_RUNNING);
    return r;
}

static BOOL WINAPI hk_SetEvent(HANDLE h) {
    if (cur_rtid() != g_main_rtid) return r_SetEvent(h);
    int woke = 0;
    for (u32 i = 0; i < g_workers_n; i++) {
        worker_t *w = &g_w[i];
        if (w->state == W_PARK_WAIT && w->wait_on == h) {
            ResetEvent(w->park);
            w->pending = 1;
            woke = 1;
        }
    }
    BOOL r = r_SetEvent(h);
    if (woke) {
        g_pending = 1;
        if (g_cs_depth == 0) threads_join_pending();
    }
    return r;
}

static void WINAPI hk_Enter(LPCRITICAL_SECTION cs) {
    if (cur_rtid() == g_main_rtid) g_cs_depth++;
    r_Enter(cs);
}

static void WINAPI hk_Leave(LPCRITICAL_SECTION cs) {
    r_Leave(cs);
    if (cur_rtid() == g_main_rtid) {
        if (g_cs_depth) g_cs_depth--;
        if (!g_cs_depth && g_pending) threads_join_pending();
    }
}

static DWORD WINAPI hk_GetCurrentThreadId(void) { return vtid_current(); }

#define HOOK(fn, h, save)                                                        \
    do {                                                                         \
        void *o = iat_hook(exe, "KERNEL32.dll", fn, (void *)(h));                \
        if (o) { *(void **)(save) = o; hooks_note("exe:" fn); }                  \
        else slog("iat miss exe %s", fn);                                        \
    } while (0)

void threads_install(void) {
    HMODULE exe = GetModuleHandleA(NULL);
    void *dummy;
    HOOK("CreateThread", hk_CreateThread, &r_CreateThread);
    HOOK("ResumeThread", hk_ResumeThread, &r_ResumeThread);
    HOOK("SuspendThread", hk_SuspendThread, &r_SuspendThread);
    HOOK("ExitThread", hk_ExitThread, &r_ExitThread);
    HOOK("WaitForSingleObject", hk_WaitForSingleObject, &r_Wait);
    HOOK("SetEvent", hk_SetEvent, &r_SetEvent);
    HOOK("EnterCriticalSection", hk_Enter, &r_Enter);
    HOOK("LeaveCriticalSection", hk_Leave, &r_Leave);
    HOOK("GetCurrentThreadId", hk_GetCurrentThreadId, &dummy);
}
