/* kst internals shared by the engine translation units. */
#ifndef KST_INT_H
#define KST_INT_H

#include <stddef.h>
#include "kst.hpp"

extern "C" {

#define KST_GRAN 65536u
#define KST_HG_MAGIC 0x4748534Bu      /* "KSHG" */
#define KST_ARENA_MAGIC 0x4152534Bu   /* "KSRA" */
#define KST_NONE 0xFFFFFFFFu
#define KST_CONT 0xFFFFFFFFu

enum { R_NONE = 0, R_GATE = 1, R_HANDOFF = 2, R_EXIT = 3 };

/* ---- harness_game (class 3): restored with memory ---- */
struct hg_slot {
    u32 state, wait_ev, vtid, entry, arg, stack_lo, stack_hi, pad;
    kst_ctx ctx;
};

struct hg_ev { u32 used, manual, signaled; };

struct kst_hg {
    u32 magic, version;
    u64 tick_cur, tick_next;   /* written at gate entry, before the capture */
    u32 nslots, next_vtid, nev, pad;
    hg_slot slot[KST_MAX_SLOTS];
    hg_ev ev[KST_MAX_EVENTS];
};

/* ---- placement allocator map, at the arena base (class 1) ---- */
struct arena_hdr {
    u32 magic, ngran, hw_gran, nalloc;
    u32 map[1];   /* ngran entries: 0 free, n = first granule of an n-granule range, KST_CONT */
};

/* ---- harness_core: never captured ---- */
struct pent { u8 key[16]; u16 prot; u8 present; u8 mclass; };

struct region {
    u32 base, size, mclass, ww;   /* ww: tracked by GetWriteWatch, else by shadow compare */
    u8 *shadow;
};

/* Shared on-disk PAGE header. Keep its packed 40-byte layout stable. */
struct __attribute__((packed)) kst_record_header {
    char magic[2];
    u8 kind, mclass;
    u16 flags, prot;
    u32 vaddr, dlen;
    u64 tick;
    u8 hash[16];
};
static_assert(sizeof(kst_record_header) == 40, "KST record header wire size changed");
static_assert(offsetof(kst_record_header, flags) == 4, "KST record flags offset changed");
static_assert(offsetof(kst_record_header, vaddr) == 8, "KST record address offset changed");
static_assert(offsetof(kst_record_header, tick) == 16, "KST record tick offset changed");
static_assert(offsetof(kst_record_header, hash) == 24, "KST record hash offset changed");

struct kst_core {
    kst_config cfg;
    kst_host host;
    kst_hg *hg;
    arena_hdr *ah;
    region reg[8];
    u32 nreg;
    u32 vpn_lo, vpn_hi;
    pent *map;          /* live map L, by vpn - vpn_lo */
    u16 *prot_now;      /* walk scratch: protection, 0 = not committed */
    u32 *mark;          /* changed-page bitmap */
    u32 mark_lo, mark_hi;
    u32 leaf_lo, nleaf;
    u8 (*leaf_hash)[32];
    u8 *leaf_dirty;
    u16 *leaf_count;
    u8 root[32];
    void **wwbuf;
    u32 wwcap;
    u32 arena_span;     /* bytes from the arena base ever scanned (never shrinks) */
    /* staging ring */
    u8 *ring_base;      /* mapping: 4096-byte header, then data */
    u32 ring_cap, ring_off;
    u64 ring_wpos;
    kst_b2s batch;
    u8 *jbuf;
    u32 jcap;
    /* threads */
    kst_ctx hctx[KST_MAX_SLOTS];
    HANDLE real_ev[KST_MAX_SLOTS];
    HANDLE real_thr[KST_MAX_SLOTS];
    volatile u32 cur, next, shutdown;
    volatile u32 reason[KST_MAX_SLOTS];
    int in_gate;
    int captured_once;
    kst_tick_stats st;
    PVOID (WINAPI *RtlCreateHeap)(ULONG, PVOID, SIZE_T, SIZE_T, PVOID, PVOID);
    u32 heap_cs_off, heap_list_off;   /* arena heap lock and list entry offsets (diagnostics) */
    /* kst_hcall records, one per slot: the harness stack pointer never touches a game stack */
    struct { u32 hsp; void (*fn)(void *); void *arg; u32 pad; } hc[KST_MAX_SLOTS];
};

extern kst_core *K;

/* kst_capture.c */
void kst__gate(void);
void kst__emit_page(u64 tick, u32 vaddr, u8 mclass, u16 prot, u16 flags, const u8 key[16],
                    const void *data, int restore);
void kst__emit_close(u64 tick, u16 tflags);
void kst__leaf_touch(u32 vpn);
void kst__compute_root(void);
void kst__ww_reset_all(void);
int kst__readable(u16 prot);
void kst__ring_open(void);
/* kst_ctx.c */
void kst__threads_init(void);
/* kst_arena.c */
void kst__arena_init(void);

static inline pent *kst__pent(u32 vpn) { return &K->map[vpn - K->vpn_lo]; }

}
static_assert(sizeof(hg_ev) == 12, "KST virtual-event layout changed");
static_assert(sizeof(hg_slot) == 56, "KST scheduler-slot layout changed");
static_assert(offsetof(kst_hg, slot) == 40, "KST scheduler-slot offset changed");
static_assert(offsetof(kst_hg, ev) == 488, "KST event-table offset changed");
static_assert(sizeof(kst_hg) == 1256, "KST harness-game state layout changed");

#endif
