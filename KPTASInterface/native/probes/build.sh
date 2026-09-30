#!/bin/sh
# Build the three probes with the ziglang cross compiler (i386 PE).
set -e
Z="${Z:-$HOME/.local/share/kptas-zig/bin/python -m ziglang}"
HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="${1:-$HERE}"
mkdir -p "$OUT"

echo "building arena.exe"
$Z cc -target x86-windows-gnu -O2 "$HERE/arena.c" -o "$OUT/arena.exe" -lkernel32

echo "building fiber.exe"
$Z cc -target x86-windows-gnu -O2 "$HERE/fiber.c" -o "$OUT/fiber.exe" -lkernel32

echo "building dinput8.dll"
$Z cc -target x86-windows-gnu -O2 -shared \
    "$HERE/dinput8_proxy.c" "$HERE/dinput8.def" \
    -o "$OUT/dinput8.dll" -lkernel32 -luser32

echo "done: $OUT"
ls -la "$OUT"/arena.exe "$OUT"/fiber.exe "$OUT"/dinput8.dll
