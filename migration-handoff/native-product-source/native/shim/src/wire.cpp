/* Control channel (contract C1, section 10): loopback TCP, NDJSON, kptas-wire/1.
 * Python listens; the shim connects from the entry detour. Requests are only
 * read on the harness fiber: before start, and at the gate. The game is never
 * blocked outside the gate.
 *
 * Verbs: hello, tier.lock, clock, clock.save_ms, clock.save_ms_next,
 * os_event, subscribe, ring.attach, capture, frames, start, step, input,
 * wait, digest, mem.read, semantic.read, gate, restore, restore.admit,
 * verify, stats, bye (and quit). Others: unsupported.
 *
 * Tiers: an unlocked session behaves like tas. Verbs whose verbclass writes
 * game state outside the game's own recorded states (mem.write,
 * semantic.apply) are refused unless tier.lock chose lab. restore rewinds to
 * one of the game's own recorded states (verbclass restore) and is allowed in
 * tas, where it is held to those states:
 *   - its target root must be one this process captured, or one of the
 *     admitted set (restore.admit, once per process); any other is refused
 *     before anything is read or written;
 *   - a restore that does not verify poisons the session: step, wait and
 *     digest are refused until a restore verifies or the process is replaced.
 *     Rolling back instead would need a copy of every page the set overwrites
 *     (hundreds of MB in a 2 GB address space; the set streams through 1 MiB)
 *     and could not undo the real side the restore also touched (closed
 *     handles, the files tree Python put first).
 * Every restore that verifies is journalled on the next tick (EV_RESTORE)
 * with the root and what admitted it, and that tick's verbclass carries
 * RESTORE. lab restores any root and is never poisoned; a lab restore that
 * does not verify is journalled with the root memory then holds. */
#include "shim.hpp"
#include "physical_input.hpp"

/* Fixed C hook ABI and assembly-referenced symbol names. */
extern "C" {

int g_wire_ok;
static SOCKET g_sock = INVALID_SOCKET;
static int ring_patience(void);   /* kst ring_wait: a streamed step waits on a slow host */
static char *g_rbuf;
static u32 g_rn;
#define RBUF (2u << 20)
static char *g_out;
#define OBUF (4u << 20)
static char *g_jent;
#define JBUF (256u << 10)
static u8 *g_scratch;
#define SCRATCH (8u << 20)

static char g_tier[16] = "lab";
static int g_tier_locked;
static int g_sub_tick = 0;
static i64 g_start_id = -1, g_step_id = -1, g_wait_id = -1;
static u64 g_step_left;
/* Streamed step (hello caps.step_stream = 1): step takes stream:true. The step runs
 * its n ticks without holding a gate between them and pushes each gate's report,
 *   {"push":"step.tick","id":<step id>,"files":<bool>,"held":<bool>,"report":<step report>},
 * whose events and verbclass are that tick's only (a 1-tick step's reply, field for
 * field). The reply follows the held gate's push and repeats its report. A tick
 * that moved g_file_touch (the game made a file-mutating call since the gate before)
 * ends the step at its own gate, before n, with "stopped":"files" and "left" (ticks
 * not run), so the host reads the stage files at that held gate. The capture
 * decision of that gate is the one the step made without the stop, as for a
 * 1-tick sub-step with capture_last false. */
static int g_step_stream;
static u32 g_stream_touch;       /* g_file_touch at the last gate */
u32 g_file_touch;
static char g_wait_event[32];
static u64 g_wait_deadline;
static u32 g_wait_base;
/* Capture cadence (hello caps.capture_cadence = 1). step and wait take
 *   capture_every N  (integer >= 1, default 1): capture the gates whose tick
 *                    plus capture_phase is a multiple of N; 1 captures every
 *                    gate (the default),
 *   capture_phase P  (integer 0..N-1, default 0): the host's global-tick offset
 *                    mod N, so the grid lands on the recording's keyframe ticks,
 *   capture_last     (boolean, default true): capture the gate where the
 *                    command ends and the gate is held.
 * The start gate, and a gate with no command, are always captured. A gate not
 * captured runs everything except the state capture and puts JOURNAL plus
 * TICK_END(flags UNCAPTURED 0x800), no PAGE records, on the ring; its tick push
 * says "captured":false and has no root. A held gate that was not captured has
 * no root in step reports and refuses restore. Absent arguments are today's
 * behaviour exactly. */
static u64 g_cap_every = 1, g_cap_phase;
static int g_cap_last = 1;
static int g_release;
static input_t g_input;          /* held input for the running request */
/* Host-only source authority is never restored with game memory. Physical
 * candidates still pass through the same di_stage, journal and capture gate. */
static telos::PhysicalInputCollector g_physical;
static telos::PhysicalInputCollector::Prepared g_physical_prepared;
static HWND g_physical_window;
static int g_physical_enabled, g_physical_ticket_valid;
static u64 g_physical_ticket;

static void physical_observe_impl(const MSG *host_message) {
    HWND real_window = static_cast<HWND>(wh_real(*(HWND *)(uintptr_t)ADDR_RENDER_WINDOW));
    if (!real_window) return;
    if (real_window != g_physical_window) {
        g_physical.bind(real_window);
        g_physical_window = real_window;
        g_physical_ticket_valid = 0;
        // A replaced native window needs a fresh acknowledged source handoff.
        if (g_physical_enabled) return;
    }
    g_physical.set_focus(wh_real(GetForegroundWindow()) == real_window);
    u8 unused[9]{};u32 unused_count=0;hud_control_observe(NULL,unused,&unused_count);
    if (host_message) {
        MSG real = *host_message;
        wh_msg_real(&real);
        u8 suppressed[9]{};u32 count=0;
        if(hud_control_observe(&real,suppressed,&count)){
            for(u32 i=0;i<count;i++)g_physical.suppress_key(suppressed[i]);
            g_physical_ticket_valid=0;
        }
        g_physical.observe(real);
    }
}

void wire_physical_observe(const MSG *host_message) {
    const DWORD error = GetLastError();
    math_host_scope_t scope;
    math_scope_begin(&scope,MATH_HOST_FP|MATH_HOST_ID);
    physical_observe_impl(host_message);
    math_scope_end(&scope);
    SetLastError(error);
}
static char g_req_note[256] = ".";
static int g_first_tick;
static u32 g_verbclass_tick, g_verbclass_acc;
static u32 g_last_load_done;
static u8 g_last_jdig[16];
static int g_input_close;        /* the held note carries !close (first tick only) */
static int g_os_close;           /* os_event close: the next tick */
static int g_input_altf4;        /* the held note carries !altf4 (first tick only) */
static int g_os_altf4;           /* os_event altf4: the next tick */
static int g_exited;
/* Snapshot of the last successfully staged candidate. This remains outside the
 * guest journal because an exit can happen before the next gate serializes it. */
static input_t g_staged_input;
static char g_staged_note[256] = ".";
static u8 g_staged_hash[16];
static int g_staged_valid, g_staged_first;
static u32 g_staged_os_events;

/* events of the running step or wait, reported in its reply */
#define SEV_MAX 64
static u32 g_sev_kind[SEV_MAX], g_sev_val[SEV_MAX], g_sev_aux[SEV_MAX], g_sev_n;
static u64 g_sev_tick[SEV_MAX];
static u32 g_sev_lost;          /* events past SEV_MAX: the report says events_truncated */
u32 g_ev_count[EV_KINDS];
static u32 g_wait_kind;
/* journalled inputs staged at a gate for the next tick */
static u32 g_stage_kind[8], g_stage_val[8], g_stage_n;

static const char *const EV_NAME[EV_KINDS] = {"load.done", "save.begin", "save.done", "close", "proc.down",
                                              "clock.save_ms", "clock.save_ms_next", "altf4", "restore"};
static int g_rest_staged;        /* an EV_RESTORE is staged for the next tick */
static u32 g_vc_next;            /* verbclass bits for the next tick (RESTORE) */
static int g_frame_fault;        /* missing mandatory output: fresh owner required */
static int g_integrity_fault;    /* a patched slot changed: the run halted at a gate */
static int g_journal_fault;      /* bounded event provenance was incomplete: relaunch required */
static int g_poisoned;           /* pending or failed full restore: execution prohibited */
static int g_restore_pending;
static u64 g_restore_tick;
static u8 g_restore_root[32];
static const char *g_restore_by;

void ev_add_aux(u32 kind, u32 val, u32 aux) {
    if (kind >= EV_KINDS) return;
    g_ev_count[kind]++;
    if ((kind <= EV_PROC_DOWN || kind == EV_ALTF4) && g_sev_n < SEV_MAX) {
        g_sev_kind[g_sev_n] = kind;
        g_sev_val[g_sev_n] = val;
        g_sev_aux[g_sev_n] = aux;
        g_sev_tick[g_sev_n] = g_gate.tick;
        g_sev_n++;
    } else if (kind <= EV_PROC_DOWN || kind == EV_ALTF4) {
        if (g_sev_lost != 0xffffffffu) g_sev_lost++;
    }
    if (kind != EV_LOAD_DONE && kind != EV_PROC_DOWN) journal_ev_add(kind, val, aux);
}

void ev_add(u32 kind, u32 val) { ev_add_aux(kind, val, 0); }

static void stage_ev(u32 kind, u32 val) {
    if (g_stage_n < 8) { g_stage_kind[g_stage_n] = kind; g_stage_val[g_stage_n] = val; g_stage_n++; }
}

/* A verified restore replaces the game state represented by gate-staged
 * commands. Their abandoned-future journal events and control requests must
 * not leak into the restored timeline. Do not touch SH here: restored input,
 * save overrides and already-staged game messages belong to the target.
 * No step/wait is active while wire_service accepts a restore at the gate.
 * Process admission history and event counters remain intact. */
static void restore_pending_reset(void) {
    g_physical.discard_pending();
    g_physical_ticket_valid = 0;
    g_stage_n = 0;
    g_rest_staged = 0;
    g_vc_next = 0;
    g_os_close = g_os_altf4 = 0;
    memset(&g_input, 0, sizeof g_input);
    memset(&g_staged_input, 0, sizeof g_staged_input);
    memset(g_staged_hash, 0, sizeof g_staged_hash);
    memset(g_staged_note, 0, sizeof g_staged_note);
    g_staged_note[0] = '.';
    g_staged_valid = g_staged_first = 0;
    g_staged_os_events = 0;
    g_input_close = g_input_altf4 = g_first_tick = 0;
    memset(g_req_note, 0, sizeof g_req_note);
    g_req_note[0] = '.';
    g_sev_n = g_sev_lost = 0;
    g_verbclass_tick = g_verbclass_acc = 0;
}

/* Called after the per-tick resets, as tick g_gate.tick begins. */
void wire_tick_begin(void) {
    for (u32 i = 0; i < g_stage_n; i++) ev_add(g_stage_kind[i], g_stage_val[i]);
    g_stage_n = 0;
    g_rest_staged = 0;
    if (SH->close_next) {
        SH->close_next = 0;
        ev_add(EV_CLOSE, 0);
    }
    if (SH->altf4_next) {
        SH->altf4_next = 0;
        ev_add(EV_ALTF4, 0);
    }
}

#define VC_INPUT 1u
#define VC_GATE 4u
#define VC_CLOCK 8u
#define VC_WAIT 16u
#define VC_CAPTURE 32u
#define VC_READ 64u
#define VC_RESTORE 512u

static int send_all(const char *p, u32 n) {
    while (n) {
        int s = send(g_sock, p, (int)(n > 65536 ? 65536 : n), 0);
        if (s <= 0) return 0;
        p += s;
        n -= (u32)s;
    }
    return 1;
}

void wire_send_line(buf_t *b) {
    if (g_sock == INVALID_SOCKET) return;
    b_ch(b, '\n');
    if (b->overflow) slog("wire: line overflow (%u bytes)", b->n);
    if (!send_all(b->p, b->n)) {
        slog("wire: send failed, peer gone; terminating");
        TerminateProcess(GetCurrentProcess(), 3);
    }
}

int wire_connect(void) {
    char port[16];
    if (!GetEnvironmentVariableA("KPTAS_PORT", port, sizeof port)) return 0;
    u32 p = (u32)k_parse_u64(port, 0);
    WSADATA wd;
    if (WSAStartup(MAKEWORD(2, 2), &wd) != 0) { slog("WSAStartup failed"); return 0; }
    g_rbuf = (char *)core_alloc(RBUF);
    g_out = (char *)core_alloc(OBUF);
    g_jent = (char *)core_alloc(JBUF);
    g_scratch = (u8 *)core_alloc(SCRATCH);
    if (!g_rbuf || !g_out || !g_jent || !g_scratch) { slog("core alloc failed"); return 0; }
    for (int attempt = 0; attempt < 300; attempt++) {
        SOCKET s = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
        struct sockaddr_in a;
        memset(&a, 0, sizeof a);
        a.sin_family = AF_INET;
        a.sin_port = htons((u16)p);
        a.sin_addr.s_addr = htonl(0x7f000001u);
        if (connect(s, (struct sockaddr *)&a, sizeof a) == 0) {
            int one = 1;
            setsockopt(s, IPPROTO_TCP, TCP_NODELAY, (const char *)&one, sizeof one);
            g_sock = s;
            g_wire_ok = 1;
            kst_ring_patience = ring_patience;
            slog("wire: connected to 127.0.0.1:%u", p);
            return 1;
        }
        closesocket(s);
        Sleep(100);
    }
    slog("wire: connect to port %u failed", p);
    return 0;
}

/* 1 = line ready (NUL-terminated, consumed from buffer by caller via *consumed), 0 = timeout, -1 = closed */
static int recv_line(char **line, u32 *consumed, u32 timeout_ms) {
    for (;;) {
        for (u32 i = 0; i < g_rn; i++) {
            if (g_rbuf[i] == '\n') {
                g_rbuf[i] = 0;
                *line = g_rbuf;
                *consumed = i + 1;
                return 1;
            }
        }
        if (g_rn >= RBUF - 1) { g_rn = 0; slog("wire: line too long, dropped"); }
        fd_set fs;
        FD_ZERO(&fs);
        FD_SET(g_sock, &fs);
        struct timeval tv = {(long)(timeout_ms / 1000), (long)((timeout_ms % 1000) * 1000)};
        int r = select(0, &fs, NULL, NULL, &tv);
        if (r < 0) return -1;
        if (r == 0) return 0;
        int n = recv(g_sock, g_rbuf + g_rn, (int)(RBUF - 1 - g_rn), 0);
        if (n <= 0) return -1;
        g_rn += (u32)n;
    }
}

static void consume(u32 n) {
    memmove(g_rbuf, g_rbuf + n, g_rn - n);
    g_rn -= n;
}

/* ---- replies ---- */
static void reply_begin(buf_t *b, i64 id) {
    b_init(b, g_out, OBUF);
    b_str(b, "{\"id\":");
    b_i64(b, id);
    b_str(b, ",\"ok\":true,\"result\":");
}
static void reply_end(buf_t *b) {
    b_str(b, "}");
    wire_send_line(b);
}
static void reply_err(i64 id, const char *code, const char *msg) {
    buf_t b;
    b_init(&b, g_out, OBUF);
    b_str(&b, "{\"id\":");
    b_i64(&b, id);
    b_str(&b, ",\"ok\":false,\"error\":{\"code\":");
    b_jstr(&b, code);
    b_str(&b, ",\"message\":");
    b_jstr(&b, msg);
    b_str(&b, "}}");
    wire_send_line(&b);
}

static void terminal_input_json(buf_t *b) {
    b_str(b, ",\"terminal_input\":{\"schema\":\"kptas-terminal-input/1\",\"boundary\":\"exited-before-gate\",\"acknowledgement\":\"none\",\"guest_consumption\":\"unproven\",\"captured_root\":null,\"native_tick\":");
    b_u64(b, g_gate.tick);
    b_str(b, ",\"source\":");
    b_jstr(b, g_physical_enabled ? "physical" : "score");
    b_str(b, ",\"note\":");
    b_jstr(b, g_staged_note);
    b_str(b, ",\"first_tick\":");
    b_str(b, g_staged_first ? "true" : "false");
    b_str(b, ",\"keys\":[");
    int first = 1;
    for (u32 key = 1; key < 256; key++) {
        if (!(g_staged_input.keys[key >> 3] & (1u << (key & 7)))) continue;
        if (!first) b_ch(b, ',');
        b_u64(b, key);
        first = 0;
    }
    b_str(b, "],\"buttons\":[");
    first = 1;
    static const char *const button_names[3] = {"lmb", "rmb", "mmb"};
    for (u32 button = 0; button < 3; button++) {
        if (!(g_staged_input.buttons & (1u << button))) continue;
        if (!first) b_ch(b, ',');
        b_jstr(b, button_names[button]);
        first = 0;
    }
    b_str(b, "],\"mouse\":{\"absolute\":");
    if (g_staged_input.has_abs) {
        b_ch(b, '['); b_i64(b, g_staged_input.abs_x); b_ch(b, ',');
        b_i64(b, g_staged_input.abs_y); b_ch(b, ']');
    } else b_str(b, "null");
    b_str(b, ",\"relative\":["); b_i64(b, g_staged_input.rel_x); b_ch(b, ',');
    b_i64(b, g_staged_input.rel_y); b_str(b, "]},\"wheel\":");
    b_i64(b, g_staged_input.wheel);
    b_str(b, ",\"os_events\":[");
    first = 1;
    static const char *const os_names[3] = {"close", "altf4", "altf4@save"};
    for (u32 i = 0; i < 3; i++) {
        if (!(g_staged_os_events & (1u << i))) continue;
        if (!first) b_ch(b, ',');
        b_jstr(b, os_names[i]);
        first = 0;
    }
    b_ch(b, ']');
    b_str(b, ",\"staged_state_blake2s128\":\""); b_hex(b, g_staged_hash, 16);
    b_str(b, "\"}");
}

static void step_report(buf_t *b, int waited, const char *outcome) {
    b_str(b, "{\"epoch\":1,\"events\":[");
    for (u32 i = 0; i < g_sev_n; i++) {
        if (i) b_ch(b, ',');
        b_str(b, "{\"name\":");
        b_jstr(b, EV_NAME[g_sev_kind[i]]);
        b_str(b, ",\"tick\":");
        b_u64(b, g_sev_tick[i]);
        if (g_sev_kind[i] == EV_SAVE_BEGIN || g_sev_kind[i] == EV_SAVE_DONE) {
            b_str(b, ",\"kind\":");
            b_jstr(b, (g_sev_aux[i] & 0xff) == SAVE_KIND_AUTO ? "auto" : (g_sev_aux[i] & 0xff) == SAVE_KIND_MANUAL ? "manual" : "none");
            b_str(b, ",\"slot\":");
            b_u64(b, g_sev_aux[i] >> 8);
            b_str(b, ",\"us\":");
            b_u64(b, g_sev_val[i]);
        }
        if (g_sev_kind[i] == EV_PROC_DOWN) { b_str(b, ",\"code\":"); b_u64(b, g_sev_val[i]); }
        b_ch(b, '}');
    }
    b_ch(b, ']');
    b_str(b, ",\"events_complete\":");
    b_str(b, g_sev_lost ? "false" : "true");
    if (g_sev_lost) { b_str(b, ",\"events_dropped\":"); b_u64(b, g_sev_lost); }
    if (g_sev_lost) b_str(b, ",\"events_truncated\":true");
    b_str(b, ",\"journal_events_complete\":");
    b_str(b, journal_event_dropped() ? "false" : "true");
    if (journal_event_dropped()) {
        b_str(b, ",\"journal_events_dropped\":");
        b_u64(b, journal_event_dropped());
    }
    b_str(b, ",\"journal\":\"");
    b_hex(b, g_last_jdig, 16);
    b_str(b, "\",\"outcome\":");
    b_jstr(b, outcome);
    b_str(b, ",\"reason\":1,\"restore_point\":");
    b_str(b, (g_capture_on && g_root_valid && state_map_current()) ? "true" : "false");
    b_str(b, ",\"server_passes\":");
    b_u64(b, g_gate.server_passes);
    b_str(b, ",\"state_root\":");
    if (g_capture_on && g_root_valid && state_map_current()) { b_ch(b, '"'); b_hex(b, g_root, 32); b_ch(b, '"'); }
    else b_str(b, "null");
    b_str(b, ",\"subticks\":");
    b_u64(b, g_gate.subticks);
    b_str(b, ",\"tick\":");
    b_u64(b, g_gate.tick);
    b_str(b, ",\"verbclass\":");
    b_u64(b, g_verbclass_acc);
    if (waited >= 0) { b_str(b, ",\"waited\":"); b_u64(b, (u64)waited); }
    if (k_streq(outcome, "exited") && g_staged_valid &&
        (g_step_id >= 0 || g_wait_id >= 0) && (g_verbclass_tick & VC_INPUT))
        terminal_input_json(b);
    b_str(b, "}");
}

/* ---- hello ---- */
static void file_sha256(const char *path, u8 out[32], int *ok) {
    HANDLE h = CreateFileA(path, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE, NULL, OPEN_EXISTING, 0, NULL);
    *ok = 0;
    if (h == INVALID_HANDLE_VALUE) return;
    sha256_t s;
    sha256_init(&s);
    for (;;) {
        DWORD got = 0;
        if (!ReadFile(h, g_scratch, SCRATCH, &got, NULL) || got == 0) break;
        sha256_update(&s, g_scratch, got);
    }
    CloseHandle(h);
    sha256_final(&s, out);
    *ok = 1;
}

static void list_json(buf_t *b, const char *csv) {
    b_ch(b, '[');
    const char *p = csv;
    int first = 1;
    while (*p) {
        const char *q = p;
        while (*q && *q != ',') q++;
        if (!first) b_ch(b, ',');
        first = 0;
        b_jstrn(b, p, (u32)(q - p));
        p = *q ? q + 1 : q;
    }
    b_ch(b, ']');
}

static void modules_now(char *out, u32 cap) {
    u32 peb;
    __asm__ volatile("movl %%fs:0x30,%0" : "=r"(peb));
    u32 ldr = *(u32 *)(uintptr_t)(peb + 0x0c);
    u32 head = ldr + 0x0c, cur = *(u32 *)(uintptr_t)head;
    u32 n = 0;
    out[0] = 0;
    for (int i = 0; cur != head && i < 128; i++) {
        u16 len = *(u16 *)(uintptr_t)(cur + 0x2c);  /* BaseDllName */
        u32 bufp = *(u32 *)(uintptr_t)(cur + 0x30);
        if (n && n + 1 < cap) out[n++] = ',';
        for (u32 j = 0; bufp && j < len / 2u && n + 1 < cap; j++) out[n++] = (char)((wchar_t *)(uintptr_t)bufp)[j];
        out[n] = 0;
        cur = *(u32 *)(uintptr_t)cur;
    }
}

/* The stage-file signal is complete only with every file-mutating import of the
 * exe hooked (bound.cpp's path hooks are off in native boundary mode). */
static int hooks_have(const char *name) {
    u32 n = (u32)strlen(name);
    for (const char *p = g_hooks_list; *p;) {
        const char *q = p;
        while (*q && *q != ',') q++;
        if ((u32)(q - p) == n && k_strneq(p, name, n)) return 1;
        p = *q ? q + 1 : q;
    }
    return 0;
}
/* No import of the exe may change files outside the hooked set. Names come from
 * the import name tables (IAT hooks rewrite only the address tables). A kernel32
 * import by ordinal, or a descriptor without its name table, is unknown: refused. */
static int imports_hooked_only(void) {
    static const char *const deny[] = {
        "CreateFileW", "CreateFile2", "WriteFileEx", "WriteFileGather", "DeleteFileW", "MoveFileW",
        "MoveFileExA", "MoveFileExW", "MoveFileWithProgressA", "MoveFileWithProgressW", "CopyFileW",
        "CopyFileExA", "CopyFileExW", "CreateDirectoryW", "CreateDirectoryExA", "CreateDirectoryExW",
        "RemoveDirectoryW", "SetFileAttributesW", "ReplaceFileA", "ReplaceFileW", "CreateHardLinkA",
        "CreateHardLinkW", "CreateSymbolicLinkA", "CreateSymbolicLinkW", "SetFileInformationByHandle",
        "SetFileValidData", "SetFileShortNameA", "SetFileShortNameW", "WritePrivateProfileStringA",
        "WritePrivateProfileStringW", "WritePrivateProfileSectionA", "WritePrivateProfileSectionW",
        "WritePrivateProfileStructA", "WritePrivateProfileStructW", "WriteProfileStringA",
        "WriteProfileStringW", "WriteProfileSectionA", "WriteProfileSectionW", "_lcreat", "_lwrite",
        "_hwrite", "_lopen", "OpenFile", "LZOpenFileA", "LZOpenFileW", "CreateFileMappingA",
        "CreateFileMappingW", "DeviceIoControl", "SHFileOperationA", "SHFileOperationW", "NtCreateFile",
        "NtWriteFile", "NtSetInformationFile", "ZwCreateFile", "ZwWriteFile", "ZwSetInformationFile",
        "fopen", "_fopen", "_wfopen", "freopen", "_open", "_wopen", "_sopen", "_creat", "_write",
        "fwrite", "remove", "_unlink", "rename", "_mkdir", "_rmdir", "_chsize"};
    const u8 *base = (const u8 *)GetModuleHandleA(NULL);
    u32 pe = *(const u32 *)(base + 0x3c);
    u32 imp = *(const u32 *)(base + pe + 24 + 104);       /* PE32 data directory 1 */
    if (!imp) return 1;
    for (const u32 *d = (const u32 *)(base + imp); d[3]; d += 5) {
        const char *dll = (const char *)(base + d[3]);
        if (!d[0]) { slog("step_stream off: %s has no import name table", dll); return 0; }
        for (const u32 *t = (const u32 *)(base + d[0]); *t; t++) {
            if (*t & 0x80000000u) {
                if (k_strieq(dll, "kernel32.dll")) { slog("step_stream off: kernel32 import by ordinal"); return 0; }
                continue;
            }
            const char *name = (const char *)(base + *t + 2);
            for (u32 i = 0; i < sizeof deny / sizeof deny[0]; i++)
                if (k_streq(name, deny[i])) { slog("step_stream off: the exe imports %s!%s", dll, name); return 0; }
        }
    }
    return 1;
}
static int step_stream_ready(void) {
    static const char *const need[] = {"exe:CreateFileA", "exe:WriteFile", "exe:SetEndOfFile", "exe:CloseHandle",
                                       "exe:DeleteFileA", "exe:MoveFileA", "exe:CopyFileA", "exe:CreateDirectoryA",
                                       "exe:RemoveDirectoryA", "exe:SetFileAttributesA"};
    static int imports = -1;
    if (g_bnd_native) return 0;
    for (u32 i = 0; i < sizeof need / sizeof need[0]; i++)
        if (!hooks_have(need[i])) return 0;
    if (imports < 0) imports = imports_hooked_only();
    return imports;
}

/* kst ring_wait asks this after 60 s without room. A streamed step lets the game
 * run ahead of the host by the ring, so a slow sink (a checkpoint on a slow disk)
 * can hold it longer: wait on while the connection is open and the host's beat
 * (ring header offset 64, a u32 a host thread bumps while a streamed step runs)
 * moved since the last ask. */
#define RH_BEAT 64u
static u32 g_beat_seen;
static int ring_patience(void) {
    if (g_step_id < 0 || !g_step_stream || g_sock == INVALID_SOCKET || !K->ring_base) return 0;
    const DWORD le = GetLastError();
    fd_set fs;
    FD_ZERO(&fs);
    FD_SET(g_sock, &fs);
    struct timeval tv = {0, 0};
    int r = select(0, &fs, NULL, NULL, &tv), alive = r >= 0;
    if (r > 0) {
        char c;
        alive = recv(g_sock, &c, 1, MSG_PEEK) > 0;      /* 0: the host closed; < 0: reset */
    }
    const u32 beat = *(volatile u32 *)(K->ring_base + RH_BEAT);
    const int moved = beat != g_beat_seen;
    g_beat_seen = beat;
    SetLastError(le);
    if (alive && moved) slog("ring: the host is reporting slower than the game; waiting at tick %u", (u32)g_gate.tick);
    return alive && moved;
}

/* Per-digest GL blobs (<session>\gl\<64 hex>) present at a streamed gate, listed
 * when glsync published any blob since the gate before: the host admits each with
 * the first reported tick whose push names it, never before. gl_all: no list (too
 * many, or the directory could not be read); the host admits what it finds. */
extern u32 g_gl_blobs;
static u32 g_gl_blobs_seen;
static void gl_names_json(buf_t *b) {
    if (g_gl_blobs == g_gl_blobs_seen) return;
    g_gl_blobs_seen = g_gl_blobs;
    char pat[MAX_PATH];
    u32 l = (u32)strlen(g_session_dir);
    if (!l || l + 8 > sizeof pat) { b_str(b, ",\"gl_all\":true"); return; }
    memcpy(pat, g_session_dir, l);
    memcpy(pat + l, "\\gl\\*", 6);
    const DWORD le = GetLastError();
    WIN32_FIND_DATAA fd;
    HANDLE h = FindFirstFileA(pat, &fd);
    int all = 0;
    u32 n = 0;
    if (h == INVALID_HANDLE_VALUE) {
        DWORD e = GetLastError();
        all = e != ERROR_FILE_NOT_FOUND && e != ERROR_PATH_NOT_FOUND;
    }
    b_str(b, ",\"gl_names\":[");
    if (h != INVALID_HANDLE_VALUE) {
        do {
            if ((fd.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) || strlen(fd.cFileName) != 64) continue;
            if (n == 4096) { all = 1; break; }
            if (n++) b_ch(b, ',');
            b_ch(b, '"');
            b_raw(b, fd.cFileName, 64);
            b_ch(b, '"');
        } while (FindNextFileA(h, &fd));
        FindClose(h);
    }
    b_ch(b, ']');
    if (all) b_str(b, ",\"gl_all\":true");
    SetLastError(le);
}

static void do_hello(i64 id) {
    buf_t b;
    char path[MAX_PATH];
    u8 shim_sha[32], exe_sha[32];
    int ok1 = 0, ok2 = 0;
    if (GetModuleFileNameA(g_self, path, MAX_PATH)) file_sha256(path, shim_sha, &ok1);
    if (GetModuleFileNameA(NULL, path, MAX_PATH)) file_sha256(path, exe_sha, &ok2);
    static char mods[4096];
    modules_now(mods, sizeof mods);
    reply_begin(&b, id);
    /* build: sha256 prefix of the loaded DLL file, so builds can be told apart */
    b_str(&b, "{\"boundary\":");
    bound_hello_json(&b);
    b_str(&b, ",\"build\":\"");
    if (ok1) b_hex(&b, shim_sha, 8);
    b_str(&b, "\",\"caps\":{\"capture\":true,\"clock\":[\"virtual\"],\"context\":\"fiber\"");
    b_str(&b, ",\"journal_event_completeness\":1");
    b_str(&b, ",\"dinput\":\"virtual_device\",\"gl_names\":\"virtual\",\"handles\":\"virtual\",\"heap\":\"arena\"");
    b_str(&b, ",\"frames\":\"every_tick\",\"messages\":\"interpose\",\"os_events\":[\"close\",\"altf4\",\"altf4@save\"],\"restore\":true,\"restore_admit\":true");
    b_str(&b, ",\"save_ms\":true,\"capture_cadence\":1,\"render_skip\":1");
    if (step_stream_ready()) b_str(&b, ",\"step_stream\":1");
    b_str(&b, ",\"gl_transport\":\"kptas-gl-spool/1\"");
    b_str(&b, ",\"threads\":\"slots_serial\"");
    aud_caps(&b);
    math_hello_json(&b);
    b_ch(&b, '}');
    b_str(&b, ",\"input_source_protocol\":\"kptas-input-source/1\",\"hud_bindings_protocol\":\"kptas-tool-bindings/1\",\"epoch\":1,\"exe_sha256\":\"");
    if (ok2) b_hex(&b, exe_sha, 32);
    b_str(&b, "\",\"game\":\"k1_gog_103\",\"hooks\":");
    list_json(&b, g_hooks_list);
    b_str(&b, ",\"integrity\":{\"entries\":");
    b_u64(&b, ledger_entries());
    b_str(&b, "},\"load_order\":");
    list_json(&b, g_modlist);
    b_str(&b, ",\"loader\":\"dinput8\",\"modules\":");
    list_json(&b, mods);
    b_str(&b, ",\"proto\":\"" PROTO "\",\"seed_hooked\":");
    b_str(&b, (g_clk.mode == 0 && k_strstr(g_hooks_list, "exe:GetTickCount")) ? "true" : "false");
    b_str(&b, ",\"shim\":\"" SHIM_NAME " " SHIM_VERSION "\",\"shim_sha256\":\"");
    if (ok1) b_hex(&b, shim_sha, 32);
    b_str(&b, "\",\"text_sha256\":\"");
    b_hex(&b, g_text_sha, 32);
    b_str(&b, "\"}");
    reply_end(&b);
}

/* ---- input args ---- */
static int input_integer(jnode *node, i64 lo, i64 hi, i64 *out) {
    if (!node || node->type != J_NUM || node->str || node->num < lo || node->num > hi) return 0;
    *out = node->num;
    return 1;
}

static int input_source_args_valid(jnode *args, int transition) {
    if (!args || args->type != J_OBJ) return 0;
    for (jnode *member = args->child; member; member = member->next) {
        if (!member->key) return 0;
        if (k_streq(member->key, "at_tick")) continue;
        if (transition && k_streq(member->key, "engaged")) continue;
        return 0;
    }
    return 1;
}

static int input_pair(jnode *node, i32 *x, i32 *y) {
    if (!node || node->type != J_ARR || !node->child || !node->child->next || node->child->next->next) return 0;
    i64 a, b;
    if (!input_integer(node->child, INT32_MIN, INT32_MAX, &a) ||
        !input_integer(node->child->next, INT32_MIN, INT32_MAX, &b)) return 0;
    *x = (i32)a;
    *y = (i32)b;
    return 1;
}

static int input_arg_allowed(const char *verb, const char *key) {
    if (k_streq(key, "note") || k_streq(key, "keys") || k_streq(key, "buttons") ||
        k_streq(key, "mouse") || k_streq(key, "wheel") || k_streq(key, "msgs") || k_streq(key, "first"))
        return 1;
    if (k_streq(verb, "input")) return 0;
    if (k_streq(verb, "step") && (k_streq(key, "n") || k_streq(key, "stream") ||
                                   k_streq(key, "physical_ticket"))) return 1;
    if (k_streq(verb, "wait") && (k_streq(key, "event") || k_streq(key, "arg") ||
                                   k_streq(key, "timeout"))) return 1;
    return k_streq(key, "capture_every") || k_streq(key, "capture_phase") ||
           k_streq(key, "capture_last") || k_streq(key, "render_skip") ||
           k_streq(key, "preview_every");
}

static int parse_input(jnode *args, const char *verb, input_t *in, const char **bad) {
    if (args && args->type != J_OBJ) { *bad = "input args must be an object"; return 0; }
    u8 seen = 0;
    for (jnode *c = args ? args->child : 0; c; c = c->next) {
        if (!c->key || !input_arg_allowed(verb, c->key)) {
            *bad = "unknown argument for input command"; return 0;
        }
        u8 bit = c->key && k_streq(c->key, "keys") ? 1 : c->key && k_streq(c->key, "buttons") ? 2 :
                 c->key && k_streq(c->key, "mouse") ? 4 : c->key && k_streq(c->key, "wheel") ? 8 : 0;
        if (bit && (seen & bit)) { *bad = "input properties keys, buttons, mouse, and wheel must not repeat"; return 0; }
        seen |= bit;
    }
    memset(in, 0, sizeof *in);
    jnode *k = jget(args, "keys");
    if (k && k->type != J_ARR) { *bad = "keys must be an array of integer DIK codes"; return 0; }
    if (k) for (jnode *c = k->child; c; c = c->next) {
        i64 d;
        if (!input_integer(c, 1, 255, &d)) { *bad = "keys entries must be integer DIK codes in 1..255"; return 0; }
        if (in->keys[d >> 3] & (1u << (d & 7))) { *bad = "keys entries must not repeat"; return 0; }
        in->keys[d >> 3] |= (u8)(1u << (d & 7));
    }
    jnode *bt = jget(args, "buttons");
    if (bt && bt->type != J_ARR) { *bad = "buttons must be an array of supported button names"; return 0; }
    if (bt) for (jnode *c = bt->child; c; c = c->next) {
        if (c->type != J_STR) { *bad = "buttons entries must be strings"; return 0; }
        u8 bit = k_streq(c->str, "lmb") ? 1 : k_streq(c->str, "rmb") ? 2 : k_streq(c->str, "mmb") ? 4 : 0;
        if (!bit) { *bad = "buttons supports lmb, rmb, and mmb"; return 0; }
        if (in->buttons & bit) { *bad = "buttons entries must not repeat"; return 0; }
        in->buttons |= bit;
    }
    jnode *m = jget(args, "mouse");
    if (m) {
        if (m->type != J_OBJ) { *bad = "mouse must be an object with abs and/or rel"; return 0; }
        int seen_abs = 0, seen_rel = 0;
        for (jnode *c = m->child; c; c = c->next) {
            if (k_streq(c->key, "abs")) {
                if (seen_abs++) { *bad = "mouse.abs must appear only once"; return 0; }
            } else if (k_streq(c->key, "rel")) {
                if (seen_rel++) { *bad = "mouse.rel must appear only once"; return 0; }
            } else {
                *bad = "mouse supports only abs and rel"; return 0;
            }
        }
        jnode *a = jget(m, "abs"), *r = jget(m, "rel");
        if (!a && !r) { *bad = "mouse must include abs and/or rel"; return 0; }
        if (a) {
            if (!input_pair(a, &in->abs_x, &in->abs_y)) { *bad = "mouse.abs must be two signed 32-bit integers"; return 0; }
            in->has_abs = 1;
        }
        if (r) {
            if (!input_pair(r, &in->rel_x, &in->rel_y)) { *bad = "mouse.rel must be two signed 32-bit integers"; return 0; }
        }
    }
    jnode *wheel = jget(args, "wheel");
    if (wheel) {
        i64 value;
        // di_stage scales wheel detents by WHEEL_DELTA (120) before staging.
        // Bound the wire value here so that multiplication cannot overflow.
        if (!input_integer(wheel, INT32_MIN / 120, INT32_MAX / 120, &value)) {
            *bad = "wheel is outside the safe signed 32-bit detent range"; return 0;
        }
        in->wheel = (i32)value;
    }
    return 1;
}

/* "msgs": OS events on the note's first tick. Returns 0 on an unsupported one. */
static int parse_msgs(jnode *args, int *close, int *altf4, const char **bad) {
    *close = *altf4 = 0;
    jnode *m = jget(args, "msgs");
    if (m && m->type == J_ARR)
        for (jnode *c = m->child; c; c = c->next) {
            const char *s = jstr(c, "");
            if (k_streq(s, "close")) *close = 1;
            else if (k_streq(s, "altf4")) *altf4 = 1;
            else if (k_streq(s, "altf4@save")) *altf4 = 2;
            else { *bad = s; return 0; }
        }
    return 1;
}

static void set_note(jnode *args) {
    const char *n = jstr(jget(args, "note"), ".");
    u32 l = (u32)strlen(n);
    if (l > sizeof g_req_note - 1) l = sizeof g_req_note - 1;
    memcpy(g_req_note, n, l);
    g_req_note[l] = 0;
}

/* ---- digest verb ---- */
static void do_digest(i64 id, jnode *args) {
    buf_t b;
    reply_begin(&b, id);
    b_ch(&b, '{');
    jnode *regs = jget(args, "regions");
    int first = 1;
    if (regs && regs->type == J_ARR) {
        for (jnode *c = regs->child; c; c = c->next) {
            const char *r = jstr(c, "");
            if (!first) b_ch(&b, ',');
            first = 0;
            b_jstr(&b, r);
            b_ch(&b, ':');
            if (k_streq(r, "data.pages")) {
                static u8 pages[256 * 16];
                u32 np = 0;
                journal_data_pages(pages, &np);
                b_ch(&b, '[');
                for (u32 i = 0; i < np; i++) {
                    if (i) b_ch(&b, ',');
                    b_ch(&b, '"');
                    b_hex(&b, pages + i * 16, 16);
                    b_ch(&b, '"');
                }
                b_ch(&b, ']');
            } else {
                /* "0xADDR:LEN" */
                const char *e;
                u64 a = k_parse_u64(r, &e);
                u64 l = (*e == ':') ? k_parse_u64(e + 1, 0) : 0;
                u8 d[16];
                if (a && l && l < (1u << 30) && !IsBadReadPtr((void *)(uintptr_t)a, (UINT_PTR)l)) {
                    blake2s128((const void *)(uintptr_t)a, (u32)l, d);
                    b_ch(&b, '"'); b_hex(&b, d, 16); b_ch(&b, '"');
                } else b_str(&b, "null");
            }
        }
    }
    b_ch(&b, '}');
    reply_end(&b);
    g_verbclass_tick |= VC_CAPTURE;
}

static void do_memread(i64 id, jnode *args) {
    u64 a = (u64)jint(jget(args, "addr"), 0);
    u64 l = (u64)jint(jget(args, "len"), 0);
    if (!a || !l || l > 65536) { reply_err(id, "bad_args", "addr and 0 < len <= 65536"); return; }
    if (IsBadReadPtr((void *)(uintptr_t)a, (UINT_PTR)l)) { reply_err(id, "bad_args", "unreadable range"); return; }
    buf_t b;
    reply_begin(&b, id);
    b_str(&b, "{\"data_b64\":\"");
    b_b64(&b, (const u8 *)(uintptr_t)a, (u32)l);
    b_str(&b, "\"}");
    reply_end(&b);
    g_verbclass_tick |= VC_READ;
}

static int b64v(char c) {
    if (c >= 'A' && c <= 'Z') return c - 'A';
    if (c >= 'a' && c <= 'z') return c - 'a' + 26;
    if (c >= '0' && c <= '9') return c - '0' + 52;
    if (c == '+') return 62;
    if (c == '/') return 63;
    return -1;
}

/* mem.write (lab only, see verb_allowed): raw bytes at addr. */
static void do_memwrite(i64 id, jnode *args) {
    static u8 data[65536];
    u64 a = (u64)jint(jget(args, "addr"), 0);
    const char *s = jstr(jget(args, "data_b64"), "");
    u32 n = 0, acc = 0, bits = 0;
    for (; *s && *s != '='; s++) {
        int v = b64v(*s);
        if (v < 0) { reply_err(id, "bad_args", "data_b64"); return; }
        acc = (acc << 6) | (u32)v;
        bits += 6;
        if (bits >= 8) {
            bits -= 8;
            if (n >= sizeof data) { reply_err(id, "bad_args", "more than 65536 bytes"); return; }
            data[n++] = (u8)(acc >> bits);
        }
    }
    if (!a || !n) { reply_err(id, "bad_args", "addr and data_b64"); return; }
    if (IsBadWritePtr((void *)(uintptr_t)a, n)) { reply_err(id, "bad_args", "unwritable range"); return; }
    memcpy((void *)(uintptr_t)a, data, n);
    slog("mem.write %x+%u (lab)", (u32)a, n);
    buf_t b;
    reply_begin(&b, id);
    b_str(&b, "{\"written\":");
    b_u64(&b, n);
    b_ch(&b, '}');
    reply_end(&b);
}

static int event_kind(const char *name) {
    for (u32 k = 0; k <= EV_PROC_DOWN; k++) if (k_streq(name, EV_NAME[k])) return (int)k;
    return -1;
}

/* only events after the wait began count; the driver carries the previous note's */
static int event_fired(void) {
    if (g_wait_kind >= EV_KINDS) return 0;
    return g_ev_count[g_wait_kind] != g_wait_base;
}

/* ---- dispatch ---- */
static int verb_allowed(const char *v) {
    /* writes outside the game's own recorded states need an explicit lab lock;
     * an unlocked session behaves like tas */
    if (k_streq(v, "mem.write") || k_streq(v, "semantic.apply"))
        return g_tier_locked && k_streq(g_tier, "lab");
    return 1;
}

/* Full restore is a two-stage native transaction. A failed memory stage
 * cannot be repaired by an unsolicited GL apply; another verified restore is
 * required. All safety decisions use structured return values, never JSON. */
static void restore_tx_begin(void) { g_poisoned=1;g_restore_pending=0; }
static void restore_tx_memory(int ok,u64 tick,const u8 root[32],const char*by) {
    if(ok<=0)return;
    restore_pending_reset();g_restore_pending=1;g_restore_tick=tick;
    memcpy(g_restore_root,root,32);g_restore_by=by;
}
static int restore_tx_finish(int applied) {
    if(!g_restore_pending||!applied||!state_restore_complete(g_restore_tick,g_restore_root))return 0;
    g_restore_pending=0;g_poisoned=0;
    journal_restore_note(g_restore_tick,g_restore_root,g_restore_by);
    if(!g_rest_staged){stage_ev(EV_RESTORE,0);g_rest_staged=1;}
    g_vc_next|=VC_RESTORE;
    return 1;
}
static int restore_tx_blocks(const char*verb) {
    return g_poisoned&&(k_streq(verb,"step")||k_streq(verb,"wait")||k_streq(verb,"digest"));
}

/* Frame output failure occurs before this gate's success report. Keep the
 * game parked and preserve captured memory/journal, but never certify missing
 * output. Neither frames off/reattach nor a plain restore repairs its archive. */
static int frame_fault_blocks(const char *verb) {
    int execution=k_streq(verb,"start")||k_streq(verb,"step")||
        k_streq(verb,"wait")||k_streq(verb,"input")||k_streq(verb,"os_event")||
        k_streq(verb,"digest");
    /* Poll before even frames-off/reattach can erase the active ring error. */
    if(frames_error()[0]||render_fault()[0])g_frame_fault=1;
    return g_frame_fault&&execution;
}
static int frame_fault_refuse(void) {
    const char *why=frames_error(),*render=render_fault();
    if(!why[0]&&!render[0]&&!g_frame_fault)return 0;
    g_frame_fault=1;g_release=0;
    /* A render-skip fault (render.cpp) refuses the same way: this gate is
     * neither captured nor reported, and execution stays refused. */
    char msg[256];buf_t m;b_init(&m,msg,sizeof msg);
    if(render[0])render_fault_message(&m);
    else b_str(&m,"frame output failed; fresh owner required");
    b_term(&m);
    if(g_start_id>=0)reply_err(g_start_id,"state",msg);
    if(g_step_id>=0)reply_err(g_step_id,"state",msg);
    if(g_wait_id>=0)reply_err(g_wait_id,"state",msg);
    g_start_id=g_step_id=g_wait_id=-1;g_step_left=0;g_step_stream=0;
    return 1;
}

/* Contract section 6: a changed hook slot halts the run with a report. At
 * the gate, before the journal: push {"push":"integrity",...}, fail the
 * pending start/step/wait with code "integrity", then serve queries only.
 * Execution verbs are refused (handle). Never returns. */
void wire_integrity_halt(u32 addr, const u8 *exp, const u8 *obs, u32 n) {
    char he[40], ho[40], msg[160];
    buf_t b;
    b_init(&b, he, sizeof he); b_hex(&b, exp, n); b_term(&b);
    b_init(&b, ho, sizeof ho); b_hex(&b, obs, n); b_term(&b);
    slog("integrity: %x changed at gate %u: expected %s observed %s; halted", addr, (u32)g_gate.tick, he, ho);
    if (!g_wire_ok) { TerminateProcess(GetCurrentProcess(), 7); for (;;) {} }
    g_integrity_fault = 1;
    g_release = 0;
    b_init(&b, g_out, OBUF);
    b_str(&b, "{\"addr\":\"");
    b_hexu32(&b, addr);
    b_str(&b, "\",\"expected\":\"");
    b_str(&b, he);
    b_str(&b, "\",\"kind\":\"");
    b_str(&b, n == 4 ? "slot" : "code");
    b_str(&b, "\",\"observed\":\"");
    b_str(&b, ho);
    b_str(&b, "\",\"push\":\"integrity\",\"tick\":");
    b_u64(&b, g_gate.tick);
    b_ch(&b, '}');
    wire_send_line(&b);
    b_init(&b, msg, sizeof msg);
    b_str(&b, "hook integrity: ");
    b_hexu32(&b, addr);
    b_str(&b, " changed (expected ");
    b_str(&b, he);
    b_str(&b, ", observed ");
    b_str(&b, ho);
    b_str(&b, "); run halted");
    b_term(&b);
    if (g_start_id >= 0) reply_err(g_start_id, "integrity", msg);
    if (g_step_id >= 0) reply_err(g_step_id, "integrity", msg);
    if (g_wait_id >= 0) reply_err(g_wait_id, "integrity", msg);
    g_start_id = g_step_id = g_wait_id = -1;
    g_step_left = 0;
    g_step_stream = 0;
    for (;;) wire_service(1);
}
static int integrity_blocks(const char *verb) {
    return (g_integrity_fault || g_journal_fault) && (k_streq(verb, "start") || k_streq(verb, "step") || k_streq(verb, "wait") ||
                                 k_streq(verb, "input") || k_streq(verb, "os_event") || k_streq(verb, "digest") ||
                                 k_streq(verb, "restore"));
}

/* Never turn a bounded event overflow into an apparently valid tick. The
 * incomplete tick remains in the ring for diagnosis, but no successful tick
 * push/reply is emitted and no subsequent input is staged. */
static void journal_incomplete_refuse(void) {
    g_journal_fault = 1;
    g_release = 0;
    const char *msg = "bounded journal or step event report overflowed; tick provenance is incomplete; relaunch required";
    slog("journal: incomplete event provenance at gate %u (journal dropped %u, report dropped %u); halted",
         (u32)g_gate.tick, journal_event_dropped(), g_sev_lost);
    if (g_start_id >= 0) reply_err(g_start_id, "journal_incomplete", msg);
    if (g_step_id >= 0) reply_err(g_step_id, "journal_incomplete", msg);
    if (g_wait_id >= 0) reply_err(g_wait_id, "journal_incomplete", msg);
    g_start_id = g_step_id = g_wait_id = -1;
    g_step_left = 0;
    g_step_stream = 0;
    for (;;) wire_service(1);
}

static u32 input_os_reserve(int first, int close, int altf4) {
    /* An earlier !altf4@save may stay armed across neutral ticks until a
     * later save pumps messages. Reserve its four edges plus SC_CLOSE too,
     * independently of a new immediate/armed event on this request. */
    return ((first && close) || g_os_close ? 1u : 0u) +
           ((first && altf4) || g_os_altf4 ? 5u : 0u) +
           (SH->altf4_save ? 5u : 0u);
}

static void handle(char *line, u32 len, int phase) {
    jnode *req = json_parse(line, len);
    if (!req) { reply_err(-1, "bad_args", "unparseable request"); return; }
    i64 id = jint(jget(req, "id"), -1);
    const char *verb = jstr(jget(req, "verb"), "");
    jnode *args = jget(req, "args");
    buf_t b;
    if (!verb_allowed(verb)) { reply_err(id, "refused", "verb outside the locked tier"); return; }
    if(frame_fault_blocks(verb)) {
        char msg[256];buf_t m;b_init(&m,msg,sizeof msg);
        if(render_fault()[0])render_fault_message(&m);
        else b_str(&m,"frame output failed; fresh owner required");
        b_term(&m);
        reply_err(id,"state",msg);return;
    }
    if (integrity_blocks(verb)) { reply_err(id, "integrity", "hook integrity failed; run halted"); return; }
    if(restore_tx_blocks(verb)) {
        reply_err(id,"refused",g_restore_pending ? "restore external reconstruction pending" :
                  "a restore failed to verify: restore a recorded state or relaunch");
        return;
    }

    if (k_streq(verb, "hello")) { do_hello(id); return; }
    if (k_streq(verb, "tier.lock")) {
        const char *t = jstr(jget(args, "tier"), "lab");
        if (g_gate.started) { reply_err(id, "state", "tier.lock must come before start"); return; }
        if (!k_streq(t, "lab") && !k_streq(t, "tas")) { reply_err(id, "bad_args", "tier must be lab or tas"); return; }
        if (g_tier_locked && !k_streq(t, g_tier)) { reply_err(id, "refused", "tier already locked"); return; }
        memcpy(g_tier, t, strlen(t) + 1);
        g_tier_locked = 1;
        hud_lock(k_streq(g_tier, "tas"));
        reply_begin(&b, id);
        b_str(&b, "{\"allow\":[\"hello\",\"tier.lock\",\"clock\",\"clock.save_ms\",\"clock.save_ms_next\",\"os_event\",\"subscribe\",\"ring.attach\",\"capture\",\"frames\",\"frame\",\"hud\",\"hud.poll\",\"hud.bindings\",\"hud.stats\",\"start\",\"step\",\"input\",\"input.state\",\"wait\",\"digest\",\"mem.read\",\"semantic.read\",\"gate\",\"restore\",\"restore.admit\",\"verify\",\"stats\",\"bye\"");
        b_str(&b,",\"input.source\",\"input.physical.prepare\"");
        if (k_streq(t, "lab")) b_str(&b, ",\"mem.write\"");
        b_str(&b, "],\"tier\":");
        b_jstr(&b, g_tier);
        b_ch(&b, '}');
        reply_end(&b);
        return;
    }
    if (k_streq(verb, "clock")) {
        const char *mode = jstr(jget(args, "mode"), "virtual");
        if (!k_streq(mode, "virtual") && g_tier_locked && k_streq(g_tier, "tas")) { reply_err(id, "refused", "tas requires the virtual clock"); return; }
        if (!k_streq(mode, g_clk.mode ? "wall" : "virtual")) { reply_err(id, "state", "mode differs from the DllMain origin"); return; }
        i64 q = jint(jget(args, "qpf_hz"), (i64)g_clk.qpf);
        i64 u0 = jint(jget(args, "uptime0_ms"), (i64)g_clk.uptime0_ms);
        i64 ft = jint(jget(args, "initial_ft"), (i64)g_clk.initial_ft);
        if ((u64)q != g_clk.qpf || (u64)u0 != g_clk.uptime0_ms || (u64)ft != g_clk.initial_ft) {
            reply_err(id, "state", "clock origin must repeat the one DllMain installed (KPTAS_CLOCK)");
            return;
        }
        i64 hz = jint(jget(args, "hz"), g_clk.hz);
        if (hz < 1 || hz > 1000) { reply_err(id, "bad_args", "hz out of range"); return; }
        /* wording only; an older host sends none and keeps the default (frame) */
        g_precision_tick = k_streq(jstr(jget(args, "precision"), "frame"), "tick");
        if ((u32)hz != g_clk.hz) {
            if (!g_gate.started) g_clk.hz = (u32)hz;
            else {
                /* new segment from the next tick */
                u64 n1 = g_gate.tick + 1;
                u64 v1 = g_clk.seg_v + (n1 - g_clk.seg_tick) * 1000000ull / g_clk.hz;
                g_clk.seg_tick = n1;
                g_clk.seg_v = v1;
                g_clk.hz = (u32)hz;
            }
        }
        reply_begin(&b, id);
        b_str(&b, "{\"hooked\":");
        list_json(&b, g_hooks_list);
        b_str(&b, ",\"mode\":");
        b_jstr(&b, g_clk.mode ? "wall" : "virtual");
        b_str(&b, ",\"us_per_tick\":");
        b_u64(&b, 1000000ull / g_clk.hz);
        b_ch(&b, '}');
        reply_end(&b);
        return;
    }
    if (k_streq(verb, "clock.save_ms") || k_streq(verb, "clock.save_ms_next")) {
        /* inputs: journalled on the next tick, rewound by a restore (SH) */
        jnode *ms = jget(args, "ms");
        if (!ms) ms = jget(req, "ms");
        if (!ms || ms->type != J_NUM || ms->num < 0 || ms->num > 3600000) {
            reply_err(id, "bad_args", "ms: 0..3600000");
            return;
        }
        int once = k_streq(verb, "clock.save_ms_next");
        if (once) { SH->save_next_ms = (u32)ms->num; SH->save_next_set = 1; }
        else SH->save_ms = (u32)ms->num;
        stage_ev(once ? EV_SAVE_MS_NEXT : EV_SAVE_MS, (u32)ms->num);
        reply_begin(&b, id);
        b_str(&b, "{\"for_tick\":");
        b_u64(&b, g_gate.started ? g_gate.tick + 1 : 0);
        b_str(&b, ",\"ms\":");
        b_u64(&b, (u64)ms->num);
        b_ch(&b, '}');
        reply_end(&b);
        return;
    }
    if (k_streq(verb, "os_event")) {
        const char *ev = jstr(jget(args, "ev"), "");
        int altf4 = k_streq(ev, "altf4") ? 1 : k_streq(ev, "altf4@save") ? 2 : 0;
        if (!altf4 && !k_streq(ev, "close")) { reply_err(id, "unsupported", "os_event: close, altf4 or altf4@save"); return; }
        if (phase != 1) { reply_err(id, "state", "os_event runs at the gate after start"); return; }
        if (altf4) g_os_altf4 = altf4;
        else g_os_close = 1;
        reply_begin(&b, id);
        b_str(&b, "{\"for_tick\":");
        b_u64(&b, g_gate.tick + 1);
        b_str(&b, ",\"outcome\":\"alive\"}");
        reply_end(&b);
        return;
    }
    if (k_streq(verb, "log")) {
        /* diagnostics only: the environment block stays pinned */
        geo_set_log((int)jint(jget(args, "geo"), 0));
        if (jget(args, "trace")) {
            g_trace = (u32)jint(jget(args, "trace"), 0);
            g_trace_site = (u32)jint(jget(args, "site"), 0);
            g_trace_from = (u32)jint(jget(args, "from"), 0);
            g_trace_to = (u32)jint(jget(args, "to"), 0x7fffffff);
            if (jget(args, "xmode")) g_xmode = (u32)jint(jget(args, "xmode"), 3);
            xtrace_set((g_trace & TR_XCALL) != 0);
            slog("trace %x site %x ticks %u..%u", g_trace, g_trace_site, g_trace_from, g_trace_to);
        }
        if (jget(args, "xdump")) xtrace_dump(jstr(jget(args, "xdump"), ""));
        reply_begin(&b, id);
        b_str(&b, "{}");
        reply_end(&b);
        return;
    }
    if (k_streq(verb, "subscribe")) {
        jnode *t = jget(args, "topics");
        g_sub_tick = 0;
        if (t && t->type == J_ARR)
            for (jnode *c = t->child; c; c = c->next)
                if (k_streq(jstr(c, ""), "tick") || k_streq(jstr(c, ""), "journal")) g_sub_tick = 1;
        reply_begin(&b, id);
        b_str(&b, "{}");
        reply_end(&b);
        return;
    }
    if (k_streq(verb, "start")) {
        if (phase != 0 || g_gate.started) { reply_err(id, "state", "already started"); return; }
        if (k_streq(g_math_state, "failed")) { reply_err(id, "state", g_math_err); return; }
        g_gate.started = 1;
        g_gate.tick = 0;
        clock_set_tick(0);
        clock_tick_reset();
        journal_tick_reset();
        msg_tick_reset();
        g_client_count = g_server_count = g_us_count = 0;
        g_start_id = id;
        g_verbclass_acc = VC_GATE | VC_CLOCK;
        g_sev_n = g_sev_lost = 0;
        wire_tick_begin();
        g_release = 1;
        return;
    }
    if (k_streq(verb, "step") || k_streq(verb, "wait")) {
        if (phase != 1) { reply_err(id, "state", "step and wait run at the gate after start"); return; }
        jnode *cap_every_n = jget(args, "capture_every"), *cap_last_n = jget(args, "capture_last");
        jnode *cap_phase_n = jget(args, "capture_phase");
        i64 cap_every = cap_every_n ? (cap_every_n->type == J_NUM && !cap_every_n->str ? cap_every_n->num : 0) : 1;
        i64 cap_phase = cap_phase_n ? (cap_phase_n->type == J_NUM && !cap_phase_n->str ? cap_phase_n->num : -1) : 0;
        if (cap_every < 1 || cap_phase < 0 || cap_phase >= cap_every || (cap_last_n && cap_last_n->type != J_BOOL)) {
            reply_err(id, "bad_args", "capture_every is an integer >= 1, capture_phase an integer in "
                      "0..capture_every-1 and capture_last a boolean"); return;
        }
        /* Render skipping (hello caps.render_skip = 1): render_skip "off" | "present" | "draw"
         * (default "off") and preview_every N (default 1; 0 = no preview readback). Host state; see render.cpp.
         * A wrong type reads as RS_BAD_MODE or PREVIEW_BAD and is refused. */
        enum { RS_BAD_MODE = 99 };
        const i64 PREVIEW_BAD = -1, PREVIEW_MAX = 3600;
        jnode *rs_n = jget(args, "render_skip"), *pv_n = jget(args, "preview_every");
        const char *rs = rs_n ? jstr(rs_n, "?") : "off";
        u32 rs_mode = k_streq(rs, "off") ? RS_OFF : k_streq(rs, "present") ? RS_PRESENT
                    : k_streq(rs, "draw") ? RS_DRAW : (u32)RS_BAD_MODE;
        i64 pv = pv_n ? (pv_n->type == J_NUM && !pv_n->str ? pv_n->num : PREVIEW_BAD) : 1;
        if (rs_mode == (u32)RS_BAD_MODE || pv < 0 || pv > PREVIEW_MAX) {
            reply_err(id, "bad_args", "render_skip is \"off\", \"present\" or \"draw\" and preview_every an integer in 0..3600 (0: no preview)");
            return;
        }
        jnode *stream_n = jget(args, "stream");
        if (stream_n && (stream_n->type != J_BOOL || !k_streq(verb, "step"))) {
            reply_err(id, "bad_args", "stream is a boolean step argument"); return;
        }
        int stream = stream_n ? (int)stream_n->num : 0;
        if (stream && !step_stream_ready()) {
            reply_err(id, "unsupported", "a streamed step needs the complete file signal (hello caps.step_stream)");
            return;
        }
        const char *bad = "";
        int cl = 0, af = 0;
        if (!parse_msgs(args, &cl, &af, &bad)) { reply_err(id, "unsupported", bad); return; }
        input_t candidate;
        if (!parse_input(args, verb, &candidate, &bad)) { reply_err(id, "bad_args", bad); return; }
        if (g_physical_enabled) {
            const u64 ticket = (u64)jint(jget(args, "physical_ticket"), 0);
            jnode *physical_first = jget(args,"first");
            if (!k_streq(verb, "step") || jint(jget(args, "n"), 1) != 1 || cl ||
                af != (g_physical_prepared.altf4 ? 1 : 0) ||
                (physical_first && (physical_first->type != J_BOOL || !physical_first->num)) ||
                !g_physical_ticket_valid || ticket != g_physical_ticket ||
                g_physical_prepared.status != telos::PhysicalInputCollector::Status::ready ||
                g_physical.prepare().revision != g_physical_prepared.revision ||
                g_physical.status() != telos::PhysicalInputCollector::Status::ready ||
                memcmp(candidate.keys, g_physical_prepared.input.keys, sizeof candidate.keys) ||
                candidate.buttons != g_physical_prepared.input.buttons ||
                candidate.has_abs != g_physical_prepared.input.has_abs ||
                candidate.abs_x != g_physical_prepared.input.abs_x ||
                candidate.abs_y != g_physical_prepared.input.abs_y ||
                candidate.rel_x || candidate.rel_y ||
                candidate.wheel != g_physical_prepared.input.wheel) {
                reply_err(id,"refused","physical input requires its exact prepared one-frame ticket");return;
            }
        } else if (jget(args,"physical_ticket")) {
            reply_err(id,"refused","physical ticket outside physical source");return;
        }
        int first = jget(args, "first") ? (jget(args, "first")->type == J_BOOL ? (int)jget(args, "first")->num : 1) : 1;
        if (!di_can_stage(&candidate, first, input_os_reserve(first, cl, af))) {
            reply_err(id, "refused", "input queue capacity: no input staged"); return;
        }
        g_input = candidate;
        g_input_close = cl;
        g_input_altf4 = af;
        set_note(args);
        g_first_tick = jget(args, "first") ? (jget(args, "first")->type == J_BOOL ? (int)jget(args, "first")->num : 1) : 1;
        g_sev_n = g_sev_lost = 0;
        g_verbclass_acc = 0;
        if (k_streq(verb, "step")) {
            i64 n = jint(jget(args, "n"), 1);
            if (n < 0) { reply_err(id, "bad_args", "n < 0"); return; }
            if (n == 0) {
                reply_begin(&b, id);
                step_report(&b, -1, "alive");
                reply_end(&b);
                return;
            }
            g_step_left = (u64)n;
            g_step_id = id;
            g_step_stream = stream;
        } else {
            g_step_stream = 0;
            const char *ev = jstr(jget(args, "event"), "");
            u32 l = (u32)strlen(ev);
            if (l >= sizeof g_wait_event) l = sizeof g_wait_event - 1;
            memcpy(g_wait_event, ev, l);
            g_wait_event[l] = 0;
            g_wait_deadline = g_gate.tick + (u64)jint(jget(args, "timeout"), 1800);
            int k = event_kind(g_wait_event);
            g_wait_kind = k < 0 ? EV_KINDS : (u32)k;
            g_wait_base = k < 0 ? 0 : g_ev_count[k];
            g_wait_id = id;
        }
        g_cap_every = (u64)cap_every;
        g_cap_phase = (u64)cap_phase;
        g_cap_last = cap_last_n ? (int)cap_last_n->num : 1;
        g_rs_mode = rs_mode;
        g_rs_preview = (u32)pv;
        g_release = 1;
        return;
    }
    if (k_streq(verb, "input")) {
        if (g_physical_enabled) { reply_err(id,"refused","authored input while physical source is active");return; }
        const char *bad = "";
        int cl = 0, af = 0;
        if (!parse_msgs(args, &cl, &af, &bad)) { reply_err(id, "unsupported", bad); return; }
        input_t candidate;
        if (!parse_input(args, verb, &candidate, &bad)) { reply_err(id, "bad_args", bad); return; }
        int first = jget(args, "first") ? (jget(args, "first")->type == J_BOOL ? (int)jget(args, "first")->num : 1) : 1;
        if (!di_can_stage(&candidate, first, input_os_reserve(first, cl, af))) {
            reply_err(id, "refused", "input queue capacity: no input staged"); return;
        }
        g_input = candidate;
        g_input_close = cl;
        g_input_altf4 = af;
        set_note(args);
        reply_begin(&b, id);
        b_str(&b, "{\"for_tick\":");
        b_u64(&b, g_gate.tick + 1);
        b_ch(&b, '}');
        reply_end(&b);
        return;
    }
    if (k_streq(verb,"input.source") || k_streq(verb,"input.physical.prepare")) {
        if (phase != 1 || g_restore_pending || g_poisoned || g_step_id >= 0 || g_wait_id >= 0) {
            reply_err(id,"state","input source requires a healthy held gate");return;
        }
        i64 requested_tick = -1;
        const int source_transition = k_streq(verb,"input.source");
        if (!input_source_args_valid(args, source_transition) ||
            !input_integer(jget(args,"at_tick"), 0, INT64_MAX, &requested_tick)) {
            reply_err(id,"bad_args","input source requires an integer at_tick and only verb-specific fields");return;
        }
        if (requested_tick != (i64)g_gate.tick) {
            reply_err(id,"refused","input source gate changed");return;
        }
        msg_host_events();
        wire_physical_observe(NULL);
        if (source_transition) {
            jnode *engaged = jget(args,"engaged");
            if (!engaged || engaged->type != J_BOOL || !g_physical_window || g_os_close || g_os_altf4) {
                reply_err(id,"bad_args","engaged boolean and a live game window required");return;
            }
            const int physical = !engaged->num;
            if (physical && g_physical.status() == telos::PhysicalInputCollector::Status::overflow) {
                reply_err(id,"refused","physical event overflow; engage before recovering");return;
            }
            g_physical.set_enabled(physical != 0);
            g_physical_enabled = physical;
            g_physical_ticket_valid = 0;
            memset(&g_input,0,sizeof g_input);
            g_input_close = g_input_altf4 = g_first_tick = 0;
            memcpy(g_req_note,".",2);
            reply_begin(&b,id);
            b_str(&b,"{\"schema\":\"kptas-input-source/1\",\"engaged\":");
            b_str(&b,physical?"false":"true");
            b_str(&b,",\"tick\":");b_u64(&b,g_gate.tick);
            b_str(&b,",\"input_source\":");b_jstr(&b,physical?"physical":"authored");
            b_ch(&b,'}');reply_end(&b);return;
        }
        if (!g_physical_enabled || g_physical.status() != telos::PhysicalInputCollector::Status::ready) {
            reply_err(id,"refused","physical input unavailable or overflowed; engage required");return;
        }
        g_physical_prepared = g_physical.prepare();
        if (++g_physical_ticket >= (1ull<<53)) {
            g_physical_ticket_valid = 0;
            reply_err(id,"refused","physical ticket space exhausted; fresh owner required");return;
        }
        g_physical_ticket_valid = 1;
        const input_t *in = &g_physical_prepared.input;
        reply_begin(&b,id);
        b_str(&b,"{\"schema\":\"kptas-physical-input/1\",\"tick\":");b_u64(&b,g_gate.tick);
        b_str(&b,",\"ticket\":");b_u64(&b,g_physical_ticket);
        b_str(&b,",\"keys\":[");int comma=0;
        for(int key=1;key<256;key++)if(in->keys[key>>3]&(1u<<(key&7))){if(comma++)b_ch(&b,',');b_u64(&b,key);}
        b_str(&b,"],\"buttons\":[");comma=0;
        static const char *names[3]={"lmb","rmb","mmb"};
        for(int button=0;button<3;button++)if(in->buttons&(1u<<button)){if(comma++)b_ch(&b,',');b_jstr(&b,names[button]);}
        b_str(&b,"],\"wheel\":");b_i64(&b,in->wheel);
        if(g_physical_prepared.altf4)b_str(&b,",\"osev\":\"altf4\"");
        if(in->has_abs){b_str(&b,",\"mouse\":{\"abs\":[");b_i64(&b,in->abs_x);b_ch(&b,',');b_i64(&b,in->abs_y);b_str(&b,"]}");}
        b_ch(&b,'}');reply_end(&b);return;
    }
    if (k_streq(verb, "ring.attach")) {
        const char *path = jstr(jget(args, "path"), "");
        i64 mb = jint(jget(args, "mb"), 0);
        static WCHAR wpath[520];
        if (!*path || MultiByteToWideChar(CP_ACP, 0, path, -1, wpath, 520) <= 0) { reply_err(id, "bad_args", "path"); return; }
        if (!kst_ring_attach(wpath, (u32)mb)) { reply_err(id, "internal", "ring attach failed"); return; }
        reply_begin(&b, id);
        b_str(&b, "{\"capacity\":");
        b_u64(&b, K->ring_cap);
        b_str(&b, ",\"write_pos\":");
        b_u64(&b, K->ring_wpos);
        b_ch(&b, '}');
        reply_end(&b);
        return;
    }
    if (k_streq(verb, "capture")) {
        if (g_gate.started) { reply_err(id, "state", "capture must be set before start"); return; }
        jnode *transport = jget(args, "gl_transport");
        if(transport && !k_streq(jstr(transport,""),"kptas-gl-spool/1")) {
            reply_err(id,"bad_args","unsupported GL capture transport");return;
        }
        glsync_spool_configure(transport!=0);
        jnode *en = jget(args, "enabled");
        g_capture_on = en ? (int)en->num : 1;
        reply_begin(&b, id);
        const char *names[] = {"game_arena", "image_data", "harness_game"};
        const u32 classes[] = {KST_GAME_ARENA, KST_IMAGE_DATA, KST_HARNESS_GAME};
        b_str(&b, "{\"classes\":[");
        for (u32 c = 0; c < 3; c++) {
            if (c) b_ch(&b, ',');
            b_str(&b, "{\"name\":"); b_jstr(&b, names[c]); b_str(&b, ",\"ranges\":[");
            u32 n = 0;
            for (u32 i = 0; i < K->nreg; i++) {
                region *r = &K->reg[i];
                if (r->mclass != classes[c]) continue;
                if (n++) b_ch(&b, ',');
                b_str(&b, "[\""); b_hexu32(&b, r->base);
                b_str(&b, "\",\""); b_hexu32(&b, r->base + r->size); b_str(&b, "\"]");
            }
            b_str(&b, "]}");
        }
        b_str(&b, "],\"enabled\":");
        b_str(&b, g_capture_on ? "true" : "false");
        if(transport)b_str(&b, ",\"gl_transport\":\"kptas-gl-spool/1\"");
        b_str(&b, ",\"ring\":");
        b_str(&b, kst_ring_attached() ? "true" : "false");
        b_ch(&b, '}');
        reply_end(&b);
        return;
    }
    if (k_streq(verb, "input.state")) {
        if (phase != 1 || !g_gate.started || !SH) {
            reply_err(id, "state", "input.state requires a held gate after start"); return;
        }
        if (g_poisoned) {
            reply_err(id, "state", "input.state unavailable until restore completes"); return;
        }
        reply_begin(&b, id);
        b_str(&b, "{\"tick\":"); b_u64(&b, g_gate.tick);
        b_str(&b, ",\"source\":\"virtual_devices\",\"sampled_at_gate\":true,\"keys\":[");
        u32 count = 0;
        for (u32 k = 0; k < 256; k++) if (SH->keys[k]) {
            if (count++) b_ch(&b, ','); b_u64(&b, k);
        }
        b_str(&b, "],\"buttons\":["); count = 0;
        static const char *names[3] = {"lmb", "rmb", "mmb"};
        for (u32 k = 0; k < 3; k++) if (SH->btn[k]) {
            if (count++) b_ch(&b, ','); b_jstr(&b, names[k]);
        }
        b_str(&b, "],\"cursor\":["); b_i64(&b, SH->vcur_x); b_ch(&b, ','); b_i64(&b, SH->vcur_y);
        b_str(&b, "],\"altf4_save_armed\":"); b_str(&b, SH->altf4_save ? "true" : "false");
        b_ch(&b, '}'); reply_end(&b); return;
    }
    if (k_streq(verb, "frame")) {
        u32 w=0,h=0;u8 digest[32];
        const char *why=frames_snapshot(jstr(jget(args,"path"),""),&w,&h,digest);
        if(why){reply_err(id,"state",why);return;}
        reply_begin(&b,id);b_str(&b,"{\"w\":");b_u64(&b,w);b_str(&b,",\"h\":");b_u64(&b,h);
        b_str(&b,",\"format\":\"bgra-bottom-up\",\"sha256\":\"");b_hex(&b,digest,32);
        b_ch(&b,'"');frames_snapshot_meta(&b);b_ch(&b,'}');reply_end(&b);return;
    }
    if (k_streq(verb, "hud.stats")) {
        reply_begin(&b,id);hud_stats_json(&b);reply_end(&b);return;
    }
    if (k_streq(verb, "hud.bindings")) {
        if(phase!=1||g_restore_pending||g_poisoned||g_step_id>=0||g_wait_id>=0||g_physical_enabled){
            reply_err(id,"state","tool bindings require a healthy stopped gate");return;
        }
        jnode *array=jget(args,"bindings");hud_binding_t bindings[16]{};u32 count=0;
        if(!array||array->type!=J_ARR){reply_err(id,"bad_args","bindings array required");return;}
        for(jnode *item=array->child;item;item=item->next){
            jnode *vk=jget(item,"vk"),*mods=jget(item,"modifiers"),*action=jget(item,"action");
            if(count==16||!vk||!mods||!action||vk->type!=J_NUM||mods->type!=J_NUM||action->type!=J_NUM||
               vk->num<1||vk->num>255||mods->num<0||mods->num>15||action->num<0||action->num>12){
                reply_err(id,"bad_args","invalid tool binding");return;
            }
            bindings[count++]={(u32)vk->num,(u32)mods->num,(u32)action->num};
        }
        if(!hud_bindings_configure(bindings,count)){reply_err(id,"bad_args","conflicting tool bindings");return;}
        g_physical_ticket_valid=0;
        reply_begin(&b,id);b_str(&b,"{\"schema\":\"kptas-tool-bindings/1\",\"count\":");
        b_u64(&b,count);b_str(&b,",\"tick\":");b_u64(&b,g_gate.tick);b_ch(&b,'}');reply_end(&b);return;
    }
    if (k_streq(verb, "hud.poll")) {
        if (!g_gate.started || g_step_id>=0 || g_wait_id>=0) {
            reply_err(id,"state","HUD controls require a stopped gate");return;
        }
        reply_begin(&b,id);b_str(&b,"{\"action\":");b_jstr(&b,hud_control_poll());
        b_ch(&b,'}');reply_end(&b);return;
    }
    if (k_streq(verb, "hud")) {
        if (!g_tier_locked) { reply_err(id, "state", "lock tier before configuring HUD"); return; }
        jnode *on=jget(args,"on"), *mark=jget(args,"watermark");
        if (!hud_config(!(on&&on->type==J_BOOL&&!on->num),
                        !(mark&&mark->type==J_BOOL&&!mark->num),
                        jint(jget(args,"offset"),0), jstr(jget(args,"text"),""))) {
            reply_err(id,"refused","TAS watermark cannot be disabled");return;
        }
        hud_refresh();
        reply_begin(&b,id);b_str(&b,"{\"enabled\":");b_str(&b,hud_on()?"true":"false");
        b_str(&b,",\"fidelity\":\"unverified\"}");reply_end(&b);return;
    }
    if (k_streq(verb, "frames")) {
        /* output only: any phase, any tier; it adds no verbclass */
        jnode *en = jget(args, "enabled");
        if (en && en->type == J_BOOL && !en->num) frames_off();
        else {
            const char *why = "";
            if (!frames_attach(jstr(jget(args, "path"), ""), (u32)jint(jget(args, "slots"), 0),
                               (u32)jint(jget(args, "slot_bytes"), 0), &why)) {
                reply_err(id, "bad_args", why);
                return;
            }
        }
        reply_begin(&b, id);
        frames_stats_json(&b);
        reply_end(&b);
        return;
    }
    if (k_streq(verb, "restore.admit")) {
        /* the admitted set: the source bundle's recorded roots, strictly ascending, once per process */
        u32 n = 0;
        u8 sha[32];
        const char *e = state_admit(jstr(jget(args, "path"), ""), jint(jget(args, "n"), -1),
                                    jstr(jget(args, "sha256"), ""), &n, sha);
        if (e) { reply_err(id, k_strstr(e, "once") ? "state" : "bad_args", e); return; }
        reply_begin(&b, id);
        b_str(&b, "{\"n\":");
        b_u64(&b, n);
        b_str(&b, ",\"sha256\":\"");
        b_hex(&b, sha, 32);
        b_str(&b, "\"}");
        reply_end(&b);
        return;
    }
    if (k_streq(verb, "restore")) {
        if (phase != 1) { reply_err(id, "state", "restore runs at the gate after start"); return; }
        if (!g_capture_on) { reply_err(id, "state", "capture is off"); return; }
        i64 t = jint(jget(args, "tick"), -1);
        const char *rh = jstr(jget(args, "root"), "");
        const char *path = jstr(jget(args, "path"), "");
        jnode *chk = jget(args, "check");
        int check = chk && chk->type == J_BOOL && chk->num;
        u8 root[32];
        if (t < 0 || strlen(rh) != 64 || (!*path && !check)) { reply_err(id, "bad_args", "tick, root (64 hex) and path"); return; }
        for (int i = 0; i < 32; i++) {
            u32 v = 0;
            for (int j = 0; j < 2; j++) {
                char c = rh[i * 2 + j];
                v = (v << 4) | (u32)(c >= 'a' ? c - 'a' + 10 : c >= 'A' ? c - 'A' + 10 : c - '0');
            }
            root[i] = (u8)v;
        }
        /* tas (and an unlocked session): only a root this process captured or an admitted one */
        int strict = !(g_tier_locked && k_streq(g_tier, "lab"));
        const char *by = "lab";
        int allowed = !strict || state_root_allowed(root, &by);
        if (check) {
            reply_begin(&b, id);
            b_str(&b, "{\"admitted\":");
            b_str(&b, allowed ? "true" : "false");
            if (allowed) { b_str(&b, ",\"by\":"); b_jstr(&b, by); }
            b_ch(&b, '}');
            reply_end(&b);
            return;
        }
        if (!allowed) {
            char r16[17];
            memcpy(r16, rh, 16);
            r16[16] = 0;
            slog("restore %u -> %u refused: root %s.. was neither captured by this process nor admitted",
                 (u32)g_gate.tick, (u32)t, r16);
            reply_err(id, "refused", "tas restores only a root this process captured or an admitted root");
            return;
        }
        if (!state_map_current()) {
            /* capture cadence: memory moved on since the map; a write set would be wrong */
            reply_err(id, "refused", "the held gate was not captured (capture cadence); the next step "
                      "that ends with capture_last (the default) captures its gate, then restore");
            return;
        }
        buf_t rb;
        static char rbuf[4096];
        b_init(&rb, rbuf, sizeof rbuf);
        restore_tx_begin();
        int ok = state_restore((u64)t, root, path, &rb);
        restore_tx_memory(ok,(u64)t,root,by);
        b_str(&rb,",\"memory_verified\":");b_str(&rb,ok>0?"true":"false");
        b_str(&rb,",\"verified\":false,\"external_pending\":");b_str(&rb,g_restore_pending?"true":"false");
        b_str(&rb,",\"poisoned\":true");
        if(ok>0){b_str(&rb,",\"by\":");b_jstr(&rb,by);}
        b_ch(&rb, '}');
        b_term(&rb);
        reply_begin(&b, id);
        b_raw(&b, rb.p, rb.n);
        reply_end(&b);
        return;
    }
    if (k_streq(verb, "gl.need") || k_streq(verb, "gl.apply")) {
        /* after a restore: the GL objects and context state the driver lacks, then their rebuild */
        if (phase != 1) { reply_err(id, "state", "gl verbs run at the gate after start"); return; }
        if (!g_capture_on) { reply_err(id, "state", "capture is off"); return; }
        static char gbuf[192 * 1024];
        buf_t gb;
        b_init(&gb, gbuf, sizeof gbuf);
        if (k_streq(verb, "gl.need")) glsync_need(&gb);
        else {
            if(!g_restore_pending) { reply_err(id,"state","gl.apply requires a verified memory restore stage");return; }
            int applied=glsync_apply(jstr(jget(args,"path"),""),&gb);
            int complete=restore_tx_finish(applied&&!gb.overflow);
            if(applied&&!complete)b_str(&gb,",\"error\":\"post-GL memory audit/root mismatch\"");
            b_str(&gb,",\"ok\":");b_str(&gb,complete?"true":"false");
            b_str(&gb,",\"verified\":");b_str(&gb,complete?"true":"false");
            b_str(&gb,",\"external_pending\":");b_str(&gb,g_restore_pending?"true":"false");
            b_str(&gb,",\"poisoned\":");b_str(&gb,g_poisoned?"true":"false");b_ch(&gb,'}');
        }
        if (gb.overflow) { reply_err(id, "internal", "gl reply too large"); return; }
        reply_begin(&b, id);
        b_raw(&b, gb.p, gb.n);
        reply_end(&b);
        return;
    }
    if (k_streq(verb, "verify") || k_streq(verb, "stats")) {
        reply_begin(&b, id);
        state_audit_json(&b,g_restore_pending);
        reply_end(&b);
        return;
    }
    if (k_streq(verb, "digest")) { do_digest(id, args); return; }
    if (k_streq(verb, "mem.read")) { do_memread(id, args); return; }
    if (k_streq(verb, "mem.write")) { do_memwrite(id, args); return; }
    if (k_streq(verb, "semantic.read")) {
        buf_t j;
        u8 d[16];
        b_init(&j, g_jent, JBUF);
        journal_tick(&j, d);
        b_term(&j);
        reply_begin(&b, id);
        b_str(&b, "{\"values\":");
        b_raw(&b, j.p, j.n);
        b_ch(&b, '}');
        reply_end(&b);
        g_verbclass_tick |= VC_READ;
        return;
    }
    if (k_streq(verb, "gate")) {
        reply_begin(&b, id);
        b_str(&b, "{\"held\":");
        b_str(&b, phase == 1 ? "true" : "false");
        b_str(&b, ",\"tick\":");
        b_u64(&b, g_gate.tick);
        b_ch(&b, '}');
        reply_end(&b);
        return;
    }
    if (k_streq(verb, "bye") || k_streq(verb, "quit")) {
        i64 code = jint(jget(args, "exit"), 0);
        reply_begin(&b, id);
        b_str(&b, "{}");
        reply_end(&b);
        FILETIME c0, e0, k0, u0;
        u32 kms = 0, ums = 0;
        if (GetProcessTimes(GetCurrentProcess(), &c0, &e0, &k0, &u0)) {
            kms = (u32)((((u64)k0.dwHighDateTime << 32) | k0.dwLowDateTime) / 10000ull);
            ums = (u32)((((u64)u0.dwHighDateTime << 32) | u0.dwLowDateTime) / 10000ull);
        }
        slog("bye: terminating with %d at tick %u (process cpu: user %u ms, kernel %u ms)", (int)code,
             (u32)g_gate.tick, ums, kms);
        shutdown(g_sock, SD_SEND);
        TerminateProcess(GetCurrentProcess(), (UINT)code);
        return;
    }
    reply_err(id, "unsupported", "verb not implemented in M1");
}

int wire_service(int phase) {
    if (phase == 0 && !g_wire_ok) {
        if (!wire_connect()) {
            slog("wire: no channel; running free (gate releases immediately)");
            return 1;
        }
    }
    for (;;) {
        char *line;
        u32 used;
        int r = recv_line(&line, &used, 10);
        if (r < 0) {
            slog("wire: peer closed; terminating");
            TerminateProcess(GetCurrentProcess(), 2);
            return 1;
        }
        if (r == 0) {
            if (phase == 1) msg_gate_pump();
            continue;
        }
        handle(line, used - 1, phase);
        consume(used);
        if (g_release) {
            g_release = 0;
            return 1;
        }
    }
}

/* Capture cadence: whether this gate is captured. Decided before the capture,
 * from what decides whether the gate is held below. */
static int gate_capture_due(void) {
    int ends;
    if (g_start_id >= 0) return 1;
    if (g_step_id >= 0) ends = g_step_left == 1;
    else if (g_wait_id >= 0) ends = event_fired() || g_gate.tick >= g_wait_deadline;
    else return 1;
    if (ends && g_cap_last) return 1;
    return g_cap_every <= 1 || (g_gate.tick + g_cap_phase) % g_cap_every == 0;
}

/* The render-due rule (render skipping; render.cpp, frames.cpp, hud.cpp and
 * gate.cpp refer here). Frame n, drawn during tick n, is render-due when any
 * of these hold:
 *   - it is a preview tick: preview_every > 0 and n % preview_every == 0;
 *   - the start gate, or no command runs;
 *   - a step runs and frame n is its last, or the one before a captured last;
 *   - a wait runs whose end is captured (its end is not known ahead);
 *   - gate n or gate n+1 is on the capture grid ((n + phase) % every is 0 or
 *     every - 1), or capture_every is 1.
 * Only these inputs can make a frame skippable. g_step_left during tick n is
 * what gate n reads (it drops after the gate), and nothing here changes
 * inside a tick, so a replay with the same arguments decides the same way. A
 * grid that the next command changes is not foreseen: the host keeps
 * capture_every and capture_phase. */
#define STEP_LAST_FRAME 1u          /* g_step_left during a step's last frame */
#define STEP_BEFORE_LAST_FRAME 2u   /* g_step_left during the frame before it */
int wire_render_due(u64 tick) {
    if (render_preview_due(tick)) return 1;
    if (g_start_id >= 0) return 1;
    if (g_step_id >= 0) {
        if (g_step_left <= STEP_LAST_FRAME || (g_cap_last && g_step_left == STEP_BEFORE_LAST_FRAME)) return 1;
    } else if (g_wait_id >= 0) {
        if (g_cap_last) return 1;
    } else return 1;
    if (g_cap_every <= 1) return 1;
    u64 p = (tick + g_cap_phase) % g_cap_every;
    return p == 0 || p == g_cap_every - 1;
}

/* Frame n is the one a held gate shows (the last of a step, any frame of a
 * wait, the start or a gate with no command): the preview reads it too. */
int wire_frame_held(void) {
    if (g_start_id >= 0) return 1;
    if (g_step_id >= 0) return g_step_left <= STEP_LAST_FRAME;
    return 1;
}

/* Called at each gate on the harness fiber: close tick n, answer, serve, stage tick n+1. */
static LARGE_INTEGER g_qpf_real, g_t_release;

void wire_gate_hook(void) {
    buf_t j;
    LARGE_INTEGER t0, t1;
    if (!g_qpf_real.QuadPart) QueryPerformanceFrequency(&g_qpf_real);
    QueryPerformanceCounter(&t0);
    u32 run_us = g_t_release.QuadPart ? (u32)((t0.QuadPart - g_t_release.QuadPart) * 1000000 / g_qpf_real.QuadPart) : 0;
    /* This gate's load event belongs to the tick being serialized. */
    if (g_load_done_count != g_last_load_done) { ev_add(EV_LOAD_DONE, 0); g_last_load_done = g_load_done_count; }
    /* The step event list also carries non-journalled process/load events.
     * Preserve their overflow in the canonical ring journal before hashing it. */
    if (g_sev_lost) journal_event_drop_note(g_sev_lost);
    b_init(&j, g_jent, JBUF);
    journal_tick(&j, g_last_jdig);
    QueryPerformanceCounter(&t1);
    u32 jr_us = (u32)((t1.QuadPart - t0.QuadPart) * 1000000 / g_qpf_real.QuadPart);
    b_term(&j);
    int captured = gate_capture_due();
    render_gate(captured);
    if (render_fault()[0]) {
        /* A render-skip fault (render.cpp): refuse before the capture, whose
         * root could differ from a run without skipping. No tick push either;
         * queries and orderly close remain available. */
        frame_fault_refuse();
        for (;;) wire_service(1);
    }
    if (captured) state_capture(g_gate.tick, j.p, j.n);
    else state_skip(g_gate.tick, j.p, j.n);
    if(frame_fault_refuse()) {
        /* No tick push, successful action reply, input staging or gate advance.
         * Queries and orderly close remain available while connected. */
        for(;;)wire_service(1);
    }
    if (j.overflow || journal_event_dropped() || g_sev_lost) journal_incomplete_refuse();
    g_verbclass_acc |= g_verbclass_tick;

    if (g_wire_ok && g_sub_tick) {
        buf_t b;
        b_init(&b, g_out, OBUF);
        b_str(&b, "{\"push\":\"tick\",\"tick\":");
        b_u64(&b, g_gate.tick);
        b_str(&b, ",\"ext\":");
        b_u64(&b, g_msg_ext_n);
        b_str(&b, ",\"run_us\":");
        b_u64(&b, run_us);
        b_str(&b, ",\"journal_us\":");
        b_u64(&b, jr_us);
        if (g_rs_mode != RS_OFF || g_rs_preview != 1) {
            b_str(&b, ",\"render\":");
            render_stats_json(&b);
        }
        if (g_guard_first_site) {
            b_str(&b, ",\"trip_site\":\"");
            b_hexu32(&b, g_guard_first_site);
            b_ch(&b, '"');
        }
        if (g_capture_on && g_root_valid && captured) {
            b_str(&b, ",\"root\":\"");
            b_hex(&b, g_root, 32);
            b_str(&b, "\",\"cap\":");
            state_stats_json(&b);
        } else if (g_capture_on && g_root_valid) {
            b_str(&b, ",\"captured\":false,\"skip\":");
            state_skip_stats_json(&b);
        }
        b_str(&b, ",\"entry\":");
        b_raw(&b, j.p, j.n);
        b_ch(&b, '}');
        wire_send_line(&b);
    }
    int hold = 1;
    /* file-mutating calls since the gate before (the tick, and the hold before it) */
    const u32 touch = g_file_touch;
    const int touched = touch != g_stream_touch;
    g_stream_touch = touch;
    if (g_start_id >= 0) {
        buf_t b;
        reply_begin(&b, g_start_id);
        step_report(&b, -1, "alive");
        reply_end(&b);
        g_start_id = -1;
    } else if (g_step_id >= 0) {
        const int last = g_step_left == 1;
        const int stop = !last && g_step_stream && touched;
        if (g_step_stream) {
            buf_t b;
            b_init(&b, g_out, OBUF);
            b_str(&b, "{\"push\":\"step.tick\",\"id\":");
            b_i64(&b, g_step_id);
            b_str(&b, ",\"files\":");
            b_str(&b, touched ? "true" : "false");
            b_str(&b, ",\"held\":");
            b_str(&b, (last || stop) ? "true" : "false");
            gl_names_json(&b);
            b_str(&b, ",\"report\":");
            step_report(&b, -1, "alive");
            b_ch(&b, '}');
            wire_send_line(&b);
        }
        if (last || stop) {
            buf_t b;
            reply_begin(&b, g_step_id);
            step_report(&b, -1, "alive");
            if (stop && !b.overflow && b.n && b.p[b.n - 1] == '}') {
                b.n--;                                   /* reopen the report object */
                b_str(&b, ",\"left\":");
                b_u64(&b, g_step_left - 1);
                b_str(&b, ",\"stopped\":\"files\"}");
            }
            reply_end(&b);
            g_step_id = -1;
            g_step_left = 0;
            g_step_stream = 0;
        } else {
            g_step_left--;
            hold = 0;
            /* the next push carries the next tick's events and verbclass only */
            if (g_step_stream) { g_sev_n = g_sev_lost = 0; g_verbclass_acc = 0; }
        }
    } else if (g_wait_id >= 0) {
        int fired = event_fired();
        if (fired || g_gate.tick >= g_wait_deadline) {
            buf_t b;
            reply_begin(&b, g_wait_id);
            step_report(&b, fired ? 1 : 0, "alive");
            reply_end(&b);
            g_wait_id = -1;
        } else hold = 0;
    }
    g_verbclass_tick = 0;
    if (!g_wire_ok) hold = 0;
    if (hold) wire_service(1);

    /* A consumer can fail after the report or while this gate is held.
     * Poll again before any next-tick input mutation or release. */
    if(frame_fault_refuse())for(;;)wire_service(1);

    /* stage tick n+1 */
    u32 ms = (u32)((g_clk.uptime0_ms * 1000ull + g_clk.seg_v + (g_gate.tick + 1 - g_clk.seg_tick) * 1000000ull / g_clk.hz) / 1000ull);
    while (!di_can_stage(&g_input, g_first_tick,
                         input_os_reserve(g_first_tick, g_input_close, g_input_altf4))) {
        /* A queued OS event or a later tick of a long request can change
         * capacity after admission. Refuse at this same gate, without partial
         * input or queue reset, and require a new caller decision. */
        const char *capmsg = g_precision_tick ? "input queue capacity: stopped before next tick"
                                              : "input queue capacity: stopped before next frame";
        if (g_step_id >= 0) reply_err(g_step_id, "refused", capmsg);
        if (g_wait_id >= 0) reply_err(g_wait_id, "refused", capmsg);
        g_step_id = g_wait_id = -1;
        g_step_stream = 0;
        wire_service(1);
    }
    g_staged_valid = 0;
    g_staged_first = g_first_tick;
    g_staged_os_events = (((g_first_tick && g_input_close) || g_os_close) ? 1u : 0u) |
                         (((g_first_tick && g_input_altf4 == 1) || g_os_altf4 == 1) ? 2u : 0u) |
                         (((g_first_tick && g_input_altf4 == 2) || g_os_altf4 == 2) ? 4u : 0u);
    if (!di_stage(&g_input, g_first_tick, ms, g_input_hash)) {
        /* Admission and staging share the gate thread. If the second check ever
         * disagrees, keep the process fenced instead of releasing an unrecorded
         * or partially staged input. */
        g_poisoned = 1;
        for (;;) wire_service(1);
    }
    g_staged_input = g_input;
    memcpy(g_staged_note, g_req_note, sizeof g_staged_note);
    memcpy(g_staged_hash, g_input_hash, sizeof g_staged_hash);
    g_staged_valid = 1;
    if (g_physical_enabled) {
        if (!g_physical_ticket_valid ||
            g_physical.commit(g_physical_prepared) != telos::PhysicalInputCollector::Status::ready) {
            // This cannot occur without a source/focus transition inside the
            // gate transaction. Never release a partially staged candidate.
            g_poisoned = 1;
            for(;;)wire_service(1);
        }
        g_physical_ticket_valid = 0;
    }
    /* OS event close: WM_CLOSE to the game window at the start of tick n+1,
     * through the staged queue so it reaches the game's WndProc */
    if ((g_first_tick && g_input_close) || g_os_close) {
        msg_synth_push(WM_CLOSE, 0, 0);
        SH->close_next = 1;
        g_os_close = 0;
    }
    /* OS event altf4: the keyboard messages a real Alt+F4 sends, pressed and
     * released within tick n+1: WM_SYSKEYDOWN VK_MENU, WM_SYSKEYDOWN VK_F4
     * (lParam bit 29: Alt is down), WM_SYSKEYUP VK_F4, then WM_KEYUP VK_MENU
     * (Alt released after another key is a plain key-up). The game's
     * WndProc and DefWindowProc take it from there (msg.c). */
    if ((g_first_tick && g_input_altf4 == 1) || g_os_altf4 == 1) {
        msg_altf4_keys();
        SH->altf4_next = 1;
    }
    /* OS event altf4@save: the same keys, pressed while the next save is
     * written. They wait for that save's first message pump (msg.c). */
    if ((g_first_tick && g_input_altf4 == 2) || g_os_altf4 == 2) {
        SH->altf4_save = 1;
        slog("altf4@save: armed for the first save from tick %u", (u32)g_gate.tick + 1);
    }
    g_os_altf4 = 0;
    g_first_tick = 0;
    memcpy(g_note, g_req_note, sizeof g_note);
    g_verbclass_tick = VC_GATE | VC_CLOCK | VC_INPUT | g_vc_next;
    g_vc_next = 0;
    QueryPerformanceCounter(&g_t_release);
}

/* The game is exiting (ExitProcess on the main slot, or the CRT entry
 * returned). Tick g_gate.tick is running and will never reach its gate.
 * Answer the pending step or wait with outcome "exited" and a proc.down
 * event, push the exit, then let the process end. The report may include the
 * staged input intent, but never claims guest consumption or a captured root:
 * the game's objects may already be freed. */
void wire_exit(u32 code) {
    if (!g_wire_ok || g_exited) return;
    g_exited = 1;
    ev_add(EV_PROC_DOWN, code);
    buf_t b;
    i64 pid = g_step_id >= 0 ? g_step_id : g_wait_id >= 0 ? g_wait_id : g_start_id;
    if (pid >= 0) {
        reply_begin(&b, pid);
        step_report(&b, -1, "exited");
        reply_end(&b);
        g_step_id = g_wait_id = g_start_id = -1;
        g_step_stream = 0;
    }
    b_init(&b, g_out, OBUF);
    b_str(&b, "{\"code\":");
    b_u64(&b, code);
    b_str(&b, ",\"last_gate\":");
    b_u64(&b, g_gate.tick ? g_gate.tick - 1 : 0);
    b_str(&b, ",\"push\":\"exit\",\"tick\":");
    b_u64(&b, g_gate.tick);
    b_ch(&b, '}');
    wire_send_line(&b);
    slog("exit: code %u during tick %u", code, (u32)g_gate.tick);
    shutdown(g_sock, SD_SEND);
}

} /* extern "C" */
