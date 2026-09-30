"""Plain-text views of ticks, headers, receipts and divergences."""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Iterable, Iterator, Sequence
from typing import Any

from kptas.types import (NULL_DIGEST, Header, Receipt, StepReport, TickFlag, TickRecord,
                         VerbClass)

_VC_SHORT = {
    VerbClass.INPUT: "I", VerbClass.OS_INPUT: "O", VerbClass.GATE: "G", VerbClass.CLOCK: "C",
    VerbClass.WAIT: "W", VerbClass.CAPTURE: "P", VerbClass.READ: "R", VerbClass.OS_EVENT: "E",
    VerbClass.SCREEN: "S", VerbClass.RESTORE: "T", VerbClass.FILES_RESTORE: "F",
    VerbClass.WRITE: "w", VerbClass.SEMANTIC: "s", VerbClass.FIXED_CLOCK: "x",
    VerbClass.FILES_WRITE: "f",
}
_FLAG_SHORT = {
    TickFlag.CAPTURED: "c", TickFlag.RESTORE_POINT: "r", TickFlag.KEYFRAME: "k",
    TickFlag.MATERIALIZED: "m", TickFlag.EVICTED: "v", TickFlag.HOLD: "h",
    TickFlag.EPOCH_START: "e", TickFlag.SIM_ROOT: "s", TickFlag.FILES: "f",
}
LEGEND = ("verbs: I input O os-input G gate C clock W wait P capture R read E os-event S screen "
          "T restore F files-restore | lab: w write s semantic x fixed-clock f files-write\n"
          "flags: c captured r restore-point k keyframe m materialized v evicted h hold "
          "e epoch-start s sim-root f files")

COLUMNS = ("tick", "ep", "verbs", "flags", "reached", "sp", "sub", "journal", "root", "note")


def short_hex(d: bytes, n: int = 8) -> str:
    return "-" if not d or d == NULL_DIGEST else d.hex()[:n]


def verbs(vc: VerbClass) -> str:
    return "".join(c for bit, c in _VC_SHORT.items() if vc & bit) or "-"


def flags(f: TickFlag) -> str:
    return "".join(c for bit, c in _FLAG_SHORT.items() if f & bit) or "-"


def _row(r: TickRecord) -> tuple[str, ...]:
    return (str(r.tick), str(r.epoch), verbs(r.verbclass), flags(r.flags),
            r.reached.name.lower(), str(r.server_passes), str(r.subticks),
            short_hex(r.journal_sha256), short_hex(r.state_root), r.note or ".")


def report_row(rep: StepReport, note: str = "") -> tuple[str, ...]:
    return (str(rep.tick), str(rep.epoch), verbs(rep.verbclass),
            "r" if rep.restore_point else "-", rep.outcome.value, str(rep.server_passes),
            str(rep.subticks), short_hex(rep.journal_sha256), short_hex(rep.state_root),
            note or ".")


def _fmt(rows: Sequence[tuple[str, ...]], head: Sequence[str]) -> Iterator[str]:
    widths = [max(len(head[i]), *(len(r[i]) for r in rows)) if rows else len(head[i])
              for i in range(len(head))]
    last = len(head) - 1
    def line(cells: Sequence[str]) -> str:
        return "  ".join(c if i == last else c.ljust(widths[i]) for i, c in enumerate(cells)).rstrip()
    yield line(head)
    for r in rows:
        yield line(r)


def tick_table(records: Iterable[TickRecord], *, start: int | None = None, end: int | None = None,
               limit: int | None = None) -> Iterator[str]:
    """One line per tick in [start, end]; at most limit rows."""
    rows = []
    for r in records:
        if start is not None and r.tick < start:
            continue
        if end is not None and r.tick > end:
            break
        rows.append(_row(r))
        if limit is not None and len(rows) >= limit:
            break
    return _fmt(rows, COLUMNS)


REPORT_HEAD = ("tick", "ep", "verbs", "rp", "proc", "sp", "sub", "journal", "root", "note")
_REPORT_W = (8, 3, 6, 2, 6, 3, 4, 8, 8)


def report_line(rep: StepReport | None, note: str = "") -> str:
    """One fixed-width row for streaming output; None gives the column heads."""
    cells = REPORT_HEAD if rep is None else report_row(rep, note)
    return "  ".join(c.ljust(w) for c, w in zip(cells, _REPORT_W)) + "  " + cells[-1]


def report_table(reps: Iterable[tuple[StepReport, str]]) -> Iterator[str]:
    return _fmt([report_row(r, n) for r, n in reps], REPORT_HEAD)


def kv_lines(obj: dict[str, Any], indent: str = "  ") -> Iterator[str]:
    w = max((len(k) for k in obj), default=0)
    for k, v in obj.items():
        if isinstance(v, (list, tuple)):
            v = ", ".join(str(x) for x in v) or "-"
        elif isinstance(v, dict):
            v = ", ".join(f"{a}={b}" for a, b in v.items()) or "-"
        elif v is None or v == "":
            v = "-"
        yield f"{indent}{k.ljust(w)}  {v}"


def header_lines(h: Header) -> Iterator[str]:
    return kv_lines(h.to_obj())


def receipt_lines(r: Receipt) -> Iterator[str]:
    return kv_lines(r.to_obj())


@dataclasses.dataclass(frozen=True, slots=True)
class Divergence:
    tick: int
    field: str
    a: TickRecord | None
    b: TickRecord | None

    def lines(self) -> Iterator[str]:
        yield f"first divergence at tick {self.tick}: {self.field}"
        for side, r in (("a", self.a), ("b", self.b)):
            if r is None:
                yield f"  {side}: (no tick)"
            else:
                v = getattr(r, self.field) if self.field != "missing" else ""
                if isinstance(v, bytes):
                    v = v.hex() or "-"
                yield f"  {side}: epoch {r.epoch} note {r.note or '.'} {self.field}={v}"


def tick_differs(a: TickRecord, b: TickRecord, compare_roots: bool = True) -> str | None:
    """Name of the first field that differs, or None.

    state_root is compared only when both ticks were captured and compare_roots
    holds (same determinism key); otherwise ticks are compared by journal digest only.
    """
    if a.epoch != b.epoch:
        return "epoch"
    if a.note != b.note:
        return "note"
    if a.journal_sha256 != b.journal_sha256:
        return "journal_sha256"
    if compare_roots and a.flags & b.flags & TickFlag.CAPTURED and a.state_root != b.state_root:
        return "state_root"
    if (compare_roots and a.flags & b.flags & TickFlag.SIM_ROOT and a.sim_root != b.sim_root):
        return "sim_root"
    return None


def diff_ticks(a: Iterable[TickRecord], b: Iterable[TickRecord], *,
               compare_roots: bool = True) -> Iterator[Divergence]:
    """Every differing tick, walking both runs in tick order."""
    ia, ib = iter(a), iter(b)
    ra, rb = next(ia, None), next(ib, None)
    while ra is not None or rb is not None:
        if rb is None or (ra is not None and ra.tick < rb.tick):
            yield Divergence(ra.tick, "missing", ra, None)
            ra = next(ia, None)
        elif ra is None or rb.tick < ra.tick:
            yield Divergence(rb.tick, "missing", None, rb)
            rb = next(ib, None)
        else:
            f = tick_differs(ra, rb, compare_roots)
            if f:
                yield Divergence(ra.tick, f, ra, rb)
            ra, rb = next(ia, None), next(ib, None)


def bisect_ticks(get_a: Callable[[int], TickRecord | None], get_b: Callable[[int], TickRecord | None],
                 lo: int, hi: int, *, compare_roots: bool = True) -> tuple[Divergence | None, int]:
    """Binary search for the first differing tick in [lo, hi].

    Assumes a split persists once it happens, as git bisect does. Returns the
    divergence (None when tick hi still matches) and the number of probes.
    """
    probes = 0

    def same(t: int) -> bool:
        nonlocal probes
        probes += 1
        x, y = get_a(t), get_b(t)
        if x is None or y is None:
            return x is None and y is None
        return tick_differs(x, y, compare_roots) is None

    if same(hi):
        return None, probes
    while lo < hi:
        mid = (lo + hi) // 2
        if same(mid):
            lo = mid + 1
        else:
            hi = mid
    x, y = get_a(lo), get_b(lo)
    field = "missing" if x is None or y is None else tick_differs(x, y, compare_roots) or "?"
    return Divergence(lo, field, x, y), probes
