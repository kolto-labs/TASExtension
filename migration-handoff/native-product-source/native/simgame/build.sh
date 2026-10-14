#!/bin/sh
# Dormant source build recipe; this migration does not authorize execution.
# Build SimGame.exe (x86-windows-gnu, freestanding, no CRT) with the kst engine
# from ../state linked in statically. Output: build/SimGame.exe
set -e
Z="${Z:-$HOME/.local/share/kptas-zig/bin/python -m ziglang}"
HERE="$(cd "$(dirname "$0")" && pwd)"
STATE="$HERE/../state"
OUT="$HERE/build"
OBJ="$OUT/obj"
mkdir -p "$OBJ"
CPPFLAGS="-target x86-windows-gnu -O2 -g0 -std=c++17 -ffreestanding -fno-builtin -msse2 -mno-stack-arg-probe \
 -fno-stack-protector -fno-asynchronous-unwind-tables -fno-unwind-tables -fno-exceptions -fno-rtti \
 -fno-threadsafe-statics -fno-use-cxa-atexit -Wall -Wextra \
 -Wglobal-constructors -Werror=global-constructors -Wexit-time-destructors -Werror=exit-time-destructors"
OBJS=""
for f in kst_rt kst_hash kst_arena kst_capture kst_ctx kst_restore; do
    $Z c++ $CPPFLAGS -c "$STATE/$f.cpp" -o "$OBJ/$f.o"
    OBJS="$OBJS $OBJ/$f.o"
done
for f in hist simgame; do
    $Z c++ $CPPFLAGS -c "$HERE/$f.cpp" -o "$OBJ/$f.o"
    OBJS="$OBJS $OBJ/$f.o"
done
$Z cc -target x86-windows-gnu -nostdlib -Wl,--entry=simgame_entry -Wl,--subsystem,console \
    $OBJS -o "$OUT/SimGame.exe" -lkernel32
rm -f "$OUT/SimGame.pdb" "$OUT/SimGame.lib"
sha256sum "$OUT/SimGame.exe"
