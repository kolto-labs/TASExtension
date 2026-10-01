#!/bin/sh
# Build the shim: build/dinput8.dll (x86-windows-gnu, freestanding, no CRT),
# with the kst savestate engine (../state) linked in statically.
# Compile with the mingw headers, then link with -nostdlib so no CRT is pulled in.
# The link stamps a time and a random CodeView id into the PE; both are zeroed
# after the link, so the DLL's sha256 (the build id hello reports) depends only
# on the sources.
set -e
Z="${Z:-$HOME/.local/share/kptas-zig/bin/python -m ziglang}"
PY="${PY:-$HOME/.local/share/kptas-zig/bin/python}"
HERE="$(cd "$(dirname "$0")" && pwd)"
STATE="$HERE/../state"
OUT="$HERE/build"
OBJ="$OUT/obj"
mkdir -p "$OBJ"
CFLAGS="-target x86-windows-gnu -O2 -g0 -std=gnu11 -ffreestanding -fno-builtin -msse2 -mno-stack-arg-probe \
 -fno-stack-protector -fno-asynchronous-unwind-tables -Wall -Wextra -Wno-unused-parameter -Wno-cast-function-type"
OBJS=""
for f in kst_rt kst_hash kst_arena kst_capture kst_ctx kst_restore; do
    $Z cc $CFLAGS -DKST_NO_CRT_HELPERS -c "$STATE/$f.c" -o "$OBJ/$f.o"
    OBJS="$OBJS $OBJ/$f.o"
done
for f in rt alloc hash json patch clock threads vmem vhandle gl state dinput msg geo gate journal wire main; do
    $Z cc $CFLAGS -c "$HERE/src/$f.c" -o "$OBJ/$f.o"
    OBJS="$OBJS $OBJ/$f.o"
done
$Z cc -target x86-windows-gnu -nostdlib -shared -Wl,--entry=DllMain -Wl,--image-base=0x68000000 \
    $OBJS "$HERE/dinput8.def" -o "$OUT/dinput8.dll" -lkernel32 -luser32 -lws2_32
rm -f "$OUT/dinput8.lib" "$OUT/dinput8.pdb"
"$PY" - "$OUT/dinput8.dll" <<'EOF'
import struct, sys
p = sys.argv[1]
d = bytearray(open(p, "rb").read())
pe = struct.unpack_from("<I", d, 0x3c)[0]
nsec, osz = struct.unpack_from("<H", d, pe + 6)[0], struct.unpack_from("<H", d, pe + 20)[0]
opt = pe + 24
secs = [struct.unpack_from("<IIII", d, opt + osz + i * 40 + 8) for i in range(nsec)]
def off(rva):
    for vs, va, rs, ra in secs:
        if va <= rva < va + max(vs, rs):
            return rva - va + ra
    raise SystemExit(f"rva {rva:#x} outside the sections")
struct.pack_into("<I", d, pe + 8, 0)                       # COFF TimeDateStamp
exp = struct.unpack_from("<I", d, opt + 96)[0]
if exp:
    struct.pack_into("<I", d, off(exp) + 4, 0)             # export directory TimeDateStamp
dbg, dbg_size = struct.unpack_from("<II", d, opt + 96 + 6 * 8)
if dbg:
    o = off(dbg)
    for i in range(dbg_size // 28):
        e = o + i * 28
        struct.pack_into("<I", d, e + 4, 0)                # debug entry TimeDateStamp
        kind, size, rva, ptr = struct.unpack_from("<IIII", d, e + 12)
        if kind == 2 and d[ptr:ptr + 4] == b"RSDS":
            d[ptr + 4:ptr + 20] = bytes(16)                # CodeView id
open(p, "wb").write(d)
EOF
sha256sum "$OUT/dinput8.dll"
