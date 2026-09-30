"""Verification helpers for seeks and judge replays (contract 5.1, 4.3).

Live tracks what a backend's staging records say about the live state: the page
map at the last gate, the last semantic journal, and whether that state is known
to equal the recording. The report helpers name the first divergent field: the
journal field, the page, a symbol for it, and the two page keys.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from typing import Any

from kptas.backend.staging import batches
from kptas.holo.pages import PageMap
from kptas.types import (NULL_DIGEST, MemClass, PageFlag, PageRef, RecordFlag, StagingRecord, TickRecord,
                         sha256)


class Live:
    """The live state as the backend's records describe it."""

    def __init__(self) -> None:
        self.map = PageMap()
        self.tick = -1                  # global tick of the last gate (or restore)
        self.journal: bytes | None = None
        self.via = "none"               # boot | replay | restore | semantic | none
        self.ok = False                 # known to equal the recording at self.tick

    def feed(self, records: Iterable[StagingRecord], offset: int) -> Iterator[tuple[int, bytes]]:
        """Fold complete tick batches; yield (global tick, journal bytes) after each is applied."""
        for t, pages, jrec, end in batches(records):
            refs = [r.page_ref() for r in pages]
            if end.flags & RecordFlag.FULL:
                self.map = PageMap(r for r in refs if not r.flags & PageFlag.DECOMMIT)
            else:
                self.map.apply(refs)
            self.tick, self.journal = t + offset, jrec.data if jrec is not None else b""
            yield self.tick, self.journal

    def restored(self, tick: int, pages: Iterable[StagingRecord], via: str = "restore") -> None:
        """A restore wrote W and emitted no records: apply W here."""
        self.map.apply(r.page_ref() for r in pages)
        self.tick, self.journal, self.via = tick, None, via


def journal_of(rec: TickRecord) -> dict[str, Any] | None:
    try:
        obj = json.loads(rec.journal) if rec.journal.startswith(b"{") else None
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def journal_diff(want: bytes | None, have: bytes | None) -> list[str]:
    """First differing semantic-journal field, as 'field: recorded X, live Y'."""
    if not want or want == have:
        return []
    if not have:
        return ["journal: no live journal at this tick"]
    try:
        a, b = json.loads(want), json.loads(have)
    except ValueError:
        return [f"journal: sha256 recorded {sha256(want).hex()[:16]}, live {sha256(have).hex()[:16]}"]
    for k in sorted(set(a) | set(b)):
        if a.get(k) != b.get(k):
            return [f"journal field {k}: recorded {a.get(k)!r}, live {b.get(k)!r}"]
    return []


def symbol(ref: PageRef, pmap: PageMap) -> str:
    """A name for a page: its memory class and offset from that class's lowest page."""
    base = min((r.vaddr for r in pmap.refs() if r.mclass == ref.mclass), default=ref.vaddr)
    return f"{MemClass(ref.mclass).label}+{ref.vaddr - base:#x}"


def page_diff(want: PageMap, have: PageMap, limit: int = 3) -> list[str]:
    """Pages whose key, class or protection differ, lowest first, with their keys."""
    out, n = [], 0
    vaddrs = sorted({r.vaddr for r in want.refs()} | {r.vaddr for r in have.refs()})
    for v in vaddrs:
        a, b = want.get(v), have.get(v)
        if a == b:
            continue
        n += 1
        if len(out) < limit:
            ref = a or b
            ka = a.key.hex() if a else "absent"
            kb = b.key.hex() if b else "absent"
            out.append(f"page {v:#010x} ({symbol(ref, want if a else have)}): recorded key {ka}, live key {kb}")
    if n > limit:
        out.append(f"... {n - limit} more differing pages")
    return out


def report(tick: int, field: str, want: TickRecord | None, have_journal: bytes | None,
           want_map: PageMap | None, have_map: PageMap | None) -> list[str]:
    """The first-divergent-field report of 5.1."""
    lines = [f"first divergence at tick {tick}: {field}"]
    if want is not None:
        lines += journal_diff(want.journal or None, have_journal)
    if want_map is not None and have_map is not None:
        lines += page_diff(want_map, have_map)
        lines.append(f"state_root recorded {want_map.root().hex()}, live {have_map.root().hex()}")
    return lines


def restore_ranges(points: Iterable[tuple[int, bool]]) -> tuple[tuple[int, int], ...]:
    """Half-open ranges of consecutive restore-point ticks."""
    out: list[list[int]] = []
    for t, rp in sorted(points):
        if not rp:
            continue
        if out and out[-1][1] == t:
            out[-1][1] = t + 1
        else:
            out.append([t, t + 1])
    return tuple((a, b) for a, b in out)


def root_hex(d: bytes) -> str:
    return "" if d == NULL_DIGEST else d.hex()


__all__ = ["Live", "journal_diff", "journal_of", "page_diff", "report", "restore_ranges", "root_hex", "symbol"]
