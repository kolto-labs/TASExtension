/* KPTASInterface native shim, milestone M1 (exact replay).
 *
 * C++ migration candidate for the freestanding x86-windows-gnu shim. Existing
 * project APIs keep C linkage. Loaded as dinput8.dll by swkotor.exe (K1 GOG
 * 1.03). See docs/design/contract.md, sections 5, 6, 9, 10.
 *
 * Environment read in DllMain:
 *   KPTAS_PORT     loopback TCP port the driver listens on. Unset = passive.
 *   KPTAS_CLOCK    clock origin, ';'-separated key=value pairs:
 *                  mode=virtual|wall  hz=60  qpf_hz=10000000  uptime0_ms=3600000
 *                  initial_ft=<FILETIME, 100 ns since 1601, decimal>
 *   KPTAS_SESSION  Windows path of the session directory (log file). Optional.
 *   KPTAS_WATCHDOG seconds without a gate before the watchdog logs a stall.
 *                  0 or unset = off (the default; diagnostics only).
 *   KPTAS_WATCHDOG_CTX 1 = on a stall, suspend the main thread and log its
 *                  context. This perturbs determinism (see gate.c).
 *   KPTAS_HANG_MS  join hang guard, default 120000.
 *   KPTAS_PRESENT  "skip" (default, [replay] present = "skip"), "draw", or N:
 *                  present only on ticks divisible by N. Output only.
 *   KPTAS_PROFILE  sample the main thread every N ms into the log (diagnostics).
 */
#pragma once
#define WIN32_LEAN_AND_MEAN
#include <winsock2.h>
#include <windows.h>
#include <stdint.h>
#include <stddef.h>
#include <stdarg.h>

using u8 = uint8_t;
using u16 = uint16_t;
using u32 = uint32_t;
using u64 = uint64_t;
using i32 = int32_t;
using i64 = int64_t;

static_assert(sizeof(void *) == sizeof(u32), "the native shim ABI requires 32-bit pointers");

/* Keep the state-engine C++ types outside this header's C-linkage block. */
#include "../../state-engine/kst_int.hpp"

#ifdef __cplusplus
extern "C" {
#endif

#define SHIM_NAME "kptas-shim"
#define SHIM_VERSION "m3.0"
#define PROTO "kptas-wire/1"

/* ---- K1 GOG 1.03 constants (contract section 2, verified by disassembly) ---- */
#define EXE_BASE          0x00400000u
#define EXE_TEXT_VA       0x00401000u
#define EXE_TEXT_SIZE     0x0033c000u
#define EXE_DATA_VA       0x0078d000u
#define EXE_DATA_END      0x00836000u  /* page-aligned end of .data (VirtualSize 0xa8498) */
#define EXE_ENTRY         0x006fb38du
#define GATE_RET          0x00404598u  /* return address of WinMain's loop-head PeekMessageA (0x404592) */
#define ADDR_CLIENT_LOOP  0x00602eb0u
#define ADDR_SERVER_LOOP  0x004babb0u
#define ADDR_UPDATESCREEN 0x00401c10u
#define ADDR_GHRT_INT     0x005ec210u  /* CExoTimersInternal::GetHighResolutionTimer */
#define ADDR_RAND         0x006fc447u
#define ADDR_SRAND        0x006fc43au
#define ADDR_LOAD_FINISH  0x004c5880u
#define ADDR_TLS_INDEX    0x007a29b0u  /* CRT TLS index global -> _tiddata */
#define ADDR_APP_MANAGER  0x007a39fcu
#define ADDR_EXOBASE      0x007a39e0u
#define ADDR_GET_PLAYER_ID 0x004aea40u /* CServerExoApp::GetPlayerCreatureId (thiscall) */
#define ADDR_GET_OBJECT    0x004ae750u /* CServerExoApp::GetGameObject (thiscall, id) */
#define ADDR_GET_MODULE    0x004ae6b0u /* CServerExoApp::GetModule (thiscall) */
#define ADDR_STALL_SAVE    0x004b3110u /* CServerExoAppInternal::StallEventSaveGame (thiscall) */
#define ADDR_RENDER_WINDOW 0x007a39d8u /* renderWindow (HWND) */

/* ---- rt.c ---- */
void *memcpy(void *d, const void *s, size_t n);
void *memset(void *d, int c, size_t n);
void *memmove(void *d, const void *s, size_t n);
int memcmp(const void *a, const void *b, size_t n);
size_t strlen(const char *s);
int k_streq(const char *a, const char *b);
int k_strieq(const char *a, const char *b);
int k_strneq(const char *a, const char *b, size_t n);
const char *k_strstr(const char *h, const char *n);
u64 k_parse_u64(const char *s, const char **end);

struct buf_t {
    char *p;
    u32 n, cap;
    int overflow;
};
void b_init(buf_t *b, char *mem, u32 cap);
void b_raw(buf_t *b, const char *s, u32 n);
void b_str(buf_t *b, const char *s);
void b_ch(buf_t *b, char c);
void b_u64(buf_t *b, u64 v);
void b_i64(buf_t *b, i64 v);
void b_hex(buf_t *b, const u8 *p, u32 n);   /* lowercase hex, no prefix */
void b_hexu32(buf_t *b, u32 v);             /* "0x" + 8 hex digits */
void b_jstr(buf_t *b, const char *s);       /* JSON string with quotes */
void b_jstrn(buf_t *b, const char *s, u32 n);
void b_b64(buf_t *b, const u8 *p, u32 n);
void b_term(buf_t *b);

/* ---- log ---- */
void slog_init(void);
void slog(const char *fmt, ...);

/* ---- alloc.c ---- */
int core_init(u32 bytes);
void *core_alloc(u32 n);
void *core_exec_alloc(u32 n);
extern u8 *g_core_base;
extern u32 g_core_size;

/* ---- blake2s.c / sha256.c ---- */
struct blake2s_t {
    u32 h[8], t[2], f[2];
    u8 buf[64];
    u32 buflen, outlen;
};
void blake2s_init(blake2s_t *s, u32 outlen);
void blake2s_update(blake2s_t *s, const void *in, u32 n);
void blake2s_final(blake2s_t *s, u8 *out);
void blake2s128(const void *in, u32 n, u8 out[16]);
int blake2s_selftest(void);

struct sha256_t {
    u32 h[8];
    u64 len;
    u8 buf[64];
    u32 buflen;
};
void sha256_init(sha256_t *s);
void sha256_update(sha256_t *s, const void *in, u32 n);
void sha256_final(sha256_t *s, u8 out[32]);
void sha256(const void *in, u32 n, u8 out[32]);

/* ---- json.c ---- */
enum { J_NULL, J_BOOL, J_NUM, J_STR, J_ARR, J_OBJ };
struct jnode {
    u8 type;
    i64 num;
    const char *str; /* J_STR value, NUL-terminated */
    const char *key; /* member name when inside an object */
    struct jnode *child, *next;
};
jnode *json_parse(const char *text, u32 len);   /* uses a static pool; valid until next call */
jnode *jget(jnode *obj, const char *key);
i64 jint(jnode *n, i64 def);
const char *jstr(jnode *n, const char *def);

/* ---- iat.c / detour.c ---- */
void *iat_hook(HMODULE mod, const char *dll, const char *fn, void *hook);
int detour(u32 addr, const u8 *expect, u32 len, const u8 *insn_lens, void *hook, void **tramp);
int patch_jmp(u32 addr, void *target);
extern char g_hooks_list[4096];
extern u32 g_hooks_n;
void hooks_note(const char *name);

/* ---- clock.c ---- */
struct vclock_t {
    int mode;           /* 0 virtual, 1 wall */
    u32 hz;
    u64 qpf;
    u64 uptime0_ms;
    u64 initial_ft;
    u64 seg_tick;       /* tick at which the current hz segment started */
    u64 seg_v;          /* V at seg_tick */
    u64 v;              /* frozen V for the current tick (us) */
    u64 extra;          /* budget-guard micro-advance within the tick */
    u64 last_u;
};
void clock_defaults(void);
int clock_parse_env(const char *s);
void clock_set_tick(u64 tick);
u64 clock_u(void);
void clock_install_exe(void);
void clock_install_module(HMODULE m, const char *label);
u64 __attribute__((thiscall)) hk_ghrt(void *self);
void clock_tick_reset(void);
void save_install(void);
#define SAVE_MS_DEFAULT 900u   /* same documented estimate as the Python sim */
extern u32 g_guard_trips, g_guard_site_reads, g_guard_step_us;
extern u32 g_guard_first_site;

/* ---- threads.c (slots, serial scheduler, virtual TLS and sync) ---- */
void threads_install(void);
void threads_join_pending(void);        /* at the gate (harness context) */
u32 vtid_current(void);
void threads_harness_wait_main(void);   /* harness loop: wait for the baton to come back */
u32 threads_tls_main(u32 idx);          /* slot 0's virtual TLS value (journal) */
extern u32 g_main_rtid;
extern u32 g_hang_ms;
extern u32 g_exe_stack_reserve;
static inline u32 cur_rtid(void) {
    u32 v;
    __asm__ volatile("movl %%fs:0x24,%0" : "=r"(v));
    return v;
}

/* ---- dinput.c ---- */
HRESULT di_create(void **out);
struct input_t {
    u8 keys[32];      /* bitmap of held DIK codes */
    u8 buttons;       /* bit0 lmb, bit1 rmb, bit2 mmb */
    int has_abs, abs_x, abs_y;
    int rel_x, rel_y; /* per-tick delta */
    int wheel;        /* one-shot, first tick of the note */
};
void di_stage(const input_t *in, int first_tick, u32 time_ms, u8 hash_out[16], u8 sha256_out[32]);
void di_set_cursor(int x, int y);
void di_init_objects(void);
extern u32 g_di_reads_kbd, g_di_reads_mouse;
void di_observation_json(buf_t *b, u64 tick);
void di_observation_reset(void);

/* ---- msg.c ---- */
void msg_install(void);
void msg_gate_pump(void);
extern int g_in_gate_pump;
extern u32 g_msg_int_n, g_msg_ext_n;
extern u8 g_msg_digest_state[128];
void msg_tick_reset(void);
void msg_tick_digest(u8 out[16]);
extern volatile int g_quit_seen;
extern WNDPROC g_wndproc_orig[4];
void msg_synth_push(UINT m, WPARAM w, LPARAM l);
int msg_synth_peek(MSG *out);

/* ---- gate.c ---- */
struct gate_state_t {
    int started;
    u64 tick;
    u32 us_depth;
    u32 client_passes, server_passes, subticks, us_max_depth;
    u32 load_done;
};
extern volatile u32 g_client_count, g_server_count, g_us_count, g_us_depth, g_load_done_count;
void gate_install(void);
void gate_enter_from_game(void);
void entry_work(void);
extern volatile LONG g_last_gate_ms;

/* ---- journal.c ---- */
void journal_tick(buf_t *out, u8 digest_out[16]);
void journal_data_pages(u8 *out_digests, u32 *npages);
void rand_count(u32 site);
void srand_note(u32 site, u32 seed);
void journal_tick_reset(void);
extern char g_note[256];
extern u8 g_input_hash[16];
extern u8 g_input_sha256[32];

/* ---- wire.c ---- */
int wire_connect(void);
int wire_service(int phase);   /* runs requests until a release; returns 1 on release */
void wire_send_line(buf_t *b);
void wire_push_tick(const char *entry, u32 n);
void wire_gate_hook(void);
void wire_tick_begin(void);    /* after the per-tick resets: events staged for this tick */
void wire_exit(u32 code);      /* the game is exiting: report it before the process ends */
extern int g_wire_ok;

/* engine and harness events: step replies, wait, and (journalled kinds) the journal */
enum { EV_LOAD_DONE = 0, EV_SAVE_BEGIN, EV_SAVE_DONE, EV_CLOSE, EV_PROC_DOWN, EV_SAVE_MS, EV_SAVE_MS_NEXT, EV_KINDS };
void ev_add(u32 kind, u32 val);
void journal_ev_add(u32 kind, u32 val);
extern u32 g_ev_count[EV_KINDS];

/* ---- geo.c: virtual display, window and cursor geometry ---- */
void geo_install(void);
FARPROC geo_proc(const char *name);   /* GetProcAddress-resolved geometry, or 0 */
void geo_set_log(int on);

/* ---- main.c ---- */
extern int g_active;
extern u8 g_text_sha[32];
extern char g_session_dir[260];
extern char g_modlist[2048];
extern HMODULE g_self;
extern u32 g_watchdog_s, g_watchdog_ctx;
extern u32 g_profile_ms;
extern u32 g_present_every, g_presents, g_presents_skipped;

/* ---- vmem.c: heap, VirtualAlloc and critical sections into the arena ---- */
void vmem_install(void);
extern u32 g_va_calls, g_va_reserved, g_heap_creates;

/* ---- vhandle.c: virtual handles (files, finds, events, mutexes, threads) ---- */
void vhandle_install(void);
u32 vh_alloc(u8 type);
void vh_free(u32 h);
void vhandle_reconcile(void);          /* after a restore: close real handles T does not own */
BOOL vhandle_close(HANDLE h, int *handled);

/* ---- gl.c: GL virtual names ---- */
void gl_install(void);
void gl_log_resolved(void);
extern u32 g_gl_retired;

/* ---- state.c: kst glue (capture, scrub, restore) ---- */
int state_init_early(void);
void state_capture(u64 tick, const char *journal, u32 jlen);
int state_restore(u64 tick, const u8 root[32], const char *winpath, buf_t *out);
void state_stats_json(buf_t *b);
void state_audit_json(buf_t *b);
extern int g_capture_on;
extern u8 g_root[32];
extern int g_root_valid;

/* ================================================================
 * harness_game state of the shim (sim class 3): every harness value that is
 * game-visible or journaled lives here, so a restore rewinds it with memory.
 * It sits in the engine's harness_game region at a fixed address.
 * ================================================================ */
#define SHG_OFF 0x10000u
#define SHG_MAGIC 0x4D485348u

struct didod_t { u32 ofs, data, ts, seq, app; };
#define QCAP 1024
struct didev_t {
    void **vtbl;
    LONG ref;
    int kind;       /* 1 keyboard, 2 mouse */
    u32 fmt_size;
    didod_t q[QCAP];
    u32 qh, qn;
};
struct di_t { void **vtbl; LONG ref; };
struct posted_t { HWND h; UINT m; WPARAM w; LPARAM l; };

enum { W_RUNNING = 0, W_PARK_SUSPEND = 1, W_PARK_WAIT = 2, W_EXITED = 3 };
struct vslot_t {
    u32 used, vtid, vh, start, arg, suspend, state, wait_vh, exit_code, pending, cs_depth;
    u32 stack, stack_size;
    u32 tls[64];
};

enum { VH_FREE = 0, VH_FILE = 1, VH_FIND = 2, VH_EVENT = 3, VH_MUTEX = 4, VH_THREAD = 5 };
struct vh_t {
    u8 type, manual, signaled, written;
    u32 serial, access, share, flags, slot, find_n, exit_code;
    u64 pos;
    char path[264];
};
#define NVH 512
#define VH_BASE 0x00F00000u

struct vrange_t { u32 vbase, n, rbase; };
#define GL_TEXMAX 65536u
#define GL_PROGMAX 4096u
#define GL_BUFMAX 4096u

struct shim_hg {
    u32 magic, version;
    gate_state_t gate;
    vclock_t clk;
    u32 joins, hang_trips;
    /* DirectInput objects and state */
    di_t di;
    didev_t kbd, mouse;
    u8 keys[256];
    u8 btn[4];
    i32 acc_x, acc_y, acc_z;
    u32 seq;
    int vcur_x, vcur_y;
    /* messages */
    MSG synth[64];
    u32 synth_h, synth_n;
    posted_t posted[64];
    u32 posted_n;
    /* threads */
    vslot_t slot[KST_MAX_SLOTS];
    u32 nthreads, pending_any, main_cs_depth;
    /* handles */
    vh_t vh[NVH];
    u32 vh_serial;
    /* GL virtual names */
    u32 gl_tex_next, gl_list_next, gl_prog_next, gl_ati_next, gl_buf_next;
    u32 gl_listbase, gl_attr_sp;
    u32 gl_attr_stack[32][2];
    u32 nlist, nati;
    vrange_t list[1024];
    vrange_t ati[256];
    u32 prog[GL_PROGMAX];
    u32 buf[GL_BUFMAX];
    u32 tex[GL_TEXMAX];
    /* appended fields: the offsets above stay fixed */
    u32 save_ms;          /* virtual length of every in-game save */
    u32 save_next_ms;     /* one-shot for the next save only */
    u32 save_next_set;
    u32 close_next;       /* WM_CLOSE staged for the next tick */
};

extern shim_hg *SH;
#define g_gate (SH->gate)
#define g_clk (SH->clk)

#ifdef __cplusplus
} /* extern "C" */

namespace shim_abi {
/* The C hook patchers erase Win32 function pointers at their C ABI boundary. */
template <typename T>
static inline u32 address32(T *pointer) {
    static_assert(sizeof(pointer) == sizeof(u32), "shim addresses are 32-bit");
    return static_cast<u32>(reinterpret_cast<uintptr_t>(pointer));
}

template <typename Fn>
static inline void *erase_function(Fn fn) {
    return reinterpret_cast<void *>(fn);
}

template <typename Fn>
static inline Fn restore_function(void *address) {
    return reinterpret_cast<Fn>(address);
}

template <typename Fn>
static inline void save_function(Fn *slot, void *address) {
    *slot = restore_function<Fn>(address);
}

static inline void save_function(int, void *) {}

template <typename HookFn, typename TrampolineFn>
static inline int install_detour(u32 address, const u8 *expected, u32 length, const u8 *instruction_lengths,
                                 HookFn hook, TrampolineFn *trampoline) {
    void *raw_trampoline = nullptr;
    int installed = ::detour(address, expected, length, instruction_lengths, erase_function(hook),
                              &raw_trampoline);
    if (installed || raw_trampoline) *trampoline = restore_function<TrampolineFn>(raw_trampoline);
    return installed;
}
} /* namespace shim_abi */
#endif
