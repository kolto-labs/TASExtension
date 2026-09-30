/* Per-tick semantic journal entry (contract 8.4, semantic/1, M1 fields).
 * Canonical JSON: sorted keys, no whitespace. No field holds a pointer;
 * pointers only enter as presence flags. Floats are written as their bit
 * patterns in hex, so equal entries mean equal bytes.
 *
 * data: BLAKE2s-128 over the per-page BLAKE2s-128 digests of the exe's
 *       writable .data/.bss (0x78d000..0x836000, 169 pages). The page list
 *       is available through the digest verb for bisecting. */
#include "shim.h"

char g_note[256] = ".";
u8 g_input_hash[16];

#define NPAGES ((EXE_DATA_END - EXE_DATA_VA) / 4096)
static u8 g_pages[NPAGES][16];

/* rand draws per call site (main thread), srand seeds */
#define RSLOTS 2048
static u32 r_site[RSLOTS], r_cnt[RSLOTS], r_stamp[RSLOTS];
static u32 r_now = 1, r_used;
static u32 r_order[RSLOTS];
static u32 g_rand_other;
static u32 g_srand_n;
static blake2s_t g_srand_h;

void journal_tick_reset(void) {
    r_now++;
    r_used = 0;
    g_rand_other = 0;
    g_srand_n = 0;
    blake2s_init(&g_srand_h, 16);
}

void rand_count(u32 site) {
    if (cur_rtid() != g_main_rtid) { g_rand_other++; return; }
    u32 h = (site * 2654435761u) >> 21;
    for (u32 i = 0; i < RSLOTS; i++) {
        u32 k = (h + i) & (RSLOTS - 1);
        if (r_stamp[k] != r_now) {
            r_stamp[k] = r_now;
            r_site[k] = site;
            r_cnt[k] = 1;
            if (r_used < RSLOTS) r_order[r_used++] = k;
            return;
        }
        if (r_site[k] == site) { r_cnt[k]++; return; }
    }
}

void srand_note(u32 site, u32 seed) {
    g_srand_n++;
    u32 rec[3] = {site, seed, cur_rtid() == g_main_rtid};
    blake2s_update(&g_srand_h, rec, sizeof rec);
    if (g_srand_n <= 2 && site < 0x5f9000 && site > 0x5f8000) slog("srand(%u) from %x", seed, site);
}

void journal_data_pages(u8 *out, u32 *npages) {
    memcpy(out, g_pages, sizeof g_pages);
    *npages = NPAGES;
}

static u32 rd32(u32 a, int *ok) {
    if (!a || IsBadReadPtr((void *)(uintptr_t)a, 4)) { *ok = 0; return 0; }
    return *(u32 *)(uintptr_t)a;
}

typedef u32(__attribute__((thiscall)) * getpid_fn)(void *);
typedef void *(__attribute__((thiscall)) * getobj_fn)(void *, u32);

static void semantic(buf_t *b) {
    int ok = 1;
    u32 app = rd32(ADDR_APP_MANAGER, &ok);
    u32 client = app ? rd32(app + 4, &ok) : 0;
    u32 cint = client ? rd32(client + 4, &ok) : 0;
    u32 server = app ? rd32(app + 8, &ok) : 0;
    u32 sint = server ? rd32(server + 4, &ok) : 0;
    b_str(b, "{");
    if (cint) {
        b_str(b, "\"anr\":"); b_u64(b, rd32(cint + 0x288, &ok));
        b_str(b, ",\"cmod\":"); b_u64(b, rd32(cint + 0x18, &ok) != 0);
        b_str(b, ",\"cpid\":"); b_u64(b, rd32(cint + 0x20, &ok));
        b_str(b, ",\"idis\":"); b_u64(b, rd32(cint + 0x2bc, &ok));
    }
    if (sint) {
        b_str(b, cint ? "," : "");
        b_str(b, "\"lfs\":"); b_u64(b, rd32(sint + 0x100c4, &ok));
        b_str(b, ",\"lmod\":"); b_u64(b, rd32(sint + 0x100b4, &ok));
    }
    if (cint) {
        b_str(b, ",\"lsg\":"); b_u64(b, rd32(cint + 0x33c, &ok));
        b_str(b, ",\"mm\":"); b_u64(b, rd32(cint + 0x280, &ok) != 0);
    }
    if (sint) {
        b_str(b, (cint || 1) ? "," : "");
        b_str(b, "\"mtm\":"); b_u64(b, rd32(sint + 0x10080, &ok));
        b_str(b, ",\"pause\":"); b_u64(b, rd32(sint + 0x10078, &ok) & 0xff);
        /* player: server object position, orientation and area */
        u32 pid = ((getpid_fn)(uintptr_t)ADDR_GET_PLAYER_ID)((void *)(uintptr_t)server);
        b_str(b, ",\"pc\":{");
        b_str(b, "\"id\":"); b_u64(b, pid);
        if (pid && pid != 0x7f000000u && pid != 0xffffffffu) {
            u32 obj = (u32)(uintptr_t)((getobj_fn)(uintptr_t)ADDR_GET_OBJECT)((void *)(uintptr_t)server, pid);
            if (obj && !IsBadReadPtr((void *)(uintptr_t)obj, 0xa8)) {
                u32 *p = (u32 *)(uintptr_t)obj;
                b_str(b, ",\"area\":"); b_u64(b, p[0x8c / 4]);
                b_str(b, ",\"ori\":[");
                for (int i = 0; i < 3; i++) { if (i) b_ch(b, ','); b_ch(b, '"'); b_hex(b, (const u8 *)&p[0x9c / 4 + i], 4); b_ch(b, '"'); }
                b_str(b, "],\"pos\":[");
                for (int i = 0; i < 3; i++) { if (i) b_ch(b, ','); b_ch(b, '"'); b_hex(b, (const u8 *)&p[0x90 / 4 + i], 4); b_ch(b, '"'); }
                b_str(b, "]");
            }
        }
        b_str(b, "}");
    }
    b_str(b, "}");
}

static void sort_sites(void) {
    for (u32 i = 1; i < r_used; i++) {
        u32 k = r_order[i];
        u32 j = i;
        while (j > 0 && r_site[r_order[j - 1]] > r_site[k]) { r_order[j] = r_order[j - 1]; j--; }
        r_order[j] = k;
    }
}

/* Build the canonical entry for tick g_gate.tick. digest_out = BLAKE2s-128(entry). */
void journal_tick(buf_t *b, u8 digest_out[16]) {
    u8 d[16];
    /* .data pages */
    for (u32 i = 0; i < NPAGES; i++) blake2s128((const void *)(uintptr_t)(EXE_DATA_VA + i * 4096), 4096, g_pages[i]);
    u8 root[16];
    blake2s128(g_pages, sizeof g_pages, root);

    int ok = 1;
    u32 idx = rd32(ADDR_TLS_INDEX, &ok);
    u32 ptd = threads_tls_main(idx);   /* the main slot's virtual TLS (CRT _tiddata) */
    u32 holdrand = ptd ? rd32(ptd + 0x14, &ok) : 0;
    u32 exob = rd32(ADDR_EXOBASE, &ok);
    u32 crand = exob ? rd32(exob + 0x10, &ok) : 0;
    u32 cri = crand ? rd32(crand, &ok) : 0;
    u8 cxr[16];
    memset(cxr, 0, 16);
    if (cri && !IsBadReadPtr((void *)(uintptr_t)cri, 0x60)) blake2s128((const void *)(uintptr_t)cri, 0x60, cxr);

    b_str(b, "{\"cl\":"); b_u64(b, g_gate.client_passes);
    b_str(b, ",\"cxr\":\""); b_hex(b, cxr, 16);
    b_str(b, "\",\"data\":\""); b_hex(b, root, 16);
    b_str(b, "\",\"hg\":"); b_u64(b, SH->hang_trips);
    b_str(b, ",\"hr\":"); b_u64(b, holdrand);
    b_str(b, ",\"in\":\""); b_hex(b, g_input_hash, 16);
    b_str(b, "\",\"joins\":"); b_u64(b, SH->joins);
    msg_tick_digest(d);
    b_str(b, ",\"msg\":{\"d\":\""); b_hex(b, d, 16);
    b_str(b, "\",\"n\":"); b_u64(b, g_msg_int_n);
    b_str(b, "},\"note\":"); b_jstr(b, g_note);
    sort_sites();
    u32 total = 0;
    for (u32 i = 0; i < r_used; i++) total += r_cnt[r_order[i]];
    b_str(b, ",\"rand\":{\"n\":"); b_u64(b, total);
    b_str(b, ",\"o\":"); b_u64(b, g_rand_other);
    b_str(b, ",\"s\":[");
    for (u32 i = 0; i < r_used; i++) {
        if (i) b_ch(b, ',');
        b_str(b, "[\"");
        b_hexu32(b, r_site[r_order[i]]);
        b_str(b, "\",");
        b_u64(b, r_cnt[r_order[i]]);
        b_ch(b, ']');
    }
    b_str(b, "]},\"sem\":");
    semantic(b);
    b_str(b, ",\"sp\":"); b_u64(b, g_gate.server_passes);
    blake2s_t sc = g_srand_h;
    blake2s_final(&sc, d);
    b_str(b, ",\"srand\":{\"d\":\""); b_hex(b, d, 16);
    b_str(b, "\",\"n\":"); b_u64(b, g_srand_n);
    b_str(b, "},\"st\":"); b_u64(b, g_gate.subticks);
    b_str(b, ",\"tick\":"); b_u64(b, g_gate.tick);
    b_str(b, ",\"trips\":"); b_u64(b, g_guard_trips);
    b_str(b, ",\"v\":"); b_u64(b, g_clk.v);
    b_str(b, "}");
    blake2s128(b->p, b->n, digest_out);
}
