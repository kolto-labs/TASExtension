#ifdef __cplusplus
extern "C" {
#endif
#ifndef KPTAS_GLBUFFERS_H
#define KPTAS_GLBUFFERS_H
/* Metadata and backing bytes belong to the captured simulation. Driver map
 * pointers and driver object existence never do. */
#define GLB_CAP 4096u
typedef struct {
    u32 reserved, live, size, usage, access, mapped;
    u8 *bytes;
} glb_object_t;
typedef struct {
    u32 next, context, used;
    glb_object_t object[GLB_CAP];
} glb_state_t;
void *glbuffers_proc(const char *name, void *real);
int glbuffers_restore(void);
void glbuffers_capture(void);
void glbuffers_real_context_reset(void);
#endif

#ifdef __cplusplus
} /* C ABI: mixed native candidate and assembly symbols */
#endif
