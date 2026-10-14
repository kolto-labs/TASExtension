"""Client-only, serialized access to an existing live owner. Never starts or closes it."""
from __future__ import annotations

import base64
import dataclasses
import hashlib
import json
import math
import os
import re
import socket
import stat
from pathlib import Path
import threading
import time
import uuid
from typing import Any

from kptas.score import compile_score
from kptas.score.ast import Note, Sync, Relaunch
from kptas.score.ast import Mark, Segment


class WorkspaceError(RuntimeError):
    pass


class ReadOnlyResponsePending(WorkspaceError):
    """A read has no complete acknowledgement; no new authority is admitted."""


class RemoteRefusal(WorkspaceError):
    pass


class UnknownMutation(WorkspaceError):
    pass


# The owner's exact compare-and-swap refusal (session.py operation()). It is
# raised before the operation is admitted, so the command never ran.
# Matched by text (the owner sends code "TelosError" for every refusal), so
# accept either unit word in case the owner wording follows clock precision.
STALE_TOKEN_REFUSAL = re.compile(r'session, take, (?:tick|frame) or revision changed; refresh required')


def _unit(precision, plural=False):
    from kptas.config import unit_name
    return unit_name(precision, plural)


def verified_landing(receipt, tick):
    """A full-root verified restore at `tick`, or restore@K + replay to it (hybrid).

    Capture cadence reaches an uncaptured frame by restoring the latest captured
    restore point K <= tick and replaying. That is never called a restore here.
    """
    if not isinstance(receipt, dict):
        return False
    if (receipt.get('result') != 'pass' or receipt.get('fallback')
            or type(receipt.get('to_tick')) is not int or receipt['to_tick'] != tick):
        return False
    mode, level = receipt.get('mode'), receipt.get('verify_level')
    if mode == 'restore':
        return level == 'full_root' and receipt.get('canary') == 'pass'
    if mode == 'hybrid':
        # T itself may be uncaptured: the owner then reports `journal` (journals
        # matched K..T after a root-exact restore at K), never `full_root`.
        if level not in ('full_root', 'journal'):
            return False
        anchor = receipt.get('anchor_tick')
        nested = receipt.get('anchor_receipt')
        if anchor is not None and (type(anchor) is not int or not 0 <= anchor <= tick):
            return False
        if nested is not None and (not isinstance(nested, dict) or nested.get('result') != 'pass'):
            return False
        if level == 'journal' and nested is None and anchor is None:
            return False      # journal-level landing is only exact behind a named, verified anchor restore
        return True
    return False


def save_summary(saved):
    """User-facing result of an acknowledged save (see save_recording)."""
    if not isinstance(saved, dict):
        return ''
    if saved.get('read_only'):
        return 'Recording is already saved'
    unit, tick, restore = saved['unit'], saved['durable_tick'], saved['restore_tick']
    if restore == tick:
        return f'Saved through {unit} {tick}; restore point at {unit} {tick}'
    return f'Saved through {unit} {tick}; exact restore point {unit} {restore}'


def landing_label(receipt, precision=None):
    """Truthful one-line description of how the owner reached its gate."""
    if not isinstance(receipt, dict):
        return ''
    if receipt.get('mode') == 'hybrid':
        label = receipt.get('label')
        anchor = receipt.get('anchor_tick')
        if isinstance(label, str) and label:
            text = label
        elif type(anchor) is int:
            text = f'rewound via restore@{anchor} + replay'
        else:
            text = 'rewound via restore + replay'
        if type(anchor) is int and type(receipt.get('to_tick')) is int:
            text += f' ({_unit(precision)} {anchor} to {receipt["to_tick"]})'
        return text[:1].upper() + text[1:]
    if receipt.get('mode') == 'restore':
        return f'Restored at {_unit(precision)} {receipt.get("to_tick")}'
    return ''


class LiveTransport:
    """One finite NDJSON connection per request; ambiguous writes are never retried."""
    def __init__(self, host='127.0.0.1', port=47610, timeout=5.0, max_bytes=32*1024*1024, *, operation_timeout=300.0):
        if not 0 < timeout <= 120:
            raise ValueError('timeout must be between 0 and 120 seconds')
        if not 0 < operation_timeout <= 1800:
            raise ValueError('operation_timeout must be between 0 and 1800 seconds')
        self.operation_timeout = operation_timeout
        self.host, self.port, self.timeout, self.max_bytes = host, port, timeout, max_bytes
        self.unknown_state = False
        self._uncertain_owner = None
        self._uncertain_run = None
        self.lock = threading.RLock()

    def request(self, op: str, **args) -> dict:
        # run_pause/run_resume/run_unload are keyed by run identity and idempotent:
        # a lost ACK is settled by repeating them or reading run_status, so they
        # never latch unknown_state. Resume is refused by the controller instead.
        if op not in {'takes', 'inspect', 'render', 'view', 'step', 'rewind', 'mark', 'branch', 'save_recording', 'load_recording', 'reconcile', 'set_engaged', 'configure_tool_bindings', 'poll_tool_controls', 'run_start', 'run_status', 'run_cancel', 'run_pause', 'run_resume', 'run_unload'}:
            raise WorkspaceError('client cannot start, close or replace the live owner')
        mutation = op in {'step', 'rewind', 'mark', 'branch', 'save_recording', 'load_recording', 'set_engaged', 'configure_tool_bindings', 'run_start', 'run_cancel'}
        cancel = (args.get('session_id'), args.get('run_id')) if op == 'run_cancel' else None
        if cancel is not None and any(type(value) is not str or not value for value in cancel):
            raise WorkspaceError('Stop requires an acknowledged run and session identity')
        with self.lock:
            if mutation and self.unknown_state and not (
                    cancel is not None and cancel == self._uncertain_run and self._uncertain_owner is None):
                raise UnknownMutation('previous command outcome unknown; reconcile with the owner before more input')
            rid = uuid.uuid4().hex
            payload = json.dumps({'id': rid, 'op': op, **args}, allow_nan=False).encode()+b'\n'
            prior = (self.unknown_state, self._uncertain_owner, self._uncertain_run)
            if mutation:
                token = args.get('expected')
                self._uncertain_owner = dict(token) if isinstance(token, dict) else None
                self._uncertain_run = cancel
            sent = False
            response_timeout = self.operation_timeout if op in {"load_recording", "rewind", "branch", "save_recording", "step", "set_engaged"} else self.timeout
            deadline = time.monotonic()+response_timeout
            try:
                with socket.create_connection((self.host, self.port), self.timeout) as sock:
                    # Linux can connect a loopback socket to itself when its
                    # ephemeral source port equals a not-yet-listening owner.
                    # Refuse before any request bytes can make a mutation
                    # ambiguous; the context manager closes the false peer.
                    if sock.getsockname()==sock.getpeername():
                        raise ConnectionError('live connection resolved to its own local socket')
                    sent = True  # Partial send is already an ambiguous mutation.
                    sock.settimeout(min(self.timeout, response_timeout))
                    sock.sendall(payload)
                    buf = bytearray()
                    while b'\n' not in buf:
                        remaining = deadline-time.monotonic()
                        if remaining <= 0:
                            if not mutation and op != 'reconcile':
                                raise ReadOnlyResponsePending('awaiting canonical owner response')
                            raise TimeoutError('live response deadline exceeded')
                        sock.settimeout(remaining)
                        try:
                            part = sock.recv(min(65536, self.max_bytes+1-len(buf)))
                        except socket.timeout:
                            # A read may wait behind an independent long command
                            # or receive only part of a large coherent frame.
                            # Neither case proves the owner failed. Discard the
                            # incomplete response and retain the last ACK; a
                            # later refresh obtains a new atomic observation.
                            if not mutation and op != 'reconcile':
                                raise ReadOnlyResponsePending('awaiting canonical owner response') from None
                            raise
                        if not part:
                            raise WorkspaceError('live owner closed before acknowledgement')
                        buf.extend(part)
                        if len(buf) > self.max_bytes:
                            raise WorkspaceError('live response exceeds size limit')
                    response = json.loads(bytes(buf).split(b'\n', 1)[0])
                    if not isinstance(response, dict) or response.get('id') != rid or type(response.get('ok')) is not bool:
                        raise WorkspaceError('invalid live acknowledgement or request ID')
                    if not response['ok']:
                        detail = response.get('error', 'live command refused')
                        if isinstance(detail, dict):
                            # Owners send {"code", "message"}; show the message.
                            detail = detail.get('message') or detail.get('code') or 'live command refused'
                        raise RemoteRefusal(str(detail))
                    result = response.get('result')
                    if not isinstance(result, dict):
                        raise WorkspaceError('live result must be an object')
                    return result
            except ReadOnlyResponsePending:
                raise
            except RemoteRefusal as exc:
                if mutation and STALE_TOKEN_REFUSAL.fullmatch(str(exc)) and 'expected' in args:
                    # The owner's compare-and-swap refused before admission
                    # (session.py operation()): nothing executed, nothing is
                    # uncertain. Keep the previous latch exactly as it was.
                    self.unknown_state, self._uncertain_owner, self._uncertain_run = prior
                elif mutation:
                    self.unknown_state = True
                raise
            except Exception as exc:
                if mutation and sent:
                    self.unknown_state = True
                    raise UnknownMutation(f'{op} outcome unknown: {exc}') from exc
                raise WorkspaceError(str(exc)) from exc

    def can_reconcile(self):
        with self.lock:
            previous = self._uncertain_owner
            return (self.unknown_state and isinstance(previous, dict)
                    and set(previous) == {'session_id', 'take', 'tick', 'revision'}
                    and isinstance(previous['session_id'], str) and bool(previous['session_id'])
                    and isinstance(previous['take'], str) and bool(previous['take'])
                    and type(previous['tick']) is int and previous['tick'] >= 0
                    and type(previous['revision']) is int and previous['revision'] >= 0)

    def reconciliation_barrier(self):
        """Validate a same-owner barrier, retaining the latch for snapshot checks."""
        with self.lock:
            if not self.can_reconcile():
                raise UnknownMutation('reconciliation requires the uncertain command atomic owner token')
            previous = self._uncertain_owner
            result = self.request('reconcile', previous=dict(previous))
            token = result.get('owner')
            if (result.get('schema') != 'kptas-owner-reconciliation/1'
                    or result.get('healthy') is not True or result.get('barrier') is not True
                    or not isinstance(token, dict) or set(token) != set(previous)
                    or token.get('session_id') != previous['session_id']
                    or not isinstance(token.get('take'), str) or type(token.get('tick')) is not int
                    or type(token.get('revision')) is not int or token['revision'] <= previous['revision']):
                raise UnknownMutation('owner did not acknowledge a healthy same-session barrier')
            return dict(token)


@dataclasses.dataclass(frozen=True)
class AuthoredRow:
    start: int | None
    end: int | None
    item: Any


@dataclasses.dataclass(frozen=True)
class RunProgress:
    """Display evidence copied from one canonical owner response."""
    run_id: str
    status: str
    start_tick: int
    acknowledged_tick: int
    durable_tick: int
    executed_frames: int
    total_frames: int
    elapsed_seconds: float
    error: str | None = None
    name: str | None = None
    end_reason: str | None = None

    @property
    def active(self):
        """Loaded: the score holds the owner, including while paused."""
        return self.status in {'queued', 'running', 'paused'}

    @property
    def paused(self):
        return self.status == 'paused'

    @property
    def playing(self):
        return self.status in {'queued', 'running'}

    @classmethod
    def from_owner(cls, value, token, take, tick):
        if value is None:
            return None
        if not isinstance(value, dict) or value.get('schema') != 'kptas-run/1':
            raise WorkspaceError('Execution progress is unavailable: invalid owner receipt')
        status = value.get('status')
        starting = value.get('starting_owner')
        if (type(status) is not str or status not in {'queued', 'running', 'paused', 'completed', 'cancelled', 'failed'}
                or not isinstance(starting, dict) or not isinstance(token, dict)
                or type(starting.get('revision')) is not int or starting['revision'] < 0):
            raise WorkspaceError('Execution progress is unavailable: invalid owner identity')
        if (value.get('session_id') != token.get('session_id')
                or starting.get('session_id') != token.get('session_id')
                or starting.get('take') != take.get('path')):
            if status not in {'queued', 'running', 'paused'}:
                return None  # A retained terminal run belongs to an older take.
            raise WorkspaceError('Execution progress does not match the current owner')
        fields = ('start_tick', 'tick', 'acknowledged_tick', 'durable_tick',
                  'executed_frames', 'total_frames')
        if (any(type(value.get(key)) is not int or value[key] < 0 for key in fields)
                or type(value.get('run_id')) is not str or not value['run_id']
                or not 0 <= value['executed_frames'] <= value['total_frames'] <= 1_000_000
                or value['total_frames'] == 0
                or value['acknowledged_tick'] != value['start_tick'] + value['executed_frames']
                or value['tick'] < value['acknowledged_tick']
                or not value['start_tick'] <= value['durable_tick'] <= value['tick']
                or type(value.get('elapsed_seconds')) not in (int, float)
                or not math.isfinite(value['elapsed_seconds']) or value['elapsed_seconds'] < 0):
            raise WorkspaceError('Execution progress is unavailable: invalid owner counters')
        if (status == 'completed' and value['executed_frames'] != value['total_frames']
                or status == 'queued' and value['executed_frames'] != 0
                or status != 'failed' and (value['tick'] != value['acknowledged_tick']
                                           or value['durable_tick'] != value['acknowledged_tick'])
                or type(starting.get('tick')) is not int or starting['tick'] != value['start_tick']):
            raise WorkspaceError('Execution progress is unavailable: inconsistent run state')
        if status in {'queued', 'running', 'paused'} and (
                value['tick'] != tick or value['durable_tick'] != take.get('durable_tick')
                or value['durable_tick'] != value['acknowledged_tick']):
            raise WorkspaceError('Execution progress is unavailable: owner boundary changed')
        failure = value.get('error')
        message = failure.get('message') if isinstance(failure, dict) else None
        name = value.get('name')
        reason = value.get('end_reason')
        return cls(value['run_id'], status, value['start_tick'], value['acknowledged_tick'],
                   value['durable_tick'], value['executed_frames'], value['total_frames'],
                   float(value['elapsed_seconds']), message if type(message) is str else None,
                   name[:200] if type(name) is str and name else None,
                   reason if reason in {'stopped', 'unloaded', 'replaced'} else None)


@dataclasses.dataclass(frozen=True)
class WorkspaceSnapshot:
    tick: int
    take_path: str
    first: int
    last: int
    durable_tick: int | None
    score_sha256: str | None
    capabilities: frozenset[str]
    rows: tuple[AuthoredRow, ...] = ()
    last_inputs: Note | None = None
    score_text: str = ''
    rewound: bool | None = None
    receipt: dict | None = None
    unknown_state: bool = False
    error: str | None = None
    blocked_from: int | None = None
    owner_token: dict | None = None
    mode: str = 'live'
    recorded_last: int | None = None
    frame_metadata: dict | None = None
    ui_execution_display: str | None = None
    clock_precision: str | None = None
    engaged: bool | None = None
    input_source: str | None = None
    pause_on_engage: bool | None = None
    physical_input_error: str | None = None
    engagement_qualified: bool = False
    run: RunProgress | None = None
    # (tick, name or None, kind) for each score start kept in this recording.
    run_history: tuple = ()
    # Live only: frames an earlier attempt on this lineage reached
    # (timeline_future.RecordedFuture). Display and replay data, never a
    # restore authority.
    future: Any = dataclasses.field(default=None, compare=False, repr=False)
    # Takes the owner session holds (each live rewind starts one).
    take_count: int | None = None
    # Live only: restores followed by a changed input (libTAS rule), and the
    # session's takes for the branch tree (timeline_future.TakeInfo).
    rerecords: int | None = None
    lineage: Any = dataclasses.field(default=None, compare=False, repr=False)


class WorkspaceController:
    """Synchronous API to run in a GUI's single background command queue."""
    def __init__(self, host='127.0.0.1', port=47610, timeout=5.0, *, transport=None, score_loader=None, operation_timeout=300.0):
        self.transport = transport or LiveTransport(host, port, timeout, operation_timeout=operation_timeout)
        self._lock = threading.RLock()
        self._cache = {}
        self._inspect = None
        self._transition = {}
        self._atomic_admitted = None
        self.last_step_observation = None
        self._score_loader = score_loader
        self._cancel_target = None
        # Display text only; never an authority for later commands.
        self.last_landing = ''   # how the last rewind/load reached its gate
        self.last_save = None    # dict(durable_tick, restore_tick, unit) of the last acknowledged save

    @staticmethod
    def _validate_score_authority(take, *, require_immutable=True):
        digest = take.get('score_sha256')
        filename = take.get('score_file')
        if require_immutable or digest is not None:
            if not isinstance(digest, str) or re.fullmatch('[0-9a-f]{64}', digest) is None:
                raise WorkspaceError('owner does not advertise an immutable authored score digest')
            if filename != 'authoring-' + digest + '.score':
                raise WorkspaceError('owner does not advertise the matching immutable score filename')
        if type(take.get('durable_tick')) is not int or take.get('durable_tick') != take.get('last'):
            raise WorkspaceError('owner authored score lacks an acknowledged durable boundary')

    @staticmethod
    def _load_score(take):
        """Read owner-authorized display text, not independent restore admission.

        The owner supplies an immutable digest and durable boundary. Restore
        admission stays in the owner and Bundle.score; this reader never scans
        a journal, page store or ancestry tree.
        """
        digest = take.get('score_sha256')
        filename = take.get('score_file')
        WorkspaceController._validate_score_authority(take)
        # Open both directory and file without following symlinks. Check the
        # actual descriptor and bound reads; a FIFO cannot block this worker.
        flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
        directory = os.open(take['path'], flags | getattr(os, 'O_DIRECTORY', 0))
        try:
            fd = os.open(filename, flags, dir_fd=directory)
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise WorkspaceError('owner authored score must be a regular file')
                with os.fdopen(fd, 'rb') as stream:
                    fd = None
                    data = stream.read(4 * 1024 * 1024 + 1)
            finally:
                if fd is not None:
                    os.close(fd)
        finally:
            os.close(directory)
        if len(data) > 4 * 1024 * 1024:
            raise WorkspaceError('owner authored score exceeds size bound')
        if hashlib.sha256(data).hexdigest() != digest:
            raise WorkspaceError('owner authored score digest mismatch')
        return data.decode('utf-8')

    @staticmethod
    def _owner_identity(state):
        take = state['takes'][-1]
        parent = next((t for t in state['takes'][:-1] if t.get('path') == take.get('parent')), None)
        # Include all metadata used to admit inherited display durability, not
        # only the immutable bytes' cache key. Byte caching never caches trust.
        return json.dumps({'tick': state.get('tick'), 'session': state.get('session'),
                           'take': take, 'parent': parent, 'owner': state.get('owner'),
                           'mode': state.get('mode'), 'capabilities': state.get('capabilities'),
                           'ui_execution_display': state.get('ui_execution_display'),
                           'execution_health': state.get('execution_health'),
                           'execution_error': state.get('execution_error'),
                           'execution_health_verified': state.get('execution_health_verified'),
                           'score_text': state.get('score_text')}, sort_keys=True)

    @staticmethod
    def _owner_token(state, take):
        if 'atomic_expected' not in state.get('capabilities', []):
            return None
        token = state.get('owner')
        if (not isinstance(token, dict) or set(token) != {'session_id', 'take', 'tick', 'revision'}
                or not isinstance(token['session_id'], str) or not token['session_id']
                or type(token['tick']) is not int or type(token['revision']) is not int
                or token['revision'] < 0 or token['take'] != take['path']
                or token['tick'] != state['tick']):
            raise WorkspaceError('owner advertised atomic expected checks without a valid token')
        return dict(token)

    def snapshot(self):
        with self._lock:
            return self._snapshot_from_state(self.transport.request('takes'))

    def _snapshot_from_state(self, state, *, atomic=False):
        if not isinstance(state, dict):
            raise WorkspaceError('owner state must be an object')
        if state.get('execution_health') not in (None, 'healthy', 'unverified'):
            raise WorkspaceError(state.get('execution_error') or 'owned game connection is unavailable')
        takes = state.get('takes')
        if not isinstance(takes, list) or not takes or type(state.get('tick')) is not int:
            raise WorkspaceError('owner has no acknowledged live take')
        take = takes[-1]
        tick = state['tick']
        mode = state.get('mode', 'live')
        if mode == 'recording':
            mode = 'live'
        if mode not in {'live', 'archive'}:
            raise WorkspaceError('unsupported owner workspace mode')
        token = self._owner_token(state, take)
        if mode == 'archive' and token is None:
            raise WorkspaceError('archive playback requires atomic owner capability')
        if take.get('last') != tick:
            raise WorkspaceError(f"owner take does not match its acknowledged {_unit(state.get('clock_precision'))}")
        display_take = take
        if take.get('durable_tick') is None and take.get('branch_tick') == tick:
            parent = next((t for t in takes[:-1] if t.get('path') == take.get('parent')), None)
            receipt = take.get('seek') or {}
            if (parent and parent.get('sealed') is True and type(parent.get('durable_tick')) is int
                    and parent['durable_tick'] >= tick and verified_landing(receipt, tick)):
                display_take = {**take, 'durable_tick': tick}
        if self._inspect is None and not atomic:
            try:
                self.transport.request('inspect')
                self._inspect = True
            except RemoteRefusal as exc:
                if 'unknown op' not in str(exc):
                    raise
                self._inspect = False
        error = None
        try:
            if mode == 'archive':
                inline = state.get('score_text')
                digest = take.get('score_sha256')
                if (not isinstance(inline, str) or len(inline.encode('utf-8')) > 4 * 1024 * 1024
                        or not isinstance(digest, str)
                        or hashlib.sha256(inline.encode('utf-8')).hexdigest() != digest):
                    raise WorkspaceError('archive owner score text/hash is invalid')
            else:
                self._validate_score_authority(display_take, require_immutable=self._score_loader is None)
            # Cache only authored content, never current owner authority.
            # Immutable bytes survive tick/durability/revision advancement;
            # an owner switch or mode/take/digest change gets a cold entry.
            digest = take.get('score_sha256')
            key = (mode, state.get('session'), token['session_id'] if token else None,
                   take['path'], digest)
            if digest is None:
                # A legacy custom loader advertises no immutable identity.
                key += (take.get('durable_tick'), tick)
            cacheable = True
            if key not in self._cache:
                text = inline if mode == 'archive' else (
                    self._score_loader(take['path']) if self._score_loader else self._load_score(display_take))
                digest = take.get('score_sha256')
                if digest is not None and hashlib.sha256(text.encode('utf-8')).hexdigest() != digest:
                    raise WorkspaceError('authored score changed after owner snapshot; refresh required')
                rows, at = [], 0
                compiled = compile_score(text)
                # A root-text digest does not identify mutable @include
                # dependencies. Canonical owner scores have none.
                cacheable = not compiled.files
                for item in compiled.items:
                    end = at+item.count if isinstance(item, Note) and at is not None else at
                    start = at+1 if isinstance(item, Note) and item.count and at is not None else at
                    rows.append(AuthoredRow(start, end, item))
                    at = None if isinstance(item, (Sync, Relaunch)) else end
                authored = (text, tuple(rows))
            else:
                authored = self._cache[key]
            if not atomic:
                coherent = self.transport.request('takes')
                if self._owner_identity(coherent) != self._owner_identity(state):
                    raise WorkspaceError('owner authoring changed while reading; refresh required')
            self._cache = {key: authored} if cacheable else {}
            text, rows = authored
        except ReadOnlyResponsePending:
            raise
        except Exception as exc:
            text, rows, error = '', (), f'Authored score unavailable: {exc}'
        active = next((r for r in reversed(rows) if isinstance(r.item, Note) and r.item.count
                       and r.start is not None and r.end is not None and r.start <= tick <= r.end), None)
        last = (dataclasses.replace(active.item, count=1,
                    wheel=active.item.wheel if tick == active.start else 0,
                    osev=active.item.osev if tick == active.start else None) if active else None)
        caps = {'render', 'step', 'rewind_backward', 'mark'}
        caps.add('atomic_expected' if token is not None else 'legacy_serialized')
        if mode == 'archive':
            caps = {'render', 'recorded_step', 'rewind_backward', 'atomic_expected', 'archive_playback'}
        recorded_last = take.get('recorded_last')
        if type(recorded_last) is not int or recorded_last < tick:
            recorded_last = None
        if mode == 'archive' and recorded_last is not None:
            caps.add('seek_recorded')
        if self._inspect:
            caps.add('inspect')
        caps.update(set(state.get('capabilities', [])) & {'recording_save', 'recording_load', 'ui_execution_focus', 'atomic_step_observation', 'atomic_view_observation', 'input_source_transition', 'tool_dispatch', 'authored_run_queue', 'run_control'})
        engaged = state.get('engaged')
        input_source = state.get('input_source')
        pause_on_engage = state.get('pause_on_engage')
        if 'input_source_transition' in caps:
            if (type(engaged) is not bool or type(pause_on_engage) is not bool or
                    input_source != ('authored' if engaged else 'physical') or
                    state.get('engagement_qualified') is not False):
                raise WorkspaceError('input source metadata is incoherent')
            if not engaged:
                caps.difference_update({'step', 'rewind_backward', 'mark', 'branch', 'recording_load'})
        display = state.get('ui_execution_display')
        if (token is None or 'ui_execution_focus' not in caps or type(display) is not str
                or re.fullmatch(r':\d+(?:\.\d+)?', display) is None):
            display = None
        if mode == 'archive' and token is not None and {'branch', 'recording_save'} <= set(state.get('capabilities', [])):
            caps.add('branch')
        blocked = next((r.start + 1 for r in rows if r.start is not None and
            ((isinstance(r.item, Note) and r.item.count == 0 and (r.item.wheel or r.item.osev))
             or not isinstance(r.item, (Note, Mark, Segment)))), None)
        snapshot = WorkspaceSnapshot(tick, take['path'], take['first'], take['last'], display_take.get('durable_tick'),
            take.get('score_sha256'), frozenset(caps), rows, last, text,
            self._transition.get('rewound'), self._transition.get('receipt'), self.transport.unknown_state, error,
            blocked, token, mode, recorded_last, ui_execution_display=display,
            clock_precision=state.get('clock_precision') if type(state.get('clock_precision')) is str and state['clock_precision'] in {'frame', 'tick'} else None,
            engaged=engaged if type(engaged) is bool else None,
            input_source=input_source if input_source in {'authored', 'physical'} else None,
            pause_on_engage=pause_on_engage if type(pause_on_engage) is bool else None,
            physical_input_error=state.get('physical_input_error'), engagement_qualified=False,
            # display_take: a restore-only rewind landing is durable in its parent.
            run=RunProgress.from_owner(state.get('run'), token, display_take, tick),
            run_history=self._run_history(state.get('run_history'), tick),
            take_count=len(takes) if mode == 'live' else None,
            **self._lineage_fields(mode, error, takes, take, rows, tick))

        self._atomic_admitted = snapshot if token is not None and {'atomic_step_observation', 'atomic_view_observation'} & caps else None
        return snapshot

    def _lineage_fields(self, mode, error, takes, take, rows, tick):
        """Recorded future, rerecord count and branch tree after live rewinds.

        Optional display/replay data: any failure leaves the snapshot unchanged.
        """
        if mode != 'live' or error:
            return {}
        try:
            from .timeline_future import lineage
            found = lineage(self, takes, take, rows, tick)
        except Exception:
            return {}
        if found is None:
            return {}
        return {'future': found.future, 'rerecords': found.rerecords, 'lineage': found.takes}

    def _preflight(self, expected):
        current = self.snapshot()
        if current.error:
            raise WorkspaceError(current.error)
        if expected is not None and (current.tick, current.take_path, current.score_sha256) != (
                expected.tick, expected.take_path, expected.score_sha256):
            raise WorkspaceError(f'acknowledged {_unit(current.clock_precision)}, take or authoring revision changed; refresh required')
        if expected is not None and current.owner_token != expected.owner_token:
            raise WorkspaceError('owner session or revision changed; refresh required')
        if expected is not None and current.ui_execution_display != expected.ui_execution_display:
            raise WorkspaceError('owner execution display changed; refresh required')
        return current

    def reconcile(self):
        """Explicit recovery of acknowledgement trust; never retry game commands."""
        with self._lock:
            if not self.transport.unknown_state:
                return self.snapshot()
            if not self.can_reconcile():
                raise UnknownMutation('reconciliation requires the uncertain command atomic owner token')
            with self.transport.lock:
                token = self.transport.reconciliation_barrier()
                current = self.snapshot()
                if current.error or current.owner_token != token:
                    raise UnknownMutation('owner changed after reconciliation barrier; remain stopped')
                self.transport.unknown_state = False
                self.transport._uncertain_owner = None
                self._transition = {}
                return dataclasses.replace(current, unknown_state=False, rewound=None, receipt=None)

    def can_reconcile(self):
        """Read-only eligibility; the owner must still prove health on invocation."""
        check = getattr(self.transport, 'can_reconcile', None)
        return bool(check()) if callable(check) else False

    def cancel_run(self, *, expected):
        """Stop one stable run identity, even if its frame cursor has advanced."""
        expected = self.cancel_target(expected)
        run = getattr(expected, 'run', None)
        owner = getattr(expected, 'owner_token', None)
        if run is None or not isinstance(owner, dict) or not owner.get('session_id'):
            raise WorkspaceError('Stop requires an acknowledged authored run')
        identity = (owner['session_id'], run.run_id)
        with self._lock, self.transport.lock:
            if self.transport.unknown_state and (
                    getattr(self.transport, '_uncertain_run', None) != identity
                    or getattr(self.transport, '_uncertain_owner', None) is not None):
                raise UnknownMutation('Stop cannot resolve another command whose outcome is unknown')
            self._cancel_target = expected
            try:
                try:
                    result = self.transport.request('run_cancel', session_id=identity[0], run_id=identity[1])
                except WorkspaceError:
                    # A lost Stop ACK is resolved only by this run's status.
                    # Never resend automatically or cancel whichever run is latest.
                    if getattr(self.transport, '_uncertain_run', None) != identity:
                        raise
                    result = self.transport.request('run_status', run_id=identity[1])
                stopped = RunProgress.from_owner(result, owner,
                    {'path': expected.take_path, 'durable_tick': result.get('durable_tick')}, result.get('tick'))
                final = result.get('final_owner')
                if (stopped is None or stopped.active or stopped.run_id != run.run_id
                        or stopped.start_tick != run.start_tick or stopped.total_frames != run.total_frames
                        or stopped.executed_frames < run.executed_frames
                        or not isinstance(final, dict) or set(final) != {'session_id', 'take', 'tick', 'revision'}
                        or final['session_id'] != identity[0] or final['take'] != expected.take_path
                        or type(final['tick']) is not int or final['tick'] != result['tick']
                        or type(final['revision']) is not int
                        or final['revision'] < result['starting_owner']['revision']):
                    raise UnknownMutation('Stop is not confirmed; refresh or choose Stop run to retry this run')
                # This terminal receipt can clear only its own cancellation
                # uncertainty. A subsequent display read may fail or observe
                # another client's new run without undoing this acknowledgement.
                if getattr(self.transport, '_uncertain_run', None) == identity:
                    self.transport.unknown_state = False
                    self.transport._uncertain_run = None
                    self._cancel_target = None
                return self.snapshot()
            except Exception:
                if getattr(self.transport, '_uncertain_run', None) == identity:
                    self.transport.unknown_state = True
                raise

    def cancel_target(self, expected):
        """Retain an uncertain Stop target even if another client starts a run."""
        pending = self._cancel_target
        if pending is not None and self.transport.unknown_state:
            identity = (pending.owner_token['session_id'], pending.run.run_id)
            if getattr(self.transport, '_uncertain_run', None) == identity:
                return pending
        return expected

    def can_cancel_run(self, expected):
        """UI eligibility only; no IO, lock wait or acknowledgement inference."""
        expected = self.cancel_target(expected)
        run = getattr(expected, 'run', None)
        owner = getattr(expected, 'owner_token', None)
        if run is None or not isinstance(owner, dict):
            return False
        identity = (owner.get('session_id'), run.run_id)
        own_uncertainty = (getattr(self.transport, '_uncertain_run', None) == identity
                           and getattr(self.transport, '_uncertain_owner', None) is None)
        return ((run.active or own_uncertainty)
                and (not self.transport.unknown_state or own_uncertainty))

    @staticmethod
    def _run_history(value, tick):
        """Score starts the owner kept for this recording, up to the current frame."""
        if not isinstance(value, list):
            return ()
        rows = []
        for entry in value[-64:]:
            if not isinstance(entry, dict):
                continue
            start, name, kind = entry.get('tick'), entry.get('name'), entry.get('kind')
            if type(start) is not int or not 0 <= start <= tick or kind not in ('load', 'splice'):
                continue
            rows.append((start, name[:200] if type(name) is str and name else None, kind))
        return tuple(rows)

    def run_command(self, op, run_id, *, expected):
        """Pause, resume or unload one score by identity; returns the owner receipt.

        These never move the game and repeat safely, so a refusal or lost ACK
        leaves no recovery latch. The caller reads a fresh snapshot afterwards.
        """
        if op not in ('run_pause', 'run_resume', 'run_unload'):
            raise WorkspaceError('unknown score command')
        owner = getattr(expected, 'owner_token', None)
        if not isinstance(owner, dict) or not owner.get('session_id') or type(run_id) is not str or not run_id:
            raise WorkspaceError('No score is loaded')
        if op == 'run_resume' and self.transport.unknown_state:
            raise WorkspaceError('Reconnect before you resume the score')
        with self._lock:
            result = self.transport.request(op, session_id=owner['session_id'], run_id=run_id)
        remainder = result.get('remainder') if isinstance(result, dict) else None
        if (not isinstance(result, dict) or result.get('schema') != 'kptas-run/1'
                or result.get('run_id') != run_id or type(result.get('status')) is not str
                or (remainder is not None and type(remainder) is not str)):
            raise WorkspaceError('The owner did not confirm the score command. Refresh to check.')
        return result

    def run_remainder(self, run_id, *, expected=None):
        """Read-only: an ended run's receipt, with `remainder` if the owner kept it."""
        if type(run_id) is not str or not run_id:
            raise WorkspaceError('No score is selected')
        with self._lock:
            result = self.transport.request('run_status', run_id=run_id)
        remainder = result.get('remainder') if isinstance(result, dict) else None
        if (not isinstance(result, dict) or result.get('schema') != 'kptas-run/1'
                or result.get('run_id') != run_id or (remainder is not None and type(remainder) is not str)):
            raise WorkspaceError('The owner did not return the score state. Refresh to check.')
        return result

    def _run_admitted(self, request_id):
        """True-or-receipt / False / None (unknown) for one run_start request."""
        try:
            result = self.transport.request('run_status', request_id=request_id)
        except RemoteRefusal as exc:
            return False if 'no matching run' in str(exc) else None
        except WorkspaceError:
            return None
        return result if isinstance(result, dict) and result.get('request_id') == request_id else None

    def start_run(self, text, *, expected, name=None, replace=None, ui_focus_display=None):
        """Admit a score at the expected gate; a clean refusal never latches recovery.

        run_start is a CAS mutation. If it is refused or its ACK is lost, the
        request_id settles it: an owner that never admitted it restores the
        previous latch, an admitted one counts as success, and only an
        unreadable outcome stays uncertain (Reconnect resolves it).
        """
        token = getattr(expected, 'owner_token', None)
        if not isinstance(token, dict):
            raise WorkspaceError('Load a score after the owner confirms its state')
        if self.transport.unknown_state:
            raise WorkspaceError('Reconnect before you load a score')
        request_id = uuid.uuid4().hex
        args = {'score': text, 'expected': dict(token), 'request_id': request_id}
        if name:
            args['name'] = name
        if replace:
            args['replace'] = replace
        if ui_focus_display:
            args['ui_focus_display'] = ui_focus_display
        transport = self.transport
        with self._lock, transport.lock:
            prior = (transport.unknown_state, transport._uncertain_owner, transport._uncertain_run)
            try:
                result = transport.request('run_start', **args)
            except (RemoteRefusal, UnknownMutation) as exc:
                admitted = self._run_admitted(request_id)
                if admitted is None:
                    raise
                transport.unknown_state, transport._uncertain_owner, transport._uncertain_run = prior
                if admitted is False:
                    if isinstance(exc, RemoteRefusal):
                        raise RemoteRefusal(str(exc)) from None
                    raise WorkspaceError('The score did not start. Try again.') from None
                result = admitted
            if (not isinstance(result, dict) or result.get('schema') != 'kptas-run/1'
                    or result.get('request_id') != request_id or type(result.get('run_id')) is not str):
                raise WorkspaceError('The owner did not confirm the score start. Refresh to check.')
            return result

    @staticmethod
    def _expected_args(snapshot):
        return {'expected': dict(snapshot.owner_token)} if snapshot.owner_token is not None else {}

    def coherent_frame(self, snapshot=None):
        """Explicit boundaries remain exact; an omitted boundary follows latest view."""
        with self._lock:
            admitted = self._atomic_admitted
            known = snapshot if snapshot is not None else admitted
            if known is not None and 'atomic_view_observation' in known.capabilities:
                args = self._expected_args(snapshot) if snapshot is not None else {}
                result = self.transport.request('view', **args)
                if result.get('schema') != 'kptas-view-observation/1':
                    raise WorkspaceError('view lacks atomic observation schema')
                current = self._snapshot_from_state(result.get('state'), atomic=True)
                if current.error or 'atomic_view_observation' not in current.capabilities or current.owner_token is None:
                    raise WorkspaceError('view lacks coherent owner authority')
                if snapshot is not None and (current.owner_token != snapshot.owner_token
                        or current.take_path != snapshot.take_path or current.tick != snapshot.tick
                        or current.score_sha256 != snapshot.score_sha256):
                    raise WorkspaceError('view differs from explicitly requested owner boundary')
                frame_tick, png, metadata = self._decode_frame(result.get('frame'))
                if frame_tick != current.tick:
                    raise WorkspaceError(f'atomic view image differs from the owner {_unit(current.clock_precision)}')
                return dataclasses.replace(current, frame_metadata=metadata), png
            requested = snapshot
            snapshot = snapshot or self.snapshot()
            for _ in range(2):
                frame_tick, png, metadata = self._render_frame()
                after = self.snapshot()
                if (frame_tick == snapshot.tick == after.tick and
                    (snapshot.take_path, snapshot.score_sha256, snapshot.owner_token)
                    == (after.take_path, after.score_sha256, after.owner_token)):
                    if requested is not None and (after.owner_token != requested.owner_token
                            or after.tick != requested.tick or after.take_path != requested.take_path
                            or after.score_sha256 != requested.score_sha256):
                        raise WorkspaceError('frame differs from explicitly requested owner boundary')
                    return dataclasses.replace(after, frame_metadata=metadata), png
                snapshot = after
            raise WorkspaceError('owner changed while reading frame; refresh required')

    def render(self):
        return self._render_frame()[1]

    def _render_frame(self):
        with self._lock:
            return self._decode_frame(self.transport.request('render'))

    def _decode_frame(self, result):
        if not isinstance(result, dict):
            raise WorkspaceError('frame must be an object')
        encoded = result.get('png')
        if not isinstance(encoded, dict) or not isinstance(encoded.get('b64'), str):
            raise WorkspaceError('render did not return PNG bytes')
        cached = getattr(self, '_decoded_frame', None)
        if cached is not None and cached[0] == encoded['b64']:
            png = cached[1]
        else:
            png = base64.b64decode(encoded['b64'], validate=True)
            if len(png) < 24 or not png.startswith(b'\x89PNG\r\n\x1a\n'):
                raise WorkspaceError('render returned invalid PNG')
            # Retain immutable bytes only. Every response still supplies fresh
            # tick/provenance below, including same-tick restore transitions.
            self._decoded_frame = (encoded['b64'], png)
        if type(result.get('tick')) is not int:
            raise WorkspaceError('render did not identify its position')
        view = result.get('view')
        metadata = dict(view) if isinstance(view, dict) else {}
        if not isinstance(metadata.get('source'), str):
            metadata['source'] = 'unknown'
        # Legacy owners provide no capture provenance. Do not synthesize
        # capture or gate ticks from the response's session tick.
        return result['tick'], png, metadata

    def _atomic_expected(self, expected):
        admitted = self._atomic_admitted
        # A view may add frame provenance to the same admitted boundary.
        return (isinstance(expected, WorkspaceSnapshot) and admitted is not None
                and dataclasses.replace(expected, frame_metadata=None) ==
                    dataclasses.replace(admitted, frame_metadata=None)
                and 'atomic_step_observation' in expected.capabilities
                and expected.owner_token is not None and not expected.error
                and not expected.unknown_state and not self.transport.unknown_state)

    def _validate_step_observation(self, result, before, count, include_inspect):
        observation = result.get('observation')
        if (not isinstance(observation, dict)
                or observation.get('schema') != 'kptas-step-observation/1'):
            raise WorkspaceError('step lacks atomic observation acknowledgement')
        state = observation.get('state')
        if not isinstance(state, dict) or not isinstance(state.get('takes'), list) or not state['takes']:
            raise WorkspaceError('atomic step state lacks take authority')
        take = state['takes'][-1]
        if (not isinstance(take, dict)
                or any(type(take.get(k)) is not int for k in ('first', 'last', 'durable_tick'))
                or not 0 <= take['first'] <= take['last']
                or type(result.get('epoch')) is not int or result['epoch'] < 0
                or not isinstance(result.get('state_root'), str)
                or re.fullmatch(r'[0-9a-f]{64}', result['state_root']) is None):
            raise WorkspaceError('atomic step boundary fields are malformed')
        after = self._snapshot_from_state(state, atomic=True)
        if (after.error or after.unknown_state or after.mode != before.mode
                or after.take_path != before.take_path or after.owner_token is None
                or 'atomic_step_observation' not in after.capabilities
                or after.owner_token['session_id'] != before.owner_token['session_id']
                or after.owner_token['revision'] != before.owner_token['revision'] + 1
                or type(result.get('tick')) is not int or after.tick != result['tick']
                or not before.tick <= after.tick <= before.tick + count
                or after.durable_tick != after.tick
                or after.ui_execution_display != before.ui_execution_display
                or after.clock_precision != before.clock_precision):
            raise WorkspaceError('atomic step state differs from committed owner boundary')
        if include_inspect:
            inspect = observation.get('inspect')
            if (not isinstance(inspect, dict) or inspect.get('source') != 'recorded_journal'
                    or inspect.get('take') != after.take_path or type(inspect.get('tick')) is not int
                    or inspect['tick'] != after.tick
                    or type(inspect.get('epoch')) is not int or inspect['epoch'] != result.get('epoch')
                    or type(inspect.get('durable_tick')) is not int or inspect['durable_tick'] != after.tick
                    or not isinstance(inspect.get('journal_sha256'), str)
                    or re.fullmatch(r'[0-9a-f]{64}', inspect['journal_sha256']) is None
                    or inspect.get('state_root') != result.get('state_root')
                    or not isinstance(inspect.get('journal'), dict)
                    or (inspect.get('semantics') is not None and not isinstance(inspect['semantics'], dict))
                    or inspect['journal'].get('tick') != after.tick
                    or inspect['journal'].get('sem') != inspect['semantics']):
                raise WorkspaceError('atomic step inspection differs from acknowledged journal')
        return after, observation

    def step(self, action, *, expected=None, ui_focus_display=None, include_inspect=False):
        from kptas.gym import to_note
        if type(include_inspect) is not bool:
            raise WorkspaceError('include_inspect must be a boolean')
        note = to_note(action)
        values = {f.name: getattr(note, f.name) for f in dataclasses.fields(note) if f.name not in {'source', 'line'}}
        with self._lock:
            self.last_step_observation = None
            if self.transport.unknown_state:
                raise UnknownMutation('previous command outcome unknown; reconcile owner before more input')
            atomic = self._atomic_expected(expected)
            before = expected if atomic else self._preflight(expected)
            atomic = atomic or ('atomic_step_observation' in before.capabilities
                                and before.owner_token is not None)
            if before.mode == 'archive':
                raise WorkspaceError('archive playback cannot execute authored inputs; create a branch first')
            if before.blocked_from is not None and before.tick + 1 >= before.blocked_from:
                raise WorkspaceError('timeline contains unsupported one-shot or execution barrier; score playback required')
            focus_args = self._ui_focus_args(before, ui_focus_display)
            optional = dict(include_state=True, include_inspect=include_inspect) if atomic else {}
            result = self.transport.request('step', action=values, **focus_args,
                                            **optional, **self._expected_args(before))
            self._transition = {}
            if atomic:
                try:
                    after, observation = self._validate_step_observation(result, before, note.count, include_inspect)
                except Exception as exc:
                    self._atomic_admitted = None
                    self.transport.unknown_state = True
                    self.transport._uncertain_owner = dict(before.owner_token)
                    raise UnknownMutation(f'atomic step acknowledgement invalid; reconcile owner: {exc}') from exc
                self.last_step_observation = observation
                return after
            after = self.snapshot()
            if (after.take_path != before.take_path or type(result.get('tick')) is not int
                    or after.tick != result['tick'] or not before.tick <= after.tick <= before.tick + note.count):
                self.transport.unknown_state = True
                raise UnknownMutation('owner changed during step; execution outcome requires reconciliation')
            return after

    def rewind(self, tick, *, expected=None):
        return self._seek(tick, expected=expected, backward_only=True)

    def configure_tool_bindings(self, shortcuts, *, expected=None):
        with self._lock:
            before = expected if self._atomic_expected(expected) else self._preflight(expected)
            if 'tool_dispatch' not in before.capabilities:
                raise WorkspaceError('native configurable tool dispatcher unavailable')
            result = self.transport.request('configure_tool_bindings', shortcuts=list(shortcuts),
                                            **self._expected_args(before))
            after = self._snapshot_from_state(result.get('state'), atomic=True)
            if (result.get('schema') != 'kptas-tool-config/1' or after.tick != before.tick or
                    after.take_path != before.take_path or after.owner_token is None or
                    after.owner_token['session_id'] != before.owner_token['session_id'] or
                    after.owner_token['revision'] <= before.owner_token['revision']):
                self.transport.unknown_state = True
                raise UnknownMutation('tool binding acknowledgement mismatch')
            return after

    def poll_tool_controls(self, *, expected=None):
        with self._lock:
            before = expected if self._atomic_expected(expected) else self._preflight(expected)
            if 'tool_dispatch' not in before.capabilities:
                return before, ''
            result = self.transport.request('poll_tool_controls', **self._expected_args(before))
            after = self._snapshot_from_state(result.get('state'), atomic=True)
            from .xhotkeys import TOOL_ACTIONS
            action = result.get('action')
            if (result.get('schema') != 'kptas-tool-poll/1' or action not in ('', *TOOL_ACTIONS) or
                    after.tick != before.tick or after.take_path != before.take_path or
                    after.owner_token != before.owner_token):
                raise WorkspaceError('tool poll acknowledgement mismatch')
            return after, action

    def set_engaged(self, engaged, *, pause_on_engage=True, expected=None):
        if type(engaged) is not bool or type(pause_on_engage) is not bool:
            raise WorkspaceError('engagement and pause policy must be booleans')
        with self._lock:
            before = expected if self._atomic_expected(expected) else self._preflight(expected)
            if 'input_source_transition' not in before.capabilities or before.owner_token is None:
                raise WorkspaceError('native acknowledged input source protocol unavailable')
            result = self.transport.request('set_engaged', engaged=engaged,
                                            pause_on_engage=pause_on_engage, **self._expected_args(before))
            try:
                after = self._snapshot_from_state(result.get('state'), atomic=True)
                if (result.get('schema') != 'kptas-engagement/1' or result.get('engaged') is not engaged or
                        result.get('tick') != before.tick or after.tick != before.tick or
                        after.take_path != before.take_path or after.owner_token is None or
                        after.owner_token['session_id'] != before.owner_token['session_id'] or
                        after.owner_token['revision'] <= before.owner_token['revision'] or
                        after.engaged is not engaged or after.pause_on_engage is not pause_on_engage):
                    raise WorkspaceError('engagement transition acknowledgement mismatch')
                return after
            except Exception as exc:
                self.transport.unknown_state = True
                raise UnknownMutation(f'engagement outcome requires reconciliation: {exc}') from exc

    def seek(self, tick, *, expected=None):
        """Strict restore to a retained archive tick; live authoring stays backward."""
        return self._seek(tick, expected=expected, backward_only=False)

    def _seek(self, tick, *, expected=None, backward_only=False):
        with self._lock:
            before = self._preflight(expected)
            maximum = before.tick
            if before.mode == 'archive' and not backward_only:
                if before.recorded_last is None:
                    raise WorkspaceError('archive owner does not expose a valid retained recording range')
                maximum = before.recorded_last
            if type(tick) is not int or not 0 <= tick <= maximum:
                if before.mode == 'archive' and not backward_only:
                    raise WorkspaceError(f'archive seek requires a retained {_unit(before.clock_precision)} in 0..{maximum}')
                raise WorkspaceError('current live owner supports backward rewind only; forward restore is unavailable')
            if before.mode != 'archive' and tick == before.tick:
                # Already there. A live restore to the head frame has no continuation to verify,
                # so sending it would seal the take and stop the session; seeking here is a no-op.
                return before
            self._transition = self.transport.request('rewind', tick=tick, **self._expected_args(before))
            receipt = self._transition.get('receipt', {})
            if not isinstance(receipt, dict):
                self.transport.unknown_state = True
                raise UnknownMutation('rewind failed verification; owner needs recovery before more input')
            mode = receipt.get('mode')
            # Restore at the frame, or restore@K + replay (hybrid) for an uncaptured one.
            valid = self._transition.get('rewound') is True and verified_landing(receipt, tick)
            if self._transition.get('noop') is True:
                try:
                    proof = json.loads(receipt.get('verifier', ''))
                except (ValueError, TypeError):
                    proof = {}
                try:
                    origin = self._recording_genesis(before.take_path)
                    ack = self._transition.get('capture_ack')
                    matched = self._capture_proof_matches(proof, receipt, origin, tick,
                        ack.get('epoch') if isinstance(ack, dict) else None,
                        before.owner_token or {}, ack)
                except Exception:
                    matched = False
                valid = (matched and tick == before.tick and self._transition.get('rewound') is False
                         and receipt.get('result') == 'pass' and not receipt.get('fallback')
                         and mode == 'replay' and receipt.get('verify_level') == 'diff_only'
                         and receipt.get('canary') == 'none' and type(receipt.get('write_pages')) is int
                         and receipt['write_pages'] == 0 and type(receipt.get('from_tick')) is int
                         and receipt.get('from_tick') == tick and isinstance(proof, dict)
                         and proof.get('format') == 'kptas-held-capture/1'
                         and proof.get('scope') == 'same_process_capture_continuity'
                         and proof.get('native_restore_qualified') is False
                         and before.owner_token is not None
                         and proof.get('session_id') == before.owner_token['session_id']
                         and proof.get('tick') == tick)
            if not valid:
                self.transport.unknown_state = True
                raise UnknownMutation('rewind failed verification; owner needs recovery before more input')
            try:
                after = self.snapshot()
            except Exception as exc:
                self.transport.unknown_state = True
                raise UnknownMutation('rewind outcome requires reconciliation after snapshot failure') from exc
            if (after.tick != tick or type(self._transition.get('tick')) is not int
                    or self._transition.get('tick') != tick or after.error
                    or after.mode != before.mode
                    or (before.owner_token is not None and (after.owner_token is None
                        or after.owner_token['session_id'] != before.owner_token['session_id']
                        or after.owner_token['revision'] <= before.owner_token['revision']))
                    or (before.mode == 'archive' and after.take_path != before.take_path)):
                self.transport.unknown_state = True
                raise UnknownMutation(f'rewind acknowledgement does not match the requested {_unit(before.clock_precision)}')
            if type(receipt.get('to_tick')) is not int or receipt.get('to_tick') != tick:
                self.transport.unknown_state = True
                raise UnknownMutation(f'rewind receipt destination does not match the requested {_unit(before.clock_precision)}')
            self.last_landing = landing_label(receipt, after.clock_precision)
            return after

    def mark(self, name, *, expected=None):
        with self._lock:
            before = self._preflight(expected)
            if before.mode == 'archive':
                raise WorkspaceError('archive playback cannot write marks; create a branch first')
            self.transport.request('mark', name=name, **self._expected_args(before))
            return self.snapshot()

    @staticmethod
    def _ui_focus_args(snapshot, display):
        if display is None:
            return {}
        if (type(display) is not str or not display or snapshot.owner_token is None
                or 'ui_execution_focus' not in snapshot.capabilities
                or display != getattr(snapshot, 'ui_execution_display', None)):
            raise WorkspaceError('owner does not advertise qualified UI execution focus')
        return {'ui_focus_display': display}

    def _validate_recorded_observation(self, result, before):
        observation = result.get('observation')
        if (not isinstance(observation, dict)
                or observation.get('schema') != 'kptas-step-observation/1'):
            raise WorkspaceError('recorded step lacks atomic observation acknowledgement')
        after = self._snapshot_from_state(observation.get('state'), atomic=True)
        if (after.unknown_state or after.mode != 'archive' or after.take_path != before.take_path
                or after.owner_token is None or before.owner_token is None
                or after.owner_token['session_id'] != before.owner_token['session_id']
                or after.owner_token['revision'] != before.owner_token['revision'] + 1
                or type(result.get('tick')) is not int or after.tick != result['tick']
                or not before.tick <= after.tick <= before.tick + 1):
            raise WorkspaceError('recorded step state differs from committed owner boundary')
        return after, observation

    def recorded_step(self, count=1, *, expected=None, ui_focus_display=None):
        with self._lock:
            self.last_step_observation = None
            atomic = self._atomic_expected(expected)
            before = expected if atomic else self._preflight(expected)
            atomic = atomic or ('atomic_step_observation' in before.capabilities
                                and before.owner_token is not None)
            if before.mode != 'archive' or 'recorded_step' not in before.capabilities:
                raise WorkspaceError('owner is not in recorded playback mode')
            if type(count) is not int or count != 1:
                raise WorkspaceError(f'recorded playback supports exactly one acknowledged {_unit(before.clock_precision)} per command')
            optional = dict(include_state=True) if atomic else {}
            result = self.transport.request('step', **self._ui_focus_args(before, ui_focus_display),
                                            **optional, **self._expected_args(before))
            if atomic:
                # The owner returns the post-step state under the same lock as
                # the step itself; nothing is assumed beyond that acknowledgement.
                try:
                    after, observation = self._validate_recorded_observation(result, before)
                except Exception as exc:
                    self._atomic_admitted = None
                    self.transport.unknown_state = True
                    self.transport._uncertain_owner = dict(before.owner_token)
                    raise UnknownMutation(f'recorded step acknowledgement invalid; reconcile owner: {exc}') from exc
                self.last_step_observation = observation
                return after
            after = self.snapshot()
            if after.tick != result.get('tick') or after.take_path != before.take_path:
                self.transport.unknown_state = True
                raise UnknownMutation('recorded playback acknowledgement changed; reconcile owner')
            return after

    @staticmethod
    def _recording_genesis(root):
        from kptas.holo.journal import ChainState
        from kptas.types import CHUNK_HEAD_SIZE, CHUNK_TRAILER_SIZE, read_chunk
        flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
        fd = os.open(Path(root) / 'journal', flags)
        with os.fdopen(fd, 'rb') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise WorkspaceError('recording journal must be a regular file')
            chain = ChainState()
            for _ in range(3):
                head = stream.read(CHUNK_HEAD_SIZE)
                if len(head) != CHUNK_HEAD_SIZE:
                    raise WorkspaceError('recording genesis is truncated')
                length = int.from_bytes(head[6:10], 'little')
                if length > 4 * 1024 * 1024:
                    raise WorkspaceError('recording genesis exceeds admission bound')
                kind, payload, _ = read_chunk(head + stream.read(length + CHUNK_TRAILER_SIZE))
                chain.feed(kind, payload)
            return chain

    @staticmethod
    def _capture_proof_matches(proof, receipt, origin, tick, epoch, owner, capture_ack):
        captured = (isinstance(proof, dict) and proof.get('format') == 'kptas-held-capture/1'
                    and proof.get('scope') == 'same_process_capture_continuity'
                    and proof.get('native_restore_qualified') is False
                    and proof.get('session_id') == owner['session_id']
                    and proof.get('parent') == origin.bundle_id
                    and type(proof.get('tick')) is int and proof['tick'] == tick
                    and type(proof.get('epoch')) is int and proof['epoch'] == epoch
                    and proof.get('state_root') == receipt.get('state_root')
                    and proof.get('env_key') == receipt.get('env_key') == origin.env.key()
                    and all(isinstance(proof.get(k), str)
                            and re.fullmatch('[0-9a-f]{64}', proof[k]) is not None
                            for k in ('state_root', 'files_root', 'env_key'))
                    and isinstance(capture_ack, dict)
                    and type(capture_ack.get('tick')) is int and type(capture_ack.get('epoch')) is int
                    and capture_ack == {k: proof.get(k) for k in ('format', 'session_id',
                        'parent', 'tick', 'epoch', 'state_root', 'files_root', 'env_key')}
                    and proof.get('backend') == origin.env.extra.get('backend')
                    and proof.get('root_qualification') == origin.env.mask_registry
                    and type(proof.get('offset')) is int and type(proof.get('gate_tick')) is int
                    and proof['gate_tick'] + proof['offset'] == tick
                    and ((proof['backend'] == 'pysim' and proof.get('evidence') == 'pysim-memory-and-files')
                         or (proof['backend'] == 'shim' and proof.get('evidence') == 'native-memory-audit-and-files')))
        if captured and proof['backend'] == 'shim':
            from kptas.tier.qualification import canonical_qualification
            audit = proof.get('audit')
            canonical_qualification(proof['root_qualification'])
            captured = (proof.get('gate_source') == 'virtual_devices'
                        and proof.get('sampled_at_gate') is True and isinstance(audit, dict)
                        and audit.get('root') == receipt['state_root']
                        and audit.get('scope') == 'memory_only' and audit.get('ok') is True
                        and audit.get('external_pending') is False
                        and all(type(audit.get(k)) is int and audit[k] == 0
                                for k in ('key_mismatch', 'missing', 'extra', 'prot_mismatch')))
        return captured

    @staticmethod
    def _branch_publication(parent, child, tick, score, checkpoint, owner, capture_ack=None):
        """Check only bounded genesis/receipt metadata, never admit a Bundle.

        Native placement and input-prefix validation belong to the resident
        owner. This confirms its published child identity and complete score
        before a caller may retire its draft.
        """
        from kptas.holo.journal import ChainState
        from kptas.types import CHUNK_HEAD_SIZE, CHUNK_TRAILER_SIZE, read_chunk
        flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)

        def regular(directory, name):
            fd = os.open(name, flags, dir_fd=directory)
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise WorkspaceError('branch publication must use regular files')
                stream = os.fdopen(fd, 'rb')
                fd = None
                return stream
            finally:
                if fd is not None:
                    os.close(fd)

        def genesis(root, with_receipt=False):
            directory = os.open(root, flags | getattr(os, 'O_DIRECTORY', 0))
            try:
                chain = ChainState()
                with regular(directory, 'journal') as stream:
                    size = os.fstat(stream.fileno()).st_size
                    score_bytes = None
                    receipt = None
                    for i in range(4 if with_receipt else 3):
                        head = stream.read(CHUNK_HEAD_SIZE)
                        if len(head) != CHUNK_HEAD_SIZE:
                            raise WorkspaceError('branch genesis is truncated')
                        length = int.from_bytes(head[6:10], 'little')
                        if length > 4 * 1024 * 1024:
                            raise WorkspaceError('branch genesis exceeds the publication bound')
                        body = stream.read(length + CHUNK_TRAILER_SIZE)
                        kind, payload, _ = read_chunk(head + body)
                        _, _, decoded = chain.feed(kind, payload)
                        if i == 1:
                            score_bytes = payload
                        if i == 3:
                            if kind != b'RCPT':
                                raise WorkspaceError('branch lacks its verified restore receipt')
                            receipt = decoded.to_obj()
                    end = stream.tell()
                metadata = {}
                if with_receipt:
                    for name in ('meta.json', 'durable.json'):
                        with regular(directory, name) as stream:
                            data = stream.read(64 * 1024 + 1)
                        if len(data) > 64 * 1024:
                            raise WorkspaceError('branch metadata exceeds the publication bound')
                        metadata[name] = json.loads(data)
                return chain, score_bytes, receipt, end, size, metadata
            finally:
                os.close(directory)

        origin, _, _, _, _, _ = genesis(parent)
        chain, published, receipt, end, size, metadata = genesis(child, True)
        lineage = chain.header.parent
        meta, durable = metadata['meta.json'], metadata['durable.json']
        # A re-recorded branch (restore@K + replay into the child) links to its
        # parent at the anchor K, not at the branch gate.
        anchor = WorkspaceController._hybrid_branch_anchor(receipt, tick)
        if (lineage is None or lineage.holo != origin.bundle_id
                or lineage.tick != (tick if anchor is None else anchor)
                or meta.get('parent_abspath') != str(Path(parent).resolve())
                or not isinstance(meta.get('parent_path'), str)
                or (Path(child) / meta['parent_path']).resolve() != Path(parent).resolve()
                or published != score.encode('utf-8')):
            raise WorkspaceError('branch child ancestry or complete score does not match the request')
        restored = (receipt.get('kind') == 'seek' and receipt.get('mode') == 'restore'
                    and receipt.get('canary') == 'pass' and receipt.get('verify_level') == 'full_root')
        restored = restored or anchor is not None
        captured = False
        if (receipt.get('kind') == 'verify' and receipt.get('mode') == 'replay'
                and receipt.get('canary') == 'none' and receipt.get('verify_level') == 'diff_only'
                and type(receipt.get('from_tick')) is int and receipt['from_tick'] == tick
                and type(receipt.get('write_pages')) is int and receipt['write_pages'] == 0):
            try:
                proof = json.loads(receipt.get('verifier', ''))
            except (ValueError, TypeError):
                proof = {}
            captured = WorkspaceController._capture_proof_matches(
                proof, receipt, origin, tick, durable.get('epoch'), owner, capture_ack)
        if (not (restored or captured) or receipt.get('result') != 'pass'
                or receipt.get('fallback')
                or type(receipt.get('to_tick')) is not int or receipt['to_tick'] != tick):
            raise WorkspaceError('branch child lacks its exact restore or held-capture acknowledgement')
        if (not isinstance(checkpoint, dict) or durable != checkpoint
                or durable.get('format') != 'kptas-durable/1'
                or type(durable.get('tick')) is not int or durable['tick'] != tick
                # A plain branch inherits its gate from the parent; a re-recorded
                # one (restore@K + replay) wrote frames K+1..tick itself.
                or durable.get('inherited') is not (anchor is None)
                or not WorkspaceController._durable_restore_point(durable, tick)
                or durable.get('bundle_id') != chain.bundle_id
                or durable.get('chain_link') != chain.link.hex()
                or durable.get('state_root') != receipt.get('state_root')
                or durable.get('journal_bytes') != end or end != size):
            raise WorkspaceError('branch child lacks the matching published durable boundary')

    @staticmethod
    def _hybrid_branch_anchor(receipt, tick):
        """Anchor K of a verified restore@K + re-record receipt, else None."""
        if (not isinstance(receipt, dict) or receipt.get('kind') != 'seek' or receipt.get('mode') != 'hybrid'
                or receipt.get('verify_level') != 'full_root' or receipt.get('canary') != 'none'
                or not isinstance(receipt.get('verifier'), str)):
            return None
        verifier = receipt['verifier']
        found = re.search(r'origin restore@(\d+); replayed (\d+) (?:ticks|frames) into the branch', verifier)
        if found is None or 'anchor full_root, canary pass' not in verifier:
            return None
        anchor, replayed = int(found.group(1)), int(found.group(2))
        if not 0 <= anchor <= tick or anchor + replayed != tick:
            return None
        return anchor

    @staticmethod
    def _durable_restore_point(durable, tick):
        """The checkpoint names a captured restore point K <= tick (capture cadence)."""
        if 'restore_tick' in durable:
            restore = durable.get('restore_tick')
            return (type(restore) is int and 0 <= restore <= tick
                    and isinstance(durable.get('restore_root'), str))
        return durable.get('captured_restore_point') is True

    def branch(self, edited_score, destination, *, expected=None):
        """Commit a complete edited score on this owner's paused archive gate.

        Returns only after the new child is durably published and verified.
        Caller drafts are never changed here, including on ambiguous failure.
        The child continues through recorded_step using the same Supervisor.
        """
        from kptas.score.compile import CompiledScore
        if isinstance(edited_score, CompiledScore):
            text = edited_score.text()
        elif isinstance(edited_score, str) and edited_score.lstrip().startswith('#!score'):
            text = edited_score
        else:
            raise WorkspaceError('branch requires a complete edited score, not a filename')
        if len(text.encode('utf-8')) > 4 * 1024 * 1024:
            raise WorkspaceError('branch score exceeds the publication bound')
        text = compile_score(text).text()
        if len(text.encode('utf-8')) > 4 * 1024 * 1024:
            raise WorkspaceError('expanded branch score exceeds the publication bound')
        digest = hashlib.sha256(text.encode('utf-8')).hexdigest()
        path = Path(destination).expanduser().resolve()
        if path.suffix != '.holo' or len(path.name) <= 5 or path.exists() or not path.parent.is_dir():
            raise WorkspaceError('choose a new .holo branch directory in an existing folder')
        with self._lock:
            before = self._preflight(expected)
            if before.unknown_state:
                raise UnknownMutation('previous command outcome unknown; reconcile before branching')
            if before.mode != 'archive' or 'branch' not in before.capabilities or before.owner_token is None:
                raise WorkspaceError('this owner does not expose atomic archive branching')
            try:
                result = self.transport.request('branch', score=text, out=str(path), **self._expected_args(before))
                child = self.snapshot()
                if (result.get('take') != str(path) or type(result.get('tick')) is not int
                        or result['tick'] != before.tick or child.take_path != str(path)
                        or child.tick != before.tick or child.mode != 'archive' or child.error
                        or child.score_sha256 != digest or child.score_text != text
                        or child.owner_token is None
                        or child.owner_token['session_id'] != before.owner_token['session_id']
                        or child.owner_token['revision'] <= before.owner_token['revision']):
                    raise WorkspaceError('branch child acknowledgement changed before publication')
                saved = self.transport.request('save_recording', **self._expected_args(child))
                after = self.snapshot()
                if (saved.get('saved') is not True or saved.get('read_only') is not False
                        or saved.get('take') != str(path) or type(saved.get('tick')) is not int
                        or saved['tick'] != before.tick or after.take_path != str(path)
                        or after.tick != before.tick or after.durable_tick != before.tick
                        or after.score_sha256 != digest or after.score_text != text
                        or after.mode != 'archive' or after.error or after.owner_token is None
                        or after.owner_token['session_id'] != before.owner_token['session_id']
                        or after.owner_token['revision'] <= child.owner_token['revision']):
                    raise WorkspaceError('branch publication acknowledgement changed')
                self._branch_publication(before.take_path, path, before.tick, text, saved.get('checkpoint'), before.owner_token, result.get('held_capture'))
            except Exception as exc:
                self.transport.unknown_state = True
                self.transport._uncertain_owner = dict(before.owner_token)
                raise UnknownMutation(f'branch outcome requires reconciliation; draft retained: {exc}') from exc
            self._transition = {}
            return dataclasses.replace(after, rewound=None, receipt=None)

    def save_recording(self, *, expected=None):
        with self._lock:
            before = self._preflight(expected)
            if 'recording_save' not in before.capabilities:
                raise WorkspaceError('this owner does not expose recording save; its existing durable state is unchanged')
            self.last_save = None
            unit = _unit(before.clock_precision)
            result = self.transport.request('save_recording', **self._expected_args(before))
            after = self.snapshot()
            if (result.get('saved') is not True or result.get('take') != after.take_path
                    or result.get('tick') != after.tick or after.error
                    or (after.take_path, after.tick) != (before.take_path, before.tick)):
                self.transport.unknown_state = True
                raise UnknownMutation('recording save acknowledgement changed; reconcile owner')
            if result.get('read_only') is True:
                # An archive that is already on disk; nothing new was written.
                self.last_save = dict(read_only=True, durable_tick=None, restore_tick=None, unit=unit)
                return after
            checkpoint = result.get('checkpoint')
            if checkpoint is None and 'checkpoint' not in result:
                # A live recording save persists through _persist(), which writes
                # the take's durable.json marker but does not echo it back.
                checkpoint = self._read_durable_marker(after.take_path)
            self.last_save = dict(read_only=False, **self._save_restore_point(checkpoint, before, after, unit))
            return after

    @staticmethod
    def _read_durable_marker(take_path):
        """The take's published durable.json, bounded and without following links; None if absent."""
        flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
        try:
            directory = os.open(take_path, flags | getattr(os, 'O_DIRECTORY', 0))
        except OSError:
            return None
        try:
            fd = os.open('durable.json', flags, dir_fd=directory)
        except OSError:
            return None
        finally:
            os.close(directory)
        with os.fdopen(fd, 'rb') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                return None
            data = stream.read(64 * 1024 + 1)
        if len(data) > 64 * 1024:
            return None
        try:
            marker = json.loads(data)
        except ValueError:
            return None
        return marker if isinstance(marker, dict) else None

    @staticmethod
    def _save_restore_point(checkpoint, before, after, unit):
        """Accept only a durable checkpoint at this gate that names a restore point.

        Capture cadence: the saved frame N may itself be uncaptured. The resume
        point is then restore_tick K <= N (restore K, replay to N). With no
        restore point at all the save is refused, not acknowledged.
        """
        tick = before.tick
        if (not isinstance(checkpoint, dict) or checkpoint.get('format') != 'kptas-durable/1'
                or type(checkpoint.get('tick')) is not int or checkpoint['tick'] != tick):
            raise WorkspaceError(f'owner did not return a durable checkpoint for {unit} {tick}; '
                                 'the save is not confirmed')
        if after.durable_tick != tick:
            raise WorkspaceError(f'owner reports durable data through {unit} {after.durable_tick}, '
                                 f'not {unit} {tick}; the save is not confirmed')
        if 'restore_tick' in checkpoint:
            restore = checkpoint.get('restore_tick')
            if restore is None:
                raise WorkspaceError(f'recording written through {unit} {tick}, but it has no captured '
                                     'restore point to resume from; the save is not confirmed')
            if (type(restore) is not int or not 0 <= restore <= tick
                    or not isinstance(checkpoint.get('restore_root'), str)
                    or re.fullmatch(r'[0-9a-f]{64}', checkpoint['restore_root']) is None):
                raise WorkspaceError('durable checkpoint names an invalid restore point; the save is not confirmed')
        elif checkpoint.get('captured_restore_point') is True:
            restore = tick  # Older owners: only an exactly captured gate is a restore point.
        else:
            raise WorkspaceError(f'recording written through {unit} {tick}, but it has no captured '
                                 'restore point to resume from; the save is not confirmed')
        return dict(durable_tick=tick, restore_tick=restore, unit=unit)

    def load_recording(self, path, tick=0, *, expected=None):
        with self._lock:
            before = self._preflight(expected)
            if 'recording_load' not in before.capabilities:
                raise WorkspaceError('this owner does not expose recording loading')
            if type(tick) is not int or tick < 0:
                raise WorkspaceError(f'recording load {_unit(before.clock_precision)} must be a nonnegative integer')
            self.last_landing = ''
            result = self.transport.request('load_recording', path=str(path), tick=tick, **self._expected_args(before))
            receipt = result.get('receipt') or {}
            # Restore at the frame, or restore@K + replay (hybrid) for an uncaptured one.
            valid = (result.get('loaded') is True and result.get('tick') == tick
                     and verified_landing(receipt, tick))
            after = self.snapshot()
            same_owner = (before.owner_token is not None and after.owner_token is not None
                and before.owner_token['session_id'] == after.owner_token['session_id'])
            expected_path = str(Path(path).expanduser().resolve())
            if (not valid or after.tick != tick or after.mode != 'archive' or not same_owner or after.error
                    or after.take_path != result.get('take') or after.take_path != expected_path):
                self.transport.unknown_state = True
                raise UnknownMutation('recording load did not retain the same verified owner; reconcile owner')
            self.last_landing = landing_label(receipt, after.clock_precision)
            return after
