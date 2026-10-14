#!/bin/sh
# Build the portable reference math and its golden programs.
#
#   refmath.o, refmath_pow.o   i386 COFF objects for linking into the shim
#   golden.exe                 i386 PE console program (run under Wine)
#   golden-x86_64              static x86_64-linux build of the same source
#
# Usage: build.sh [outdir]   (default $HOME/.cache/kptas-portable)
set -e
Z="${Z:-$HOME/.local/share/kptas-zig/bin/python -m ziglang}"
HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="${1:-$HOME/.cache/kptas-portable}"
mkdir -p "$OUT"

# No -ffast-math, no FMA contraction, no libm builtins, no x87 math.
MATH="-O2 -ffp-contract=off -fno-fast-math -ffreestanding -fno-builtin \
 -fno-stack-protector -fno-unwind-tables -fno-asynchronous-unwind-tables -Wall -Wextra"
CXX="-x c++ -std=c++17 -fno-exceptions -fno-rtti -fno-threadsafe-statics"
I386="-target x86-windows-gnu -march=pentium4 -msse2 -mfpmath=sse"
X64="-target x86_64-linux-musl -march=x86_64"

$Z cc $I386 $CXX $MATH -c "$HERE/refmath.cpp" -o "$OUT/refmath.o"
$Z cc $I386 $CXX $MATH -c "$HERE/refmath_pow.cpp" -o "$OUT/refmath_pow.o"
$Z cc $I386 $CXX $MATH -c "$HERE/golden/golden.cpp" -o "$OUT/golden.o"
$Z cc $I386 "$OUT/golden.o" "$OUT/refmath.o" "$OUT/refmath_pow.o" -o "$OUT/golden.exe"

$Z cc $X64 $CXX $MATH -c "$HERE/refmath.cpp" -o "$OUT/refmath-x86_64.o"
$Z cc $X64 $CXX $MATH -c "$HERE/refmath_pow.cpp" -o "$OUT/refmath_pow-x86_64.o"
$Z cc $X64 $CXX $MATH -c "$HERE/golden/golden.cpp" -o "$OUT/golden-x86_64.o"
$Z cc $X64 -static "$OUT/golden-x86_64.o" \
    "$OUT/refmath-x86_64.o" "$OUT/refmath_pow-x86_64.o" -o "$OUT/golden-x86_64"

ls -la "$OUT"/refmath.o "$OUT"/refmath_pow.o "$OUT"/golden.exe "$OUT"/golden-x86_64
