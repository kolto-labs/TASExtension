#!/bin/sh
# Build the shim: build/dinput8.dll (x86-windows-gnu, freestanding, no CRT),
# with the kst savestate engine (../state) and refmath (../portable) linked in statically.
# Compile the isolated C++17 candidate with MinGW headers.
# Link with the C driver and -nostdlib; no C++ runtime or CRT is linked.
# Root alone may run this production DLL build; it never executes a probe.
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
COMMON_FLAGS="-target x86-windows-gnu -O2 -g0 -ffreestanding -fno-builtin -fno-strict-aliasing -msse2 -mno-stack-arg-probe \
 -fno-stack-protector -fno-asynchronous-unwind-tables -Wall -Wextra -Wno-unused-parameter -Wno-cast-function-type"
# The restore arena contains raw captured object representations; preserve its
# established aliasing boundary instead of optimizing through typed snapshot views.
CPPFLAGS="$COMMON_FLAGS -std=c++17 -fno-exceptions -fno-rtti -fno-threadsafe-statics \
 -fno-use-cxa-atexit -fno-unwind-tables -Wglobal-constructors -Werror=global-constructors \
 -Wexit-time-destructors -Werror=exit-time-destructors"
# Compile independent translation units in bounded batches. Drain every child
# on a failure before refusing the link; never link stale objects after a failed compile.
KPTAS_BUILD_JOBS="${KPTAS_BUILD_JOBS:-2}"
case "$KPTAS_BUILD_JOBS" in
    ''|*[!0-9]*|0) printf '%s\n' 'KPTAS_BUILD_JOBS must be a positive integer' >&2; exit 2 ;;
esac
KPTAS_BUILD_PIDS=""
KPTAS_BUILD_COUNT=0
wait_compilers() {
    KPTAS_BUILD_FAILED=0
    for KPTAS_BUILD_PID in $KPTAS_BUILD_PIDS; do
        if ! wait "$KPTAS_BUILD_PID"; then KPTAS_BUILD_FAILED=1; fi
    done
    KPTAS_BUILD_PIDS=""
    KPTAS_BUILD_COUNT=0
    return "$KPTAS_BUILD_FAILED"
}
queue_compile() {
    "$@" &
    KPTAS_BUILD_PIDS="$KPTAS_BUILD_PIDS $!"
    KPTAS_BUILD_COUNT=$((KPTAS_BUILD_COUNT + 1))
    if [ "$KPTAS_BUILD_COUNT" -ge "$KPTAS_BUILD_JOBS" ]; then wait_compilers; fi
}
OBJS=""
for f in kst_rt kst_hash kst_arena kst_capture kst_ctx kst_restore; do
    queue_compile $Z c++ $CPPFLAGS -DKST_NO_CRT_HELPERS -c "$STATE/$f.cpp" -o "$OBJ/$f.o"
    OBJS="$OBJS $OBJ/$f.o"
done
# Reference math retains strict SSE2 arithmetic and uses explicit representation copies.
# Historical golden coverage is unchanged; no golden program is run by this build.
for f in refmath refmath_pow; do
    queue_compile $Z c++ $CPPFLAGS -march=pentium4 -mfpmath=sse -ffp-contract=off -fno-fast-math \
        -c "$HERE/../portable/$f.cpp" -o "$OBJ/$f.o"
    OBJS="$OBJS $OBJ/$f.o"
done
for f in rt alloc hash json patch diag xtrace btrace glenv clock threads vmem vhandle gl glbuffers gldraw glati gllists glsync hud frames render state dinput physical_input msg geo gate journal audio math wire bound winhandles main; do
    queue_compile $Z c++ $CPPFLAGS -c "$HERE/src/$f.cpp" -o "$OBJ/$f.o"
    OBJS="$OBJS $OBJ/$f.o"
done
wait_compilers
# GL boundary fast path (glenv.cpp): the thunk fxsaves the game's FP state only
# after the fast C halves return, so they and everything they call must not
# touch x87, MMX or MXCSR state, or do SSE arithmetic. XMM moves and logic are
# allowed (the thunk reloads XMM0-7). Refuse the link if the compiler emitted
# anything else, or a call outside the allowed set (the ge_die exit path).
"$PY" - "$OBJ/glenv.o" "$OBJ/math.o" <<'EOF'
import re, shutil, subprocess, sys
tool = next((t for t in (shutil.which("objdump"), "/home/linuxbrew/.linuxbrew/bin/objdump",
                         shutil.which("x86_64-w64-mingw32-objdump")) if t and shutil.os.path.exists(t)), None)
cmd = [tool] if tool else (["host-spawn", "objdump"] if shutil.which("host-spawn") else None)
if not cmd: sys.exit("fast-path check: no objdump (container, linuxbrew, mingw or host-spawn)")
funcs, cur = {}, None
for o in sys.argv[1:]:
    out = subprocess.run(cmd + ["-b", "pe-i386", "-m", "i386", "-dr", "--no-show-raw-insn", o],
                         check=True, capture_output=True, text=True).stdout
    for line in out.splitlines():
        m = re.match(r"^[0-9a-f]+ <(\S+)>:$", line)
        if m: cur = funcs.setdefault(m.group(1), []); continue
        m = re.match(r"^\s+[0-9a-f]+:\s+(?:DISP32|dir32)\s+(\S+)$", line)
        if m:
            if cur: cur[-1][1] = m.group(1)
            continue
        m = re.match(r"^\s+[0-9a-f]+:\s+(\S.*)$", line)
        if m and cur is not None: cur.append([m.group(1), None])
roots = ["_glenv_enter_fast_c", "_glenv_return_fast_c", "_math_scope_fast_slot", "_math_scope_link", "_math_scope_unlink"]
roots += [f for f in funcs if "ge_thread" in f and "ge_threads" not in f]
external = {"_slog", "__imp__GetCurrentProcess@0", "__imp__TerminateProcess@8"}
prefixes = {"lock", "rep", "repz", "repe", "repnz", "repne", "data16", "addr16", "cs", "ds", "es", "fs", "gs", "ss", "notrack", "bnd"}
xmm_ok = re.compile(r"^(mov(aps|ups|apd|upd|dqa|dqu|q|d|ss|sd|hps|lps|hpd|lpd|hlps|lhps)|(xor|and|andn|or)p[sd]|pxor|pand|pandn|por|p?unpck\w+|pshuf\w*|shufp[sd])$")
bad, seen, work = [], set(), list(roots)
for r in roots:
    if r not in funcs: bad.append(f"{r}: not found")
while work:
    fn = work.pop()
    if fn in seen or fn not in funcs: continue
    seen.add(fn)
    for text, reloc in funcs[fn]:
        toks = text.split()
        while toks and toks[0] in prefixes: toks = toks[1:]
        if not toks: continue
        op, args = toks[0], " ".join(toks[1:])
        if (op.startswith("f") or op in ("emms", "femms") or "mxcsr" in op or "%st" in args
                or re.search(r"(?<!x)%mm\d", args) or ("%xmm" in args and not xmm_ok.match(op))):
            bad.append(f"{fn}: {text}")
        if op.startswith("call") or (op.startswith("j") and (reloc or "*" in args)):
            m = re.search(r"<([^>+]+)", args)
            target = reloc or (m.group(1) if m else None)
            if target in funcs and target != fn: work.append(target)
            elif target != fn and target not in external: bad.append(f"{fn}: call outside the allowed set: {text} {reloc or ''}")
if bad: sys.exit("GL boundary fast path is not integer-only:\n  " + "\n  ".join(bad))
EOF
$Z cc -target x86-windows-gnu -nostdlib -shared -Wl,--entry=DllMain -Wl,--image-base=0x10000000 \
    $OBJS "$HERE/dinput8.def" -o "$OUT/dinput8.dll" -lkernel32 -luser32 -lgdi32 -lws2_32
rm -f "$OUT/dinput8.lib" "$OUT/dinput8.pdb"
"$PY" - "$OUT/dinput8.dll" <<'EOF'
import struct, sys
p = sys.argv[1]
d = bytearray(open(p, "rb").read())
pe = struct.unpack_from("<I", d, 0x3c)[0]
nsec, osz = struct.unpack_from("<H", d, pe + 6)[0], struct.unpack_from("<H", d, pe + 20)[0]
opt = pe + 24
# Zig's MinGW link driver has no disable-dynamicbase switch. Set the PE flag
# directly: callback pointers and saved return addresses require the fixed image.
chars = struct.unpack_from("<H", d, opt + 70)[0]
struct.pack_into("<H", d, opt + 70, chars & ~0x40)
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
