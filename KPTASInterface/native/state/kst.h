/* kst: in-process savestate engine for the KPTASInterface native side (M2/M3 core).
 *
 * One state serialization == one tick. The sim set is:
 *   game_arena   (1) a fixed-base MEM_WRITE_WATCH reservation: placement allocator
 *                    (map inside the arena), heaps, game-thread stacks;
 *   image_data   (2) the exe's .data and .bss;
 *   harness_game (3) a fixed-base MEM_WRITE_WATCH region: slot table, context
 *                    records, virtual events, tick counter.
 * Engine bookkeeping (live page map, ring, stats) lives in harness_core: private
 * VirtualAlloc memory plus the ".kstcore" section. It is never captured or restored.
 *
 * Freestanding: no CRT. Needs kernel32 only (ntdll RtlCreateHeap via GetProcAddress). */
#ifndef KST_H
#define KST_H

#include <windows.h>

typedef unsigned char u8;
typedef unsigned short u16;
typedef unsigned int u32;
typedef unsigned long long u64;
typedef signed int i32;
typedef signed long long i64;

/* Globals that must stay out of image_data (harness_core). */
#define KST_CORE __attribute__((section(".kstcore")))

#define KST_PAGE 4096u
#define KST_MAX_SLOTS 8u
#define KST_MAX_EVENTS 64u

enum { KST_GAME_ARENA = 1, KST_IMAGE_DATA = 2, KST_HARNESS_GAME = 3 };
enum { KST_REC_PAGE = 1, KST_REC_TICK_END = 2, KST_REC_WRAP = 3, KST_REC_JOURNAL = 4 };
enum {
    KST_F_DATA = 0x1, KST_F_ZERO = 0x2, KST_F_DECOMMIT = 0x4, KST_F_SWEEP = 0x8,
    KST_F_RESTORE_POINT = 0x100, KST_F_FULL = 0x200,
};

/* ---- runtime (kst_rt.c) ---- */
void *memcpy(void *d, const void *s, unsigned int n);
void *memset(void *d, int c, unsigned int n);
void *memmove(void *d, const void *s, unsigned int n);
int memcmp(const void *a, const void *b, unsigned int n);

typedef struct { char *p; u32 n, cap; } kst_buf;
void kb_init(kst_buf *b, char *mem, u32 cap);
void kb_raw(kst_buf *b, const void *s, u32 n);
void kb_str(kst_buf *b, const char *s);
void kb_ch(kst_buf *b, char c);
void kb_u64(kst_buf *b, u64 v);
void kb_hex(kst_buf *b, const u8 *p, u32 n);
void kb_hex32(kst_buf *b, u32 v);
void kst_out(const char *s, u32 n);          /* stdout */
void kst_outs(const char *s);
void kst_fatal(const char *msg);             /* stderr, then ExitProcess(3) */
u64 kst_qpc(void);
u64 kst_us(u64 qpc_delta);                   /* QPC ticks -> microseconds */
u32 kst_strlen(const char *s);
int kst_wcsicmp_n(const WCHAR *a, const char *b);
u64 kst_wtou64(const WCHAR *s);
void *kst_core_alloc(u32 bytes);             /* harness_core memory (VirtualAlloc anywhere) */
int kst_read_file(const WCHAR *path, u8 **out, u32 *len);   /* harness_core buffer */
int kst_write_file(const WCHAR *path, const void *p, u32 len);

/* ---- hashing (kst_hash.c) ---- */
typedef struct { u32 h[8], t[2]; u8 buf[64]; u32 n, outlen; } kst_b2s;
void kst_b2s_init(kst_b2s *s, u32 outlen);
void kst_b2s_update(kst_b2s *s, const void *in, u32 n);
void kst_b2s_final(kst_b2s *s, u8 *out);
void kst_blake2s128(const void *in, u32 n, u8 out[16]);
void kst_page_key(const void *page, u8 out[16]);   /* BLAKE2s-128 of one 4096-byte page */
int kst_hash_selfcheck(void);                      /* SSE2 compress == scalar reference */
typedef struct { u32 h[8]; u64 len; u8 buf[64]; u32 n; } kst_sha;
void kst_sha_init(kst_sha *s);
void kst_sha_update(kst_sha *s, const void *in, u32 n);
void kst_sha_final(kst_sha *s, u8 out[32]);
extern const u8 KST_ZERO_KEY[16];

/* ---- context records (kst_ctx.c) ---- */
typedef struct kst_ctx {
    u32 esp;          /* points at the saved frame: cw, mxcsr, eflags, edi, esi, ebx, ebp, ret */
    u32 seh;          /* TEB fs:[0x00] ExceptionList */
    u32 stack_base;   /* fs:[0x04] */
    u32 stack_limit;  /* fs:[0x08] */
    u32 dealloc;      /* fs:[0xE0C] DeallocationStack */
    u32 last_error;   /* fs:[0x34] */
} kst_ctx;
void kst_switch(kst_ctx *save, kst_ctx *load);

/* ---- configuration and host callbacks ---- */
typedef struct kst_config {
    u32 arena_base, arena_size;   /* default 0x10000000, 1 GiB */
    u32 hg_base, hg_size;         /* harness_game region; default 0x0E000000, 1 MiB */
    const WCHAR *ring_path;       /* staging ring file; NULL disables the ring */
    u32 ring_mb;                  /* capacity if the engine has to create the file */
    u32 nslots;                   /* game thread slots including main (<= KST_MAX_SLOTS) */
    u32 main_stack;               /* bytes, fully committed in the arena */
} kst_config;

typedef struct kst_tick_stats {
    u64 tick;
    u32 ww_dirty;        /* pages GetWriteWatch reported */
    u32 img_dirty;       /* image_data pages whose bytes changed */
    u32 commit_changed;  /* pages whose commit state or protection changed */
    u32 hashed;          /* pages hashed */
    u32 emitted;         /* PAGE records emitted (real changes) */
    u32 decommits;
    u32 sweep;           /* 1 for a full sweep batch */
    u64 us_total;        /* engine time: excludes host callbacks (us_host) */
    u64 us_ww, us_walk, us_hash, us_root, us_ring, us_host;
    u64 ring_wait_us;
} kst_tick_stats;

typedef struct kst_host {
    void *ud;
    /* Every PAGE record the engine emits (restore = 1 for a restore batch). */
    void (*on_page)(void *ud, u64 tick, u32 vaddr, u8 mclass, u16 prot, u16 form,
                    const u8 key[16], const void *data, int restore);
    /* Semantic entry for the JOURNAL record; returns its length. */
    u32 (*journal)(void *ud, u64 tick, u8 *buf, u32 cap);
    /* After TICK_END. May call kst_restore_file / kst_audit (harness stack, game parked). */
    void (*on_tick)(void *ud, u64 tick, const u8 root[32], const kst_tick_stats *st);
} kst_host;

typedef struct kst_audit_result {
    u32 pages;          /* committed sim pages hashed */
    u32 key_mismatch;   /* live map key differs from memory */
    u32 missing;        /* committed in memory, absent from the map */
    u32 extra;          /* present in the map, not committed */
    u32 prot_mismatch;
    u32 first_bad_vaddr;
    u64 us;
} kst_audit_result;

typedef struct kst_restore_stats {
    u64 target;
    u32 records;        /* records in the set */
    u32 skipped;        /* already equal to the live map (minimality guard) */
    u32 written;        /* DATA/ZERO pages written */
    u32 committed;      /* pages committed before writing */
    u32 decommitted;
    u32 prot_changes;
    u32 verify_bad;     /* re-hash after write != target key */
    u32 root_ok;
    kst_audit_result audit;
    u64 us_apply, us_verify;
} kst_restore_stats;

/* ---- engine (harness side) ---- */
int kst_init(const kst_config *cfg, const kst_host *host);
/* Runs game_main on the main slot (arena stack) until it returns. */
void kst_run(void (*game_main)(void *), void *arg);
void kst_root(u8 out[32]);
int kst_audit(kst_audit_result *r);
/* Apply a record set (section 9 record format, PAGE records only) to reach tick
 * `target`, then verify root == expect_root. Callable only from on_tick. */
int kst_restore_records(const u8 *buf, u32 len, u64 target, const u8 expect_root[32],
                        kst_restore_stats *st);
int kst_restore_file(const WCHAR *path, u64 target, const u8 expect_root[32],
                     kst_restore_stats *st);
/* Live map access for hosts that compute write sets. Returns 1 when present. */
int kst_page_get(u32 vpn, u8 key[16], u16 *prot, u8 *mclass);
void kst_vpn_span(u32 *lo, u32 *hi);   /* [lo, hi) of every sim vpn */
u32 kst_region_class(u32 vpn);         /* 0 when vpn is not in the sim set */
/* Scheduler introspection (harness_game): state of slot k, 0 free. */
u32 kst_slot_state(u32 k);
u64 kst_tick(void);
int kst_ring_attached(void);

/* ---- game side ---- */
void kst_gate(void);                   /* main slot only, top of each loop pass */
void *kst_valloc(u32 size);            /* reserve + commit RW, 64 KiB aligned */
void *kst_vreserve(u32 size);          /* reserve only */
int kst_vcommit(void *p, u32 size);
int kst_vdecommit(void *p, u32 size);
int kst_vfree(void *p);                /* decommit and release the range */
HANDLE kst_heap_create(u32 reserve, u32 commit, u32 flags);   /* RtlCreateHeap in the arena */
int kst_in_arena(const void *p);
/* Run fn(arg) on the harness stack of the current real thread (game side). */
void kst_hcall(void (*fn)(void *), void *arg);
int kst_thread_create(void (*proc)(void *), void *arg, u32 stack_size);   /* vtid, or -1 */
void kst_yield(void);
u32 kst_event_create(int manual);
void kst_event_set(u32 ev);
void kst_event_reset(u32 ev);
void kst_event_wait(u32 ev);

enum { KST_SLOT_FREE = 0, KST_SLOT_RUNNABLE = 1, KST_SLOT_BLOCKED = 2, KST_SLOT_RUNNING = 3 };

#endif
