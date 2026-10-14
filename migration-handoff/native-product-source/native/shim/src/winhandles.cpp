/* Game-side USER/GDI/WGL identities. All wrappers sit outside earlier geometry,
 * message and presentation hooks: those hooks continue receiving real handles.
 * Callback copies travel in the other direction. Never rewrite a real Windows
 * message or caller-owned structure in place while translating its handles. */
#include "shim.hpp"
#include "winhandles.hpp"

/* Private typed views of the existing opaque identity API. These helpers only
 * express the pointer conversion; table ownership and captured bytes are unchanged. */
extern "C" { static void *owned(void *,u32,void *); }
template<typename T> static T *wh_real(T *value) {
    return static_cast<T *>(wh_real(static_cast<void *>(value)));
}
template<typename T> static T *wh_alias(T *value,u32 kind) {
    return static_cast<T *>(wh_alias(static_cast<void *>(value),kind));
}
template<typename T> static T *owned(T *value,u32 kind,void *owner) {
    return static_cast<T *>(owned(static_cast<void *>(value),kind,owner));
}

/* Fixed C hook ABI and assembly-referenced symbol names. */
extern "C" {

typedef struct { void *value; u32 kind, serial, owner, live; wh_desc_t desc; } wh_real_t;
static wh_real_t actual[WH_CAP];
static int failed;
static int reconciling;
static int bindings_pending;
int wh_reconciling(void) { return reconciling; }
static void save_desc(u32 i) { if(i<WH_CAP) actual[i].desc=SH->wh.desc[i]; }
static void bounded_name(char out[WH_NAME],const char *in) {
    u32 n=in?(u32)strlen(in):0;
    if(n>=WH_NAME) { slog("window descriptor string too long");TerminateProcess(GetCurrentProcess(),6);return; }
    memset(out,0,WH_NAME);if(n)memcpy(out,in,n);
}
static void attributes(int out[WH_ATTR],const int *a) {
    memset(out,0,sizeof(int)*WH_ATTR);if(!a)return;
    for(u32 i=0;i<WH_ATTR-1;i+=2) { out[i]=a[i];if(!a[i])return;out[i+1]=a[i+1]; }
    slog("window descriptor attribute list too long");TerminateProcess(GetCurrentProcess(),6);
}
static u32 lifecycle_logged;
static u32 alias_serial(void *v) {
    u32 h=(u32)(uintptr_t)v;
    return h>=WH_BASE && h<WH_ALIAS_END && !(h&3) ? (h-WH_BASE)/4+1 : 0;
}
static void *alias_for_slot(u32 i) { return (void *)(uintptr_t)(WH_BASE+(SH->wh.entry[i].serial-1)*4); }
static u32 index_of(void *v) {
    u32 serial=alias_serial(v);
    if(serial)for(u32 i=0;i<SH->wh.count && i<WH_CAP;i++)if(SH->wh.entry[i].serial==serial)return i;
    return WH_CAP;
}
u32 wh_context_slot(void) {
    u32 i=index_of((void *)(uintptr_t)SH->wh.current_gl);
    return i<WH_CAP && SH->wh.entry[i].live && SH->wh.entry[i].kind==WH_GL ? i : WH_CAP;
}
/* Logical initial framebuffer selectors belong to the configured drawable,
 * not a fullscreen implementation FBO queried through native GL. */
int wh_current_doublebuffer(void) {
    u32 i=index_of((void *)(uintptr_t)SH->wh.current_dc);
    if(i>=WH_CAP || !SH->wh.entry[i].live || SH->wh.entry[i].kind!=WH_DC)return -1;
    wh_desc_t *d=&SH->wh.desc[i];
    if(d->recipe==WR_PBUFFERDC) {
        i=index_of((void *)(uintptr_t)SH->wh.entry[i].owner);
        if(i>=WH_CAP || !SH->wh.entry[i].live || SH->wh.entry[i].kind!=WH_PBUFFER)return -1;
        d=&SH->wh.desc[i];
    }
    if(!d->pixel && SH->wh.entry[i].kind==WH_DC) {
        u32 owner=index_of((void *)(uintptr_t)SH->wh.entry[i].owner);
        if(owner<WH_CAP && SH->wh.entry[owner].live && SH->wh.entry[owner].kind==WH_WINDOW)
            d=&SH->wh.desc[owner];
    }
    if(!d->pixel)return -1;
    return (d->pfd.dwFlags&PFD_DOUBLEBUFFER)!=0;
}
static void fail(const char *what) {
    failed = 1;
    slog("window handle boundary: %s", what);
}
/* Only consulted when a real window has no alias yet. The desktop belongs to
 * the Wine shell process but is a USER fact every run shares (WR_DESKTOP). */
static int host_only_window(HWND h) {
    if (!h || h == GetDesktopWindow()) return 0;
    DWORD error=GetLastError(),pid=0;
    DWORD thread=GetWindowThreadProcessId(h,&pid);
    int host=thread && pid!=GetCurrentProcessId();
    char name[32];
    if(!host && thread && GetClassNameA(h,name,sizeof name)>0 && k_streq(name,"KPTAS.OutputHUD.v1")) host=1;
    SetLastError(error);
    static u32 logged;
    if(host && logged++ < 16) slog("window alias refused for host-only window real=%x pid=%u",(u32)(uintptr_t)h,(u32)pid);
    return host;
}
void *wh_real(void *v) {
    if (g_bnd_native || !v) return v;
    u32 i = index_of(v);
    if (i == WH_CAP) {
        if(!alias_serial(v))return v; /* NULL, special HWND_* values and host-only calls */
        slog("window alias expired alias=%x serial=%u last=%u",(u32)(uintptr_t)v,alias_serial(v),SH->wh.serial);
        fail("expired resource generation");TerminateProcess(GetCurrentProcess(),6);for(;;){}
    }
    wh_entry_t *s = &SH->wh.entry[i]; wh_real_t *r = &actual[i];
    if (i >= SH->wh.count || !s->live || !r->live || s->kind != r->kind || s->serial != r->serial) {
        slog("window alias invalid alias=%x slot=%u count=%u saved(kind=%u serial=%u live=%u owner=%x) real(value=%x kind=%u serial=%u live=%u owner=%x) caller=%x tick=%u",
             (u32)(uintptr_t)v,i,SH->wh.count,s->kind,s->serial,s->live,s->owner,
             (u32)(uintptr_t)r->value,r->kind,r->serial,r->live,r->owner,
             (u32)(uintptr_t)__builtin_return_address(0),(u32)g_gate.tick);
        fail("missing or stale resource");
        /* NULL is meaningful to GetDC/SetCapture and must never stand in for a
         * failed alias lookup. Stop before any wrapper can call the host. */
        TerminateProcess(GetCurrentProcess(), 6);
        for (;;) {}
    }
    return r->value;
}
void *wh_alias(void *v, u32 kind) {
    if (g_bnd_native || !v || alias_serial(v)) return v;
    /* USER pseudo handles are values, not resource identities. */
    if (kind == WH_WINDOW && ((u32)(uintptr_t)v <= 1 || (u32)(uintptr_t)v >= 0xfffffffdu)) return v;
    for (u32 i = 0; i < SH->wh.count; i++)
        if (actual[i].value == v && actual[i].kind == kind && actual[i].live && SH->wh.entry[i].live &&
            actual[i].serial == SH->wh.entry[i].serial) return alias_for_slot(i);
    /* Windows the game never created and cannot own never enter the table:
     * another process's windows (a host tool or desktop shell) and the shim's
     * own output HUD, an owned popup that can surface as a z-order neighbour
     * in WINDOWPOS.hwndInsertAfter. Their existence follows host settings, so
     * an alias would make count/serial host-dependent and leave a slot with no
     * recreation recipe. The game sees NULL instead, deterministically. */
    if (kind == WH_WINDOW && host_only_window(static_cast<HWND>(v))) return NULL;
    u32 i=0;
    while(i<SH->wh.count && (SH->wh.entry[i].live || actual[i].live))i++;
    if(i==WH_CAP) { fail("concurrent resource table exhausted");TerminateProcess(GetCurrentProcess(),6);return NULL; }
    if(i==SH->wh.count)SH->wh.count++;
    if(SH->wh.serial>=(WH_ALIAS_END-WH_BASE)/4) { fail("resource generation namespace exhausted");TerminateProcess(GetCurrentProcess(),6);return NULL; }
    u32 serial=++SH->wh.serial;
    SH->wh.entry[i] = wh_entry_t{kind, serial, 1, 0, 0};
    memset(&SH->wh.desc[i],0,sizeof(wh_desc_t));
    actual[i] = wh_real_t{v,kind,serial,0,1,{}};
    if(kind==WH_WINDOW && v==GetDesktopWindow()) SH->wh.desc[i].recipe=WR_DESKTOP;
    save_desc(i);
    if(lifecycle_logged++ < 48) slog("window alias create slot=%u kind=%u real=%x serial=%u",i,kind,(u32)(uintptr_t)v,serial);
    return alias_for_slot(i);
}
static void *owned(void *v, u32 kind, void *owner) {
    void *a = wh_alias(v, kind); u32 i = index_of(a);
    if (i < WH_CAP) SH->wh.entry[i].owner = actual[i].owner = (u32)(uintptr_t)owner;
    return a;
}
static void retire(void *a) {
    u32 i = index_of(a);
    if (i < WH_CAP) {
        if(lifecycle_logged++ < 48) slog("window alias retire slot=%u kind=%u real=%x serial=%u",i,actual[i].kind,(u32)(uintptr_t)actual[i].value,actual[i].serial);
        SH->wh.entry[i].live = 0; SH->wh.entry[i].wndproc=0; actual[i].live = 0;
        memset(&SH->wh.desc[i],0,sizeof SH->wh.desc[i]);
    }
}

/* A known destroyed identity remains a tombstone. Querying or destroying it
 * again has normal Win32 failure semantics; never forward its old host value,
 * which could already have been recycled for a different resource. */
static int retired_identity(void *h,u32 kind) {
    u32 i=index_of(h);
    if(g_bnd_native)return 0;
    if(i>=SH->wh.count || i>=WH_CAP) {
        u32 serial=alias_serial(h);
        return serial && serial<=SH->wh.serial; /* Issued generation whose retired slot was reused. */
    }
    wh_entry_t *s=&SH->wh.entry[i];wh_real_t *r=&actual[i];
    return s->kind==kind && r->kind==kind &&
           s->serial==r->serial && !s->live && !r->live;
}

/* Geometry probes may legitimately inspect the engine's stale HWND global
 * after destruction. This nullable lookup is never used for host operations. */
void *wh_window_real_if_live(void *h) {
    return retired_identity(h,WH_WINDOW)?NULL:wh_real(h);
}

/* ---- Virtual activation model (every boundary except native) ----
 * Focus, activation, foreground, capture and the cursor shape are game state.
 * SH->wh.{focus,active,capture,cursor} hold game aliases and change only in
 * the game's own USER32 calls below, following Win32 semantics; the
 * foreground window is the virtual active window. All four start at 0 (SH is
 * zeroed before any window exists). Nothing reads them back from the host:
 * when the user works in another window, the real values change and the game
 * neither sees nor captures that. Real USER32 still receives the game's own
 * calls, so presentation is unchanged.
 *
 * Message admission (msg.c interp): an activation-class message reaching the
 * game WndProc outside the gate pump is delivered only inside one of these
 * game calls, and only for the classes the call changes in the virtual model.
 * The decision uses virtual state at call entry, never real state, so it is
 * identical on replay. In a clean run the first activation arrives inside
 * the game's first activating ShowWindow/SetWindowPos (virtual active 0 ->
 * window) and is delivered exactly as before. A later activating call on an
 * already active window admits nothing, so host-caused real deactivation
 * cannot make Wine's re-activation messages visible to the game. */
#define WH_HOST_STYLE (WS_MINIMIZE|WS_MAXIMIZE|WS_VISIBLE)
#define WH_HOST_EXSTYLE WS_EX_TOPMOST
static u32 g_act_depth,g_act_admit;
typedef struct { u32 admit; int main; } act_scope_t;
static void act_begin(act_scope_t *s,u32 admit) {
    s->main=cur_rtid()==g_main_rtid;s->admit=g_act_admit;
    if(!s->main)return;
    g_act_depth++;g_act_admit|=admit;
}
static void act_end(act_scope_t *s) {
    if(!s->main)return;
    g_act_depth--;g_act_admit=s->admit;
}
u32 wh_activation_class(UINT m) {
    switch(m) {
    case WM_ACTIVATE: case WM_ACTIVATEAPP: case WM_NCACTIVATE: case WM_QUERYNEWPALETTE:
    case WM_IME_SETCONTEXT: case WM_IME_NOTIFY: return WH_ADM_ACTIVE;
    case WM_SETFOCUS: case WM_KILLFOCUS: return WH_ADM_FOCUS;
    case WM_CAPTURECHANGED: return WH_ADM_CAPTURE;
    case WM_CANCELMODE: return WH_ADM_CANCEL;
    default: return 0;
    }
}
u32 wh_activation_admit(void) { return cur_rtid()==g_main_rtid && g_act_depth ? g_act_admit : 0; }
u32 wh_activation_depth(void) { return g_act_depth; }
/* Live game-created window slot for an alias, or WH_CAP. */
static u32 game_window(void *a) {
    u32 i=index_of(a);
    return i<WH_CAP && i<SH->wh.count && SH->wh.entry[i].live && SH->wh.entry[i].kind==WH_WINDOW &&
           SH->wh.desc[i].recipe==WR_WINDOW ? i : WH_CAP;
}
/* Parent chain from the virtual descriptors (WS_CHILD windows only). */
static int within(u32 a,u32 h) {
    for(u32 n=0;a && n<WH_CAP;n++) {
        if(a==h)return 1;
        u32 i=index_of((void *)(uintptr_t)a);
        if(i>=WH_CAP || !(SH->wh.desc[i].style&WS_CHILD))return 0;
        a=SH->wh.desc[i].parent;
    }
    return 0;
}
static u32 top_of(u32 a) {
    for(u32 n=0;a && n<WH_CAP;n++) {
        u32 i=index_of((void *)(uintptr_t)a);
        if(i>=WH_CAP || !(SH->wh.desc[i].style&WS_CHILD))return a;
        a=SH->wh.desc[i].parent;
    }
    return a;
}
static int is_child(u32 i) { return i<WH_CAP && (SH->wh.desc[i].style&WS_CHILD); }
/* Classes a call that hides, minimizes, disables or destroys h changes. */
static u32 touches(u32 h) {
    u32 m=0;
    if(SH->wh.active && within(SH->wh.active,h))m|=WH_ADM_ACTIVE|WH_ADM_FOCUS|WH_ADM_CANCEL;
    if(SH->wh.focus && within(SH->wh.focus,h))m|=WH_ADM_FOCUS;
    return m;
}
/* Classes activating top-level window top changes. */
static u32 activates(u32 top) { return top && top!=SH->wh.active ? WH_ADM_ACTIVE|WH_ADM_FOCUS : 0; }
static void v_activate(u32 top) {
    if(!top)return;
    SH->wh.active=top;
    if(!SH->wh.focus || top_of(SH->wh.focus)!=top)SH->wh.focus=top;
}
/* h is hidden, minimized or destroyed: activation leaves it (no other game
 * window is activated in the model), focus moves to its parent if h is a
 * child, and destroy also releases capture. Computed from descriptors that
 * still exist, so call before retire(). */
typedef struct { u32 active,focus,capture; } v_focus_t;
static v_focus_t v_lose(u32 h,int destroy) {
    v_focus_t v={SH->wh.active,SH->wh.focus,SH->wh.capture};
    u32 i=index_of((void *)(uintptr_t)h);
    if(v.active && within(v.active,h)){v.active=0;v.focus=0;}
    if(v.focus && within(v.focus,h))v.focus=is_child(i)?SH->wh.desc[i].parent:0;
    if(destroy && v.capture && within(v.capture,h))v.capture=0;
    return v;
}
static void v_commit(v_focus_t v) { SH->wh.active=v.active;SH->wh.focus=v.focus;SH->wh.capture=v.capture; }
/* Geometry is sampled once, synchronously, right after the game's own create,
 * move, size or show call: Wine has applied the request and no host event
 * has been read since. Later window-manager moves or resizes never reach the
 * descriptor. Raw host coordinates (the game's own frame, as passed to
 * CreateWindowExA/SetWindowPos); children are relative to the parent client. */
static void sample_geometry(u32 i,int move,int size) {
    if(i>=WH_CAP || (!move && !size))return;
    HWND real=static_cast<HWND>(actual[i].value);RECT r;
    if(!GetWindowRect(real,&r))return;
    wh_desc_t *d=&SH->wh.desc[i];
    if(move) {
        POINT p={r.left,r.top};
        if(d->style&WS_CHILD){HWND parent=GetParent(real);if(parent)ScreenToClient(parent,&p);}
        d->x=p.x;d->y=p.y;
    }
    if(size){d->w=r.right-r.left;d->h=r.bottom-r.top;}
    save_desc(i);
}
static void set_style_bits(u32 i,u32 clear,u32 set) {
    if(i>=WH_CAP)return;
    wh_desc_t *d=&SH->wh.desc[i];
    d->style=(d->style&~clear)|set;d->visible=(d->style&WS_VISIBLE)!=0;save_desc(i);
}

/* Only message fields documented as handles are translated. Numeric IDs and
 * arbitrary application pointers must retain their original bit patterns. */
typedef struct {
    LPARAM original;
    union { CREATESTRUCTA create; WINDOWPOS pos; NCCALCSIZE_PARAMS calc; } copy;
    WINDOWPOS calc_pos;
    UINT kind;
    HWND pos_from[2], pos_to[2];   /* hwnd, hwndInsertAfter before and after translation */
} message_copy_t;
static void *convert(void *v, u32 kind, int to_game) { return to_game ? wh_alias(v, kind) : wh_real(v); }
static void pos_convert(WINDOWPOS *p, int game) {
    p->hwnd = static_cast<HWND>(convert(p->hwnd, WH_WINDOW, game));
    p->hwndInsertAfter = static_cast<HWND>(convert(p->hwndInsertAfter, WH_WINDOW, game));
}
/* A handle the callee left unchanged goes back as the exact original value.
 * A window with no alias (host-only neighbour in the z-order) reaches the game
 * as NULL; Wine's own structure must still get its real handle back. */
static void pos_forward(WINDOWPOS *p, int game, message_copy_t *c) {
    c->pos_from[0] = p->hwnd; c->pos_from[1] = p->hwndInsertAfter;
    pos_convert(p, game);
    c->pos_to[0] = p->hwnd; c->pos_to[1] = p->hwndInsertAfter;
}
static void pos_back(WINDOWPOS *p, int game, const message_copy_t *c) {
    p->hwnd = p->hwnd == c->pos_to[0] ? c->pos_from[0] : static_cast<HWND>(convert(p->hwnd, WH_WINDOW, !game));
    p->hwndInsertAfter = p->hwndInsertAfter == c->pos_to[1] ? c->pos_from[1] :
                         static_cast<HWND>(convert(p->hwndInsertAfter, WH_WINDOW, !game));
}
static void message_args(UINT m, WPARAM *w, LPARAM *l, int game, message_copy_t *c) {
    memset(c, 0, sizeof *c); c->original = *l;
    switch (m) {
    case WM_CREATE: case WM_NCCREATE:
        if (*l) {
            c->copy.create = *(CREATESTRUCTA *)*l;
            c->copy.create.hwndParent = static_cast<HWND>(convert(c->copy.create.hwndParent, WH_WINDOW, game));
            c->kind = 1; *l = (LPARAM)&c->copy.create;
        }
        break;
    case WM_WINDOWPOSCHANGING: case WM_WINDOWPOSCHANGED:
        if (*l) { c->copy.pos = *(WINDOWPOS *)*l; pos_forward(&c->copy.pos, game, c); c->kind = 2; *l = (LPARAM)&c->copy.pos; }
        break;
    case WM_NCCALCSIZE:
        if (*w && *l) {
            c->copy.calc = *(NCCALCSIZE_PARAMS *)*l;
            if (c->copy.calc.lppos) { c->calc_pos = *c->copy.calc.lppos; pos_forward(&c->calc_pos, game, c); c->copy.calc.lppos = &c->calc_pos; }
            c->kind = 3; *l = (LPARAM)&c->copy.calc;
        }
        break;
    case WM_ACTIVATE: case WM_COMMAND: case WM_CONTEXTMENU:
        if (m == WM_CONTEXTMENU) *w = (WPARAM)convert((void *)*w, WH_WINDOW, game);
        else *l = (LPARAM)convert((void *)*l, WH_WINDOW, game);
        break;
    case WM_SETFOCUS: case WM_KILLFOCUS: case WM_SETCURSOR: case WM_MOUSEACTIVATE:
    case WM_CAPTURECHANGED: /* capture target is lParam */
        if (m == WM_CAPTURECHANGED) *l = (LPARAM)convert((void *)*l, WH_WINDOW, game);
        else *w = (WPARAM)convert((void *)*w, WH_WINDOW, game);
        break;
    case WM_PARENTNOTIFY:
        if (LOWORD(*w) == WM_CREATE || LOWORD(*w) == WM_DESTROY) *l = (LPARAM)convert((void *)*l, WH_WINDOW, game);
        break;
    case WM_ERASEBKGND: case WM_ICONERASEBKGND: case WM_PRINT: case WM_PRINTCLIENT:
        *w = (WPARAM)convert((void *)*w, WH_DC, game); break;
    case WM_CTLCOLORMSGBOX: case WM_CTLCOLOREDIT: case WM_CTLCOLORLISTBOX:
    case WM_CTLCOLORBTN: case WM_CTLCOLORDLG: case WM_CTLCOLORSCROLLBAR: case WM_CTLCOLORSTATIC:
        *w = (WPARAM)convert((void *)*w, WH_DC, game);
        *l = (LPARAM)convert((void *)*l, WH_WINDOW, game); break;
    }
}
static void message_done(message_copy_t *c, int game) {
    if (c->kind == 2) {
        WINDOWPOS p = c->copy.pos; pos_back(&p, game, c); *(WINDOWPOS *)c->original = p;
    } else if (c->kind == 3) {
        NCCALCSIZE_PARAMS *out = (NCCALCSIZE_PARAMS *)c->original;
        memcpy(out->rgrc, c->copy.calc.rgrc, sizeof out->rgrc);
        if (out->lppos && c->copy.calc.lppos) {
            WINDOWPOS p = *c->copy.calc.lppos; pos_back(&p, game, c); *out->lppos = p;
        }
    }
}
LRESULT wh_wndproc_call(WNDPROC p, HWND h, UINT m, WPARAM w, LPARAM l) {
    if (g_bnd_native) return p(h,m,w,l);
    message_copy_t c; message_args(m,&w,&l,1,&c);
    HWND alias=wh_alias(h,WH_WINDOW);
    math_callback_scope_t fp;math_callback_begin(&fp);
    LRESULT r = p(alias,m,w,l);
    math_callback_end(&fp);
    message_done(&c,1);
    /* Brushes returned by control-color callbacks go back to USER. */
    if (m >= WM_CTLCOLORMSGBOX && m <= WM_CTLCOLORSTATIC) r = (LRESULT)wh_real((void *)r);
    return r;
}
static void msg_convert(MSG *m,int game) {
    m->hwnd = static_cast<HWND>(convert(m->hwnd,WH_WINDOW,game));
    message_copy_t copy; LPARAM l=m->lParam;
    message_args(m->message,&m->wParam,&l,game,&copy);
    if(!copy.kind) m->lParam=l; /* queued pointer messages cannot use a temporary copy */
}
void wh_msg_alias(MSG *m) { msg_convert(m,1); }
void wh_msg_real(MSG *m) { msg_convert(m,0); }

#define DECL(name) static decltype(name) *r_##name
#define SIMPLE(ret,name,args,expr) DECL(name); static ret WINAPI w_##name args { u32 fp=math_host_begin();ret result=(expr);math_host_end(#name,fp);return result; }
SIMPLE(HWND,GetDesktopWindow,(void),wh_alias(r_GetDesktopWindow(),WH_WINDOW))
SIMPLE(HWND,FindWindowA,(LPCSTR a,LPCSTR b),wh_alias(r_FindWindowA(a,b),WH_WINDOW))
SIMPLE(HWND,GetParent,(HWND h),wh_alias(r_GetParent(wh_real(h)),WH_WINDOW))
SIMPLE(HWND,GetAncestor,(HWND h,UINT f),wh_alias(r_GetAncestor(wh_real(h),f),WH_WINDOW))
/* Virtual getters: no host read (see the activation model above). */
DECL(GetActiveWindow);DECL(GetForegroundWindow);DECL(GetFocus);DECL(GetCapture);DECL(GetCursor);
static HWND WINAPI w_GetActiveWindow(void) { return (HWND)(uintptr_t)SH->wh.active; }
static HWND WINAPI w_GetForegroundWindow(void) { return (HWND)(uintptr_t)SH->wh.active; }
static HWND WINAPI w_GetFocus(void) { return (HWND)(uintptr_t)SH->wh.focus; }
static HWND WINAPI w_GetCapture(void) { return (HWND)(uintptr_t)SH->wh.capture; }
static HCURSOR WINAPI w_GetCursor(void) { return (HCURSOR)(uintptr_t)SH->wh.cursor; }
/* A destroyed or unknown window cannot take focus, activation or capture. */
static int virtual_target(HWND h) {
    if(retired_identity(h,WH_WINDOW) || (h && !alias_serial(h))) { SetLastError(ERROR_INVALID_WINDOW_HANDLE);return 0; }
    return 1;
}
DECL(SetFocus);
static HWND WINAPI w_SetFocus(HWND h) {
    if(!virtual_target(h))return NULL;
    math_host_scope_t fp;math_scope_begin(&fp,MATH_HOST_FP|MATH_HOST_ID);
    u32 a=(u32)(uintptr_t)h,top=top_of(a),previous=SH->wh.focus;
    u32 admit=a ? activates(top)|(previous!=a?WH_ADM_FOCUS:0) : (previous?WH_ADM_FOCUS:0);
    act_scope_t s;act_begin(&s,admit);
    r_SetFocus(wh_real(h));DWORD host_error=GetLastError();
    act_end(&s);
    if(a){SH->wh.active=top;SH->wh.focus=a;}else SH->wh.focus=0;
    SetLastError(host_error);math_scope_end(&fp);return (HWND)(uintptr_t)previous;
}
DECL(SetActiveWindow);
static HWND WINAPI w_SetActiveWindow(HWND h) {
    if(!virtual_target(h))return NULL;
    math_host_scope_t fp;math_scope_begin(&fp,MATH_HOST_FP|MATH_HOST_ID);
    u32 top=top_of((u32)(uintptr_t)h),previous=SH->wh.active;
    act_scope_t s;act_begin(&s,top?activates(top):touches(previous));
    r_SetActiveWindow(wh_real(h));DWORD host_error=GetLastError();
    act_end(&s);
    if(top)v_activate(top);else{SH->wh.active=0;SH->wh.focus=0;}
    SetLastError(host_error);math_scope_end(&fp);return (HWND)(uintptr_t)previous;
}
DECL(SetForegroundWindow);
static BOOL WINAPI w_SetForegroundWindow(HWND h) {
    if(!h || !virtual_target(h))return FALSE;
    math_host_scope_t fp;math_scope_begin(&fp,MATH_HOST_FP|MATH_HOST_ID);
    u32 top=top_of((u32)(uintptr_t)h);
    act_scope_t s;act_begin(&s,activates(top));
    r_SetForegroundWindow(wh_real(h));DWORD host_error=GetLastError();
    act_end(&s);
    v_activate(top);
    SetLastError(host_error);math_scope_end(&fp);return TRUE;
}
/* WM_CAPTURECHANGED from Wine follows the REAL capture transition, which
 * after a restore or host interaction need not match the game's. Every
 * Wine-originated one is therefore withheld (WH_ADM_CAPTURE is never in an
 * admit mask), and the game instead gets exactly one synthesized message per
 * virtual transition away from a window: sent to the previous virtual capture
 * window, wParam 0, lParam the new capture window (0 on release), as Win32
 * does. It is sent through the game route (msg_game_enter), on the main
 * thread, after the real call and before the wrapper returns. */
static void capture_changed(u32 previous,u32 next) {
    if(!previous || previous==next || cur_rtid()!=g_main_rtid)return;
    u32 i=index_of((void *)(uintptr_t)previous);
    if(i>=WH_CAP || i>=SH->wh.count || !SH->wh.entry[i].live || SH->wh.entry[i].kind!=WH_WINDOW ||
       !actual[i].live)return;
    HWND target=static_cast<HWND>(actual[i].value);
    HWND moved=next?static_cast<HWND>(wh_real((void *)(uintptr_t)next)):NULL;
    DWORD error=GetLastError();
    msg_game_enter(WM_CAPTURECHANGED);
    SendMessageA(target,WM_CAPTURECHANGED,0,(LPARAM)moved);
    msg_game_leave();
    SetLastError(error);
}
DECL(SetCapture);
static HWND WINAPI w_SetCapture(HWND h) {
    if(h && !virtual_target(h))return NULL;
    math_host_scope_t fp;math_scope_begin(&fp,MATH_HOST_FP|MATH_HOST_ID);
    u32 previous=SH->wh.capture,a=(u32)(uintptr_t)h;
    act_scope_t s;act_begin(&s,0);
    r_SetCapture(wh_real(h));DWORD host_error=GetLastError();
    act_end(&s);
    SH->wh.capture=a;
    capture_changed(previous,a);
    SetLastError(host_error);math_scope_end(&fp);return (HWND)(uintptr_t)previous;
}
DECL(ReleaseCapture);
static BOOL WINAPI w_ReleaseCapture(void) {
    math_host_scope_t fp;math_scope_begin(&fp,MATH_HOST_FP|MATH_HOST_ID);
    u32 previous=SH->wh.capture;
    act_scope_t s;act_begin(&s,0);
    r_ReleaseCapture();
    act_end(&s);
    SH->wh.capture=0;
    capture_changed(previous,0);
    math_scope_end(&fp);SetLastError(0);return TRUE;
}
SIMPLE(BOOL,SetWindowTextA,(HWND h,LPCSTR s),r_SetWindowTextA(wh_real(h),s))
/* Disabling drops virtual focus and capture inside it. WM_CANCELMODE is part
 * of every disable, so the call admits it; focus and capture classes only
 * when the model holds them inside the window. */
DECL(EnableWindow);
static BOOL WINAPI w_EnableWindow(HWND h,BOOL v) {
    u32 fp=math_host_begin();
    u32 a=(u32)(uintptr_t)h;
    u32 admit=v?0:(touches(a)&WH_ADM_FOCUS)|WH_ADM_CANCEL;
    act_scope_t s;act_begin(&s,admit);
    BOOL result=r_EnableWindow(wh_real(h),v);
    act_end(&s);
    if(!v) {
        if(SH->wh.focus && within(SH->wh.focus,a))SH->wh.focus=0;
        if(SH->wh.capture && within(SH->wh.capture,a)){u32 previous=SH->wh.capture;SH->wh.capture=0;capture_changed(previous,0);}
    }
    math_host_end("EnableWindow",fp);return result;
}
static int show_activates(int c) {
    return c==SW_SHOWNORMAL||c==SW_SHOWMINIMIZED||c==SW_SHOWMAXIMIZED||c==SW_SHOW||c==SW_RESTORE||c==SW_SHOWDEFAULT;
}
static int show_deactivates(int c) { return c==SW_HIDE||c==SW_MINIMIZE||c==SW_FORCEMINIMIZE; }
DECL(ShowWindow);
static BOOL WINAPI w_ShowWindow(HWND h,int v) {
    math_host_scope_t fp;math_scope_begin(&fp,MATH_HOST_FP|MATH_HOST_ID);
    u32 a=(u32)(uintptr_t)h,i=game_window(h);
    u32 top=i<WH_CAP && !is_child(i)?a:0;
    u32 admit=(show_activates(v)?activates(top):0)|(show_deactivates(v)?touches(a):0);
    act_scope_t s;act_begin(&s,admit);
    BOOL result=r_ShowWindow(wh_real(h),v);
    act_end(&s);
    if(i<WH_CAP) {
        int minimize=v==SW_MINIMIZE||v==SW_SHOWMINIMIZED||v==SW_SHOWMINNOACTIVE||v==SW_FORCEMINIMIZE;
        if(v==SW_HIDE)set_style_bits(i,WS_VISIBLE,0);
        else if(minimize)set_style_bits(i,WS_MAXIMIZE,WS_VISIBLE|WS_MINIMIZE);
        else if(v==SW_SHOWMAXIMIZED)set_style_bits(i,WS_MINIMIZE,WS_VISIBLE|WS_MAXIMIZE);
        else if(v==SW_SHOWNORMAL||v==SW_RESTORE||v==SW_SHOWDEFAULT)set_style_bits(i,WS_MINIMIZE|WS_MAXIMIZE,WS_VISIBLE);
        else set_style_bits(i,0,WS_VISIBLE);
        if(minimize||v==SW_SHOWMAXIMIZED||v==SW_SHOWNORMAL||v==SW_RESTORE||v==SW_SHOWDEFAULT)sample_geometry(i,1,1);
        if(show_deactivates(v))v_commit(v_lose(a,0));
        else if(top && show_activates(v)){v_activate(top);if(v==SW_SHOWMINIMIZED)SH->wh.focus=0;}
    }
    math_scope_end(&fp);return result;
}
DECL(IsWindow);
static BOOL WINAPI w_IsWindow(HWND h) { return retired_identity(h,WH_WINDOW) ? FALSE : r_IsWindow(wh_real(h)); }
SIMPLE(BOOL,GetWindowRect,(HWND h,LPRECT p),r_GetWindowRect(wh_real(h),p))
SIMPLE(BOOL,GetClientRect,(HWND h,LPRECT p),r_GetClientRect(wh_real(h),p))
/* Activation status and the host-changeable style bits come from the model. */
DECL(GetWindowInfo);
static BOOL WINAPI w_GetWindowInfo(HWND h,PWINDOWINFO p) {
    u32 fp=math_host_begin();
    BOOL result=r_GetWindowInfo(wh_real(h),p);
    u32 i=game_window(h);
    if(result && i<WH_CAP) {
        wh_desc_t *d=&SH->wh.desc[i];
        p->dwStyle=(p->dwStyle&~WH_HOST_STYLE)|(d->style&WH_HOST_STYLE);
        p->dwExStyle=(p->dwExStyle&~WH_HOST_EXSTYLE)|(d->exstyle&WH_HOST_EXSTYLE);
        p->dwWindowStatus=SH->wh.active==(u32)(uintptr_t)h?WS_ACTIVECAPTION:0;
    }
    math_host_end("GetWindowInfo",fp);return result;
}
SIMPLE(BOOL,ClientToScreen,(HWND h,LPPOINT p),r_ClientToScreen(wh_real(h),p))
SIMPLE(BOOL,ScreenToClient,(HWND h,LPPOINT p),r_ScreenToClient(wh_real(h),p))
DECL(MoveWindow);
static BOOL WINAPI w_MoveWindow(HWND h,int x,int y,int w,int z,BOOL b) {
    u32 fp=math_host_begin();
    act_scope_t s;act_begin(&s,0);
    BOOL result=r_MoveWindow(wh_real(h),x,y,w,z,b);
    act_end(&s);
    if(result)sample_geometry(game_window(h),1,1);
    math_host_end("MoveWindow",fp);return result;
}
/* Without SWP_NOACTIVATE a top-level window is activated (Wine does so for a
 * window that is still hidden, too). The model follows the flags only. */
DECL(SetWindowPos);
static BOOL WINAPI w_SetWindowPos(HWND h,HWND a,int x,int y,int w,int z,UINT f) {
    u32 fp=math_host_begin();
    u32 alias=(u32)(uintptr_t)h,i=game_window(h);
    u32 top=i<WH_CAP && !is_child(i) && !(f&(SWP_NOACTIVATE|SWP_HIDEWINDOW))?alias:0;
    u32 admit=activates(top)|((f&SWP_HIDEWINDOW)?touches(alias):0);
    act_scope_t s;act_begin(&s,admit);
    BOOL result=r_SetWindowPos(wh_real(h),wh_real(a),x,y,w,z,f);
    act_end(&s);
    if(result && i<WH_CAP) {
        sample_geometry(i,!(f&SWP_NOMOVE),!(f&SWP_NOSIZE));
        if(f&SWP_SHOWWINDOW)set_style_bits(i,0,WS_VISIBLE);
        if(f&SWP_HIDEWINDOW)set_style_bits(i,WS_VISIBLE,0);
        if(!(f&SWP_NOZORDER) && (a==HWND_TOPMOST || a==HWND_NOTOPMOST)) {
            wh_desc_t *d=&SH->wh.desc[i];
            d->exstyle=a==HWND_TOPMOST?d->exstyle|WS_EX_TOPMOST:d->exstyle&~WS_EX_TOPMOST;save_desc(i);
        }
        if(f&SWP_HIDEWINDOW)v_commit(v_lose(alias,0));
        else if(top)v_activate(top);
    }
    math_host_end("SetWindowPos",fp);return result;
}
SIMPLE(int,MessageBoxA,(HWND h,LPCSTR a,LPCSTR b,UINT f),r_MessageBoxA(wh_real(h),a,b,f))
SIMPLE(HMONITOR,MonitorFromWindow,(HWND h,DWORD f),wh_alias(r_MonitorFromWindow(wh_real(h),f),WH_MONITOR))
SIMPLE(BOOL,GetMonitorInfoA,(HMONITOR h,LPMONITORINFO p),r_GetMonitorInfoA(wh_real(h),p))
DECL(GetDC);DECL(GetWindowDC);
static HDC acquire_dc(HWND h,int window) {
    math_host_scope_t fp;math_scope_begin(&fp,MATH_HOST_FP|MATH_HOST_ID);
    DWORD entry_error=GetLastError();
    HWND real_window=wh_real(h);
    btrace_point("GetDC.host.entry",20);
    HDC real=window?r_GetWindowDC(real_window):r_GetDC(real_window);
    DWORD host_error=GetLastError();
    btrace_point("GetDC.host.return",21);
    HDC a=owned(real,WH_DC,h);u32 i=index_of(a);
    if(i<WH_CAP){
        SH->wh.desc[i].recipe=window?WR_WINDOWDC:WR_DC;
        u32 owner=index_of(h);
        if(owner<WH_CAP && SH->wh.desc[owner].pixel) {
            SH->wh.desc[i].pixel=1;SH->wh.desc[i].pfd=SH->wh.desc[owner].pfd;
        }
        save_desc(i);
    }
    /* A successful logical acquisition has no extended-error result. Keep
     * host helper errors out of game state, including metadata sampling. */
    SetLastError(a?entry_error:host_error);
    math_scope_end(&fp);return a;
}
static HDC WINAPI w_GetDC(HWND h){return acquire_dc(h,0);}
static HDC WINAPI w_GetWindowDC(HWND h){return acquire_dc(h,1);}
DECL(ChoosePixelFormat);
static int WINAPI w_ChoosePixelFormat(HDC h,const PIXELFORMATDESCRIPTOR *p){
    HDC real=wh_real(h);u32 observed=math_host_begin();
    math_host_scope_t fp;math_scope_begin(&fp,MATH_HOST_FP);
    int result=r_ChoosePixelFormat(real,p);
    math_host_end("ChoosePixelFormat",observed);
    math_scope_end(&fp);
    return result;
}
SIMPLE(int,DescribePixelFormat,(HDC h,int f,UINT n,LPPIXELFORMATDESCRIPTOR p),r_DescribePixelFormat(wh_real(h),f,n,p))
DECL(SetPixelFormat);
static BOOL WINAPI w_SetPixelFormat(HDC h,int f,const PIXELFORMATDESCRIPTOR *p){
    math_host_scope_t fp;math_scope_begin(&fp,MATH_HOST_FP|MATH_HOST_ID);
    BOOL ok=r_SetPixelFormat(wh_real(h),f,p);u32 i=index_of(h);
    if(ok && i<WH_CAP){
        SH->wh.desc[i].pixel=1;
        if(p)SH->wh.desc[i].pfd=*p;
        else r_DescribePixelFormat(wh_real(h),f,sizeof(PIXELFORMATDESCRIPTOR),&SH->wh.desc[i].pfd);
        save_desc(i);
        /* Window pixel formats survive ReleaseDC; retain the canonical
         * configuration with the drawable as well as this DC generation. */
        u32 owner=index_of((void *)(uintptr_t)SH->wh.entry[i].owner);
        if(owner<WH_CAP && SH->wh.entry[owner].live && SH->wh.entry[owner].kind==WH_WINDOW) {
            SH->wh.desc[owner].pixel=1;SH->wh.desc[owner].pfd=SH->wh.desc[i].pfd;save_desc(owner);
        }
    }math_scope_end(&fp);return ok;
}
SIMPLE(BOOL,SwapBuffers,(HDC h),r_SwapBuffers(wh_real(h)))
SIMPLE(BOOL,PatBlt,(HDC h,int x,int y,int w,int z,DWORD f),r_PatBlt(wh_real(h),x,y,w,z,f))
DECL(SetDeviceGammaRamp);DECL(GetDeviceGammaRamp);
static u32 gamma_host_failures;
static BOOL host_set_gamma(HDC real,LPVOID p) {
    /* Gamma conversion has no caller FP result. Keep Wine/driver status flags
     * and scratch registers out of the captured game floating-point state. */
    math_host_scope_t fp;math_scope_begin(&fp,MATH_HOST_FP);
    BOOL ok=r_SetDeviceGammaRamp(real,p);
    math_scope_end(&fp);
    if(!ok && gamma_host_failures++<4)slog("virtual monitor gamma retained; host gamma application unavailable");
    return ok;
}
static BOOL WINAPI w_SetDeviceGammaRamp(HDC h,LPVOID p){
    HDC real=wh_real(h);
    if(!p || !real || GetObjectType(real)!=OBJ_DC){SetLastError(ERROR_INVALID_PARAMETER);return FALSE;}
    /* The portable profile owns one logical monitor. Its ramp and API success
     * do not depend on the compositor's optional hardware-gamma support. */
    memcpy(SH->wh.gamma,p,sizeof SH->wh.gamma);
    host_set_gamma(real,SH->wh.gamma);
    return TRUE;
}
static BOOL WINAPI w_GetDeviceGammaRamp(HDC h,LPVOID p){
    HDC real=wh_real(h);
    if(!p || !real || GetObjectType(real)!=OBJ_DC){SetLastError(ERROR_INVALID_PARAMETER);return FALSE;}
    memcpy(p,SH->wh.gamma,sizeof SH->wh.gamma);return TRUE;
}
DECL(GetStockObject);DECL(LoadCursorA);DECL(LoadIconA);
static void resource_desc(void *a,u32 recipe,HINSTANCE h,LPCSTR name){
    u32 i=index_of(a);if(i>=WH_CAP)return;wh_desc_t *d=&SH->wh.desc[i];
    d->recipe=recipe;d->instance=(u32)(uintptr_t)h;
    if((uintptr_t)name<=0xffff)d->resource_atom=(u32)(uintptr_t)name;
    else bounded_name(d->resource_name,name);save_desc(i);
}
static HGDIOBJ WINAPI w_GetStockObject(int n){
    HGDIOBJ a=wh_alias(r_GetStockObject(n),WH_OBJECT);resource_desc(a,WR_STOCK,NULL,(LPCSTR)(uintptr_t)n);return a;
}
static HCURSOR WINAPI w_LoadCursorA(HINSTANCE h,LPCSTR n){
    math_host_scope_t fp;math_scope_begin(&fp,MATH_HOST_FP|MATH_HOST_ID);
    HCURSOR a=wh_alias(r_LoadCursorA(h,n),WH_CURSOR);resource_desc(a,WR_CURSOR,h,n);math_scope_end(&fp);return a;
}
static HICON WINAPI w_LoadIconA(HINSTANCE h,LPCSTR n){
    math_host_scope_t fp;math_scope_begin(&fp,MATH_HOST_FP|MATH_HOST_ID);
    HICON a=wh_alias(r_LoadIconA(h,n),WH_ICON);resource_desc(a,WR_ICON,h,n);math_scope_end(&fp);return a;
}
/* The shape is shown on the host, but the previous cursor returned to the game
 * is the model's: the real one follows whatever the pointer last crossed, and
 * aliasing a host-chosen handle would allocate a host-dependent slot. */
DECL(SetCursor);
static HCURSOR WINAPI w_SetCursor(HCURSOR h) {
    u32 fp=math_host_begin();
    u32 previous=SH->wh.cursor;
    r_SetCursor(wh_real(h));
    SH->wh.cursor=(u32)(uintptr_t)h;
    math_host_end("SetCursor",fp);return (HCURSOR)(uintptr_t)previous;
}
DECL(wglCreateContext);
static HGLRC WINAPI w_wglCreateContext(HDC h){
    HDC dc=wh_real(h);u32 observed=math_host_begin();
    math_host_scope_t fp;math_scope_begin(&fp,MATH_HOST_FP);
    HGLRC real=r_wglCreateContext(dc);math_host_end("wglCreateContext",observed);
    math_scope_end(&fp);
    HGLRC a=owned(real,WH_GL,h);u32 i=index_of(a);
    if(i<WH_CAP){SH->wh.desc[i].recipe=WR_CONTEXT;save_desc(i);}return a;
}
/* Mesa (seen on radeonsi, Mesa 26.1) reports GL_MATRIX_MODE 0 for a fresh
 * context until the first glMatrixMode, although its current stack is the
 * modelview stack. 0 is not an accepted glMatrixMode argument, so a captured
 * 0 can never be re-established once a later state changed the mode: restore
 * to such a tick failed with GL_INVALID_ENUM. The spec's initial value is
 * GL_MODELVIEW; establish it explicitly when the game makes a context current
 * and the mode is still unset. The game's own call order fixes when this
 * happens, so it is identical in every run, and it selects the stack that is
 * already current, so rendering is unchanged. Contexts the game has already
 * given a mode are left alone. */
static void normalize_matrix_mode(void) {
    typedef void (WINAPI *get_fn)(u32,int *);
    typedef void (WINAPI *mode_fn)(u32);
    static get_fn get;static mode_fn mode;
    if(!get||!mode) {
        HMODULE gl=GetModuleHandleA("opengl32.dll");
        get=gl?reinterpret_cast<get_fn>(GetProcAddress(gl,"glGetIntegerv")):nullptr;
        mode=gl?reinterpret_cast<mode_fn>(GetProcAddress(gl,"glMatrixMode")):nullptr;
        if(!get||!mode)return;
    }
    int current=-1;get(0x0BA0,&current);
    if(current==0)mode(0x1700);
}
DECL(wglMakeCurrent);
static BOOL WINAPI w_wglMakeCurrent(HDC d,HGLRC c) {
    HDC dc=wh_real(d);HGLRC context=wh_real(c);u32 observed=math_host_begin();
    math_host_scope_t fp;math_scope_begin(&fp,MATH_HOST_FP | MATH_HOST_ID);
    BOOL ok=r_wglMakeCurrent(dc,context);
    if(ok && context && !g_bnd_native)normalize_matrix_mode();
    math_host_end("wglMakeCurrent",observed);
    math_scope_end(&fp);
    if(ok) {
        SH->wh.current_dc=(u32)(uintptr_t)d; SH->wh.current_gl=(u32)(uintptr_t)c;
        /* glsync's own check compares only the context alias, and a deleted
         * context's alias can return for a new one: invalidate on every switch. */
        glsync_tex_shadow_invalidate();
    }
    return ok;
}
DECL(wglShareLists);
static BOOL WINAPI w_wglShareLists(HGLRC a,HGLRC b){
    BOOL ok=r_wglShareLists(wh_real(a),wh_real(b));u32 i=index_of(b);
    if(ok && i<WH_CAP){SH->wh.desc[i].share=(u32)(uintptr_t)a;save_desc(i);}return ok;
}
SIMPLE(HDC,wglGetCurrentDC,(void),wh_alias(r_wglGetCurrentDC(),WH_DC))
SIMPLE(HGLRC,wglGetCurrentContext,(void),wh_alias(r_wglGetCurrentContext(),WH_GL))

DECL(CreateWindowExA);
static HWND WINAPI w_CreateWindowExA(DWORD ex,LPCSTR cls,LPCSTR title,DWORD style,int x,int y,int w,int h,HWND par,HMENU menu,HINSTANCE inst,LPVOID arg) {
    math_host_scope_t fp;math_scope_begin(&fp,MATH_HOST_FP|MATH_HOST_ID);
    /* A new visible top-level window is shown and activated inside the call. */
    int shown_top=(style&WS_VISIBLE) && !(style&WS_CHILD);
    act_scope_t scope;act_begin(&scope,shown_top?WH_ADM_ACTIVE|WH_ADM_FOCUS:0);
    HWND real=r_CreateWindowExA(ex,cls,title,style,x,y,w,h,wh_real(par),menu,inst,arg);
    act_end(&scope);
    HWND alias=wh_alias(real,WH_WINDOW);u32 i=index_of(alias);
    if(i<WH_CAP) {
        wh_desc_t *d=&SH->wh.desc[i];d->recipe=WR_WINDOW;d->parent=(u32)(uintptr_t)par;
        d->exstyle=ex;d->style=style;d->instance=(u32)(uintptr_t)inst;d->menu=(u32)(uintptr_t)menu;
        d->visible=(style&WS_VISIBLE)!=0;
        d->x=x;d->y=y;d->w=w;d->h=h;
        sample_geometry(i,1,1); /* CW_USEDEFAULT and the WndProc's own adjustments resolved */
        if(shown_top)v_activate((u32)(uintptr_t)alias);
        if((uintptr_t)cls<=0xffff)d->class_atom=(u32)(uintptr_t)cls;else bounded_name(d->class_name,cls);
        bounded_name(d->title,title);
        if(!GetClassInfoA(inst,cls,&d->wc) && !GetClassInfoA(NULL,cls,&d->wc)) {
            char actual_name[WH_NAME]={0};
            HINSTANCE actual_module=(HINSTANCE)(uintptr_t)GetClassLongA(real,GCL_HMODULE);
            int n=GetClassNameA(real,actual_name,WH_NAME);
            if(n<=0 || n>=WH_NAME-1 || !GetClassInfoA(actual_module,actual_name,&d->wc)) {
                slog("window class lookup failed hwnd=%x instance=%x class=%s actual=%s module=%x error=%u",
                     (u32)(uintptr_t)real,(u32)(uintptr_t)inst,(uintptr_t)cls>0xffff?cls:"#atom",
                     actual_name,(u32)(uintptr_t)actual_module,GetLastError());
                fail("cannot capture window class");TerminateProcess(GetCurrentProcess(),6);
            }
            /* System-class atoms need not identify the same class in another
             * process. Recreate from the actual class name instead. */
            d->class_atom=0;bounded_name(d->class_name,actual_name);
        }
        d->wc.lpfnWndProc=msg_unwrap_wndproc(d->wc.lpfnWndProc);
        d->wc.hCursor=wh_alias(d->wc.hCursor,WH_CURSOR);d->wc.hIcon=wh_alias(d->wc.hIcon,WH_ICON);
        if((uintptr_t)d->wc.hbrBackground>32)d->wc.hbrBackground=wh_alias(d->wc.hbrBackground,WH_OBJECT);
        if(d->wc.lpszMenuName && (uintptr_t)d->wc.lpszMenuName>0xffff) {
            fail("string class menu needs resource descriptor");TerminateProcess(GetCurrentProcess(),6);
        }
        d->wc.lpszClassName=NULL;
        SH->wh.entry[i].wndproc=(u32)(uintptr_t)msg_unwrap_wndproc((WNDPROC)GetWindowLongA(real,GWL_WNDPROC));
        save_desc(i);
    }
    math_scope_end(&fp);return alias;
}
DECL(DestroyWindow);
static BOOL WINAPI w_DestroyWindow(HWND h) {
    if(retired_identity(h,WH_WINDOW)) { SetLastError(ERROR_INVALID_WINDOW_HANDLE);return FALSE; }
    math_host_scope_t fp;math_scope_begin(&fp,MATH_HOST_FP|MATH_HOST_ID);
    u32 a=(u32)(uintptr_t)h;
    v_focus_t after=v_lose(a,1);
    /* Destroy releases capture before the window goes (Win32/Wine order). */
    if(SH->wh.capture && !after.capture && IsWindow(static_cast<HWND>(wh_real(h)))) {
        u32 previous=SH->wh.capture;SH->wh.capture=0;capture_changed(previous,0);
    }
    act_scope_t scope;act_begin(&scope,touches(a));
    BOOL ok=r_DestroyWindow(wh_real(h));
    act_end(&scope);
    if(ok) {
        v_commit(after);
        retire(h);
        for(u32 i=0;i<SH->wh.count;i++)
            if(SH->wh.entry[i].live && SH->wh.entry[i].owner==(u32)(uintptr_t)h &&
               (SH->wh.entry[i].kind==WH_DC || SH->wh.entry[i].kind==WH_IMC))retire(alias_for_slot(i));
    }math_scope_end(&fp);return ok;
}
DECL(ReleaseDC);
static int WINAPI w_ReleaseDC(HWND h,HDC d) {
    if(retired_identity(h,WH_WINDOW)||retired_identity(d,WH_DC))return 0;
    math_host_scope_t fp;math_scope_begin(&fp,MATH_HOST_FP|MATH_HOST_ID);
    HWND real=wh_real(h);int own=real && (GetClassLongA(real,GCL_STYLE)&(CS_OWNDC|CS_CLASSDC));
    int ok=r_ReleaseDC(real,wh_real(d));if(ok && !own)retire(d);math_scope_end(&fp);return ok;
}
DECL(wglDeleteContext);
static BOOL WINAPI w_wglDeleteContext(HGLRC h) {
    math_host_scope_t fp;math_scope_begin(&fp,MATH_HOST_FP|MATH_HOST_ID);
    BOOL ok=r_wglDeleteContext(wh_real(h));if(ok)retire(h);math_scope_end(&fp);return ok;
}
DECL(DestroyCursor);
static BOOL WINAPI w_DestroyCursor(HCURSOR h) {
    if(retired_identity(h,WH_CURSOR)){SetLastError(ERROR_INVALID_CURSOR_HANDLE);return FALSE;}
    BOOL ok=r_DestroyCursor(wh_real(h));if(ok)retire(h);return ok;
}
DECL(GetCursorInfo);
static BOOL WINAPI w_GetCursorInfo(PCURSORINFO p) {
    DWORD entry_error=GetLastError();
    BOOL ok=r_GetCursorInfo(p);DWORD host_error=GetLastError();
    if(ok)p->hCursor=(HCURSOR)(uintptr_t)SH->wh.cursor; /* the model's shape, never the host's */
    /* Like successful GetDC, a successful logical cursor query has no
     * extended-error result; native failure still reports its own error. */
    SetLastError(ok?entry_error:host_error);return ok;
}
DECL(SetWindowLongA);
static LONG WINAPI w_SetWindowLongA(HWND h,int n,LONG v) {
    if(n==GWL_WNDPROC) {
        SetLastError(0);
        LONG old=r_SetWindowLongA(wh_real(h),n,(LONG)msg_wrap_wndproc((WNDPROC)v));
        u32 i=index_of(h);
        if(i<WH_CAP && (old || !GetLastError())) SH->wh.entry[i].wndproc=(u32)v;
        return (LONG)msg_unwrap_wndproc((WNDPROC)old);
    }
    u32 i=game_window(h);
    LONG r=r_SetWindowLongA(wh_real(h),n,n==GWL_HWNDPARENT?(LONG)wh_real((void *)v):v);
    if(i<WH_CAP && (n==GWL_STYLE || n==GWL_EXSTYLE)) {
        /* The game's own style write sets the virtual host-changeable bits;
         * WS_EX_TOPMOST changes only through SetWindowPos, as in USER. The
         * previous value returned keeps the model's bits too. */
        wh_desc_t *d=&SH->wh.desc[i];
        if(n==GWL_STYLE){r=(LONG)(((u32)r&~WH_HOST_STYLE)|(d->style&WH_HOST_STYLE));set_style_bits(i,WH_HOST_STYLE,(u32)v&WH_HOST_STYLE);}
        else r=(LONG)(((u32)r&~WH_HOST_EXSTYLE)|(d->exstyle&WH_HOST_EXSTYLE));
    }
    return n==GWL_HWNDPARENT?(LONG)wh_alias((void *)r,WH_WINDOW):r;
}
DECL(GetWindowLongA);
static LONG WINAPI w_GetWindowLongA(HWND h,int n) {
    LONG r=r_GetWindowLongA(wh_real(h),n);if(n==GWL_WNDPROC)return (LONG)msg_unwrap_wndproc((WNDPROC)r);
    u32 i=game_window(h);
    if(i<WH_CAP && n==GWL_STYLE)r=(LONG)(((u32)r&~WH_HOST_STYLE)|(SH->wh.desc[i].style&WH_HOST_STYLE));
    if(i<WH_CAP && n==GWL_EXSTYLE)r=(LONG)(((u32)r&~WH_HOST_EXSTYLE)|(SH->wh.desc[i].exstyle&WH_HOST_EXSTYLE));
    return n==GWL_HWNDPARENT?(LONG)wh_alias((void *)r,WH_WINDOW):r;
}
DECL(TranslateMessage);
static BOOL WINAPI w_TranslateMessage(const MSG *m) { BOOL result; if (msg_translate_synth(m,&result)) return result; MSG copy=*m; wh_msg_real(&copy); return r_TranslateMessage(&copy); }
DECL(DispatchMessageA);
static LRESULT WINAPI w_DispatchMessageA(const MSG *m) { MSG copy=*m; wh_msg_real(&copy); msg_game_enter(copy.message); LRESULT r=r_DispatchMessageA(&copy); msg_game_leave(); return r; }
DECL(GetMessageA);
static BOOL WINAPI w_GetMessageA(LPMSG m,HWND h,UINT a,UINT b) {
    BOOL r=r_GetMessageA(m,wh_real(h),a,b);if(r>=0) wh_msg_alias(m);return r;
}
DECL(DefWindowProcA); DECL(SendMessageA); DECL(PostMessageA);
static LRESULT WINAPI w_DefWindowProcA(HWND h,UINT m,WPARAM w,LPARAM l) {
    math_host_scope_t fp;math_scope_begin(&fp,MATH_HOST_FP|MATH_HOST_ID);
    message_copy_t c;message_args(m,&w,&l,0,&c);LRESULT r=r_DefWindowProcA(wh_real(h),m,w,l);message_done(&c,0);math_scope_end(&fp);return r;
}
static LRESULT WINAPI w_SendMessageA(HWND h,UINT m,WPARAM w,LPARAM l) {
    message_copy_t c;message_args(m,&w,&l,0,&c);HWND real=wh_real(h);msg_game_enter(m);LRESULT r=r_SendMessageA(real,m,w,l);msg_game_leave();message_done(&c,0);return r;
}
static BOOL WINAPI w_PostMessageA(HWND h,UINT m,WPARAM w,LPARAM l) {
    message_copy_t c;message_args(m,&w,&l,0,&c);
    if(c.kind) { SetLastError(ERROR_MESSAGE_SYNC_ONLY); return FALSE; }
    return r_PostMessageA(wh_real(h),m,w,l);
}
/* Bink receives the game's HWND directly, outside USER32. */
static void *(WINAPI *r_BinkBufferOpen)(HWND,u32,u32,u32);
static void *WINAPI w_BinkBufferOpen(HWND h,u32 w,u32 z,u32 f) { return r_BinkBufferOpen(wh_real(h),w,z,f); }
/* IME has an HWND->HIMC bridge. Keep the HIMC opaque but deterministic too. */
static HANDLE (WINAPI *r_ImmGetContext)(HWND);
static BOOL (WINAPI *r_ImmReleaseContext)(HWND,HANDLE);
static BOOL (WINAPI *r_ImmGetOpenStatus)(HANDLE);
static DWORD (WINAPI *r_ImmGetCandidateListA)(HANDLE,DWORD,void *,DWORD);
static LONG (WINAPI *r_ImmGetCompositionStringA)(HANDLE,DWORD,void *,DWORD);
static HANDLE WINAPI w_ImmGetContext(HWND h) { return owned(r_ImmGetContext(wh_real(h)),WH_IMC,h); }
static BOOL WINAPI w_ImmReleaseContext(HWND h,HANDLE c) { return r_ImmReleaseContext(wh_real(h),wh_real(c)); }
/* IME open state, candidates and composition text belong to the host's input
 * method; the game's text input is the score. A closed IME with no
 * composition is the fixed stand-in (the handle is still validated). */
static BOOL WINAPI w_ImmGetOpenStatus(HANDLE c) { (void)wh_real(c); return FALSE; }
static DWORD WINAPI w_ImmGetCandidateListA(HANDLE c,DWORD i,void *p,DWORD n) { (void)wh_real(c); return 0; }
static LONG WINAPI w_ImmGetCompositionStringA(HANDLE c,DWORD i,void *p,DWORD n) { (void)wh_real(c); return 0; }

/* WGL extension procedures have typed handle arguments too. Their wrappers
 * keep fixed code addresses, while real targets are rebound in each process. */
static BOOL (WINAPI *r_wglChoosePixelFormatARB)(HDC,const int *,const FLOAT *,UINT,int *,UINT *);
static BOOL WINAPI x_wglChoosePixelFormatARB(HDC d,const int *a,const FLOAT *b,UINT n,int *p,UINT *c) {
    return r_wglChoosePixelFormatARB(wh_real(d),a,b,n,p,c);
}
static BOOL (WINAPI *r_wglGetPixelFormatAttribivARB)(HDC,int,int,UINT,const int *,int *);
static BOOL WINAPI x_wglGetPixelFormatAttribivARB(HDC d,int p,int l,UINT n,const int *a,int *v) {
    return r_wglGetPixelFormatAttribivARB(wh_real(d),p,l,n,a,v);
}
static BOOL (WINAPI *r_wglGetPixelFormatAttribfvARB)(HDC,int,int,UINT,const int *,FLOAT *);
static BOOL WINAPI x_wglGetPixelFormatAttribfvARB(HDC d,int p,int l,UINT n,const int *a,FLOAT *v) {
    return r_wglGetPixelFormatAttribfvARB(wh_real(d),p,l,n,a,v);
}
static HANDLE (WINAPI *r_wglCreatePbufferARB)(HDC,int,int,int,const int *);
static HANDLE WINAPI x_wglCreatePbufferARB(HDC d,int f,int w,int h,const int *a) {
    HANDLE v=owned(r_wglCreatePbufferARB(wh_real(d),f,w,h,a),WH_PBUFFER,d);u32 i=index_of(v);
    if(i<WH_CAP){wh_desc_t *s=&SH->wh.desc[i];s->recipe=WR_PBUFFER;s->pixel=1;s->w=w;s->h=h;
        if(!r_DescribePixelFormat(wh_real(d),f,sizeof s->pfd,&s->pfd)){fail("pbuffer pixel format has no descriptor");TerminateProcess(GetCurrentProcess(),6);}
        attributes(s->attrib,a);save_desc(i);}return v;
}
static HDC (WINAPI *r_wglGetPbufferDCARB)(HANDLE);
static HDC WINAPI x_wglGetPbufferDCARB(HANDLE p) {
    HDC a=owned(r_wglGetPbufferDCARB(wh_real(p)),WH_DC,p);u32 i=index_of(a);
    if(i<WH_CAP){SH->wh.desc[i].recipe=WR_PBUFFERDC;save_desc(i);}return a;
}
static int (WINAPI *r_wglReleasePbufferDCARB)(HANDLE,HDC);
static int WINAPI x_wglReleasePbufferDCARB(HANDLE p,HDC d) { int ok=r_wglReleasePbufferDCARB(wh_real(p),wh_real(d));if(ok)retire(d);return ok; }
static BOOL (WINAPI *r_wglDestroyPbufferARB)(HANDLE);
static BOOL WINAPI x_wglDestroyPbufferARB(HANDLE p) { BOOL ok=r_wglDestroyPbufferARB(wh_real(p));if(ok)retire(p);return ok; }
static BOOL (WINAPI *r_wglQueryPbufferARB)(HANDLE,int,int *);
static BOOL WINAPI x_wglQueryPbufferARB(HANDLE p,int a,int *v) { return r_wglQueryPbufferARB(wh_real(p),a,v); }
static BOOL (WINAPI *r_wglBindTexImageARB)(HANDLE,int);
static BOOL WINAPI x_wglBindTexImageARB(HANDLE p,int b) {
    BOOL ok=r_wglBindTexImageARB(wh_real(p),b);u32 i=index_of(p);
    if(ok&&i<WH_CAP){
        wh_desc_t *d=&SH->wh.desc[i];int target=0;
        void (WINAPI *get)(u32,int *)=reinterpret_cast<decltype(get)>(GetProcAddress(GetModuleHandleA("opengl32.dll"),"glGetIntegerv"));
        if(!r_wglQueryPbufferARB(wh_real(p),0x2073,&target)||!get){fail("cannot capture pbuffer texture binding");TerminateProcess(GetCurrentProcess(),6);}
        d->texture_target=target==0x2079?0x0de1:target==0x207a?0x8513:0;
        if(!d->texture_target){fail("unsupported pbuffer texture target");TerminateProcess(GetCurrentProcess(),6);}
        get(d->texture_target==0x8513?0x8514:0x8069,(int *)&d->texture_name);
        d->texture_name=gl_tex_logical_name(d->texture_name);
        get(0x84e0,(int *)&d->texture_unit);
        d->texture_bound=1;d->texture_buffer=b;save_desc(i);
    }return ok;
}
static BOOL (WINAPI *r_wglReleaseTexImageARB)(HANDLE,int);
static BOOL WINAPI x_wglReleaseTexImageARB(HANDLE p,int b) { BOOL ok=r_wglReleaseTexImageARB(wh_real(p),b);u32 i=index_of(p);if(ok&&i<WH_CAP){SH->wh.desc[i].texture_bound=0;save_desc(i);}return ok; }
static BOOL (WINAPI *r_wglSetPbufferAttribARB)(HANDLE,const int *);
static BOOL WINAPI x_wglSetPbufferAttribARB(HANDLE p,const int *a) { BOOL ok=r_wglSetPbufferAttribARB(wh_real(p),a);u32 i=index_of(p);if(ok&&i<WH_CAP){attributes(SH->wh.desc[i].mutable_attrib,a);save_desc(i);}return ok; }
typedef struct { const char *name; void *hook; void **target; } extension_t;
#define X(n) {#n,(void *)x_##n,(void **)&r_##n}
static extension_t extensions[]={
    X(wglChoosePixelFormatARB),X(wglGetPixelFormatAttribivARB),X(wglGetPixelFormatAttribfvARB),
    X(wglCreatePbufferARB),X(wglGetPbufferDCARB),X(wglReleasePbufferDCARB),X(wglDestroyPbufferARB),
    X(wglQueryPbufferARB),X(wglBindTexImageARB),X(wglReleaseTexImageARB),X(wglSetPbufferAttribARB),
};
void *wh_gl_proc(const char *name,void *target) {
    if(g_bnd_native) return NULL;
    for(u32 i=0;i<sizeof extensions/sizeof extensions[0];i++) if(k_streq(name,extensions[i].name)) {
        if(!target || (uintptr_t)target<=3 || (uintptr_t)target==0xffffffffu) return NULL;
        SH->wh.extensions_used |= 1u<<i;
        *extensions[i].target=target;return extensions[i].hook;
    }
    return NULL;
}

typedef struct { const char *dll,*name; void *hook; void **real; } hook_t;
#define H(d,n) {d,#n,(void *)w_##n,(void **)&r_##n}
static hook_t hooks[] = {
#define U(n) H("user32.dll",n)
    U(CreateWindowExA),U(DestroyWindow),U(GetDesktopWindow),U(FindWindowA),U(GetParent),U(GetAncestor),
    U(GetActiveWindow),U(GetForegroundWindow),U(GetFocus),U(SetFocus),U(SetCapture),U(GetCapture),
    U(SetActiveWindow),U(SetForegroundWindow),U(ReleaseCapture),U(GetCursor),
    U(SetWindowTextA),U(EnableWindow),U(ShowWindow),U(IsWindow),U(GetWindowRect),U(GetClientRect),U(GetWindowInfo),
    U(ClientToScreen),U(ScreenToClient),U(MoveWindow),U(SetWindowPos),U(MessageBoxA),U(MonitorFromWindow),
    U(GetMonitorInfoA),U(GetDC),U(GetWindowDC),U(ReleaseDC),U(LoadCursorA),U(LoadIconA),U(SetCursor),U(DestroyCursor),
    U(GetCursorInfo),U(SetWindowLongA),U(GetWindowLongA),U(TranslateMessage),U(DispatchMessageA),U(GetMessageA),
    U(DefWindowProcA),U(SendMessageA),U(PostMessageA),
#define G(n) H("gdi32.dll",n)
    G(ChoosePixelFormat),G(DescribePixelFormat),G(SetPixelFormat),G(SwapBuffers),G(PatBlt),G(SetDeviceGammaRamp),G(GetDeviceGammaRamp),G(GetStockObject),
#define W(n) H("opengl32.dll",n)
    W(wglCreateContext),W(wglMakeCurrent),W(wglShareLists),W(wglGetCurrentDC),W(wglGetCurrentContext),W(wglDeleteContext),
#define I(n) H("imm32.dll",n)
    I(ImmGetContext),I(ImmReleaseContext),I(ImmGetOpenStatus),I(ImmGetCandidateListA),I(ImmGetCompositionStringA),
    {"binkw32.dll","_BinkBufferOpen@16",(void *)w_BinkBufferOpen,(void **)&r_BinkBufferOpen},
};
FARPROC wh_proc(const char *name) {
    if (g_bnd_native) return NULL;
    for(u32 i=0;i<sizeof hooks/sizeof hooks[0];i++) if(k_streq(name,hooks[i].name) && *hooks[i].real) return (FARPROC)hooks[i].hook;
    return NULL;
}
void wh_install(void) {
    if(g_bnd_native) return;
    for(u32 c=0;c<3;c++)for(u32 i=0;i<256;i++)SH->wh.gamma[c][i]=(u16)(i*257u);
    HMODULE exe=GetModuleHandleA(NULL);
    for(u32 i=0;i<sizeof hooks/sizeof hooks[0];i++) {
        hook_t *h=&hooks[i];
        void *old=iat_hook(exe,h->dll,h->name,h->hook);
        *h->real=old?old:(void *)GetProcAddress(LoadLibraryA(h->dll),h->name);
        if(old) hooks_note(h->name);
    }
    HDC gamma_dc=r_GetDC(NULL);
    if(gamma_dc){host_set_gamma(gamma_dc,SH->wh.gamma);r_ReleaseDC(NULL,gamma_dc);}
}
/* Capture game-visible geometry through the already-installed geometry hooks.
 * Host-only reconstruction must not invoke game callbacks or mutate this table. */
enum { WHP_GEOMETRY,WHP_EXTRA,WHP_TITLE,WHP_DC,WHP_GLOBALS,WHP_TOTAL,WHP_COUNT };
static capture_profile_t g_wh_phase[WHP_COUNT];
static const char *WH_PHASE_NAMES[]={"window_geometry","extra_bytes","title_and_wndproc","dc_validity","globals","total"};
void wh_capture_profile_json(buf_t *b) {
    b_str(b,"{");
    for(u32 i=0;i<WHP_COUNT;i++) {
        if(i)b_str(b,",");b_jstr(b,WH_PHASE_NAMES[i]);b_str(b,":{\"calls\":");b_u64(b,g_wh_phase[i].calls);
        b_str(b,",\"total_us\":");b_u64(b,g_wh_phase[i].total_us);
        b_str(b,",\"max_us\":");b_u64(b,g_wh_phase[i].max_us);b_str(b,"}");
    }
    b_str(b,"}");
}
int wh_capture_state(void) {
    if(g_bnd_native || reconciling)return 1;
    u64 begin=glsync_profile_now(),stamp;
    for(u32 i=0;i<SH->wh.count;i++) {
        wh_entry_t *s=&SH->wh.entry[i];wh_desc_t *d=&SH->wh.desc[i];wh_real_t *r=&actual[i];
        if(!s->live)continue;
        if(s->kind==WH_WINDOW && !IsWindow(static_cast<HWND>(r->value))) { retire(alias_for_slot(i));continue; }
        if(s->kind==WH_DC) {
            stamp=glsync_profile_now();int valid=GetObjectType(static_cast<HGDIOBJ>(r->value))!=0;
            glsync_profile_record(&g_wh_phase[WHP_DC],stamp,glsync_profile_now());
            if(!valid){retire(alias_for_slot(i));continue;}
        }
        if(s->kind==WH_WINDOW && d->recipe==WR_WINDOW) {
            stamp=glsync_profile_now();
            /* Host-writable window facts are never read here. Placement and
             * size (window manager), minimized/maximized/visible style bits and
             * WS_EX_TOPMOST (_NET_WM_STATE) keep the values the game's own calls
             * set (see the activation model). The remaining style bits, user
             * data, parent, enabled state, extra bytes, title and WndProc
             * change only through the game's USER32 calls and are read back. */
            d->style=((u32)GetWindowLongA(static_cast<HWND>(r->value),GWL_STYLE)&~WH_HOST_STYLE)|(d->style&WH_HOST_STYLE);
            d->exstyle=((u32)GetWindowLongA(static_cast<HWND>(r->value),GWL_EXSTYLE)&~WH_HOST_EXSTYLE)|(d->exstyle&WH_HOST_EXSTYLE);
            d->userdata=GetWindowLongA(static_cast<HWND>(r->value),GWL_USERDATA);
            d->parent=(u32)(uintptr_t)wh_alias(GetParent(static_cast<HWND>(r->value)),WH_WINDOW);
            d->visible=(d->style&WS_VISIBLE)!=0;d->enabled=IsWindowEnabled(static_cast<HWND>(r->value));
            glsync_profile_record(&g_wh_phase[WHP_GEOMETRY],stamp,glsync_profile_now());
            stamp=glsync_profile_now();d->extra_size=GetClassLongA(static_cast<HWND>(r->value),GCL_CBWNDEXTRA);
            if(d->extra_size<0 || d->extra_size>(int)sizeof d->extra || d->extra_size%4) {
                fail("unsupported window extra-byte size");return 0;
            }
            for(int n=0;n<d->extra_size;n+=4)d->extra[n/4]=GetWindowLongA(static_cast<HWND>(r->value),n);
            glsync_profile_record(&g_wh_phase[WHP_EXTRA],stamp,glsync_profile_now());
            stamp=glsync_profile_now();
            if(GetWindowTextLengthA(static_cast<HWND>(r->value))>=WH_NAME){fail("window title too long");return 0;}
            memset(d->title,0,sizeof d->title);GetWindowTextA(static_cast<HWND>(r->value),d->title,WH_NAME);
            s->wndproc=(u32)(uintptr_t)msg_unwrap_wndproc((WNDPROC)GetWindowLongA(static_cast<HWND>(r->value),GWL_WNDPROC));
            glsync_profile_record(&g_wh_phase[WHP_TITLE],stamp,glsync_profile_now());
        }
        save_desc(i);
    }
    /* Focus, capture, activation and cursor are already the virtual values in
     * SH->wh; the host's real values are not game state and are never read. */
    stamp=glsync_profile_now();
    u64 end=glsync_profile_now();
    glsync_profile_record(&g_wh_phase[WHP_GLOBALS],stamp,end);
    glsync_profile_record(&g_wh_phase[WHP_TOTAL],begin,end);
    return 1;
}
static int same_identity(u32 i) {
    wh_entry_t *s=&SH->wh.entry[i];wh_real_t *r=&actual[i];
    return i<SH->wh.count && s->live && r->live && s->kind==r->kind && s->serial==r->serial && s->owner==r->owner;
}
static int dependency_ready(u32 alias) {
    u32 i=index_of((void *)(uintptr_t)alias);
    return i==WH_CAP ? !alias_serial((void *)(uintptr_t)alias) : same_identity(i);
}
static void *real_dependency(u32 alias) {
    u32 i=index_of((void *)(uintptr_t)alias);
    return i==WH_CAP?(alias_serial((void *)(uintptr_t)alias)?NULL:(void *)(uintptr_t)alias):actual[i].value;
}
static void *core_dependency(u32 alias) {
    u32 serial=alias_serial((void *)(uintptr_t)alias);
    if(!serial)return (void *)(uintptr_t)alias;
    for(u32 i=0;i<WH_CAP;i++)if(actual[i].serial==serial)return actual[i].value;
    return NULL;
}
static int release_actual(u32 i) {
    wh_real_t *r=&actual[i];wh_desc_t *d=&r->desc;
    if(!r->live)return 1;
    slog("window reconcile release slot=%u kind=%u recipe=%u owner=%x",i,r->kind,d->recipe,r->owner);
    BOOL ok=TRUE;
    switch(d->recipe) {
    case WR_WINDOW:ok=!IsWindow(static_cast<HWND>(r->value))||r_DestroyWindow(static_cast<HWND>(r->value));break;
    case WR_CONTEXT:ok=r_wglDeleteContext(static_cast<HGLRC>(r->value));break;
    case WR_DC:case WR_WINDOWDC:
        /* A DC dies with its window; a failed ReleaseDC on an already invalid
         * handle does not imply that a live resource remains. */
        if(GetObjectType(static_cast<HGDIOBJ>(r->value)))ok=r_ReleaseDC(static_cast<HWND>(core_dependency(r->owner)),static_cast<HDC>(r->value))!=0;
        break;
    case WR_PBUFFERDC:
        ok=r_wglReleasePbufferDCARB && r_wglReleasePbufferDCARB(core_dependency(r->owner),static_cast<HDC>(r->value))!=0;break;
    case WR_PBUFFER:ok=r_wglDestroyPbufferARB && r_wglDestroyPbufferARB(r->value);break;
    default: /* Borrowed host resources are not ours to destroy. */break;
    }
    if(ok)r->live=0;return ok;
}
static int create_actual(u32 i) {
    wh_entry_t *s=&SH->wh.entry[i];wh_desc_t *d=&SH->wh.desc[i];void *v=NULL;
    if(!dependency_ready(s->owner)||!dependency_ready(d->parent)||!dependency_ready(d->share))return 0;
    switch(d->recipe) {
    case WR_DESKTOP:v=GetDesktopWindow();break;
    case WR_WINDOW: {
        if(!dependency_ready((u32)(uintptr_t)d->wc.hIcon)||!dependency_ready((u32)(uintptr_t)d->wc.hCursor)||
           !dependency_ready((u32)(uintptr_t)d->wc.hbrBackground))return 0;
        LPCSTR cls=d->class_atom?(LPCSTR)(uintptr_t)d->class_atom:d->class_name;
        WNDCLASSA wc=d->wc,have;
        if(!GetClassInfoA((HINSTANCE)(uintptr_t)d->instance,cls,&have)) {
            wc.lpfnWndProc=msg_wrap_wndproc(wc.lpfnWndProc);
            wc.hIcon=static_cast<HICON>(real_dependency((u32)(uintptr_t)wc.hIcon));wc.hCursor=static_cast<HCURSOR>(real_dependency((u32)(uintptr_t)wc.hCursor));
            wc.hbrBackground=static_cast<HBRUSH>(real_dependency((u32)(uintptr_t)wc.hbrBackground));wc.lpszClassName=cls;
            if(d->class_atom || !RegisterClassA(&wc))return -1;
        }
        /* Never shown (and so activated) here: reconcile shows it without
         * activation after its attributes are applied. */
        v=r_CreateWindowExA(d->exstyle,cls,d->title,d->style&~WS_VISIBLE,d->x,d->y,d->w,d->h,
            static_cast<HWND>(real_dependency(d->parent)),(HMENU)(uintptr_t)d->menu,(HINSTANCE)(uintptr_t)d->instance,NULL);
        if(v && d->pixel) {
            HDC dc=r_GetDC(static_cast<HWND>(v));int format=dc?r_ChoosePixelFormat(dc,&d->pfd):0;
            BOOL ok=format && r_SetPixelFormat(dc,format,&d->pfd);
            if(dc)r_ReleaseDC(static_cast<HWND>(v),dc);
            if(!ok){r_DestroyWindow(static_cast<HWND>(v));v=NULL;}
        }
        break;
    }
    case WR_DC:v=r_GetDC(static_cast<HWND>(real_dependency(s->owner)));break;
    case WR_WINDOWDC:v=r_GetWindowDC(static_cast<HWND>(real_dependency(s->owner)));break;
    case WR_CONTEXT:v=r_wglCreateContext(static_cast<HDC>(real_dependency(s->owner)));
        if(v && d->share && !r_wglShareLists(static_cast<HGLRC>(real_dependency(d->share)),static_cast<HGLRC>(v))){r_wglDeleteContext(static_cast<HGLRC>(v));v=NULL;}break;
    case WR_PBUFFER: {
        /* Pixel-format indices belong to the current driver. Re-select from
         * the captured format requirements before recreating the pbuffer. */
        int f=0;UINT n=0;
        int a[]={0x202d,1,0x2010,1,0x2013,d->pfd.iPixelType==PFD_TYPE_RGBA?0x202b:0x202c,
            0x2014,d->pfd.cColorBits,0x201b,d->pfd.cAlphaBits,0x2022,d->pfd.cDepthBits,
            0x2023,d->pfd.cStencilBits,0};
        if(r_wglCreatePbufferARB && r_wglChoosePixelFormatARB &&
           r_wglChoosePixelFormatARB(static_cast<HDC>(real_dependency(s->owner)),a,NULL,1,&f,&n) && n)
            v=r_wglCreatePbufferARB(static_cast<HDC>(real_dependency(s->owner)),f,d->w,d->h,d->attrib);
        break;
    }
    case WR_PBUFFERDC:if(r_wglGetPbufferDCARB)v=r_wglGetPbufferDCARB(real_dependency(s->owner));break;
    case WR_STOCK:v=GetStockObject((int)d->resource_atom);break;
    case WR_CURSOR:v=LoadCursorA((HINSTANCE)(uintptr_t)d->instance,d->resource_atom?(LPCSTR)(uintptr_t)d->resource_atom:d->resource_name);break;
    case WR_ICON:v=LoadIconA((HINSTANCE)(uintptr_t)d->instance,d->resource_atom?(LPCSTR)(uintptr_t)d->resource_atom:d->resource_name);break;
    default:slog("window reconcile missing descriptor slot=%u kind=%u serial=%u owner=%x",i,s->kind,s->serial,s->owner);return -1;
    }
    if(!v){slog("window reconcile create failed slot=%u recipe=%u error=%u",i,d->recipe,GetLastError());return -1;}
    slog("window reconcile created slot=%u kind=%u recipe=%u real=%x",i,s->kind,d->recipe,(u32)(uintptr_t)v);
    actual[i]=wh_real_t{v,s->kind,s->serial,s->owner,1,*d};
    if(s->kind==WH_DC && d->pixel) {
        int current=GetPixelFormat(static_cast<HDC>(v));
        if(!current) {
            int format=r_ChoosePixelFormat(static_cast<HDC>(v),&d->pfd);
            if(!format || !r_SetPixelFormat(static_cast<HDC>(v),format,&d->pfd))return -1;
        }
    }
    return 1;
}
static int reconcile_resources(void) {
    u8 replace[WH_CAP]={0};int contexts_changed=0;
    for(u32 i=0;i<WH_CAP;i++) {
        replace[i]=actual[i].live && !same_identity(i);
        if(i<SH->wh.count && same_identity(i)) {
            if(actual[i].kind==WH_WINDOW && !IsWindow(static_cast<HWND>(actual[i].value)))replace[i]=1;
            if(actual[i].kind==WH_DC && !GetObjectType(static_cast<HGDIOBJ>(actual[i].value)))replace[i]=1;
        }
    }
    /* A child with an unchanged alias still needs rebuilding if its owner's
     * host object is replaced. Propagate before releasing anything. */
    for(u32 pass=0;pass<WH_CAP;pass++) {
        int changed=0;
        for(u32 i=0;i<WH_CAP;i++)if(actual[i].live && !replace[i]) {
            u32 deps[]={actual[i].owner,actual[i].desc.parent,actual[i].desc.share};
            for(u32 k=0;k<3;k++){
                u32 serial=alias_serial((void *)(uintptr_t)deps[k]);
                for(u32 dep=0;serial && dep<WH_CAP;dep++)if(actual[dep].serial==serial && replace[dep]){replace[i]=1;changed=1;break;}
                if(replace[i])break;
            }
        }
        if(!changed)break;
    }
    for(u32 i=0;i<WH_CAP;i++)if(actual[i].live && actual[i].desc.recipe==WR_PBUFFER && actual[i].desc.texture_bound) {
        if(!r_wglReleaseTexImageARB || !r_wglReleaseTexImageARB(actual[i].value,actual[i].desc.texture_buffer))return 0;
        actual[i].desc.texture_bound=0;
    }
    if(!r_wglMakeCurrent(NULL,NULL))return 0;
    /* Children are allocated after owners in the observed resource paths. Use
     * dependency checks too, so slot order is not an implicit correctness rule. */
    for(u32 pass=0;pass<WH_CAP;pass++) {
        int remaining=0,progress=0;
        for(u32 n=WH_CAP;n>0;n--) {u32 i=n-1;if(!replace[i]||!actual[i].live)continue;
            int child=0;
            for(u32 j=0;j<WH_CAP;j++)if(j!=i&&replace[j]&&actual[j].live) {
                u32 a=WH_BASE+(actual[i].serial-1)*4;
                if(actual[j].owner==a||actual[j].desc.parent==a||actual[j].desc.share==a){child=1;break;}
            }
            if(child){remaining++;continue;}
            if(actual[i].kind==WH_GL)contexts_changed=1;
            if(!release_actual(i))return 0;progress=1;
        }
        if(!remaining)break;if(!progress)return 0;
    }
    for(u32 pass=0;pass<WH_CAP;pass++) {
        int remaining=0,progress=0;
        for(u32 i=0;i<SH->wh.count;i++)if(SH->wh.entry[i].live&&!same_identity(i)) {
            int rc=create_actual(i);if(rc<0){fail("resource creation descriptor cannot be replayed");return 0;}
            if(rc){progress=1;if(SH->wh.entry[i].kind==WH_GL)contexts_changed=1;}else remaining++;
        }
        if(!remaining)break;if(!progress){fail("resource dependency cycle or missing owner");return 0;}
    }
    for(u32 i=0;i<SH->wh.count;i++) {
        wh_entry_t *s=&SH->wh.entry[i];wh_desc_t *d=&SH->wh.desc[i];void *v=actual[i].value;
        if(!s->live){
            actual[i]=wh_real_t{nullptr,s->kind,s->serial,s->owner,0,*d};
            continue;
        }
        if(!same_identity(i))return 0;
        if(s->kind==WH_WINDOW && d->recipe==WR_WINDOW) {
            SetLastError(0);
            if(!SetWindowLongA(static_cast<HWND>(v),GWL_WNDPROC,(LONG)msg_wrap_wndproc((WNDPROC)(uintptr_t)s->wndproc))&&GetLastError())return 0;
            SetLastError(0);
            /* Visibility changes only through the ShowWindow below, which never
             * activates; a style write must not show the window by itself. */
            DWORD shown=(DWORD)GetWindowLongA(static_cast<HWND>(v),GWL_STYLE)&WS_VISIBLE;
            SetLastError(0);
            if(!SetWindowLongA(static_cast<HWND>(v),GWL_STYLE,(d->style&~WS_VISIBLE)|shown)&&GetLastError())return 0;
            SetLastError(0);
            if(!SetWindowLongA(static_cast<HWND>(v),GWL_EXSTYLE,d->exstyle)&&GetLastError())return 0;
            if(GetParent(static_cast<HWND>(v))!=real_dependency(d->parent)) {
                SetLastError(0);
                if(d->style&WS_CHILD){if(!SetParent(static_cast<HWND>(v),static_cast<HWND>(real_dependency(d->parent)))&&GetLastError())return 0;}
                else if(!SetWindowLongA(static_cast<HWND>(v),GWL_HWNDPARENT,(LONG)real_dependency(d->parent))&&GetLastError())return 0;
            }
            SetLastError(0);if(!SetWindowLongA(static_cast<HWND>(v),GWL_USERDATA,d->userdata)&&GetLastError())return 0;
            for(int n=0;n<d->extra_size;n+=4){SetLastError(0);if(!SetWindowLongA(static_cast<HWND>(v),n,d->extra[n/4])&&GetLastError())return 0;}
            if(!r_SetWindowTextA(static_cast<HWND>(v),d->title))return 0;
            r_EnableWindow(static_cast<HWND>(v),d->enabled);
            if(!r_SetWindowPos(static_cast<HWND>(v),NULL,d->x,d->y,d->w,d->h,SWP_NOZORDER|SWP_NOACTIVATE|SWP_FRAMECHANGED))return 0;
            r_ShowWindow(static_cast<HWND>(v),d->visible?SW_SHOWNOACTIVATE:SW_HIDE);
        }
    }
    /* Focus, activation, capture and cursor are restored with SH->wh and stay
     * virtual. The host's real focus belongs to the user's desktop: restore
     * never takes it, and never moves the real cursor shape. */
    HDC gamma_dc=r_GetDC(NULL);
    if(gamma_dc){host_set_gamma(gamma_dc,SH->wh.gamma);r_ReleaseDC(NULL,gamma_dc);}
    if(!r_wglMakeCurrent(static_cast<HDC>(real_dependency(SH->wh.current_dc)),static_cast<HGLRC>(real_dependency(SH->wh.current_gl))))return 0;
    if(contexts_changed) {
        glsync_real_context_reset();
        if(!glati_restorable() || !gllists_restorable()) {
            fail("recreated GL context has incomplete ATI shader or display-list definitions");
            return 0;
        }
    }
    bindings_pending=1;
    return 1;
}

static int resolve_restore_extensions(void) {
    void *(WINAPI *resolve)(const char *)=reinterpret_cast<decltype(resolve)>(GetProcAddress(GetModuleHandleA("opengl32.dll"),"wglGetProcAddress"));
    for(u32 i=0;i<sizeof extensions/sizeof extensions[0];i++) {
        /* Resolve all known entrypoints, including ones first requested after boot
         * in the recorded process. Missing unused entries remain unavailable. */
        void *p=resolve?resolve(extensions[i].name):NULL;
        if((uintptr_t)p<=3 || (uintptr_t)p==0xffffffffu) p=NULL;
        if(!p && (SH->wh.extensions_used & (1u<<i))) {
            fail("restored WGL extension is unavailable");return 0;
        }
        *extensions[i].target=p;
    }
    return 1;
}

int wh_restore_validate(void) {
    if(g_bnd_native)return 1;
    if(SH->wh.count>WH_CAP||SH->wh.callback_count>4)return 0;
    for(u32 i=0;i<SH->wh.count;i++) {
        wh_desc_t *d=&SH->wh.desc[i];
        if(d->recipe>WR_ICON || d->extra_size<0 || d->extra_size>(int)sizeof d->extra || d->extra_size%4 ||
           d->class_name[WH_NAME-1] || d->title[WH_NAME-1] || d->resource_name[WH_NAME-1])return 0;
        if(SH->wh.entry[i].live && !dependency_ready(SH->wh.entry[i].owner)) {
            u32 owner=index_of((void *)(uintptr_t)SH->wh.entry[i].owner);
            if(owner>=SH->wh.count || !SH->wh.entry[owner].live)return 0;
        }
    }
    /* The current bootstrap/future context must resolve dependencies before
     * reconciliation unbinds it or attempts to recreate a target pbuffer. */
    if(!resolve_restore_extensions())return 0;
    reconciling=1;
    int ok=reconcile_resources();
    reconciling=0;
    if(!ok){fail("resource reconciliation failed");return 0;}
    if(!resolve_restore_extensions())return 0;
    failed=0;return 1;
}

/* Called by glsync only after texture bytes and context state were rebuilt. */
int wh_restore_gl_bindings(void) {
    if(g_bnd_native || !bindings_pending)return 1;
    HMODULE gl=GetModuleHandleA("opengl32.dll");
    void (WINAPI *get)(u32,int *)=reinterpret_cast<decltype(get)>(GetProcAddress(gl,"glGetIntegerv"));
    void (WINAPI *bind)(u32,u32)=reinterpret_cast<decltype(bind)>(GetProcAddress(gl,"glBindTexture"));
    void *(WINAPI *proc)(const char *)=reinterpret_cast<decltype(proc)>(GetProcAddress(gl,"wglGetProcAddress"));
    void (WINAPI *active)(u32)=proc?reinterpret_cast<decltype(active)>(proc("glActiveTextureARB")):nullptr;
    for(u32 i=0;i<SH->wh.count;i++) {
        wh_entry_t *e=&SH->wh.entry[i];wh_desc_t *d=&SH->wh.desc[i];
        if(!e->live || d->recipe!=WR_PBUFFER)continue;
        if(d->mutable_attrib[0] && (!r_wglSetPbufferAttribARB || !r_wglSetPbufferAttribARB(actual[i].value,d->mutable_attrib)))return 0;
        if(d->texture_bound){
            if(!get||!bind||(uintptr_t)active<=3||(uintptr_t)active==0xffffffffu||!r_wglBindTexImageARB)return 0;
            int unit=0,texture=0;get(0x84e0,&unit);active(d->texture_unit);
            u32 query=d->texture_target==0x8513?0x8514:0x8069;get(query,&texture);
            bind(d->texture_target,gl_tex_real_name(d->texture_name));
            BOOL ok=r_wglBindTexImageARB(actual[i].value,d->texture_buffer);
            bind(d->texture_target,texture);active(unit);if(!ok)return 0;
        }
        save_desc(i);
    }
    bindings_pending=0;
    return 1;
}

} /* extern "C" */
