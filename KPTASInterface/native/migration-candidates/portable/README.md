# Portable reference-math C++ candidates

These files are staged only. No active source list includes this directory;
the `.c` implementation and its build scripts remain authoritative until the
whole candidate group is selected and qualified together.

| Candidate | Source | Notes |
| --- | --- | --- |
| `refmath.hpp` | `native/portable/refmath.h` | Same public names and C ABI, expressed as a C++ header. |
| `refmath.cpp` | `native/portable/refmath.c` | Keeps the fdlibm/musl-derived arithmetic and x87 assembly behavior. |
| `refmath_pow.cpp` | `native/portable/refmath_pow.c` | Keeps the musl/Arm pow implementation and tables. |

The numerical kernels stay close to their sources. Representation reads and
writes use C++20 `std::bit_cast`, which preserves the exact object bits without
reading an inactive union member; no arithmetic expression or evaluation order
was changed. Other C++ changes are limited to the API header/linkage and ordering
the `exp_data` designators by declaration order as required by C++20 (each value
still initializes the same named field). The x87 code still requires the
existing GNU-style i386 assembler conventions, and candidate integration
requires a C++20-capable compiler with the project's existing x86 target
support.

`refmath.cpp` and `refmath_pow.cpp` include the candidate header, whose
`extern "C"` declarations preserve the function names expected by the shim
and assembly. The separate golden-vector utility remains at
`../tooling/golden.cpp`; when the portable group is integrated, that utility's
include should target this candidate header. The source files also require an
IEEE-754 binary64 `double`; compile-time checks reject other representations.

Both implementation units compile to objects with Zig 0.16.0's C++20 driver
for `x86-windows-gnu` and `x86_64-linux-musl`. No executable was linked and no
tests or runtime comparison were run; no active build input was changed. Both
implementations reject non-binary64 `double` targets and `-ffast-math`, which
can alter the NaN, signed-zero, and rounding behavior the reference kernels
preserve.
