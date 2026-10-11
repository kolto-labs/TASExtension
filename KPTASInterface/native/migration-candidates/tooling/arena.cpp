// X12 arena probe: address-space walk, fixed-base reserve with write-watch,
// heap-in-arena test, sub-range query, and timing. Console exe, 32-bit.
// Touches only this process's own memory.

#include <windows.h>
#include <stdio.h>
#include <stdint.h>
#include <setjmp.h>

static_assert(sizeof(void *) == 4, "the arena probe requires a 32-bit Windows target");

typedef PVOID (WINAPI *RtlCreateHeap_t)(ULONG, PVOID, SIZE_T, SIZE_T, PVOID, PVOID);

// guard: survive an AV during the risky reserved-heap probe and report it
static jmp_buf g_jb;
static volatile int g_guard = 0;
static volatile DWORD g_faultCode = 0;
static LONG CALLBACK veh(PEXCEPTION_POINTERS ep){
    if(g_guard){
        g_faultCode = ep->ExceptionRecord->ExceptionCode;
        g_guard = 0;
        longjmp(g_jb, 1);
    }
    return EXCEPTION_CONTINUE_SEARCH;
}

#define MB (1024ull*1024ull)

static double now_ms(LARGE_INTEGER freq) {
    LARGE_INTEGER c; QueryPerformanceCounter(&c);
    return static_cast<double>(c.QuadPart) * 1000.0 / static_cast<double>(freq.QuadPart);
}

int main(void) {
    setvbuf(stdout, NULL, _IONBF, 0);   // unbuffered so output survives a crash
    SYSTEM_INFO si; GetSystemInfo(&si);
    SIZE_T pg = si.dwPageSize;
    printf("=== X12 arena probe ===\n");
    printf("pageSize=%lu allocGran=%lu minAppAddr=%p maxAppAddr=%p\n",
           static_cast<unsigned long>(pg), static_cast<unsigned long>(si.dwAllocationGranularity),
           si.lpMinimumApplicationAddress, si.lpMaximumApplicationAddress);

    // 1. Walk the address space, list free ranges >= 64 MB.
    printf("\n[1] free ranges >= 64 MB:\n");
    uintptr_t addr = reinterpret_cast<uintptr_t>(si.lpMinimumApplicationAddress);
    uintptr_t maxAddr = reinterpret_cast<uintptr_t>(si.lpMaximumApplicationAddress);
    MEMORY_BASIC_INFORMATION mbi;
    uintptr_t bestBase = 0; SIZE_T bestFree = 0;
    while (addr < maxAddr) {
        SIZE_T r = VirtualQuery((LPCVOID)addr, &mbi, sizeof(mbi));
        if (r == 0) break;
        if (mbi.State == MEM_FREE && mbi.RegionSize >= 64*MB) {
            printf("  base=%p size=%.1f MB\n", mbi.BaseAddress,
                   static_cast<double>(mbi.RegionSize)/MB);
            if (mbi.RegionSize > bestFree) {
                bestFree = mbi.RegionSize; bestBase = reinterpret_cast<uintptr_t>(mbi.BaseAddress);
            }
        }
        addr = reinterpret_cast<uintptr_t>(mbi.BaseAddress) + mbi.RegionSize;
    }
    printf("  largest free base=%p size=%.1f MB\n", reinterpret_cast<void *>(bestBase), static_cast<double>(bestFree)/MB);

    // 2. Reserve a fixed-base region with MEM_WRITE_WATCH.
    printf("\n[2] fixed-base reserve + write-watch:\n");
    uintptr_t tryBases[] = {0x10000000,0x18000000,0x20000000,0x28000000,
                            0x30000000,0x40000000,0x50000000,0x60000000};
    SIZE_T trySizes[] = {1024*MB, 768*MB, 512*MB, 256*MB, 128*MB, 64*MB};
    void *arena = NULL; SIZE_T arenaSize = 0; uintptr_t arenaBase = 0;
    for (unsigned s = 0; s < sizeof(trySizes)/sizeof(trySizes[0]) && !arena; s++) {
        for (unsigned b = 0; b < sizeof(tryBases)/sizeof(tryBases[0]); b++) {
            void *p = VirtualAlloc(reinterpret_cast<void *>(tryBases[b]), trySizes[s],
                        MEM_RESERVE|MEM_WRITE_WATCH, PAGE_READWRITE);
            if (p) {
                arena = p; arenaSize = trySizes[s]; arenaBase = reinterpret_cast<uintptr_t>(p);
                printf("  reserved base=%p size=%.0f MB (requested base=%p)\n",
                       p, static_cast<double>(arenaSize)/MB, reinterpret_cast<void *>(tryBases[b]));
                break;
            }
        }
    }
    if (!arena) { printf("  FAIL: no fixed-base reserve worked\n"); return 1; }

    // Commit some chunks and write a known set of pages.
    SIZE_T commitBytes = 16*MB;
    void *c1 = VirtualAlloc(arena, commitBytes, MEM_COMMIT, PAGE_READWRITE);
    printf("  commit %p..%p rc=%p\n", arena, reinterpret_cast<void *>(arenaBase+commitBytes), c1);
    // Write to a known set of pages: pages 0,1,2,10,50,100,500,1000.
    SIZE_T wpi[] = {0,1,2,10,50,100,500,1000};
    unsigned nWrite = sizeof(wpi)/sizeof(wpi[0]);
    for (unsigned i=0;i<nWrite;i++) {
        volatile char *pp = static_cast<char *>(arena) + wpi[i]*pg;
        *pp = static_cast<char>(i+1);
    }
    // GetWriteWatch with RESET should return exactly those pages.
    void *wbuf[64]; ULONG_PTR wcount = 64; ULONG wgran = 0;
    UINT rc = GetWriteWatch(WRITE_WATCH_FLAG_RESET, arena, commitBytes,
                            wbuf, &wcount, &wgran);
    printf("  GetWriteWatch rc=%u count=%lu gran=%lu\n", rc,static_cast<unsigned long>(wcount),static_cast<unsigned long>(wgran));
    // Verify count and that each written page is present.
    int allFound = (rc==0 && wcount==nWrite);
    for (unsigned i=0;i<nWrite && rc==0;i++) {
        void *want = static_cast<char *>(arena) + wpi[i]*pg; int found=0;
        for (ULONG_PTR j=0;j<wcount;j++) if (wbuf[j]==want){found=1;break;}
        if(!found){allFound=0; printf("  MISSING watched page %p\n", want);}
    }
    printf("  write-watch exact-match: %s\n", allFound?"YES":"NO");

    // Second GetWriteWatch immediately after reset: should be empty.
    wcount=64; rc=GetWriteWatch(0, arena, commitBytes, wbuf, &wcount, &wgran);
    printf("  after reset, dirty pages=%lu (expect 0)\n",static_cast<unsigned long>(wcount));

    // decommit + recommit, check zero-fill and watch still reports.
    VirtualFree(arena, commitBytes, MEM_DECOMMIT);
    void *c2 = VirtualAlloc(arena, commitBytes, MEM_COMMIT, PAGE_READWRITE);
    printf("  recommit rc=%p\n", c2);
    int zero=1;
    for (unsigned i=0;i<nWrite;i++){ char v=(static_cast<char *>(arena))[wpi[i]*pg]; if(v!=0){zero=0;} }
    printf("  recommitted pages read zero: %s\n", zero?"YES":"NO");
    for (unsigned i=0;i<nWrite;i++){ volatile char *pp=static_cast<char *>(arena)+wpi[i]*pg; *pp=static_cast<char>(9); }
    wcount=64; rc=GetWriteWatch(WRITE_WATCH_FLAG_RESET, arena, commitBytes, wbuf,&wcount,&wgran);
    printf("  write-watch after recommit: rc=%u count=%lu (expect %u)\n",
           rc,static_cast<unsigned long>(wcount),nWrite);

    // 3. RtlCreateHeap with HeapBase inside the arena.
    printf("\n[3] RtlCreateHeap inside arena:\n");
    HMODULE nt = GetModuleHandleA("ntdll.dll");
    const auto RtlCreateHeap = reinterpret_cast<RtlCreateHeap_t>(GetProcAddress(nt,"RtlCreateHeap"));
    printf("  RtlCreateHeap=%p\n",reinterpret_cast<void *>(RtlCreateHeap));
    if (RtlCreateHeap) {
        // committed variant: heap base in an 8 MB sub-block at arena+32MB,
        // committed here (the arena itself is only reserved).
        SIZE_T heapRegion = 8*MB;
        void *hbaseC = VirtualAlloc(static_cast<char *>(arena) + 32*MB, heapRegion,
                            MEM_COMMIT, PAGE_READWRITE);
        printf("  heap-base(committed)=%p\n", hbaseC);
        // RtlCreateHeap(flags, base, reserve, commit, lock, params), no flags.
        PVOID heapC = hbaseC ? RtlCreateHeap(0, hbaseC, heapRegion, 64*1024, NULL, NULL) : NULL;
        printf("  RtlCreateHeap(committed base) -> %p\n", heapC);
        if (heapC) {
            void *a1 = HeapAlloc(heapC, 0, 128);
            int in = a1 && reinterpret_cast<uintptr_t>(a1)>=arenaBase && reinterpret_cast<uintptr_t>(a1)<arenaBase+arenaSize;
            printf("  HeapAlloc=%p inside-arena=%s\n", a1, in?"YES":"NO");
        } else {
            printf("  committed-heap FAIL err=%lu\n",static_cast<unsigned long>(GetLastError()));
        }
        // reserved variant: base at arena+64MB, reserved but not committed.
        // May fault when the heap writes its header into uncommitted memory;
        // a vectored handler + setjmp lets us report the failure exactly.
        void *veh_h = AddVectoredExceptionHandler(1, veh);
        void *hbaseR = static_cast<char *>(arena) + 64*MB;
        if (setjmp(g_jb)==0) {
            g_guard = 1;
            PVOID heapR = RtlCreateHeap(0, hbaseR, 8*MB, 0, NULL, NULL);
            void *a2 = heapR ? HeapAlloc(heapR, 0, 128) : NULL;
            g_guard = 0;
            printf("  RtlCreateHeap(reserved base=%p) -> %p\n", hbaseR, heapR);
            if (heapR) {
                int in = a2 && reinterpret_cast<uintptr_t>(a2)>=arenaBase && reinterpret_cast<uintptr_t>(a2)<arenaBase+arenaSize;
                printf("  HeapAlloc=%p inside-arena=%s\n", a2, in?"YES":"NO");
            } else {
                printf("  reserved-heap FAIL err=%lu\n",static_cast<unsigned long>(GetLastError()));
            }
        } else {
            printf("  reserved-heap FAULTED code=0x%08lx (uncommitted base rejected)\n",
                   static_cast<unsigned long>(g_faultCode));
        }
        if (veh_h) RemoveVectoredExceptionHandler(veh_h);
    }

    // 4. VirtualQuery on a carved sub-range.
    printf("\n[4] VirtualQuery on sub-range arena+2MB:\n");
    void *sub = static_cast<char *>(arena) + 2*MB;
    if (VirtualQuery(sub,&mbi,sizeof(mbi))) {
        printf("  AllocationBase=%p BaseAddress=%p RegionSize=%.1fMB State=0x%lx Protect=0x%lx Type=0x%lx\n",
               mbi.AllocationBase, mbi.BaseAddress,static_cast<double>(mbi.RegionSize)/MB,
               static_cast<unsigned long>(mbi.State),static_cast<unsigned long>(mbi.Protect),static_cast<unsigned long>(mbi.Type));
    }

    // 5. Timing.
    printf("\n[5] timing:\n");
    LARGE_INTEGER freq; QueryPerformanceFrequency(&freq);
    // dirty up to 100k pages (~391 MB) in a region above both heap sub-blocks
    // (+32MB and +64MB, 8 MB each); skipped when the arena is too small
    ULONG_PTR counts[] = {1000,10000,100000};
    SIZE_T bigPages = 100000;
    SIZE_T bigBytes = (bigPages+16)*pg;
    SIZE_T bigOff = 80*MB;
    char *bigBase = static_cast<char *>(arena) + bigOff;
    void *big = bigOff + bigBytes <= arenaSize
        ? VirtualAlloc(bigBase, bigBytes, MEM_COMMIT, PAGE_READWRITE) : NULL;
    printf("  commit %.1f MB for dirtying at %p (rc=%p)\n",static_cast<double>(bigBytes)/MB,static_cast<void *>(bigBase),big);
    static void *wb2[100000+16];
    ULONG_PTR dummy;
    for (unsigned k=0;k<3 && big;k++){
        ULONG_PTR n=counts[k];
        dummy=0; GetWriteWatch(WRITE_WATCH_FLAG_RESET, bigBase, bigBytes, wb2, &dummy, &wgran);
        for (ULONG_PTR i=0;i<n;i++){ volatile char *pp=bigBase+i*pg; *pp=static_cast<char>(1); }
        ULONG_PTR gc=bigPages+16; double t0=now_ms(freq);
        rc=GetWriteWatch(WRITE_WATCH_FLAG_RESET, bigBase, bigBytes, wb2,&gc,&wgran);
        double t1=now_ms(freq);
        printf("  GetWriteWatch(reset) %lu dirty pages: rc=%u returned=%lu time=%.3f ms\n",
               static_cast<unsigned long>(n), rc,static_cast<unsigned long>(gc), t1-t0);
    }
    if (!big)
        printf("  SKIPPED GetWriteWatch timing: no %.1f MB committed at +%.0f MB of a %.0f MB arena\n",
               static_cast<double>(bigBytes)/MB,static_cast<double>(bigOff)/MB,static_cast<double>(arenaSize)/MB);
    // cost of a first-touch 4 KiB write fault on a fresh watched region vs a
    // fresh unwatched region (both committed, untouched).
    {
        SIZE_T N=10000;
        void *watched = VirtualAlloc(NULL, N*pg, MEM_RESERVE|MEM_COMMIT|MEM_WRITE_WATCH, PAGE_READWRITE);
        void *plain   = VirtualAlloc(NULL, N*pg, MEM_RESERVE|MEM_COMMIT, PAGE_READWRITE);
        double t0=now_ms(freq);
        for (SIZE_T i=0;i<N;i++){ volatile char *pp=static_cast<char *>(watched)+i*pg; *pp=static_cast<char>(2); }
        double t1=now_ms(freq);
        for (SIZE_T i=0;i<N;i++){ volatile char *pp=static_cast<char *>(plain)+i*pg; *pp=static_cast<char>(2); }
        double t2=now_ms(freq);
        printf("  first-touch %lu pages: watched=%.3f ms (%.1f ns/pg) unwatched=%.3f ms (%.1f ns/pg)\n",
               static_cast<unsigned long>(N), t1-t0,(t1-t0)*1e6/N, t2-t1,(t2-t1)*1e6/N);
        if(watched) VirtualFree(watched,0,MEM_RELEASE);
        if(plain) VirtualFree(plain,0,MEM_RELEASE);
    }

    printf("\n=== X12 done ===\n");
    return 0;
}
