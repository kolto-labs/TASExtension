#!/bin/sh
# Dormant probe source build recipe (i386 PE); do not execute for this migration.
set -e
Z="${Z:-$HOME/.local/share/kptas-zig/bin/python -m ziglang}"
HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="${1:-$HERE}"
mkdir -p "$OUT"
CPPFLAGS="-target x86-windows-gnu -O2 -std=c++17 -fno-exceptions -fno-rtti \
 -fno-threadsafe-statics -fno-use-cxa-atexit -Wglobal-constructors -Werror=global-constructors \
 -Wexit-time-destructors -Werror=exit-time-destructors"

echo "building arena.exe"
$Z c++ $CPPFLAGS "$HERE/arena.cpp" -o "$OUT/arena.exe" -lkernel32

echo "building fiber.exe"
$Z c++ $CPPFLAGS "$HERE/fiber.cpp" -o "$OUT/fiber.exe" -lkernel32

echo "building miles.exe"
$Z c++ $CPPFLAGS "$HERE/miles.cpp" -o "$OUT/miles.exe" -lkernel32

echo "building dinput8.dll"
$Z c++ $CPPFLAGS -shared \
    "$HERE/dinput8_proxy.cpp" "$HERE/dinput8.def" \
    -o "$OUT/dinput8.dll" -lkernel32 -luser32

echo "done: $OUT"
ls -la "$OUT"/arena.exe "$OUT"/fiber.exe "$OUT"/miles.exe "$OUT"/dinput8.dll
