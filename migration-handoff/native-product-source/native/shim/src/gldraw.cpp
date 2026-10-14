/* Keep the application's draw/read selectors, not a Wine fullscreen FBO's
 * GL_COLOR_ATTACHMENT0. Calls still go through Wine so it can map the logical
 * selector to its drawable. Each context and attribute stack is captured.
 */
#include "shim.hpp"

extern "C" {
#define GLAPI __attribute__((stdcall))
#define GL_TEXTURE_BIT_ 0x00040000u
static void (GLAPI *p_geti)(u32,i32*);
static u32 (GLAPI *p_error)(void);
static void (GLAPI *r_draw)(u32), (GLAPI *r_read)(u32), (GLAPI *r_push)(u32), (GLAPI *r_pop)(void);
static void (GLAPI *r_geti)(u32,i32*), (GLAPI *r_getf)(u32,float*), (GLAPI *r_getd)(u32,double*), (GLAPI *r_getb)(u32,u8*);

static gld_context_t *current(void) {
    u32 slot=wh_context_slot();
    if(slot>=WH_CAP) return 0;
    gld_context_t *c=&SH->gld.context[slot];
    if(c->serial!=SH->wh.entry[slot].serial) {
        i32 dbl=wh_current_doublebuffer();
        if(dbl<0) {
            slog("GL drawable pixel format missing from captured handle state");
            TerminateProcess(GetCurrentProcess(),6);for(;;){}
        }
        memset(c,0,sizeof *c);
        c->serial=SH->wh.entry[slot].serial;
        c->draw=c->read=dbl?0x0405:0x0404; /* BACK / FRONT */
    }
    return c;
}
static void preserve_error(u32 e) { if(e&&!SH->gl_err) SH->gl_err=e; }
static int finish(void) { u32 e=p_error(); preserve_error(e); return !e; }
static int compiling(void) {
    /* Profiling: the driver query was 2.2% of a frame. With the list boundary
     * installed, every glNewList/glEndList the game issues goes through gllists,
     * and the gllists guard rejects glDrawBuffer/glReadBuffer/glPush/PopAttrib
     * while a list is open, so the shim's own state equals the driver's answer.
     * native mode installs no list wrappers: keep asking the driver there. */
    if(!g_bnd_native)return gllists_compiling();
    i32 list=0,mode=0;
    p_geti(0x0B33,&list);
    if(list) p_geti(0x0B30,&mode);
    return list&&mode==0x1300;
}
static void GLAPI draw(u32 value) {
    gld_context_t *c=current();
    int deferred=compiling();
    preserve_error(p_error()); r_draw(value);
    if(finish()&&c&&!deferred)c->draw=value;
}
static void GLAPI read_buffer(u32 value) {
    gld_context_t *c=current();
    int deferred=compiling();
    preserve_error(p_error()); r_read(value);
    if(finish()&&c&&!deferred)c->read=value;
}
static void GLAPI push(u32 mask) {
    gld_context_t *c=current();
    int deferred=compiling();
    preserve_error(p_error());r_push(mask);
    if(!finish()||!c||deferred)return;
    if(c->depth==GLD_STACK){slog("GL logical attribute stack capacity exceeded");TerminateProcess(GetCurrentProcess(),6);}
    c->stack[c->depth++]=gld_saved_t{mask,c->draw,c->read};
}
static void GLAPI pop(void) {
    gld_context_t *c=current();
    int deferred=compiling();
    /* TEXTURE_BIT restores bindings, the active unit and bound textures'
     * parameters: the texture shadow is stale. With no logical top (no
     * context, a list being compiled, or an empty logical stack) the popped
     * mask is unknown, so assume it held TEXTURE_BIT. */
    if(!c||deferred||!c->depth||(c->stack[c->depth-1].mask&GL_TEXTURE_BIT_))glsync_tex_shadow_invalidate();
    preserve_error(p_error());r_pop();
    if(!finish()||!c||deferred)return;
    if(!c->depth){slog("GL logical attribute stack missing");TerminateProcess(GetCurrentProcess(),6);}
    gld_saved_t *s=&c->stack[--c->depth];
    if(s->mask&0x4000)c->draw=s->draw; /* COLOR_BUFFER_BIT */
    if(s->mask&0x0020)c->read=s->read; /* PIXEL_MODE_BIT */
    memset(s,0,sizeof *s);
}
static int query(u32 e,i32 *v) {
    if(gl_tex_binding_enum(e)||glbuffers_binding_enum(e)){
        i32 native=0;preserve_error(p_error());p_geti(e,&native);
        if(!finish())return -1;
        *v=static_cast<i32>(gl_tex_binding_enum(e)?gl_tex_logical_name(static_cast<u32>(native)):glbuffers_logical_name(static_cast<u32>(native)));return 1;
    }
    if(e!=0x0C01&&e!=0x0C02)return 0;
    gld_context_t *c=current();if(!c)return 0;
    *v=static_cast<i32>(e==0x0C01?c->draw:c->read);return 1;
}
static void GLAPI geti(u32 e,i32 *v){i32 x;int q=query(e,&x);if(q>0)*v=x;else if(!q)r_geti(e,v);}
static void GLAPI getf(u32 e,float *v){i32 x;int q=query(e,&x);if(q>0)*v=static_cast<float>(x);else if(!q)r_getf(e,v);}
static void GLAPI getd(u32 e,double *v){i32 x;int q=query(e,&x);if(q>0)*v=static_cast<double>(x);else if(!q)r_getd(e,v);}
static void GLAPI getb(u32 e,u8 *v){i32 x;int q=query(e,&x);if(q>0)*v=x!=0;else if(!q)r_getb(e,v);}
void gldraw_selectors(i32 *d,i32 *r){
    gld_context_t *c=current();
    if(c){*d=static_cast<i32>(c->draw);*r=static_cast<i32>(c->read);}
    else {p_geti(0x0C01,d);p_geti(0x0C02,r);}
}
void *gldraw_proc(const char *name,void *target){
    (void)target;
#define P(n,f) if(k_streq(name,n))return reinterpret_cast<void*>(f)
    P("glDrawBuffer",draw);P("glReadBuffer",read_buffer);P("glPushAttrib",push);P("glPopAttrib",pop);
    P("glGetIntegerv",geti);P("glGetFloatv",getf);P("glGetDoublev",getd);P("glGetBooleanv",getb);
#undef P
    return 0;
}
void gldraw_install(void){
    HMODULE exe=GetModuleHandleA(NULL),gl=GetModuleHandleA("opengl32.dll");
    if(!gl)return;
    p_geti=reinterpret_cast<decltype(p_geti)>(GetProcAddress(gl,"glGetIntegerv"));
    p_error=reinterpret_cast<decltype(p_error)>(GetProcAddress(gl,"glGetError"));
#define H(n,f,r) do{void *old=iat_hook(exe,"OPENGL32.dll",n,reinterpret_cast<void*>(f));r=reinterpret_cast<decltype(r)>(old?old:reinterpret_cast<void*>(GetProcAddress(gl,n)));}while(0)
    H("glDrawBuffer",draw,r_draw);H("glReadBuffer",read_buffer,r_read);
    H("glPushAttrib",push,r_push);H("glPopAttrib",pop,r_pop);
    H("glGetIntegerv",geti,r_geti);H("glGetFloatv",getf,r_getf);
    H("glGetDoublev",getd,r_getd);H("glGetBooleanv",getb,r_getb);
#undef H
}

} // extern "C"
