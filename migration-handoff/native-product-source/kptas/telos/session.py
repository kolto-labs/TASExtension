"""One authoritative TAS session for live recording, archive playback, and frontends.

The session owns one Supervisor, its current recording, and serialized action admission. `rewind` seals the take, seeks the
live game back to a recorded tick (strict restore by default; replay only when explicitly configured), branches
the take there and records on into the branch. The takes form a chain in wall order; `video` stitches
their frames in that order, so the tick counter jumps back at every rewind.

Each take embeds the score authored up to its own first tick (SCOR is part of a bundle's genesis and
cannot grow). The full authored score of a take is written beside it as <take>/live.score when the take
changes, with a durable manifest naming its exact version. Read-only bundle score access validates that
version and its acknowledged boundary, so reopened controls and seeks retain the authored tail.
`telos run` can also re-record that score into a take whose embedded SCOR covers every tick.

Ops ({"id","op",...} -> {"id","ok","result"|"error"}): start {prefix?}, step {action}, inspect (alias state), render, mark {name},
rewind {tick}, takes, video {out?}, close.
"""

from __future__ import annotations

import dataclasses
import json
import hashlib
import uuid
import weakref
import os
import struct
import sys
import threading
import time
from contextlib import contextmanager
from functools import wraps
from itertools import chain, islice
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

from kptas.holo.seek import resume_cursor, seek
from kptas.score import Assert, Mark, Note, SaveMs, Segment, compile_score
from kptas.score.compile import CompiledScore
from kptas.config import unit_name
from kptas.supervisor import BundleSink, RunError, Supervisor, TelosError, session_dir
from kptas.types import BackendError, EngineEvent, FormatError, Receipt, SeekMode, Tier, VerifyLevel

DEFAULT_PORT = 47600
MANIFEST = ".session.json"


def owned_operation(method):
    """Serialize direct compatibility calls as well as dispatched commands."""
    @wraps(method)
    def call(self, *args, **kwargs):
        if not hasattr(self, "_lock"):  # Legacy harnesses construct an uninitialized controller.
            return method(self, *args, **kwargs)
        with self.operation():
            if getattr(self, 'run_loaded', False):
                if method.__name__ == 'close':
                    self._cancel_active_run()
                elif not (method.__name__ == 'step' and getattr(self, '_run_step_in_progress', False)
                          and kwargs.get('_run_prefix') is not None):
                    raise TelosError('a score is loaded; stop or unload it before changing the session')
            if (not getattr(self, 'engaged', True) and method.__name__ in
                    {'rewind', '_archive_rewind', 'rewind_mark', 'branch', 'load_recording', 'mark', 'toggle_play'}):
                raise TelosError('engage TAS before authoring or seeking')
            if method.__name__ in {'rewind', '_archive_rewind', 'branch', 'load_recording'}:
                self._engagement_resume_note = None
            preview = getattr(self, '_presentation_channel', None)
            if preview is not None:
                if method.__name__ not in ('step', '_archive_step'):
                    preview.invalidate(inputs=False)
                preview.attach()
            if self.closed and method.__name__ != "close":
                raise TelosError("the session is closed")
            return method(self, *args, **kwargs)
    return call


def _png_size(png: bytes) -> tuple[int, int]:
    if png[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    return struct.unpack(">II", png[16:24])


def receipt_dict(r: Receipt) -> dict[str, Any]:
    return {"mode": str(r.mode), "result": r.result, "canary": r.canary, "fallback": r.fallback,
            "from_tick": r.from_tick, "to_tick": r.to_tick, "verify_level": str(r.verify_level),
            "state_root": r.state_root, "write_pages": r.write_pages, "micros": r.micros,
            "first_divergence": r.first_divergence, "verifier": r.verifier, "env_key": r.env_key}


def _hybrid_fields(r: Receipt | None) -> dict[str, Any]:
    """Labels for a seek that restored an earlier tick K and replayed to its target.

    Capture cadence leaves ticks with no root; those are reached by restore@K + replay,
    never called a restore. Empty for any other receipt."""
    import re
    if r is None or not r.verifier:
        return {}
    anchor = re.search(r"\b(restore|replay)@(\d+)(?!.*\b(?:restore|replay)@\d)", r.verifier)
    replayed = re.search(r"replayed (\d+) ticks", r.verifier)
    into_branch = "into the branch" in r.verifier
    if r.mode is not SeekMode.HYBRID and not into_branch:
        return {}
    out: dict[str, Any] = {"hybrid": r.mode is SeekMode.HYBRID}
    if anchor is not None:
        out["anchor_tick"] = int(anchor.group(2))
    if replayed is not None:
        out["replayed_ticks"] = int(replayed.group(1))
    via = f"{anchor.group(1)}@{anchor.group(2)}" if anchor is not None else "restore"
    out["label"] = (f"rewound via {via} + replay" if r.mode is SeekMode.HYBRID
                    else f"rewound via {via} + re-record")
    return out


class _ScoreView:
    """A bundle read through another score (placement only; never written)."""
    def __init__(self, bundle, score):
        self._bundle, self.score = bundle, score

    def __getattr__(self, name):
        return getattr(self._bundle, name)


@dataclasses.dataclass
class Take:
    path: str
    parent: str | None = None          # the take this one branched from
    branch_tick: int | None = None     # the last tick it shares with its parent
    first: int = 0                     # first tick of its own frames (the branch tick for a branch)
    last: int = -1                     # last tick it recorded
    rewound_to: int | None = None      # the tick the session rewound to when this take ended
    score_file: str | None = None
    score_sha256: str | None = None
    durable_tick: int | None = None
    sealed: bool = False
    seek: dict[str, Any] | None = None  # the receipt of the seek that started it


class TASSession:
    """One live game, the current take and the authored score of that take from boot."""

    def __init__(self, ctx: Any = None, cfg: dict | None = None, *, backend: str | None = None, attach: bool = False,
                 log: Callable[[str], None] | None = None, over: Mapping[str, Any] | None = None):
        self.ctx, self.backend_name, self.attach = ctx, backend, attach
        self.log = log or (lambda s: print(f"telos live: {s}", file=sys.stderr, flush=True))
        self.over = dict(over or {})
        self.cfg: dict[str, Any] | None = None
        self.sup: Supervisor | None = None
        self.sink: BundleSink | None = None
        self.header: dict[str, Any] = {}
        self.items: list[Any] = []            # authored items of the current take, from boot
        self.takes: list[Take] = []
        self.manifest: Path | None = None
        self.held: list[Any] = []             # read handles a live branch resolves its parent through
        self.broken = False                   # no input until recording ownership is restored
        self._seal_error: Exception | None = None
        self.frame_seq = 0
        self.closed = False
        self.own_frames = False               # True: frames come from render PNGs, not capture.frames
        self.session_id = uuid.uuid4().hex
        self.revision = 0
        self._lock = threading.RLock()
        self._operation_depth = 0
        self.mode = "idle"
        self.playing = False
        self.engaged = True
        self.pause_on_engage = True
        self._physical_step_in_progress = False
        self._physical_input_error = None
        self._engagement_resume_note = None
        from .run_queue import RunQueue
        self._run_queue = RunQueue()
        self._run_step_in_progress = False
        self.branch_ok = False
        self._env_prewarm = None
        self._label_sent = None
        if cfg is not None:
            self.open_archive(ctx, cfg, attach=attach)

    @classmethod
    def from_session(cls, session):
        if not isinstance(session, cls):
            raise TypeError("expected the authoritative TASSession")
        return session

    @property
    def session(self):
        return self

    @property
    def bundle(self):
        if getattr(self, "mode", "archive") == "recording":
            return self.sink.bundle if self.sink is not None else None
        return self._bundle

    @bundle.setter
    def bundle(self, value):
        self._bundle = value

    @property
    def score(self):
        if getattr(self, "mode", "archive") == "recording":
            return compile_score(self.text())
        return self._score

    @score.setter
    def score(self, value):
        self._score = value

    @owned_operation
    def _archive_rewind(self, tick: int, *, strict_restore: bool = False) -> None:
        """Strict restore to a recorded archive tick. Capture cadence: a tick that is not a
        captured restore point is reached hybrid (restore@K + replay, receipt mode hybrid),
        or refused with strict_restore."""
        if tick < 0:
            raise TelosError(f"cannot rewind before {self._unit()} 0")
        self.last_rewind_phases = None   # never attach a previous rewind's timings
        clock = time.perf_counter
        phases, started = {}, clock()
        mark = [started]
        def lap(name):
            now = clock()
            phases[name] = round(now - mark[0], 6)
            mark[0] = now
        # A held capture is checked afresh; it is not a restoration receipt.
        if tick == self.sup.tick and (self.sink is not None or getattr(self, "_held_capture", None) is not None):
            proof = self._held_capture_receipt()
            rec = self.bundle.record(tick)
            return {"rewound": False, "noop": True, "tick": tick,
                    "receipt": receipt_dict(proof), "capture_ack": {
                        "format": "kptas-held-capture/1", "session_id": self.session_id,
                        "parent": self.bundle.bundle_id, "tick": tick, "epoch": rec.epoch,
                        "state_root": rec.state_root.hex(), "files_root": rec.files_root.hex(),
                        "env_key": self.bundle.env.key()}}
        from kptas.holo.seek import recorded_events
        try:
            target = self.bundle.record(tick)
        except KeyError:
            raise TelosError(f"recording has no {self._unit()} {tick}") from None
        mode, anchor, at = SeekMode.RESTORE, tick, None
        if not self._restorable(target, SeekMode.RESTORE):
            why = self._unreachable_reason(target)
            if strict_restore:
                raise TelosError(f"{why}; strict restore cannot reach it")
            if not SeekMode.HYBRID.allowed(Tier.parse(self.cfg["run"]["tier"])):
                raise TelosError(f"{why}; restore + replay is not allowed in tier {self.cfg['run']['tier']}")
            planner = self._planner(self.bundle)
            at = planner.place(tick)
            anchor = self._pick_anchor(planner, tick, SeekMode.RESTORE, at)
            if anchor is None:
                raise TelosError(f"{why}, and no earlier {self._unit()} in its epoch is a restorable "
                                 "restore point with a score position")
            mode = SeekMode.HYBRID
        canary_at = target if mode is SeekMode.RESTORE else self.bundle.record(anchor)
        try:
            following = self.bundle.record(canary_at.tick + 1)
        except KeyError:
            following = None
        if (following is None or following.epoch != canary_at.epoch) and not any(
                name == 'proc.down' for name, _ in recorded_events(self.bundle).get(canary_at.tick + 1, ())):
            raise TelosError("strict verification requires a recorded continuation canary; current writer preserved")
        self._held_capture = None
        self.playing = False
        lap("admit")
        if getattr(self, "sink", None) is not None:
            self.finish_branch(seal=getattr(self, "branch_ok", False))
            lap("finish_branch")
        self.broken = True
        self.verified_placement = None
        self.play_cursor = None
        self._label_sent = None
        # Strict restore only: no fallback chain, so a refused restore is refused, never a
        # silent replay from boot.
        from copy import deepcopy
        strict = deepcopy(self.cfg)
        strict["seek"].update(on_incompatible="error", fallback_chain=[])
        started_ns = time.perf_counter_ns()
        receipt = seek(self.bundle, anchor, self.sup, mode="restore", cfg=strict, log=self.log)
        lap("seek")
        if (receipt.result != "pass" or receipt.mode is not SeekMode.RESTORE or receipt.fallback
                or receipt.verify_level is not VerifyLevel.FULL_ROOT or receipt.canary != "pass"):
            raise RunError("control restore did not pass strict verification")
        if mode is SeekMode.HYBRID:
            receipt = self._archive_replay(anchor, tick, at, receipt, started_ns=started_ns)
            lap("replay")
        self.last_receipt = receipt
        if mode is SeekMode.RESTORE:
            self._arm_paused_seek_view(self.bundle, tick)
        else:
            # The recorded root at an uncaptured tick is null: pinning the paused view to it would
            # label replayed pixels as a restore. The live frame stands.
            self._paused_seek_view = self._paused_seek_render = None
        self.verified_placement = (id(self.bundle), id(self.sup.backend), self.sup.tick)
        lap("arm_view")
        self.label(force=True)
        lap("label")
        phases["total"] = round(clock() - started, 6)
        self.last_rewind_phases = phases
        self.broken = False

    @owned_operation
    def rewind_mark(self) -> None:
        """Back to the latest score @mark below the current tick: one strict restore, not N single ticks."""
        here = self.sup.tick
        marks = [m["tick"] for m in getattr(self.bundle, "marks", ())
                 if not str(m.get("name", "")).startswith("event:") and m.get("tick", here) < here]
        if not marks:
            raise TelosError(f"no score @mark before {self._unit()} {here}")
        self.rewind(max(marks))

    @owned_operation
    def toggle_play(self) -> None:
        if getattr(self, '_engagement_resume_note', None) is not None:
            self._engagement_resume_note = None
            self.playing = False
            self.label()
            return
        self.playing = not self.playing
        self.label()

    def label(self, text: str = "", *, force: bool = False) -> None:
        # The host workspace owns labels; suppress the obsolete native debug strip.
        state = "PLAYING" if getattr(self, "playing", False) else "PAUSED"
        if getattr(self, "closed", False) or self.sup.backend is None or not getattr(self.sup, "alive", True):
            return
        hud = getattr(self.sup.backend, 'hud', None)
        if not callable(hud):
            return
        shown = text or (state + " CTRL ALT F7 PLAY F8 STEP F9 BACK SHIFT F9 MARK F10 BRANCH")
        # Resend only when the native strip could differ: new text, offset, epoch or
        # backend. Restore, seek and branch force it. The cache is set only after
        # the native call returns, so a fault always resends.
        # The backend is held weakly so a replaced/closed backend is never kept alive.
        backend = self.sup.backend
        sent = (self.sup.epoch, self.sup.offset, shown)
        last = getattr(self, "_label_sent", None)
        if (not force and last is not None and last[0]() is backend and last[1:] == sent):
            return
        self._label_sent = None
        hud(False, shown, watermark=True, offset=self.sup.offset)
        try:
            self._label_sent = (weakref.ref(backend), *sent)
        except TypeError:   # not weak-referenceable: resend every time rather than pin it
            self._label_sent = None

    @owned_operation
    def play_step(self) -> bool:
        """Play to the next gate, completing any score wait/relaunch on the owner thread.

        Unlike Step, a timed transition can advance several ticks. Supervisor owns
        transition timeouts and event carry; its documented @assert is a notice,
        not an evaluated assertion.
        """
        return self.step(transitions=True)

    @owned_operation
    def _archive_step(self, *, transitions: bool = False, ui_focus_display=None) -> bool:
        self._held_capture = None
        if getattr(self, "broken", False):
            raise TelosError("the session needs recovery before playback")
        identity = (id(self.bundle), id(self.sup.backend), id(self.score), self.sup.tick)
        cached = getattr(self, "play_cursor", None)
        if cached is not None and cached[1] is not None and cached[0] == identity:
            cur = cached[1]
        else:
            cur = self._resume_cursor(self.bundle, self.sup.tick, self.score)
            # Genesis is before any score item; carry the boot's actual events.
            if cur.i == 0 and cur.j == 0 and self.sup.carry:
                from dataclasses import replace
                cur = replace(cur, carry=tuple((e.name, e.arg) for e in self.sup.carry))
        if cur.i >= len(self.score.items):
            self.playing = False
            return False
        probe = cur.i
        while probe < len(self.score.items):
            candidate = self.score.items[probe]
            if isinstance(candidate, (Assert, Mark, SaveMs, Segment)) or isinstance(candidate, Note) and candidate.count == 0:
                probe += 1
                continue
            break
        if probe == len(self.score.items):
            tail = (id(self.bundle), id(self.sup.backend), self.sup.tick, id(self.score))
            if getattr(self, "completed_tail", None) != tail:
                self.verified_placement = None
                before = self.sup.tick
                self.broken = True
                result = self.sup.run_score(self.score, start=cur, max_ticks=before + 1)
                self.play_cursor = ((id(self.bundle), id(self.sup.backend), id(self.score), self.sup.tick), result.cursor)
                if self.sup.tick != before:
                    raise RunError("zero-time score tail advanced the game")
                self.completed_tail = tail
                self.broken = False
            self.playing = False
            return False
        note = self.score.items[probe]
        if not transitions and (not isinstance(note, Note) or note.count <= (cur.j if probe == cur.i else 0)):
            raise TelosError(f"this transition needs score playback; one-{self._unit()} stepping is unavailable")
        self._require_execution_health()
        self._ui_execution_focus(ui_focus_display, note)
        self.sup.carry = [EngineEvent(name, self.sup.tick, arg) for name, arg in cur.carry]
        target = self.sup.tick + 1
        self.verified_placement = None
        self.broken = True
        result = self.sup.run_score(self.score, start=cur, max_ticks=target)
        self.play_cursor = ((id(self.bundle), id(self.sup.backend), id(self.score), self.sup.tick), result.cursor)
        if result.stopped == "halted":
            raise RunError("score playback halted before completion")
        if not transitions and (not self.sup.alive or self.sup.tick != target):
            raise RunError(f"one-{self._unit()} control did not reach its target")
        self.broken = False
        self.last_note = note if isinstance(note, Note) else None
        if transitions and result.stopped in ("end", "exit"):
            self.playing = False
            return False
        self.label()
        return True

    @owned_operation
    def finish_branch(self, sink=None, *, seal=False):
        """Persist the take before moving its playhead; later edits require a new child."""
        from kptas.holo import Bundle
        from kptas.supervisor import session_dir
        sink = sink if sink is not None else self.sink
        if sink is None:
            return self.bundle
        self.broken = True
        if getattr(self, "_seal_error", None) is not None:
            raise TelosError("the previous branch could not be sealed; close the session for recovery") from self._seal_error
        root = sink.bundle.root
        sink.checkpoint_if_due(force=True)
        self.playing = False
        self.sup.frames_to(session_dir(self.cfg, "controls") / ("playback-" + uuid.uuid4().hex))
        try:
            sink.close(seal=seal, backend_ceiling=self.sup.caps.tier_ceiling)
        except Exception as exc:
            self._seal_error = exc
            self.sup.sink = None
            raise
        self.sup.sink = None
        self.sink = None
        self.verified_placement = None
        try:
            reuse = getattr(sink.bundle, 'reopen_reader', None)
            self.bundle = reuse() if callable(reuse) else Bundle.open(root)
        except (FormatError, OSError) as exc:
            # A separately finalized ancestor may have changed since admission.
            # Refresh it normally rather than reusing any stale cached state.
            log = getattr(self, 'log', None)
            if callable(log):
                log(f"branch reader reuse refused; full admission required: {exc}")
            self.bundle = Bundle.open(root)
        if hasattr(self, "held"):
            self.held.append(self.bundle)
        # BundleSink consumed the backend deltas. Seed the seek planner from the
        # durable current tick before it computes a minimal backward write set.
        from kptas.holo.pages import PageMap
        session = self._seek_session()
        try:
            # The closed writer's own live map (root-checked) or the session's (tick, link)
            # map cache spare a full keyframe+delta rebuild of map(live tick).
            session.live.map = self._recorded_live_map(self.bundle, self.sup.tick, session, writer=sink.bundle)
            session.live.ok = session.live.captured = True
        except Exception:
            # Unknown live memory requires a full write, never an optimistic delta.
            # (An uncaptured held gate has no recorded map: capture cadence.)
            session.live.map, session.live.ok, session.live.captured = PageMap(), False, False
        session.live.tick, session.live.via = self.sup.tick, "replay"
        session.live.journal = None
        self.broken = False
        return self.bundle


    def _seek_session(self):
        """The seek planner's per-supervisor session, shared with seek() so its caches persist."""
        seekmod = __import__("importlib").import_module("kptas.holo.seek")
        from kptas.holo.verify import Live
        session = seekmod._SESSIONS.get(self.sup)
        if session is None:
            session = seekmod._SESSIONS[self.sup] = seekmod._Session(Live())
        return session

    @staticmethod
    def _recorded_live_map(bundle, tick, session, *, writer=None):
        """map(tick) of `bundle`, as a private PageMap, without a duplicate rebuild.

        Order: the seek session's map cache under (tick, link_at(tick)) -- the key _Seek.map_at
        uses, naming the whole chain prefix; then a closed writer's incremental live map, only
        when its tick and epoch match the recorded tick and its root equals the recorded
        state_root (the reopen_reader proof); then Bundle.map_at. The result is cached under
        the same key with the seek planner's LRU bound.
        """
        seekmod = __import__("importlib").import_module("kptas.holo.seek")
        from kptas.holo.pages import PageMap
        try:
            key = (tick, bundle.link_at(tick))
        except KeyError:
            key = None
        maps = session.maps
        refs = maps.get(key) if key is not None else None
        if refs is not None:
            maps.move_to_end(key)
            return PageMap(refs)
        resolved = None
        try:
            live = getattr(writer, 'live', None)
            if (live is not None and writer.live_tick == tick
                    and writer.bundle_id == bundle.bundle_id):
                rec = bundle.record(tick)
                if rec.epoch == writer.live_epoch and live.root() == rec.state_root:
                    resolved = PageMap(live.refs())
        except Exception:  # noqa: BLE001 - evidence unavailable; rebuild from the recording
            resolved = None
        if resolved is None:
            resolved = bundle.map_at(tick)
        if key is not None:
            maps[key] = tuple(resolved.refs())
            while len(maps) > seekmod._MAP_CACHE_ENTRIES:
                maps.popitem(last=False)
        return resolved

    def _rewind_receipt(self):
        """receipt_dict of the last strict seek, plus that rewind's wall phases when known."""
        rc = receipt_dict(self.last_receipt)
        rc.update(_hybrid_fields(self.last_receipt))
        phases = getattr(self, 'last_rewind_phases', None)
        if phases:
            rc['phases'] = dict(phases)
        return rc

    # ------------------------------------------------------------ capture cadence

    @staticmethod
    def _restorable(rec, mode) -> bool:
        """Whether a seek in `mode` lands on `rec` exactly and a branch can resume there.

        Native modes need a captured restore point; replay needs only a captured
        tick (a branch resumes from map(tick), which an uncaptured tick lacks)."""
        from kptas.types import TickFlag
        if not rec.flags & TickFlag.CAPTURED:
            return False
        return mode is SeekMode.REPLAY or bool(rec.flags & TickFlag.RESTORE_POINT)

    def _unit(self, plural: bool = False) -> str:
        """The word for one gate step in user-facing messages: frame unless clock.precision = "tick"."""
        cfg = getattr(self, "cfg", None)
        if cfg is None and getattr(self, "ctx", None) is not None:
            try:
                cfg = self.ctx.cfg()
            except Exception:   # wording only: an unreadable config reads as the default
                cfg = None
        return unit_name((cfg or {}).get("clock", {}).get("precision"), plural)

    def _unreachable_reason(self, rec) -> str:
        from kptas.types import TickFlag
        if not rec.flags & TickFlag.CAPTURED:
            return f"{self._unit()} {rec.tick} was not captured"
        return f"{self._unit()} {rec.tick} is not a restore point ({rec.reason.name.lower()})"

    def _planner(self, bundle, score=None, cfg=None):
        """A placement-only seek planner over `bundle`; `score` overrides the score it places
        (a live take's SCOR stops at its first tick) without touching the bundle itself."""
        from importlib import import_module
        seekmod = import_module("kptas.holo.seek")
        view = bundle if score is None else _ScoreView(bundle, score)
        return seekmod._Seek(view, None, self.cfg if cfg is None else cfg, lambda _s: None)

    def _pick_anchor(self, planner, tick, mode, at):
        """K: the latest tick <= `tick` in its epoch that a seek in `mode` lands on exactly,
        with a score position in `at` and a map the store resolves. None when there is none.
        The latest restore point, not the nearest keyframe: one strict restore, the shortest
        replay, and the same K whether it is validated or restored."""
        want = planner.rec(tick)
        if want is None:
            return None
        for k in range(tick, -1, -1):
            rec = planner.rec(k)
            if rec is None or rec.epoch != want.epoch:
                return None
            if self._restorable(rec, mode) and k in at and planner.map_at(k) is not None:
                return k
        return None

    def _capture_notice(self, step):
        """Ack fields disclosing a capture step: the user sees the extra recorded frame."""
        if not step:
            return {}
        return {"capture_step": list(step),
                "notice": f"captured 1 extra {self._unit()} at {step[1]} to make it a restore point"}

    def _held_gate_captured(self):
        """Whether this writer's held gate has a captured root; None when unknown."""
        from kptas.types import TickFlag
        sup, sink = self.sup, getattr(self, 'sink', None)
        if sup is None or sink is None:
            return None
        try:
            return bool(sink.bundle.record(sup.tick).flags & TickFlag.CAPTURED)
        except Exception:
            return None

    def _capture_held_gate(self):
        """Capture cadence: native restore refuses while the held gate is uncaptured
        (a pause that landed mid-cadence, or a failed multi-tick step). Step one tick
        holding the current input (one-shots dropped) with capture_last=True into this
        take, recorded, authored and persisted like any step, so the restore leaves
        from a captured gate. Refuses when the take's boundary is not coherent."""
        if self._held_gate_captured() is not False:
            return None
        if self.broken:
            raise TelosError(f"the held gate {self.sup.tick} was not captured and the session needs recovery; "
                             "close it and reopen its recording")
        sup, sink, take = self.sup, self.sink, self.cur
        journal = getattr(getattr(sink, 'bundle', None), 'journal', None)
        if (take.last != sup.tick or sink.last_tick != sup.tick or sup.sink is not sink
                or sup.backend is None or sup.alive is not True
                or getattr(sup.backend, 'poisoned', False) not in (False, None)
                or getattr(journal, 'write_failure', None) is not None):
            raise TelosError(f"the held gate {sup.tick} was not captured and this take's recorded boundary "
                             "is not coherent; close the session and reopen its recording")
        if not getattr(self, 'engaged', True):
            raise TelosError(f"the held gate {sup.tick} was not captured; engage TAS so one captured {self._unit()} "
                             "can be stepped before restoring")
        previous = self.items[-1] if self.items else None
        note = (dataclasses.replace(previous, count=1, wheel=0, osev=None)
                if isinstance(previous, Note) else Note(1))
        before = sup.tick
        self._pending("step", chain(self.items, (note,)))
        try:
            rep = sup.step(1, note)                      # capture_last=True
        except Exception:
            self.broken = True
            raise
        if rep.tick != before + 1:
            self.broken = True
            raise TelosError(f'the capture step did not advance exactly one {self._unit()}')
        self.items.append(note)
        take.last = rep.tick
        self._persist()
        self._frame(rep.tick, before)
        if self._held_gate_captured() is not True:
            self.broken = True
            raise TelosError(f"the capture step to {self._unit()} {rep.tick} did not capture its gate")
        self.log(f"held gate {before} was not captured (capture cadence); stepped one held {self._unit()} to "
                 f"captured gate {rep.tick} before restoring")
        return [before, rep.tick]

    def _rerecord_span(self, par, anchor, target, at, anchor_receipt, *, started_ns):
        """Drive `par`'s recorded score from `anchor` to `target` with this branch's sink
        attached, then prove the re-recorded ticks equal the recording.

        The drive itself checks nothing (the sink drains the records), so the proof
        follows it, record against record: the journal digest of every tick (restore
        events aside, as seek compares), state_root wherever both sides captured, and
        the landing gate itself captured. Returns the hybrid (or replay) seek receipt."""
        from importlib import import_module
        from kptas.holo import verify as hv
        from kptas.types import NULL_DIGEST, TickFlag
        seekmod = import_module("kptas.holo.seek")
        sup, sink = self.sup, self.sink
        if sink is None or sup.sink is not sink or sink.last_tick != anchor or sup.tick != anchor:
            raise TelosError(f"re-recording needs the branch sink attached at its branch {self._unit()}")
        driver = seekmod._Seek(par, sup, self.cfg, self.log)
        try:
            driver.drive(at[anchor], target)
        except seekmod._Mismatch as e:
            raise RunError(f"re-recording {anchor}..{target} stopped at {self._unit()} {e.tick}: {e.field}") from None
        if sup.tick != target or sink.last_tick != target:
            raise RunError(f"re-recording {anchor}..{target} ended at {self._unit()} {sup.tick}")
        child = sink.bundle
        roots, journals, rooted_target = 0, 0, False
        for t in range(anchor + 1, target + 1):
            want, have = par.record(t), child.record(t)
            if have.epoch != want.epoch:
                raise RunError(f"re-recorded {self._unit()} {t}: epoch {have.epoch}, recorded {want.epoch}")
            if want.journal_sha256 != NULL_DIGEST:
                if hv.journal_digest(have) != hv.journal_digest(want):
                    raise RunError(f"re-recorded {self._unit()} {t}: journal_sha256 differs from the recording")
                journals += 1
            if want.flags & have.flags & TickFlag.CAPTURED:
                if have.state_root != want.state_root:
                    raise RunError(f"re-recorded {self._unit()} {t}: state_root differs from the recording")
                roots += 1
                rooted_target = t == target
        last = child.record(target)
        if not last.flags & TickFlag.CAPTURED:
            raise RunError(f"re-recorded {self._unit()} {target} was not captured; it cannot be restored or branched")
        native = anchor_receipt.mode is not SeekMode.REPLAY
        n = target - anchor
        return Receipt(
            kind="seek", result="pass", from_tick=anchor_receipt.from_tick, to_tick=target,
            mode=SeekMode.HYBRID if native else SeekMode.REPLAY,
            # full_root only when the recording's root was compared at the landing gate itself;
            # an uncaptured target is proved by journals over a root-exact anchor, said as such.
            verify_level=(anchor_receipt.verify_level if rooted_target else VerifyLevel.JOURNAL),
            write_pages=anchor_receipt.write_pages,
            micros=(time.perf_counter_ns() - started_ns) // 1000,
            state_root=last.state_root.hex(), journal_sha256=last.journal_sha256.hex(),
            # The continuation canary ran at K, not at target: it stays in the verifier text.
            canary="none", restore_points=anchor_receipt.restore_points,
            verifier=(f"{sup.name}; origin {'restore' if native else 'replay'}@{anchor}; replayed {n} ticks "
                      f"into the branch; journals matched at {journals} of {n} ticks (the rest recorded none); "
                      f"roots compared at {roots} ticks"
                      f"{f' including {target}' if rooted_target else f'; none at {target} (not captured)'}; "
                      f"anchor {anchor_receipt.verify_level}, canary {anchor_receipt.canary} at {anchor}"),
            env_key=anchor_receipt.env_key)

    def _archive_replay(self, anchor, target, at, anchor_receipt, *, started_ns):
        """Archive hybrid: from a strict restore at `anchor`, replay the recorded score to
        `target`, checking every tick's journal and the state_root wherever the live gate and
        the recording were both captured (no sink: the seek's own per-tick checks run).
        verify_level is full_root only when a root was compared at `target` itself."""
        from importlib import import_module
        from kptas.types import TickFlag
        seekmod = import_module("kptas.holo.seek")
        sup = self.sup
        if sup.sink is not None or sup.tick != anchor:
            raise TelosError(f"archive replay needs the restored gate at {self._unit()} {anchor}")
        driver = seekmod._Seek(self.bundle, sup, self.cfg, self.log)
        driver.roots = True
        compared = []
        check = driver.compare

        def compare(t, journal, rep, strict):
            want = driver.rec(t)
            rooted = (want is not None and want.flags & TickFlag.CAPTURED
                      and (rep is not None or driver.live.captured))
            check(t, journal, rep, strict)
            if rooted:
                compared.append(t)
        driver.compare = compare
        try:
            driver.drive(at[anchor], target)
        except seekmod._Mismatch as e:
            raise RunError(f"replay {anchor}..{target} diverged at {self._unit()} {e.tick}: {e.field}") from None
        live = driver.live
        if sup.tick != target or live.tick != target or not live.ok:
            raise RunError(f"replay {anchor}..{target} ended at {self._unit()} {sup.tick}")
        if not live.captured:
            raise RunError(f"the replayed gate at {self._unit()} {target} was not captured")
        live.via = "restore"
        n = target - anchor
        rooted_target = target in compared
        r = Receipt(
            kind="seek", result="pass", from_tick=anchor_receipt.from_tick, to_tick=target,
            mode=SeekMode.HYBRID,
            verify_level=VerifyLevel.FULL_ROOT if rooted_target else VerifyLevel.JOURNAL,
            write_pages=anchor_receipt.write_pages, micros=(time.perf_counter_ns() - started_ns) // 1000,
            state_root=live.map.root().hex(),
            journal_sha256=hashlib.sha256(live.journal).hexdigest() if live.journal else "",
            canary="none", restore_points=anchor_receipt.restore_points,
            verifier=(f"{sup.name}; origin restore@{anchor}; replayed {n} ticks; journals checked at every recorded {self._unit()}; "
                      f"roots compared at {len(compared)} ticks"
                      f"{f' including {target}' if rooted_target else f'; none at {target} (not captured)'}; "
                      f"anchor {anchor_receipt.verify_level}, canary {anchor_receipt.canary} at {anchor}"),
            env_key=anchor_receipt.env_key)
        seekmod._append(self.bundle, r, self.log)
        return r

    @contextmanager
    def operation(self, expected=None):
        """One ownership boundary for native access, including failed mutations.

        Admission and execution hold the same lock. A failed operation may have
        moved a native gate, so it invalidates prior tokens too.
        """
        with self._lock:
            if expected is not None:
                actual = self.owner_token()
                if (not isinstance(expected, dict) or expected.keys() != actual.keys()
                        or any(type(expected[k]) is not type(v) or expected[k] != v
                               for k, v in actual.items())):
                    raise TelosError(f"session, take, {self._unit()} or revision changed; refresh required")
            outer = self._operation_depth == 0
            self._operation_depth += 1
            try:
                yield
            finally:
                self._operation_depth -= 1
                if outer:
                    self.revision += 1

    def owner_token(self):
        with self._lock:
            take = (str(self.bundle.root) if self.mode == "archive" else
                    self.cur.path if self.takes else None)
            return {"session_id": self.session_id, "take": take,
                    "tick": self.sup.tick if self.sup else None, "revision": self.revision}

    def create_supervisor(self, cfg, **kwargs):
        """Only this owner allocates native lifetime; attaching a view cannot."""
        with self._lock:
            if self.sup is not None or self.closed:
                raise TelosError("session already owns a supervisor; attach controls to this session")
            factory = kwargs.pop("factory", Supervisor)
            self.sup = factory(cfg, **kwargs)
            return self.sup

    def _resume_cursor(self, bundle, tick, score):
        """resume_cursor, memoised on (bundle id, chain link, tick, score digest, cfg).

        Pure in those inputs. A score with @include files is never cached (its text
        digest does not name the dependencies). Errors are not cached.
        """
        compiled = compile_score(score) if isinstance(score, str) else score
        key = None
        if not getattr(compiled, 'files', None):
            try:
                key = (bundle.bundle_id, bundle.chain.link, tick,
                       hashlib.sha256(compiled.text().encode()).digest(), id(self.cfg))
            except Exception:
                key = None
        cache = getattr(self, '_resume_cache', None)
        if cache is None:
            cache = self._resume_cache = {}
        hit = cache.get(key) if key is not None else None
        if hit is not None and hit[0] is self.cfg:
            return dataclasses.replace(hit[1])
        cur = resume_cursor(bundle, tick, compiled, self.cfg)
        if key is not None:
            if len(cache) >= 4:
                cache.pop(next(iter(cache)))
            cache[key] = (self.cfg, dataclasses.replace(cur))
        return cur

    def _start_env_prewarm(self, cfg):
        """Warm the env digest caches off-thread; never blocks and never raises into the session."""
        try:
            from kptas.tier.envpin import prewarm_env_digests
            self._env_prewarm = prewarm_env_digests(cfg, background=True)
        except Exception as exc:  # noqa: BLE001 - an optimisation only
            self._env_prewarm = None
            self.log(f"env digest prewarm skipped: {exc!r}")

    @owned_operation
    def open_archive(self, bundle, cfg, *, attach=False, supervisor_factory=None, allow_backend=False):
        from copy import deepcopy
        from collections import deque
        if self.sup is not None or self.closed:
            raise TelosError("session already owns a supervisor; save and close before replacement")
        if cfg["run"]["backend"] != "shim" and not allow_backend:
            raise TelosError("in-game controls require the shim's stopped gate")
        cfg = deepcopy(cfg)
        cfg["seek"]["on_incompatible"] = "error"
        cfg["seek"]["fallback_chain"] = []
        cfg["verify"]["on_mismatch"] = "halt"
        cfg["verify"]["canary_every_seeks"] = 1
        cfg["replay"]["present"] = "draw"
        if SeekMode.parse(cfg["seek"].get("mode", "restore")) is not SeekMode.REPLAY:
            from kptas.tier.envpin import require_native_restore_dll
            candidate = bundle
            while candidate is not None:
                require_native_restore_dll(candidate.env, cfg)
                candidate = candidate.parent
        score = compile_score(bundle.score)
        self.playing, self.notices, self.verified_placement = False, deque(maxlen=64), None
        self.bundle, self.cfg, self.score = bundle, cfg, score
        self._start_env_prewarm(cfg)
        self.create_supervisor(cfg, env=bundle.env, attach=attach, run_name="controls", factory=supervisor_factory or Supervisor)
        original_log = self.sup.log
        def notice(message):
            self.notices.append(message)
            original_log(message)
        self.sup.log = notice
        self.mode = "archive"
        self.broken = False

    @owned_operation
    def save_recording(self):
        """Save the existing recording; read-only archives are already on disk."""
        if self.mode == 'archive' and self.sink is None:
            if self.broken:
                raise TelosError('the session needs recovery before saving')
            return {'tick': self.sup.tick, 'take': str(self.bundle.root),
                    'saved': True, 'read_only': True, 'durable_tick': None}
        result = self.save()
        return {**result, 'saved': True, 'read_only': False}

    @owned_operation
    def load_recording(self, path, tick=0):
        """Validate first, persist the current take, then replace this owner's recording."""
        from copy import deepcopy
        from types import SimpleNamespace
        from kptas.holo import Bundle
        from kptas.telos.cli import bundle_cfg, pin_env
        from kptas.config import resolve
        from kptas.types import TickFlag
        if self.mode not in {'recording', 'archive'} or self.broken:
            raise TelosError('an acknowledged open session is required to load a recording')
        if type(tick) is not int or tick < 0:
            raise TelosError(f'recording load {self._unit()} must be a nonnegative integer')
        root = Path(path).expanduser().resolve()
        if root.suffix != '.holo' or not root.is_dir():
            raise TelosError('choose an existing .holo recording directory')
        candidate = Bundle.open(root)
        adopted = False
        try:
            compiled = compile_score(candidate.score)
            context = self.ctx
            if not callable(getattr(context, 'cfg', None)):
                # Controls constructed from a bundle still retain their resolved
                # environment; apply the candidate headers through the same
                # bundle_cfg seam without replacing those installation paths.
                def configured(header=None, **over):
                    raw = deepcopy(self.cfg)
                    if header:
                        raw['run']['tier'] = header['tier']; raw['run']['game'] = header['game']
                        raw['clock']['hz'] = header['hz']
                    for key,value in over.items():
                        if value is not None:
                            table,field = key.split('__'); raw.setdefault(table,{})[field] = value
                    return resolve(raw, '<current recording environment>')
                context = SimpleNamespace(cfg=configured)
            cfg = bundle_cfg(context, candidate, run__backend=self.cfg['run']['backend'])
            cfg = deepcopy(cfg)
            cfg['seek'].update(mode='restore', on_incompatible='error', fallback_chain=[])
            cfg['verify'].update(on_mismatch='halt', canary_every_seeks=1)
            cfg['env']['guard'] = 'refuse'
            if (cfg['threads']['model'] == 'free' or cfg['tick']['context'] == 'inline'
                    or cfg['store']['pages'] == 'digests'
                    or not SeekMode.RESTORE.allowed(Tier.parse(cfg['run']['tier']))
                    or cfg['verify']['canary_ticks'] < 1):
                raise TelosError('candidate configuration cannot provide strict restore and a continuation canary')
            if pin_env(cfg).key() != candidate.env.key():
                raise TelosError("the environment differs from the recording's pin; current session was preserved")
            from kptas.tier.envpin import require_native_restore_dll
            recorded = candidate
            while recorded is not None:
                require_native_restore_dll(recorded.env, cfg)
                recorded = recorded.parent
            record = candidate.record(tick)
            # Capture cadence: an uncaptured (or non-restore-point) tick is loaded hybrid, by
            # restoring the latest restore point K <= tick and replaying to it (receipt mode
            # hybrid). K is chosen exactly as _archive_rewind will choose it (same bundle, cfg and
            # placement), so the K validated here is the K restored.
            anchor = tick
            if not self._restorable(record, SeekMode.RESTORE):
                if not SeekMode.HYBRID.allowed(Tier.parse(cfg['run']['tier'])):
                    raise TelosError(f'{self._unreachable_reason(record)}; restore + replay is not allowed '
                                     f"in tier {cfg['run']['tier']}")
                planner = self._planner(candidate, cfg=cfg)
                anchor = self._pick_anchor(planner, tick, SeekMode.RESTORE, planner.place(tick))
                if anchor is None:
                    raise TelosError(f'{self._unreachable_reason(record)}, and no earlier {self._unit()} in its epoch '
                                     'is a restorable restore point with a score position')
                record = candidate.record(anchor)
            target_map = candidate.map_at(anchor)
            # Resolving the map alone does not read each compressed payload.
            # Verify the stored pages/files before ending the current process.
            for reference in target_map.refs():
                candidate.page(reference.key)
            if record.flags & TickFlag.FILES:
                from kptas.filetree import validate_rows
                rows = candidate.file_tree(record.files_root)
                blob = lambda digest: candidate.file(bytes.fromhex(digest))
                if cfg['run']['backend'] == 'shim' and cfg['game']['dir'] and cfg['files']['store']:
                    from kptas.backend.rig import StageFiles
                    StageFiles(Path(os.path.expanduser(cfg['game']['dir'])), cfg['files']['stage_paths']).preflight(rows, blob)
                else:
                    validate_rows(rows, blob)
            resume_cursor(candidate, tick, compiled, cfg)
            if self.sup.attach_mode:
                raise TelosError('loading cannot replace an attached external process')
            # All candidate validation above is read-only. From here a failure
            # blocks input; the same owner retains every acquired cleanup handle.
            self.broken = True
            self.playing = False
            if self.mode == 'recording':
                self._persist()
                self._seal()
                self._write_manifest(closed=True)
            elif self.sink is not None:
                self.finish_branch(seal=self.branch_ok)
                self.broken = True
            self.sup.close()
            self.sup = None
            while self.held:
                self.held[-1].close()
                self.held.pop()
            self.mode = 'idle'
            self.open_archive(candidate, cfg, allow_backend=True)
            self.held.append(candidate); adopted = True
            self.broken = True
            self.rewind(tick)
            return {'loaded': True, 'tick': self.sup.tick, 'take': str(candidate.root),
                    'receipt': self._rewind_receipt()}
        finally:
            if not adopted:
                candidate.close()

    @owned_operation
    def replace_archive(self, bundle, cfg, *, supervisor_factory=None):
        """Replace native lifetime only after the caller preserved its draft/take."""
        if self.mode != "archive" or self.closed:
            raise TelosError("archive replacement requires an open archive session")
        if self.sink is not None:
            raise TelosError("save the current branch before replacing its recording")
        if cfg["run"]["backend"] != "shim":
            raise TelosError("in-game controls require the shim's stopped gate")
        compile_score(bundle.score)  # Reject malformed authoring before closing the game.
        self.broken = True
        self.sup.close()
        self.sup = None
        self.mode = "idle"
        self.open_archive(bundle, cfg, supervisor_factory=supervisor_factory or Supervisor)
        self.branch_ok = False
        self.rewind(0)

    @owned_operation
    def save(self):
        if self.mode == "recording":
            self._live_ok()
            self._persist()
            return {"tick": self.sup.tick, "take": self.cur.path}
        if self.sink is None:
            raise TelosError("there is no recording branch to save")
        checkpoint = self.sink.checkpoint_if_due(force=True)
        return {"tick": self.sup.tick, "take": str(self.bundle.root), "checkpoint": checkpoint}

    def _held_gate_key(self):
        """Identity of the gate a held-capture receipt just verified; None when it cannot be named."""
        sup, bundle = self.sup, self.bundle
        try:
            return (sup.backend, bundle, bundle.bundle_id, bundle.link_at(sup.tick), bundle.chain.link,
                    sup.epoch, sup.tick)
        except Exception:
            return None

    def _held_gate_unchanged(self, gate):
        """True only when the gate a receipt verified is provably still the same (shim only).

        Same backend, bundle object, chain links, epoch and tick, plus the native poison and gate
        probe the full audit makes around its memory audit. Anything else returns False so the
        caller runs the full audit. Callers must have made no backend call since the receipt.
        True does not stand in for an audit: native memory and host files can change without a
        call, so the caller still runs the held-capture receipt (environment, files root, native
        FULL_ROOT audit, retained predicate) and may skip only its duplicated pre-files audit.
        """
        sup = self.sup
        try:
            if (gate is None or gate != self._held_gate_key() or sup.name != 'shim' or self.broken
                    or sup.backend is not gate[0] or sup.alive is not True
                    or getattr(sup.backend, 'poisoned', True) is not False
                    or sup.last_osev not in (None, 'focus', 'blur')):
                return False
        except Exception:
            return False
        backend = sup.backend
        backend.digest([])  # Native poison guard, as in the full audit.
        state = backend.input_state()
        return (state.get('source') == 'virtual_devices' and state.get('sampled_at_gate') is True
                and type(state.get('tick')) is int and state['tick'] + sup.offset == sup.tick)

    def _held_capture_receipt(self, *, pre_files_audit=True):
        """Verify this writer's current gate without restoring or advancing it.

        A capture continuation is deliberately a verify/replay receipt with no
        canary. It never qualifies an actual restore or stamps RESTORE inputs.
        pre_files_audit=False skips only the memory audit before the files
        snapshot; every other check, the files root and the final native
        memory audit after the snapshot still run. Only branch() uses it, right
        after a full receipt whose gate _held_gate_unchanged proved unchanged.
        """
        from kptas.filetree import decode_snapshot
        from kptas.holo.verify import journal_of
        from kptas.holo.seek import recorded_events, recorded_epochs
        from kptas.tier.envpin import pin_env
        from kptas.tier.qualification import canonical_qualification, coverage_field
        from kptas.types import TickFlag, canonical_json, sha256
        sup, sink, bundle = self.sup, self.sink, self.bundle
        backend = sup.backend
        identity = (backend, bundle.bundle_id, sup.epoch, sup.tick)
        writer = (sink is not None and sup.sink is sink and sink.bundle is bundle
                  and sink.last_tick == sup.tick and sink.epoch_seen == sup.epoch)
        retained = (sink is None and sup.sink is None
                    and getattr(self, '_held_capture', None) == identity)
        if (self.broken or not (writer or retained)
                or backend is None or sup.alive is not True
                or getattr(backend, 'poisoned', True) is not False
                or sup.last_osev not in (None, 'focus', 'blur')):
            raise TelosError('held capture requires a healthy current writer gate')
        rec = bundle.record(sup.tick)
        if (rec.epoch != sup.epoch or coverage_field(rec) is not None
                or not rec.flags & TickFlag.RESTORE_POINT or rec.flags & TickFlag.EVICTED
                or (writer and (sink.pmap is None or sink.pmap.root() != rec.state_root
                                or sink.files_root != rec.files_root))):
            raise TelosError('held capture lacks complete current memory and file roots')
        events = recorded_events(bundle)
        if (any(e['epoch'] == sup.epoch for e in recorded_epochs(bundle))
                or any(name in {'proc.down', 'close'} for t in (sup.tick, sup.tick + 1)
                       for name, _ in events.get(t, ()))):
            raise TelosError('held capture cannot continue a closing process epoch')
        env = pin_env(self.cfg)
        if env.key() != bundle.env.key():
            raise TelosError('held capture environment differs from the recording')
        journal = journal_of(rec)
        if not journal:
            raise TelosError('held capture lacks its captured journal')
        def memory_at_gate():
            audit, state = None, None
            if sup.name == 'pysim':
                # Synthetic model evidence remains explicitly labelled as such.
                from kptas.backend.pysim import PySim, PROT_RW
                from kptas.types import PageRef, page_key, state_root
                if type(backend) is not PySim:
                    raise TelosError('held capture requires the concrete PySim model')
                backend._alive()
                root = state_root(PageRef(v, page_key(bytes(data)), backend.mclass(v), PROT_RW)
                                  for v, data in backend._pages.items())
                if backend._get('tick') + sup.offset != sup.tick or backend._epoch != sup.epoch:
                    raise TelosError('held capture synthetic gate differs from the owner')
                evidence = 'pysim-memory-and-files'
            elif sup.name == 'shim':
                # An optional cross-launch declaration is parsed, never invented.
                # Continuity of this held process does not qualify native restore.
                canonical_qualification(env.mask_registry)
                if (getattr(backend, 'simgame', True) is not False
                        or type(journal.get('trips')) is not int or journal['trips'] != 0):
                    raise TelosError('held capture lacks native journal evidence')
                backend.digest([])  # Native poison guard; the host flag alone can be stale.
                state = backend.input_state()
                if (state.get('source') != 'virtual_devices' or state.get('sampled_at_gate') is not True
                        or type(state.get('tick')) is not int or state['tick'] + sup.offset != sup.tick):
                    raise TelosError('held capture native gate differs from the owner')
                audit = backend.verify(VerifyLevel.FULL_ROOT, rec.state_root)
                if (audit.get('scope') != 'memory_only' or audit.get('ok') is not True
                        or audit.get('external_pending') is not False
                        or any(type(audit.get(k)) is not int or audit[k] != 0
                               for k in ('key_mismatch', 'missing', 'extra', 'prot_mismatch'))):
                    raise TelosError('held capture native memory audit failed')
                root = bytes.fromhex(audit.get('root', ''))
                backend.digest([])
                state = backend.input_state()
                if (state.get('source') != 'virtual_devices' or state.get('sampled_at_gate') is not True
                        or type(state.get('tick')) is not int or state['tick'] + sup.offset != sup.tick):
                    raise TelosError('held capture native gate changed during memory audit')
                evidence = 'native-memory-audit-and-files'
            else:
                raise TelosError('held capture is unsupported for this backend')
            if (backend is not sup.backend or backend.poisoned or not sup.alive
                    or rec.tick != sup.tick or rec.epoch != sup.epoch):
                raise TelosError('held capture identity changed during verification')
            if root != rec.state_root:
                raise TelosError('held capture live memory differs from captured root')
            return root, evidence, audit, state

        if pre_files_audit:
            root, evidence, audit, state = memory_at_gate()
        rows, _ = decode_snapshot(backend.files('snap'))
        files_root = sha256(canonical_json({'entries': rows}))
        if files_root != rec.files_root:
            raise TelosError('held capture live files differ from captured root')
        # File snapshotting is an external call: re-audit actual live memory
        # and the native poison/gate contract after it, never a cached root.
        root, evidence, audit, state = memory_at_gate()
        proof = {'format': 'kptas-held-capture/1', 'session_id': self.session_id,
                 'parent': bundle.bundle_id, 'tick': sup.tick, 'epoch': sup.epoch,
                 'state_root': root.hex(), 'files_root': files_root.hex(),
                 'env_key': env.key(), 'evidence': evidence, 'backend': sup.name,
                 'root_qualification': env.mask_registry, 'offset': sup.offset,
                 'gate_tick': sup.tick - sup.offset,
                 'scope': 'same_process_capture_continuity', 'native_restore_qualified': False}
        if sup.name == 'shim':
            proof['audit'] = {key: audit[key] for key in ('scope', 'ok', 'external_pending',
                              'key_mismatch', 'missing', 'extra', 'prot_mismatch', 'root')}
            proof['gate_source'] = state['source']
            proof['sampled_at_gate'] = state['sampled_at_gate']
        return Receipt(kind='verify', result='pass', from_tick=sup.tick, to_tick=sup.tick,
                       mode=SeekMode.REPLAY, verify_level=VerifyLevel.DIFF_ONLY,
                       state_root=root.hex(), journal_sha256=rec.journal_sha256.hex(),
                       env_key=env.key(), verifier=canonical_json(proof).decode())

    @owned_operation
    def branch(self, edited, destination, *, on_durable=None):
        """Continue the current verified archive gate with edited inputs, paused."""
        from kptas.holo import create_branch
        if self.mode != "archive":
            raise TelosError("live recording branches through rewind; archive branch requires an opened archive")
        self._resume_cursor(self.bundle, self.sup.tick, edited)
        if Path(destination).exists():
            raise TelosError("branch output already exists")
        capture_receipt = None
        capture_ack = None
        gate = None
        if self.sink is not None or getattr(self, '_held_capture', None) is not None:
            try:
                capture_receipt = self._held_capture_receipt()
                self._held_capture = (self.sup.backend, self.bundle.bundle_id, self.sup.epoch, self.sup.tick)
                if self.sink is not None:
                    self.finish_branch(seal=self.branch_ok)
                    # Parent publication can invoke backend/output work. Verify the
                    # actual retained gate again after it, including on retry.
                    # Without a sink nothing ran since the receipt above, which
                    # already proved this same retained identity.
                    capture_receipt = self._held_capture_receipt()
                gate = self._held_gate_key()
            except Exception:
                self._held_capture = None
                self.broken = True
                raise
            rec = self.bundle.record(self.sup.tick)
            capture_ack = {'format': 'kptas-held-capture/1', 'session_id': self.session_id,
                           'parent': self.bundle.bundle_id, 'tick': rec.tick, 'epoch': rec.epoch,
                           'state_root': rec.state_root.hex(), 'files_root': rec.files_root.hex(),
                           'env_key': self.bundle.env.key()}
        parent, branch_at = self.bundle, self.sup.tick
        if capture_receipt is None and not self._restorable(parent.record(branch_at), SeekMode.REPLAY):
            # Capture cadence: no map(branch_at) to resume a branch from.
            return self._branch_rerecorded(edited, destination, on_durable=on_durable)
        placement = (id(parent), id(self.sup.backend), branch_at)
        if capture_receipt is None and self.verified_placement != placement:
            self.rewind(branch_at)
        child = create_branch(parent, branch_at, destination, score=edited.text())
        try:
            child.receipt(capture_receipt or self.last_receipt)
            sink = BundleSink(child, child.header.tier, on_durable=on_durable)
            sink.resume(branch_at, restored=capture_receipt is None)
            if capture_receipt is not None:
                try:
                    # Genesis/receipt publication must not make an earlier
                    # successful check sufficient for adopting this child:
                    # native memory can change without a call (external_pending)
                    # and host files can change during publication fsyncs. So
                    # env, files root, one native FULL_ROOT audit and the
                    # retained predicate always run again. Only when the gate,
                    # bundle and chain links are provably the ones just audited
                    # is the duplicated pre-files memory audit skipped.
                    if self._held_gate_unchanged(gate):
                        self._held_capture_receipt(pre_files_audit=False)
                    else:
                        self._held_capture_receipt()
                except Exception:
                    self._held_capture = None
                    self.broken = True
                    raise
        except Exception:
            child.close()
            raise
        # Own the writer before later output setup can fail. A failed setup
        # blocks execution; close/restore still has every handle for recovery.
        self.broken = True
        self._held_capture = None
        self.sup.sink = self.sink = sink
        self.score, self.bundle = edited, child
        self.playing, self.branch_ok = False, False
        self.sup.frames_to(Path(destination) / "frames")
        self.label("BRANCH PAUSED - EDITED INPUTS", force=True)
        self.broken = False
        return {"tick": branch_at, "take": str(destination), "held_capture": capture_ack}

    def _discard_branch(self, sink):
        """Detach and close an unsealed branch writer after a failed re-record; never raises."""
        if self.sup is not None and self.sup.sink is sink:
            self.sup.sink = None
        if self.sink is sink:
            self.sink = None
        try:
            sink.close(seal=False)
        except Exception as exc:  # noqa: BLE001 - the writer is abandoned either way
            self.log(f"discarded branch {getattr(sink.bundle, 'root', '?')} did not close cleanly: {exc}")
        self.broken = True

    def _branch_rerecorded(self, edited, destination, *, on_durable=None):
        """Archive branch at a gate the recording did not capture (capture cadence).

        There is no map(T) for a branch to resume from. Restore the latest restore point
        K < T strictly (with its continuation canary), branch at K, then re-record K..T
        from the recorded score, which equals `edited` through T (branch() checked that
        with _resume_cursor), proved tick by tick. The branch holds T with an exact history."""
        from kptas.holo import create_branch
        started_ns = time.perf_counter_ns()
        parent, target = self.bundle, self.sup.tick
        rec = parent.record(target)
        planner = self._planner(parent)
        at = planner.place(max(target, parent.last_tick))
        anchor = self._pick_anchor(planner, target, SeekMode.RESTORE, at)
        if anchor is None:
            raise TelosError(f"{self._unreachable_reason(rec)}, and no earlier restore point in its epoch has "
                             f"a score position; branch at a captured {self._unit()}")
        self._archive_rewind(anchor, strict_restore=True)
        if self.sup.tick != anchor or self.bundle is not parent:
            raise TelosError("the anchor restore did not hold its gate")
        anchor_receipt = self.last_receipt
        child = create_branch(parent, anchor, destination, score=edited.text())
        try:
            child.receipt(anchor_receipt)
            sink = BundleSink(child, child.header.tier, on_durable=on_durable)
            sink.resume(anchor, restored=True)
        except Exception:
            child.close()
            raise
        # Own the writer before the re-record; a failure leaves the session blocked for recovery.
        self.broken = True
        self._held_capture = None
        self._paused_seek_view = self._paused_seek_render = None
        self.sup.sink = self.sink = sink
        old_score = self.score
        self.score, self.bundle = edited, child
        self.playing, self.branch_ok = False, False
        self.verified_placement = self.play_cursor = None
        self.sup.frames_to(Path(destination) / "frames")
        try:
            via = self._rerecord_span(parent, anchor, target, at, anchor_receipt, started_ns=started_ns)
            child.receipt(via)
            # Acknowledge T only once its re-recorded ticks are durable.
            sink.checkpoint_if_due(force=True)
        except Exception as exc:
            # Roll back to the parent recording: discard the unsealed child, keep the session
            # blocked (the live gate is somewhere in K..T) until a rewind restores a known gate.
            self._discard_branch(sink)
            self.score, self.bundle = old_score, parent
            raise TelosError(f"branch at {self._unit()} {target} failed while re-recording from "
                             f"restore@{anchor}: {exc}; the branch was discarded, rewind to recover") from exc
        self.last_receipt = via
        self.label("BRANCH PAUSED - EDITED INPUTS", force=True)
        self.broken = False
        return {"tick": target, "take": str(destination), "held_capture": None,
                "receipt": {**receipt_dict(via), **_hybrid_fields(via)}}

    def dispatch(self, request):
        """The common command/GUI admission and execution queue."""
        op = request.get("op")
        if op == 'run_start':
            return self.run_start(request.get('score'), request.get('expected'),
                                  request_id=request.get('request_id'),
                                  ui_focus_display=request.get('ui_focus_display'),
                                  name=request.get('name'), replace=request.get('replace'))
        if op == 'run_status':
            return self.run_status(request.get('run_id'), request_id=request.get('request_id'))
        if op == 'run_cancel':
            return self.run_cancel(request.get('run_id'), request.get('session_id'))
        if op in ('run_pause', 'run_resume', 'run_unload'):
            return getattr(self, op)(request.get('run_id'), request.get('session_id'))
        # A paused score holds the game at its frame: only reads, close and the
        # stateless reconciliation barrier may run until it resumes or unloads.
        if self.run_loaded and op not in {
                'takes', 'inspect', 'state', 'render', 'view', 'close'} and not (
                op == 'reconcile' and not self.run_scheduler_running):
            raise TelosError('a score is loaded; stop or unload it before changing the session')
        if op in ('configure_tool_bindings', 'poll_tool_controls'):
            if request.get('expected') is None:
                raise TelosError('tool dispatcher requires the atomic owner token')
            with self._lock:
                backend = self.sup.backend if self.sup is not None else None
                if not getattr(backend, 'supports_tool_bindings', lambda: False)():
                    raise TelosError('native configurable tool dispatcher unavailable')
                if op == 'configure_tool_bindings':
                    with self.operation(request['expected']):
                        if self.run_loaded:
                            raise TelosError('a score is loaded; stop or unload it before changing tool bindings')
                        self._require_execution_health()
                        from .xhotkeys import native_bindings
                        backend.hud_bindings(native_bindings(request.get('shortcuts', ())))
                    return {'schema': 'kptas-tool-config/1', 'state': self.list_takes()}
                actual = self.owner_token()
                expected = request['expected']
                if (not isinstance(expected, dict) or expected.keys() != actual.keys() or
                        any(type(expected[k]) is not type(v) or expected[k] != v for k, v in actual.items())):
                    raise TelosError(f'session, take, {self._unit()} or revision changed; refresh required')
                self._require_execution_health()
                action = backend.hud_poll()
                return {'schema': 'kptas-tool-poll/1', 'action': action, 'state': self.list_takes()}
        if op == 'set_engaged':
            if request.get('expected') is None:
                raise TelosError('input source transition requires the atomic owner token')
            with self._lock:
                with self.operation(request['expected']):
                    result = self.set_engaged(request.get('engaged'), request.get('pause_on_engage', True))
                return {**result, 'state': self.list_takes()}
        observation_flags = {'include_state', 'include_inspect'} & request.keys()
        if observation_flags:
            if (op != 'step' or any(type(request[k]) is not bool for k in observation_flags)
                    or request.get('include_inspect', False) and not request.get('include_state', False)):
                raise TelosError('step observation requires boolean include_state and optional include_inspect')
            if request.get('include_state', False):
                # Keep the outer owner lock across the original operation and
                # observation, but let its revision finally complete first.
                with self._lock:
                    if self._operation_depth:
                        raise TelosError('step observation requires a completed owner boundary')
                    canonical = {k: v for k, v in request.items() if k not in observation_flags}
                    result = self.dispatch(canonical)
                    observation = {'schema': 'kptas-step-observation/1', 'state': self.list_takes()}
                    if request.get('include_inspect', False):
                        observation['inspect'] = self.inspect()
                    return {**result, 'observation': observation}
        if op == 'view':
            with self._lock:
                before = self.owner_token()
                expected = request.get('expected')
                if expected is not None and (not isinstance(expected, dict) or expected.keys() != before.keys()
                        or any(type(expected[k]) is not type(v) or expected[k] != v for k, v in before.items())):
                    raise TelosError(f'session, take, {self._unit()} or revision changed; refresh required')
                state = self.list_takes()
                frame = self.render()
                if before != self.owner_token() or state['owner'] != before or state['tick'] != frame['tick']:
                    raise TelosError('owner changed during view; refresh required')
                return {'schema': 'kptas-view-observation/1', 'state': state, 'frame': frame}
        ui_focus = request.get('ui_focus_display')
        if 'ui_focus_display' in request and (op != 'step' or type(ui_focus) is not str or not ui_focus
                or request.get('expected') is None):
            raise TelosError('UI execution focus requires an explicit display and expected step owner')
        if op == 'reconcile':
            return self.reconcile(request.get('previous'))
        if op in {"takes", "inspect", "state", "render", "video"}:
            with self._lock:
                if op == "takes": return self.list_takes()
                if op in {"inspect", "state"}: return self.inspect()
                if op == "render": return self.render()
                return self.video(request.get("out"))
        if op not in {"start", "step", "mark", "rewind", "close", "play_pause", "save", "branch", "save_recording", "load_recording", "set_engaged"}:
            raise TelosError(f"unknown op {op!r}; expected start, step, inspect, state, render, mark, rewind, takes, video or close")
        with self.operation(request.get("expected")):
            if (not getattr(self, 'engaged', True) and
                    op in {'step', 'mark', 'rewind', 'branch', 'load_recording', 'play_pause'}):
                raise TelosError('engage TAS before authoring or seeking')
            if op in {'step', 'rewind', 'branch', 'load_recording'}:
                self._engagement_resume_note = None
            if op == 'play_pause' and self.mode == 'recording' and self._engagement_resume_note is not None:
                self._engagement_resume_note = None
                # Pump frames always capture, so the pause gate is a restore point. If it somehow is
                # not, capture one more frame now and say so: the user sees the extra recorded frame.
                ack = {'tick': self.sup.tick, 'playing': False}
                if self._held_gate_captured() is False:
                    try:
                        ack.update(self._capture_notice(self._capture_held_gate()))
                    except TelosError as exc:
                        ack['notice'] = f'paused at an uncaptured {self._unit()}: {exc}'
                    ack['tick'] = self.sup.tick
                return ack
            if op == "close": return self.close()
            if op == "start": return self.start(request.get("prefix"))
            if op == "save": return self.save()
            if op == 'save_recording': return self.save_recording()
            if op == 'load_recording':
                result = self.load_recording(request['path'], request.get('tick', 0))
                if getattr(self, '_run_queue', None) is not None:
                    self._run_queue.history.clear()  # Score starts belong to the replaced recording.
                return result
            if op == "branch": return self.branch(compile_score(request["score"]), request["out"])
            if self.mode == "archive":
                if op == "step":
                    if "action" in request:
                        raise TelosError("archive step follows its authored score; branch before editing inputs")
                    advanced = self.step(ui_focus_display=ui_focus)
                    return {"tick": self.sup.tick, "advanced": advanced}
                if op == "rewind":
                    result = self.rewind(int(request["tick"]), strict_restore=request.get("strict_restore") is True)
                    if isinstance(result, dict) and result.get("noop") is True:
                        return result
                    return {"rewound": True, "tick": self.sup.tick, "receipt": self._rewind_receipt()}
                if op == "play_pause":
                    self.toggle_play()
                    return {"tick": self.sup.tick, "playing": self.playing}
                raise TelosError(f"{op} requires an active recording")
            if op == "step": return self.step(request.get("action", "."), ui_focus_display=ui_focus)
            if op == "mark": return self.mark(str(request["name"]))
            if op == "rewind":
                result = self.rewind(int(request["tick"]), strict_restore=request.get("strict_restore") is True)
                if getattr(self, '_run_queue', None) is not None:
                    self._run_queue.prune_history(self.sup.tick)  # Later score starts were discarded.
                return result
            raise TelosError(f"{op} requires an archive playback session")

    @property
    def run_scheduler_running(self):
        queue = getattr(self, '_run_queue', None)
        return queue is not None and queue.active

    @property
    def run_loaded(self):
        """A queued, running or paused score holds the owner at its frame."""
        queue = getattr(self, '_run_queue', None)
        return queue is not None and queue.loaded

    def _run_durable_boundary(self):
        """The acknowledged gate if it is durable, else None.

        A restore-only rewind lands on a new take whose gate is durable in its
        sealed parent; the take records no local checkpoint until it steps.
        """
        tick = self.sup.tick if self.sup is not None else None
        if not self.takes or self.cur.last != tick:
            return None
        if self.cur.durable_tick == tick:
            return tick
        if self.cur.durable_tick is None and self.cur.branch_tick == tick and self.cur.parent:
            parent = next((t for t in self.takes[:-1] if t.path == self.cur.parent), None)
            if (parent is not None and parent.sealed and type(parent.durable_tick) is int
                    and parent.durable_tick >= tick):
                return tick
        return None

    def _run_admission_check(self, ui_focus_display, verb='loading'):
        """verb: 'loading' a new score or 'resuming' a paused one, for plain refusals."""
        unit = self._unit()
        self._live_ok()
        self._require_execution_health()
        if self.mode != 'recording':
            raise TelosError('scores play only in a live recording')
        if not self.engaged:
            raise TelosError(f'engage TAS before {verb} the score')
        if self.input_scheduler_running or self.playing or self._physical_step_in_progress:
            raise TelosError(f'pause playback before {verb} the score')
        if self.cfg.get('clock', {}).get('precision') != 'frame':
            raise TelosError('scores play only with frame clock precision')
        if getattr(self.sup, 'hz', 60) != 60:
            raise TelosError(f'scores play live only at 60 {unit}s per second; this game runs at {self.sup.hz}')
        if self._run_durable_boundary() is None:
            raise TelosError(f'the current {unit} is not saved yet; step once or save, then try again')
        if ui_focus_display is not None and not callable(
                getattr(self.sup.backend, 'request_ui_execution_focus', None)):
            raise TelosError('this owner cannot hand UI execution focus to its game')

    def run_start(self, score, expected, *, request_id=None, ui_focus_display=None, name=None, replace=None):
        """Admit a score at the acknowledged gate.

        replace names the paused score it takes over from: that score ends at
        this same gate and keeps its unexecuted inputs as the latest remainder.
        Every check precedes the hand-over, so a refusal leaves it paused.
        """
        from .run_queue import compile_run, run_name, MAX_RUNS
        # No owner state or native input changes until every Note is admitted.
        notes, total, digest = compile_run(score)
        name = run_name(name)
        if (request_id is not None and
                (type(request_id) is not str or not 1 <= len(request_id) <= 128)):
            raise TelosError('request_id must be a string of 1..128 characters')
        if ui_focus_display is not None and (type(ui_focus_display) is not str or not ui_focus_display):
            raise TelosError('UI execution focus requires an explicit display')
        if replace is not None and (type(replace) is not str or not replace):
            raise TelosError('replace must name the loaded run')
        if expected is None:
            raise TelosError('run start requires the atomic owner token')
        with self._lock:
            previous = self._run_queue.requests.get(request_id) if request_id is not None else None
            if previous is not None:
                if (previous.score_sha256 != digest or previous.ui_focus_display != ui_focus_display
                        or not isinstance(expected, dict) or expected.keys() != previous.starting_owner.keys()
                        or any(type(expected[k]) is not type(value) or expected[k] != value
                               for k, value in previous.starting_owner.items())):
                    raise TelosError('request_id already names a different run start')
                return previous.snapshot()
            if self._operation_depth:
                raise TelosError('run start requires a completed owner boundary')
            queue = self._run_queue
            with self.operation(expected):
                current = queue.current if queue.loaded else None
                if replace is None and current is not None:
                    raise TelosError('a score is already loaded; unload it or load the new score in its place')
                if replace is not None and (current is None or current.run_id != replace):
                    raise TelosError('the loaded score changed; refresh and try again')
                if current is not None and current.status != 'paused':
                    raise TelosError('pause the loaded score before replacing it')
                if len(queue.runs) >= MAX_RUNS:
                    raise TelosError('owner run receipt limit reached; preserve this session before starting another owner')
                self._run_admission_check(ui_focus_display)
                if current is not None and (current.tick != self.sup.tick
                                            or current.starting_owner['take'] != self.cur.path):
                    raise TelosError('the paused score no longer matches the game; refresh and try again')
                replaced = None
                if current is not None:
                    queue.end(current, 'cancelled', reason='replaced')
                    current.final_owner = self.owner_token()
                    current.final_owner['revision'] += 1
                    queue.unload()
                    text, frames = queue.remainder_of(current.run_id)
                    replaced = {'run_id': current.run_id, 'name': current.name, 'tick': current.tick,
                                'remainder': text, 'remaining_frames': frames}
                run = queue.admit(notes=notes, total=total, digest=digest,
                    owner=self.owner_token(), durable_tick=self.sup.tick,
                    request_id=request_id, display=ui_focus_display,
                    name=name, kind='load' if current is None else 'splice')
            result = run.snapshot()
            if replaced is not None:
                result['replaced'] = replaced
            return result

    def run_status(self, run_id=None, *, request_id=None):
        with self._lock:
            run = self._run_queue.find(run_id, request_id)
            result = run.snapshot()
            if not run.loaded:
                # The latest ended run's unexecuted inputs, so a Stop or a lost
                # unload/replace ACK can still keep them as a draft.
                text, frames = self._run_queue.remainder_of(run.run_id)
                if text:
                    result.update(remainder=text, remaining_frames=frames)
            return result

    def _cancel_active_run(self):
        if self.run_loaded:
            run = self._run_queue.current
            self._run_queue.end(run, 'cancelled', reason='stopped')
            # close() is the only implicit cancellation; no neutral frame is
            # added and the last acknowledged input remains the recording's.
            run.final_owner = self.owner_token()
            if self._operation_depth:
                run.final_owner['revision'] += 1

    def _run_control(self, run_id, session_id, what):
        if type(run_id) is not str or not run_id or session_id != self.session_id:
            raise TelosError(f'{what} requires the score run_id and owner session_id')
        if self._operation_depth:
            raise TelosError(f'{what} requires a completed owner boundary')
        return self._run_queue.find(run_id)

    def run_cancel(self, run_id, session_id):
        with self._lock:
            run = self._run_control(run_id, session_id, 'run cancellation')
            if run.loaded:
                with self.operation():
                    self._run_queue.end(run, 'cancelled', reason='stopped')
                run.final_owner = self.owner_token()
            return run.snapshot()

    def run_pause(self, run_id, session_id):
        """Stop scheduling at the last acknowledged frame; idempotent by run_id.

        The pump serves commands only between acknowledged batches, so the
        pause lands on a recorded, captured gate. No input is added or dropped.
        """
        with self._lock:
            run = self._run_control(run_id, session_id, 'pause')
            if run.active and run is self._run_queue.current:
                with self.operation():
                    run.pause()
            return run.snapshot()

    def run_resume(self, run_id, session_id):
        """Continue a paused score with its next unexecuted input at the same gate."""
        with self._lock:
            run = self._run_control(run_id, session_id, 'resume')
            if run.status != 'paused' or run is not self._run_queue.current:
                return run.snapshot()
            unit = self._unit()
            if (self.closed or self.broken or self.sup is None or self.sup.tick != run.tick
                    or self.cur.path != run.starting_owner['take']):
                raise TelosError(f'the game left the paused {unit} {run.tick}; unload the score, '
                                 'then load its remainder here')
            with self.operation():
                self._run_admission_check(run.ui_focus_display, 'resuming')
                if self._run_durable_boundary() != run.tick:
                    raise TelosError(f'the paused {unit} {run.tick} is not saved; unload the score')
                run.resume()
            return run.snapshot()

    def run_unload(self, run_id, session_id):
        """End the score at its current frame and drop it; the game stays there.

        Idempotent: unloading an ended run only clears it from display, and the
        latest ended run still returns its unexecuted inputs as `remainder`.
        """
        with self._lock:
            run = self._run_control(run_id, session_id, 'unload')
            queue = self._run_queue
            if run.loaded:
                with self.operation():
                    queue.end(run, 'cancelled', reason='unloaded')
                run.final_owner = self.owner_token()
            if run is queue.current:
                queue.unload()
            text, frames = queue.remainder_of(run.run_id)
            return {**run.snapshot(), 'remainder': text, 'remaining_frames': frames}

    def run_scheduler_step(self, max_frames=1, capture_last=True):
        """Advance 1..max_frames frames of one authored Note in one owner step.

        Every frame is still its own recorded gate; a batch never crosses a
        Note boundary and is never retried. capture_last=False (capture cadence,
        the frame pump steps on at once) leaves the batch's last gate off the
        capture.every grid uncaptured; the batch that completes the run always
        captures, so the run's end is a restore point.
        """
        with self._lock:
            if not self.run_scheduler_running:
                return None
            run = self._run_queue.current
            import time
            started = time.perf_counter_ns()
            phases = {}
            acknowledged = False
            frames = max(1, min(int(max_frames), run.frames_left_in_note()))
            capture_last = capture_last or run.executed_frames + frames >= run.total_frames
            try:
                with self.operation():
                    if self.closed or self.broken or self.mode != 'recording' or not self.engaged:
                        raise TelosError('authored run lost its recording owner')
                    if self.sup.tick != run.tick or self.cur.path != run.starting_owner['take']:
                        raise TelosError('authored run boundary changed outside its queue')
                    run.status = 'running'
                    self._run_step_in_progress = True
                    try:
                        result = self.step(run.frame(frames), ui_focus_display=(
                                               run.ui_focus_display if run.frame_in_note == 0 else None),
                                           _run_prefix=run.authored_prefix(frames),
                                           _run_continuation=run.frame_in_note > 0,
                                           _run_timing=phases, capture_last=capture_last)
                    finally:
                        self._run_step_in_progress = False
                    if result['tick'] != run.tick + frames or self.cur.durable_tick != result['tick']:
                        raise TelosError(f'authored batch did not acknowledge exactly its durable {self._unit(True)}')
                    run.acknowledge(result['tick'], self.cur.durable_tick, frames)
                    acknowledged = True
            except Exception as exc:
                self.broken = True
                # A failed gate can have advanced native state without its full
                # publication. Report that observed boundary, never count it as
                # an acknowledged queue frame or silently submit it again.
                run.tick = self.sup.tick if self.sup else run.tick
                run.durable_tick = self.cur.durable_tick if self.takes else run.durable_tick
                run.finish('failed', error={'code': type(exc).__name__, 'message': str(exc)})
                raise
            finally:
                run.record_timing(time.perf_counter_ns() - started, phases, acknowledged, frames)
                if not run.active:
                    run.final_owner = self.owner_token()
            return run.snapshot()

    @owned_operation
    def set_engaged(self, engaged, pause_on_engage=True):
        if type(engaged) is not bool or type(pause_on_engage) is not bool:
            raise TelosError('engagement and pause policy must be booleans')
        self._live_ok()
        self._require_execution_health()
        backend = self.sup.backend
        supports = getattr(backend, 'supports_input_source', None)
        if self.mode != 'recording' or not callable(supports) or not supports():
            raise TelosError('native acknowledged input source protocol unavailable')
        changed = engaged != getattr(self, 'engaged', True)
        try:
            acknowledgement = backend.set_engaged(engaged)
        except Exception:
            # A missing transition acknowledgement can mean native authority
            # changed. Never continue with an assumed source or auto-retry it.
            self.broken = True
            raise
        self.engaged = engaged
        self.pause_on_engage = pause_on_engage
        self._physical_input_error = None
        if changed:
            self.playing = False
            self._engagement_resume_note = None
            if engaged and not pause_on_engage:
                previous = self.items[-1] if self.items else Note(1)
                self._engagement_resume_note = dataclasses.replace(
                    previous, count=1, wheel=0, osev=None) if isinstance(previous, Note) else Note(1)
        return {'schema': 'kptas-engagement/1', 'tick': self.sup.tick,
                'engaged': engaged, 'input_source': acknowledgement['input_source'],
                'pause_on_engage': pause_on_engage, 'changed': changed,
                'running': self.input_scheduler_running, 'qualified': False}

    @property
    def input_scheduler_running(self):
        return (getattr(self, 'mode', None) == 'recording' and not self.closed and
                not self.broken and getattr(self, '_physical_input_error', None) is None and
                (not getattr(self, 'engaged', True) or
                 getattr(self, '_engagement_resume_note', None) is not None))

    @owned_operation
    def input_scheduler_step(self, capture_last=True):
        """One owner gate, ordinary pending Note and durable sink; no free run.

        capture_last=False (capture cadence): the frame pump steps on at once, so this
        gate is captured only on the capture.every grid. The pump passes True whenever a
        command is waiting, so a pause it serves lands on a captured gate."""
        if not self.input_scheduler_running:
            return None
        self._input_scheduler_step_in_progress = True
        before_tick = self.sup.tick
        try:
            if getattr(self, 'engaged', True):
                return self.step(self._engagement_resume_note, capture_last=capture_last)
            try:
                note = self.sup.backend.prepare_physical_note()
            except Exception as exc:
                # Observation failure moves no game frame. Park and retain
                # the durable owner; explicit Engage can recover overflow.
                self._physical_input_error = str(exc)
                raise
            self._physical_step_in_progress = True
            return self.step(note, capture_last=capture_last)
        except Exception as exc:
            # Failure may occur before release (pending persistence), during
            # native execution, or after it (durable publication/preview).
            # Never retry an automatic input frame after an uncertain outcome.
            # Retain its durable/pending evidence and require an explicit owner
            # recovery rather than silently advancing the next physical frame.
            self._engagement_resume_note = None
            self._physical_input_error = str(exc)
            journal = getattr(getattr(self.sink, 'bundle', None), 'journal', None)
            if self.sup.tick != before_tick or getattr(journal, 'write_failure', None) is not None:
                # An advanced transaction or poisoned writer needs explicit
                # close/recovery. Engage cannot clear it and authorize another
                # input against an unacknowledged durable boundary.
                self.broken = True
            raise
        finally:
            self._physical_step_in_progress = False
            self._input_scheduler_step_in_progress = False

    @owned_operation
    def park_input_scheduler(self, reason):
        """Keep the acknowledged source and durable gate; never disconnect."""
        self._engagement_resume_note = None
        if not getattr(self, 'engaged', True):
            self._physical_input_error = str(reason)

    def reconcile(self, previous):
        """An explicit healthy-owner barrier; never repeat the uncertain command.

        Advancing revision also refuses a delayed command carrying the previous
        CAS token. Return the token only after operation accounting completes.
        """
        with self._lock:
            if self.run_scheduler_running:
                raise TelosError('use run status to reconcile an active authored run')
            current = self.owner_token()
            if (not isinstance(previous, dict) or previous.keys() != current.keys()
                    or any(type(previous[k]) is not type(v) for k, v in current.items())
                    or previous['session_id'] != self.session_id
                    or not 0 <= previous['revision'] <= self.revision):
                raise TelosError('reconciliation requires the same atomic owner session')
            if self._operation_depth:
                raise TelosError('reconciliation requires a completed command boundary')
            with self.operation():
                if (self.closed or self.broken or self.mode not in {'recording', 'archive'}
                        or self.sup is None or self.sup.backend is None or not self.sup.alive
                        or getattr(self.sup.backend, 'poisoned', False)):
                    raise TelosError('owner needs recovery before reconciliation')
                if self.mode == 'recording':
                    self._live_ok()
                    if self.cur.last != self.sup.tick or self.cur.durable_tick != self.sup.tick:
                        raise TelosError('owner lacks an acknowledged durable gate')
                elif self.sink is not None:
                    if self.sup.sink is not self.sink or self._archive_durable_tick() != self.sup.tick:
                        raise TelosError('archive branch lacks an acknowledged durable gate')
                else:
                    self.bundle.record(self.sup.tick)
            return {'schema': 'kptas-owner-reconciliation/1', 'healthy': True,
                    'barrier': True, 'owner': self.owner_token()}

    # ------------------------------------------------------------ helpers

    @property
    def cur(self) -> Take:
        if not self.takes:
            raise TelosError("no session: send start first")
        return self.takes[-1]

    def text(self, items: Iterable[Any] | None = None) -> str:
        return CompiledScore(dict(self.header), self.items if items is None else items, {}, []).text()

    def _write_manifest(self, *, closed=None) -> None:
        if self.manifest is None:
            return
        from kptas.durability import atomic_json
        atomic_json(self.manifest, {
            "format": "telos-live/1", "tick": self.sup.tick if self.sup else None,
            "closed": self.closed if closed is None else closed, "takes": [dataclasses.asdict(t) for t in self.takes],
            "precision_effective": self._precision_effective()})

    def _precision_effective(self) -> dict[str, Any]:
        """The resolved precision, its user-facing unit and the sub-frame journal flags this owner runs
        with. A record of the effective config only: never part of the determinism key or journal bytes."""
        from kptas.config import SUBFRAME_JOURNAL_KEYS
        cfg = getattr(self, "cfg", None) or {}
        clock, journal = cfg.get("clock", {}), cfg.get("journal", {})
        return {"precision": clock.get("precision"), "unit": self._unit(), "hz": clock.get("hz"),
                "journal": {k: journal.get(k) for k in SUBFRAME_JOURNAL_KEYS}}

    @staticmethod
    def _atomic_bytes(path: Path, data: bytes) -> None:
        from kptas.platform_sync import require_directory_barrier, sync_dir
        require_directory_barrier()
        tmp=path.with_name(path.name+'.tmp-'+uuid.uuid4().hex)
        with tmp.open('xb') as f:
            f.write(data);f.flush();os.fsync(f.fileno())
        os.replace(tmp,path);sync_dir(path.parent)

    def _write_score(self, take: Take) -> None:
        data=self.text().encode('utf-8');digest=hashlib.sha256(data).hexdigest()
        version=Path(take.path)/('authoring-'+digest+'.score')
        # The old manifest still names an immutable prior version if publication
        # is interrupted between the score update and the next manifest.
        if not version.exists():self._atomic_bytes(version,data)
        elif version.read_bytes()!=data:raise TelosError('authored score digest collision or corruption')
        self._atomic_bytes(Path(take.path)/'live.score',data)
        take.score_file,take.score_sha256=version.name,digest

    def _pending(self, action: str, items: Iterable[Any]) -> None:
        from kptas.durability import atomic_json
        atomic_json(Path(self.cur.path)/'live.pending.json', {
            'format':'telos-live-pending/1','status':'pending','action':action,
            'before_tick':self.sup.tick,'candidate_score':self.text(items),
            'executed':False})

    def _persist(self) -> None:
        """Acknowledge actions only after their executed boundary and authoring text persist."""
        try:
            if self.sink is not None:
                if self.sink.bundle.chain.last_tick>=0:
                    result=self.sink.checkpoint_if_due(force=True)
                    if result is not None:self.cur.durable_tick=result['tick']
                else:
                    # A fresh child has only inherited ticks. Its genesis and
                    # receipt still need a barrier, but it has no local executed
                    # tick eligible for checkpoint() yet.
                    from kptas.platform_sync import sync_dir
                    self.sink.bundle.journal.fsync()
                    sync_dir(self.sink.bundle.root);sync_dir(self.sink.bundle.root.parent)
            self._write_score(self.cur)
            self._write_manifest()
            from kptas.durability import atomic_json
            atomic_json(Path(self.cur.path)/'live.pending.json', {
                'format':'telos-live-pending/1','status':'committed',
                'tick':self.cur.last,'durable_tick':self.cur.durable_tick,
                'score_sha256':self.cur.score_sha256})
        except Exception:
            self.broken=True
            raise

    def _frame(self, upto: int, since: int) -> None:
        """Point every tick in since+1..upto (and since itself when since == upto) at one render."""
        if not self.own_frames or self.sup is None or self.sup.backend is None or not self.sup.alive:
            return
        try:
            png = self.sup.backend.frame()
        except Exception as e:      # a backend without frames: no video, but the session goes on
            self.log(f"image at {self._unit()} {upto}: {type(e).__name__}: {e}")
            return
        if not png:
            return
        w, h = _png_size(png)
        d = Path(self.cur.path) / "frames"
        d.mkdir(exist_ok=True)
        name = f"live-{upto:08d}-{self.frame_seq:06d}.png"
        (d / name).write_bytes(png)
        lo = since + 1 if upto > since else upto
        with open(d / "index.jsonl", "a", encoding="utf-8") as f:
            for t in range(lo, upto + 1):
                self.frame_seq += 1
                f.write(json.dumps({"seq": self.frame_seq, "tick": t, "epoch": self.sup.epoch, "file": name,
                                    "frame": 0, "w": w, "h": h,
                                    "flags": [] if t == upto else ["repeat"]}) + "\n")

    def _ceiling(self) -> Tier | None:
        return self.sup.caps.tier_ceiling if self.sup is not None and self.sup.caps else None

    def _live_ok(self) -> None:
        if self.sup is None or self.closed:
            raise TelosError("no session: send start first")
        if self.broken or self.sink is None or self.sup.sink is not self.sink:
            raise TelosError("the session needs recovery after a failed rewind or save; stepping is stopped")

    # ------------------------------------------------------------ ops

    @owned_operation
    def start(self, prefix: str | None = None) -> dict[str, Any]:
        from kptas.telos.cli import build_header, holo, pin_env
        if self.sup is not None or self.closed:
            raise TelosError("the session has already started")
        score = self.ctx.compile(prefix) if prefix else compile_score("#!score v1\n")
        raw,_=self.ctx.raw()
        requested=self.over.get('seek__mode',raw.get('seek',{}).get('mode','restore'))
        mode=SeekMode.RESTORE if requested=='auto' else SeekMode.parse(requested)
        if mode not in (SeekMode.RESTORE,SeekMode.REPLAY):
            raise TelosError('live rewind supports strict restore or explicitly configured replay')
        if self.over.get('verify__canary_every_seeks', 1) != 1:
            raise TelosError('live restore requires a continuation canary on every rewind; use canary_every_seeks=1')
        over = {"run__backend": self.backend_name, **self.over, "seek__mode": mode.value,
                "seek__on_incompatible": "error", "seek__fallback_chain": [],
                "verify__on_mismatch": "halt", "verify__canary_every_seeks": 1}
        cfg = self.cfg = self.ctx.cfg(score.header, **over)
        if mode is SeekMode.RESTORE and cfg["store"]["pages"] == "digests":
            raise TelosError("telos live rewinds by restore, which needs stored pages; store.pages is digests")
        if not mode.allowed(Tier.parse(cfg["run"]["tier"])):
            raise TelosError(f"{mode} is not allowed in tier {cfg['run']['tier']}")
        out_dir = Path(os.path.expanduser(cfg["run"]["out_dir"]))
        out_dir.mkdir(parents=True, exist_ok=True)
        n = 1
        while (out_dir / f"live-{n}.holo").exists() or (out_dir / f"live-{n}{MANIFEST}").exists():
            n += 1
        dest = out_dir / f"live-{n}.holo"
        self.manifest = out_dir / f"live-{n}{MANIFEST}"
        env = pin_env(cfg)
        header = build_header(cfg, score, env)
        b = holo("Bundle").Bundle.create(dest, header, score.text(), env, config=cfg)
        self.sink = BundleSink(b, header.tier)
        self.header, self.items = dict(score.header), list(score.items)
        self.own_frames = cfg["capture"]["frames"] == "off" and not cfg["capture"].get("frames_view", False)
        self.takes.append(Take(str(dest)))
        self.create_supervisor(cfg, env=env, attach=self.attach, sink=self.sink, run_name=dest.stem, log=self.log)
        self.mode = "recording"
        self._write_score(self.cur)
        self._write_manifest()
        rep = self.sup.open()
        self.cur.first = rep.tick
        self._frame(rep.tick, rep.tick)
        last = [rep]
        if score.items:
            def on_report(r: Any, _n: Any) -> None:
                self._frame(r.tick, last[0].tick)
                last[0] = r
            self.sup.run_score(score, on_report=on_report)
        self.cur.last = self.sup.tick
        self._persist()
        return {"take": str(dest), "session": str(self.manifest), "tick": self.sup.tick,
                "epoch": self.sup.epoch, "backend": self.sup.name}

    def _execution_status(self):
        """Transport eligibility only, separate from offline journal ownership."""
        # A live transport cannot establish recording ownership after a
        # failed restore or publication. Keep queries available while exposing
        # the same recovery requirement already enforced by input admission.
        if getattr(self, 'broken', False):
            return 'dead', 'session recording ownership needs recovery; execution is stopped'
        sup = self.sup
        backend = getattr(sup, 'backend', None)
        probe = getattr(backend, 'execution_health', None)
        if (getattr(self, 'closed', False) or getattr(sup, 'alive', True) is False
                or sup is not None and backend is None):
            status = 'dead'
        elif not callable(probe):
            return None, None  # Other backend contracts retain their admission.
        else:
            try:
                status = probe()
            except (BackendError, OSError):
                status = 'unknown'
            if status not in {'healthy', 'unverified', 'dead', 'unknown'}:
                status = 'unknown'
        error = (None if status in {'healthy', 'unverified'} else
                 'owned game connection is closed or poisoned; execution is stopped' if status == 'dead' else
                 'owned game connection health is unknown; execution is stopped')
        return status, error

    def _require_execution_health(self):
        _, error = self._execution_status()
        if error:
            self.playing = False
            raise TelosError(error)

    def _ui_execution_focus(self, display, note):
        if display is None:
            return
        # Authored OS transitions and their blurred continuation remain the
        # score's behavior. UI presentation never normalizes them into focus.
        if not isinstance(note, Note) or note.osev is not None or getattr(self.sup, 'last_osev', None) == 'blur':
            return
        focus = getattr(self.sup.backend, 'request_ui_execution_focus', None)
        if not callable(focus):
            raise TelosError('this owner cannot hand UI execution focus to its game')
        focus(display)

    @owned_operation
    def step(self, action: Any = None, *, transitions: bool = False, ui_focus_display=None,
             _run_prefix=None, _run_continuation=False, _run_timing=None, capture_last: bool = True):
        """capture_last=False (capture cadence): the caller steps on at once, so the last tick is
        captured only on the capture.every grid. Only the owner's free-running schedulers pass it,
        never for a pause point; an explicit step (frame advance) always captures its gate."""
        if (_run_prefix is not None or _run_continuation or _run_timing is not None) and not self._run_step_in_progress:
            raise TelosError('queued frame metadata requires the active owner scheduler')
        if not getattr(self, 'engaged', True) and not getattr(self, '_physical_step_in_progress', False):
            raise TelosError('engage TAS before authored stepping')
        if not getattr(self, '_input_scheduler_step_in_progress', False):
            self._engagement_resume_note = None
        if getattr(self, "mode", "archive") == "archive":
            if action is not None:
                raise TelosError("archive step follows its authored score; branch before editing inputs")
            return self._archive_step(transitions=transitions, ui_focus_display=ui_focus_display)
        if action is None:
            action = "."
        from kptas.gym import to_note
        self._live_ok()
        note = to_note(action)
        if note.count < 1:
            raise TelosError(f"a step holds its note for at least one {self._unit()}")
        self._require_execution_health()
        self._ui_execution_focus(ui_focus_display, note)
        before = self.sup.tick
        authored = _run_prefix if _run_prefix is not None else note
        prefix = islice(self.items, len(self.items) - 1) if _run_continuation else self.items
        def timed(name, fn, *args):
            if _run_timing is None:
                return fn(*args)
            import time
            started = time.perf_counter_ns()
            try:
                return fn(*args)
            finally:
                _run_timing[name] = time.perf_counter_ns() - started

        timed('pending_ns', self._pending, "step", chain(prefix, (authored,)))
        cadence = {} if capture_last else {'capture_last': False}
        try:
            rep = (self.sup.step(note.count, note, keep_carry=True, **cadence) if _run_continuation
                   else self.sup.step(note.count, note, **cadence))
        except Exception:
            self.broken=True
            raise
        executed=rep.tick-before
        if not 0<=executed<=note.count:
            self.broken=True
            raise TelosError(f'step returned an inconsistent executed {self._unit()} count')
        if executed:
            if _run_continuation:
                self.items[-1] = authored
            else:
                self.items.append(dataclasses.replace(authored, count=executed))
        self.cur.last = rep.tick
        timed('persist_ns', self._persist)
        timed('frame_export_ns', self._frame, rep.tick, before)
        # An uncaptured gate (capture cadence) has no root: state_root stays the 64-hex null
        # digest the recorded journal holds, and the result says "not captured" beside it.
        captured = self._held_gate_captured()
        return {"tick": rep.tick, "epoch": rep.epoch, "outcome": rep.outcome.value,
                "state_root": rep.state_root.hex(), "captured": captured is True,
                "state": {True: "captured", False: "not captured"}.get(captured, "unknown"),
                "events": [[e.name, e.tick, e.arg] for e in rep.events]}

    def inspect(self) -> dict[str, Any]:
        """Read the last acknowledged journal from the resident take, without RPC."""
        import math

        if self.mode == "archive":
            if self.closed or self.sup is None:
                raise TelosError("archive session is closed")
            bundle, tick = self.bundle, self.sup.tick
            take = Take(str(bundle.root))
            durable = self._archive_durable_tick()
            provenance = "archive_journal"
        else:
            self._live_ok()
            take, bundle = self.cur, self.sink.bundle
            tick = self.sup.tick
            inherited = bundle.chain.last_tick < 0 and take.branch_tick == tick
            if take.last != tick or (bundle.last_tick != tick and not inherited):
                raise TelosError(f"recorded state does not match the acknowledged session {self._unit()}")
            durable = take.durable_tick
            provenance = "local_checkpoint"
            if inherited:
                if durable is not None:
                    raise TelosError(f"inherited state unexpectedly claims a local durable {self._unit()}")
                # A fresh child has no local checkpoint. Rewind acknowledged a
                # verified inherited record after persisting its genesis/receipt.
                parent = next((t for t in reversed(self.takes[:-1]) if t.path == take.parent), None)
                if (parent is not None and parent.sealed and parent.durable_tick is not None
                        and parent.durable_tick >= tick and take.seek is not None
                        and take.seek.get("result") == "pass"):
                    durable = tick
                    provenance = "inherited_checkpoint"
            if durable != tick:
                raise TelosError(f"recorded state has no matching acknowledged durable {self._unit()}")
        record = bundle.record(tick)  # Always reads journal framing/CRC, even with decoded cache.
        if record.tick != tick:
            raise TelosError(f"recorded journal {self._unit()} differs from the acknowledged boundary")
        if provenance == "inherited_checkpoint" and take.seek.get("recorded_root") != record.state_root.hex():
            raise TelosError("inherited journal root differs from the acknowledged rewind")
        if record.journal and hashlib.sha256(record.journal).digest() != record.journal_sha256:
            raise TelosError("recorded journal digest mismatch")
        journal = json.loads(record.journal) if record.journal else {}
        if not isinstance(journal, dict):
            raise TelosError("recorded journal is not an object")
        semantics = journal.get("sem")
        floats = {}
        pc = semantics.get("pc", {}) if isinstance(semantics, dict) else {}
        if isinstance(pc, dict):
            for field in ("pos", "ori"):
                if field not in pc:
                    continue
                values = pc[field]
                if not isinstance(values, list):
                    raise TelosError(f"recorded pc.{field} is not a float-byte list")
                decoded = []
                for value in values:
                    if not isinstance(value, str) or len(value) != 8:
                        raise TelosError(f"recorded pc.{field} has invalid float bytes")
                    raw = bytes.fromhex(value)
                    number, = struct.unpack("<f", raw)
                    bits, = struct.unpack("<I", raw)
                    decoded.append({"le_hex": value, "bits": f"0x{bits:08x}",
                                    "float": number if math.isfinite(number) else None})
                floats[field] = decoded
        from kptas.types import TickFlag
        captured = bool(record.flags & TickFlag.CAPTURED)   # capture cadence: no root when False
        return {"source": "recorded_journal", "take": take.path, "tick": tick,
                "durable_tick": durable, "durability_source": provenance,
                "epoch": record.epoch, "state_root": record.state_root.hex(),
                "captured": captured, "state": "captured" if captured else "not captured",
                "journal_sha256": record.journal_sha256.hex(), "journal": journal,
                "semantics": semantics, "pc_float32": floats}

    def _arm_paused_seek_view(self, bundle, tick):
        """Pin original produced output after strict seek, separately from physical GL state."""
        record = bundle.record(tick)
        self._label_sent = None      # a restore/seek may reset the native strip; resend on the next label
        token = dict(self.owner_token())
        # owned_operation publishes the outer revision after this method returns.
        if self._operation_depth:
            token['revision'] += 1
        self._paused_seek_view = (token, bundle, record.epoch, record.state_root.hex(),
                                  self.sup, self.sup.backend, self.sup.epoch, self.sup.offset)
        self._paused_seek_render = None

    def _paused_seek_frame(self):
        candidate = getattr(self, '_paused_seek_view', None)
        if candidate is None:
            return None
        token, bundle, epoch, root, sup, backend, owner_epoch, owner_offset = candidate
        if (self.broken or self.playing or self._operation_depth
                or sup is not self.sup or backend is not self.sup.backend
                or self.sup.epoch != owner_epoch or self.sup.offset != owner_offset
                or token != self.owner_token() or self._execution_status()[0] != 'healthy'):
            self._paused_seek_view = self._paused_seek_render = None
            return None
        observed = self.inspect()
        if observed.get('tick') != token['tick'] or observed.get('state_root') != root:
            self._paused_seek_view = self._paused_seek_render = None
            return None
        cached = getattr(self, '_paused_seek_render', None)
        if cached is None:
            from kptas.telos.frame_preview import recorded_preview
            from subprocess import TimeoutExpired
            from kptas.frames import png_bytes
            try:
                decoded = recorded_preview(bundle, token['tick'], exact=True, epoch=epoch)
            except (BackendError, OSError, ValueError, KeyError, TimeoutExpired):
                return None  # Missing/corrupt output cannot replace the physical native view.
            if decoded is None:
                return None
            pixels, w, h, metadata = decoded
            view = dict(metadata)
            view.update(recorded_state_root=root, restored_owner=dict(token),
                        simulation_state_source='verified_full_root_restore',
                        virtual_us=self._observed_virtual_us(observed, token['tick']))
            cached = {'tick': token['tick'], 'png': png_bytes(pixels, w, h), 'view': view}
            self._paused_seek_render = cached
        from copy import deepcopy
        return deepcopy(cached)

    def _render_cache_key(self):
        backend = getattr(self.sup, 'backend', None)
        if (getattr(backend, 'name', None) != 'shim' or self.broken or self.playing
                or self._operation_depth or self._execution_status()[0] != 'healthy'):
            return None
        return (self.session_id, id(self.sup), id(backend), self.sup.epoch, self.sup.tick,
                self.sup.offset, self.revision, self.owner_token()['take'],
                getattr(backend, '_seq', None),
                json.dumps(getattr(backend, 'last_view_metadata', None), sort_keys=True, default=str))

    @staticmethod
    def _observed_virtual_us(observed, tick):
        if not isinstance(observed, Mapping) or observed.get('tick') != tick:
            return None
        journal = observed.get('journal')
        value = journal.get('v') if isinstance(journal, Mapping) else None
        return value if type(value) is int and 0 <= value < 1 << 64 else None

    @staticmethod
    def _render_cache_ready(observed):
        sem = observed.get('semantics')
        invalid = (0, 0x7f000000, 0xffffffff)
        return (isinstance(sem, dict) and sem.get('cmod') == 1
                and all(type(sem.get(k)) is int and sem[k] == 0 for k in ('anr','lfs','lmod','idis'))
                and type(sem.get('cpid')) is int and sem['cpid'] not in invalid
                and isinstance(sem.get('pc'), dict) and type(sem['pc'].get('area')) is int
                and sem['pc']['area'] not in invalid)

    def render(self) -> dict[str, Any]:
        with self._lock:
            if self.closed or self.sup is None or self.sup.backend is None:
                raise TelosError("no session: send start first")
            from copy import deepcopy
            historical = self._paused_seek_frame()
            if historical is not None:
                return historical
            key = self._render_cache_key()
            cached = getattr(self, '_render_cache', None)
            if key is not None and cached is not None and cached[0] == key:
                return deepcopy(cached[1])
            self._render_cache = None
            ready = False
            observed = None
            if key is not None:
                try:
                    observed = self.inspect()
                    ready = self._render_cache_ready(observed)
                except (TelosError, BackendError, OSError, ValueError, KeyError):
                    pass  # Offline/unready previews remain readable, never cached.
            png = self.sup.backend.frame()
            metadata = getattr(self.sup.backend, "last_view_metadata", None)
            view = dict(metadata) if isinstance(metadata, Mapping) else {}
            view['virtual_us'] = self._observed_virtual_us(observed, self.sup.tick)
            if not isinstance(view.get("source"), str):
                view["source"] = "unknown"
            # Native capture ticks are epoch-local; expose logical ticks like
            # the owner. Missing provenance stays missing on older backends.
            offset = getattr(self.sup, "offset", 0)
            for field in ("gate_tick", "captured_tick"):
                if type(view.get(field)) is int:
                    view[field] += offset
                elif field in view and view[field] is not None:
                    del view[field]
            result = {"tick": self.sup.tick, "png": png, "view": view}
            local_tick = self.sup.tick - offset
            provenance = (isinstance(metadata, Mapping)
                and metadata.get('source') in {'captured_back_buffer', 'restored_framebuffer'}
                and metadata.get('blank') is False and type(metadata.get('repeat')) is bool
                and type(metadata.get('gate_tick')) is int and metadata['gate_tick'] == local_tick
                and type(metadata.get('captured_tick')) is int and 0 <= metadata['captured_tick'] <= local_tick
                and metadata.get('format') == 'bgra-bottom-up'
                and isinstance(metadata.get('sha256'), str) and len(metadata['sha256']) == 64
                and all(c in '0123456789abcdef' for c in metadata['sha256']))
            after = self._render_cache_key()
            # Native frame export advances _seq and metadata, but cannot change
            # owner/captured state. Cache only its verified immutable output.
            if (ready and provenance and isinstance(png, bytes) and png.startswith(b'\x89PNG\r\n\x1a\n')
                    and after is not None and after[:8] == key[:8]):
                self._render_cache = (after, deepcopy(result), self.sup, self.sup.backend)
            return result

    @owned_operation
    def mark(self, name: str) -> dict[str, Any]:
        self._live_ok()
        item = compile_score(f"#!score v1\n@mark {name}\n").items
        if len(item) != 1 or not isinstance(item[0], Mark):
            raise TelosError(f"not a mark name: {name!r}")
        self._pending("mark",[*self.items,item[0]])
        self.sink.mark(name, self.sup.tick)
        self.items.append(item[0])
        self._persist()
        return {"tick": self.sup.tick, "name": name}

    def _seal(self) -> None:
        """Detach only after blocking input; retain failed-close ownership for cleanup."""
        self.broken = True
        take = self.cur
        if take.sealed:
            # A prior close succeeded but authoring publication may have failed.
            self._write_score(take)
            return
        if self._seal_error is not None:
            raise TelosError("the previous take could not be sealed; close this session and "
                             "reopen its saved holorecord for recovery") from self._seal_error
        sink = self.sink
        if sink is None:
            raise TelosError("no recording sink is available to seal")
        self.sup.sink = None
        try:
            sink.close(seal=True, backend_ceiling=self._ceiling())
        except Exception as exc:
            # BundleSink.close closes its bundle even when checkpointing fails.
            # Never retry checkpointing this potentially closed writer or lose
            # the reference needed to release any remaining handles.
            self._seal_error = exc
            raise
        self.sink = None
        take.sealed = True
        if sink.last_durable is not None:take.durable_tick=sink.last_durable["tick"]
        self._write_score(take)

    @owned_operation
    def rewind(self, tick: int, *, strict_restore: bool = False):
        """Seek back to recorded `tick` and branch there.

        Capture cadence: a tick that is not a captured restore point is reached hybrid,
        never called a restore: restore the latest restore point K <= tick, branch at K,
        and re-record K..tick into the branch from the recorded score (proved tick by
        tick against the recording), so the view lands on `tick` with an exact history.
        strict_restore refuses such a tick instead ("tick T was not captured")."""
        if getattr(self, "mode", "archive") == "archive":
            return self._archive_rewind(tick, strict_restore=strict_restore)
        from importlib import import_module
        seekmod = import_module("kptas.holo.seek")
        from kptas.holo.pages import PageMap
        from kptas.telos.cli import fresh_path, holo
        if self.sup is None or self.closed:
            raise TelosError("no session: send start first")
        tick = int(tick)
        clock = time.perf_counter
        phases, started = {}, clock()
        started_ns = time.perf_counter_ns()
        mark = [started]
        def lap(name):
            now = clock()
            phases[name] = round(now - mark[0], 6)
            mark[0] = now
        def timed(rc):
            phases["total"] = round(clock() - started, 6)
            return {**rc, "phases": dict(phases)}   # a copy: Take.seek keeps the plain receipt
        sup, cfg, old = self.sup, self.cfg, self.cur
        if tick < 0 or tick > old.last:
            raise TelosError(f"rewind goes back to a recorded {self._unit()}, 0..{old.last}; not {tick}")
        mode = SeekMode.parse(cfg["seek"]["mode"])
        # Every refusal here comes before the capture step and the seal, so it leaves this take
        # recording and unchanged: placement (not inside a wait), the truncated score, and for a
        # tick that is not a restore point (capture cadence) the anchor K it would replay from.
        pre_at = None
        if self.sink is not None:
            planner = self._planner(self.sink.bundle, score=self.text())
            pre_at = planner.place(max(tick, planner.b.last_tick))
            pre_target = planner.rec(tick)
            if pre_target is None:
                raise TelosError(f"{old.path} has no {self._unit()} {tick}")
            if tick not in pre_at:
                raise TelosError(f"{self._unit()} {tick} is inside a wait; rewind to a {self._unit()} outside it")
            check = [tick]
            if not self._restorable(pre_target, mode):
                why = self._unreachable_reason(pre_target)
                if strict_restore:
                    raise TelosError(f"{why}; strict restore cannot reach it")
                if mode is not SeekMode.REPLAY and not SeekMode.HYBRID.allowed(Tier.parse(cfg["run"]["tier"])):
                    raise TelosError(f"{why}; restore + replay is not allowed in tier {cfg['run']['tier']}")
                pre_anchor = self._pick_anchor(planner, tick, mode, pre_at)
                if pre_anchor is None:
                    raise TelosError(f"{why}, and no earlier {self._unit()} in its epoch is a restorable "
                                     "restore point with a score position")
                check.append(pre_anchor)
            if mode is SeekMode.RESTORE:
                # The restore canary replays the recorded frames after the landing point. Refuse here,
                # before the capture step and the seal, when there is nothing after it to replay (the
                # current head): otherwise the seek fails its canary and the session needs recovery.
                canary_tick = tick if self._restorable(pre_target, mode) else pre_anchor
                nxt = planner.rec(canary_tick + 1)
                from kptas.holo.seek import recorded_events
                if (nxt is None or nxt.epoch != planner.rec(canary_tick).epoch) and not any(
                        name == 'proc.down' for name, _ in recorded_events(planner.b).get(canary_tick + 1, ())):
                    raise TelosError(f"{self._unit()} {canary_tick} is the newest recorded {self._unit()}; "
                                     "rewind to an earlier one. The take is unchanged")
            items_now = list(self.items)
            for t in check:
                c = pre_at[t]
                text = self.text(items_now[:c.i] + ([dataclasses.replace(items_now[c.i], count=c.j)] if c.j else []))
                if compile_score(text).text() != text:
                    raise TelosError("the truncated score does not round-trip")
        # Native restore refuses while the held gate itself is uncaptured. Pump frames always
        # capture, so this is a rare fallback; when it steps, the extra recorded frame is
        # disclosed in this ack and its receipt.
        capture_step = self._capture_held_gate() if mode is not SeekMode.REPLAY else None
        noted = self._capture_notice(capture_step)
        if capture_step is not None:
            pre_at = None    # the take gained a tick and a score item: place again below
        # Sealing this writer ends its journal, not its native process. Retain
        # narrowly scoped continuity evidence before relinquishing ownership.
        live_eof = seekmod._LiveEOFContext.capture(self)
        self.broken = True           # block input before any external transition
        if not self.own_frames:      # the seek's own replay frames stay out of the takes
            sup.frames_to(session_dir(cfg, sup.run_name) / f"seek-{len(self.takes)}-frames")
        # Close keeps the writer's incremental live map; it is reused below only after its
        # root matches the reopened recording at the live tick.
        writer = self.sink.bundle if self.sink is not None else None
        lap("admit")
        self._seal()
        lap("seal")
        # The closed child borrowed these readers. Reopening old.path below
        # creates its own recursive ancestry; retaining prior trees duplicates
        # all older generations and grows open handles quadratically.
        while self.held:
            self.held[-1].close()
            self.held.pop()
        Bundle = holo("Bundle").Bundle
        par = Bundle.open(old.path)
        lap("open_parent")
        keep = False
        try:
            live_eof = live_eof.bind(par) if live_eof is not None else None
            try:
                par.record(tick)
            except KeyError:
                raise TelosError(f"{old.path} has no {self._unit()} {tick}") from None
            # The take's SCOR stops at its first tick; seek and placement read the score authored since.
            par.chain.score = self.text()
            at = pre_at if pre_at is not None else self._planner(par).place(max(tick, par.last_tick))
            lap("placement")
            c = at.get(tick)
            if c is None:
                raise TelosError(f"{self._unit()} {tick} is inside a wait; rewind to a {self._unit()} outside it")
            items = list(compile_score(par.score).items)
            kept = items[:c.i] + ([dataclasses.replace(items[c.i], count=c.j)] if c.j else [])
            branch_text = self.text(kept)
            if compile_score(branch_text).text() != branch_text:
                raise TelosError("the truncated score does not round-trip")
            resume = seekmod.resume_cursor(par, tick, branch_text, cfg)
            lap("resume_cursor")
            # Capture cadence: an uncaptured (or non-restore-point) tick has no map to restore or
            # branch from. Seek the latest restorable K <= tick instead, branch there, re-record K..tick.
            target = par.record(tick)
            anchor, kept_a, branch_text_a, resume_a = tick, kept, branch_text, resume
            if not self._restorable(target, mode):
                anchor = self._pick_anchor(self._planner(par), tick, mode, at)
                if anchor is None:
                    old.rewound_to = None
                    self._write_manifest()
                    return {"rewound": False, "tick": sup.tick, "take": old.path, **noted,
                            "reason": f"{self._unreachable_reason(target)}, and no earlier restorable {self._unit()} in "
                                      "its epoch has a score position"}
                ca = at[anchor]
                kept_a = items[:ca.i] + ([dataclasses.replace(items[ca.i], count=ca.j)] if ca.j else [])
                branch_text_a = self.text(kept_a)
                if compile_score(branch_text_a).text() != branch_text_a:
                    raise TelosError("the truncated score does not round-trip")
                resume_a = seekmod.resume_cursor(par, anchor, branch_text_a, cfg)
                lap("anchor")
            # The sink drained the records, so the seek's view of live memory is stale: give it the map
            # of the tick the game is at, which the take recorded.
            s = self._seek_session()
            try:
                s.live.map = self._recorded_live_map(par, sup.tick, s, writer=writer)
                s.live.tick, s.live.ok, s.live.via = sup.tick, True, "replay"
                s.live.captured = True
            except Exception as e:
                self.log(f"no recorded map at the live {self._unit()} {sup.tick} ({e}); the restore writes every page")
                s.live.map, s.live.tick, s.live.ok, s.live.via = PageMap(), sup.tick, False, "replay"
                s.live.captured = False
            lap("live_map")
            epoch0 = sup.epoch
            self._label_sent = None
            r: Receipt = seekmod.seek(par, anchor, sup, mode=mode, cfg=cfg, log=self.log, _live_eof=live_eof)
            lap("seek")
            rc = receipt_dict(r)
            rc["epoch_before"], rc["epoch_after"] = epoch0, sup.epoch
            rc["recorded_root"] = par.record(anchor).state_root.hex()
            if (r.result != "pass" or r.mode is not mode or r.fallback
                    or (mode is SeekMode.RESTORE and (r.verify_level is not VerifyLevel.FULL_ROOT or r.canary != "pass"))):
                old.rewound_to = None
                self._write_manifest()
                return {"rewound": False, "tick": sup.tick, "receipt": timed(rc), "take": old.path, **noted}
            root = Path(old.path)
            # Readable, non-stacking names: <base>@b<branch number>@<frame>.holo. The part before the
            # first '@' must stay the session base: authoring.py and bundle.py find the session
            # manifest from it. Take.parent and branch_tick in the manifest are the real link.
            import re as _re
            base = _re.sub(r"\.b\d+$", "", root.stem.split("@", 1)[0]) or root.stem
            dest = fresh_path(root.with_name(f"{base}@b{len(self.takes)}@{tick}.holo"))
            child = holo("create_branch").create_branch(par, anchor, dest, score=branch_text_a)
            child.receipt(r)
            sink = BundleSink(child, child.header.tier)
            sink.resume(anchor, restored=r.mode is not SeekMode.REPLAY)
            lap("create_branch")
            sup.sink = self.sink = sink
            self.items = kept_a
            old.rewound_to = tick
            self.takes.append(Take(str(dest), parent=old.path, branch_tick=anchor, first=anchor, last=sup.tick,
                                   seek=rc))
            self.held.append(par)    # the branch resolves its inherited ticks through this handle
            keep = True
            # Own the new take and its parent before another fallible boundary.
            if not self.own_frames:
                sup.frames_to(dest / "frames")
            if r.mode in (SeekMode.RESTORE, SeekMode.SEMANTIC):
                sup.carry = [EngineEvent(e, anchor, x) for e, x in resume_a.carry]
            if anchor != tick:
                # Re-record K..tick into the branch; the session stays blocked until it is proved.
                try:
                    via = self._rerecord_span(par, anchor, tick, at, r, started_ns=started_ns)
                except Exception as exc:
                    # Roll back to the sealed old take: discard the unsealed branch, drop it from
                    # the takes and restore the authored items. The live gate is somewhere in
                    # K..tick, so input stays blocked; another rewind recovers from the old take.
                    self._discard_branch(sink)
                    if self.takes and self.takes[-1].path == str(dest):
                        self.takes.pop()
                    old.rewound_to = None
                    self.items = list(items)
                    if not self.own_frames:
                        sup.frames_to(session_dir(cfg, sup.run_name) / f"seek-{len(self.takes)}-frames")
                    try:
                        self._write_manifest()
                    except Exception:
                        pass
                    raise TelosError(f"rewind to {self._unit()} {tick} failed while replaying from "
                                     f"restore@{anchor}: {exc}; the branch was discarded and the take is "
                                     f"unchanged; rewind again or close") from exc
                lap("rerecord")
                child.receipt(via)
                self.items = kept
                self.cur.last = sup.tick
                hybrid = {**receipt_dict(via), **_hybrid_fields(via), "anchor_receipt": rc,
                          "epoch_before": epoch0, "epoch_after": sup.epoch,
                          "recorded_root": target.state_root.hex() if self._restorable(target, SeekMode.REPLAY)
                                           else None}
                rc = self.cur.seek = hybrid
                s.live.map, s.live.tick, s.live.journal = PageMap(sink.pmap.refs()), sup.tick, None
                s.live.ok, s.live.via, s.live.captured = True, "replay", True
            if noted:
                rc = {**rc, **noted}    # the UI reads the receipt: it shows the extra captured frame
                self.cur.seek = rc
            self._persist()
            lap("persist")
            self._frame(sup.tick, anchor)    # anchor == tick: just this gate; else K+1..tick
            lap("frame")
            if (anchor == tick and r.mode is SeekMode.RESTORE and r.verify_level is VerifyLevel.FULL_ROOT
                    and r.canary == "pass"):
                self._arm_paused_seek_view(par, tick)
            lap("arm_view")
            self.broken = False
            return {"rewound": True, "tick": sup.tick, "epoch": sup.epoch, "receipt": timed(rc), "take": str(dest),
                    **noted}
        finally:
            if not keep:
                par.close()

    def _archive_durable_tick(self):
        checkpoint = self.sink.last_durable if self.sink is not None else None
        return checkpoint.get("tick") if isinstance(checkpoint, dict) else None

    def list_takes(self) -> dict[str, Any]:
        focus = getattr(getattr(self.sup, 'backend', None), 'ui_game_focus', None)
        display = getattr(getattr(focus, 'anchor', None), 'display', None)
        status, health_error = self._execution_status()
        eligible = health_error is None
        focus_caps = ['ui_execution_focus'] if focus is not None and eligible else []
        health_metadata = ({'execution_health': status, 'execution_error': health_error,
                            'execution_health_verified': status in {'healthy', 'dead'}} if status is not None else {})
        recording_caps = ['recording_save', 'recording_load'] if eligible else []
        backend = getattr(self.sup, 'backend', None)
        supports = getattr(backend, 'supports_input_source', None)
        source_protocol = self.mode == 'recording' and callable(supports) and supports()
        source_caps = ['input_source_transition'] if eligible and source_protocol else []
        tool_caps = ['tool_dispatch'] if (eligible and not self.run_loaded
                    and getattr(backend, 'supports_tool_bindings', lambda: False)()) else []
        # run_control: pause, resume, unload and in-place replacement of a loaded score.
        run_caps = ['authored_run_queue', 'run_control'] if (self.mode == 'recording' and eligible
                   and self.cfg.get('clock', {}).get('precision') == 'frame') else []
        queue = getattr(self, '_run_queue', None)
        run_metadata = {'run': queue.current.snapshot() if queue is not None and queue.current else None,
                        'run_history': queue.history_snapshot() if queue is not None else []}
        source_metadata = ({'engaged': getattr(self, 'engaged', True),
                            'input_source': 'authored' if getattr(self, 'engaged', True) else 'physical',
                            'pause_on_engage': getattr(self, 'pause_on_engage', True),
                            'input_scheduler_running': self.input_scheduler_running,
                            'physical_input_error': getattr(self, '_physical_input_error', None),
                            'engagement_qualified': False} if source_protocol else {})
        # The launching owner supplies the game's display; the GUI may render
        # on another X server. Actual focus still revalidates the live binding.
        focus_metadata = {'ui_execution_display': display} if eligible and type(display) is str and display else {}
        if self.mode == "archive":
            text = self.score.text()
            take = dataclasses.asdict(Take(str(self.bundle.root), first=0, last=self.sup.tick,
                score_sha256=hashlib.sha256(text.encode()).hexdigest(),
                durable_tick=self._archive_durable_tick(), sealed=bool(self.bundle.sealed)))
            take["recorded_last"] = self.bundle.last_tick
            return {"tick": self.sup.tick, "session": None, "mode": "archive", "takes": [take],
                    "owner": self.owner_token(), "score_text": text,
                    "clock_precision": self.cfg.get("clock", {}).get("precision"),
                    "precision_effective": self._precision_effective(),
                    "capabilities": ["atomic_expected", "atomic_step_observation", "atomic_view_observation", "archive_playback", "inspect", "render", "recorded_step", "rewind", "branch", "save", *recording_caps, *focus_caps, *tool_caps], **focus_metadata, **health_metadata}
        return {"tick": self.sup.tick if self.sup else None, "session": str(self.manifest) if self.manifest else None,
                "takes": [dataclasses.asdict(t) for t in self.takes],
                "owner": self.owner_token(), "clock_precision": self.cfg.get("clock", {}).get("precision"), "precision_effective": self._precision_effective(), "capabilities": ["atomic_expected", "atomic_step_observation", "atomic_view_observation", *recording_caps, *focus_caps, *source_caps, *tool_caps, *run_caps], "mode": self.mode, **focus_metadata, **health_metadata, **source_metadata, **run_metadata}

    def video(self, out: str | None = None, **kw: Any) -> dict[str, Any]:
        if self.manifest is None:
            raise TelosError("no session: send start first")
        from .live import stitch
        dest = Path(out) if out else self.manifest.with_name(self.manifest.name[:-len(MANIFEST)] + ".mp4")
        return {"out": str(dest), "summary": stitch(self.manifest, dest, **kw)}

    @owned_operation
    def close(self) -> dict[str, Any]:
        if self.mode == "archive":
            if not self.closed:
                errors = []
                try:
                    self.sup.close()
                except Exception as exc:
                    errors.append(exc)
                sink = self.sink
                if sink is not None:
                    self.sup.sink = None
                    try:
                        if self._seal_error is not None:
                            sink.bundle.close()
                            errors.append(self._seal_error)
                        else:
                            sink.close(seal=not errors and getattr(self, "branch_ok", False),
                                       backend_ceiling=self._ceiling())
                    except Exception as exc:
                        errors.append(exc)
                    finally:
                        self.sink = None
                for reader in self.held:
                    try:
                        reader.close()
                    except Exception as exc:
                        errors.append(exc)
                self.held.clear()
                self._label_sent = None
                self.closed = True
                if errors:
                    raise errors[0]
            return self.list_takes()
        self.broken = True
        errors = []
        if not self.closed:
            ok = False
            try:
                if self.sup is not None:
                    self.sup.close()
                ok = True
            except Exception as exc:
                errors.append(exc)
            sink = self.sink
            if sink is not None:
                if self.sup is not None:self.sup.sink = None
                try:
                    if self._seal_error is not None:
                        # Do not checkpoint a writer whose first close failed.
                        sink.bundle.close()
                        errors.append(self._seal_error)
                    else:
                        sink.close(seal=ok, backend_ceiling=self._ceiling())
                        if self.takes:self.cur.sealed = ok
                except Exception as exc:
                    errors.append(exc)
                finally:
                    self.sink = None
                    if self.takes and sink.last_durable is not None:
                        self.cur.durable_tick = sink.last_durable["tick"]
            for h in self.held:
                try:h.close()
                except Exception as exc:errors.append(exc)
            self.held.clear()
            self._label_sent = None
            self.closed = True
        # Retrying close retries publication, never backend execution or a
        # checkpoint against an already closed bundle.
        try:
            if self.takes:
                self.cur.last = max(self.cur.last, self.sup.tick if self.sup else -1)
                self._write_score(self.cur)
            self._write_manifest()
        except Exception as exc:
            errors.append(exc)
        if errors:raise errors[0]
        return self.list_takes()
