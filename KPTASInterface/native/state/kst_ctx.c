/* Context records, the fiber gate, thread slots and the serial scheduler.
 *
 * Every game thread runs on an arena stack. Its context record (esp plus the TEB
 * fields a fiber switch owns) lives in harness_game, and its callee-saved
 * registers, EFLAGS, x87 CW and MXCSR are pushed on its own stack, so a restore
 * of memory restores the thread. Each slot k is driven by one pre-created real
 * thread k (slot 0: the process main thread). Exactly one game context runs at a
 * time; a switch happens only at kst_yield / kst_event_wait / thread exit, and
 * the baton passes between real threads through real auto-reset events that live
 * in harness_core. At the gate every other slot is parked by construction. */
#include "kst_int.h"

/* void kst_switch(kst_ctx *save, kst_ctx *load) */
__attribute__((naked)) void kst_switch(kst_ctx *save, kst_ctx *load) {
    __asm__ volatile(
        "movl 4(%esp), %eax\n\t"
        "movl 8(%esp), %edx\n\t"
        "pushl %ebp\n\t"
        "pushl %ebx\n\t"
        "pushl %esi\n\t"
        "pushl %edi\n\t"
        "pushfl\n\t"
        "subl $8, %esp\n\t"
        "fnstcw (%esp)\n\t"
        "stmxcsr 4(%esp)\n\t"
        "movl %esp, 0(%eax)\n\t"
        "movl %fs:0x00, %ecx\n\t"
        "movl %ecx, 4(%eax)\n\t"
        "movl %fs:0x04, %ecx\n\t"
        "movl %ecx, 8(%eax)\n\t"
        "movl %fs:0x08, %ecx\n\t"
        "movl %ecx, 12(%eax)\n\t"
        "movl %fs:0xE0C, %ecx\n\t"
        "movl %ecx, 16(%eax)\n\t"
        "movl %fs:0x34, %ecx\n\t"
        "movl %ecx, 20(%eax)\n\t"
        "movl 4(%edx), %ecx\n\t"
        "movl %ecx, %fs:0x00\n\t"
        "movl 8(%edx), %ecx\n\t"
        "movl %ecx, %fs:0x04\n\t"
        "movl 12(%edx), %ecx\n\t"
        "movl %ecx, %fs:0x08\n\t"
        "movl 16(%edx), %ecx\n\t"
        "movl %ecx, %fs:0xE0C\n\t"
        "movl 20(%edx), %ecx\n\t"
        "movl %ecx, %fs:0x34\n\t"
        "movl 0(%edx), %esp\n\t"
        "ldmxcsr 4(%esp)\n\t"
        "fldcw (%esp)\n\t"
        "addl $8, %esp\n\t"
        "popfl\n\t"
        "popl %edi\n\t"
        "popl %esi\n\t"
        "popl %ebx\n\t"
        "popl %ebp\n\t"
        "ret\n\t");
}

static void thread_exit(void);

/* Public slot setup for hosts with their own entry trampolines: the slot's first
 * switch-in "returns" into entry(arg) (cdecl) on the given stack. */
void kst_slot_init(u32 k, void *stack, u32 size, void (*entry)(void *), void *arg, u32 state) {
    hg_slot *s = &K->hg->slot[k];
    u32 hi = ((u32)stack + size) & ~15u, lo = (u32)stack;
    u32 *sp = (u32 *)(hi - 16);
    *--sp = (u32)arg;                   /* entry's argument */
    *--sp = 0;                          /* entry's return address (entry never returns) */
    *--sp = (u32)entry;                 /* ret */
    *--sp = 0;                          /* ebp */
    *--sp = 0;                          /* ebx */
    *--sp = 0;                          /* esi */
    *--sp = 0;                          /* edi */
    *--sp = 0x202;                      /* eflags */
    *--sp = 0x1F80;                     /* mxcsr */
    *--sp = 0x027F;                     /* x87 control word */
    s->ctx.esp = (u32)sp;
    s->ctx.seh = 0xFFFFFFFFu;
    s->ctx.stack_base = hi;
    s->ctx.stack_limit = lo;
    s->ctx.dealloc = lo;
    s->ctx.last_error = 0;
    s->entry = (u32)entry;
    s->arg = (u32)arg;
    s->stack_lo = lo;
    s->stack_hi = (u32)stack + size;
    s->wait_ev = KST_NONE;
    s->vtid = 0;
    s->state = state;
}

/* First code a slot runs on its arena stack. */
static void __cdecl slot_trampoline(void) {
    hg_slot *s = &K->hg->slot[K->cur];
    void (*proc)(void *) = (void (*)(void *))s->entry;
    proc((void *)s->arg);
    thread_exit();
}

static void slot_setup(u32 k, void *stack, u32 size, u32 entry, u32 arg, u32 state) {
    hg_slot *s = &K->hg->slot[k];
    u32 hi = ((u32)stack + size) & ~15u, lo = (u32)stack;
    u32 *sp = (u32 *)(hi - 16);
    *--sp = 0;                          /* fake return address of the trampoline */
    *--sp = (u32)slot_trampoline;       /* ret */
    *--sp = 0;                          /* ebp */
    *--sp = 0;                          /* ebx */
    *--sp = 0;                          /* esi */
    *--sp = 0;                          /* edi */
    *--sp = 0x202;                      /* eflags */
    *--sp = 0x1F80;                     /* mxcsr */
    *--sp = 0x027F;                     /* x87 control word */
    s->ctx.esp = (u32)sp;
    s->ctx.seh = 0xFFFFFFFFu;
    s->ctx.stack_base = hi;
    s->ctx.stack_limit = lo;
    s->ctx.dealloc = lo;
    s->ctx.last_error = 0;
    s->entry = entry;
    s->arg = arg;
    s->stack_lo = lo;
    s->stack_hi = (u32)stack + size;
    s->wait_ev = KST_NONE;
    s->vtid = K->hg->next_vtid++;
    s->state = state;
}

/* ---------------------------------------------------------------- real threads */

static DWORD WINAPI real_slot_main(void *p) {
    u32 k = (u32)p;
    for (;;) {
        WaitForSingleObject(K->real_ev[k], INFINITE);
        if (K->shutdown) return 0;
        K->cur = k;
        kst_switch(&K->hctx[k], &K->hg->slot[k].ctx);
        if (K->reason[k] == R_GATE) kst_fatal("kst_gate called from a worker slot");
        SetEvent(K->real_ev[K->next]);
    }
}

void kst__threads_init(void) {
    for (u32 k = 0; k < K->cfg.nslots; k++) {
        K->real_ev[k] = CreateEventW(0, FALSE, FALSE, 0);
        if (!K->real_ev[k]) kst_fatal("CreateEvent failed");
        if (k) {
            K->real_thr[k] = CreateThread(0, 256 * 1024, real_slot_main, (void *)k, 0, 0);
            if (!K->real_thr[k]) kst_fatal("CreateThread failed");
        }
    }
    K->hg->nslots = K->cfg.nslots;
    K->hg->next_vtid = 1;
}

/* ---------------------------------------------------------------- scheduler (game side) */

static u32 pick_next(u32 cur) {
    kst_hg *hg = K->hg;
    for (u32 i = 1; i < hg->nslots; i++) {
        u32 k = (cur + i) % hg->nslots;
        if (hg->slot[k].state == KST_SLOT_RUNNABLE) return k;
    }
    return KST_NONE;
}

/* The current slot has set its own state; hand the baton to the next runnable slot. */
static void switch_away(u32 reason) {
    kst_hg *hg = K->hg;
    u32 cur = K->cur, nx = pick_next(cur);
    if (nx == KST_NONE) {
        if (hg->slot[cur].state == KST_SLOT_RUNNABLE) { hg->slot[cur].state = KST_SLOT_RUNNING; return; }
        kst_fatal("deadlock: no runnable game slot");
    }
    hg->slot[nx].state = KST_SLOT_RUNNING;
    K->next = nx;
    K->reason[cur] = reason;
    kst_switch(&hg->slot[cur].ctx, &K->hctx[cur]);
}

static void thread_exit(void) {
    kst_hg *hg = K->hg;
    u32 cur = K->cur;
    hg->slot[cur].state = KST_SLOT_FREE;
    if (cur == 0) {
        K->reason[0] = R_EXIT;
        kst_switch(&hg->slot[0].ctx, &K->hctx[0]);
    }
    switch_away(R_EXIT);
    kst_fatal("exited slot resumed");
}

void kst_yield(void) {
    K->hg->slot[K->cur].state = KST_SLOT_RUNNABLE;
    switch_away(R_HANDOFF);
}

int kst_thread_create(void (*proc)(void *), void *arg, u32 stack_size) {
    kst_hg *hg = K->hg;
    for (u32 k = 1; k < hg->nslots; k++) {
        if (hg->slot[k].state != KST_SLOT_FREE) continue;
        if (!stack_size) stack_size = 256 * 1024;
        void *stk = kst_valloc(stack_size);
        slot_setup(k, stk, stack_size, (u32)proc, (u32)arg, KST_SLOT_RUNNABLE);
        return (int)hg->slot[k].vtid;
    }
    return -1;
}

u32 kst_event_create(int manual) {
    kst_hg *hg = K->hg;
    if (hg->nev >= KST_MAX_EVENTS) kst_fatal("too many virtual events");
    u32 e = hg->nev++;
    hg->ev[e].used = 1;
    hg->ev[e].manual = manual ? 1 : 0;
    hg->ev[e].signaled = 0;
    return e;
}

void kst_event_set(u32 e) {
    kst_hg *hg = K->hg;
    hg_ev *ev = &hg->ev[e];
    int woke = 0;
    for (u32 k = 0; k < hg->nslots; k++) {
        hg_slot *s = &hg->slot[k];
        if (s->state != KST_SLOT_BLOCKED || s->wait_ev != e) continue;
        s->state = KST_SLOT_RUNNABLE;
        s->wait_ev = KST_NONE;
        woke = 1;
        if (!ev->manual) return;   /* auto-reset: the signal is consumed by the waiter */
    }
    if (ev->manual || !woke) ev->signaled = 1;
}

void kst_event_reset(u32 e) { K->hg->ev[e].signaled = 0; }

void kst_event_wait(u32 e) {
    kst_hg *hg = K->hg;
    hg_ev *ev = &hg->ev[e];
    if (ev->signaled) {
        if (!ev->manual) ev->signaled = 0;
        return;
    }
    hg_slot *s = &hg->slot[K->cur];
    s->state = KST_SLOT_BLOCKED;
    s->wait_ev = e;
    switch_away(R_HANDOFF);
}

u32 kst_slot_state(u32 k) { return k < KST_MAX_SLOTS ? K->hg->slot[k].state : 0; }

/* ---------------------------------------------------------------- harness calls */

static inline u32 rdfs(u32 off) {
    u32 v;
    __asm__ volatile("movl %%fs:(%1), %0" : "=r"(v) : "r"(off));
    return v;
}
static inline void wrfs(u32 off, u32 v) { __asm__ volatile("movl %0, %%fs:(%1)" ::"r"(v), "r"(off) : "memory"); }

/* Run fn(arg) on the current real thread's harness stack. Engine services that
 * enter Wine go through here, so Wine's locals (process-heap pointers, sizes,
 * handles) land on the excluded harness stack, never on a game stack. Called on
 * a harness stack already (before kst_run, or inside the gate), it is a plain call. */
__attribute__((noinline)) void kst_hcall(void (*fn)(void *), void *arg) {
    u32 esp_now;
    __asm__ volatile("movl %%esp, %0" : "=r"(esp_now));
    if (!kst_in_arena((void *)esp_now)) { fn(arg); return; }
    u32 cur = K->cur;
    kst_ctx *h = &K->hctx[cur];
    u32 seh = rdfs(0x00), sb = rdfs(0x04), sl = rdfs(0x08), dl = rdfs(0xE0C);
    wrfs(0x00, h->seh);
    wrfs(0x04, h->stack_base);
    wrfs(0x08, h->stack_limit);
    wrfs(0xE0C, h->dealloc);
    K->hc[cur].hsp = (h->esp - 512) & ~15u;
    K->hc[cur].fn = fn;
    K->hc[cur].arg = arg;
    void *cc = &K->hc[cur];
    __asm__ volatile(
        "movl %%esp, %%esi\n\t"
        "movl 0(%0), %%esp\n\t"
        "subl $12, %%esp\n\t"
        "pushl 8(%0)\n\t"
        "call *4(%0)\n\t"
        "movl %%esi, %%esp\n\t"
        :
        : "D"(cc)
        : "esi", "eax", "ecx", "edx", "memory", "cc");
    h->stack_limit = rdfs(0x08);   /* guard-page growth may have moved it */
    wrfs(0x00, seh);
    wrfs(0x04, sb);
    wrfs(0x08, sl);
    wrfs(0xE0C, dl);
}

/* ---------------------------------------------------------------- gate and main loop */

void kst_gate(void) {
    if (K->cur != 0) kst_fatal("kst_gate called from a worker slot");
    K->reason[0] = R_GATE;
    kst_switch(&K->hg->slot[0].ctx, &K->hctx[0]);
}

void kst_run(void (*game_main)(void *), void *arg) {
    kst_hg *hg = K->hg;
    void *stk = kst_valloc(K->cfg.main_stack);
    slot_setup(0, stk, K->cfg.main_stack, (u32)game_main, (u32)arg, KST_SLOT_RUNNING);
    for (;;) {
        K->cur = 0;
        K->reason[0] = R_NONE;
        /* After a restore this loads T's record from harness_game. */
        kst_switch(&K->hctx[0], &hg->slot[0].ctx);
        u32 r = K->reason[0];
        if (r == R_GATE) {
            K->in_gate = 1;
            kst__gate();
            K->in_gate = 0;
        } else if (r == R_HANDOFF) {
            SetEvent(K->real_ev[K->next]);
            WaitForSingleObject(K->real_ev[0], INFINITE);
        } else if (r == R_EXIT) {
            break;
        } else {
            kst_fatal("main slot switched out without a reason");
        }
    }
    K->shutdown = 1;
    for (u32 k = 1; k < K->cfg.nslots; k++) SetEvent(K->real_ev[k]);
    for (u32 k = 1; k < K->cfg.nslots; k++) WaitForSingleObject(K->real_thr[k], 5000);
}
