/* Minimal JSON reader for wire requests (contract section 10).
 * Integers only for numbers (fractions are truncated). One static pool,
 * valid until the next json_parse call. */
#include "shim.hpp"

#define JPOOL 4096
#define JSTRPOOL (256 * 1024)
static jnode g_pool[JPOOL];
static u32 g_pool_n;
static char g_str[JSTRPOOL];
static u32 g_str_n;

struct jp_t {
    const char *p, *end;
    int err;
};

static void ws(jp_t *j) {
    while (j->p < j->end && (*j->p == ' ' || *j->p == '\t' || *j->p == '\r' || *j->p == '\n')) j->p++;
}

static jnode *newnode(jp_t *j, u8 t) {
    if (g_pool_n >= JPOOL) { j->err = 1; return nullptr; }
    jnode *n = &g_pool[g_pool_n++];
    memset(n, 0, sizeof *n);
    n->type = t;
    return n;
}

static int hexv(char c) {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

static const char *pstring(jp_t *j) {
    if (j->p >= j->end || *j->p != '"') { j->err = 1; return nullptr; }
    j->p++;
    char *out = g_str + g_str_n;
    u32 n = 0;
    while (j->p < j->end && *j->p != '"') {
        char c = *j->p++;
        if (c == '\\' && j->p < j->end) {
            char e = *j->p++;
            switch (e) {
            case 'n': c = '\n'; break;
            case 't': c = '\t'; break;
            case 'r': c = '\r'; break;
            case 'b': c = '\b'; break;
            case 'f': c = '\f'; break;
            case 'u': {
                u32 cp = 0;
                for (int i = 0; i < 4 && j->p < j->end; i++) {
                    int h = hexv(*j->p++);
                    if (h < 0) { j->err = 1; return nullptr; }
                    cp = cp << 4 | (u32)h;
                }
                c = cp < 0x80 ? (char)cp : '?';
                break;
            }
            default: c = e;
            }
        }
        if (g_str_n + n + 2 >= JSTRPOOL) { j->err = 1; return nullptr; }
        out[n++] = c;
    }
    if (j->p >= j->end) { j->err = 1; return nullptr; }
    j->p++;
    out[n] = 0;
    g_str_n += n + 1;
    return out;
}

static jnode *pvalue(jp_t *j, int depth);

static jnode *pvalue(jp_t *j, int depth) {
    if (depth > 32) { j->err = 1; return nullptr; }
    ws(j);
    if (j->p >= j->end) { j->err = 1; return nullptr; }
    char c = *j->p;
    if (c == '{' || c == '[') {
        int obj = c == '{';
        jnode *n = newnode(j, obj ? J_OBJ : J_ARR);
        if (!n) return nullptr;
        j->p++;
        ws(j);
        jnode **tail = &n->child;
        if (j->p < j->end && *j->p == (obj ? '}' : ']')) { j->p++; return n; }
        for (;;) {
            const char *key = nullptr;
            ws(j);
            if (obj) {
                key = pstring(j);
                if (j->err) return nullptr;
                ws(j);
                if (j->p >= j->end || *j->p != ':') { j->err = 1; return nullptr; }
                j->p++;
            }
            jnode *v = pvalue(j, depth + 1);
            if (!v) return nullptr;
            v->key = key;
            *tail = v;
            tail = &v->next;
            ws(j);
            if (j->p < j->end && *j->p == ',') { j->p++; continue; }
            if (j->p < j->end && *j->p == (obj ? '}' : ']')) { j->p++; return n; }
            j->err = 1;
            return nullptr;
        }
    }
    if (c == '"') {
        jnode *n = newnode(j, J_STR);
        if (!n) return nullptr;
        n->str = pstring(j);
        return j->err ? nullptr : n;
    }
    if (c == 't' || c == 'f' || c == 'n') {
        const char *w = c == 't' ? "true" : c == 'f' ? "false" : "null";
        u32 l = (u32)strlen(w);
        if ((u32)(j->end - j->p) < l || !k_strneq(j->p, w, l)) { j->err = 1; return nullptr; }
        j->p += l;
        jnode *n = newnode(j, c == 'n' ? J_NULL : J_BOOL);
        if (n) n->num = c == 't';
        return n;
    }
    if (c == '-' || (c >= '0' && c <= '9')) {
        int neg = 0;
        if (c == '-') { neg = 1; j->p++; }
        u64 v = 0;
        while (j->p < j->end && *j->p >= '0' && *j->p <= '9') v = v * 10 + (u64)(*j->p++ - '0');
        /* skip fraction and exponent */
        if (j->p < j->end && *j->p == '.') { j->p++; while (j->p < j->end && *j->p >= '0' && *j->p <= '9') j->p++; }
        if (j->p < j->end && (*j->p == 'e' || *j->p == 'E')) {
            j->p++;
            if (j->p < j->end && (*j->p == '+' || *j->p == '-')) j->p++;
            while (j->p < j->end && *j->p >= '0' && *j->p <= '9') j->p++;
        }
        jnode *n = newnode(j, J_NUM);
        if (n) n->num = neg ? -(i64)v : (i64)v;
        return n;
    }
    j->err = 1;
    return nullptr;
}

jnode *json_parse(const char *text, u32 len) {
    g_pool_n = 0;
    g_str_n = 0;
    jp_t j = {text, text + len, 0};
    jnode *n = pvalue(&j, 0);
    return j.err ? nullptr : n;
}

jnode *jget(jnode *obj, const char *key) {
    if (!obj || obj->type != J_OBJ) return nullptr;
    for (jnode *c = obj->child; c; c = c->next)
        if (c->key && k_streq(c->key, key)) return c;
    return nullptr;
}

i64 jint(jnode *n, i64 def) {
    if (!n) return def;
    if (n->type == J_NUM || n->type == J_BOOL) return n->num;
    if (n->type == J_STR) return (i64)k_parse_u64(n->str, 0);
    return def;
}

const char *jstr(jnode *n, const char *def) {
    return (n && n->type == J_STR) ? n->str : def;
}
