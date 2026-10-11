# Inactive C++ shim candidates

This directory contains C++ candidates for all 18 active shim implementation
units. The candidate `.cpp` files preserve the current behavior and the dirty
SHA-256 input changes present in `dinput`, `journal`, `state`, `wire`, and the
shim declarations. The active `.c` files and build script remain unchanged.

## C++ work completed

- `shim.hpp` consumes the staged `../../state-engine/kst_int.hpp` declarations
  from `src/`, keeping the dormant C++ migration closure self-contained. The
  C-linkage declarations preserve the existing state-engine symbols.
- Shared data types in `shim.hpp` use direct C++ `struct` declarations rather
  than C `typedef struct` wrappers; member order and declarations are unchanged.
- Fixed-width aliases and local function-pointer aliases use C++ `using`, and
  pointer-valued failure paths use `nullptr`. The header rejects non-32-bit
  pointer targets at compile time, matching the shim's address and hook ABI.
- Candidate-local parser, real-handle, and VM-call records are direct C++
  structs; fixed GL constants are typed `constexpr` values rather than macros.
- Callback trampolines and vtables use typed function-pointer conversion at
  the existing erased ABI boundary; local control flow stays allocation-free
  because the shim is freestanding and its arenas/serialized structs have fixed
  lifetimes and layouts.
- `shim_abi` centralizes function-pointer conversions required by the C
  `iat_hook`/`detour` interfaces. Hook saves restore the declared callback
  type, retaining `WINAPI` and other existing signatures.
- The detour call sites use a typed C++ adapter for their function-pointer
  trampoline outputs instead of passing function-pointer slots as `void **`.
- Gate trampoline globals referenced by x86 inline assembly retain C symbol
  names; the entry trampoline itself now has a typed `void (*)(void)` slot.
- The save-game trampoline globals and `save_begin`/`save_done` callbacks also
  retain C symbol names because the naked x86 stub references them directly.
- Callbacks handed to the unchanged C state engine (`kst_hcall`, slot entry,
  output callback, and journal callback) use C linkage at their boundary.
- The OpenGL extension table now stores erased addresses as values and uses
  typed setter helpers for functions called internally. It no longer aliases
  function-pointer objects through `void **`.
- `DirectInput8Create` remains explicitly C-linked and exported. DirectInput
  vtable entries are explicitly erased at the Win32 ABI boundary. Procedure
  lookup returns and the `REFGUID` byte view use explicit C++ casts.
- Pointer views through allocator results, PE tables, Win32/game structures,
  fixed 32-bit addresses, and serialized bytes now use explicit `static_cast`
  or `reinterpret_cast` operations. This makes each C++ conversion category
  visible while keeping the original 32-bit layout and access behavior.
- Pointer-to-address narrowing is centralized through `shim_abi::address32`,
  which has a compile-time 32-bit pointer-width guard; the staged units use it
  at the existing game-address boundary rather than repeating C-style
  `(u32)(uintptr_t)` casts. Pointer-valued allocation/search failures use
  `nullptr` instead of integer zero. Scalar status returns remain integer zero.

## Source-parity audit (2026-10-09)

Compared all 18 candidate translation units and `shim.hpp` against the active
`native/shim/src/*.c` and `shim.h`, including the five currently modified
active files. The active SHA-256 input-fingerprint changes are present in the
candidates: the `di_stage` signature and digest, journal field, initial empty
input stage, and per-gate input stage. No missing active implementation unit,
public shim declaration, or algorithm change was found in this source-level
comparison. Candidate body changes are explicit C++ casts, C-linkage callback
boundaries, typed trampoline handling, and the local pointer/type cleanups
listed above. The additional `save_*` functions in `gl.cpp` only replace erased
function-pointer slot writes with typed assignment helpers.

This is source-parity evidence only; it does not establish that the candidate
set compiles, links without C++ runtime initialization, preserves exports, or
matches runtime behavior. Those promotion checks remain pending.

The staging pass also routes 71 existing pointer-to-32-bit-address conversions
through `shim_abi::address32`, preserving the x86 address representation while
making its width requirement explicit. Remaining integer `0` returns are
status, scalar, or handle values; pointer-returning allocation/search failures
in the reviewed units use `nullptr`.

## C++ source review follow-up (2026-10-09)

- The staged state-engine header is included before the shim declarations' C
  linkage block; its own public declarations retain their explicit C linkage.
- DirectInput's fixed HRESULT and flag values are typed `constexpr` constants,
  and GUID byte decoding uses `memcpy` rather than an aliasing `u32` load.
- Volatile gate counters are reset independently, avoiding deprecated chained
  assignment in C++20; the detour helper initializes its raw pointer with
  `nullptr`, and the default FILETIME is a typed constant.
- Compile-only syntax validation passed for all 18 staged `.cpp` units with
  `i686-w64-mingw32-g++ -std=c++20 -fsyntax-only -Werror=volatile`. No tests
  were run and no build artifacts were created in the repository.

This is still a source-review and syntax-only result. The existing candidate
DLL link receipt is separate historical evidence; this pass does not reproduce
that link or qualify runtime behavior. Deliberate 32-bit address views and
remaining scalar narrowing casts still require a compiler/ABI audit before
promotion.

Most algorithms, Win32 layouts, x86 address arithmetic, inline assembly, and
C-compatible aggregate initialization were carried over from the C units.
Scalar narrowing casts and the low-level pointer aliasing used for packed
32-bit memory remain close to the original implementation; those sites still
need compiler and runtime review for alignment, aliasing, and x86 assumptions.

## Remaining checks before activation

This directory is not referenced by any active build. The 18 staged shim units
and six staged state-engine units compile as C++20 and link as an x86-Windows
DLL with Zig 0.16.0 under `-nostdlib`; outputs were kept in
`/tmp/kptas-candidate-cxx20-e7idrbri/`. The resulting PE imports only
KERNEL32, USER32, and WS2_32, exports `DirectInput8Create` (including its
stdcall-decorated alias), and the 24 object files have no unresolved C++
runtime symbols. This is compiler/link qualification only: it does not prove
runtime behavior, output parity, save/restore determinism, or safe promotion.

The C hook APIs still erase function pointers to `void *`; `shim_abi` makes
those Win32/x86 ABI crossings explicit but cannot prove that the selected
compiler uses compatible function and object pointer representations. The
DirectInput vtables also remain erased `void *` arrays because the shared game
state layout exposes them that way. Before promotion, compare the inspected
`DirectInput8Create` export set with the active DLL, verify Win32 callback
calling conventions, and qualify replay determinism against the active C
build.
