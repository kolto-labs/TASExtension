/* BLAKE2s (RFC 7693) and SHA-256 (FIPS 180-4), freestanding.
 * kst_blake2s128 must equal hashlib.blake2s(data, digest_size=16).digest().
 * The compress function is SSE2 (row vectors, diagonalized G); the scalar one
 * is kept as the reference and for kst_hash_selfcheck. */
#include "kst.hpp"
#include <emmintrin.h>

static const u32 B2S_IV[8] = {
    0x6A09E667u, 0xBB67AE85u, 0x3C6EF372u, 0xA54FF53Au,
    0x510E527Fu, 0x9B05688Cu, 0x1F83D9ABu, 0x5BE0CD19u,
};
static const u8 SIG[10][16] = {
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

static inline u32 ror(u32 x, int n) { return (x >> n) | (x << (32 - n)); }
static inline u32 ld32(const u8 *p) { return static_cast<u32>(p[0]) | static_cast<u32>(p[1]) << 8 | static_cast<u32>(p[2]) << 16 | static_cast<u32>(p[3]) << 24; }

#define G(a, b, c, d, x, y)                         \
    do {                                            \
        v[a] += v[b] + (x); v[d] = ror(v[d] ^ v[a], 16); \
        v[c] += v[d];       v[b] = ror(v[b] ^ v[c], 12); \
        v[a] += v[b] + (y); v[d] = ror(v[d] ^ v[a], 8);  \
        v[c] += v[d];       v[b] = ror(v[b] ^ v[c], 7);  \
    } while (0)

static void b2s_compress_scalar(u32 h[8], const u32 t[2], const u8 *blk, int last) {
    u32 m[16], v[16];
    for (int i = 0; i < 16; i++) m[i] = ld32(blk + 4 * i);
    for (int i = 0; i < 8; i++) { v[i] = h[i]; v[i + 8] = B2S_IV[i]; }
    v[12] ^= t[0];
    v[13] ^= t[1];
    if (last) v[14] = ~v[14];
    for (int r = 0; r < 10; r++) {
        const u8 *s = SIG[r];
        G(0, 4, 8, 12, m[s[0]], m[s[1]]);
        G(1, 5, 9, 13, m[s[2]], m[s[3]]);
        G(2, 6, 10, 14, m[s[4]], m[s[5]]);
        G(3, 7, 11, 15, m[s[6]], m[s[7]]);
        G(0, 5, 10, 15, m[s[8]], m[s[9]]);
        G(1, 6, 11, 12, m[s[10]], m[s[11]]);
        G(2, 7, 8, 13, m[s[12]], m[s[13]]);
        G(3, 4, 9, 14, m[s[14]], m[s[15]]);
    }
    for (int i = 0; i < 8; i++) h[i] ^= v[i] ^ v[i + 8];
}

#define ROT(x, n) _mm_or_si128(_mm_srli_epi32((x), (n)), _mm_slli_epi32((x), 32 - (n)))
#define GV(r1, r2, r3, r4, b1, b2)                                   \
    do {                                                             \
        r1 = _mm_add_epi32(_mm_add_epi32(r1, b1), r2);               \
        r4 = _mm_xor_si128(r4, r1); r4 = ROT(r4, 16);                \
        r3 = _mm_add_epi32(r3, r4);                                  \
        r2 = _mm_xor_si128(r2, r3); r2 = ROT(r2, 12);                \
        r1 = _mm_add_epi32(_mm_add_epi32(r1, b2), r2);               \
        r4 = _mm_xor_si128(r4, r1); r4 = ROT(r4, 8);                 \
        r3 = _mm_add_epi32(r3, r4);                                  \
        r2 = _mm_xor_si128(r2, r3); r2 = ROT(r2, 7);                 \
    } while (0)

static void b2s_compress(u32 h[8], const u32 t[2], const u8 *blk, int last) {
    __m128i r1 = _mm_loadu_si128(reinterpret_cast<const __m128i *>(&h[0]));
    __m128i r2 = _mm_loadu_si128(reinterpret_cast<const __m128i *>(&h[4]));
    __m128i r3 = _mm_loadu_si128(reinterpret_cast<const __m128i *>(&B2S_IV[0]));
    __m128i r4 = _mm_xor_si128(_mm_loadu_si128(reinterpret_cast<const __m128i *>(&B2S_IV[4])),
                               _mm_set_epi32(0, last ? -1 : 0, static_cast<int>(t[1]), static_cast<int>(t[0])));
    const __m128i h1 = r1, h2 = r2;
    for (int r = 0; r < 10; r++) {
        const u8 *s = SIG[r];
        __m128i b1 = _mm_set_epi32(static_cast<int>(ld32(blk + 4 * s[6])), static_cast<int>(ld32(blk + 4 * s[4])), static_cast<int>(ld32(blk + 4 * s[2])), static_cast<int>(ld32(blk + 4 * s[0])));
        __m128i b2 = _mm_set_epi32(static_cast<int>(ld32(blk + 4 * s[7])), static_cast<int>(ld32(blk + 4 * s[5])), static_cast<int>(ld32(blk + 4 * s[3])), static_cast<int>(ld32(blk + 4 * s[1])));
        __m128i b3 = _mm_set_epi32(static_cast<int>(ld32(blk + 4 * s[14])), static_cast<int>(ld32(blk + 4 * s[12])), static_cast<int>(ld32(blk + 4 * s[10])), static_cast<int>(ld32(blk + 4 * s[8])));
        __m128i b4 = _mm_set_epi32(static_cast<int>(ld32(blk + 4 * s[15])), static_cast<int>(ld32(blk + 4 * s[13])), static_cast<int>(ld32(blk + 4 * s[11])), static_cast<int>(ld32(blk + 4 * s[9])));
        GV(r1, r2, r3, r4, b1, b2);
        r4 = _mm_shuffle_epi32(r4, _MM_SHUFFLE(2, 1, 0, 3));
        r3 = _mm_shuffle_epi32(r3, _MM_SHUFFLE(1, 0, 3, 2));
        r2 = _mm_shuffle_epi32(r2, _MM_SHUFFLE(0, 3, 2, 1));
        GV(r1, r2, r3, r4, b3, b4);
        r4 = _mm_shuffle_epi32(r4, _MM_SHUFFLE(0, 3, 2, 1));
        r3 = _mm_shuffle_epi32(r3, _MM_SHUFFLE(1, 0, 3, 2));
        r2 = _mm_shuffle_epi32(r2, _MM_SHUFFLE(2, 1, 0, 3));
    }
    _mm_storeu_si128(reinterpret_cast<__m128i *>(&h[0]), _mm_xor_si128(h1, _mm_xor_si128(r1, r3)));
    _mm_storeu_si128(reinterpret_cast<__m128i *>(&h[4]), _mm_xor_si128(h2, _mm_xor_si128(r2, r4)));
}

/* 1 when the SSE2 compress equals the scalar reference on a few blocks. */
int kst_hash_selfcheck(void) {
    u8 blk[64];
    for (int k = 0; k < 8; k++) {
        for (int i = 0; i < 64; i++) blk[i] = static_cast<u8>(i * 31 + k * 7 + (i >> 3));
        u32 a[8], b[8], t[2] = {static_cast<u32>(64 * (k + 1)), static_cast<u32>(k)};
        for (int i = 0; i < 8; i++) a[i] = b[i] = B2S_IV[i] ^ static_cast<u32>(k * 0x9E3779B9u);
        b2s_compress_scalar(a, t, blk, k & 1);
        b2s_compress(b, t, blk, k & 1);
        if (memcmp(a, b, 32)) return 0;
    }
    return 1;
}

void kst_b2s_init(kst_b2s *s, u32 outlen) {
    for (int i = 0; i < 8; i++) s->h[i] = B2S_IV[i];
    s->h[0] ^= 0x01010000u ^ outlen;   /* depth 1, fanout 1, no key */
    s->t[0] = s->t[1] = 0;
    s->n = 0;
    s->outlen = outlen;
}

void kst_b2s_update(kst_b2s *s, const void *in, u32 n) {
    const u8 *p = static_cast<const u8 *>(in);
    while (n) {
        if (s->n == 64) {   /* a full buffered block that is not the last one */
            s->t[0] += 64;
            if (s->t[0] < 64) s->t[1]++;
            b2s_compress(s->h, s->t, s->buf, 0);
            s->n = 0;
        }
        u32 k = 64 - s->n;
        if (k > n) k = n;
        memcpy(s->buf + s->n, p, k);
        s->n += k;
        p += k;
        n -= k;
    }
}

void kst_b2s_final(kst_b2s *s, u8 *out) {
    s->t[0] += s->n;
    if (s->t[0] < s->n) s->t[1]++;
    memset(s->buf + s->n, 0, 64 - s->n);
    b2s_compress(s->h, s->t, s->buf, 1);
    for (u32 i = 0; i < s->outlen; i++) out[i] = static_cast<u8>(s->h[i >> 2] >> (8 * (i & 3)));
}

void kst_blake2s128(const void *in, u32 n, u8 out[16]) {
    kst_b2s s;
    kst_b2s_init(&s, 16);
    kst_b2s_update(&s, in, n);
    kst_b2s_final(&s, out);
}

/* One page: 64 blocks, no buffering. */
void kst_page_key(const void *page, u8 out[16]) {
    u32 h[8], t[2] = {0, 0};
    const u8 *p = static_cast<const u8 *>(page);
    for (int i = 0; i < 8; i++) h[i] = B2S_IV[i];
    h[0] ^= 0x01010000u ^ 16u;
    for (int b = 0; b < 64; b++) {
        t[0] += 64;
        b2s_compress(h, t, p + 64 * b, b == 63);
    }
    for (u32 i = 0; i < 16; i++) out[i] = static_cast<u8>(h[i >> 2] >> (8 * (i & 3)));
}

/* hashlib.blake2s(bytes(4096), digest_size=16) */
const u8 KST_ZERO_KEY[16] = {
    0xc3, 0xf7, 0x25, 0xe0, 0xc2, 0x9f, 0x69, 0xcd, 0x61, 0x6a, 0x0f, 0xe9, 0xb7, 0x62, 0xbc, 0xb5,
};

/* ---------------------------------------------------------------- SHA-256 */

static const u32 SK[64] = {
    0x428a2f98u, 0x71374491u, 0xb5c0fbcfu, 0xe9b5dba5u, 0x3956c25bu, 0x59f111f1u, 0x923f82a4u, 0xab1c5ed5u,
    0xd807aa98u, 0x12835b01u, 0x243185beu, 0x550c7dc3u, 0x72be5d74u, 0x80deb1feu, 0x9bdc06a7u, 0xc19bf174u,
    0xe49b69c1u, 0xefbe4786u, 0x0fc19dc6u, 0x240ca1ccu, 0x2de92c6fu, 0x4a7484aau, 0x5cb0a9dcu, 0x76f988dau,
    0x983e5152u, 0xa831c66du, 0xb00327c8u, 0xbf597fc7u, 0xc6e00bf3u, 0xd5a79147u, 0x06ca6351u, 0x14292967u,
    0x27b70a85u, 0x2e1b2138u, 0x4d2c6dfcu, 0x53380d13u, 0x650a7354u, 0x766a0abbu, 0x81c2c92eu, 0x92722c85u,
    0xa2bfe8a1u, 0xa81a664bu, 0xc24b8b70u, 0xc76c51a3u, 0xd192e819u, 0xd6990624u, 0xf40e3585u, 0x106aa070u,
    0x19a4c116u, 0x1e376c08u, 0x2748774cu, 0x34b0bcb5u, 0x391c0cb3u, 0x4ed8aa4au, 0x5b9cca4fu, 0x682e6ff3u,
    0x748f82eeu, 0x78a5636fu, 0x84c87814u, 0x8cc70208u, 0x90befffau, 0xa4506cebu, 0xbef9a3f7u, 0xc67178f2u,
};

static void sha_block(kst_sha *s, const u8 *p) {
    u32 w[64], a, b, c, d, e, f, g, h;
    for (int i = 0; i < 16; i++)
        w[i] = static_cast<u32>(p[4 * i]) << 24 | static_cast<u32>(p[4 * i + 1]) << 16 | static_cast<u32>(p[4 * i + 2]) << 8 | p[4 * i + 3];
    for (int i = 16; i < 64; i++) {
        u32 s0 = ror(w[i - 15], 7) ^ ror(w[i - 15], 18) ^ (w[i - 15] >> 3);
        u32 s1 = ror(w[i - 2], 17) ^ ror(w[i - 2], 19) ^ (w[i - 2] >> 10);
        w[i] = w[i - 16] + s0 + w[i - 7] + s1;
    }
    a = s->h[0]; b = s->h[1]; c = s->h[2]; d = s->h[3];
    e = s->h[4]; f = s->h[5]; g = s->h[6]; h = s->h[7];
    for (int i = 0; i < 64; i++) {
        u32 t1 = h + (ror(e, 6) ^ ror(e, 11) ^ ror(e, 25)) + ((e & f) ^ (~e & g)) + SK[i] + w[i];
        u32 t2 = (ror(a, 2) ^ ror(a, 13) ^ ror(a, 22)) + ((a & b) ^ (a & c) ^ (b & c));
        h = g; g = f; f = e; e = d + t1; d = c; c = b; b = a; a = t1 + t2;
    }
    s->h[0] += a; s->h[1] += b; s->h[2] += c; s->h[3] += d;
    s->h[4] += e; s->h[5] += f; s->h[6] += g; s->h[7] += h;
}

void kst_sha_init(kst_sha *s) {
    static const u32 iv[8] = {0x6a09e667u, 0xbb67ae85u, 0x3c6ef372u, 0xa54ff53au,
                              0x510e527fu, 0x9b05688cu, 0x1f83d9abu, 0x5be0cd19u};
    for (int i = 0; i < 8; i++) s->h[i] = iv[i];
    s->len = 0;
    s->n = 0;
}

void kst_sha_update(kst_sha *s, const void *in, u32 n) {
    const u8 *p = static_cast<const u8 *>(in);
    s->len += n;
    while (n) {
        u32 k = 64 - s->n;
        if (k > n) k = n;
        memcpy(s->buf + s->n, p, k);
        s->n += k;
        p += k;
        n -= k;
        if (s->n == 64) { sha_block(s, s->buf); s->n = 0; }
    }
}

void kst_sha_final(kst_sha *s, u8 out[32]) {
    u64 bits = s->len * 8;
    u8 pad = 0x80, z = 0;
    kst_sha_update(s, &pad, 1);
    while (s->n != 56) kst_sha_update(s, &z, 1);
    u8 lb[8];
    for (int i = 0; i < 8; i++) lb[i] = static_cast<u8>(bits >> (56 - 8 * i));
    kst_sha_update(s, lb, 8);
    for (int i = 0; i < 8; i++) {
        out[4 * i] = static_cast<u8>(s->h[i] >> 24);
        out[4 * i + 1] = static_cast<u8>(s->h[i] >> 16);
        out[4 * i + 2] = static_cast<u8>(s->h[i] >> 8);
        out[4 * i + 3] = static_cast<u8>(s->h[i]);
    }
}
