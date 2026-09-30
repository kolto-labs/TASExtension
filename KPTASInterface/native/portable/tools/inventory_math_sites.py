"""List the x87 transcendental instructions in a disassembly, one TOML row each.

Input is `objdump -d -M intel` output for the exe. An optional kotorxid sqlite
file adds the containing function's name. The output is an inventory only;
the shim checks each address itself before it redirects anything. Rows have
the fields of the committed math_sites.toml, which came from a Ghidra listing
instead (see its header).

Each fyl2x row also gets `fyl2x`: ln, lg or l2 when the instructions on the
site's one straight-line path load st1 with fldln2, fldlg2 or fld1, else
generic. The x87 stack is followed backwards through fld, fxch and pops, so
`fldln2; fld x; fyl2x` is ln and `fld x; fldln2; fyl2x` is not.

usage: inventory_math_sites.py LISTING [--db KOTORXID.sqlite --binary-id N] > math_sites.toml
"""
import argparse
import bisect
import re
import sqlite3
import sys

OPS = ("fsin", "fcos", "fptan", "fpatan", "fyl2x", "fsincos")
CRT_START = 0x6FB000  # statically linked CRT; its routines are listed but marked crt
RAW = re.compile(r"(?:[0-9a-f]{2} ?)+\s*")

CONST = {"fldln2": "ln", "fldlg2": "lg", "fld1": "l2"}
PUSH = {"fld", "fild", "fbld", "fld1", "fldz", "fldpi", "fldl2e", "fldl2t", "fldln2", "fldlg2"}
POP = {"fstp": 1, "fistp": 1, "fisttp": 1, "fbstp": 1, "fcomp": 1, "ficomp": 1, "fucomp": 1,
       "fcompp": 2, "fucompp": 2}
KEEP = {"wait", "fwait", "fnstsw", "fstsw", "fnstcw", "fstcw", "fldcw", "fnclex", "fclex", "ftst", "fxam",
        "fcom", "ficom", "fucom", "fnop"}
ST0_ONLY = {"fadd", "fsub", "fsubr", "fmul", "fdiv", "fdivr", "fiadd", "fisub", "fisubr", "fimul", "fidiv",
            "fidivr", "fchs", "fabs", "fsqrt", "frndint", "fsin", "fcos", "f2xm1", "fscale", "fprem", "fprem1"}
STOP = ("jmp", "ret", "call", "iret", "int3")


def st_regs(ops: str) -> list[int]:
    return [int(n) if n else 0 for n in re.findall(r"\bst\(?([0-7])?\)?(?![a-z])", ops.lower())]


def fyl2x_variant(prev: list[tuple[int, str, str]]) -> str:
    """prev: the (address, mnemonic, operands) that reach the fyl2x, nearest first, on its one path."""
    k = 1                                    # fyl2x reads st1; follow that slot backwards
    for _a, mn, ops in prev:
        mn, regs = mn.lower(), st_regs(ops)
        if not mn.startswith("f") and mn != "wait":
            continue                         # integer code leaves the x87 stack alone
        if mn in KEEP or (mn in ("fst", "fist") and not regs):
            continue
        if mn == "fxch":
            i = regs[-1] if regs else 1
            k = i if k == 0 else 0 if k == i else k
        elif mn in PUSH:
            if k:
                k -= 1
            elif mn == "fld" and regs:       # fld st(i) copies st(i) as it was before the push
                k = regs[0]
            else:
                return CONST.get(mn, "generic")
        elif mn in POP and not regs:
            k += POP[mn]
        elif mn in ST0_ONLY and not regs and k:
            continue
        else:
            return "generic"
    return "generic"


def path_before(ins: list[tuple[int, str, str]], i: int, targets: set[int], limit: int = 16
                ) -> list[tuple[int, str, str]]:
    """Instructions before ins[i], nearest first, while no other path can join (jump target, jmp, ret, call)."""
    out = []
    for j in range(i - 1, max(i - 1 - limit, -1), -1):
        if ins[j + 1][0] in targets or ins[j][1].lower().startswith(STOP):
            break
        out.append(ins[j])
    return out


def read_listing(path: str) -> tuple[list[tuple[int, str, str]], set[int]]:
    ins, targets = [], set()
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            parts = line.rstrip("\n").split("\t")
            if len(parts) < 2 or not re.fullmatch(r"\s*[0-9a-f]+:", parts[0]):
                continue
            if len(parts) == 2 and RAW.fullmatch(parts[1]):
                continue                     # wrapped raw bytes of a long instruction
            mn, _, ops = parts[-1].strip().partition(" ")
            ins.append((int(parts[0].strip()[:-1], 16), mn, ops.strip()))
            if mn.startswith(("j", "call", "loop")) and (m := re.match(r"(?:0x)?([0-9a-f]+)\b", ops.strip())):
                targets.add(int(m[1], 16))
    return ins, targets


def load_funcs(db_path: str, binary_id: int) -> tuple[list[int], list[tuple[int, int, str]]]:
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    rows = db.execute("select addr, size, name from func where binary_id = ? order by addr",
                      (binary_id,)).fetchall()
    return [r[0] for r in rows], rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("listing")
    ap.add_argument("--db")
    ap.add_argument("--binary-id", type=int, default=8)
    a = ap.parse_args()
    starts, funcs = load_funcs(a.db, a.binary_id) if a.db else ([], [])

    ins, targets = read_listing(a.listing)
    sites = [(i, addr, mn) for i, (addr, mn, _ops) in enumerate(ins) if mn in OPS]

    out = sys.stdout
    out.write("# x87 transcendental sites in the exe. Inventory only; generated by\n")
    out.write("# tools/inventory_math_sites.py from an objdump listing.\n\n")
    for i, addr, op in sites:
        name = ""
        f = bisect.bisect_right(starts, addr) - 1
        if f >= 0:
            fa, size, fname = funcs[f]
            if size is None or addr < fa + max(size, 1):
                name = fname
        out.write("[[site]]\n")
        out.write(f"address = 0x{addr:08x}\n")
        out.write(f'op = "{op}"\n')
        if op == "fyl2x":
            out.write(f'fyl2x = "{fyl2x_variant(path_before(ins, i, targets))}"\n')
        out.write(f'kind = "{"crt" if addr >= CRT_START else "game"}"\n')
        out.write(f'function_name = "{name or "-"}"\n')
        out.write("\n")
    by_kind: dict[str, int] = {}
    for _i, addr, _op in sites:
        k = "crt" if addr >= CRT_START else "game"
        by_kind[k] = by_kind.get(k, 0) + 1
    print(f"{len(sites)} sites: {by_kind}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
