"""Seek planner (contract 5.1): bring a live backend to a recorded tick T and verify it.

  replay    all tiers  from a boot origin (or a live state reached by replay), replay the embedded score
  restore   lab, tas   write W, the pages where the live map and map(T) differ, then verify
  hybrid    lab, tas   restore the latest restore point K <= T, then replay K..T
  semantic  lab        restore K, then write back T's journalled engine fields; the branch is tainted

A requested mode that cannot apply to T (not a restore point, no
stored map) is replaced per seek.on_incompatible. A mode whose check fails moves
down seek.fallback_chain, each level once. When the last level fails, the seek
halts: the first-divergent-field report goes to the log and the receipt says fail.
Every seek appends one RCPT to the bundle. Save origins need a backend that can
reload a save, which none has yet, so replay origins are boots.
"""

from __future__ import annotations

import base64
import dataclasses
import contextlib
import functools
import os
import sys
import threading
import time
import weakref
from collections import OrderedDict
from collections.abc import Callable, Mapping
from typing import Any
from pathlib import Path

from kptas.filetree import validate_rows
from kptas.holo import verify as hv
from kptas.holo.bundle import EVENT_MARK, Bundle, MapUnavailableError
from kptas.holo.pages import PageMap
from kptas.score import Expect, Note, Relaunch, SaveMs, Segment, Sync, compile_score
from kptas.supervisor import PROCESS_BACKENDS, RunError, TelosError, merge_oneshots
from kptas.types import (NULL_DIGEST, PAGE_SIZE, ZERO_PAGE_KEY, BackendError, EngineEvent, FormatError, MemClass,
                         PageRef, Receipt, SeekMode, StagingRecord, Tier, TickFlag, TickRecord, VerbClass, VerifyLevel, canonical_json, sha256)

_SESSIONS: "weakref.WeakKeyDictionary[Any, _Session]" = weakref.WeakKeyDictionary()
_MAP_CACHE_ENTRIES = 8      # resolved recorded maps kept per session (~20k refs each); seek.hot_maps sets it


def _configure_hot(cfg: Mapping[str, Any]) -> None:
    """Apply [seek] hot_tier_mb (process-wide verified object bytes) and hot_maps (resolved maps
    per session). telos.session reads _MAP_CACHE_ENTRIES at call time, so both trims agree."""
    global _MAP_CACHE_ENTRIES
    s = cfg.get("seek", {}) if isinstance(cfg, Mapping) else {}
    from kptas.holo.pages import HOT
    if "hot_tier_mb" in s:      # a partial or older cfg leaves the tier as it is
        HOT.configure(int(s["hot_tier_mb"]) << 20)
    if "hot_maps" in s:
        _MAP_CACHE_ENTRIES = max(1, int(s["hot_maps"]))


def hot_stats() -> dict[str, int]:
    """Hits, misses and size of the process-wide hot tier (seek.hot_tier_mb)."""
    from kptas.holo.pages import HOT
    return HOT.stats()


def _profiling() -> bool:
    return os.environ.get("KPTAS_SEEK_PROFILE") == "1"


@contextlib.contextmanager
def _phase(log: Callable[[str], None], name: str):
    enabled = _profiling()
    start = time.perf_counter_ns() if enabled else 0
    try:
        yield
    finally:
        if enabled:
            log(f"seek-phase {name} us={(time.perf_counter_ns() - start) // 1000}")


def _profiled(name: str):
    def decorate(fn):
        @functools.wraps(fn)
        def timed(self, *args, **kwargs):
            with _phase(self.log, name):
                return fn(self, *args, **kwargs)
        return timed
    return decorate


class _Mismatch(Exception):
    def __init__(self, tick: int, field: str, lines: list[str] | None = None):
        super().__init__(f"tick {tick}: {field}")
        self.tick, self.field, self.lines = tick, field, lines or []


@dataclasses.dataclass
class Cursor:
    """Where the score stands after a recorded tick: the next item, ticks of it already run, and state."""
    i: int
    j: int = 0
    pending: Any = None          # count-0 one-shots waiting for the next note
    hz: int = 60
    scale: int = 100
    carry: tuple[tuple[str, str | None], ...] = ()   # events a @sync right after this point is met from
    next_save_ms: int | None = None
    last_osev: str | None = None


@dataclasses.dataclass
class _Session:
    live: hv.Live
    seeks: int = 0
    played: set[int] = dataclasses.field(default_factory=set)   # ticks this process ran itself, since its boot
    roots: dict[str, list[bytes]] = dataclasses.field(default_factory=dict)   # recorded roots by bundle id
    # Recording-derived, so they outlive commands and reboots (which reset only live/played).
    # maps: (tick, chain link after that TICK) -> frozen refs of map(tick). The link
    # names the whole chain prefix, ancestry included (HOLO binds the parent link); a hit also needs the
    # journal's state_root at tick to match, so an index-supplied link alone never admits a map.
    # Values are bare ref tuples: telos.session._recorded_live_map shares this dict and builds
    # PageMap(value) from it. The recorded root each tuple was proved against lives in map_roots.
    maps: OrderedDict[tuple[int, bytes], tuple[PageRef, ...]] = dataclasses.field(default_factory=OrderedDict)
    map_roots: dict[tuple[int, bytes], bytes] = dataclasses.field(default_factory=dict)
    # checked: Store object -> {(tick, link, lineage store generations)} whose every page that store
    # resolved hash-verified. Weak keys: a closed or reopened store never inherits another's proof.
    checked: "weakref.WeakKeyDictionary[Any, set[tuple[int, bytes, tuple[int, ...]]]]" = dataclasses.field(
        default_factory=weakref.WeakKeyDictionary)


class _LiveEOFContext:
    """One recording owner's seal transition, never an archive or wire capability."""

    @classmethod
    def capture(cls, owner: Any):
        sup, sink = owner.sup, owner.sink
        if (owner.mode != 'recording' or owner.closed or owner.broken
                or not owner._operation_depth or sup is None or sink is None
                or sup.sink is not sink or owner.cur.sealed or sink.bundle.sealed is not None
                or sup.name != 'shim' or sup.attach_mode or sup.alive is not True
                or sup.epoch_closed or sup.backend is None
                or getattr(sup.backend, 'simgame', True) is not False
                or getattr(sup.backend, 'poisoned', True) is not False
                or sink.last_tick != owner.cur.last or sink.last_tick != sup.tick
                or sink.epoch_seen != sup.epoch or sink.bundle.last_tick != sup.tick
                or str(sink.bundle.root) != owner.cur.path or sink.pmap is None):
            return None
        link, wire = sup.link, getattr(sup.backend, 'wire', None)
        proc = getattr(link, 'proc', None)
        if link is None or wire is None or proc is None or proc.poll() is not None:
            return None
        try:
            rec = sink.bundle.record(sup.tick)
        except (KeyError, FormatError, OSError):
            return None
        if (rec.epoch != sup.epoch or sink.pmap.root() != rec.state_root
                or sink.files_root != rec.files_root):
            return None
        self = cls()
        self.owner, self.take, self.sink = owner, owner.cur, sink
        self.session_id, self.revision = owner.session_id, owner.revision
        self.thread = threading.get_ident()
        self.sup, self.backend, self.link, self.proc, self.wire = sup, sup.backend, link, proc, wire
        self.epoch, self.offset, self.last = sup.epoch, sup.offset, sup.tick
        self.bundle_id, self.path = sink.bundle.bundle_id, sink.bundle.root
        self.root, self.files_root = rec.state_root, rec.files_root
        self.bundle = None
        self.claimed = self.used = False
        return self

    def owner_matches(self, sup: Any, last: int, epoch: int) -> bool:
        owner = self.owner
        return (threading.get_ident() == self.thread and owner._operation_depth > 0
                and owner.session_id == self.session_id and owner.revision == self.revision
                and owner.mode == 'recording' and not owner.closed and owner.cur is self.take
                and owner.sup is sup and sup is self.sup and sup.backend is self.backend
                and sup.alive is True and getattr(self.backend, 'poisoned', True) is False
                and sup.link is self.link and self.link.proc is self.proc
                and getattr(self.backend, 'wire', None) is self.wire
                and self.proc.poll() is None and not sup.attach_mode and not sup.epoch_closed
                and sup.epoch == epoch == self.epoch and sup.offset == self.offset
                and sup.tick == last == self.last and owner.sink is None and sup.sink is None
                and self.take.sealed and self.take.last == self.last)

    def bind(self, b: Bundle):
        """Bind only after the writer successfully sealed and this exact reader reopened."""
        if (self.bundle is not None or not self.owner_matches(self.sup, self.last, self.epoch)
                or b.root != self.path or b.bundle_id != self.bundle_id or b.last_tick != self.last
                or b.sealed is None or b.link != self.sink.bundle.link
                or b.sealed != self.sink.bundle.sealed
                or self.owner._seal_error is not None or self.take.durable_tick != self.last):
            return None
        try:
            rec = b.record(self.last)
        except (KeyError, FormatError, OSError):
            return None
        if rec.epoch != self.epoch or rec.state_root != self.root or rec.files_root != self.files_root:
            return None
        self.bundle, self.seal, self.chain_link = b, dict(b.sealed), b.link
        return self

    def claim(self, b: Bundle, sup: Any):
        claimed, self.claimed = self.claimed, True
        return self if not claimed and self.matches(b, sup, self.last, self.epoch) else None

    def matches(self, b: Bundle, sup: Any, last: int, epoch: int) -> bool:
        return (b is self.bundle and b.bundle_id == self.bundle_id and b.root == self.path
                and b.link == self.chain_link and b.sealed == self.seal and b.last_tick == self.last
                and self.owner_matches(sup, last, epoch))

    def consume(self, b: Bundle, sup: Any, last: int, epoch: int) -> bool:
        used, self.used = self.used, True
        return not used and self.matches(b, sup, last, epoch)


def _held(note: Any) -> Any:
    return dataclasses.replace(note, wheel=0, osev=None) if note.wheel or note.osev else note


def recorded_events(b: Bundle) -> dict[int, list[tuple[str, str | None]]]:
    """Engine events by tick, from the event marks of b and of its ancestors up to each branch tick."""
    out: dict[int, list[tuple[str, str | None]]] = {}
    upto = None
    while b is not None:
        for m in b.marks:
            if m["name"].startswith(EVENT_MARK) and (upto is None or m["tick"] <= upto):
                name, sep, arg = m["name"][len(EVENT_MARK):].partition(":")
                out.setdefault(m["tick"], []).append((name, arg if sep else None))
        if b.header.parent is not None:
            upto = min(upto, b.header.parent.tick) if upto is not None else b.header.parent.tick
        b = b.parent
    return out


def terminal_input_attempt_ticks(b: Bundle) -> frozenset[int]:
    """Ticks with an exited-before-gate input receipt (intent only, no guest effect proof)."""
    return frozenset(tick for tick, events in recorded_events(b).items()
                     if any(name == "input.attempted_exit" for name, _arg in events))


def recorded_epochs(b: Bundle) -> list[dict[str, Any]]:
    """Completed epochs on the ancestry, with the branch's own ends taking precedence."""
    epochs: dict[int, dict[str, Any]] = {}
    limit = None
    while b is not None:
        seen: set[int] = set()
        for ep in b.epochs:
            if ep["epoch"] in seen:
                raise FormatError(f"duplicate EPOC {ep['epoch']} in {b.root.name}")
            seen.add(ep["epoch"])
            if (limit is None or ep["tick"] <= limit) and ep["epoch"] not in epochs:
                epochs[ep["epoch"]] = ep
        fork = b.header.parent
        if fork is not None:
            limit = min(limit, fork.tick) if limit is not None else fork.tick
        b = b.parent
    return [ep for _number, ep in sorted(epochs.items())]


class _Seek:
    def __init__(self, b: Bundle, sup: Any, cfg: Mapping[str, Any], log: Callable[[str], None]):
        self.b, self.sup, self.cfg, self.log = b, sup, cfg, log
        _configure_hot(cfg)
        self._recs: dict[int, TickRecord | None] = {}
        self._rows: dict[int, tuple[int, int]] | None = None
        self._checked_files: set[bytes] = set()
        score = compile_score(b.score)
        self.items = score.items
        hz = score.header.get("hz")
        self.base_hz = int(hz) if hz else cfg["clock"]["hz"]
        self.base_scale = cfg["clock"]["scale"]
        self.save_ms = score.header.get("save_ms")
        self.timeout = cfg["score"]["default_timeout_ticks"]
        s = _SESSIONS.get(sup) if sup is not None else None
        if s is None:
            s = _Session(hv.Live())
            if sup is not None:          # sup None: placement only (resume_cursor)
                _SESSIONS[sup] = s
        self.session = s
        self.roots = False           # compare state roots per tick while driving
        self.replayed = 0
        self._native_restore = False
        self._live_eof: _LiveEOFContext | None = None
        self.failed: set[int] = set()    # ticks a restore (or its canary) failed at in this seek

    def require_restore_dll(self) -> None:
        from kptas.tier.envpin import require_native_restore_dll
        bundle = self.b
        while bundle is not None:
            require_native_restore_dll(bundle.env, self.cfg, self.sup.backend)
            bundle = bundle.parent

    @property
    def live(self) -> hv.Live:
        return self.session.live

    # ------------------------------------------------------------ recording

    def rec(self, t: int) -> TickRecord | None:
        if t not in self._recs:
            try:
                self._recs[t] = self.b.record(t) if t >= 0 else None
            except KeyError:
                self._recs[t] = None
        return self._recs[t]

    def first_tick(self) -> int:
        b = self.b
        while b.parent is not None:
            b = b.parent
        return b.ticks()[0] if b.ticks() else 0

    @_profiled("map_at")
    def map_at(self, t: int) -> PageMap | None:
        # map(t) is a pure function of the chain prefix through t's TICK, which link_at(t) names.
        # Reuse it across commands of this session under that key; never lend a mutable PageMap.
        try:
            key: tuple[int, bytes] | None = (t, self.b.link_at(t))
        except KeyError:
            key = None                 # no such tick: let Bundle.map_at refuse, cache nothing
        rec = self.rec(t)
        root = rec.state_root if rec is not None else None
        maps, proved = self.session.maps, self.session.map_roots
        hit = maps.get(key) if key is not None and root is not None else None
        if hit is not None:
            # An entry another caller stored carries no proof yet: its own tree root must equal
            # the journal's state_root before it stands in for map(t).
            m = PageMap(hit)
            if proved.get(key) == root or m.root() == root:
                proved[key] = root
                maps.move_to_end(key)
                return m
        try:
            resolved = self.b.map_at(t)
        except MapUnavailableError:
            return None
        refs = tuple(resolved.refs())  # PageRef is frozen; containers stay private.
        if key is not None and root is not None:
            maps[key], proved[key] = refs, root
            while len(maps) > _MAP_CACHE_ENTRIES:
                old, _refs = maps.popitem(last=False)
                proved.pop(old, None)
            if len(proved) > 4 * _MAP_CACHE_ENTRIES:   # entries the session's own trim dropped
                for k in [k for k in proved if k not in maps]:
                    del proved[k]
        return PageMap(refs)

    @_profiled("place")
    def place(self, end: int) -> dict[int, Cursor]:
        """Place the score on the recorded tick line, mirroring Supervisor.run_score, up to tick `end`.

        A @sync or @expect exit consumes the recorded run of WAIT ticks; a @sync that consumed none was
        met from the previous report's events. Two waits in a row cannot be told apart, so placement
        stops there. Ticks inside a wait are not resume points. A cursor inside a note carries the
        events recorded on the note's ticks so far, which the rest of the note's report would have held.

        Whether the game is still running follows the recording, not the notes: !close only asks the game
        to quit (K1 opens its quit box), so the process ends where its epoch ends. A note whose epoch ends
        before the note does was cut there by the exit, as Supervisor.run_score cuts it.
        """
        evs = recorded_events(self.b)
        t = self.first_tick()
        last_of: dict[int, int] = {}          # epoch -> its last recorded tick
        rows = self.flags()
        for tk, (ep, _fl) in rows.items():
            last_of[ep] = max(last_of.get(ep, tk), tk)
        next_save_ms, last_osev = None, None
        pending, hz, scale, alive, prev_wait = None, self.base_hz, self.base_scale, True, False
        at = {t: Cursor(0, 0, None, hz, scale)}
        for i, it in enumerate(self.items):
            if t >= end:
                break
            n = 0
            if isinstance(it, Segment):
                hz, scale = it.hz or self.base_hz, it.scale or self.base_scale
            elif isinstance(it, Note):
                if it.count == 0:
                    pending = merge_oneshots(pending, it)
                    continue
                n = min(it.count, end - t)
                row = rows.get(t)
                cut = row is not None and last_of.get(row[0], t + n) < t + n   # the process exited inside it
                if cut:
                    n = last_of[row[0]] - t
                osev = it.osev or (pending.osev if pending is not None else None)
                if n and osev:
                    last_osev = osev
                seen: tuple[tuple[str, str | None], ...] = ()
                for j in range(1, n + 1):
                    seen += tuple(evs.get(t + j, ()))
                    if any(name == "save.begin" for name, _arg in evs.get(t + j, ())):
                        next_save_ms = None
                    if j < it.count:
                        at[t + j] = Cursor(i, j, None, hz, scale, seen, next_save_ms, last_osev)
                if cut:
                    alive = False      # run_score goes on with the next item; at[t + n] is set below
                elif n < it.count:
                    return at          # the note runs on past `end`
                pending, prev_wait = None, False
            elif isinstance(it, (Sync, Expect)):
                if isinstance(it, Expect) and not ("exit" in it.outcome and alive):
                    continue
                while (r := self.rec(t + n + 1)) is not None and t + n < end and r.verbclass & VerbClass.WAIT \
                        and not r.flags & TickFlag.EPOCH_START:
                    n += 1
                if prev_wait:
                    at.pop(t, None)    # two waits in a row: their boundary is unknown
                    return at
                if n == 0 and isinstance(it, Sync) and t in at:
                    at[t] = dataclasses.replace(at[t], carry=at[t].carry + ((it.event, it.arg),))
                prev_wait = n > 0
                alive = alive and isinstance(it, Sync)
            elif isinstance(it, SaveMs):
                next_save_ms = it.ms
            elif isinstance(it, Relaunch):
                if it.cond == "exit" and alive:
                    continue
                row = rows.get(t + 1)
                n = 1 if row is not None and row[1] & TickFlag.EPOCH_START else 0
                alive, prev_wait, last_osev = True, False, None
            if any(name == "save.begin" for tk in range(t + 1, t + n + 1)
                   for name, _arg in evs.get(tk, ())):
                next_save_ms = None
            t += n
            if n:
                at[t] = Cursor(i + 1, 0, pending, hz, scale, next_save_ms=next_save_ms, last_osev=last_osev)
                if isinstance(it, Expect) and any(name == "proc.down" for name, _arg in evs.get(t + 1, ())):
                    # The final WAIT gate precedes the exit itself. Resuming here must still wait for it.
                    at[t] = dataclasses.replace(at[t], i=i)
        return at

    # ------------------------------------------------------------ live state

    def absorb(self, rep: Any = None, strict: bool = True) -> None:
        """Fold the backend's new records into the live map and compare each tick with the recording."""
        sup, live = self.sup, self.live
        try:
            recs = list(sup.backend.records())
        except BackendError:
            recs = None
        if recs is None:
            if rep is not None:
                live.tick, live.journal = sup.tick, None
                self.compare(sup.tick, None, rep, strict)
            return
        first = None
        for t, journal in live.feed(recs, sup.offset):
            self.session.played.add(t)
            try:
                self.compare(t, journal, None, strict)
            except _Mismatch as e:        # keep folding: records already drained must reach live.map
                first = first or e
        if live.tick > sup.tick:          # an in-process relaunch reports its boot tick with the next step
            sup.tick = live.tick
        if first is not None:
            live.ok = False
            raise first

    def compare(self, t: int, journal: bytes | None, rep: Any, strict: bool) -> None:
        want, live = self.rec(t), self.live
        field = None
        if want is None:
            field = "missing (the recording has no such tick)"
        elif want.epoch != self.sup.epoch:
            field = f"epoch (recorded {want.epoch}, live {self.sup.epoch})"
        else:
            # restore events are left out on both sides: the recording's first tick after a restore has one,
            # and so does a live tick played right after this seek's own restore
            jd = sha256(hv.journal_core(journal)) if journal else (rep.journal_sha256 if rep is not None else NULL_DIGEST)
            if want.journal_sha256 != NULL_DIGEST and jd != hv.journal_digest(want):
                field = "journal_sha256"
            elif self.roots and want.flags & TickFlag.CAPTURED and (rep is not None or live.captured):
                # capture cadence: a live tick the shim did not capture has no root to compare
                have = live.map.root() if rep is None else rep.state_root
                if have != want.state_root:
                    field = "state_root"
        live.ok = field is None
        if field is None:
            return
        if strict:
            wm = self.map_at(t) if want is not None and want.flags & TickFlag.CAPTURED else None
            raise _Mismatch(t, field, hv.report(t, field, want, journal, wm, live.map if wm is not None else None))

    def epoch_start(self, T: int) -> int:
        epoch = self.rec(T).epoch
        starts = [t for t, (ep, fl) in self.flags().items()
                  if ep == epoch and fl & TickFlag.EPOCH_START]
        if len(starts) != 1:
            raise TelosError(f"epoch {epoch} needs exactly one recorded EPOCH_START; found {len(starts)}")
        origin = starts[0]
        rec = self.rec(origin)
        first = min(t for t, (ep, _fl) in self.flags().items() if ep == epoch)
        if (rec is None or rec.epoch != epoch or not rec.flags & TickFlag.EPOCH_START
                or origin != first or origin > T):
            raise FormatError(f"epoch {epoch} origin {origin} disagrees with its recorded index")
        return origin

    def check_files(self, root: bytes) -> None:
        """Resolve every overlay blob before replacing a process or writing files."""
        if root == NULL_DIGEST:
            raise FormatError("recorded files overlay has a null root")
        rows = self.b.file_tree(root)
        blob = lambda digest: self.b.file(bytes.fromhex(digest))
        if self.sup.name == 'shim' and self.cfg['game']['dir'] and self.cfg['files']['store']:
            from kptas.backend.rig import StageFiles
            import os
            StageFiles(Path(os.path.expanduser(self.cfg['game']['dir'])),
                       self.cfg['files']['stage_paths']).preflight(rows, blob)
        elif root not in self._checked_files:
            validate_rows(rows, blob)
        self._checked_files.add(root)

    def needs_boot(self, T: int) -> bool:
        sup = self.sup
        proc = getattr(sup.link, "proc", None)
        disconnected = (sup.name in PROCESS_BACKENDS and sup.backend is not None
                        and hasattr(sup.backend, "wire") and sup.backend.wire is None)
        return (sup.backend is None or not sup.alive or sup.epoch != self.rec(T).epoch
                or disconnected or (proc is not None and proc.poll() is not None))

    def check_lifecycle(self) -> None:
        if self.sup.sink is not None:
            raise TelosError("seek requires a branch transition before an active recording sink")
        if self.sup.attach_mode:
            raise TelosError("seek cannot replace or bootstrap an attached external process")

    def check_image(self, T: int, want: PageMap) -> None:
        """Resolve every page of map(T), hash-verified, before a path that abandons the live process.

        Remembered per session for this exact store object, chain link and eviction state."""
        store = self.b.store
        stamp, bundle = [], self.b
        while bundle is not None:
            stamp.append(bundle.store.generation)
            bundle = bundle.parent
        key = (T, self.b.link_at(T), tuple(stamp))
        done = self.session.checked.setdefault(store, set())
        if key in done:
            return
        with _phase(self.log, "preflight_image"):
            for ref in want.refs():
                if ref.key != ZERO_PAGE_KEY:
                    self.b.page(ref.key)
        if len(done) >= _MAP_CACHE_ENTRIES:
            done.clear()
        done.add(key)

    @_profiled("preflight_restore")
    def preflight_restore(self, T: int, at: dict[int, Cursor], mode: SeekMode = SeekMode.RESTORE) -> None:
        """Check target bytes and origin metadata before abandoning or mutating the live process.

        A verified live process that needs no boot and no warm-up keeps running: its restore writes
        only W, and write_set resolves (hash-verified) every page of W before any backend call. Every
        other path (boot, cold or unverified live map, semantic, warm-up) checks the full image first."""
        self.require_restore_dll()
        want = self.map_at(T)
        if want is None:
            raise _Mismatch(T, "map(T) is not resolvable from the store")
        boot, L = self.needs_boot(T), self.live
        if (boot or mode is SeekMode.SEMANTIC or not L.ok or not len(L.map) or L.via == "semantic"
                or self.warm_target(T) is not None):
            self.check_image(T, want)
        elif _profiling():
            self.log(f"seek-preflight delta tick {T}: full-image read skipped on a verified live process; "
                     "W's pages are fetched and hash-verified in write_set before native mutation")
        target = self.rec(T)
        if target.flags & TickFlag.FILES:
            self.check_files(target.files_root)
        if boot:
            self.check_lifecycle()
            origin = self.epoch_start(T)
            if origin not in at:
                raise TelosError(f"epoch {target.epoch} origin {origin} has no score cursor")
            rec = self.rec(origin)
            if rec.flags & TickFlag.FILES:
                self.check_files(rec.files_root)
            elif rec.epoch and self.sup.name in PROCESS_BACKENDS:
                raise TelosError(f"epoch {rec.epoch} start has no recorded files overlay")

    def reboot(self, T: int | None = None, at: dict[int, Cursor] | None = None) -> None:
        """Start the recorded epoch with its files overlay, without replaying earlier epochs."""
        sup = self.sup
        if self._native_restore:
            self.require_restore_dll()
        self.check_lifecycle()
        origin = self.first_tick() if T is None else self.epoch_start(T)
        rec = self.rec(origin)
        files = None
        files_root = rec.files_root if rec.flags & TickFlag.FILES else NULL_DIGEST
        if files_root == NULL_DIGEST and rec.epoch and sup.name not in PROCESS_BACKENDS:
            # In-process relaunch retains the previous epoch's overlay. PySim has
            # epoch snapshots even when per-tick file tracking is disabled.
            prev = next((ep for ep in recorded_epochs(self.b) if ep["epoch"] == rec.epoch - 1), None)
            if prev is not None:
                files_root = bytes.fromhex(prev["files_root"])
        if files_root != NULL_DIGEST:
            self.check_files(files_root)
            entries = [[p, h, n, vt, base64.b64encode(self.b.file(bytes.fromhex(h))).decode()]
                       for p, h, n, vt in self.b.file_tree(files_root)]
            files = canonical_json({"entries": entries})
        elif rec.epoch:
            raise _Mismatch(origin, "epoch start has no recorded files overlay")
        if at is not None and origin not in at:
            raise TelosError(f"epoch {rec.epoch} origin {origin} has no score cursor")
        if sup.backend is not None:
            sup.close()
        cur = at.get(origin) if at is not None else None
        sup.carry, sup.next_save_ms = [], cur.next_save_ms if cur is not None else None
        sup.hz, sup.scale = (cur.hz, cur.scale) if cur is not None else (self.base_hz, self.base_scale)
        self.session.live = hv.Live()
        self.session.played = set()
        sup.save_ms = int(self.save_ms) if self.save_ms is not None else None
        # Process boot uses its launch pin. The recorded start tree is the tree
        # *after* initialization (including rewritten INI files), so put it back
        # at the held boot gate, never feed it into initialization itself.
        native = sup.name in PROCESS_BACKENDS
        rep = sup.open(origin_tick=origin, origin_epoch=rec.epoch, files=None if native else files)
        if self._native_restore:
            self.require_restore_dll()
        if rec.epoch and not native and not hasattr(sup.backend, "set_epoch_origin"):
            raise TelosError(f"backend {sup.name} cannot bootstrap a recorded epoch")
        if native and files is not None:
            self.put_files(origin)
        self.live.via = "boot"
        self.absorb(rep, strict=False)
        if rec.epoch:
            sup.save_ms = int(self.save_ms) if self.save_ms is not None else None
            sup._resave()

    def flags(self) -> dict[int, tuple[int, int]]:
        """(epoch, flags) of every recorded tick on the bundle's ancestry, from the index rows."""
        if self._rows is None:
            out: dict[int, tuple[int, int]] = {}
            b, upto = self.b, None
            while b is not None:
                for r in b.rows:
                    if (upto is None or r.tick <= upto) and r.tick not in out:
                        out[r.tick] = (r.epoch, r.flags)
                if b.header.parent is not None:
                    upto = min(upto, b.header.parent.tick) if upto is not None else b.header.parent.tick
                b = b.parent
            self._rows = out
        return self._rows

    def warm_target(self, K: int) -> int | None:
        """The tick the live process plays up to before a restore to K under seek.restore_warmup = "loads".

        A restore is complete without it: the pages, the files and (gl_sync) the driver's GL objects and
        context state all come back. "loads" is for diagnosing a restore that is not: the process first
        plays, checked like replay, up to the tick after its first tick and after the last load-screen tick
        (HOLD) at or before K, never past K, so state those ticks build outside the sim set is in place.
        None: nothing to play.
        """
        if self.cfg["seek"]["restore_warmup"] != "loads" or self.rec(K) is None:
            return None
        first, epoch = self.epoch_start(K), self.rec(K).epoch
        if K <= first:
            return None
        holds = [t for t, (ep, fl) in self.flags().items() if ep == epoch and fl & TickFlag.HOLD and first < t <= K]
        need = {first + 1, *holds}
        if need <= self.session.played:
            return None
        return min(max(need) + 1, K)

    def warm(self, K: int, at: dict[int, Cursor], out: dict[str, Any]) -> None:
        target = self.warm_target(K)
        if target is None:
            return
        L = self.live
        if not (L.ok and L.tick <= target and L.tick in at and self.rec(L.tick) is not None
                and self.rec(L.tick).epoch == self.sup.epoch):
            self.roots = False
            self.reboot(K, at)
            L = self.live
            if not L.ok:
                self.compare(L.tick, L.journal, None, True)
        self.roots = self.cfg["replay"]["require_verify_level"] == "full_root"
        self.drive(at[L.tick], target)
        L.via = "replay"
        out["warm"] = target

    @_profiled("write_set")
    def write_set(self, want: PageMap, t: int) -> list[StagingRecord]:
        """W: pages where the live map differs from want, plus decommits. After a semantic write the live
        map is stale, so every page of want is written."""
        live, lt = self.live.map, t - self.sup.offset
        full = self.live.via == "semantic"
        out = []
        try:
            for r in want.refs():
                if full or live.get(r.vaddr) != r:
                    data = bytes(PAGE_SIZE) if r.key == ZERO_PAGE_KEY else self.b.page(r.key)
                    out.append(StagingRecord.page(lt, r.vaddr, data, MemClass(r.mclass), r.prot))
        except (KeyError, FormatError, OSError) as e:    # MissingObject/EvictedError are KeyErrors
            raise _Mismatch(t, f"store: {e}") from None
        pages = len(out)
        out += [StagingRecord.decommit(lt, h.vaddr, MemClass(h.mclass)) for h in live.refs()
                if want.get(h.vaddr) is None]
        if _profiling():
            self.log(f"seek-write_set tick {t}: {pages} pages fetched and hash-verified, "
                     f"{len(out) - pages} decommits{' (full write)' if full else ''}")
        return sorted(out, key=lambda r: r.vaddr)

    @_profiled("put_files")
    def put_files(self, t: int) -> None:
        """Write T's files tree back before its pages, for a backend that tracks the game's files."""
        rec, put = self.rec(t), getattr(self.sup.backend, "put_files", None)
        if rec is None or not rec.flags & TickFlag.FILES:
            return
        if put is None:
            raise _Mismatch(t, f"backend {self.sup.name} cannot restore the recorded files overlay")
        try:
            self.check_files(rec.files_root)
            rows = self.b.file_tree(rec.files_root)
            put(rows, lambda h: self.b.file(bytes.fromhex(h)))
        except (KeyError, FormatError, BackendError, OSError) as e:
            raise _Mismatch(t, f"files tree: {e}") from None

    @_profiled("admit")
    def admit(self) -> None:
        """A tas restore reaches only roots the process captured itself or was handed once (contract 4.1):
        hand it this bundle's recorded roots, the first time this process restores."""
        self.require_restore_dll()
        b = self.sup.backend
        if self.sup.tier is Tier.LAB or getattr(b, "admitted", True) is not None:
            return
        if not getattr(b, "can_admit", lambda: False)():
            return
        roots = self.session.roots.get(self.b.bundle_id)
        if roots is None:
            roots = self.session.roots[self.b.bundle_id] = hv.recorded_roots(self.b)
        b.admit_roots(roots)

    @_profiled("restore_to")
    def restore_to(self, t: int, mode: SeekMode = SeekMode.RESTORE) -> int:
        self.require_restore_dll()
        caps = self.sup.caps
        if caps is not None and (mode not in caps.seek_modes or caps.tier_ceiling < self.sup.tier):
            raise _Mismatch(t, "attached backend cannot satisfy restore mode or tier")
        want = self.map_at(t)
        if want is None:
            raise _Mismatch(t, "map(T) is not resolvable from the store")
        sup = self.sup
        root = want.root()
        W = self.write_set(want, t)   # pure read: every page of W is hash-verified before any backend call
        self.admit()
        check = getattr(sup.backend, "restore_admitted", None)
        if check is not None and not check(t - sup.offset, root):    # before T's files go down
            raise _Mismatch(t, "restore target not admitted",
                            [f"restore to tick {t}: root {root.hex()[:16]} is neither one this process captured "
                             "nor in the roots it admitted"])
        self.put_files(t)
        with _phase(self.log, "backend.restore"):
            rc = sup.backend.restore(t - sup.offset, root, W)
        self.live.restored(t, W)
        if rc.result != "pass" or rc.state_root != want.root().hex():
            self.live.ok = False
            raise _Mismatch(t, "state_root after restore",
                            [f"restore to tick {t}: backend root {rc.state_root or '-'}, recorded {want.root().hex()}"])
        sup.tick, sup.carry, sup.alive = t, [], True
        cur = self.place(t + 1).get(t)
        sup.next_save_ms = cur.next_save_ms if cur is not None else None
        sup.last_osev = cur.last_osev if cur is not None else None
        if cur is not None:
            sup.hz, sup.scale = cur.hz, cur.scale
        gl = getattr(sup.backend, "gl_sync", None)
        if gl is not None:
            try:
                with _phase(self.log, "gl_sync"):
                    res = gl(self.b.file)
            except (KeyError, FormatError, BackendError) as e:
                self.live.ok = False
                raise _Mismatch(t, f"GL objects after restore: {e}") from None
            if (res.get("ok") is not True or "error" in res
                    or (rc.verify_level is VerifyLevel.NONE and
                        (res.get("verified") is not True or res.get("external_pending") is not False
                         or res.get("poisoned") is not False))):
                self.live.ok = False
                raise _Mismatch(t, "GL objects after restore", [f"gl.apply at tick {t}: {res}"])
        elif rc.verify_level is VerifyLevel.NONE:
            self.live.ok = False
            raise _Mismatch(t, "memory-stage restore has no external reconstruction completion")
        self.live.map, self.live.ok = PageMap(want.refs()), True
        return len(W)

    def journal_fields(self, t: int) -> list[str]:
        """Read back T's journalled scalar fields the backend can read (restore's journal check)."""
        want = hv.journal_of(self.rec(t)) if self.rec(t) is not None else None
        if not want:
            return []
        try:
            have = self.sup.backend.semantic("read", [])
        except BackendError:
            return []
        return [f"journal field {k}: recorded {v!r}, live {have[k]!r}" for k, v in sorted(want.items())
                if k in have and isinstance(v, int) and have[k] != v]

    # ------------------------------------------------------------ driving the score

    @_profiled("drive")
    def drive(self, cur: Cursor, target: int) -> None:
        """Run score items from `cur` until the live tick reaches `target`, checking every tick."""
        sup = self.sup
        start = sup.tick
        sup.hz, sup.scale = cur.hz, cur.scale
        if self.live.via in ("restore", "semantic"):
            sup.carry = [EngineEvent(e, sup.tick, a) for e, a in cur.carry]
        pending = cur.pending
        sup.save_ms = int(self.save_ms) if self.save_ms is not None else None   # a relaunch sends it again
        if self.save_ms is not None and (cur.i, cur.j) == (0, 0) and sup.tick < target:
            sup.save_length(int(self.save_ms), once=False)     # as run_score does right after the boot

        def hit(evs: Any, it: Any) -> bool:
            return any(e.name == it.event and (it.arg is None or e.arg == it.arg) for e in evs)

        try:
            for i in range(cur.i, len(self.items)):
                if sup.tick >= target:
                    break
                it = self.items[i]
                if isinstance(it, Segment):
                    sup.set_clock(it.hz or self.base_hz, it.scale or self.base_scale)
                elif isinstance(it, Note):
                    if it.count == 0:
                        pending = merge_oneshots(pending, it)
                        continue
                    note, pending = merge_oneshots(pending, it), None
                    n, resumed = it.count, i == cur.i and cur.j > 0
                    if resumed:
                        note, n = _held(note), n - cur.j
                    self.absorb(sup.step(min(n, target - sup.tick), note, keep_carry=resumed))
                elif isinstance(it, Sync):
                    if hit(sup.carry, it):
                        sup.carry = []
                        continue
                    rep = sup.wait(it.event, it.arg, min(it.timeout or self.timeout, target - sup.tick))
                    self.absorb(rep)
                    sup.carry = []
                    if not hit(rep.events, it) and sup.tick < target:
                        raise _Mismatch(sup.tick, f"@sync {it.event} timed out")
                elif isinstance(it, Expect):
                    want = set(it.outcome.split("|"))
                    if sup.alive and "exit" in want:
                        self.absorb(sup.wait("proc.down", None, min(it.timeout or self.timeout, target - sup.tick)))
                    if sup.alive and sup.tick >= target:
                        break
                    if not sup.alive:
                        if "exit" not in want:
                            raise _Mismatch(sup.tick, "the game exited where the score expects it alive")
                        if not sup.epoch_closed:
                            if sup.name in PROCESS_BACKENDS:
                                sup.close()
                            else:
                                sup.epoch_closed = True
                    elif "alive" not in want:
                        raise _Mismatch(sup.tick, "the game stayed alive where the score expects an exit")
                elif isinstance(it, Relaunch):
                    if it.cond == "exit" and sup.alive:
                        continue
                    rep = sup.relaunch("relaunch")
                    self.absorb(rep)
                elif isinstance(it, SaveMs):
                    sup.save_length(it.ms, once=True)
        finally:
            self.replayed += max(0, self.live.tick - start)
        if sup.tick != target:
            raise _Mismatch(sup.tick, f"the score stops at tick {sup.tick}, not {target}")

    # ------------------------------------------------------------ modes

    def incompatible(self, mode: SeekMode, T: int, at: dict[int, Cursor]) -> str | None:
        sup, want = self.sup, self.rec(T)
        caps = sup.caps if sup.backend is not None else None
        if caps is not None and mode not in caps.seek_modes:
            return f"backend {sup.name} cannot {mode}"
        if mode is SeekMode.REPLAY:
            if "boot" not in self.cfg["replay"]["origins"]:
                return "replay.origins has no boot origin"
            try:
                origin = self.epoch_start(T)
            except (TelosError, FormatError) as exc:
                return f"replay cannot establish a valid epoch origin: {exc}"
            # Replay starts at boot for this backend; an attempted terminal input
            # in any earlier epoch is still part of the path to T and cannot be
            # substituted with a verified effect from a later epoch.
            terminal = next((tick for tick in sorted(terminal_input_attempt_ticks(self.b))
                             if tick <= T), None)
            if terminal is not None:
                return (f"replay refused at tick {terminal}: the recording preserves an attempted input "
                        "whose process exited before a gate; guest consumption and resulting state are unproven")
            for tick, (epoch, _flags) in sorted(self.flags().items()):
                if epoch != want.epoch or tick < origin or tick > T:
                    continue
                rec = self.rec(tick)
                if rec is None:
                    return f"replay tick {tick} is missing from the recording"
                try:
                    hv.require_journal_complete(rec.journal)
                except FormatError as exc:
                    return f"replay refused at tick {tick}: {exc}"
            return None
        if self.cfg["threads"]["model"] == "free":
            return "threads.model free allows replay only"
        if self.cfg["tick"]["context"] == "inline":
            return "tick.context inline allows replay only"
        if self.cfg["store"]["pages"] == "digests":
            return "store.pages digests keeps no pages to restore"
        if mode is SeekMode.SEMANTIC and want.epoch != sup.epoch:
            return f"tick {T} is in epoch {want.epoch} and a restore never crosses an EPOC"
        if mode is SeekMode.RESTORE:
            if not want.flags & TickFlag.CAPTURED:
                return f"tick {T} was not captured"
            if not want.flags & TickFlag.RESTORE_POINT:
                return f"tick {T} is not a restore point ({want.reason.name.lower()})"
            return None if self.map_at(T) is not None else f"map({T}) is not in the store"
        if mode is SeekMode.SEMANTIC and (hv.journal_of(want) is None or not want.flags & TickFlag.CAPTURED):
            return f"tick {T} keeps no semantic journal or was not captured"
        if mode is SeekMode.SEMANTIC and self.map_at(T) is None:
            return f"map({T}) is not in the store"
        if self.anchor(T, at, need_cursor=mode is SeekMode.HYBRID) is None:
            return f"no restore point at or before tick {T} in epoch {want.epoch}"
        return None

    def anchor(self, T: int, at: dict[int, Cursor], need_cursor: bool) -> int | None:
        """K: the latest restore point <= T in T's epoch whose map resolves (and whose score position is known).
        With need_cursor (hybrid), a keyframe is preferred: hybrid restores the nearest keyframe and replays
        from there, even where every tick is a restore point. A tick where a restore already failed in this
        seek is never chosen again: a restore there would be the same attempt."""
        epoch = self.rec(T).epoch
        fallback = None
        for k in range(T, self.first_tick() - 1, -1):
            r = self.rec(k)
            if r is None or r.epoch != epoch:
                break
            if k in self.failed:
                continue
            if r.flags & TickFlag.CAPTURED and r.flags & TickFlag.RESTORE_POINT and (k in at or not need_cursor):
                if not need_cursor or r.flags & TickFlag.KEYFRAME:
                    if self.map_at(k) is not None:
                        return k
                elif fallback is None and self.map_at(k) is not None:
                    fallback = k
        return fallback

    def attempt(self, mode: SeekMode, T: int, at: dict[int, Cursor], out: dict[str, Any]) -> None:
        self._native_restore = mode is not SeekMode.REPLAY
        if self._native_restore:
            self.require_restore_dll()
        if mode is SeekMode.REPLAY:
            L = self.live
            if not (L.ok and L.via in ("boot", "replay") and L.tick <= T and L.tick in at
                    and self.rec(L.tick) is not None and self.rec(L.tick).epoch == self.sup.epoch
                    and self.sup.epoch == self.rec(T).epoch):
                self.roots = False
                self.reboot(T, at)
                L = self.live
                if not L.ok:
                    self.compare(L.tick, L.journal, None, True)
            out["origin"] = f"{L.via}@{L.tick}"
            self.roots = self.cfg["replay"]["require_verify_level"] == "full_root"
            self.drive(at[L.tick], T)
            L.via = "replay"
            return
        K = T if mode is SeekMode.RESTORE else self.anchor(T, at, need_cursor=mode is SeekMode.HYBRID)
        if K is None:
            raise _Mismatch(T, f"no restore point left to anchor at (failed at {sorted(self.failed)})")
        self.preflight_restore(K, at, mode)
        if self.needs_boot(K):
            want = self.map_at(K)        # the process may have died since preflight: guard every reboot
            if want is None:
                raise _Mismatch(K, "map(T) is not resolvable from the store")
            self.check_image(K, want)    # a cached hit when preflight already checked this image
            self.reboot(K, at)
        # Replacement backends may advertise different effective capabilities.
        if self.sup.caps is not None:
            if mode not in self.sup.caps.seek_modes:
                raise _Mismatch(K, f"backend {self.sup.name} cannot {mode} after attach")
            if self.sup.caps.tier_ceiling < self.sup.tier:
                raise _Mismatch(K, "attached backend cannot satisfy the requested tier")
        self.warm(K, at, out)
        try:
            out["W"] += self.restore_to(K, mode)  # always: the restore call is also the root check
            out["origin"] = (f"replay@{out['warm']} > " if out.get("warm") is not None else "") + f"restore@{K}"
            if K == T and mode is not SeekMode.SEMANTIC:
                # restore, or hybrid with nothing to replay: both are a restore at T and get its checks
                bad = self.journal_fields(T)
                if bad:
                    raise _Mismatch(T, "journal", bad)
                self.canary(T, at, out, mode)
        except (_Mismatch, BackendError, RunError):
            self.failed.add(K)
            raise
        if mode is SeekMode.HYBRID:
            self.roots = True
            self.drive(at[K], T)
            want = self.rec(T)
            if not self.live.captured:
                # the replay's last step captures its last tick; without it there is no state to check
                raise _Mismatch(T, "the live gate at T was not captured")
            if want.flags & TickFlag.CAPTURED and self.live.map.root() != want.state_root:
                raise _Mismatch(T, "state_root")
            self.live.via = "restore"
        elif mode is SeekMode.SEMANTIC:
            self.semantic(T, out)

    def semantic(self, T: int, out: dict[str, Any]) -> None:
        sup, want = self.sup, hv.journal_of(self.rec(T)) or {}
        have = sup.backend.semantic("read", [])
        write = {k: v for k, v in want.items() if k in have and type(v) is int and have[k] != v}
        if write:
            sup.backend.semantic("apply", write)
            self.live.via, self.live.ok = "semantic", False
        sup.tick = self.live.tick = T
        wm = self.map_at(T)
        rc = sup.backend.restore(T - sup.offset, wm.root(), [])   # empty W: verify only (diff_only)
        bad = self.journal_fields(T)
        if rc.result != "pass" or bad:
            raise _Mismatch(T, "state after semantic write-back",
                            [f"semantic wrote {sorted(write) or 'nothing'}; live root {rc.state_root}, "
                             f"recorded {wm.root().hex()}", *bad])
        self.live.map, self.live.ok, self.live.via = PageMap(wm.refs()), True, "restore"

    def canary(self, T: int, at: dict[int, Cursor], out: dict[str, Any],
               mode: SeekMode = SeekMode.RESTORE) -> None:
        """Continuation canary: run on from T against the recording, then restore T again."""
        v = self.cfg["verify"]
        every = max(1, v["canary_every_seeks"])
        if self.session.seeks % every != 1 % every or T not in at:
            return
        epoch, k, last = self.rec(T).epoch, v["canary_ticks"], T
        bound = v["continuation_ticks"]
        while last - T < k and (r := self.rec(last + 1)) is not None and r.epoch == epoch:
            last += 1
            if bound == "to_next_keyframe" and r.flags & TickFlag.KEYFRAME:
                break
            if isinstance(bound, int) and last - T >= bound:
                break
        next_events = recorded_events(self.b).get(last + 1, ())
        if any(name == "input.attempted_exit" for name, _arg in next_events):
            out["canary"] = "pending"
            self.log(f"continuation from tick {last} reaches an exited-before-gate input attempt at {last + 1}; "
                     "the intent is durable, but replay cannot verify guest consumption or a resulting state")
            return
        expects_exit = any(name == "proc.down" for name, _arg in next_events)
        if last == T and not expects_exit:
            return
        out["canary"] = "fail"
        self.roots = True
        before = self.replayed
        from .canary_frames import CanaryFrames
        evidence=CanaryFrames(self.b,T,last)
        previous=getattr(self.sup,'canary_frame_evidence',None)
        self.sup.canary_frame_evidence=evidence.retain
        forced=getattr(self.sup,'force_capture_every_tick',False)
        self.sup.force_capture_every_tick=True   # capture cadence: compare the root at every canary tick
        try:
            self.drive(at[T], last)
        except Exception as failure:
            try:
                path=evidence.persist(failure)
                self.log(f'failed canary framebuffer evidence: {path}')
            except Exception as error:
                self.log(f'failed canary framebuffer evidence not persisted: {type(error).__name__}: {error}')
            raise
        finally:
            self.sup.canary_frame_evidence=previous
            self.sup.force_capture_every_tick=forced
        if expects_exit and self.sup.alive:
            self.absorb(self.sup.wait("proc.down", None, 1))
            if self.sup.tick != last or self.live.tick != last:
                raise _Mismatch(self.sup.tick, "unexpected completed gate before recorded process exit")
            if self.sup.alive:
                raise _Mismatch(last + 1, "recorded process exit did not occur after restore")
        self.replayed = before
        terminal = max(t for t, (ep, _fl) in self.flags().items() if ep == epoch)
        if not self.sup.alive or (last == terminal and
                not self.reusable_live_eof(last, epoch, expects_exit)):
            # Quit processing can destroy native windows before proc.down becomes visible.
            # Never rewind that process: recreate the epoch before restoring the target.
            want = self.map_at(T)
            if want is None:
                raise _Mismatch(T, "map(T) is not resolvable from the store")
            self.check_image(T, want)    # the boot path keeps its full-image check before the reboot
            self.reboot(T, at)
        self.restore_to(T, mode)
        out["canary"] = "pass"

    def reusable_live_eof(self, last: int, epoch: int, expects_exit: bool) -> bool:
        """Only reuse a verified ordinary live EOF with a native held-gate proof.

        Recording exhaustion alone is not process teardown. Missing lifecycle,
        journal or native-gate evidence keeps the conservative reboot path.
        """
        sup, live = self.sup, self.live
        backend = sup.backend
        rec = self.rec(last)
        # Bundle.sealed is its SEAL mapping, or None for an unsealed journal.
        # An absent property is unknown, not proof of an unsealed recording.
        missing_seal = object()
        sealed = getattr(self.b, 'sealed', missing_seal) is not None
        context = self._live_eof
        owned_seal = sealed and context is not None and context.consume(self.b, sup, last, epoch)
        if (expects_exit or (sealed and not owned_seal)
                or sup.name != 'shim' or sup.alive is not True
                or backend is None or getattr(backend, 'poisoned', True) is not False
                or sup.epoch != epoch or sup.tick != last or live.tick != last or not live.ok
                or getattr(sup, 'last_osev', 'unknown') not in (None, 'focus', 'blur')
                or rec is None or rec.epoch != epoch
                or not rec.flags & TickFlag.CAPTURED or not rec.flags & TickFlag.RESTORE_POINT
                or live.map.root() != rec.state_root):
            return False
        if any(ep['epoch'] == epoch for ep in recorded_epochs(self.b)):
            return False
        events = recorded_events(self.b)
        if any(name in {'proc.down', 'close'} for tick in (last, last + 1)
               for name, _arg in events.get(tick, ())):
            return False
        journal = hv.journal_of(rec)
        if not journal or type(journal.get('trips')) is not int or journal['trips'] != 0:
            return False
        probe = getattr(backend, 'input_state', None)
        if not callable(probe):
            return False
        try:
            state = probe()
        except (BackendError, OSError):
            return False
        reusable = ((not owned_seal or context.matches(self.b, sup, last, epoch))
                and isinstance(state, Mapping) and state.get('source') == 'virtual_devices'
                and state.get('sampled_at_gate') is True and type(state.get('tick')) is int
                and state['tick'] + sup.offset == last)
        if reusable and owned_seal:
            self.log(f"seek reuses owner-held native EOF at tick {last} after sealing its live take")
        return reusable


def _level(mode: SeekMode, cfg: Mapping[str, Any]) -> VerifyLevel:
    """Replay checks journals (full roots only when replay.require_verify_level asks; sim_root needs a
    mask registry nothing computes yet). Restore and hybrid check full roots; semantic reports a diff."""
    if mode is SeekMode.REPLAY:
        full = cfg["replay"]["require_verify_level"] == "full_root"
        return VerifyLevel.FULL_ROOT if full else VerifyLevel.JOURNAL
    return VerifyLevel.DIFF_ONLY if mode is SeekMode.SEMANTIC else VerifyLevel.FULL_ROOT


def _append(b: Bundle, r: Receipt, log: Callable[[str], None]) -> None:
    try:
        with _phase(log, "append"):
            b.append_seek_receipt(r)
        return
    except (OSError, FormatError) as e:
        log(f"seek receipt not appended to {b.root}: {e}")


def seek(b: Bundle, tick: int, sup: Any, *, mode: SeekMode | str | None = None,
         cfg: Mapping[str, Any] | None = None, log: Callable[[str], None] | None = None,
         _live_eof: _LiveEOFContext | None = None) -> Receipt:
    """Bring `sup`'s live backend to recorded tick `tick` of bundle `b`; return (and append) the receipt.

    The receipt's mode is the mode actually used, `fallback` the path taken, `verifier` the origin and
    the number of ticks replayed, and `result` whether the reached state verified.
    """
    from kptas.tier.envpin import pin_env   # lazy: kptas.tier imports kptas.holo
    cfg = cfg or sup.cfg
    requested = SeekMode.parse(mode or cfg["seek"]["mode"])
    if requested is not SeekMode.REPLAY:
        from kptas.tier.envpin import require_native_restore_dll
        bundle = b
        while bundle is not None:
            require_native_restore_dll(bundle.env, cfg, sup.backend)
            bundle = bundle.parent
    log = log or (lambda s: print(f"telos: {s}", file=sys.stderr, flush=True))
    with _phase(log, "pin_env"):
        env_key = pin_env(cfg).key()           # the live environment, not the bundle's pin
    if env_key != b.env.key():
        if cfg["env"]["guard"] == "refuse":
            raise TelosError("the environment differs from the recording's pin (env.guard is refuse)")
        log("the environment differs from the recording's pin; env.guard is warn")
    tier = Tier.parse(cfg["run"]["tier"])
    if not requested.allowed(tier):
        raise TelosError(f"seek mode {requested} is not allowed in tier {tier}")
    if sup.sink is not None:
        raise TelosError("seek requires a branch transition before an active recording sink")
    with _phase(log, "planner"):
        s = _Seek(b, sup, cfg, log)
    if type(_live_eof) is _LiveEOFContext:
        s._live_eof = _live_eof.claim(b, sup)
    if s.rec(tick) is None:
        raise TelosError(f"{b.root.name} has no tick {tick}")
    t0 = time.perf_counter_ns()
    hot0 = hot_stats()
    s.session.seeks += 1
    if s.live.via == "none" and sup.backend is not None:
        s.live.via = "boot"
        s.absorb(None, strict=False)
    elif sup.backend is not None and s.live.tick != sup.tick:
        # Playback outside seek() leaves captured deltas queued. Fold them before
        # computing W; the old seek map no longer describes the live process.
        s.absorb(None, strict=False)
    from_tick = sup.tick
    # place past T: a @sync met at T from the events before it adds its carry to at[T] only when placement
    # goes on beyond T, and the canary after a restore at T (restore, or hybrid anchored at T) needs it
    at = s.place(tick + max(1, cfg["verify"]["canary_ticks"]))
    # Ownership refusals apply to the operation, not just one fallback mode.
    if s.needs_boot(tick) and sup.attach_mode:
        raise TelosError("seek cannot replace or bootstrap an attached external process")
    if requested is SeekMode.SEMANTIC and sup.backend is None and s.rec(tick).epoch == 0:
        sup.open()
        s.require_restore_dll()
        s.live.via = "boot"
        s.absorb(None, strict=False)
    order = [requested]
    first_why = s.incompatible(requested, tick, at)
    if first_why:
        sub = cfg["seek"]["on_incompatible"]
        if sub == "error":
            order = []
        else:
            order.append(SeekMode(sub))
    if not first_why or cfg["seek"]["on_incompatible"] != "error":
        order += [SeekMode.parse(m) for m in cfg["seek"]["fallback_chain"]]
    path: list[str] = []
    out: dict[str, Any] = {"W": 0, "origin": "-", "canary": "none"}
    used, fail, done, tried = requested, None, False, set()
    for m in order:
        if m in tried or not m.allowed(tier):
            continue
        tried.add(m)
        why = first_why if m is requested else s.incompatible(m, tick, at)
        if why:
            path.append(f"{m} ({why})")
            log(f"seek {tick}: {m} is not possible: {why}")
            continue
        out.update(W=0, origin="-", canary="none", warm=None)
        s.replayed, used = 0, m
        try:
            s.attempt(m, tick, at, out)
            done = True
            break
        except (_Mismatch, BackendError, RunError) as e:
            fail = e if isinstance(e, _Mismatch) else _Mismatch(sup.tick, f"{type(e).__name__}: {e}")
            s.live.ok = False
            path.append(f"{m} (failed at tick {fail.tick}: {fail.field})")
            log(f"seek {tick}: {m} failed at tick {fail.tick}: {fail.field}")
            if cfg["verify"]["on_mismatch"] == "halt":
                break
    if not done:
        fail = fail or _Mismatch(tick, first_why or "no seek mode was possible")
        log(f"seek {tick} halted after {' > '.join(path) or 'nothing'}")
        for line in fail.lines or [f"first divergence at tick {fail.tick}: {fail.field}"]:
            log(line)
    else:
        fail = None
    live = s.live
    lo = min(tick, from_tick if from_tick >= 0 else tick)
    rps = [(t, bool(r.flags & TickFlag.RESTORE_POINT)) for t in range(max(lo, s.first_tick()), tick + 1)
           if (r := s.rec(t)) is not None]
    r = Receipt(
        kind="seek", result="fail" if fail else "pass", from_tick=from_tick, to_tick=tick, mode=used,
        verify_level=(VerifyLevel.NONE if not done and used in (SeekMode.RESTORE, SeekMode.HYBRID)
                      else _level(used, cfg)), write_pages=out["W"],
        micros=(time.perf_counter_ns() - t0) // 1000,
        state_root=live.map.root().hex() if len(live.map) else "",
        journal_sha256=sha256(live.journal).hex() if live.journal and live.tick == tick else "",
        canary=out["canary"], fallback=" > ".join([*path, str(used)]) if path and done else " > ".join(path),
        first_divergence=fail.tick if fail else None, restore_points=hv.restore_ranges(rps),
        verifier=f"{sup.name}; origin {out['origin']}; replayed {s.replayed} ticks",
        env_key=env_key)
    if _profiling():
        hot1 = hot_stats()
        log(f"seek-hot tick {tick}: hits={hot1['hits'] - hot0['hits']} misses={hot1['misses'] - hot0['misses']} "
            f"objects={hot1['objects']} bytes={hot1['bytes']} budget={hot1['budget']} maps={len(s.session.maps)}")
    _append(b, r, log)
    return r


def resume_cursor(b: Bundle, tick: int, score: Any, cfg: Mapping[str, Any]) -> Cursor:
    """Where `score` resumes after recorded tick `tick` of `b`, for a branch that continues from there.

    The branch embeds `score` and the judge replays it from boot, so `score` must play exactly what `b`
    recorded through `tick`: the same header, the same items before the tick (marks and asserts aside)
    and the note spanning the tick unchanged up to it. Anything else is a TelosError naming the item.
    """
    from kptas.score import Assert, Mark
    from kptas.score.fmt import render
    own = _Seek(b, None, cfg, lambda _s: None)
    new = compile_score(score) if isinstance(score, str) else score
    if tick not in own.flags():
        raise TelosError(f"{b.root.name} has no tick {tick}")
    old = compile_score(b.score).header
    for k in sorted(set(old) | set(new.header)):
        if old.get(k) != new.header.get(k):
            raise TelosError(f"the score header has {k}={new.header.get(k)} but {b.root.name} was recorded with "
                             f"{k}={old.get(k)}")
    cur = own.place(max(tick, b.last_tick)).get(tick)
    if cur is None:
        raise TelosError(f"tick {tick} is inside a wait of {b.root.name}; continue from a tick outside it")

    def key(it: Any) -> str:
        return f"@segment {it.hz} {it.scale} {it.pace}" if isinstance(it, Segment) else render(it)

    def where(it: Any) -> str:
        return f"{getattr(it, 'source', '?')}:{getattr(it, 'line', '?')}"

    skip = (Mark, Assert)
    want = [it for it in own.items[:cur.i] if not isinstance(it, skip)]
    i = 0
    for w in want:
        while i < len(new.items) and isinstance(new.items[i], skip):
            i += 1
        if i == len(new.items):
            raise TelosError(f"the score ends before tick {tick}; `{render(w)}` ({where(w)}) is missing")
        if key(new.items[i]) != key(w):
            raise TelosError(f"{where(new.items[i])}: `{render(new.items[i])}` differs from `{render(w)}`, which "
                             f"{b.root.name} played before tick {tick}; edits must come after the branch tick")
        i += 1
    if not cur.j:
        return dataclasses.replace(cur, i=i)
    span = own.items[cur.i]           # the note running at `tick`, cur.j of its ticks already played
    while i < len(new.items) and isinstance(new.items[i], skip):
        i += 1
    it = new.items[i] if i < len(new.items) else None
    if not (isinstance(it, Note) and it.count >= cur.j
            and render(dataclasses.replace(it, count=1)) == render(dataclasses.replace(span, count=1))):
        got = f"`{render(it)}` ({where(it)})" if it is not None else "nothing"
        raise TelosError(f"tick {tick} is tick {cur.j} of `{render(span)}` ({where(span)}), but the score has {got} "
                         f"there; edit after the note or branch at its last tick")
    return dataclasses.replace(cur, i=i + 1, j=0) if it.count == cur.j else dataclasses.replace(cur, i=i)


__all__ = ["Cursor", "recorded_events", "terminal_input_attempt_ticks", "resume_cursor", "seek"]
