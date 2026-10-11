# Dormant tooling and probe C++ candidates

These files are standalone candidate copies outside the active builds. No
build scripts, `.def` files, callers, or originals were changed.

## Candidates

| Candidate | Source owner | Notes for later integration |
| --- | --- | --- |
| `arena.cpp` | `native/probes/arena.c` | Win32 address-space, write-watch, and heap probe; remains a standalone 32-bit console executable. |
| `fiber.cpp` | `native/probes/fiber.c` | Win32 x86 TEB/SEH and inline-assembly fiber probe; remains a standalone 32-bit console executable. |
| `dinput8_proxy.cpp` | `native/probes/dinput8_proxy.c` | Win32 DLL proxy/IAT probe; retain the existing `dinput8.def` export definition and DLL link settings. |
| `golden.cpp` | `native/portable/golden/golden.c` | Project-owned reference-vector program; includes the staged C-compatible `../portable/refmath.hpp` API. |

These are mechanical C++ migration candidates, not redesigns: the probe and
golden-vector behavior is retained. Pointer and scalar conversions use explicit
C++ casts, and the two `GetProcAddress` results are explicitly converted to
their function-pointer types. The proxy entry points and
golden assembly helper symbols use explicit C linkage; `damage_k` keeps external
linkage for the golden program’s inline assembly. Golden double bit conversion
uses `memcpy` rather than C union type punning. The proxy logger closes its file
handle with a non-copyable scoped owner. The arena, fiber, and proxy candidates
reject non-32-bit targets at compile time. The fiber and golden candidates
include x86 inline assembly that needs the current compiler and architecture.
All four translation units compile to objects with Zig 0.16.0's C++20 driver
for `x86-windows-gnu`; `golden.cpp` also compiles for `x86_64-linux-musl`.
Object compilation does not verify exports, linking, or runtime behavior. In
particular, the probes require 32-bit Windows ABI, GCC-compatible x86 inline
assembly, Windows x86 SEH/TEB layout, and the unchanged `dinput8.def`
export/link setup. `arena.cpp` keeps
`setjmp`/`longjmp` only around its raw Win32 fault probe; do not add C++ objects
with non-trivial destructors across that jump. The `golden.cpp` candidate is
authored by the project. The math implementations are staged separately in
`../portable/` because they are derived from musl, Sun fdlibm, and Zig sources
and must retain their notices and numerical behavior.

## Remaining omitted sources and files

- `native/portable/refmath.c` and `native/portable/refmath_pow.c` are
  third-party-derived math implementations; their C++ translations and
  preserved notices are staged in `../portable/`.
- `native/portable/refmath.h` is the corresponding existing C-compatible API
  header. It already provides C linkage and is referenced directly; a renamed
  copy would add no useful conversion.
- `native/probes/dinput8.def` is a linker export-definition file, not a C
  source or header. It remains in its original location for future integration.
- `native/probes/build.sh`, `native/portable/build.sh`, golden check scripts,
  digests, TOML inventories, and Python tooling are shell/data/Python inputs,
  not C/C++ source candidates.
- C sources under `native/shim`, `native/simgame`, and `native/state` are
  covered by their separate migration-candidate directories and are not
  duplicated here.

The original build inputs remain active until a later integration updates the
source lists and verifies the Windows targets.
