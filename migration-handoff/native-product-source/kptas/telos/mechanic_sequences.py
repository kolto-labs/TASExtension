"""Declarative combo guides fed by committed inputs, never authored previews.

No game access, background polling, or input injection occurs in this module.
Callers must bind receipts to their already validated owner command result.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import os
from pathlib import Path
import re
import stat
import tomllib
from typing import Mapping
from types import MappingProxyType

from kptas.score.keys import BUTTONS, canonical_key, token_keys


class SequenceError(ValueError):
    pass


def _fields(value, allowed, context):
    if not isinstance(value, dict) or set(value) - set(allowed):
        raise SequenceError(f'{context}: unknown fields or invalid table')


def _integer(value, context, maximum=1_000_000):
    if type(value) is not int or not 0 <= value <= maximum:
        raise SequenceError(f'{context}: expected integer between 0 and {maximum}')
    return value


def _text(value, context, maximum=200):
    if not isinstance(value, str) or not value or len(value) > maximum or any(ord(c) < 32 for c in value):
        raise SequenceError(f'{context}: expected short printable text')
    return value


def _names(value, context, buttons=False):
    if not isinstance(value, list) or len(value) > 32:
        raise SequenceError(f'{context}: expected at most 32 input names')
    names = []
    for item in value:
        if not isinstance(item, str):
            raise SequenceError(f'{context}: input names must be strings')
        name = item.lower() if buttons else canonical_key(item)
        if buttons and name not in BUTTONS:
            raise SequenceError(f'{context}: unknown mouse button {item}')
        if name in names:
            raise SequenceError(f'{context}: duplicate input {name}')
        names.append(name)
    return frozenset(names)


@dataclass(frozen=True)
class Step:
    id: str
    label: str
    kind: str
    match: str = 'press'
    inputs: frozenset[str] = frozenset()
    exact: bool = True
    minimum: int = 0
    maximum: int = 120
    duration: int = 1
    predicate: str | None = None
    equals: bool | int | str | None = None


@dataclass(frozen=True)
class Profile:
    id: str
    title: str
    precision: str
    strict: bool
    steps: tuple[Step, ...]
    sha256: str


def load_profile(path: str | Path, *, predicates: Mapping[str, type] | None = None) -> Profile:
    """Bounded regular-file loader; predicates are explicitly registered types."""
    predicates = predicates or {}
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, 'O_NOFOLLOW', 0))
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 65536:
            raise SequenceError('Sequence must be a regular file no larger than 64 KiB')
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise SequenceError('Sequence exceeds 64 KiB')
    finally:
        os.close(fd)
    try:
        data = tomllib.loads(raw.decode('utf-8'))
        _fields(data, {'version','id','title','precision','start','unrelated_inputs','feedback','steps'}, 'sequence')
        if type(data.get('version')) is not int or data['version'] != 1:
            raise SequenceError('Unsupported sequence version')
        if data.get('precision') not in {'frame','tick'} or data.get('start') != 'explicit-boundary':
            raise SequenceError('Specify frame/tick precision and explicit-boundary start')
        if data.get('unrelated_inputs', 'ignore') not in {'ignore','reject'}:
            raise SequenceError('unrelated_inputs must be ignore or reject')
        feedback = data.get('feedback', {})
        _fields(feedback, {'motion','sound'}, 'feedback')
        if feedback.get('motion', 'respect-system') not in {'respect-system','none'} or feedback.get('sound', False) is not False:
            raise SequenceError('This implementation supports reduced motion and silent visual feedback only')
        rows = data.get('steps')
        if not isinstance(rows, list) or not 1 <= len(rows) <= 256:
            raise SequenceError('Sequence requires 1 to 256 steps')
        steps, ids = [], set()
        for i, row in enumerate(rows):
            context = f'steps[{i}]'
            _fields(row, {'id','label','kind','match','keys','buttons','chord','window_min','window_max','duration','predicate','equals'}, context)
            ident = _text(row.get('id'), context+'.id', 80)
            if ident in ids:
                raise SequenceError(f'{context}: duplicate step ID')
            ids.add(ident)
            label = _text(row.get('label'), context+'.label')
            minimum = _integer(row.get('window_min', 0), context+'.window_min')
            maximum = _integer(row.get('window_max', 120), context+'.window_max')
            if maximum < minimum:
                raise SequenceError(f'{context}: timing window is reversed')
            kind = row.get('kind')
            if kind == 'outcome':
                if set(row) & {'match','keys','buttons','chord','duration'}:
                    raise SequenceError(f'{context}: outcome cannot contain input matching fields')
                predicate = row.get('predicate')
                expected_type = predicates.get(predicate) if isinstance(predicate, str) else None
                equals = row.get('equals')
                if expected_type not in {bool,int,str} or type(equals) is not expected_type:
                    raise SequenceError(f'{context}: unregistered predicate or wrong value type')
                steps.append(Step(ident,label,kind,minimum=minimum,maximum=maximum,predicate=predicate,equals=equals))
            elif kind == 'input':
                if set(row) & {'predicate','equals'}:
                    raise SequenceError(f'{context}: input cannot contain outcome fields')
                match = row.get('match', 'press')
                chord = row.get('chord', 'exact')
                duration = _integer(row.get('duration', 1), context+'.duration')
                if match not in {'press','release','hold'} or chord not in {'exact','contains'} or duration < 1:
                    raise SequenceError(f'{context}: unsupported match, chord or duration')
                if match != 'hold' and 'duration' in row:
                    raise SequenceError(f'{context}: duration is only valid for hold')
                inputs = _names(row.get('keys', []), context+'.keys') | _names(row.get('buttons', []),context+'.buttons',True)
                if not inputs:
                    raise SequenceError(f'{context}: specify a key or button')
                steps.append(Step(ident,label,kind,match,inputs,chord=='exact',minimum,maximum,duration))
            else:
                raise SequenceError(f'{context}: unsupported step kind')
        return Profile(_text(data.get('id'),'id',80),_text(data.get('title'),'title'),data['precision'],data.get('unrelated_inputs')=='reject',tuple(steps),hashlib.sha256(raw).hexdigest())
    except (UnicodeError, tomllib.TOMLDecodeError, ValueError, TypeError) as exc:
        if isinstance(exc, SequenceError):
            raise
        raise SequenceError(str(exc)) from exc


@dataclass(frozen=True)
class InputReceipt:
    session: str
    take: str
    revision: int
    boundary: int
    precision: str
    held: frozenset[str]
    journal_sha256: str
    observations: Mapping[str, bool | int | str] = field(default_factory=dict)

    def __post_init__(self):
        if (type(self.boundary) is not int or self.boundary < 0
                or type(self.revision) is not int or self.revision < 0
                or type(self.precision) is not str or self.precision not in {'frame','tick'}
                or not isinstance(self.session, str) or not self.session
                or not isinstance(self.take, str) or not self.take
                or not isinstance(self.journal_sha256, str)
                or re.fullmatch('[0-9a-f]{64}', self.journal_sha256) is None):
            raise SequenceError('Malformed committed input receipt')
        held = frozenset(self.held)
        for name in held:
            if not isinstance(name, str) or (name not in BUTTONS and canonical_key(name) != name):
                raise SequenceError('Receipt inputs must use canonical names')
        observations = dict(self.observations)
        if len(observations) > 64 or any(not isinstance(key, str) or len(key) > 200
                or type(value) not in {bool,int,str} for key, value in observations.items()):
            raise SequenceError('Malformed typed observation receipt')
        object.__setattr__(self, 'held', held)
        object.__setattr__(self, 'observations', MappingProxyType(observations))

    @property
    def identity(self):
        return self.session, self.take, self.revision, self.boundary, self.journal_sha256


def acknowledged_step_receipt(before, after, action, observation, *, precision: str) -> InputReceipt:
    """Adapter for WorkspaceController's validated SINGLE-boundary step result.

    A multi-boundary final journal proves its final input, not intermediate edges.
    Refusing that result prevents fabricated combo progress during fast playback.
    """
    from kptas.gym import to_note
    if type(precision) is not str or precision not in {'frame', 'tick'}:
        raise SequenceError('Owner precision metadata is unavailable')
    note = to_note(action)
    if note.count != 1 or after.tick != before.tick + 1:
        raise SequenceError('Per-boundary input evidence is required for multi-boundary steps')
    token, prior = after.owner_token, before.owner_token
    if (not token or not prior or after.error or after.unknown_state or after.durable_tick != after.tick
            or token['session_id'] != prior['session_id'] or token['revision'] != prior['revision'] + 1
            or after.take_path != before.take_path
            or before.clock_precision != precision or after.clock_precision != precision):
        raise SequenceError('Input lacks a committed owner acknowledgement')
    if not isinstance(observation, dict) or observation.get('schema') != 'kptas-step-observation/1':
        raise SequenceError('Input lacks an atomic recorded journal')
    inspect = observation.get('inspect', {})
    if not isinstance(inspect, dict):
        raise SequenceError('Input lacks a recorded inspection table')
    digest = inspect.get('journal_sha256', '')
    if (inspect.get('source') != 'recorded_journal' or inspect.get('take') != after.take_path
            or inspect.get('tick') != after.tick or inspect.get('durable_tick') != after.tick
            or not isinstance(digest, str) or re.fullmatch('[0-9a-f]{64}',digest) is None):
        raise SequenceError('Input inspection does not match the acknowledged boundary')
    held = frozenset(key for token in note.held for key in token_keys(token)) | frozenset(note.buttons)
    return InputReceipt(token['session_id'],after.take_path,token['revision'],after.tick,precision,held,digest)


class SequenceTracker:
    """One explicit lineage, ordered edges, and bounded rewindable event history."""
    def __init__(self, profile: Profile):
        self.profile = profile
        self.events: list[InputReceipt] = []
        self.hits: list[int] = []
        self.status = 'Load a recording, then start at an acknowledged boundary'
        self.active = False
        self._seen = set()
        self._hold = 0

    def start(self, *, session: str, take: str, boundary: int, precision: str, held: frozenset[str], revision: int):
        if precision != self.profile.precision:
            raise SequenceError(f'Sequence requires {self.profile.precision} precision')
        self.session, self.take, self.start_boundary = session, take, boundary
        self.boundary, self.revision, self.held = boundary, revision, held
        self.baseline = held
        self.start_revision = revision
        self.events.clear(); self.hits.clear(); self._seen.clear(); self._hold = 0; self._failed = False
        self.active = True
        self.status = 'Ready'

    def suspend(self, reason):
        if self.active:
            self.status = reason
            self.active = False

    def consume(self, receipt: InputReceipt):
        if not self.active or receipt.identity in self._seen:
            return False
        if (receipt.session != self.session or receipt.take != self.take or receipt.precision != self.profile.precision
                or receipt.boundary != self.boundary + 1 or receipt.revision <= self.revision):
            self.suspend('Input history changed or has a gap; start again from an acknowledged boundary')
            return False
        if len(self.events) >= 100_000:
            self.suspend('Sequence history limit reached; start a new guide')
            return False
        self.events.append(receipt); self._seen.add(receipt.identity)
        prior = self.held
        self.held, self.boundary, self.revision = receipt.held, receipt.boundary, receipt.revision
        if self._failed or len(self.hits) == len(self.profile.steps):
            return False
        step = self.profile.steps[len(self.hits)]
        elapsed = receipt.boundary - (self.hits[-1] if self.hits else self.start_boundary)
        if elapsed > step.maximum:
            self._failed = True
            self.status = 'Timing window missed; rewind or restart this guide'
            return False
        pressed, released = receipt.held - prior, prior - receipt.held
        if step.kind == 'outcome':
            actual = receipt.observations.get(step.predicate)
            matched = type(actual) is type(step.equals) and actual == step.equals
            self.status = 'Unverified' if step.predicate not in receipt.observations else 'Waiting for result'
        else:
            values = pressed if step.match == 'press' else released if step.match == 'release' else receipt.held
            matched = (step.inputs <= receipt.held and bool(pressed & step.inputs)) if step.match == 'press' else step.inputs <= values
            if step.exact:
                matched = matched and (values == step.inputs if step.match == 'release' else receipt.held == step.inputs)
            if step.match == 'hold':
                self._hold = self._hold + 1 if matched else 0
                matched = self._hold >= step.duration
            unexpected = pressed - step.inputs
            if self.profile.strict and unexpected:
                self._failed = True
                self.status = 'Unexpected input; rewind or restart this guide'
                return False
            self.status = 'Waiting for input'
        if matched and step.minimum <= elapsed <= step.maximum:
            self.hits.append(receipt.boundary); self._hold = 0
            self.status = 'Sequence finished' if len(self.hits) == len(self.profile.steps) else 'Step completed'
            return True
        return False

    def restore(self, *, boundary: int, revision: int, validated_full_restore: bool):
        """Caller must validate the native restore receipt before truncation."""
        if not validated_full_restore:
            self.suspend('Restore is unverified; progress retained')
            return
        if boundary < self.start_boundary or boundary > self.boundary:
            self.suspend('Restored outside this guide; start again')
            return
        retained = [event for event in self.events if event.boundary <= boundary]
        self.start(session=self.session,take=self.take,boundary=self.start_boundary,precision=self.profile.precision,
                   held=self.baseline,revision=self.start_revision)
        for event in retained:
            self.consume(event)
        self.revision = revision
        self.status = 'Rewound' if len(self.hits) < len(self.profile.steps) else 'Sequence finished'
