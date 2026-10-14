"""Write the shim's math patch table (native/shim/src/math_sites.inc) from math_sites.toml.

Every site in math_sites.toml gets one row, and the table says how the shim
takes it over under portable.math = "reference":

  window  a game site. The shim copies a few whole instructions around the
          x87 opcode to a cave, with a call to the refmath stub in place of
          the opcode, and writes a jmp to the cave over them. A window holds
          no branch, call or return, and no address inside it (other than its
          first) is a jump target or appears as a 32-bit value anywhere in the
          image (jump tables, handler tables). Overlapping windows are merged.
  entry   a CRT routine game code calls (_CIacos, _CIasin, _CIpow): a jmp to
          the refmath _CI stub at its first instruction.
  trap    a CRT site. Once the entries are hooked nothing reaches it: every
          path back from it ends at a hooked entry or at code that no branch,
          call or 32-bit value in the image names. The opcode becomes ud2, so a
          path this analysis missed stops the game instead of running the
          CPU's x87 result.

The tool refuses (exit 1) when a game site has no safe window, when a CRT site
is reachable some other way, or when the entries game code calls are not the
three it knows stubs for. The shim checks every row's bytes before it patches
anything.

Input is `objdump -d -M intel` output for the exe, as for inventory_math_sites.py.

usage: gen_math_patches.py LISTING EXE [--sites math_sites.toml] > ../shim/src/math_sites.inc
"""
import argparse
import os
import struct
import sys
import tomllib
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from inventory_math_sites import CRT_START, read_listing  # noqa: E402

HERE = Path(__file__).resolve().parent
# CRT routines game code calls, and the refmath stub that replaces each. The
# _CI convention (operands on the x87 stack, result in st0) is the same for
# both; _CIpow's operand order was read at its call site 0x423dc0.
ENTRIES = {0x6FC0E0: ("_CIacos", "MS_CIACOS"), 0x6FC1F0: ("_CIpow", "MS_CIPOW"),
           0x6FCFF0: ("_CIasin", "MS_CIASIN")}
STUB = {"fsin": "MS_FSIN", "fcos": "MS_FCOS", "fptan": "MS_FPTAN", "fpatan": "MS_FPATAN",
        ("fyl2x", "ln"): "MS_FYL2X_LN", ("fyl2x", "lg"): "MS_FYL2X_LG", ("fyl2x", "l2"): "MS_FYL2X_L2",
        ("fyl2x", "generic"): "MS_FYL2X"}
OPCODE = {"fsin": b"\xd9\xfe", "fcos": b"\xd9\xff", "fptan": b"\xd9\xf2", "fpatan": b"\xd9\xf3",
          "fyl2x": b"\xd9\xf1"}
FLOW = ("j", "call", "loop", "ret", "int", "hlt", "iret")
NOFALL = ("jmp", "ret", "int3", "hlt", "iret")
PREFIX = {"rep", "repz", "repnz", "repe", "repne", "lock", "bnd", "notrack", "data16", "addr16",
          "cs", "ds", "es", "ss", "fs", "gs"}
MAXWIN = 24          # bytes the shim's row holds
MAXSITES = 3         # opcodes one window may hold


class Image:
    def __init__(self, path: str):
        self.data = open(path, "rb").read()
        d = self.data
        pe = struct.unpack_from("<I", d, 0x3C)[0]
        nsec, osz = struct.unpack_from("<H", d, pe + 6)[0], struct.unpack_from("<H", d, pe + 20)[0]
        self.base = struct.unpack_from("<I", d, pe + 24 + 28)[0]
        self.secs = []
        for i in range(nsec):
            name, vs, va, rs, rp = struct.unpack_from("<8sIIII", d, pe + 24 + osz + 40 * i)
            self.secs.append((name.rstrip(b"\0").decode(), self.base + va, vs, rp, rs))
        self.text = next(s for s in self.secs if s[0] == ".text")

    def read(self, va: int, n: int) -> bytes:
        for _name, sva, vs, rp, rs in self.secs:
            if sva <= va and va + n <= sva + min(vs, rs):
                return self.data[rp + va - sva:rp + va - sva + n]
        raise SystemExit(f"{va:#x}+{n} is not in the image")

    def dwords_into_text(self) -> set[int]:
        """Every 32-bit value, at any byte offset of any section, that points into .text."""
        lo, hi = self.text[1], self.text[1] + self.text[2]
        out = set()
        for _name, _va, _vs, rp, rs in self.secs:
            blob = self.data[rp:rp + rs]
            for o in range(len(blob) - 3):
                v = struct.unpack_from("<I", blob, o)[0]
                if lo <= v < hi:
                    out.add(v)
        return out


def is_flow(mn: str, ops: str) -> bool:
    """A control transfer, looking past prefixes objdump prints as the mnemonic (repz ret, bnd jmp)."""
    words = [mn.lower(), *ops.lower().split()]
    while len(words) > 1 and words[0] in PREFIX:
        words.pop(0)
    return words[0].startswith(FLOW)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("listing")
    ap.add_argument("exe")
    ap.add_argument("--sites", default=str(HERE.parent / "math_sites.toml"))
    a = ap.parse_args()

    img = Image(a.exe)
    ins, targets = read_listing(a.listing)
    pos = {addr: i for i, (addr, _mn, _ops) in enumerate(ins)}
    size = [ins[i + 1][0] - ins[i][0] if i + 1 < len(ins) else 0 for i in range(len(ins))]
    dwords = img.dwords_into_text()
    named = targets | dwords
    sites = tomllib.loads(Path(a.sites).read_text())["site"]
    err = []

    def stub_of(s):
        return STUB[(s["op"], s["fyl2x"])] if s["op"] == "fyl2x" else STUB[s["op"]]

    def check_opcode(s):
        got = img.read(s["address"], 2)
        if got != OPCODE[s["op"]]:
            err.append(f"{s['address']:#x}: bytes {got.hex()} are not {s['op']}")

    # ---- game sites: the smallest safe window around each opcode
    def window(i):
        best = None
        for start in range(i, max(i - 6, -1), -1):
            n, j, ok = 0, start, True
            while ok and (n < 5 or j <= i):
                addr, mn, ops = ins[j]
                if (j > start and addr in named) or (j != i and is_flow(mn, ops)) or not size[j]:
                    ok = False
                n += size[j]
                j += 1
            if ok and n <= MAXWIN and (best is None or n < best[1]):
                best = (ins[start][0], n)
        return best

    wins = []
    for s in sites:
        check_opcode(s)
        if s["kind"] != "game":
            continue
        w = window(pos[s["address"]])
        if w is None:
            err.append(f"{s['address']:#x} {s['op']}: no window of whole, branch-free instructions "
                       f"without an inner jump target")
            continue
        wins.append([w[0], w[1], [s]])
    wins.sort(key=lambda w: w[0])
    merged = []
    for w in wins:
        if merged and w[0] < merged[-1][0] + merged[-1][1]:
            m = merged[-1]
            end = max(m[0] + m[1], w[0] + w[1])
            m[1], m[2] = end - m[0], m[2] + w[2]
            inner = [ins[j][0] for j in range(pos[m[0]] + 1, pos[end] if end in pos else len(ins))]
            if any(x in named for x in inner) or m[1] > MAXWIN or len(m[2]) > MAXSITES:
                err.append(f"{m[0]:#x}: overlapping windows cannot be merged")
        else:
            merged.append(w)

    # ---- CRT sites: every path back must end at a hooked entry or at unnamed code
    preds: dict[int, list[tuple[int, str]]] = {}
    for i, (addr, mn, ops) in enumerate(ins):
        m = mn.lower()
        if m.startswith(("j", "call", "loop")):
            t = ops.split()[0] if ops else ""
            try:
                preds.setdefault(int(t, 16), []).append((addr, "call" if m.startswith("call") else "jump"))
            except ValueError:
                pass
        if i + 1 < len(ins) and not m.startswith(NOFALL):
            preds.setdefault(ins[i + 1][0], []).append((addr, "next"))

    def reach_back(site):
        """(external entries, problems) for the code that can reach site."""
        seen, todo, ext, bad = set(), [site], set(), []
        while todo:
            x = todo.pop()
            if x in seen:
                continue
            seen.add(x)
            if x in ENTRIES:
                ext.add(x)
                continue
            if x in dwords:
                bad.append(f"{x:#x} appears as a 32-bit value in the image")
            for src, how in preds.get(x, ()):
                if how == "call" and src < CRT_START:
                    bad.append(f"{x:#x} is called from game code at {src:#x}")
                else:
                    todo.append(src)
        return ext, bad

    traps, used_entries = [], set()
    for s in sites:
        if s["kind"] != "crt":
            continue
        ext, bad = reach_back(s["address"])
        used_entries |= ext
        if bad:
            err.append(f"{s['address']:#x} {s['op']} (crt): " + "; ".join(sorted(set(bad))[:4]))
        traps.append((s, sorted(ext)))
    if set(ENTRIES) - used_entries:
        err.append("hooked entries no CRT site needs: " + ", ".join(f"{x:#x}" for x in sorted(set(ENTRIES) - used_entries)))
    entry_rows = []
    for e, (name, stub) in sorted(ENTRIES.items()):
        callers = [src for src, how in preds.get(e, ()) if how == "call" and src < CRT_START]
        n, j = 0, pos[e]
        while n < 5:
            n += size[j]
            j += 1
        inner = [ins[k][0] for k in range(pos[e] + 1, j) if ins[k][0] < e + 5]
        if any(x in named for x in inner):
            err.append(f"{e:#x} {name}: a jump target lies inside its first 5 bytes")
        entry_rows.append((e, n, name, stub, len(callers)))

    if err:
        for e in err:
            print(f"gen_math_patches: {e}", file=sys.stderr)
        return 1

    out = sys.stdout
    out.write("/* Math patch table for K1 GOG 1.03 (portable.math = \"reference\").\n"
              " * Generated by native/portable/tools/gen_math_patches.py from\n"
              " * native/portable/math_sites.toml and an objdump listing of the exe; do not\n"
              " * edit. See that tool for what window, entry and trap rows mean.\n"
              " * Row: address, length, sites, opcode offsets, stubs, expected bytes. */\n")
    rows = []
    for at, n, ss in merged:
        ss = sorted(ss, key=lambda s: s["address"])
        offs = [s["address"] - at for s in ss]
        rows.append((at, n, offs, [stub_of(s) for s in ss],
                     " ".join(f"{s['address']:#010x} {s['op']}{'/' + s['fyl2x'] if s['op'] == 'fyl2x' else ''}"
                              f" {s['function_name']}" for s in ss)))
    for e, n, name, stub, ncall in entry_rows:
        rows.append((e, n, [0], [stub], f"{name}, {ncall} call sites in game code"))
    for s, ext in traps:
        why = ("behind " + ", ".join(ENTRIES[x][0] for x in ext)) if ext else "unreachable"
        rows.append((s["address"], 2, [0], ["MS_TRAP"],
                     f"{s['address']:#010x} {s['op']} {s['function_name']}: {why}"))
    rows.sort(key=lambda r: r[0])
    out.write("static const math_patch_t MATH_PATCHES[] = {\n")
    for at, n, offs, stubs, note in rows:
        b = img.read(at, n)
        out.write(f"    /* {note} */\n")
        out.write(f"    {{0x{at:08x}, {n}, {len(offs)}, {{{', '.join(map(str, offs))}}}, {{{', '.join(stubs)}}},\n")
        out.write(f"     {{{', '.join(f'0x{x:02x}' for x in b)}}}}},\n")
    out.write("};\n")
    nsites = sum(len(r[2]) for r in rows if r[3][0] not in ("MS_CIACOS", "MS_CIPOW", "MS_CIASIN"))
    out.write(f"#define MATH_NSITES {nsites}\n")
    print(f"{len(rows)} rows: {len(merged)} windows ({sum(len(w[2]) for w in merged)} sites), "
          f"{len(entry_rows)} entries, {len(traps)} traps", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
