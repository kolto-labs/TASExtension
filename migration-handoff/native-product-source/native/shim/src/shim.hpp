/* KPTASInterface native shim, milestone M1 (exact replay).
 *
 * Freestanding mixed C/C++17 for x86-windows-gnu, no CRT. Loaded as dinput8.dll by
 * swkotor.exe (K1 GOG 1.03). See docs/design/contract.md, sections 5, 6, 9, 10.
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
 *   KPTAS_AUDIO    the Miles device, ';'-separated key=value pairs (audio.c):
 *                  device=virtual|real|none  provider=software|hardware|eax  shadow=0|1
 *                  Unset = none: no Miles hooks, the sound-off key.
 *   KPTAS_PRESENT  "skip" (default, [replay] present = "skip"), "draw", or N:
 *                  present only on ticks divisible by N. Output only.
 * Frame capture has no variable: the environment block reaches game memory,
 * so the frames verb turns it on.
 *   KPTAS_PROFILE  sample the main thread every N ms into the log (diagnostics).
 *   KPTAS_PORTABLE portable.math and the control words, ';'-separated:
 *                  math=ref|nat  x87_cw=0x027F  mxcsr=0x1F80. Removed from the
 *                  environment once read; its length must not depend on the
 *                  mode, since Wine sized its heap allocations before that.
 *   KPTAS_BOUNDARY the virtual machine the game sees, ';'-separated key=value
 *                  pairs from the portable profile (bound.c); "native" passes
 *                  the real values through. Unset = the built-in baseline-1.
 *
 * The shim loads only as the game's dinput8.dll. KPM is not supported: with
 * KPM's loader in the process an active shim exits with KPTAS_EXIT_KPM.
 */
#pragma once
#define WIN32_LEAN_AND_MEAN
#include <winsock2.h>
#include <windows.h>
#include <stdint.h>
#include <stddef.h>
#include <stdarg.h>

typedef uint8_t u8;
typedef uint16_t u16;
typedef uint32_t u32;
typedef uint64_t u64;
typedef int32_t i32;
typedef int64_t i64;
#include "winhandles.hpp"
#include "glbuffers.hpp"
#include "gldraw.hpp"
#include "glenv.hpp"
#include "glati.hpp"
#include "gllists.hpp"

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
/* The original bytes at EXE_ENTRY (push 60h; push 75A040h): the entry detour's
 * expected bytes (gate.cpp) and the foreign-patch check (main.cpp). */
#define EXE_ENTRY_LEN     7u
static const u8 EXE_ENTRY_BYTES[EXE_ENTRY_LEN] = {0x6a, 0x60, 0x68, 0x40, 0xa0, 0x75, 0x00};
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
#define ADDR_SAVE_GAME     0x004b58a0u /* CServerExoAppInternal::SaveGame (thiscall, 4 args) */
#define RET_AUTOSAVE       0x004bae51u /* return address of the server MainLoop's script-autosave call */
#define ADDR_PC_AUTOSAVE   0x004b8300u /* CServerExoAppInternal::DoPCAutosave (thiscall, 2 args) */
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

typedef struct {
    char *p;
    u32 n, cap;
    int overflow;
} buf_t;
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
typedef struct {
    u32 h[8], t[2], f[2];
    u8 buf[64];
    u32 buflen, outlen;
} blake2s_t;
void blake2s_init(blake2s_t *s, u32 outlen);
void blake2s_update(blake2s_t *s, const void *in, u32 n);
void blake2s_final(blake2s_t *s, u8 *out);
void blake2s128(const void *in, u32 n, u8 out[16]);
int blake2s_selftest(void);

typedef struct {
    u32 h[8];
    u64 len;
    u8 buf[64];
    u32 buflen;
} sha256_t;
void sha256_init(sha256_t *s);
void sha256_update(sha256_t *s, const void *in, u32 n);
void sha256_final(sha256_t *s, u8 out[32]);
void sha256(const void *in, u32 n, u8 out[32]);

/* ---- json.c ---- */
enum { J_NULL, J_BOOL, J_NUM, J_STR, J_ARR, J_OBJ };
typedef struct jnode {
    u8 type;
    i64 num;
    const char *str; /* J_STR value, NUL-terminated; J_NUM: non-null if it had a fraction or exponent */
    const char *key; /* member name when inside an object */
    struct jnode *child, *next;
} jnode;
jnode *json_parse(const char *text, u32 len);   /* uses a static pool; valid until next call */
jnode *jget(jnode *obj, const char *key);
i64 jint(jnode *n, i64 def);
const char *jstr(jnode *n, const char *def);

/* ---- iat.c / detour.c ---- */
void *iat_hook(HMODULE mod, const char *dll, const char *fn, void *hook);
u32 *iat_slot(HMODULE mod, const char *dll, const char *fn);
int iat_set(u32 *slot, void *hook);
int detour(u32 addr, const u8 *expect, u32 len, const u8 *insn_lens, void *hook, void **tramp);
int patch_jmp(u32 addr, void *target);
int patch_free(u32 addr, u32 n);              /* no earlier patch owns any byte of [addr, addr+n) */
int patch_claim(u32 addr, u32 n);             /* take ownership; 0 if a byte is taken */
int patch_bytes(u32 addr, const void *src, u32 n);
/* Hook ledger (contract section 6 integrity): every IAT slot and code byte the
 * shim rewrote, with the value it left. Checked at every gate. */
void ledger_note(u32 addr, u32 n);            /* record [addr, addr+n); snapshots now once sealed */
void ledger_seal(void);                       /* after the last install: expected = memory now */
int ledger_check(u32 *addr, u8 exp[16], u8 obs[16], u32 *n);   /* 0 = intact, else first mismatch */
u32 ledger_entries(void);
extern char g_hooks_list[4096];
extern u32 g_hooks_n;
void hooks_note(const char *name);

/* ---- clock.c ---- */
typedef struct {
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
} vclock_t;
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
extern u32 g_guard_site_reads, g_guard_step_us;

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
typedef struct {
    u8 keys[32];      /* bitmap of held DIK codes */
    u8 buttons;       /* bit0 lmb, bit1 rmb, bit2 mmb */
    int has_abs, abs_x, abs_y;
    int rel_x, rel_y; /* per-tick delta */
    int wheel;        /* one-shot, first tick of the note */
} input_t;
int di_can_stage(const input_t *in, int first_tick, u32 extra);
int di_stage(const input_t *in, int first_tick, u32 time_ms, u8 hash_out[16]);
void di_set_cursor(int x, int y);
void di_init_objects(void);
extern u32 g_di_reads_kbd, g_di_reads_mouse;

/* ---- msg.c ---- */
void msg_install(void);
void msg_game_enter(UINT message);     /* the game sends or dispatches this message */
void msg_game_leave(void);
void msg_restore_prepare(void);
int msg_restore_finish(void);
void msg_gate_pump(void);
u32 msg_host_events(void);              /* stopped gate: let Wine read X, drop host input */
extern u32 g_msg_host_dropped;
extern int g_in_gate_pump;
extern u32 g_msg_int_n, g_msg_ext_n;
extern u8 g_msg_digest_state[128];
void msg_tick_reset(void);
void msg_tick_digest(u8 out[16]);
#define g_quit_seen (SH->quit_seen)
int msg_synth_available(u32 count);
void msg_synth_push(UINT m, WPARAM w, LPARAM l);
void msg_synth_push_front(HWND h, UINT m, WPARAM w, LPARAM l);
int msg_synth_peek(MSG *out);
int msg_translate_synth(const MSG *message, BOOL *result);
void msg_altf4_keys(void);
int msg_save_pump(MSG *out, u32 ret);
void msg_save_end(void);
extern u32 g_save_depth;

/* ---- gate.c ---- */
typedef struct {
    int started;
    u64 tick;
    u32 us_depth;
    u32 client_passes, server_passes, subticks, us_max_depth;
    u32 load_done;
} gate_state_t;
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
u32 journal_event_dropped(void);
void journal_event_drop_note(u32 n);
void journal_restore_note(u64 tick, const u8 root[32], const char *set);   /* the EV_RESTORE entry */
extern char g_note[256];
extern u8 g_input_hash[16];

/* ---- wire.c ---- */
int wire_connect(void);
int wire_service(int phase);   /* runs requests until a release; returns 1 on release */
void wire_send_line(buf_t *b);
void wire_push_tick(const char *entry, u32 n);
void wire_gate_hook(void);
void wire_tick_begin(void);    /* after the per-tick resets: events staged for this tick */
void wire_exit(u32 code);      /* the game is exiting: report it before the process ends */
void wire_integrity_halt(u32 addr, const u8 *exp, const u8 *obs, u32 n);   /* never returns */
extern int g_wire_ok;
/* File-mutating calls of the game (vhandle.cpp, bound.cpp). Shim DLL data, outside the
 * sim set: a streamed step (hello caps.step_stream) ends at a gate whose tick moved it. */
extern u32 g_file_touch;

/* engine and harness events: step replies, wait, and (journalled kinds) the journal */
enum { EV_LOAD_DONE = 0, EV_SAVE_BEGIN, EV_SAVE_DONE, EV_CLOSE, EV_PROC_DOWN, EV_SAVE_MS, EV_SAVE_MS_NEXT,
       EV_ALTF4, EV_RESTORE, EV_KINDS };
/* save.begin and save.done carry the save's kind and slot in aux */
enum { SAVE_KIND_NONE = 0, SAVE_KIND_AUTO, SAVE_KIND_MANUAL };
#define SAVE_AUX(kind, slot) ((kind) | ((slot) << 8))
void ev_add(u32 kind, u32 val);
void ev_add_aux(u32 kind, u32 val, u32 aux);
void journal_ev_add(u32 kind, u32 val, u32 aux);
extern u32 g_ev_count[EV_KINDS];

/* ---- geo.c: virtual display, window and cursor geometry ---- */
void geo_install(void);
FARPROC geo_proc(const char *name);   /* GetProcAddress-resolved geometry, or 0 */
void geo_set_log(int on);

/* ---- audio.c: the virtual Miles device ---- */
enum { AUD_NONE = 0, AUD_VIRTUAL = 1, AUD_REAL = 2 };
void audio_parse_env(const char *s);
void audio_install(void);
void aud_tick_reset(void);
void aud_journal(buf_t *b);           /* ",\"aud\":{...}" when the virtual device is on, else nothing */
void aud_caps(buf_t *b);              /* ",\"audio\":{...}" for hello caps */
extern int g_aud_mode;

/* ---- math.c: portable.math and the pinned control words ---- */
void math_parse_env(const char *s);   /* KPTAS_PORTABLE */
void math_install(void);              /* last code patch in DllMain */
void math_gate_check(void);
void math_host_probe(const char *where,u32 before);
u32 math_host_begin(void);
void math_host_end(const char *where,u32 before);
enum { MATH_HOST_FP=1, MATH_HOST_ID=2 };
typedef struct math_host_scope {
    u8 fp[512] __attribute__((aligned(16)));
    struct math_host_scope *previous;
    u32 mask, id, thread;
} math_host_scope_t;
typedef struct {
    u8 fp[512] __attribute__((aligned(16)));
    math_host_scope_t *scope;
    u32 id;
} math_callback_scope_t;
void math_scope_begin(math_host_scope_t *scope,u32 mask);
void math_scope_end(math_host_scope_t *scope);
void math_callback_begin(math_callback_scope_t *scope);
void math_callback_end(math_callback_scope_t *scope);
void glsync_real_context_reset(void);
void glsync_buffers_restored(void);
void math_hello_json(buf_t *b);       /* caps members, each with a leading comma */
extern int g_math_ref;
extern u32 g_x87_cw, g_mxcsr, g_math_sites, g_fpu_drift;
extern const char *g_math_state;      /* "native", "reference" or "failed" */
extern char g_math_err[160];

/* ---- bound.c: the virtual machine boundary (KPTAS_BOUNDARY) ---- */
typedef struct { u32 w, h, hz; } bound_mode;
extern bound_mode g_bnd_modes[16];
extern u32 g_bnd_nmodes, g_bnd_window_w, g_bnd_window_h, g_bnd_refresh;
extern u32 g_bnd_total_mb, g_bnd_avail_mb, g_bnd_disk_mb;
int bound_init_early(void);
void bound_install(void);
BOOL WINAPI threads_init_cs_spin(LPCRITICAL_SECTION cs, DWORD spin);
FARPROC bound_proc(const char *name);                    /* GetProcAddress-resolved names, or 0 */
void *bound_gl_proc(const char *name, void *real);       /* wglGetProcAddress-resolved names, or 0 */
const char *bound_v2r(const char *p, char *out, u32 cap); /* a virtual install path as the real one */
void bound_log_genseed(void);
void bound_hello_json(buf_t *b);
extern int g_bnd_native;
extern u32 g_bnd_gl_missing;

/* ---- main.c ---- */
extern int g_active;
extern u8 g_text_sha[32];
extern char g_session_dir[260];
extern char g_modlist[2048];
extern HMODULE g_self;
/* KPM is not supported (main.cpp). Refusal logs "refuse: kpm present: ..." and
 * exits with KPTAS_EXIT_KPM; the host maps either to its KPM message. */
#define KPTAS_EXIT_KPM 75u
void kpm_guard_entry(void);                      /* entry_work: mark the entry reached, check again */
void kpm_require_entry(const char *who);         /* active: refuse if the entry detour never ran */
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

/* ---- frames.c: per-tick frame capture (output only) ---- */
void frames_swap(HDC dc);              /* SwapBuffers hook, before the present */
void frames_gate(u64 tick);            /* gate n: tick n's frame into the ring */
void frames_restored(void);            /* after a restore: the back buffer is from another timeline */
int frames_attach(const char *path, u32 slots, u32 slot_bytes, const char **why);
void frames_off(void);
void frames_stats_json(buf_t *b);
const char *frames_error(void);       /* sticky until attach; wire faults remain latched */

/* ---- render.c: render skipping on unviewed, uncaptured frames (host state only) ---- */
enum { RS_OFF = 0, RS_PRESENT = 1, RS_DRAW = 2 };
extern u32 g_rs_mode, g_rs_preview;
int wire_render_due(u64 tick);        /* wire.c: the render-due rule (defined there) */
int wire_frame_held(void);            /* wire.c: frame n is the one a held gate shows */
int render_present_due(void);
int render_preview_due(u64 tick);
void render_note_present(int real, int by_render);
void render_swap_target(void *target);
int render_skip_elements(u32 mode, i32 count, u32 type);
int render_skip_arrays(u32 mode, i32 first, i32 count);
int render_skip_clear(u32 mask);
int render_refuse_read(void);         /* 1: refuse the driver call (fault latched) */
void render_check_draw(void);
void render_note_copy(u32 target, i32 level, i32 xo, i32 yo, i32 w, i32 h, int sub);
void render_texture_deleted(u32 real);
void render_gate(int captured);
void render_restored(void);
void render_latch(const char *why);
const char *render_fault(void);       /* "" or the latched fault; the gate refuses on it */
void render_fault_message(buf_t *b);  /* the one wording of a fault, for refusals */
void render_stats_json(buf_t *b);

/* ---- diag.c: call-site traces (diagnostics, set by the log verb) ---- */
enum { TR_ALLOC = 1, TR_RAND = 2, TR_HEAP = 4, TR_XCALL = 8 };
extern u32 g_trace, g_trace_site, g_trace_from, g_trace_to;
int trace_on(u32 bit);
void diag_bt(const char *tag, u32 a, u32 b);
extern u32 g_xmode;
void xtrace_install(void);
void *xtrace_wrap(const char *dll, const char *fn, void *real);
void xtrace_set(int on);
void xtrace_dump(const char *path);

/* ---- gl.c: GL virtual names ---- */
void gl_install(void);
void gl_log_resolved(void);
int gl_restore_procs(void);
void *gl_export_proc(const char *name,void *real);

/* ---- glsync.c: GL objects and context state in the tick ---- */
/* Opt-in wall-time diagnostics, outside captured SH. Integer/QPC helpers
 * preserve LastError; no sampler thread or game callback is involved. */
typedef struct {u64 calls,total_us,max_us;} capture_profile_t;
int glsync_profile_enabled(void);
u64 glsync_profile_now(void);
void glsync_profile_record(capture_profile_t *phase,u64 start,u64 end);
void wh_capture_profile_json(buf_t *b);
void glsync_capture(void);
/* An uncaptured gate (capture cadence): only the per-gate GL error transfer. */
void glsync_gate_skip(void);
void glsync_spool_configure(int enabled);
void glsync_tex_dirty(unsigned target, int pixels);
void glsync_tex_validate(unsigned target,int pixels);
void glsync_tex_param_validate(unsigned target,unsigned pname);
void glsync_program_prepare(unsigned target);
int glsync_tex_param_same(unsigned target, unsigned pname, int value);
void glsync_tex_param_set(unsigned target, unsigned pname, int value);
void glsync_tex_shadow_bind(unsigned target, unsigned v);
void glsync_tex_shadow_active(unsigned unit);
void glsync_tex_shadow_invalidate(void);
extern int g_gl_shadow_verify;
void glsync_tex_target(unsigned v, unsigned target);
void glsync_tex_deleted(unsigned v);
void glsync_tex_real_gone(unsigned v);
u32 gl_tex_real_name(u32 logical);
u32 gl_tex_logical_name(u32 real);
u32 gl_tex_saved_binding(u32 real);
void gl_tex_discard_real(u32 logical);
void gl_tex_real_context_reset(void);
int gl_tex_reconcile(void);
int gl_tex_binding_enum(u32 pname);
u32 glbuffers_real_name(u32 logical);
u32 glbuffers_logical_name(u32 real);
int glbuffers_binding_enum(u32 pname);
void glsync_program_bound(u32 v,u32 target);
void glsync_program(unsigned target, unsigned format, int len, const void *str);
void glsync_program_deleted(unsigned v);
void glsync_prog_real_gone(unsigned v);
void glsync_ati_begin(void);
void glsync_ati_end(void);
void glsync_ati_bind(unsigned v);
void glsync_ati_constant(unsigned dst, const float *v);
unsigned glsync_get_error(unsigned (__attribute__((stdcall)) *real)(void));
void glsync_need(buf_t *out);
/* Returns structured success; JSON object is left open for wire transaction fields. */
int glsync_apply(const char *path, buf_t *out);
void glsync_stats_json(buf_t *b);

/* ---- state.c: kst glue (capture, scrub, restore) ---- */
int state_init_early(void);
void state_capture(u64 tick, const char *journal, u32 jlen);
/* An uncaptured gate (capture cadence): window state, GL error, dead-stack
 * scrub and kst_skip_tick (dirty pages accumulate; JOURNAL + TICK_END only). */
void state_skip(u64 tick, const char *journal, u32 jlen);
/* The live map equals live memory at this gate: a capture or a restore ran here
 * since the last state_skip. Restore and the memory audit need it. */
int state_map_current(void);
void state_skip_stats_json(buf_t *b);
int state_restore(u64 tick, const u8 root[32], const char *winpath, buf_t *out);
int state_restore_complete(u64 tick,const u8 root[32]);
int state_root_allowed(const u8 root[32], const char **by);   /* by: "self" or the admitted set's sha256 */
const char *state_admit(const char *winpath, i64 n_expect, const char *sha_hex, u32 *n_out, u8 sha[32]);
void state_stats_json(buf_t *b);
void state_audit_json(buf_t *b,int external_pending);
extern int g_capture_on;
extern u8 g_root[32];
extern int g_root_valid;

/* ================================================================
 * harness_game state of the shim (sim class 3): every harness value that is
 * game-visible or journaled lives here, so a restore rewinds it with memory.
 * It sits in the engine's harness_game region at a fixed address.
 * ================================================================ */
#include "../../state/kst_int.hpp"

#define SHG_OFF 0x10000u
#define SHG_MAGIC 0x4D485348u

typedef struct { u32 ofs, data, ts, seq, app; } didod_t;
#define QCAP 1024
typedef struct {
    void **vtbl;
    LONG ref;
    int kind;       /* 1 keyboard, 2 mouse */
    u32 fmt_size;
    didod_t q[QCAP];
    u32 qh, qn;
} didev_t;
typedef struct { void **vtbl; LONG ref; } di_t;
typedef struct { HWND h; UINT m; WPARAM w; LPARAM l; } posted_t;

enum { W_RUNNING = 0, W_PARK_SUSPEND = 1, W_PARK_WAIT = 2, W_EXITED = 3 };
typedef struct {
    u32 used, vtid, vh, start, arg, suspend, state, wait_vh, exit_code, pending, cs_depth;
    u32 stack, stack_size;
    u32 tls[64];
} vslot_t;

enum { VH_FREE = 0, VH_FILE = 1, VH_FIND = 2, VH_EVENT = 3, VH_MUTEX = 4, VH_THREAD = 5 };
typedef struct {
    u8 type, manual, signaled, written;
    u32 serial, access, share, flags, slot, find_n, exit_code;
    u64 pos;
    char path[264];
} vh_t;
#define NVH 512
#define VH_BASE 0x00F00000u

typedef struct { u32 vbase, n, rbase; } vrange_t;
#define GL_TEXMAX 65536u
#define GL_PROGMAX 4096u
#define GL_BUFMAX 4096u
#define ATI_MAX 32u

/* virtual Miles device (audio.c). Float arguments are kept as their bit patterns. */
#define AUD_N2D 64u
#define AUD_N3D 64u
#define AUD_NSTREAM 32u
#define AUD_NPROV 7u
typedef struct {
    u8 used, state, wav_ok, fmt;  /* state: Miles SMP_* code */
    u16 channels, bits;
    u32 file_rate, rate, block, data_len, frames, loops;
    u64 pos0, t0;                 /* frames played before t0; virtual us of the last start */
    u32 vol[2], dist[2], fx, obst, pos3[3];
    u32 file;                     /* sim address of the WAV image: logged, never journalled */
} avoice_t;
typedef struct {
    u8 used, state, fmt, pad;
    u32 loops, total_ms, data_off, data_len, vh;
    u64 total_us, pos_us, t0;     /* one pass; played before t0; virtual us of the last start */
    u32 vol[2], volpan[2], reverb[2];
    u8 id[8];                     /* sha256 prefix of the stream's bytes */
} astream_t;
typedef struct {
    u32 started, startup[5], prefs[8];
    u32 prov_open[AUD_NPROV], room[AUD_NPROV], rolloff[AUD_NPROV];
    u32 master_room, listener[9];  /* position, forward, up */
    avoice_t v2[AUD_N2D], v3[AUD_N3D];
    astream_t st[AUD_NSTREAM];
    u32 decodes, decode_bytes, opens, open_fails;
} aud_hg_t;

typedef struct shim_hg {
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
    u32 tex[GL_TEXMAX]; /* captured logical texture owner context alias; no host names */
    /* appended fields: the offsets above stay fixed */
    u32 save_ms;          /* virtual length of every in-game save */
    u32 save_next_ms;     /* one-shot for the next save only */
    u32 save_next_set;
    u32 close_next;       /* WM_CLOSE staged for the next tick */
    u32 save_kind;        /* who called SaveGame for the armed save (SAVE_KIND_*) */
    u32 save_slot;        /* its slot number; 0 is the quicksave */
    u32 altf4_next;       /* Alt+F4 staged for the next tick */
    int synth_pt_x, synth_pt_y;  /* cursor of the last synthesized mouse message */
    aud_hg_t aud;         /* virtual Miles device */
    u8 gl_tex_live[GL_TEXMAX / 8];   /* texture names the game has bound and not deleted: glIsTexture */
    /* GL objects and context state (glsync.c): keys the state root covers */
    u8 gl_tex_target[GL_TEXMAX];     /* 0 none, 1 2D, 2 cube map, 3 1D */
    u8 gl_tex_key[GL_TEXMAX][32];
    u8 gl_prog_live[GL_PROGMAX / 8];
    u8 gl_prog_key[GL_PROGMAX][32];
    u8 gl_ctx_key[32];
    u8 gl_framebuffer_key[32];
    u32 gl_default_owner; /* captured logical context owning mutable default parameters */
    u32 gl_err;                      /* GL error the game has not read yet */
    u32 ati_bound;
    u8 ati_const_set[ATI_MAX];
    float ati_const[ATI_MAX][8][4];
    u32 gl_wrappers_used;
    u32 gl_proc_count;
    char gl_proc_name[512][96];
    wh_state_t wh;
    u32 quit_seen, quit_status;
    u32 tls_used[2];       /* process-wide virtual TLS indices; values live in slot[].tls */
    glb_state_t glb;
    gld_state_t gld;
    glati_state_t glati;
    gll_state_t gll;
    u8 gl_proc_export[512]; /* slot resolver: 0 WGL, 1 opengl32 named export */
    /* Boundary diagnostics are journaled and must rewind with the clock. */
    u32 guard_trips, guard_first_site;
    /* !altf4@save: Alt+F4 armed for the next save's first message pump (msg.c) */
    u32 altf4_save;
    /* Exact GetMessage provenance for deterministic TranslateMessage; appended
     * so established captured offsets remain unchanged. */
    MSG synth_translate;
    u32 synth_translate_pending;
} shim_hg;

extern shim_hg *SH;
#define g_gate (SH->gate)
#define g_clk (SH->clk)
#define g_guard_trips (SH->guard_trips)
#define g_guard_first_site (SH->guard_first_site)

/* Optional, core-only diagnostic boundary tracer. */
void btrace_install(void);
/* Profiling: the unconditional call cost a pushf/pusha/fxsave/fxrstor four
 * times per GL call even with tracing off. Test the flag inline instead.
 * always_inline keeps the call site's own return address as the recorded
 * caller (f[9]) when tracing is on; an out-of-line copy would change it. */
extern int g_bt_enabled;
void btrace_point_asm(const char *name,u32 phase);
static inline __attribute__((always_inline)) void btrace_point(const char *name,u32 phase) {
    if(__builtin_expect(g_bt_enabled,0))btrace_point_asm(name,phase);
}
void *btrace_proc(const char *name,void *target);
u32 btrace_caller(u32 ret);
void btrace_flush(void);

/* HUD output; no gameplay or fidelity certification. */
void hud_lock(int tas);
/* clock.precision from the host's clock verb: wording only (HUD label, refusal text). 0 = frame. */
extern int g_precision_tick;
int hud_config(int on,int watermark,i64 offset,const char *text);
int hud_on(void);
void hud_pixels(u8 *pixels,u32 w,u32 h,u64 tick);
void hud_swap(HDC dc);
struct hud_binding_t { u32 vk, modifiers, action; };
int hud_bindings_configure(const hud_binding_t *bindings,u32 count);
int hud_control_observe(const MSG *message,u8 *suppressed,u32 *count);
const char *hud_control_poll(void);
const char *frames_snapshot(const char *path,u32 *w,u32 *h,u8 digest[32]);
void frames_snapshot_meta(buf_t *b);
void frames_accept_restored(const u8 *rgba,u32 w,u32 h);
void hud_stats_json(buf_t *b);
void hud_refresh(void);
void hud_restored(void);
void hud_verified_frame(void);          /* exact restored presentation completed */

#ifdef __cplusplus
} /* C ABI: mixed native candidate and assembly symbols */
#endif
