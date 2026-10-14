#ifdef __cplusplus
extern "C" {
#endif
#ifndef KPTAS_WINHANDLES_H
#define KPTAS_WINHANDLES_H
/* Only deterministic identity belongs to the snapshot. OS resources stay in core. */
#define WH_CAP 256
#define WH_BASE 0x0f100000u
#define WH_ALIAS_END 0x10000000u
enum { WH_WINDOW = 1, WH_DC, WH_GL, WH_MONITOR, WH_IMC, WH_CURSOR, WH_ICON, WH_OBJECT, WH_PBUFFER };
typedef struct { u32 kind, serial, live, owner, wndproc; } wh_entry_t;
enum { WR_NONE, WR_WINDOW, WR_DESKTOP, WR_DC, WR_WINDOWDC, WR_CONTEXT, WR_PBUFFER, WR_PBUFFERDC, WR_STOCK, WR_CURSOR, WR_ICON };
#define WH_NAME 192
#define WH_EXTRA 64
#define WH_ATTR 64
typedef struct {
    u32 recipe, parent, share, style, exstyle, instance, menu, userdata;
    int x,y,w,h,pixel,visible,enabled,extra_size;
    LONG extra[WH_EXTRA];
    WNDCLASSA wc;
    PIXELFORMATDESCRIPTOR pfd;
    u32 class_atom, resource_atom, texture_bound, texture_target, texture_name, texture_unit;
    int attrib[WH_ATTR], mutable_attrib[WH_ATTR], texture_buffer;
    char class_name[WH_NAME],title[WH_NAME],resource_name[WH_NAME];
} wh_desc_t;
typedef struct { u32 count, serial, current_dc, current_gl, extensions_used, callback_count;
    u32 focus,capture,active,cursor;
    u16 gamma[3][256];
    wh_desc_t desc[WH_CAP]; WNDPROC callbacks[4]; wh_entry_t entry[WH_CAP]; } wh_state_t;
void *wh_real(void *value);
u32 wh_context_slot(void);
int wh_current_doublebuffer(void);
void *wh_window_real_if_live(void *value);
void *wh_alias(void *value, u32 kind);
void wh_msg_alias(MSG *message);
void wh_msg_real(MSG *message);
LRESULT wh_wndproc_call(WNDPROC proc, HWND window, UINT message, WPARAM w, LPARAM l);
/* Activation-class messages (msg.c admission; see the model in winhandles.c). */
enum { WH_ADM_ACTIVE = 1, WH_ADM_FOCUS = 2, WH_ADM_CAPTURE = 4, WH_ADM_CANCEL = 8 };
u32 wh_activation_class(UINT message);
u32 wh_activation_admit(void);
u32 wh_activation_depth(void);
int wh_restore_validate(void);
void wh_install(void);
int wh_capture_state(void);
int wh_reconciling(void);
int wh_restore_gl_bindings(void);
WNDPROC msg_wrap_wndproc(WNDPROC proc);
WNDPROC msg_unwrap_wndproc(WNDPROC proc);
FARPROC wh_proc(const char *name);
void *wh_gl_proc(const char *name, void *target);
#endif

#ifdef __cplusplus
} /* C ABI: mixed native candidate and assembly symbols */
#endif
