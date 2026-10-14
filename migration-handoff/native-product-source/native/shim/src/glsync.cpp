/* GL objects and context state in the tick (contract 3.7).
 *
 * The driver's objects and context state live outside the sim set, yet the
 * game sees them: GLRender::MakeLookUpTexture rebuilds a texture glIsTexture
 * does not know, every pixel of a save's Screen.tga comes from textures,
 * programs and state (glClearColor is set once per area load), and the
 * program, fog and light state of the last frame carries into the next.
 * So each of them gets a key in harness_game, which the state root covers:
 *
 *   texture name -> sha256 of its descriptor: target, parameters, and per
 *                   image (face, level) its format, size and a data blob,
 *                   read back from the driver at the gate after any change;
 *   ARB program  -> sha256 of {target, format, string} from glProgramStringARB;
 *   context      -> sha256 of the context state read back at the gate:
 *                   enables, functions, masks, fog, lights, materials,
 *                   matrices, viewport, texture units, client arrays,
 *                   program bindings and environment parameters;
 *   ATI shaders  -> the bound shader and per-shader constants, kept in
 *                   harness_game directly (the driver cannot be asked).
 *
 * Descriptors and blobs are content-addressed files the shim writes to
 * <session>/gl/ once per process; Python keeps them in the bundle. A restore
 * rewinds the keys with the pages; then gl.need lists every key the driver
 * does not hold (harness_core remembers what it built or read back) and
 * gl.apply rebuilds those objects and the context from the blobs, reads each
 * one back and checks its key. The driver then matches tick T as far as the
 * game or a frame can tell.
 *
 * Display lists, buffer objects, render-to-texture copies and pbuffers are not
 * used on this path (the gl census); ATI fragment shaders are all defined
 * before tick 0, the same in every process. A definition after tick 0 is
 * logged as unrestorable. */
#include "shim.hpp"

/* Fixed C hook ABI and assembly-referenced symbol names. */
extern "C" {

#define GLAPI __attribute__((stdcall))
typedef unsigned int GLenum_;
typedef unsigned int GLuint_;
typedef int GLsizei_;
typedef int GLint_;
typedef unsigned char GLboolean_;
typedef float GLfloat_;
typedef double GLdouble_;

#define K_TEX 1
#define K_PROG 2
#define K_CTX 3
#define DESC_MAGIC_TEX 0x5447504Bu    /* "KPGT" */
#define DESC_MAGIC_PROG 0x5047504Bu   /* "KPGP" */
#define DESC_MAGIC_CTX 0x4347504Bu    /* "KPGC" */
static constexpr u32 CTX_VERSION = 6; /* v5 omitted combiner and unit LOD bias state */
#define SET_MAGIC 0x4247504Bu         /* "KPGB": one blob in a gl.apply set */

/* Capture failure is terminal: no stale descriptor/root may be published.
 * This is intentionally not a retry transaction after arbitrary driver reads. */
static __attribute__((noreturn)) void capture_abort(const char *why) {
    slog("glsync: capture refused: %s",why);
    TerminateProcess(GetCurrentProcess(),6);for(;;){}
}

/* ---------------------------------------------------------------- real entry points */

#define GLFN(ret, name, args) static ret (GLAPI *p_##name) args;
#define GL_CORE(X) \
    X(void, glGetIntegerv, (GLenum_, GLint_ *)) \
    X(void, glGetFloatv, (GLenum_, GLfloat_ *)) \
    X(void, glGetDoublev, (GLenum_, GLdouble_ *)) \
    X(void, glGetBooleanv, (GLenum_, GLboolean_ *)) \
    X(GLboolean_, glIsEnabled, (GLenum_)) \
    X(GLenum_, glGetError, (void)) \
    X(void, glGetPointerv, (GLenum_, void **)) \
    X(void, glGetLightfv, (GLenum_, GLenum_, GLfloat_ *)) \
    X(void, glGetMaterialfv, (GLenum_, GLenum_, GLfloat_ *)) \
    X(void, glGetTexGeniv, (GLenum_, GLenum_, GLint_ *)) \
    X(void, glGetTexGenfv, (GLenum_, GLenum_, GLfloat_ *)) \
    X(void, glGetTexEnviv, (GLenum_, GLenum_, GLint_ *)) \
    X(void, glGetTexEnvfv, (GLenum_, GLenum_, GLfloat_ *)) \
    X(void, glGetTexParameteriv, (GLenum_, GLenum_, GLint_ *)) \
    X(void, glGetTexParameterfv, (GLenum_, GLenum_, GLfloat_ *)) \
    X(void, glGetTexLevelParameteriv, (GLenum_, GLint_, GLenum_, GLint_ *)) \
    X(void, glGetTexImage, (GLenum_, GLint_, GLenum_, GLenum_, void *)) \
    X(const u8 *, glGetString, (GLenum_)) \
    X(void, glBindTexture, (GLenum_, GLuint_)) \
    X(void, glDeleteTextures, (GLsizei_, const GLuint_ *)) \
    X(void, glTexImage1D, (GLenum_,GLint_,GLint_,GLsizei_,GLint_,GLenum_,GLenum_,const void *)) \
    X(void, glTexImage2D, (GLenum_, GLint_, GLint_, GLsizei_, GLsizei_, GLint_, GLenum_, GLenum_, const void *)) \
    X(void, glTexParameteri, (GLenum_, GLenum_, GLint_)) \
    X(void, glTexParameterf, (GLenum_, GLenum_, GLfloat_)) \
    X(void, glTexParameterfv, (GLenum_, GLenum_, const GLfloat_ *)) \
    X(void, glPixelStorei, (GLenum_, GLint_)) \
    X(void, glEnable, (GLenum_)) \
    X(void, glDisable, (GLenum_)) \
    X(void, glEnableClientState, (GLenum_)) \
    X(void, glDisableClientState, (GLenum_)) \
    X(void, glAlphaFunc, (GLenum_, GLfloat_)) \
    X(void, glBlendFunc, (GLenum_, GLenum_)) \
    X(void, glDepthFunc, (GLenum_)) \
    X(void, glDepthMask, (GLboolean_)) \
    X(void, glClearDepth, (GLdouble_)) \
    X(void, glDepthRange, (GLdouble_, GLdouble_)) \
    X(void, glColorMask, (GLboolean_, GLboolean_, GLboolean_, GLboolean_)) \
    X(void, glClearColor, (GLfloat_, GLfloat_, GLfloat_, GLfloat_)) \
    X(void, glStencilFunc, (GLenum_, GLint_, GLuint_)) \
    X(void, glStencilOp, (GLenum_, GLenum_, GLenum_)) \
    X(void, glStencilMask, (GLuint_)) \
    X(void, glClearStencil, (GLint_)) \
    X(void, glCullFace, (GLenum_)) \
    X(void, glFrontFace, (GLenum_)) \
    X(void, glPolygonMode, (GLenum_, GLenum_)) \
    X(void, glPolygonOffset, (GLfloat_, GLfloat_)) \
    X(void, glShadeModel, (GLenum_)) \
    X(void, glLineWidth, (GLfloat_)) \
    X(void, glPointSize, (GLfloat_)) \
    X(void, glLightModelfv, (GLenum_, const GLfloat_ *)) \
    X(void, glLightModeli, (GLenum_, GLint_)) \
    X(void, glColorMaterial, (GLenum_, GLenum_)) \
    X(void, glFogi, (GLenum_, GLint_)) \
    X(void, glFogf, (GLenum_, GLfloat_)) \
    X(void, glFogfv, (GLenum_, const GLfloat_ *)) \
    X(void, glViewport, (GLint_, GLint_, GLsizei_, GLsizei_)) \
    X(void, glScissor, (GLint_, GLint_, GLsizei_, GLsizei_)) \
    X(void, glPopAttrib, (void)) \
    X(void, glPopClientAttrib, (void)) \
    X(void, glPopMatrix, (void)) \
    X(void, glMatrixMode, (GLenum_)) \
    X(void, glLoadMatrixf, (const GLfloat_ *)) \
    X(void, glLoadIdentity, (void)) \
    X(void, glLightfv, (GLenum_, GLenum_, const GLfloat_ *)) \
    X(void, glMaterialfv, (GLenum_, GLenum_, const GLfloat_ *)) \
    X(void, glTexGeni, (GLenum_, GLenum_, GLint_)) \
    X(void, glTexGenfv, (GLenum_, GLenum_, const GLfloat_ *)) \
    X(void, glTexEnvi, (GLenum_, GLenum_, GLint_)) \
    X(void, glTexEnvfv, (GLenum_, GLenum_, const GLfloat_ *)) \
    X(void, glDrawBuffer, (GLenum_)) \
    X(void, glReadBuffer, (GLenum_)) \
    X(void, glColor4fv, (const GLfloat_ *)) \
    X(void, glNormal3fv, (const GLfloat_ *)) \
    X(void, glVertexPointer, (GLint_, GLenum_, GLsizei_, const void *)) \
    X(void, glNormalPointer, (GLenum_, GLsizei_, const void *)) \
    X(void, glColorPointer, (GLint_, GLenum_, GLsizei_, const void *)) \
    X(void, glTexCoordPointer, (GLint_, GLenum_, GLsizei_, const void *))
#define GL_EXT(X) \
    X(void, glBindBufferARB, (GLenum_, GLuint_)) \
    X(void, glActiveTextureARB, (GLenum_)) \
    X(void, glClientActiveTextureARB, (GLenum_)) \
    X(void, glMultiTexCoord4fvARB, (GLenum_, const GLfloat_ *)) \
    X(void, glGetCompressedTexImageARB, (GLenum_, GLint_, void *)) \
    X(void, glCompressedTexImage1DARB, (GLenum_,GLint_,GLenum_,GLsizei_,GLint_,GLsizei_,const void *)) \
    X(void, glCompressedTexImage2DARB, (GLenum_, GLint_, GLenum_, GLsizei_, GLsizei_, GLint_, GLsizei_, const void *)) \
    X(void, glBindProgramARB, (GLenum_, GLuint_)) \
    X(void, glProgramStringARB, (GLenum_, GLenum_, GLsizei_, const void *)) \
    X(void, glDeleteProgramsARB, (GLsizei_, const GLuint_ *)) \
    X(void, glGetProgramStringARB, (GLenum_,GLenum_,void *)) \
    X(void, glGetProgramivARB, (GLenum_, GLenum_, GLint_ *)) \
    X(void, glGetProgramEnvParameterfvARB, (GLenum_, GLuint_, GLfloat_ *)) \
    X(void, glProgramEnvParameter4fvARB, (GLenum_, GLuint_, const GLfloat_ *)) \
    X(void, glGetProgramLocalParameterdvARB, (GLenum_,GLuint_,GLdouble_ *)) \
    X(void, glProgramLocalParameter4dvARB, (GLenum_,GLuint_,const GLdouble_ *)) \
    X(void, glGetVertexAttribdvARB, (GLuint_,GLenum_,GLdouble_ *)) \
    X(void, glVertexAttrib4dvARB, (GLuint_,const GLdouble_ *)) \
    X(void, glGetVertexAttribivARB, (GLuint_, GLenum_, GLint_ *)) \
    X(void, glGetVertexAttribPointervARB, (GLuint_, GLenum_, void **)) \
    X(void, glVertexAttribPointerARB, (GLuint_, GLint_, GLenum_, GLboolean_, GLsizei_, const void *)) \
    X(void, glEnableVertexAttribArrayARB, (GLuint_)) \
    X(void, glDisableVertexAttribArrayARB, (GLuint_)) \
    X(void, glBindFragmentShaderATI, (GLuint_)) \
    X(void, glSetFragmentShaderConstantATI, (GLuint_, const GLfloat_ *))
GL_CORE(GLFN)
GL_EXT(GLFN)

static int g_resolved;            /* 1 resolved, -1 failed */
static int g_has_aniso;
enum { TEXENV_COMBINE = 1u, TEXENV_LOD_BIAS = 2u };
static u32 g_texenv_caps;
static u32 g_stencil_caps; /* bit0 EXT_two_side, bit1 core separate stencil */
static void (GLAPI *p_glActiveStencilFaceEXT)(GLenum_);
static void (GLAPI *p_glStencilFuncSeparate)(GLenum_,GLenum_,GLint_,GLuint_);
static void (GLAPI *p_glStencilOpSeparate)(GLenum_,GLenum_,GLenum_,GLenum_);
static void (GLAPI *p_glStencilMaskSeparate)(GLenum_,GLuint_);
#define GL_ACTIVE_TEXTURE_ARB_ 0x84E0u
#define GL_TEXTURE0_ARB_ 0x84C0u
#define TS_UNITS 8u /* units captured by the context walk and shadowed by the texture shadow */
static u32 g_units, g_lights, g_attribs, g_env[2],g_local[2];

static int driver_extension(const char *extensions,const char *name) {
    if(!extensions)return 0;
    u32 n=static_cast<u32>(strlen(name));const char *p=extensions;
    while((p=k_strstr(p,name))) {
        if((p==extensions||p[-1]==' ')&&(!p[n]||p[n]==' '))return 1;
        p+=n;
    }
    return 0;
}
static int driver_version_at_least(const char *version,u32 major,u32 minor) {
    if(!version||version[0]<'0'||version[0]>'9')return 0;
    const char *end;u64 actual_major=k_parse_u64(version,&end);
    if(*end!='.'||end[1]<'0'||end[1]>'9')return 0;
    u64 actual_minor=k_parse_u64(end+1,nullptr);
    return actual_major>major||(actual_major==major&&actual_minor>=minor);
}

static int resolve(void) {
    if (g_resolved) return g_resolved > 0;
    HMODULE gl = GetModuleHandleA("opengl32.dll");
    if (!gl) return 0;
    void *(GLAPI *wgp)(const char *) = (void *(GLAPI *)(const char *))(void *)GetProcAddress(gl, "wglGetProcAddress");
    HGLRC (WINAPI *cur)(void) = (HGLRC (WINAPI *)(void))(void *)GetProcAddress(gl, "wglGetCurrentContext");
    if (!wgp || !cur || !cur()) return 0;     /* no context yet: try again at the next gate */
    int ok = 1;
#define RCORE(ret, name, args) if (!(p_##name = (ret (GLAPI *) args)(void *)GetProcAddress(gl, #name))) { slog("glsync: %s missing", #name); ok = 0; }
#define REXT(ret, name, args) do { void *p=wgp(#name); if((uintptr_t)p<=3 || (uintptr_t)p==0xffffffffu)p=NULL; p_##name=(ret (GLAPI *) args)p; if(!p) { slog("glsync: %s missing", #name); ok=0; } } while(0);
    GL_CORE(RCORE)
    GL_EXT(REXT)
    g_resolved = ok ? 1 : -1;
    if (!ok) {
        slog("glsync: refusing capture with unresolved GL entry points in a current context");
        TerminateProcess(GetCurrentProcess(),6);for(;;){}
    }
    GLenum_ pending=p_glGetError();
    if(pending && !SH->gl_err)SH->gl_err=pending;
    const char *ext = (const char *)p_glGetString(0x1F03);
    g_has_aniso = ext && k_strstr(ext, "GL_EXT_texture_filter_anisotropic") != 0;
    const char *version=(const char*)p_glGetString(0x1f02);
    /* Query the real driver, not the game's virtual extension/version strings.
     * Core 1.3/1.4 and the extension forms share these state tokens. */
    if(!driver_version_at_least(version,1,0))
        capture_abort("desktop GL version required for context capture");
    g_texenv_caps=(driver_version_at_least(version,1,3)||
        driver_extension(ext,"GL_ARB_texture_env_combine")||
        driver_extension(ext,"GL_EXT_texture_env_combine")?TEXENV_COMBINE:0u)|
        (driver_version_at_least(version,1,4)||
        driver_extension(ext,"GL_EXT_texture_lod_bias")?TEXENV_LOD_BIAS:0u);
    g_stencil_caps=(ext&&k_strstr(ext,"GL_EXT_stencil_two_side")?1u:0u)|
        (version&&version[0]>='2'&&version[0]<='9'?2u:0u);
#define STENCIL_PROC(name) do {void *p=wgp(#name);if((uintptr_t)p<=3||(uintptr_t)p==0xffffffffu)capture_abort("stencil entrypoint missing");p_##name=reinterpret_cast<decltype(p_##name)>(p);}while(0)
    if(g_stencil_caps&1u)STENCIL_PROC(glActiveStencilFaceEXT);
    if(g_stencil_caps&2u) {
        STENCIL_PROC(glStencilFuncSeparate);STENCIL_PROC(glStencilOpSeparate);STENCIL_PROC(glStencilMaskSeparate);
    }
#undef STENCIL_PROC
    GLint_ v = 0;
    p_glGetIntegerv(0x84E2, &v); g_units = v > (GLint_)TS_UNITS ? TS_UNITS : (u32)v;            /* MAX_TEXTURE_UNITS_ARB */
    p_glGetIntegerv(0x0D31, &v); g_lights = v > 8 ? 8 : (u32)v;           /* MAX_LIGHTS */
    p_glGetIntegerv(0x8869, &v); g_attribs = v > 16 ? 16 : (u32)v;        /* MAX_VERTEX_ATTRIBS_ARB */
    p_glGetProgramivARB(0x8620, 0x88B5, &v); g_env[0] = v > 256 ? 256 : (u32)v;   /* MAX_PROGRAM_ENV_PARAMETERS_ARB */
    p_glGetProgramivARB(0x8804, 0x88B5, &v); g_env[1] = v > 256 ? 256 : (u32)v;
    for(u32 t=0;t<2;t++) {
        p_glGetProgramivARB(t?0x8804:0x8620,0x88b4,&v);
        if(v<=0||v>4096)capture_abort("program local parameter capacity unsupported");
        g_local[t]=(u32)v;
    }
    GLenum_ e = p_glGetError();
    slog("glsync: units %u lights %u attribs %u env %u/%u aniso %d (error after setup %x)", g_units, g_lights,
         g_attribs, g_env[0], g_env[1], g_has_aniso, e);
    if(e)capture_abort("GL capability query failed");
    return 1;
}

/* ---------------------------------------------------------------- harness_core side */

static u8 (*g_real_tex)[32];      /* what the driver holds per texture name (zero: nothing known) */
static u8 (*g_real_prog)[32];
static u32 g_real_prog_target[GL_PROGMAX]; /* physical lifetime, including empty programs */
static u8 g_real_ctx[32];
static u8 *g_dirty_px, *g_dirty_par;
static u32 g_dirty_any;
static u8 (*g_emitted)[32];       /* blobs this process has written (open addressing) */
#define EMIT_SLOTS 65536u
static u32 g_emitted_slots, g_emitted_count, g_emitted_failed_slots;
static volatile LONG g_emitted_lock;
static u8 *g_buf;                 /* descriptor scratch */
#define BUF_SIZE (2u << 20)
static u8 *g_img;                 /* one image read back; grows to the largest level seen */
static u32 g_img_cap;
static u8 *g_setbuf;              /* the descriptor of one group of a gl.apply set */
#define SETBUF_SIZE (1u << 20)
static char g_gldir[300];
/* Transport state is host-owned, outside SH and all captured roots. */
static int g_spool_mode, g_spool_active;
static HANDLE g_spool_handle = INVALID_HANDLE_VALUE;
static u64 g_spool_sequence, g_spool_bytes;
static u32 g_spool_count;
static u8 g_spool_keys[8192][32];
static char g_spool_tmp[400], g_spool_ready[400];
#define GL_SPOOL_MAX_BYTES (256u << 20)
#define GL_SPOOL_MAX_BLOB (64u << 20)
void glsync_spool_configure(int enabled) { g_spool_mode = enabled; }
u32 g_gl_captures, g_gl_blobs, g_gl_rebuilt, g_gl_verify_bad, g_gl_unrestorable;
static u64 g_gl_bytes;
static int g_ati_defining;

/* Read-only wall-time totals outside the captured state. These identify the
 * expensive capture phase without changing gate clocks or verification. */
enum { GLP_OBJECTS,GLP_TEXTURES,GLP_CONTEXT,GLP_FRAMEBUFFER,GLP_TOTAL,
       GLP_BLOB_WRITE,GLP_BLOB_FLUSH,GLP_BLOB_PUBLISH,GLP_TEXTURE_READ,GLP_TEXTURE_HASH,
       GLP_FB_FRONT_READ,GLP_FB_BACK_READ,GLP_FB_DEPTH_READ,GLP_FB_STENCIL_READ,
       GLP_FB_FRONT_HASH,GLP_FB_BACK_HASH,GLP_FB_DEPTH_HASH,GLP_FB_STENCIL_HASH,
       GLP_FB_DESCRIPTOR_HASH,GLP_FB_HASH_PARALLEL,GLP_COUNT };
static const char *GL_PHASE_NAMES[]={"objects","textures","context","framebuffer","total",
    "blob_write","blob_flush","blob_publish","texture_read","texture_hash",
    "framebuffer_front_read","framebuffer_back_read","framebuffer_depth_read","framebuffer_stencil_read",
    "framebuffer_front_hash","framebuffer_back_hash","framebuffer_depth_hash","framebuffer_stencil_hash",
    "framebuffer_descriptor_hash","framebuffer_hash_parallel"};
static_assert(sizeof GL_PHASE_NAMES/sizeof GL_PHASE_NAMES[0]==GLP_COUNT,"GL phase names");
static capture_profile_t g_gl_phase[GLP_COUNT];
/* Independent opt-in: no KPTAS_PROFILE sampling thread is required. Cache the
 * launch environment once, outside SH and the captured game address space. */
static int g_gl_profile_checked,g_gl_profile_enabled;
int glsync_profile_enabled(void) {
    if(!g_gl_profile_checked) {
        DWORD last_error=GetLastError();char value[2];
        DWORD n=GetEnvironmentVariableA("KPTAS_GL_PROFILE",value,sizeof value);
        g_gl_profile_enabled=n==1&&value[0]=='1';g_gl_profile_checked=1;
        SetLastError(last_error);
    }
    return g_gl_profile_enabled;
}
static u64 g_gl_qpf;
u64 glsync_profile_now(void) {
    if(!glsync_profile_enabled())return 0;
    DWORD last_error=GetLastError();LARGE_INTEGER value;
    if(!g_gl_qpf&&QueryPerformanceFrequency(&value)&&value.QuadPart>0)g_gl_qpf=(u64)value.QuadPart;
    u64 tick=QueryPerformanceCounter(&value)&&value.QuadPart>0?(u64)value.QuadPart:0;
    SetLastError(last_error);return tick;
}
void glsync_profile_record(capture_profile_t *phase,u64 start,u64 end) {
    if(!g_gl_profile_enabled)return;
    if(!g_gl_qpf||!start||end<start)return;
    u64 delta=end-start;
    u64 us=(delta/g_gl_qpf)*1000000ull+(delta%g_gl_qpf)*1000000ull/g_gl_qpf;
    phase->calls++;phase->total_us+=us;
    if(us>phase->max_us)phase->max_us=us;
}
static void gl_profile_record(u32 phase,u64 start,u64 end) {
    if(phase<GLP_COUNT)glsync_profile_record(&g_gl_phase[phase],start,end);
}


static constexpr u8 ZERO32[32]{};

static int img_room(u32 need) {
    if (need <= g_img_cap) return 1;
    if(need>0xffff0000u)return 0;
    if (g_img) VirtualFree(g_img, 0, MEM_RELEASE);
    g_img_cap = (need + 0xffffu) & ~0xffffu;
    g_img = (u8 *)VirtualAlloc(0, g_img_cap, MEM_RESERVE | MEM_COMMIT, PAGE_READWRITE);
    if (!g_img) g_img_cap = 0;
    return g_img != 0;
}

/* The table is an optional process-local persistence cache, not captured GL
 * state. Serialize lookup/rehash so no reader can retain a freed table. It
 * survives context recreation: a durable blob does not depend on its context. */
static void emitted_lock(void) {
    while(InterlockedCompareExchange(&g_emitted_lock,1,0))Sleep(0);
}
static void emitted_unlock(void) { InterlockedExchange(&g_emitted_lock,0); }
static u32 emitted_hash(const u8 key[32],u32 slots) {
    return ((u32)key[0]|((u32)key[1]<<8)|((u32)key[2]<<16)|((u32)key[3]<<24))&(slots-1);
}
/* Caller holds the lock. Failure keeps the old table and never admits an
 * unpersisted blob; callers simply persist future uncached keys again. */
static int emitted_grow(void) {
    u32 old_slots=g_emitted_slots;
    if(g_emitted_failed_slots==old_slots && old_slots)return 0;
    if(old_slots>=0x02000000u)return 0; /* cap allocation arithmetic at 1 GiB */
    u32 slots=old_slots?old_slots*2:EMIT_SLOTS;
    u8 (*table)[32]=(u8 (*)[32])VirtualAlloc(0,slots*32,MEM_RESERVE|MEM_COMMIT,PAGE_READWRITE);
    if(!table){g_emitted_failed_slots=old_slots;return 0;}
    for(u32 i=0;i<old_slots;i++) {
        if(!memcmp(g_emitted[i],ZERO32,32))continue;
        u32 h=emitted_hash(g_emitted[i],slots);
        while(memcmp(table[h],ZERO32,32))h=(h+1)&(slots-1);
        memcpy(table[h],g_emitted[i],32);
    }
    u8 (*old)[32]=g_emitted;
    g_emitted=table;g_emitted_slots=slots;g_emitted_failed_slots=0;
    if(old)VirtualFree(old,0,MEM_RELEASE);
    return 1;
}

static int core_ready(void) {
    if (g_real_tex) return 1;
    g_real_tex = (u8 (*)[32])VirtualAlloc(0, GL_TEXMAX * 32, MEM_RESERVE | MEM_COMMIT, PAGE_READWRITE);
    g_real_prog = (u8 (*)[32])VirtualAlloc(0, GL_PROGMAX * 32, MEM_RESERVE | MEM_COMMIT, PAGE_READWRITE);
    g_dirty_px = (u8 *)VirtualAlloc(0, GL_TEXMAX * 2, MEM_RESERVE | MEM_COMMIT, PAGE_READWRITE);
    emitted_lock();if(!g_emitted_slots)emitted_grow();emitted_unlock();
    g_buf = (u8 *)VirtualAlloc(0, BUF_SIZE, MEM_RESERVE | MEM_COMMIT, PAGE_READWRITE);
    g_setbuf = (u8 *)VirtualAlloc(0, SETBUF_SIZE, MEM_RESERVE | MEM_COMMIT, PAGE_READWRITE);
    if (!g_real_tex || !g_real_prog || !g_dirty_px || !g_buf || !g_setbuf) { g_real_tex = 0; return 0; }
    g_dirty_par = g_dirty_px + GL_TEXMAX;
    return 1;
}

static int emitted(const u8 key[32], int add) {
    /* Zero is the empty-slot sentinel, so even that digest falls back to IO. */
    if(!memcmp(key,ZERO32,32))return 0;
    emitted_lock();
    if(!g_emitted_slots){emitted_unlock();return 0;}
    u32 h=emitted_hash(key,g_emitted_slots);
    while(memcmp(g_emitted[h],ZERO32,32)) {
        if(!memcmp(g_emitted[h],key,32)){emitted_unlock();return 1;}
        h=(h+1)&(g_emitted_slots-1);
    }
    if(add) {
        if(g_emitted_count>=g_emitted_slots-g_emitted_slots/4) {
            /* Never fill the table when growth fails: absent-key lookups must
             * still find an empty slot, and persistence remains the fallback. */
            if(!emitted_grow()){emitted_unlock();return 0;}
            h=emitted_hash(key,g_emitted_slots);
            while(memcmp(g_emitted[h],ZERO32,32))h=(h+1)&(g_emitted_slots-1);
        }
        memcpy(g_emitted[h],key,32);g_emitted_count++;
    }
    emitted_unlock();
    return 0;
}

static void hexkey(char *out, const u8 key[32]) {
    static const char hx[] = "0123456789abcdef";
    for (u32 i = 0; i < 32; i++) { out[2 * i] = hx[key[i] >> 4]; out[2 * i + 1] = hx[key[i] & 15]; }
    out[64] = 0;
}

static void spool_u32(u8 *p,u32 n) { for(u32 i=0;i<4;i++)p[i]=(u8)(n>>(8*i)); }
static void spool_u64(u8 *p,u64 n) { for(u32 i=0;i<8;i++)p[i]=(u8)(n>>(8*i)); }
static void spool_write(const void *p,u32 n) {
    DWORD wrote=0;u64 stamp=glsync_profile_now();
    BOOL ok=WriteFile(g_spool_handle,p,n,&wrote,0)&&wrote==n;
    gl_profile_record(GLP_BLOB_WRITE,stamp,glsync_profile_now());
    if(!ok)capture_abort("cannot write GL frame spool");
    g_spool_bytes+=n;
}
static void spool_open(void) {
    if(g_spool_sequence==~(u64)0)capture_abort("GL spool sequence exhausted");
    g_spool_sequence++;
    u32 l=(u32)strlen(g_gldir);
    memcpy(g_spool_ready,g_gldir,l);memcpy(g_spool_ready+l,"\\frame-",7);
    static const char hx[]="0123456789abcdef";
    for(u32 i=0;i<16;i++)g_spool_ready[l+7+i]=hx[(g_spool_sequence>>(60-4*i))&15];
    memcpy(g_spool_ready+l+23,".kpgs",6);
    memcpy(g_spool_tmp,g_spool_ready,l+29);memcpy(g_spool_tmp+l+28,".tmp",5);
    g_spool_handle=CreateFileA(g_spool_tmp,GENERIC_WRITE,0,0,CREATE_NEW,0,0);
    if(g_spool_handle==INVALID_HANDLE_VALUE)capture_abort("cannot create GL frame spool");
    u8 header[32]={0};memcpy(header,"KPGS0001",8);
    spool_u32(header+8,1);spool_u32(header+12,32);
    spool_u64(header+16,g_spool_sequence);spool_u64(header+24,g_gate.tick);
    g_spool_bytes=0;spool_write(header,sizeof header);
}
static void spool_emit(const u8 key[32],const void *p,u32 n) {
    for(u32 i=0;i<g_spool_count;i++)if(!memcmp(g_spool_keys[i],key,32))return;
    /* Account for the new header before validating its entry/footer budget. */
    if(g_spool_handle==INVALID_HANDLE_VALUE)spool_open();
    if(n>GL_SPOOL_MAX_BLOB||g_spool_count>=8192||
       g_spool_bytes>GL_SPOOL_MAX_BYTES-68u||n>GL_SPOOL_MAX_BYTES-68u-g_spool_bytes)
        capture_abort("GL frame spool exceeds transport bounds");
    u8 entry[36];memcpy(entry,key,32);spool_u32(entry+32,n);
    spool_write(entry,sizeof entry);spool_write(p,n);
    memcpy(g_spool_keys[g_spool_count++],key,32);
}
static void spool_finish(void) {
    if(g_spool_handle==INVALID_HANDLE_VALUE){g_spool_active=0;return;}
    u8 footer[32]={0};memcpy(footer,"KPGSEND1",8);
    spool_u64(footer+8,g_spool_bytes+32);spool_u32(footer+16,g_spool_count);
    spool_u64(footer+24,g_spool_sequence);spool_write(footer,sizeof footer);
    u64 stamp=glsync_profile_now();BOOL ok=FlushFileBuffers(g_spool_handle);
    gl_profile_record(GLP_BLOB_FLUSH,stamp,glsync_profile_now());
    if(!CloseHandle(g_spool_handle))ok=FALSE;
    g_spool_handle=INVALID_HANDLE_VALUE;
    if(ok){stamp=glsync_profile_now();ok=MoveFileExA(g_spool_tmp,g_spool_ready,MOVEFILE_WRITE_THROUGH);
        gl_profile_record(GLP_BLOB_PUBLISH,stamp,glsync_profile_now());}
    if(!ok)capture_abort("cannot publish GL frame spool");
    /* A ready, flushed spool is the persistence receipt, never an entry write. */
    for(u32 i=0;i<g_spool_count;i++)emitted(g_spool_keys[i],1);
    g_gl_blobs+=g_spool_count;g_gl_bytes+=g_spool_bytes-64u-36u*g_spool_count;
    g_spool_active=0;g_spool_count=0;g_spool_bytes=0;
}

/* Write one blob to <session>/gl/<sha256> unless this process already did. */
static void emit(const u8 key[32], const void *p, u32 n) {
    if(emitted(key,0))return;
    if(!g_session_dir[0])capture_abort("blob session path missing");
    if (!g_gldir[0]) {
        u32 l = (u32)strlen(g_session_dir);
        if (l + 80 > sizeof g_gldir) capture_abort("blob session path too long");
        memcpy(g_gldir, g_session_dir, l);
        memcpy(g_gldir + l, "\\gl", 4);
        CreateDirectoryA(g_gldir, 0);
    }
    if(g_spool_active && n<=GL_SPOOL_MAX_BLOB){
        if(g_spool_count>=8192 || g_spool_bytes>GL_SPOOL_MAX_BYTES-68u ||
           n>GL_SPOOL_MAX_BYTES-68u-g_spool_bytes){
            spool_finish();g_spool_active=1;
        }
        spool_emit(key,p,n);return;
    }
    /* Large legitimate textures retain the existing per-digest transport;
     * the optimization must not narrow the game's admitted GL dimensions. */
    char path[400];
    u32 l = (u32)strlen(g_gldir);
    memcpy(path, g_gldir, l);
    path[l] = '\\';
    hexkey(path + l + 1, key);
    memcpy(path + l + 65, ".tmp", 5);
    HANDLE h = CreateFileA(path, GENERIC_WRITE, 0, 0, CREATE_ALWAYS, 0, 0);
    if (h == INVALID_HANDLE_VALUE) capture_abort("cannot create GL blob");
    DWORD w = 0;
    u64 stamp=glsync_profile_now();
    BOOL ok = WriteFile(h, p, n, &w, 0) && w == n;
    gl_profile_record(GLP_BLOB_WRITE,stamp,glsync_profile_now());
    if(ok) {
        stamp=glsync_profile_now();ok=FlushFileBuffers(h);
        gl_profile_record(GLP_BLOB_FLUSH,stamp,glsync_profile_now());
    }
    if(!CloseHandle(h))ok=FALSE;
    char fin[400];
    memcpy(fin, path, l + 65);
    fin[l + 65] = 0;
    if(ok) {
        stamp=glsync_profile_now();ok=MoveFileExA(path,fin,MOVEFILE_REPLACE_EXISTING);
        gl_profile_record(GLP_BLOB_PUBLISH,stamp,glsync_profile_now());
    }
    if(!ok)capture_abort("cannot persist GL blob");
    emitted(key, 1);
    g_gl_blobs++;
    g_gl_bytes += n;
}

/* ---------------------------------------------------------------- byte builder */

typedef struct { u8 *p; u32 n, cap; int over; } bb_t;
static void bb_raw(bb_t *b, const void *s, u32 n) {
    if (b->n>b->cap || n > b->cap-b->n) { b->over = 1; return; }
    memcpy(b->p + b->n, s, n);
    b->n += n;
}
static void bb_u32(bb_t *b, u32 v) { bb_raw(b, &v, 4); }

/* ---------------------------------------------------------------- game-side tracking */

/* Texture targets as SH->gl_tex_target encodes them: 1 2D, 2 cube map, 3 1D
 * (0 none). A cube face is kind 2; a face is still told apart where GL treats
 * it differently (parameters are set on the cube target, never a face). Any
 * other target is kind 0; every caller has validated its target first. */
#define TEX_KIND_2D 1u
#define TEX_KIND_CUBE 2u
#define TEX_KIND_1D 3u
#define TEX_KINDS 3u
static int tex_is_face(GLenum_ target) { return target >= 0x8515 && target <= 0x851A; }
static u32 tex_kind(GLenum_ target) {
    return tex_is_face(target) || target == 0x8513 ? TEX_KIND_CUBE :
           target == 0x0DE0 ? TEX_KIND_1D : target == 0x0DE1 ? TEX_KIND_2D : 0u;
}

static GLuint_ bound_tex_driver(GLenum_ target) {
    GLint_ v = 0;
    u32 kind = tex_kind(target);
    GLenum_ q = kind == TEX_KIND_CUBE ? 0x8514 : kind == TEX_KIND_1D ? 0x8068 : 0x8069;
    p_glGetIntegerv(q, &v);
    return gl_tex_logical_name((u32)v);
}

/* Texture shadow (host state, never captured): the active unit, the logical
 * name bound per (unit, target), and the last value the game set per (logical
 * texture, pname). It replaces only the shim's own read-only driver queries;
 * every game GL call still reaches the driver unchanged.
 *
 * An entry is valid while its generation equals the current one.
 * g_ts_bind_gen covers the active unit and bindings; g_ts_param_gen covers
 * parameter values (a capture rebinds textures but writes no parameter).
 * Generation 0 is reserved for "invalid": entries start zeroed, a refused
 * slot is set to 0, and both counters start at 1 and only increase. They are
 * u64 and never wrap.
 *
 * Writers outside the game hooks bump them: glPopAttrib (TEXTURE_BIT restores
 * bindings, the active unit and bound objects' parameters), texture deletion
 * and real-object discard, glPrioritizeTextures, wglMakeCurrent and any
 * current-context change, real context reset, and glsync capture/apply entry
 * and exit (they bind, activate, pop and set parameters through p_gl*
 * directly). Whether a bump comes before or after the driver call it
 * accompanies does not matter: no lookup can run between the two.
 * render.cpp check_draw needs no bump: it restores the active unit it sets.
 *
 * Display lists need nothing: gllists' guard refuses every command but
 * glBitmap/glGet/glIs/glPixelStore inside a definition and
 * COMPILE_AND_EXECUTE dies. glTexParameterf/fv/iv, glActiveTexture and
 * glCallList are absent from glenv's supported table, so the game cannot
 * reach them unhooked. Native mode installs neither boundary: the shadow is
 * off there and every lookup queries the driver as before.
 *
 * Threading: no lock. Every access accompanies a GL call on the calling
 * thread: the game's hooked call, or capture/apply at the gate, where
 * resolve() requires a current context on that thread. Like SH->wh.current_gl
 * and gldraw's logical attribute stacks, the shadow assumes one thread issues
 * GL at a time. Nothing here excludes a concurrent GL call from a second game
 * thread (off_main only logs one). */
int g_gl_shadow_verify; /* KPTAS_GL_SHADOW_VERIFY=1: every hit is checked against the driver */
#define TP_BITS 14u
#define TP_SLOTS (1u << TP_BITS)
#define TS_HASH_MUL 0x9E3779B1u /* golden-ratio multiplicative hash */
/* The parameter key packs the logical name into the high 16 bits. */
static_assert(GL_TEXMAX <= 65536u, "texture parameter shadow key needs 16-bit logical names");
static u64 g_ts_bind_gen = 1, g_ts_param_gen = 1;
static u32 g_ts_ctx, g_ts_active;
static u64 g_ts_active_gen;
typedef struct { u64 gen; u32 name; } ts_bind_t;
static ts_bind_t g_ts_bind[TS_UNITS][TEX_KINDS];
typedef struct { u64 gen; u32 key; GLint_ value; } ts_param_t;
static ts_param_t g_ts_param[TP_SLOTS];
/* Verify mode only: the driver value read before a game call shadowed as
 * "same", compared after that call returns. */
static struct { u32 armed; GLenum_ target, pname; GLint_ before; } g_ts_check;

static __attribute__((noreturn)) void shadow_fatal(const char *what, u32 a, u32 b, u32 c, u32 d) {
    slog("glsync: texture shadow mismatch: %s (%x %x shadow=%x driver=%x)", what, a, b, c, d);
    TerminateProcess(GetCurrentProcess(), 6);for(;;){}
}
static void ts_invalidate_bindings(void) { g_ts_bind_gen++; }
void glsync_tex_shadow_invalidate(void) { g_ts_bind_gen++; g_ts_param_gen++; }
static void ts_sync_ctx(void) {
    u32 ctx = SH->wh.current_gl;
    if (ctx != g_ts_ctx) { g_ts_ctx = ctx; glsync_tex_shadow_invalidate(); }
}
/* g_ts_bind column: kind - 1; an unvalidated target uses the 2D column. */
static u32 ts_bind_index(GLenum_ target) {
    u32 kind = tex_kind(target);
    return (kind ? kind : TEX_KIND_2D) - 1u;
}
static u32 ts_param_slot(u32 key) { return (key * TS_HASH_MUL) >> (32u - TP_BITS); }
static GLenum_ ts_param_target(GLenum_ target) { return tex_is_face(target) ? 0x8513 : target; }
static GLint_ ts_param_driver(GLenum_ target, GLenum_ pname) {
    GLint_ cur = 0x7fffffff;
    p_glGetTexParameteriv(ts_param_target(target), pname, &cur);
    return cur;
}
/* The driver's active unit as an index (out-of-range values stay out of range). */
static u32 active_unit_driver(void) {
    GLint_ a = 0;
    p_glGetIntegerv(GL_ACTIVE_TEXTURE_ARB_, &a);
    return (u32)a - GL_TEXTURE0_ARB_;
}

static GLuint_ bound_tex(GLenum_ target) {
    if (g_bnd_native) return bound_tex_driver(target);
    ts_sync_ctx();
    if (g_ts_active_gen != g_ts_bind_gen) {
        u32 u = active_unit_driver();
        if (u >= g_units || u >= TS_UNITS) return bound_tex_driver(target);
        g_ts_active = u;g_ts_active_gen = g_ts_bind_gen;
    } else if (g_gl_shadow_verify) {
        u32 u = active_unit_driver();
        if (u != g_ts_active) shadow_fatal("active unit", target, 0, g_ts_active, u);
    }
    ts_bind_t *s = &g_ts_bind[g_ts_active][ts_bind_index(target)];
    if (s->gen == g_ts_bind_gen) {
        if (g_gl_shadow_verify) {
            GLuint_ v = bound_tex_driver(target);
            if (v != s->name) shadow_fatal("binding", target, g_ts_active, s->name, v);
        }
        return s->name;
    }
    GLuint_ v = bound_tex_driver(target);
    s->gen = g_ts_bind_gen;s->name = v;
    return v;
}

/* After the game's glBindTexture reached the driver, before glsync_tex_target
 * records a new name's kind. A name whose recorded kind differs from the
 * target is refused by the driver (INVALID_OPERATION, binding unchanged):
 * that slot becomes unknown. */
void glsync_tex_shadow_bind(GLenum_ target, GLuint_ v) {
    if (g_bnd_native) return;
    ts_sync_ctx();
    if (!g_capture_on) { ts_invalidate_bindings(); return; }
    if (g_ts_active_gen != g_ts_bind_gen) return; /* no slot of this generation exists */
    u32 column = ts_bind_index(target), kind = column + 1u;
    ts_bind_t *s = &g_ts_bind[g_ts_active][column];
    if (v && v < GL_TEXMAX && SH->gl_tex_target[v] && SH->gl_tex_target[v] != kind) { s->gen = 0; return; }
    s->gen = g_ts_bind_gen;s->name = v;
}

/* After the game's glActiveTextureARB reached the driver. An enum outside the
 * driver's units leaves the unit unchanged (INVALID_ENUM), and units past
 * TS_UNITS are not shadowed: either way the unit becomes unknown, and a new
 * generation keeps a later bind on that unit from leaving a stale slot. */
void glsync_tex_shadow_active(GLenum_ unit) {
    if (g_bnd_native) return;
    ts_sync_ctx();
    if (!g_capture_on) { ts_invalidate_bindings(); return; }
    u32 u = (u32)unit - GL_TEXTURE0_ARB_;
    if (g_resolved > 0 && u < g_units && u < TS_UNITS) { g_ts_active = u;g_ts_active_gen = g_ts_bind_gen; }
    else ts_invalidate_bindings();
}

/* The supported object model owns named textures. A mutation of a default
 * object cannot silently bypass its keyed image/parameter state. */
void glsync_tex_validate(GLenum_ target,int pixels) {
    if(!g_capture_on)return;
    if(target!=0x0de0&&target!=0x0de1&&target!=0x8513&&!(target>=0x8515&&target<=0x851a))
        capture_abort("unmodeled texture mutation target");
    if(!resolve()||!core_ready())capture_abort("texture shadow unavailable");
    if(pixels&&!bound_tex(target))capture_abort("default texture images unsupported");
}
void glsync_program_prepare(GLenum_ target) {
    if(!g_capture_on || (target!=0x8620&&target!=0x8804))return;
    if(!resolve()||!core_ready())capture_abort("program shadow unavailable");
    GLint_ name=0;p_glGetProgramivARB(target,0x8677,&name);
    if(name<=0||(u32)name>=GL_PROGMAX)capture_abort("default program string unsupported");
}

/* A texture-changing call on the bound name of `target`: its images or parameters change. */
void glsync_tex_dirty(GLenum_ target, int pixels) {
    if(!g_capture_on)return;
    if(!resolve()||!core_ready())capture_abort("cannot track texture mutation");
    GLuint_ v = bound_tex(target);
    if (!v || v >= GL_TEXMAX) return;
    if (pixels) g_dirty_px[v] = 1;
    else g_dirty_par[v] = 1;
    g_dirty_any = 1;
}

/* glTexParameteri marks the texture only when the value changes (the game sets
 * them every bind). The parameter shadow answers with the last value the game
 * set on this logical texture: a repeated identical call leaves the driver as
 * the first left it (applied, clamped or refused alike), so "same" never hides
 * a change. The one exception is a call refused for transient state (inside
 * glBegin/glEnd) and repeated outside it; verify mode catches that. Named
 * textures on the three bindable targets only; the default objects and face
 * targets keep the driver query. */
static int ts_param_key(GLenum_ target, GLenum_ pname, u32 *key) {
    if (g_bnd_native || !tex_kind(target) || tex_is_face(target) || pname > 0xffffu) return 0;
    GLuint_ v = bound_tex(target);
    if (!v || v >= GL_TEXMAX) return 0;
    *key = (v << 16) | pname;
    return 1;
}
int glsync_tex_param_same(GLenum_ target, GLenum_ pname, GLint_ value) {
    g_ts_check.armed = 0;
    if (!g_capture_on || !resolve()) return 0;
    u32 key;
    if (ts_param_key(target, pname, &key)) {
        ts_param_t *e = &g_ts_param[ts_param_slot(key)];
        if (e->gen == g_ts_param_gen && e->key == key) {
            int same = e->value == value;
            if (same && g_gl_shadow_verify) {
                /* Verify the claim that matters: the game's call changes nothing. */
                g_ts_check.armed = 1;g_ts_check.target = target;g_ts_check.pname = pname;
                g_ts_check.before = ts_param_driver(target, pname);
            }
            return same;
        }
    }
    return ts_param_driver(target, pname) == value;
}

/* After the game's glTexParameteri reached the driver. */
void glsync_tex_param_set(GLenum_ target, GLenum_ pname, GLint_ value) {
    if (g_bnd_native) return;
    ts_sync_ctx();
    if (!g_capture_on || g_resolved <= 0) { g_ts_check.armed = 0;g_ts_param_gen++; return; }
    if (g_ts_check.armed) {
        g_ts_check.armed = 0;
        GLint_ after = ts_param_driver(g_ts_check.target, g_ts_check.pname);
        if (after != g_ts_check.before)
            shadow_fatal("parameter changed by a call shadowed as same", target, pname, (u32)g_ts_check.before, (u32)after);
    }
    u32 key;
    if (!ts_param_key(target, pname, &key)) return;
    ts_param_t *e = &g_ts_param[ts_param_slot(key)];
    e->gen = g_ts_param_gen;e->key = key;e->value = value;
}

void glsync_tex_target(GLuint_ v, GLenum_ target) {
    if (!v || v >= GL_TEXMAX || SH->gl_tex_target[v]) return;
    SH->gl_tex_target[v] = target == 0x8513 ? 2 : target == 0x0DE0 ? 3 : 1;
    if(g_capture_on) {
        if(!core_ready())capture_abort("cannot track texture target");
        g_dirty_par[v]=1;g_dirty_any=1;
    }
}

void glsync_tex_real_gone(GLuint_ v) {
    if (v && v < GL_TEXMAX && g_real_tex) memset(g_real_tex[v], 0, 32);
    memset(g_real_ctx,0,sizeof g_real_ctx);
}

void glsync_prog_real_gone(GLuint_ v) {
    if(v&&v<GL_PROGMAX)g_real_prog_target[v]=0;
    if (v && v < GL_PROGMAX && g_real_prog) memset(g_real_prog[v], 0, 32);
}

void glsync_tex_deleted(GLuint_ v) {
    if (!v || v >= GL_TEXMAX) return;
    memset(SH->gl_tex_key[v], 0, 32);
    SH->gl_tex_target[v] = 0;
    if (g_real_tex) {
        memset(g_real_tex[v], 0, 32);
        g_dirty_px[v] = g_dirty_par[v] = 0;
    }
}

/* ---------------------------------------------------------------- texture descriptors */

/* Preserve every supported fixed-point component. An 8-bit projection of
 * RGB16 (or an unknown floating format) is not an exact texture snapshot. */
static u32 base_format(GLint_ ifmt, GLenum_ *fmt, GLenum_ *type, u32 *bpp) {
    u32 components=0;
    switch(ifmt) {
    case 0x1906: case 0x803B: case 0x803C: case 0x803D: case 0x803E:
        *fmt=0x1906;components=1;break;
    case 1: case 0x1909: case 0x803F: case 0x8040: case 0x8041: case 0x8042:
    case 0x8049: case 0x804A: case 0x804B: case 0x804C: case 0x804D:
        *fmt=0x1909;components=1;break;
    case 2: case 0x190A: case 0x8043: case 0x8044: case 0x8045: case 0x8046: case 0x8047: case 0x8048:
        *fmt=0x190A;components=2;break;
    case 3: case 0x1907: case 0x2A10: case 0x804F: case 0x8050: case 0x8051: case 0x8052: case 0x8053: case 0x8054:
        *fmt=0x1907;components=3;break;
    case 4: case 0x1908: case 0x8055: case 0x8056: case 0x8057: case 0x8058: case 0x8059: case 0x805A: case 0x805B:
        *fmt=0x1908;components=4;break;
    case 0x1902: case 0x81A5: case 0x81A6: case 0x81A7:
        *fmt=0x1902;*type=0x1405;*bpp=4;return 1;
    default:return 0;
    }
    *type=0x1401;*bpp=components;
    switch(ifmt) {
    case 0x803D: case 0x803E: case 0x8041: case 0x8042:
    case 0x8046: case 0x8047: case 0x8048: case 0x804C: case 0x804D:
    case 0x8052: case 0x8053: case 0x8054: case 0x8059: case 0x805A: case 0x805B:
        *type=0x1403;*bpp=components*2;break; /* UNSIGNED_SHORT */
    }
    return 1;
}
static int texture_precision(GLenum_ face,GLint_ level,GLenum_ fmt,GLenum_ type) {
    GLint_ bits=0,nonzero=0;
    if(fmt==0x1902) {
        p_glGetTexLevelParameteriv(face,level,0x884a,&bits); /* DEPTH_SIZE */
        return bits>0&&bits<=24; /* same integer-depth admission as the drawable */
    }
    GLint_ maximum=type==0x1403?16:8;
    for(u32 i=0;i<6;i++) {
        p_glGetTexLevelParameteriv(face,level,0x805c+i,&bits); /* R/G/B/A/L/I sizes */
        if(bits<0||bits>maximum)return 0;
        nonzero|=bits;
    }
    return nonzero!=0;
}

static const GLenum_ TEX_IPARAMS[] = {0x2801, 0x2800, 0x2802, 0x2803, 0x8072, 0x8191, 0x813C, 0x813D, 0x884b, 0x884c, 0x884d};
static const GLenum_ TEX_FPARAMS[] = {0x813A, 0x813B, 0x8066, 0x8501};

static void default_texture_owner(void) {
    u32 context=SH->wh.current_gl;
    if(!context)capture_abort("default texture parameters require current context");
    if(SH->gl_default_owner&&SH->gl_default_owner!=context)
        capture_abort("default texture parameters span unsupported GL contexts");
    SH->gl_default_owner=context;
}
void glsync_tex_param_validate(GLenum_ target,GLenum_ pname) {
    if(!g_capture_on)return;
    glsync_tex_validate(target,0);
    int modeled=pname==0x84fe&&g_has_aniso;
    for(u32 i=0;i<sizeof TEX_IPARAMS/sizeof TEX_IPARAMS[0];i++)modeled|=pname==TEX_IPARAMS[i];
    for(u32 i=0;i<sizeof TEX_FPARAMS/sizeof TEX_FPARAMS[0];i++)modeled|=pname==TEX_FPARAMS[i];
    if(!modeled)capture_abort("unmodeled texture parameter pname");
    if(g_capture_on&&!bound_tex(target))default_texture_owner();
}

typedef struct { u32 face, level, ifmt, w, h, border, compressed, fmt, type, size, child; } img_t;

/* Read texture v (bound on unit 0 by the caller) into a descriptor; blobs are emitted. */
static int tex_describe(GLuint_ v, GLenum_ target, bb_t *d, u8 key[32]) {
    static img_t imgs[6 * 16];
    static u8 kids[6 * 16][32];
    u32 nimg = 0;
    u32 nfaces = target == 0x8513 ? 6 : 1;
    GLint_ maximum=0;p_glGetIntegerv(target==0x8513?0x851c:0x0d33,&maximum);
    if(maximum<=0)return 0;
    u32 levels=0;for(u32 size=(u32)maximum;size;size>>=1)levels++;
    if(levels>16)return 0;
    for (u32 f = 0; f < nfaces; f++) {
        GLenum_ face = target == 0x8513 ? 0x8515 + f : target;
        for (GLint_ lv = 0; (u32)lv < levels; lv++) {
            GLint_ w = 0, h = 0, ifmt = 0, border = 0, comp = 0, csize = 0;
            p_glGetTexLevelParameteriv(face, lv, 0x1000, &w);
            if (w <= 0) continue; /* sparse mip definitions remain observable */
            if(target==0x0de0)h=1;else p_glGetTexLevelParameteriv(face,lv,0x1001,&h);
            p_glGetTexLevelParameteriv(face, lv, 0x1003, &ifmt);
            p_glGetTexLevelParameteriv(face, lv, 0x1005, &border);
            p_glGetTexLevelParameteriv(face, lv, 0x86A1, &comp);
            img_t *im = &imgs[nimg];
            im->face = face; im->level = (u32)lv; im->ifmt = (u32)ifmt; im->w = (u32)w; im->h = (u32)h;
            im->border = (u32)border; im->compressed = comp != 0;
            u32 need;
            GLenum_ fmt = 0, type = 0;
            u32 bpp = 0;
            if (comp) {
                p_glGetTexLevelParameteriv(face, lv, 0x86A0, &csize);
                if(csize<=0)return 0;
                need = (u32)csize;
            } else {
                if (!base_format(ifmt, &fmt, &type, &bpp)) {
                    slog("glsync: texture %u internal format %x has no exact transfer",v,ifmt);
                    return 0;
                }
                if(!texture_precision(face,lv,fmt,type)) {
                    slog("glsync: texture %u level %d precision exceeds exact transfer",v,lv);return 0;
                }
                /* GL_TEXTURE_WIDTH/HEIGHT already include any border. */
                if(h<=0 || (border!=0&&border!=1))return 0;
                u64 bytes=(u64)(u32)w*(u32)h*bpp;
                if(!bytes || bytes>0xffff0000u)return 0;
                need=(u32)bytes;
            }
            if (need > (8u << 20)) {
                static u32 big;
                if (big++ < 10) slog("glsync: texture %u level %d is %ux%u format %x, %u bytes", v, lv, w, h, ifmt, need);
            }
            if (!img_room(need)) {
                slog("glsync: no memory for texture %u level %d (%ux%u, %u bytes)", v, lv, w, h, need);
                return 0;
            }
            u8 *data = g_img;
            u64 stamp=glsync_profile_now();
            if (comp) {
                p_glGetCompressedTexImageARB(face, lv, data);
                im->fmt = im->type = 0;
                im->size = (u32)csize;
            } else {
                p_glGetTexImage(face, lv, fmt, type, data);
                im->fmt = fmt; im->type = type; im->size = need;
            }
            gl_profile_record(GLP_TEXTURE_READ,stamp,glsync_profile_now());
            stamp=glsync_profile_now();sha256(data, im->size, kids[nimg]);
            gl_profile_record(GLP_TEXTURE_HASH,stamp,glsync_profile_now());
            emit(kids[nimg], data, im->size);
            im->child = nimg;
            nimg++;
        }
    }
    d->n = 0; d->over = 0;
    bb_u32(d, DESC_MAGIC_TEX);
    bb_u32(d, 1);
    bb_u32(d, nimg);
    for (u32 i = 0; i < nimg; i++) bb_raw(d, kids[i], 32);
    bb_u32(d, target);
    u32 np = sizeof TEX_IPARAMS / 4 + sizeof TEX_FPARAMS / 4 + 4 + (g_has_aniso ? 1 : 0);
    bb_u32(d, np);
    for (u32 i = 0; i < sizeof TEX_IPARAMS / 4; i++) {
        GLint_ x = 0;
        p_glGetTexParameteriv(target, TEX_IPARAMS[i], &x);
        bb_u32(d, TEX_IPARAMS[i]); bb_u32(d, 0); bb_u32(d, (u32)x);
    }
    for (u32 i = 0; i < sizeof TEX_FPARAMS / 4; i++) {
        GLfloat_ x = 0;
        p_glGetTexParameterfv(target, TEX_FPARAMS[i], &x);
        bb_u32(d, TEX_FPARAMS[i]); bb_u32(d, 1); bb_raw(d, &x, 4);
    }
    GLfloat_ bc[4] = {0, 0, 0, 0};
    p_glGetTexParameterfv(target, 0x1004, bc);
    for (u32 i = 0; i < 4; i++) { bb_u32(d, 0x1004); bb_u32(d, 2 + i); bb_raw(d, &bc[i], 4); }
    if (g_has_aniso) {
        GLfloat_ x = 0;
        p_glGetTexParameterfv(target, 0x84FE, &x);
        bb_u32(d, 0x84FE); bb_u32(d, 1); bb_raw(d, &x, 4);
    }
    bb_u32(d, nimg);
    for (u32 i = 0; i < nimg; i++) bb_raw(d, &imgs[i], sizeof imgs[i]);
    if (d->over) return 0;
    sha256(d->p, d->n, key);
    emit(key, d->p, d->n);
    return 1;
}

/* Every accepted pixel-store parameter. Internal transfers use tightly packed
 * client memory regardless of application strides, offsets or native PBOs. */
static const GLenum_ PIXEL_STORE[16]={0x0d00,0x0d01,0x0d02,0x0d03,0x0d04,0x0d05,0x806b,0x806c,
                                    0x0cf0,0x0cf1,0x0cf2,0x0cf3,0x0cf4,0x0cf5,0x806d,0x806e};
typedef struct { GLint_ active,b2d,bcube,b1d,pixel[16],pbo[2];u32 logical[3]; } texsave_t;

static void tex_bind_begin(texsave_t *s) {
    p_glGetIntegerv(0x84E0, &s->active);
    p_glActiveTextureARB(0x84C0);
    p_glGetIntegerv(0x8069, &s->b2d);
    p_glGetIntegerv(0x8514, &s->bcube);
    p_glGetIntegerv(0x8068, &s->b1d);
    s->logical[0]=gl_tex_saved_binding((u32)s->b2d);
    s->logical[1]=gl_tex_saved_binding((u32)s->bcube);
    s->logical[2]=gl_tex_saved_binding((u32)s->b1d);
    for(u32 i=0;i<16;i++) {
        p_glGetIntegerv(PIXEL_STORE[i],&s->pixel[i]);
        p_glPixelStorei(PIXEL_STORE[i],(i==5||i==13)?1:0);
    }
    p_glGetIntegerv(0x88ed,&s->pbo[0]);p_glGetIntegerv(0x88ef,&s->pbo[1]);
    p_glBindBufferARB(0x88eb,0);p_glBindBufferARB(0x88ec,0);
}

static GLuint_ saved_tex_name(u32 logical,u32 raw,u32 type) {
    if(logical==0xffffffffu)return raw; /* untouched host-private object */
    if(!logical||!SH->tex[logical]||SH->gl_tex_target[logical]!=type)return 0;
    return gl_tex_real_name(logical); /* allocation may have changed on replay */
}
static void tex_bind_end(texsave_t *s) {
    p_glBindTexture(0x0DE1, saved_tex_name(s->logical[0],(u32)s->b2d,1));
    p_glBindTexture(0x8513, saved_tex_name(s->logical[1],(u32)s->bcube,2));
    p_glBindTexture(0x0DE0, saved_tex_name(s->logical[2],(u32)s->b1d,3));
    p_glBindBufferARB(0x88eb,(GLuint_)s->pbo[0]);p_glBindBufferARB(0x88ec,(GLuint_)s->pbo[1]);
    for(u32 i=0;i<16;i++)p_glPixelStorei(PIXEL_STORE[i],s->pixel[i]);
    p_glActiveTextureARB((GLenum_)s->active);
}

static GLenum_ tex_target(GLuint_ v) {
    u8 t = SH->gl_tex_target[v];
    return t == 2 ? 0x8513 : t == 3 ? 0x0DE0 : 0x0DE1;
}

static void capture_textures(void) {
    if (!g_dirty_any) return;
    texsave_t s;
    int begun = 0;
    bb_t d = {g_buf, 0, 2u << 20, 0};
    for (u32 v = 1; v < GL_TEXMAX; v++) {
        if (!g_dirty_px[v] && !g_dirty_par[v]) continue;
        if (!((SH->gl_tex_live[v >> 3] >> (v & 7)) & 1)) {
            g_dirty_px[v]=g_dirty_par[v]=0;continue;
        }
        if (!begun) { tex_bind_begin(&s); begun = 1; }
        GLenum_ target = tex_target(v);
        p_glBindTexture(target, gl_tex_real_name(v));
        u8 key[32];
        if (!tex_describe(v, target, &d, key)) capture_abort("texture acquisition/descriptor failed");
        if(p_glGetError())capture_abort("texture capture query/readback failed");
        memcpy(SH->gl_tex_key[v], key, 32);
        memcpy(g_real_tex[v], key, 32);
        g_dirty_px[v]=g_dirty_par[v]=0;
        g_gl_captures++;
    }
    if (begun) tex_bind_end(&s);
    if(p_glGetError())capture_abort("texture capture temporary state failed");
    g_dirty_any=0;
}

/* ---------------------------------------------------------------- ARB programs */

void glsync_program_bound(GLuint_ v,GLenum_ target) {
    if(v&&v<GL_PROGMAX)g_real_prog_target[v]=target;
}

void glsync_program(GLenum_ target, GLenum_ format, GLsizei_ len, const void *str) {
    if(!g_capture_on)return;
    if(!resolve()||!core_ready())capture_abort("program shadow storage unavailable");
    /* Preserve native validation errors, then capture the accepted object. */
    GLenum_ pending=p_glGetError();if(pending&&!SH->gl_err)SH->gl_err=pending;
    if(target!=0x8620&&target!=0x8804)return;
    GLint_ name=0,actual_len=0,actual_format=0;
    p_glGetProgramivARB(target,0x8677,&name);
    if(name<=0||(u32)name>=GL_PROGMAX)capture_abort("default/unknown program string cannot be captured");
    p_glGetProgramivARB(target,0x8627,&actual_len);
    p_glGetProgramivARB(target,0x8876,&actual_format);
    if(p_glGetError())capture_abort("program metadata acquisition failed");
    if(!actual_len){memset(SH->gl_prog_key[name],0,32);memset(g_real_prog[name],0,32);return;}
    if(actual_len<0||(u32)actual_len>BUF_SIZE-24u)capture_abort("program descriptor overflow");
    bb_t d={g_buf,0,BUF_SIZE,0};
    bb_u32(&d,DESC_MAGIC_PROG);bb_u32(&d,1);bb_u32(&d,0);
    bb_u32(&d,target);bb_u32(&d,(u32)actual_format);bb_u32(&d,(u32)actual_len);
    p_glGetProgramStringARB(target,0x8628,d.p+d.n);d.n+=(u32)actual_len;
    if(p_glGetError())capture_abort("program acquisition failed");
    u8 key[32];sha256(d.p,d.n,key);emit(key,d.p,d.n);
    memcpy(SH->gl_prog_key[name],key,32);memcpy(g_real_prog[name],key,32);
}

void glsync_program_deleted(GLuint_ v) {
    if (!v || v >= GL_PROGMAX) return;
    memset(SH->gl_prog_key[v], 0, 32);
    SH->gl_prog_live[v >> 3] &= (u8)~(1u << (v & 7));
    SH->prog[v]=0;
    g_real_prog_target[v]=0;
    if (g_real_prog) memset(g_real_prog[v], 0, 32);
}

/* ---------------------------------------------------------------- ATI fragment shaders */

void glsync_ati_begin(void) {
    /* glati records every typed definition operand in captured arena memory. */
    g_ati_defining = 1;
}
void glsync_ati_end(void) { g_ati_defining = 0; }
void glsync_ati_bind(GLuint_ v) { SH->ati_bound = v; }
void glsync_ati_constant(GLuint_ dst, const GLfloat_ *v) {
    if (g_ati_defining) return;
    u32 s = SH->ati_bound, c = dst - 0x8941;       /* GL_CON_0_ATI */
    if (s >= ATI_MAX || c >= 8) return;
    memcpy(SH->ati_const[s][c], v, 16);
    SH->ati_const_set[s] |= (u8)(1u << c);
}

/* ---------------------------------------------------------------- context state */

static const GLenum_ ENABLES[] = {
    0x0BC0, 0x0BE2, 0x0B44, 0x0B71, 0x0B60, 0x0B50, 0x0B57, 0x0BA1, 0x803A, 0x8037, 0x2A02, 0x2A01,
    0x0C11, 0x0B90, 0x0BD0, 0x8620, 0x8804, 0x8920, 0x0B20, 0x0B10, 0x0B41, 0x0BF2, 0x3000, 0x3001,
    0x3002, 0x3003, 0x3004, 0x3005, 0x809D, 0x0B24, 0x0B42, 0x8458, 0x8642, 0x8643};
static const GLenum_ UNIT_ENABLES[] = {0x0DE0, 0x0DE1, 0x8513, 0x0C60, 0x0C61, 0x0C62, 0x0C63};
static const GLenum_ CLIENT_ENABLES[] = {0x8074, 0x8075, 0x8076, 0x8077, 0x8079, 0x845E, 0x8457};
static const GLenum_ LIGHT_PARAMS[] = {0x1200, 0x1201, 0x1202, 0x1203, 0x1204, 0x1205, 0x1206, 0x1207, 0x1208, 0x1209};
static const u8 LIGHT_N[] = {4, 4, 4, 4, 3, 1, 1, 1, 1, 1};
static const GLenum_ MAT_PARAMS[] = {0x1200, 0x1201, 0x1202, 0x1600, 0x1601};
static const u8 MAT_N[] = {4, 4, 4, 4, 1};

/* The state vector: a fixed sequence of values. Capture and apply walk the
 * same sequence; op 0 reads into the builder, op 1 reads from a blob and sets. */
typedef struct { int apply; const u8 *in; u32 at, len; bb_t *out; int bad; } walk_t;
/* Apply only: name the context section that raised a native GL error. Any
 * pending error already fails the apply; this attributes it to its section
 * instead of to whichever later group first checks. Capture is unchanged. */
static int walk_gl_ok(walk_t *w,const char *section);

static void w_raw(walk_t *w, void *v, u32 n) {
    if (!w->apply) { bb_raw(w->out, v, n); return; }
    if (w->at>w->len || n>w->len-w->at) { w->bad = 1; memset(v, 0, n); return; }
    memcpy(v, w->in + w->at, n);
    w->at += n;
}
#define WI(w, x) w_raw(w, &(x), 4)

/* ATI fragment shaders: constants and binding from harness_game (no driver query exists) */
static void ati_apply(void) {
    for (u32 s = 1; !SH->glati.used && s < ATI_MAX; s++) {
        if (!SH->ati_const_set[s]) continue;
        p_glBindFragmentShaderATI(s);
        for (u32 c = 0; c < 8; c++)
            if ((SH->ati_const_set[s] >> c) & 1) p_glSetFragmentShaderConstantATI(0x8941 + c, SH->ati_const[s][c]);
    }
    p_glBindFragmentShaderATI(SH->ati_bound);
}

/* Captured gate contexts currently have base stacks. Remove abandoned-future
 * frames before installing that target; loading only the top matrix is not a
 * restore. Native pops intentionally bypass captured logical wrapper stacks. */
static int stack_to_base(GLenum_ depth_name, GLenum_ max_name, GLint_ base,
                         void (GLAPI *pop)(void)) {
    GLint_ depth=0,maximum=0;
    p_glGetIntegerv(depth_name,&depth);p_glGetIntegerv(max_name,&maximum);
    if(depth<base || maximum<base || depth>maximum)return 0;
    while(depth-->base)pop();
    p_glGetIntegerv(depth_name,&depth);
    return depth==base;
}
static int context_base_stacks(void) {
    if(!stack_to_base(0x0BB0,0x0D35,0,p_glPopAttrib) ||
       !stack_to_base(0x0BB1,0x0D3B,0,p_glPopClientAttrib))return 0;
    p_glMatrixMode(0x1700);
    if(!stack_to_base(0x0BA3,0x0D36,1,p_glPopMatrix))return 0;
    p_glMatrixMode(0x1701);
    if(!stack_to_base(0x0BA4,0x0D38,1,p_glPopMatrix))return 0;
    for(u32 u=0;u<g_units;u++) {
        p_glActiveTextureARB(0x84C0+u);p_glMatrixMode(0x1702);
        if(!stack_to_base(0x0BA5,0x0D39,1,p_glPopMatrix))return 0;
    }
    return p_glGetError()==0;
}

/* Object-local program constants include unbound live objects and the two
 * default objects. Sparse serialization avoids embedding unused zero vectors;
 * apply still resets every component so abandoned future values cannot survive. */
static void walk_program_local(walk_t*w,GLenum_ target,u32 maximum) {
    u32 count=0,at=w->apply?0:w->out->n;
    WI(w,count);
    if(w->apply) {
        if(count>maximum){w->bad=5;return;}
        const GLdouble_ zero[4]={0,0,0,0};
        for(u32 i=0;i<maximum;i++)p_glProgramLocalParameter4dvARB(target,i,zero);
        u32 prior=0;
        for(u32 j=0;j<count;j++) {
            u32 index=0;GLdouble_ value[4];WI(w,index);w_raw(w,value,sizeof value);
            if(w->bad||index>=maximum||(j&&index<=prior)){w->bad=5;return;}
            p_glProgramLocalParameter4dvARB(target,index,value);prior=index;
        }
    } else {
        const GLdouble_ zero[4]={0,0,0,0};
        for(u32 i=0;i<maximum;i++) {
            GLdouble_ value[4];p_glGetProgramLocalParameterdvARB(target,i,value);
            if(!memcmp(value,zero,sizeof value))continue;
            WI(w,i);w_raw(w,value,sizeof value);count++;
        }
        if(!w->out->over)memcpy(w->out->p+at,&count,4);
    }
}

/* Default texture parameters are observable and the game sets them before
 * allocating named objects. Images on default objects remain unsupported. */
static void walk_default_texture_parameters(walk_t *w) {
    default_texture_owner();
    texsave_t saved;tex_bind_begin(&saved);
    const GLenum_ targets[3]={0x0de0,0x0de1,0x8513};
    for(u32 t=0;t<3;t++) {
        GLenum_ target=targets[t];p_glBindTexture(target,0);
        for(u32 i=0;i<sizeof TEX_IPARAMS/sizeof TEX_IPARAMS[0];i++) {
            GLint_ value=0;if(!w->apply)p_glGetTexParameteriv(target,TEX_IPARAMS[i],&value);
            WI(w,value);if(w->apply&&!w->bad)p_glTexParameteri(target,TEX_IPARAMS[i],value);
        }
        for(u32 i=0;i<sizeof TEX_FPARAMS/sizeof TEX_FPARAMS[0];i++) {
            GLfloat_ value=0;if(!w->apply)p_glGetTexParameterfv(target,TEX_FPARAMS[i],&value);
            w_raw(w,&value,4);if(w->apply&&!w->bad)p_glTexParameterf(target,TEX_FPARAMS[i],value);
        }
        GLfloat_ border[4]={0};if(!w->apply)p_glGetTexParameterfv(target,0x1004,border);
        w_raw(w,border,16);if(w->apply&&!w->bad)p_glTexParameterfv(target,0x1004,border);
        if(g_has_aniso) {
            GLfloat_ value=0;if(!w->apply)p_glGetTexParameterfv(target,0x84fe,&value);
            w_raw(w,&value,4);if(w->apply&&!w->bad)p_glTexParameterf(target,0x84fe,value);
        }
    }
    tex_bind_end(&saved);
}

/* EXT_two_side and OpenGL2 have distinct back-face vectors. The EXT face
 * selector affects legacy queries/setters, while core BACK queries name the
 * separate core vector. Serialize all three, including currently inactive state. */
static void walk_stencil(walk_t *w) {
    static const GLenum_ front_names[7]={0x0b92,0x0b97,0x0b93,0x0b94,0x0b95,0x0b96,0x0b98};
    static const GLenum_ back_names[7]={0x8800,0x8ca3,0x8ca4,0x8801,0x8802,0x8803,0x8ca5};
    u32 caps=g_stencil_caps;GLint_ active=0x0404,enabled=0,clear=0,vectors[3][7]={{0}};
    if(!w->apply) {
        if(caps&1u) {
            p_glGetIntegerv(0x8911,&active);enabled=p_glIsEnabled(0x8910);
            if(active!=0x0404&&active!=0x0405){w->bad=6;return;}
            p_glActiveStencilFaceEXT(0x0404);
        }
        p_glGetIntegerv(0x0b91,&clear);
        for(u32 i=0;i<7;i++)p_glGetIntegerv(front_names[i],&vectors[0][i]);
        if(caps&2u)for(u32 i=0;i<7;i++)p_glGetIntegerv(back_names[i],&vectors[1][i]);
        if(caps&1u) {
            p_glActiveStencilFaceEXT(0x0405);
            for(u32 i=0;i<7;i++)p_glGetIntegerv(front_names[i],&vectors[2][i]);
            p_glActiveStencilFaceEXT((GLenum_)active);
        }
    }
    WI(w,caps);WI(w,active);WI(w,enabled);WI(w,clear);w_raw(w,vectors,sizeof vectors);
    if(w->bad)return;
    if(caps!=g_stencil_caps||(active!=0x0404&&active!=0x0405)||enabled<0||enabled>1||
       (!(caps&1u)&&(active!=0x0404||enabled))) {w->bad=6;return;}
    if(!w->apply)return;
    if(caps&1u)p_glActiveStencilFaceEXT(0x0404);
    p_glStencilFunc(vectors[0][0],vectors[0][1],(GLuint_)vectors[0][2]);
    p_glStencilOp(vectors[0][3],vectors[0][4],vectors[0][5]);p_glStencilMask((GLuint_)vectors[0][6]);
    if(caps&2u) {
        p_glStencilFuncSeparate(0x0405,vectors[1][0],vectors[1][1],(GLuint_)vectors[1][2]);
        p_glStencilOpSeparate(0x0405,vectors[1][3],vectors[1][4],vectors[1][5]);
        p_glStencilMaskSeparate(0x0405,(GLuint_)vectors[1][6]);
    }
    if(caps&1u) {
        p_glActiveStencilFaceEXT(0x0405);
        p_glStencilFunc(vectors[2][0],vectors[2][1],(GLuint_)vectors[2][2]);
        p_glStencilOp(vectors[2][3],vectors[2][4],vectors[2][5]);p_glStencilMask((GLuint_)vectors[2][6]);
        p_glActiveStencilFaceEXT((GLenum_)active);
        (enabled?p_glEnable:p_glDisable)(0x8910);
    }
    p_glClearStencil(clear);
}

/* KPGC v6 includes dormant combiner settings too: a later COMBINE selection
 * must not expose parameters from the abandoned future. Scale values are the
 * exact integers 1, 2 or 4; LOD bias needs the floating-point query/setter.
 * ARB/EXT_texture_env_combine and EXT_texture_lod_bias define these tokens. */
static void walk_texture_environment(walk_t *w) {
    static constexpr GLenum_ combine_names[]={
        0x8571,0x8572,                      /* COMBINE_RGB, COMBINE_ALPHA */
        0x8580,0x8581,0x8582,               /* SOURCE0/1/2_RGB */
        0x8588,0x8589,0x858a,               /* SOURCE0/1/2_ALPHA */
        0x8590,0x8591,0x8592,               /* OPERAND0/1/2_RGB */
        0x8598,0x8599,0x859a,               /* OPERAND0/1/2_ALPHA */
        0x8573,0x0d1c};                    /* RGB_SCALE, ALPHA_SCALE */
    GLint_ mode=0,combine[sizeof combine_names/sizeof combine_names[0]]={};
    GLfloat_ color[4]={},lod_bias=0;
    if(!w->apply) {
        p_glGetTexEnviv(0x2300,0x2200,&mode);
        p_glGetTexEnvfv(0x2300,0x2201,color);
        if(g_texenv_caps&TEXENV_COMBINE)
            for(u32 i=0;i<sizeof combine_names/sizeof combine_names[0];i++)
                p_glGetTexEnviv(0x2300,combine_names[i],&combine[i]);
        if(g_texenv_caps&TEXENV_LOD_BIAS)p_glGetTexEnvfv(0x8500,0x8501,&lod_bias);
    }
    WI(w,mode);w_raw(w,color,sizeof color);
    if(g_texenv_caps&TEXENV_COMBINE)w_raw(w,combine,sizeof combine);
    if(g_texenv_caps&TEXENV_LOD_BIAS)w_raw(w,&lod_bias,sizeof lod_bias);
    if(w->bad||!w->apply)return;
    if(g_texenv_caps&TEXENV_COMBINE)
        for(u32 i=0;i<sizeof combine_names/sizeof combine_names[0];i++)
            p_glTexEnvi(0x2300,combine_names[i],combine[i]);
    if(g_texenv_caps&TEXENV_LOD_BIAS)p_glTexEnvfv(0x8500,0x8501,&lod_bias);
    p_glTexEnvfv(0x2300,0x2201,color);p_glTexEnvi(0x2300,0x2200,mode);
}

static void walk_ctx(walk_t *w) {
    GLfloat_ f16[16];
    GLboolean_ b4[4];
    u32 x;
    /* limits: apply refuses a vector from a driver with other limits */
    u32 lim[7] = {g_units,g_lights,g_attribs,g_env[0],g_env[1],g_local[0],g_local[1]},lim2[7];
    memcpy(lim2, lim, sizeof lim);
    w_raw(w, lim2, sizeof lim2);
    if (w->apply && memcmp(lim, lim2, sizeof lim)) { w->bad = 2; return; }
    u32 texenv_caps=g_texenv_caps;WI(w,texenv_caps);
    if(w->bad)return;
    if(texenv_caps!=g_texenv_caps) {
        slog("glsync: texture environment capability mismatch descriptor=%x driver=%x",texenv_caps,g_texenv_caps);
        w->bad=7;return;
    }
    GLint_ active = 0, client = 0, mode = 0;
    if (!w->apply) { p_glGetIntegerv(0x84E0, &active); p_glGetIntegerv(0x84E1, &client); p_glGetIntegerv(0x0BA0, &mode); }
    WI(w, active); WI(w, client); WI(w, mode);
    if (w->apply && !w->bad && mode == 0) {
        /* No GL call can re-enter Mesa's unset initial matrix mode. Recordings
         * made before the shim set GL_MODELVIEW at make-current hold this
         * value at their first ticks; refuse them by name instead of failing
         * later with GL_INVALID_ENUM. */
        slog("glsync: context descriptor holds the unset initial matrix mode 0; re-record with this shim");
        w->bad = 9; return;
    }
    GLint_ buffers[2]={0,0};
    if(!w->apply) { p_glGetIntegerv(0x8894,&buffers[0]);p_glGetIntegerv(0x8895,&buffers[1]); }
    if(!w->apply)for(u32 i=0;i<2;i++)buffers[i]=(GLint_)glbuffers_logical_name((u32)buffers[i]);
    w_raw(w,buffers,sizeof buffers);
    /* stack depths: the gate expects every stack at its base */
    GLint_ depth[4] = {1, 1, 0, 0};
    if (!w->apply) { p_glGetIntegerv(0x0BA3, &depth[0]); p_glGetIntegerv(0x0BA4, &depth[1]);
                     p_glGetIntegerv(0x0BB0, &depth[2]); p_glGetIntegerv(0x0BB1, &depth[3]); }
    w_raw(w, depth, sizeof depth);
    if(depth[0]!=1 || depth[1]!=1 || depth[2]!=0 || depth[3]!=0) {
        slog("GL context stack capture incomplete: modelview=%d projection=%d attrib=%d client=%d",depth[0],depth[1],depth[2],depth[3]);
        w->bad=3;return;
    }
    if(w->apply) {
        if(!context_base_stacks()){w->bad=4;return;}
        p_glDisable(0x0B57); /* COLOR_MATERIAL off while color/materials go in */
    }
    /* matrices; lights and texgen planes are set under an identity modelview */
    GLfloat_ mv[16], pj[16];
    if (!w->apply) { p_glGetFloatv(0x0BA6, mv); p_glGetFloatv(0x0BA7, pj); }
    w_raw(w, mv, sizeof mv); w_raw(w, pj, sizeof pj);
    if (w->apply) { p_glMatrixMode(0x1701); p_glLoadMatrixf(pj); p_glMatrixMode(0x1700); p_glLoadIdentity(); }
    for (u32 l = 0; l < g_lights; l++)
        for (u32 k = 0; k < sizeof LIGHT_PARAMS / 4; k++) {
            if (!w->apply) p_glGetLightfv(0x4000 + l, LIGHT_PARAMS[k], f16);
            w_raw(w, f16, LIGHT_N[k] * 4u);
            if (w->apply) p_glLightfv(0x4000 + l, LIGHT_PARAMS[k], f16);
        }
    for (u32 u = 0; u < g_units; u++) {
        p_glActiveTextureARB(0x84C0 + u);
        for (u32 c = 0; c < 4; c++) {
            GLint_ gm = 0;
            if (!w->apply) p_glGetTexGeniv(0x2000 + c, 0x2500, &gm);
            WI(w, gm);
            if (w->apply) p_glTexGeni(0x2000 + c, 0x2500, gm);
            for (u32 pl = 0x2501; pl <= 0x2502; pl++) {
                if (!w->apply) p_glGetTexGenfv(0x2000 + c, pl, f16);
                w_raw(w, f16, 16);
                if (w->apply) p_glTexGenfv(0x2000 + c, pl, f16);
            }
        }
        walk_texture_environment(w);if(w->bad||!walk_gl_ok(w,"texgen and texture environment"))return;
        GLfloat_ tm[16];
        if (!w->apply) p_glGetFloatv(0x0BA8, tm);
        w_raw(w, tm, sizeof tm);
        if (w->apply) { p_glMatrixMode(0x1702); p_glLoadMatrixf(tm); p_glMatrixMode(0x1700); }
        GLint_ tdepth = 1;
        if (!w->apply) p_glGetIntegerv(0x0BA5, &tdepth);
        WI(w, tdepth);
        if(tdepth!=1) { slog("GL texture matrix stack capture incomplete: unit=%u depth=%d",u,tdepth);w->bad=3;return; }
        GLfloat_ tc[4];
        if (!w->apply) p_glGetFloatv(0x0B03, tc);
        w_raw(w, tc, 16);
        if (w->apply) p_glMultiTexCoord4fvARB(0x84C0 + u, tc);
        GLint_ b[3] = {0, 0, 0};
        if (!w->apply) { p_glGetIntegerv(0x8068, &b[0]); p_glGetIntegerv(0x8069, &b[1]); p_glGetIntegerv(0x8514, &b[2]); }
        if(!w->apply)for(u32 i=0;i<3;i++)b[i]=(GLint_)gl_tex_logical_name((u32)b[i]);
        w_raw(w, b, sizeof b);
        if (w->apply) { p_glBindTexture(0x0DE0, gl_tex_real_name(b[0])); p_glBindTexture(0x0DE1, gl_tex_real_name(b[1])); p_glBindTexture(0x8513, gl_tex_real_name(b[2])); }
        for (u32 k = 0; k < sizeof UNIT_ENABLES / 4; k++) {
            x = !w->apply ? p_glIsEnabled(UNIT_ENABLES[k]) : 0;
            WI(w, x);
            if (w->apply) (x ? p_glEnable : p_glDisable)(UNIT_ENABLES[k]);
        }
    }
    for (u32 u = 0; u < g_units; u++) {
        p_glClientActiveTextureARB(0x84C0 + u);
        x = !w->apply ? p_glIsEnabled(0x8078) : 0;
        WI(w, x);
        GLint_ sz = 4, ty = 0x1406, st = 0, buffer = 0;
        void *ptr = 0;
        if (!w->apply) { p_glGetIntegerv(0x8088, &sz); p_glGetIntegerv(0x8089, &ty); p_glGetIntegerv(0x808A, &st);
                         p_glGetPointerv(0x8092, &ptr); p_glGetIntegerv(0x889A,&buffer); }
        if(!w->apply)buffer=(GLint_)glbuffers_logical_name((u32)buffer);
        WI(w, sz); WI(w, ty); WI(w, st); w_raw(w, &ptr, 4); WI(w,buffer);
        if (w->apply) { p_glBindBufferARB(0x8892,glbuffers_real_name((u32)buffer));p_glTexCoordPointer(sz, ty, st, ptr); (x ? p_glEnableClientState : p_glDisableClientState)(0x8078); }
    }
    p_glActiveTextureARB(0x84C0);
    /* the modelview last among the transforms */
    if (w->apply) p_glLoadMatrixf(mv);
    /* client arrays */
    for (u32 k = 0; k < sizeof CLIENT_ENABLES / 4; k++) {
        x = !w->apply ? p_glIsEnabled(CLIENT_ENABLES[k]) : 0;
        WI(w, x);
        if (w->apply) (x ? p_glEnableClientState : p_glDisableClientState)(CLIENT_ENABLES[k]);
    }
    {
        GLint_ v[3] = {4, 0x1406, 0}, n[2] = {0x1406, 0}, c[3] = {4, 0x1406, 0};
        void *pv = 0, *pn = 0, *pc = 0;
        GLint_ vb=0,nb=0,cb=0;
        if (!w->apply) {
            p_glGetIntegerv(0x807A, &v[0]); p_glGetIntegerv(0x807B, &v[1]); p_glGetIntegerv(0x807C, &v[2]);
            p_glGetPointerv(0x808E, &pv);
            p_glGetIntegerv(0x807E, &n[0]); p_glGetIntegerv(0x807F, &n[1]); p_glGetPointerv(0x808F, &pn);
            p_glGetIntegerv(0x8081, &c[0]); p_glGetIntegerv(0x8082, &c[1]); p_glGetIntegerv(0x8083, &c[2]);
            p_glGetPointerv(0x8090, &pc);
            p_glGetIntegerv(0x8896,&vb);p_glGetIntegerv(0x8897,&nb);p_glGetIntegerv(0x8898,&cb);
        }
        w_raw(w, v, sizeof v); w_raw(w, &pv, 4); w_raw(w, n, sizeof n); w_raw(w, &pn, 4);
        w_raw(w, c, sizeof c); w_raw(w, &pc, 4);
        if(!w->apply){vb=(GLint_)glbuffers_logical_name((u32)vb);nb=(GLint_)glbuffers_logical_name((u32)nb);cb=(GLint_)glbuffers_logical_name((u32)cb);}
        WI(w,vb);WI(w,nb);WI(w,cb);
        if (w->apply) { p_glBindBufferARB(0x8892,glbuffers_real_name((u32)vb));p_glVertexPointer(v[0], v[1], v[2], pv);
                        p_glBindBufferARB(0x8892,glbuffers_real_name((u32)nb));p_glNormalPointer(n[0], n[1], pn);
                        p_glBindBufferARB(0x8892,glbuffers_real_name((u32)cb));p_glColorPointer(c[0], c[1], c[2], pc); }
    }
    for (u32 a = 0; a < g_attribs; a++) {
        GLint_ q[5] = {0, 4, 0, 0x1406, 0}, ab=0;
        void *pa = 0;
        if (!w->apply) {
            p_glGetVertexAttribivARB(a, 0x8622, &q[0]); p_glGetVertexAttribivARB(a, 0x8623, &q[1]);
            p_glGetVertexAttribivARB(a, 0x8624, &q[2]); p_glGetVertexAttribivARB(a, 0x8625, &q[3]);
            p_glGetVertexAttribivARB(a, 0x886A, &q[4]); p_glGetVertexAttribPointervARB(a, 0x8645, &pa);p_glGetVertexAttribivARB(a,0x889F,&ab);
        }
        if(!w->apply)ab=(GLint_)glbuffers_logical_name((u32)ab);
        w_raw(w, q, sizeof q); w_raw(w, &pa, 4);WI(w,ab);
        if (w->apply) {
            p_glBindBufferARB(0x8892,glbuffers_real_name((u32)ab));
            if (a || ab) p_glVertexAttribPointerARB(a, q[1], q[3], (GLboolean_)q[4], q[2], pa);
            (q[0] ? p_glEnableVertexAttribArrayARB : p_glDisableVertexAttribArrayARB)(a);
        }
    }
    if(w->apply) { p_glBindBufferARB(0x8892,glbuffers_real_name((u32)buffers[0]));p_glBindBufferARB(0x8893,glbuffers_real_name((u32)buffers[1])); }
    if(!walk_gl_ok(w,"texture units, client arrays and buffers"))return;
    /* materials before the enables: COLOR_MATERIAL, once on, copies the current color */
    for (u32 face = 0x0404; face <= 0x0405; face++)
        for (u32 k = 0; k < sizeof MAT_PARAMS / 4; k++) {
            if (!w->apply) p_glGetMaterialfv(face, MAT_PARAMS[k], f16);
            w_raw(w, f16, MAT_N[k] * 4u);
            if (w->apply) p_glMaterialfv(face, MAT_PARAMS[k], f16);
        }
    if (!w->apply) { p_glGetFloatv(0x0B00, f16); p_glGetFloatv(0x0B02, f16 + 4); }
    w_raw(w, f16, 28);
    if (w->apply) { p_glColor4fv(f16); p_glNormal3fv(f16 + 4); }
    /* functions, masks, values */
    GLint_ iv[2];
    GLfloat_ fv;
    if (!w->apply) { p_glGetIntegerv(0x0BC1, &iv[0]); p_glGetFloatv(0x0BC2, &fv); }
    WI(w, iv[0]); WI(w, fv);
    if (w->apply) p_glAlphaFunc(iv[0], fv);
    if (!w->apply) { p_glGetIntegerv(0x0BE1, &iv[0]); p_glGetIntegerv(0x0BE0, &iv[1]); }
    w_raw(w, iv, 8);
    if (w->apply) p_glBlendFunc(iv[0], iv[1]);
    if (!w->apply) p_glGetIntegerv(0x0B74, &iv[0]);
    WI(w, iv[0]);
    if (w->apply) p_glDepthFunc(iv[0]);
    if (!w->apply) { p_glGetBooleanv(0x0B72, b4); x = b4[0]; }
    WI(w, x);
    if (w->apply) p_glDepthMask((GLboolean_)x);
    {
        GLboolean_ cm[4];
        if (!w->apply) p_glGetBooleanv(0x0C23, cm);
        w_raw(w, cm, 4);
        if (w->apply) p_glColorMask(cm[0], cm[1], cm[2], cm[3]);
    }
    GLdouble_ dd[2];
    if (!w->apply) p_glGetDoublev(0x0B73, dd);
    w_raw(w, dd, 8);
    if (w->apply) p_glClearDepth(dd[0]);
    if (!w->apply) p_glGetDoublev(0x0B70, dd);
    w_raw(w, dd, 16);
    if (w->apply) p_glDepthRange(dd[0], dd[1]);
    if (!w->apply) p_glGetFloatv(0x0C22, f16);
    w_raw(w, f16, 16);
    if (w->apply) p_glClearColor(f16[0], f16[1], f16[2], f16[3]);
    if(!walk_gl_ok(w,"materials, functions and clear values"))return;
    walk_stencil(w);if(w->bad||!walk_gl_ok(w,"stencil"))return;
    {
        GLint_ m[6];
        GLfloat_ po[4];
        if (!w->apply) {
            p_glGetIntegerv(0x0B45, &m[0]); p_glGetIntegerv(0x0B46, &m[1]); p_glGetIntegerv(0x0B40, &m[2]);
            p_glGetIntegerv(0x0B54, &m[4]); p_glGetIntegerv(0x0B55, &m[5]);
            p_glGetFloatv(0x8038, &po[0]); p_glGetFloatv(0x2A00, &po[1]); p_glGetFloatv(0x0B21, &po[2]);
            p_glGetFloatv(0x0B11, &po[3]);
        }
        w_raw(w, m, sizeof m); w_raw(w, po, sizeof po);
        GLint_ cmp = 0;
        if (!w->apply) p_glGetIntegerv(0x0B56, &cmp);
        WI(w, cmp);
        if (w->apply) {
            p_glCullFace(m[0]); p_glFrontFace(m[1]); p_glPolygonMode(0x0404, m[2]); p_glPolygonMode(0x0405, m[3]);
            p_glShadeModel(m[4]); p_glColorMaterial(m[5], cmp);
            p_glPolygonOffset(po[0], po[1]); p_glLineWidth(po[2]); p_glPointSize(po[3]);
        }
    }
    {
        GLfloat_ lm[4];
        GLint_ lmi[3];
        if (!w->apply) { p_glGetFloatv(0x0B53, lm); p_glGetIntegerv(0x0B51, &lmi[0]); p_glGetIntegerv(0x0B52, &lmi[1]);
                         p_glGetIntegerv(0x81F8, &lmi[2]); }
        w_raw(w, lm, 16); w_raw(w, lmi, 12);
        if (w->apply) { p_glLightModelfv(0x0B53, lm); p_glLightModeli(0x0B51, lmi[0]); p_glLightModeli(0x0B52, lmi[1]);
                        p_glLightModeli(0x81F8, lmi[2]); }
    }
    {
        GLint_ fm = 0;
        GLfloat_ fp[7];
        if (!w->apply) { p_glGetIntegerv(0x0B65, &fm); p_glGetFloatv(0x0B62, &fp[0]); p_glGetFloatv(0x0B63, &fp[1]);
                         p_glGetFloatv(0x0B64, &fp[2]); p_glGetFloatv(0x0B66, fp + 3); }
        WI(w, fm); w_raw(w, fp, sizeof fp);
        if (w->apply) { p_glFogi(0x0B65, fm); p_glFogf(0x0B62, fp[0]); p_glFogf(0x0B63, fp[1]); p_glFogf(0x0B64, fp[2]);
                        p_glFogfv(0x0B66, fp + 3); }
    }
    {
        GLint_ vp[4],sc[4],db[2],ps[16];
        if(!w->apply) {
            p_glGetIntegerv(0x0ba2,vp);p_glGetIntegerv(0x0c10,sc);gldraw_selectors(&db[0],&db[1]);
            for(u32 i=0;i<16;i++)p_glGetIntegerv(PIXEL_STORE[i],&ps[i]);
        }
        w_raw(w,vp,16);w_raw(w,sc,16);w_raw(w,db,8);w_raw(w,ps,sizeof ps);
        if(w->apply) {
            p_glViewport(vp[0],vp[1],vp[2],vp[3]);p_glScissor(sc[0],sc[1],sc[2],sc[3]);
            p_glDrawBuffer(db[0]);p_glReadBuffer(db[1]);
            for(u32 i=0;i<16;i++)p_glPixelStorei(PIXEL_STORE[i],ps[i]);
        }
    }
    /* ARB programs: bindings and environment parameters */
    for (u32 t = 0; t < 2; t++) {
        GLenum_ target = t ? 0x8804 : 0x8620;
        GLint_ bind = 0;
        if (!w->apply) p_glGetProgramivARB(target, 0x8677, &bind);
        WI(w, bind);
        if (w->apply) p_glBindProgramARB(target, (GLuint_)bind);
        for (u32 i = 0; i < g_env[t]; i++) {
            if (!w->apply) p_glGetProgramEnvParameterfvARB(target, i, f16);
            w_raw(w, f16, 16);
            if (w->apply) p_glProgramEnvParameter4fvARB(target, i, f16);
        }
    }
    for(u32 t=0;t<2;t++) {
        GLenum_ target=t?0x8804:0x8620;GLint_ previous=0;
        p_glGetProgramivARB(target,0x8677,&previous);
        /* SH's target table is captured and restored before this descriptor. */
        for(u32 v=0;v<GL_PROGMAX;v++) {
            if(v&&SH->prog[v]!=target)continue;
            p_glBindProgramARB(target,v);
            if(v)g_real_prog_target[v]=target;
            walk_program_local(w,target,g_local[t]);
            if(w->bad)break;
        }
        p_glBindProgramARB(target,(GLuint_)previous);
        if(w->bad)return;
    }
    /* Attribute zero emits vertices and has no CURRENT state in ARB_vertex_program. */
    for(u32 a=1;a<g_attribs;a++) {
        GLdouble_ value[4];
        if(!w->apply)p_glGetVertexAttribdvARB(a,0x8626,value);
        w_raw(w,value,sizeof value);
        if(w->apply&&!w->bad)p_glVertexAttrib4dvARB(a,value);
    }
    if(!walk_gl_ok(w,"modes, ARB programs and vertex attributes"))return;
    walk_default_texture_parameters(w);
    if(w->bad||!walk_gl_ok(w,"default texture parameters"))return;
    if (w->apply) ati_apply();
    if(!walk_gl_ok(w,"ATI fragment shader state"))return;
    /* enables last */
    for (u32 k = 0; k < sizeof ENABLES / 4; k++) {
        x = !w->apply ? p_glIsEnabled(ENABLES[k]) : 0;
        WI(w, x);
        if (w->apply) (x ? p_glEnable : p_glDisable)(ENABLES[k]);
    }
    for (u32 l = 0; l < g_lights; l++) {
        x = !w->apply ? p_glIsEnabled(0x4000 + l) : 0;
        WI(w, x);
        if (w->apply) (x ? p_glEnable : p_glDisable)(0x4000 + l);
    }
    if (w->apply) {
        p_glMatrixMode((GLenum_)mode);
        p_glClientActiveTextureARB((GLenum_)client);
        p_glActiveTextureARB((GLenum_)active);
        walk_gl_ok(w,"enables and selectors");
    } else {
        p_glClientActiveTextureARB((GLenum_)client);
        p_glActiveTextureARB((GLenum_)active);
    }
}

static int walk_gl_ok(walk_t *w,const char *section) {
    if(!w->apply||w->bad)return !w->bad;
    GLenum_ e=p_glGetError();
    if(!e)return 1;
    slog("glsync: context apply GL error %x in %s",e,section);
    w->bad=8;return 0;
}

static int ctx_describe(bb_t *d, u8 key[32]) {
    d->n = 0; d->over = 0;
    bb_u32(d, DESC_MAGIC_CTX); bb_u32(d, CTX_VERSION); bb_u32(d, 0);
    walk_t w = {0, 0, 0, 0, d, 0};
    walk_ctx(&w);
    if (d->over || w.bad) return 0;
    sha256(d->p, d->n, key);
    return 1;
}

static void capture_ctx(void) {
    bb_t d = {g_buf, 0, 1u << 20, 0};
    u8 key[32];
    if (!ctx_describe(&d, key)) {
        slog("glsync: refusing incomplete GL context capture");
        TerminateProcess(GetCurrentProcess(),6);for(;;){}
    }
    emit(key,d.p,d.n); /* restored keys still need a local persistence receipt */
    if (memcmp(key, SH->gl_ctx_key, 32)) {
        memcpy(SH->gl_ctx_key, key, 32);
    }
    memcpy(g_real_ctx, key, 32);
}

/* ---------------------------------------------------------------- gate */

static void take_error(void) {
    GLenum_ e = p_glGetError();
    btrace_point("glsync.take_error",0x10000u|e);
    if (e && !SH->gl_err) SH->gl_err = e;
}

/* An uncaptured gate (capture cadence). The pending GL error still moves into
 * harness_game here, as at every captured gate, so what the game's glGetError
 * returns and what SH holds never depend on the capture schedule. Everything
 * else needs no per-gate work: texture dirty bits live in harness_core and are
 * cleared only by a capture, a delete or a context reset (restore only), so the
 * next capture reads back every texture changed since the last one; the context
 * walk and the framebuffer planes are whole snapshots taken at the capture;
 * programs are captured when defined; buffers, lists and ATI shaders keep their
 * state in captured arena/SH memory and the gate only validates them. */
void glsync_gate_skip(void) {
    if (!g_capture_on) return;
    if(!resolve())capture_abort("no current GL context");
    take_error();
}

static void framebuffer_capture(void);
static u8 g_real_fb[32];
/* At each captured gate, before the pages are walked: read back what changed. */
static void capture_impl(void) {
    btrace_point("glsync.capture.entry",30);
    if (!g_capture_on) return;
    if(!resolve())capture_abort("no current GL context");
    if (!core_ready()) {
        slog("glsync: refusing capture without GL shadow storage");
        TerminateProcess(GetCurrentProcess(),6);for(;;){}
    }
    take_error();                      /* the game's pending error moves into harness_game */
    if(g_spool_active||g_spool_handle!=INVALID_HANDLE_VALUE)capture_abort("nested GL frame spool");
    g_spool_active=g_spool_mode;g_spool_count=0;
    u64 begin=glsync_profile_now(),stamp=begin,next;
    glbuffers_capture();
    btrace_point("glsync.buffers.done",31);
    gllists_capture();
    btrace_point("glsync.lists.done",32);
    if(!glati_capture_ready()) {
        slog("ATI shader capture incomplete");
        capture_abort("ATI shader capture incomplete");
    }
    btrace_point("glsync.ati.done",33);
    next=glsync_profile_now();gl_profile_record(0,stamp,next);stamp=next;
    capture_textures();
    btrace_point("glsync.textures.done",34);
    next=glsync_profile_now();gl_profile_record(1,stamp,next);stamp=next;
    capture_ctx();
    next=glsync_profile_now();gl_profile_record(2,stamp,next);stamp=next;
    framebuffer_capture();
    next=glsync_profile_now();gl_profile_record(3,stamp,next);
    GLenum_ e = p_glGetError();
    btrace_point("glsync.capture.error",0x20000u|e);
    if(e)capture_abort("capture-owned GL query/readback error");
    spool_finish();
    gl_profile_record(4,begin,glsync_profile_now());
}

/* Capture binds, activates and pushes/pops through p_gl* directly and writes
 * no texture parameter: the binding shadow is invalid on both sides of it. */
void glsync_capture(void) {
    ts_invalidate_bindings();
    capture_impl();
    ts_invalidate_bindings();
}

/* The game's glGetError: the error harness_game holds first, then the driver's. */
GLenum_ glsync_get_error(GLenum_ (GLAPI *real)(void)) {
    if (SH->gl_err) { GLenum_ e = SH->gl_err; SH->gl_err = 0; return e; }
    return real();
}

/* ---------------------------------------------------------------- restore */

/* gl.need: the descriptor keys the driver lacks, as "t:<name>:<hex>" / "p:..." / "c:<hex>" items. */
void glsync_need(buf_t *out) {
    char hx[65];
    b_str(out, "{\"need\":[");
    u32 n = 0;
    if (!resolve() || !core_ready()) { b_str(out, "],\"ok\":false,\"error\":\"no GL context\"}"); return; }
    for (u32 v = 1; v < GL_TEXMAX; v++) {
        if (!memcmp(SH->gl_tex_key[v], g_real_tex[v], 32) || !memcmp(SH->gl_tex_key[v], ZERO32, 32)) continue;
        hexkey(hx, SH->gl_tex_key[v]);
        if (n++) b_ch(out, ',');
        b_str(out, "\""); b_str(out, hx); b_str(out, "\"");
    }
    for (u32 v = 1; v < GL_PROGMAX; v++) {
        if (!memcmp(SH->gl_prog_key[v], g_real_prog[v], 32) || !memcmp(SH->gl_prog_key[v], ZERO32, 32)) continue;
        hexkey(hx, SH->gl_prog_key[v]);
        if (n++) b_ch(out, ',');
        b_str(out, "\""); b_str(out, hx); b_str(out, "\"");
    }
    if(n)memset(g_real_ctx,0,sizeof g_real_ctx); /* object replay can replace physical bindings */
    if (memcmp(SH->gl_ctx_key, g_real_ctx, 32) && memcmp(SH->gl_ctx_key, ZERO32, 32)) {
        hexkey(hx, SH->gl_ctx_key);
        if (n++) b_ch(out, ',');
        b_str(out, "\""); b_str(out, hx); b_str(out, "\"");
    }
    /* Always request pixels after memory restore: driver buffer contents are
     * not implied by object/context shadow identity. */
    if(memcmp(SH->gl_framebuffer_key,ZERO32,32)) {
        hexkey(hx,SH->gl_framebuffer_key);if(n++)b_ch(out,',');
        b_ch(out,'"');b_str(out,hx);b_ch(out,'"');
    }
    b_str(out, "],\"ok\":true}");
}

/* The apply set is a sequence of groups, one per descriptor:
 *   {u32 'KPGG', u32 count} then count records {u32 'KPGB', u32 len, sha256, bytes, pad to 4},
 * the descriptor last, its blobs before it. The descriptor is read into g_setbuf; an image is read
 * from its file offset into g_img just before it is uploaded. */
#define GROUP_MAGIC 0x4747504Bu       /* "KPGG" */
#define SET_MAX 256u
static struct { u8 key[32]; u32 off, len; } g_set[SET_MAX];
static u32 g_nset;
static HANDLE g_seth;

static const u8 *fetch(const u8 key[32], u32 *len) {
    for (u32 i = 0; i < g_nset; i++) {
        if (memcmp(g_set[i].key, key, 32)) continue;
        DWORD got = 0;
        if (!img_room(g_set[i].len) || SetFilePointer(g_seth, (LONG)g_set[i].off, 0, FILE_BEGIN) == INVALID_SET_FILE_POINTER ||
            !ReadFile(g_seth, g_img, g_set[i].len, &got, 0) || got != g_set[i].len)
            return 0;
        u8 actual[32];sha256(g_img,g_set[i].len,actual);
        if(memcmp(actual,key,32))return 0;
        *len = g_set[i].len;
        return g_img;
    }
    return 0;
}

#include "glframe.inc"

static int tex_build(GLuint_ v, const u8 *d, u32 n, u8 key_out[32]) {
    if (n < 12) return 0;
    u32 nchild = ((const u32 *)d)[2];
    if(nchild>(n-12)/32)return 0;
    u32 at=12+nchild*32;
    if(n-at<8)return 0;
    const u8 (*kids)[32] = (const u8 (*)[32])(d + 12);
    GLenum_ target = *(const u32 *)(d + at); at += 4;
    u32 np = *(const u32 *)(d + at); at += 4;
    const u8 *params = d + at;
    if(np>(n-at)/12)return 0;
    at+=np*12;
    if(n-at<4)return 0;
    u32 nimg = *(const u32 *)(d + at); at += 4;
    if(nimg>(n-at)/sizeof(img_t))return 0;
    if(at+nimg*sizeof(img_t)!=n)return 0;
    const img_t *imgs = (const img_t *)(d + at);
    if(target!=0x0de0&&target!=0x0de1&&target!=0x8513)return 0;
    if(nimg>96||nchild!=nimg)return 0;
    for(u32 i=0;i<nimg;i++) {
        const img_t*im=&imgs[i];
        if(!im->w||!im->h||im->w>0x7fffffffu||im->h>0x7fffffffu||im->border>1||
           im->level>=16||im->child>=nchild||!im->size||im->size>0x7fffffffu)return 0;
        if(target==0x8513 ? (im->face<0x8515||im->face>0x851a) : im->face!=target)return 0;
        if(target==0x0de0&&im->h!=1)return 0;
        if(!im->compressed) {
            GLenum_ fmt,type;u32 bpp;
            if(!base_format((GLint_)im->ifmt,&fmt,&type,&bpp))return 0;
            if(im->fmt!=fmt||im->type!=type||(u64)im->w*im->h*bpp!=im->size)return 0;
        }
    }
    gl_tex_discard_real(v);
    p_glBindTexture(target, gl_tex_real_name(v));
    SH->gl_tex_target[v] = target == 0x8513 ? 2 : target == 0x0DE0 ? 3 : 1;
    p_glTexParameteri(target, 0x8191, 0);          /* no generation while the levels go in */
    for (u32 i = 0; i < nimg; i++) {
        const img_t *im = &imgs[i];
        u32 len = 0;
        const u8 *px = im->child < nchild ? fetch(kids[im->child], &len) : 0;
        if (!px || len != im->size) { slog("glsync: texture %u image blob missing", v); return 0; }
        if(target==0x0de0) {
            if(im->h!=1||im->face!=target)return 0;
            if(im->compressed)p_glCompressedTexImage1DARB(target,(GLint_)im->level,im->ifmt,(GLsizei_)im->w,
                                                         (GLint_)im->border,(GLsizei_)im->size,px);
            else p_glTexImage1D(target,(GLint_)im->level,(GLint_)im->ifmt,(GLsizei_)im->w,
                               (GLint_)im->border,im->fmt,im->type,px);
        } else if (im->compressed)
            p_glCompressedTexImage2DARB(im->face, (GLint_)im->level, im->ifmt, (GLsizei_)im->w, (GLsizei_)im->h,
                                        (GLint_)im->border, (GLsizei_)im->size, px);
        else
            p_glTexImage2D(im->face, (GLint_)im->level, (GLint_)im->ifmt, (GLsizei_)im->w, (GLsizei_)im->h,
                           (GLint_)im->border, im->fmt, im->type, px);
    }
    GLfloat_ bc[4] = {0, 0, 0, 0};
    for (u32 i = 0; i < np; i++) {
        const u32 *p = (const u32 *)(params + i * 12);
        if (p[1] == 0) p_glTexParameteri(target, p[0], (GLint_)p[2]);
        else if (p[1] == 1) { GLfloat_ value; __builtin_memcpy(&value, &p[2], sizeof value); p_glTexParameterf(target, p[0], value); }
        else if (p[1] <= 5) { __builtin_memcpy(&bc[p[1] - 2], &p[2], sizeof bc[0]); if (p[1] == 5) p_glTexParameterfv(target, 0x1004, bc); }
    }
    bb_t chk = {g_buf, 0, 2u << 20, 0};
    return tex_describe(v, target, &chk, key_out);
}

static int prog_build(GLuint_ v, const u8 *d, u32 n) {
    if (n < 24) return 0;
    const u32 *w = (const u32 *)d;
    GLenum_ target = w[3], format = w[4];
    u32 len = w[5];
    if(len>n-24 || len!=n-24)return 0;
    GLint_ prev = 0;
    p_glGetProgramivARB(target, 0x8677, &prev);
    p_glBindProgramARB(target, v);
    g_real_prog_target[v]=target;
    p_glProgramStringARB(target, format, (GLsizei_)len, d + 24);
    GLint_ actual_len=0,actual_format=0;
    p_glGetProgramivARB(target,0x8627,&actual_len);
    p_glGetProgramivARB(target,0x8876,&actual_format);
    int ok=actual_len>=0&&(u32)actual_len==len&&(u32)actual_format==format&&len<=BUF_SIZE;
    if(ok){p_glGetProgramStringARB(target,0x8628,g_buf);ok=!memcmp(g_buf,d+24,len);}
    p_glBindProgramARB(target, (GLuint_)prev);
    if(p_glGetError())ok=0;
    if(ok){u8 key[32];sha256(d,n,key);emit(key,d,n);}
    return ok;
}

static int read_all(HANDLE h, void *p, u32 n) {
    DWORD got = 0;
    return ReadFile(h, p, n, &got, 0) && got == n;
}

/* gl.apply: delete what T does not have, rebuild every object whose key the driver lacks from the
 * set, then the context; each rebuilt object is read back and its key checked. */
static int apply_failed(void) {
    /* A driver error may be discovered after a per-object key was installed.
     * Every unsuccessful attempt invalidates those external-state claims. */
    if(g_real_tex)memset(g_real_tex,0,GL_TEXMAX*32);
    if(g_real_prog)memset(g_real_prog,0,GL_PROGMAX*32);
    memset(g_real_ctx,0,sizeof g_real_ctx);
    return 0;
}
static int apply_impl(const char *path, buf_t *out) {
    u32 rebuilt = 0, bad = 0, missing = 0, deleted = 0, groups = 0;
    const char *err = 0;
    if (!resolve() || !core_ready()) { b_str(out, "{\"error\":\"no GL context\""); return apply_failed(); }
    p_glGetError();
    memset(g_real_fb,0,32);
    if(!gl_tex_reconcile()){b_str(out,"{\"error\":\"texture context ownership mismatch\"");return apply_failed();}
    if(!gllists_restore()) { b_str(out,"{\"error\":\"display-list reconstruction failed\"");return apply_failed(); }
    if(!glati_restore()) { b_str(out,"{\"error\":\"ATI shader reconstruction failed\"");return apply_failed(); }
    u32 group_error=0;
#define APPLY_GL_CHECK(stage,detail) do { GLenum_ ge_=p_glGetError(); \
        if(ge_){ if(group_error++<8)slog("glsync: apply GL error %x after %s (%x)",ge_,stage,(u32)(detail)); \
                 bad++; if(!err)err="native GL error while rebuilding (see shim log)"; } } while(0)
    for (u32 v = 1; v < GL_TEXMAX; v++)
        if (!memcmp(SH->gl_tex_key[v], ZERO32, 32) && memcmp(g_real_tex[v], ZERO32, 32)) {
            gl_tex_discard_real(v);
            memset(g_real_tex[v], 0, 32);
            deleted++;
        }
    for (u32 v = 1; v < GL_PROGMAX; v++)
        if (g_real_prog_target[v] && (SH->prog[v]!=g_real_prog_target[v] ||
            !memcmp(SH->gl_prog_key[v],ZERO32,32))) {
            GLuint_ name = v;
            p_glDeleteProgramsARB(1, &name);
            g_real_prog_target[v]=0;
            memset(g_real_ctx,0,sizeof g_real_ctx); /* deleting resets object-local values */
            memset(g_real_prog[v], 0, 32);
            deleted++;
        }
    APPLY_GL_CHECK("stale object deletion",deleted);
    HANDLE h = INVALID_HANDLE_VALUE;
    if (path && path[0]) {
        h = CreateFileA(path, GENERIC_READ, FILE_SHARE_READ, 0, OPEN_EXISTING, 0, 0);
        if (h == INVALID_HANDLE_VALUE) err = "cannot open the set";
    }
    g_seth = h;
    texsave_t s;
    tex_bind_begin(&s);
    APPLY_GL_CHECK("binding save",0);
    u32 pos = 0;
    while (!err && h != INVALID_HANDLE_VALUE) {
        u32 gh[2];
        if(pos>0x7fffffffu-8u){err="set group position overflow";break;}
        DWORD got = 0;
        if (SetFilePointer(h, (LONG)pos, 0, FILE_BEGIN) == INVALID_SET_FILE_POINTER || !ReadFile(h, gh, 8, &got, 0)) {
            err = "cannot read the set";
            break;
        }
        if (got == 0) break;
        if (got != 8 || gh[0] != GROUP_MAGIC || !gh[1] || gh[1] > SET_MAX) { err = "bad group header"; break; }
        pos += 8;
        g_nset = 0;
        for (u32 i = 0; i < gh[1] && !err; i++) {
            u32 rh[10];
            if (SetFilePointer(h, (LONG)pos, 0, FILE_BEGIN) == INVALID_SET_FILE_POINTER || !read_all(h, rh, 40) ||
                rh[0] != SET_MAGIC) {
                err = "bad set record";
                break;
            }
            memcpy(g_set[g_nset].key, rh + 2, 32);
            if(rh[1]>0x7fffffb8u || pos>0x7fffffffu-40u-((rh[1]+3u)&~3u)) {
                err="set record size overflow";break;
            }
            g_set[g_nset].off=pos+40;
            g_set[g_nset].len=rh[1];
            g_nset++;
            pos+=40+((rh[1]+3u)&~3u);
        }
        if (err) break;
        groups++;
        u32 dlen = g_set[g_nset - 1].len;
        if (dlen > SETBUF_SIZE || SetFilePointer(h, (LONG)g_set[g_nset - 1].off, 0, FILE_BEGIN) == INVALID_SET_FILE_POINTER ||
            !read_all(h, g_setbuf, dlen)) {
            err = "cannot read a descriptor";
            break;
        }
        const u8 *key = g_set[g_nset - 1].key, *d = g_setbuf;
        u8 actual[32];sha256(d,dlen,actual);
        if(memcmp(actual,key,32)){err="descriptor hash mismatch";break;}
        /* Incoming keys are not persistence receipts. Readback persists validated blobs. */
        u32 magic = dlen >= 4 ? *(const u32 *)d : 0;
        if (magic == DESC_MAGIC_TEX) {
            for (u32 v = 1; v < GL_TEXMAX; v++) {
                if (memcmp(SH->gl_tex_key[v], key, 32) || !memcmp(g_real_tex[v], key, 32)) continue;
                u8 got2[32]={0};
                if (!tex_build(v, d, dlen, got2) || memcmp(got2, key, 32)) {
                    bad++;
                    slog("glsync: texture %u rebuilt with another key", v);
                }
                memcpy(g_real_tex[v], got2, 32);
                g_dirty_px[v] = g_dirty_par[v] = 0;
                rebuilt++;
                APPLY_GL_CHECK("texture rebuild",v);
            }
        } else if (magic == DESC_MAGIC_PROG) {
            for (u32 v = 1; v < GL_PROGMAX; v++) {
                if (memcmp(SH->gl_prog_key[v], key, 32) || !memcmp(g_real_prog[v], key, 32)) continue;
                if (!prog_build(v, d, dlen)) { bad++;memset(g_real_prog[v],0,32); }
                else memcpy(g_real_prog[v], key, 32);
                rebuilt++;
                APPLY_GL_CHECK("program rebuild",v);
            }
        } else if (magic == DESC_MAGIC_CTX) {
            if(dlen<12 || ((const u32 *)d)[1]!=CTX_VERSION) {
                err="unsupported GL context descriptor version: KPGC v6/new recording pin required";break;
            }
            if (!memcmp(SH->gl_ctx_key, key, 32) && memcmp(g_real_ctx, key, 32)) {
                tex_bind_end(&s);              /* the context sets every binding itself */
                APPLY_GL_CHECK("binding restore before the context",0);
                walk_t w = {1, d, 12, dlen, 0, 0};
                walk_ctx(&w);
                bb_t chk = {g_buf, 0, 1u << 20, 0};
                u8 got2[32];
                if (w.bad || !ctx_describe(&chk, got2) || memcmp(got2, key, 32)) {
                    bad++;
                    slog("glsync: context state applied with another key (walk %d)", w.bad);
                    memset(g_real_ctx,0,32);
                } else memcpy(g_real_ctx, key, 32);
                rebuilt++;
                APPLY_GL_CHECK("context describe",0);
                tex_bind_begin(&s);
                APPLY_GL_CHECK("binding save after the context",0);
            }
        } else if (magic == DESC_MAGIC_FB) {
            tex_bind_end(&s);
            APPLY_GL_CHECK("binding restore before the framebuffer",groups);
            if(memcmp(key,SH->gl_framebuffer_key,32)) {
                char descriptor_key[65], target_key[65];
                hexkey(descriptor_key,key);hexkey(target_key,SH->gl_framebuffer_key);
                slog("glsync: framebuffer descriptor key mismatch target=%s descriptor=%s",
                     target_key,descriptor_key);
                bad++;err="default framebuffer descriptor key mismatch";
            } else if(!framebuffer_apply(d,dlen,key)) {
                bad++;err="default framebuffer reconstruction/readback failed";
            }else rebuilt++;
            tex_bind_begin(&s);
        } else err = "unknown descriptor";
    }
    tex_bind_end(&s);
    ati_apply();       /* in harness_game, not keyed: set whether or not the context came back */
    if(!err && !bad && !wh_restore_gl_bindings()) {
        bad++;err="cannot restore pbuffer texture bindings";
    }
    if (h != INVALID_HANDLE_VALUE) CloseHandle(h);
    g_seth = INVALID_HANDLE_VALUE;
    /* the context goes last: when it came earlier in the set, the texture work above moved bindings */
    for (u32 v = 1; v < GL_TEXMAX; v++)
        if (memcmp(SH->gl_tex_key[v], g_real_tex[v], 32)) missing++;
    for (u32 v = 1; v < GL_PROGMAX; v++)
        if (memcmp(SH->gl_prog_key[v], g_real_prog[v], 32)) missing++;
    if (memcmp(SH->gl_ctx_key, ZERO32, 32) && memcmp(SH->gl_ctx_key, g_real_ctx, 32)) missing++;
    if(memcmp(SH->gl_framebuffer_key,g_real_fb,32))missing++;
#undef APPLY_GL_CHECK
    GLenum_ e = p_glGetError();
    if (e) { bad++;slog("glsync: GL error %x while rebuilding", e); }
    g_gl_rebuilt += rebuilt;
    g_gl_verify_bad += bad;
    slog("glsync: apply groups=%u rebuilt=%u deleted=%u missing=%u verify_bad=%u%s%s", groups, rebuilt, deleted,
         missing, bad, err ? " error: " : "", err ? err : "");
    b_str(out, "{\"rebuilt\":"); b_u64(out, rebuilt);
    b_str(out, ",\"deleted\":"); b_u64(out, deleted);
    b_str(out, ",\"missing\":"); b_u64(out, missing);
    b_str(out, ",\"verify_bad\":"); b_u64(out, bad);
    b_str(out, ",\"groups\":"); b_u64(out, groups);
    if (err) { b_str(out, ",\"error\":"); b_jstr(out, err); }
    return !missing && !bad && !err ? 1 : apply_failed();
}

/* Apply rebuilds bindings, parameters, attribute stacks and the active unit
 * through p_gl* directly (including wh_restore_gl_bindings): every exit path
 * leaves the whole texture shadow invalid. */
int glsync_apply(const char *path, buf_t *out) {
    glsync_tex_shadow_invalidate();
    int ok = apply_impl(path, out);
    glsync_tex_shadow_invalidate();
    return ok;
}

void glsync_stats_json(buf_t *b) {
    b_str(b, "{\"captures\":"); b_u64(b, g_gl_captures);
    b_str(b, ",\"blobs\":"); b_u64(b, g_gl_blobs);
    b_str(b, ",\"bytes\":"); b_u64(b, g_gl_bytes);
    b_str(b, ",\"rebuilt\":"); b_u64(b, g_gl_rebuilt);
    b_str(b, ",\"verify_bad\":"); b_u64(b, g_gl_verify_bad);
    b_str(b, ",\"unrestorable\":"); b_u64(b, g_gl_unrestorable);
    b_str(b, ",\"profiling_enabled\":");b_str(b,glsync_profile_enabled()?"true":"false");
    b_str(b, ",\"capture_phases\":{");
    for(u32 i=0;i<GLP_COUNT;i++) {
        if(i)b_str(b,",");b_jstr(b,GL_PHASE_NAMES[i]);b_str(b,":{\"calls\":");b_u64(b,g_gl_phase[i].calls);
        b_str(b,",\"total_us\":");b_u64(b,g_gl_phase[i].total_us);
        b_str(b,",\"max_us\":");b_u64(b,g_gl_phase[i].max_us);b_str(b,"}");
    }
    b_str(b, "}}");
}

/* Actual context recreation invalidates every host-object/readback claim.
 * Captured keys remain intact so gl.need requests the corresponding blobs. */
void glsync_real_context_reset(void) {
    glsync_tex_shadow_invalidate();
    fb_hash_memo_reset();
    gl_tex_real_context_reset();
    memset(g_real_prog_target,0,sizeof g_real_prog_target);
    g_resolved=0; /* WGL procedure addresses and limits belong to the new context. */
    if(g_real_tex)memset(g_real_tex,0,GL_TEXMAX*32);
    if(g_real_prog)memset(g_real_prog,0,GL_PROGMAX*32);
    memset(g_real_ctx,0,sizeof g_real_ctx);
    if(g_dirty_px)memset(g_dirty_px,0,GL_TEXMAX*2);
    g_dirty_any=0;
    glbuffers_real_context_reset();
    glati_real_context_reset();
    gllists_real_context_reset();
}

/* Rebuilding buffers changes bindings even if the saved context key matches. */
void glsync_buffers_restored(void) { memset(g_real_ctx,0,sizeof g_real_ctx); }

} /* extern "C" */
