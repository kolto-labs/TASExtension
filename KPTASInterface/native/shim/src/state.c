/* kst glue for the shim (milestones M2 and M3): the sim set, per-tick capture
 * at the gate, dead-stack scrubbing, restore and audits.
 *
 * Sim set (contract 3.4): game_arena (heap, VirtualAlloc, game-thread stacks),
 * image_data (the exe's .data), harness_game (engine slots plus shim_hg).
 * The engine's own bookkeeping, the shim's DLL data and harness_core stay out.
 *
 * Dead stack. Everything below a parked slot's saved ESP is dead. Wine code the
 * game calls leaves its locals there (process-heap pointers, handles, times),
 * and those differ between a run and its continuation after a restore. At each
 * gate the pages written since the last capture are zeroed below the saved ESP,
 * so a slot's stack pages hold only live frames when they are hashed. */
#include "shim.h"

shim_hg *SH;
int g_capture_on;
u8 g_root[32];
int g_root_valid;
static const char *g_jptr;
static u32 g_jlen;
static int g_restoring;
static u32 g_scrub_pages;
static kst_tick_stats g_last_st;
static u64 g_cap_n, g_cap_us_sum, g_cap_us_max, g_emit_sum, g_ww_sum;
static u32 g_emit_max, g_ww_max;
static u64 g_last_tick_captured = (u64)-1;

#define ARENA_BASE 0x31000000u   /* the 1171 MB free range at DllMain starts at 0x30072000 (vm map in the log) */
#define ARENA_SIZE (1024u << 20)
#define HG_BASE 0x0E000000u
#define HG_SIZE (4u << 20)

static void kout(const char *s, u32 n) {
    char line[600];
    if (n > sizeof line - 1) n = sizeof line - 1;
    memcpy(line, s, n);
    while (n && (line[n - 1] == '\n' || line[n - 1] == '\r')) n--;
    line[n] = 0;
    slog("%s", line);
}

static u32 cb_journal(void *ud, u64 tick, u8 *buf, u32 cap) {
    (void)ud; (void)tick;
    if (g_restoring || !g_jptr) return 0;
    u32 n = g_jlen < cap ? g_jlen : cap;
    memcpy(buf, g_jptr, n);
    return n;
}

/* Address-space map at DllMain: every region of 16 MB or more, for choosing
 * the fixed bases (logged once per boot). */
static void log_free_map(void) {
    u32 a = 0x10000;
    while (a < 0x7fff0000u) {
        MEMORY_BASIC_INFORMATION m;
        if (!VirtualQuery((void *)(uintptr_t)a, &m, sizeof m)) break;
        u32 b = (u32)(uintptr_t)m.BaseAddress, n = (u32)m.RegionSize;
        if (n >= (16u << 20) || m.State != MEM_FREE)
            if (n >= (16u << 20)) slog("vm: %x+%x (%u MB) %s", b, n, n >> 20,
                                       m.State == MEM_FREE ? "free" : m.State == MEM_RESERVE ? "reserved" : "committed");
        if (b + n <= a) break;
        a = b + n;
    }
}

int state_init_early(void) {
    static kst_config cfg;
    static kst_host host;
    cfg.arena_base = ARENA_BASE;
    cfg.arena_size = ARENA_SIZE;
    cfg.hg_base = HG_BASE;
    cfg.hg_size = HG_SIZE;
    cfg.ring_path = 0;
    cfg.ring_mb = 256;
    cfg.nslots = KST_MAX_SLOTS;
    cfg.main_stack = 0;
    host.journal = cb_journal;
    kst_set_out(kout);
    log_free_map();
    if (!kst_hash_selfcheck()) { slog("kst hash self-check failed"); return 0; }
    kst_init_mem(&cfg, &host);
    u32 need = ((u32)sizeof(shim_hg) + 4095u) & ~4095u;
    if (SHG_OFF + need > HG_SIZE) { slog("shim_hg too large (%u)", need); return 0; }
    void *p = VirtualAlloc((void *)(uintptr_t)(HG_BASE + SHG_OFF), need, MEM_COMMIT, PAGE_READWRITE);
    if (p != (void *)(uintptr_t)(HG_BASE + SHG_OFF)) { slog("shim_hg commit failed"); return 0; }
    SH = (shim_hg *)p;
    SH->magic = SHG_MAGIC;
    SH->version = 1;
    SH->vcur_x = 400;
    SH->vcur_y = 300;
    SH->gl_tex_next = 1;
    SH->gl_list_next = 1;
    SH->gl_prog_next = 1;
    SH->gl_ati_next = 1;
    SH->gl_buf_next = 1;
    clock_defaults();
    di_init_objects();
    slog("state: arena %x+%x hg %x+%x shim_hg %x (%u bytes)", ARENA_BASE, ARENA_SIZE, HG_BASE, HG_SIZE,
         (u32)(uintptr_t)SH, (u32)sizeof(shim_hg));
    return 1;
}

/* Zero what is dead below each parked slot's saved ESP on the pages written
 * since the last capture. */
static void scrub_stacks(void) {
    static void *pages[4096];
    for (u32 k = 0; k < KST_MAX_SLOTS; k++) {
        vslot_t *s = &SH->slot[k];
        if (!s->used || s->state == W_EXITED) continue;
        hg_slot *hs = &K->hg->slot[k];
        u32 lo = hs->stack_lo, esp = hs->ctx.esp;
        if (!lo || esp <= lo || esp > hs->stack_hi) continue;
        u32 end = (esp + 4095u) & ~4095u;
        ULONG_PTR n = sizeof pages / sizeof pages[0];
        ULONG gran = 0;
        if (GetWriteWatch(0, (void *)(uintptr_t)lo, end - lo, pages, &n, &gran)) continue;
        for (ULONG_PTR i = 0; i < n; i++) {
            u32 p = (u32)(uintptr_t)pages[i];
            u32 stop = p + 4096u <= esp ? p + 4096u : esp;
            if (stop > p) { memset((void *)(uintptr_t)p, 0, stop - p); g_scrub_pages++; }
        }
    }
}

void state_capture(u64 tick, const char *journal, u32 jlen) {
    if (!g_capture_on) return;
    g_jptr = journal;
    g_jlen = jlen;
    scrub_stacks();
    kst_capture_tick(tick, 0);
    g_jptr = 0;
    kst_root(g_root);
    g_root_valid = 1;
    g_last_st = K->st;
    g_last_tick_captured = tick;
    g_cap_n++;
    g_cap_us_sum += K->st.us_total;
    if (K->st.us_total > g_cap_us_max) g_cap_us_max = K->st.us_total;
    g_emit_sum += K->st.emitted;
    if (K->st.emitted > g_emit_max && tick) g_emit_max = K->st.emitted;
    g_ww_sum += K->st.ww_dirty;
    if (K->st.ww_dirty > g_ww_max && tick) g_ww_max = K->st.ww_dirty;
}

/* Per-tick capture numbers for the tick push. */
void state_stats_json(buf_t *b) {
    kst_tick_stats *st = &g_last_st;
    b_str(b, "{\"ww\":"); b_u64(b, st->ww_dirty);
    b_str(b, ",\"img\":"); b_u64(b, st->img_dirty);
    b_str(b, ",\"commit\":"); b_u64(b, st->commit_changed);
    b_str(b, ",\"hashed\":"); b_u64(b, st->hashed);
    b_str(b, ",\"emitted\":"); b_u64(b, st->emitted);
    b_str(b, ",\"decommits\":"); b_u64(b, st->decommits);
    b_str(b, ",\"us\":"); b_u64(b, st->us_total);
    b_str(b, ",\"us_ww\":"); b_u64(b, st->us_ww);
    b_str(b, ",\"us_walk\":"); b_u64(b, st->us_walk);
    b_str(b, ",\"us_hash\":"); b_u64(b, st->us_hash);
    b_str(b, ",\"us_root\":"); b_u64(b, st->us_root);
    b_str(b, ",\"us_ring\":"); b_u64(b, st->us_ring);
    b_str(b, ",\"ring_wait_us\":"); b_u64(b, st->ring_wait_us);
    b_str(b, ",\"scrub\":"); b_u64(b, g_scrub_pages);
    b_str(b, ",\"arena_hw\":"); b_u64(b, (u64)K->ah->hw_gran * 65536ull);
    b_str(b, "}");
    g_scrub_pages = 0;
}

void state_audit_json(buf_t *b) {
    kst_audit_result a;
    int ok = kst_audit(&a);
    u8 r[32];
    kst_root(r);
    b_str(b, "{\"ok\":"); b_str(b, ok ? "true" : "false");
    b_str(b, ",\"pages\":"); b_u64(b, a.pages);
    b_str(b, ",\"key_mismatch\":"); b_u64(b, a.key_mismatch);
    b_str(b, ",\"missing\":"); b_u64(b, a.missing);
    b_str(b, ",\"extra\":"); b_u64(b, a.extra);
    b_str(b, ",\"prot_mismatch\":"); b_u64(b, a.prot_mismatch);
    b_str(b, ",\"first_bad\":\""); b_hexu32(b, a.first_bad_vaddr);
    b_str(b, "\",\"us\":"); b_u64(b, a.us);
    b_str(b, ",\"root\":\""); b_hex(b, r, 32);
    b_str(b, "\",\"captures\":"); b_u64(b, g_cap_n);
    b_str(b, ",\"cap_us_mean\":"); b_u64(b, g_cap_n ? g_cap_us_sum / g_cap_n : 0);
    b_str(b, ",\"cap_us_max\":"); b_u64(b, g_cap_us_max);
    b_str(b, ",\"emitted_mean\":"); b_u64(b, g_cap_n ? g_emit_sum / g_cap_n : 0);
    b_str(b, ",\"emitted_max\":"); b_u64(b, g_emit_max);
    b_str(b, ",\"ww_mean\":"); b_u64(b, g_cap_n ? g_ww_sum / g_cap_n : 0);
    b_str(b, ",\"ww_max\":"); b_u64(b, g_ww_max);
    b_str(b, ",\"arena_hw\":"); b_u64(b, (u64)K->ah->hw_gran * 65536ull);
    b_str(b, ",\"heap_creates\":"); b_u64(b, g_heap_creates);
    b_str(b, ",\"va_calls\":"); b_u64(b, g_va_calls);
    b_str(b, ",\"gl_retired\":"); b_u64(b, g_gl_retired);
    b_str(b, "}");
}

/* Restore to tick T at this gate (contract 4): the real side first (real file
 * and find handles are closed; they reopen by path on use), then the minimal
 * write set with per-page commit reconciliation, then the verification: the
 * re-hash of every written page, the state root against T's, and a full audit.
 * Registers come back through the context records in harness_game. */
int state_restore(u64 tick, const u8 root[32], const char *winpath, buf_t *out) {
    WCHAR wp[520];
    int n = MultiByteToWideChar(CP_ACP, 0, winpath, -1, wp, 520);
    if (n <= 0) { b_str(out, "{\"error\":\"bad path\"}"); return 0; }
    u64 from = g_gate.tick;
    LARGE_INTEGER f, t0, t1;
    QueryPerformanceFrequency(&f);
    QueryPerformanceCounter(&t0);
    vhandle_reconcile();
    kst_restore_stats rs;
    g_restoring = 1;
    int ok = kst_restore_file(wp, tick, root, &rs);
    g_restoring = 0;
    vhandle_reconcile();
    QueryPerformanceCounter(&t1);
    if (ok < 0) { b_str(out, "{\"error\":\"cannot open the restore set\"}"); return 0; }
    kst_root(g_root);
    g_root_valid = 1;
    u32 us = (u32)((t1.QuadPart - t0.QuadPart) * 1000000 / f.QuadPart);
    int tick_ok = g_gate.tick == tick;
    slog("restore %u -> %u: ok=%d records=%u skipped=%u written=%u committed=%u decommitted=%u prot=%u "
         "verify_bad=%u root_ok=%u audit(pages=%u key=%u missing=%u extra=%u prot=%u first=%x) tick_ok=%d us=%u",
         (u32)from, (u32)tick, ok, rs.records, rs.skipped, rs.written, rs.committed, rs.decommitted,
         rs.prot_changes, rs.verify_bad, rs.root_ok, rs.audit.pages, rs.audit.key_mismatch, rs.audit.missing,
         rs.audit.extra, rs.audit.prot_mismatch, rs.audit.first_bad_vaddr, tick_ok, us);
    b_str(out, "{\"from\":"); b_u64(out, from);
    b_str(out, ",\"tick\":"); b_u64(out, g_gate.tick);
    b_str(out, ",\"verified\":"); b_str(out, (ok > 0 && tick_ok) ? "true" : "false");
    b_str(out, ",\"root\":\""); b_hex(out, g_root, 32);
    b_str(out, "\",\"root_ok\":"); b_u64(out, rs.root_ok);
    b_str(out, ",\"records\":"); b_u64(out, rs.records);
    b_str(out, ",\"skipped\":"); b_u64(out, rs.skipped);
    b_str(out, ",\"pages\":"); b_u64(out, rs.written);
    b_str(out, ",\"committed\":"); b_u64(out, rs.committed);
    b_str(out, ",\"decommitted\":"); b_u64(out, rs.decommitted);
    b_str(out, ",\"prot_changes\":"); b_u64(out, rs.prot_changes);
    b_str(out, ",\"verify_bad\":"); b_u64(out, rs.verify_bad);
    b_str(out, ",\"audit\":{\"pages\":"); b_u64(out, rs.audit.pages);
    b_str(out, ",\"key_mismatch\":"); b_u64(out, rs.audit.key_mismatch);
    b_str(out, ",\"missing\":"); b_u64(out, rs.audit.missing);
    b_str(out, ",\"extra\":"); b_u64(out, rs.audit.extra);
    b_str(out, ",\"prot_mismatch\":"); b_u64(out, rs.audit.prot_mismatch);
    b_str(out, ",\"first_bad\":\""); b_hexu32(out, rs.audit.first_bad_vaddr);
    b_str(out, "\"},\"us_apply\":"); b_u64(out, rs.us_apply);
    b_str(out, ",\"us_verify\":"); b_u64(out, rs.us_verify);
    b_str(out, ",\"us\":"); b_u64(out, us);
    b_str(out, "}");
    return ok > 0 && tick_ok;
}
