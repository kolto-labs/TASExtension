/* portable.math (contract section 7, item 3).
 *
 * "reference" (the default) takes over every x87 transcendental site the exe
 * has (native/portable/math_sites.toml) with the refmath routines, which give
 * the same bits on every CPU:
 *   - a game site runs the few whole instructions around its opcode from a
 *     cave, with a call to the refmath x87 stub in place of the opcode;
 *   - _CIacos, _CIasin and _CIpow jump to the refmath _CI stubs at their
 *     entry, which also drops _CIpow's CPUID-chosen SSE2 path;
 *   - the CRT's own x87 sites become ud2: once those entries are hooked,
 *     nothing reaches them.
 * The rows come from native/portable/tools/gen_math_patches.py, which also
 * proves the windows and the CRT coverage from a listing of the exe. Every
 * row's bytes are checked before any is written. One mismatch and nothing is
 * patched: hello reports math "failed" and start is refused.
 * "native" leaves the exe's code alone.
 *
 * In both modes every game thread starts with the x87 control word and MXCSR
 * KPTAS_PORTABLE names (the kst slot frames), and each gate counts the parked
 * frames whose control words have moved off those values. */
#include "shim.hpp"
#include "../../portable/refmath.h"

/* Fixed C hook ABI and assembly-referenced symbol names. */
extern "C" {

enum { MS_FSIN, MS_FCOS, MS_FPTAN, MS_FPATAN, MS_FYL2X_LN, MS_FYL2X_LG, MS_FYL2X_L2, MS_FYL2X,
       MS_CIACOS, MS_CIASIN, MS_CIPOW, MS_TRAP };

typedef struct {
    u32 at;           /* first byte rewritten */
    u8 len;           /* bytes checked (and owned) at at */
    u8 n;             /* opcodes in a window */
    u8 off[3];        /* their offsets from at */
    u8 fn[3];         /* MS_* for each */
    u8 bytes[24];     /* what the exe holds at at */
} math_patch_t;

#include "math_sites.inc"

#define NPATCH (sizeof MATH_PATCHES / sizeof MATH_PATCHES[0])

static void (*const STUB[])(void) = {
    refmath_x87_fsin, refmath_x87_fcos, refmath_x87_fptan, refmath_x87_fpatan,
    refmath_x87_fyl2x_ln, refmath_x87_fyl2x_lg, refmath_x87_fyl2x_l2, refmath_x87_fyl2x,
    refmath_CIacos, refmath_CIasin, refmath_CIpow,
};

int g_math_ref = 1;
u32 g_x87_cw = 0x027F, g_mxcsr = 0x1F80;
const char *g_math_state = "native";
char g_math_err[160];
u32 g_math_sites, g_fpu_drift;

/* Caves live in the shim's own image: no allocation, and they move only with it. */
static u8 g_caves[8192] __attribute__((aligned(16)));

static void math_fail(const char *what, u32 at) {
    buf_t b;
    b_init(&b, g_math_err, sizeof g_math_err);
    b_str(&b, what);
    if (at) { b_str(&b, " at "); b_hexu32(&b, at); }
    b_term(&b);
    g_math_state = "failed";
    slog("math: reference not served: %s", g_math_err);
}

/* KPTAS_PORTABLE: math=ref|nat;x87_cw=0x027F;mxcsr=0x1F80 (one length for both modes) */
void math_parse_env(const char *s) {
    if (!s) return;
    while (*s) {
        const char *k = s;
        while (*s && *s != '=' && *s != ';') s++;
        u32 kl = (u32)(s - k);
        if (*s != '=') { if (*s) s++; continue; }
        const char *v = ++s;
        while (*s && *s != ';') s++;
        u32 vl = (u32)(s - v);
        if (kl == 4 && k_strneq(k, "math", 4)) {
            if (vl == 3 && k_strneq(v, "ref", 3)) g_math_ref = 1;
            else if (vl == 3 && k_strneq(v, "nat", 3)) g_math_ref = 0;
            else math_fail("KPTAS_PORTABLE: math is neither ref nor nat", 0);
        } else if (kl == 6 && k_strneq(k, "x87_cw", 6)) g_x87_cw = (u32)k_parse_u64(v, 0) & 0xFFFFu;
        else if (kl == 5 && k_strneq(k, "mxcsr", 5)) g_mxcsr = (u32)k_parse_u64(v, 0);
        if (*s) s++;
    }
}

/* op rel32 written at p, for an instruction that will run at address at */
static u8 *rel32(u8 *p, u32 at, u8 op, const void *to) {
    p[0] = op;
    const i32 relative = (i32)((u32)(uintptr_t)to - (at + 5));
    __builtin_memcpy(p + 1, &relative, sizeof relative);
    return p + 5;
}
#define HERE(p) ((u32)(uintptr_t)(p))

/* Runs last in DllMain, after every other code patch, so a math row that
 * overlaps one of them refuses instead of breaking it. */
void math_install(void) {
    if (g_math_err[0]) return;
    if (!g_math_ref) { g_math_state = "native"; return; }
    for (u32 i = 0; i < NPATCH; i++) {
        const math_patch_t *p = &MATH_PATCHES[i];
        if (memcmp((const void *)(uintptr_t)p->at, p->bytes, p->len) != 0) { math_fail("exe bytes differ", p->at); return; }
        if (!patch_free(p->at, p->len)) { math_fail("another patch owns", p->at); return; }
    }
    DWORD old;
    if (!VirtualProtect(g_caves, sizeof g_caves, PAGE_EXECUTE_READWRITE, &old)) { math_fail("cave protect", 0); return; }
    u8 *c = g_caves, *end = g_caves + sizeof g_caves;
    for (u32 i = 0; i < NPATCH; i++) {
        const math_patch_t *p = &MATH_PATCHES[i];
        u8 code[24];
        u32 n;
        if (p->fn[0] == MS_TRAP) {
            code[0] = 0x0F; code[1] = 0x0B;                       /* ud2 */
            n = 2;
        } else if (p->fn[0] >= MS_CIACOS) {
            rel32(code, p->at, 0xE9, (const void *)STUB[p->fn[0]]);    /* jmp _CI stub */
            n = 5;
        } else {
            if (c + p->len + 3u * p->n + 5 > end) { math_fail("caves full", p->at); return; }
            u8 *cave = c;
            u32 from = 0;
            for (u32 k = 0; k < p->n; k++) {
                memcpy(c, p->bytes + from, p->off[k] - from);
                c += p->off[k] - from;
                c = rel32(c, HERE(c), 0xE8, (const void *)STUB[p->fn[k]]);  /* call stub, for the 2-byte opcode */
                from = p->off[k] + 2u;
            }
            memcpy(c, p->bytes + from, p->len - from);
            c += p->len - from;
            c = rel32(c, HERE(c), 0xE9, (const void *)(uintptr_t)(p->at + p->len));
            rel32(code, p->at, 0xE9, cave);
            memset(code + 5, 0xCC, p->len - 5u);
            n = p->len;
        }
        if (!patch_claim(p->at, p->len) || !patch_bytes(p->at, code, n)) { math_fail("patch write", p->at); return; }
    }
    FlushInstructionCache(GetCurrentProcess(), g_caves, (SIZE_T)(c - g_caves));
    g_math_state = "reference";
    g_math_sites = MATH_NSITES;
    hooks_note("exe:math(reference)");
    slog("math: reference on %u sites (%u rows, %u cave bytes), x87_cw=%x mxcsr=%x",
         g_math_sites, (u32)NPATCH, (u32)(c - g_caves), g_x87_cw, g_mxcsr);
}

/* At a gate every live slot is parked, its control words saved at ctx.esp
 * (cw, mxcsr: see kst_switch). Count the frames that differ from the pinned
 * values (census probe 6); nothing is changed. MXCSR bits 0-5 are sticky
 * exception flags, not control, so they only get a log line, once. */
void math_gate_check(void) {
    btrace_flush();
    static int flags_seen;
    for (u32 k = 0; k < KST_MAX_SLOTS; k++) {
        if (!SH->slot[k].used || SH->slot[k].state == W_EXITED) continue;
        u32 esp = K->hg->slot[k].ctx.esp;
        if (!esp) continue;
        u16 cw = *(const u16 *)(uintptr_t)esp;
        u32 mx = *(const u32 *)(uintptr_t)(esp + 4);
        if ((mx & 0x3Fu) && !flags_seen) {
            flags_seen = 1;
            slog("math: tick %u slot %u parked with MXCSR exception flags %x", (u32)g_gate.tick, k, mx & 0x3Fu);
        }
        if (cw == (u16)g_x87_cw && (mx & ~0x3Fu) == (g_mxcsr & ~0x3Fu)) continue;
        if (g_fpu_drift++ < 16)
            slog("math: tick %u slot %u parked with cw=%x mxcsr=%x (pinned %x %x)",
                 (u32)g_gate.tick, k, cw, mx, g_x87_cw, g_mxcsr);
    }
}

/* Diagnostic only: observe host/guest sticky flags without changing them.
 * Logging itself calls host APIs, so restore exactly the observed word. */
void math_host_probe(const char *where,u32 before) {
    static u32 count;
    if(count>=32)return;
    u8 fp[512] __attribute__((aligned(16)));
    u32 after,flags;__asm__ volatile("fxsave %0":"=m"(fp)::"memory");
    __asm__ volatile("stmxcsr %0":"=m"(after));
    __asm__ volatile("pushfl; popl %0":"=r"(flags));
    DWORD error=GetLastError();count++;
    slog("math boundary %s tick=%u before=%x after=%x id=%u",where,(u32)g_gate.tick,before,after,(flags>>21)&1);
    SetLastError(error);
    __asm__ volatile("fxrstor %0"::"m"(fp):"memory");
    __asm__ volatile("pushl %0;popfl"::"r"(flags):"cc","memory");
}
/* Diagnostic bit31 carries EFLAGS.ID alongside the real low16 MXCSR bits.
 * It is never loaded into MXCSR or used to rewrite the captured flags. */
u32 math_host_begin(void) {
    u32 v,f;__asm__ volatile("stmxcsr %0":"=m"(v));
    __asm__ volatile("pushfl; popl %0":"=r"(f));return v|((f&EFLAGS_ID)<<10);
}
void math_host_end(const char *where,u32 before) {
    u32 after=math_host_begin();if(after!=before)math_host_probe(where,before);
}

/* Scoped driver state never reaches captured pages. During a game callback,
 * detach the driver scope so nested game calls use their own current state.
 * On return, preserve the callback's deliberate changes for the caller. */
static DWORD scope_tls=TLS_OUT_OF_INDEXES;
static DWORD scope_slot(void) {
    if(scope_tls==TLS_OUT_OF_INDEXES) {
        DWORD slot=TlsAlloc();
        if(slot==TLS_OUT_OF_INDEXES) {
            slog("driver environment TLS allocation failed");
            TerminateProcess(GetCurrentProcess(),6);for(;;){}
        }
        DWORD prior=(DWORD)InterlockedCompareExchange((volatile LONG *)&scope_tls,(LONG)slot,(LONG)TLS_OUT_OF_INDEXES);
        if(prior!=TLS_OUT_OF_INDEXES)TlsFree(slot);
    }
    return scope_tls;
}
/* Profiling: kernel32 TlsGetValue/TlsSetValue plus the error save/restore cost
 * ~3% of a frame. For an index below 64 both are the TEB TlsSlots array at
 * fs:[0xE10] (Wine i386 kernelbase: TlsGetValue zeroes LastErrorValue then
 * reads [teb+idx*4+0xE10]; TlsSetValue stores there and returns TRUE), so
 * read/write it directly; no last error is touched, so none is saved. The
 * shim's own TlsAlloc (real kernel32; only the exe's IAT is virtualized) picks
 * the index. Unallocated (TLS_OUT_OF_INDEXES) or >= 64 keeps the kernel32 path. */
#define TEB_TLS_SLOTS_OFF 0xE10 /* TEB.TlsSlots */
#define TEB_TLS_SLOTS 64        /* inline slots; higher indexes live elsewhere */
static inline math_host_scope_t *tls_raw_get(DWORD slot) {
    math_host_scope_t *s;
    __asm__ volatile("movl %%fs:%c1(,%2,4),%0":"=r"(s):"i"(TEB_TLS_SLOTS_OFF),"r"(slot):"memory");
    return s;
}
static inline void tls_raw_set(DWORD slot,math_host_scope_t *s) {
    __asm__ volatile("movl %1,%%fs:%c2(,%0,4)"::"r"(slot),"r"(s),"i"(TEB_TLS_SLOTS_OFF):"memory");
}
static math_host_scope_t *scope_get(void) {
    DWORD slot=scope_tls;
    if(__builtin_expect(slot<TEB_TLS_SLOTS,1))return tls_raw_get(slot);
    DWORD error=GetLastError();math_host_scope_t *s=static_cast<math_host_scope_t *>(TlsGetValue(scope_slot()));SetLastError(error);return s;
}
static void scope_set(math_host_scope_t *s) {
    DWORD slot=scope_tls;
    if(__builtin_expect(slot<TEB_TLS_SLOTS,1)){tls_raw_set(slot,s);return;}
    DWORD error=GetLastError();
    if(!TlsSetValue(scope_slot(),s)) {
        slog("driver environment TLS update failed");
        TerminateProcess(GetCurrentProcess(),6);for(;;){}
    }
    SetLastError(error);
}
/* GL boundary fast path (glenv.cpp). The TEB index scope_get/scope_set use,
 * or MATH_NO_FAST_SLOT when they would call kernel32 (index unallocated or
 * >= 64) or when tracing is on (btrace_point would record). Then the boundary runs its
 * canonical math_scope_begin/math_scope_end body instead. scope_tls changes
 * at most once (unallocated -> index), so a returned index stays valid. */
u32 math_scope_fast_slot(void) {
    DWORD slot=scope_tls;
    return slot<TEB_TLS_SLOTS && !g_bt_enabled?slot:MATH_NO_FAST_SLOT;
}
/* The field writes of math_scope_begin(s,MATH_HOST_ID) followed by the
 * boundary's own s->mask=mask and s->id=id, minus their dead work: read_id
 * (its value is overwritten by id) and btrace_point (off, see above). The
 * TLS accesses are scope_get/scope_set's TEB branch. Integer only; the
 * caller fills s->fp. Keep in step with math_scope_begin: a field it sets
 * must be set here too. */
void math_scope_link(math_host_scope_t *s,u32 slot,u32 mask,u32 id) {
    s->thread=cur_rtid();s->mask=mask;
    s->previous=tls_raw_get(slot);s->id=id;
    tls_raw_set(slot,s);
}
/* math_scope_end's scope_set(s->previous) alone: its fxrstor and put_id are
 * dead on the GL boundary, whose return thunk loads the final FP state and
 * EFLAGS itself (see glenv_return_asm). */
void math_scope_unlink(const math_host_scope_t *s,u32 slot) {
    tls_raw_set(slot,s->previous);
}
static u32 read_id(void) {
    u32 f;__asm__ volatile("pushfl;popl %0":"=r"(f));return f&EFLAGS_ID;
}
static void put_id(u32 id) {
    u32 f;__asm__ volatile("pushfl;popl %0":"=r"(f));
    f=(f&~EFLAGS_ID)|id;
    __asm__ volatile("pushl %0;popfl"::"r"(f):"cc","memory");
}
/* Keep in step with math_scope_link (GL boundary fast path), which must set
 * the same fields; and math_scope_end with math_scope_unlink. */
void math_scope_begin(math_host_scope_t *s,u32 mask) {
    btrace_point("scope.begin",10);
    s->thread=cur_rtid(); /* same value as kernel32 GetCurrentThreadId (teb+0x24), no call */
    s->mask=mask;s->previous=scope_get();s->id=read_id();
    if(mask&MATH_HOST_FP)__asm__ volatile("fxsave %0":"=m"(s->fp)::"memory");
    scope_set(s);
    btrace_point("scope.saved",11);
}
void math_scope_end(math_host_scope_t *s) {
    btrace_point("scope.end.before",12);
    scope_set(s->previous);
    if(s->mask&MATH_HOST_FP)__asm__ volatile("fxrstor %0"::"m"(s->fp):"memory");
    if(s->mask&MATH_HOST_ID)put_id(s->id);
    btrace_point("scope.end.restored",13);
}
void math_callback_begin(math_callback_scope_t *c) {
    btrace_point("callback.host",14);
    c->scope=scope_get();
    if(!c->scope || c->scope->thread!=cur_rtid()){c->scope=NULL;return;}
    math_host_scope_t *s=c->scope;scope_set(NULL);
    c->id=read_id();
    if(s->mask&MATH_HOST_FP) {
        __asm__ volatile("fxsave %0":"=m"(c->fp)::"memory");
        __asm__ volatile("fxrstor %0"::"m"(s->fp):"memory");
    }
    if(s->mask&MATH_HOST_ID)put_id(s->id);
    btrace_point("callback.game.entry",15);
}
void math_callback_end(math_callback_scope_t *c) {
    btrace_point("callback.game.return",16);
    math_host_scope_t *s=c->scope;if(!s)return;
    if(s->mask&MATH_HOST_FP) {
        __asm__ volatile("fxsave %0":"=m"(s->fp)::"memory");
        __asm__ volatile("fxrstor %0"::"m"(c->fp):"memory");
    }
    if(s->mask&MATH_HOST_ID){s->id=read_id();put_id(c->id);}
    scope_set(s);
    btrace_point("callback.host.resume",17);
}

void math_hello_json(buf_t *b) {
    b_str(b, ",\"math\":");
    b_jstr(b, g_math_state);
    if (g_math_err[0]) { b_str(b, ",\"math_error\":"); b_jstr(b, g_math_err); }
    b_str(b, ",\"math_sites\":");
    b_u64(b, g_math_sites);
    b_str(b, ",\"mxcsr\":");
    b_u64(b, g_mxcsr);
    b_str(b, ",\"portable\":[");
    if (k_streq(g_math_state, "reference")) b_str(b, "\"math\"");
    b_str(b, "],\"x87_cw\":");
    b_u64(b, g_x87_cw);
}

} /* extern "C" */
