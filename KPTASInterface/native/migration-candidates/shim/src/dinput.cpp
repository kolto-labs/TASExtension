/* Virtual DirectInput 8 (contract section 6, "DirectInput"): a fake
 * IDirectInput8A with keyboard and mouse IDirectInputDevice8A objects fed
 * from score notes at the gate. Wine's dinput is never acquired.
 *
 * K1 GOG 1.03 usage (CExoRawInputInternal 0x5e3e80, 0x5e3fa0, 0x5e3ae0, 0x5e4050):
 *   keyboard: c_dfDIKeyboard, buffered GetDeviceData(20-byte records, 256), NONEXCLUSIVE|FOREGROUND
 *   mouse:    c_dfDIMouse, immediate GetDeviceState(16), buffer flushed only. */
#include "shim.hpp"

static constexpr HRESULT DI_OK = 0;
static constexpr HRESULT DIERR_NOTACQUIRED = 0x8007000Cu;
static constexpr HRESULT DIERR_DEVICENOTREG = 0x80040154u;
static constexpr HRESULT DIERR_UNSUPPORTED = 0x80004001u;
static constexpr HRESULT DIERR_INVALIDPARAM = 0x80070057u;
static constexpr HRESULT DIERR_OBJECTNOTFOUND = 0x80070002u;
static constexpr DWORD DIGDD_PEEK = 1;

/* The device objects and every input state value live in harness_game
 * (shim_hg): the game holds pointers to the objects and sees their reference
 * counts, queue contents and sequence numbers, so a restore rewinds them. */
#define g_kbd (SH->kbd)
#define g_mouse (SH->mouse)
#define g_di (SH->di)
#define g_keys (SH->keys)
#define g_btn (SH->btn)
#define g_acc_x (SH->acc_x)
#define g_acc_y (SH->acc_y)
#define g_acc_z (SH->acc_z)
#define g_seq (SH->seq)
#define g_vcur_x (SH->vcur_x)
#define g_vcur_y (SH->vcur_y)
u32 g_di_reads_kbd, g_di_reads_mouse;

/* These counters describe the interval closed by a gate. They are diagnostic
 * observations, not rewindable game state. */
struct di_obs_t {
    u32 state_calls, state_nonzero;
    u32 data_calls, data_exposed, data_dequeued, data_peeked;
};
static di_obs_t g_di_obs_kbd, g_di_obs_mouse;
static u32 g_di_staged, g_di_stage_dropped;

static u32 sat_add(u32 a, u32 b) { return 0xffffffffu - a < b ? 0xffffffffu : a + b; }

static int q_push(didev_t *d, u32 ofs, u32 data, u32 ts) {
    if (d->qn >= QCAP) return 0;
    didod_t *e = &d->q[(d->qh + d->qn) % QCAP];
    e->ofs = ofs; e->data = data; e->ts = ts; e->seq = ++g_seq; e->app = 0;
    d->qn++;
    return 1;
}

/* ---- IUnknown ---- */
static HRESULT WINAPI u_QI(void *self, REFIID riid, void **out) {
    (void)riid;
    if (!out) return E_POINTER;
    *out = self;
    return S_OK;
}
static ULONG WINAPI u_AddRef(void *self) { return static_cast<ULONG>(InterlockedIncrement(&static_cast<di_t *>(self)->ref)); }
static ULONG WINAPI u_Release(void *self) {
    LONG r = InterlockedDecrement(&static_cast<di_t *>(self)->ref);
    return r > 0 ? (ULONG)r : 0;  /* static objects: never freed */
}

/* ---- IDirectInput8A ---- */
static HRESULT WINAPI di_CreateDevice(void *self, REFGUID g, void **out, void *outer) {
    (void)self; (void)outer;
    if (!out) return DIERR_INVALIDPARAM;
    const u8 *b = reinterpret_cast<const u8 *>(&g);
    static const u8 tail[12] = {0xA0, 0xD5, 0xCF, 0x11, 0xBF, 0xC7, 0x44, 0x45, 0x53, 0x54, 0x00, 0x00};
    u32 d1;
    memcpy(&d1, b, sizeof d1);
    if (memcmp(b + 4, tail, 12) == 0 && (d1 == 0x6F1D2B61u || d1 == 0x6F1D2B60u)) {
        didev_t *d = d1 == 0x6F1D2B61u ? &g_kbd : &g_mouse;
        d->ref++;
        *out = d;
        slog("DI CreateDevice %s", d1 == 0x6F1D2B61u ? "keyboard" : "mouse");
        return DI_OK;
    }
    *out = nullptr;
    return DIERR_DEVICENOTREG;
}
static HRESULT WINAPI di_EnumDevices(void *s, DWORD t, void *cb, void *ref, DWORD f) {
    (void)s; (void)t; (void)cb; (void)ref; (void)f;
    return DI_OK;  /* no joysticks */
}
static HRESULT WINAPI di_GetDeviceStatus(void *s, REFGUID g) { (void)s; (void)g; return DI_OK; }
static HRESULT WINAPI di_RunControlPanel(void *s, HWND h, DWORD f) { (void)s; (void)h; (void)f; return DI_OK; }
static HRESULT WINAPI di_Initialize(void *s, HINSTANCE h, DWORD v) { (void)s; (void)h; (void)v; return DI_OK; }
static HRESULT WINAPI di_FindDevice(void *s, REFGUID g, LPCSTR n, GUID *o) {
    (void)s; (void)g; (void)n; (void)o;
    return DIERR_DEVICENOTREG;
}
static HRESULT WINAPI di_EnumBySem(void *s, LPCSTR a, void *b, void *c, void *d, DWORD e) {
    (void)s; (void)a; (void)b; (void)c; (void)d; (void)e;
    return DI_OK;
}
static HRESULT WINAPI di_Configure(void *s, void *a, void *b, DWORD c, void *d) {
    (void)s; (void)a; (void)b; (void)c; (void)d;
    return DI_OK;
}
static void *g_di_vtbl[] = {
    shim_abi::erase_function(u_QI), shim_abi::erase_function(u_AddRef), shim_abi::erase_function(u_Release), shim_abi::erase_function(di_CreateDevice), shim_abi::erase_function(di_EnumDevices),
    shim_abi::erase_function(di_GetDeviceStatus), shim_abi::erase_function(di_RunControlPanel), shim_abi::erase_function(di_Initialize), shim_abi::erase_function(di_FindDevice),
    shim_abi::erase_function(di_EnumBySem), shim_abi::erase_function(di_Configure),
};

/* ---- IDirectInputDevice8A ---- */
static HRESULT WINAPI dv_GetCapabilities(void *s, void *caps) {
    didev_t *d = static_cast<didev_t *>(s);
    u32 *c = static_cast<u32 *>(caps);  /* DIDEVCAPS: dwSize, dwFlags, dwDevType, dwAxes, dwButtons, dwPOVs, ... */
    if (!c || c[0] < 24) return DIERR_INVALIDPARAM;
    u32 sz = c[0];
    memset(c + 1, 0, sz - 4);
    c[1] = 0x1;  /* DIDC_ATTACHED */
    if (d->kind == 1) { c[2] = 0x13; c[4] = 128; }  /* DI8DEVTYPE_KEYBOARD */
    else { c[2] = 0x12; c[3] = 3; c[4] = 3; }       /* DI8DEVTYPE_MOUSE */
    return DI_OK;
}
static HRESULT WINAPI dv_EnumObjects(void *s, void *cb, void *ref, DWORD f) {
    (void)s; (void)cb; (void)ref; (void)f;
    return DI_OK;
}
static HRESULT WINAPI dv_GetProperty(void *s, REFGUID g, void *ph) {
    (void)s; (void)g; (void)ph;
    return DIERR_UNSUPPORTED;
}
static HRESULT WINAPI dv_SetProperty(void *s, REFGUID g, const void *ph) {
    (void)s; (void)g; (void)ph;
    return DI_OK;
}
static HRESULT WINAPI dv_Acquire(void *s) { (void)s; return DI_OK; }
static HRESULT WINAPI dv_Unacquire(void *s) { (void)s; return DI_OK; }
static HRESULT WINAPI dv_GetDeviceState(void *s, DWORD cb, void *out) {
    didev_t *d = static_cast<didev_t *>(s);
    if (!out) return DIERR_INVALIDPARAM;
    memset(out, 0, cb);
    if (d->kind == 1) {
        g_di_obs_kbd.state_calls = sat_add(g_di_obs_kbd.state_calls, 1);
        memcpy(out, g_keys, cb < 256 ? cb : 256);
        for (u32 i = 0; i < cb && i < 256; i++) {
            if (static_cast<u8 *>(out)[i]) {
                g_di_obs_kbd.state_nonzero = sat_add(g_di_obs_kbd.state_nonzero, 1);
                break;
            }
        }
    } else {
        g_di_reads_mouse++;
        g_di_obs_mouse.state_calls = sat_add(g_di_obs_mouse.state_calls, 1);
        i32 st[4] = {g_acc_x, g_acc_y, g_acc_z, 0};
        u8 *o = static_cast<u8 *>(out);
        memcpy(o, st, cb < 12 ? cb : 12);
        for (u32 i = 12; i < cb && i < 20; i++) o[i] = g_btn[(i - 12) & 3];
        int nonzero = 0;
        for (u32 i = 0; i < cb && i < 20; i++) {
            if (o[i]) { nonzero = 1; break; }
        }
        if (nonzero) g_di_obs_mouse.state_nonzero = sat_add(g_di_obs_mouse.state_nonzero, 1);
        g_acc_x = g_acc_y = g_acc_z = 0;
    }
    return DI_OK;
}
static HRESULT WINAPI dv_GetDeviceData(void *s, DWORD cbrec, void *recs, DWORD *inout, DWORD flags) {
    didev_t *d = static_cast<didev_t *>(s);
    if (!inout) return DIERR_INVALIDPARAM;
    di_obs_t *obs = d->kind == 1 ? &g_di_obs_kbd : &g_di_obs_mouse;
    obs->data_calls = sat_add(obs->data_calls, 1);
    if (d->kind == 1) g_di_reads_kbd++;
    u32 want = *inout;
    u32 k = want < d->qn ? want : d->qn;
    if (recs) {
        u8 *o = static_cast<u8 *>(recs);
        for (u32 i = 0; i < k; i++) {
            memset(o + i * cbrec, 0, cbrec);
            memcpy(o + i * cbrec, &d->q[(d->qh + i) % QCAP], cbrec < 20 ? cbrec : 20);
        }
    }
    if (recs) obs->data_exposed = sat_add(obs->data_exposed, k);
    if (flags & DIGDD_PEEK) {
        if (recs) obs->data_peeked = sat_add(obs->data_peeked, k);
    } else {
        obs->data_dequeued = sat_add(obs->data_dequeued, k);
        d->qh = (d->qh + k) % QCAP; d->qn -= k;
    }
    *inout = k;
    return DI_OK;
}
static HRESULT WINAPI dv_SetDataFormat(void *s, const void *fmt) {
    didev_t *d = static_cast<didev_t *>(s);
    if (fmt) d->fmt_size = static_cast<const u32 *>(fmt)[3];
    return DI_OK;
}
static HRESULT WINAPI dv_SetEventNotification(void *s, HANDLE h) { (void)s; (void)h; return DI_OK; }
static HRESULT WINAPI dv_SetCooperativeLevel(void *s, HWND h, DWORD f) { (void)s; (void)h; (void)f; return DI_OK; }
static HRESULT WINAPI dv_GetObjectInfo(void *s, void *o, DWORD a, DWORD b) {
    (void)s; (void)o; (void)a; (void)b;
    return DIERR_OBJECTNOTFOUND;
}
static HRESULT WINAPI dv_GetDeviceInfo(void *s, void *o) { (void)s; (void)o; return DIERR_UNSUPPORTED; }
static HRESULT WINAPI dv_RunControlPanel(void *s, HWND h, DWORD f) { (void)s; (void)h; (void)f; return DI_OK; }
static HRESULT WINAPI dv_Initialize(void *s, HINSTANCE h, DWORD v, REFGUID g) {
    (void)s; (void)h; (void)v; (void)g;
    return DI_OK;
}
static HRESULT WINAPI dv_CreateEffect(void *s, REFGUID g, const void *e, void **o, void *u) {
    (void)s; (void)g; (void)e; (void)u;
    if (o) *o = nullptr;
    return DIERR_UNSUPPORTED;
}
static HRESULT WINAPI dv_EnumEffects(void *s, void *cb, void *r, DWORD t) { (void)s; (void)cb; (void)r; (void)t; return DI_OK; }
static HRESULT WINAPI dv_GetEffectInfo(void *s, void *e, REFGUID g) { (void)s; (void)e; (void)g; return DIERR_UNSUPPORTED; }
static HRESULT WINAPI dv_GetFFState(void *s, DWORD *o) { (void)s; if (o) *o = 0; return DIERR_UNSUPPORTED; }
static HRESULT WINAPI dv_SendFFCmd(void *s, DWORD f) { (void)s; (void)f; return DIERR_UNSUPPORTED; }
static HRESULT WINAPI dv_EnumCreated(void *s, void *cb, void *r, DWORD f) { (void)s; (void)cb; (void)r; (void)f; return DI_OK; }
static HRESULT WINAPI dv_Escape(void *s, void *e) { (void)s; (void)e; return DIERR_UNSUPPORTED; }
static HRESULT WINAPI dv_Poll(void *s) { (void)s; return DI_OK; }
static HRESULT WINAPI dv_SendDeviceData(void *s, DWORD cb, const void *d, DWORD *io, DWORD f) {
    (void)s; (void)cb; (void)d; (void)f;
    if (io) *io = 0;
    return DI_OK;
}
static HRESULT WINAPI dv_EnumEffectsInFile(void *s, LPCSTR n, void *cb, void *r, DWORD f) {
    (void)s; (void)n; (void)cb; (void)r; (void)f;
    return DI_OK;
}
static HRESULT WINAPI dv_WriteEffectToFile(void *s, LPCSTR n, DWORD c, void *e, DWORD f) {
    (void)s; (void)n; (void)c; (void)e; (void)f;
    return DIERR_UNSUPPORTED;
}
static HRESULT WINAPI dv_BuildActionMap(void *s, void *a, LPCSTR u, DWORD f) { (void)s; (void)a; (void)u; (void)f; return DIERR_UNSUPPORTED; }
static HRESULT WINAPI dv_SetActionMap(void *s, void *a, LPCSTR u, DWORD f) { (void)s; (void)a; (void)u; (void)f; return DIERR_UNSUPPORTED; }
static HRESULT WINAPI dv_GetImageInfo(void *s, void *h) { (void)s; (void)h; return DIERR_UNSUPPORTED; }

static void *g_dev_vtbl[] = {
    shim_abi::erase_function(u_QI), shim_abi::erase_function(u_AddRef), shim_abi::erase_function(u_Release), shim_abi::erase_function(dv_GetCapabilities), shim_abi::erase_function(dv_EnumObjects),
    shim_abi::erase_function(dv_GetProperty), shim_abi::erase_function(dv_SetProperty), shim_abi::erase_function(dv_Acquire), shim_abi::erase_function(dv_Unacquire),
    shim_abi::erase_function(dv_GetDeviceState), shim_abi::erase_function(dv_GetDeviceData), shim_abi::erase_function(dv_SetDataFormat),
    shim_abi::erase_function(dv_SetEventNotification), shim_abi::erase_function(dv_SetCooperativeLevel), shim_abi::erase_function(dv_GetObjectInfo),
    shim_abi::erase_function(dv_GetDeviceInfo), shim_abi::erase_function(dv_RunControlPanel), shim_abi::erase_function(dv_Initialize), shim_abi::erase_function(dv_CreateEffect),
    shim_abi::erase_function(dv_EnumEffects), shim_abi::erase_function(dv_GetEffectInfo), shim_abi::erase_function(dv_GetFFState), shim_abi::erase_function(dv_SendFFCmd),
    shim_abi::erase_function(dv_EnumCreated), shim_abi::erase_function(dv_Escape), shim_abi::erase_function(dv_Poll), shim_abi::erase_function(dv_SendDeviceData),
    shim_abi::erase_function(dv_EnumEffectsInFile), shim_abi::erase_function(dv_WriteEffectToFile), shim_abi::erase_function(dv_BuildActionMap),
    shim_abi::erase_function(dv_SetActionMap), shim_abi::erase_function(dv_GetImageInfo),
};

void di_init_objects(void) {
    g_di.vtbl = g_di_vtbl;
    g_di.ref = 0;
    g_kbd.vtbl = g_dev_vtbl;
    g_kbd.kind = 1;
    g_mouse.vtbl = g_dev_vtbl;
    g_mouse.kind = 2;
}

HRESULT di_create(void **out) {
    g_di.ref = 1;
    *out = &g_di;
    slog("DirectInput8Create -> virtual IDirectInput8A");
    return DI_OK;
}

void di_set_cursor(int x, int y) { g_vcur_x = x; g_vcur_y = y; }

/* Stage the input for the next tick (contract 5.3 step 7). */
void di_stage(const input_t *in, int first_tick, u32 time_ms, u8 hash_out[16], u8 sha256_out[32]) {
    u32 nev = 0;
    for (int pass = 0; pass < 2; pass++) {  /* releases first, then presses, ascending DIK */
        for (int k = 1; k < 256; k++) {
            int want = (in->keys[k >> 3] >> (k & 7)) & 1;
            int have = g_keys[k] != 0;
            if (want == have) continue;
            if (pass == 0 && !want) { g_keys[k] = 0; if (q_push(&g_kbd, (u32)k, 0, time_ms)) g_di_staged = sat_add(g_di_staged, 1); else g_di_stage_dropped = sat_add(g_di_stage_dropped, 1); nev++; }
            if (pass == 1 && want) { g_keys[k] = 0x80; if (q_push(&g_kbd, (u32)k, 0x80, time_ms)) g_di_staged = sat_add(g_di_staged, 1); else g_di_stage_dropped = sat_add(g_di_stage_dropped, 1); nev++; }
        }
    }
    i32 dx = 0, dy = 0, dz = 0;
    if (in->has_abs) {
        dx = in->abs_x - g_vcur_x;
        dy = in->abs_y - g_vcur_y;
        g_vcur_x = in->abs_x;
        g_vcur_y = in->abs_y;
    }
    dx += in->rel_x;
    dy += in->rel_y;
    if (first_tick && in->wheel) dz = in->wheel * 120;
    g_acc_x += dx;
    g_acc_y += dy;
    g_acc_z += dz;
    /* GUI mouse input reaches the game as window messages (WinMessageHandler
     * 0x402800 -> CExoInput::BufferEvent), so notes also stage client-space
     * WM_MOUSEMOVE and button messages, in a fixed order. */
    u32 mk = 0;
    for (int b = 0; b < 3; b++) if ((in->buttons >> b) & 1) mk |= (b == 0 ? MK_LBUTTON : b == 1 ? MK_RBUTTON : MK_MBUTTON);
    LPARAM lp = (LPARAM)(((u32)(u16)g_vcur_y << 16) | (u16)g_vcur_x);
    if (dx || dy || (in->has_abs && first_tick)) { msg_synth_push(WM_MOUSEMOVE, mk, lp); nev++; }
    static const UINT down[3] = {WM_LBUTTONDOWN, WM_RBUTTONDOWN, WM_MBUTTONDOWN};
    static const UINT up[3] = {WM_LBUTTONUP, WM_RBUTTONUP, WM_MBUTTONUP};
    for (int b = 0; b < 3; b++) {
        u8 v = (in->buttons >> b) & 1 ? 0x80 : 0;
        if (v != g_btn[b]) {
            g_btn[b] = v;
            if (q_push(&g_mouse, 12u + static_cast<u32>(b), v, time_ms)) g_di_staged = sat_add(g_di_staged, 1); else g_di_stage_dropped = sat_add(g_di_stage_dropped, 1);
            msg_synth_push(v ? down[b] : up[b], mk, lp);
            nev++;
        }
    }
    struct {
        u8 keys[256];
        u8 btn[4];
        i32 dx, dy, dz;
        u32 nev;
        i32 cx, cy;
    } frame;
    memcpy(frame.keys, g_keys, 256);
    memcpy(frame.btn, g_btn, 4);
    frame.dx = dx; frame.dy = dy; frame.dz = dz; frame.nev = nev;
    frame.cx = g_vcur_x; frame.cy = g_vcur_y;
    blake2s128(&frame, sizeof frame, hash_out);
    sha256(&frame, sizeof frame, sha256_out);
}

/* Append an honest per-gate API-observation receipt. "dequeued" means removed
 * by GetDeviceData, not proof that game logic acted on the event.
 * GetDeviceState is reported separately as a poll. */
void di_observation_json(buf_t *b, u64 tick) {
    b_str(b, "{\"keyboard\":{\"data_polls\":"); b_u64(b, g_di_obs_kbd.data_calls);
    b_str(b, ",\"records_dequeued\":"); b_u64(b, g_di_obs_kbd.data_dequeued);
    b_str(b, ",\"records_exposed\":"); b_u64(b, g_di_obs_kbd.data_exposed);
    b_str(b, ",\"records_peeked\":"); b_u64(b, g_di_obs_kbd.data_peeked);
    b_str(b, ",\"state_nonzero_polls\":"); b_u64(b, g_di_obs_kbd.state_nonzero);
    b_str(b, ",\"state_polls\":"); b_u64(b, g_di_obs_kbd.state_calls);
    b_str(b, "},\"mouse\":{\"data_polls\":"); b_u64(b, g_di_obs_mouse.data_calls);
    b_str(b, ",\"records_dequeued\":"); b_u64(b, g_di_obs_mouse.data_dequeued);
    b_str(b, ",\"records_exposed\":"); b_u64(b, g_di_obs_mouse.data_exposed);
    b_str(b, ",\"records_peeked\":"); b_u64(b, g_di_obs_mouse.data_peeked);
    b_str(b, ",\"state_nonzero_polls\":"); b_u64(b, g_di_obs_mouse.state_nonzero);
    b_str(b, ",\"state_polls\":"); b_u64(b, g_di_obs_mouse.state_calls);
    b_str(b, "},\"schema\":\"telos.directinput-observation/v1\",\"stage_queue_drops\":"); b_u64(b, g_di_stage_dropped);
    b_str(b, ",\"staged_records\":"); b_u64(b, g_di_staged);
    b_str(b, ",\"tick\":"); b_u64(b, tick);
    b_str(b, "}");
}

void di_observation_reset(void) {
    memset(&g_di_obs_kbd, 0, sizeof g_di_obs_kbd);
    memset(&g_di_obs_mouse, 0, sizeof g_di_obs_mouse);
    g_di_staged = g_di_stage_dropped = 0;
}
