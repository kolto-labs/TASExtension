/* ARB_vertex_buffer_object boundary. ARRAY/ELEMENT storage only: GPU writes,
 * other targets and multiple contexts are rejected before silent divergence.
 * See Khronos ARB_vertex_buffer_object, sections 2.8A and 6.1.13. */
#include "shim.hpp"
#include "glbuffers.hpp"

/* Fixed C hook ABI and assembly-referenced symbol names. */
extern "C" {
#define API __attribute__((stdcall))
#define ARRAY 0x8892u
#define ELEMENT 0x8893u
#define READ_ONLY 0x88b8u
#define WRITE_ONLY 0x88b9u
#define READ_WRITE 0x88bau
#define S (SH->glb)
static void *(API *resolve)(const char *);
static u32 (API *error)(void);
static void (API *getint)(u32,int *);
static void (API *p_bind)(u32,u32);
static void (API *del)(int,const u32 *);
static void (API *gen)(int,u32 *);
static void (API *data)(u32,int,const void *,u32);
static void (API *subdata)(u32,int,int,const void *);
static void (API *getsubdata)(u32,int,int,void *);
static void *(API *map)(u32,u32);
static u8 (API *unmap)(u32);
static void *driver_map[GLB_CAP];
static u8 driver_live[GLB_CAP];
static u32 driver_name[GLB_CAP],driver_context;
static void die(const char *why) {
    slog("GL buffer boundary unsupported: %s tick=%u",why,(u32)g_gate.tick);
    TerminateProcess(GetCurrentProcess(),6); for(;;) {}
}
static void seterr(u32 e) { if(!SH->gl_err) SH->gl_err=e; }
static void preserve_error(void) { u32 e=error();if(e)seterr(e); }
static void checked(void) { if(error())die("driver rejected validated operation"); }
static int resolve_all(void) {
    HMODULE gl=GetModuleHandleA("opengl32.dll");
    resolve=reinterpret_cast<decltype(resolve)>(GetProcAddress(gl,"wglGetProcAddress"));
    error=reinterpret_cast<decltype(error)>(GetProcAddress(gl,"glGetError"));
    getint=reinterpret_cast<decltype(getint)>(GetProcAddress(gl,"glGetIntegerv"));
    if(!resolve || !error || !getint)return 0;
#define RESOLVE(v,n) do { void *p=resolve(n); if((uintptr_t)p<=3 || (uintptr_t)p==0xffffffffu)return 0; v=reinterpret_cast<decltype(v)>(p); } while(0)
    RESOLVE(p_bind,"glBindBufferARB");RESOLVE(del,"glDeleteBuffersARB");RESOLVE(gen,"glGenBuffersARB");
    RESOLVE(data,"glBufferDataARB");RESOLVE(subdata,"glBufferSubDataARB");
    RESOLVE(getsubdata,"glGetBufferSubDataARB");
    RESOLVE(map,"glMapBufferARB");RESOLVE(unmap,"glUnmapBufferARB");
#undef RESOLVE
    return 1;
}
static void mapping_context(void) {
    if(!SH->wh.current_gl)die("no tracked mapping context");
    if(driver_context && driver_context!=SH->wh.current_gl)die("native buffer mapping belongs to another context");
    driver_context=SH->wh.current_gl;
}
static u32 reserve_native(u32 logical) {
    mapping_context();
    if(!logical || logical>=GLB_CAP)die("invalid mapped logical name");
    if(!driver_name[logical]) {
        u32 native=0;gen(1,&native);checked();
        if(!native)die("native name allocation failed");
        for(u32 i=1;i<GLB_CAP;i++)if(driver_name[i]==native)die("driver returned duplicate reserved name");
        driver_name[logical]=native;
    }
    return driver_name[logical];
}
u32 glbuffers_real_name(u32 logical) {
    if(!logical)return 0;
    mapping_context();
    if(logical>=GLB_CAP || !S.object[logical].reserved || !driver_name[logical])die("missing native name for logical buffer");
    return driver_name[logical];
}
u32 glbuffers_logical_name(u32 native) {
    if(!native)return 0;
    mapping_context();
    for(u32 i=1;i<GLB_CAP;i++)if(driver_name[i]==native && S.object[i].reserved)return i;
    die("foreign native buffer in game binding");return 0;
}
int glbuffers_binding_enum(u32 pname) {
    return pname==0x8894 || pname==0x8895 || (pname>=0x8896 && pname<=0x889e);
}
static void enter(void) {
    if(!p_bind && !resolve_all())die("required ARB API unavailable");
    if(K->cur!=0)die("buffer operation outside main simulation thread");
    u32 ctx=SH->wh.current_gl;
    if(!ctx)die("no tracked current context");
    if(S.used && S.context!=ctx)die("multiple buffer contexts");
    mapping_context();S.used=1;S.context=ctx;preserve_error();
}
static int target(u32 t) {
    if(t==ARRAY || t==ELEMENT)return 1;
    /* These are valid newer targets, but their GPU-write semantics need an
     * additional boundary. Do not misrepresent them as invalid GL enums. */
    if(t==0x88eb || t==0x88ec || t==0x8f36 || t==0x8f37 || t==0x8c8e ||
       t==0x8a11 || t==0x8c2a || t==0x90d2 || t==0x90ee || t==0x8f3f || t==0x9192)
        die("buffer target outside ARRAY/ELEMENT contract");
    seterr(0x500);return 0;
}
static glb_object_t *bound(u32 t,u32 *name) {
    if(!target(t))return NULL;
    int n=0;getint(t==ARRAY?0x8894:0x8895,&n);checked();
    if(!n){seterr(0x502);return NULL;}
    n=(int)glbuffers_logical_name((u32)n);
    if(!S.object[n].live)die("untracked binding");
    if(name)*name=(u32)n;return &S.object[n];
}
static int usage(u32 u) {
    return (u>=0x88e0 && u<=0x88e2)||(u>=0x88e4 && u<=0x88e6)||(u>=0x88e8 && u<=0x88ea);
}
static void API w_Gen(int n,u32 *out) {
    enter();if(n<0){seterr(0x501);return;}
    for(int i=0;i<n;i++) {
        /* The cursor is captured, but capacity bounds concurrent names, not
         * lifetime allocations. A restored high cursor may have free names below. */
        u32 v=S.next;
        if(!v || v>=GLB_CAP)v=1;
        u32 start=v;
        while(S.object[v].reserved) {
            if(++v==GLB_CAP)v=1;
            if(v==start)die("name capacity exhausted");
        }
        reserve_native(v);
        S.next=v+1;S.object[v].reserved=1;out[i]=v;
    }
}
static void API w_Bind(u32 t,u32 n) {
    enter();if(!target(t))return;if(n>=GLB_CAP)die("name outside bounded namespace");
    p_bind(t,n?reserve_native(n):0);checked();
    if(n && !S.object[n].live) {
        S.object[n]=glb_object_t{1,1,0,0x88e4,READ_WRITE,0,nullptr};
        driver_live[n]=1;
    }
}
static void API w_Delete(int n,const u32 *names) {
    enter();if(n<0){seterr(0x501);return;}
    for(int i=0;i<n;i++) {
        u32 v=names[i];if(!v || v>=GLB_CAP)continue;
        glb_object_t *o=&S.object[v];if(!o->reserved)continue;
        u32 native=glbuffers_real_name(v);del(1,&native);checked();
        driver_name[v]=0;driver_map[v]=NULL;driver_live[v]=0;
        if(o->bytes && !kst_vfree(o->bytes))die("backing release failed");
        memset(o,0,sizeof *o);
        if(!S.next || v<S.next)S.next=v;
    }
}
static u8 API w_Is(u32 n) { enter();return n<GLB_CAP && S.object[n].live; }
static void API w_Data(u32 t,int size,const void *src,u32 use) {
    enter();u32 n;glb_object_t *o=bound(t,&n);if(!o)return;
    if(size<0){seterr(0x501);return;}if(!usage(use)){seterr(0x500);return;}
    u8 *p=size?static_cast<u8 *>(kst_valloc(static_cast<u32>(size))):nullptr;
    if(size && !p){seterr(0x505);return;}
    if(size){if(src)memcpy(p,src,(u32)size);else memset(p,0,(u32)size);}
    /* Undefined initial contents receive an actual deterministic initialization
     * in both stores. This does not alter defined source-data behavior. */
    data(t,size,p,use);u32 e=error();
    if(e){if(p)kst_vfree(p);seterr(e);return;}
    if(o->bytes && !kst_vfree(o->bytes))die("old backing release failed");
    o->bytes=p;o->size=(u32)size;o->usage=use;o->mapped=0;o->access=READ_WRITE;driver_map[n]=NULL;
}
static int range(glb_object_t *o,int off,int n) {
    if(off<0 || n<0 || (u32)off>o->size || (u32)n>o->size-(u32)off){seterr(0x501);return 0;}
    if(o->mapped){seterr(0x502);return 0;}return 1;
}
static void API w_Sub(u32 t,int off,int n,const void *p) {
    enter();glb_object_t *o=bound(t,NULL);if(!o || !range(o,off,n))return;
    subdata(t,off,n,p);checked();if(n)memcpy(o->bytes+off,p,(u32)n);
}
static void API w_GetSub(u32 t,int off,int n,void *p) {
    enter();glb_object_t *o=bound(t,NULL);if(!o || !range(o,off,n))return;
    if(n)memcpy(p,o->bytes+off,(u32)n);
}
static void *API w_Map(u32 t,u32 access) {
    enter();u32 n;glb_object_t *o=bound(t,&n);if(!o)return NULL;
    if(access<READ_ONLY || access>READ_WRITE){seterr(0x500);return NULL;}
    if(o->mapped){seterr(0x502);return NULL;}
    void *p=map(t,access);u32 e=error();if(e){seterr(e);return NULL;}
    if(!p)return NULL;
    if(!o->size || !o->bytes)die("driver mapped empty storage");
    driver_map[n]=p;o->mapped=1;o->access=access;return o->bytes;
}
static u8 API w_Unmap(u32 t) {
    enter();u32 n;glb_object_t *o=bound(t,&n);if(!o)return 0;
    if(!o->mapped){seterr(0x502);return 0;}
    if(!driver_map[n])die("missing host map");
    if(o->access!=READ_ONLY)memcpy(driver_map[n],o->bytes,o->size);
    u8 ok=unmap(t);checked();driver_map[n]=NULL;o->mapped=0;
    if(!ok)die("driver reported corrupted mapped storage");return ok;
}
static void API w_Param(u32 t,u32 p,int *out) {
    enter();glb_object_t *o=bound(t,NULL);if(!o)return;
    switch(p){case 0x8764:*out=(int)o->size;break;case 0x8765:*out=(int)o->usage;break;
    case 0x88bb:*out=(int)o->access;break;case 0x88bc:*out=(int)o->mapped;break;default:seterr(0x500);}
}
static void API w_Pointer(u32 t,u32 p,void **out) {
    enter();glb_object_t *o=bound(t,NULL);if(!o)return;
    if(p!=0x88bd){seterr(0x500);return;}*out=o->mapped?o->bytes:NULL;
}
static void (API *attrib_i)(u32,u32,int *);
static void (API *attrib_f)(u32,u32,float *);
static void (API *attrib_d)(u32,u32,double *);
static int attrib_binding(u32 index,u32 *value) {
    enter();
    if(!attrib_i) {
        void *p=resolve("glGetVertexAttribivARB");
        if((uintptr_t)p<=3 || (uintptr_t)p==0xffffffffu)die("attribute query API missing");
        attrib_i=reinterpret_cast<decltype(attrib_i)>(p);
    }
    int native=0;attrib_i(index,0x889f,&native);
    u32 e=error();if(e){seterr(e);return 0;}
    *value=glbuffers_logical_name((u32)native);return 1;
}
static void API query_attrib_i(u32 index,u32 pname,int *out) {
    if(pname!=0x889f){attrib_i(index,pname,out);return;}
    u32 value;if(attrib_binding(index,&value))*out=(int)value;
}
static void API query_attrib_f(u32 index,u32 pname,float *out) {
    if(pname!=0x889f){attrib_f(index,pname,out);return;}
    u32 value;if(attrib_binding(index,&value))*out=(float)value;
}
static void API query_attrib_d(u32 index,u32 pname,double *out) {
    if(pname!=0x889f){attrib_d(index,pname,out);return;}
    u32 value;if(attrib_binding(index,&value))*out=(double)value;
}
static void API unsupported(void) { die("buffer entrypoint outside ARB VBO contract"); }
void *glbuffers_proc(const char *name,void *real) {
    if(g_bnd_native)return NULL;
    if(k_streq(name,"glGetVertexAttribiv") || k_streq(name,"glGetVertexAttribivARB")){attrib_i=reinterpret_cast<decltype(attrib_i)>(real);return reinterpret_cast<void *>(query_attrib_i);}
    if(k_streq(name,"glGetVertexAttribfv") || k_streq(name,"glGetVertexAttribfvARB")){attrib_f=reinterpret_cast<decltype(attrib_f)>(real);return reinterpret_cast<void *>(query_attrib_f);}
    if(k_streq(name,"glGetVertexAttribdv") || k_streq(name,"glGetVertexAttribdvARB")){attrib_d=reinterpret_cast<decltype(attrib_d)>(real);return reinterpret_cast<void *>(query_attrib_d);}
    static const struct {const char *name;void *fn;} api[]={
        {"glGenBuffers",reinterpret_cast<void *>(w_Gen)},{"glBindBuffer",reinterpret_cast<void *>(w_Bind)},{"glDeleteBuffers",reinterpret_cast<void *>(w_Delete)},
        {"glIsBuffer",reinterpret_cast<void *>(w_Is)},{"glBufferData",reinterpret_cast<void *>(w_Data)},{"glBufferSubData",reinterpret_cast<void *>(w_Sub)},
        {"glGetBufferSubData",reinterpret_cast<void *>(w_GetSub)},{"glMapBuffer",reinterpret_cast<void *>(w_Map)},{"glUnmapBuffer",reinterpret_cast<void *>(w_Unmap)},
        {"glGetBufferParameteriv",reinterpret_cast<void *>(w_Param)},{"glGetBufferPointerv",reinterpret_cast<void *>(w_Pointer)}};
    for(u32 i=0;i<sizeof api/sizeof api[0];i++) {
        u32 n=(u32)strlen(api[i].name);
        if(strlen(name)>=n && !memcmp(name,api[i].name,n) && (!name[n] || k_streq(name+n,"ARB")))return api[i].fn;
    }
    static const char *extra[]={"glMapBufferRange","glFlushMappedBufferRange","glBufferStorage",
        "glCopyBufferSubData","glBindBufferBase","glBindBufferRange","glGetBufferParameteri64v",
        "glClearBufferData","glClearBufferSubData","glInvalidateBufferData","glInvalidateBufferSubData",
        "glMakeBufferResidentNV","glMakeBufferNonResidentNV","glGetBufferParameterui64vNV",NULL};
    for(u32 i=0;extra[i];i++) {
        u32 n=(u32)strlen(extra[i]);
        if(strlen(name)>=n && !memcmp(name,extra[i],n) &&
           (!name[n] || k_streq(name+n,"ARB") || k_streq(name+n,"EXT")))return (void *)unsupported;
    }
    const char *prefix[]={"glNamedBuffer","glGetNamedBuffer","glMapNamedBuffer","glUnmapNamedBuffer",
        "glFlushMappedNamedBuffer","glCopyNamedBuffer","glClearNamedBuffer","glVertexArray",NULL};
    for(u32 i=0;prefix[i];i++) {
        u32 n=(u32)strlen(prefix[i]);if(strlen(name)>=n && !memcmp(name,prefix[i],n))return (void *)unsupported;
    }
    (void)real;return NULL;
}
void glbuffers_capture(void) {
    if(!S.used)return;
    if(S.context!=SH->wh.current_gl)die("capture in different buffer context");
    int depth=0;getint(0x0bb1,&depth);
    if(error() || depth)die("client attribute stack at capture requires stack reconstruction");
    int vao=0;getint(0x85b5,&vao);u32 ve=error();
    if((ve && ve!=0x500) || vao)die("vertex array objects outside captured subset");
    /* glsync currently reconstructs vertex/normal/color/texture/generic arrays.
     * Refuse buffer-backed legacy arrays until their full descriptors are added. */
    const u32 unsupported_arrays[]={0x8899,0x889b,0x889c,0x889d,0x889e};
    for(u32 i=0;i<sizeof unsupported_arrays/sizeof unsupported_arrays[0];i++) {
        int n=0;getint(unsupported_arrays[i],&n);
        u32 e=error();if(e==0x500)continue; /* absent legacy extension */
        if(e || n)die("legacy buffer array descriptor outside captured subset");
    }
    /* Writes through stable mappings already land in captured arena pages.
     * Host mapped storage is never consumed for hashing or simulation. */
}
void glbuffers_real_context_reset(void) {
    attrib_i=NULL;attrib_f=NULL;attrib_d=NULL;
    memset(driver_name,0,sizeof driver_name);driver_context=0;
    memset(driver_live,0,sizeof driver_live);
    memset(driver_map,0,sizeof driver_map);
    p_bind=NULL; /* resolve entrypoints against newly current context */
}
/* Validate the complete captured backing before any driver upload/readback.
 * Checking its first address alone permits a span across an arena gap or a
 * decommitted/guarded region. Execute-only pages are not readable backing. */
static int readable_arena(const void *data,u32 size) {
    uintptr_t p=(uintptr_t)data,end=p+size;
    if(!data || !size || end<p)return 0;
    while(p<end) {
        MEMORY_BASIC_INFORMATION m;
        if(!VirtualQuery((void *)p,&m,sizeof m) || m.State!=MEM_COMMIT ||
           (m.Protect&(PAGE_GUARD|PAGE_NOACCESS)))return 0;
        u32 prot=m.Protect&0xffu;
        if(prot!=PAGE_READONLY && prot!=PAGE_READWRITE && prot!=PAGE_WRITECOPY &&
           prot!=PAGE_EXECUTE_READ && prot!=PAGE_EXECUTE_READWRITE && prot!=PAGE_EXECUTE_WRITECOPY)return 0;
        uintptr_t next=(uintptr_t)m.BaseAddress+m.RegionSize;
        if(next<=p)return 0;
        if(next>end)next=end;
        if(!kst_in_arena((void *)p) || !kst_in_arena((void *)(next-1)))return 0;
        p=next;
    }
    return 1;
}
static int metadata_valid(void) {
    if(S.used>1 || S.next>GLB_CAP || (!S.used && (S.context || S.next)))return 0;
    if(S.used && (!S.context || S.context!=SH->wh.current_gl))return 0;
    for(u32 i=0;i<GLB_CAP;i++) {
        const glb_object_t *o=&S.object[i];
        if(o->reserved>1 || o->live>1 || o->mapped>1)return 0;
        if(!i || !o->live) {
            if((!i && o->reserved) || o->live || o->size || o->usage || o->access || o->mapped || o->bytes)return 0;
            if(o->reserved && !S.used)return 0;
            continue;
        }
        if(!S.used || !o->reserved || !usage(o->usage) ||
           o->access<READ_ONLY || o->access>READ_WRITE || o->size>0x7fffffffu)return 0;
        if(o->size ? !readable_arena(o->bytes,o->size) : o->bytes!=NULL)return 0;
        if(o->mapped && !o->size)return 0;
    }
    return 1;
}
int glbuffers_restore(void) {
    if(!metadata_valid()) { slog("GL buffer restore: invalid captured metadata/backing");return 0; }
    int any=S.used;for(u32 i=1;i<GLB_CAP;i++)any|=driver_name[i];
    if(!any)return 1;
    if(!resolve_all() || (S.used && S.context!=SH->wh.current_gl))return 0;
    if(driver_context && driver_context!=SH->wh.current_gl)return 0;
    mapping_context();
    error(); /* discard abandoned-future host error; SH already holds target error */
    for(u32 i=1;i<GLB_CAP;i++) {
        if(driver_name[i])del(1,&driver_name[i]);
        driver_name[i]=0;driver_live[i]=0;driver_map[i]=NULL;
    }
    if(error())return 0;
    for(u32 i=1;i<GLB_CAP;i++) {
        glb_object_t *o=&S.object[i];if(!o->reserved)continue;
        u32 native=reserve_native(i);if(!o->live)continue;
        p_bind(ARRAY,native);data(ARRAY,(int)o->size,o->bytes,o->usage);
        if(error())return 0;driver_live[i]=1;
        u8 check[4096];
        for(u32 off=0;off<o->size;) {
            u32 count=o->size-off;if(count>sizeof check)count=sizeof check;
            getsubdata(ARRAY,(int)off,(int)count,check);
            if(error() || memcmp(check,o->bytes+off,count))return 0;
            off+=count;
        }
        if(o->mapped) {
            driver_map[i]=map(ARRAY,o->access);
            if(!driver_map[i] || error())return 0;
        }
    }
    /* glsync context replay restores ARRAY/ELEMENT and all per-array bindings. */
    p_bind(ARRAY,0);p_bind(ELEMENT,0);return error()==0;
}

} /* extern "C" */
