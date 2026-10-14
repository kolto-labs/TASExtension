/* Render skipping on unviewed, uncaptured frames (output and GPU work only).
 *
 * Modes come with every step and wait (render_skip, preview_every; absent =
 * "off" and 1, the behaviour before render skipping). All state here is host
 * state in plain shim statics: never SH, harness_game, the arena or the
 * journal.
 *
 * Which frames are render-due is defined once, above wire_render_due
 * (wire.cpp). The rule is one-way: only the tick, the capture grid and the
 * command's arguments can make a frame skippable. Every other input (a
 * latch, a foreign SwapBuffers, list compilation, UpdateScreen, a save, a
 * worker slot, native mode) can only force a frame to render.
 *
 *   "present": a frame that is not render-due does not call the real
 *     SwapBuffers (gate.cpp present_host).
 *   "draw": such a frame also returns from glDrawElements, glDrawArrays and
 *     glClear without the driver. Only arguments that cannot raise
 *     GL_INVALID_ENUM or GL_INVALID_VALUE are skipped. GL_INVALID_OPERATION
 *     (a call inside glBegin/glEnd, an incomplete program) is assumed not to
 *     arise, as the recorded game never raises it on these calls; glGetError
 *     is then the same with and without skipping.
 *
 * Texture shadow (glsync.cpp): a skip never suppresses a bind, a texture
 * parameter, an active-unit change or an attribute pop, so the shadow sees
 * every call it tracks. check_draw and render_note_copy query the driver
 * directly; check_draw changes the active unit and puts it back before it
 * returns.
 *
 * Guards fail closed. A violation latches a fault: skipping stops, and the
 * gate that closes the tick refuses the command before its capture and tick
 * push, then serves queries only (wire.cpp frame_fault_refuse). Faults:
 *   - glReadPixels on a draw-mode frame that is not render-due or skipped a
 *     draw, or a front-buffer read while a render-skipped present left the
 *     front stale (any mode but "off"): the driver call is not made.
 *   - glCopyTex* on a draw-skipped frame taints its target texture, by driver
 *     name. A full level-0 copy on a rendered frame, or deleting the texture,
 *     clears the taint. A copy into no named texture, a full taint list, a
 *     rendered draw or list call with a tainted texture bound on any unit, or
 *     a captured gate with any taint left, is a fault.
 * glCopyPixels, glAccum and 1D/3D copies are not in the GL boundary catalog
 * (glenv.cpp supported[]): resolving them already ends the process.
 *
 * A restore drops all taint and the stale-front mark: the restore rebuilds
 * every texture from its captured key and draws all four framebuffer planes
 * from the capture (glsync.cpp, glframe.inc), and a captured gate never holds
 * taint (render_gate faults first). Nothing from a skipped frame survives it. */
#include "shim.hpp"

extern "C" {

#define GLAPI __attribute__((stdcall))
#define GL_FRONT_LEFT_ 0x0400u
#define GL_FRONT_RIGHT_ 0x0401u
#define GL_FRONT_ 0x0404u
#define GL_LEFT_ 0x0406u
#define GL_RIGHT_ 0x0407u
#define GL_POLYGON_ 0x0009u
#define GL_UNSIGNED_BYTE_ 0x1401u
#define GL_UNSIGNED_SHORT_ 0x1403u
#define GL_UNSIGNED_INT_ 0x1405u
#define GL_CLEAR_BITS_ 0x4700u  /* COLOR 0x4000 | STENCIL 0x400 | ACCUM 0x200 | DEPTH 0x100 */
#define GL_TEXTURE_1D_ 0x0DE0u
#define GL_TEXTURE_2D_ 0x0DE1u
#define GL_TEXTURE_CUBE_MAP_ 0x8513u
#define GL_TEXTURE_CUBE_MAP_POSITIVE_X_ 0x8515u
#define GL_TEXTURE_CUBE_MAP_NEGATIVE_Z_ 0x851Au
#define GL_TEXTURE_BINDING_1D_ 0x8068u
#define GL_TEXTURE_BINDING_2D_ 0x8069u
#define GL_TEXTURE_BINDING_CUBE_MAP_ 0x8514u
#define GL_TEXTURE0_ARB_ 0x84C0u
#define GL_ACTIVE_TEXTURE_ARB_ 0x84E0u
#define GL_MAX_TEXTURE_UNITS_ARB_ 0x84E2u
#define GL_TEXTURE_WIDTH_ 0x1000u
#define GL_TEXTURE_HEIGHT_ 0x1001u
#define RS_TAINT_CAP 64u
#define RS_MAX_UNITS 32

u32 g_rs_mode;                  /* RS_OFF, RS_PRESENT, RS_DRAW */
u32 g_rs_preview = 1;           /* preview readback every N ticks; 0 = none */
static int g_rs_latched;
static char g_rs_latch_why[96];
static int g_rs_swap_foreign;   /* the real SwapBuffers is not gdi32/opengl32: never skip it */
static int g_rs_front_stale;    /* a render-skipped present since the last real one */
static u64 g_rs_frame = ~0ull;  /* tick whose draws g_rs_frame_skips counts */
static u32 g_rs_frame_skips;
static u32 g_rs_taint[RS_TAINT_CAP];   /* driver texture names copied on draw-skipped frames */
static u32 g_rs_taint_n;
static u64 n_draw_skip, n_clear_skip, n_copy, n_copy_skip, n_read, n_read_skip;

static void (GLAPI *p_geti)(u32, i32 *);
static void (GLAPI *p_levi)(u32, i32, u32, i32 *);
static void (GLAPI *p_active)(u32);
enum { Q_UNTRIED, Q_READY, Q_UNAVAILABLE };
static int q_state = Q_UNTRIED;

void render_latch(const char *why) {
    if (g_rs_latched) return;
    g_rs_latched = 1;
    u32 n = (u32)strlen(why);
    if (n > sizeof g_rs_latch_why - 1) n = sizeof g_rs_latch_why - 1;
    memcpy(g_rs_latch_why, why, n);
    g_rs_latch_why[n] = 0;
    slog("render_skip stopped at tick %u: %s. This gate refuses its command.", (u32)g_gate.tick, g_rs_latch_why);
}
/* "" or the latched reason (wire.cpp refuses the gate on it). */
const char *render_fault(void) { return g_rs_latched ? g_rs_latch_why : ""; }
/* The one wording of a fault, for every reply that refuses on it. */
void render_fault_message(buf_t *b) {
    b_str(b, "render_skip stopped: ");
    b_str(b, g_rs_latch_why);
    b_str(b, ". Set capture.render_skip to \"present\" or \"off\". A fresh owner is required.");
}

/* Present now? Frames that are render-due always present. */
int render_present_due(void) {
    if (g_rs_mode == RS_OFF || g_rs_latched || g_rs_swap_foreign) return 1;
    return wire_render_due(g_gate.tick);
}

int render_preview_due(u64 tick) {
    return g_rs_preview && tick % g_rs_preview == 0;
}

/* real: the driver presented. by_render: render skipping (not KPTAS_PRESENT) skipped it. */
void render_note_present(int real, int by_render) {
    if (real) g_rs_front_stale = 0;
    else if (by_render) g_rs_front_stale = 1;
}

/* Called once with the real SwapBuffers the present hook chains to. */
void render_swap_target(void *target) {
    HMODULE m = 0;
    if (!GetModuleHandleExA(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
                            reinterpret_cast<LPCSTR>(target), &m) ||
        (m != GetModuleHandleA("gdi32.dll") && m != GetModuleHandleA("opengl32.dll"))) {
        g_rs_swap_foreign = 1;
        char name[MAX_PATH];
        name[0] = 0;
        if (m) GetModuleFileNameA(m, name, sizeof name);
        slog("render: SwapBuffers chains to %x (%s), not gdi32 or opengl32. Presents are never skipped.",
             (u32)reinterpret_cast<uintptr_t>(target), name[0] ? name : "unknown module");
    }
}

/* May this call's draw be skipped? Cheap: runs on every draw. */
static int draws_skippable(void) {
    if (g_rs_mode != RS_DRAW || g_rs_latched || g_bnd_native) return 0;
    if (K->cur != 0 || g_us_depth || g_save_depth || gllists_compiling()) return 0;
    return !wire_render_due(g_gate.tick);
}
static void count_skip(void) {
    if (g_rs_frame != g_gate.tick) { g_rs_frame = g_gate.tick; g_rs_frame_skips = 0; }
    g_rs_frame_skips++;
}
static int draws_were_skipped(void) { return g_rs_frame == g_gate.tick && g_rs_frame_skips; }

static int q_resolve(void) {
    if (q_state != Q_UNTRIED) return q_state == Q_READY;
    q_state = Q_UNAVAILABLE;
    HMODULE gl = GetModuleHandleA("opengl32.dll");
    if (!gl) return 0;
    p_levi = reinterpret_cast<decltype(p_levi)>(GetProcAddress(gl, "glGetTexLevelParameteriv"));
    p_geti = reinterpret_cast<decltype(p_geti)>(GetProcAddress(gl, "glGetIntegerv"));
    void *(GLAPI *gp)(const char *) = reinterpret_cast<decltype(gp)>(GetProcAddress(gl, "wglGetProcAddress"));
    void *a = gp ? gp("glActiveTextureARB") : nullptr;
    const uintptr_t ai = reinterpret_cast<uintptr_t>(a);
    if (ai <= 3 || ai == 0xffffffffu) a = nullptr;
    p_active = reinterpret_cast<decltype(p_active)>(a);
    if (!p_geti || !p_levi || !p_active) return 0;
    q_state = Q_READY;
    return 1;
}
/* The binding query for a texture target (a cube face's is the cube map's). */
static u32 binding_of(u32 target) {
    if (target == GL_TEXTURE_CUBE_MAP_ ||
        (target >= GL_TEXTURE_CUBE_MAP_POSITIVE_X_ && target <= GL_TEXTURE_CUBE_MAP_NEGATIVE_Z_))
        return GL_TEXTURE_BINDING_CUBE_MAP_;
    return target == GL_TEXTURE_1D_ ? GL_TEXTURE_BINDING_1D_ : GL_TEXTURE_BINDING_2D_;
}

static int tainted(u32 real) {
    for (u32 i = 0; i < g_rs_taint_n; i++) if (g_rs_taint[i] == real) return 1;
    return 0;
}
static void taint_add(u32 real) {
    if (tainted(real)) return;
    if (g_rs_taint_n == RS_TAINT_CAP) { render_latch("too many textures were copied on draw-skipped frames"); return; }
    g_rs_taint[g_rs_taint_n++] = real;
}
static void taint_drop(u32 real) {
    for (u32 i = 0; i < g_rs_taint_n; i++)
        if (g_rs_taint[i] == real) { g_rs_taint[i] = g_rs_taint[--g_rs_taint_n]; return; }
}
/* gl.cpp gl_tex_discard_real: the driver name is deleted; its taint goes with it. */
void render_texture_deleted(u32 real) { if (real) taint_drop(real); }

/* A draw that runs while a texture copied on a skipped frame still holds its
 * stale copy: if any unit has it bound, the draw may sample it. Runs only
 * while taint exists. Direct driver queries; the active unit is put back. */
static void check_draw(void) {
    if (!g_rs_taint_n || g_rs_latched || gllists_compiling() || K->cur != 0) return;
    if (!q_resolve()) { render_latch("the texture taint check has no driver entry points"); return; }
    DWORD error = GetLastError();
    i32 active = (i32)GL_TEXTURE0_ARB_, units = 1;
    p_geti(GL_ACTIVE_TEXTURE_ARB_, &active);
    p_geti(GL_MAX_TEXTURE_UNITS_ARB_, &units);
    if (units < 1) units = 1;
    if (units > RS_MAX_UNITS) {
        SetLastError(error);
        render_latch("the driver reports more texture units than the taint check covers");
        return;
    }
    static const u32 binds[3] = {GL_TEXTURE_BINDING_1D_, GL_TEXTURE_BINDING_2D_, GL_TEXTURE_BINDING_CUBE_MAP_};
    int hit = 0;
    for (i32 u = 0; u < units && !hit; u++) {
        p_active(GL_TEXTURE0_ARB_ + (u32)u);
        for (u32 k = 0; k < 3 && !hit; k++) {
            i32 real = 0;
            p_geti(binds[k], &real);
            if (real && tainted((u32)real)) hit = 1;
        }
    }
    p_active((u32)active);
    SetLastError(error);
    if (hit) render_latch("a rendered draw binds a texture copied on a draw-skipped frame");
}

/* glDrawElements / glDrawArrays / glClear: 1 = skip the call. Only arguments
 * that cannot raise GL_INVALID_ENUM or GL_INVALID_VALUE are skipped. */
int render_skip_elements(u32 mode, i32 count, u32 type) {
    if (mode > GL_POLYGON_ || count < 0 ||
        (type != GL_UNSIGNED_BYTE_ && type != GL_UNSIGNED_SHORT_ && type != GL_UNSIGNED_INT_) || !draws_skippable()) {
        check_draw();
        return 0;
    }
    count_skip(); n_draw_skip++; return 1;
}
int render_skip_arrays(u32 mode, i32 first, i32 count) {
    if (mode > GL_POLYGON_ || first < 0 || count < 0 || !draws_skippable()) { check_draw(); return 0; }
    count_skip(); n_draw_skip++; return 1;
}
int render_skip_clear(u32 mask) {
    if ((mask & ~GL_CLEAR_BITS_) || !draws_skippable()) return 0;
    count_skip(); n_clear_skip++; return 1;
}
/* Before glEnd and glCallLists, which are never skipped but draw too. */
void render_check_draw(void) { check_draw(); }

static int read_selects_front(i32 read) {
    return read == (i32)GL_FRONT_ || read == (i32)GL_FRONT_LEFT_ || read == (i32)GL_FRONT_RIGHT_ ||
           read == (i32)GL_LEFT_ || read == (i32)GL_RIGHT_;
}

/* The game's glReadPixels, before the driver call: 1 = do not call the
 * driver (a fault is latched; the gate refuses the tick). */
int render_refuse_read(void) {
    n_read++;
    if (g_rs_mode == RS_OFF) return 0;
    if (g_rs_mode == RS_DRAW && !g_bnd_native && (draws_were_skipped() || !wire_render_due(g_gate.tick))) {
        n_read_skip++;
        render_latch("the game read pixels on a draw-skipped frame");
        return 1;
    }
    if (g_rs_front_stale) {
        i32 draw = 0, read = 0;
        gldraw_selectors(&draw, &read);
        if (read_selects_front(read)) {
            render_latch("the game read the front buffer after a skipped present");
            return 1;
        }
    }
    return 0;
}

/* The game's glCopyTex(Sub)Image2D on target, after the driver call. */
void render_note_copy(u32 target, i32 level, i32 xo, i32 yo, i32 w, i32 h, int sub) {
    n_copy++;
    if (g_rs_mode != RS_DRAW || g_rs_latched || g_bnd_native) return;
    /* UpdateScreen copies on such a frame read a framebuffer whose draws were skipped too */
    int skipped = draws_were_skipped() || draws_skippable() || (K->cur == 0 && !wire_render_due(g_gate.tick));
    if (!skipped && !g_rs_taint_n) return;
    if (!q_resolve()) { render_latch("the copy check has no driver entry points"); return; }
    i32 real = 0;
    DWORD error = GetLastError();
    p_geti(binding_of(target), &real);
    SetLastError(error);
    if (skipped) {
        n_copy_skip++;
        if (!real) { render_latch("the game copied into no named texture on a draw-skipped frame"); return; }
        taint_add((u32)real);
        return;
    }
    if (!real || !tainted((u32)real) || level != 0) return;
    int full = !sub;
    if (sub && xo == 0 && yo == 0) {
        i32 tw = 0, th = 0;
        p_levi(target, 0, GL_TEXTURE_WIDTH_, &tw);
        p_levi(target, 0, GL_TEXTURE_HEIGHT_, &th);
        SetLastError(error);
        full = w >= tw && h >= th;
    }
    if (full) taint_drop((u32)real);
}

/* Gate n, after the capture decision and before the capture. */
void render_gate(int captured) {
    if (captured && g_rs_taint_n && !g_rs_latched) {
        slog("render: %u textures copied on draw-skipped frames were not rewritten by captured tick %u "
             "(first driver name %u)", g_rs_taint_n, (u32)g_gate.tick, g_rs_taint[0]);
        render_latch("a texture copied on a draw-skipped frame reached a capture");
    }
}

/* After a restore (see the top: nothing from a skipped frame survives it). */
void render_restored(void) {
    g_rs_taint_n = 0;
    g_rs_front_stale = 0;
    g_rs_frame = ~0ull;
}

void render_stats_json(buf_t *b) {
    b_str(b, "{\"mode\":");
    b_str(b, g_rs_mode == RS_DRAW ? "\"draw\"" : g_rs_mode == RS_PRESENT ? "\"present\"" : "\"off\"");
    b_str(b, ",\"preview_every\":"); b_u64(b, g_rs_preview);
    b_str(b, ",\"presents\":"); b_u64(b, g_presents);
    b_str(b, ",\"presents_skipped\":"); b_u64(b, g_presents_skipped);
    b_str(b, ",\"draws_skipped\":"); b_u64(b, n_draw_skip);
    b_str(b, ",\"clears_skipped\":"); b_u64(b, n_clear_skip);
    b_str(b, ",\"copies\":"); b_u64(b, n_copy);
    b_str(b, ",\"copies_on_skip\":"); b_u64(b, n_copy_skip);
    b_str(b, ",\"tainted\":"); b_u64(b, g_rs_taint_n);
    b_str(b, ",\"reads\":"); b_u64(b, n_read);
    b_str(b, ",\"reads_on_skip\":"); b_u64(b, n_read_skip);
    b_str(b, ",\"swap_foreign\":"); b_str(b, g_rs_swap_foreign ? "true" : "false");
    b_ch(b, '}');
}

} /* extern "C" */
