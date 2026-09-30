/* Freestanding runtime for kst: mem*, 64-bit division, text buffers, output, files. */
#include "kst.h"

void *memcpy(void *d, const void *s, unsigned int n) {
    u8 *dp = (u8 *)d;
    const u8 *sp = (const u8 *)s;
    if (!(((u32)dp | (u32)sp | n) & 3)) {
        u32 *d4 = (u32 *)dp;
        const u32 *s4 = (const u32 *)sp;
        for (n >>= 2; n; n--) *d4++ = *s4++;
        return d;
    }
    while (n--) *dp++ = *sp++;
    return d;
}

void *memset(void *d, int c, unsigned int n) {
    u8 *dp = (u8 *)d;
    if (!(((u32)dp | n) & 3)) {
        u32 v = (u8)c * 0x01010101u;
        u32 *d4 = (u32 *)dp;
        for (n >>= 2; n; n--) *d4++ = v;
        return d;
    }
    while (n--) *dp++ = (u8)c;
    return d;
}

void *memmove(void *d, const void *s, unsigned int n) {
    u8 *dp = (u8 *)d;
    const u8 *sp = (const u8 *)s;
    if (dp < sp) {
        while (n--) *dp++ = *sp++;
    } else {
        dp += n;
        sp += n;
        while (n--) *--dp = *--sp;
    }
    return d;
}

int memcmp(const void *a, const void *b, unsigned int n) {
    const u8 *x = (const u8 *)a, *y = (const u8 *)b;
    if (!(((u32)x | (u32)y | n) & 3)) {
        const u32 *x4 = (const u32 *)x, *y4 = (const u32 *)y;
        u32 k = n >> 2;
        while (k && *x4 == *y4) { x4++; y4++; k--; }
        if (!k) return 0;
        x = (const u8 *)x4;
        y = (const u8 *)y4;
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
i64 __divdi3(i64 a, i64 b) {
    int neg = (a < 0) ^ (b < 0);
    u64 q = __udivmoddi4(a < 0 ? (u64)-a : (u64)a, b < 0 ? (u64)-b : (u64)b, 0);
    return neg ? -(i64)q : (i64)q;
}
i64 __moddi3(i64 a, i64 b) {
    u64 r;
    __udivmoddi4(a < 0 ? (u64)-a : (u64)a, b < 0 ? (u64)-b : (u64)b, &r);
    return a < 0 ? -(i64)r : (i64)r;
}

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
    do { t[--i] = (char)('0' + (int)(v % 10)); v /= 10; } while (v);
    kb_raw(b, t + i, (u32)(24 - i));
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
void kst_out(const char *s, u32 n) { write_handle(STD_OUTPUT_HANDLE, s, n); }
void kst_outs(const char *s) { kst_out(s, kst_strlen(s)); }

void kst_fatal(const char *msg) {
    write_handle(STD_ERROR_HANDLE, "kst fatal: ", 11);
    write_handle(STD_ERROR_HANDLE, msg, kst_strlen(msg));
    write_handle(STD_ERROR_HANDLE, "\n", 1);
    write_handle(STD_OUTPUT_HANDLE, "kst fatal: ", 11);
    write_handle(STD_OUTPUT_HANDLE, msg, kst_strlen(msg));
    write_handle(STD_OUTPUT_HANDLE, "\n", 1);
    ExitProcess(3);
}

KST_CORE static u64 g_qpf;
u64 kst_qpc(void) { LARGE_INTEGER c; QueryPerformanceCounter(&c); return (u64)c.QuadPart; }
u64 kst_us(u64 d) {
    if (!g_qpf) { LARGE_INTEGER f; QueryPerformanceFrequency(&f); g_qpf = (u64)f.QuadPart; }
    return d * 1000000ull / g_qpf;
}

int kst_wcsicmp_n(const WCHAR *a, const char *b) {
    for (;; a++, b++) {
        WCHAR x = *a;
        char y = *b;
        if (x >= 'A' && x <= 'Z') x = (WCHAR)(x + 32);
        if (y >= 'A' && y <= 'Z') y = (char)(y + 32);
        if (x != (WCHAR)(u8)y) return 1;
        if (!x) return 0;
    }
}

u64 kst_wtou64(const WCHAR *s) {
    u64 v = 0;
    while (*s >= '0' && *s <= '9') v = v * 10 + (u64)(*s++ - '0');
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
    u8 *p = (u8 *)VirtualAlloc(0, lo ? lo : 1, MEM_RESERVE | MEM_COMMIT, PAGE_READWRITE);
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
        if (!WriteFile(h, (const u8 *)p + off, len - off, &w, 0) || !w) break;
        off += w;
    }
    CloseHandle(h);
    return off == len;
}
