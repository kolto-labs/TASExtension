"""Success predicates and objectives over one tick's recorded journal. No eval().

Predicate: clauses joined by `&&`, all must hold.

    same                          state_root equals the original recording's at that frame
    PATH OP VALUE                 OP is == != < <= > >= or `has` (list membership)
    PATH ~ VALUE +- TOL           numeric or vector distance within TOL (also `±`)

PATH is dotted into the journal object, e.g. sem.pc.area, sem.lmod, sem.pc.pos, sem.pc.pos.x.
pos/ori hold little-endian float32 hex; they are decoded, and .x/.y/.z pick 0/1/2. `ev` reads as
the list of event names. VALUE is a number, a "string", a [list] of numbers, or `orig` (the
original recording's value at the same frame). A missing path makes the clause false.

Objective: PATH (numeric), or dist(PATH, [x, y, z]) / dist(PATH, orig).
"""
from __future__ import annotations

import json
import math
import re
import struct
from dataclasses import dataclass
from typing import Any

_MISSING = object()
_OPS = ('==', '!=', '<=', '>=', '<', '>', 'has')
_AXES = {'x': 0, 'y': 1, 'z': 2}


class PredicateError(ValueError):
    pass


def _decode(value):
    if isinstance(value, str) and len(value) == 8 and re.fullmatch(r'[0-9a-fA-F]{8}', value):
        number, = struct.unpack('<f', bytes.fromhex(value))
        return number
    if isinstance(value, list) and value and all(isinstance(v, str) and len(v) == 8 for v in value):
        try:
            return [_decode(v) for v in value]
        except ValueError:
            return value
    return value


def lookup(journal: Any, path: str):
    node = journal
    parts = path.split('.')
    if parts[0] == 'ev':
        events = journal.get('ev', []) if isinstance(journal, dict) else []
        return [e.get('name') for e in events if isinstance(e, dict)]
    for part in parts:
        node = _decode(node)
        if isinstance(node, dict) and part in node:
            node = node[part]
        elif isinstance(node, list) and (part.isdigit() or part in _AXES):
            index = int(part) if part.isdigit() else _AXES[part]
            if index >= len(node):
                return _MISSING
            node = node[index]
        else:
            return _MISSING
    return _decode(node)


def _value(text: str):
    text = text.strip()
    if text == 'orig':
        return 'orig'
    try:
        value = json.loads(text)
    except ValueError:
        raise PredicateError(f'cannot read value {text!r}; use a number, "text", [list] or orig') from None
    if isinstance(value, list) and not all(isinstance(v, (int, float)) for v in value):
        raise PredicateError(f'list value {text!r} must hold numbers')
    if not isinstance(value, (int, float, str, list)) or isinstance(value, bool):
        raise PredicateError(f'unsupported value {text!r}')
    return value


def _path(text: str) -> str:
    text = text.strip()
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)*', text):
        raise PredicateError(f'bad path {text!r}; use dotted names such as sem.pc.area')
    return text


@dataclass(frozen=True)
class Clause:
    kind: str           # same | cmp | near
    path: str = ''
    op: str = ''
    value: Any = None
    tol: float = 0.0


@dataclass(frozen=True)
class Predicate:
    text: str
    clauses: tuple[Clause, ...]

    @property
    def needs_root(self) -> bool:
        return any(c.kind == 'same' for c in self.clauses)

    @property
    def uses_orig(self) -> bool:
        return any(c.value == 'orig' for c in self.clauses if c.kind != 'same')


def parse_predicate(text: str) -> Predicate:
    if not isinstance(text, str) or not text.strip() or len(text) > 2000:
        raise PredicateError('a predicate is required (at most 2000 characters)')
    clauses = []
    for raw in text.split('&&'):
        raw = raw.strip()
        if raw == 'same':
            clauses.append(Clause('same'))
            continue
        near = re.fullmatch(r'(.+?)~(.+?)(?:\+-|±)(.+)', raw)
        if near:
            try:
                tol = float(near.group(3))
            except ValueError:
                raise PredicateError(f'bad tolerance in {raw!r}') from None
            if not (tol >= 0 and math.isfinite(tol)):
                raise PredicateError(f'tolerance must be a finite number >= 0 in {raw!r}')
            clauses.append(Clause('near', _path(near.group(1)), '~', _value(near.group(2)), tol))
            continue
        for op in _OPS:
            pattern = r'(.+?)\s+has\s+(.+)' if op == 'has' else r'(.+?)' + re.escape(op) + r'(.+)'
            m = re.fullmatch(pattern, raw)
            if m and (op == 'has' or not re.search(r'[<>=!]$', m.group(1))):
                clauses.append(Clause('cmp', _path(m.group(1)), op, _value(m.group(2))))
                break
        else:
            raise PredicateError(f'cannot read clause {raw!r}; expected same, PATH OP VALUE or PATH ~ VALUE +- TOL')
    return Predicate(text.strip(), tuple(clauses))


def _distance(a, b):
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(a - b)
    if isinstance(a, list) and isinstance(b, list) and len(a) == len(b) and all(
            isinstance(v, (int, float)) for v in (*a, *b)):
        return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))
    return None


def evaluate(pred: Predicate, journal: dict, orig: dict | None, *, root=None, orig_root=None):
    """(holds, notes). Missing data makes a clause false and adds a note."""
    notes = []
    for c in pred.clauses:
        if c.kind == 'same':
            if root is None or orig_root is None:
                notes.append('same: no captured state root to compare')
                return False, notes
            if root != orig_root:
                return False, notes
            continue
        have = lookup(journal, c.path)
        want = c.value
        if want == 'orig':
            want = lookup(orig or {}, c.path)
            if want is _MISSING:
                notes.append(f'{c.path}: missing in the original')
                return False, notes
        if have is _MISSING:
            notes.append(f'{c.path}: missing')
            return False, notes
        if c.kind == 'near':
            d = _distance(have, want)
            if d is None:
                notes.append(f'{c.path}: not comparable with ~')
                return False, notes
            if d > c.tol:
                return False, notes
            continue
        try:
            if c.op == 'has':
                ok = isinstance(have, list) and want in have
            elif c.op == '==':
                ok = have == want
            elif c.op == '!=':
                ok = have != want
            elif c.op == '<':
                ok = have < want
            elif c.op == '<=':
                ok = have <= want
            elif c.op == '>':
                ok = have > want
            else:
                ok = have >= want
        except TypeError:
            notes.append(f'{c.path}: {c.op} does not apply to {type(have).__name__}')
            return False, notes
        if not ok:
            return False, notes
    return True, notes


@dataclass(frozen=True)
class Objective:
    text: str
    path: str
    target: Any = None      # None: the value itself; else distance to this ([list], number or 'orig')


def parse_objective(text: str) -> Objective:
    if not isinstance(text, str) or not text.strip():
        raise PredicateError('an objective is required')
    raw = text.strip()
    m = re.fullmatch(r'dist\((.+?),(.+)\)', raw)
    if m:
        return Objective(raw, _path(m.group(1)), _value(m.group(2)))
    return Objective(raw, _path(raw))


def score(obj: Objective, journal: dict, orig: dict | None):
    """A float, or None when the value is missing or not numeric."""
    have = lookup(journal, obj.path)
    if have is _MISSING:
        return None
    if obj.target is None:
        return float(have) if isinstance(have, (int, float)) and not isinstance(have, bool) else None
    want = lookup(orig or {}, obj.path) if obj.target == 'orig' else obj.target
    if want is _MISSING:
        return None
    return _distance(have, want)
