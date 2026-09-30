/* SimGame: a tiny deterministic Win32 console "game" driven by the kst engine.
 *
 * Game (sim) side, all state in the sim set:
 *   - a heap-allocated world in an arena heap (RtlCreateHeap in the arena);
 *   - a CRT-style LCG (holdrand in .data, class image_data);
 *   - entity updates plus HeapFree/HeapAlloc churn every tick;
 *   - a worker thread (arena stack, serial scheduler) that "loads resources":
 *     kst_valloc a buffer, ReadFile it slice by slice at explicit offsets, and
 *     yield after every slice, so it is often parked mid-job at a gate;
 *   - main loop: kst_gate at the top of every pass.
 * Harness side (harness_core only): roots and stats logs, a reference page
 * store, a restore plan and a polled control file for restore-to-tick. */
#include "../state/kst.h"
#include "hist.h"

/* ================================================================ game (sim) */

#define NENT 1024
#define RES_SIZE (8u << 20)
#define CHUNK (16u * 1024)

typedef struct ent { u32 id, size, hp, x, y, vx, vy, gen; u8 payload[4]; } ent;
typedef struct res { u8 *buf; u32 size, id, sum; } res;
typedef struct job { u32 state, res_id, size, off, done_bytes, sum; u8 *buf; } job;   /* 0 idle 1 posted 2 running 3 done */
typedef struct world {
    ent *e[NENT];
    res r[4];
    u32 nres, next_res, next_id;
    job job;
    u32 ev_job, checksum, tick, loads_done, joins;
} world;

static u32 g_holdrand = 1;     /* .data: the CRT's _holdrand stand-in */
static world *g_world;         /* .bss */
static HANDLE g_heap;          /* .bss */

static u32 crt_rand(void) {
    g_holdrand = g_holdrand * 214013u + 2531011u;
    return (g_holdrand >> 16) & 0x7FFF;
}
static u32 rnd(u32 n) { return ((crt_rand() << 15) | crt_rand()) % n; }
static u32 rotl(u32 x, int n) { return (x << n) | (x >> (32 - n)); }

/* harness services the game calls (they touch only harness_core state) */
static void sim_res_read(u32 off, void *buf, u32 n);
static int sim_quit(void);

static ent *ent_new(u32 id) {
    u32 size = 32 + rnd(3000);
    ent *e = (ent *)HeapAlloc(g_heap, 0, sizeof(ent) + size);
    if (!e || !kst_in_arena(e)) kst_fatal("HeapAlloc outside the arena");
    e->id = id;
    e->size = size;
    e->hp = 100 + rnd(900);
    e->x = rnd(4096);
    e->y = rnd(4096);
    e->vx = rnd(7);
    e->vy = rnd(7);
    e->gen = g_holdrand;
    for (u32 i = 0; i < size; i += 64) e->payload[i] = (u8)crt_rand();
    return e;
}

static void worker(void *arg) {
    world *w = (world *)arg;
    u32 wr = 0x1234567u;   /* worker LCG: lives on the worker's arena stack */
    for (;;) {
        kst_event_wait(w->ev_job);
        job *j = &w->job;
        j->state = 2;
        j->buf = (u8 *)kst_valloc(j->size);
        u32 sum = 0;
        for (u32 off = 0; off < j->size; off += CHUNK) {
            u32 n = j->size - off < CHUNK ? j->size - off : CHUNK;
            sim_res_read(j->off + off, j->buf + off, n);   /* kernel-side writes into watched pages */
            for (u32 i = 0; i < n; i += 4) {
                wr = wr * 1103515245u + 12345u;
                u32 *p = (u32 *)(j->buf + off + i);
                *p ^= wr;
                sum = rotl(sum, 5) ^ *p;
            }
            j->done_bytes = off + n;
            kst_yield();   /* one slice per tick: parked mid-job at the gate */
        }
        j->sum = sum;
        j->state = 3;
    }
}

static void tick_body(world *w) {
    w->tick++;
    for (u32 k = 0; k < 48; k++) {
        ent *e = w->e[rnd(NENT)];
        e->x += e->vx;
        e->y += e->vy;
        e->hp = (e->hp * 7 + crt_rand()) & 0xFFFF;
        e->payload[rnd(e->size)] ^= (u8)crt_rand();
        w->checksum = rotl(w->checksum, 3) ^ e->x ^ e->hp;
    }
    for (u32 k = 0; k < 8; k++) {
        u32 i = rnd(NENT);
        HeapFree(g_heap, 0, w->e[i]);
        w->e[i] = ent_new(w->next_id++);
    }
    job *j = &w->job;
    if (j->state == 0 && w->tick % 4 == 0) {
        j->res_id = w->next_res++;
        j->size = (1 + rnd(7)) * 65536;
        j->off = rnd(RES_SIZE - j->size) & ~4095u;
        j->done_bytes = 0;
        j->state = 1;
        kst_event_set(w->ev_job);
    }
    kst_yield();   /* the worker gets one slice */
    if (w->tick % 200 == 150 && (j->state == 1 || j->state == 2)) {
        w->joins++;   /* synchronous load: spin on yield until the worker finishes */
        while (j->state != 3) kst_yield();
    }
    if (j->state == 3) {
        if (w->nres == 4) {
            kst_vfree(w->r[0].buf);
            for (u32 i = 1; i < 4; i++) w->r[i - 1] = w->r[i];
            w->nres = 3;
        }
        res *r = &w->r[w->nres++];
        r->buf = j->buf;
        r->size = j->size;
        r->id = j->res_id;
        r->sum = j->sum;
        w->checksum ^= j->sum;
        w->loads_done++;
        j->state = 0;
    }
}

static void game_main(void *arg) {
    g_holdrand = (u32)arg;
    g_heap = kst_heap_create(32u << 20, 64u << 10, 0);
    world *w = (world *)HeapAlloc(g_heap, HEAP_ZERO_MEMORY, sizeof(world));
    if (!w || !kst_in_arena(w)) kst_fatal("world allocation outside the arena");
    for (u32 i = 0; i < NENT; i++) w->e[i] = ent_new(w->next_id++);
    w->ev_job = kst_event_create(0);
    if (kst_thread_create(worker, w, 256 * 1024) < 0) kst_fatal("no free slot for the worker");
    g_world = w;
    for (;;) {
        kst_gate();
        if (sim_quit()) break;
        tick_body(w);
    }
}

/* ================================================================ harness */

typedef struct plan_ent { u64 at, target; } plan_ent;

typedef struct host {
    WCHAR dir[260], ring[300], ctl[300], tag[32];
    u32 seed, audit_every, have_ring, have_ctl;
    u64 ticks;
    plan_ent plan[16];
    u32 nplan, plan_i;
    HANDLE res, roots, stats, events;
    u8 (*first_root)[32];
    u8 *have_root;
    u32 maxticks;
    u64 gates;
    int quit;
    /* continuation check */
    u64 cont_checked, cont_bad, cont_first_bad;
    /* per-gate samples (steady state only) */
    u32 *cap_us, *emitted, *wwd, *hashed;
    u32 nsamp;
    u64 sweep_us, sweep_pages;
    u32 audits, audits_bad;
    u32 restores, restores_bad;
    u64 restore_us_sum;
    int store;   /* reference page store on (needed for restores) */
    u64 dump_at;  /* write every sim page at this gate tick (diagnostics), ~0 off */
} host;

KST_CORE static host *H;

/* The worker's file read, as a shim-hooked file API would do it: on the harness
 * stack (kst_hcall), explicit offset, kernel-side writes into watched arena pages. */
typedef struct rread { u32 off; void *buf; u32 n, ok; } rread;

static void res_read_fn(void *a) {
    rread *r = (rread *)a;
    OVERLAPPED ov;
    memset(&ov, 0, sizeof ov);
    ov.Offset = r->off;
    DWORD got = 0;
    r->ok = ReadFile(H->res, r->buf, r->n, &got, &ov) && got == r->n;
}

static void sim_res_read(u32 off, void *buf, u32 n) {
    rread r = {off, buf, n, 0};
    kst_hcall(res_read_fn, &r);
    if (!r.ok) kst_fatal("resource read failed");
}

static int sim_quit(void) { return H->quit; }

static void path_join(WCHAR *out, const WCHAR *dir, const char *name) {
    u32 i = 0;
    while (dir[i] && i < 250) { out[i] = dir[i]; i++; }
    if (i && out[i - 1] != '\\') out[i++] = '\\';
    for (u32 k = 0; name[k] && i < 298; k++) out[i++] = (WCHAR)(u8)name[k];
    out[i] = 0;
}

static void name_tag(char *out, const char *pre, const char *suf) {
    u32 n = 0;
    for (u32 k = 0; pre[k]; k++) out[n++] = pre[k];
    for (u32 k = 0; H->tag[k] && n < 60; k++) out[n++] = (char)H->tag[k];
    for (u32 k = 0; suf[k]; k++) out[n++] = suf[k];
    out[n] = 0;
}

static HANDLE open_out(const char *pre, const char *suf) {
    char nm[80];
    WCHAR p[300];
    name_tag(nm, pre, suf);
    path_join(p, H->dir, nm);
    HANDLE h = CreateFileW(p, GENERIC_WRITE, FILE_SHARE_READ, 0, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, 0);
    if (h == INVALID_HANDLE_VALUE) kst_fatal("cannot create a log file");
    return h;
}

static void hwrite(HANDLE h, kst_buf *b) {
    DWORD w;
    WriteFile(h, b->p, b->n, &w, 0);
}

static void ensure_resource(void) {
    WCHAR p[300];
    path_join(p, H->dir, "res.bin");
    H->res = CreateFileW(p, GENERIC_READ | GENERIC_WRITE, FILE_SHARE_READ, 0, OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, 0);
    if (H->res == INVALID_HANDLE_VALUE) kst_fatal("cannot open res.bin");
    DWORD hi = 0;
    if (GetFileSize(H->res, &hi) == RES_SIZE) return;
    u32 *blk = (u32 *)kst_core_alloc(RES_SIZE);
    u32 s = 0xC0FFEEu;
    for (u32 i = 0; i < RES_SIZE / 4; i++) { s = s * 1664525u + 1013904223u; blk[i] = s; }
    DWORD w = 0;
    SetFilePointer(H->res, 0, 0, FILE_BEGIN);
    if (!WriteFile(H->res, blk, RES_SIZE, &w, 0) || w != RES_SIZE) kst_fatal("cannot write res.bin");
    SetEndOfFile(H->res);
    VirtualFree(blk, 0, MEM_RELEASE);
}

/* ---------------------------------------------------------------- engine callbacks */

static void cb_page(void *ud, u64 tick, u32 vaddr, u8 mclass, u16 prot, u16 form, const u8 key[16],
                    const void *data, int restore) {
    (void)ud;
    if (!restore && H->store) hist_page(tick, vaddr, mclass, prot, form, key, data);
}

static u32 cb_journal(void *ud, u64 tick, u8 *buf, u32 cap) {
    (void)ud;
    kst_buf b;
    kb_init(&b, (char *)buf, cap);
    kb_str(&b, "{\"holdrand\":");
    kb_u64(&b, g_holdrand);
    kb_str(&b, ",\"job\":");
    kb_u64(&b, g_world ? g_world->job.state : 0);
    kb_str(&b, ",\"tick\":");
    kb_u64(&b, tick);
    kb_ch(&b, '}');
    return b.n;
}

static void log_event(kst_buf *b) {
    kb_ch(b, '\n');
    hwrite(H->events, b);
    kst_out(b->p, b->n);
}

static void do_audit(u64 t) {
    kst_audit_result a;
    int ok = kst_audit(&a);
    H->audits++;
    if (!ok) H->audits_bad++;
    char line[256];
    kst_buf b;
    kb_init(&b, line, sizeof line);
    kb_str(&b, "audit tick ");
    kb_u64(&b, t);
    kb_str(&b, ok ? " ok" : " MISMATCH");
    kb_str(&b, " pages ");
    kb_u64(&b, a.pages);
    kb_str(&b, " key ");
    kb_u64(&b, a.key_mismatch);
    kb_str(&b, " missing ");
    kb_u64(&b, a.missing);
    kb_str(&b, " extra ");
    kb_u64(&b, a.extra);
    kb_str(&b, " prot ");
    kb_u64(&b, a.prot_mismatch);
    kb_str(&b, " first_bad ");
    kb_hex32(&b, a.first_bad_vaddr);
    kb_str(&b, " us ");
    kb_u64(&b, a.us);
    log_event(&b);
}

static const char *slot_name(u32 s) {
    switch (s) {
    case KST_SLOT_FREE: return "free";
    case KST_SLOT_RUNNABLE: return "runnable";
    case KST_SLOT_BLOCKED: return "blocked";
    case KST_SLOT_RUNNING: return "running";
    }
    return "?";
}

static void do_restore(u64 now, u64 target) {
    char line[512];
    kst_buf b;
    kb_init(&b, line, sizeof line);
    if (target >= H->maxticks || !H->have_root[target]) {
        kb_str(&b, "restore refused: tick ");
        kb_u64(&b, target);
        kb_str(&b, " was never recorded");
        log_event(&b);
        return;
    }
    u64 t0 = kst_qpc();
    WCHAR wp[300];
    char nm[80], num[24];
    kst_buf nb;
    kb_init(&nb, num, sizeof num - 1);
    kb_u64(&nb, H->gates);
    num[nb.n] = 0;
    name_tag(nm, "w-", "");
    u32 L = kst_strlen(nm);
    nm[L++] = '-';
    for (u32 k = 0; num[k]; k++) nm[L++] = num[k];
    memcpy(nm + L, ".rec", 5);
    path_join(wp, H->dir, nm);
    u32 ndata = 0, nrec = hist_write_w(target, wp, &ndata);
    u64 t1 = kst_qpc();
    kst_restore_stats rs;
    int ok = kst_restore_file(wp, target, H->first_root[target], &rs);
    u64 t2 = kst_qpc();
    hist_truncate(target);
    H->restores++;
    if (!ok) H->restores_bad++;
    H->restore_us_sum += kst_us(t2 - t0);
    kb_str(&b, "restore at tick ");
    kb_u64(&b, now);
    kb_str(&b, " -> ");
    kb_u64(&b, target);
    kb_str(&b, ok ? " OK" : " FAILED");
    kb_str(&b, " W ");
    kb_u64(&b, nrec);
    kb_str(&b, " (data ");
    kb_u64(&b, ndata);
    kb_str(&b, ") skipped ");
    kb_u64(&b, rs.skipped);
    kb_str(&b, " written ");
    kb_u64(&b, rs.written);
    kb_str(&b, " committed ");
    kb_u64(&b, rs.committed);
    kb_str(&b, " decommitted ");
    kb_u64(&b, rs.decommitted);
    kb_str(&b, " prot ");
    kb_u64(&b, rs.prot_changes);
    kb_str(&b, " verify_bad ");
    kb_u64(&b, rs.verify_bad);
    kb_str(&b, " root ");
    kb_str(&b, rs.root_ok ? "==" : "!=");
    kb_str(&b, " audit ");
    kb_u64(&b, rs.audit.pages);
    kb_str(&b, "p/");
    kb_u64(&b, rs.audit.key_mismatch + rs.audit.missing + rs.audit.extra + rs.audit.prot_mismatch);
    kb_str(&b, "bad us: build_w ");
    kb_u64(&b, kst_us(t1 - t0));
    kb_str(&b, " apply ");
    kb_u64(&b, rs.us_apply);
    kb_str(&b, " verify+audit ");
    kb_u64(&b, rs.us_verify);
    log_event(&b);
    /* what the restored tick looks like: is the worker parked mid-job? */
    kb_init(&b, line, sizeof line);
    world *w = g_world;
    kb_str(&b, "  state at ");
    kb_u64(&b, kst_tick());
    kb_str(&b, ": worker slot ");
    kb_str(&b, slot_name(kst_slot_state(1)));
    kb_str(&b, " job.state ");
    kb_u64(&b, w->job.state);
    kb_str(&b, " done ");
    kb_u64(&b, w->job.done_bytes);
    kb_ch(&b, '/');
    kb_u64(&b, w->job.size);
    kb_str(&b, " holdrand ");
    kb_u64(&b, g_holdrand);
    log_event(&b);
}

static void poll_ctl(u64 t) {
    u8 *buf;
    u32 len;
    if (GetFileAttributesW(H->ctl) == INVALID_FILE_ATTRIBUTES) return;
    if (!kst_read_file(H->ctl, &buf, &len)) return;
    DeleteFileW(H->ctl);
    char cmd[64];
    u32 n = len < 63 ? len : 63;
    memcpy(cmd, buf, n);
    cmd[n] = 0;
    VirtualFree(buf, 0, MEM_RELEASE);
    if (!memcmp(cmd, "restore ", 8)) {
        u64 v = 0;
        for (u32 i = 8; cmd[i] >= '0' && cmd[i] <= '9'; i++) v = v * 10 + (u64)(cmd[i] - '0');
        char line[64];
        kst_buf b;
        kb_init(&b, line, sizeof line);
        kb_str(&b, "ctl: restore ");
        kb_u64(&b, v);
        log_event(&b);
        do_restore(t, v);
    } else if (!memcmp(cmd, "quit", 4)) {
        H->quit = 1;
    }
}

/* Diagnostics: every present sim page at gate t -> dump-<tag>-<t>.bin as
 * (vaddr u32, class u8, pad u8, prot u16, key 16, page 4096) entries. */
static void dump_pages(u64 t) {
    char nm[80], num[24];
    kst_buf nb;
    kb_init(&nb, num, sizeof num - 1);
    kb_u64(&nb, t);
    num[nb.n] = 0;
    name_tag(nm, "dump-", "-");
    u32 L = kst_strlen(nm);
    for (u32 k = 0; num[k]; k++) nm[L++] = num[k];
    memcpy(nm + L, ".bin", 5);
    WCHAR p[300];
    path_join(p, H->dir, nm);
    HANDLE h = CreateFileW(p, GENERIC_WRITE, FILE_SHARE_READ, 0, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, 0);
    if (h == INVALID_HANDLE_VALUE) return;
    u32 lo, hi;
    kst_vpn_span(&lo, &hi);
    for (u32 v = lo; v < hi; v++) {
        u8 e[24], mc;
        u16 prot;
        if (!kst_page_get(v, e + 8, &prot, &mc)) continue;
        u32 va = v << 12;
        memcpy(e, &va, 4);
        e[4] = mc;
        e[5] = 0;
        memcpy(e + 6, &prot, 2);
        DWORD w;
        WriteFile(h, e, 24, &w, 0);
        WriteFile(h, (void *)va, 4096, &w, 0);
    }
    CloseHandle(h);
}

static void cb_tick(void *ud, u64 t, const u8 root[32], const kst_tick_stats *st) {
    (void)ud;
    H->gates++;
    char line[256];
    kst_buf b;
    kb_init(&b, line, sizeof line);
    kb_u64(&b, t);
    kb_ch(&b, ' ');
    kb_hex(&b, root, 32);
    kb_ch(&b, '\n');
    hwrite(H->roots, &b);
    kb_init(&b, line, sizeof line);
    kb_u64(&b, t);
    const u64 f[] = {st->emitted, st->ww_dirty, st->img_dirty, st->commit_changed, st->hashed, st->decommits,
                     st->us_total, st->us_ww, st->us_walk, st->us_hash, st->us_root, st->us_ring, st->ring_wait_us,
                     st->us_host, kst_slot_state(1), g_world ? g_world->job.state : 0,
                     g_world ? g_world->job.done_bytes : 0};
    for (u32 i = 0; i < sizeof f / sizeof f[0]; i++) { kb_ch(&b, ' '); kb_u64(&b, f[i]); }
    kb_ch(&b, '\n');
    hwrite(H->stats, &b);
    if (st->sweep) {
        H->sweep_us = st->us_total;
        H->sweep_pages = st->emitted;
    } else if (H->nsamp < H->maxticks * 2) {
        u32 i = H->nsamp++;
        H->cap_us[i] = (u32)st->us_total;
        H->emitted[i] = st->emitted;
        H->wwd[i] = st->ww_dirty;
        H->hashed[i] = st->hashed;
    }
    if (t >= H->maxticks) kst_fatal("tick beyond the host table");
    if (!H->have_root[t]) {
        memcpy(H->first_root[t], root, 32);
        H->have_root[t] = 1;
    } else {
        H->cont_checked++;
        if (memcmp(H->first_root[t], root, 32)) {
            if (!H->cont_bad) H->cont_first_bad = t;
            H->cont_bad++;
        }
    }
    if (H->store) hist_tick_end(t);
    if (t == H->dump_at) dump_pages(t);
    if (H->audit_every && t % H->audit_every == 0) do_audit(t);
    if (H->plan_i < H->nplan && H->plan[H->plan_i].at == t) {
        u64 target = H->plan[H->plan_i++].target;
        do_restore(t, target);
        return;
    }
    if (H->have_ctl) poll_ctl(t);
    if (t + 1 >= H->ticks && H->plan_i >= H->nplan) H->quit = 1;
}

/* ---------------------------------------------------------------- summary */

static void sort_u32(u32 *a, u32 n) {
    for (u32 gap = n / 2; gap; gap /= 2)
        for (u32 i = gap; i < n; i++) {
            u32 v = a[i], j = i;
            while (j >= gap && a[j - gap] > v) { a[j] = a[j - gap]; j -= gap; }
            a[j] = v;
        }
}

static void dist(kst_buf *b, const char *name, u32 *a, u32 n) {
    u64 sum = 0;
    for (u32 i = 0; i < n; i++) sum += a[i];
    sort_u32(a, n);
    kb_str(b, name);
    if (!n) { kb_str(b, " n=0\n"); return; }
    kb_str(b, " n=");
    kb_u64(b, n);
    kb_str(b, " min=");
    kb_u64(b, a[0]);
    kb_str(b, " p50=");
    kb_u64(b, a[n / 2]);
    kb_str(b, " p90=");
    kb_u64(b, a[(u32)((u64)n * 90 / 100)]);
    kb_str(b, " p99=");
    kb_u64(b, a[(u32)((u64)n * 99 / 100)]);
    kb_str(b, " max=");
    kb_u64(b, a[n - 1]);
    kb_str(b, " mean=");
    kb_u64(b, sum / n);
    kb_ch(b, '\n');
}

static void summary(void) {
    char *mem = (char *)kst_core_alloc(8192);
    kst_buf b;
    kb_init(&b, mem, 8192);
    u8 root[32];
    kst_root(root);
    kb_str(&b, "summary: seed ");
    kb_u64(&b, H->seed);
    kb_str(&b, " gates ");
    kb_u64(&b, H->gates);
    kb_str(&b, " final tick ");
    kb_u64(&b, kst_tick());
    kb_str(&b, " final root ");
    kb_hex(&b, root, 32);
    kb_str(&b, "\nsweep tick 0: pages ");
    kb_u64(&b, H->sweep_pages);
    kb_str(&b, " us ");
    kb_u64(&b, H->sweep_us);
    kb_ch(&b, '\n');
    dist(&b, "capture_us", H->cap_us, H->nsamp);
    dist(&b, "pages_emitted", H->emitted, H->nsamp);
    dist(&b, "ww_dirty", H->wwd, H->nsamp);
    dist(&b, "pages_hashed", H->hashed, H->nsamp);
    kb_str(&b, "continuation ticks checked ");
    kb_u64(&b, H->cont_checked);
    kb_str(&b, " mismatched ");
    kb_u64(&b, H->cont_bad);
    if (H->cont_bad) { kb_str(&b, " first "); kb_u64(&b, H->cont_first_bad); }
    kb_str(&b, "\nrestores ");
    kb_u64(&b, H->restores);
    kb_str(&b, " failed ");
    kb_u64(&b, H->restores_bad);
    kb_str(&b, " audits ");
    kb_u64(&b, H->audits);
    kb_str(&b, " audits_bad ");
    kb_u64(&b, H->audits_bad);
    kb_str(&b, "\nstore pages ");
    kb_u64(&b, H->store ? hist_npages() : 0);
    kb_str(&b, " world loads ");
    kb_u64(&b, g_world ? g_world->loads_done : 0);
    kb_str(&b, " joins ");
    kb_u64(&b, g_world ? g_world->joins : 0);
    kb_ch(&b, '\n');
    kst_out(b.p, b.n);
    hwrite(H->events, &b);
}

/* ---------------------------------------------------------------- selftest */

static void selftest(void) {
    char *mem = (char *)kst_core_alloc(4096);
    u8 *page = (u8 *)kst_core_alloc(4096);
    kst_buf b;
    kb_init(&b, mem, 4096);
    u8 k[32];
    static const char abc[] = "abc";
    kst_blake2s128(abc, 3, k);
    kb_str(&b, "b2s128(abc) ");
    kb_hex(&b, k, 16);
    kst_blake2s128(abc, 0, k);
    kb_str(&b, "\nb2s128() ");
    kb_hex(&b, k, 16);
    for (u32 i = 0; i < 4096; i++) page[i] = (u8)(i * 7 + (i >> 8));
    kst_page_key(page, k);
    kb_str(&b, "\npagekey(i*7+(i>>8)) ");
    kb_hex(&b, k, 16);
    kst_blake2s128(page, 4096, k);
    kb_str(&b, "\nb2s128(same) ");
    kb_hex(&b, k, 16);
    kst_blake2s128(page, 1000, k);
    kb_str(&b, "\nb2s128(first 1000) ");
    kb_hex(&b, k, 16);
    memset(page, 0, 4096);
    kst_page_key(page, k);
    kb_str(&b, "\npagekey(zero) ");
    kb_hex(&b, k, 16);
    kb_str(&b, memcmp(k, KST_ZERO_KEY, 16) ? " != KST_ZERO_KEY" : " == KST_ZERO_KEY");
    kst_sha s;
    kst_sha_init(&s);
    kst_sha_update(&s, abc, 3);
    kst_sha_final(&s, k);
    kb_str(&b, "\nsha256(abc) ");
    kb_hex(&b, k, 32);
    kst_sha_init(&s);
    kst_sha_update(&s, "KPRT", 4);
    kst_sha_final(&s, k);
    kb_str(&b, "\nsha256(KPRT) ");
    kb_hex(&b, k, 32);
    kb_str(&b, "\nsse2 compress == scalar: ");
    kb_str(&b, kst_hash_selfcheck() ? "yes" : "NO");
    /* throughput: 4096 page keys over a 16 MiB buffer, and SHA-256 over 22 KiB */
    u8 *big = (u8 *)kst_core_alloc(16u << 20);
    for (u32 i = 0; i < (16u << 20); i += 4) *(u32 *)(big + i) = i * 2654435761u;
    u64 t0 = kst_qpc();
    for (u32 i = 0; i < 4096; i++) kst_page_key(big + i * 4096, k);
    u64 t1 = kst_qpc();
    for (u32 i = 0; i < 100; i++) { kst_sha_init(&s); kst_sha_update(&s, big, 22528); kst_sha_final(&s, k); }
    u64 t2 = kst_qpc();
    kb_str(&b, "\nbench: page_key ns/page ");
    kb_u64(&b, kst_us((t1 - t0) * 1000) / 4096);
    kb_str(&b, ", sha256 us per 22 KiB leaf ");
    kb_u64(&b, kst_us(t2 - t1) / 100);
    kb_ch(&b, '\n');
    kst_out(b.p, b.n);
}

/* ---------------------------------------------------------------- entry */

static int next_arg(const WCHAR **pp, WCHAR *out, u32 cap) {
    const WCHAR *p = *pp;
    while (*p == ' ' || *p == '\t') p++;
    if (!*p) return 0;
    u32 n = 0;
    int q = 0;
    while (*p && (q || (*p != ' ' && *p != '\t'))) {
        if (*p == '"') { q = !q; p++; continue; }
        if (n + 1 < cap) out[n++] = *p;
        p++;
    }
    out[n] = 0;
    *pp = p;
    return 1;
}

static void wcopy(WCHAR *d, const WCHAR *s, u32 cap) {
    u32 i = 0;
    while (s[i] && i + 1 < cap) { d[i] = s[i]; i++; }
    d[i] = 0;
}

static void parse_plan(const WCHAR *s) {
    while (*s && H->nplan < 16) {
        u64 at = kst_wtou64(s);
        while (*s >= '0' && *s <= '9') s++;
        if (*s != ':') kst_fatal("--plan wants at:target[,at:target...]");
        s++;
        u64 tg = kst_wtou64(s);
        while (*s >= '0' && *s <= '9') s++;
        H->plan[H->nplan].at = at;
        H->plan[H->nplan].target = tg;
        H->nplan++;
        if (*s == ',') s++;
    }
}

void __cdecl simgame_entry(void) {
    H = (host *)kst_core_alloc(sizeof(host));
    H->seed = 1;
    H->ticks = 2000;
    H->dump_at = ~0ull;
    wcopy(H->tag, L"run", 32);
    const WCHAR *cl = GetCommandLineW();
    WCHAR arg[300], val[300];
    next_arg(&cl, arg, 300);   /* program name */
    int want_selftest = 0;
    while (next_arg(&cl, arg, 300)) {
        if (!kst_wcsicmp_n(arg, "--selftest")) { want_selftest = 1; continue; }
        if (!next_arg(&cl, val, 300)) kst_fatal("option without a value");
        if (!kst_wcsicmp_n(arg, "--dir")) wcopy(H->dir, val, 260);
        else if (!kst_wcsicmp_n(arg, "--ring")) { wcopy(H->ring, val, 300); H->have_ring = 1; }
        else if (!kst_wcsicmp_n(arg, "--ctl")) { wcopy(H->ctl, val, 300); H->have_ctl = 1; }
        else if (!kst_wcsicmp_n(arg, "--tag")) wcopy(H->tag, val, 32);
        else if (!kst_wcsicmp_n(arg, "--seed")) H->seed = (u32)kst_wtou64(val);
        else if (!kst_wcsicmp_n(arg, "--ticks")) H->ticks = kst_wtou64(val);
        else if (!kst_wcsicmp_n(arg, "--audit-every")) H->audit_every = (u32)kst_wtou64(val);
        else if (!kst_wcsicmp_n(arg, "--plan")) parse_plan(val);
        else if (!kst_wcsicmp_n(arg, "--dump-at")) H->dump_at = kst_wtou64(val);
        else kst_fatal("unknown option");
    }
    if (want_selftest) { selftest(); ExitProcess(0); }
    if (!H->dir[0]) kst_fatal("--dir is required");
    H->maxticks = (u32)H->ticks + 16;
    H->first_root = (u8 (*)[32])kst_core_alloc(H->maxticks * 32);
    H->have_root = (u8 *)kst_core_alloc(H->maxticks);
    H->cap_us = (u32 *)kst_core_alloc(H->maxticks * 2 * 4);
    H->emitted = (u32 *)kst_core_alloc(H->maxticks * 2 * 4);
    H->wwd = (u32 *)kst_core_alloc(H->maxticks * 2 * 4);
    H->hashed = (u32 *)kst_core_alloc(H->maxticks * 2 * 4);
    ensure_resource();
    H->roots = open_out("roots-", ".txt");
    H->stats = open_out("stats-", ".txt");
    H->events = open_out("events-", ".txt");

    kst_config cfg;
    memset(&cfg, 0, sizeof cfg);
    cfg.arena_base = 0x10000000u;
    cfg.arena_size = 1024u << 20;
    cfg.hg_base = 0x0E000000u;
    cfg.hg_size = 1u << 20;
    cfg.ring_path = H->have_ring ? H->ring : 0;
    cfg.ring_mb = 64;
    cfg.nslots = 4;
    cfg.main_stack = 1u << 20;
    kst_host hs;
    memset(&hs, 0, sizeof hs);
    hs.on_page = cb_page;
    hs.journal = cb_journal;
    hs.on_tick = cb_tick;
    kst_init(&cfg, &hs);
    H->store = H->nplan || H->have_ctl;
    if (H->store) {
        WCHAR pp[300];
        char nm[80];
        name_tag(nm, "pages-", ".bin");
        path_join(pp, H->dir, nm);
        hist_init(pp, H->maxticks);
    }
    kst_run(game_main, (void *)H->seed);
    summary();
    ExitProcess(H->cont_bad || H->restores_bad || H->audits_bad ? 2 : 0);
}
