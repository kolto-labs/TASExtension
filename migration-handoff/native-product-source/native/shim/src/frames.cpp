/* Frame capture (contract 9, "Frame ring"): one frame per tick, output only.
 *
 * At every SwapBuffers the back buffer is read with glReadPixels into one
 * reused buffer, before the present runs or is skipped. At gate n the last
 * frame read during tick n goes into the driver's frame ring. A tick without
 * a present repeats the frame before it (REPEAT); before the first read, and
 * after a restore, the frame is black (BLANK).
 *
 * The cost is llvmpipe drawing the frame, which a skipped present never does.
 * glFinish before the read splits that wait from the copy. A read into a
 * pixel-pack buffer does not hide it: on llvmpipe glReadPixels into one blocks
 * until the frame is drawn, just as a plain read does (measured: 274 ms in the
 * read, 0.4 ms in the later fetch).
 *
 * Capture must not change the game, so nothing here exists only while it is
 * on and is visible between gates:
 *   - the buffer comes from harness_core, which DllMain commits in every run;
 *   - the ring is a file the driver creates. It is opened, written with
 *     WriteFile at fixed offsets and closed inside each gate, so the handle
 *     table and the address space are the same with capture on and off;
 *   - it is turned on by the frames verb, never by a KPTAS_* variable (the
 *     environment block is copied into the arena);
 *   - the hook makes the same calls on the game stack with capture on and
 *     off. The present and the gate's PeekMessageA are both called from
 *     WinMain at one depth, so what those calls leave below the present stays
 *     in the gate frame's unwritten slots, which are live and hashed: when
 *     only the on path made them, the stack's top page (0x3111f000) differed
 *     from tick 1.
 * The read runs on the harness stack and puts back what it changes: the read
 * buffer, pixel-store state, the pixel-pack buffer binding, the x87
 * environment, MXCSR and LastError. It never calls glGetError and passes only
 * valid arguments, so the game's pending GL error (it polls glGetError) is
 * left alone. */
#include "shim.hpp"

/* Fixed C hook ABI and assembly-referenced symbol names. */
extern "C" {

#define GLAPI __attribute__((stdcall))
typedef unsigned int GLenum_;
typedef int GLint_;
typedef int GLsizei_;
typedef unsigned int GLuint_;

#define GL_BACK_ 0x0405u
#define GL_READ_BUFFER_ 0x0C02u
#define GL_PACK_SWAP_BYTES_ 0x0D00u
#define GL_PACK_LSB_FIRST_ 0x0D01u
#define GL_PACK_ROW_LENGTH_ 0x0D02u
#define GL_PACK_SKIP_ROWS_ 0x0D03u
#define GL_PACK_SKIP_PIXELS_ 0x0D04u
#define GL_PACK_ALIGNMENT_ 0x0D05u
#define GL_BGRA_ 0x80E1u
#define GL_UNSIGNED_BYTE_ 0x1401u
#define GL_PIXEL_PACK_BUFFER_ 0x88EBu
#define GL_PIXEL_PACK_BUFFER_BINDING_ 0x88EDu

/* ring layout (contract 9) */
#define FR_HDR 4096u
#define FR_SLOT_HDR 4096u
#define FH_WRITTEN 16u
#define FH_READ 24u
#define FH_PPID 32u
#define FH_STATE 36u /* 1 active, 2 orderly stop, 3 consumer failure */
#define FL_REPEAT 1u
#define FL_BLANK 2u
#define FL_CROPPED 4u
#define BLANK_W g_bnd_window_w
#define BLANK_H g_bnd_window_h

typedef struct __attribute__((packed)) {
    u64 seq, tick;
    u32 w, h, format, swaps, reads, flags, read_us, skipped, finish_us;
} fr_slot_t;

static void (GLAPI *gl_ReadPixels)(GLint_, GLint_, GLsizei_, GLsizei_, GLenum_, GLenum_, void *);
static void (GLAPI *gl_GetIntegerv)(GLenum_, GLint_ *);
static void (GLAPI *gl_ReadBuffer)(GLenum_);
static void (GLAPI *gl_PixelStorei)(GLenum_, GLint_);
static void (GLAPI *gl_Finish)(void);
static HDC (GLAPI *wgl_GetCurrentDC)(void);
static void *(GLAPI *wgl_GetProcAddress)(const char *);
static void (GLAPI *gl_BindBuffer)(GLenum_, GLuint_);
static int g_resolved;

/* the frame being written: what one read or one tick produced */
typedef struct { u32 w, h, flags, swaps, reads, skipped, read_us, finish_us; u64 tick; } frame_t;

static int g_on;
static WCHAR g_path[520];
static u32 g_slots, g_slot_bytes;
static u8 *g_buf;                    /* the latest frame in memory */
static u32 g_buf_cap;
static u64 g_written;
static frame_t g_last;               /* what g_buf holds */
static int g_restored_view;
static int g_have;                   /* g_buf holds a frame of this timeline */
static frame_t g_tk;                 /* this tick so far */
/* totals (diagnostics) */
static u64 g_n_frames, g_n_repeat, g_n_blank, g_n_reads, g_n_swaps, g_n_skipped;
static u64 g_read_us_sum, g_finish_us_sum, g_write_us_sum, g_wait_us_sum;
static u32 g_read_us_max, g_finish_us_max, g_write_us_max;
static char g_err[96];

static u64 qpc_us(u64 d) {
    static LARGE_INTEGER f;
    if (!f.QuadPart) QueryPerformanceFrequency(&f);
    return d * 1000000ull / (u64)f.QuadPart;
}
static u64 qpc(void) {
    LARGE_INTEGER t;
    QueryPerformanceCounter(&t);
    return (u64)t.QuadPart;
}

static int resolve(void) {
    if (g_resolved) return g_resolved > 0;
    HMODULE m = GetModuleHandleA("opengl32.dll");
    g_resolved = -1;
    if (!m) return 0;
    gl_ReadPixels = reinterpret_cast<decltype(gl_ReadPixels)>(GetProcAddress(m, "glReadPixels"));
    gl_GetIntegerv = reinterpret_cast<decltype(gl_GetIntegerv)>(GetProcAddress(m, "glGetIntegerv"));
    gl_ReadBuffer = reinterpret_cast<decltype(gl_ReadBuffer)>(GetProcAddress(m, "glReadBuffer"));
    gl_PixelStorei = reinterpret_cast<decltype(gl_PixelStorei)>(GetProcAddress(m, "glPixelStorei"));
    gl_Finish = reinterpret_cast<decltype(gl_Finish)>(GetProcAddress(m, "glFinish"));
    wgl_GetCurrentDC = reinterpret_cast<decltype(wgl_GetCurrentDC)>(GetProcAddress(m, "wglGetCurrentDC"));
    wgl_GetProcAddress = reinterpret_cast<decltype(wgl_GetProcAddress)>(GetProcAddress(m, "wglGetProcAddress"));
    if (!gl_ReadPixels || !gl_GetIntegerv || !gl_ReadBuffer || !gl_PixelStorei || !gl_Finish || !wgl_GetCurrentDC ||
        !wgl_GetProcAddress)
        return 0;
    /* null when the context lacks buffer objects; then no pack buffer can be bound */
    gl_BindBuffer = reinterpret_cast<decltype(gl_BindBuffer)>(wgl_GetProcAddress("glBindBufferARB"));
    g_resolved = 1;
    return 1;
}

static const GLenum_ PACK[6] = {GL_PACK_SWAP_BYTES_, GL_PACK_LSB_FIRST_, GL_PACK_ROW_LENGTH_,
                                GL_PACK_SKIP_ROWS_, GL_PACK_SKIP_PIXELS_, GL_PACK_ALIGNMENT_};
static const GLint_ PACK_WANT[6] = {0, 0, 0, 0, 0, 4};

/* The GL state a read changes, saved and put back around it. */
typedef struct { u8 fenv[28]; u32 mx; GLint_ rb, pk[6], pbo; } glsave_t;

static void save_fpu(glsave_t *s) {
    __asm__ volatile("fnstenv %0" : "=m"(s->fenv));
    __asm__ volatile("stmxcsr %0" : "=m"(s->mx));
}
static void load_fpu(glsave_t *s) {
    __asm__ volatile("ldmxcsr %0" : : "m"(s->mx));
    __asm__ volatile("fldenv %0" : : "m"(s->fenv));
}

static u32 g_skip_logs;
static void skip(const char *why, HDC dc, HDC cur, HWND wnd, const RECT *rc) {
    g_tk.skipped++;
    if (g_skip_logs++ < 8)
        slog("frames: present at tick %u not read (%s): dc %x current %x hwnd %x client %dx%d", (u32)g_gate.tick,
             why, (u32)(uintptr_t)dc, (u32)(uintptr_t)cur, (u32)(uintptr_t)wnd, rc ? (int)rc->right : 0,
             rc ? (int)rc->bottom : 0);
}

static void note_times(u32 read_us, u32 finish_us) {
    g_n_reads++;
    g_read_us_sum += read_us;
    g_finish_us_sum += finish_us;
    if (read_us > g_read_us_max) g_read_us_max = read_us;
    if (finish_us > g_finish_us_max) g_finish_us_max = finish_us;
}

/* Preview throttle: frame n is viewed (read back, and the HUD refreshed at
 * its gate) on preview ticks and when a held gate shows it and it is
 * render-due (rule above wire_render_due). Other ticks repeat the last read
 * frame in the ring. Output only, as capture off. */
static int frame_view_due(u64 tick) {
    return render_preview_due(tick) || (wire_frame_held() && wire_render_due(tick));
}

/* On the harness stack, with the game's context current. */
static void read_fn(void *arg) {
    HDC dc = (HDC)arg;
    if (!g_on) return;
    if (!frame_view_due(g_gate.tick)) return;
    if (!resolve()) { skip("opengl32 entry points missing", dc, 0, 0, 0); return; }
    /* The game presents through its own GetDC handle, which is not the handle it made current, so compare
     * windows: a pbuffer DC has none. */
    HDC cur = wgl_GetCurrentDC();
    HWND wnd = WindowFromDC(dc);
    if (!cur || !wnd || WindowFromDC(cur) != wnd) { skip("another drawable is current", dc, cur, wnd, 0); return; }
    RECT rc = {0, 0, 0, 0};
    if (!GetClientRect(wnd, &rc) || rc.right <= 0 || rc.bottom <= 0) {
        skip("no client area", dc, cur, wnd, &rc);
        return;
    }
    u32 w = (u32)rc.right, h = (u32)rc.bottom, fl = 0;
    if (w * 4u > g_buf_cap) { skip("wider than the ring slot", dc, cur, wnd, &rc); return; }
    if ((u64)w * h * 4u > g_buf_cap) { h = g_buf_cap / (w * 4u); fl |= FL_CROPPED; }

    glsave_t s;
    save_fpu(&s);
    s.rb = 0;
    s.pbo = 0;
    gl_GetIntegerv(GL_READ_BUFFER_, &s.rb);
    for (int i = 0; i < 6; i++) gl_GetIntegerv(PACK[i], &s.pk[i]);
    if (gl_BindBuffer) gl_GetIntegerv(GL_PIXEL_PACK_BUFFER_BINDING_, &s.pbo);
    if (s.rb != (GLint_)GL_BACK_) gl_ReadBuffer(GL_BACK_);
    for (int i = 0; i < 6; i++) if (s.pk[i] != PACK_WANT[i]) gl_PixelStorei(PACK[i], PACK_WANT[i]);
    if (s.pbo) gl_BindBuffer(GL_PIXEL_PACK_BUFFER_, 0);
    /* glReadPixels waits for the frame anyway; finishing first splits drawing from the copy */
    u64 t0 = qpc();
    gl_Finish();
    u64 t1 = qpc();
    gl_ReadPixels(0, 0, (GLsizei_)w, (GLsizei_)h, GL_BGRA_, GL_UNSIGNED_BYTE_, g_buf);
    u32 us = (u32)qpc_us(qpc() - t1), fus = (u32)qpc_us(t1 - t0);
    if (s.pbo) gl_BindBuffer(GL_PIXEL_PACK_BUFFER_, (GLuint_)s.pbo);
    for (int i = 5; i >= 0; i--) if (s.pk[i] != PACK_WANT[i]) gl_PixelStorei(PACK[i], s.pk[i]);
    if (s.rb != (GLint_)GL_BACK_) gl_ReadBuffer((GLenum_)s.rb);
    load_fpu(&s);

    g_tk.w = w;
    g_tk.h = h;
    g_tk.flags = fl;
    g_tk.reads++;
    g_tk.read_us += us;
    g_tk.finish_us += fus;
    note_times(us, fus);
    g_tk.tick = g_gate.tick;  /* provenance of the actual back-buffer read */
    g_last = g_tk;
    g_have = 1;
    g_restored_view = 0;
}

/* SwapBuffers hook, game side (any slot), before the present. With capture off
 * it makes the same calls and read_fn returns at once (see the top). */
void frames_swap(HDC dc) {
    g_tk.swaps++;
    g_n_swaps++;
    DWORD le = GetLastError();
    kst_hcall(read_fn, (void *)dc);
    SetLastError(le);
}

static int f_rw(HANDLE f, int write, u32 off, void *p, u32 n) {
    OVERLAPPED o;
    memset(&o, 0, sizeof o);
    o.Offset = off;
    DWORD got = 0;
    BOOL ok = write ? WriteFile(f, p, n, &got, &o) : ReadFile(f, p, n, &got, &o);
    return ok && got == n;
}

static HANDLE ring_open(void) {
    return CreateFileW(g_path, GENERIC_READ | GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE, NULL,
                       OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, NULL);
}

static void fail(const char *why) {
    u32 n = (u32)strlen(why);
    if (n > sizeof g_err - 1) n = sizeof g_err - 1;
    memcpy(g_err, why, n);
    g_err[n] = 0;
    slog("frames: %s; capture off at frame %u", g_err, (u32)g_written);
    g_on = 0;
}

static void log_totals(const char *when) {
    slog("frames %s: %u frames (%u repeat, %u blank), %u swaps, %u reads (%u skipped), finish us mean %u max %u, "
         "read us mean %u max %u, write us mean %u max %u, wait us %u",
         when, (u32)g_n_frames, (u32)g_n_repeat, (u32)g_n_blank, (u32)g_n_swaps, (u32)g_n_reads, (u32)g_n_skipped,
         (u32)(g_n_reads ? g_finish_us_sum / g_n_reads : 0), g_finish_us_max,
         (u32)(g_n_reads ? g_read_us_sum / g_n_reads : 0), g_read_us_max,
         (u32)(g_n_frames ? g_write_us_sum / g_n_frames : 0), g_write_us_max, (u32)g_wait_us_sum);
}

/* g_buf as frame fr into the next slot. */
/* Return promptly to the common held gate on consumer failure. The wire
 * latches this output fault before reports; failure cannot acknowledge a tick. */
static int consumer_ready(HANDLE f) {
    u32 state=0;
    if(!f_rw(f,0,FH_STATE,&state,4))fail("cannot read consumer state");
    else if(state!=1)fail(state==3?"frame consumer failed":"frame consumer stopped");
    else return 1;
    return 0;
}
const char *frames_error(void) {
    if(g_on&&!g_err[0]) {
        HANDLE f=ring_open();
        if(f==INVALID_HANDLE_VALUE)fail("cannot open the ring for consumer health");
        else {consumer_ready(f);CloseHandle(f);}
    }
    return g_err;
}

static void put(const frame_t *fr) {
    u64 t0 = qpc();
    HANDLE f = ring_open();
    if (f == INVALID_HANDLE_VALUE) { fail("cannot open the ring"); return; }
    /* wait for a free slot: the driver drains on its own thread */
    u64 rd = 0, tw = 0;
    u32 spins = 0;
    for (;;) {
        if(!consumer_ready(f)){CloseHandle(f);return;}
        if (!f_rw(f, 0, FH_READ, &rd, 8)) { CloseHandle(f); fail("cannot read the ring header"); return; }
        if (g_written - rd < g_slots) break;
        if (!tw) tw = qpc();
        if (qpc_us(qpc() - tw) > 120000000ull) {
            CloseHandle(f);
            fail("the driver stopped draining for 120 s");
            return;
        }
        Sleep(spins++ < 64 ? 0 : 1);
    }
    if (tw) g_wait_us_sum += qpc_us(qpc() - tw);
    fr_slot_t s;
    memset(&s, 0, sizeof s);
    s.seq = g_written;
    s.tick = fr->tick;
    s.w = fr->w;
    s.h = fr->h;
    s.format = 1;        /* BGRA, rows bottom-up */
    s.swaps = fr->swaps;
    s.reads = fr->reads;
    s.flags = fr->flags;
    s.read_us = fr->read_us;
    s.skipped = fr->skipped;
    s.finish_us = fr->finish_us;
    u32 off = FR_HDR + (u32)(g_written % g_slots) * (FR_SLOT_HDR + g_slot_bytes);
    hud_pixels(g_buf, fr->w, fr->h, fr->tick);
    u32 n = fr->w * fr->h * 4u;
    u64 next = g_written + 1;
    if(!consumer_ready(f)){CloseHandle(f);return;}
    int ok = (!n || f_rw(f, 1, off + FR_SLOT_HDR, g_buf, n)) && f_rw(f, 1, off, &s, sizeof s) &&
             f_rw(f, 1, FH_WRITTEN, &next, 8);
    if(!consumer_ready(f)){CloseHandle(f);return;}
    CloseHandle(f);
    if (!ok) { fail("cannot write the ring"); return; }
    g_written = next;
    g_n_frames++;
    g_n_skipped += fr->skipped;
    if (fr->flags & FL_REPEAT) g_n_repeat++;
    u32 us = (u32)qpc_us(qpc() - t0);
    g_write_us_sum += us;
    if (us > g_write_us_max) g_write_us_max = us;
    if (g_n_frames % 1000 == 0) log_totals("so far");
}

/* A tick that read nothing: the frame before it again, or black before the first read. */
static void put_repeat(frame_t *tk) {
    if (!g_have) {
        g_last.w = BLANK_W;
        g_last.h = BLANK_H;
        if ((u64)BLANK_W * BLANK_H * 4u > g_buf_cap) g_last.w = g_last.h = 0;
        memset(g_buf, 0, g_last.w * g_last.h * 4u);
        g_last.flags = FL_BLANK;
        g_have = 1;
        g_n_blank++;
        tk->flags = FL_BLANK;
    } else tk->flags = (g_last.flags & (FL_BLANK | FL_CROPPED)) | FL_REPEAT;
    tk->w = g_last.w;
    tk->h = g_last.h;
    put(tk);
}

/* Gate n (harness stack): tick n's frame into the next slot. */
void frames_gate(u64 tick) {
    if (frame_view_due(tick)) hud_refresh();
    frame_t tk = g_tk;
    memset(&g_tk, 0, sizeof g_tk);
    if (!g_on) return;
    tk.tick = tick;
    if (tk.reads) put(&tk);
    else put_repeat(&tk);
}

/* After a restore: the back buffer and g_buf are from another timeline. */
void frames_restored(void) {
    hud_restored();
    g_have = 0;
    g_restored_view = 0;
    memset(&g_tk, 0, sizeof g_tk);
}

/* frames verb: attach to the ring at path. Returns 0 with *why on a refusal. */
int frames_attach(const char *path, u32 slots, u32 slot_bytes, const char **why) {
    if (!slots || !slot_bytes || slot_bytes % 4096u || slots > 64 ||
        (u64)FR_HDR + (u64)slots * (FR_SLOT_HDR + slot_bytes) > 0x7fffffffull) {
        *why = "slots 1..64 and slot_bytes a multiple of 4096";
        return 0;
    }
    if (!g_buf) {
        g_buf = (u8 *)core_alloc(slot_bytes);
        if (!g_buf) { *why = "slot_bytes exceeds the free harness_core"; return 0; }
        g_buf_cap = slot_bytes;
    } else if (slot_bytes > g_buf_cap) {
        *why = "slot_bytes exceeds the buffer of the first attach";
        return 0;
    }
    if (MultiByteToWideChar(CP_ACP, 0, path, -1, g_path, 520) <= 0) { *why = "path"; return 0; }
    HANDLE f = ring_open();
    if (f == INVALID_HANDLE_VALUE) { *why = "cannot open the ring"; return 0; }
    u32 h[4] = {0}, pid = GetCurrentProcessId();
    u64 wr = 0;u32 consumer=0;
    int ok = f_rw(f, 0, 0, h, sizeof h) && f_rw(f, 0, FH_WRITTEN, &wr, 8) && f_rw(f,0,FH_STATE,&consumer,4) && consumer==1 && f_rw(f, 1, FH_PPID, &pid, 4);
    CloseHandle(f);
    if (!ok || memcmp(h, "KPFR", 4) || h[1] != 1 || h[2] != slots || h[3] != slot_bytes) {
        *why = "ring header does not match (magic KPFR, version 1, slots, slot_bytes)";
        return 0;
    }
    g_slots = slots;
    g_slot_bytes = slot_bytes;
    g_written = wr;
    g_err[0] = 0;
    g_on = 1;
    slog("frames: on, ring %u slots of %u bytes, written %u", slots, slot_bytes, (u32)wr);
    return 1;
}

void frames_off(void) {
    if (g_on) log_totals("off");
    g_on = 0;
}

void frames_stats_json(buf_t *b) {
    b_str(b, "{\"enabled\":");
    b_str(b, g_on ? "true" : "false");
    b_str(b, ",\"written\":"); b_u64(b, g_written);
    b_str(b, ",\"frames\":"); b_u64(b, g_n_frames);
    b_str(b, ",\"repeat\":"); b_u64(b, g_n_repeat);
    b_str(b, ",\"blank\":"); b_u64(b, g_n_blank);
    b_str(b, ",\"swaps\":"); b_u64(b, g_n_swaps);
    b_str(b, ",\"reads\":"); b_u64(b, g_n_reads);
    b_str(b, ",\"skipped\":"); b_u64(b, g_n_skipped);
    b_str(b, ",\"finish_us_mean\":"); b_u64(b, g_n_reads ? g_finish_us_sum / g_n_reads : 0);
    b_str(b, ",\"finish_us_max\":"); b_u64(b, g_finish_us_max);
    b_str(b, ",\"read_us_mean\":"); b_u64(b, g_n_reads ? g_read_us_sum / g_n_reads : 0);
    b_str(b, ",\"read_us_max\":"); b_u64(b, g_read_us_max);
    b_str(b, ",\"write_us_mean\":"); b_u64(b, g_n_frames ? g_write_us_sum / g_n_frames : 0);
    b_str(b, ",\"write_us_max\":"); b_u64(b, g_write_us_max);
    b_str(b, ",\"wait_us\":"); b_u64(b, g_wait_us_sum);
    b_str(b, ",\"error\":"); b_jstr(b, g_err);
    b_ch(b, '}');
}

/* Snapshot what the player sees now, without issuing a new GL read: the last
 * captured frame with the HUD repainted for this gate's tick and label (the
 * panel is repainted whole, so this is idempotent). Before the first read and
 * after a restore the view is black, as the ring's BLANK frame is; the HUD then
 * says the game view was not refreshed. The host encodes this BGRA payload. */
const char *frames_snapshot(const char *path,u32 *w,u32 *h,u8 digest[32]) {
    if(!g_on)return "enable frame capture first";
    u32 fw=g_last.w,fh=g_last.h;
    if(!g_have||!fw||!fh){
        fw=BLANK_W;fh=BLANK_H;
        if(!fw||!fh||(u64)fw*fh*4>g_buf_cap)return "no frame and no blank size";
        memset(g_buf,0,fw*fh*4u);   /* g_have stays 0: the ring still writes this tick as BLANK */
    }
    hud_pixels(g_buf,fw,fh,g_gate.tick);
    u64 bytes=(u64)fw*fh*4;
    if(bytes>g_buf_cap||bytes>0xffffffffu)return "frame size exceeds capture buffer";
    HANDLE f=CreateFileA(path,GENERIC_WRITE,0,NULL,CREATE_NEW,FILE_ATTRIBUTE_NORMAL,NULL);
    if(f==INVALID_HANDLE_VALUE)return "cannot create snapshot (path must be new)";
    DWORD written=0;int ok=WriteFile(f,g_buf,(DWORD)bytes,&written,NULL)&&written==bytes;
    if(ok)ok=FlushFileBuffers(f);if(!CloseHandle(f))ok=0;
    if(!ok)return "cannot persist complete frame";
    sha256(g_buf,(u32)bytes,digest);*w=fw;*h=fh;return NULL;
}

/* Output provenance is separate from the held simulation gate. A restore
 * invalidates g_have, and a gate with no read repeats an older capture. */
void frames_snapshot_meta(buf_t *b) {
    int blank = !g_have || !g_last.w || !g_last.h || (g_last.flags & FL_BLANK);
    b_str(b, ",\"gate_tick\":"); b_u64(b, g_gate.tick);
    b_str(b, ",\"captured_tick\":");
    if (blank) b_str(b, "null"); else b_u64(b, g_last.tick);
    b_str(b, ",\"blank\":"); b_str(b, blank ? "true" : "false");
    b_str(b, ",\"repeat\":"); b_str(b, !blank && g_last.tick != g_gate.tick ? "true" : "false");
    b_str(b, ",\"cropped\":"); b_str(b, !blank && (g_last.flags & FL_CROPPED) ? "true" : "false");
    b_str(b, ",\"source\":"); b_jstr(b, blank ? "unavailable" : g_restored_view ? "restored_framebuffer" : "captured_back_buffer");
}

/* Verified reconstructed front buffer, read back by glsync at this gate.
 * Output cache only: no game tick, native swap or GL state is changed here. */
void frames_accept_restored(const u8 *rgba,u32 w,u32 h) {
    if(!g_on||!g_buf||(u64)w*h*4>g_buf_cap)return;
    for(u32 i=0;i<w*h;i++) {
        g_buf[i*4]=rgba[i*4+2];g_buf[i*4+1]=rgba[i*4+1];
        g_buf[i*4+2]=rgba[i*4];g_buf[i*4+3]=rgba[i*4+3];
    }
    memset(&g_last,0,sizeof g_last);g_last.w=w;g_last.h=h;g_last.tick=g_gate.tick;
    g_have=1;g_restored_view=1;
}

} /* extern "C" */
