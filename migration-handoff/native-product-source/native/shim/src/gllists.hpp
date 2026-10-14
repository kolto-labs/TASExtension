#ifdef __cplusplus
extern "C" {
#endif
#ifndef KPTAS_GLLISTS_H
#define KPTAS_GLLISTS_H
#define GLL_CAP 4096u
typedef struct {
    u32 live, bitmap, width, height, bytes;
    float xorigin,yorigin,xmove,ymove;
    u8 *pixels;
} gll_definition_t;
typedef struct {
    u32 context, active;
    gll_definition_t pending;
    gll_definition_t *definitions;
} gll_state_t;
void gllists_install(void);
void *gllists_proc(const char *name,void *real);
void *gllists_import_target(const char *name);
int gllists_restore(void);
int gllists_restorable(void);
void gllists_capture(void);
void gllists_real_context_reset(void);
void gllists_guard(u32 allowed);
u32 gllists_allowed(const char *name);
void gllists_guard_code(u8 code[32],void **target,u32 *allowed);
int gllists_compiling(void);
#endif

#ifdef __cplusplus
} /* C ABI: mixed native candidate and assembly symbols */
#endif
