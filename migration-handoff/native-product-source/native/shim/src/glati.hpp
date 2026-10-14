#ifdef __cplusplus
extern "C" {
#endif
#ifndef KPTAS_GLATI_H
#define KPTAS_GLATI_H
/* ATI instruction operands are copied, never retained as game/driver pointers.
 * Command arrays are allocated in the captured placement arena. */
#define GATI_CAP 256u
#define GATI_COMMAND_LIMIT 4096u
typedef struct { u32 op,args[13]; } glati_command_t;
typedef struct {
    u32 live, complete, count, capacity, owner;
    glati_command_t *commands;
    u8 key[32];
} glati_object_t;
typedef struct {
    u32 used, defining, defining_id, constant_mask, context;
    float constants[8][4];
    glati_object_t object[GATI_CAP];
} glati_state_t;
void *glati_proc(const char *name,void *target);
int glati_restore(void);
int glati_capture_ready(void);
int glati_restorable(void);
void glati_real_context_reset(void);
#endif

#ifdef __cplusplus
} /* C ABI: mixed native candidate and assembly symbols */
#endif
