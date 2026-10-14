#ifdef __cplusplus
extern "C" {
#endif
#ifndef KPTAS_GLENV_H
#define KPTAS_GLENV_H
/* Finite void/integer/pointer GL boundary; unsupported GL APIs refuse. */
int glenv_supported(const char *name);
void glenv_install(void);
int glenv_capture_ready(void);
int glenv_active(void);
void *glenv_proc(const char *name, void *target);
u32 glenv_caller(u32 ret);
/* EFLAGS.ID (bit 21): the CPUID-probe bit the GL boundary and math scopes
 * restore for the game. */
#define EFLAGS_ID 0x200000u
/* math.cpp scope helpers used only by the GL boundary fast path. Declared here
 * because shim.hpp includes this header before math_host_scope_t is complete.
 * math_scope_fast_slot returns a TEB TLS index, or MATH_NO_FAST_SLOT when the
 * canonical math_scope_begin/math_scope_end path must run instead. */
#define MATH_NO_FAST_SLOT (~0u)
struct math_host_scope;
u32 math_scope_fast_slot(void);
void math_scope_link(struct math_host_scope *s, u32 slot, u32 mask, u32 id);
void math_scope_unlink(const struct math_host_scope *s, u32 slot);
#endif

#ifdef __cplusplus
} /* C ABI: mixed native candidate and assembly symbols */
#endif
