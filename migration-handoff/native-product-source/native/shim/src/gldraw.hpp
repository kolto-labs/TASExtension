#ifdef __cplusplus
extern "C" {
#endif
#ifndef KPTAS_GLDRAW_H
#define KPTAS_GLDRAW_H
/* Logical default-framebuffer selectors. Wine's fullscreen FBO is host state. */
#define GLD_STACK 32
typedef struct { u32 mask, draw, read; } gld_saved_t;
typedef struct {
    u32 serial, draw, read, depth;
    gld_saved_t stack[GLD_STACK];
} gld_context_t;
typedef struct { gld_context_t context[WH_CAP]; } gld_state_t;
void gldraw_install(void);
void *gldraw_proc(const char *name, void *target);
void gldraw_selectors(i32 *draw, i32 *read);
#endif

#ifdef __cplusplus
} /* C ABI: mixed native candidate and assembly symbols */
#endif
