// LOAD probe: dinput8.dll proxy. Exports DirectInput8Create (stdcall,
// undecorated) and forwards to the real system dinput8.dll. In DllMain
// PROCESS_ATTACH (no threads created) it logs a timestamp, thread id, the
// loaded-module list, and IAT-hooks the exe's kernel32 time functions so the
// first call of each is logged with its return address. Only touches this
// process. 32-bit.

#include <windows.h>
#include <stdint.h>
#include <stdio.h>

#define LOGPATH "kptas_load.log"

static void logline(const char *fmt, ...){
    char buf[1024];
    va_list ap; va_start(ap,fmt);
    int n=wvsprintfA(buf,fmt,ap);
    va_end(ap);
    HANDLE h=CreateFileA(LOGPATH, FILE_APPEND_DATA, FILE_SHARE_READ|FILE_SHARE_WRITE,
                         NULL, OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
    if(h!=INVALID_HANDLE_VALUE){
        DWORD w; SetFilePointer(h,0,NULL,FILE_END);
        WriteFile(h,buf,n,&w,NULL);
        WriteFile(h,"\r\n",2,&w,NULL);
        CloseHandle(h);
    }
}

// ---- real dinput8 forwarding (lazy load, outside loader lock) ----
typedef HRESULT (WINAPI *DI8C_t)(HINSTANCE,DWORD,REFIID,LPVOID*,LPVOID);
static DI8C_t real_DI8C = NULL;

static void load_real(void){
    if(real_DI8C) return;
    char sysdir[MAX_PATH]; GetSystemDirectoryA(sysdir,MAX_PATH);
    char path[MAX_PATH];
    wsprintfA(path,"%s\\dinput8.dll",sysdir);
    HMODULE m=LoadLibraryA(path);
    logline("[fwd] LoadLibrary(%s) -> %p", path, m);
    if(m) real_DI8C=(DI8C_t)GetProcAddress(m,"DirectInput8Create");
    logline("[fwd] real DirectInput8Create=%p", (void*)real_DI8C);
}

__declspec(dllexport) HRESULT WINAPI DirectInput8Create(
        HINSTANCE hinst, DWORD ver, REFIID riid, LPVOID *out, LPVOID outer){
    load_real();
    logline("[fwd] DirectInput8Create called ver=%#lx tid=%lu", ver, GetCurrentThreadId());
    if(!real_DI8C) return E_FAIL;
    return real_DI8C(hinst,ver,riid,out,outer);
}

// ---- PEB module-list walk ----
typedef struct _LIST_ENTRY32 { uint32_t Flink,Blink; } LE;
static void dump_modules(void){
    uint32_t peb;
    __asm__ volatile("movl %%fs:0x30,%0":"=r"(peb));
    uint32_t ldr=*(uint32_t*)(peb+0x0C);            // PEB.Ldr
    uint32_t head=ldr+0x0C;                          // InLoadOrderModuleList
    uint32_t cur=*(uint32_t*)head;
    int i=0;
    logline("[peb] module list (InLoadOrder):");
    while(cur!=head && i<64){
        // LDR_DATA_TABLE_ENTRY: +0x18 DllBase, +0x1C EntryPoint, +0x24 FullDllName(UNICODE_STRING len@+0x24 buf@+0x28... actually +0x24 base? )
        uint32_t dllbase=*(uint32_t*)(cur+0x18);
        uint32_t entry  =*(uint32_t*)(cur+0x1C);
        // FullDllName is at +0x24 (UNICODE_STRING: USHORT Length,MaxLen; PWSTR Buffer@+0x28)
        uint16_t len=*(uint16_t*)(cur+0x24);
        uint32_t bufp=*(uint32_t*)(cur+0x28);
        char name[260]; int k=0;
        if(bufp && len){ wchar_t *w=(wchar_t*)bufp;
            for(int j=0;j<len/2 && k<259;j++) name[k++]=(char)w[j]; }
        name[k]=0;
        logline("  base=%08x entry=%08x %s", dllbase, entry, name);
        cur=*(uint32_t*)cur; i++;
    }
}

// ---- IAT hook of the exe's kernel32 time functions ----
static DWORD (WINAPI *real_GTC)(void)=NULL;
static void  (WINAPI *real_GSTAFT)(LPFILETIME)=NULL;
static BOOL  (WINAPI *real_QPC)(LARGE_INTEGER*)=NULL;
static volatile LONG g_gtc_first=0,g_gstaft_first=0,g_qpc_first=0;

__attribute__((noinline)) static DWORD WINAPI hook_GTC(void){
    if(InterlockedExchange(&g_gtc_first,1)==0)
        logline("[hook] GetTickCount FIRST retaddr=%p tid=%lu",
                __builtin_return_address(0), GetCurrentThreadId());
    return real_GTC();
}
__attribute__((noinline)) static void WINAPI hook_GSTAFT(LPFILETIME ft){
    if(InterlockedExchange(&g_gstaft_first,1)==0)
        logline("[hook] GetSystemTimeAsFileTime FIRST retaddr=%p tid=%lu",
                __builtin_return_address(0), GetCurrentThreadId());
    real_GSTAFT(ft);
}
__attribute__((noinline)) static BOOL WINAPI hook_QPC(LARGE_INTEGER *p){
    if(InterlockedExchange(&g_qpc_first,1)==0)
        logline("[hook] QueryPerformanceCounter FIRST retaddr=%p tid=%lu",
                __builtin_return_address(0), GetCurrentThreadId());
    return real_QPC(p);
}

static int patch1(uint32_t *slot, void *newfn, void **saveOrig){
    DWORD old;
    if(!VirtualProtect(slot,4,PAGE_READWRITE,&old)) return 0;
    *saveOrig=(void*)(uintptr_t)*slot;
    *slot=(uint32_t)(uintptr_t)newfn;
    VirtualProtect(slot,4,old,&old);
    return 1;
}

static void iat_hook_exe(void){
    HMODULE exe=GetModuleHandleA(NULL);
    uint8_t *base=(uint8_t*)exe;
    IMAGE_DOS_HEADER *dos=(IMAGE_DOS_HEADER*)base;
    IMAGE_NT_HEADERS *nt=(IMAGE_NT_HEADERS*)(base+dos->e_lfanew);
    logline("[iat] exe base=%p entryRVA=%#lx (entry runs after loader; DllMain is now)",
            base,(unsigned long)nt->OptionalHeader.AddressOfEntryPoint);
    IMAGE_DATA_DIRECTORY idd=nt->OptionalHeader.DataDirectory[IMAGE_DIRECTORY_ENTRY_IMPORT];
    if(!idd.VirtualAddress){ logline("[iat] no import dir"); return; }
    IMAGE_IMPORT_DESCRIPTOR *imp=(IMAGE_IMPORT_DESCRIPTOR*)(base+idd.VirtualAddress);
    int hooked=0;
    for(;imp->Name;imp++){
        const char *dll=(const char*)(base+imp->Name);
        if(lstrcmpiA(dll,"KERNEL32.dll")!=0 && lstrcmpiA(dll,"KERNEL32.DLL")!=0) continue;
        IMAGE_THUNK_DATA *oft=(IMAGE_THUNK_DATA*)(base+(imp->OriginalFirstThunk?imp->OriginalFirstThunk:imp->FirstThunk));
        IMAGE_THUNK_DATA *ft =(IMAGE_THUNK_DATA*)(base+imp->FirstThunk);
        for(; oft->u1.AddressOfData; oft++,ft++){
            if(oft->u1.Ordinal & IMAGE_ORDINAL_FLAG32) continue;
            IMAGE_IMPORT_BY_NAME *ibn=(IMAGE_IMPORT_BY_NAME*)(base+oft->u1.AddressOfData);
            const char *nm=(const char*)ibn->Name;
            if(!lstrcmpA(nm,"GetTickCount")){
                if(patch1((uint32_t*)&ft->u1.Function,(void*)hook_GTC,(void**)&real_GTC)){hooked++;
                    logline("[iat] hooked GetTickCount slot=%p orig=%p",&ft->u1.Function,(void*)real_GTC);}
            } else if(!lstrcmpA(nm,"GetSystemTimeAsFileTime")){
                if(patch1((uint32_t*)&ft->u1.Function,(void*)hook_GSTAFT,(void**)&real_GSTAFT)){hooked++;
                    logline("[iat] hooked GetSystemTimeAsFileTime slot=%p orig=%p",&ft->u1.Function,(void*)real_GSTAFT);}
            } else if(!lstrcmpA(nm,"QueryPerformanceCounter")){
                if(patch1((uint32_t*)&ft->u1.Function,(void*)hook_QPC,(void**)&real_QPC)){hooked++;
                    logline("[iat] hooked QueryPerformanceCounter slot=%p orig=%p",&ft->u1.Function,(void*)real_QPC);}
            }
        }
    }
    logline("[iat] total hooks installed=%d", hooked);
}

BOOL WINAPI DllMain(HINSTANCE h, DWORD reason, LPVOID reserved){
    (void)h;(void)reserved;
    if(reason==DLL_PROCESS_ATTACH){
        DisableThreadLibraryCalls(h);
        LARGE_INTEGER qpc,freq; QueryPerformanceCounter(&qpc); QueryPerformanceFrequency(&freq);
        logline("==== dinput8 proxy PROCESS_ATTACH ====");
        logline("[attach] qpc=%I64d freq=%I64d tid=%lu",
                qpc.QuadPart, freq.QuadPart, GetCurrentThreadId());
        dump_modules();
        iat_hook_exe();
        logline("[attach] DllMain complete (before exe entry point / CRT / WinMain)");
    } else if(reason==DLL_PROCESS_DETACH){
        logline("==== dinput8 proxy PROCESS_DETACH gtc_seen=%d gstaft_seen=%d qpc_seen=%d ====",
                (int)g_gtc_first,(int)g_gstaft_first,(int)g_qpc_first);
    }
    return TRUE;
}
