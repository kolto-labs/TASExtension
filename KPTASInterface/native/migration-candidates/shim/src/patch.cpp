/* IAT slot patching and 5-byte detours with fixed, pre-checked prologues. */
#include "shim.hpp"

char g_hooks_list[4096];
u32 g_hooks_n;

void hooks_note(const char *name) {
    u32 l = (u32)strlen(g_hooks_list), n = (u32)strlen(name);
    if (l + n + 2 >= sizeof g_hooks_list) return;
    if (l) g_hooks_list[l++] = ',';
    memcpy(g_hooks_list + l, name, n);
    g_hooks_list[l + n] = 0;
    g_hooks_n++;
}

static int write_code(void *at, const void *src, u32 n) {
    DWORD old;
    if (!VirtualProtect(at, n, PAGE_EXECUTE_READWRITE, &old)) return 0;
    memcpy(at, src, n);
    VirtualProtect(at, n, old, &old);
    FlushInstructionCache(GetCurrentProcess(), at, n);
    return 1;
}

/* Replace the IAT slot of dll!fn in module mod. Returns the original target or 0. */
void *iat_hook(HMODULE mod, const char *dll, const char *fn, void *hook) {
    u8 *base = reinterpret_cast<u8 *>(mod);
    IMAGE_DOS_HEADER *dos = reinterpret_cast<IMAGE_DOS_HEADER *>(base);
    IMAGE_NT_HEADERS *nt = reinterpret_cast<IMAGE_NT_HEADERS *>(base + dos->e_lfanew);
    IMAGE_DATA_DIRECTORY idd = nt->OptionalHeader.DataDirectory[IMAGE_DIRECTORY_ENTRY_IMPORT];
    if (!idd.VirtualAddress) return nullptr;
    IMAGE_IMPORT_DESCRIPTOR *imp = reinterpret_cast<IMAGE_IMPORT_DESCRIPTOR *>(base + idd.VirtualAddress);
    for (; imp->Name; imp++) {
        const char *name = reinterpret_cast<const char *>(base + imp->Name);
        if (!k_strieq(name, dll)) continue;
        IMAGE_THUNK_DATA *oft = reinterpret_cast<IMAGE_THUNK_DATA *>(base + (imp->OriginalFirstThunk ? imp->OriginalFirstThunk : imp->FirstThunk));
        IMAGE_THUNK_DATA *ft = reinterpret_cast<IMAGE_THUNK_DATA *>(base + imp->FirstThunk);
        for (; oft->u1.AddressOfData; oft++, ft++) {
            if (oft->u1.Ordinal & IMAGE_ORDINAL_FLAG32) continue;
            IMAGE_IMPORT_BY_NAME *ibn = reinterpret_cast<IMAGE_IMPORT_BY_NAME *>(base + oft->u1.AddressOfData);
            if (!k_streq(reinterpret_cast<const char *>(ibn->Name), fn)) continue;
            DWORD old;
            u32 *slot = reinterpret_cast<u32 *>(&ft->u1.Function);
            if (!VirtualProtect(slot, 4, PAGE_READWRITE, &old)) return 0;
            void *orig = reinterpret_cast<void *>(static_cast<uintptr_t>(*slot));
            *slot = static_cast<u32>(reinterpret_cast<uintptr_t>(hook));
            VirtualProtect(slot, 4, old, &old);
            return orig;
        }
    }
    return nullptr;
}

int patch_jmp(u32 addr, void *target) {
    u8 j[5];
    j[0] = 0xE9;
    *reinterpret_cast<i32 *>(j + 1) = static_cast<i32>(static_cast<u32>(reinterpret_cast<uintptr_t>(target)) - (addr + 5));
    return write_code(reinterpret_cast<void *>(static_cast<uintptr_t>(addr)), j, 5);
}

/* Detour addr: check its first len bytes equal expect, copy them to an
 * executable trampoline (fixing E8/E9 rel32 at instruction starts given by
 * insn_lens, 0-terminated), append a jmp back, then write jmp hook at addr.
 * *tramp receives the trampoline, which behaves as the original function. */
int detour(u32 addr, const u8 *expect, u32 len, const u8 *insn_lens, void *hook, void **tramp) {
    if (len < 5 || len > 16) return 0;
    if (memcmp(reinterpret_cast<const void *>(static_cast<uintptr_t>(addr)), expect, len) != 0) return 0;
    u8 *t = static_cast<u8 *>(core_exec_alloc(len + 5));
    if (!t) return 0;
    memcpy(t, reinterpret_cast<const void *>(static_cast<uintptr_t>(addr)), len);
    u32 off = 0;
    for (const u8 *il = insn_lens; *il && off < len; il++) {
        u8 op = t[off];
        if ((op == 0xE8 || op == 0xE9) && *il == 5) {
            u32 dst = addr + off + 5 + *reinterpret_cast<i32 *>(t + off + 1);
            *reinterpret_cast<i32 *>(t + off + 1) = static_cast<i32>(dst - (static_cast<u32>(reinterpret_cast<uintptr_t>(t)) + off + 5));
        }
        off += *il;
    }
    if (off != len) return 0;
    t[len] = 0xE9;
    *reinterpret_cast<i32 *>(t + len + 1) = static_cast<i32>((addr + len) - (static_cast<u32>(reinterpret_cast<uintptr_t>(t)) + len + 5));
    FlushInstructionCache(GetCurrentProcess(), t, len + 5);
    *tramp = t;
    return patch_jmp(addr, hook);
}
