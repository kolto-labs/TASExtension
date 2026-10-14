"""Verification helpers for seeks and judge replays (contract 5.1, 4.3).

Live tracks what a backend's staging records say about the live state: the page
map at the last gate, the last semantic journal, and whether that state is known
to equal the recording. The report helpers name the first divergent field: the
journal field, the page, a symbol for it, and the two page keys.

A restore is journalled on the tick played after it (the shim's EV_RESTORE: the
root, the tick and what admitted the root). A replay of the same ticks has no such
event, so journals are compared without it (journal_core), and the judge checks
the events themselves against the recording (restore_events, recorded_roots).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Iterator
from typing import Any

from kptas.backend.staging import batches
from kptas.holo.pages import PageMap
from kptas.types import (NULL_DIGEST, MemClass, PageFlag, PageRef, RecordFlag, StagingRecord, TickFlag, TickRecord,
                         sha256)


class Live:
    """The live state as the backend's records describe it."""

    def __init__(self) -> None:
        self.map = PageMap()
        self.tick = -1                  # global tick of the last gate (or restore)
        self.journal: bytes | None = None
        self.via = "none"               # boot | replay | restore | semantic | none
        self.ok = False                 # known to equal the recording at self.tick
        self.captured = True            # map is the state at self.tick (False: capture cadence skipped it,
                                        # map is the last captured gate's)

    def feed(self, records: Iterable[StagingRecord], offset: int) -> Iterator[tuple[int, bytes]]:
        """Fold complete tick batches; yield (global tick, journal bytes) after each is applied."""
        for t, pages, jrec, end in batches(records):
            refs = [r.page_ref() for r in pages]
            self.captured = not end.flags & RecordFlag.UNCAPTURED
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
        self.captured = True


_EV = re.compile(rb"\{[^{}]*\}")
_RESTORE = b'{"name":"restore",'


def require_journal_complete(journal: bytes | None) -> None:
    """Reject a journal whose event provenance is missing, truncated, or unprovable.

    New native entries carry an explicit marker. Legacy entries are accepted only
    when their old 16-event bounded list is unambiguously below its cap.
    """
    if not journal:
        raise FormatError("tick has no semantic journal to prove event completeness")
    try:
        obj = json.loads(journal)
    except (ValueError, UnicodeDecodeError):
        raise FormatError("tick semantic journal is not valid JSON") from None
    if not isinstance(obj, dict):
        raise FormatError("tick semantic journal is not an object")
    if "ev_complete" in obj and "ev" not in obj:
        raise FormatError("tick journal completeness marker has no event list")
    events = obj.get("ev", [])
    if not isinstance(events, list):
        raise FormatError("tick semantic journal event list is malformed")
    marker = obj.get("ev_complete")
    dropped = obj.get("ev_dropped", 0)
    if ("ev_complete" in obj and type(marker) is not bool) or type(dropped) is not int:
        raise FormatError("tick semantic journal completeness metadata is malformed")
    if obj.get("events_truncated") or len(events) > 64:
        raise FormatError("tick semantic journal declares or exceeds its event bound")
    if marker is False or dropped != 0:
        raise FormatError(f"tick semantic journal is incomplete ({obj.get('ev_dropped', 'unknown')} event(s) dropped)")
    if marker is True:
        return
    # Before explicit markers the native journal retained at most 16 entries.
    # Exactly 16 is ambiguous: there may have been more and they were discarded.
    if len(events) >= 16 or obj.get("events_truncated"):
        raise FormatError("legacy tick journal may have truncated event provenance")


def journal_core(journal: bytes) -> bytes:
    """The journal without its restore events: the bytes a replay of the same tick journals. A journal
    with no restore event comes back as it is."""
    require_journal_complete(journal)
    i = journal.find(b'"ev":[')
    if i < 0 or _RESTORE not in journal:
        return journal
    k = journal.find(b"]", i)
    keep = [e for e in _EV.findall(journal, i + 6, k) if not e.startswith(_RESTORE)]
    if keep:
        return journal[:i + 6] + b",".join(keep) + journal[k:]
    if journal[i - 1:i] == b",":
        return journal[:i - 1] + journal[k + 1:]
    return journal[:i] + journal[k + 2 if journal[k + 1:k + 2] == b"," else k + 1:]


def journal_digest(rec: TickRecord) -> bytes:
    """journal_sha256 of a tick as a replay would journal it (restore events left out)."""
    require_journal_complete(rec.journal)
    if rec.journal and _RESTORE in rec.journal:
        return sha256(journal_core(rec.journal))
    return rec.journal_sha256


def restore_events(journal: bytes) -> list[dict[str, Any]]:
    """The restore events a tick's journal carries: {root, set, tick}, oldest first."""
    if _RESTORE not in journal:
        return []
    i = journal.find(b'"ev":[')
    k = journal.find(b"]", i)
    out = []
    for e in _EV.findall(journal, i + 6, k):
        if e.startswith(_RESTORE):
            try:
                out.append(json.loads(e))
            except ValueError:
                out.append({"name": "restore", "root": "", "set": "?", "tick": -1})
    return out


def ancestry(b: Any) -> Iterator[TickRecord]:
    """Every tick on a bundle's ancestry, inherited ones first."""
    if b.parent is not None:
        floor = b.chain.tick_floor
        for r in ancestry(b.parent):
            if r.tick > floor:
                break
            yield r
    for t in b.ticks():
        yield b.record(t)


def recorded_roots(b: Any) -> list[bytes]:
    """The roots a seek in b admits for tas restores (contract 4.1): the state roots of every captured
    restore point on b's ancestry, ascending and without repeats."""
    want = TickFlag.CAPTURED | TickFlag.RESTORE_POINT
    return sorted({r.state_root for r in ancestry(b) if r.flags & want == want and r.state_root != NULL_DIGEST})


def roots_digest(roots: Iterable[bytes]) -> bytes:
    """sha256 over the concatenated roots: what the shim journals as an admitted set's name."""
    return sha256(b"".join(roots))


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
        a, b = json.loads(journal_core(want)), json.loads(journal_core(have))
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


__all__ = ["Live", "ancestry", "journal_core", "journal_diff", "journal_digest", "journal_of", "page_diff",
           "recorded_roots", "report", "restore_events", "restore_ranges", "root_hex", "roots_digest", "symbol"]
