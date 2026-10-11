// X13 fiber probe: hand-rolled cooperative context switch between the main
// stack and a committed stack inside a reserved region, swapping the TEB
// NT_TIB fields on every switch, with a manual SEH frame, RaiseException,
// stack probing and timing.
// Console exe, 32-bit. Touches only this process's own memory.

#include <windows.h>
#include <stdio.h>
#include <stdint.h>

static_assert(sizeof(void *) == 4, "the fiber probe requires a 32-bit Windows target");

#define MB (1024u*1024u)
#define MYCODE 0xE0000123u   // custom, application-defined bit set

// ---- fs/TEB helpers (NT_TIB offsets on x86) ----
static inline uint32_t rdfs(uint32_t off){ uint32_t v;
    __asm__ volatile("movl %%fs:(%1),%0":"=r"(v):"r"(off)); return v; }
static inline void wrfs(uint32_t off,uint32_t v){
    __asm__ volatile("movl %0,%%fs:(%1)"::"r"(v),"r"(off):"memory"); }

// ---- context switch: save current esp+callee regs+NT_TIB+fpu, load the other ----
// void ctx_switch(void** save_esp, void* load_esp)
// Saved frame, low to high: fpucw, mxcsr, ExceptionList, StackBase, StackLimit,
// DeallocationStack, edi, esi, ebx, ebp, return address. Each side keeps its own
// NT_TIB fields, so SEH and stack checks always see the stack that is running.
__attribute__((naked)) static void ctx_switch(void** save_esp, void* load_esp){
    __asm__ volatile(
        "movl 4(%esp), %eax\n\t"   // eax = save_esp
        "movl 8(%esp), %edx\n\t"   // edx = load_esp
        "pushl %ebp\n\t pushl %ebx\n\t pushl %esi\n\t pushl %edi\n\t"
        "pushl %fs:0xE0C\n\t pushl %fs:0x08\n\t pushl %fs:0x04\n\t pushl %fs:0x00\n\t"
        "subl $8, %esp\n\t"
        "fnstcw (%esp)\n\t"
        "stmxcsr 4(%esp)\n\t"
        "movl %esp, (%eax)\n\t"    // *save_esp = esp
        "movl %edx, %esp\n\t"      // esp = load_esp
        "ldmxcsr 4(%esp)\n\t"
        "fldcw (%esp)\n\t"
        "addl $8, %esp\n\t"
        "popl %fs:0x00\n\t popl %fs:0x04\n\t popl %fs:0x08\n\t popl %fs:0xE0C\n\t"
        "popl %edi\n\t popl %esi\n\t popl %ebx\n\t popl %ebp\n\t"
        "ret\n\t"
    );
}

static void report_tib(const char *who){
    uint32_t sp; __asm__ volatile("movl %%esp,%0":"=r"(sp));
    uint32_t base = rdfs(0x04), limit = rdfs(0x08);
    printf("  [%s] esp=%08x StackBase=%08x StackLimit=%08x ExcList=%08x esp inside: %s\n",
           who, sp, base, limit, rdfs(0x00), sp < base && sp >= limit ? "YES" : "NO");
}

// ---- manual SEH handler ----
static volatile int g_av_caught = 0;
static volatile int g_raise_caught = 0;
static void *g_reserve = NULL;   // region whose page we commit on AV

// EXCEPTION_DISPOSITION handler(record, frame, context, dispatch)
static EXCEPTION_DISPOSITION my_handler(EXCEPTION_RECORD *rec, void *frame,
                                        CONTEXT *ctx, void *disp){
    static_cast<void>(frame);static_cast<void>(ctx);static_cast<void>(disp);
    if (rec->ExceptionCode == MYCODE){
        g_raise_caught = 1;
        return ExceptionContinueExecution;   // resume after RaiseException
    }
    if (rec->ExceptionCode == EXCEPTION_ACCESS_VIOLATION){
        void *fault = reinterpret_cast<void *>(rec->ExceptionInformation[1]);
        // commit the faulting page then re-execute the instruction
        void *pg = reinterpret_cast<void *>(reinterpret_cast<uintptr_t>(fault) & ~static_cast<uintptr_t>(0xFFF));
        VirtualAlloc(pg, 0x1000, MEM_COMMIT, PAGE_READWRITE);
        g_av_caught = 1;
        return ExceptionContinueExecution;
    }
    return ExceptionContinueSearch;
}

struct seh_reg { struct seh_reg *prev; void *handler; };

// ---- fiber state ----
static void *g_main_sp = NULL;
static void *g_fiber_sp = NULL;
static volatile int g_tests_done = 0;

static void fiber_entry(void){
    // (report from arena stack)
    report_tib("fiber");

    // install manual SEH frame: push handler; push fs:[0]; mov fs:[0],esp
    struct seh_reg reg;
    reg.handler = reinterpret_cast<void *>(my_handler);
    reg.prev = reinterpret_cast<seh_reg *>(static_cast<uintptr_t>(rdfs(0x00)));
    wrfs(0x00, static_cast<uint32_t>(reinterpret_cast<uintptr_t>(&reg)));
    printf("  [fiber] SEH frame installed at %p (prev=%p)\n",static_cast<void *>(&reg),reg.prev);

    // (a) trigger an access violation into a reserved-but-uncommitted page
    volatile int *bad = reinterpret_cast<int *>(static_cast<char *>(g_reserve) + 0x1000);
    *bad = 0x1234;   // faults; handler commits page and resumes
    printf("  [fiber] (a) AV handled=%d, value now=%d\n", g_av_caught, *bad);

    // (b) RaiseException with a custom code caught by our frame
    RaiseException(MYCODE, 0, 0, NULL);
    printf("  [fiber] (b) RaiseException handled=%d\n", g_raise_caught);

    // (c) 256 KiB local array, touch every page (stack probing)
    volatile char big[256*1024];
    for (int i=0;i<256*1024;i+=0x1000) big[i]=static_cast<char>(i);
    big[256*1024-1]=7;
    printf("  [fiber] (c) 256KiB stack array touched ok (last=%d)\n", big[256*1024-1]);

    // pop SEH frame
    wrfs(0x00, static_cast<uint32_t>(reinterpret_cast<uintptr_t>(reg.prev)));

    g_tests_done = 1;
    // hand control back to main; then serve the ping-pong loop
    ctx_switch(&g_fiber_sp, g_main_sp);
    for(;;){
        ctx_switch(&g_fiber_sp, g_main_sp);
    }
}

int main(void){
    printf("=== X13 fiber probe ===\n");

    // reserve region used by the AV test (reserved, not committed)
    g_reserve = VirtualAlloc(NULL, 64*1024, MEM_RESERVE, PAGE_READWRITE);
    printf("reserve for AV=%p\n", g_reserve);

    // allocate a fully committed arena stack inside a MEM_RESERVE region
    SIZE_T stackSize = 1*MB;
    void *stackRegion = VirtualAlloc(NULL, stackSize, MEM_RESERVE, PAGE_READWRITE);
    if(!stackRegion){ printf("FAIL reserve stack\n"); return 1; }
    void *stackCommit = VirtualAlloc(stackRegion, stackSize, MEM_COMMIT, PAGE_READWRITE);
    printf("arena stack region=%p commit=%p size=%luKB\n",
           stackRegion, stackCommit,static_cast<unsigned long>(stackSize/1024));
    uintptr_t stackBase = reinterpret_cast<uintptr_t>(stackRegion) + stackSize; // high end
    uintptr_t stackLimit = reinterpret_cast<uintptr_t>(stackRegion);

    // capture current fpu control word + mxcsr to seed the fiber context
    uint16_t fpucw; uint32_t mxcsr;
    __asm__ volatile("fnstcw %0":"=m"(fpucw));
    __asm__ volatile("stmxcsr %0":"=m"(mxcsr));

    // build initial fiber stack image (see ctx_switch load path); its NT_TIB
    // fields describe the arena stack and take effect on the first switch in
    uint32_t *sp = reinterpret_cast<uint32_t *>(stackBase & ~static_cast<uintptr_t>(0xF));
    *--sp = static_cast<uint32_t>(reinterpret_cast<uintptr_t>(fiber_entry)); // return address
    *--sp = 0;   // ebp
    *--sp = 0;   // ebx
    *--sp = 0;   // esi
    *--sp = 0;   // edi
    *--sp = static_cast<uint32_t>(stackLimit);  // DeallocationStack
    *--sp = static_cast<uint32_t>(stackLimit);  // StackLimit
    *--sp = static_cast<uint32_t>(stackBase);   // StackBase
    *--sp = 0xFFFFFFFF;            // ExceptionList = end of list
    *--sp = mxcsr;               // [esp+4]
    *--sp = static_cast<uint32_t>(fpucw);     // [esp+0]
    g_fiber_sp = static_cast<void *>(sp);
    printf("fiber TEB: StackBase=%08x StackLimit=%08x\n", static_cast<uint32_t>(stackBase),static_cast<uint32_t>(stackLimit));
    report_tib("main");

    // first switch: runs the fiber test body, returns after g_tests_done
    ctx_switch(&g_main_sp, g_fiber_sp);
    printf("back in main, tests_done=%d\n", g_tests_done);
    report_tib("main");

    // (d) ping-pong 1e6 times and time it
    LARGE_INTEGER freq,t0,t1; QueryPerformanceFrequency(&freq);
    const long N=1000000;
    QueryPerformanceCounter(&t0);
    for(long i=0;i<N;i++){
        ctx_switch(&g_main_sp, g_fiber_sp);   // in
        // fiber immediately switches back -> control returns here
    }
    QueryPerformanceCounter(&t1);
    double ms=static_cast<double>(t1.QuadPart-t0.QuadPart)*1000.0/freq.QuadPart;
    double switches = 2.0*N; // each iter = one in + one out
    printf("(d) %ld ping-pong iters (%.0f switches): %.3f ms, %.1f ns/switch\n",
           N, switches, ms, ms*1e6/switches);

    report_tib("main");
    printf("=== X13 done ===\n");
    return 0;
}
