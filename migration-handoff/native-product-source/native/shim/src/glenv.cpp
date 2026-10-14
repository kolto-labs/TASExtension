/* Production game->GL environment boundary. Host state stays in core. No
 * diagnostic probes, status-bit clearing, or tick-level normalization. */
#include "shim.hpp"
#include "glenv.hpp"

/* Fixed C hook ABI and assembly-referenced symbol names. */
extern "C" {
#define GE_NAMES 512u
#define GE_THREADS 32u
#define GE_DEPTH 128u
/* FXSAVE image: 512 bytes, XMM0 at byte 160. Bytes 0-159 are the x87
 * environment, MXCSR and ST0-7, which the boundary restores for the game. */
#define FXSAVE_SIZE 512
#define FXSAVE_XMM0_OFF 160
static const char *const supported[]={
    "glActiveStencilFaceEXT", /* void */
    "glActiveTextureARB", /* void */
    "glAlphaFragmentOp1ATI", /* void */
    "glAlphaFragmentOp2ATI", /* void */
    "glAlphaFragmentOp3ATI", /* void */
    "glAlphaFunc", /* void */
    "glBegin", /* void */
    "glBeginFragmentShaderATI", /* void */
    "glBindBufferARB", /* void */
    "glBindFragmentShaderATI", /* void */
    "glBindProgramARB", /* void */
    "glBindTexture", /* void */
    "glBitmap", /* void */
    "glBlendFunc", /* void */
    "glBufferDataARB", /* void */
    "glBufferSubDataARB", /* void */
    "glCallLists", /* void */
    "glClear", /* void */
    "glClearColor", /* void */
    "glClearStencil", /* void */
    "glClientActiveTextureARB", /* void */
    "glColor3f", /* void */
    "glColor4f", /* void */
    "glColor4fv", /* void */
    "glColor4ubv", /* void */
    "glColorFragmentOp1ATI", /* void */
    "glColorFragmentOp2ATI", /* void */
    "glColorFragmentOp3ATI", /* void */
    "glColorMask", /* void */
    "glColorMaterial", /* void */
    "glColorPointer", /* void */
    "glCompressedTexImage2DARB", /* void */
    "glCopyTexImage2D", /* void */
    "glCopyTexSubImage2D", /* void */
    "glDeleteBuffersARB", /* void */
    "glDeleteFragmentShaderATI", /* void */
    "glDeleteLists", /* void */
    "glDeleteProgramsARB", /* void */
    "glDeleteTextures", /* void */
    "glDepthFunc", /* void */
    "glDepthMask", /* void */
    "glDisable", /* void */
    "glDisableClientState", /* void */
    "glDisableVertexAttribArrayARB", /* void */
    "glDrawArrays", /* void */
    "glDrawBuffer", /* void */
    "glDrawElements", /* void */
    "glEnable", /* void */
    "glEnableClientState", /* void */
    "glEnableVertexAttribArrayARB", /* void */
    "glEnd", /* void */
    "glEndFragmentShaderATI", /* void */
    "glEndList", /* void */
    "glFinish", /* void */
    "glFlush", /* void */
    "glFogCoordPointerEXT", /* void */
    "glFogCoordfEXT", /* void */
    "glFogf", /* void */
    "glFogfv", /* void */
    "glFogi", /* void */
    "glFrontFace", /* void */
    "glGenBuffersARB", /* void */
    "glGenFragmentShadersATI", /* GLuint */
    "glGenLists", /* GLuint */
    "glGenProgramsARB", /* void */
    "glGenTextures", /* void */
    "glGetBufferParameterivARB", /* void */
    "glGetBufferPointervARB", /* void */
    "glGetBufferSubDataARB", /* void */
    "glGetCompressedTexImageARB", /* void */
    "glGetError", /* GLenum */
    "glGetFloatv", /* void */
    "glGetIntegerv", /* void */
    "glGetProgramEnvParameterdvARB", /* void */
    "glGetProgramEnvParameterfvARB", /* void */
    "glGetProgramLocalParameterdvARB", /* void */
    "glGetProgramLocalParameterfvARB", /* void */
    "glGetProgramStringARB", /* void */
    "glGetProgramivARB", /* void */
    "glGetString", /* const GLubyte * */
    "glGetVertexAttribPointervARB", /* void */
    "glGetVertexAttribdvARB", /* void */
    "glGetVertexAttribfvARB", /* void */
    "glGetVertexAttribivARB", /* void */
    "glIsBufferARB", /* GLboolean */
    "glIsEnabled", /* GLboolean */
    "glIsProgramARB", /* GLboolean */
    "glIsTexture", /* GLboolean */
    "glLightModelfv", /* void */
    "glLightfv", /* void */
    "glLineWidth", /* void */
    "glListBase", /* void */
    "glLoadIdentity", /* void */
    "glLockArraysEXT", /* void */
    "glMapBufferARB", /* void * */
    "glMaterialfv", /* void */
    "glMatrixMode", /* void */
    "glMultMatrixf", /* void */
    "glMultiTexCoord2fARB", /* void */
    "glMultiTexCoord2fvARB", /* void */
    "glMultiTexCoord2iARB", /* void */
    "glMultiTexCoord3fARB", /* void */
    "glMultiTexCoord3fvARB", /* void */
    "glMultiTexCoord4fvARB", /* void */
    "glNewList", /* void */
    "glNormal3f", /* void */
    "glNormal3fv", /* void */
    "glNormalPointer", /* void */
    "glOrtho", /* void */
    "glPassTexCoordATI", /* void */
    "glPixelStorei", /* void */
    "glPointSize", /* void */
    "glPolygonMode", /* void */
    "glPopAttrib", /* void */
    "glPopClientAttrib", /* void */
    "glPopMatrix", /* void */
    "glProgramEnvParameter4dARB", /* void */
    "glProgramEnvParameter4dvARB", /* void */
    "glProgramEnvParameter4fARB", /* void */
    "glProgramEnvParameter4fvARB", /* void */
    "glProgramLocalParameter4dARB", /* void */
    "glProgramLocalParameter4dvARB", /* void */
    "glProgramLocalParameter4fARB", /* void */
    "glProgramLocalParameter4fvARB", /* void */
    "glProgramStringARB", /* void */
    "glPushAttrib", /* void */
    "glPushClientAttrib", /* void */
    "glPushMatrix", /* void */
    "glRasterPos2f", /* void */
    "glReadPixels", /* void */
    "glRotatef", /* void */
    "glSampleMapATI", /* void */
    "glScalef", /* void */
    "glSecondaryColor3fvEXT", /* void */
    "glSetFragmentShaderConstantATI", /* void */
    "glStencilFunc", /* void */
    "glStencilMask", /* void */
    "glStencilOp", /* void */
    "glTexCoord2f", /* void */
    "glTexCoordPointer", /* void */
    "glTexEnvi", /* void */
    "glTexGeni", /* void */
    "glTexImage1D", /* void */
    "glTexImage2D", /* void */
    "glTexParameteri", /* void */
    "glTexSubImage2D", /* void */
    "glTranslatef", /* void */
    "glUnlockArraysEXT", /* void */
    "glUnmapBufferARB", /* GLboolean */
    "glVertex3f", /* void */
    "glVertex3fv", /* void */
    "glVertex4f", /* void */
    "glVertexAttrib1dARB", /* void */
    "glVertexAttrib1dvARB", /* void */
    "glVertexAttrib1fARB", /* void */
    "glVertexAttrib1fvARB", /* void */
    "glVertexAttrib1sARB", /* void */
    "glVertexAttrib1svARB", /* void */
    "glVertexAttrib2dARB", /* void */
    "glVertexAttrib2dvARB", /* void */
    "glVertexAttrib2fARB", /* void */
    "glVertexAttrib2fvARB", /* void */
    "glVertexAttrib2sARB", /* void */
    "glVertexAttrib2svARB", /* void */
    "glVertexAttrib3dARB", /* void */
    "glVertexAttrib3dvARB", /* void */
    "glVertexAttrib3fARB", /* void */
    "glVertexAttrib3fvARB", /* void */
    "glVertexAttrib3sARB", /* void */
    "glVertexAttrib3svARB", /* void */
    "glVertexAttrib4NbvARB", /* void */
    "glVertexAttrib4NivARB", /* void */
    "glVertexAttrib4NsvARB", /* void */
    "glVertexAttrib4NubARB", /* void */
    "glVertexAttrib4NubvARB", /* void */
    "glVertexAttrib4NuivARB", /* void */
    "glVertexAttrib4NusvARB", /* void */
    "glVertexAttrib4bvARB", /* void */
    "glVertexAttrib4dARB", /* void */
    "glVertexAttrib4dvARB", /* void */
    "glVertexAttrib4fARB", /* void */
    "glVertexAttrib4fvARB", /* void */
    "glVertexAttrib4ivARB", /* void */
    "glVertexAttrib4sARB", /* void */
    "glVertexAttrib4svARB", /* void */
    "glVertexAttrib4ubvARB", /* void */
    "glVertexAttrib4uivARB", /* void */
    "glVertexAttrib4usvARB", /* void */
    "glVertexAttribPointerARB", /* void */
    "glVertexPointer", /* void */
    "glViewport", /* void */
    "gluBuild2DMipmaps", /* GLint */
    "gluErrorString", /* const GLubyte * */
    "gluPerspective", /* void */
};
int glenv_supported(const char *name) {
    for(u32 i=0;i<sizeof supported/sizeof supported[0];i++)if(k_streq(name,supported[i]))return 1;
    return 0;
}
typedef struct {u32 target;char name[96];} ge_name_t;
typedef struct {math_host_scope_t scope;u32 caller;} ge_call_t;
typedef struct {u32 tid,slot,depth;ge_call_t calls[GE_DEPTH];} ge_thread_t;
static ge_name_t ge_names[GE_NAMES];
static u32 ge_nnames,ge_executable;
static u8 ge_code[GE_NAMES][16] __attribute__((aligned(4096)));
static ge_thread_t ge_threads[GE_THREADS];
/* The fast thunks fxsave/fxrstor straight into a record: keep every fp 16-aligned. */
static_assert(sizeof(((math_host_scope_t *)0)->fp)==FXSAVE_SIZE,"scope fp must hold one FXSAVE image");
static_assert(offsetof(math_host_scope_t,fp)%16==0 && offsetof(ge_call_t,scope)%16==0 &&
    sizeof(ge_call_t)%16==0 && offsetof(ge_thread_t,calls)%16==0 && alignof(ge_thread_t)%16==0,
    "GL boundary FXSAVE area must be 16-byte aligned");
static volatile LONG ge_lock;
/* One past the highest entry index ever claimed. Written only under ge_lock and
 * only upward; entries at or above it were never claimed, so their tid is 0. */
static volatile u32 ge_high;
static void ge_die(const char *why) {
    slog("GL environment boundary: %s",why);TerminateProcess(GetCurrentProcess(),6);for(;;){}
}
static void ge_acquire(void){while(InterlockedCompareExchange(&ge_lock,1,0)){} }
static void ge_release(void){InterlockedExchange(&ge_lock,0);}
/* Profiling: the lock was taken three times per GL call, and since entries are
 * freed at depth 0, every outermost call also missed on all 32 entries (each
 * on its own page) before claiming. An entry holding this thread's tid is
 * claimed and freed only by this thread (others write only their own tid into
 * free entries), so finding our own entry needs no lock: aligned word reads,
 * and no other thread ever stores our tid. The match scan stops at ge_high:
 * any entry this thread claimed raised ge_high first, in this thread's program
 * order; entries above it hold tid 0, which no thread id equals. Claiming a
 * free entry still takes the lock so two threads cannot claim the same one. */
static ge_thread_t *ge_thread(u32 tid,u32 slot,int create) {
    const u32 high=ge_high;
    for(u32 i=0;i<high;i++) {
        volatile ge_thread_t *e=&ge_threads[i];
        if(e->tid==tid && e->slot==slot)return &ge_threads[i];
    }
    if(create) {
        ge_acquire();
        for(u32 i=0;i<GE_THREADS;i++)if(!ge_threads[i].tid) {
            ge_threads[i].slot=slot;ge_threads[i].tid=tid;
            if(i>=ge_high)ge_high=i+1;
            ge_release();return &ge_threads[i];
        }
        ge_release();
    }
    ge_die("missing or exhausted active thread/slot stack");return NULL;
}
/* Profiling: kernel32 GetLastError/SetLastError (RtlSetLastWin32Error) per GL
 * call. In Wine i386 both are just TEB LastErrorValue (teb+0x34) and the
 * thread id is TEB ClientId.UniqueThread (teb+0x24, see cur_rtid); fs is the
 * TEB. "memory" keeps the save/restore ordered around the shim's own calls. */
static inline DWORD ge_get_error(void){DWORD v;__asm__ volatile("movl %%fs:0x34,%0":"=r"(v)::"memory");return v;}
static inline void ge_set_error(DWORD v){__asm__ volatile("movl %0,%%fs:0x34"::"r"(v):"memory");}
void glenv_return_asm(void);
void __attribute__((used,noinline)) glenv_enter_c(u32 *f,const u8 *fx) {
    DWORD error=ge_get_error();
    ge_thread_t *t=ge_thread(cur_rtid(),K?K->cur:0,1);
    u32 name=f[9];if(name>=ge_nnames || t->depth==GE_DEPTH)ge_die("invalid thunk or nesting exhausted");
    ge_call_t *c=&t->calls[t->depth++];c->caller=f[10];
    /* C/TLS helpers may change FP/ID before scope_begin saves it. The assembly
     * entry snapshot is authoritative; scope remains linked for callbacks.
     * Begin without MATH_HOST_FP: its fxsave was fully overwritten by the
     * 512-byte copy below (fxsave leaves FP state unchanged), so skip it. Nothing
     * on this thread reads scope.fp or mask before the copy completes. */
    math_scope_begin(&c->scope,MATH_HOST_ID);
    c->scope.mask|=MATH_HOST_FP;
    memcpy(c->scope.fp,fx,FXSAVE_SIZE);c->scope.id=f[8]&EFLAGS_ID;
    f[9]=ge_names[name].target;f[10]=(u32)(uintptr_t)glenv_return_asm;
    ge_set_error(error);
}
void __attribute__((used,noinline)) glenv_return_c(u32 *f,u8 *fx) {
    DWORD error=ge_get_error();
    ge_thread_t *t=ge_thread(cur_rtid(),K?K->cur:0,0);
    if(!t->depth)ge_die("return without entry");
    ge_call_t *c=&t->calls[t->depth-1];
    math_scope_end(&c->scope);
    /* Catalog excludes FP returns: restoring x87 stack and environment is
     * correct. Keep actual XMM register outputs (offset160 onward), and the
     * saved pushad frame retains EAX/EDX and all integer ABI registers. */
    memcpy(fx,c->scope.fp,FXSAVE_XMM0_OFF);
    f[8]=(f[8]&~EFLAGS_ID)|c->scope.id;f[9]=c->caller;
    /* Freeing needs no lock: only this thread writes this entry while its tid
     * is ours. All reads of c and the depth store come first (compiler barrier;
     * x86 keeps store order), so a claimer that sees tid 0 also sees depth 0. */
    if(!--t->depth){__asm__ volatile("":::"memory");*(volatile u32 *)&t->tid=0;}
    ge_set_error(error);
}
/* Fast path (profiling: the boundary was ~22% of main-thread samples, against
 * 6% for glDrawElements itself). Taken when math_scope_fast_slot says the
 * canonical body would reach no kernel32 TLS call and no btrace point; else
 * these return NULL before any side effect and the thunk runs that body.
 * Fast and slow halves may mix within one call (the first call can allocate
 * the TLS index on the slow path and return on the fast one): both build and
 * consume the same record, link the same TLS slot, and leave the same state.
 * Record contents match glenv_enter_c/glenv_return_c in every byte fxsave
 * writes and fxrstor reads (fxsave leaves 464-511, and in 32-bit mode may
 * leave 288-463, holding their prior contents; nothing reads them). What goes:
 *  - the 512-byte copy and the trailing fxrstor: the thunk fxsaves the entry
 *    state straight into the record after this returns. This code and
 *    everything it calls must be integer-only: no x87, MMX, MXCSR access or
 *    SSE arithmetic (build.sh refuses the link otherwise). XMM clobbers are
 *    allowed, because GE_XLOAD reloads XMM0-7 before the fxsave. So the state
 *    the thunk saves, and then hands the API unrestored, is the entry state
 *    bit for bit;
 *  - read_id's pushf (its value was overwritten by f[8]'s ID bit);
 *  - the LastError save/restore: nothing here calls an API (ge_die exits). */
u8 *__attribute__((used,noinline)) glenv_enter_fast_c(u32 *f) {
    const u32 slot=math_scope_fast_slot();
    if(slot==MATH_NO_FAST_SLOT)return NULL;
    ge_thread_t *t=ge_thread(cur_rtid(),K?K->cur:0,1);
    u32 name=f[9];if(name>=ge_nnames || t->depth==GE_DEPTH)ge_die("invalid thunk or nesting exhausted");
    ge_call_t *c=&t->calls[t->depth++];c->caller=f[10];
    math_scope_link(&c->scope,slot,MATH_HOST_FP|MATH_HOST_ID,f[8]&EFLAGS_ID);
    f[9]=ge_names[name].target;f[10]=(u32)(uintptr_t)glenv_return_asm;
    return c->scope.fp;
}
/* What goes on return:
 *  - math_scope_end's fxrstor and put_id (pushf/popf): the thunk's final
 *    fxrstor and popfl overwrote both;
 *  - the fxsave and 160-byte copy: the thunk fxrstors the record itself
 *    (bytes 0-159: x87 environment, MXCSR, ST0-7, as the copy delivered) and
 *    then reloads XMM0-7 as they were at the thunk's entry (bytes 160-287 in
 *    the old frame). Bytes 288-511 are not loaded by fxrstor in 32-bit mode;
 *  - the LastError save/restore, as on entry.
 * The entry is freed by the thunk after its fxrstor has read the record;
 * free_tid gets the tid word to clear, or 0 while calls remain on this thread. */
u8 *__attribute__((used,noinline)) glenv_return_fast_c(u32 *f,u32 *free_tid) {
    const u32 slot=math_scope_fast_slot();
    if(slot==MATH_NO_FAST_SLOT)return NULL;
    ge_thread_t *t=ge_thread(cur_rtid(),K?K->cur:0,0);
    if(!t->depth)ge_die("return without entry");
    ge_call_t *c=&t->calls[t->depth-1];
    math_scope_unlink(&c->scope,slot);
    f[8]=(f[8]&~EFLAGS_ID)|c->scope.id;f[9]=c->caller;
    *free_tid=--t->depth?0:(u32)(uintptr_t)&t->tid;
    return c->scope.fp;
}
#ifdef GLENV_ELF
#define GE_CSYM(s) s
#else
#define GE_CSYM(s) "_" s
#endif
#define GE_STR_(x) #x
#define GE_STR(x) GE_STR_(x)
/* Stack areas: GE_FX_FRAME is one FXSAVE image plus 16 bytes of slack for the
 * andl $-16 alignment; GE_XMM_AREA holds XMM0-7 (8 x 16 bytes), also aligned. */
#define GE_FX_FRAME 528
#define GE_XMM_AREA 128
static_assert(GE_FX_FRAME==FXSAVE_SIZE+16 && GE_XMM_AREA==8*16,"GL boundary stack areas");
/* f=pushad, f[8]=flags, f[9]=synthetic id/target, f[10]=caller.
 * ret consumes only our synthetic target; API cdecl/stdcall cleanup is intact.
 * GE_REST is the canonical body after "pushfl;pushal;movl %esp,%ebp", run when
 * the fast C half returns NULL; XMM0-7 are reloaded first, and integer C left
 * x87 and MXCSR alone, so its fxsave sees the thunk's entry state. */
#define GE_REST(fn) "movl %ebp,%esp;subl $" GE_STR(GE_FX_FRAME) ",%esp;andl $-16,%esp;fxsave (%esp);movl %esp,%eax;subl $8,%esp;pushl %eax;pushl %ebp;call " GE_CSYM(fn) ";addl $16,%esp;fxrstor (%esp);movl %ebp,%esp;popal;popfl;ret"
#define GE_XSAVE "movaps %xmm0,(%esp);movaps %xmm1,16(%esp);movaps %xmm2,32(%esp);movaps %xmm3,48(%esp);movaps %xmm4,64(%esp);movaps %xmm5,80(%esp);movaps %xmm6,96(%esp);movaps %xmm7,112(%esp);"
#define GE_XLOAD "movaps (%esp),%xmm0;movaps 16(%esp),%xmm1;movaps 32(%esp),%xmm2;movaps 48(%esp),%xmm3;movaps 64(%esp),%xmm4;movaps 80(%esp),%xmm5;movaps 96(%esp),%xmm6;movaps 112(%esp),%xmm7;"
#define GE_FRAME "pushfl;pushal;movl %esp,%ebp;subl $" GE_STR(GE_XMM_AREA) ",%esp;andl $-16,%esp;" GE_XSAVE
/* Fast-thunk invariants (exactness of the entry snapshot and return state):
 *  - from thunk entry to the fast enter's fxsave, nothing may touch x87, MMX
 *    or MXCSR state: only pushfl/pushal, movaps, integer C (checked by
 *    build.sh) and test. The fast return's fxrstor then sets all of it;
 *  - GE_XLOAD sits after the C call and before fxsave (enter) or after
 *    fxrstor (return), and before GE_REST on both slow branches, so XMM0-7
 *    are the thunk-entry values wherever they are saved or handed back;
 *  - the fast return clears the tid only after fxrstor has read the record;
 *  - labels are numeric locals (1:, 2:), so the two naked bodies cannot clash. */
void __attribute__((naked,used)) glenv_enter_asm(void){__asm__ volatile(
    GE_FRAME "subl $12,%esp;pushl %ebp;call " GE_CSYM("glenv_enter_fast_c") ";addl $16,%esp;" GE_XLOAD
    "testl %eax,%eax;jz 1f;fxsave (%eax);movl %ebp,%esp;popal;popfl;ret;"
    "1:" GE_REST("glenv_enter_c"));}
void __attribute__((naked,used)) glenv_return_asm(void){__asm__ volatile(
    "pushl $0;" GE_FRAME "subl $16,%esp;leal 8(%esp),%eax;movl %eax,4(%esp);movl %ebp,(%esp);call " GE_CSYM("glenv_return_fast_c") ";"
    "movl 8(%esp),%edx;addl $16,%esp;testl %eax,%eax;jz 1f;fxrstor (%eax);" GE_XLOAD
    "testl %edx,%edx;jz 2f;movl $0,(%edx);2:movl %ebp,%esp;popal;popfl;ret;"
    "1:" GE_XLOAD GE_REST("glenv_return_c"));}
void *glenv_proc(const char *name,void *target) {
    if(g_bnd_native || name[0]!='g' || name[1]!='l')return target;
    if(!target || (uintptr_t)target<=3 || (uintptr_t)target==0xffffffffu)return NULL;
    if(!glenv_supported(name)){slog("unsupported GL environment API: %s",name);ge_die("unsupported return/signature contract");}
    DWORD error=GetLastError();ge_acquire();
    if(!ge_executable) {
        DWORD old;if(!VirtualProtect(ge_code,sizeof ge_code,PAGE_EXECUTE_READWRITE,&old))ge_die("executable thunk allocation failed");
        ge_executable=1;
    }
    u32 i=0;for(;i<ge_nnames;i++)if(k_streq(ge_names[i].name,name))break;
    if(i==ge_nnames) {
        if(i==GE_NAMES || strlen(name)>=sizeof ge_names[i].name)ge_die("API catalog capacity exhausted");
        memcpy(ge_names[i].name,name,strlen(name)+1);
        u8 *p=ge_code[i];p[0]=0x68;__builtin_memcpy(p+1,&i,sizeof i);p[5]=0xe9;
        const u32 relative=(u32)(uintptr_t)glenv_enter_asm-(u32)(uintptr_t)(p+10);
        __builtin_memcpy(p+6,&relative,sizeof relative);
        if(!FlushInstructionCache(GetCurrentProcess(),p,16))ge_die("instruction cache flush failed");
        ge_nnames++;
    }
    if(target!=ge_code[i])ge_names[i].target=(u32)(uintptr_t)target;
    ge_release();SetLastError(error);return ge_code[i];
}
int glenv_active(void) {
    DWORD error=GetLastError();u32 tid=GetCurrentThreadId(),slot=K?K->cur:0;
    int active=0;ge_acquire();
    for(u32 i=0;i<GE_THREADS;i++)if(ge_threads[i].tid==tid && ge_threads[i].slot==slot) {
        active=ge_threads[i].depth!=0;break;
    }
    ge_release();SetLastError(error);return active;
}
int glenv_capture_ready(void) {
    int ready=1;ge_acquire();
    for(u32 i=0;i<GE_THREADS;i++)if(ge_threads[i].depth){ready=0;break;}
    ge_release();return ready;
}
u32 glenv_caller(u32 ret) {
    if(ret!=(u32)(uintptr_t)glenv_return_asm)return ret;
    DWORD error=GetLastError();ge_thread_t *t=ge_thread(GetCurrentThreadId(),K?K->cur:0,0);
    if(!t->depth)ge_die("caller lookup without entry");
    ret=t->calls[t->depth-1].caller;SetLastError(error);return ret;
}
#ifndef GLENV_ELF
void glenv_install(void) {
    if(g_bnd_native)return;
    u8 *base=(u8*)GetModuleHandleA(NULL);IMAGE_DOS_HEADER *dos=reinterpret_cast<IMAGE_DOS_HEADER *>(base);
    IMAGE_NT_HEADERS *nt=reinterpret_cast<IMAGE_NT_HEADERS *>((base+dos->e_lfanew));
    IMAGE_IMPORT_DESCRIPTOR *imp=reinterpret_cast<IMAGE_IMPORT_DESCRIPTOR *>((base+nt->OptionalHeader.DataDirectory[IMAGE_DIRECTORY_ENTRY_IMPORT].VirtualAddress));
    if(!nt->OptionalHeader.DataDirectory[IMAGE_DIRECTORY_ENTRY_IMPORT].VirtualAddress)return;
    for(;imp->Name;imp++) {
        if(!imp->OriginalFirstThunk)ge_die("import names unavailable");
        IMAGE_THUNK_DATA *names=reinterpret_cast<IMAGE_THUNK_DATA *>((base+imp->OriginalFirstThunk)),*slots=reinterpret_cast<IMAGE_THUNK_DATA *>((base+imp->FirstThunk));
        for(;names->u1.AddressOfData;names++,slots++) {
            if(IMAGE_SNAP_BY_ORDINAL(names->u1.Ordinal))continue;
            const char *name=(const char*)((IMAGE_IMPORT_BY_NAME*)(base+names->u1.AddressOfData))->Name;
            if(name[0]!='g'||name[1]!='l')continue;
            void *target=(void*)(uintptr_t)slots->u1.Function;
            if(!iat_set((u32*)&slots->u1.Function,glenv_proc(name,target)))ge_die("IAT replacement failed");
        }
    }
}
#endif

} /* extern "C" */
