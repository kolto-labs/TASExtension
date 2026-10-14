/* IAT slot patching, 5-byte detours with fixed, pre-checked prologues, and the
 * registry of rewritten exe code. */
#include "shim.hpp"

// Retain the native C ABI, including CRT and assembly-referenced symbols.
extern "C" {

char g_hooks_list[4096];
u32 g_hooks_n;

void hooks_note(const char *name) {
    u32 l = static_cast<u32>(strlen(g_hooks_list)), n = static_cast<u32>(strlen(name));
    if (l + n + 2 >= sizeof g_hooks_list) return;
    if (l) g_hooks_list[l++] = ',';
    memcpy(g_hooks_list + l, name, n);
    g_hooks_list[l + n] = 0;
    g_hooks_n++;
}

/* Exe code ranges already rewritten: one owner per byte, so two patches
 * (a detour and a math window, say) can never overlap. */
static constexpr u32 NCLAIM = 256;
static u32 g_claim_lo[NCLAIM], g_claim_hi[NCLAIM], g_nclaim;

int patch_free(u32 addr, u32 n) {
    for (u32 i = 0; i < g_nclaim; i++)
        if (addr < g_claim_hi[i] && g_claim_lo[i] < addr + n) return 0;
    return g_nclaim < NCLAIM;
}

int patch_claim(u32 addr, u32 n) {
    if (!patch_free(addr, n)) return 0;
    g_claim_lo[g_nclaim] = addr;
    g_claim_hi[g_nclaim] = addr + n;
    g_nclaim++;
    return 1;
}

/* The hook ledger (contract section 6: "each gate checks the shim's patched
 * bytes, and a changed slot halts the run with a report"). The write
 * primitives below note every address they rewrite; ledger_seal, after the
 * last install, takes the expected bytes from memory, so a chained slot holds
 * the final value the shim left. A note after the seal (a later rewrite by
 * the shim itself) snapshots at once, so it never trips the check. Flat
 * arrays, no allocation: the gate compares about a thousand words. */
static constexpr u32 NLEDGER = 2048, LEDGER_B = 16;
static u32 g_led_addr[NLEDGER], g_nled, g_led_dropped;
static u8 g_led_n[NLEDGER], g_led_b[NLEDGER][LEDGER_B];
static int g_led_sealed;

static void led_one(u32 addr, u32 n) {
    u32 i = 0;
    while (i < g_nled && !(g_led_addr[i] == addr && g_led_n[i] == n)) i++;
    if (i == g_nled) {
        if (g_nled == NLEDGER) { g_led_dropped++; return; }
        g_led_addr[i] = addr;
        g_led_n[i] = static_cast<u8>(n);
        g_nled++;
    }
    if (g_led_sealed) memcpy(g_led_b[i], (const void *)reinterpret_cast<uintptr_t>(addr), n);
}

void ledger_note(u32 addr, u32 n) {
    while (n) {
        u32 k = n > LEDGER_B ? LEDGER_B : n;
        led_one(addr, k);
        addr += k;
        n -= k;
    }
}

/* Every exe IAT slot is either hooked or wrapped by xtrace (which writes the
 * slots directly), so the whole import table joins, then everything noted is
 * read back as the expected value. */
void ledger_seal(void) {
    u8 *base = reinterpret_cast<u8 *>(GetModuleHandleA(NULL));
    IMAGE_NT_HEADERS *nt = reinterpret_cast<IMAGE_NT_HEADERS *>(base + reinterpret_cast<IMAGE_DOS_HEADER *>(base)->e_lfanew);
    IMAGE_DATA_DIRECTORY idd = nt->OptionalHeader.DataDirectory[IMAGE_DIRECTORY_ENTRY_IMPORT];
    if (idd.VirtualAddress)
        for (IMAGE_IMPORT_DESCRIPTOR *imp = reinterpret_cast<IMAGE_IMPORT_DESCRIPTOR *>(base + idd.VirtualAddress); imp->Name; imp++)
            for (IMAGE_THUNK_DATA *ft = reinterpret_cast<IMAGE_THUNK_DATA *>(base + imp->FirstThunk); ft->u1.Function; ft++)
                ledger_note(static_cast<u32>(reinterpret_cast<uintptr_t>(&ft->u1.Function)), 4);
    for (u32 i = 0; i < g_nled; i++) memcpy(g_led_b[i], (const void *)reinterpret_cast<uintptr_t>(g_led_addr[i]), g_led_n[i]);
    g_led_sealed = 1;
    slog("ledger: %u entries sealed%s", g_nled, g_led_dropped ? " (FULL: some writes not covered)" : "");
    if (g_led_dropped) slog("ledger: %u entries dropped", g_led_dropped);
}

u32 ledger_entries(void) { return g_nled; }

/* Gate check. IAT slots compare as one word; code sites with memcmp. */
int ledger_check(u32 *addr, u8 exp[16], u8 obs[16], u32 *n) {
    for (u32 i = 0; i < g_nled; i++) {
        const u8 *at = (const u8 *)reinterpret_cast<uintptr_t>(g_led_addr[i]);
        if (g_led_n[i] == 4) {
            u32 a, b;
            __builtin_memcpy(&a, at, 4);
            __builtin_memcpy(&b, g_led_b[i], 4);
            if (a == b) continue;
        } else if (memcmp(at, g_led_b[i], g_led_n[i]) == 0) continue;
        *addr = g_led_addr[i];
        *n = g_led_n[i];
        memcpy(exp, g_led_b[i], *n);
        memcpy(obs, at, *n);
        return 1;
    }
    return 0;
}

int patch_bytes(u32 addr, const void *src, u32 n) {
    void *at = (void *)reinterpret_cast<uintptr_t>(addr);
    DWORD old;
    if (!VirtualProtect(at, n, PAGE_EXECUTE_READWRITE, &old)) return 0;
    memcpy(at, src, n);
    VirtualProtect(at, n, old, &old);
    FlushInstructionCache(GetCurrentProcess(), at, n);
    ledger_note(addr, n);
    return 1;
}

/* The IAT slot of dll!fn in module mod, or 0. */
u32 *iat_slot(HMODULE mod, const char *dll, const char *fn) {
    u8 *base = reinterpret_cast<u8 *>(mod);
    IMAGE_DOS_HEADER *dos = reinterpret_cast<IMAGE_DOS_HEADER *>(base);
    IMAGE_NT_HEADERS *nt = reinterpret_cast<IMAGE_NT_HEADERS *>((base + dos->e_lfanew));
    IMAGE_DATA_DIRECTORY idd = nt->OptionalHeader.DataDirectory[IMAGE_DIRECTORY_ENTRY_IMPORT];
    if (!idd.VirtualAddress) return 0;
    IMAGE_IMPORT_DESCRIPTOR *imp = reinterpret_cast<IMAGE_IMPORT_DESCRIPTOR *>((base + idd.VirtualAddress));
    for (; imp->Name; imp++) {
        if (!k_strieq(reinterpret_cast<const char *>((base + imp->Name)), dll)) continue;
        IMAGE_THUNK_DATA *oft = reinterpret_cast<IMAGE_THUNK_DATA *>((base + (imp->OriginalFirstThunk ? imp->OriginalFirstThunk : imp->FirstThunk)));
        IMAGE_THUNK_DATA *ft = reinterpret_cast<IMAGE_THUNK_DATA *>((base + imp->FirstThunk));
        for (; oft->u1.AddressOfData; oft++, ft++) {
            if (oft->u1.Ordinal & IMAGE_ORDINAL_FLAG32) continue;
            IMAGE_IMPORT_BY_NAME *ibn = reinterpret_cast<IMAGE_IMPORT_BY_NAME *>((base + oft->u1.AddressOfData));
            if (k_streq(reinterpret_cast<const char *>(ibn->Name), fn)) return reinterpret_cast<u32 *>(&ft->u1.Function);
        }
    }
    return 0;
}

/* Point an IAT slot at hook. */
int iat_set(u32 *slot, void *hook) {
    DWORD old;
    if (!VirtualProtect(slot, 4, PAGE_READWRITE, &old)) return 0;
    *slot = static_cast<u32>(reinterpret_cast<uintptr_t>(hook));
    VirtualProtect(slot, 4, old, &old);
    ledger_note(static_cast<u32>(reinterpret_cast<uintptr_t>(slot)), 4);
    return 1;
}

static int write_jmp(u32 addr, void *target) {
    u8 j[5];
    j[0] = 0xE9;
    const i32 relative = static_cast<i32>((static_cast<u32>(reinterpret_cast<uintptr_t>(target)) - (addr + 5)));
    __builtin_memcpy(j + 1, &relative, sizeof relative);
    return patch_bytes(addr, j, 5);
}

/* Replace the IAT slot of dll!fn in module mod. Returns the original target or 0. */
void *iat_hook(HMODULE mod, const char *dll, const char *fn, void *hook) {
    u8 *base = reinterpret_cast<u8 *>(mod);
    IMAGE_DOS_HEADER *dos = reinterpret_cast<IMAGE_DOS_HEADER *>(base);
    IMAGE_NT_HEADERS *nt = reinterpret_cast<IMAGE_NT_HEADERS *>((base + dos->e_lfanew));
    IMAGE_DATA_DIRECTORY idd = nt->OptionalHeader.DataDirectory[IMAGE_DIRECTORY_ENTRY_IMPORT];
    if (!idd.VirtualAddress) return 0;
    IMAGE_IMPORT_DESCRIPTOR *imp = reinterpret_cast<IMAGE_IMPORT_DESCRIPTOR *>((base + idd.VirtualAddress));
    for (; imp->Name; imp++) {
        const char *name = reinterpret_cast<const char *>((base + imp->Name));
        if (!k_strieq(name, dll)) continue;
        IMAGE_THUNK_DATA *oft = reinterpret_cast<IMAGE_THUNK_DATA *>((base + (imp->OriginalFirstThunk ? imp->OriginalFirstThunk : imp->FirstThunk)));
        IMAGE_THUNK_DATA *ft = reinterpret_cast<IMAGE_THUNK_DATA *>((base + imp->FirstThunk));
        for (; oft->u1.AddressOfData; oft++, ft++) {
            if (oft->u1.Ordinal & IMAGE_ORDINAL_FLAG32) continue;
            IMAGE_IMPORT_BY_NAME *ibn = reinterpret_cast<IMAGE_IMPORT_BY_NAME *>((base + oft->u1.AddressOfData));
            if (!k_streq(reinterpret_cast<const char *>(ibn->Name), fn)) continue;
            DWORD old;
            u32 *slot = reinterpret_cast<u32 *>(&ft->u1.Function);
            if (!VirtualProtect(slot, 4, PAGE_READWRITE, &old)) return 0;
            void *orig = (void *)reinterpret_cast<uintptr_t>(*slot);
            *slot = static_cast<u32>(reinterpret_cast<uintptr_t>(hook));
            VirtualProtect(slot, 4, old, &old);
            ledger_note(static_cast<u32>(reinterpret_cast<uintptr_t>(slot)), 4);
            return orig;
        }
    }
    return 0;
}

int patch_jmp(u32 addr, void *target) {
    return patch_claim(addr, 5) && write_jmp(addr, target);
}

/* Detour addr: check its first len bytes equal expect, copy them to an
 * executable trampoline (fixing E8/E9 rel32 at instruction starts given by
 * insn_lens, 0-terminated), append a jmp back, then write jmp hook at addr.
 * *tramp receives the trampoline, which behaves as the original function. */
int detour(u32 addr, const u8 *expect, u32 len, const u8 *insn_lens, void *hook, void **tramp) {
    if (len < 5 || len > 16) return 0;
    if (memcmp((const void *)reinterpret_cast<uintptr_t>(addr), expect, len) != 0) return 0;
    if (!patch_claim(addr, len)) return 0;
    u8 *t = reinterpret_cast<u8 *>(core_exec_alloc(len + 5));
    if (!t) return 0;
    memcpy(t, (const void *)reinterpret_cast<uintptr_t>(addr), len);
    u32 off = 0;
    for (const u8 *il = insn_lens; *il && off < len; il++) {
        u8 op = t[off];
        if ((op == 0xE8 || op == 0xE9) && *il == 5) {
            i32 old_relative; __builtin_memcpy(&old_relative, t + off + 1, sizeof old_relative);
            u32 dst = addr + off + 5 + old_relative;
            const i32 relative = static_cast<i32>((dst - (static_cast<u32>(reinterpret_cast<uintptr_t>(t)) + off + 5)));
            __builtin_memcpy(t + off + 1, &relative, sizeof relative);
        }
        off += *il;
    }
    if (off != len) return 0;
    t[len] = 0xE9;
    const i32 relative = static_cast<i32>(((addr + len) - (static_cast<u32>(reinterpret_cast<uintptr_t>(t)) + len + 5)));
    __builtin_memcpy(t + len + 1, &relative, sizeof relative);
    FlushInstructionCache(GetCurrentProcess(), t, len + 5);
    *tramp = t;
    return write_jmp(addr, hook);
}

} // extern "C"
