/* BLAKE2s (RFC 7693) and SHA-256 (FIPS 180-4), freestanding.
 * blake2s128 must match hashlib.blake2s(data, digest_size=16). */
#include "shim.hpp"

static const u32 B2S_IV[8] = {
    0x6A09E667u, 0xBB67AE85u, 0x3C6EF372u, 0xA54FF53Au,
    0x510E527Fu, 0x9B05688Cu, 0x1F83D9ABu, 0x5BE0CD19u,
};
static const u8 B2S_SIGMA[10][16] = {
    {0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15},
    {14, 10, 4, 8, 9, 15, 13, 6, 1, 12, 0, 2, 11, 7, 5, 3},
    {11, 8, 12, 0, 5, 2, 15, 13, 10, 14, 3, 6, 7, 1, 9, 4},
    {7, 9, 3, 1, 13, 12, 11, 14, 2, 6, 5, 10, 4, 0, 15, 8},
    {9, 0, 5, 7, 2, 4, 10, 15, 14, 1, 11, 12, 6, 8, 3, 13},
    {2, 12, 6, 10, 0, 11, 8, 3, 4, 13, 7, 5, 15, 14, 1, 9},
    {12, 5, 1, 15, 14, 13, 4, 10, 0, 7, 6, 3, 9, 2, 8, 11},
    {13, 11, 7, 14, 12, 1, 3, 9, 5, 0, 15, 4, 8, 6, 2, 10},
    {6, 15, 14, 9, 11, 3, 0, 8, 12, 2, 13, 7, 1, 4, 10, 5},
    {10, 2, 8, 4, 7, 6, 1, 5, 15, 11, 9, 14, 3, 12, 13, 0},
};

static inline u32 rotr32(u32 x, int n) { return (x >> n) | (x << (32 - n)); }
static inline u32 ld32(const u8 *p) { return (u32)p[0] | (u32)p[1] << 8 | (u32)p[2] << 16 | (u32)p[3] << 24; }

#define G(a, b, c, d, x, y)              \
    do {                                 \
        v[a] = v[a] + v[b] + (x);        \
        v[d] = rotr32(v[d] ^ v[a], 16);  \
        v[c] = v[c] + v[d];              \
        v[b] = rotr32(v[b] ^ v[c], 12);  \
        v[a] = v[a] + v[b] + (y);        \
        v[d] = rotr32(v[d] ^ v[a], 8);   \
        v[c] = v[c] + v[d];              \
        v[b] = rotr32(v[b] ^ v[c], 7);   \
    } while (0)

static void b2s_compress(blake2s_t *s, const u8 *blk, int last) {
    u32 m[16], v[16];
    for (int i = 0; i < 16; i++) m[i] = ld32(blk + 4 * i);
    for (int i = 0; i < 8; i++) { v[i] = s->h[i]; v[i + 8] = B2S_IV[i]; }
    v[12] ^= s->t[0];
    v[13] ^= s->t[1];
    if (last) v[14] = ~v[14];
    for (int r = 0; r < 10; r++) {
        const u8 *sg = B2S_SIGMA[r];
        G(0, 4, 8, 12, m[sg[0]], m[sg[1]]);
        G(1, 5, 9, 13, m[sg[2]], m[sg[3]]);
        G(2, 6, 10, 14, m[sg[4]], m[sg[5]]);
        G(3, 7, 11, 15, m[sg[6]], m[sg[7]]);
        G(0, 5, 10, 15, m[sg[8]], m[sg[9]]);
        G(1, 6, 11, 12, m[sg[10]], m[sg[11]]);
        G(2, 7, 8, 13, m[sg[12]], m[sg[13]]);
        G(3, 4, 9, 14, m[sg[14]], m[sg[15]]);
    }
    for (int i = 0; i < 8; i++) s->h[i] ^= v[i] ^ v[i + 8];
}

void blake2s_init(blake2s_t *s, u32 outlen) {
    for (int i = 0; i < 8; i++) s->h[i] = B2S_IV[i];
    s->h[0] ^= 0x01010000u ^ outlen;  /* depth 1, fanout 1, no key */
    s->t[0] = s->t[1] = 0;
    s->f[0] = s->f[1] = 0;
    s->buflen = 0;
    s->outlen = outlen;
}

void blake2s_update(blake2s_t *s, const void *in, u32 n) {
    const u8 *p = static_cast<const u8 *>(in);
    while (n > 0) {
        if (s->buflen == 64) {
            s->t[0] += 64;
            if (s->t[0] < 64) s->t[1]++;
            b2s_compress(s, s->buf, 0);
            s->buflen = 0;
        }
        u32 take = 64 - s->buflen;
        if (take > n) take = n;
        memcpy(s->buf + s->buflen, p, take);
        s->buflen += take;
        p += take;
        n -= take;
    }
}

void blake2s_final(blake2s_t *s, u8 *out) {
    s->t[0] += s->buflen;
    if (s->t[0] < s->buflen) s->t[1]++;
    memset(s->buf + s->buflen, 0, 64 - s->buflen);
    b2s_compress(s, s->buf, 1);
    for (u32 i = 0; i < s->outlen; i++) out[i] = (u8)(s->h[i >> 2] >> (8 * (i & 3)));
}

void blake2s128(const void *in, u32 n, u8 out[16]) {
    blake2s_t s;
    blake2s_init(&s, 16);
    blake2s_update(&s, in, n);
    blake2s_final(&s, out);
}

/* The contract's zero-page key: BLAKE2s-128 of 4096 zero bytes. */
int blake2s_selftest(void) {
    static const u8 want[16] = {0xc3, 0xf7, 0x25, 0xe0, 0xc2, 0x9f, 0x69, 0xcd,
                                0x61, 0x6a, 0x0f, 0xe9, 0xb7, 0x62, 0xbc, 0xb5};
    blake2s_t s;
    u8 zero[256], out[16];
    memset(zero, 0, sizeof zero);
    blake2s_init(&s, 16);
    for (int i = 0; i < 16; i++) blake2s_update(&s, zero, 256);
    blake2s_final(&s, out);
    return memcmp(out, want, 16) == 0;
}

/* ---- SHA-256 ---- */
static const u32 K256[64] = {
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
};

static void sha_block(sha256_t *s, const u8 *p) {
    u32 w[64];
    for (int i = 0; i < 16; i++)
        w[i] = (u32)p[4 * i] << 24 | (u32)p[4 * i + 1] << 16 | (u32)p[4 * i + 2] << 8 | p[4 * i + 3];
    for (int i = 16; i < 64; i++) {
        u32 s0 = rotr32(w[i - 15], 7) ^ rotr32(w[i - 15], 18) ^ (w[i - 15] >> 3);
        u32 s1 = rotr32(w[i - 2], 17) ^ rotr32(w[i - 2], 19) ^ (w[i - 2] >> 10);
        w[i] = w[i - 16] + s0 + w[i - 7] + s1;
    }
    u32 a = s->h[0], b = s->h[1], c = s->h[2], d = s->h[3], e = s->h[4], f = s->h[5], g = s->h[6], h = s->h[7];
    for (int i = 0; i < 64; i++) {
        u32 S1 = rotr32(e, 6) ^ rotr32(e, 11) ^ rotr32(e, 25);
        u32 ch = (e & f) ^ (~e & g);
        u32 t1 = h + S1 + ch + K256[i] + w[i];
        u32 S0 = rotr32(a, 2) ^ rotr32(a, 13) ^ rotr32(a, 22);
        u32 mj = (a & b) ^ (a & c) ^ (b & c);
        u32 t2 = S0 + mj;
        h = g; g = f; f = e; e = d + t1; d = c; c = b; b = a; a = t1 + t2;
    }
    s->h[0] += a; s->h[1] += b; s->h[2] += c; s->h[3] += d;
    s->h[4] += e; s->h[5] += f; s->h[6] += g; s->h[7] += h;
}

void sha256_init(sha256_t *s) {
    static const u32 iv[8] = {0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a,
                              0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19};
    for (int i = 0; i < 8; i++) s->h[i] = iv[i];
    s->len = 0;
    s->buflen = 0;
}

void sha256_update(sha256_t *s, const void *in, u32 n) {
    const u8 *p = static_cast<const u8 *>(in);
    s->len += n;
    if (s->buflen) {
        while (n && s->buflen < 64) { s->buf[s->buflen++] = *p++; n--; }
        if (s->buflen == 64) { sha_block(s, s->buf); s->buflen = 0; }
    }
    while (n >= 64) { sha_block(s, p); p += 64; n -= 64; }
    while (n) { s->buf[s->buflen++] = *p++; n--; }
}

void sha256_final(sha256_t *s, u8 out[32]) {
    u64 bits = s->len * 8;
    u8 pad = 0x80;
    sha256_update(s, &pad, 1);
    u8 z = 0;
    while (s->buflen != 56) sha256_update(s, &z, 1);
    u8 lb[8];
    for (int i = 0; i < 8; i++) lb[i] = (u8)(bits >> (56 - 8 * i));
    sha256_update(s, lb, 8);
    for (int i = 0; i < 8; i++) {
        out[4 * i] = (u8)(s->h[i] >> 24);
        out[4 * i + 1] = (u8)(s->h[i] >> 16);
        out[4 * i + 2] = (u8)(s->h[i] >> 8);
        out[4 * i + 3] = (u8)s->h[i];
    }
}

void sha256(const void *in, u32 n, u8 out[32]) {
    sha256_t s;
    sha256_init(&s);
    sha256_update(&s, in, n);
    sha256_final(&s, out);
}
