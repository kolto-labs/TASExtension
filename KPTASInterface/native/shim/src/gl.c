/* GL virtual names (contract 3.7), the part that sim exactness needs.
 *
 * Every GL name the game can store (textures, display lists, ARB programs, ATI
 * fragment shaders, ARB buffers) comes from a counter in harness_game, never
 * from the driver: Mesa hands out max+1, which keeps counting across a restore,
 * so its names would differ in a continued run. The shim's name IS the real
 * name: the compatibility profile creates an object on first bind (or
 * glNewList) of any unused name, so no virtual-to-real table exists, and no
 * driver value enters sim memory or harness_game.
 *
 * Fresh on allocation: when a counter hands out a name, any real object left
 * under it by a discarded future (after a restore) is deleted first, so the
 * first bind creates a default object as in the recorded run.
 *
 * Retire instead of delete: a name the game deletes keeps its real object, up
 * to a budget, so a restore to a tick that still uses it renders it. Past the
 * budget the oldest retired name is really deleted; that affects pixels only. */
#include "shim.h"

#define GLAPI __attribute__((stdcall))
typedef unsigned int GLenum_;
typedef unsigned int GLuint_;
typedef int GLsizei_;
typedef int GLint_;
typedef unsigned char GLboolean_;
typedef unsigned int GLbitfield_;

#define GL_LIST_BIT_ 0x00020000u
#define GL_ALL_ATTRIB_BITS_ 0x000fffffu
#define GL_LIST_BASE_ 0x0B32u
#define GL_LIST_INDEX_ 0x0B33u
#define GL_TEXTURE_BINDING_1D_ 0x8068u
#define GL_TEXTURE_BINDING_2D_ 0x8069u
#define GL_TEXTURE_BINDING_3D_ 0x806Au
#define GL_TEXTURE_BINDING_CUBE_MAP_ 0x8514u
#define GL_UNSIGNED_INT_ 0x1405u

u32 g_gl_retired;
static u32 g_off_main;

static void (GLAPI *r_glGenTextures)(GLsizei_, GLuint_ *);
static void (GLAPI *r_glDeleteTextures)(GLsizei_, const GLuint_ *);
static GLuint_ (GLAPI *r_glGenLists)(GLsizei_);
static void (GLAPI *r_glDeleteLists)(GLuint_, GLsizei_);
static GLenum_ (GLAPI *r_glGetError)(void);
static void *(GLAPI *r_wglGetProcAddress)(const char *);

static void (GLAPI *x_DeleteProgramsARB)(GLsizei_, const GLuint_ *);
static void (GLAPI *x_DeleteFragmentShaderATI)(GLuint_);
static void (GLAPI *x_DeleteBuffersARB)(GLsizei_, const GLuint_ *);
static void *(GLAPI *x_MapBufferARB)(GLenum_, GLenum_);

static void off_main(const char *fn) {
    if (K->cur != 0 && g_off_main < 20) { g_off_main++; slog("GL %s from slot %u", fn, K->cur); }
}

/* ---------------------------------------------------------------- retire (textures) */
#define RETIRE_MAX 8192u
static u32 g_ret[RETIRE_MAX];
static u32 g_ret_h, g_ret_n;
static u8 g_retired[GL_TEXMAX];   /* harness_core: name still retired (not re-allocated since) */

static void retire_tex(u32 v) {
    if (v >= GL_TEXMAX) return;
    if (g_ret_n == RETIRE_MAX) {
        GLuint_ old = g_ret[g_ret_h];
        if (g_retired[old]) { g_retired[old] = 0; r_glDeleteTextures(1, &old); }
        g_ret_h = (g_ret_h + 1) % RETIRE_MAX;
        g_ret_n--;
    }
    g_ret[(g_ret_h + g_ret_n) % RETIRE_MAX] = v;
    g_ret_n++;
    g_retired[v] = 1;
    g_gl_retired++;
}

/* ---------------------------------------------------------------- textures */
static void GLAPI hk_glGenTextures(GLsizei_ n, GLuint_ *out) {
    off_main("glGenTextures");
    for (GLsizei_ i = 0; i < n; i++) {
        GLuint_ v = SH->gl_tex_next++;
        if (v >= GL_TEXMAX) { slog("GL texture name space full"); TerminateProcess(GetCurrentProcess(), 6); }
        g_retired[v] = 0;
        r_glDeleteTextures(1, &v);   /* fresh: drop whatever a discarded future left under this name */
        out[i] = v;
    }
}

static void GLAPI hk_glDeleteTextures(GLsizei_ n, const GLuint_ *names) {
    for (GLsizei_ i = 0; i < n; i++)
        if (names[i]) retire_tex(names[i]);
}

/* ---------------------------------------------------------------- display lists */
static GLuint_ GLAPI hk_glGenLists(GLsizei_ range) {
    off_main("glGenLists");
    if (range <= 0) return 0;
    GLuint_ v = SH->gl_list_next;
    SH->gl_list_next += (u32)range;
    r_glDeleteLists(v, range);        /* fresh */
    return v;
}

static void GLAPI hk_glDeleteLists(GLuint_ v, GLsizei_ range) {
    (void)v; (void)range;             /* retired: real lists are kept */
}

static u32 g_err_logs;
static GLenum_ GLAPI hk_glGetError(void) {
    GLenum_ e = r_glGetError();
    if (e && g_err_logs < 50) { g_err_logs++; slog("GL glGetError -> %x at tick %u", e, (u32)g_gate.tick); }
    return e;
}

/* ---------------------------------------------------------------- extension names */
static void GLAPI w_GenProgramsARB(GLsizei_ n, GLuint_ *out) {
    for (GLsizei_ i = 0; i < n; i++) {
        GLuint_ v = SH->gl_prog_next++;
        if (x_DeleteProgramsARB) x_DeleteProgramsARB(1, &v);
        out[i] = v;
    }
}
static void GLAPI w_DeleteProgramsARB(GLsizei_ n, const GLuint_ *names) { (void)n; (void)names; }

static GLuint_ GLAPI w_GenFragmentShadersATI(GLuint_ range) {
    if (!range) return 0;
    GLuint_ v = SH->gl_ati_next;
    SH->gl_ati_next += range;
    for (GLuint_ i = 0; i < range && x_DeleteFragmentShaderATI; i++) x_DeleteFragmentShaderATI(v + i);
    return v;
}
static void GLAPI w_DeleteFragmentShaderATI(GLuint_ v) { (void)v; }

static void GLAPI w_GenBuffersARB(GLsizei_ n, GLuint_ *out) {
    for (GLsizei_ i = 0; i < n; i++) {
        GLuint_ v = SH->gl_buf_next++;
        if (x_DeleteBuffersARB) x_DeleteBuffersARB(1, &v);
        out[i] = v;
    }
}
static void GLAPI w_DeleteBuffersARB(GLsizei_ n, const GLuint_ *names) { (void)n; (void)names; }
static u32 g_map_logs;
static void *GLAPI w_MapBufferARB(GLenum_ t, GLenum_ a) {
    void *p = x_MapBufferARB(t, a);
    if (g_map_logs < 20) { g_map_logs++; slog("GL glMapBufferARB -> %x (a driver pointer reaches sim memory)", (u32)(uintptr_t)p); }
    return p;
}

/* Deletes are resolved first: a Gen wrapper uses them to free its name. */
typedef struct { const char *name; void **real; void *wrap; } xwrap_t;
static void *x_gen_programs, *x_gen_ati, *x_gen_buffers;
static xwrap_t g_xw[] = {
    {"glGenProgramsARB", &x_gen_programs, (void *)w_GenProgramsARB},
    {"glDeleteProgramsARB", (void **)&x_DeleteProgramsARB, (void *)w_DeleteProgramsARB},
    {"glGenFragmentShadersATI", &x_gen_ati, (void *)w_GenFragmentShadersATI},
    {"glDeleteFragmentShaderATI", (void **)&x_DeleteFragmentShaderATI, (void *)w_DeleteFragmentShaderATI},
    {"glGenBuffersARB", &x_gen_buffers, (void *)w_GenBuffersARB},
    {"glDeleteBuffersARB", (void **)&x_DeleteBuffersARB, (void *)w_DeleteBuffersARB},
    {"glMapBufferARB", (void **)&x_MapBufferARB, (void *)w_MapBufferARB},
    {0, 0, 0},
};
static const char *g_need_delete[] = {"glDeleteProgramsARB", "glDeleteFragmentShaderATI", "glDeleteBuffersARB", 0};

/* Names that hand out objects we do not virtualize: logged when resolved. */
static const char *g_unvirt[] = {"glGenProgramsNV", "glGenFencesNV", "glGenVertexShadersEXT", "glGenSymbolsEXT",
                                  "wglCreatePbufferARB", "wglGetPbufferDCARB", "wglCreateBufferRegionARB",
                                  "wglAllocateMemoryNV", 0};

static char g_resolved[4096];
static void *GLAPI hk_wglGetProcAddress(const char *name) {
    void *p = r_wglGetProcAddress(name);
    if ((uintptr_t)name <= 0xffff) return p;
    u32 l = (u32)strlen(g_resolved), n = (u32)strlen(name);
    if (l + n + 12 < sizeof g_resolved) {
        if (l) g_resolved[l++] = ',';
        memcpy(g_resolved + l, name, n);
        l += n;
        if (!p) { memcpy(g_resolved + l, "(null)", 6); l += 6; }
        g_resolved[l] = 0;
    }
    for (u32 i = 0; g_unvirt[i]; i++)
        if (p && k_streq(name, g_unvirt[i])) slog("GL %s resolved (not virtualized)", name);
    if (!p) return p;
    /* a Gen wrapper needs its Delete even when the game never asks for it */
    for (u32 i = 0; g_need_delete[i]; i++) {
        for (xwrap_t *w = g_xw; w->name; w++)
            if (!*w->real && k_streq(w->name, g_need_delete[i])) *w->real = r_wglGetProcAddress(w->name);
    }
    for (xwrap_t *w = g_xw; w->name; w++)
        if (k_streq(name, w->name)) { *w->real = p; return w->wrap; }
    return p;
}

void gl_log_resolved(void) { slog("GL resolved: %s", g_resolved); }

#define HOOK(fn, h, save)                                                        \
    do {                                                                         \
        void *o = iat_hook(exe, "OPENGL32.dll", fn, (void *)(h));                \
        if (o) { *(void **)(save) = o; hooks_note("exe:" fn); }                  \
        else slog("iat miss exe %s", fn);                                        \
    } while (0)

void gl_install(void) {
    HMODULE exe = GetModuleHandleA(NULL);
    HOOK("glGenTextures", hk_glGenTextures, &r_glGenTextures);
    HOOK("glDeleteTextures", hk_glDeleteTextures, &r_glDeleteTextures);
    HOOK("glGenLists", hk_glGenLists, &r_glGenLists);
    HOOK("glDeleteLists", hk_glDeleteLists, &r_glDeleteLists);
    HOOK("glGetError", hk_glGetError, &r_glGetError);
    HOOK("wglGetProcAddress", hk_wglGetProcAddress, &r_wglGetProcAddress);
}
