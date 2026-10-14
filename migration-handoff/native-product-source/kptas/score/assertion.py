"""@assert: one comparison against the journal entry of the last tick played.

Syntax (after @assert):  FIELD OP VALUE [tol=N] [on_fail=stop|report]

FIELD is a name from FIELDS, or a dotted path into the journal entry (e.g. sem.lfs, rand.s.0.1).
OP is one of == != < <= > >=. VALUE is a decimal integer, a decimal number, or 0x hex. On a
float field (pc.x and friends), a 0x VALUE is compared with the float's raw IEEE-754 bits.
tol=N lets == and != treat values within N as equal. on_fail=stop (the default) stops the run;
on_fail=report logs the failure and goes on.

Evaluation only reads bytes the run already has: it never asks the game for anything.
"""

from __future__ import annotations

import json
import math
import re
import struct
from dataclasses import dataclass
from typing import Any

OPS = ("==", "!=", "<=", ">=", "<", ">")
ON_FAIL = ("stop", "report")

_EXPR = re.compile(r"\s*([A-Za-z_][A-Za-z0-9_.]*)\s*(==|!=|<=|>=|<|>)\s*(\S+)((?:\s+\S+)*)\s*\Z")
_PATH = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)*\Z")
_INT = re.compile(r"[+-]?[0-9]+\Z")
_HEX = re.compile(r"0x[0-9a-fA-F]+\Z")
_NUM = re.compile(r"[+-]?([0-9]+\.?[0-9]*|\.[0-9]+)([eE][+-]?[0-9]+)?\Z")
_F32 = re.compile(r"[0-9a-f]{8}\Z")

# name -> (kind, candidate journal paths). The first path present in the entry is used.
# "f32": the shim journals floats as the hex of their 4 little-endian bytes.
FIELDS: dict[str, tuple[str, tuple[str, ...]]] = {
    "pc.x": ("f32", ("sem.pc.pos.0",)),
    "pc.y": ("f32", ("sem.pc.pos.1",)),
    "pc.z": ("f32", ("sem.pc.pos.2",)),
    "pc.ori.x": ("f32", ("sem.pc.ori.0",)),
    "pc.ori.y": ("f32", ("sem.pc.ori.1",)),
    "pc.ori.z": ("f32", ("sem.pc.ori.2",)),
    "pc.area": ("int", ("sem.pc.area",)),
    "pc.id": ("int", ("sem.pc.id",)),
    "pause": ("int", ("sem.pause",)),
    "rand.n": ("int", ("rand.n", "@rand_draws")),
    "rand.other": ("int", ("rand.o",)),
    "srand.n": ("int", ("srand.n",)),
    "holdrand": ("int", ("hr", "holdrand")),
    "clock_us": ("int", ("v",)),
    "server_passes": ("int", ("sp", "server_passes")),
    "subticks": ("int", ("st", "subticks")),
    "client_passes": ("int", ("cl",)),
    "tick": ("int", ("tick",)),
}


@dataclass(frozen=True, slots=True)
class Check:
    field: str
    op: str
    value: str
    tol: str | None = None
    on_fail: str = "stop"

    def text(self) -> str:
        """Canonical expression text; parse(text()) == self."""
        out = f"{self.field} {self.op} {self.value}"
        if self.tol is not None:
            out += f" tol={self.tol}"
        if self.on_fail != "stop":
            out += f" on_fail={self.on_fail}"
        return out


def parse(expr: str) -> Check:
    """Parse an @assert expression. Raises ValueError with a plain message."""
    m = _EXPR.match(expr)
    if not m:
        raise ValueError("@assert wants FIELD OP VALUE, e.g. '@assert pc.x >= 12.5' "
                         f"(OP is one of {' '.join(OPS)})")
    field, op, value, rest = m.group(1), m.group(2), m.group(3), m.group(4)
    if not _PATH.match(field) or field.endswith("."):
        raise ValueError(f"bad field name '{field}'")
    if not (_INT.match(value) or _HEX.match(value) or _NUM.match(value)):
        raise ValueError(f"'{value}' is not a number (write 12, -3, 12.5 or 0x41480000)")
    if _HEX.match(value):
        value = "0x" + value[2:].lower()
    tol, on_fail = None, "stop"
    seen: set[str] = set()
    for opt in rest.split():
        k, eq, v = opt.partition("=")
        if not eq or k not in ("tol", "on_fail"):
            raise ValueError(f"unexpected '{opt}' after the comparison (options: tol=N, on_fail=stop|report)")
        if k in seen:
            raise ValueError(f"option '{k}' is given twice")
        seen.add(k)
        if k == "tol":
            if not (_INT.match(v) or _NUM.match(v)) or v.startswith("-") or float(v) < 0:
                raise ValueError(f"tol must be a number >= 0, got '{v}'")
            if op not in ("==", "!="):
                raise ValueError("tol= only goes with == or !=")
            if _HEX.match(value):
                raise ValueError("tol= does not go with a 0x value")
            tol = v
        else:
            if v not in ON_FAIL:
                raise ValueError(f"on_fail must be stop or report, got '{v}'")
            on_fail = v
    return Check(field, op, value, tol, on_fail)


class _Missing(Exception):
    pass


def _walk(entry: Any, path: str) -> Any:
    cur = entry
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            raise _Missing(path)
    return cur


def _lookup(entry: dict, field: str) -> tuple[str, Any]:
    """(kind, raw value) for a field: kind is int, f32 or raw."""
    kind, paths = FIELDS.get(field, ("raw", (field,)))
    for p in paths:
        if p == "@rand_draws":            # pysim: draws per site in this tick
            d = entry.get("rand_draws")
            if isinstance(d, dict) and all(type(x) is int for x in d.values()):
                return kind, sum(d.values())
            continue
        try:
            return kind, _walk(entry, p)
        except _Missing:
            continue
    raise _Missing(field)


def _num(text: str) -> int | float:
    if _HEX.match(text):
        return int(text, 16)
    return int(text) if _INT.match(text) else float(text)


def _short(val: float) -> str:
    """The shortest text that reads back as the same float32."""
    for n in range(6, 10):
        t = f"{val:.{n}g}"
        if struct.pack("<f", float(t)) == struct.pack("<f", val):
            return t
    return repr(val)


def _cmp(a: float, op: str, b: float, tol: float | None) -> bool:
    if op in ("==", "!=") and tol is not None:
        eq = abs(a - b) <= tol
        return eq if op == "==" else not eq
    return {"==": a == b, "!=": a != b, "<": a < b, "<=": a <= b, ">": a > b, ">=": a >= b}[op]


def evaluate(check: Check, journal: bytes | None, tick: int | None = None) -> tuple[bool, str]:
    """(passed, actual value as text). Raises ValueError when the value cannot be read or compared.
    `tick` is the run's global tick; the field `tick` uses it, since the shim journals the tick within
    its own process."""
    if check.field == "tick" and tick is not None:
        return _cmp(tick, check.op, _num(check.value), float(check.tol) if check.tol else None), str(tick)
    if not journal or not journal.startswith(b"{"):
        raise ValueError("no journal entry was recorded for it")
    try:
        entry = json.loads(journal)
    except ValueError:
        raise ValueError("its journal entry is not JSON") from None
    if not isinstance(entry, dict):
        raise ValueError("its journal entry is not an object")
    try:
        kind, raw = _lookup(entry, check.field)
    except _Missing:
        raise ValueError(f"{check.field} is not in this backend's journal") from None
    want = _num(check.value)
    tol = float(check.tol) if check.tol is not None else None
    if kind == "f32" or (kind == "raw" and isinstance(raw, str) and _F32.match(raw)
                         and (".pos." in check.field or ".ori." in check.field)):
        if not isinstance(raw, str) or not _F32.match(raw):
            raise ValueError(f"{check.field} is {raw!r} in the journal, not a float")
        bits = struct.unpack("<I", bytes.fromhex(raw))[0]
        val = struct.unpack("<f", bytes.fromhex(raw))[0]
        shown = f"{_short(val)} (bits 0x{bits:08x})"
        if _HEX.match(check.value):
            return _cmp(bits, check.op, want, None), shown
        if math.isnan(val):
            return check.op == "!=", shown
        return _cmp(val, check.op, want, tol), shown
    if type(raw) is bool or not isinstance(raw, (int, float)):
        raise ValueError(f"{check.field} is {json.dumps(raw)[:60]} in the journal, not a number")
    return _cmp(raw, check.op, want, tol), str(raw)
