/* Per-tick semantic journal entry (contract 8.4, semantic/1, M1 fields).
 * Canonical JSON: sorted keys, no whitespace. No field holds a pointer;
 * pointers only enter as presence flags. Floats are written as their bit
 * patterns in hex, so equal entries mean equal bytes.
 *
 * data: BLAKE2s-128 over the per-page BLAKE2s-128 digests of the exe's
 *       writable .data/.bss (0x78d000..0x836000, 169 pages). The page list
 *       is available through the digest verb for bisecting.
 *       Each gate compares every page with a harness_core shadow copy and
 *       rehashes only the pages that differ. A page digest is always the hash
 *       of its shadow, and the shadow always equals the live page after the
 *       compare, so the digests and the entry bytes match a full rehash. */
#include "shim.hpp"
#include <emmintrin.h>

// Retain the native C ABI, including CRT and assembly-referenced symbols.
extern "C" {

char g_note[256] = ".";
u8 g_input_hash[16];

#define NPAGES ((EXE_DATA_END - EXE_DATA_VA) / 4096)
static u8 g_pages[NPAGES][16];
/* Shadow of the exe pages in harness_core (never captured or restored).
 * g_shadow_ok is false at load and after a restore; the next gate then copies
 * and rehashes every page. One allocation attempt: on failure every gate
 * keeps the full rehash, and nothing is allocated mid-run. */
static u8 *g_shadow;
static int g_shadow_tried, g_shadow_ok;

/* rand draws per call site (main thread), srand seeds */
#define RSLOTS 2048
static u32 r_site[RSLOTS], r_cnt[RSLOTS], r_stamp[RSLOTS];
static u32 r_now = 1, r_used;
static u32 r_order[RSLOTS];
static u32 g_rand_other;
static u32 g_srand_n;
static blake2s_t g_srand_h;

/* journalled events of the current tick (saves, close, save-length inputs, restores) */
#define JEV_MAX 64
static u32 g_jev_kind[JEV_MAX], g_jev_val[JEV_MAX], g_jev_aux[JEV_MAX], g_jev_n;
static u32 g_jev_dropped;
static u32 g_jev_external_dropped;

/* The restore the tick was played from: target tick, its root, and what admitted the root
 * ("self", the admitted set's sha256, or "lab"). Set at the gate, journalled on the next tick. */
static struct { u64 tick; u8 root[32]; char set[68]; } g_jrest;

void journal_restore_note(u64 tick, const u8 root[32], const char *set) {
    u32 n = static_cast<u32>(strlen(set));
    if (n > 64) n = 64;
    g_jrest.tick = tick;
    memcpy(g_jrest.root, root, 32);
    memcpy(g_jrest.set, set, n);
    g_jrest.set[n] = 0;
    g_shadow_ok = 0;   /* the compare alone would catch it; rehash anyway after a restore */
}

void journal_tick_reset(void) {
    r_now++;
    r_used = 0;
    g_rand_other = 0;
    g_srand_n = 0;
    blake2s_init(&g_srand_h, 16);
    g_jev_n = 0;
    g_jev_dropped = 0;
    g_jev_external_dropped = 0;
    aud_tick_reset();
}

void journal_ev_add(u32 kind, u32 val, u32 aux) {
    if (g_jev_n >= JEV_MAX) {
        if (g_jev_dropped != 0xffffffffu) g_jev_dropped++;
        return;
    }
    g_jev_kind[g_jev_n] = kind;
    g_jev_val[g_jev_n] = val;
    g_jev_aux[g_jev_n] = aux;
    g_jev_n++;
}

void journal_event_drop_note(u32 n) {
    if (0xffffffffu - g_jev_external_dropped < n) g_jev_external_dropped = 0xffffffffu;
    else g_jev_external_dropped += n;
}

u32 journal_event_dropped(void) {
    if (0xffffffffu - g_jev_dropped < g_jev_external_dropped) return 0xffffffffu;
    return g_jev_dropped + g_jev_external_dropped;
}

/* save.begin / save.done: who asked (auto or manual), the slot, the virtual us */
static void journal_save_ev(buf_t *b, const char *name, u32 v, u32 aux) {
    static const char *const kind[3] = {"none", "auto", "manual"};
    u32 k = aux & 0xff;
    b_str(b, "{\"kind\":");
    b_jstr(b, kind[k < 3 ? k : 0]);
    b_str(b, ",\"name\":");
    b_jstr(b, name);
    b_str(b, ",\"slot\":");
    b_u64(b, aux >> 8);
    b_str(b, ",\"us\":");
    b_u64(b, v);
    b_ch(b, '}');
}

/* The completeness fields make the bounded event list safe to consume. The
 * gate refuses to publish a successful tick if either event channel overflowed. */
static void journal_ev(buf_t *b) {
    b_str(b, ",\"ev\":[");
    for (u32 i = 0; i < g_jev_n; i++) {
        u32 k = g_jev_kind[i], v = g_jev_val[i], aux = g_jev_aux[i];
        if (i) b_ch(b, ',');
        switch (k) {
        case EV_SAVE_BEGIN: journal_save_ev(b, "save.begin", v, aux); break;
        case EV_SAVE_DONE: journal_save_ev(b, "save.done", v, aux); break;
        case EV_CLOSE: b_str(b, "{\"name\":\"close\"}"); break;
        case EV_ALTF4: b_str(b, "{\"name\":\"altf4\"}"); break;
        case EV_SAVE_MS: b_str(b, "{\"ms\":"); b_u64(b, v); b_str(b, ",\"name\":\"clock.save_ms\"}"); break;
        case EV_SAVE_MS_NEXT: b_str(b, "{\"ms\":"); b_u64(b, v); b_str(b, ",\"name\":\"clock.save_ms_next\"}"); break;
        case EV_RESTORE:
            b_str(b, "{\"name\":\"restore\",\"root\":\"");
            b_hex(b, g_jrest.root, 32);
            b_str(b, "\",\"set\":");
            b_jstr(b, g_jrest.set);
            b_str(b, ",\"tick\":");
            b_u64(b, g_jrest.tick);
            b_ch(b, '}');
            break;
        default: b_str(b, "{\"name\":\"?\"}"); break;
        }
    }
    const u32 dropped = journal_event_dropped();
    b_str(b, "],\"ev_complete\":");
    b_str(b, dropped ? "false" : "true");
    if (dropped) {
        b_str(b, ",\"ev_dropped\":");
        b_u64(b, dropped);
    }
}

void rand_count(u32 site) {
    if (cur_rtid() != g_main_rtid) { g_rand_other++; return; }
    if ((site == g_trace_site || !g_trace_site) && trace_on(TR_RAND)) diag_bt("rand", site, 0);
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

/* 1 when the 4096-byte pages a and b are equal. Both are 16-byte aligned. */
static int page_equal(const u8 *a, const u8 *b) {
    const __m128i *x = reinterpret_cast<const __m128i *>(a);
    const __m128i *y = reinterpret_cast<const __m128i *>(b);
    for (u32 i = 0; i < 4096 / 16; i += 8) {
        __m128i d = _mm_or_si128(_mm_or_si128(_mm_or_si128(_mm_xor_si128(_mm_load_si128(x + i), _mm_load_si128(y + i)),
                                                           _mm_xor_si128(_mm_load_si128(x + i + 1), _mm_load_si128(y + i + 1))),
                                              _mm_or_si128(_mm_xor_si128(_mm_load_si128(x + i + 2), _mm_load_si128(y + i + 2)),
                                                           _mm_xor_si128(_mm_load_si128(x + i + 3), _mm_load_si128(y + i + 3)))),
                                 _mm_or_si128(_mm_or_si128(_mm_xor_si128(_mm_load_si128(x + i + 4), _mm_load_si128(y + i + 4)),
                                                           _mm_xor_si128(_mm_load_si128(x + i + 5), _mm_load_si128(y + i + 5))),
                                              _mm_or_si128(_mm_xor_si128(_mm_load_si128(x + i + 6), _mm_load_si128(y + i + 6)),
                                                           _mm_xor_si128(_mm_load_si128(x + i + 7), _mm_load_si128(y + i + 7)))));
        if (_mm_movemask_epi8(_mm_cmpeq_epi8(d, _mm_setzero_si128())) != 0xffff) return 0;
    }
    return 1;
}

/* Bring g_pages up to date with the live exe pages. */
static void data_pages_update(void) {
    if (!g_shadow_tried) {
        g_shadow_tried = 1;
        g_shadow = static_cast<u8 *>(core_alloc(NPAGES * 4096));
        g_shadow_ok = 0;
    }
    if (!g_shadow) {
        for (u32 i = 0; i < NPAGES; i++) blake2s128((const void *)reinterpret_cast<uintptr_t>((EXE_DATA_VA + i * 4096)), 4096, g_pages[i]);
        return;
    }
    const int all = !g_shadow_ok;
    for (u32 i = 0; i < NPAGES; i++) {
        const u8 *live = reinterpret_cast<const u8 *>(static_cast<uintptr_t>(EXE_DATA_VA + i * 4096));
        u8 *sh = g_shadow + i * 4096;
        if (!all && page_equal(live, sh)) continue;
        memcpy(sh, live, 4096);
        blake2s128(sh, 4096, g_pages[i]);
    }
    g_shadow_ok = 1;
}

static u32 rd32(u32 a, int *ok) {
    if (!a || IsBadReadPtr((void *)reinterpret_cast<uintptr_t>(a), 4)) { *ok = 0; return 0; }
    return *reinterpret_cast<u32 *>(reinterpret_cast<uintptr_t>(a));
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
        b_str(b, ",");
        b_str(b, "\"mtm\":"); b_u64(b, rd32(sint + 0x10080, &ok));
        b_str(b, ",\"pause\":"); b_u64(b, rd32(sint + 0x10078, &ok) & 0xff);
        /* player: server object position, orientation and area */
        u32 pid = (reinterpret_cast<getpid_fn>(reinterpret_cast<uintptr_t>(ADDR_GET_PLAYER_ID)))((void *)reinterpret_cast<uintptr_t>(server));
        b_str(b, ",\"pc\":{");
        b_str(b, "\"id\":"); b_u64(b, pid);
        if (pid && pid != 0x7f000000u && pid != 0xffffffffu) {
            u32 obj = static_cast<u32>(reinterpret_cast<uintptr_t>((reinterpret_cast<getobj_fn>(reinterpret_cast<uintptr_t>(ADDR_GET_OBJECT)))((void *)reinterpret_cast<uintptr_t>(server), pid)));
            if (obj && !IsBadReadPtr((void *)reinterpret_cast<uintptr_t>(obj), 0xa8)) {
                u32 *p = reinterpret_cast<u32 *>(reinterpret_cast<uintptr_t>(obj));
                b_str(b, ",\"area\":"); b_u64(b, p[0x8c / 4]);
                b_str(b, ",\"ori\":[");
                for (int i = 0; i < 3; i++) { if (i) b_ch(b, ','); b_ch(b, '"'); b_hex(b, reinterpret_cast<const u8 *>(&p[0x9c / 4 + i]), 4); b_ch(b, '"'); }
                b_str(b, "],\"pos\":[");
                for (int i = 0; i < 3; i++) { if (i) b_ch(b, ','); b_ch(b, '"'); b_hex(b, reinterpret_cast<const u8 *>(&p[0x90 / 4 + i]), 4); b_ch(b, '"'); }
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
    data_pages_update();
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
    if (cri && !IsBadReadPtr((void *)reinterpret_cast<uintptr_t>(cri), 0x60)) blake2s128((const void *)reinterpret_cast<uintptr_t>(cri), 0x60, cxr);

    b_ch(b, '{');
    aud_journal(b);
    b_str(b, "\"cl\":"); b_u64(b, g_gate.client_passes);
    b_str(b, ",\"cxr\":\""); b_hex(b, cxr, 16);
    b_str(b, "\",\"data\":\""); b_hex(b, root, 16);
    b_ch(b, '"');
    journal_ev(b);
    b_str(b, ",\"hg\":"); b_u64(b, SH->hang_trips);
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

} // extern "C"
