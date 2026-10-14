"""The authored inputs after an anchor frame, as notes and as per-frame token sets.

Frame numbering follows the workspace and trainer: frame N is the input applied from tick N-1
to tick N. Edits move whole held tokens (keys, chords, mouse buttons) between frames; every
other input keeps its absolute frame. One-shots (wheel, osev) fire only on the first frame of
the note that carried them.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field

from kptas.score import Mark, Note
from kptas.score.keys import BUTTONS
from kptas.supervisor import TelosError

ZERO_TIME = (Mark,)


def placement(session, end: int):
    """tick -> Cursor for the session's current take, exactly as rewind places it.

    The one place this package uses the owner's placement helper (seek internals change
    under other work; keep the dependency here)."""
    planner = session._planner(session.sink.bundle, score=session.text())
    return planner.place(max(end, planner.b.last_tick))


def tokens(note: Note) -> frozenset[str]:
    return frozenset(note.held) | frozenset(note.buttons)


@dataclass
class Seg:
    note: Note          # as executed after the anchor (a continuation drops one-shots)
    first: int          # first frame
    last: int           # last frame


@dataclass
class Tail:
    anchor: int
    end: int
    items: list         # Notes and Marks as authored after the anchor (the return trip replays these)
    segs: list[Seg]
    frames: dict[int, tuple[int, bool]] = field(default_factory=dict)   # frame -> (seg index, first frame of seg)
    hz: int = 60
    continuation: bool = False

    def toks(self, frame: int) -> frozenset[str]:
        return tokens(self.segs[self.frames[frame][0]].note)


def build_tail(items, at, anchor: int, end: int) -> Tail:
    cur = at.get(anchor)
    if cur is None:
        raise TelosError(f'frame {anchor} is inside a wait; it cannot anchor trials')
    i, j = cur.i, cur.j
    out, segs, frame = [], [], anchor
    continuation = False
    while frame < end:
        if i >= len(items):
            raise TelosError(f'the authored score ends at frame {frame}, before frame {end}')
        it = items[i]
        if isinstance(it, ZERO_TIME):
            if j:
                raise TelosError('unexpected cursor inside a mark')
            out.append(it)
            i += 1
            continue
        if not isinstance(it, Note) or it.count < 1:
            raise TelosError(f'frames {anchor + 1}..{end} contain `{type(it).__name__}` '
                             f'(line {getattr(it, "line", "?")}); analysis needs plain inputs and marks only')
        n = it
        if j:
            n = dataclasses.replace(it, count=it.count - j, wheel=0, osev=None)
            continuation = True
            j = 0
        if frame + n.count > end:
            raise TelosError(f'an input runs past frame {end}; the take must end on an input boundary')
        segs.append(Seg(n, frame + 1, frame + n.count))
        out.append(n)
        frame += n.count
        i += 1
    while i < len(items) and isinstance(items[i], ZERO_TIME):
        out.append(items[i])
        i += 1
    tail = Tail(anchor, end, out, segs, hz=getattr(cur, 'hz', 60) or 60, continuation=continuation)
    for k, s in enumerate(segs):
        for t in range(s.first, s.last + 1):
            tail.frames[t] = (k, t == s.first)
    return tail


def compress(tail: Tail, add: dict[int, frozenset] | None = None, remove: dict[int, frozenset] | None = None,
             upto: int | None = None) -> list[Note]:
    """Notes for frames anchor+1..upto with tokens added/removed per frame."""
    add, remove = add or {}, remove or {}
    upto = tail.end if upto is None else upto
    notes, group = [], None     # group = [seg index, token set, start frame, count]

    def close():
        if group is None:
            return
        k, toks, start, count = group
        base = tail.segs[k].note
        n = dataclasses.replace(base, count=count)
        if toks != tokens(base):
            held = [x for x in base.held if x in toks] + sorted(x for x in toks - set(base.held) if x not in BUTTONS)
            buttons = [x for x in base.buttons if x in toks] + sorted(x for x in toks - set(base.buttons) if x in BUTTONS)
            n = dataclasses.replace(n, held=tuple(held), buttons=tuple(buttons))
        if start != tail.segs[k].first:
            n = dataclasses.replace(n, wheel=0, osev=None)
        notes.append(n)

    for t in range(tail.anchor + 1, upto + 1):
        k, first = tail.frames[t]
        toks = (tail.toks(t) - remove.get(t, frozenset())) | add.get(t, frozenset())
        if group is not None and not first and group[0] == k and group[1] == toks:
            group[3] += 1
            continue
        close()
        group = [k, toks, t, 1]
    close()
    return notes


def cut(tail: Tail, upto: int) -> list[Note]:
    """The authored notes (verbatim) for frames anchor+1..upto; the last one is shortened."""
    out, frame = [], tail.anchor
    for s in tail.segs:
        if frame >= upto:
            break
        n = s.note if frame + s.note.count <= upto else dataclasses.replace(s.note, count=upto - frame)
        out.append(n)
        frame += n.count
    return out


def press_at(tail: Tail, frame: int, keys: frozenset | None = None):
    """(tokens, first frame, last frame) of the press held at `frame`.

    The press is the tokens that went down at the latest frame <= `frame` where any of the
    frame's tokens went down; `keys` narrows it. It lasts while all its tokens stay held."""
    if frame not in tail.frames:
        raise TelosError(f'frame {frame} is outside the analysed frames {tail.anchor + 1}..{tail.end}')
    held = tail.toks(frame)
    if keys:
        missing = set(keys) - held
        if missing:
            raise TelosError(f'frame {frame} does not hold {", ".join(sorted(missing))}')
        held = frozenset(keys)
    t = frame
    while True:
        prev = tail.toks(t - 1) if (t - 1) in tail.frames else None
        if prev is None:
            raise TelosError(f'the press at frame {frame} starts at or before the anchor; analyse from an earlier frame')
        down = (held - prev)
        if down:
            break
        t -= 1
    press = frozenset(down) if not keys else frozenset(keys)
    last = t
    while (last + 1) in tail.frames and press <= tail.toks(last + 1):
        last += 1
    return press, t, last


def note_inputs_at(tail: Tail, frame: int) -> frozenset[str]:
    from kptas.score.keys import token_keys
    return frozenset(k for tok in tail.toks(frame) for k in token_keys(tok))
