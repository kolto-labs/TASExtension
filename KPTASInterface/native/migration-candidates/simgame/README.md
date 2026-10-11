# Inactive SimGame C++ candidate

This slice carries `native/simgame/simgame.c` into `simgame.cpp` and adds
`simgame.hpp` for its declaration boundary. The active SimGame files and build
script are unchanged. Object compilation passed for `x86-windows-gnu` with
Zig 0.16.0's C++20 driver; no link, tests, or runtime comparison was performed.

The candidate uses native C++ `struct` declarations, explicit casts for
pointer/value conversions, `nullptr` for optional pointer arguments, and
aggregate zero-initialization for Win32 and state-engine configuration records.
It retains C ABI declarations for `simgame_entry`, the state API, and `hist_*`;
`../state-engine/kst.hpp` and `../other/hist.hpp` provide the staged C ABI
headers. Its aligned raw-buffer word accesses use the same GNU-compatible
`may_alias` extension as the staged engine. The simulation and harness logic
otherwise follow the C source.

The deterministic algorithm, raw arrays, fixed-width C aliases, `memcpy` and
`memset` operations on byte-oriented state, Win32 calls, and low-level arena
address behavior are still largely mechanical carryover. These edits improve
C++ type/declaration clarity without claiming an idiomatic rewrite.

Before activation, still verify:

- C++-mode compilation with Zig/Clang. The active script uses `zig cc`,
  `-std=gnu11`, and compiles `hist.c` plus `simgame.c`; selecting these
  candidates requires a C++ driver and the existing `hist.cpp` candidate.
- Linking with the C engine under `-nostdlib`, including that the C++ compiler
  emits no exception, RTTI, static-constructor, or other C++ runtime helpers.
- The x86 `simgame_entry` symbol and `__cdecl` entry convention still match
  `--entry=simgame_entry`; the runner deliberately stores a worker argument
  through `uintptr_t` into its 32-bit seed.
- Win32 callback signatures, C ABI compatibility with `kst_*` and `hist_*`,
  and deterministic output compared with the active C executable.

This is an inactive candidate, not a validated replacement.
