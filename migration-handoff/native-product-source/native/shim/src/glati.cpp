/* Replayable ATI_fragment_shader definitions. Native pointers and the driver's
 * installed-definition cache remain in core; all definition data is captured.
 * Signatures/compile semantics: Khronos ATI_fragment_shader, revision1.8. */
#include "shim.hpp"
#include "glati.hpp"

/* Fixed C hook ABI and assembly-referenced symbol names. */
extern "C" {
static u8 installed[GATI_CAP][32];
static u8 installed_live[GATI_CAP];
static u32 real_context; /* native context whose ATI baseline/cache is installed */
static void die(const char *reason) {
    slog("ATI definition boundary: %s",reason);TerminateProcess(GetCurrentProcess(),6);for(;;){}
}
static void (WINAPI *r_BindFragmentShaderATI)(u32);
static void (WINAPI *r_DeleteFragmentShaderATI)(u32);
static void (WINAPI *r_BeginFragmentShaderATI)(void);
static void (WINAPI *r_EndFragmentShaderATI)(void);
static void (WINAPI *r_SetFragmentShaderConstantATI)(u32,const float *);
static void *(WINAPI *r_proc)(const char *);
static u32 (WINAPI *r_GetError)(void);
static void context_check(void) {
    if(!SH->wh.current_gl)die("shader operation requires a tracked current context");
    if(SH->glati.context && SH->glati.context!=SH->wh.current_gl)
        die("multiple ATI contexts are not captured");
}
static void refresh_context_procs(void);
static void own_context(void) {
    context_check();
    if(real_context!=SH->wh.current_gl) {
        refresh_context_procs();
        if(!SH->wh.current_gl)die("shader baseline requires a tracked current context");
        /* The portable profile defines this otherwise unqueryable state.
         * Native profile bypasses this module unchanged.
         * The API has no getter for initial global constants. Establish
         * the captured model's zero baseline before the first game use. */
        void *set=r_proc?r_proc("glSetFragmentShaderConstantATI"):NULL;
        if((uintptr_t)set<=3 || (uintptr_t)set==0xffffffffu)die("shader constant API unavailable");
        r_SetFragmentShaderConstantATI=reinterpret_cast<decltype(r_SetFragmentShaderConstantATI)>(set);
        r_GetError=reinterpret_cast<decltype(r_GetError)>(GetProcAddress(GetModuleHandleA("opengl32.dll"),"glGetError"));
        if(!r_GetError)die("shader error API unavailable");
        math_host_scope_t fp;int local_scope=!glenv_active();
        if(local_scope)math_scope_begin(&fp,MATH_HOST_FP | MATH_HOST_ID);
        u32 pending=r_GetError();if(pending&&!SH->gl_err)SH->gl_err=pending;
        static const float zero[4]={0,0,0,0};
        for(u32 c=0;c<8;c++)r_SetFragmentShaderConstantATI(0x8941u+c,zero);
        u32 error=r_GetError();if(local_scope)math_scope_end(&fp);
        if(error)die("shader constant baseline initialization failed");
        real_context=SH->wh.current_gl;
        memset(installed,0,sizeof installed);memset(installed_live,0,sizeof installed_live);
    }
    SH->glati.context=SH->wh.current_gl;
}
static glati_object_t *object(u32 v) {
    if(v>=GATI_CAP)die("shader name outside captured namespace");
    return &SH->glati.object[v];
}
static void append(u32 op,const u32 *args,u32 n) {
    if(!SH->glati.defining)return; /* The driver owns invalid-call errors. */
    glati_object_t *o=object(SH->glati.defining_id);
    if(o->count>=GATI_COMMAND_LIMIT)die("shader instruction recording limit exceeded");
    if(o->count==o->capacity) {
        u32 cap=o->capacity?o->capacity*2:64;
        glati_command_t *p=static_cast<glati_command_t *>(kst_valloc(cap*sizeof *p));
        if(!p)die("shader instruction allocation failed");
        if(o->count)memcpy(p,o->commands,o->count*sizeof *p);
        if(o->commands&&!kst_vfree(o->commands))die("shader instruction release failed");
        o->commands=p;o->capacity=cap;
    }
    glati_command_t *c=&o->commands[o->count++];memset(c,0,sizeof *c);c->op=op;
    if(n>13)die("invalid internal opcode argument count");
    memcpy(c->args,args,n*4);
}
static void WINAPI w_BindFragmentShaderATI(u32 v) { own_context();
    if(SH->glati.defining){r_BindFragmentShaderATI(v);return;}
    glati_object_t *o=object(v);
    if(!o->live && v) {
        if(!r_DeleteFragmentShaderATI)r_DeleteFragmentShaderATI=r_proc?reinterpret_cast<decltype(r_DeleteFragmentShaderATI)>(r_proc("glDeleteFragmentShaderATI")):nullptr;
        if((uintptr_t)r_DeleteFragmentShaderATI<=3 || (uintptr_t)r_DeleteFragmentShaderATI==0xffffffffu)die("shader delete API unavailable");
        r_DeleteFragmentShaderATI(v);installed_live[v]=0;
    }
    o->live=1;
    if(!o->owner)o->owner=SH->wh.current_gl;
    glsync_ati_bind(v);r_BindFragmentShaderATI(v);
}
static void WINAPI w_BeginFragmentShaderATI(void) { own_context();
    if(SH->glati.defining){r_BeginFragmentShaderATI();return;}
    u32 v=SH->ati_bound;glati_object_t *o=object(v);
    o->live=1;o->complete=0;o->count=0;o->owner=SH->wh.current_gl;
    memset(o->key,0,32);SH->glati.defining=1;SH->glati.defining_id=v;
    glsync_ati_begin();r_BeginFragmentShaderATI();
}
static void WINAPI w_EndFragmentShaderATI(void) { own_context();
    r_EndFragmentShaderATI();
    if(!SH->glati.defining)return;
    glati_object_t *o=object(SH->glati.defining_id);
    sha256(o->commands,o->count*sizeof *o->commands,o->key);o->complete=1;
    memcpy(installed[SH->glati.defining_id],o->key,32);installed_live[SH->glati.defining_id]=1;
    SH->glati.defining=0;glsync_ati_end();
}
static void WINAPI w_SetFragmentShaderConstantATI(u32 dst,const float *value) { own_context();
    if(SH->glati.defining) {
        u32 a[5]={dst};if(!value)die("null shader constant operands");memcpy(a+1,value,16);append(9,a,5);
    } else {
        u32 c=dst-0x8941u;
        if(c<8 && value){memcpy(SH->glati.constants[c],value,16);SH->glati.constant_mask|=1u<<c;}
        glsync_ati_constant(dst,value);
    }
    r_SetFragmentShaderConstantATI(dst,value);
}
static u32 WINAPI w_GenFragmentShadersATI(u32 range) { own_context();
    if(!range){if(!SH->gl_err)SH->gl_err=0x0501;return 0;}
    if(SH->glati.defining){if(!SH->gl_err)SH->gl_err=0x0502;return 0;}
    u32 first=SH->gl_ati_next;
    if(!first || first>=GATI_CAP)first=1;
    u32 wrapped=first==1;
    for(;;) {
        if(first>=GATI_CAP || range>GATI_CAP-first) {
            if(wrapped)die("shader namespace exhausted");
            first=1;wrapped=1;continue;
        }
        u32 i;for(i=0;i<range;i++)if(object(first+i)->live)break;
        if(i==range)break;
        first+=i+1;
    }
    if(!r_DeleteFragmentShaderATI) {
        r_DeleteFragmentShaderATI=r_proc?reinterpret_cast<decltype(r_DeleteFragmentShaderATI)>(r_proc("glDeleteFragmentShaderATI")):nullptr;
        if((uintptr_t)r_DeleteFragmentShaderATI<=3||(uintptr_t)r_DeleteFragmentShaderATI==0xffffffffu)die("shader delete API unavailable");
    }
    for(u32 v=first;v<first+range;v++) {
        glati_object_t *o=object(v);
        if(o->commands&&!kst_vfree(o->commands))die("shader backing release failed");
        memset(o,0,sizeof *o);o->live=1;o->owner=SH->wh.current_gl;
        r_DeleteFragmentShaderATI(v);installed_live[v]=0;
    }
    SH->gl_ati_next=first+range;return first;
}
static void WINAPI w_DeleteFragmentShaderATI(u32 v) { own_context();
    if(!v || v>=GATI_CAP){r_DeleteFragmentShaderATI(v);return;}
    if(SH->glati.defining){r_DeleteFragmentShaderATI(v);return;}
    r_DeleteFragmentShaderATI(v);glati_object_t *o=object(v);
    if(o->commands&&!kst_vfree(o->commands))die("shader backing release failed");
    memset(o,0,sizeof *o);installed_live[v]=0;memset(installed[v],0,32);
    if(!SH->gl_ati_next || v<SH->gl_ati_next)SH->gl_ati_next=v;
    if(v<ATI_MAX){SH->ati_const_set[v]=0;memset(SH->ati_const[v],0,sizeof SH->ati_const[v]);}
}

static void (WINAPI *r_PassTexCoordATI)(u32 a0,u32 a1,u32 a2);
static void WINAPI w_PassTexCoordATI(u32 a0,u32 a1,u32 a2) { own_context(); u32 a[]={a0,a1,a2};append(1,a,3);r_PassTexCoordATI(a0,a1,a2); }

static void (WINAPI *r_SampleMapATI)(u32 a0,u32 a1,u32 a2);
static void WINAPI w_SampleMapATI(u32 a0,u32 a1,u32 a2) { own_context(); u32 a[]={a0,a1,a2};append(2,a,3);r_SampleMapATI(a0,a1,a2); }

static void (WINAPI *r_ColorFragmentOp1ATI)(u32 a0,u32 a1,u32 a2,u32 a3,u32 a4,u32 a5,u32 a6);
static void WINAPI w_ColorFragmentOp1ATI(u32 a0,u32 a1,u32 a2,u32 a3,u32 a4,u32 a5,u32 a6) { own_context(); u32 a[]={a0,a1,a2,a3,a4,a5,a6};append(3,a,7);r_ColorFragmentOp1ATI(a0,a1,a2,a3,a4,a5,a6); }

static void (WINAPI *r_ColorFragmentOp2ATI)(u32 a0,u32 a1,u32 a2,u32 a3,u32 a4,u32 a5,u32 a6,u32 a7,u32 a8,u32 a9);
static void WINAPI w_ColorFragmentOp2ATI(u32 a0,u32 a1,u32 a2,u32 a3,u32 a4,u32 a5,u32 a6,u32 a7,u32 a8,u32 a9) { own_context(); u32 a[]={a0,a1,a2,a3,a4,a5,a6,a7,a8,a9};append(4,a,10);r_ColorFragmentOp2ATI(a0,a1,a2,a3,a4,a5,a6,a7,a8,a9); }

static void (WINAPI *r_ColorFragmentOp3ATI)(u32 a0,u32 a1,u32 a2,u32 a3,u32 a4,u32 a5,u32 a6,u32 a7,u32 a8,u32 a9,u32 a10,u32 a11,u32 a12);
static void WINAPI w_ColorFragmentOp3ATI(u32 a0,u32 a1,u32 a2,u32 a3,u32 a4,u32 a5,u32 a6,u32 a7,u32 a8,u32 a9,u32 a10,u32 a11,u32 a12) { own_context(); u32 a[]={a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10,a11,a12};append(5,a,13);r_ColorFragmentOp3ATI(a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10,a11,a12); }

static void (WINAPI *r_AlphaFragmentOp1ATI)(u32 a0,u32 a1,u32 a2,u32 a3,u32 a4,u32 a5);
static void WINAPI w_AlphaFragmentOp1ATI(u32 a0,u32 a1,u32 a2,u32 a3,u32 a4,u32 a5) { own_context(); u32 a[]={a0,a1,a2,a3,a4,a5};append(6,a,6);r_AlphaFragmentOp1ATI(a0,a1,a2,a3,a4,a5); }

static void (WINAPI *r_AlphaFragmentOp2ATI)(u32 a0,u32 a1,u32 a2,u32 a3,u32 a4,u32 a5,u32 a6,u32 a7,u32 a8);
static void WINAPI w_AlphaFragmentOp2ATI(u32 a0,u32 a1,u32 a2,u32 a3,u32 a4,u32 a5,u32 a6,u32 a7,u32 a8) { own_context(); u32 a[]={a0,a1,a2,a3,a4,a5,a6,a7,a8};append(7,a,9);r_AlphaFragmentOp2ATI(a0,a1,a2,a3,a4,a5,a6,a7,a8); }

static void (WINAPI *r_AlphaFragmentOp3ATI)(u32 a0,u32 a1,u32 a2,u32 a3,u32 a4,u32 a5,u32 a6,u32 a7,u32 a8,u32 a9,u32 a10,u32 a11);
static void WINAPI w_AlphaFragmentOp3ATI(u32 a0,u32 a1,u32 a2,u32 a3,u32 a4,u32 a5,u32 a6,u32 a7,u32 a8,u32 a9,u32 a10,u32 a11) { own_context(); u32 a[]={a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10,a11};append(8,a,12);r_AlphaFragmentOp3ATI(a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10,a11); }

typedef struct {const char *name;void **real;void *wrapper;} api_t;
#define API(n) {"gl"#n,(void **)&r_##n,(void *)w_##n}
static void *r_gen;
static api_t api[]={
    {"glGenFragmentShadersATI",&r_gen,(void *)w_GenFragmentShadersATI},
    API(BindFragmentShaderATI),API(DeleteFragmentShaderATI),API(BeginFragmentShaderATI),API(EndFragmentShaderATI),
    API(SetFragmentShaderConstantATI),
    API(PassTexCoordATI),
    API(SampleMapATI),
    API(ColorFragmentOp1ATI),
    API(ColorFragmentOp2ATI),
    API(ColorFragmentOp3ATI),
    API(AlphaFragmentOp1ATI),
    API(AlphaFragmentOp2ATI),
    API(AlphaFragmentOp3ATI),
};
static void refresh_context_procs(void) {
    /* A pointer fetched during address-only setup in another WGL context is
     * not evidence that it is callable in the newly owned context. */
    for(u32 i=0;i<sizeof api/sizeof api[0];i++)if(SH->glati.used&(1u<<i)) {
        void *p=r_proc?r_proc(api[i].name):NULL;
        if((uintptr_t)p<=3 || (uintptr_t)p==0xffffffffu)die("shader API unavailable in current context");
        *api[i].real=p;
    }
}
void *glati_proc(const char *name,void *target) {
    if(g_bnd_native)return NULL;
    for(u32 i=0;i<sizeof api/sizeof api[0];i++)if(k_streq(name,api[i].name)) {
        if(!target || (uintptr_t)target<=3 || (uintptr_t)target==0xffffffffu)return NULL;
        context_check();
        *api[i].real=target;SH->glati.used|=1u<<i;
        r_proc=reinterpret_cast<decltype(r_proc)>(GetProcAddress(GetModuleHandleA("opengl32.dll"),"wglGetProcAddress"));
        return api[i].wrapper;
    }
    return NULL;
}
static int commands_valid(const glati_object_t *o) {
    if(o->count>o->capacity || o->capacity>GATI_COMMAND_LIMIT || (!o->commands&&o->count))return 0;
    if(o->count) {
        if(!kst_in_arena(o->commands))return 0;
        uintptr_t p=(uintptr_t)o->commands,end=p+o->count*sizeof *o->commands;
        if(end<p)return 0;
        while(p<end) {
            MEMORY_BASIC_INFORMATION m;
            if(!VirtualQuery((void *)p,&m,sizeof m)||m.State!=MEM_COMMIT||(m.Protect&(PAGE_GUARD|PAGE_NOACCESS)))return 0;
            uintptr_t next=(uintptr_t)m.BaseAddress+m.RegionSize;
            if(next<=p)return 0;
            p=next;
        }
        for(u32 i=0;i<o->count;i++)if(o->commands[i].op<1||o->commands[i].op>9)return 0;
    }
    u8 key[32];sha256(o->commands,o->count*sizeof *o->commands,key);return !memcmp(key,o->key,32);
}
int glati_capture_ready(void) {
    if(SH->glati.defining || (SH->glati.context && SH->glati.context!=SH->wh.current_gl))return 0;
    for(u32 v=0;v<GATI_CAP;v++)if(SH->glati.object[v].live && SH->glati.object[v].complete && !commands_valid(&SH->glati.object[v]))return 0;
    return 1;
}
int glati_restorable(void) {
    if(!glati_capture_ready() || SH->gl_ati_next>GATI_CAP)return 0;
    if(!SH->glati.used && SH->gl_ati_next>1)return 0;
    for(u32 v=0;v<GATI_CAP;v++)if(SH->glati.object[v].live && SH->glati.object[v].owner &&
        SH->glati.object[v].owner!=SH->wh.current_gl)return 0;
    return 1;
}
void glati_real_context_reset(void){real_context=0;memset(installed,0,sizeof installed);memset(installed_live,0,sizeof installed_live);}
static void replay_command(const glati_command_t *c) {
    const u32 *a=c->args;
    switch(c->op){
    case 1:r_PassTexCoordATI(a[0],a[1],a[2]);break;
    case 2:r_SampleMapATI(a[0],a[1],a[2]);break;
    case 3:r_ColorFragmentOp1ATI(a[0],a[1],a[2],a[3],a[4],a[5],a[6]);break;
    case 4:r_ColorFragmentOp2ATI(a[0],a[1],a[2],a[3],a[4],a[5],a[6],a[7],a[8],a[9]);break;
    case 5:r_ColorFragmentOp3ATI(a[0],a[1],a[2],a[3],a[4],a[5],a[6],a[7],a[8],a[9],a[10],a[11],a[12]);break;
    case 6:r_AlphaFragmentOp1ATI(a[0],a[1],a[2],a[3],a[4],a[5]);break;
    case 7:r_AlphaFragmentOp2ATI(a[0],a[1],a[2],a[3],a[4],a[5],a[6],a[7],a[8]);break;
    case 8:r_AlphaFragmentOp3ATI(a[0],a[1],a[2],a[3],a[4],a[5],a[6],a[7],a[8],a[9],a[10],a[11]);break;
    case 9:r_SetFragmentShaderConstantATI(a[0],(const float *)(a+1));break;
    }
}
int glati_restore(void) {
    if(!glati_restorable())return 0;
    if(!SH->glati.used && !real_context)return SH->gl_ati_next<=1;
    r_proc=reinterpret_cast<decltype(r_proc)>(GetProcAddress(GetModuleHandleA("opengl32.dll"),"wglGetProcAddress"));
    if(!r_proc)return 0;
    for(u32 i=0;i<sizeof api/sizeof api[0];i++) {
        void *p=r_proc(api[i].name);
        if((uintptr_t)p<=3||(uintptr_t)p==0xffffffffu)return 0;
        *api[i].real=p;
    }
    r_GetError=reinterpret_cast<decltype(r_GetError)>(GetProcAddress(GetModuleHandleA("opengl32.dll"),"glGetError"));
    if(!r_GetError)return 0;
    u32 pending=r_GetError();
    if(pending){slog("ATI reconstruction entered with native GL error %x",pending);return 0;}
    real_context=SH->wh.current_gl;
    u32 saved=SH->ati_bound;
    for(u32 v=0;v<GATI_CAP;v++) {
        glati_object_t *o=object(v);
        if(!o->live){if(v)r_DeleteFragmentShaderATI(v);installed_live[v]=0;memset(installed[v],0,32);continue;}
        if(o->owner && o->owner!=SH->wh.current_gl)return 0; /* No false cross-context acceptance. */
        if(!o->complete) {
            /* Generated but never defined: preserve an empty name. */
            if(v)r_DeleteFragmentShaderATI(v);
            r_BindFragmentShaderATI(v);installed_live[v]=0;
            continue;
        }
        if(!commands_valid(o))return 0;
        r_BindFragmentShaderATI(v);
        if(!installed_live[v]||memcmp(installed[v],o->key,32)) {
            r_BeginFragmentShaderATI();for(u32 c=0;c<o->count;c++)replay_command(&o->commands[c]);r_EndFragmentShaderATI();
            u32 error=r_GetError();
            if(error){slog("ATI shader %u replay error %x",v,error);installed_live[v]=0;return 0;}
            memcpy(installed[v],o->key,32);installed_live[v]=1;
        }

    }
    /* Unset global constants have the established zero baseline. Replaying only
     * set bits leaks abandoned-future constants when retaining a context. */
    static const float zero[4]={0,0,0,0};
    for(u32 c=0;c<8;c++)r_SetFragmentShaderConstantATI(0x8941u+c,
        SH->glati.constant_mask&(1u<<c)?SH->glati.constants[c]:zero);
    r_BindFragmentShaderATI(saved);return r_GetError()==0;
}

} /* extern "C" */
