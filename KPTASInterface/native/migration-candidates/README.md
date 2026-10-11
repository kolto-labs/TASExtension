# Dormant C++ migration candidates

These directories hold C++ translations prepared alongside the active C
implementation. Nothing in this tree is included by the active shim or harness
builds. The original `.c` and `.h` files remain the build inputs until a single
integration change selects the complete candidate set.

## Current candidate groups

| Candidate directory | Source area | Translation units | Status |
| --- | --- | ---: | --- |
| `shim/` | `native/shim/src/` | 18 | C++20 x86-Windows compile and staged DLL link pass; runtime/ABI behavior remains unqualified |
| `state-engine/` | `native/state/` | 6 | C++20 x86-Windows compile and staged DLL link pass; capture/restore behavior remains unqualified |
| `other/` | `native/simgame/hist.c` | 1 | x86 Windows C++20 object compile passed; link, C ABI, and runtime behavior remain unqualified |
| `simgame/` | `native/simgame/simgame.c` | 1 | x86 Windows C++20 object compile passed; link with the staged state engine and runtime behavior remain unqualified |
| `tooling/` | native probes and golden-vector utility | 4 | x86 Windows C++20 objects passed; golden also passed x86_64 Linux-musl; exports, links, and runtime remain unqualified |
| `portable/` | portable reference math | 2 | C++20 objects passed for i386 Windows and x86_64 Linux-musl; numerical equivalence remains unqualified |

Inventory on 2026-10-09: all 32 active native `.c` files map one-to-one to 32
staged `.cpp` files; no source is omitted or duplicated. The groups map as
follows: shim 18/18; state engine 6/6; probes 3/3; SimGame history and runner
2/2 (split across `other/` and `simgame/`); portable math and its golden utility
3/3 (`refmath*` in `portable/`, `golden.c` in `tooling/`). All five active `.h`
interfaces have staged C++ header counterparts; `simgame/simgame.hpp` is an
additional candidate boundary header, with no active `simgame.h` counterpart.
The reference-math candidates retain the upstream-derived notices. Each group
has a local README with its source mapping and qualification state. The staged
SimGame, history, and shim headers reference the staged state-engine headers.

The staged `tooling/golden.cpp` now includes the staged
`portable/refmath.hpp`; no candidate translation unit needs an active C header
for that dependency. The original C files and build definitions remain in
place as the source of truth until a coordinated promotion.

The shim plus state-engine candidate closure compiled and linked as a
freestanding x86 Windows DLL using Zig 0.16.0's C++20 driver, with output under
`/tmp/kptas-candidate-cxx20-e7idrbri/`. Its import table contains only KERNEL32,
USER32, and WS2_32; the 24 candidate objects had no unresolved C++ runtime
symbols. The export definition file was read from the active tree but not
modified. All other groups have object-only compile receipts, not link receipts.
No compile/link receipt establishes runtime behavior, deterministic
equivalence, memory-layout equivalence, or readiness to replace the active C
build.

The portable-math candidates specifically require C++20 for `std::bit_cast`,
an IEEE-754 binary64 `double`, and compilation without `-ffast-math`; they remain
inactive alongside every other candidate until the complete group is qualified
and selected together.

## Staging and promotion

The candidate tree is deliberately outside every active source list, so work
here does not partially convert or disrupt the live C integration. Continue
writing and reviewing the C++ candidates in this tree. Promote them only as a
coordinated source-and-build change after ABI, layout, numerical, export, and
runtime qualification; do not move or select a subset in the active build.

## Integration rule

Keep all candidates inert during active runtime qualification. When migration
is scheduled, freeze the corresponding C sources, configure the compiler and
ABI explicitly, select the whole dependency group in the build, and qualify
exports, structure layout, determinism, and runtime behavior before removing
the C inputs. Do not copy files into active source directories piecemeal.
