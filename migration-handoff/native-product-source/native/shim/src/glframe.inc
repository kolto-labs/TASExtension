/* Default drawable pixels are external state too. KPGF v1 uses the same
 * content-addressed descriptors as textures. Included by glsync.cpp. */
#define DESC_MAGIC_FB 0x4647504bu
static int fb_ready;
#define FB_API(X) \
 X(void,glPushAttrib,(GLuint_)) X(void,glPopAttrib,(void)) \
 X(void,glReadPixels,(GLint_,GLint_,GLsizei_,GLsizei_,GLenum_,GLenum_,void*)) \
 X(void,glDrawPixels,(GLsizei_,GLsizei_,GLenum_,GLenum_,const void*)) \
 X(void,glReadBuffer,(GLenum_)) X(void,glDrawBuffer,(GLenum_)) \
 X(void,glPixelTransferf,(GLenum_,GLfloat_)) X(void,glPixelZoom,(GLfloat_,GLfloat_)) \
 X(void,glFlush,(void)) X(void,glFinish,(void)) X(void,glGenTextures,(GLsizei_,GLuint_*))
#define FB_DECL(r,n,a) static r (GLAPI *fb_##n) a;
FB_API(FB_DECL)
static void (GLAPI *fb_windowpos)(GLint_,GLint_);
static int fb_resolve(void) {
    if(fb_ready)return 1;
    HMODULE m=GetModuleHandleA("opengl32.dll");if(!m)return 0;
#define FB_LOAD(r,n,a) if(!(fb_##n=reinterpret_cast<decltype(fb_##n)>(GetProcAddress(m,#n))))return 0;
    FB_API(FB_LOAD)
    void *(GLAPI *gp)(const char*)=reinterpret_cast<decltype(gp)>(GetProcAddress(m,"wglGetProcAddress"));
    if(!gp)return 0;fb_windowpos=reinterpret_cast<decltype(fb_windowpos)>(gp("glWindowPos2iARB"));
    if((uintptr_t)fb_windowpos<=3||(uintptr_t)fb_windowpos==0xffffffffu)return 0;
    fb_ready=1;return 1;
}
/* KPGF v1 has integer RGBA8/depth-u32 planes. Prove the drawable is
 * fixed-point rather than hashing a quantized view of a floating format. */
static int fb_pixel_format(HDC dc) {
    int pf=GetPixelFormat(dc);PIXELFORMATDESCRIPTOR pfd;
    if(!pf||!DescribePixelFormat(dc,pf,sizeof pfd,&pfd)||pfd.iPixelType!=PFD_TYPE_RGBA)return 0;
    void *(GLAPI *gp)(const char*)=reinterpret_cast<decltype(gp)>(GetProcAddress(GetModuleHandleA("opengl32.dll"),"wglGetProcAddress"));
    if(!gp)return 0;
    const char *(WINAPI *extensions)(HDC)=reinterpret_cast<decltype(extensions)>(gp("wglGetExtensionsStringARB"));
    BOOL (WINAPI *query)(HDC,int,int,UINT,const int*,int*)=reinterpret_cast<decltype(query)>(gp("wglGetPixelFormatAttribivARB"));
    if((uintptr_t)extensions<=3||(uintptr_t)extensions==0xffffffffu||
       (uintptr_t)query<=3||(uintptr_t)query==0xffffffffu)return 0;
    const char *ext=extensions(dc);if(!ext||!k_strstr(ext,"WGL_ARB_pixel_format"))return 0;
    int attribute=0x2013,value=0; /* WGL_PIXEL_TYPE_ARB / WGL_TYPE_RGBA_ARB */
    if(!query(dc,pf,0,1,&attribute,&value)||value!=0x202b)return 0;
    if(k_strstr(ext,"WGL_EXT_depth_float")) {
        BOOL (WINAPI *query_ext)(HDC,int,int,UINT,const int*,int*)=reinterpret_cast<decltype(query_ext)>(gp("wglGetPixelFormatAttribivEXT"));
        if((uintptr_t)query_ext<=3||(uintptr_t)query_ext==0xffffffffu)return 0;
        attribute=0x2040;value=0; /* WGL_DEPTH_FLOAT_EXT */
        if(!query_ext(dc,pf,0,1,&attribute,&value)||value)return 0;
    }
    return 1;
}
static int fb_dimensions(u32 dims[4]) {
    HDC (WINAPI *dcfn)(void)=reinterpret_cast<decltype(dcfn)>(GetProcAddress(GetModuleHandleA("opengl32.dll"),"wglGetCurrentDC"));
    if(!dcfn)return 0;HDC dc=dcfn();HWND wnd=WindowFromDC(dc);RECT rc;
    if(!fb_pixel_format(dc))return 0;
    if(!wnd||!GetClientRect(wnd,&rc)||rc.right<=0||rc.bottom<=0)return 0;
    GLint_ rgba=0,color[4]={0};
    p_glGetIntegerv(0x0c31,&rgba);if(!rgba)return 0; /* GL_RGBA_MODE */
    for(u32 i=0;i<4;i++) {
        p_glGetIntegerv(0x0d52+i,&color[i]); /* RED/GREEN/BLUE/ALPHA_BITS */
        if(color[i]<(i==3?0:1)||color[i]>8)return 0;
    }
    GLint_ db=0,stereo=0,samples=0,depth=0,stencil=0,aux=0;
    p_glGetIntegerv(0x0c32,&db);p_glGetIntegerv(0x0c33,&stereo);
    p_glGetIntegerv(0x80a8,&samples);p_glGetIntegerv(0x0d56,&depth);
    p_glGetIntegerv(0x0d57,&stencil);p_glGetIntegerv(0x0c00,&aux);
    if(!db||stereo||samples||aux||depth!=24||stencil!=8)return 0;
    /* GLSL current-program state is not represented by the existing context
     * schema. Refuse it rather than running pixel restoration through a shader. */
    const char *version=(const char*)p_glGetString(0x1f02);
    if(version&&version[0]>='2'&&version[0]<='9') {
        GLint_ program=0;p_glGetIntegerv(0x8b8d,&program);if(program)return 0;
    }
    const char *ext=(const char*)p_glGetString(0x1f03);
    if(ext&&(k_strstr(ext,"GL_EXT_framebuffer_object")||k_strstr(ext,"GL_ARB_framebuffer_object"))) {
        GLint_ binding=0;p_glGetIntegerv(0x8ca6,&binding);if(binding)return 0;
        if(k_strstr(ext,"GL_ARB_framebuffer_object")||k_strstr(ext,"GL_EXT_framebuffer_blit")) {
            p_glGetIntegerv(0x8caa,&binding);if(binding)return 0;
        }
    }
    if((u64)(u32)rc.right*(u32)rc.bottom*4>0x7fffffffu)return 0;
    dims[0]=rc.right;dims[1]=rc.bottom;dims[2]=depth;dims[3]=stencil;return 1;
}
/* A failed push must never be paired with pop: that would consume the
 * game's own attribute stack. Reject prior GL errors before issuing the push
 * so its immediate result cannot be confused with an earlier failure. */
static int fb_push_attrib(void) {
    if(p_glGetError())return 0;
    GLint_ depth=0,limit=0;
    p_glGetIntegerv(0x0bb0,&depth); /* GL_ATTRIB_STACK_DEPTH */
    p_glGetIntegerv(0x0d35,&limit); /* GL_MAX_ATTRIB_STACK_DEPTH */
    if(p_glGetError()||depth<0||limit<=0||depth>=limit)return 0;
    fb_glPushAttrib(0x000fffffu);
    return p_glGetError()==0;
}
static void fb_transfer_identity(void) {
    static const GLenum_ scale[]={0x0d14,0x0d18,0x0d1a,0x0d1c,0x0d1e};
    static const GLenum_ zero[]={0x0d10,0x0d11,0x0d12,0x0d13,0x0d15,0x0d19,0x0d1b,0x0d1d,0x0d1f};
    for(u32 i=0;i<sizeof scale/4;i++)fb_glPixelTransferf(scale[i],1);
    for(u32 i=0;i<sizeof zero/4;i++)fb_glPixelTransferf(zero[i],0);
    fb_glPixelZoom(1,1);
}
static u32 fb_plane_size(const u32 d[4],u32 plane) {return d[0]*d[1]*(plane==3?1:4);}
static GLenum_ fb_format(u32 plane) {return plane<2?0x1908:plane==2?0x1902:0x1901;}
static GLenum_ fb_type(u32 plane) {return plane==2?0x1405:0x1401;}
/* A digest memo is only an acceleration of exact capture hashing, never a
 * claim about what the driver holds. Every capture still reads every plane.
 * Restore verification uses the original independent SHA path below.
 * Storage is host-owned, outside SH: rewinding pages cannot rewind this memo,
 * but matching every newly read byte makes that harmless. Context resets
 * invalidate even reused WGL handles. Limit optional storage to 64 MiB total;
 * larger planes and allocation failures retain the original capture path. */
static constexpr u32 FB_MEMO_PLANE_LIMIT=16u*1024u*1024u;
struct fb_hash_memo_t {
    u8 *bytes;
    u32 capacity,size,dims[4];
    GLenum_ format,type;
    HGLRC context;
    HDC drawable;
    u8 key[32];
    int valid,allocation_failed;
};
static fb_hash_memo_t fb_hash_memo[4];
static HGLRC (WINAPI *fb_memo_context)(void);
static HDC (WINAPI *fb_memo_drawable)(void);
static void fb_hash_memo_reset(void) {
    for(u32 i=0;i<4;i++) {
        fb_hash_memo[i].valid=0;
        fb_hash_memo[i].allocation_failed=0;
    }
}
/* Each plane has its own readback buffer, so the four SHA-256 digests can be
 * computed at once. Storage is host-owned (VirtualAlloc, like g_img), outside
 * SH and the arena; it grows to the drawable's plane size and is reused. g_img
 * remains the scratch of the packed depth/stencil restore below. A buffer is
 * only regrown on the GL thread while no hash job is outstanding. */
static u8 *fb_plane_buf[4];
static u32 fb_plane_cap[4];
static u8 *fb_plane_room(u32 plane,u32 need) {
    if(need<=fb_plane_cap[plane])return fb_plane_buf[plane];
    if(need>0xffff0000u)return 0;
    if(fb_plane_buf[plane])VirtualFree(fb_plane_buf[plane],0,MEM_RELEASE);
    fb_plane_cap[plane]=(need+0xffffu)&~0xffffu;
    fb_plane_buf[plane]=(u8*)VirtualAlloc(0,fb_plane_cap[plane],MEM_RESERVE|MEM_COMMIT,PAGE_READWRITE);
    if(!fb_plane_buf[plane])fb_plane_cap[plane]=0;
    return fb_plane_buf[plane];
}
/* One plane's hash work. A job reads only host-owned plane/memo buffers and
 * writes only its own result fields and (on a memo miss) its plane's memo
 * bytes. It never touches GL, SH, the arena, slog or the profile tables. */
struct fb_job_t {
    const u8 *bytes;        /* this capture's plane bytes */
    const u8 *memo_bytes;   /* nonzero: memo whose scalar fields already match */
    const u8 *front_bytes;  /* nonzero (back plane only): this capture's front bytes */
    u8 *store;              /* nonzero: memo storage to refresh on a miss */
    u32 n;
    int hit,same_front;
    u64 start,end;
    u8 key[32];
};
static fb_job_t fb_jobs[4];
static void fb_job_run(fb_job_t *j) {
    j->start=glsync_profile_now();
    j->hit=0;j->same_front=0;
    if(j->memo_bytes&&!memcmp(j->memo_bytes,j->bytes,j->n))j->hit=1;
    else {
        if(j->front_bytes&&!memcmp(j->front_bytes,j->bytes,j->n))j->same_front=1;
        else sha256(j->bytes,j->n,j->key);
        if(j->store)memcpy(j->store,j->bytes,j->n);
    }
    j->end=glsync_profile_now();
}
/* Shim-owned hash workers. They are created once, at the first framebuffer
 * capture, through kernel32 directly: the shim's own imports are not the exe
 * IAT that threads.cpp virtualizes, so they get no game slot, virtual TID or
 * virtual handle, and the slot scheduler, stack scrubbing and capture never
 * see them. Between jobs each worker blocks on its own auto-reset event. Every
 * dispatch is joined before its digest is consumed and before the capturing
 * function returns, so no worker runs during a restore or a later gate step.
 * Creation is all-or-nothing; any failure keeps the serial path for the life
 * of the process. */
#ifndef STACK_SIZE_PARAM_IS_A_RESERVATION
#define STACK_SIZE_PARAM_IS_A_RESERVATION 0x00010000
#endif
static constexpr u32 FB_WORKERS=3;
static constexpr u32 FB_WORKER_STACK=64u*1024u;
static HANDLE fb_pool_go[FB_WORKERS],fb_pool_done[FB_WORKERS],fb_pool_thread[FB_WORKERS];
static fb_job_t *volatile fb_pool_job[FB_WORKERS];
static int fb_pool_state;   /* 0 not tried, 1 workers ready, -1 serial */
static u32 fb_pool_busy;    /* dispatched and not yet joined, GL thread only */
static DWORD WINAPI fb_pool_main(LPVOID arg) {
    u32 w=(u32)(uintptr_t)arg;
    /* Ready receipt: loader thread attach has finished on this thread. */
    if(!SetEvent(fb_pool_done[w]))return 0;
    /* A failed wait ends the thread; the join observes the exit and refuses. */
    while(WaitForSingleObject(fb_pool_go[w],INFINITE)==WAIT_OBJECT_0) {
        fb_job_run(fb_pool_job[w]);
        if(!SetEvent(fb_pool_done[w]))break;
    }
    return 0;
}
struct fb_pool_create_t {const char *step;DWORD error;};
static void fb_pool_create(void *arg) {
    fb_pool_create_t *c=static_cast<fb_pool_create_t *>(arg);
    for(u32 w=0;w<FB_WORKERS;w++) {
        fb_pool_go[w]=CreateEventW(0,FALSE,FALSE,0);
        fb_pool_done[w]=CreateEventW(0,FALSE,FALSE,0);
        if(!fb_pool_go[w]||!fb_pool_done[w]){c->step="event";c->error=GetLastError();return;}
    }
    for(u32 w=0;w<FB_WORKERS;w++) {
        fb_pool_thread[w]=CreateThread(0,FB_WORKER_STACK,fb_pool_main,(void *)(uintptr_t)w,
                                       STACK_SIZE_PARAM_IS_A_RESERVATION,0);
        if(!fb_pool_thread[w]){c->step="thread";c->error=GetLastError();return;}
    }
    /* Wait until every worker has finished starting (its loader thread attach
     * included) before the capture continues, so no foreign attach code runs
     * concurrently with it, and a worker that dies while starting selects the
     * serial path instead of failing a later join. */
    for(u32 w=0;w<FB_WORKERS;w++) {
        HANDLE wait[2]={fb_pool_done[w],fb_pool_thread[w]};
        DWORD r=WaitForMultipleObjects(2,wait,FALSE,INFINITE);
        if(r!=WAIT_OBJECT_0){c->step="start";c->error=r==WAIT_FAILED?GetLastError():r;return;}
    }
}
static void fb_pool_init(void) {
    DWORD last_error=GetLastError();
    fb_pool_create_t c={0,0};
    /* Wine's thread-creation locals land on a harness stack, never a game stack. */
    kst_hcall(fb_pool_create,&c);
    /* Workers that did start stay parked on events nobody signals. */
    fb_pool_state=c.step?-1:1;
    if(c.step)slog("framebuffer: hash workers unavailable step=%s error=%u; hashing serially",c.step,c.error);
    else slog("framebuffer: %u hash workers started",FB_WORKERS);
    SetLastError(last_error);
}
static void fb_pool_run(u32 w,fb_job_t *j) {
    if(fb_pool_state!=1||w>=FB_WORKERS||(fb_pool_busy&(1u<<w))){fb_job_run(j);return;}
    fb_pool_job[w]=j;fb_pool_busy|=1u<<w;
    if(!SetEvent(fb_pool_go[w])){fb_pool_busy&=~(1u<<w);fb_job_run(j);}
}
static void fb_pool_join(void) {
    for(u32 w=0;w<FB_WORKERS;w++) {
        if(!(fb_pool_busy&(1u<<w)))continue;
        HANDLE wait[2]={fb_pool_done[w],fb_pool_thread[w]};
        if(WaitForMultipleObjects(2,wait,FALSE,INFINITE)!=WAIT_OBJECT_0)
            capture_abort("framebuffer hash worker join failed");
        fb_pool_busy&=~(1u<<w);
    }
}
static void fb_memo_current(HGLRC *context,HDC *drawable) {
    if(!fb_memo_context||!fb_memo_drawable) {
        HMODULE gl=GetModuleHandleA("opengl32.dll");
        if(gl) {
            fb_memo_context=reinterpret_cast<decltype(fb_memo_context)>(GetProcAddress(gl,"wglGetCurrentContext"));
            fb_memo_drawable=reinterpret_cast<decltype(fb_memo_drawable)>(GetProcAddress(gl,"wglGetCurrentDC"));
        }
    }
    *context=fb_memo_context?fb_memo_context():0;
    *drawable=fb_memo_drawable?fb_memo_drawable():0;
}
/* Before a plane is dispatched (GL thread): offer its memo for an exact byte
 * comparison only when every scalar field matches, and settle its memo
 * storage. Regrowth implies memo.size!=n, so that plane cannot hit and its
 * memo is invalidated now rather than after the miss. */
static void fb_memo_prepare(const u32 dims[4],u32 plane,HGLRC context,HDC drawable,fb_job_t *j) {
    fb_hash_memo_t &memo=fb_hash_memo[plane];
    u32 n=fb_plane_size(dims,plane);
    GLenum_ format=fb_format(plane),type=fb_type(plane);
    j->memo_bytes=0;j->front_bytes=0;j->store=0;j->n=n;
    if(context&&drawable&&memo.valid&&memo.context==context&&memo.drawable==drawable&&
       memo.size==n&&memo.format==format&&memo.type==type&&
       !memcmp(memo.dims,dims,sizeof memo.dims))
        j->memo_bytes=memo.bytes;
    if(context&&drawable&&n<=FB_MEMO_PLANE_LIMIT&&!memo.allocation_failed) {
        if(n>memo.capacity) {
            memo.valid=0;j->memo_bytes=0;
            if(memo.bytes&&!VirtualFree(memo.bytes,0,MEM_RELEASE)) {
                memo.allocation_failed=1;return;
            }
            memo.bytes=0;memo.capacity=0;
            memo.bytes=(u8*)VirtualAlloc(0,n,MEM_RESERVE|MEM_COMMIT,PAGE_READWRITE);
            if(memo.bytes)memo.capacity=n;
            else memo.allocation_failed=1;
        }
        if(memo.bytes&&memo.capacity>=n)j->store=memo.bytes;
    }
}
/* After the join, in plane order (GL thread): the same digest and memo
 * transitions as the former serial memo. framebuffer_capture finishes front
 * before back. A finished, valid front memo holds exactly this capture's front
 * bytes (a verified hit or a fresh copy), so the job's full comparison of back
 * against this capture's front buffer is the former comparison against the
 * front memo. If the front memo is unavailable or mismatched, hash the back
 * bytes here. No driver read or restore verification is skipped. */
static void fb_memo_finish(const u32 dims[4],u32 plane,HGLRC context,HDC drawable,const fb_job_t *j,u8 key[32]) {
    fb_hash_memo_t &memo=fb_hash_memo[plane];
    if(j->hit){memcpy(key,memo.key,32);return;}
    GLenum_ format=fb_format(plane),type=fb_type(plane);
    const fb_hash_memo_t &front=fb_hash_memo[0];
    if(j->same_front) {
        if(plane==1&&context&&drawable&&front.valid&&
           front.context==context&&front.drawable==drawable&&front.size==j->n&&
           front.format==format&&front.type==type&&
           !memcmp(front.dims,dims,sizeof front.dims))
            memcpy(key,front.key,32);
        else
            sha256(j->bytes,j->n,key);
    } else memcpy(key,j->key,32);
    memo.valid=0;
    if(j->store) {
        memcpy(memo.key,key,32);
        memcpy(memo.dims,dims,sizeof memo.dims);
        memo.size=j->n;memo.format=format;memo.type=type;
        memo.context=context;memo.drawable=drawable;memo.valid=1;
    }
}
static int fb_error(const char *stage,u32 plane,u32 detail) {
    GLenum_ e=p_glGetError();
    if(!e)return 0;
    slog("framebuffer: stage=%s plane=%u detail=%x GL_error=%x",stage,plane,detail,e);
    return 1;
}
/* Driver readback of one plane into its own buffer (GL thread only). */
static u8 *fb_read_into(const u32 d[4],u32 plane) {
    u32 n=fb_plane_size(d,plane);u8 *buf=fb_plane_room(plane,n);
    if(!buf){slog("framebuffer: plane=%u allocation=%u failed",plane,n);return 0;}
    fb_glReadBuffer(plane==0?0x0404:0x0405);
    u64 stamp=glsync_profile_now();
    fb_glReadPixels(0,0,d[0],d[1],fb_format(plane),fb_type(plane),buf);
    gl_profile_record(GLP_FB_FRONT_READ+plane,stamp,glsync_profile_now());
    if(fb_error("read",plane,fb_format(plane)))return 0;
    return buf;
}
/* Read planes in order on the GL thread, handing front, back and depth to the
 * workers as each read completes, then hash stencil on this thread and join.
 * Stops at the first failed read; returns how many planes were read. The
 * caller may release its GL state, then must call fb_hash_last_and_join
 * before it consumes any digest or returns. */
static u32 fb_read_and_hash(const u32 dims[4],int memo,HGLRC context,HDC drawable,u64 *hash_begin) {
    u32 read=0;*hash_begin=0;int dispatched=0;
    for(u32 i=0;i<4;i++) {
        fb_job_t *j=&fb_jobs[i];
        if(memo) {
            DWORD last_error=GetLastError();
            fb_memo_prepare(dims,i,context,drawable,j);
            SetLastError(last_error);
        } else {
            j->memo_bytes=0;j->front_bytes=0;j->store=0;j->n=fb_plane_size(dims,i);
        }
        u8 *buf=fb_read_into(dims,i);if(!buf)break;
        j->bytes=buf;
        if(memo&&i==1&&context&&drawable)j->front_bytes=fb_jobs[0].bytes;
        read++;
        if(i+1<4) {
            if(!dispatched){*hash_begin=glsync_profile_now();dispatched=1;}
            fb_pool_run(i,j);
        }
    }
    return read;
}
static void fb_hash_last_and_join(u32 read,u64 hash_begin) {
    if(read==4)fb_job_run(&fb_jobs[3]);
    fb_pool_join();
    if(read)gl_profile_record(GLP_FB_HASH_PARALLEL,hash_begin,glsync_profile_now());
}
static void *fb_fbo_proc(const char *core,const char *ext);
static int fb_default_packed(const u32 dims[4],const char *stage,const u32 *wanted) {
    u32 n=dims[0]*dims[1];if(!img_room(n*4))return 0;
    fb_glReadPixels(0,0,dims[0],dims[1],0x84f9,0x84fa,g_img);
    if(fb_error("default packed read",2,0))return 0;
    u8 key[32];char hex[65];sha256(g_img,n*4,key);hexkey(hex,key);emit(key,g_img,n*4);
    const u32 *got=(const u32*)g_img;
    u32 changed=0,first=0;
    if(wanted)for(u32 i=0;i<n;i++)if(got[i]!=wanted[i]){if(!changed)first=i;changed++;}
    slog("framebuffer: default packed stage=%s hash=%s compared=%u changed=%u first=%u want_value=%x actual_value=%x",
         stage,hex,wanted!=0,changed,first,wanted?wanted[first]:0,got[first]);
    const char *ext=(const char*)p_glGetString(0x1f03);
    if(ext&&k_strstr(ext,"GL_ARB_framebuffer_object")) {
        void (GLAPI *query)(GLenum_,GLenum_,GLenum_,GLint_*)=reinterpret_cast<decltype(query)>(fb_fbo_proc(
            "glGetFramebufferAttachmentParameteriv","glGetFramebufferAttachmentParameterivEXT"));
        if(!query){slog("framebuffer: default attachment query unavailable");return 0;}
        GLint_ depth_type=0,depth_bits=0,stencil_bits=0;
        query(0x8ca8,0x1801,0x8211,&depth_type); /* READ, DEPTH, COMPONENT_TYPE */
        query(0x8ca8,0x1801,0x8216,&depth_bits);
        query(0x8ca8,0x1802,0x8217,&stencil_bits);
        if(fb_error("default attachment format",2,0))return 0;
        slog("framebuffer: default attachment stage=%s depth_type=%x depth_bits=%u stencil_bits=%u",
             stage,depth_type,depth_bits,stencil_bits);
    }
    return 1;
}
static void framebuffer_capture(void) {
    u32 dims[4];u8 desc[156],kids[4][32],key[32];
    if(!fb_resolve()||!fb_dimensions(dims))capture_abort("unsupported default framebuffer (needs fixed-point RGBA8-or-less/depth/stencil, mono double buffer, no MSAA/aux)");
    if(!fb_pool_state)fb_pool_init(); /* once, at the first admitted capture */
    texsave_t saved;tex_bind_begin(&saved);
    if(!fb_push_attrib()) {
        tex_bind_end(&saved);
        capture_abort("default framebuffer attribute stack unavailable");
    }
    fb_transfer_identity();
    HGLRC context;HDC drawable;
    {DWORD last_error=GetLastError();fb_memo_current(&context,&drawable);SetLastError(last_error);}
    u64 hash_begin;u32 read=fb_read_and_hash(dims,1,context,drawable,&hash_begin);
    fb_glPopAttrib();tex_bind_end(&saved);
    fb_hash_last_and_join(read,hash_begin);
    /* Digests, memo transitions and blobs in plane order, as before; planes
     * read before a failed read are still persisted before the refusal. */
    for(u32 i=0;i<read;i++) {
        DWORD last_error=GetLastError();
        fb_memo_finish(dims,i,context,drawable,&fb_jobs[i],kids[i]);
        SetLastError(last_error);
        gl_profile_record(GLP_FB_FRONT_HASH+i,fb_jobs[i].start,fb_jobs[i].end);
        emit(kids[i],fb_jobs[i].bytes,fb_jobs[i].n);
    }
    int ok=read==4;
    if(!ok||p_glGetError())capture_abort("default framebuffer readback failed");
    /* After SwapBuffers the back buffer's contents are undefined: the driver
     * rotates a 3+ buffer swap chain, so which stale frame reads back as "back"
     * depends on timing (canaries diverged 1-2 ticks after strict restores while
     * memory and the front plane matched). The state keys the back plane as the
     * front; restore then draws the front into GL_BACK and verifies that. */
    memcpy(kids[1],kids[0],32);
    u32 hdr[3]={DESC_MAGIC_FB,1,4};memcpy(desc,hdr,12);memcpy(desc+12,kids,128);memcpy(desc+140,dims,16);
    u64 stamp=glsync_profile_now();sha256(desc,sizeof desc,key);
    gl_profile_record(GLP_FB_DESCRIPTOR_HASH,stamp,glsync_profile_now());emit(key,desc,sizeof desc);
    memcpy(SH->gl_framebuffer_key,key,32);memcpy(g_real_fb,key,32);
}
/* DrawPixels converts integer depth through floating point, which lost one
 * D24 code on 25,052 pixels in the qualification capture. Packed transfer and
 * an equal-format NEAREST depth/stencil blit preserve the integer codes. */
static void *fb_fbo_proc(const char *core,const char *ext) {
    void *(GLAPI *gp)(const char*)=reinterpret_cast<decltype(gp)>(GetProcAddress(GetModuleHandleA("opengl32.dll"),"wglGetProcAddress"));
    if(!gp)return 0;
    void *p=gp(core);if((uintptr_t)p<=3||(uintptr_t)p==0xffffffffu)p=gp(ext);
    return ((uintptr_t)p<=3||(uintptr_t)p==0xffffffffu)?0:p;
}
static int fb_restore_d24s8(const u32 dims[4],const u8 kids[4][32]) {
    if(dims[2]!=24||dims[3]!=8){slog("framebuffer: packed restore requires D24S8");return 0;}
    const char *ext=(const char*)p_glGetString(0x1f03);
    if(!ext||(!k_strstr(ext,"GL_ARB_framebuffer_object")&&
       !(k_strstr(ext,"GL_EXT_framebuffer_object")&&k_strstr(ext,"GL_EXT_framebuffer_blit")&&
         k_strstr(ext,"GL_EXT_packed_depth_stencil")))) {
        slog("framebuffer: packed depth/stencil blit unsupported");return 0;
    }
    void (GLAPI *gen)(GLsizei_,GLuint_*)=reinterpret_cast<decltype(gen)>(fb_fbo_proc("glGenFramebuffers","glGenFramebuffersEXT"));
    void (GLAPI *del)(GLsizei_,const GLuint_*)=reinterpret_cast<decltype(del)>(fb_fbo_proc("glDeleteFramebuffers","glDeleteFramebuffersEXT"));
    void (GLAPI *bind)(GLenum_,GLuint_)=reinterpret_cast<decltype(bind)>(fb_fbo_proc("glBindFramebuffer","glBindFramebufferEXT"));
    void (GLAPI *attach)(GLenum_,GLenum_,GLenum_,GLuint_,GLint_)=reinterpret_cast<decltype(attach)>(fb_fbo_proc("glFramebufferTexture2D","glFramebufferTexture2DEXT"));
    GLenum_ (GLAPI *status)(GLenum_)=reinterpret_cast<decltype(status)>(fb_fbo_proc("glCheckFramebufferStatus","glCheckFramebufferStatusEXT"));
    void (GLAPI *blit)(GLint_,GLint_,GLint_,GLint_,GLint_,GLint_,GLint_,GLint_,GLuint_,GLenum_)=reinterpret_cast<decltype(blit)>(fb_fbo_proc("glBlitFramebuffer","glBlitFramebufferEXT"));
    if(!gen||!del||!bind||!attach||!status||!blit){slog("framebuffer: packed blit entrypoint missing");return 0;}
    u32 bytes=0,n=dims[0]*dims[1];const u32 *depth=(const u32*)fetch(kids[2],&bytes);
    if(!depth||bytes!=n*4)return 0;
    u32 *packed=static_cast<u32 *>(VirtualAlloc(0,n*4,MEM_RESERVE|MEM_COMMIT,PAGE_READWRITE));
    if(!packed){slog("framebuffer: packed scratch allocation failed");return 0;}
    int ok=1;
    for(u32 i=0;i<n;i++) {
        u32 code=depth[i]>>8;
        if(((code<<8)|(code>>16))!=depth[i]) {
            slog("framebuffer: noncanonical D24 readback at pixel=%u value=%x",i,depth[i]);ok=0;break;
        }
        packed[i]=code<<8;
    }
    const u8 *stencil=ok?fetch(kids[3],&bytes):0;
    if(!stencil||bytes!=n)ok=0;
    if(ok)for(u32 i=0;i<n;i++)packed[i]|=stencil[i];
    if(!ok){VirtualFree(packed,0,MEM_RELEASE);return 0;}
    GLint_ read_fb=0,draw_fb=0;
    p_glGetIntegerv(0x8caa,&read_fb);p_glGetIntegerv(0x8ca6,&draw_fb);
    if(fb_error("packed saved bindings",2,0)){VirtualFree(packed,0,MEM_RELEASE);return 0;}
    u32 *candidate=static_cast<u32 *>(VirtualAlloc(0,n*4,MEM_RESERVE|MEM_COMMIT,PAGE_READWRITE));
    if(!candidate){VirtualFree(packed,0,MEM_RELEASE);return 0;}
    memcpy(candidate,packed,n*4); /* packed remains the immutable recorded target */
    texsave_t saved;tex_bind_begin(&saved);
    GLuint_ texture=0,fbo=0;
    fb_glGenTextures(1,&texture);gen(1,&fbo);
    if(fb_error("packed allocate",2,0)||!texture||!fbo)ok=0;
    if(ok) {
        p_glBindTexture(0x0de1,texture);
        p_glTexParameteri(0x0de1,0x2801,0x2600);p_glTexParameteri(0x0de1,0x2800,0x2600);
        p_glTexImage2D(0x0de1,0,0x88f0,dims[0],dims[1],0,0x84f9,0x84fa,packed);
        if(fb_error("packed upload",2,0))ok=0;
    }
    if(ok) {
        bind(0x8d40,fbo);
        attach(0x8d40,0x8d00,0x0de1,texture,0); /* depth */
        attach(0x8d40,0x8d20,0x0de1,texture,0); /* stencil */
        fb_glReadBuffer(0);fb_glDrawBuffer(0);
        GLenum_ complete=status(0x8d40);
        if(fb_error("packed attach",2,complete)||complete!=0x8cd5) {
            slog("framebuffer: packed framebuffer status=%x",complete);ok=0;
        }
    }
    if(ok) {
        /* Distinguish upload/storage conversion from blit and normalized
         * readback conversion. These diagnostics do not relax final hashes. */
        if(!img_room(n*4))ok=0;
        if(ok) {
            fb_glReadPixels(0,0,dims[0],dims[1],0x84f9,0x84fa,g_img);
            if(fb_error("packed scratch raw read",2,0))ok=0;
        }
        if(ok) {
            const u32 *got=(const u32*)g_img;u32 changed=0,depth_changed=0,stencil_changed=0,first=0;
            for(u32 i=0;i<n;i++)if(got[i]!=packed[i]) {
                if(!changed)first=i;
                changed++;depth_changed+=(got[i]>>8)!=(packed[i]>>8);
                stencil_changed+=(got[i]&255)!=(packed[i]&255);
            }
            u8 expected[32],observed[32];char expected_hex[65],observed_hex[65];
            sha256(packed,n*4,expected);sha256(g_img,n*4,observed);
            hexkey(expected_hex,expected);hexkey(observed_hex,observed);
            emit(expected,packed,n*4);emit(observed,g_img,n*4);
            slog("framebuffer: scratch packed changed=%u depth=%u stencil=%u first=%u want_value=%x actual_value=%x want=%s actual=%s",
                 changed,depth_changed,stencil_changed,first,packed[first],got[first],expected_hex,observed_hex);
            fb_glReadPixels(0,0,dims[0],dims[1],0x1902,0x1405,g_img);
            if(fb_error("packed scratch normalized read",2,0))ok=0;
            else {
                sha256(g_img,n*4,observed);hexkey(observed_hex,observed);hexkey(expected_hex,kids[2]);
                emit(observed,g_img,n*4);
                slog("framebuffer: scratch normalized depth want=%s actual=%s",expected_hex,observed_hex);
            }
            if(changed)ok=0; /* integer upload itself must preserve every bit */
        }
    }
    int exact=0;
    for(u32 attempt=0;ok&&attempt<3;attempt++) {
        bind(0x8ca8,fbo);bind(0x8ca9,(GLuint_)draw_fb);
        if(attempt) {
            p_glTexImage2D(0x0de1,0,0x88f0,dims[0],dims[1],0,0x84f9,0x84fa,candidate);
            if(fb_error("packed correction upload",2,attempt)){ok=0;break;}
            /* Admission requires an exact integer scratch upload on every try. */
            fb_glReadPixels(0,0,dims[0],dims[1],0x84f9,0x84fa,g_img);
            if(fb_error("packed correction scratch read",2,attempt)||memcmp(candidate,g_img,n*4)) {
                slog("framebuffer: correction scratch mismatch attempt=%u",attempt+1);ok=0;break;
            }
        }
        blit(0,0,dims[0],dims[1],0,0,dims[0],dims[1],0x00000500,0x2600);
        if(fb_error("packed blit",2,attempt)){ok=0;break;}
        bind(0x8ca8,(GLuint_)read_fb);
        if(!fb_default_packed(dims,"after blit",packed)){ok=0;break;}
        const u32 *observed=(const u32*)g_img;
        /* Compare every stored bit, not a compensated hash or a tolerance. */
        if(!memcmp(observed,packed,n*4)){exact=1;break;}
        if(attempt==2)break;
        for(u32 i=0;i<n;i++) {
            if((observed[i]&255)!=(packed[i]&255)) {
                slog("framebuffer: correction stencil mismatch pixel=%u attempt=%u",i,attempt+1);ok=0;break;
            }
            i64 next=(i64)(candidate[i]>>8)+(i64)(packed[i]>>8)-(i64)(observed[i]>>8);
            if(next<0||next>0xffffff) {
                slog("framebuffer: correction out of range pixel=%u attempt=%u",i,attempt+1);ok=0;break;
            }
            candidate[i]=((u32)next<<8)|(packed[i]&255);
        }
    }
    if(!exact){slog("framebuffer: exact packed restore did not converge within three blits");ok=0;}
    bind(0x8ca8,(GLuint_)read_fb);bind(0x8ca9,(GLuint_)draw_fb);
    if(fbo)del(1,&fbo);
    if(texture)p_glDeleteTextures(1,&texture);
    tex_bind_end(&saved);VirtualFree(candidate,0,MEM_RELEASE);VirtualFree(packed,0,MEM_RELEASE);
    if(fb_error("packed cleanup",2,0))ok=0;
    return ok;
}
static int framebuffer_apply(const u8 *desc,u32 n,const u8 key[32]) {
    if(n!=156||((const u32*)desc)[0]!=DESC_MAGIC_FB||((const u32*)desc)[1]!=1||((const u32*)desc)[2]!=4) {
        slog("framebuffer: invalid descriptor bytes=%u",n);return 0;
    }
    if(fb_error("entry",0,0))return 0;
    u32 dims[4],actual[4];memcpy(dims,desc+140,16);
    if(!fb_resolve()){slog("framebuffer: entrypoints unavailable");return 0;}
    if(!fb_dimensions(actual)){slog("framebuffer: drawable admission failed GL_error=%x",p_glGetError());return 0;}
    if(memcmp(dims,actual,16)) {
        slog("framebuffer: dimensions want=%ux%u depth=%u stencil=%u actual=%ux%u depth=%u stencil=%u",
             dims[0],dims[1],dims[2],dims[3],actual[0],actual[1],actual[2],actual[3]);return 0;
    }
    const u8 (*kids)[32]=(const u8(*)[32])(desc+12);
    for(u32 i=0;i<4;i++) {
        u32 bytes=0;if(!fetch(kids[i],&bytes)||bytes!=fb_plane_size(dims,i)) {
            slog("framebuffer: preflight plane=%u bytes=%u wanted=%u",i,bytes,fb_plane_size(dims,i));return 0;
        }
    }
    /* Logical selectors remain captured while raw scratch FBO operations
     * temporarily select GL_NONE. Reapply them explicitly after returning to
     * the default drawable; a raw attribute pop is not a Wine selector receipt. */
    GLint_ saved_draw=0,saved_read=0;gldraw_selectors(&saved_draw,&saved_read);
    texsave_t saved;tex_bind_begin(&saved);
    if(!fb_push_attrib()){slog("framebuffer: attribute push refused");tex_bind_end(&saved);return 0;}
    int ok=1;
    fb_transfer_identity();
    if(fb_error("transfer setup",0,0))ok=0;
    for(u32 i=0;ok&&i<sizeof ENABLES/4;i++) {
        p_glDisable(ENABLES[i]);if(fb_error("disable",0,ENABLES[i]))ok=0;
    }
    for(u32 unit=0;ok&&unit<g_units;unit++) {
        p_glActiveTextureARB(0x84c0+unit);
        if(fb_error("texture unit",unit,0)){ok=0;break;}
        for(u32 i=0;ok&&i<sizeof UNIT_ENABLES/4;i++) {
            p_glDisable(UNIT_ENABLES[i]);if(fb_error("texture disable",unit,UNIT_ENABLES[i]))ok=0;
        }
    }
    if(ok) {
        p_glColorMask(1,1,1,1);p_glDepthMask(0);p_glStencilMask(0xffffffffu);
        if(fb_error("write masks",0,0))ok=0;
    }
    /* Restore the recorded planes without presenting. An extra raw swap can
     * change driver buffer history even when immediate readback is exact.
     * The paused preview uses separately attributed recorded pixels. */
    for(u32 i=0;ok&&i<2;i++) {
        u32 bytes=0;const u8 *pixels=fetch(kids[i],&bytes);
        if(!pixels||bytes!=fb_plane_size(dims,i)){slog("framebuffer: draw payload plane=%u failed",i);ok=0;break;}
        fb_glDrawBuffer(i==0?0x0404:0x0405);
        if(fb_error("plane setup",i,0)){ok=0;break;}
        fb_windowpos(0,0);
        if(fb_error("window position",i,0)){ok=0;break;}
        fb_glDrawPixels(dims[0],dims[1],fb_format(i),fb_type(i),pixels);
        if(fb_error("draw",i,fb_format(i))){ok=0;break;}
    }
    if(ok)ok=fb_restore_d24s8(dims,kids);
    /* Independent SHA of every plane (no memo, no front/back reuse). Each
     * plane is still read, persisted and compared in order; the first
     * mismatch fails the restore. Reads complete before the comparisons, so a
     * mismatch no longer prevents reading the later planes. */
    if(ok) {
        u64 hash_begin;u32 read=fb_read_and_hash(dims,0,0,0,&hash_begin);
        fb_hash_last_and_join(read,hash_begin);
        if(read<4)ok=0;
        for(u32 i=0;i<read;i++) {
            const u8 *got=fb_jobs[i].key;
            gl_profile_record(GLP_FB_FRONT_HASH+i,fb_jobs[i].start,fb_jobs[i].end);
            emit(got,fb_jobs[i].bytes,fb_jobs[i].n);
            if(memcmp(got,kids[i],32)) {
                char want_hex[65],got_hex[65];hexkey(want_hex,kids[i]);hexkey(got_hex,got);
                u32 want_bytes=0;const u8 *want=fetch(kids[i],&want_bytes);
                if(!want||want_bytes!=fb_jobs[i].n)
                    slog("framebuffer: readback mismatch plane=%u want=%s actual=%s expected blob unavailable",i,want_hex,got_hex);
                else {
                    u32 first=0;while(first<fb_jobs[i].n&&fb_jobs[i].bytes[first]==want[first])first++;
                    if(first==fb_jobs[i].n){slog("framebuffer: readback digest mismatch without byte mismatch plane=%u",i);ok=0;break;}
                    u32 pixel=first/(i==3?1u:4u);
                    slog("framebuffer: readback mismatch plane=%u want=%s actual=%s first_byte=%u pixel=%u component=%u want_byte=%02x actual_byte=%02x",
                         i,want_hex,got_hex,first,pixel,i==3?0u:first%4u,(u32)want[first],(u32)fb_jobs[i].bytes[first]);
                }
                ok=0;break;
            }
        }
    }
    fb_glFlush();fb_glPopAttrib();
    GLint_ raw_draw=0,raw_read=0;
    p_glGetIntegerv(0x0c01,&raw_draw);p_glGetIntegerv(0x0c02,&raw_read);
    slog("framebuffer: cleanup selectors logical=%x/%x raw_before=%x/%x",
         saved_draw,saved_read,raw_draw,raw_read);
    p_glDrawBuffer((GLenum_)saved_draw);fb_glReadBuffer((GLenum_)saved_read);
    p_glActiveTextureARB(0x84c0);tex_bind_end(&saved);
    if(fb_error("cleanup selectors",0,0))ok=0;
    if(ok){
        emit(key,desc,n);
        u32 bytes=0;const u8 *front=fetch(kids[0],&bytes);
        if(!front||bytes!=fb_plane_size(dims,0)){slog("framebuffer: verified front fetch failed");return 0;}
        memcpy(g_real_fb,key,32);
        frames_accept_restored(front,dims[0],dims[1]);
        hud_verified_frame();
        slog("framebuffer: verified four planes %ux%u depth=%u stencil=%u",dims[0],dims[1],dims[2],dims[3]);
    }else memset(g_real_fb,0,32);
    return ok;
}
