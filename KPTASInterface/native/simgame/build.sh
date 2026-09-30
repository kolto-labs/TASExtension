#!/bin/sh
# Build SimGame.exe (x86-windows-gnu, freestanding, no CRT) with the kst engine
# from ../state linked in statically. Output: build/SimGame.exe
set -e
Z="${Z:-$HOME/.local/share/kptas-zig/bin/python -m ziglang}"
HERE="$(cd "$(dirname "$0")" && pwd)"
STATE="$HERE/../state"
OUT="$HERE/build"
OBJ="$OUT/obj"
mkdir -p "$OBJ"
CFLAGS="-target x86-windows-gnu -O2 -g0 -std=gnu11 -ffreestanding -fno-builtin -msse2 -mno-stack-arg-probe \
 -fno-stack-protector -fno-asynchronous-unwind-tables -Wall -Wextra -Wno-unused-parameter -Wno-cast-function-type"
OBJS=""
for f in kst_rt kst_hash kst_arena kst_capture kst_ctx kst_restore; do
    $Z cc $CFLAGS -c "$STATE/$f.c" -o "$OBJ/$f.o"
    OBJS="$OBJS $OBJ/$f.o"
done
for f in hist simgame; do
    $Z cc $CFLAGS -c "$HERE/$f.c" -o "$OBJ/$f.o"
    OBJS="$OBJS $OBJ/$f.o"
done
$Z cc -target x86-windows-gnu -nostdlib -Wl,--entry=simgame_entry -Wl,--subsystem,console \
    $OBJS -o "$OUT/SimGame.exe" -lkernel32
rm -f "$OUT/SimGame.pdb" "$OUT/SimGame.lib"
sha256sum "$OUT/SimGame.exe"
