/* Freestanding runtime for kst: mem*, 64-bit division, text buffers, output, files.
 * Build with -DKST_NO_CRT_HELPERS when the host (the shim) already defines mem* and
 * the 64-bit division helpers. */
#include "kst.hpp"

#ifndef KST_NO_CRT_HELPERS

/* These word paths operate on arbitrary byte buffers, not u32 objects. */
typedef u32 alias_u32 __attribute__((__may_alias__));

void *memcpy(void *d, const void *s, unsigned int n) {
    u8 *dp = static_cast<u8 *>(d);
    const u8 *sp = static_cast<const u8 *>(s);
    if (!((static_cast<u32>(reinterpret_cast<uintptr_t>(dp)) | static_cast<u32>(reinterpret_cast<uintptr_t>(sp)) | n) & 3)) {
        alias_u32 *d4 = reinterpret_cast<alias_u32 *>(dp);
        const alias_u32 *s4 = reinterpret_cast<const alias_u32 *>(sp);
        for (n >>= 2; n; n--) *d4++ = *s4++;
        return d;
    }
    while (n--) *dp++ = *sp++;
    return d;
}

void *memset(void *d, int c, unsigned int n) {
    u8 *dp = static_cast<u8 *>(d);
    if (!((static_cast<u32>(reinterpret_cast<uintptr_t>(dp)) | n) & 3)) {
        u32 v = static_cast<u8>(c) * 0x01010101u;
        alias_u32 *d4 = reinterpret_cast<alias_u32 *>(dp);
        for (n >>= 2; n; n--) *d4++ = v;
        return d;
    }
    while (n--) *dp++ = static_cast<u8>(c);
    return d;
}

void *memmove(void *d, const void *s, unsigned int n) {
    u8 *dp = static_cast<u8 *>(d);
    const u8 *sp = static_cast<const u8 *>(s);
    /* Relational comparison of pointers to unrelated objects is not defined
     * by C++. The engine is intentionally 32-bit flat-address x86, so compare
     * the corresponding address values while preserving the overlap direction. */
    if (reinterpret_cast<uintptr_t>(dp) < reinterpret_cast<uintptr_t>(sp)) {
        while (n--) *dp++ = *sp++;
    } else {
        dp += n;
        sp += n;
        while (n--) *--dp = *--sp;
    }
    return d;
}

int memcmp(const void *a, const void *b, unsigned int n) {
    const u8 *x = static_cast<const u8 *>(a), *y = static_cast<const u8 *>(b);
    if (!((static_cast<u32>(reinterpret_cast<uintptr_t>(x)) | static_cast<u32>(reinterpret_cast<uintptr_t>(y)) | n) & 3)) {
        const alias_u32 *x4 = reinterpret_cast<const alias_u32 *>(x), *y4 = reinterpret_cast<const alias_u32 *>(y);
        u32 k = n >> 2;
        while (k && *x4 == *y4) { x4++; y4++; k--; }
        if (!k) return 0;
        x = reinterpret_cast<const u8 *>(x4);
        y = reinterpret_cast<const u8 *>(y4);
        n = 4;
    }
    for (; n; n--, x++, y++)
        if (*x != *y) return *x < *y ? -1 : 1;
    return 0;
}

u64 __udivmoddi4(u64 n, u64 d, u64 *rem) {
    u64 q = 0, r = 0;
    if (d == 0) kst_fatal("division by zero");
    for (int i = 63; i >= 0; i--) {
        r = (r << 1) | ((n >> i) & 1);
        if (r >= d) { r -= d; q |= 1ull << i; }
    }
    if (rem) *rem = r;
    return q;
}
u64 __udivdi3(u64 a, u64 b) { return __udivmoddi4(a, b, 0); }
u64 __umoddi3(u64 a, u64 b) { u64 r; __udivmoddi4(a, b, &r); return r; }
static u64 signed_magnitude(i64 value) {
    const u64 bits = static_cast<u64>(value);
    return value < 0 ? u64{0} - bits : bits;
}
i64 __divdi3(i64 a, i64 b) {
    int neg = (a < 0) ^ (b < 0);
    u64 q = __udivmoddi4(signed_magnitude(a), signed_magnitude(b), 0);
    if (!neg) return static_cast<i64>(q);
    /* Preserve the representable INT64_MIN result without negating it. */
    if (q == (u64{1} << 63)) return -static_cast<i64>(q - 1) - 1;
    return -static_cast<i64>(q);
}
i64 __moddi3(i64 a, i64 b) {
    u64 r;
    __udivmoddi4(signed_magnitude(a), signed_magnitude(b), &r);
    if (a >= 0) return static_cast<i64>(r);
    if (r == (u64{1} << 63)) return -static_cast<i64>(r - 1) - 1;
    return -static_cast<i64>(r);
}

#endif /* KST_NO_CRT_HELPERS */

u32 kst_strlen(const char *s) { u32 n = 0; while (s[n]) n++; return n; }

void kb_init(kst_buf *b, char *mem, u32 cap) { b->p = mem; b->n = 0; b->cap = cap; }
void kb_raw(kst_buf *b, const void *s, u32 n) {
    if (b->n + n > b->cap) n = b->cap - b->n;
    memcpy(b->p + b->n, s, n);
    b->n += n;
}
void kb_str(kst_buf *b, const char *s) { kb_raw(b, s, kst_strlen(s)); }
void kb_ch(kst_buf *b, char c) { kb_raw(b, &c, 1); }
void kb_u64(kst_buf *b, u64 v) {
    char t[24];
    int i = 24;
    do { t[--i] = static_cast<char>('0' + static_cast<int>(v % 10)); v /= 10; } while (v);
    kb_raw(b, t + i, static_cast<u32>(24 - i));
}
void kb_hex(kst_buf *b, const u8 *p, u32 n) {
    static const char hx[] = "0123456789abcdef";
    for (u32 i = 0; i < n; i++) { kb_ch(b, hx[p[i] >> 4]); kb_ch(b, hx[p[i] & 15]); }
}
void kb_hex32(kst_buf *b, u32 v) {
    static const char hx[] = "0123456789abcdef";
    for (int s = 28; s >= 0; s -= 4) kb_ch(b, hx[(v >> s) & 15]);
}

static void write_handle(DWORD which, const char *s, u32 n) {
    HANDLE h = GetStdHandle(which);
    DWORD w;
    if (h && h != INVALID_HANDLE_VALUE) WriteFile(h, s, n, &w, 0);
}
KST_CORE static void (*g_outfn)(const char *, u32);
void kst_set_out(void (*fn)(const char *, u32)) { g_outfn = fn; }
void kst_out(const char *s, u32 n) {
    if (g_outfn) { g_outfn(s, n); return; }
    write_handle(STD_OUTPUT_HANDLE, s, n);
}
void kst_outs(const char *s) { kst_out(s, kst_strlen(s)); }

void kst_fatal(const char *msg) {
    if (g_outfn) {
        g_outfn("kst fatal: ", 11);
        g_outfn(msg, kst_strlen(msg));
        g_outfn("\n", 1);
    }
    write_handle(STD_ERROR_HANDLE, "kst fatal: ", 11);
    write_handle(STD_ERROR_HANDLE, msg, kst_strlen(msg));
    write_handle(STD_ERROR_HANDLE, "\n", 1);
    write_handle(STD_OUTPUT_HANDLE, "kst fatal: ", 11);
    write_handle(STD_OUTPUT_HANDLE, msg, kst_strlen(msg));
    write_handle(STD_OUTPUT_HANDLE, "\n", 1);
    ExitProcess(3);
}

KST_CORE static u64 g_qpf;
u64 kst_qpc(void) { LARGE_INTEGER c; QueryPerformanceCounter(&c); return static_cast<u64>(c.QuadPart); }
u64 kst_us(u64 d) {
    if (!g_qpf) { LARGE_INTEGER f; QueryPerformanceFrequency(&f); g_qpf = static_cast<u64>(f.QuadPart); }
    return d * 1000000ull / g_qpf;
}

int kst_wcsicmp_n(const WCHAR *a, const char *b) {
    for (;; a++, b++) {
        WCHAR x = *a;
        char y = *b;
        if (x >= 'A' && x <= 'Z') x = static_cast<WCHAR>(x + 32);
        if (y >= 'A' && y <= 'Z') y = static_cast<char>(y + 32);
        if (x != static_cast<WCHAR>(static_cast<u8>(y))) return 1;
        if (!x) return 0;
    }
}

u64 kst_wtou64(const WCHAR *s) {
    u64 v = 0;
    while (*s >= '0' && *s <= '9') v = v * 10 + static_cast<u64>(*s++ - '0');
    return v;
}

void *kst_core_alloc(u32 bytes) {
    void *p = VirtualAlloc(0, bytes, MEM_RESERVE | MEM_COMMIT, PAGE_READWRITE);
    if (!p) kst_fatal("harness_core allocation failed");
    return p;
}

int kst_read_file(const WCHAR *path, u8 **out, u32 *len) {
    HANDLE h = CreateFileW(path, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE, 0, OPEN_EXISTING,
                           FILE_ATTRIBUTE_NORMAL, 0);
    if (h == INVALID_HANDLE_VALUE) return 0;
    DWORD hi = 0, lo = GetFileSize(h, &hi), got = 0;
    if (hi || lo > 0x40000000u) { CloseHandle(h); return 0; }
    u8 *p = static_cast<u8 *>(VirtualAlloc(nullptr, lo ? lo : 1, MEM_RESERVE | MEM_COMMIT, PAGE_READWRITE));
    if (!p) { CloseHandle(h); return 0; }
    u32 off = 0;
    while (off < lo) {
        if (!ReadFile(h, p + off, lo - off, &got, 0) || !got) break;
        off += got;
    }
    CloseHandle(h);
    if (off != lo) { VirtualFree(p, 0, MEM_RELEASE); return 0; }
    *out = p;
    *len = lo;
    return 1;
}

int kst_write_file(const WCHAR *path, const void *p, u32 len) {
    HANDLE h = CreateFileW(path, GENERIC_WRITE, FILE_SHARE_READ, 0, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, 0);
    if (h == INVALID_HANDLE_VALUE) return 0;
    DWORD w = 0;
    u32 off = 0;
    while (off < len) {
        if (!WriteFile(h, static_cast<const u8 *>(p) + off, len - off, &w, 0) || !w) break;
        off += w;
    }
    CloseHandle(h);
    return off == len;
}
