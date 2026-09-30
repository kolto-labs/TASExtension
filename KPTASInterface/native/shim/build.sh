#!/bin/sh
# Build the M1 shim: build/dinput8.dll (x86-windows-gnu, freestanding, no CRT).
# Compile with the mingw headers, then link with -nostdlib so no CRT is pulled in.
set -e
Z="${Z:-$HOME/.local/share/kptas-zig/bin/python -m ziglang}"
HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="$HERE/build"
OBJ="$OUT/obj"
mkdir -p "$OBJ"
CFLAGS="-target x86-windows-gnu -O2 -g0 -std=gnu11 -ffreestanding -fno-builtin -mno-stack-arg-probe \
 -fno-stack-protector -fno-asynchronous-unwind-tables -Wall -Wextra -Wno-unused-parameter -Wno-cast-function-type"
OBJS=""
for f in rt alloc hash json patch clock threads dinput msg gate journal wire main; do
    $Z cc $CFLAGS -c "$HERE/src/$f.c" -o "$OBJ/$f.o"
    OBJS="$OBJS $OBJ/$f.o"
done
$Z cc -target x86-windows-gnu -nostdlib -shared -Wl,--entry=DllMain -Wl,--image-base=0x68000000 \
    $OBJS "$HERE/dinput8.def" -o "$OUT/dinput8.dll" -lkernel32 -luser32 -lws2_32
rm -f "$OUT/dinput8.lib" "$OUT/dinput8.pdb"
sha256sum "$OUT/dinput8.dll"
