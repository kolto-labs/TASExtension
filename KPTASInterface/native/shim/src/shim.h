/* KPTASInterface native shim, milestone M1 (exact replay).
 *
 * Freestanding C for x86-windows-gnu, no CRT. Loaded as dinput8.dll by
 * swkotor.exe (K1 GOG 1.03). See docs/design/contract.md, sections 5, 6, 9, 10.
 *
 * Environment read in DllMain:
 *   KPTAS_PORT     loopback TCP port the driver listens on. Unset = passive.
 *   KPTAS_CLOCK    clock origin, ';'-separated key=value pairs:
 *                  mode=virtual|wall  hz=60  qpf_hz=10000000  uptime0_ms=3600000
 *                  initial_ft=<FILETIME, 100 ns since 1601, decimal>
 *   KPTAS_SESSION  Windows path of the session directory (log file). Optional.
 *   KPTAS_CONTEXT  "fiber" (default) or "inline".
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

typedef uint8_t u8;
typedef uint16_t u16;
typedef uint32_t u32;
typedef uint64_t u64;
typedef int32_t i32;
typedef int64_t i64;

#define SHIM_NAME "kptas-shim"
#define SHIM_VERSION "m1.0"
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
    const char *str; /* J_STR value, NUL-terminated */
    const char *key; /* member name when inside an object */
    struct jnode *child, *next;
} jnode;
jnode *json_parse(const char *text, u32 len);   /* uses a static pool; valid until next call */
jnode *jget(jnode *obj, const char *key);
i64 jint(jnode *n, i64 def);
const char *jstr(jnode *n, const char *def);

/* ---- iat.c / detour.c ---- */
void *iat_hook(HMODULE mod, const char *dll, const char *fn, void *hook);
int detour(u32 addr, const u8 *expect, u32 len, const u8 *insn_lens, void *hook, void **tramp);
int patch_jmp(u32 addr, void *target);
extern char g_hooks_list[2048];
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
extern vclock_t g_clk;
int clock_parse_env(const char *s);
void clock_set_tick(u64 tick);
u64 clock_u(void);
void clock_install_exe(void);
void clock_install_module(HMODULE m, const char *label);
u64 __attribute__((thiscall)) hk_ghrt(void *self);
void clock_tick_reset(void);
extern u32 g_guard_trips, g_guard_site_reads, g_guard_step_us;
extern u32 g_guard_first_site;

/* ---- threads.c ---- */
void threads_install(void);
void threads_join_pending(void);
u32 vtid_current(void);
extern u32 g_joins, g_hang_trips, g_workers_n;
extern u32 g_main_rtid;
extern u32 g_hang_ms;
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
void di_stage(const input_t *in, int first_tick, u32 time_ms, u8 hash_out[16]);
void di_set_cursor(int x, int y);
extern int g_vcur_x, g_vcur_y;
extern u32 g_di_reads_kbd, g_di_reads_mouse;

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
typedef struct {
    int started;
    u64 tick;
    u32 us_depth;
    u32 client_passes, server_passes, subticks, us_max_depth;
    u32 load_done;
} gate_state_t;
extern gate_state_t g_gate;
extern volatile u32 g_client_count, g_server_count, g_us_count, g_us_depth, g_load_done_count;
void gate_install(void);
void gate_enter_from_game(void);
void fiber_init(void);
void entry_work(void);
extern int g_use_fiber;
extern volatile LONG g_last_gate_ms;

/* ---- journal.c ---- */
void journal_tick(buf_t *out, u8 digest_out[16]);
void journal_data_pages(u8 *out_digests, u32 *npages);
void rand_count(u32 site);
void srand_note(u32 site, u32 seed);
void journal_tick_reset(void);
extern char g_note[256];
extern u8 g_input_hash[16];

/* ---- wire.c ---- */
int wire_connect(void);
int wire_service(int phase);   /* runs requests until a release; returns 1 on release */
void wire_send_line(buf_t *b);
void wire_push_tick(const char *entry, u32 n);
void wire_gate_hook(void);
extern int g_wire_ok;

/* ---- main.c ---- */
extern int g_active;
extern u8 g_text_sha[32];
extern char g_session_dir[260];
extern char g_modlist[2048];
extern HMODULE g_self;
extern u32 g_watchdog_s, g_watchdog_ctx;
extern u32 g_profile_ms;
extern u32 g_present_every, g_presents, g_presents_skipped;
