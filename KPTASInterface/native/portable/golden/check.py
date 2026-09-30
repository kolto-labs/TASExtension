#!/usr/bin/env python3
"""Check refmath golden output. Standard library only.

usage: check.py REF_A [REF_B] [--vanilla VANILLA]

REF_A, REF_B  output of `golden ref` from two builds (for example i386 under
              Wine and x86_64-linux). They must be byte-identical after CRLF
              is stripped. The sha256 of the normalized text is the refmath
              golden digest, and it must equal the one golden/digests.txt
              records for the REFMATH_VERSION in refmath.h.

For every result the script also reports
  - the ulp distance to the correctly rounded value, computed with decimal
    at 80+ digits (the real accuracy of refmath), and
  - the ulp distance to Python's math module (the host libm; a sanity bound,
    not a reference).

--vanilla     output of `golden vanilla` (raw x87 opcodes on this CPU). The
              script counts how often the vanilla value differs from refmath
              and from the correctly rounded value.
"""
import decimal
import hashlib
import math
import re
import struct
import sys
from decimal import Decimal
from pathlib import Path

HERE = Path(__file__).resolve().parent

D = decimal.Context(prec=80, Emax=999999, Emin=-999999)
decimal.setcontext(D)


def h2f(h):
    return struct.unpack(">d", bytes.fromhex(h))[0]


def f2i(x):
    return struct.unpack(">q", struct.pack(">d", x))[0]


def ordered(x):
    i = f2i(x)
    return i if i >= 0 else -(i & 0x7FFFFFFFFFFFFFFF) - 1


def ulps(a, b):
    """ulp distance between doubles; None when exactly one is nan."""
    if math.isnan(a) or math.isnan(b):
        return 0 if math.isnan(a) and math.isnan(b) else None
    return abs(ordered(a) - ordered(b))


# ------------------------------------------------------------ pi to 1200 digits

def _pi_digits(n):
    scale = 10 ** (n + 10)

    def arctan_inv(k):
        total, term, k2, i, sign = 0, scale // k, k * k, 1, 1
        while term:
            total += sign * (term // i)
            term //= k2
            i += 2
            sign = -sign
        return total

    return Decimal(16 * arctan_inv(5) - 4 * arctan_inv(239)).scaleb(-(n + 10), context=decimal.Context(prec=n + 20))


PI = _pi_digits(1200)


def dsin_cos(x):
    """sin(x), cos(x) of an exact Decimal x at 80 digits."""
    with decimal.localcontext(decimal.Context(prec=1100, Emax=999999, Emin=-999999)):
        half = PI / 2
        k = (x / half).to_integral_value(rounding=decimal.ROUND_HALF_EVEN)
        r = x - k * half
    q = int(k) % 4
    with decimal.localcontext(decimal.Context(prec=90, Emax=999999, Emin=-999999)):
        r = +r
        r2 = r * r
        s, t, n = r, r, 1
        while True:
            t = -t * r2 / ((2 * n) * (2 * n + 1))
            if t == 0 or abs(t) < abs(s) * Decimal("1e-88"):
                break
            s += t
            n += 1
        c, t, n = Decimal(1), Decimal(1), 1
        while True:
            t = -t * r2 / ((2 * n - 1) * (2 * n))
            if t == 0 or abs(t) < Decimal("1e-88"):
                break
            c += t
            n += 1
    return [(s, c), (c, -s), (-s, -c), (-c, s)][q]


def datan(x):
    with decimal.localcontext(decimal.Context(prec=95, Emax=999999, Emin=-999999)):
        sign = -1 if x < 0 else 1
        x = abs(x)
        if x == 0:
            return Decimal(0)
        base = Decimal(0)
        if x > 1:
            base, x, sign2 = PI / 2, 1 / x, -1
        else:
            sign2 = 1
        m = 0
        while x > Decimal("0.1"):
            x = x / (1 + (1 + x * x).sqrt())
            m += 1
        x2, s, t, n = x * x, x, x, 1
        while True:
            t = -t * x2
            term = t / (2 * n + 1)
            if abs(term) < abs(s) * Decimal("1e-93"):
                break
            s += term
            n += 1
        a = s * (2 ** m)
        return sign * (base + sign2 * a)


def cr(fn, a, b=None):
    """Correctly rounded double of fn at the double input(s), or None when the
    case is a special value left to the Python math comparison."""
    if not math.isfinite(a) or (b is not None and not math.isfinite(b)):
        return None
    x = Decimal(a)
    try:
        if fn in ("sin", "cos", "tan"):
            if a == 0:
                return None
            s, c = dsin_cos(x)
            v = {"sin": s, "cos": c, "tan": s / c if c else None}[fn]
        elif fn == "atan":
            if a == 0:
                return None
            v = datan(x)
        elif fn in ("asin", "acos"):
            if abs(a) > 1 or a == 0:
                return None
            if abs(a) == 1:
                v = PI / 2 * (1 if a > 0 else -1)
            else:
                with decimal.localcontext(decimal.Context(prec=95)):
                    v = datan(x / (1 - x * x).sqrt())
            if fn == "acos":
                v = PI / 2 - v
        elif fn == "atan2":
            y, xx = x, Decimal(b)
            if a == 0 or b == 0:
                return None
            v = datan(y / xx)
            if b < 0:
                v = v + PI if a > 0 else v - PI
        elif fn == "exp":
            v = x.exp()
        elif fn in ("log", "log10", "log2"):
            if a <= 0 or a == 1:
                return None
            v = {"log": x.ln(), "log10": x.log10(), "log2": x.ln() / Decimal(2).ln()}[fn]
        elif fn == "sqrt":
            if a < 0:
                return None
            v = x.sqrt()
        elif fn == "pow":
            if a == 0 or b == 0 or a == 1:
                return None
            y = Decimal(b)
            yint = y == y.to_integral_value()
            if a < 0 and not yint:
                return None
            v = (y * abs(x).ln()).exp()
            if a < 0 and yint and int(y) % 2 == 1:
                v = -v
        elif fn == "fmod":
            return math.fmod(a, b) if b != 0 else None
        elif fn in ("floor", "ceil"):
            fl = lambda t: t if t == int(t) else float(math.floor(t))
            return fl(a) if fn == "floor" else -fl(-a)
        else:
            return None
    except (decimal.Overflow, OverflowError):
        return None
    return float(v)


def pymath(fn, a, b=None):
    """Python math, with its exceptions mapped to the C99 values: log*(+-0) and
    pow(+-0, y<0) raise ValueError, overflow raises OverflowError, and
    math.floor/ceil return ints that lose -0.0."""
    if fn in ("floor", "ceil"):
        fl = lambda t: t if not math.isfinite(t) or t == int(t) else float(math.floor(t))
        return fl(a) if fn == "floor" else -fl(-a)
    f = {"log10": math.log10, "log2": math.log2}.get(fn) or getattr(math, fn)
    odd = b is not None and math.isfinite(b) and b == int(b) and int(b) % 2 == 1
    try:
        return f(a) if b is None else f(a, b)
    except ValueError:
        if fn in ("log", "log10", "log2") and a == 0:
            return float("-inf")
        if fn == "pow" and a == 0 and b < 0:
            return math.copysign(float("inf"), a) if odd else float("inf")
        return float("nan")
    except OverflowError:
        return float("-inf") if fn == "pow" and a < 0 and odd else float("inf")


def parse(path):
    raw = open(path, "rb").read()
    text = raw.replace(b"\r\n", b"\n")
    rows = []
    for line in text.decode().splitlines():
        if not line or line.startswith("#"):
            continue
        p = line.split()
        rows.append((p[0], [h2f(h) for h in p[1:-1]], h2f(p[-1]), p[1:]))
    return text, rows


def expected_digest():
    """(REFMATH_VERSION from refmath.h, the digest golden/digests.txt records for it or None)."""
    version = re.search(r'#define REFMATH_VERSION "([^"]+)"', (HERE.parent / "refmath.h").read_text())[1]
    for line in (HERE / "digests.txt").read_text().splitlines():
        p = line.split()
        if len(p) == 2 and p[0] == version:
            return version, p[1]
    return version, None


def main(argv):
    args, vanilla = [], None
    it = iter(argv[1:])
    for a in it:
        if a == "--vanilla":
            vanilla = next(it)
        else:
            args.append(a)
    text_a, rows = parse(args[0])
    digest = hashlib.sha256(text_a).hexdigest()
    print(f"golden digest (sha256, LF): {digest}  lines={len(rows)}")
    status = 0
    version, want = expected_digest()
    if want is None:
        status = 1
        print(f"expected digest: NONE for {version} in golden/digests.txt; record one when the version changes")
    elif want != digest:
        status = 1
        print(f"expected digest: MISMATCH, {version} is {want}; a result changed without a REFMATH_VERSION bump")
    else:
        print(f"expected digest: MATCH ({version})")
    if len(args) > 1:
        text_b, _ = parse(args[1])
        if text_a == text_b:
            print(f"cross-build: IDENTICAL ({args[0]} == {args[1]})")
        else:
            status = 1
            la, lb = text_a.split(b"\n"), text_b.split(b"\n")
            diffs = [i for i in range(min(len(la), len(lb))) if la[i] != lb[i]]
            print(f"cross-build: DIFFERENT, {len(diffs)} lines, first: {la[diffs[0]]!r} vs {lb[diffs[0]]!r}" if diffs
                  else f"cross-build: DIFFERENT lengths {len(la)} vs {len(lb)}")

    stats, worst = {}, {}
    for fn, ins, out, hexes in rows:
        s = stats.setdefault(fn, {"n": 0, "cr_n": 0, "cr_off": 0, "cr_max": 0, "py_max": 0, "py_nan": 0})
        s["n"] += 1
        want = cr(fn, *ins)
        if want is not None:
            s["cr_n"] += 1
            u = ulps(out, want)
            if u is None or u > 0:
                s["cr_off"] += 1
                u = 1 << 62 if u is None else u
                if u > s["cr_max"]:
                    s["cr_max"] = u
                    worst[fn] = (hexes, want)
        p = pymath(fn, *ins)
        u = ulps(out, p)
        if u is None:
            s["py_nan"] += 1
        elif u > s["py_max"]:
            s["py_max"] = u
    print(f"{'fn':6} {'n':>5} {'cr_n':>5} {'!=cr':>5} {'max_ulp_cr':>10} {'max_ulp_py':>10} {'nan_mism_py':>11}")
    for fn, s in stats.items():
        print(f"{fn:6} {s['n']:5} {s['cr_n']:5} {s['cr_off']:5} {s['cr_max']:10} {s['py_max']:10} {s['py_nan']:11}")
        if s["cr_max"] > 1:
            status = 1
            print(f"   worst vs correctly rounded: {worst[fn]}")

    if vanilla:
        refd = {(fn, tuple(h[:-1])): out for fn, ins, out, h in rows}
        vmap = {"fsin": "sin", "fcos": "cos", "fptan": "tan", "fpatan": "atan2", "fyl2x_ln": "log",
                "fyl2x_lg": "log10", "fyl2x_l2": "log2", "x87acos": "acos", "x87asin": "asin"}
        _, vrows = parse(vanilla)
        vs = {}
        for op, ins, out, h in vrows:
            fn = vmap[op]
            s = vs.setdefault(op, {"n": 0, "ne_ref": 0, "max_ref": 0, "cr_n": 0, "ne_cr": 0, "max_cr": 0,
                                   "ref_ne_cr": 0, "g_n": 0, "g_ne": 0, "g_max": 0})
            s["n"] += 1
            ref = refd.get((fn, tuple(h[:-1])))
            game = all(math.isfinite(v) and abs(v) <= 64 for v in ins)   # game-range operands
            if ref is not None:
                u = ulps(out, ref)
                if game:
                    s["g_n"] += 1
                if u != 0:
                    s["ne_ref"] += 1
                    s["max_ref"] = max(s["max_ref"], 1 << 62 if u is None else u)
                    if game:
                        s["g_ne"] += 1
                        s["g_max"] = max(s["g_max"], 1 << 62 if u is None else u)
            want = cr(fn, *ins)
            if want is not None and abs(ins[0]) < 2.0 ** 63:
                s["cr_n"] += 1
                u = ulps(out, want)
                if u != 0:
                    s["ne_cr"] += 1
                    s["max_cr"] = max(s["max_cr"], 1 << 62 if u is None else u)
                if ref is not None and ulps(ref, want) != 0:
                    s["ref_ne_cr"] += 1
        print("vanilla x87 (this CPU, stored as double) against refmath and correctly rounded:")
        print(f"{'op':9} {'n':>5} {'!=ref':>6} {'max_ulp_ref':>12} {'cr_n':>5} {'x87!=cr':>7} {'max_ulp_cr':>10} {'ref!=cr':>7}"
              f" | {'game_n':>6} {'!=ref':>6} {'max_ulp':>7}  (game: every operand finite, |v| <= 64)")
        for op, s in vs.items():
            mr = s["max_ref"] if s["max_ref"] < 1 << 40 else ">2^40"
            mc = s["max_cr"] if s["max_cr"] < 1 << 40 else ">2^40"
            print(f"{op:9} {s['n']:5} {s['ne_ref']:6} {mr!s:>12} {s['cr_n']:5} {s['ne_cr']:7} {mc!s:>10} {s['ref_ne_cr']:7}"
                  f" | {s['g_n']:6} {s['g_ne']:6} {s['g_max']:7}")
    return status


if __name__ == "__main__":
    sys.exit(main(sys.argv))
