/* External call trace (diagnostics): every exe import the shim does not hook.
 *
 * Each such IAT slot is pointed at a small thunk. With the trace off the thunk
 * jumps straight to the real function. With it on (log verb, trace bit
 * TR_XCALL, inside the tick window) the call goes through xcall_enter, which
 * swaps the return address for xcall_post and keeps the real one on a shadow
 * stack per real thread; xcall_post records {tick, slot, import, caller, eax,
 * edx} and returns to the caller. Hooked imports are left alone: their answers
 * are virtual already, and some of them read their own return address.
 *
 * xcall_post touches no x87 or SSE register, so float returns pass through.
 * An exception that unwinds past a traced call would leave a stale shadow
 * entry; a record whose entry sits above the returning esp is marked (flag 1).
 * The records go to a file with the log verb's xdump. */
#include "shim.hpp"

// Retain the native C ABI, including CRT and assembly-referenced symbols.
extern "C" {

#define XMAX 1024u
#define XTHUNK 32u
#define XRING (1u << 21)          /* records */
#define XSHADOW 256u
#define XTHREADS 16u

typedef struct { u32 tick, id, caller, eax, edx, a0, a1, out; } xrec_t;
typedef struct { u32 rtid, n; u32 ret[XSHADOW], id[XSHADOW], sp[XSHADOW], a0[XSHADOW], a1[XSHADOW], outp[XSHADOW]; } xshadow_t;

/* per import: 1 = a GL call with no result (skipped under xmode bit 1), 2 = SetLastError or GetLastError
 * (skipped under xmode bit 2); out: 1-based index of an argument that points at a result dword */
static u8 g_xkind[XMAX], g_xout[XMAX];
u32 g_xmode = 3;           /* 4: count calls per tick and import instead of recording them */
#define XCTICKS 1024u
static u32 *g_xcount;         /* [tick - g_trace_from][id] while counting */

u8 g_xon;                         /* read by every thunk */
u32 g_xreal[XMAX];
static char *g_xname[XMAX];
static u32 g_xn;
static u8 *g_xcode;
static xrec_t *g_xring;
static u32 g_xpos, g_xlost;
static xshadow_t g_xsh[XTHREADS];

void xcall_post(void);

static xshadow_t *shadow(void) {
    u32 r = cur_rtid();
    for (u32 i = 0; i < XTHREADS; i++) {
        if (g_xsh[i].rtid == r) return &g_xsh[i];
        if (!g_xsh[i].rtid) { g_xsh[i].rtid = r; return &g_xsh[i]; }
    }
    return 0;
}

/* stack at entry: [pret] = caller's return address; returns the real target */
__attribute__((used)) u32 xcall_enter(u32 id, u32 *pret) {
    if (g_xmode & 4) {
        u32 t = static_cast<u32>(g_gate.tick) - g_trace_from;
        if (g_xcount && t < XCTICKS && trace_on(TR_XCALL)) g_xcount[t * XMAX + id]++;
        return g_xreal[id];
    }
    xshadow_t *s = shadow();
    if (!s || s->n >= XSHADOW || (g_xkind[id] & g_xmode) || !trace_on(TR_XCALL)) return g_xreal[id];
    s->ret[s->n] = *pret;
    s->id[s->n] = id;
    s->sp[s->n] = static_cast<u32>(reinterpret_cast<uintptr_t>(pret));
    /* arguments are read here: after a stdcall return xcall_post's own pushes overwrite them */
    s->a0[s->n] = pret[1];
    s->a1[s->n] = pret[2];
    s->outp[s->n] = g_xout[id] ? pret[g_xout[id]] : 0;
    s->n++;
    *pret = static_cast<u32>(reinterpret_cast<uintptr_t>(xcall_post));
    return g_xreal[id];
}

__attribute__((used)) u32 xcall_leave(u32 eax, u32 edx, u32 sp) {
    xshadow_t *s = shadow();
    s->n--;
    u32 flag = s->sp[s->n] >= sp;
    u32 ret = s->ret[s->n];
    if (g_xpos < XRING) {
        xrec_t *r = &g_xring[g_xpos++];
        r->tick = static_cast<u32>(g_gate.tick);
        r->id = s->id[s->n] | ((K ? K->cur : 0) << 16) | (flag << 24);
        r->caller = ret;
        r->eax = eax;
        r->edx = edx;
        r->a0 = s->a0[s->n];
        r->a1 = s->a1[s->n];
        u32 p = s->outp[s->n];
        r->out = 0;
        if (p) {
            MEMORY_BASIC_INFORMATION m;
            if (VirtualQuery((void *)reinterpret_cast<uintptr_t>(p), &m, sizeof m) && m.State == MEM_COMMIT) r->out = *reinterpret_cast<const u32 *>(reinterpret_cast<uintptr_t>(p));
        }
    } else g_xlost++;
    return ret;
}

/* [esp] = id, [esp+4] = caller's return address */
__attribute__((naked, used)) void xcall_slow(void) {
    __asm__ volatile("pushl %ecx\n\t"
                     "pushl %edx\n\t"
                     "pushl %eax\n\t"
                     "leal 16(%esp), %eax\n\t"
                     "pushl %eax\n\t"
                     "pushl 16(%esp)\n\t"
                     "call _xcall_enter\n\t"
                     "addl $8, %esp\n\t"
                     "movl %eax, 12(%esp)\n\t"
                     "popl %eax\n\t"
                     "popl %edx\n\t"
                     "popl %ecx\n\t"
                     "ret\n\t");
}

/* the real function returned here; [esp] is the caller's frame */
__attribute__((naked, used)) void xcall_post(void) {
    __asm__ volatile("subl $4, %esp\n\t"
                     "pushl %eax\n\t"
                     "pushl %edx\n\t"
                     "pushl %ecx\n\t"
                     "leal 16(%esp), %ecx\n\t"
                     "pushl %ecx\n\t"
                     "pushl %edx\n\t"
                     "pushl %eax\n\t"
                     "call _xcall_leave\n\t"
                     "addl $12, %esp\n\t"
                     "movl %eax, 12(%esp)\n\t"
                     "popl %ecx\n\t"
                     "popl %edx\n\t"
                     "popl %eax\n\t"
                     "ret\n\t");
}

static int in_shim(u32 a) {
    u8 *base = reinterpret_cast<u8 *>(g_self);
    IMAGE_NT_HEADERS *nt = reinterpret_cast<IMAGE_NT_HEADERS *>((base + (reinterpret_cast<IMAGE_DOS_HEADER *>(base))->e_lfanew));
    return a >= static_cast<u32>(reinterpret_cast<uintptr_t>(base)) && a < static_cast<u32>(reinterpret_cast<uintptr_t>(base)) + nt->OptionalHeader.SizeOfImage;
}

static int starts(const char *s, const char *p) {
    while (*p) if (*s++ != *p++) return 0;
    return 1;
}

static void classify(u32 id, const char *dll, const char *fn) {
    static const struct { const char *name; u8 out; } outs[] = {
        {"glGetIntegerv", 2}, {"glGetFloatv", 2}, {"glGetBooleanv", 2}, {"glGetDoublev", 2},
        {"glGetTexParameteriv", 3}, {"glGetTexParameterfv", 3}, {"glGetTexLevelParameteriv", 4},
        {"glGetTexLevelParameterfv", 4}, {"glGetTexEnviv", 3}, {"glGetTexEnvfv", 3}, {"glGetLightfv", 3},
        {"glGetMaterialfv", 3}, {"glGetPointerv", 2}, {"GetCursorPos", 1}, {"GetClientRect", 2},
        {"GetWindowRect", 2}, {"ScreenToClient", 2}, {"ClientToScreen", 2}, {"QueryPerformanceCounter", 1},
        {"GetSystemInfo", 1}, {"GetVersionExA", 1}, {0, 0}};
    for (u32 i = 0; outs[i].name; i++)
        if (k_streq(fn, outs[i].name)) g_xout[id] = outs[i].out;
    if ((k_strieq(dll, "opengl32.dll") || k_strieq(dll, "glu32.dll")) && !starts(fn, "glGet") && !starts(fn, "glIs") &&
        !starts(fn, "glAre") && !starts(fn, "glRenderMode") && !starts(fn, "glReadPixels") && !starts(fn, "wgl") &&
        !starts(fn, "glu"))
        g_xkind[id] = 1;
    if (k_streq(fn, "SetLastError") || k_streq(fn, "GetLastError")) g_xkind[id] = 2;
}

static char *dup2(const char *a, const char *b) {
    u32 la = static_cast<u32>(strlen(a)), lb = static_cast<u32>(strlen(b));
    char *p = reinterpret_cast<char *>(core_alloc(la + lb + 2));
    if (!p) return 0;
    memcpy(p, a, la);
    p[la] = '!';
    memcpy(p + la + 1, b, lb + 1);
    return p;
}

static void *thunk(u32 id) {
    u8 *t = g_xcode + id * XTHUNK;
    /* cmp byte [g_xon],0 ; jne slow ; jmp [g_xreal+4*id] ; slow: push id ; jmp xcall_slow */
    t[0] = 0x80; t[1] = 0x3D; const u32 on_address = static_cast<u32>(reinterpret_cast<uintptr_t>(&g_xon));
    __builtin_memcpy(t + 2, &on_address, sizeof on_address); t[6] = 0x00;
    t[7] = 0x75; t[8] = 0x06;
    t[9] = 0xFF; t[10] = 0x25; const u32 real_address = static_cast<u32>(reinterpret_cast<uintptr_t>(&g_xreal[id]));
    __builtin_memcpy(t + 11, &real_address, sizeof real_address);
    t[15] = 0x68; __builtin_memcpy(t + 16, &id, sizeof id);
    t[20] = 0xE9; const i32 relative = static_cast<i32>((static_cast<u32>(reinterpret_cast<uintptr_t>(xcall_slow)) - (static_cast<u32>(reinterpret_cast<uintptr_t>(t)) + 25)));
    __builtin_memcpy(t + 21, &relative, sizeof relative);
    FlushInstructionCache(GetCurrentProcess(), t, XTHUNK);
    return t;
}

/* A GetProcAddress-style pointer the exe will store and call: give it a thunk
 * of its own (one per name), so extension calls are traced like imports. */
void *xtrace_wrap(const char *dll, const char *fn, void *real) {
    if (!g_xcode || !real || reinterpret_cast<uintptr_t>(fn) <= 0xffff) return real;
    for (u32 i = 0; i < g_xn; i++)
        if (g_xreal[i] == static_cast<u32>(reinterpret_cast<uintptr_t>(real)) && g_xname[i]) return g_xcode + i * XTHUNK;
    if (g_xn >= XMAX) return real;
    u32 id = g_xn++;
    g_xreal[id] = static_cast<u32>(reinterpret_cast<uintptr_t>(real));
    g_xname[id] = dup2(dll, fn);
    classify(id, dll, fn);
    return thunk(id);
}

/* Wrap every unhooked exe import. Runs last in attach, after every iat_hook. */
void xtrace_install(void) {
    g_xcode = reinterpret_cast<u8 *>(VirtualAlloc(NULL, XMAX * XTHUNK, MEM_RESERVE | MEM_COMMIT | MEM_TOP_DOWN, PAGE_EXECUTE_READWRITE));
    if (!g_xcode) return;
    u8 *base = reinterpret_cast<u8 *>(GetModuleHandleA(NULL));
    IMAGE_NT_HEADERS *nt = reinterpret_cast<IMAGE_NT_HEADERS *>((base + (reinterpret_cast<IMAGE_DOS_HEADER *>(base))->e_lfanew));
    IMAGE_DATA_DIRECTORY idd = nt->OptionalHeader.DataDirectory[IMAGE_DIRECTORY_ENTRY_IMPORT];
    IMAGE_IMPORT_DESCRIPTOR *imp = reinterpret_cast<IMAGE_IMPORT_DESCRIPTOR *>((base + idd.VirtualAddress));
    for (; imp->Name && g_xn < XMAX; imp++) {
        const char *dll = reinterpret_cast<const char *>((base + imp->Name));
        IMAGE_THUNK_DATA *oft = reinterpret_cast<IMAGE_THUNK_DATA *>((base + (imp->OriginalFirstThunk ? imp->OriginalFirstThunk : imp->FirstThunk)));
        IMAGE_THUNK_DATA *ft = reinterpret_cast<IMAGE_THUNK_DATA *>((base + imp->FirstThunk));
        for (; oft->u1.AddressOfData && g_xn < XMAX; oft++, ft++) {
            u32 *slot = reinterpret_cast<u32 *>(&ft->u1.Function);
            if (in_shim(*slot) || (oft->u1.Ordinal & IMAGE_ORDINAL_FLAG32)) continue;
            u32 id = g_xn++;
            g_xreal[id] = *slot;
            const char *fn = reinterpret_cast<const char *>((reinterpret_cast<IMAGE_IMPORT_BY_NAME *>((base + oft->u1.AddressOfData)))->Name);
            g_xname[id] = dup2(dll, fn);
            classify(id, dll, fn);
            u8 *t = reinterpret_cast<u8 *>(thunk(id));
            DWORD old;
            if (VirtualProtect(slot, 4, PAGE_READWRITE, &old)) {
                *slot = static_cast<u32>(reinterpret_cast<uintptr_t>(t));
                VirtualProtect(slot, 4, old, &old);
            }
        }
    }
    slog("xtrace: %u imports wrapped", g_xn);
}

void xtrace_set(int on) {
    if (on && !g_xring) g_xring = reinterpret_cast<xrec_t *>(VirtualAlloc(NULL, XRING * sizeof(xrec_t), MEM_RESERVE | MEM_COMMIT, PAGE_READWRITE));
    if (on && !g_xring) return;
    if (on) { g_xpos = 0; g_xlost = 0; }
    if (on && (g_xmode & 4)) {
        if (!g_xcount) g_xcount = reinterpret_cast<u32 *>(VirtualAlloc(NULL, XCTICKS * XMAX * 4, MEM_RESERVE | MEM_COMMIT, PAGE_READWRITE));
        else memset(g_xcount, 0, XCTICKS * XMAX * 4);
    }
    g_xon = static_cast<u8>((on != 0));
}

/* {names[], records} to path: a text header of names, then the raw records */
void xtrace_dump(const char *path) {
    HANDLE h = CreateFileA(path, GENERIC_WRITE, 0, 0, CREATE_ALWAYS, 0, 0);
    if (h == INVALID_HANDLE_VALUE) { slog("xtrace: cannot write %s", path); return; }
    DWORD w;
    char line[300];
    for (u32 i = 0; i < g_xn; i++) {
        int n = wsprintfA(line, "%u %s %u %u\n", i, g_xname[i] ? g_xname[i] : "?", g_xkind[i], g_xout[i]);
        WriteFile(h, line, static_cast<DWORD>(n), &w, 0);
    }
    int n = wsprintfA(line, "records %u lost %u\n", g_xpos, g_xlost);
    WriteFile(h, line, static_cast<DWORD>(n), &w, 0);
    if (g_xring && g_xpos) WriteFile(h, g_xring, g_xpos * static_cast<u32>(sizeof(xrec_t)), &w, 0);
    if (g_xcount) {
        n = wsprintfA(line, "counts from %u\n", g_trace_from);
        WriteFile(h, line, static_cast<DWORD>(n), &w, 0);
        for (u32 t = 0; t < XCTICKS; t++)
            for (u32 i = 0; i < g_xn; i++)
                if (g_xcount[t * XMAX + i]) {
                    n = wsprintfA(line, "%u %u %u\n", g_trace_from + t, i, g_xcount[t * XMAX + i]);
                    WriteFile(h, line, static_cast<DWORD>(n), &w, 0);
                }
    }
    CloseHandle(h);
    slog("xtrace: %u records (%u lost) to %s", g_xpos, g_xlost, path);
}

} // extern "C"
