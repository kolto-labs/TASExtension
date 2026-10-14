# Native C++17 migration receipt

The canonical source in `src/native` now contains 48 C++ implementation files and 10 private C++ headers. The shim, state engine, reference math, dormant SimGame and dormant probe/golden sources were converted. Build references select the new files. Public `state/kst.h` and `portable/refmath.h` retain their conventional C API names and guarded C linkage.

Root's canonical production DLL build passed with SHA-256 `4b3143ea887f8b1b5a8e5ae6e6bbefb5be1061555de7b74b9a9372d7c1c060c7`. Independent source reviews found no blocking migration issue. No unit, synthetic or filesystem tests, probes, dormant builds or golden runs were performed. Gameplay and full restore acceptance are separate; a fresh frame-precision Routine session uses a new recording pin. The old `0536214b` DLL and all recordings remain preserved.

## Implementation

Typed data/handle/procedure conversions replace ambiguous C conversions. Private constants/helpers use constexpr where applicable; aggregate construction uses C++ syntax. Native exports, public APIs, assembly-referenced names, calling conventions, raw record layouts, instruction offsets and allocation ownership remain unchanged. The implementation stays freestanding: no exceptions, RTTI, C++ runtime, standard-library heap, hidden static initialization or exit-time destructors. Compile flags reject global constructors. Independent compiler jobs run in bounded batches and all children finish before any failed batch refuses the link.

Reference-math representation access uses fixed-size builtin byte copies rather than inactive union reads. Its arithmetic, coefficients, evaluation order, MXCSR policy, algorithm version, assembly and license text are preserved in source. Pow aggregates follow their declaration order. Strict SSE2, no FMA contraction and no fast-math remain mandatory. This is not a new result-equivalence or speed claim.

Float/GUID representation reads and unaligned instruction stores use explicit byte copies. Runtime word-copy helpers use byte assignments without recursively calling themselves. Raw restored arena/descriptor views remain an inherited compatibility boundary; `-fno-strict-aliasing` prevents optimizer alias assumptions but is not a general proof of C++ object lifetime or alignment.

## Graphics coverage repair

KPGC context descriptors now use version 6. They capture and restore all 16 per-unit combiner parameters, inactive combiner state and floating texture LOD bias where the real desktop driver supports those groups. Capability mismatch and older descriptors are refused; context readback is rehashed. FRONT, BACK, depth and stencil remain in full-root verification. There are no added swaps or relaxed canaries. Missing coverage was verified in source; it has not been proved to be the sole cause of the old boundary-350 highlight divergence.

## Recoverability

The original C source and build recipes are archived at `migration/native-c-source-before-cpp-20261003.tar.xz`, SHA-256 `214a726bcdc893fe62c1b7258dcdd2bc800e3543c93a5eda371c7afa711c6d00`. `migration/native-cpp-integration.json` records the exact copied and retired paths. The isolated candidate remains source provenance, not another runtime/controller or execution board. Project requirements and acceptance remain in the canonical product plan.
