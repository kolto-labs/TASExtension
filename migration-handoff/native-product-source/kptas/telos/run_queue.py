"""Bounded authored runs, advanced only by the existing session owner.

The cursor is memory resident. Executed frames use the ordinary pending score,
journal and durable acknowledgement; an owner restart never replays this queue.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
import hashlib
import os
import time
import uuid

from kptas.score import Note, compile_score
from kptas.score.ast import NoteLine
from kptas.score.parser import parse
from kptas.supervisor import TelosError

MAX_SCORE_BYTES = 1_048_576
MAX_NOTES = 100_000
MAX_FRAMES = 1_000_000
MAX_RUNS = 1024
MAX_HISTORY = 256
MAX_NAME = 200
# ACTIVE: the frame pump schedules the run. LOADED: the run holds the owner;
# a paused run is loaded but never stepped, so nothing else may move the game.
ACTIVE = frozenset({'queued', 'running'})
LOADED = ACTIVE | {'paused'}


def run_name(name):
    """A display name for the loaded score; never a path the owner opens."""
    if name is None:
        return None
    if type(name) is not str:
        raise TelosError('score name must be text')
    name = ''.join(ch for ch in name if ch.isprintable()).strip()[:MAX_NAME]
    return name or None


def remainder_text(notes, note_index, frame_in_note):
    """The not-yet-executed Notes as a plain run score, or None when nothing is left.

    A partly executed Note keeps only its remaining frames; its wheel belongs
    to its first frame, so it is kept only when that frame has not run.
    """
    from kptas.score.fmt import render
    if not notes or note_index >= len(notes):
        return None
    head = notes[note_index]
    if frame_in_note:
        # The cursor already sits at mouse_abs after the Note's first frame;
        # repeating it would add a mouse move the original run never sent.
        head = replace(head, count=head.count - frame_in_note, wheel=0, mouse_abs=None)
    else:
        head = replace(head, count=head.count)
    rest = (head, *notes[note_index + 1:])
    return '#!score v1\n' + ''.join(render(note) + '\n' for note in rest)


def compile_run(text):
    """Validate literal text before compilation can resolve any includes."""
    if type(text) is not str or len(text) > MAX_SCORE_BYTES:
        raise TelosError('run score must be literal text of at most 1 MiB')
    if len(text.encode('utf-8')) > MAX_SCORE_BYTES:
        raise TelosError('run score must be at most 1 MiB of UTF-8')
    ast = parse(text)
    if ast.header is None or ast.header:
        raise TelosError('run score requires a plain #!score v1 header; header options and projections are unsupported')
    notes = [line.node for line in ast.lines if line.node is not None]
    if not notes or len(notes) > MAX_NOTES:
        raise TelosError(f'run score requires 1..{MAX_NOTES} Notes')
    total = 0
    for note in notes:
        if not isinstance(note, NoteLine):
            raise TelosError('run score accepts Notes only; directives and includes are unsupported')
        if note.count < 1 or note.osev is not None or note.text is not None or note.aliases:
            raise TelosError('run Notes require positive counts and ordinary controls; OS events, text and aliases are unsupported')
        total += note.count
        if total > MAX_FRAMES:
            raise TelosError(f'run score exceeds {MAX_FRAMES} frames')
    # Validated AST contains no include or expansion nodes; compilation stays
    # entirely in memory and cannot interpret the argument as a file path.
    score = compile_score(text + '\n')
    return tuple(score.items), total, hashlib.sha256(text.encode('utf-8')).hexdigest()


class BatchSizer:
    """Ticks per Play command or run batch: amortize command cost, bound pause latency.

    Every tick is still one recorded owner gate; only the number of gates per
    acknowledged command changes. Pause lands on the next command boundary.
    Doubles while per-tick wall time improves by more than 10% (or the batch
    is far below the responsiveness target) and the predicted batch stays
    under max_wall_s; halves when a batch exceeds it.
    """
    def __init__(self, *, target_s=0.1, max_wall_s=None, max_ticks=240):
        if max_wall_s is None:
            try:
                max_wall_s = float(os.environ.get('KPTAS_PLAY_BATCH_MAX_S', '1.0'))
            except ValueError:
                max_wall_s = 1.0
        self.target_s = target_s
        self.max_wall_s = min(10.0, max(target_s, max_wall_s))
        self.max_ticks = max_ticks
        self.ticks = 1
        self.best_per_tick = None
        self.previous = None  # (ticks, seconds) for a marginal per-tick estimate

    def configure(self, max_ticks=None, max_wall_s=None):
        """Change the limits live. None keeps a limit; the next batch fits the new ones."""
        if max_ticks is not None:
            if type(max_ticks) is not int or max_ticks < 1:
                raise ValueError('max_ticks must be a positive integer')
            self.max_ticks = max_ticks
            self.ticks = min(self.ticks, max_ticks)
        if max_wall_s is not None:
            self.max_wall_s = min(10.0, max(self.target_s, float(max_wall_s)))

    def record(self, ticks, seconds):
        if type(ticks) is not int or ticks < 1 or not seconds >= 0:
            return
        previous, self.previous = self.previous, (ticks, seconds)
        if seconds > self.max_wall_s and ticks > 1:
            self.ticks = max(1, min(self.ticks, ticks) // 2)
            self.best_per_tick = None
            return
        if ticks < self.ticks:
            return  # An input change cut the group short; not this size's sample.
        per_tick = seconds / ticks
        improved = self.best_per_tick is None or per_tick < 0.9 * self.best_per_tick
        if self.best_per_tick is None or per_tick < self.best_per_tick:
            self.best_per_tick = per_tick
        marginal = None
        if previous is not None and previous[0] != ticks:
            marginal = (seconds - previous[1]) / (ticks - previous[0])
        predicted = seconds + ticks * marginal if marginal is not None and marginal > 0 else 2 * seconds
        if (improved or seconds * 2 <= self.target_s) and predicted <= self.max_wall_s:
            self.ticks = min(self.max_ticks, ticks * 2)


@dataclass
class AuthoredRun:
    run_id: str
    session_id: str
    request_id: str | None
    score_sha256: str
    notes: tuple[Note, ...]
    total_frames: int
    start_tick: int
    tick: int
    durable_tick: int | None
    starting_owner: dict
    ui_focus_display: str | None
    status: str = 'queued'
    executed_frames: int = 0
    note_index: int = 0
    frame_in_note: int = 0
    error: dict | None = None
    final_owner: dict | None = None
    timing: dict = field(default_factory=lambda: {
        'attempted_frames': 0, 'acknowledged_frames': 0,
        'total_frame_ns': 0, 'last_frame_ns': 0, 'max_frame_ns': 0,
        'pending_ns': 0, 'persist_ns': 0, 'frame_export_ns': 0, 'batches': 0})
    started_at: float = field(default_factory=time.monotonic)
    finished_at: float | None = None
    name: str | None = None
    end_reason: str | None = None  # stopped, unloaded or replaced when cancelled
    paused_at: float | None = None
    paused_seconds: float = 0.0

    @property
    def active(self):
        return self.status in ACTIVE

    @property
    def loaded(self):
        return self.status in LOADED

    def pause(self):
        """Stop scheduling at the acknowledged boundary; nothing is injected."""
        if self.active:
            self.status = 'paused'
            self.paused_at = time.monotonic()

    def resume(self):
        # Straight to running: queued means no frame has executed yet.
        if self.status == 'paused':
            self.paused_seconds += time.monotonic() - self.paused_at
            self.paused_at = None
            self.status = 'running'

    def remainder(self):
        """(score text, frames) for the inputs that have not run yet."""
        left = self.total_frames - self.executed_frames
        if not left:
            return None, 0
        return remainder_text(self.notes, self.note_index, self.frame_in_note), left

    def frames_left_in_note(self):
        return self.notes[self.note_index].count - self.frame_in_note

    def frame(self, frames=1):
        """The next `frames` ticks of the current Note as one Note.

        Note(count=k) is k identical gates; wheel applies at its first tick,
        so it is kept only when the batch starts the authored Note.
        """
        note = self.notes[self.note_index]
        if not 1 <= frames <= note.count - self.frame_in_note:
            raise TelosError('run batch must stay inside one authored Note')
        return replace(note, count=frames, wheel=note.wheel if self.frame_in_note == 0 else 0)

    def authored_prefix(self, frames=1):
        return replace(self.notes[self.note_index], count=self.frame_in_note + frames)

    def acknowledge(self, tick, durable_tick, frames=1):
        self.tick, self.durable_tick = tick, durable_tick
        self.executed_frames += frames
        self.frame_in_note += frames
        if self.frame_in_note == self.notes[self.note_index].count:
            self.note_index += 1
            self.frame_in_note = 0
        if self.executed_frames == self.total_frames:
            self.finish('completed')

    def finish(self, status, *, error=None, reason=None):
        if self.paused_at is not None:
            self.paused_seconds += time.monotonic() - self.paused_at
            self.paused_at = None
        self.status, self.error = status, error
        self.end_reason = reason if status == 'cancelled' else None
        self.finished_at = time.monotonic()
        self.notes = ()  # Retain bounded results, not every completed score.

    def record_timing(self, elapsed_ns, phases, acknowledged, frames=1):
        # Fixed-size resident counters: no extra publication or disk traffic.
        # A batch of k frames is one owner call; per-frame fields are averages.
        timing = self.timing
        per_frame = elapsed_ns // max(1, frames)
        timing['attempted_frames'] += frames
        timing['acknowledged_frames'] += frames if acknowledged else 0
        timing['total_frame_ns'] += elapsed_ns
        timing['last_frame_ns'] = per_frame
        timing['max_frame_ns'] = max(timing['max_frame_ns'], per_frame)
        timing['batches'] += 1
        for name in ('pending_ns', 'persist_ns', 'frame_export_ns'):
            timing[name] += phases.get(name, 0)

    def snapshot(self):
        # Paused time is not execution time; the rate covers frames that ran.
        end = self.finished_at or self.paused_at or time.monotonic()
        elapsed = end - self.started_at - self.paused_seconds
        return {'schema': 'kptas-run/1', 'run_id': self.run_id,
                'session_id': self.session_id, 'request_id': self.request_id,
                'status': self.status, 'score_sha256': self.score_sha256,
                'start_tick': self.start_tick, 'tick': self.tick,
                'acknowledged_tick': self.start_tick + self.executed_frames,
                'durable_tick': self.durable_tick, 'total_frames': self.total_frames,
                'executed_frames': self.executed_frames, 'error': self.error,
                'starting_owner': dict(self.starting_owner),
                'final_owner': dict(self.final_owner) if self.final_owner else None,
                'elapsed_seconds': max(0.0, elapsed),
                'timing': {**self.timing, 'scope': 'owner_scheduler_frame_call',
                           'phases': 'nested; includes failed attempts; not engine FPS'},
                'recovery': 'executed_prefix_only', 'disconnect_policy': 'continue',
                'input_at_end': 'last_executed_note',
                'name': self.name, 'end_reason': self.end_reason}


class RunQueue:
    """One active run with bounded terminal receipts; caller holds owner lock."""
    def __init__(self):
        self.runs = {}
        self.requests = {}
        self.current = None
        # Where each score started in the current recording: frame, name, kind.
        self.history = []
        # Only the latest ended run keeps its unexecuted inputs, bounded to one score.
        self.last_remainder = None  # (run_id, text, frames)

    @property
    def active(self):
        return self.current is not None and self.current.active

    @property
    def loaded(self):
        return self.current is not None and self.current.loaded

    def end(self, run, status, *, reason=None, error=None):
        """Finish a loaded run, keeping its remainder before the Notes are released."""
        text, frames = run.remainder()
        self.last_remainder = (run.run_id, text, frames) if text else None
        run.finish(status, error=error, reason=reason)

    def remainder_of(self, run_id):
        kept = self.last_remainder
        return (kept[1], kept[2]) if kept is not None and kept[0] == run_id else (None, 0)

    def unload(self):
        """Drop the current receipt from display; it stays findable by run_id."""
        self.current = None

    def prune_history(self, tick):
        """A rewind to `tick` discards every score start at or after it."""
        self.history = [entry for entry in self.history if entry['tick'] < tick]

    def history_snapshot(self):
        return [dict(entry) for entry in self.history[-64:]]

    def find(self, run_id=None, request_id=None):
        if run_id is not None and (type(run_id) is not str or not run_id):
            raise TelosError('run_id must be a nonempty string')
        if request_id is not None and (type(request_id) is not str or not request_id):
            raise TelosError('request_id must be a nonempty string')
        run = self.runs.get(run_id) if run_id is not None else (
            self.requests.get(request_id) if request_id is not None else self.current)
        if run is None or request_id is not None and run.request_id != request_id:
            raise TelosError('no matching run in this owner session')
        return run

    def admit(self, *, notes, total, digest, owner, durable_tick, request_id, display,
              name=None, kind='load'):
        if self.loaded:
            raise TelosError('a score is already loaded; unload it or load the new score in its place')
        if len(self.runs) >= MAX_RUNS:
            raise TelosError('owner run receipt limit reached; preserve this session before starting another owner')
        run = AuthoredRun(uuid.uuid4().hex, owner['session_id'], request_id, digest,
                          notes, total, owner['tick'], owner['tick'], durable_tick,
                          dict(owner), display, name=name)
        self.runs[run.run_id] = run
        if request_id is not None:
            self.requests[request_id] = run
        self.current = run
        self.history.append({'tick': run.start_tick, 'name': name, 'run_id': run.run_id,
                             'kind': kind, 'frames': total})
        del self.history[:-MAX_HISTORY]
        return run
