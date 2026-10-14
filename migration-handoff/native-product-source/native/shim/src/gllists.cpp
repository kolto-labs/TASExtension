/* The exercised K1 font path compiles empty or single-bitmap lists. Its pixel
 * source is copied under the current unpack rules, not retained as a pointer.
 * Unsupported commands during list compilation terminate before execution. */
#include "shim.hpp"
#include "gllists.hpp"

/* Fixed C hook ABI and assembly-referenced symbol names. */
extern "C" {
#define API __attribute__((stdcall))
#define L (SH->gll)
static void (API *r_new)(u32,u32);
static void (API *r_end)(void);
static void (API *r_delete)(u32,int);
static void (API *r_bitmap)(int,int,float,float,float,float,const u8 *);
static void (API *r_get)(u32,int *);
static void (API *r_store)(u32,int);
static u32 (API *r_error)(void);
static u8 (API *r_is)(u32);
static u8 real_live[GLL_CAP];
static void die(const char *why) {
    slog("GL list boundary: %s",why);TerminateProcess(GetCurrentProcess(),6);for(;;){}
}
static void err(u32 e) {if(!SH->gl_err)SH->gl_err=e;}
static int resolve(void) {
    HMODULE gl=GetModuleHandleA("opengl32.dll");
#define R(v,n) do{v=reinterpret_cast<decltype(v)>(GetProcAddress(gl,n));if(!v)return 0;}while(0)
    R(r_new,"glNewList");R(r_end,"glEndList");R(r_delete,"glDeleteLists");R(r_bitmap,"glBitmap");
    R(r_get,"glGetIntegerv");R(r_store,"glPixelStorei");R(r_error,"glGetError");R(r_is,"glIsList");
#undef R
    return 1;
}
static void enter(int allocate) {
    if(!r_new&&!resolve())die("missing core API");
    if(K->cur!=0 || !SH->wh.current_gl)die("untracked context/thread");
    if(L.context && L.context!=SH->wh.current_gl)die("multiple list contexts");
    if(allocate && !L.definitions) {
        L.definitions=static_cast<gll_definition_t *>(kst_valloc(sizeof(gll_definition_t)*GLL_CAP));
        if(!L.definitions)die("definition storage exhausted");
        L.context=SH->wh.current_gl;
    }
    u32 e=r_error();if(e)err(e);
}
static void checked(void) {if(r_error())die("driver rejected recorded command");}
void gllists_guard(u32 allowed) {if(L.active&&!allowed)die("unsupported command in list definition");}
u32 gllists_allowed(const char *n) {
    return k_streq(n,"glBitmap")||k_streq(n,"glEndList")||k_streq(n,"glNewList")||
        (strlen(n)>=5&&!memcmp(n,"glGet",5))||(strlen(n)>=4&&!memcmp(n,"glIs",4))||
        k_streq(n,"glPixelStorei")||k_streq(n,"glPixelStoref");
}
static u32 API w_gen(int range) {
    enter(1);if(range<0){err(0x501);return 0;}if(!range)return 0;
    u32 first=SH->gl_list_next;
    if(!first || first>=GLL_CAP)first=1;
    u32 wrapped=first==1;
    for(;;){
        if(first>=GLL_CAP || (u32)range>GLL_CAP-first) {
            if(wrapped)die("list namespace exhausted");
            first=1;wrapped=1;continue;
        }
        u32 i;for(i=0;i<(u32)range;i++)if(L.definitions[first+i].live)break;
        if(i==(u32)range)break;first+=i+1;
    }
    /* glGenLists creates empty lists, unlike glGenBuffers' reserved names. */
    for(u32 i=first;i<first+(u32)range;i++) {
        r_delete(i,1);r_new(i,0x1300);r_end();checked();
        L.definitions[i].live=1;real_live[i]=1;
    }
    SH->gl_list_next=first+(u32)range;return first;
}
static void API w_delete(u32 first,int range) {
    enter(0);if(range<0){err(0x501);return;}
    r_delete(first,range);checked();
    for(u32 i=first;i<GLL_CAP && i-first<(u32)range;i++) {
        if(L.definitions){gll_definition_t *d=&L.definitions[i];if(d->pixels)kst_vfree(d->pixels);memset(d,0,sizeof *d);}
        real_live[i]=0;
        if(i && (!SH->gl_list_next || i<SH->gl_list_next))SH->gl_list_next=i;
    }
}
static u8 API w_is(u32 name) {enter(0);return name<GLL_CAP && L.definitions && L.definitions[name].live;}
static void API w_new(u32 name,u32 mode) {
    enter(1);if(!name){err(0x501);return;}if(L.active){err(0x502);return;}
    if(mode!=0x1300){if(mode==0x1301)die("GL_COMPILE_AND_EXECUTE requires expanded list grammar");err(0x500);return;}
    if(name>=GLL_CAP)die("list name outside captured namespace");
    r_new(name,mode);checked();L.active=name;memset(&L.pending,0,sizeof L.pending);L.pending.live=1;
}
static void API w_bitmap(int w,int h,float xo,float yo,float xm,float ym,const u8 *pixels) {
    enter(0);
    if(!L.active){r_bitmap(w,h,xo,yo,xm,ym,pixels);return;}
    if(w<0||h<0){err(0x501);return;}
    if(L.pending.bitmap)die("multiple bitmap commands require expanded list grammar");
    gll_definition_t *d=&L.pending;d->bitmap=1;d->width=(u32)w;d->height=(u32)h;
    d->xorigin=xo;d->yorigin=yo;d->xmove=xm;d->ymove=ym;
    if(pixels && w && h) {
        int a=0,row=0,skiprow=0,skip=0,lsb=0,pbo=0;
        r_get(0x0cf5,&a);r_get(0x0cf2,&row);r_get(0x0cf3,&skiprow);r_get(0x0cf4,&skip);
        r_get(0x0cf1,&lsb);r_get(0x88ef,&pbo);checked();
        if(pbo)die("pixel-unpack buffers require a bitmap source boundary");
        if(a!=1&&a!=2&&a!=4&&a!=8)die("invalid unpack alignment");
        if(row<0||skiprow<0||skip<0)die("invalid unpack geometry");
        u64 pitch=(((u64)(row?row:w)+7)/8+(u32)a-1)&~((u64)(u32)a-1);
        u64 stride=((u64)(u32)w+7)/8,size=stride*(u32)h;
        if(size>0x7fffffffu || (u64)skiprow*pitch+(u64)(u32)h*pitch+((u64)skip+w+7)/8>0xffffffffu)
            die("bitmap source extent overflow");
        d->bytes=(u32)size;d->pixels=static_cast<u8 *>(kst_valloc(d->bytes));if(!d->pixels)die("bitmap allocation failed");
        memset(d->pixels,0,d->bytes);
        for(u32 y=0;y<(u32)h;y++)for(u32 x=0;x<(u32)w;x++) {
            u32 bit=(u32)skip+x;
            u8 v=pixels[((u64)(u32)skiprow+y)*pitch+bit/8];
            if(v&(1u<<(lsb?(bit%8):(7-bit%8))))d->pixels[y*(u32)stride+x/8]|=(u8)(1u<<(7-x%8));
        }
    }
    r_bitmap(w,h,xo,yo,xm,ym,pixels);checked();
}
static void API w_end(void) {
    enter(0);if(!L.active){err(0x502);return;}
    r_end();checked();gll_definition_t *d=&L.definitions[L.active];
    if(d->pixels)kst_vfree(d->pixels);*d=L.pending;real_live[L.active]=1;
    memset(&L.pending,0,sizeof L.pending);L.active=0;
}
/* The tracked GL_COMPILE state: w_new sets active only after the driver
 * accepted glNewList (checked() dies otherwise), w_end clears it after glEndList;
 * COMPILE_AND_EXECUTE dies and rejected enums/nesting never reach the driver.
 * The w_gen and restore glNewList/glEndList pairs are closed internal sequences.
 * Valid only when not g_bnd_native (no wrappers are installed then). */
int gllists_compiling(void) {return L.active!=0;}
void *gllists_proc(const char *n,void *real) {
    (void)real;if(g_bnd_native)return NULL;
    if(k_streq(n,"glGenLists"))return reinterpret_cast<void *>(w_gen);if(k_streq(n,"glDeleteLists"))return reinterpret_cast<void *>(w_delete);
    if(k_streq(n,"glNewList"))return reinterpret_cast<void *>(w_new);if(k_streq(n,"glEndList"))return reinterpret_cast<void *>(w_end);
    if(k_streq(n,"glBitmap"))return reinterpret_cast<void *>(w_bitmap);if(k_streq(n,"glIsList"))return reinterpret_cast<void *>(w_is);return NULL;
}
static int readable_arena(const void *data,u32 size) {
    uintptr_t p=(uintptr_t)data,end=p+size;
    if(!data || end<p || !kst_in_arena(data) || (size&&!kst_in_arena((void *)(end-1))))return 0;
    while(p<end) {
        MEMORY_BASIC_INFORMATION m;
        if(!VirtualQuery((void *)p,&m,sizeof m) || m.State!=MEM_COMMIT ||
           (m.Protect&(PAGE_GUARD|PAGE_NOACCESS)))return 0;
        u32 prot=m.Protect&0xffu;
        if(prot!=PAGE_READONLY && prot!=PAGE_READWRITE && prot!=PAGE_WRITECOPY &&
           prot!=PAGE_EXECUTE_READ && prot!=PAGE_EXECUTE_READWRITE && prot!=PAGE_EXECUTE_WRITECOPY)return 0;
        uintptr_t next=(uintptr_t)m.BaseAddress+m.RegionSize;
        if(next<=p)return 0;if(next>end)next=end;
        if(!kst_in_arena((void *)p) || !kst_in_arena((void *)(next-1)))return 0;
        p=next;
    }
    return 1;
}
int gllists_restorable(void) {
    if(L.active || SH->gl_list_next>GLL_CAP)return 0;
    if(!L.definitions)return SH->gl_list_next<=1;
    if(L.context!=SH->wh.current_gl || !readable_arena(L.definitions,sizeof(gll_definition_t)*GLL_CAP))return 0;
    for(u32 i=1;i<GLL_CAP;i++) {
        const gll_definition_t *d=&L.definitions[i];
        if(d->live>1 || d->bitmap>1)return 0;
        if(!d->live)continue;
        u64 expected=(((u64)d->width+7)/8)*d->height;
        if(d->width>0x7fffffffu || d->height>0x7fffffffu || expected>0x7fffffffu ||
           (d->pixels && (!d->bitmap || d->bytes!=expected || !readable_arena(d->pixels,d->bytes))) ||
           (!d->pixels && d->bytes))return 0;
    }
    return 1;
}
void gllists_capture(void) {if(!gllists_restorable())die("incomplete display-list capture");}
void gllists_real_context_reset(void) {memset(real_live,0,sizeof real_live);r_new=NULL;}
int gllists_restore(void) {
    int any=L.definitions!=NULL;for(u32 i=1;i<GLL_CAP;i++)any|=real_live[i];if(!any)return 1;
    if(!gllists_restorable()||!resolve() || (L.context&&L.context!=SH->wh.current_gl))return 0;
    r_error();
    for(u32 i=1;i<GLL_CAP;i++)if(real_live[i]){r_delete(i,1);real_live[i]=0;}
    const u32 param[]={0x0cf5,0x0cf2,0x0cf3,0x0cf4,0x0cf1};int saved[5];
    for(u32 i=0;i<5;i++){r_get(param[i],&saved[i]);r_store(param[i],i?0:1);}
    int ok=r_error()==0;
    if(L.definitions)for(u32 i=1;i<GLL_CAP && ok;i++) {
        gll_definition_t *d=&L.definitions[i];if(!d->live)continue;
        u64 expected=(((u64)d->width+7)/8)*d->height;
        if(d->width>0x7fffffffu||d->height>0x7fffffffu||expected>0x7fffffffu ||
           (d->pixels && (!d->bitmap || d->bytes!=expected)) || (!d->pixels && d->bytes)) {ok=0;break;}
        r_new(i,0x1300);
        if(d->bitmap)r_bitmap((int)d->width,(int)d->height,d->xorigin,d->yorigin,d->xmove,d->ymove,d->pixels);
        r_end();if(r_error()||!r_is(i))ok=0;else real_live[i]=1;
    }
    for(u32 i=0;i<5;i++)r_store(param[i],saved[i]);return ok&&r_error()==0;
}

/* ABI-transparent precheck followed by the existing hook/driver function. */
void gllists_guard_code(u8 c[32],void **target,u32 *allowed) {
    memset(c,0xcc,32);c[0]=0x9c;c[1]=0x60;c[2]=0xff;c[3]=0x35;
    u32 p=(u32)(uintptr_t)allowed;memcpy(c+4,&p,4);
    c[8]=0xe8;p=(u32)(uintptr_t)gllists_guard-((u32)(uintptr_t)c+13);memcpy(c+9,&p,4);
    c[13]=0x83;c[14]=0xc4;c[15]=4;c[16]=0x61;c[17]=0x9d;c[18]=0xff;c[19]=0x25;
    p=(u32)(uintptr_t)target;memcpy(c+20,&p,4);
}
#define IAT_CAP 256
static u8 iat_code[IAT_CAP][32] __attribute__((aligned(4096)));
static void *iat_target[IAT_CAP];static u32 iat_allowed[IAT_CAP];
static const char *iat_name[IAT_CAP];static u32 iat_count;
void *gllists_import_target(const char *name) {
    for(u32 i=0;i<iat_count;i++)if(k_streq(name,iat_name[i]))return iat_code[i];
    return NULL;
}
void gllists_install(void) {
    if(g_bnd_native)return;
    if(!resolve())die("core GL resolver unavailable");
    u8 *base=(u8 *)GetModuleHandleA(NULL);IMAGE_DOS_HEADER *dos=reinterpret_cast<IMAGE_DOS_HEADER *>(base);
    IMAGE_NT_HEADERS *nt=reinterpret_cast<IMAGE_NT_HEADERS *>((base+dos->e_lfanew));
    IMAGE_IMPORT_DESCRIPTOR *imp=reinterpret_cast<IMAGE_IMPORT_DESCRIPTOR *>((base+nt->OptionalHeader.DataDirectory[IMAGE_DIRECTORY_ENTRY_IMPORT].VirtualAddress));
    DWORD old;if(!VirtualProtect(iat_code,sizeof iat_code,PAGE_EXECUTE_READWRITE,&old))die("guard memory protection");
    u32 count=0;
    for(;imp->Name;imp++)if(k_strieq((char *)(base+imp->Name),"OPENGL32.dll")) {
        /* Installation follows other hooks, so FirstThunk already contains
         * callable addresses and cannot stand in for an absent name table. */
        if(!imp->OriginalFirstThunk)die("GL imports lack a preserved name table");
        IMAGE_THUNK_DATA *names=reinterpret_cast<IMAGE_THUNK_DATA *>((base+imp->OriginalFirstThunk));
        IMAGE_THUNK_DATA *slots=reinterpret_cast<IMAGE_THUNK_DATA *>((base+imp->FirstThunk));
        for(;names->u1.AddressOfData;names++,slots++) {
            if(names->u1.Ordinal&IMAGE_ORDINAL_FLAG32)die("ordinal GL import lacks command classification");
            IMAGE_IMPORT_BY_NAME *in=reinterpret_cast<IMAGE_IMPORT_BY_NAME *>((base+names->u1.AddressOfData));const char *name=(const char *)in->Name;
            if(count==IAT_CAP)die("GL IAT guard capacity");
            void *target=(void *)(uintptr_t)slots->u1.Function,*replacement=gllists_proc(name,target);
            iat_target[count]=replacement?replacement:target;iat_allowed[count]=gllists_allowed(name);
            iat_name[count]=name;
            gllists_guard_code(iat_code[count],&iat_target[count],&iat_allowed[count]);
            if(!iat_set((u32 *)&slots->u1.Function,iat_code[count]))die("GL IAT guard installation");count++;
        }
    }
    iat_count=count;
    VirtualProtect(iat_code,sizeof iat_code,PAGE_EXECUTE_READ,&old);FlushInstructionCache(GetCurrentProcess(),iat_code,sizeof iat_code);
}

} /* extern "C" */
