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
 * so a slot's stack pages hold only live frames when they are hashed.
 *
 * Restore targets (contract 4.1, tas). A tas restore may target only a root
 * this process captured itself, or a root of the admitted set the driver hands
 * over once per process (the source bundle's recorded roots). Both sets live in
 * harness_core memory, outside the sim set, so no restore can rewrite them. */
#include "shim.hpp"

/* Fixed C hook ABI and assembly-referenced symbol names. */
extern "C" {

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
/* Capture cadence (harness_core): whether the live map equals live memory at
 * this gate, and the skipped gates' numbers. */
static int g_map_current = 1;   /* no uncaptured gate since the last capture or restore */
static kst_tick_stats g_skip_st;
static u64 g_skip_n;
/* Opt-in wall clocks live in harness_core, never in SH or the restored sim set.
 * kst's existing counters exclude window/GL capture and stack scrubbing. */
enum { SCP_WINDOW, SCP_GL, SCP_SCRUB, SCP_MEMORY, SCP_ROOT_OWNERSHIP, SCP_TOTAL, SCP_COUNT };
static const char *STATE_PHASE_NAMES[SCP_COUNT] = {
    "window", "gl", "stack_scrub", "memory", "root_ownership", "total"};
static capture_profile_t g_state_phase[SCP_COUNT];
static u64 g_state_last_us[SCP_COUNT];
static void state_profile_record(u32 phase,u64 start,u64 end) {
    u64 before=g_state_phase[phase].total_us;
    glsync_profile_record(&g_state_phase[phase],start,end);
    g_state_last_us[phase]=g_state_phase[phase].total_us-before;
}
static void state_profile_json(buf_t *b) {
    b_str(b,"{\"enabled\":");b_str(b,glsync_profile_enabled()?"true":"false");
    b_str(b,",\"phases\":{");
    for(u32 i=0;i<SCP_COUNT;i++) {
        if(i)b_ch(b,',');b_jstr(b,STATE_PHASE_NAMES[i]);b_str(b,":{\"last_us\":");
        b_u64(b,g_state_last_us[i]);b_str(b,",\"calls\":");b_u64(b,g_state_phase[i].calls);
        b_str(b,",\"total_us\":");b_u64(b,g_state_phase[i].total_us);
        b_str(b,",\"max_us\":");b_u64(b,g_state_phase[i].max_us);b_ch(b,'}');
    }
    b_str(b,"}}");
}

/* roots this process captured: open addressing on the root's first word (roots are SHA-256
 * output), an all-zero slot is empty */
static u8 (*g_own)[32];
static u32 g_own_cap, g_own_n;
/* the admitted set: strictly ascending roots, read once per process */
static u8 (*g_adm)[32];
static u32 g_adm_n;
static int g_adm_set;
static char g_adm_hex[65];

static void *core_pages(u32 bytes) {
    return VirtualAlloc(0, bytes, MEM_RESERVE | MEM_COMMIT | MEM_TOP_DOWN, PAGE_READWRITE);
}

static int is_zero32(const u8 *p) {
    for (u32 i = 0; i < 32; i++)
        if (p[i]) return 0;
    return 1;
}

static u32 own_slot(u8 (*tab)[32], u32 cap, const u8 r[32]) {
    u32 i;
    memcpy(&i, r, 4);
    for (i &= cap - 1;; i = (i + 1) & (cap - 1))
        if (is_zero32(tab[i]) || !memcmp(tab[i], r, 32)) return i;
}

static void own_add(const u8 r[32]) {
    if (is_zero32(r)) return;
    if ((g_own_n + 1) * 2 > g_own_cap) {
        u32 cap = g_own_cap ? g_own_cap * 2 : 8192;
        u8 (*tab)[32] = static_cast<u8 (*)[32]>(core_pages(cap * 32));
        if (!tab) { slog("state: no memory for %u recorded roots; tas restores refuse new ones", cap); return; }
        for (u32 i = 0; i < g_own_cap; i++)
            if (!is_zero32(g_own[i])) memcpy(tab[own_slot(tab, cap, g_own[i])], g_own[i], 32);
        if (g_own) VirtualFree(g_own, 0, MEM_RELEASE);
        g_own = tab;
        g_own_cap = cap;
    }
    u32 i = own_slot(g_own, g_own_cap, r);
    if (is_zero32(g_own[i])) { memcpy(g_own[i], r, 32); g_own_n++; }
}

static int own_has(const u8 r[32]) {
    return g_own_cap && !is_zero32(r) && !is_zero32(g_own[own_slot(g_own, g_own_cap, r)]);
}

static int adm_has(const u8 r[32]) {
    u32 lo = 0, hi = g_adm_n;
    while (lo < hi) {
        u32 m = (lo + hi) / 2;
        int c = memcmp(g_adm[m], r, 32);
        if (!c) return 1;
        if (c < 0) lo = m + 1;
        else hi = m;
    }
    return 0;
}

int state_root_allowed(const u8 root[32], const char **by) {
    if (own_has(root)) { *by = "self"; return 1; }
    if (g_adm_set && adm_has(root)) { *by = g_adm_hex; return 1; }
    return 0;
}

/* The admitted set: a file of strictly ascending 32-byte roots. Once per process. */
const char *state_admit(const char *winpath, i64 n_expect, const char *sha_hex, u32 *n_out, u8 sha[32]) {
    if (g_adm_set) return "the admitted roots are set once per process";
    WCHAR wp[520];
    if (!*winpath || MultiByteToWideChar(CP_ACP, 0, winpath, -1, wp, 520) <= 0) return "bad path";
    HANDLE h = CreateFileW(wp, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE, 0, OPEN_EXISTING,
                           FILE_ATTRIBUTE_NORMAL, 0);
    if (h == INVALID_HANDLE_VALUE) return "cannot open the admitted roots";
    DWORD size = GetFileSize(h, 0), got = 0;
    if (size == INVALID_FILE_SIZE || size % 32 || size > (256u << 20)) { CloseHandle(h); return "the file is not a whole number of roots"; }
    u8 (*tab)[32] = size ? static_cast<u8 (*)[32]>(core_pages(size)) : nullptr;
    if (size && !tab) { CloseHandle(h); return "no memory for the admitted roots"; }
    int ok = !size || (ReadFile(h, tab, size, &got, 0) && got == size);
    CloseHandle(h);
    const char *err = ok ? 0 : "read failed";
    u32 n = size / 32;
    for (u32 i = 1; !err && i < n; i++)
        if (memcmp(tab[i - 1], tab[i], 32) >= 0) err = "roots must be strictly ascending";
    if (!err && n_expect >= 0 && (u64)n_expect != n) err = "n differs from the file";
    sha256(tab, size, sha);
    char hex[65];
    for (u32 i = 0; i < 32; i++) {
        hex[i * 2] = "0123456789abcdef"[sha[i] >> 4];
        hex[i * 2 + 1] = "0123456789abcdef"[sha[i] & 15];
    }
    hex[64] = 0;
    if (!err && *sha_hex && !k_strieq(sha_hex, hex)) err = "sha256 differs from the file";
    if (err) {
        if (tab) VirtualFree(tab, 0, MEM_RELEASE);
        return err;
    }
    g_adm = tab;
    g_adm_n = n;
    memcpy(g_adm_hex, hex, 65);
    g_adm_set = 1;
    *n_out = n;
    slog("state: %u admitted roots, sha256 %s", n, hex);
    return 0;
}

#define ARENA_BASE 0x22000000u
/* Keep 1 GiB usable in both old and new WoW64 maps, excluding the shared
 * loader reservation at 30000000..31000000. The upper extent holds the heap. */
#define ARENA_SIZE (1040u << 20)
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
    cfg.arena_gap_base = 0x30000000u;
    cfg.arena_gap_size = 16u << 20;
    cfg.hg_base = HG_BASE;
    cfg.hg_size = HG_SIZE;
    cfg.ring_path = 0;
    cfg.ring_mb = 256;
    cfg.nslots = KST_MAX_SLOTS;
    cfg.main_stack = 0;
    cfg.x87_cw = g_x87_cw;
    cfg.mxcsr = g_mxcsr;
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
    SH->save_ms = SAVE_MS_DEFAULT;
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
    if(!glenv_capture_ready()){slog("capture refused inside active GL boundary");TerminateProcess(GetCurrentProcess(),6);for(;;){} }
    u64 begin=glsync_profile_now(),stamp=begin,next;
    if(!wh_capture_state()) {
        slog("capture refused: window resource state is incomplete");
        TerminateProcess(GetCurrentProcess(),6);for(;;) {}
    }
    next=glsync_profile_now();state_profile_record(SCP_WINDOW,stamp,next);stamp=next;
    glsync_capture();
    next=glsync_profile_now();state_profile_record(SCP_GL,stamp,next);stamp=next;
    scrub_stacks();
    next=glsync_profile_now();state_profile_record(SCP_SCRUB,stamp,next);stamp=next;
    kst_capture_tick(tick, 0);
    next=glsync_profile_now();state_profile_record(SCP_MEMORY,stamp,next);stamp=next;
    g_jptr = 0;
    kst_root(g_root);
    g_root_valid = 1;
    own_add(g_root);
    next=glsync_profile_now();state_profile_record(SCP_ROOT_OWNERSHIP,stamp,next);
    state_profile_record(SCP_TOTAL,begin,next);
    g_last_st = K->st;
    g_last_tick_captured = tick;
    g_map_current = 1;
    g_cap_n++;
    g_cap_us_sum += K->st.us_total;
    if (K->st.us_total > g_cap_us_max) g_cap_us_max = K->st.us_total;
    g_emit_sum += K->st.emitted;
    if (K->st.emitted > g_emit_max && tick) g_emit_max = K->st.emitted;
    g_ww_sum += K->st.ww_dirty;
    if (K->st.ww_dirty > g_ww_max && tick) g_ww_max = K->st.ww_dirty;
}

/* An uncaptured gate (capture cadence). The work that changes what the game or
 * the sim set can see runs exactly as at a captured gate: the GL-boundary check,
 * window state (descriptors in SH, retirement of dead windows and DCs, which
 * the game observes through alias reuse), the GL error transfer, the dead-stack
 * scrub over the pages written during this tick only (kst_skip_tick resets
 * write-watch after it), and the harness_game tick counters. Hashing, the root,
 * GL object/framebuffer readback and the spool are left to the next capture. */
void state_skip(u64 tick, const char *journal, u32 jlen) {
    if (!g_capture_on) return;
    g_jptr = journal;
    g_jlen = jlen;
    if(!glenv_capture_ready()){slog("capture refused inside active GL boundary");TerminateProcess(GetCurrentProcess(),6);for(;;){} }
    if(!wh_capture_state()) {
        slog("capture refused: window resource state is incomplete");
        TerminateProcess(GetCurrentProcess(),6);for(;;) {}
    }
    glsync_gate_skip();
    scrub_stacks();
    kst_skip_tick(tick);
    g_jptr = 0;
    g_map_current = 0;
    g_skip_st = K->st;
    g_skip_n++;
}

int state_map_current(void) { return g_map_current; }

void state_skip_stats_json(buf_t *b) {
    kst_tick_stats *st = &g_skip_st;
    b_str(b, "{\"ww\":"); b_u64(b, st->ww_dirty);
    b_str(b, ",\"commit\":"); b_u64(b, st->commit_changed);
    b_str(b, ",\"us\":"); b_u64(b, st->us_total);
    b_str(b, ",\"us_ww\":"); b_u64(b, st->us_ww);
    b_str(b, ",\"us_walk\":"); b_u64(b, st->us_walk);
    b_str(b, ",\"us_ring\":"); b_u64(b, st->us_ring);
    b_str(b, ",\"scrub\":"); b_u64(b, g_scrub_pages);
    b_str(b, ",\"skipped\":"); b_u64(b, g_skip_n);
    b_str(b, "}");
    g_scrub_pages = 0;
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
    b_str(b, ",\"capture_phases\":");state_profile_json(b);
    b_str(b, "}");
    g_scrub_pages = 0;
}

void state_audit_json(buf_t *b,int external_pending) {
    kst_audit_result a;
    int ok = kst_audit(&a);
    u8 r[32];
    kst_root(r);
    b_str(b,"{\"scope\":\"memory_only\",\"external_pending\":");b_str(b,external_pending?"true":"false");
    if (!g_map_current) {
        /* capture cadence: this gate was not captured, so the map is the last
         * capture's and live memory has moved on; mismatches are expected */
        ok = 0;
        b_str(b, ",\"map_current\":false,\"captured_tick\":"); b_u64(b, g_last_tick_captured);
    }
    b_str(b, ",\"ok\":"); b_str(b, ok ? "true" : "false");
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
    b_str(b, ",\"fpu_drift\":"); b_u64(b, g_fpu_drift);
    b_str(b, ",\"gl\":"); glsync_stats_json(b);
    b_str(b, ",\"capture_phases\":");state_profile_json(b);
    b_str(b, "}");
}

/* Restore to tick T at this gate (contract 4): the real side first (real file
 * and find handles are closed; they reopen by path on use), then the minimal
 * write set with per-page commit reconciliation, then the verification: the
 * re-hash of every written page, the state root against T's, and a full audit.
 * Registers come back through the context records in harness_game.
 * Returns 1 when T verified, 0 when the set was applied and did not verify,
 * -1 when nothing was written. The reply object is left open for the caller. */
int state_restore(u64 tick, const u8 root[32], const char *winpath, buf_t *out) {
    WCHAR wp[520];
    int n = MultiByteToWideChar(CP_ACP, 0, winpath, -1, wp, 520);
    if (n <= 0) { b_str(out, "{\"error\":\"bad path\""); return -1; }
    /* Resolve the restore artifact before disturbing the held game. A missing
     * write-set must not drain queued messages, close the game's real handles,
     * or invalidate the presented frame. Pass this exact open handle to KST
     * so validation and application cannot race a second path lookup. */
    HANDLE restore_set = CreateFileW(wp, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE,
                                     0, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, 0);
    if (restore_set == INVALID_HANDLE_VALUE) {
        b_str(out, "{\"error\":\"cannot open the restore set\"");
        return -1;
    }
    u64 from = g_gate.tick;
    LARGE_INTEGER f, t0, t1;
    QueryPerformanceFrequency(&f);
    QueryPerformanceCounter(&t0);
    msg_restore_prepare();
    vhandle_reconcile();
    kst_restore_stats rs;
    g_restoring = 1;
    int ok = kst_restore_handle(restore_set, tick, root, &rs);
    CloseHandle(restore_set);
    g_restoring = 0;
    /* Any restore that reached kst's finish rehashed every page it wrote, so
     * the map describes live memory again (verified or not). */
    if (ok >= 0) { g_map_current = 1; g_last_tick_captured = tick; }
    vhandle_reconcile();
    frames_restored();
    render_restored();
    QueryPerformanceCounter(&t1);
    if (ok > 0 && !wh_restore_validate()) {
        b_str(out, "{\"error\":\"cannot rebind restored window resources\"");
        return 0;
    }
    if (ok > 0 && !msg_restore_finish()) {
        b_str(out, "{\"error\":\"cannot reconstruct restored message queue\"");
        return 0;
    }
    if (ok > 0 && !gl_restore_procs()) {
        b_str(out, "{\"error\":\"cannot rebind restored GL procedures\"");
        return 0;
    }
    if (ok < 0) { b_str(out, "{\"error\":\"cannot open the restore set\""); return -1; }
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
    return ok > 0 && tick_ok;
}

/* External reconstruction commits only if it left all captured memory exactly
 * at the requested target. The audit rehashes live bytes, not cached roots. */
int state_restore_complete(u64 tick,const u8 root[32]) {
    kst_audit_result a;u8 actual[32];
    int ok=kst_audit(&a);kst_root(actual);
    return ok && g_gate.tick==tick && !memcmp(actual,root,32);
}

} /* extern "C" */
