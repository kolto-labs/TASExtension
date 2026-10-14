/* Virtual Miles device (audio.device = "virtual") and the lab hooks of the real one.
 *
 * The exe reaches Miles only through its 61 _AIL_* imports. It registers no
 * Miles callbacks and no file callbacks. Every call comes from the CExoSound*
 * code on the main thread slot; quick_handles also comes from the movie
 * thread slot, which the serial scheduler orders like any other.
 *
 * In virtual mode every import is answered here from a model in harness_game
 * (SH->aud), so a restore rewinds it with memory and the host's audio
 * hardware never reaches the game:
 *   - quick_startup succeeds. The driver, providers, listener, voices and
 *     streams are small tagged values, allocated in call order.
 *   - The 3D provider list is pinned to the names Miles 6.5h's providers
 *     report; audio.provider chooses which of them open.
 *   - Sample and stream status and positions are computed when asked, from
 *     the virtual clock, so every query within a tick sees one state.
 *     Lengths come from the WAV header (PCM, IMA ADPCM) or the MP3 frame
 *     count, never from the real Miles.
 *   - Decoded waves (decompress_ASI, decompress_ADPCM) are canonical: a PCM
 *     WAV image of the size and format the decoder produces, allocated on the
 *     exe's own CRT heap (so in the arena), with the samples zeroed. The game
 *     reads only the header.
 *   - Bink gets no sound track: the driver the game hands it is not a real
 *     Miles driver.
 * Nothing is played (audio.output = "null").
 *
 * In real mode (lab) the real Miles answers. open_stream still gets a real
 * file handle in place of the game's virtual one, or Miles could not read
 * it (the stock m3 shim hands the virtual one over, and streams fail). audio.shadow also logs the real answers beside the model's pure
 * functions: provider list, WAV info, decode sizes and stream lengths. */
#include "shim.hpp"

/* Fixed C hook ABI and assembly-referenced symbol names. */
extern "C" {

int g_aud_mode;
static int g_aud_shadow;
static u32 g_aud_open_mask = 0x61u;   /* provider i opens when bit i is set */
static const char *g_aud_provider = "software";
static u32 g_aud_hooked;
static u32 g_aud_n, g_aud_ev;          /* this tick: calls, state changes */

#define A (SH->aud)

enum { SMP_FREE = 1, SMP_DONE = 2, SMP_PLAYING = 4, SMP_STOPPED = 8 };

#define H_DRIVER   0x0A500004u
#define H_LISTENER 0x0A520004u
#define H_S2D      0x0A530000u
#define H_S3D      0x0A540000u
#define H_STREAM   0x0A550000u

/* The 3D providers in the order, and with the handles, the real Miles 6.5h enumerates them under Wine
 * (shadow log): handles 9..15. There, with a device, the DirectSound3D hardware and EAX providers fail
 * to open (8) and the other three open. */
static const char *const PROV_NAME[AUD_NPROV] = {
    "DirectSound3D Software Emulation", "DirectSound3D Hardware Support", "Creative Labs EAX 3 (TM)",
    "Creative Labs EAX 2 (TM)", "Creative Labs EAX (TM)", "Dolby Surround", "Miles Fast 2D Positional Audio",
};
#define PROV_FIRST 9u
#define M3D_FAIL 8u

#define EXE_MALLOC 0x006fd8fbu   /* _malloc (static CRT) */
#define EXE_FREE   0x006fb7b2u   /* _free */
typedef void *(__cdecl *malloc_fn)(u32);
typedef void(__cdecl *free_fn)(void *);

/* ---------------------------------------------------------------- environment */

void audio_parse_env(const char *s) {
    g_aud_mode = AUD_NONE;
    if (!s) return;
    while (*s) {
        const char *k = s;
        while (*s && *s != '=' && *s != ';') s++;
        u32 kl = (u32)(s - k);
        if (*s != '=') { if (*s) s++; continue; }
        const char *v = ++s;
        while (*s && *s != ';') s++;
        u32 vl = (u32)(s - v);
        if (kl == 6 && k_strneq(k, "device", 6))
            g_aud_mode = (vl == 7 && k_strneq(v, "virtual", 7)) ? AUD_VIRTUAL : (vl == 4 && k_strneq(v, "real", 4)) ? AUD_REAL : AUD_NONE;
        else if (kl == 8 && k_strneq(k, "provider", 8)) {
            if (vl == 8 && k_strneq(v, "hardware", 8)) { g_aud_open_mask = 0x63u; g_aud_provider = "hardware"; }
            else if (vl == 3 && k_strneq(v, "eax", 3)) { g_aud_open_mask = 0x7Fu; g_aud_provider = "eax"; }
            else { g_aud_open_mask = 0x61u; g_aud_provider = "software"; }
        } else if (kl == 6 && k_strneq(k, "shadow", 6))
            g_aud_shadow = vl && *v == '1';
        if (*s) s++;
    }
}

/* ---------------------------------------------------------------- helpers */

static u64 now_us(void) {
    u64 u = g_clk.uptime0_ms * 1000ull + g_clk.v + g_clk.extra;
    return u < g_clk.last_u ? g_clk.last_u : u;   /* clock_u's value, without moving last_u */
}

static u16 rd16(const u8 *p) { return (u16)(p[0] | p[1] << 8); }
static u32 rd32(const u8 *p) { return (u32)p[0] | (u32)p[1] << 8 | (u32)p[2] << 16 | (u32)p[3] << 24; }
static void wr16(u8 *p, u32 v) { p[0] = (u8)v; p[1] = (u8)(v >> 8); }
static void wr32(u8 *p, u32 v) { p[0] = (u8)v; p[1] = (u8)(v >> 8); p[2] = (u8)(v >> 16); p[3] = (u8)(v >> 24); }

typedef struct {
    u32 fmt, rate, bits, channels, block, data_len, data_off, samples, fact, has_fact, spb, avg;
} wav_t;

/* IMA ADPCM sample count of len bytes in blocks of `block` bytes. */
static u32 adpcm_samples(u32 len, u32 block, u32 ch, u32 spb) {
    if (!ch || block <= 4 * ch) return 0;
    if (!spb) spb = (block - 4 * ch) * 2 / ch + 1;
    u32 rem = len % block;
    return (len / block) * spb + (rem > 4 * ch ? (rem - 4 * ch) * 2 / ch + 1 : rem == 4 * ch ? 1 : 0);
}

/* RIFF WAVE header at p, of which avail bytes exist. data_len is the data chunk's own length, as Miles
 * takes it; only a file read clamps it to the bytes that exist. */
static int wav_parse(const u8 *p, u32 avail, int clamp, wav_t *w) {
    memset(w, 0, sizeof *w);
    if (!p || avail < 12 || IsBadReadPtr(p, 12) || memcmp(p, "RIFF", 4) || memcmp(p + 8, "WAVE", 4)) return 0;
    int have_fmt = 0;
    u32 spb = 0;
    for (u32 o = 12; o + 8 <= avail && !IsBadReadPtr(p + o, 8);) {
        u32 len = rd32(p + o + 4);
        const u8 *c = p + o + 8;
        if (!memcmp(p + o, "fmt ", 4) && len >= 16 && !IsBadReadPtr(c, len < 20 ? len : 20)) {
            w->fmt = rd16(c); w->channels = rd16(c + 2); w->rate = rd32(c + 4); w->avg = rd32(c + 8);
            w->block = rd16(c + 12); w->bits = rd16(c + 14);
            if (len >= 20 && rd16(c + 16) >= 2) spb = w->spb = rd16(c + 18);
            have_fmt = 1;
        } else if (!memcmp(p + o, "fact", 4) && len >= 4 && !IsBadReadPtr(c, 4)) {
            w->fact = rd32(c); w->has_fact = 1;
        } else if (!memcmp(p + o, "data", 4)) {
            w->data_off = o + 8;
            w->data_len = len;
            if (clamp && w->data_off + len > avail) w->data_len = avail - w->data_off;
            break;
        }
        if (len > avail - o - 8) return 0;
        o += 8 + len + (len & 1);
    }
    if (!have_fmt || !w->data_off || !w->channels || !w->rate) return 0;
    if (w->fmt == 1) w->samples = w->block ? w->data_len / w->block : 0;
    else if (w->fmt == 0x11) w->samples = adpcm_samples(w->data_len, w->block, w->channels, spb);
    else w->samples = w->has_fact ? w->fact : 0;
    return 1;
}

/* ---- MP3 frames: a byte source that is either memory or a file window ---- */
typedef struct {
    const u8 *mem;            /* memory source, or 0 */
    HANDLE h;                 /* file source */
    u64 base;                 /* file offset of byte 0 */
    u32 len;                  /* bytes in the source */
    u32 woff, wlen;           /* the window g_win holds */
} src_t;
static u8 g_win[65536];

static const u8 *src_at(src_t *s, u32 off, u32 n) {
    if (off + n > s->len || off + n < off) return 0;
    if (s->mem) return s->mem + off;
    if (off >= s->woff && off + n <= s->woff + s->wlen) return g_win + (off - s->woff);
    LONG hi = (LONG)((s->base + off) >> 32);
    if (SetFilePointer(s->h, (LONG)(u32)(s->base + off), &hi, FILE_BEGIN) == INVALID_SET_FILE_POINTER &&
        GetLastError() != NO_ERROR)
        return 0;
    DWORD want = s->len - off < sizeof g_win ? s->len - off : sizeof g_win, got = 0;
    if (!ReadFile(s->h, g_win, want, &got, NULL) || got < n) return 0;
    s->woff = off;
    s->wlen = got;
    return g_win;
}

static const u16 BR1[3][16] = {
    {0, 32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448, 0},
    {0, 32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384, 0},
    {0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0},
};
static const u16 BR2[3][16] = {
    {0, 32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256, 0},
    {0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0},
    {0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0},
};
static const u32 SR[3][3] = {{11025, 12000, 8000}, {22050, 24000, 16000}, {44100, 48000, 32000}};

typedef struct { u32 len, spf, rate, channels, key, kbps; } mph_t;

static int mp3_header(const u8 *b, mph_t *f) {
    if (b[0] != 0xFF || (b[1] & 0xE0) != 0xE0) return 0;
    u32 ver = (b[1] >> 3) & 3, layer = (b[1] >> 1) & 3, bri = b[2] >> 4, sri = (b[2] >> 2) & 3, pad = (b[2] >> 1) & 1;
    if (ver == 1 || layer == 0 || bri == 0 || bri == 15 || sri == 3) return 0;
    u32 l = 3 - layer;                                    /* 0 Layer I, 1 Layer II, 2 Layer III */
    u32 kbps = ver == 3 ? BR1[l][bri] : BR2[l][bri];
    u32 rate = SR[ver == 3 ? 2 : ver == 2 ? 1 : 0][sri];
    if (l == 0) { f->len = (12000 * kbps / rate + pad) * 4; f->spf = 384; }
    else if (l == 1 || ver == 3) { f->len = 144000 * kbps / rate + pad; f->spf = 1152; }
    else { f->len = 72000 * kbps / rate + pad; f->spf = 576; }
    f->rate = rate;
    f->kbps = kbps;
    f->channels = (b[3] >> 6) == 3 ? 1 : 2;
    f->key = (u32)(b[1] & 0xFE) << 8 | (b[2] & 0x0C);    /* version, layer, rate: constant in a stream */
    return f->len >= 4;
}

/* Count complete MP3 frames: samples, the first frame's rate and channels. An ID3v2 tag is skipped; a
 * sync is accepted when the next frame also syncs, or when it is the last frame. */
static u32 g_mp3_kbps;   /* the bit rate of the frame the last scan locked on */

static int mp3_scan(src_t *s, u32 *samples, u32 *rate, u32 *channels, u32 *frames) {
    u32 o = 0;
    const u8 *b = src_at(s, 0, 10);
    if (b && !memcmp(b, "ID3", 3)) {
        o = 10 + ((u32)(b[6] & 0x7f) << 21 | (u32)(b[7] & 0x7f) << 14 | (u32)(b[8] & 0x7f) << 7 | (b[9] & 0x7f));
        if (b[5] & 0x10) o += 10;
    }
    u32 key = 0, n = 0, total = 0;
    g_mp3_kbps = 0;
    while (o + 4 <= s->len) {
        mph_t f, g;
        b = src_at(s, o, 4);
        if (!b) break;
        if (!mp3_header(b, &f) || (key && f.key != key) || o + f.len > s->len) { o++; continue; }
        if (!key) {
            const u8 *nb = o + f.len + 4 <= s->len ? src_at(s, o + f.len, 4) : 0;
            if (nb && !(mp3_header(nb, &g) && g.key == f.key)) { o++; continue; }
            if (!nb && o + f.len != s->len) { o++; continue; }
            key = f.key;
            g_mp3_kbps = f.kbps;
            *rate = f.rate;
            *channels = f.channels;
        }
        total += f.spf;
        n++;
        o += f.len;
    }
    *samples = total;
    *frames = n;
    return n > 0;
}

/* ---------------------------------------------------------------- model: samples */

static avoice_t *voice(u32 h) {
    u32 tag = h & 0xFFFF0000u, i = ((h & 0xFFFFu) >> 2) - 1;
    if (tag == H_S2D && i < AUD_N2D && A.v2[i].used) return &A.v2[i];
    if (tag == H_S3D && i < AUD_N3D && A.v3[i].used) return &A.v3[i];
    return 0;
}

static u64 v_played(const avoice_t *v, u64 now) {
    if (v->state != SMP_PLAYING) return v->pos0;
    return v->pos0 + (now > v->t0 ? now - v->t0 : 0) * v->rate / 1000000ull;
}

/* A playing sample whose loops have run out is done. */
static void v_settle(avoice_t *v, u64 now) {
    if (v->state != SMP_PLAYING || !v->loops) return;
    u64 end = (u64)v->loops * v->frames;
    if (v_played(v, now) >= end) { v->state = SMP_DONE; v->pos0 = end; }
}

static void v_init(avoice_t *v) {
    memset(v, 0, sizeof *v);
    v->used = 1;
    v->state = SMP_DONE;
    v->loops = 1;
}

static u32 v_alloc(avoice_t *arr, u32 n, u32 tag) {
    for (u32 i = 0; i < n; i++)
        if (!arr[i].used) { v_init(&arr[i]); return tag + 4 * (i + 1); }
    return 0;
}

static u32 v_set_file(avoice_t *v, const void *img) {
    wav_t w;
    int ok = wav_parse((const u8 *)img, 0xFFFFFFF0u, 0, &w);
    v->file = (u32)(uintptr_t)img;
    v->wav_ok = (u8)ok;
    v->fmt = (u8)w.fmt;
    v->channels = (u16)w.channels;
    v->bits = (u16)w.bits;
    v->file_rate = v->rate = w.rate;
    v->block = w.block;
    v->data_len = w.data_len;
    v->frames = w.samples;
    v->pos0 = 0;
    v->state = SMP_DONE;
    return ok ? 1u : 0u;
}

static void v_stop(avoice_t *v, u64 now) {
    v_settle(v, now);
    if (v->state == SMP_PLAYING) { v->pos0 = v_played(v, now); v->state = SMP_STOPPED; }
}

static void v_resume(avoice_t *v, u64 now) {
    v_settle(v, now);
    if (v->state == SMP_DONE || v->state == SMP_STOPPED) { v->t0 = now; v->state = SMP_PLAYING; }
}

static void v_set_pos(avoice_t *v, u32 bytes, u64 now) {
    u64 f = v->data_len ? (u64)bytes * v->frames / v->data_len : 0;
    if (f > v->frames) f = v->frames;
    v->pos0 = f;
    v->t0 = now;
}

static u32 v_pos(avoice_t *v, u64 now) {
    v_settle(v, now);
    if (!v->frames || !v->data_len) return 0;
    u64 p = v_played(v, now);
    u64 f = (v->loops && p >= (u64)v->loops * v->frames) ? v->frames : p % v->frames;
    return (u32)(f * v->data_len / v->frames);
}

static void v_set_rate(avoice_t *v, u32 r, u64 now) {
    v_settle(v, now);
    if (v->state == SMP_PLAYING) { v->pos0 = v_played(v, now); v->t0 = now; }
    v->rate = r;
}

/* ---------------------------------------------------------------- model: streams */

static astream_t *stream(u32 h) {
    u32 i = ((h & 0xFFFFu) >> 2) - 1;
    if ((h & 0xFFFF0000u) == H_STREAM && i < AUD_NSTREAM && A.st[i].used) return &A.st[i];
    return 0;
}

static u64 s_played(const astream_t *s, u64 now) {
    if (s->state != SMP_PLAYING) return s->pos_us;
    return s->pos_us + (now > s->t0 ? now - s->t0 : 0);
}

static void s_settle(astream_t *s, u64 now) {
    if (s->state != SMP_PLAYING || !s->loops) return;
    u64 end = (u64)s->loops * s->total_us;
    if (s_played(s, now) >= end) { s->state = SMP_DONE; s->pos_us = end; }
}

static u64 s_cur_us(astream_t *s, u64 now) {
    s_settle(s, now);
    if (!s->total_us) return 0;
    u64 p = s_played(s, now);
    return (s->loops && p >= (u64)s->loops * s->total_us) ? s->total_us : p % s->total_us;
}

/* Where a stream's bytes are: the game hands Miles "\\<handle>,<length>[.ext]", the handle being one of
 * its own virtual file handles (vhandle.c) positioned at the data, or else a path. */
typedef struct { char path[264]; u64 off; u32 len; u32 vh; int handle_form; u32 prefix; const char *ext; } sname_t;

static int stream_name(const char *name, sname_t *n) {
    memset(n, 0, sizeof *n);
    if (!name || IsBadReadPtr(name, 1)) return 0;
    const char *p = name;
    while (*p == '\\') p++;
    n->prefix = (u32)(p - name);
    if (n->prefix >= 2 && *p >= '0' && *p <= '9') {
        const char *q;
        u32 h = (u32)k_parse_u64(p, &q);
        if (*q != ',') return 0;
        n->len = (u32)k_parse_u64(q + 1, &q);
        n->ext = q;
        n->handle_form = 1;
        n->vh = h;
        if (h < VH_BASE || h >= VH_BASE + 4 * NVH || (h & 3)) return 0;
        vh_t *e = &SH->vh[(h - VH_BASE) >> 2];
        if (e->type != VH_FILE) return 0;
        u32 l = (u32)strlen(e->path);
        if (l >= sizeof n->path) return 0;
        memcpy(n->path, e->path, l + 1);
        n->off = e->pos;
        return 1;
    }
    u32 l = (u32)strlen(name);
    if (l >= sizeof n->path) return 0;
    memcpy(n->path, name, l + 1);
    n->ext = name + l;
    return 1;
}

/* num / den rounded to nearest, ties to even (the x87 default) */
static u32 rdiv(u64 num, u64 den) {
    u64 q = num / den, r = num % den;
    return (u32)(2 * r > den || (2 * r == den && (q & 1)) ? q + 1 : q);
}

/* The length of one pass of a stream (decoded, which is when the real Miles stops playing it), the total
 * the real Miles reports for it, and the span of its data within the bytes Miles is given. The real total
 * is the data size over the byte rate: the WAV header's average byte rate, or the first MP3 frame's bit
 * rate (measured over all 15008 stream files of K1 GOG: native/probes/miles.c). */
typedef struct { u64 total_us; u32 total_ms, data_off, data_len, fmt; u8 id[8]; } sinfo_t;

static int stream_info(const sname_t *n, sinfo_t *si) {
    memset(si, 0, sizeof *si);
    HANDLE h = CreateFileA(n->path, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE, NULL, OPEN_EXISTING, 0, NULL);
    if (h == INVALID_HANDLE_VALUE) return 0;
    DWORD hi = 0, lo = GetFileSize(h, &hi);
    u64 size = (u64)hi << 32 | lo;
    src_t s = {0, h, n->off, 0, 0, 0};
    u64 len = size > n->off ? size - n->off : 0;
    if (n->handle_form && n->len < len) len = n->len;
    s.len = (u32)len;
    s.wlen = 0;
    int ok = 0;
    const u8 *b = src_at(&s, 0, s.len < 4096 ? s.len : 4096);
    if (b) {
        u8 dig[32];
        sha256(b, s.wlen < 65536 ? s.wlen : 65536, dig);
        memcpy(si->id, dig, 8);
    }
    wav_t w;
    /* the header is in the window; the data chunk runs to the end of the stream, which may be longer */
    if (b && s.len >= 12 && wav_parse(b, s.wlen, 0, &w)) {
        u32 smp = 0, rate = w.rate, ch = 0, fr = 0;
        si->fmt = w.fmt;
        si->data_off = w.data_off;
        si->data_len = s.len <= w.data_off ? 0 : w.data_len < s.len - w.data_off ? w.data_len : s.len - w.data_off;
        if (w.fmt == 0x55) {
            src_t m = {0, h, n->off + w.data_off, si->data_len, 0, 0};
            if (!mp3_scan(&m, &smp, &rate, &ch, &fr)) smp = 0;
        } else if (w.fmt == 1) {
            smp = w.block ? si->data_len / w.block : 0;
        } else if (w.fmt == 0x11) {
            smp = adpcm_samples(si->data_len, w.block, w.channels, w.spb);
        } else {
            smp = w.samples;
        }
        if (smp && rate) {
            si->total_us = (u64)smp * 1000000ull / rate;
            si->total_ms = w.avg ? rdiv((u64)si->data_len * 1000ull, w.avg) : (u32)(si->total_us / 1000ull);
            ok = 1;
        }
    } else if (s.len) {
        u32 smp = 0, rate = 0, ch = 0, fr = 0;
        s.woff = s.wlen = 0;
        if (mp3_scan(&s, &smp, &rate, &ch, &fr)) {
            si->fmt = 0x55;
            si->data_off = 0;
            si->data_len = s.len;
            si->total_us = (u64)smp * 1000000ull / rate;
            si->total_ms = g_mp3_kbps ? rdiv((u64)s.len * 8ull, g_mp3_kbps) : (u32)(si->total_us / 1000ull);
            ok = 1;
        }
    }
    CloseHandle(h);
    return ok;
}

/* ---------------------------------------------------------------- virtual imports */

#define CALL() (g_aud_n++)
#define MUT() (g_aud_n++, g_aud_ev++)

static u32 WINAPI v_quick_startup(u32 dig, u32 midi, u32 rate, u32 bits, u32 ch) {
    MUT();
    A.started = 1;
    A.startup[0] = dig; A.startup[1] = midi; A.startup[2] = rate; A.startup[3] = bits; A.startup[4] = ch;
    slog("audio: virtual quick_startup(%u,%u,%u,%u,%u) -> 1", dig, midi, rate, bits, ch);
    return 1;
}
static void WINAPI v_quick_shutdown(void) {
    MUT();
    A.started = 0;
}
static void WINAPI v_quick_handles(u32 *dig, u32 *midi, u32 *dls) {
    CALL();
    if (dig) *dig = A.started ? H_DRIVER : 0;
    if (midi) *midi = 0;
    if (dls) *dls = 0;
}
static const char *WINAPI v_set_redist_directory(const char *dir) {
    MUT();
    (void)dir;
    return "";
}
static u32 WINAPI v_set_preference(u32 n, u32 val) {
    MUT();
    u32 old = n < 8 ? A.prefs[n] : 0;
    if (n < 8) A.prefs[n] = val;
    return old;
}
static u32 WINAPI v_enumerate_3D_providers(u32 *next, u32 *dest, const char **name) {
    CALL();
    if (!next) return 0;
    u32 i = *next;
    if (i >= AUD_NPROV) return 0;
    if (dest) *dest = PROV_FIRST + i;
    if (name) *name = PROV_NAME[i];
    *next = i + 1;
    return 1;
}
static u32 prov_index(u32 p) {
    return p >= PROV_FIRST && p < PROV_FIRST + AUD_NPROV ? p - PROV_FIRST : 0xFFFFFFFFu;
}
static u32 WINAPI v_open_3D_provider(u32 p) {
    MUT();
    u32 i = prov_index(p);
    if (i == 0xFFFFFFFFu || !(g_aud_open_mask >> i & 1)) return M3D_FAIL;
    A.prov_open[i]++;
    return 0;
}
static void WINAPI v_close_3D_provider(u32 p) {
    MUT();
    u32 i = prov_index(p);
    if (i != 0xFFFFFFFFu && A.prov_open[i]) A.prov_open[i]--;
}
static u32 WINAPI v_set_3D_provider_preference(u32 p, const char *name, const void *val) {
    CALL();
    (void)p; (void)name; (void)val;
    return 1;
}
static void WINAPI v_set_3D_room_type(u32 p, u32 t) {
    MUT();
    u32 i = prov_index(p);
    if (i != 0xFFFFFFFFu) A.room[i] = t;   /* the real Miles answers 3D_room_type with it, EAX or not */
}
static u32 WINAPI v_3D_room_type(u32 p) {
    CALL();
    u32 i = prov_index(p);
    return i != 0xFFFFFFFFu ? A.room[i] : 0;
}
static void WINAPI v_set_digital_master_room_type(u32 dig, u32 t) {
    MUT();
    (void)dig;
    A.master_room = t;
}
static void WINAPI v_set_3D_rolloff_factor(u32 p, u32 f) {
    MUT();
    u32 i = prov_index(p);
    if (i != 0xFFFFFFFFu) A.rolloff[i] = f;
}
static u32 WINAPI v_open_3D_listener(u32 p) {
    CALL();
    return prov_index(p) != 0xFFFFFFFFu ? H_LISTENER : 0;
}
static void WINAPI v_close_3D_listener(u32 l) { CALL(); (void)l; }
static void WINAPI v_set_3D_position(u32 obj, u32 x, u32 y, u32 z) {
    MUT();
    avoice_t *v;
    if (obj == H_LISTENER) { A.listener[0] = x; A.listener[1] = y; A.listener[2] = z; }
    else if ((v = voice(obj))) { v->pos3[0] = x; v->pos3[1] = y; v->pos3[2] = z; }
}
static void WINAPI v_set_3D_orientation(u32 obj, u32 fx, u32 fy, u32 fz, u32 ux, u32 uy, u32 uz) {
    MUT();
    if (obj != H_LISTENER) return;
    A.listener[3] = fx; A.listener[4] = fy; A.listener[5] = fz;
    A.listener[6] = ux; A.listener[7] = uy; A.listener[8] = uz;
}

static u32 WINAPI v_allocate_sample_handle(u32 dig) {
    MUT();
    return dig == H_DRIVER && A.started ? v_alloc(A.v2, AUD_N2D, H_S2D) : 0;
}
static u32 WINAPI v_allocate_3D_sample_handle(u32 p) {
    MUT();
    u32 i = prov_index(p);
    return i != 0xFFFFFFFFu && A.prov_open[i] ? v_alloc(A.v3, AUD_N3D, H_S3D) : 0;
}
static void WINAPI v_release_sample_handle(u32 s) { MUT(); avoice_t *v = voice(s); if (v) memset(v, 0, sizeof *v); }
static void WINAPI v_release_3D_sample_handle(u32 s) { MUT(); avoice_t *v = voice(s); if (v) memset(v, 0, sizeof *v); }
static void WINAPI v_init_sample(u32 s) { MUT(); avoice_t *v = voice(s); if (v) v_init(v); }
static u32 WINAPI v_set_sample_file(u32 s, const void *img, u32 block) {
    MUT();
    (void)block;
    avoice_t *v = voice(s);
    return v ? v_set_file(v, img) : 0;
}
static u32 WINAPI v_set_3D_sample_file(u32 s, const void *img) {
    MUT();
    avoice_t *v = voice(s);
    return v ? v_set_file(v, img) : 0;
}
static void WINAPI v_set_sample_loop_count(u32 s, u32 n) { MUT(); avoice_t *v = voice(s); if (v) { v_settle(v, now_us()); v->loops = n; } }
static void WINAPI v_set_3D_sample_loop_count(u32 s, u32 n) { MUT(); avoice_t *v = voice(s); if (v) { v_settle(v, now_us()); v->loops = n; } }
static void WINAPI v_set_sample_playback_rate(u32 s, u32 r) { MUT(); avoice_t *v = voice(s); if (v) v_set_rate(v, r, now_us()); }
static void WINAPI v_set_3D_sample_playback_rate(u32 s, u32 r) { MUT(); avoice_t *v = voice(s); if (v) v_set_rate(v, r, now_us()); }
static void WINAPI v_set_sample_position(u32 s, u32 b) { MUT(); avoice_t *v = voice(s); if (v) v_set_pos(v, b, now_us()); }
static void WINAPI v_set_3D_sample_offset(u32 s, u32 b) { MUT(); avoice_t *v = voice(s); if (v) v_set_pos(v, b, now_us()); }
static u32 WINAPI v_sample_position(u32 s) { CALL(); avoice_t *v = voice(s); return v ? v_pos(v, now_us()) : 0; }
static u32 WINAPI v_3D_sample_offset(u32 s) { CALL(); avoice_t *v = voice(s); return v ? v_pos(v, now_us()) : 0; }
static void WINAPI v_set_sample_volume_levels(u32 s, u32 l, u32 r) { MUT(); avoice_t *v = voice(s); if (v) { v->vol[0] = l; v->vol[1] = r; } }
static void WINAPI v_set_3D_sample_volume(u32 s, u32 vol) { MUT(); avoice_t *v = voice(s); if (v) v->vol[0] = vol; }
static void WINAPI v_set_3D_sample_distances(u32 s, u32 mx, u32 mn) { MUT(); avoice_t *v = voice(s); if (v) { v->dist[0] = mx; v->dist[1] = mn; } }
static void WINAPI v_set_3D_sample_effects_level(u32 s, u32 l) { MUT(); avoice_t *v = voice(s); if (v) v->fx = l; }
static void WINAPI v_set_3D_sample_obstruction(u32 s, u32 o) { MUT(); avoice_t *v = voice(s); if (v) v->obst = o; }
static void WINAPI v_resume_sample(u32 s) { MUT(); avoice_t *v = voice(s); if (v) v_resume(v, now_us()); }
static void WINAPI v_resume_3D_sample(u32 s) { MUT(); avoice_t *v = voice(s); if (v) v_resume(v, now_us()); }
static void WINAPI v_stop_sample(u32 s) { MUT(); avoice_t *v = voice(s); if (v) v_stop(v, now_us()); }
static void WINAPI v_stop_3D_sample(u32 s) { MUT(); avoice_t *v = voice(s); if (v) v_stop(v, now_us()); }
static void v_end(avoice_t *v) { v->state = SMP_DONE; v->pos0 = (u64)v->loops * v->frames; }
static void WINAPI v_end_sample(u32 s) { MUT(); avoice_t *v = voice(s); if (v) v_end(v); }
static void WINAPI v_end_3D_sample(u32 s) { MUT(); avoice_t *v = voice(s); if (v) v_end(v); }
static u32 WINAPI v_sample_status(u32 s) {
    CALL();
    avoice_t *v = voice(s);
    if (!v) return SMP_FREE;
    v_settle(v, now_us());
    return v->state;
}
static u32 WINAPI v_3D_sample_status(u32 s) {
    CALL();
    avoice_t *v = voice(s);
    if (!v) return SMP_FREE;
    v_settle(v, now_us());
    return v->state;
}

static u32 WINAPI v_open_stream(u32 dig, const char *name, u32 mem) {
    MUT();
    (void)mem;
    sname_t n;
    sinfo_t si;
    u32 i;
    if (dig != H_DRIVER || !A.started) return 0;
    A.opens++;
    if (!stream_name(name, &n) || !stream_info(&n, &si)) {
        A.open_fails++;
        slog("audio: open_stream %s: no length (path %s)", name ? name : "(null)", n.path);
        return 0;
    }
    for (i = 0; i < AUD_NSTREAM && A.st[i].used; i++) {}
    if (i == AUD_NSTREAM) { A.open_fails++; slog("audio: stream table full"); return 0; }
    astream_t *s = &A.st[i];
    memset(s, 0, sizeof *s);
    s->used = 1;
    s->state = SMP_DONE;
    s->fmt = (u8)si.fmt;
    s->loops = 1;
    s->total_us = si.total_us;
    s->total_ms = si.total_ms;
    s->data_off = si.data_off;
    s->data_len = si.data_len;
    s->vh = n.vh;
    memcpy(s->id, si.id, 8);
    slog("audio: open_stream %s (%s @%u) -> stream %u, fmt %x, plays %u ms, reports %u ms", name, n.path,
         (u32)n.off, i, si.fmt, (u32)(si.total_us / 1000ull), s->total_ms);
    return H_STREAM + 4 * (i + 1);
}
static void WINAPI v_close_stream(u32 h) { MUT(); astream_t *s = stream(h); if (s) memset(s, 0, sizeof *s); }
static void WINAPI v_start_stream(u32 h) {
    MUT();
    astream_t *s = stream(h);
    if (!s) return;
    s->pos_us = 0;
    s->t0 = now_us();
    s->state = SMP_PLAYING;
}
static void WINAPI v_pause_stream(u32 h, u32 on) {
    MUT();
    astream_t *s = stream(h);
    if (!s) return;
    u64 now = now_us();
    s_settle(s, now);
    if (on) {
        if (s->state == SMP_PLAYING) { s->pos_us = s_played(s, now); s->state = SMP_STOPPED; }
    } else if (s->state != SMP_PLAYING) {
        s->t0 = now;
        s->state = SMP_PLAYING;
    }
}
static void WINAPI v_set_stream_loop_count(u32 h, u32 n) { MUT(); astream_t *s = stream(h); if (s) { s_settle(s, now_us()); s->loops = n; } }
static void WINAPI v_set_stream_position(u32 h, u32 off) {
    MUT();
    astream_t *s = stream(h);
    if (!s) return;
    u32 d = off > s->data_off ? off - s->data_off : 0;
    if (d > s->data_len) d = s->data_len;
    s->pos_us = s->data_len ? (u64)d * s->total_us / s->data_len : 0;
    s->t0 = now_us();
}
static u32 WINAPI v_stream_position(u32 h) {
    CALL();
    astream_t *s = stream(h);
    if (!s || !s->total_us) return 0;
    return s->data_off + (u32)(s_cur_us(s, now_us()) * s->data_len / s->total_us);
}
static u32 WINAPI v_stream_status(u32 h) {
    CALL();
    astream_t *s = stream(h);
    if (!s) return SMP_FREE;
    s_settle(s, now_us());
    return s->state;
}
static void WINAPI v_stream_ms_position(u32 h, i32 *total, i32 *cur) {
    CALL();
    astream_t *s = stream(h);
    if (total) *total = s ? (i32)s->total_ms : 0;
    if (cur) *cur = s ? (i32)(s_cur_us(s, now_us()) / 1000ull) : 0;
}
static void WINAPI v_set_stream_volume_levels(u32 h, u32 l, u32 r) { MUT(); astream_t *s = stream(h); if (s) { s->vol[0] = l; s->vol[1] = r; } }
static void WINAPI v_set_stream_volume_pan(u32 h, u32 vol, u32 pan) { MUT(); astream_t *s = stream(h); if (s) { s->volpan[0] = vol; s->volpan[1] = pan; } }
static void WINAPI v_set_stream_reverb_levels(u32 h, u32 dry, u32 wet) { MUT(); astream_t *s = stream(h); if (s) { s->reverb[0] = dry; s->reverb[1] = wet; } }

/* AILSOUNDINFO (Miles 6) */
typedef struct { i32 format; const void *data_ptr; u32 data_len, rate; i32 bits, channels; u32 samples, block_size; const void *initial_ptr; } ailinfo_t;

static u32 WINAPI v_WAV_info(const void *data, ailinfo_t *info) {
    CALL();
    wav_t w;
    if (!info || !wav_parse((const u8 *)data, 0xFFFFFFF0u, 0, &w)) return 0;
    info->format = (i32)w.fmt;
    info->data_ptr = (const u8 *)data + w.data_off;
    info->data_len = w.data_len;
    info->rate = w.rate;
    info->bits = (i32)w.bits;
    info->channels = (i32)w.channels;
    info->samples = w.samples;
    info->block_size = w.block;
    info->initial_ptr = info->data_ptr;
    return 1;
}

/* A 16-bit PCM WAV image of `samples` frames with zeroed samples, on the exe's heap. */
static void *canon_wav(u32 rate, u32 ch, u32 samples, u32 *size) {
    u32 dl = samples * ch * 2, n = 44 + dl;
    u8 *p = (u8 *)((malloc_fn)(uintptr_t)EXE_MALLOC)(n);
    if (!p) return 0;
    memset(p, 0, n);
    memcpy(p, "RIFF", 4); wr32(p + 4, n - 8); memcpy(p + 8, "WAVEfmt ", 8); wr32(p + 16, 16);
    wr16(p + 20, 1); wr16(p + 22, ch); wr32(p + 24, rate); wr32(p + 28, rate * ch * 2); wr16(p + 32, ch * 2);
    wr16(p + 34, 16); memcpy(p + 36, "data", 4); wr32(p + 40, dl);
    A.decodes++;
    A.decode_bytes += n;
    *size = n;
    return p;
}

static int ext_mp3(const char *ext) {
    if (!ext || IsBadReadPtr(ext, 1)) return 0;
    u32 l = (u32)strlen(ext);
    return l >= 3 && k_strieq(ext + l - 3, "mp3");
}

/* canonical decode sizes, shared with the shadow log */
static int asi_size(const void *in, u32 insize, u32 *rate, u32 *ch, u32 *samples) {
    src_t s = {(const u8 *)in, 0, 0, insize, 0, 0};
    u32 fr = 0;
    if (!in || IsBadReadPtr(in, insize)) return 0;
    return mp3_scan(&s, samples, rate, ch, &fr);
}

static u32 WINAPI v_decompress_ASI(const void *in, u32 insize, const char *ext, void **out, u32 *outsize, void *cb) {
    MUT();
    (void)cb;
    u32 rate = 0, ch = 0, smp = 0, n = 0;
    if (!out || !outsize || !ext_mp3(ext) || !asi_size(in, insize, &rate, &ch, &smp)) return 0;
    void *p = canon_wav(rate, ch, smp, &n);
    if (!p) return 0;
    *out = p;
    *outsize = n;
    return 1;
}
static u32 WINAPI v_decompress_ADPCM(const ailinfo_t *info, void **out, u32 *outsize) {
    MUT();
    u32 n = 0;
    if (!info || !out || !outsize || info->format != 0x11) return 0;
    u32 smp = adpcm_samples(info->data_len, info->block_size, (u32)info->channels, 0);
    void *p = canon_wav(info->rate, (u32)info->channels, smp, &n);
    if (!p) return 0;
    *out = p;
    *outsize = n;
    return 1;
}
static void WINAPI v_mem_free_lock(void *p) {
    MUT();
    if (p) ((free_fn)(uintptr_t)EXE_FREE)(p);
}

/* ---------------------------------------------------------------- Bink */

static u32(WINAPI *r_BinkSetSoundTrack)(u32, u32 *);
static void *(WINAPI *r_BinkOpen)(const char *, u32);
#define BINKSNDTRACK 0x00004000u

static u32 WINAPI v_BinkSetSoundSystem(void *open, u32 param) {
    slog("audio: BinkSetSoundSystem(%x, %x) not passed on: the virtual driver is not a Miles driver",
         (u32)(uintptr_t)open, param);
    return 1;
}
static void *WINAPI v_BinkOpen(const char *name, u32 flags) {
    if (r_BinkSetSoundTrack) {
        r_BinkSetSoundTrack(0, 0);
        flags |= BINKSNDTRACK;
    }
    void *b = r_BinkOpen(name, flags);
    slog("audio: BinkOpen(%s, %x) with no sound track -> %x", name ? name : "", flags, (u32)(uintptr_t)b);
    return b;
}

/* ---------------------------------------------------------------- real mode (lab) */

static u32(WINAPI *r_open_stream)(u32, const char *, u32);
static void(WINAPI *r_close_stream)(u32);
static u32(WINAPI *r_quick_startup)(u32, u32, u32, u32, u32);
static u32(WINAPI *r_enumerate_3D_providers)(u32 *, u32 *, const char **);
static u32(WINAPI *r_open_3D_provider)(u32);
static u32(WINAPI *r_WAV_info)(const void *, ailinfo_t *);
static u32(WINAPI *r_decompress_ASI)(const void *, u32, const char *, void **, u32 *, void *);
static u32(WINAPI *r_decompress_ADPCM)(const ailinfo_t *, void **, u32 *);
static void(WINAPI *r_stream_ms_position)(u32, i32 *, i32 *);
static u32(WINAPI *r_sample_status)(u32);
static u32(WINAPI *r_3D_sample_status)(u32);
static u32(WINAPI *r_stream_status)(u32);
static u32(WINAPI *r_3D_room_type)(u32);

#define SHADOW_ST 64
/* Passive diagnostics: independent per-lifetime budgets retain status and
 * close after a position flood. A process position cap reserves 1024 events
 * for status/lifecycle; the global cap still makes all later evidence missing.
 * Each saturated lane/table emits one explicit diagnostic marker. */
#define SHADOW_EVENTS 4096u
#define SHADOW_POSITION_EVENTS 3072u
#define SHADOW_PER_STREAM 64u
 enum { SH_POSITION, SH_STATUS, SH_LIFECYCLE, SH_LANES };
static struct {
    u32 h, model_ms, events[SH_LANES], have_status, status, have_pos;
    i32 total, cur, bucket;
    char name[64];
} g_sh_st[SHADOW_ST];
static u32 g_sh_events, g_sh_positions, g_sh_serial, g_sh_table_full, g_sh_open_details;

static void shadow_table_full(u32 h) {
    DWORD le = GetLastError();
    if (!g_sh_table_full) {
        g_sh_table_full = 1;
        slog("audio shadow missing: stream table full capacity %u untracked_handle %x", SHADOW_ST, h);
    }
    SetLastError(le);
}
static void shadow_observe(u32 i, u32 lane, const char *kind, i32 a, i32 b) {
    DWORD le = GetLastError();
    u32 limit = lane == SH_LIFECYCLE ? 2u : SHADOW_PER_STREAM;
    if (g_sh_events < SHADOW_EVENTS && g_sh_st[i].events[lane] < limit &&
        (lane != SH_POSITION || g_sh_positions < SHADOW_POSITION_EVENTS)) {
        u64 us = now_us(), tick = g_gate.tick;
        g_sh_events++;
        g_sh_st[i].events[lane]++;
        if (lane == SH_POSITION) g_sh_positions++;
        slog("audio shadow event: seq %u stream %x asset %s kind %s a %d b %d model_ms %u tick_hi %u tick_lo %u us_hi %u us_lo %u",
             ++g_sh_serial, g_sh_st[i].h, g_sh_st[i].name, kind, a, b,
             g_sh_st[i].model_ms, (u32)(tick >> 32), (u32)tick, (u32)(us >> 32), (u32)us);
        if (lane != SH_LIFECYCLE && g_sh_st[i].events[lane] == limit)
            slog("audio shadow missing: stream %x lane %u saturated limit %u", g_sh_st[i].h, lane, limit);
        if (lane == SH_POSITION && g_sh_positions == SHADOW_POSITION_EVENTS)
            slog("audio shadow missing: process position saturated limit %u", SHADOW_POSITION_EVENTS);
        if (g_sh_events == SHADOW_EVENTS)
            slog("audio shadow missing: process all lanes saturated limit %u", SHADOW_EVENTS);
    }
    SetLastError(le);
}
static u32 g_sh_codes[4];

/* The game's stream name carries one of its virtual file handles, which the real Miles cannot read: give
 * it a real handle to the same file at the same position. Miles leaves that handle open when the stream
 * closes (native/probes/miles.c), so close_stream closes it. */
#define REAL_ST 64
static struct { u32 s; HANDLE h; } g_real_st[REAL_ST];
static u32 WINAPI x_open_stream(u32 dig, const char *name, u32 mem) {
    sname_t n{};
    sinfo_t si{};
    char buf[320];
    const char *use = name;
    int named = stream_name(name, &n);
    if (named && n.handle_form) {
        HANDLE h = CreateFileA(n.path, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE, NULL, OPEN_EXISTING, 0, NULL);
        if (h != INVALID_HANDLE_VALUE) {
            LONG hi = (LONG)(n.off >> 32);
            SetFilePointer(h, (LONG)(u32)n.off, &hi, FILE_BEGIN);
            buf_t b;
            b_init(&b, buf, sizeof buf);
            for (u32 i = 0; i < n.prefix; i++) b_ch(&b, '\\');
            b_u64(&b, (u32)(uintptr_t)h);
            b_ch(&b, ',');
            b_u64(&b, n.len);
            b_str(&b, n.ext);
            b_term(&b);
            use = buf;
        }
    }
    u32 s = r_open_stream(dig, use, mem);
    if (use != name) {
        u32 h = (u32)k_parse_u64(use + n.prefix, 0), i;
        for (i = 0; s && i < REAL_ST && g_real_st[i].s; i++) {}
        if (s && i < REAL_ST) { g_real_st[i].s = s; g_real_st[i].h = (HANDLE)(uintptr_t)h; }
        else CloseHandle((HANDLE)(uintptr_t)h);
    }
    if (g_aud_shadow) {
        int ok = named && stream_info(&n, &si);
        if (g_sh_open_details < SHADOW_ST) { g_sh_open_details++; slog("audio shadow: open_stream %s (%s @%u len %u) -> %x; model %s total_ms %u fmt %x", name ? name : "",
             named ? n.path : "?", (u32)n.off, n.len, s, ok ? "ok" : "none", ok ? si.total_ms : 0, si.fmt); }
        u32 i;
        for (i = 0; s && i < SHADOW_ST; i++)
            if (!g_sh_st[i].h || g_sh_st[i].h == s) {
                g_sh_st[i].h = s;
                g_sh_st[i].model_ms = ok ? si.total_ms : 0xFFFFFFFFu;
                memset(g_sh_st[i].events, 0, sizeof g_sh_st[i].events);
                g_sh_st[i].have_status = g_sh_st[i].have_pos = 0;
                u32 l = named ? (u32)strlen(n.path) : 0, k = l > 63 ? l - 63 : 0;
                memcpy(g_sh_st[i].name, named ? n.path + k : "", l - k + 1);
                shadow_observe(i, SH_LIFECYCLE, "open", 0, 0);
                break;
            }
        if (s && i == SHADOW_ST) shadow_table_full(s);
    }
    return s;
}

static void WINAPI x_close_stream(u32 s) {
    r_close_stream(s);
    if (g_aud_shadow) for (u32 i = 0; i < SHADOW_ST; i++)
        if (s && g_sh_st[i].h == s) {
            shadow_observe(i, SH_LIFECYCLE, "close", 0, 0);
            g_sh_st[i].h = 0;
            break;
        }
    for (u32 i = 0; i < REAL_ST; i++)
        if (s && g_real_st[i].s == s) { CloseHandle(g_real_st[i].h); g_real_st[i].s = 0; g_real_st[i].h = 0; }
}

static u32 WINAPI x_quick_startup(u32 dig, u32 midi, u32 rate, u32 bits, u32 ch) {
    u32 r = r_quick_startup(dig, midi, rate, bits, ch);
    slog("audio shadow: quick_startup(%u,%u,%u,%u,%u) -> %u", dig, midi, rate, bits, ch, r);
    return r;
}
static u32 WINAPI x_enumerate_3D_providers(u32 *next, u32 *dest, const char **name) {
    u32 r = r_enumerate_3D_providers(next, dest, name);
    slog("audio shadow: enumerate_3D_providers -> %u: %x \"%s\"", r, r && dest ? *dest : 0, r && name && *name ? *name : "");
    return r;
}
static u32 WINAPI x_open_3D_provider(u32 p) {
    u32 r = r_open_3D_provider(p);
    slog("audio shadow: open_3D_provider(%x) -> %u", p, r);
    return r;
}
static u32 WINAPI x_3D_room_type(u32 p) {
    u32 r = r_3D_room_type(p);
    static u32 last = 0xFFFFFFF0u;
    if (r != last) slog("audio shadow: 3D_room_type(%x) -> %d", p, (i32)r);
    last = r;
    return r;
}
static u32 WINAPI x_WAV_info(const void *data, ailinfo_t *info) {
    u32 r = r_WAV_info(data, info);
    ailinfo_t m;
    memset(&m, 0, sizeof m);
    u32 mr = v_WAV_info(data, &m);
    g_aud_n--;
    if (r != mr || (r && (info->format != m.format || info->data_len != m.data_len || info->rate != m.rate ||
                          info->bits != m.bits || info->channels != m.channels || info->samples != m.samples ||
                          info->block_size != m.block_size || info->data_ptr != m.data_ptr)))
        slog("audio shadow: WAV_info differs: real %u fmt %x len %u rate %u bits %d ch %d smp %u blk %u off %u | "
             "model %u fmt %x len %u rate %u bits %d ch %d smp %u blk %u off %u", r, info->format, info->data_len,
             info->rate, info->bits, info->channels, info->samples, info->block_size,
             (u32)((const u8 *)info->data_ptr - (const u8 *)data), mr, m.format, m.data_len, m.rate, m.bits,
             m.channels, m.samples, m.block_size, (u32)((const u8 *)m.data_ptr - (const u8 *)data));
    return r;
}
static void shadow_out(const char *what, u32 r, void *out, u32 size, u32 mrate, u32 mch, u32 msmp, u32 mok) {
    wav_t w;
    int ok = r && out && wav_parse((const u8 *)out, size, 1, &w);
    u32 msize = 44 + msmp * mch * 2;
    if (!ok || !mok || size != msize || w.rate != mrate || w.channels != mch || w.samples != msmp)
        slog("audio shadow: %s differs: real %u size %u rate %u ch %u smp %u data_off %u | model %u size %u rate %u "
             "ch %u smp %u", what, r, size, ok ? w.rate : 0, ok ? w.channels : 0, ok ? w.samples : 0,
             ok ? w.data_off : 0, mok, msize, mrate, mch, msmp);
}
static u32 WINAPI x_decompress_ASI(const void *in, u32 insize, const char *ext, void **out, u32 *outsize, void *cb) {
    u32 rate = 0, ch = 0, smp = 0;
    u32 mok = ext_mp3(ext) && asi_size(in, insize, &rate, &ch, &smp);
    u32 r = r_decompress_ASI(in, insize, ext, out, outsize, cb);
    shadow_out("decompress_ASI", r, out ? *out : 0, outsize ? *outsize : 0, rate, ch, smp, mok);
    return r;
}
static u32 WINAPI x_decompress_ADPCM(const ailinfo_t *info, void **out, u32 *outsize) {
    u32 smp = info ? adpcm_samples(info->data_len, info->block_size, (u32)info->channels, 0) : 0;
    u32 r = r_decompress_ADPCM(info, out, outsize);
    shadow_out("decompress_ADPCM", r, out ? *out : 0, outsize ? *outsize : 0, info ? info->rate : 0,
               info ? (u32)info->channels : 0, smp, info && info->format == 0x11);
    return r;
}
static void WINAPI x_stream_ms_position(u32 h, i32 *total, i32 *cur) {
    /* Keep pointers, including NULL, exactly as supplied by the engine. */
    r_stream_ms_position(h, total, cur);
    for (u32 i = 0; h && i < SHADOW_ST; i++)
        if (g_sh_st[i].h == h) {
            i32 t = total ? *total : 0, c = cur ? *cur : 0;
            i32 bucket = c / 25;
            u32 outputs = (total ? 1u : 0u) | (cur ? 2u : 0u);
            if (!g_sh_st[i].have_pos || (outputs & ~g_sh_st[i].have_pos) ||
                (total && t != g_sh_st[i].total) ||
                (cur && (bucket != g_sh_st[i].bucket || c < g_sh_st[i].cur))) {
                shadow_observe(i, SH_POSITION, total ? (cur ? "position_total_cur" : "position_total") :
                               (cur ? "position_cur" : "position_no_outputs"), t, c);
            }
            g_sh_st[i].have_pos |= outputs | 4u;
            if (total) g_sh_st[i].total = t;
            if (cur) { g_sh_st[i].cur = c; g_sh_st[i].bucket = bucket; }
            break;
        }
}
static void census(u32 k, const char *what, u32 h, u32 r) {
    if (r < 32 && !(g_sh_codes[k] >> r & 1)) {
        g_sh_codes[k] |= 1u << r;
        slog("audio shadow: %s(%x) first returns %u", what, h, r);
    }
}
static u32 WINAPI x_sample_status(u32 h) { u32 r = r_sample_status(h); census(0, "sample_status", h, r); return r; }
static u32 WINAPI x_3D_sample_status(u32 h) { u32 r = r_3D_sample_status(h); census(1, "3D_sample_status", h, r); return r; }
static u32 WINAPI x_stream_status(u32 h) {
    u32 r = r_stream_status(h);
    for (u32 i = 0; h && i < SHADOW_ST; i++)
        if (g_sh_st[i].h == h) {
            if (!g_sh_st[i].have_status || g_sh_st[i].status != r)
                shadow_observe(i, SH_STATUS, "status", (i32)r, 0);
            g_sh_st[i].status = r;
            g_sh_st[i].have_status = 1;
            break;
        }
    census(2, "stream_status", h, r);
    return r;
}

/* ---------------------------------------------------------------- install */

/* Every MSS32 import of the exe: decorated name, the model's answer, and the lab hook (or 0). The @N
 * argument bytes must match the hook's parameter list (all 4-byte values; floats are kept as bits). */
#define AIL(name, n, lab, real) {"_AIL_" #name "@" #n, (void *)v_##name, (void *)(lab), (void **)(real)}
static const struct { const char *name; void *virt, *lab; void **real; } AIL_TABLE[] = {
    AIL(3D_room_type, 4, x_3D_room_type, &r_3D_room_type),
    AIL(3D_sample_offset, 4, 0, 0),
    AIL(3D_sample_status, 4, x_3D_sample_status, &r_3D_sample_status),
    AIL(allocate_3D_sample_handle, 4, 0, 0),
    AIL(allocate_sample_handle, 4, 0, 0),
    AIL(close_3D_listener, 4, 0, 0),
    AIL(close_3D_provider, 4, 0, 0),
    AIL(close_stream, 4, x_close_stream, &r_close_stream),
    AIL(decompress_ADPCM, 12, x_decompress_ADPCM, &r_decompress_ADPCM),
    AIL(decompress_ASI, 24, x_decompress_ASI, &r_decompress_ASI),
    AIL(end_3D_sample, 4, 0, 0),
    AIL(end_sample, 4, 0, 0),
    AIL(enumerate_3D_providers, 12, x_enumerate_3D_providers, &r_enumerate_3D_providers),
    AIL(init_sample, 4, 0, 0),
    AIL(mem_free_lock, 4, 0, 0),
    AIL(open_3D_listener, 4, 0, 0),
    AIL(open_3D_provider, 4, x_open_3D_provider, &r_open_3D_provider),
    AIL(open_stream, 12, x_open_stream, &r_open_stream),
    AIL(pause_stream, 8, 0, 0),
    AIL(quick_handles, 12, 0, 0),
    AIL(quick_shutdown, 0, 0, 0),
    AIL(quick_startup, 20, x_quick_startup, &r_quick_startup),
    AIL(release_3D_sample_handle, 4, 0, 0),
    AIL(release_sample_handle, 4, 0, 0),
    AIL(resume_3D_sample, 4, 0, 0),
    AIL(resume_sample, 4, 0, 0),
    AIL(sample_position, 4, 0, 0),
    AIL(sample_status, 4, x_sample_status, &r_sample_status),
    AIL(set_3D_orientation, 28, 0, 0),
    AIL(set_3D_position, 16, 0, 0),
    AIL(set_3D_provider_preference, 12, 0, 0),
    AIL(set_3D_room_type, 8, 0, 0),
    AIL(set_3D_rolloff_factor, 8, 0, 0),
    AIL(set_3D_sample_distances, 12, 0, 0),
    AIL(set_3D_sample_effects_level, 8, 0, 0),
    AIL(set_3D_sample_file, 8, 0, 0),
    AIL(set_3D_sample_loop_count, 8, 0, 0),
    AIL(set_3D_sample_obstruction, 8, 0, 0),
    AIL(set_3D_sample_offset, 8, 0, 0),
    AIL(set_3D_sample_playback_rate, 8, 0, 0),
    AIL(set_3D_sample_volume, 8, 0, 0),
    AIL(set_digital_master_room_type, 8, 0, 0),
    AIL(set_preference, 8, 0, 0),
    AIL(set_redist_directory, 4, 0, 0),
    AIL(set_sample_file, 12, 0, 0),
    AIL(set_sample_loop_count, 8, 0, 0),
    AIL(set_sample_playback_rate, 8, 0, 0),
    AIL(set_sample_position, 8, 0, 0),
    AIL(set_sample_volume_levels, 12, 0, 0),
    AIL(set_stream_loop_count, 8, 0, 0),
    AIL(set_stream_position, 8, 0, 0),
    AIL(set_stream_reverb_levels, 12, 0, 0),
    AIL(set_stream_volume_levels, 12, 0, 0),
    AIL(set_stream_volume_pan, 12, 0, 0),
    AIL(start_stream, 4, 0, 0),
    AIL(stop_3D_sample, 4, 0, 0),
    AIL(stop_sample, 4, 0, 0),
    AIL(stream_ms_position, 12, x_stream_ms_position, &r_stream_ms_position),
    AIL(stream_position, 4, 0, 0),
    AIL(stream_status, 4, x_stream_status, &r_stream_status),
    AIL(WAV_info, 8, x_WAV_info, &r_WAV_info),
};
#define AIL_COUNT (sizeof AIL_TABLE / sizeof AIL_TABLE[0])

/* Virtual: hook all 61 imports or none, so a partial install can never leave the real Miles answering
 * some calls. Real: the stream-name fix always, the shadow log when asked. */
void audio_install(void) {
    HMODULE exe = GetModuleHandleA(NULL);
    u32 *slot[AIL_COUNT];
    u32 found = 0;
    if (g_aud_mode == AUD_NONE) return;
    for (u32 i = 0; i < AIL_COUNT; i++)
        if ((slot[i] = iat_slot(exe, "MSS32.dll", AIL_TABLE[i].name))) found++;
        else slog("audio: import %s not found", AIL_TABLE[i].name);
    if (g_aud_mode == AUD_VIRTUAL) {
        if (found != AIL_COUNT || AIL_COUNT != 61) {
            slog("audio: %u of %u Miles imports found; the virtual device is off", found, (u32)AIL_COUNT);
            g_aud_mode = AUD_NONE;
            return;
        }
        for (u32 i = 0; i < AIL_COUNT; i++)
            if (iat_set(slot[i], AIL_TABLE[i].virt)) g_aud_hooked++;
        HMODULE bink = GetModuleHandleA("binkw32.dll");
        r_BinkSetSoundTrack = bink ? (u32(WINAPI *)(u32, u32 *))(void *)GetProcAddress(bink, "_BinkSetSoundTrack@8") : 0;
        u32 *sss = iat_slot(exe, "binkw32.dll", "_BinkSetSoundSystem@8");
        u32 *bo = iat_slot(exe, "binkw32.dll", "_BinkOpen@8");
        if (sss && bo) {
            r_BinkOpen = (void *(WINAPI *)(const char *, u32))(uintptr_t)*bo;
            iat_set(sss, (void *)v_BinkSetSoundSystem);
            iat_set(bo, (void *)v_BinkOpen);
            hooks_note("exe:BinkSetSoundSystem");
            hooks_note("exe:BinkOpen");
        }
        hooks_note("exe:AIL*61");
        slog("audio: virtual device, %u Miles imports hooked, provider %s (open mask %x), output null, Bink sound "
             "track %s", g_aud_hooked, g_aud_provider, g_aud_open_mask, r_BinkSetSoundTrack ? "off" : "untouched");
        return;
    }
    for (u32 i = 0; i < AIL_COUNT; i++) {
        if (!slot[i] || !AIL_TABLE[i].lab || !AIL_TABLE[i].real) continue;
        if (!g_aud_shadow && AIL_TABLE[i].real != (void **)&r_open_stream && AIL_TABLE[i].real != (void **)&r_close_stream)
            continue;
        *AIL_TABLE[i].real = (void *)(uintptr_t)*slot[i];
        if (iat_set(slot[i], AIL_TABLE[i].lab)) g_aud_hooked++;
    }
    slog("audio: real device (lab), %u Miles imports hooked%s", g_aud_hooked, g_aud_shadow ? ", shadow log on" : "");
}

/* ---------------------------------------------------------------- journal and caps */

void aud_tick_reset(void) {
    g_aud_n = 0;
    g_aud_ev = 0;
}

/* `"aud":{"d":...,"ev":...,"n":...},` (the entry's first key) when the virtual device is on. d is a digest
 * of the device model with the WAV image addresses left out. */
void aud_journal(buf_t *b) {
    static aud_hg_t c;
    u8 d[16];
    if (g_aud_mode != AUD_VIRTUAL) return;
    memcpy(&c, &A, sizeof c);
    for (u32 i = 0; i < AUD_N2D; i++) c.v2[i].file = 0;
    for (u32 i = 0; i < AUD_N3D; i++) c.v3[i].file = 0;
    blake2s128(&c, sizeof c, d);
    b_str(b, "\"aud\":{\"d\":\"");
    b_hex(b, d, 16);
    b_str(b, "\",\"ev\":");
    b_u64(b, g_aud_ev);
    b_str(b, ",\"n\":");
    b_u64(b, g_aud_n);
    b_str(b, "},");
}

void aud_caps(buf_t *b) {
    b_str(b, ",\"audio\":{\"device\":\"");
    b_str(b, g_aud_mode == AUD_VIRTUAL ? "virtual" : g_aud_mode == AUD_REAL ? "real" : "none");
    b_str(b, "\",\"hooked\":");
    b_u64(b, g_aud_hooked);
    if (g_aud_mode == AUD_VIRTUAL) {
        b_str(b, ",\"output\":\"null\",\"provider\":\"");
        b_str(b, g_aud_provider);
        b_ch(b, '"');
    }
    if (g_aud_mode == AUD_REAL) b_str(b, g_aud_shadow ? ",\"shadow\":true" : ",\"shadow\":false");
    b_ch(b, '}');
}

} /* extern "C" */
