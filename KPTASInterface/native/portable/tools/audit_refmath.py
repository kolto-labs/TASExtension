#!/usr/bin/env python3
"""Audit the i386 COFF objects of refmath for CPU-dependent instructions.

Pass: every code byte decodes; the only x87 instructions are exact data
moves (fld/fstp of a double or a stack register, fld1, fldz, fchs, fabs,
fxch); no x87 arithmetic or transcendental opcode, no FMA, no AVX, no
rcp*/rsqrt* approximations, no cpuid/rdtsc; the only undefined external
symbols are refmath's own.

Needs capstone (dev tool only). Set CAPSTONE_PATH to a directory holding the
capstone package if it is not importable.

usage: audit_refmath.py refmath.o refmath_pow.o
"""
import os
import struct
import sys

if os.environ.get("CAPSTONE_PATH"):
    sys.path.insert(0, os.environ["CAPSTONE_PATH"])
import capstone  # noqa: E402

X87_EXACT = {"fld", "fstp", "fst", "fld1", "fldz", "fchs", "fabs", "fxch"}
BANNED = {"cpuid", "rdtsc", "rdtscp", "rcpss", "rcpps", "rsqrtss", "rsqrtps"}


def coff(path):
    b = open(path, "rb").read()
    machine, nsec, _, symptr, nsym, optsz, _ = struct.unpack_from("<HHIIIHH", b, 0)
    if machine != 0x14C:
        raise SystemExit(f"{path}: not an i386 COFF object (machine {machine:#x})")
    strtab = symptr + 18 * nsym
    def name(raw):
        if raw[:4] == b"\0\0\0\0":
            off = struct.unpack_from("<I", raw, 4)[0]
            return b[strtab + off:b.index(b"\0", strtab + off)].decode()
        return raw.rstrip(b"\0").decode()
    secs = []
    for i in range(nsec):
        o = 20 + optsz + 40 * i
        nm = b[o:o + 8].rstrip(b"\0").decode()
        if nm.startswith("/"):
            off = int(nm[1:])
            nm = b[strtab + off:b.index(b"\0", strtab + off)].decode()
        size, ptr, flags = struct.unpack_from("<I", b, o + 16)[0], struct.unpack_from("<I", b, o + 20)[0], struct.unpack_from("<I", b, o + 36)[0]
        secs.append((nm, b[ptr:ptr + size] if ptr else b"", flags))
    syms, i = [], 0
    while i < nsym:
        o = symptr + 18 * i
        raw = b[o:o + 8]
        value, secno, typ, cls, naux = struct.unpack_from("<IhHBB", b, o + 8)
        syms.append((name(raw), value, secno, typ, cls))
        i += 1 + naux
    return secs, syms


def audit(path):
    secs, syms = coff(path)
    md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)
    bad, x87, count = [], {}, 0
    for si, (nm, data, flags) in enumerate(secs, 1):
        if not flags & 0x20:          # IMAGE_SCN_CNT_CODE
            continue
        labels = sorted((v, n) for n, v, s, t, c in syms if s == si and c in (2, 3) and not n.startswith("."))
        def where(a):
            best = "?"
            for v, n in labels:
                if v <= a:
                    best = n
            return best
        end = 0
        for ins in md.disasm(data, 0):
            count += 1
            end = ins.address + ins.size
            m = ins.mnemonic
            text = f"{m} {ins.op_str}".strip()
            if m in BANNED or m.startswith("v") or "fma" in m:
                bad.append((where(ins.address), ins.address, text))
            elif m.startswith("f") and m not in ("fnstsw",):
                if m not in X87_EXACT or ("ptr" in ins.op_str and "qword ptr" not in ins.op_str):
                    bad.append((where(ins.address), ins.address, text))
                else:
                    x87.setdefault(text, set()).add(where(ins.address))
        if end != len(data):
            # trailing bytes that do not decode (padding is fine only if int3/nop)
            tail = data[end:]
            if tail.strip(b"\xcc\x90"):
                bad.append((nm, end, f"undecodable tail {tail[:16].hex()}"))
    undefined = sorted({n for n, v, s, t, c in syms if s == 0 and c == 2 and v == 0})
    foreign = [n for n in undefined if not n.startswith("_refmath_")]
    print(f"{os.path.basename(path)}: {count} instructions")
    for text, where_ in sorted(x87.items()):
        print(f"  x87 allowed: {text:32s} in {', '.join(sorted(where_))}")
    print(f"  undefined externals: {', '.join(undefined) or 'none'}")
    for w, a, t in bad:
        print(f"  REJECT {w}+{a:#x}: {t}")
    if foreign:
        print(f"  REJECT foreign externals: {', '.join(foreign)}")
    return not bad and not foreign


def main(argv):
    ok = all([audit(p) for p in argv[1:]])
    print("audit:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
