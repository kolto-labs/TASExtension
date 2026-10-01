"""Seek planner (contract 5.1): bring a live backend to a recorded tick T and verify it.

  replay    all tiers  from a boot origin (or a live state reached by replay), replay the embedded score
  restore   lab, tas   write W, the pages where the live map and map(T) differ, then verify
  hybrid    lab, tas   restore the latest restore point K <= T, then replay K..T
  semantic  lab        restore K, then write back T's journalled engine fields; the branch is tainted

A requested mode that cannot apply to T (not a restore point, another epoch, no
stored map) is replaced per seek.on_incompatible. A mode whose check fails moves
down seek.fallback_chain, each level once. When the last level fails, the seek
halts: the first-divergent-field report goes to the log and the receipt says fail.
Every seek appends one RCPT to the bundle. Save origins need a backend that can
reload a save, which none has yet, so replay origins are boots.
"""

from __future__ import annotations

import dataclasses
import sys
import time
import weakref
from collections.abc import Callable, Mapping
from typing import Any

from kptas.holo import verify as hv
from kptas.holo.bundle import EVENT_MARK, Bundle
from kptas.holo.pages import PageMap
from kptas.score import Expect, Note, Relaunch, SaveMs, Segment, Sync, compile_score
from kptas.supervisor import PROCESS_BACKENDS, RunError, TelosError, merge_oneshots
from kptas.types import (NULL_DIGEST, PAGE_SIZE, ZERO_PAGE_KEY, BackendError, EngineEvent, FormatError, MemClass,
                         Receipt, SeekMode, StagingRecord, Tier, TickFlag, TickRecord, VerbClass, VerifyLevel, sha256)

_SESSIONS: "weakref.WeakKeyDictionary[Any, _Session]" = weakref.WeakKeyDictionary()


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


@dataclasses.dataclass
class _Session:
    live: hv.Live
    seeks: int = 0
    played: set[int] = dataclasses.field(default_factory=set)   # ticks this process ran itself, since its boot


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
        upto = b.header.parent.tick if b.header.parent is not None else None
        b = b.parent
    return out


class _Seek:
    def __init__(self, b: Bundle, sup: Any, cfg: Mapping[str, Any], log: Callable[[str], None]):
        self.b, self.sup, self.cfg, self.log = b, sup, cfg, log
        self._recs: dict[int, TickRecord | None] = {}
        self._rows: dict[int, tuple[int, int]] | None = None
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

    @property
    def live(self) -> hv.Live:
        return self.session.live

    # ------------------------------------------------------------ recording

    def rec(self, t: int) -> TickRecord | None:
        if t not in self._recs:
            try:
                self._recs[t] = self.b.record(t) if t >= 0 else None
            except (KeyError, FormatError):
                self._recs[t] = None
        return self._recs[t]

    def first_tick(self) -> int:
        b = self.b
        while b.parent is not None:
            b = b.parent
        return b.ticks()[0] if b.ticks() else 0

    def map_at(self, t: int) -> PageMap | None:
        try:
            return self.b.map_at(t)
        except (FormatError, KeyError):
            return None

    def place(self, end: int) -> dict[int, Cursor]:
        """Place the score on the recorded tick line, mirroring Supervisor.run_score, up to tick `end`.

        A @sync or @expect exit consumes the recorded run of WAIT ticks; a @sync that consumed none was
        met from the previous report's events. Two waits in a row cannot be told apart, so placement
        stops there. Ticks inside a wait are not resume points. A cursor inside a note carries the
        events recorded on the note's ticks so far, which the rest of the note's report would have held.
        """
        evs = recorded_events(self.b)
        t = self.first_tick()
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
                seen: tuple[tuple[str, str | None], ...] = ()
                for j in range(1, n + 1):
                    seen += tuple(evs.get(t + j, ()))
                    if j < it.count:
                        at[t + j] = Cursor(i, j, None, hz, scale, seen)
                if n < it.count:
                    return at          # the note runs on past `end`
                pending, prev_wait = None, False
                alive = alive and it.osev not in ("altf4", "close")
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
            elif isinstance(it, Relaunch):
                if it.cond == "exit" and alive:
                    continue
                r = self.rec(t + 1)
                n = 1 if r is not None and r.flags & TickFlag.EPOCH_START else 0
                alive, prev_wait = True, False
            t += n
            if n:
                at[t] = Cursor(i + 1, 0, pending, hz, scale)
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
            jd = sha256(journal) if journal else (rep.journal_sha256 if rep is not None else NULL_DIGEST)
            if want.journal_sha256 != NULL_DIGEST and jd != want.journal_sha256:
                field = "journal_sha256"
            elif self.roots and want.flags & TickFlag.CAPTURED:
                have = live.map.root() if rep is None else rep.state_root
                if have != want.state_root:
                    field = "state_root"
        live.ok = field is None
        if field is None:
            return
        if strict:
            wm = self.map_at(t) if want is not None and want.flags & TickFlag.CAPTURED else None
            raise _Mismatch(t, field, hv.report(t, field, want, journal, wm, live.map if wm is not None else None))

    def reboot(self) -> None:
        """A fresh process (or PySim instance) at the boot origin."""
        sup = self.sup
        if sup.backend is not None:
            sup.close()
        sup.tick, sup.epoch, sup.offset, sup.carry = -1, -1, 0, []
        sup.hz, sup.scale = self.base_hz, self.base_scale
        self.session.live = hv.Live()
        self.session.played = set()
        rep = sup.open()
        self.live.via = "boot"
        self.absorb(rep, strict=False)

    def flags(self) -> dict[int, tuple[int, int]]:
        """(epoch, flags) of every recorded tick on the bundle's ancestry, from the index rows."""
        if self._rows is None:
            out: dict[int, tuple[int, int]] = {}
            b, upto = self.b, None
            while b is not None:
                for r in b.rows:
                    if (upto is None or r.tick <= upto) and r.tick not in out:
                        out[r.tick] = (r.epoch, r.flags)
                upto = b.header.parent.tick if b.header.parent is not None else None
                b = b.parent
            self._rows = out
        return self._rows

    def warm_target(self, K: int) -> int | None:
        """The tick the live process must have played up to before a restore to K (seek.restore_warmup).

        In the game a restore rewinds the sim set, but some state outside it is built by what the process
        ran: its first tick after boot, and the load screens of an area load. A restore at the boot gate,
        or into an area this process never loaded, verifies its root and then diverges on the next ticks.
        So the process first plays, checked like replay, up to the tick after its first tick and after the
        last load-screen tick (HOLD) at or before K, never past K. None: nothing to play.
        """
        if self.cfg["seek"]["restore_warmup"] != "loads" or self.rec(K) is None:
            return None
        first, epoch = self.first_tick(), self.rec(K).epoch
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
            self.reboot()
            L = self.live
            if not L.ok:
                self.compare(L.tick, L.journal, None, True)
        self.roots = self.cfg["replay"]["require_verify_level"] == "full_root"
        self.drive(at[L.tick], target)
        L.via = "replay"
        out["warm"] = target

    def write_set(self, want: PageMap, t: int) -> list[StagingRecord]:
        """W: pages where the live map differs from want, plus decommits. After a semantic write the live
        map is stale, so every page of want is written."""
        live, lt = self.live.map, t - self.sup.offset
        full = self.live.via == "semantic"
        out = []
        for r in want.refs():
            if full or live.get(r.vaddr) != r:
                data = bytes(PAGE_SIZE) if r.key == ZERO_PAGE_KEY else self.b.page(r.key)
                out.append(StagingRecord.page(lt, r.vaddr, data, MemClass(r.mclass), r.prot))
        out += [StagingRecord.decommit(lt, h.vaddr, MemClass(h.mclass)) for h in live.refs()
                if want.get(h.vaddr) is None]
        return sorted(out, key=lambda r: r.vaddr)

    def put_files(self, t: int) -> None:
        """Write T's files tree back before its pages, for a backend that tracks the game's files."""
        rec, put = self.rec(t), getattr(self.sup.backend, "put_files", None)
        if put is None or rec is None or not rec.flags & TickFlag.FILES:
            return
        try:
            rows = self.b.file_tree(rec.files_root)
            put(rows, lambda h: self.b.file(bytes.fromhex(h)))
        except (KeyError, FormatError, BackendError, OSError) as e:
            raise _Mismatch(t, f"files tree: {e}") from None

    def restore_to(self, t: int) -> int:
        want = self.map_at(t)
        if want is None:
            raise _Mismatch(t, "map(T) is not resolvable from the store")
        W = self.write_set(want, t)
        sup = self.sup
        self.put_files(t)
        rc = sup.backend.restore(t - sup.offset, want.root(), W)
        self.live.restored(t, W)
        sup.tick, sup.carry, sup.alive = t, [], True
        if rc.result != "pass" or rc.state_root != want.root().hex():
            self.live.ok = False
            raise _Mismatch(t, "state_root after restore",
                            [f"restore to tick {t}: backend root {rc.state_root or '-'}, recorded {want.root().hex()}"])
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

    def drive(self, cur: Cursor, target: int) -> None:
        """Run score items from `cur` until the live tick reaches `target`, checking every tick."""
        sup = self.sup
        start = sup.tick
        sup.hz, sup.scale = cur.hz, cur.scale
        if self.live.via in ("restore", "semantic"):
            sup.carry = [EngineEvent(e, sup.tick, a) for e, a in cur.carry]
        pending = cur.pending
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
        caps, sup, want = self.sup.caps, self.sup, self.rec(T)
        if caps is not None and mode not in caps.seek_modes:
            return f"backend {sup.name} cannot {mode}"
        if mode is SeekMode.REPLAY:
            return None if "boot" in self.cfg["replay"]["origins"] else "replay.origins has no boot origin"
        if self.cfg["threads"]["model"] == "free":
            return "threads.model free allows replay only"
        if self.cfg["tick"]["context"] == "inline":
            return "tick.context inline allows replay only"
        if self.cfg["store"]["pages"] == "digests":
            return "store.pages digests keeps no pages to restore"
        if want.epoch != sup.epoch:
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
        from there, even where every tick is a restore point."""
        epoch = self.rec(T).epoch
        fallback = None
        for k in range(T, self.first_tick() - 1, -1):
            r = self.rec(k)
            if r is None or r.epoch != epoch:
                break
            if r.flags & TickFlag.CAPTURED and r.flags & TickFlag.RESTORE_POINT and (k in at or not need_cursor):
                if not need_cursor or r.flags & TickFlag.KEYFRAME:
                    if self.map_at(k) is not None:
                        return k
                elif fallback is None and self.map_at(k) is not None:
                    fallback = k
        return fallback

    def attempt(self, mode: SeekMode, T: int, at: dict[int, Cursor], out: dict[str, Any]) -> None:
        if mode is SeekMode.REPLAY:
            L = self.live
            if not (L.ok and L.via in ("boot", "replay") and L.tick <= T and L.tick in at
                    and self.rec(L.tick) is not None and self.rec(L.tick).epoch == self.sup.epoch):
                self.roots = False
                self.reboot()
                L = self.live
                if not L.ok:
                    self.compare(L.tick, L.journal, None, True)
            out["origin"] = f"{L.via}@{L.tick}"
            self.roots = self.cfg["replay"]["require_verify_level"] == "full_root"
            self.drive(at[L.tick], T)
            L.via = "replay"
            return
        K = T if mode is SeekMode.RESTORE else self.anchor(T, at, need_cursor=mode is SeekMode.HYBRID)
        self.warm(K, at, out)
        out["W"] += self.restore_to(K)          # always: the restore call is also the root check
        out["origin"] = (f"replay@{out['warm']} > " if out.get("warm") is not None else "") + f"restore@{K}"
        if mode is SeekMode.RESTORE:
            bad = self.journal_fields(T)
            if bad:
                raise _Mismatch(T, "journal", bad)
            self.canary(T, at, out)
        elif mode is SeekMode.HYBRID:
            self.roots = True
            self.drive(at[K], T)
            want = self.rec(T)
            if want.flags & TickFlag.CAPTURED and self.live.map.root() != want.state_root:
                raise _Mismatch(T, "state_root")
            self.live.via = "restore"
            if K == T:
                self.canary(T, at, out)     # nothing replayed: this is a restore, so it gets the restore's check
        else:
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

    def canary(self, T: int, at: dict[int, Cursor], out: dict[str, Any]) -> None:
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
        if last == T:
            return
        out["canary"] = "fail"
        self.roots = True
        before = self.replayed
        self.drive(at[T], last)
        self.replayed = before
        self.restore_to(T)
        out["canary"] = "pass"


def _level(mode: SeekMode, cfg: Mapping[str, Any]) -> VerifyLevel:
    """Replay checks journals (full roots only when replay.require_verify_level asks; sim_root needs a
    mask registry nothing computes yet). Restore and hybrid check full roots; semantic reports a diff."""
    if mode is SeekMode.REPLAY:
        full = cfg["replay"]["require_verify_level"] == "full_root"
        return VerifyLevel.FULL_ROOT if full else VerifyLevel.JOURNAL
    return VerifyLevel.DIFF_ONLY if mode is SeekMode.SEMANTIC else VerifyLevel.FULL_ROOT


def _append(b: Bundle, r: Receipt, log: Callable[[str], None]) -> None:
    try:
        if b.writable:
            b.receipt(r)
            return
        with Bundle.open(b.root, writable=True) as w:
            w.receipt(r)
    except (OSError, FormatError) as e:
        log(f"seek receipt not appended to {b.root}: {e}")


def seek(b: Bundle, tick: int, sup: Any, *, mode: SeekMode | str | None = None,
         cfg: Mapping[str, Any] | None = None, log: Callable[[str], None] | None = None) -> Receipt:
    """Bring `sup`'s live backend to recorded tick `tick` of bundle `b`; return (and append) the receipt.

    The receipt's mode is the mode actually used, `fallback` the path taken, `verifier` the origin and
    the number of ticks replayed, and `result` whether the reached state verified.
    """
    from kptas.tier.envpin import pin_env   # lazy: kptas.tier imports kptas.holo
    cfg = cfg or sup.cfg
    log = log or (lambda s: print(f"telos: {s}", file=sys.stderr, flush=True))
    env_key = pin_env(cfg).key()           # the live environment, not the bundle's pin
    requested = SeekMode.parse(mode or cfg["seek"]["mode"])
    tier = Tier.parse(cfg["run"]["tier"])
    if not requested.allowed(tier):
        raise TelosError(f"seek mode {requested} is not allowed in tier {tier}")
    if sup.backend is None:
        sup.open()
    s = _Seek(b, sup, cfg, log)
    if s.rec(tick) is None:
        raise TelosError(f"{b.root.name} has no tick {tick}")
    t0 = time.perf_counter_ns()
    s.session.seeks += 1
    if s.live.via == "none":              # first seek on this session: take in the boot the caller started
        s.live.via = "boot"
        s.absorb(None, strict=False)
    from_tick = sup.tick
    ahead = cfg["verify"]["canary_ticks"] if requested is SeekMode.RESTORE else 0
    at = s.place(tick + ahead)
    order = [requested]
    first_why = s.incompatible(requested, tick, at)
    if first_why:
        sub = cfg["seek"]["on_incompatible"]
        if sub == "error":
            order = []
        else:
            order.append(SeekMode(sub))
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
        fail = fail or _Mismatch(tick, "no seek mode was possible")
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
        verify_level=_level(used, cfg), write_pages=out["W"],
        micros=(time.perf_counter_ns() - t0) // 1000,
        state_root=live.map.root().hex() if len(live.map) else "",
        journal_sha256=sha256(live.journal).hex() if live.journal and live.tick == tick else "",
        canary=out["canary"], fallback=" > ".join([*path, str(used)]) if path and done else " > ".join(path),
        first_divergence=fail.tick if fail else None, restore_points=hv.restore_ranges(rps),
        verifier=f"{sup.name}; origin {out['origin']}; replayed {s.replayed} ticks",
        env_key=env_key)
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
    if own.rec(tick) is None:
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


__all__ = ["Cursor", "recorded_events", "resume_cursor", "seek"]
