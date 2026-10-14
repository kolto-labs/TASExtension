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
 * Existence is game state: GLRender::MakeLookUpTexture asks glIsTexture each
 * frame and rebuilds its texture when the answer is no, and the renderer asks
 * glIsProgramARB. The answers come from harness_game (a name exists from its
 * first bind until the game deletes it, as in the compatibility profile), so a
 * restored process answers as the recording did whatever its driver holds.
 *
 * Content is game state too: the calls that change a texture, a program, an
 * ATI shader's constants or the GL error flag report to glsync.c, which puts
 * them in the tick and rebuilds them after a restore. A deleted texture or
 * program is really deleted; glsync rebuilds it when a restore goes back to a
 * tick that still has it. */
#include "shim.hpp"

/* Fixed C hook ABI and assembly-referenced symbol names. */
extern "C" {

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

static u32 g_off_main;

static void (GLAPI *r_glGenTextures)(GLsizei_, GLuint_ *);
static void (GLAPI *r_glBindTexture)(GLenum_, GLuint_);
static void (GLAPI *r_glDeleteTextures)(GLsizei_, const GLuint_ *);
static GLuint_ (GLAPI *r_glGenLists)(GLsizei_);
static void (GLAPI *r_glDeleteLists)(GLuint_, GLsizei_);
static GLenum_ (GLAPI *r_glGetError)(void);
static void *(GLAPI *r_wglGetProcAddress)(const char *);

static void (GLAPI *x_DeleteProgramsARB)(GLsizei_, const GLuint_ *);
static void (GLAPI *x_DeleteFragmentShaderATI)(GLuint_);

static void off_main(const char *fn) {
    if (K->cur != 0 && g_off_main < 20) { g_off_main++; slog("GL %s from slot %u", fn, K->cur); }
}

/* Typed routing retained; glenv supplies one callback-aware scope around the
 * final game-facing GL boundary, avoiding nested scopes losing callback state. */
static void (GLAPI *r_glEnable)(GLenum_);
static void (GLAPI *r_glFinish)(void);
static void GLAPI hk_glFinish(void) {
    r_glFinish();
}
static void (GLAPI *r_glDrawElements)(GLenum_,GLsizei_,GLenum_,const void *);
static void GLAPI hk_glEnable(GLenum_ cap) {
    r_glEnable(cap);
}
static void GLAPI hk_glDrawElements(GLenum_ mode,GLsizei_ count,GLenum_ type,const void *indices) {
    if(render_skip_elements(mode,count,type))return;   /* render.cpp: draw-skipped frame */
    r_glDrawElements(mode,count,type,indices);
}
static void (GLAPI *r_glDrawArrays)(GLenum_,GLint_,GLsizei_);
static void GLAPI hk_glDrawArrays(GLenum_ mode,GLint_ first,GLsizei_ count) {
    if(render_skip_arrays(mode,first,count))return;
    r_glDrawArrays(mode,first,count);
}
static void (GLAPI *r_glClear)(GLbitfield_);
static void GLAPI hk_glClear(GLbitfield_ mask) {
    if(render_skip_clear(mask))return;
    r_glClear(mask);
}
static void (GLAPI *r_glReadPixels)(GLint_,GLint_,GLsizei_,GLsizei_,GLenum_,GLenum_,void *);
static void GLAPI hk_glReadPixels(GLint_ x,GLint_ y,GLsizei_ w,GLsizei_ h,GLenum_ f,GLenum_ t,void *p) {
    if(render_refuse_read())return;   /* draw-skipped or stale front: fault latched, the gate refuses this tick */
    r_glReadPixels(x,y,w,h,f,t,p);
}
/* Immediate mode and display lists are never skipped; they draw, so they are
 * checked for textures copied on a draw-skipped frame (render.cpp) before the
 * driver call: at glBegin, since no query is allowed inside glBegin/glEnd and
 * no binding can change there. */
static void (GLAPI *r_glBegin)(GLenum_);
static void GLAPI hk_glBegin(GLenum_ mode) {
    render_check_draw();
    r_glBegin(mode);
}
static void (GLAPI *r_glCallLists)(GLsizei_,GLenum_,const void *);
static void GLAPI hk_glCallLists(GLsizei_ n,GLenum_ type,const void *lists) {
    render_check_draw();
    r_glCallLists(n,type,lists);
}

/* ---------------------------------------------------------------- textures */
/* Host/Wine private objects share this namespace. Only driver-generated names
 * are ours to bind or delete; captured tex[] records logical context ownership. */
static u32 g_tex_real[GL_TEXMAX],g_tex_owner[GL_TEXMAX];
static void tex_boundary_fail(const char *why,u32 value) {
    slog("GL texture boundary: %s name=%u context=%x",why,value,SH->wh.current_gl);
    TerminateProcess(GetCurrentProcess(),6);for(;;){}
}
static u32 tex_context(void) {
    if(!SH->wh.current_gl||wh_context_slot()>=WH_CAP)tex_boundary_fail("no current context",0);
    return SH->wh.current_gl;
}
u32 gl_tex_real_name(u32 v) {
    if(!v)return 0;
    u32 ctx=tex_context();
    if(v>=GL_TEXMAX||SH->tex[v]!=ctx)tex_boundary_fail("missing logical owner",v);
    if(g_tex_real[v]&&g_tex_owner[v]!=ctx)tex_boundary_fail("physical owner mismatch",v);
    if(!g_tex_real[v]){
        r_glGenTextures(1,&g_tex_real[v]);
        if(!g_tex_real[v])tex_boundary_fail("driver allocation failed",v);
        g_tex_owner[v]=ctx;
    }
    return g_tex_real[v];
}
/* Temporary host state may legitimately contain a Wine-owned name. The
 * sentinel is for temporary saves only; never serialize it as a game ID. */
u32 gl_tex_saved_binding(u32 real) {
    if(!real)return 0;u32 ctx=tex_context();
    for(u32 v=1;v<GL_TEXMAX;v++)if(g_tex_real[v]==real&&g_tex_owner[v]==ctx)return v;
    return 0xffffffffu;
}
u32 gl_tex_logical_name(u32 real) {
    if(!real)return 0;u32 ctx=tex_context();
    for(u32 v=1;v<GL_TEXMAX;v++)if(g_tex_real[v]==real&&g_tex_owner[v]==ctx){
        if(SH->tex[v]!=ctx)tex_boundary_fail("native binding absent from snapshot",real);
        return v;
    }
    tex_boundary_fail("unowned native binding",real);return 0;
}
void gl_tex_discard_real(u32 v) {
    if(!v||v>=GL_TEXMAX)return;
    if(g_tex_real[v]){
        if(g_tex_owner[v]!=tex_context())tex_boundary_fail("delete from wrong context",v);
        render_texture_deleted(g_tex_real[v]);   /* its taint goes with the driver name (render.cpp) */
        r_glDeleteTextures(1,&g_tex_real[v]);
        g_tex_real[v]=g_tex_owner[v]=0;
        glsync_tex_shadow_invalidate(); /* bindings revert to 0, parameters are gone */
    }
    glsync_tex_real_gone(v);
}
int gl_tex_reconcile(void) {
    u32 ctx=tex_context();
    for(u32 v=1;v<GL_TEXMAX;v++){
        if(SH->tex[v]&&SH->tex[v]!=ctx)return 0;
        if(!SH->tex[v]){gl_tex_discard_real(v);continue;}
        if(!((SH->gl_tex_live[v>>3]>>(v&7))&1))gl_tex_discard_real(v);
        (void)gl_tex_real_name(v);
    }
    return 1;
}
void gl_tex_real_context_reset(void) {
    memset(g_tex_real,0,sizeof g_tex_real);memset(g_tex_owner,0,sizeof g_tex_owner);
}
int gl_tex_binding_enum(u32 e) {
    return e==GL_TEXTURE_BINDING_1D_||e==GL_TEXTURE_BINDING_2D_||e==GL_TEXTURE_BINDING_3D_||e==GL_TEXTURE_BINDING_CUBE_MAP_;
}
static void GLAPI hk_glGenTextures(GLsizei_ n, GLuint_ *out) {
    off_main("glGenTextures");
    if(n<0){if(!SH->gl_err)SH->gl_err=0x501;return;}
    if(!n)return;u32 ctx=tex_context();
    for(GLsizei_ i=0;i<n;i++){
        u32 v=SH->gl_tex_next;if(!v||v>=GL_TEXMAX)v=1;
        u32 tries=0;while(SH->tex[v]&&tries++<GL_TEXMAX-1){if(++v==GL_TEXMAX)v=1;}
        if(tries>=GL_TEXMAX-1)tex_boundary_fail("logical capacity exhausted",v);
        gl_tex_discard_real(v);SH->tex[v]=ctx;
        (void)gl_tex_real_name(v);out[i]=v;SH->gl_tex_next=v+1;
    }
}
static void GLAPI hk_glDeleteTextures(GLsizei_ n,const GLuint_ *names) {
    if(n<0){if(!SH->gl_err)SH->gl_err=0x501;return;}
    if(n)glsync_tex_shadow_invalidate();
    for(GLsizei_ i=0;i<n;i++){
        u32 v=names[i];if(!v||v>=GL_TEXMAX||!SH->tex[v])continue;
        if(SH->tex[v]!=tex_context())tex_boundary_fail("logical delete owner mismatch",v);
        gl_tex_discard_real(v);SH->tex[v]=0;
        SH->gl_tex_live[v>>3]&=(u8)~(1u<<(v&7));glsync_tex_deleted(v);
    }
}
static void GLAPI hk_glBindTexture(GLenum_ target,GLuint_ v) {
    if(target!=0x0de0&&target!=0x0de1&&target!=0x8513)tex_boundary_fail("unsupported texture target",target);
    if(v>=GL_TEXMAX)tex_boundary_fail("logical name out of range",v);
    if(v&&!SH->tex[v]){gl_tex_discard_real(v);SH->tex[v]=tex_context();}
    r_glBindTexture(target,gl_tex_real_name(v));
    /* Shadow right after the driver call and before glsync_tex_target: the refusal check reads the old gl_tex_target. */
    glsync_tex_shadow_bind(target,v);
    if(v){SH->gl_tex_live[v>>3]|=(u8)(1u<<(v&7));if(!SH->gl_tex_target[v])glsync_tex_target(v,target);}
}

/* Pixel-store extension pnames outside the modeled GL1.2 state are refused
 * before driver mutation. Valid base pnames retain native validation/errors. */
static void (GLAPI *r_glPixelStorei)(GLenum_,GLint_);
static void GLAPI hk_glPixelStorei(GLenum_ p,GLint_ v) {
    if(!((p>=0x0d00&&p<=0x0d05)||(p>=0x0cf0&&p<=0x0cf5)||(p>=0x806b&&p<=0x806e)))
        tex_boundary_fail("unmodeled pixel-store pname",p);
    r_glPixelStorei(p,v);
}

/* calls that change the bound texture's images or parameters */
static void (GLAPI *r_glTexImage1D)(GLenum_,GLint_,GLint_,GLsizei_,GLint_,GLenum_,GLenum_,const void *);
static void GLAPI hk_glTexImage1D(GLenum_ t,GLint_ l,GLint_ f,GLsizei_ w,GLint_ b,GLenum_ fm,GLenum_ ty,const void*p) {
    glsync_tex_validate(t,1);r_glTexImage1D(t,l,f,w,b,fm,ty,p);glsync_tex_dirty(t,1);
}
static void (GLAPI *r_glTexImage2D)(GLenum_, GLint_, GLint_, GLsizei_, GLsizei_, GLint_, GLenum_, GLenum_, const void *);
static void (GLAPI *r_glTexSubImage2D)(GLenum_, GLint_, GLint_, GLint_, GLsizei_, GLsizei_, GLenum_, GLenum_,
                                       const void *);
static void (GLAPI *r_glCopyTexImage2D)(GLenum_, GLint_, GLenum_, GLint_, GLint_, GLsizei_, GLsizei_, GLint_);
static void (GLAPI *r_glCopyTexSubImage2D)(GLenum_, GLint_, GLint_, GLint_, GLint_, GLint_, GLsizei_, GLsizei_);
static void (GLAPI *r_glTexParameteri)(GLenum_, GLenum_, GLint_);
static int (GLAPI *r_gluBuild2DMipmaps)(GLenum_, GLint_, GLsizei_, GLsizei_, GLenum_, GLenum_, const void *);

static void GLAPI hk_glTexImage2D(GLenum_ t, GLint_ l, GLint_ f, GLsizei_ w, GLsizei_ h, GLint_ b, GLenum_ fm,
                                  GLenum_ ty, const void *p) {
    glsync_tex_validate(t,1);
    r_glTexImage2D(t, l, f, w, h, b, fm, ty, p);
    glsync_tex_dirty(t, 1);
}
static void GLAPI hk_glTexSubImage2D(GLenum_ t, GLint_ l, GLint_ x, GLint_ y, GLsizei_ w, GLsizei_ h, GLenum_ fm,
                                     GLenum_ ty, const void *p) {
    glsync_tex_validate(t,1);
    r_glTexSubImage2D(t, l, x, y, w, h, fm, ty, p);
    glsync_tex_dirty(t, 1);
}
static void GLAPI hk_glCopyTexImage2D(GLenum_ t, GLint_ l, GLenum_ f, GLint_ x, GLint_ y, GLsizei_ w, GLsizei_ h,
                                      GLint_ b) {
    glsync_tex_validate(t,1);
    r_glCopyTexImage2D(t, l, f, x, y, w, h, b);
    render_note_copy(t, l, 0, 0, w, h, 0);
    glsync_tex_dirty(t, 1);
}
static void GLAPI hk_glCopyTexSubImage2D(GLenum_ t, GLint_ l, GLint_ xo, GLint_ yo, GLint_ x, GLint_ y, GLsizei_ w,
                                         GLsizei_ h) {
    glsync_tex_validate(t,1);
    r_glCopyTexSubImage2D(t, l, xo, yo, x, y, w, h);
    render_note_copy(t, l, xo, yo, w, h, 1);
    glsync_tex_dirty(t, 1);
}
static void GLAPI hk_glTexParameteri(GLenum_ t, GLenum_ pname, GLint_ v) {
    glsync_tex_param_validate(t,pname);
    /* same() before the driver call, set() right after with nothing between: g_ts_check carries verify state across it. */
    int same = glsync_tex_param_same(t, pname, v);
    r_glTexParameteri(t, pname, v);
    glsync_tex_param_set(t, pname, v);
    if (!same) glsync_tex_dirty(t, 0);
}
/* Shadowed so glsync's bound-texture lookups need no driver query. An
 * extension, never an opengl32 import: wglGetProcAddress and kernel32
 * GetProcAddress both reach it through select_gl_proc -> texture_proc. */
static void (GLAPI *r_glActiveTextureARB)(GLenum_);
static void GLAPI hk_glActiveTextureARB(GLenum_ unit) {
    r_glActiveTextureARB(unit);
    /* Shadow right after the driver call: a refused unit enum becomes "unknown", never a stale unit. */
    glsync_tex_shadow_active(unit);
}
static int GLAPI hk_gluBuild2DMipmaps(GLenum_ t, GLint_ c, GLsizei_ w, GLsizei_ h, GLenum_ f, GLenum_ ty,
                                      const void *p) {
    glsync_tex_validate(t,1);
    int r = r_gluBuild2DMipmaps(t, c, w, h, f, ty, p);
    glsync_tex_dirty(t, 1);
    return r;
}

static GLboolean_ GLAPI hk_glIsTexture(GLuint_ v) {
    return v && v < GL_TEXMAX && SH->tex[v]==tex_context() && (SH->gl_tex_live[v >> 3] >> (v & 7)) & 1;
}

static GLboolean_ (GLAPI *r_glAreTexturesResident)(GLsizei_,const GLuint_*,GLboolean_*);
static void (GLAPI *r_glPrioritizeTextures)(GLsizei_,const GLuint_*,const float*);
static GLboolean_ GLAPI hk_glAreTexturesResident(GLsizei_ n,const GLuint_*names,GLboolean_*res) {
    if(n<0){if(!SH->gl_err)SH->gl_err=0x501;return 0;}
    for(GLsizei_ i=0;i<n;i++)if(!names[i]||names[i]>=GL_TEXMAX||!hk_glIsTexture(names[i])){
        if(!SH->gl_err)SH->gl_err=0x501;return 0;
    }
    GLboolean_ all=1;
    for(GLsizei_ i=0;i<n;i++){u32 real=gl_tex_real_name(names[i]);GLboolean_ one=1;
        GLboolean_ yes=r_glAreTexturesResident(1,&real,&one);res[i]=yes?1:one;if(!yes)all=0;}
    return all;
}
static void GLAPI hk_glPrioritizeTextures(GLsizei_ n,const GLuint_*names,const float*priority) {
    if(n<0){if(!SH->gl_err)SH->gl_err=0x501;return;}
    for(GLsizei_ i=0;i<n;i++)if(names[i]&&names[i]<GL_TEXMAX&&hk_glIsTexture(names[i])){
        u32 real=gl_tex_real_name(names[i]);r_glPrioritizeTextures(1,&real,priority+i);
    }
    if(n)glsync_tex_shadow_invalidate(); /* TEXTURE_PRIORITY is a texture parameter */
}
static void *texture_proc(const char*name,void*p) {
#define T(n,h,r) if(k_streq(name,n)){if(!r)r=reinterpret_cast<decltype(r)>(p);return reinterpret_cast<void *>(h);}
    T("glGenTextures",hk_glGenTextures,r_glGenTextures)
    T("glDeleteTextures",hk_glDeleteTextures,r_glDeleteTextures)
    T("glBindTexture",hk_glBindTexture,r_glBindTexture)
    T("glPixelStorei",hk_glPixelStorei,r_glPixelStorei)
    T("glTexImage1D",hk_glTexImage1D,r_glTexImage1D)
    T("glTexImage2D",hk_glTexImage2D,r_glTexImage2D)
    T("glTexSubImage2D",hk_glTexSubImage2D,r_glTexSubImage2D)
    T("glCopyTexImage2D",hk_glCopyTexImage2D,r_glCopyTexImage2D)
    T("glCopyTexSubImage2D",hk_glCopyTexSubImage2D,r_glCopyTexSubImage2D)
    T("glTexParameteri",hk_glTexParameteri,r_glTexParameteri)
    T("glActiveTextureARB",hk_glActiveTextureARB,r_glActiveTextureARB)
    T("glAreTexturesResident",hk_glAreTexturesResident,r_glAreTexturesResident)
    T("glPrioritizeTextures",hk_glPrioritizeTextures,r_glPrioritizeTextures)
#undef T
    if(k_streq(name,"glIsTexture"))return (void*)hk_glIsTexture;
    return 0;
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
    u32 before;__asm__ volatile("stmxcsr %0":"=m"(before));
    GLenum_ e = glsync_get_error(r_glGetError);
    if(g_gate.tick<2)math_host_probe("glGetError",before);
    if (e && g_err_logs < 50) { g_err_logs++; slog("GL glGetError -> %x at tick %u", e, (u32)g_gate.tick); }
    return e;
}

/* ---------------------------------------------------------------- extension names */
static void GLAPI w_GenProgramsARB(GLsizei_ n, GLuint_ *out) {
    for (GLsizei_ i = 0; i < n; i++) {
        GLuint_ v = SH->gl_prog_next++;
        if (x_DeleteProgramsARB) x_DeleteProgramsARB(1, &v);
        glsync_prog_real_gone(v);
        out[i] = v;
    }
}
static void GLAPI w_DeleteProgramsARB(GLsizei_ n, const GLuint_ *names) {
    if (x_DeleteProgramsARB) x_DeleteProgramsARB(n, names);
    for (GLsizei_ i = 0; i < n; i++) glsync_program_deleted(names[i]);
}

static void (GLAPI *x_BindProgramARB)(GLenum_, GLuint_);
static GLboolean_ (GLAPI *x_IsProgramARB)(GLuint_);
static void (GLAPI *x_ProgramStringARB)(GLenum_, GLenum_, GLsizei_, const void *);
static void (GLAPI *x_CompressedTexImage2DARB)(GLenum_, GLint_, GLenum_, GLsizei_, GLsizei_, GLint_, GLsizei_,
                                              const void *);
static void (GLAPI *x_BindFragmentShaderATI)(GLuint_);
static void (GLAPI *x_SetFragmentShaderConstantATI)(GLuint_, const float *);
static void (GLAPI *x_BeginFragmentShaderATI)(void);
static void (GLAPI *x_EndFragmentShaderATI)(void);

static void GLAPI w_BindProgramARB(GLenum_ t, GLuint_ v) {
    if(v>=GL_PROGMAX)tex_boundary_fail("program name outside captured namespace",v);
    x_BindProgramARB(t,v);
    if(v&&(t==0x8620||t==0x8804)&&(!SH->prog[v]||SH->prog[v]==t)) {
        SH->prog[v]=t;glsync_program_bound(v,t);SH->gl_prog_live[v>>3]|=(u8)(1u<<(v&7));
    }
}
static GLboolean_ GLAPI w_IsProgramARB(GLuint_ v) {
    return v && v < GL_PROGMAX && (SH->gl_prog_live[v >> 3] >> (v & 7)) & 1;
}
static void GLAPI w_ProgramStringARB(GLenum_ t, GLenum_ f, GLsizei_ n, const void *s) {
    glsync_program_prepare(t);
    x_ProgramStringARB(t, f, n, s);
    glsync_program(t, f, n, s);
}
static void GLAPI w_CompressedTexImage2DARB(GLenum_ t, GLint_ l, GLenum_ f, GLsizei_ w, GLsizei_ h, GLint_ b,
                                            GLsizei_ n, const void *p) {
    glsync_tex_validate(t,1);
    x_CompressedTexImage2DARB(t, l, f, w, h, b, n, p);
    glsync_tex_dirty(t, 1);
}
static void GLAPI w_BindFragmentShaderATI(GLuint_ v) {
    glsync_ati_bind(v);
    x_BindFragmentShaderATI(v);
}
static void GLAPI w_SetFragmentShaderConstantATI(GLuint_ dst, const float *v) {
    glsync_ati_constant(dst, v);
    x_SetFragmentShaderConstantATI(dst, v);
}
static void GLAPI w_BeginFragmentShaderATI(void) {
    glsync_ati_begin();
    x_BeginFragmentShaderATI();
}
static void GLAPI w_EndFragmentShaderATI(void) {
    x_EndFragmentShaderATI();
    glsync_ati_end();
}

static GLuint_ GLAPI w_GenFragmentShadersATI(GLuint_ range) {
    if (!range) return 0;
    GLuint_ v = SH->gl_ati_next;
    SH->gl_ati_next += range;
    for (GLuint_ i = 0; i < range && x_DeleteFragmentShaderATI; i++) x_DeleteFragmentShaderATI(v + i);
    return v;
}
static void GLAPI w_DeleteFragmentShaderATI(GLuint_ v) { (void)v; }

/* Deletes are resolved first: a Gen wrapper uses them to free its name. */
typedef struct { const char *name; void **real; void *wrap; } xwrap_t;
static void *x_gen_programs, *x_gen_ati;
static xwrap_t g_xw[] = {
    {"glGenProgramsARB", &x_gen_programs, (void *)w_GenProgramsARB},
    {"glDeleteProgramsARB", (void **)&x_DeleteProgramsARB, (void *)w_DeleteProgramsARB},
    {"glGenFragmentShadersATI", &x_gen_ati, (void *)w_GenFragmentShadersATI},
    {"glDeleteFragmentShaderATI", (void **)&x_DeleteFragmentShaderATI, (void *)w_DeleteFragmentShaderATI},
    {"glBindProgramARB", (void **)&x_BindProgramARB, (void *)w_BindProgramARB},
    {"glIsProgramARB", (void **)&x_IsProgramARB, (void *)w_IsProgramARB},
    {"glProgramStringARB", (void **)&x_ProgramStringARB, (void *)w_ProgramStringARB},
    {"glCompressedTexImage2DARB", (void **)&x_CompressedTexImage2DARB, (void *)w_CompressedTexImage2DARB},
    {"glBindFragmentShaderATI", (void **)&x_BindFragmentShaderATI, (void *)w_BindFragmentShaderATI},
    {"glSetFragmentShaderConstantATI", (void **)&x_SetFragmentShaderConstantATI,
     (void *)w_SetFragmentShaderConstantATI},
    {"glBeginFragmentShaderATI", (void **)&x_BeginFragmentShaderATI, (void *)w_BeginFragmentShaderATI},
    {"glEndFragmentShaderATI", (void **)&x_EndFragmentShaderATI, (void *)w_EndFragmentShaderATI},
    {0, 0, 0},
};
static const char *g_need_delete[] = {"glDeleteProgramsARB", "glDeleteFragmentShaderATI", 0};

/* Names that hand out objects we do not virtualize: logged when resolved. */
static const char *g_unvirt[] = {"glGenProgramsNV", "glGenFencesNV", "glGenVertexShadersEXT", "glGenSymbolsEXT",
                                  "wglCreatePbufferARB", "wglGetPbufferDCARB", "wglCreateBufferRegionARB",
                                  "wglAllocateMemoryNV", 0};

/* Stable entry addresses for driver functions whose raw Wine pointers would
 * otherwise live in the exe's globals. A tail jump preserves every calling
 * convention and creates no extra return address on a saved game stack.
 * Targets are host state, outside harness_game; each launch resolves them anew. */
#define GLPROC_MAX 512
static u8 g_proc_code[GLPROC_MAX][32] __attribute__((aligned(4096)));
static void *g_proc_target[GLPROC_MAX];
static u32 g_proc_allowed[GLPROC_MAX];
static void *select_gl_proc(const char *name,void *p);
#define g_proc_name SH->gl_proc_name
#define g_proc_count SH->gl_proc_count
static void *stable_gl_proc(const char *name, void *target,u32 from_export) {
    if (!target || g_bnd_native) return target;
    for (u32 i = 0; i < g_proc_count; i++)
        if (SH->gl_proc_export[i]==from_export && k_streq(name, g_proc_name[i])) {
            g_proc_target[i] = glenv_proc(name,target);
            g_proc_allowed[i] = gllists_allowed(name);
            return g_proc_code[i];
        }
    u32 n = (u32)strlen(name);
    if (g_proc_count == GLPROC_MAX || n >= sizeof g_proc_name[0]) {
        slog("GL procedure boundary exhausted");
        TerminateProcess(GetCurrentProcess(), 6);
        return NULL;
    }
    u32 i = g_proc_count++;
    memcpy(g_proc_name[i], name, n + 1);
    SH->gl_proc_export[i]=(u8)from_export;
    g_proc_target[i] = glenv_proc(name,target);
    g_proc_allowed[i] = gllists_allowed(name);
    return g_proc_code[i];
}

/* Rebuild every captured slot before resumed game code can call its pointer.
 * The table is game state; the targets and executable stubs are host state. */
int gl_restore_procs(void) {
    if (g_proc_count > GLPROC_MAX) return 0;
    for (u32 i = 0; g_xw[i].name; i++) {
        void *p = r_wglGetProcAddress(g_xw[i].name);
        if ((uintptr_t)p <= 3 || (uintptr_t)p == 0xffffffffu) p = NULL;
        if ((SH->gl_wrappers_used & (1u << i)) && !p) return 0;
        *g_xw[i].real = p;
    }
    for (u32 i = 0; i < g_proc_count; i++) {
        u32 n = 0;
        while (n < sizeof g_proc_name[i] && g_proc_name[i][n]) n++;
        if (!n || n == sizeof g_proc_name[i]) return 0;
        if(SH->gl_proc_export[i]>1)return 0;
        void *p = SH->gl_proc_export[i] ?
            (void *)GetProcAddress(GetModuleHandleA("opengl32.dll"),g_proc_name[i]) :
            r_wglGetProcAddress(g_proc_name[i]);
        if ((uintptr_t)p <= 3 || (uintptr_t)p == 0xffffffffu) {
            slog("GL restore procedure missing: %s", g_proc_name[i]);
            return 0;
        }
        g_proc_target[i] = glenv_proc(g_proc_name[i],select_gl_proc(g_proc_name[i],p));
        g_proc_allowed[i] = gllists_allowed(g_proc_name[i]);
    }
    if(!glbuffers_restore())return 0;
    glsync_buffers_restored();
    return 1;
}

static void *select_gl_proc(const char *name,void *p) {
    /* Reuse the final imported target, including texture/handle hooks, rather
     * than letting a different lookup API bypass an already installed hook. */
    void *b = gllists_import_target(name);
    if(b)return b;
    b=texture_proc(name,p);if(b)return b;
    if(k_streq(name,"glFinish")){if(!r_glFinish)r_glFinish=reinterpret_cast<decltype(r_glFinish)>(p);return reinterpret_cast<void *>(hk_glFinish);}
    if(k_streq(name,"glEnable")){if(!r_glEnable)r_glEnable=reinterpret_cast<decltype(r_glEnable)>(p);return reinterpret_cast<void *>(hk_glEnable);}
    if(k_streq(name,"glDrawElements")){if(!r_glDrawElements)r_glDrawElements=reinterpret_cast<decltype(r_glDrawElements)>(p);return reinterpret_cast<void *>(hk_glDrawElements);}
    if(k_streq(name,"glDrawArrays")){if(!r_glDrawArrays)r_glDrawArrays=reinterpret_cast<decltype(r_glDrawArrays)>(p);return reinterpret_cast<void *>(hk_glDrawArrays);}
    if(k_streq(name,"glClear")){if(!r_glClear)r_glClear=reinterpret_cast<decltype(r_glClear)>(p);return reinterpret_cast<void *>(hk_glClear);}
    if(k_streq(name,"glReadPixels")){if(!r_glReadPixels)r_glReadPixels=reinterpret_cast<decltype(r_glReadPixels)>(p);return reinterpret_cast<void *>(hk_glReadPixels);}
    if(k_streq(name,"glBegin")){if(!r_glBegin)r_glBegin=reinterpret_cast<decltype(r_glBegin)>(p);return reinterpret_cast<void *>(hk_glBegin);}
    if(k_streq(name,"glCallLists")){if(!r_glCallLists)r_glCallLists=reinterpret_cast<decltype(r_glCallLists)>(p);return reinterpret_cast<void *>(hk_glCallLists);}
    b=(void *)wh_proc(name);
    if(b)return b;
    b = gllists_proc(name,p);
    if(b)return b;
    b = gldraw_proc(name, p);
    if (b) return b;
    b = bound_gl_proc(name, p);
    if (b) return b;
    b = wh_gl_proc(name, p);
    if (b) return b;
    b = glati_proc(name,p);
    if(b)return b;
    b = glbuffers_proc(name,p);
    if(b)return b;
    /* a Gen wrapper needs its Delete even when the game never asks for it */
    for (u32 i = 0; g_need_delete[i]; i++) {
        for (xwrap_t *w = g_xw; w->name; w++)
            if (!*w->real && k_streq(w->name, g_need_delete[i])) *w->real = r_wglGetProcAddress(w->name);
    }
    for (xwrap_t *w = g_xw; w->name; w++)
        if (k_streq(name, w->name)) {
            SH->gl_wrappers_used |= 1u << (u32)(w - g_xw);
            *w->real = p; return w->wrap;
        }
    return p;
}

static char g_resolved[4096];
static void *GLAPI hk_wglGetProcAddress(const char *name) {
    u32 fp=math_host_begin();
    void *p = r_wglGetProcAddress(name);
    math_host_end("wglGetProcAddress",fp);
    if ((uintptr_t)p <= 3 || (uintptr_t)p == 0xffffffffu) p = NULL;
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
    return btrace_proc(name,stable_gl_proc(name,select_gl_proc(name,p),0));
}

/* kernel32 GetProcAddress reaches core GL exports as well as WGL. Keep its
 * resolution source in captured slot metadata: core exports need not be
 * returned by wglGetProcAddress in a restored context or another Wine. */
void *gl_export_proc(const char *name,void *real) {
    if(!real || g_bnd_native)return real;
    if((uintptr_t)name<=0xffff) {
        u32 ordinal=(u32)(uintptr_t)name;
        u8 *base=(u8 *)GetModuleHandleA("opengl32.dll");
        IMAGE_DOS_HEADER *dos=reinterpret_cast<IMAGE_DOS_HEADER *>(base);
        IMAGE_NT_HEADERS *nt=reinterpret_cast<IMAGE_NT_HEADERS *>((base+dos->e_lfanew));
        u32 rva=nt->OptionalHeader.DataDirectory[IMAGE_DIRECTORY_ENTRY_EXPORT].VirtualAddress;
        IMAGE_EXPORT_DIRECTORY *exp=rva?reinterpret_cast<IMAGE_EXPORT_DIRECTORY *>(base+rva):nullptr;
        name=NULL;
        if(exp) {
            const u16 *ord=reinterpret_cast<const u16 *>(base+exp->AddressOfNameOrdinals);
            const u32 *names=reinterpret_cast<const u32 *>(base+exp->AddressOfNames);
            for(u32 i=0;i<exp->NumberOfNames;i++)if(exp->Base+ord[i]==ordinal) {
                name=(const char *)(base+names[i]);break;
            }
        }
        if(!name) {
            slog("GL export ordinal %u has no classifiable name",ordinal);
            TerminateProcess(GetCurrentProcess(),6);for(;;){}
        }
    }
    return btrace_proc(name,stable_gl_proc(name,select_gl_proc(name,real),1));
}

void gl_log_resolved(void) { slog("GL resolved: %s", g_resolved); }

#define HOOK(fn, h, save)                                                        \
    do {                                                                         \
        void *o = iat_hook(exe, "OPENGL32.dll", fn, (void *)(h));                \
        if (o) { *(void **)(save) = o; hooks_note("exe:" fn); }                  \
        else slog("iat miss exe %s", fn);                                        \
    } while (0)

void gl_install(void) {
    DWORD old;
    if (!VirtualProtect(g_proc_code, sizeof g_proc_code, PAGE_EXECUTE_READWRITE, &old)) {
        slog("GL procedure boundary protection failed");
        TerminateProcess(GetCurrentProcess(), 6);
        return;
    }
    for (u32 i = 0; i < GLPROC_MAX; i++) {
        gllists_guard_code(g_proc_code[i],&g_proc_target[i],&g_proc_allowed[i]);
    }
    VirtualProtect(g_proc_code, sizeof g_proc_code, PAGE_EXECUTE_READ, &old);
    FlushInstructionCache(GetCurrentProcess(), g_proc_code, sizeof g_proc_code);
    HMODULE exe = GetModuleHandleA(NULL);
    HOOK("glFinish", hk_glFinish, &r_glFinish);
    HOOK("glEnable", hk_glEnable, &r_glEnable);
    HOOK("glDrawElements", hk_glDrawElements, &r_glDrawElements);
    HOOK("glDrawArrays", hk_glDrawArrays, &r_glDrawArrays);
    HOOK("glClear", hk_glClear, &r_glClear);
    HOOK("glReadPixels", hk_glReadPixels, &r_glReadPixels);
    HOOK("glBegin", hk_glBegin, &r_glBegin);
    HOOK("glCallLists", hk_glCallLists, &r_glCallLists);
    HOOK("glGenTextures", hk_glGenTextures, &r_glGenTextures);
    HOOK("glDeleteTextures", hk_glDeleteTextures, &r_glDeleteTextures);
    HOOK("glBindTexture", hk_glBindTexture, &r_glBindTexture);
    void *unused;
    HOOK("glIsTexture", hk_glIsTexture, &unused);
    HOOK("glAreTexturesResident",hk_glAreTexturesResident,&r_glAreTexturesResident);
    HOOK("glPrioritizeTextures",hk_glPrioritizeTextures,&r_glPrioritizeTextures);
    HOOK("glPixelStorei",hk_glPixelStorei,&r_glPixelStorei);
    HOOK("glTexImage1D",hk_glTexImage1D,&r_glTexImage1D);
    HOOK("glTexImage2D", hk_glTexImage2D, &r_glTexImage2D);
    HOOK("glTexSubImage2D", hk_glTexSubImage2D, &r_glTexSubImage2D);
    HOOK("glCopyTexImage2D", hk_glCopyTexImage2D, &r_glCopyTexImage2D);
    HOOK("glCopyTexSubImage2D", hk_glCopyTexSubImage2D, &r_glCopyTexSubImage2D);
    HOOK("glTexParameteri", hk_glTexParameteri, &r_glTexParameteri);
    {
        void *o = iat_hook(exe, "GLU32.dll", "gluBuild2DMipmaps", (void *)hk_gluBuild2DMipmaps);
        if (o) { r_gluBuild2DMipmaps = reinterpret_cast<decltype(r_gluBuild2DMipmaps)>(o); hooks_note("exe:gluBuild2DMipmaps"); }
        else slog("iat miss exe gluBuild2DMipmaps");
    }
    HOOK("glGenLists", hk_glGenLists, &r_glGenLists);
    HOOK("glDeleteLists", hk_glDeleteLists, &r_glDeleteLists);
    HOOK("glGetError", hk_glGetError, &r_glGetError);
    HOOK("wglGetProcAddress", hk_wglGetProcAddress, &r_wglGetProcAddress);
}

} /* extern "C" */
