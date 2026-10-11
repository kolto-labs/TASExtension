# Other native C++ migration candidates

This directory is deliberately outside every active build. It contains one
C++ candidate for the SimGame page-history module; no build files or callers
point here yet. It includes the staged `state-engine/kst.hpp`, so the candidate
cluster no longer reaches into the active C header tree.

## Candidate ownership

| Candidate | Source owner | C++ changes | Later integration |
| --- | --- | --- | --- |
| `hist.cpp`, `hist.hpp` | `native/simgame/hist.c`, `native/simgame/hist.h` | Private record types use native C++ `struct` declarations; pointer casts are explicit; null pointers use `nullptr`; and Win32 `OVERLAPPED` records use aggregate zero-initialization. The header exposes the existing history API with C linkage. | Object compilation passed for `x86-windows-gnu` with Zig 0.16.0's C++20 driver. Later integration still needs the Windows 32-bit target, freestanding runtime, state-engine linkage, behavior, and C ABI qualified before selecting it instead of `hist.c`. |

The page-history algorithm, fixed-width aliases, raw arrays, byte-level record
encoding, `memcpy`/`memcmp` calls, and Win32 API usage remain close to the C
source to preserve its file format and behavior. This is a staged C++ conversion,
not a rewritten or validated implementation. It does not add a second
implementation to the active build.

## Survey and exclusions

- `native/shim/src/*.c` and `shim.h` are owned by the separately scoped shim
  conversion work; excluded here. Five shim files and the header were dirty
  during this inventory.
- `native/portable/refmath.c`, `refmath_pow.c`, and
  `portable/golden/golden.c` have candidates in `../portable/` and
  `../tooling/`. Preserve upstream notices and qualify exact arithmetic and
  x87 calling conventions before activation.
- `native/probes/arena.c`, `fiber.c`, and `dinput8_proxy.c` have candidates in
  `../tooling/`; their Win32/x86 assumptions still need qualification.
- `native/simgame/simgame.c` and `hist.c` have candidates in `../simgame/` and
  this directory; they combine game simulation, harness control, file I/O, and
  state-engine callbacks.
- `native/state/kst_arena.c`, `kst_capture.c`, `kst_ctx.c`, `kst_hash.c`,
  `kst_restore.c`, and `kst_rt.c` are project-owned engine sources. They are
  tightly coupled through `kst.h` and `kst_int.h`, Win32 memory/state APIs,
  freestanding runtime helpers, and x86 assembly. Their complete candidate
  cluster is maintained in `../state-engine/` and shares its C ABI.

No generated source was selected. This inventory found no other tracked
project-authored `.c` or `.h` outside the shim and the sources listed above.
