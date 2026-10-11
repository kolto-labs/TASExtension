/* Control channel (contract C1, section 10): loopback TCP, NDJSON, kptas-wire/1.
 * Python listens; the shim connects from the entry detour. Requests are only
 * read on the harness fiber: before start, and at the gate. The game is never
 * blocked outside the gate.
 *
 * Verbs: hello, tier.lock, clock, clock.save_ms, clock.save_ms_next,
 * os_event, subscribe, ring.attach, capture, start, step, input, wait,
 * digest, mem.read, semantic.read, gate, restore, verify, stats, bye (and
 * quit). Others: unsupported.
 *
 * Tiers: an unlocked session behaves like tas. Verbs whose verbclass writes
 * game state outside the game's own recorded states (mem.write,
 * semantic.apply) are refused unless tier.lock chose lab. restore rewinds to
 * one of the game's own recorded states (verbclass restore) and is allowed in
 * tas. */
#include "shim.h"

int g_wire_ok;
static SOCKET g_sock = INVALID_SOCKET;
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
static char g_wait_event[32];
static u64 g_wait_deadline;
static u32 g_wait_base;
static int g_release;
static input_t g_input;          /* held input for the running request */
static char g_req_note[256] = ".";
static int g_first_tick;
static u32 g_verbclass_tick, g_verbclass_acc;
static u32 g_last_load_done;
static u8 g_last_jdig[16];
static int g_input_close;        /* the held note carries !close (first tick only) */
static int g_os_close;           /* os_event close: the next tick */
static int g_close_pending;       /* enqueue retry for score or OS close */
static int g_exited;

/* events of the running step or wait, reported in its reply */
#define SEV_MAX 64
static u32 g_sev_kind[SEV_MAX], g_sev_val[SEV_MAX], g_sev_n;
static u64 g_sev_tick[SEV_MAX];
u32 g_ev_count[EV_KINDS];
static u32 g_wait_kind;
/* journalled inputs staged at a gate for the next tick */
static u32 g_stage_kind[8], g_stage_val[8], g_stage_n;

static const char *const EV_NAME[EV_KINDS] = {"load.done", "save.begin", "save.done", "close", "proc.down",
                                              "clock.save_ms", "clock.save_ms_next"};

void ev_add(u32 kind, u32 val) {
    if (kind >= EV_KINDS) return;
    g_ev_count[kind]++;
    if (kind <= EV_PROC_DOWN && g_sev_n < SEV_MAX) {
        g_sev_kind[g_sev_n] = kind;
        g_sev_val[g_sev_n] = val;
        g_sev_tick[g_sev_n] = g_gate.tick;
        g_sev_n++;
    }
    if (kind != EV_LOAD_DONE && kind != EV_PROC_DOWN) journal_ev_add(kind, val);
}

static void stage_ev(u32 kind, u32 val) {
    if (g_stage_n < 8) { g_stage_kind[g_stage_n] = kind; g_stage_val[g_stage_n] = val; g_stage_n++; }
}

/* Called after the per-tick resets, as tick g_gate.tick begins. */
void wire_tick_begin(void) {
    for (u32 i = 0; i < g_stage_n; i++) ev_add(g_stage_kind[i], g_stage_val[i]);
    g_stage_n = 0;
    if (SH->close_next) {
        SH->close_next = 0;
        ev_add(EV_CLOSE, 0);
    }
}

#define VC_INPUT 1u
#define VC_GATE 4u
#define VC_CLOCK 8u
#define VC_WAIT 16u
#define VC_CAPTURE 32u
#define VC_READ 64u

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

static void step_report(buf_t *b, int waited, const char *outcome) {
    b_str(b, "{\"epoch\":1,\"events\":[");
    for (u32 i = 0; i < g_sev_n; i++) {
        if (i) b_ch(b, ',');
        b_str(b, "{\"name\":");
        b_jstr(b, EV_NAME[g_sev_kind[i]]);
        b_str(b, ",\"tick\":");
        b_u64(b, g_sev_tick[i]);
        if (g_sev_kind[i] == EV_SAVE_BEGIN || g_sev_kind[i] == EV_SAVE_DONE) { b_str(b, ",\"us\":"); b_u64(b, g_sev_val[i]); }
        if (g_sev_kind[i] == EV_PROC_DOWN) { b_str(b, ",\"code\":"); b_u64(b, g_sev_val[i]); }
        b_ch(b, '}');
    }
    b_str(b, "],\"journal\":\"");
    b_hex(b, g_last_jdig, 16);
    b_str(b, "\",\"outcome\":");
    b_jstr(b, outcome);
    b_str(b, ",\"reason\":1,\"restore_point\":");
    b_str(b, (g_capture_on && g_root_valid) ? "true" : "false");
    b_str(b, ",\"server_passes\":");
    b_u64(b, g_gate.server_passes);
    b_str(b, ",\"state_root\":");
    if (g_capture_on && g_root_valid) { b_ch(b, '"'); b_hex(b, g_root, 32); b_ch(b, '"'); }
    else b_str(b, "null");
    b_str(b, ",\"subticks\":");
    b_u64(b, g_gate.subticks);
    b_str(b, ",\"tick\":");
    b_u64(b, g_gate.tick);
    b_str(b, ",\"verbclass\":");
    b_u64(b, g_verbclass_acc);
    if (waited >= 0) { b_str(b, ",\"waited\":"); b_u64(b, (u64)waited); }
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
    b_str(&b, "{\"build\":\"");
    if (ok1) b_hex(&b, shim_sha, 8);
    b_str(&b, "\",\"caps\":{\"capture\":true,\"clock\":[\"virtual\"],\"context\":\"fiber\"");
    b_str(&b, ",\"dinput\":\"virtual_device\",\"gl_names\":\"virtual\",\"handles\":\"virtual\",\"heap\":\"arena\"");
    b_str(&b, ",\"messages\":\"interpose\",\"os_events\":[\"close\"],\"restore\":true,\"save_ms\":true");
    b_str(&b, ",\"threads\":\"slots_serial\"}");
    b_str(&b, ",\"epoch\":1,\"exe_sha256\":\"");
    if (ok2) b_hex(&b, exe_sha, 32);
    b_str(&b, "\",\"game\":\"k1_gog_103\",\"hooks\":");
    list_json(&b, g_hooks_list);
    b_str(&b, ",\"load_order\":");
    list_json(&b, g_modlist);
    b_str(&b, ",\"modules\":");
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
static int input_i16(jnode *node, int fallback, int *value) {
    if (!node) { *value = fallback; return 1; }
    if (node->type != J_NUM || node->num < -32768 || node->num > 32767) return 0;
    *value = (int)node->num;
    return 1;
}

static int input_i32(jnode *node, int fallback, int *value) {
    if (!node) { *value = fallback; return 1; }
    if (node->type != J_NUM || node->num < (-2147483647LL - 1) || node->num > 2147483647LL) return 0;
    *value = (int)node->num;
    return 1;
}

static int input_vec2(jnode *node, int *x, int *y) {
    if (!node) return 1;
    if (node->type != J_ARR || !node->child || !node->child->next || node->child->next->next)
        return 0;
    return input_i16(node->child, 0, x) && input_i16(node->child->next, 0, y);
}

static int input_vec2_i32(jnode *node, int *x, int *y) {
    if (!node) return 1;
    if (node->type != J_ARR || !node->child || !node->child->next || node->child->next->next)
        return 0;
    return input_i32(node->child, 0, x) && input_i32(node->child->next, 0, y);
}

static int parse_input(jnode *args, input_t *in, const char **bad) {
    memset(in, 0, sizeof *in);
    jnode *k = jget(args, "keys");
    if (k) {
        if (k->type != J_ARR) { *bad = "keys must be an array of DIK codes"; return 0; }
        for (jnode *c = k->child; c; c = c->next) {
            if (c->type != J_NUM || c->num < 1 || c->num > 255) {
                *bad = "each key must be an integer DIK code from 1 to 255";
                return 0;
            }
            u32 d = (u32)c->num;
            in->keys[d >> 3] |= (u8)(1u << (d & 7));
        }
    }
    jnode *bt = jget(args, "buttons");
    if (bt) {
        if (bt->type != J_ARR) { *bad = "buttons must be an array of lmb, rmb, or mmb"; return 0; }
        for (jnode *c = bt->child; c; c = c->next) {
            if (c->type != J_STR) { *bad = "button names must be strings"; return 0; }
            const char *s = c->str;
            if (k_streq(s, "lmb")) in->buttons |= 1;
            else if (k_streq(s, "rmb")) in->buttons |= 2;
            else if (k_streq(s, "mmb")) in->buttons |= 4;
            else { *bad = "button name must be lmb, rmb, or mmb"; return 0; }
        }
    }
    jnode *m = jget(args, "mouse");
    if (m) {
        if (m->type != J_OBJ) { *bad = "mouse must be an object"; return 0; }
        jnode *a = jget(m, "abs"), *r = jget(m, "rel");
        if (a) {
            if (!input_vec2(a, &in->abs_x, &in->abs_y)) {
                *bad = "mouse.abs must contain two signed 16-bit integers";
                return 0;
            }
            in->has_abs = 1;
        }
        if (r && !input_vec2_i32(r, &in->rel_x, &in->rel_y)) {
            *bad = "mouse.rel must contain two signed 32-bit integers";
            return 0;
        }
    }
    if (!input_i32(jget(args, "wheel"), 0, &in->wheel)) {
        *bad = "wheel must be a signed 32-bit integer";
        return 0;
    }
    return 1;
}

/* "msgs": OS events on the note's first tick. Returns 0 on an unsupported one. */
static int parse_msgs(jnode *args, int *close, const char **bad) {
    *close = 0;
    jnode *m = jget(args, "msgs");
    if (m && m->type == J_ARR)
        for (jnode *c = m->child; c; c = c->next) {
            const char *s = jstr(c, "");
            if (k_streq(s, "close")) *close = 1;
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

static void handle(char *line, u32 len, int phase) {
    jnode *req = json_parse(line, len);
    if (!req) { reply_err(-1, "bad_args", "unparseable request"); return; }
    i64 id = jint(jget(req, "id"), -1);
    const char *verb = jstr(jget(req, "verb"), "");
    jnode *args = jget(req, "args");
    buf_t b;
    if (!verb_allowed(verb)) { reply_err(id, "refused", "verb outside the locked tier"); return; }

    if (k_streq(verb, "hello")) { do_hello(id); return; }
    if (k_streq(verb, "tier.lock")) {
        const char *t = jstr(jget(args, "tier"), "lab");
        if (g_gate.started) { reply_err(id, "state", "tier.lock must come before start"); return; }
        if (!k_streq(t, "lab") && !k_streq(t, "tas")) { reply_err(id, "bad_args", "tier must be lab or tas"); return; }
        if (g_tier_locked && !k_streq(t, g_tier)) { reply_err(id, "refused", "tier already locked"); return; }
        memcpy(g_tier, t, strlen(t) + 1);
        g_tier_locked = 1;
        reply_begin(&b, id);
        b_str(&b, "{\"allow\":[\"hello\",\"tier.lock\",\"clock\",\"clock.save_ms\",\"clock.save_ms_next\",\"os_event\",\"subscribe\",\"ring.attach\",\"capture\",\"start\",\"step\",\"input\",\"wait\",\"digest\",\"mem.read\",\"semantic.read\",\"gate\",\"restore\",\"verify\",\"stats\",\"bye\"");
        if (k_streq(t, "lab")) b_str(&b, ",\"mem.write\",\"semantic.apply\"");
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
        if (!k_streq(ev, "close")) { reply_err(id, "unsupported", "os_event: only close"); return; }
        if (phase != 1) { reply_err(id, "state", "os_event runs at the gate after start"); return; }
        g_os_close = 1;
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
        g_gate.started = 1;
        g_gate.tick = 0;
        clock_set_tick(0);
        clock_tick_reset();
        journal_tick_reset();
        msg_tick_reset();
        g_client_count = g_server_count = g_us_count = 0;
        g_start_id = id;
        g_verbclass_acc = VC_GATE | VC_CLOCK;
        g_sev_n = 0;
        wire_tick_begin();
        g_release = 1;
        return;
    }
    if (k_streq(verb, "step") || k_streq(verb, "wait")) {
        if (phase != 1) { reply_err(id, "state", "step and wait run at the gate after start"); return; }
        const char *bad = "";
        int cl = 0;
        if (!parse_msgs(args, &cl, &bad)) { reply_err(id, "unsupported", bad); return; }
        input_t next_input;
        if (!parse_input(args, &next_input, &bad)) { reply_err(id, "bad_args", bad); return; }
        int next_first = jget(args, "first")
                         ? (jget(args, "first")->type == J_BOOL ? (int)jget(args, "first")->num : 1)
                         : 1;
        i64 step_count = 0;
        i64 wait_timeout = 0;
        const char *next_wait_event = "";
        if (k_streq(verb, "step")) {
            step_count = jint(jget(args, "n"), 1);
            if (step_count < 0) { reply_err(id, "bad_args", "n must be non-negative"); return; }
            if (step_count == 0) {
                /* A zero-step query must not replace held input or the current
                 * note. Keep it observational and leave pending controls intact. */
                g_sev_n = 0;
                g_verbclass_acc = 0;
                reply_begin(&b, id);
                step_report(&b, -1, "alive");
                reply_end(&b);
                return;
            }
        } else {
            next_wait_event = jstr(jget(args, "event"), "");
            wait_timeout = jint(jget(args, "timeout"), 1800);
            if (wait_timeout < 0) { reply_err(id, "bad_args", "timeout must be non-negative"); return; }
        }

        /* Commit the requested input only after all control arguments have
         * passed validation. Rejected commands must not leak key presses into
         * a later accepted step. */
        g_input = next_input;
        g_input_close = cl;
        set_note(args);
        g_first_tick = next_first;
        g_sev_n = 0;
        g_verbclass_acc = 0;
        if (k_streq(verb, "step")) {
            g_step_left = (u64)step_count;
            g_step_id = id;
        } else {
            u32 l = (u32)strlen(next_wait_event);
            if (l >= sizeof g_wait_event) l = sizeof g_wait_event - 1;
            memcpy(g_wait_event, next_wait_event, l);
            g_wait_event[l] = 0;
            g_wait_deadline = g_gate.tick + (u64)wait_timeout;
            int k = event_kind(g_wait_event);
            g_wait_kind = k < 0 ? EV_KINDS : (u32)k;
            g_wait_base = k < 0 ? 0 : g_ev_count[k];
            g_wait_id = id;
        }
        g_release = 1;
        return;
    }
    if (k_streq(verb, "input")) {
        const char *bad = "";
        int cl = 0;
        if (!parse_msgs(args, &cl, &bad)) { reply_err(id, "unsupported", bad); return; }
        input_t next_input;
        if (!parse_input(args, &next_input, &bad)) { reply_err(id, "bad_args", bad); return; }
        g_input = next_input;
        g_input_close = cl;
        set_note(args);
        reply_begin(&b, id);
        b_str(&b, "{\"for_tick\":");
        b_u64(&b, g_gate.tick + 1);
        b_ch(&b, '}');
        reply_end(&b);
        return;
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
        jnode *en = jget(args, "enabled");
        g_capture_on = en ? (int)en->num : 1;
        reply_begin(&b, id);
        b_str(&b, "{\"classes\":[{\"name\":\"game_arena\",\"ranges\":[[\"0x31000000\",\"0x71000000\"]]},");
        b_str(&b, "{\"name\":\"image_data\",\"ranges\":[[\"0x0078d000\",\"0x00836000\"]]},");
        b_str(&b, "{\"name\":\"harness_game\",\"ranges\":[[\"0x0e000000\",\"0x0e400000\"]]}],\"enabled\":");
        b_str(&b, g_capture_on ? "true" : "false");
        b_str(&b, ",\"ring\":");
        b_str(&b, kst_ring_attached() ? "true" : "false");
        b_ch(&b, '}');
        reply_end(&b);
        return;
    }
    if (k_streq(verb, "restore")) {
        if (phase != 1) { reply_err(id, "state", "restore runs at the gate after start"); return; }
        if (!g_capture_on) { reply_err(id, "state", "capture is off"); return; }
        i64 t = jint(jget(args, "tick"), -1);
        const char *rh = jstr(jget(args, "root"), "");
        const char *path = jstr(jget(args, "path"), "");
        u8 root[32];
        if (t < 0 || strlen(rh) != 64 || !*path) { reply_err(id, "bad_args", "tick, root (64 hex) and path"); return; }
        for (int i = 0; i < 32; i++) {
            u32 v = 0;
            for (int j = 0; j < 2; j++) {
                char c = rh[i * 2 + j];
                v = (v << 4) | (u32)(c >= 'a' ? c - 'a' + 10 : c >= 'A' ? c - 'A' + 10 : c - '0');
            }
            root[i] = (u8)v;
        }
        buf_t rb;
        static char rbuf[4096];
        b_init(&rb, rbuf, sizeof rbuf);
        int ok = state_restore((u64)t, root, path, &rb);
        b_term(&rb);
        reply_begin(&b, id);
        b_raw(&b, rb.p, rb.n);
        reply_end(&b);
        if (ok) g_verbclass_tick |= VC_CAPTURE;
        return;
    }
    if (k_streq(verb, "verify") || k_streq(verb, "stats")) {
        reply_begin(&b, id);
        state_audit_json(&b);
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
        slog("bye: terminating with %d at tick %u", (int)code, (u32)g_gate.tick);
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

/* Called at each gate on the harness fiber: close tick n, answer, serve, stage tick n+1. */
static LARGE_INTEGER g_qpf_real, g_t_release;

void wire_gate_hook(void) {
    buf_t j;
    LARGE_INTEGER t0, t1;
    if (!g_qpf_real.QuadPart) QueryPerformanceFrequency(&g_qpf_real);
    QueryPerformanceCounter(&t0);
    u32 run_us = g_t_release.QuadPart ? (u32)((t0.QuadPart - g_t_release.QuadPart) * 1000000 / g_qpf_real.QuadPart) : 0;
    b_init(&j, g_jent, JBUF);
    journal_tick(&j, g_last_jdig);
    QueryPerformanceCounter(&t1);
    u32 jr_us = (u32)((t1.QuadPart - t0.QuadPart) * 1000000 / g_qpf_real.QuadPart);
    b_term(&j);
    state_capture(g_gate.tick, j.p, j.n);
    g_verbclass_acc |= g_verbclass_tick;
    if (g_load_done_count != g_last_load_done) { ev_add(EV_LOAD_DONE, 0); g_last_load_done = g_load_done_count; }

    if (g_wire_ok && g_sub_tick) {
        buf_t b;
        b_init(&b, g_out, OBUF);
        b_str(&b, "{\"push\":\"tick\",\"tick\":");
        b_u64(&b, g_gate.tick);
        b_str(&b, ",\"epoch\":1,\"input_sha256\":\"");
        b_hex(&b, g_input_sha256, 32);
        b_ch(&b, '\"');
        b_str(&b, ",\"ext\":");
        b_u64(&b, g_msg_ext_n);
        b_str(&b, ",\"run_us\":");
        b_u64(&b, run_us);
        b_str(&b, ",\"journal_us\":");
        b_u64(&b, jr_us);
        if (g_guard_first_site) {
            b_str(&b, ",\"trip_site\":\"");
            b_hexu32(&b, g_guard_first_site);
            b_ch(&b, '"');
        }
        if (g_capture_on && g_root_valid) {
            b_str(&b, ",\"root\":\"");
            b_hex(&b, g_root, 32);
            b_str(&b, "\",\"cap\":");
            state_stats_json(&b);
        }
        b_str(&b, ",\"input_observation\":");
        di_observation_json(&b, g_gate.tick);
        b_str(&b, ",\"entry\":");
        b_raw(&b, j.p, j.n);
        b_ch(&b, '}');
        wire_send_line(&b);
    }
    di_observation_reset();
    int hold = 1;
    if (g_start_id >= 0) {
        buf_t b;
        reply_begin(&b, g_start_id);
        step_report(&b, -1, "alive");
        reply_end(&b);
        g_start_id = -1;
    } else if (g_step_id >= 0) {
        if (--g_step_left == 0) {
            buf_t b;
            reply_begin(&b, g_step_id);
            step_report(&b, -1, "alive");
            reply_end(&b);
            g_step_id = -1;
        } else hold = 0;
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

    /* stage tick n+1 */
    u32 ms = (u32)((g_clk.uptime0_ms * 1000ull + g_clk.seg_v + (g_gate.tick + 1 - g_clk.seg_tick) * 1000000ull / g_clk.hz) / 1000ull);
    di_stage(&g_input, g_first_tick, ms, g_input_hash, g_input_sha256);
    /* OS event close: WM_CLOSE to the game window at the start of tick n+1,
     * through the staged queue so it reaches the game's WndProc */
    if ((g_first_tick && g_input_close) || g_os_close) g_close_pending = 1;
    if (g_close_pending && msg_synth_push(WM_CLOSE, 0, 0)) {
        /* Publish the close event only after its matching window message has
         * entered the bounded queue. A full queue leaves the request pending
         * so the next gate can retry after the game drains messages. */
        SH->close_next = 1;
        g_close_pending = 0;
        g_os_close = 0;
    }
    g_first_tick = 0;
    memcpy(g_note, g_req_note, sizeof g_note);
    g_verbclass_tick = VC_GATE | VC_CLOCK | VC_INPUT;
    QueryPerformanceCounter(&g_t_release);
}

/* The game is exiting (ExitProcess on the main slot, or the CRT entry
 * returned). Tick g_gate.tick is running and will never reach its gate.
 * Answer the pending step or wait with outcome "exited" and a proc.down
 * event, push the exit, then let the process end. No journal entry is built:
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
