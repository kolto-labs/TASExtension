/* Virtual handles (contract 3.5, "Handles").
 *
 * Every handle the exe gets for a file, a find, an event, a mutex or a thread
 * is a virtual value from a table in harness_game, so the values that reach
 * sim memory depend only on the game's own sequence of calls, and a restore
 * rewinds the table with memory.
 *
 * Files. The virtual object holds the path, access, share mode and flags and a
 * virtual position; reads and writes use explicit offsets. Real handles live in
 * harness_core and are opened and closed freely (closed for real on
 * CloseHandle, so DeleteFileA and MoveFileA behave as in vanilla). After a
 * restore every real file and find handle is closed; a virtual handle T had
 * open reopens by path (OPEN_EXISTING) on its next use.
 *
 * File times. Files the game wrote get their times set to the virtual clock
 * when they are closed, and find data reports the write time for all three
 * times, so directory listings carry no wall-clock or access-time values. */
#include "shim.h"

enum { RK_NONE = 0, RK_FILE = 1, RK_FIND = 2, RK_MUTEX = 3 };
typedef struct { HANDLE real; u32 serial; u32 find_n; u32 kind; char path[264]; } rh_t;
static rh_t g_rh[NVH];
static u32 g_reopens, g_logs;

static HANDLE (WINAPI *r_CreateFileA)(LPCSTR, DWORD, DWORD, LPSECURITY_ATTRIBUTES, DWORD, DWORD, HANDLE);
static BOOL (WINAPI *r_ReadFile)(HANDLE, LPVOID, DWORD, LPDWORD, LPOVERLAPPED);
static BOOL (WINAPI *r_WriteFile)(HANDLE, LPCVOID, DWORD, LPDWORD, LPOVERLAPPED);
static DWORD (WINAPI *r_SetFilePointer)(HANDLE, LONG, PLONG, DWORD);
static BOOL (WINAPI *r_SetEndOfFile)(HANDLE);
static BOOL (WINAPI *r_FlushFileBuffers)(HANDLE);
static DWORD (WINAPI *r_GetFileType)(HANDLE);
static BOOL (WINAPI *r_CloseHandle)(HANDLE);
static HANDLE (WINAPI *r_FindFirstFileA)(LPCSTR, LPWIN32_FIND_DATAA);
static BOOL (WINAPI *r_FindNextFileA)(HANDLE, LPWIN32_FIND_DATAA);
static BOOL (WINAPI *r_FindClose)(HANDLE);
static HANDLE (WINAPI *r_CreateMutexA)(LPSECURITY_ATTRIBUTES, BOOL, LPCSTR);

static inline u32 vidx(u32 h) { return (h - VH_BASE) >> 2; }

static vh_t *vh_get(HANDLE h, u8 type) {
    u32 v = (u32)(uintptr_t)h;
    if (v < VH_BASE || v >= VH_BASE + 4 * NVH || (v & 3)) return 0;
    vh_t *e = &SH->vh[vidx(v)];
    if (!e->type || (type && e->type != type)) return 0;
    return e;
}

u32 vh_alloc(u8 type) {
    for (u32 i = 1; i < NVH; i++) {
        vh_t *e = &SH->vh[i];
        if (e->type) continue;
        memset(e, 0, sizeof *e);
        e->type = type;
        e->serial = ++SH->vh_serial;
        return VH_BASE + 4 * i;
    }
    slog("virtual handle table full");
    return 0;
}

void vh_free(u32 h) {
    if (h < VH_BASE || h >= VH_BASE + 4 * NVH) return;
    memset(&SH->vh[vidx(h)], 0, sizeof(vh_t));
}

static int str_eq(const char *a, const char *b) { return k_streq(a, b); }

static void copy_path(char *dst, const char *src) {
    u32 n = (u32)strlen(src);
    if (n > 263) n = 263;
    memcpy(dst, src, n);
    dst[n] = 0;
}

/* The real handle behind virtual file i, reopened by path when the core entry
 * belongs to another incarnation (after a restore, or never opened here). */
static HANDLE real_file(u32 i) {
    vh_t *e = &SH->vh[i];
    rh_t *r = &g_rh[i];
    if (r->real && r->serial == e->serial && str_eq(r->path, e->path)) return r->real;
    if (r->real) { r_CloseHandle(r->real); r->real = 0; }
    DWORD le = GetLastError();
    HANDLE h = r_CreateFileA(e->path, e->access, e->share, NULL, OPEN_EXISTING,
                             e->flags & ~(DWORD)FILE_FLAG_DELETE_ON_CLOSE, NULL);
    SetLastError(le);
    g_reopens++;
    slog("reopen vh=%x serial=%u path=%s -> %s", VH_BASE + 4 * i, e->serial, e->path,
         h == INVALID_HANDLE_VALUE ? "FAILED" : "ok");
    if (h == INVALID_HANDLE_VALUE) return INVALID_HANDLE_VALUE;
    r->real = h;
    r->serial = e->serial;
    r->kind = RK_FILE;
    r->find_n = 0;
    copy_path(r->path, e->path);
    return h;
}

static void pin_times(LPWIN32_FIND_DATAA d) {
    d->ftCreationTime = d->ftLastWriteTime;
    d->ftLastAccessTime = d->ftLastWriteTime;
}

/* ---------------------------------------------------------------- files */

static HANDLE WINAPI hk_CreateFileA(LPCSTR path, DWORD access, DWORD share, LPSECURITY_ATTRIBUTES sa, DWORD disp,
                                   DWORD flags, HANDLE tmpl) {
    HANDLE r = r_CreateFileA(path, access, share, sa, disp, flags, tmpl);
    if (r == INVALID_HANDLE_VALUE) {
        if (g_logs < 2000) { g_logs++; slog("CreateFileA %s disp=%u -> fail %u tick=%u", path, disp, GetLastError(), (u32)g_gate.tick); }
        return r;
    }
    DWORD err = GetLastError();
    if (r_GetFileType(r) != FILE_TYPE_DISK) { SetLastError(err); return r; }
    u32 h = vh_alloc(VH_FILE);
    if (!h) { SetLastError(err); return r; }
    u32 i = vidx(h);
    vh_t *e = &SH->vh[i];
    copy_path(e->path, path);
    e->access = access;
    e->share = share;
    e->flags = flags;
    e->pos = 0;
    g_rh[i].real = r;
    g_rh[i].serial = e->serial;
    g_rh[i].kind = RK_FILE;
    g_rh[i].find_n = 0;
    copy_path(g_rh[i].path, path);
    if (g_logs < 2000) {
        g_logs++;
        slog("CreateFileA %s acc=%x disp=%u -> vh=%x serial=%u tick=%u", path, access, disp, h, e->serial, (u32)g_gate.tick);
    }
    SetLastError(err);
    return (HANDLE)(uintptr_t)h;
}

static BOOL WINAPI hk_ReadFile(HANDLE h, LPVOID buf, DWORD n, LPDWORD got, LPOVERLAPPED ov) {
    vh_t *e = vh_get(h, VH_FILE);
    if (!e) return r_ReadFile(h, buf, n, got, ov);
    u32 i = (u32)(e - SH->vh);
    HANDLE r = real_file(i);
    if (r == INVALID_HANDLE_VALUE) { if (got) *got = 0; SetLastError(ERROR_INVALID_HANDLE); return FALSE; }
    if (ov) { slog("ReadFile with OVERLAPPED on vh=%x", (u32)(uintptr_t)h); return r_ReadFile(r, buf, n, got, ov); }
    OVERLAPPED o;
    memset(&o, 0, sizeof o);
    o.Offset = (DWORD)e->pos;
    o.OffsetHigh = (DWORD)(e->pos >> 32);
    DWORD g = 0;
    BOOL ok = r_ReadFile(r, buf, n, &g, &o);
    if (!ok && GetLastError() == ERROR_HANDLE_EOF) { ok = TRUE; g = 0; SetLastError(0); }
    e->pos += g;
    if (got) *got = g;
    return ok;
}

static BOOL WINAPI hk_WriteFile(HANDLE h, LPCVOID buf, DWORD n, LPDWORD put, LPOVERLAPPED ov) {
    vh_t *e = vh_get(h, VH_FILE);
    if (!e) return r_WriteFile(h, buf, n, put, ov);
    u32 i = (u32)(e - SH->vh);
    HANDLE r = real_file(i);
    if (r == INVALID_HANDLE_VALUE) { if (put) *put = 0; SetLastError(ERROR_INVALID_HANDLE); return FALSE; }
    if (ov) { slog("WriteFile with OVERLAPPED on vh=%x", (u32)(uintptr_t)h); return r_WriteFile(r, buf, n, put, ov); }
    OVERLAPPED o;
    memset(&o, 0, sizeof o);
    o.Offset = (DWORD)e->pos;
    o.OffsetHigh = (DWORD)(e->pos >> 32);
    DWORD p = 0;
    BOOL ok = r_WriteFile(r, buf, n, &p, &o);
    e->pos += p;
    e->written = 1;
    if (put) *put = p;
    return ok;
}

static DWORD WINAPI hk_SetFilePointer(HANDLE h, LONG lo, PLONG phi, DWORD method) {
    vh_t *e = vh_get(h, VH_FILE);
    if (!e) return r_SetFilePointer(h, lo, phi, method);
    i64 dist = phi ? (i64)(((u64)(u32)*phi << 32) | (u32)lo) : (i64)lo;
    i64 base = 0;
    if (method == FILE_CURRENT) base = (i64)e->pos;
    else if (method == FILE_END) {
        HANDLE r = real_file((u32)(e - SH->vh));
        LARGE_INTEGER sz;
        if (r == INVALID_HANDLE_VALUE || !GetFileSizeEx(r, &sz)) { SetLastError(ERROR_INVALID_HANDLE); return INVALID_SET_FILE_POINTER; }
        base = sz.QuadPart;
    }
    i64 np = base + dist;
    if (np < 0) { SetLastError(ERROR_NEGATIVE_SEEK); return INVALID_SET_FILE_POINTER; }
    e->pos = (u64)np;
    if (phi) *phi = (LONG)((u64)np >> 32);
    SetLastError(NO_ERROR);
    return (DWORD)np;
}

static BOOL WINAPI hk_SetEndOfFile(HANDLE h) {
    vh_t *e = vh_get(h, VH_FILE);
    if (!e) return r_SetEndOfFile(h);
    HANDLE r = real_file((u32)(e - SH->vh));
    if (r == INVALID_HANDLE_VALUE) { SetLastError(ERROR_INVALID_HANDLE); return FALSE; }
    LARGE_INTEGER p;
    p.QuadPart = (LONGLONG)e->pos;
    SetFilePointerEx(r, p, NULL, FILE_BEGIN);
    e->written = 1;
    return r_SetEndOfFile(r);
}

static BOOL WINAPI hk_FlushFileBuffers(HANDLE h) {
    vh_t *e = vh_get(h, VH_FILE);
    if (!e) return r_FlushFileBuffers(h);
    HANDLE r = real_file((u32)(e - SH->vh));
    return r == INVALID_HANDLE_VALUE ? FALSE : r_FlushFileBuffers(r);
}

static DWORD WINAPI hk_GetFileType(HANDLE h) {
    if (vh_get(h, VH_FILE)) return FILE_TYPE_DISK;
    return r_GetFileType(h);
}

static void close_file(u32 i) {
    vh_t *e = &SH->vh[i];
    rh_t *r = &g_rh[i];
    if (e->written) {
        HANDLE rh = real_file(i);
        if (rh != INVALID_HANDLE_VALUE) {
            u64 v = g_clk.initial_ft + 10ull * g_clk.v;
            FILETIME ft;
            ft.dwLowDateTime = (DWORD)v;
            ft.dwHighDateTime = (DWORD)(v >> 32);
            SetFileTime(rh, &ft, &ft, &ft);
        }
    }
    if (r->real && r->serial == e->serial) { r_CloseHandle(r->real); r->real = 0; }
}

/* ---------------------------------------------------------------- finds */

static HANDLE real_find(u32 i, LPWIN32_FIND_DATAA d) {
    vh_t *e = &SH->vh[i];
    rh_t *r = &g_rh[i];
    if (r->real && r->serial == e->serial && str_eq(r->path, e->path) && r->find_n == e->find_n) return r->real;
    if (r->real) { r_FindClose(r->real); r->real = 0; }
    HANDLE h = r_FindFirstFileA(e->path, d);
    g_reopens++;
    slog("find reopen vh=%x pattern=%s skip=%u", VH_BASE + 4 * i, e->path, e->find_n);
    if (h == INVALID_HANDLE_VALUE) return h;
    for (u32 k = 1; k < e->find_n; k++)
        if (!r_FindNextFileA(h, d)) break;
    r->real = h;
    r->serial = e->serial;
    r->find_n = e->find_n;
    r->kind = RK_FIND;
    copy_path(r->path, e->path);
    return h;
}

static HANDLE WINAPI hk_FindFirstFileA(LPCSTR pat, LPWIN32_FIND_DATAA d) {
    HANDLE r = r_FindFirstFileA(pat, d);
    if (r == INVALID_HANDLE_VALUE) return r;
    DWORD err = GetLastError();
    pin_times(d);
    u32 h = vh_alloc(VH_FIND);
    if (!h) return r;
    u32 i = vidx(h);
    vh_t *e = &SH->vh[i];
    copy_path(e->path, pat);
    e->find_n = 1;
    g_rh[i].real = r;
    g_rh[i].serial = e->serial;
    g_rh[i].find_n = 1;
    g_rh[i].kind = RK_FIND;
    copy_path(g_rh[i].path, pat);
    if (g_logs < 2000) { g_logs++; slog("FindFirstFileA %s -> vh=%x first=%s tick=%u", pat, h, d->cFileName, (u32)g_gate.tick); }
    SetLastError(err);
    return (HANDLE)(uintptr_t)h;
}

static BOOL WINAPI hk_FindNextFileA(HANDLE h, LPWIN32_FIND_DATAA d) {
    vh_t *e = vh_get(h, VH_FIND);
    if (!e) return r_FindNextFileA(h, d);
    u32 i = (u32)(e - SH->vh);
    HANDLE r = real_find(i, d);
    if (r == INVALID_HANDLE_VALUE) { SetLastError(ERROR_NO_MORE_FILES); return FALSE; }
    BOOL ok = r_FindNextFileA(r, d);
    if (ok) {
        pin_times(d);
        e->find_n++;
        g_rh[i].find_n = e->find_n;
    }
    return ok;
}

static BOOL WINAPI hk_FindClose(HANDLE h) {
    vh_t *e = vh_get(h, VH_FIND);
    if (!e) return r_FindClose(h);
    u32 i = (u32)(e - SH->vh);
    if (g_rh[i].real && g_rh[i].serial == e->serial) { r_FindClose(g_rh[i].real); g_rh[i].real = 0; }
    vh_free((u32)(uintptr_t)h);
    return TRUE;
}

/* ---------------------------------------------------------------- mutexes */

static HANDLE WINAPI hk_CreateMutexA(LPSECURITY_ATTRIBUTES sa, BOOL own, LPCSTR name) {
    HANDLE r = r_CreateMutexA(sa, own, name);
    DWORD err = GetLastError();
    if (!r) return r;
    u32 h = vh_alloc(VH_MUTEX);
    if (!h) { SetLastError(err); return r; }
    u32 i = vidx(h);
    g_rh[i].real = r;
    g_rh[i].serial = SH->vh[i].serial;
    g_rh[i].path[0] = 0;
    g_rh[i].kind = RK_MUTEX;
    g_rh[i].find_n = 0;
    if (name) copy_path(SH->vh[i].path, name);
    slog("CreateMutexA %s -> vh=%x err=%u", name ? name : "-", h, err);
    SetLastError(err);
    return (HANDLE)(uintptr_t)h;
}

HANDLE vh_real_mutex(u32 h) {
    u32 i = vidx(h);
    return g_rh[i].real;
}

/* ---------------------------------------------------------------- CloseHandle */

static BOOL WINAPI hk_CloseHandle(HANDLE h) {
    vh_t *e = vh_get(h, 0);
    if (!e) return r_CloseHandle(h);
    u32 i = (u32)(e - SH->vh);
    switch (e->type) {
    case VH_FILE: close_file(i); break;
    case VH_FIND:
        if (g_rh[i].real && g_rh[i].serial == e->serial) { r_FindClose(g_rh[i].real); g_rh[i].real = 0; }
        break;
    case VH_MUTEX: break;   /* the named mutex lives for the session */
    case VH_THREAD: case VH_EVENT: default: break;
    }
    vh_free((u32)(uintptr_t)h);
    return TRUE;
}

/* After a restore: every real file and find handle is closed; the virtual
 * handles T owns reopen by path on their next use. */
void vhandle_reconcile(void) {
    u32 files = 0, finds = 0;
    for (u32 i = 0; i < NVH; i++) {
        rh_t *r = &g_rh[i];
        if (!r->real) continue;
        if (r->kind == RK_FIND) { r_FindClose(r->real); finds++; }
        else if (r->kind == RK_FILE) { r_CloseHandle(r->real); files++; }
        else continue;   /* named mutexes live for the session */
        r->real = 0;
    }
    slog("reconcile: closed %u real file and %u find handles (reopens so far %u)", files, finds, g_reopens);
}

#define HOOK(fn, h, save)                                                        \
    do {                                                                         \
        void *o = iat_hook(exe, "KERNEL32.dll", fn, (void *)(h));                \
        if (o) { *(void **)(save) = o; hooks_note("exe:" fn); }                  \
        else slog("iat miss exe %s", fn);                                        \
    } while (0)

void vhandle_install(void) {
    HMODULE exe = GetModuleHandleA(NULL);
    HOOK("CreateFileA", hk_CreateFileA, &r_CreateFileA);
    HOOK("ReadFile", hk_ReadFile, &r_ReadFile);
    HOOK("WriteFile", hk_WriteFile, &r_WriteFile);
    HOOK("SetFilePointer", hk_SetFilePointer, &r_SetFilePointer);
    HOOK("SetEndOfFile", hk_SetEndOfFile, &r_SetEndOfFile);
    HOOK("FlushFileBuffers", hk_FlushFileBuffers, &r_FlushFileBuffers);
    HOOK("GetFileType", hk_GetFileType, &r_GetFileType);
    HOOK("CloseHandle", hk_CloseHandle, &r_CloseHandle);
    HOOK("FindFirstFileA", hk_FindFirstFileA, &r_FindFirstFileA);
    HOOK("FindNextFileA", hk_FindNextFileA, &r_FindNextFileA);
    HOOK("FindClose", hk_FindClose, &r_FindClose);
    HOOK("CreateMutexA", hk_CreateMutexA, &r_CreateMutexA);
}
