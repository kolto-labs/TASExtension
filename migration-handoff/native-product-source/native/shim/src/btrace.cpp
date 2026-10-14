/* Optional diagnostic trace: core-only records, transparent x86 entry/return
 * trampolines. Never part of capture identity and never an acceptance mask. */
#include "shim.hpp"

/* Fixed C hook ABI and assembly-referenced symbol names. */
extern "C" {
#define BT_NAMES 2048u
#define BT_EVENTS 200000u
#define BT_THREADS 32u
#define BT_DEPTH 1024u
#define BT_SIGNATURES 65536u
/* Exported for the inline btrace_point fast path in shim.hpp. Written once, by
 * btrace_install (the last install in attach_body), before any traced code runs. */
int g_bt_enabled;
static int bt_first_gl_error,bt_probe_gl_status;
static void bt_gl_boundary(const char *name,u32 caller,u32 arg,int entering);
static void bt_gl_point(const char *name,u32 phase);
typedef struct {u32 target;char name[124];} bt_name_t;
typedef struct {u32 seq,tick,tid,slot,phase,name,caller,sp,mxcsr,cw_sw,flags,error,eax,edx,gl_error;} bt_event_t;
typedef struct {u32 name,caller,sp,arg,probe;bt_event_t entry;} bt_call_t;
typedef struct {u32 tid,slot,depth,has_last;bt_event_t last;bt_call_t call[BT_DEPTH];} bt_thread_t;
static bt_name_t bt_names[BT_NAMES];static u32 bt_nnames;
static bt_event_t bt_events[BT_EVENTS];static volatile LONG bt_count;
static bt_thread_t bt_threads[BT_THREADS];
typedef struct {u32 used,hits;bt_event_t before,after;} bt_signature_t;
static bt_signature_t bt_signatures[BT_SIGNATURES];
static volatile LONG bt_sequence,bt_lock,bt_signature_overflow;
static u8 bt_code[BT_NAMES][16] __attribute__((aligned(4096)));
static HANDLE bt_file=INVALID_HANDLE_VALUE;
static void bt_die(void){TerminateProcess(GetCurrentProcess(),6);for(;;){}}
static bt_thread_t *bt_thread(u32 tid,u32 slot) {
    for(u32 i=0;i<BT_THREADS;i++)if(bt_threads[i].tid==tid && bt_threads[i].slot==slot)return &bt_threads[i];
    for(u32 i=0;i<BT_THREADS;i++)if(!bt_threads[i].tid){bt_threads[i].slot=slot;bt_threads[i].tid=tid;return &bt_threads[i];}
    bt_die();return NULL;
}
/* Observe every boundary; retain the first occurrence of each exact state
 * transition. Heap/TLS/file loops otherwise fill a bounded log before graphics
 * initialization. Repeated transitions increment a counter, not a blind sample.
 * Sequence numbers are observations, so omissions are explicit in the output. */
static bt_event_t bt_key(bt_event_t e) {
    e.seq=e.edx=0;e.flags&=0x200000u;
    if(!k_streq(bt_names[e.name].name,"glGetError"))e.eax=0;
    return e;
}
static void bt_pair(bt_event_t prev,bt_event_t e) {
    if(bt_signature_overflow)return;
    bt_event_t pair[2]={bt_key(prev),bt_key(e)};
    const u32 *v=(const u32*)pair;u32 hash=2166136261u;
    for(u32 i=0;i<sizeof pair/4;i++)hash=(hash^v[i])*16777619u;
    while(InterlockedCompareExchange(&bt_lock,1,0)){}
    for(u32 probes=0;probes<BT_SIGNATURES;probes++) {
        bt_signature_t *sig=&bt_signatures[(hash+probes)&(BT_SIGNATURES-1)];
        if(sig->used) {
            if(!memcmp(&sig->before,pair,sizeof pair)){sig->hits++;InterlockedExchange(&bt_lock,0);return;}
            continue;
        }
        sig->used=1;sig->hits=1;sig->before=pair[0];sig->after=pair[1];
        u32 n=(u32)bt_count;
        if(n+2<=BT_EVENTS){bt_events[n]=prev;bt_events[n+1]=e;bt_count=n+2;}
        else bt_signature_overflow=1;
        InterlockedExchange(&bt_lock,0);return;
    }
    bt_signature_overflow=1;InterlockedExchange(&bt_lock,0);
}
static void bt_record(u32 phase,u32 name,u32 caller,u32 sp,const u32 *frame,const u8 *fx,u32 error,u32 tid,u32 slot) {
    if(!g_bt_enabled || g_gate.tick>2)return;
    bt_thread_t *t=bt_thread(tid,slot);
    bt_event_t e={(u32)InterlockedIncrement(&bt_sequence)-1,(u32)g_gate.tick,tid,slot,phase,name,caller,sp,
        *(const u32*)(fx+24),*(const u32*)fx,frame[8],error,frame[7],frame[5],SH?SH->gl_err:0};
    bt_event_t prev=t->has_last?t->last:e;t->last=e;t->has_last=1;
    bt_pair(prev,e);
}
/* Deliberately perturbing diagnostic, finite observed void/integer/pointer GL
 * returns only. It clears MXCSR status bits, preserving rounding/masks/FTZ/DAZ,
 * arguments and every return register. Never used for acceptance. */
static int bt_probe_supported(const char *name) {
    static const char *names[]={
        "glActiveTextureARB",
        "glAlphaFragmentOp1ATI",
        "glAlphaFragmentOp2ATI",
        "glAlphaFunc",
        "glBegin",
        "glBeginFragmentShaderATI",
        "glBindFragmentShaderATI",
        "glBindProgramARB",
        "glBindTexture",
        "glBlendFunc",
        "glClear",
        "glClearColor",
        "glClearStencil",
        "glClientActiveTextureARB",
        "glColor4f",
        "glColor4ubv",
        "glColorFragmentOp1ATI",
        "glColorFragmentOp2ATI",
        "glColorFragmentOp3ATI",
        "glColorMask",
        "glCompressedTexImage2DARB",
        "glDeleteProgramsARB",
        "glDepthFunc",
        "glDepthMask",
        "glDisable",
        "glDisableClientState",
        "glDisableVertexAttribArrayARB",
        "glDrawElements",
        "glEnable",
        "glEnableClientState",
        "glEnableVertexAttribArrayARB",
        "glEnd",
        "glEndFragmentShaderATI",
        "glFinish",
        "glFogf",
        "glFogfv",
        "glGenFragmentShadersATI",
        "glGenProgramsARB",
        "glGenTextures",
        "glGetError",
        "glGetFloatv",
        "glGetIntegerv",
        "glGetString",
        "glIsProgramARB",
        "glLightModelfv",
        "glLightfv",
        "glLoadIdentity",
        "glMaterialfv",
        "glMatrixMode",
        "glMultMatrixf",
        "glNormalPointer",
        "glOrtho",
        "glPassTexCoordATI",
        "glPolygonMode",
        "glPopAttrib",
        "glPopMatrix",
        "glProgramEnvParameter4fARB",
        "glProgramStringARB",
        "glPushAttrib",
        "glPushMatrix",
        "glSampleMapATI",
        "glScalef",
        "glSetFragmentShaderConstantATI",
        "glStencilFunc",
        "glStencilMask",
        "glStencilOp",
        "glTexCoord2f",
        "glTexCoordPointer",
        "glTexGeni",
        "glTexImage2D",
        "glTexParameteri",
        "glTexSubImage2D",
        "glTranslatef",
        "glVertex3fv",
        "glVertex4f",
        "glVertexAttribPointerARB",
        "glVertexPointer",
        "glViewport",
        "gluBuild2DMipmaps",
        "gluErrorString",
        "gluPerspective",
};
    for(u32 i=0;i<sizeof names/sizeof names[0];i++)if(k_streq(name,names[i]))return 1;
    return 0;
}
void btrace_return_asm(void);
void __attribute__((used,noinline)) btrace_enter_c(u32 *f,const u8 *fx) {
    DWORD error=GetLastError(),tid=GetCurrentThreadId();u32 slot=K?K->cur:0;
    u32 name=f[9],caller=f[10];bt_thread_t *t=bt_thread(tid,slot);
    if(name>=bt_nnames || t->depth==BT_DEPTH)bt_die();
    t->call[t->depth++]=bt_call_t{name,caller,static_cast<u32>(reinterpret_cast<uintptr_t>(f+10)),f[11],0,{}};
    bt_record(1,name,caller,(u32)(uintptr_t)(f+10),f,fx,error,tid,slot);
    t->call[t->depth-1].entry=t->last;
    if(bt_probe_gl_status&&bt_names[name].name[0]=='g'&&bt_names[name].name[1]=='l'){
        if(bt_probe_supported(bt_names[name].name)){
            t->call[t->depth-1].probe=1;
            *(u32*)(fx+24)&=~0x3fu;
            bt_record(80,name,caller,(u32)(uintptr_t)(f+10),f,fx,error,tid,slot);
        }else bt_record(82,name,caller,(u32)(uintptr_t)(f+10),f,fx,error,tid,slot);
    }
    if(bt_first_gl_error)bt_gl_boundary(bt_names[name].name,caller,f[11],1);
    f[9]=bt_names[name].target;f[10]=(u32)(uintptr_t)btrace_return_asm;SetLastError(error);
}
void __attribute__((used,noinline)) btrace_return_c(u32 *f,const u8 *fx) {
    DWORD error=GetLastError(),tid=GetCurrentThreadId();u32 slot=K?K->cur:0;
    bt_thread_t *t=bt_thread(tid,slot);if(!t->depth)bt_die();bt_call_t c=t->call[--t->depth];
    bt_record(2,c.name,c.caller,(u32)(uintptr_t)(f+10),f,fx,error,tid,slot);
    if(g_gate.tick<=2 && (c.entry.mxcsr!=t->last.mxcsr || c.entry.cw_sw!=t->last.cw_sw ||
        c.entry.error!=t->last.error || c.entry.gl_error!=t->last.gl_error ||
        ((c.entry.flags^t->last.flags)&0x200000u)))bt_pair(c.entry,t->last);
    if(bt_first_gl_error)bt_gl_boundary(bt_names[c.name].name,c.caller,c.arg,0);
    if(c.probe){
        *(u32*)(fx+24)=(*(u32*)(fx+24)&~0x3fu)|(c.entry.mxcsr&0x3fu);
        bt_record(81,c.name,c.caller,(u32)(uintptr_t)(f+10),f,fx,error,tid,slot);
    }
    f[9]=c.caller;SetLastError(error);
}
static u32 bt_name(const char *name,void *target) {
    for(u32 i=0;i<bt_nnames;i++)if(bt_names[i].target==(u32)(uintptr_t)target && k_streq(bt_names[i].name,name))return i;
    if(bt_nnames==BT_NAMES)bt_die();u32 i=bt_nnames++;bt_names[i].target=(u32)(uintptr_t)target;
    u32 n=(u32)strlen(name);if(n>=sizeof bt_names[i].name)n=sizeof bt_names[i].name-1;
    memcpy(bt_names[i].name,name,n);bt_names[i].name[n]=0;return i;
}
void __attribute__((used,noinline)) btrace_point_c(u32 *f,const u8 *fx) {
    if(!g_bt_enabled)return;
    DWORD error=GetLastError(),tid=GetCurrentThreadId();u32 slot=K?K->cur:0;
    const char *name=(const char *)(uintptr_t)f[10];u32 phase=f[11];
    bt_record(phase,bt_name(name,NULL),f[9],(u32)(uintptr_t)(f+9),f,fx,error,tid,slot);
    if(bt_first_gl_error)bt_gl_point(name,phase);SetLastError(error);
}
/* f is the pushad frame, with original flags at f[8]. The aligned temporary
 * FXSAVE area preserves x87 returns and all XMM registers as well as control.
 * A synthetic target word is consumed with ret; API stack cleanup is intact. */
#ifdef BTRACE_ELF
#define BT_CSYM(s) s
#else
#define BT_CSYM(s) "_" s
#endif
#define BT_BODY(fn) "pushfl;pushal;movl %esp,%ebp;subl $528,%esp;andl $-16,%esp;fxsave (%esp);movl %esp,%eax;subl $8,%esp;pushl %eax;pushl %ebp;call " BT_CSYM(fn) ";addl $16,%esp;fxrstor (%esp);movl %ebp,%esp;popal;popfl;ret"
void __attribute__((naked,used)) btrace_enter_asm(void) {__asm__ volatile(BT_BODY("btrace_enter_c"));}
void __attribute__((naked,used)) btrace_return_asm(void) {__asm__ volatile("pushl $0;" BT_BODY("btrace_return_c"));}
/* Reached only through the always-inline btrace_point in shim.hpp, which skips
 * the call (and this full flags/register/FX save) when tracing is off. */
void __attribute__((naked,used)) btrace_point_asm(const char *name,u32 phase) {__asm__ volatile(BT_BODY("btrace_point_c"));}
void *btrace_proc(const char *name,void *target) {
    if(!g_bt_enabled || !target)return target;
    DWORD error=GetLastError();u32 i=bt_name(name,target);u8 *p=bt_code[i];
    p[0]=0x68;__builtin_memcpy(p+1,&i,sizeof i);p[5]=0xe9;
    const u32 relative=(u32)(uintptr_t)btrace_enter_asm-(u32)(uintptr_t)(p+10);
    __builtin_memcpy(p+6,&relative,sizeof relative);
    SetLastError(error);return p;
}
u32 btrace_caller(u32 ret) {
    ret=glenv_caller(ret);
    if(!g_bt_enabled || ret!=(u32)(uintptr_t)btrace_return_asm)return ret;
    DWORD error=GetLastError();bt_thread_t *t=bt_thread(GetCurrentThreadId(),K?K->cur:0);
    if(t->depth)ret=t->call[t->depth-1].caller;SetLastError(error);return ret;
}
void btrace_flush(void) {
    if(!g_bt_enabled || bt_file==INVALID_HANDLE_VALUE)return;
    u32 count=(u32)bt_count,kept=count<BT_EVENTS?count:BT_EVENTS;
    u32 header[]={0x3154424b,2,bt_nnames,kept,(u32)bt_signature_overflow,(u32)bt_sequence,sizeof(bt_event_t)};DWORD wrote;
    SetFilePointer(bt_file,0,NULL,FILE_BEGIN);
    WriteFile(bt_file,header,sizeof header,&wrote,NULL);
    WriteFile(bt_file,bt_names,bt_nnames*sizeof *bt_names,&wrote,NULL);
    WriteFile(bt_file,bt_events,kept*sizeof *bt_events,&wrote,NULL);SetEndOfFile(bt_file);FlushFileBuffers(bt_file);
}

/* Explicit consume-and-abort diagnostic. Never enabled by ordinary tracing.
 * All calls execute inside the trampoline's full register/FX/flags save, so
 * successful zero-error observations cannot contaminate the observed caller. */
static u32 (WINAPI *bt_GetError)(void);
static void (WINAPI *bt_GetInteger)(u32,int*);
static HGLRC (WINAPI *bt_GetContext)(void);
static HDC (WINAPI *bt_GetDC)(void);
static void *(WINAPI *bt_GetProc)(const char*);
static void (WINAPI *bt_GetAttachment)(u32,u32,u32,int*);
static u32 (WINAPI *bt_CheckFramebuffer)(u32);
static void (WINAPI *bt_GetTextureLevel)(u32,int,u32,int*);
static u32 bt_gl_thread,bt_gl_armed,bt_gl_inside,bt_texture_observations;
static int bt_valid_proc(void*p){u32 v=(u32)(uintptr_t)p;return v>3&&v!=0xffffffffu;}
static void bt_gl_abort(const char *where,const char *name,u32 caller,u32 arg,u32 error) {
    slog("FIRST_GL_ERROR where=%s api=%s caller=%08x arg0=%08x native=%x shadow=%x tick=%u sequence=%u context=%x dc=%x",
         where,name,caller,arg,error,SH->gl_err,(u32)g_gate.tick,(u32)bt_sequence,
         (u32)(uintptr_t)bt_GetContext(),(u32)(uintptr_t)bt_GetDC());
    btrace_flush();TerminateProcess(GetCurrentProcess(),86);for(;;){}
}
static void bt_gl_check(const char *where,const char *name,u32 caller,u32 arg) {
    u32 e=bt_GetError();if(e)bt_gl_abort(where,name,caller,arg,e);
}
static int bt_gl_ready(void) {
    if(!bt_GetError){HMODULE m=GetModuleHandleA("opengl32.dll");if(!m)return 0;
        bt_GetError=reinterpret_cast<decltype(bt_GetError)>(GetProcAddress(m,"glGetError"));bt_GetInteger=reinterpret_cast<decltype(bt_GetInteger)>(GetProcAddress(m,"glGetIntegerv"));
        bt_GetContext=reinterpret_cast<decltype(bt_GetContext)>(GetProcAddress(m,"wglGetCurrentContext"));bt_GetDC=reinterpret_cast<decltype(bt_GetDC)>(GetProcAddress(m,"wglGetCurrentDC"));
        bt_GetProc=reinterpret_cast<decltype(bt_GetProc)>(GetProcAddress(m,"wglGetProcAddress"));}
    return bt_GetError&&bt_GetInteger&&bt_GetContext&&bt_GetDC&&bt_GetProc&&bt_GetContext();
}
static void bt_texture_observe(const char*label) {
    if(!bt_gl_ready())return;
    bt_gl_check("pre-observation",label,0,0);
    if(!bt_GetAttachment){void*p=bt_GetProc("glGetFramebufferAttachmentParameteriv");if(bt_valid_proc(p))bt_GetAttachment=reinterpret_cast<decltype(bt_GetAttachment)>(p);}
    if(!bt_CheckFramebuffer){void*p=bt_GetProc("glCheckFramebufferStatus");if(bt_valid_proc(p))bt_CheckFramebuffer=reinterpret_cast<decltype(bt_CheckFramebuffer)>(p);}
    if(!bt_GetTextureLevel){void*p=bt_GetProc("glGetTextureLevelParameteriv");if(bt_valid_proc(p))bt_GetTextureLevel=reinterpret_cast<decltype(bt_GetTextureLevel)>(p);}
    int draw=0,read=0,selector=0,type=0,name=0,w=-1,h=-1;u32 status=0;
    bt_GetInteger(0x8ca6,&draw);bt_GetInteger(0x8caa,&read);bt_GetInteger(0x0c01,&selector);
    if(draw&&bt_GetAttachment){bt_GetAttachment(0x8ca9,0x8ce0,0x8cd0,&type);bt_GetAttachment(0x8ca9,0x8ce0,0x8cd1,&name);}
    if(bt_CheckFramebuffer)status=bt_CheckFramebuffer(0x8ca9);
    if(type==0x1702&&name&&bt_GetTextureLevel){bt_GetTextureLevel(name,0,0x1000,&w);bt_GetTextureLevel(name,0,0x1001,&h);}
    bt_gl_check("observation-query",label,0,0);
    slog("GL_TEXTURE_OWNERSHIP %s draw_fbo=%x read_fbo=%x draw_buffer=%x attachment_type=%x attachment_name=%u width=%d height=%d framebuffer_status=%x logical_next=%u",
         label,draw,read,selector,type,name,w,h,status,SH->gl_tex_next);
}
static void bt_gl_point(const char *name,u32 phase) {
    if(k_streq(name,"glsync.capture.error")&&phase==0x20000u&&g_gate.tick==0&&bt_gl_ready()){
        bt_gl_thread=GetCurrentThreadId();bt_gl_armed=1;
        slog("FIRST_GL_ERROR armed after clean tick0 capture sequence=%u context=%x",(u32)bt_sequence,(u32)(uintptr_t)bt_GetContext());
    }
    if(k_streq(name,"gl.texture.first.before")||k_streq(name,"gl.texture.first.after")) {
        if(bt_texture_observations<2){bt_texture_observe(name);bt_texture_observations++;}
    }
}
static void bt_gl_boundary(const char *name,u32 caller,u32 arg,int entering) {
    if(!bt_gl_armed||g_gate.tick!=1||GetCurrentThreadId()!=bt_gl_thread||!bt_gl_ready()||name[0]!='g'||name[1]!='l')return;
    if(entering){
        if(!bt_gl_inside)bt_gl_check("pre-existing",name,caller,arg);
        if(k_streq(name,"glBegin"))bt_gl_inside=1;
    } else {
        if(k_streq(name,"glEnd"))bt_gl_inside=0;
        if(!bt_gl_inside)bt_gl_check("return",name,caller,arg);
    }
}
void btrace_install(void) {
    char value[16];if(!GetEnvironmentVariableA("KPTAS_BOUNDARY_TRACE",value,sizeof value)||value[0]!='1')return;
    bt_probe_gl_status=GetEnvironmentVariableA("KPTAS_BOUNDARY_PROBE_GL_STATUS",value,sizeof value)&&value[0]=='1';
    bt_first_gl_error=GetEnvironmentVariableA("KPTAS_BOUNDARY_FIRST_GL_ERROR",value,sizeof value)&&value[0]=='1';
    DWORD old;if(!VirtualProtect(bt_code,sizeof bt_code,PAGE_EXECUTE_READWRITE,&old))bt_die();
    char path[1024];buf_t b;b_init(&b,path,sizeof path);b_str(&b,g_session_dir);b_str(&b,"\\kptas_boundary_trace.bin");b_term(&b);
    bt_file=CreateFileA(path,GENERIC_WRITE,FILE_SHARE_READ,NULL,CREATE_ALWAYS,0,NULL);
    if(bt_file==INVALID_HANDLE_VALUE)bt_die();g_bt_enabled=1;
    u8 *base=(u8*)GetModuleHandleA(NULL);IMAGE_DOS_HEADER *dos=reinterpret_cast<IMAGE_DOS_HEADER *>(base);
    IMAGE_NT_HEADERS *nt=reinterpret_cast<IMAGE_NT_HEADERS *>((base+dos->e_lfanew));
    IMAGE_IMPORT_DESCRIPTOR *imp=reinterpret_cast<IMAGE_IMPORT_DESCRIPTOR *>((base+nt->OptionalHeader.DataDirectory[IMAGE_DIRECTORY_ENTRY_IMPORT].VirtualAddress));
    for(;imp->Name;imp++) {
        if(!imp->OriginalFirstThunk)bt_die();
        IMAGE_THUNK_DATA *names=reinterpret_cast<IMAGE_THUNK_DATA *>((base+imp->OriginalFirstThunk)),*slots=reinterpret_cast<IMAGE_THUNK_DATA *>((base+imp->FirstThunk));
        for(;names->u1.AddressOfData;names++,slots++) {
            if(IMAGE_SNAP_BY_ORDINAL(names->u1.Ordinal))continue;
            const char *name=(const char*)((IMAGE_IMPORT_BY_NAME*)(base+names->u1.AddressOfData))->Name;
            void *target=(void*)(uintptr_t)slots->u1.Function;
            if(!iat_set((u32*)&slots->u1.Function,btrace_proc(name,target)))bt_die();
        }
    }
    if(bt_probe_gl_status)slog("BTRACE PERTURBING GL MXCSR status probe enabled: roots and behavior NOT acceptance evidence");
    slog("btrace enabled: imports=%u event_capacity=%u first3ticks",bt_nnames,BT_EVENTS);
}

} /* extern "C" */
