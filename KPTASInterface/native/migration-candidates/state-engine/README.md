# Dormant state-engine C++ candidate

This directory is a complete candidate copy of the native state engine. It is
not included by any build or caller; the active `native/state` sources remain
unchanged.

## File ownership

| Candidate | Source |
| --- | --- |
| `kst_arena.cpp` | `native/state/kst_arena.c` |
| `kst_capture.cpp` | `native/state/kst_capture.c` |
| `kst_ctx.cpp` | `native/state/kst_ctx.c` |
| `kst_hash.cpp` | `native/state/kst_hash.c` |
| `kst_restore.cpp` | `native/state/kst_restore.c` |
| `kst_rt.cpp` | `native/state/kst_rt.c` |
| `kst.hpp` | `native/state/kst.h` |
| `kst_int.hpp` | `native/state/kst_int.h` |

These candidates preserve the engine algorithms and ABI while using C++ type
aliases and native `struct` declarations in the staged headers, plus explicit
casts where C++ requires them. They are not an object-oriented rewrite.

Both headers expose project functions and globals with C linkage so the existing
C callers and separately compiled shim can keep their symbol names. The
internal declarations in `kst_int.hpp` use the same ABI. `windows.h` is
included before the linkage block.

## Constraints to preserve during integration

- This is a 32-bit x86 Windows engine. The context switch, TEB field access,
  `__cdecl` trampoline, and stack handoff use GCC-style inline assembly and
  x86 register/stack assumptions. Raw-word access over byte buffers uses the
  GNU-compatible `may_alias` type attribute to retain the aligned fast path
  without violating C++ strict aliasing. Keep a compatible compiler and target.
- The engine uses Win32 memory, write-watch, file mapping, threads, events,
  and heap APIs, plus `ntdll`'s `RtlCreateHeap` entry point.
- `kst_rt.cpp` supplies freestanding `mem*` and 64-bit division helpers unless
  `KST_NO_CRT_HELPERS` is set. Keep the no-CRT build/link behavior and avoid
  introducing C++ runtime dependencies.
- Packed journal records, section attributes, page layout, and all binary
  protocol fields must retain their current size and offsets.

## Conversion status and remaining work

The source files are staged as C++ translation units and point at the copied
`.hpp` headers. Common C-style pointer and numeric conversions use explicit
`static_cast` or `reinterpret_cast`; public record fields and ordering are
unchanged while declarations use C++ struct syntax. Word access to arbitrary
buffers is explicitly alias-safe, and the SIMD hash decodes little-endian words
through the byte-based helper. Capture and restore copy the shared packed record
header to/from byte buffers, avoiding typed overlays while retaining the same
40-byte layout. Compile-time assertions pin that wire size plus the 32-bit
pointer, context, config, scheduler-slot, and harness-game layouts. In
`kst_rt.cpp`,
`memmove` now compares the integer address values of its pointers instead of
using a relational comparison between potentially unrelated pointers; on the
required 32-bit flat-address target this preserves overlap direction while
avoiding that C++ undefined comparison. Compiler attributes, inline assembly,
and freestanding helper definitions remain. The six state-engine translation
units compile with Zig 0.16.0's C++20 x86-Windows target and link with all 18
staged shim translation units under `-nostdlib`; no tests or runtime checks were
run. Before integration, verify the 32-bit pointer/integer assumptions, `mem*`
declarations against compiler built-ins, packed-structure layout, callback
linkage supplied by the shim, and actual capture/restore behavior.

The source review also added a four-byte alignment check before inspecting a
candidate heap-lock debug pointer and computes signed integer magnitudes in
unsigned arithmetic so `INT64_MIN` does not trigger signed-negation overflow.
The positive `INT64_MIN / -1` case remains outside the C signed-division
contract; callers must not rely on it.

Later integration must update the native state build source list as one cluster,
compile all six `.cpp` files with the existing x86/freestanding flags, link the
C ABI callers, and validate state capture/restore behavior. Do not move only a
subset into the active build.
