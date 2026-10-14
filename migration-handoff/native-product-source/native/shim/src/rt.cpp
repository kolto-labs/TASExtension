/* Freestanding runtime: memory and string helpers, 64-bit division, a
 * bounded text buffer, and a file log. No CRT is linked. */
#include "shim.hpp"

// Retain the native C ABI, including CRT and assembly-referenced symbols.
extern "C" {

void *memcpy(void *d, const void *s, size_t n) {
    // Profiling: the byte loop was 12% of a walking frame (the GL boundary
    // copies 512+160 bytes per call). rep movsb with DF=0 (Win32 ABI; the
    // thunks restore the caller's flags) has the architectural result of a
    // sequential forward byte copy, the same as the old loop, even when the
    // ranges overlap. Being asm, it can never be lowered into a call to itself.
    void *r = d;
    __asm__ volatile("rep movsb" : "+D"(d), "+S"(s), "+c"(n) : : "memory");
    return r;
}

void *memset(void *d, int c, size_t n) {
    u8 *dp = reinterpret_cast<u8 *>(d);
    const u8 byte = static_cast<u8>(c);
    while (n >= 4) {
        dp[0] = byte; dp[1] = byte; dp[2] = byte; dp[3] = byte;
        dp += 4; n -= 4;
    }
    while (n--) *dp++ = static_cast<u8>(c);
    return d;
}

void *memmove(void *d, const void *s, size_t n) {
    u8 *dp = reinterpret_cast<u8 *>(d);
    const u8 *sp = reinterpret_cast<const u8 *>(s);
    if (dp == sp || n == 0) return d;
    if (dp < sp) { while (n--) *dp++ = *sp++; }
    else { dp += n; sp += n; while (n--) *--dp = *--sp; }
    return d;
}

int memcmp(const void *a, const void *b, size_t n) {
    const u8 *x = reinterpret_cast<const u8 *>(a), *y = reinterpret_cast<const u8 *>(b);
    for (size_t i = 0; i < n; i++)
        if (x[i] != y[i]) return x[i] < y[i] ? -1 : 1;
    return 0;
}

size_t strlen(const char *s) {
    size_t n = 0;
    while (s[n]) n++;
    return n;
}

int k_streq(const char *a, const char *b) {
    while (*a && *a == *b) { a++; b++; }
    return *a == *b;
}

static char lower(char c) { return (c >= 'A' && c <= 'Z') ? static_cast<char>((c + 32)) : c; }

int k_strieq(const char *a, const char *b) {
    while (*a && lower(*a) == lower(*b)) { a++; b++; }
    return lower(*a) == lower(*b);
}

int k_strneq(const char *a, const char *b, size_t n) {
    for (size_t i = 0; i < n; i++) {
        if (a[i] != b[i]) return 0;
        if (!a[i]) return 1;
    }
    return 1;
}

const char *k_strstr(const char *h, const char *n) {
    size_t ln = strlen(n);
    for (; *h; h++)
        if (k_strneq(h, n, ln)) return h;
    return 0;
}

u64 k_parse_u64(const char *s, const char **end) {
    u64 v = 0;
    if (s[0] == '0' && (s[1] == 'x' || s[1] == 'X')) {
        s += 2;
        for (;;) {
            char c = *s;
            int d;
            if (c >= '0' && c <= '9') d = c - '0';
            else if (c >= 'a' && c <= 'f') d = c - 'a' + 10;
            else if (c >= 'A' && c <= 'F') d = c - 'A' + 10;
            else break;
            v = (v << 4) | static_cast<u64>(d);
            s++;
        }
    } else {
        while (*s >= '0' && *s <= '9') { v = v * 10 + static_cast<u64>((*s - '0')); s++; }
    }
    if (end) *end = s;
    return v;
}

/* ---- 64-bit division (i386 has no native 64/64 divide) ---- */
u64 __udivmoddi4(u64 n, u64 d, u64 *rem) {
    if (d == 0) { if (rem) *rem = 0; return 0; }
    if ((n >> 32) == 0 && (d >> 32) == 0) {
        u32 q = static_cast<u32>(n) / static_cast<u32>(d);
        if (rem) *rem = static_cast<u32>(n) % static_cast<u32>(d);
        return q;
    }
    u64 q = 0, r = 0;
    for (int i = 63; i >= 0; i--) {
        r = (r << 1) | ((n >> i) & 1);
        if (r >= d) { r -= d; q |= 1ULL << i; }
    }
    if (rem) *rem = r;
    return q;
}
u64 __udivdi3(u64 a, u64 b) { return __udivmoddi4(a, b, 0); }
u64 __umoddi3(u64 a, u64 b) { u64 r; __udivmoddi4(a, b, &r); return r; }
i64 __divdi3(i64 a, i64 b) {
    int neg = (a < 0) ^ (b < 0);
    u64 q = __udivmoddi4(a < 0 ? static_cast<u64>(-a) : static_cast<u64>(a), b < 0 ? static_cast<u64>(-b) : static_cast<u64>(b), 0);
    return neg ? -static_cast<i64>(q) : static_cast<i64>(q);
}
i64 __moddi3(i64 a, i64 b) {
    u64 r;
    __udivmoddi4(a < 0 ? static_cast<u64>(-a) : static_cast<u64>(a), b < 0 ? static_cast<u64>(-b) : static_cast<u64>(b), &r);
    return a < 0 ? -static_cast<i64>(r) : static_cast<i64>(r);
}

/* ---- bounded buffer ---- */
void b_init(buf_t *b, char *mem, u32 cap) { b->p = mem; b->n = 0; b->cap = cap; b->overflow = 0; }
void b_raw(buf_t *b, const char *s, u32 n) {
    if (b->n + n + 1 > b->cap) { b->overflow = 1; n = b->cap > b->n + 1 ? b->cap - b->n - 1 : 0; }
    memcpy(b->p + b->n, s, n);
    b->n += n;
}
void b_str(buf_t *b, const char *s) { b_raw(b, s, static_cast<u32>(strlen(s))); }
void b_ch(buf_t *b, char c) { b_raw(b, &c, 1); }
void b_u64(buf_t *b, u64 v) {
    char t[24];
    int i = 23;
    t[i] = 0;
    do { t[--i] = static_cast<char>(('0' + static_cast<int>((v % 10)))); v /= 10; } while (v);
    b_str(b, t + i);
}
void b_i64(buf_t *b, i64 v) {
    if (v < 0) { b_ch(b, '-'); b_u64(b, static_cast<u64>((-v))); } else b_u64(b, static_cast<u64>(v));
}
static constexpr char HEX[] = "0123456789abcdef";
void b_hex(buf_t *b, const u8 *p, u32 n) {
    for (u32 i = 0; i < n; i++) { b_ch(b, HEX[p[i] >> 4]); b_ch(b, HEX[p[i] & 15]); }
}
void b_hexu32(buf_t *b, u32 v) {
    char t[11];
    t[0] = '0'; t[1] = 'x';
    for (int i = 0; i < 8; i++) t[2 + i] = HEX[(v >> (28 - 4 * i)) & 15];
    t[10] = 0;
    b_str(b, t);
}
void b_jstrn(buf_t *b, const char *s, u32 n) {
    b_ch(b, '"');
    for (u32 i = 0; i < n && s[i]; i++) {
        u8 c = static_cast<u8>(s[i]);
        if (c == '"' || c == '\\') { b_ch(b, '\\'); b_ch(b, static_cast<char>(c)); }
        else if (c < 0x20 || c >= 0x7f) {
            char t[7] = {'\\', 'u', '0', '0', HEX[c >> 4], HEX[c & 15], 0};
            b_str(b, t);
        } else b_ch(b, static_cast<char>(c));
    }
    b_ch(b, '"');
}
void b_jstr(buf_t *b, const char *s) { b_jstrn(b, s, 0xffffffffu); }
static constexpr char B64[] = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
void b_b64(buf_t *b, const u8 *p, u32 n) {
    u32 i = 0;
    for (; i + 3 <= n; i += 3) {
        u32 v = static_cast<u32>(p[i]) << 16 | static_cast<u32>(p[i + 1]) << 8 | p[i + 2];
        b_ch(b, B64[v >> 18]); b_ch(b, B64[(v >> 12) & 63]); b_ch(b, B64[(v >> 6) & 63]); b_ch(b, B64[v & 63]);
    }
    if (n - i == 1) {
        u32 v = static_cast<u32>(p[i]) << 16;
        b_ch(b, B64[v >> 18]); b_ch(b, B64[(v >> 12) & 63]); b_str(b, "==");
    } else if (n - i == 2) {
        u32 v = static_cast<u32>(p[i]) << 16 | static_cast<u32>(p[i + 1]) << 8;
        b_ch(b, B64[v >> 18]); b_ch(b, B64[(v >> 12) & 63]); b_ch(b, B64[(v >> 6) & 63]); b_ch(b, '=');
    }
}
void b_term(buf_t *b) { b->p[b->n < b->cap ? b->n : b->cap - 1] = 0; }

/* ---- log: appends to <session>\kptas_shim.log. Diagnostics only; never part of the journal. ---- */
static char g_logpath[300];
static HANDLE g_logh = INVALID_HANDLE_VALUE;

void slog_init(void) {
    if (!g_session_dir[0]) return;
    buf_t b;
    b_init(&b, g_logpath, sizeof g_logpath);
    b_str(&b, g_session_dir);
    b_str(&b, "\\kptas_shim.log");
    b_term(&b);
    g_logh = CreateFileA(g_logpath, FILE_APPEND_DATA, FILE_SHARE_READ | FILE_SHARE_WRITE, NULL,
                         OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
}

void slog(const char *fmt, ...) {
    if (g_logh == INVALID_HANDLE_VALUE) return;
    char line[1100];
    va_list ap;
    va_start(ap, fmt);
    DWORD le = GetLastError();
    int n = wvsprintfA(line, fmt, ap);
    va_end(ap);
    if (n < 0) n = 0;
    if (n > 1024) n = 1024;
    line[n++] = '\r';
    line[n++] = '\n';
    DWORD w;
    WriteFile(g_logh, line, static_cast<DWORD>(n), &w, NULL);
    SetLastError(le);
}

} // extern "C"
