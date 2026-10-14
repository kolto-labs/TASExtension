"""Recorded future of a live take: the frames an earlier attempt reached.

A live rewind never truncates work. The owner seals the current take and
starts a branch at the target frame (`session.rewind`), so the frames after it
stay on disk in the sealed parent take. This module rebuilds that future for
the GUI from the owner's own take list: each ancestor's immutable authored
score, its `branch_tick` and its `last` frame.

The result is display and replay data only. It never decides a restore. A
forward seek replays these recorded inputs through ordinary owner steps, so
the owner still verifies every frame. Nothing here adds or changes an input:

* frames up to the current one come from the current take;
* later frames come from the parent take's document: its own frames, then
  its parent's beyond them when it changed no input (the recorded future,
  like a TAStudio input log after a rewind). Sealed-take documents are
  memoized, so long chains of one-frame rewinds stay cheap;
* the first frame where this branch recorded a different input is the
  invalidation point. Frames after it in the earlier attempt stay in that
  take on disk but are no longer reachable by replay from here.

Pure logic: no Qt imports. Safe to call on the controller worker thread.
"""
from __future__ import annotations

import bisect
import dataclasses
import hashlib
from typing import Any

from kptas.score import compile_score
from kptas.score.ast import Mark, Note, Segment


def input_key(note):
    """Input identity of one frame; source/line positions do not count."""
    if note is None:
        return None
    return (tuple(note.held), tuple(note.buttons), note.mouse_abs, note.mouse_rel,
            note.wheel, note.osev)


def score_rows(text):
    """(start, end, item) rows with the same positions as WorkspaceSnapshot.rows."""
    from kptas.score.ast import Relaunch, Sync
    rows, at = [], 0
    for item in compile_score(text).items:
        end = at + item.count if isinstance(item, Note) and at is not None else at
        start = at + 1 if isinstance(item, Note) and item.count and at is not None else at
        rows.append((start, end, item))
        at = None if isinstance(item, (Sync, Relaunch)) else end
    return tuple(rows)


class RowIndex:
    """Frame lookup over authored rows, matching Timeline.note without drafts.

    `limit` is the last frame this index may answer for. Frames after a
    barrier, a zero-time one-shot or an OS event are excluded: an ordinary
    step cannot replay them.
    """

    def __init__(self, rows, limit):
        starts, ends, items, os_events = [], [], [], []
        marks = {}
        blocked = None
        for row in rows:
            start, end, item = (row.start, row.end, row.item) if hasattr(row, 'item') else row
            if start is None:
                continue
            if isinstance(item, Mark) and start <= limit:
                marks.setdefault(item.name, start)
            if ((isinstance(item, Note) and item.count == 0 and (item.wheel or item.osev))
                    or not isinstance(item, (Note, Mark, Segment))):
                blocked = start + 1 if blocked is None else min(blocked, start + 1)
                continue
            if not isinstance(item, Note) or not item.count or end is None:
                continue
            if item.osev:
                # note_action refuses an OS event: replay stops before it.
                os_events.append(start)
            starts.append(start)
            ends.append(end)
            items.append(item)
        self.starts, self.ends, self.items = starts, ends, items
        self.os_events = os_events
        self.marks = marks          # score @mark name -> frame
        self.limit = limit if blocked is None else min(limit, blocked - 1)

    def replay_end(self, after):
        """Last frame after `after` an ordinary step can replay (before an OS event)."""
        i = bisect.bisect_right(self.os_events, after)
        return self.limit if i == len(self.os_events) else min(self.limit, self.os_events[i] - 1)

    def _row(self, tick):
        if tick < 1 or tick > self.limit:
            return None
        i = bisect.bisect_left(self.ends, tick)
        if i < len(self.ends) and self.starts[i] <= tick:
            return i
        return None

    def note(self, tick):
        i = self._row(tick)
        if i is None:
            return None
        item, start = self.items[i], self.starts[i]
        return dataclasses.replace(item, count=1, wheel=item.wheel if tick == start else 0,
                                   osev=item.osev if tick == start else None)

    def runs(self, low, high):
        """Pieces (lo, hi, item, row_start) covering frames low+1..high.

        Stops at the first uncovered frame; the caller treats that frame as
        different from any input.
        """
        pieces = []
        tick = low + 1
        while tick <= high:
            i = self._row(tick)
            if i is None:
                break
            hi = min(high, self.ends[i], self.limit)
            pieces.append((tick, hi, self.items[i], self.starts[i]))
            tick = hi + 1
        return pieces, tick


def _first_difference(left, right, low, high):
    """First frame in low+1..high where two run lists differ, else None."""
    a, a_end = left
    b, b_end = right
    covered = min(a_end, b_end)   # first frame either side cannot answer
    i = j = 0
    tick = low + 1
    while tick <= high and tick < covered:
        while a[i][1] < tick:
            i += 1
        while b[j][1] < tick:
            j += 1
        lo_a, hi_a, item_a, start_a = a[i]
        lo_b, hi_b, item_b, start_b = b[j]
        hi = min(hi_a, hi_b, high, covered - 1)
        if (tuple(item_a.held), tuple(item_a.buttons), item_a.mouse_abs, item_a.mouse_rel) != (
                tuple(item_b.held), tuple(item_b.buttons), item_b.mouse_abs, item_b.mouse_rel):
            return tick
        first_a = (item_a.wheel, item_a.osev) if tick == start_a else (0, None)
        first_b = (item_b.wheel, item_b.osev) if tick == start_b else (0, None)
        if first_a != first_b:
            return tick
        tick = hi + 1
    return tick if tick <= high else None


class _Document:
    """Inputs a take leaves for its children: its own frames, then its parent's
    document beyond them when the take did not change any parent input.

    `segments` are (low, high, RowIndex, take_path) covering frames low+1..high,
    contiguous from frame 1 to `end`. Sealed takes never change, so a document
    is computed once per take and reused by every later snapshot.
    """
    __slots__ = ('segments', 'end', 'rerecords', 'changed_at', 'verified_end')

    def __init__(self, segments, rerecords=0, changed_at=None, verified_end=None):
        self.segments = tuple(segments)
        self.end = self.segments[-1][1] if self.segments else 0
        self.rerecords = rerecords      # restores followed by a changed input, up to this take
        self.changed_at = changed_at    # first frame this take changed from its parent
        # Frames after verified_end are kept inputs that no take has run on
        # top of this take's inputs yet (TAStudio: invalidated greenzone).
        self.verified_end = self.end if verified_end is None else verified_end

    def beyond(self, tick):
        return tuple((max(low, tick), high, index, path)
                     for low, high, index, path in self.segments if high > tick)

    def note(self, tick):
        for low, high, index, _ in self.segments:
            if low < tick <= high:
                return index.note(tick)
        return None

    def runs(self, low, high):
        pieces, tick = [], low + 1
        for seg_low, seg_high, index, _ in self.segments:
            if tick > high:
                break
            if seg_high < tick:
                continue
            chunk, stop = index.runs(tick - 1, min(high, seg_high))
            pieces.extend(chunk)
            tick = stop
            if stop <= min(high, seg_high):
                break    # a gap: the caller treats this frame as changed
        return pieces, tick

    def replay_end(self, after):
        """Last frame after `after` an ordinary step can replay (before an OS event)."""
        for low, high, index, _ in self.segments:
            if high <= after:
                continue
            end = index.replay_end(max(after, low))
            if end < high:
                return end
        return self.end


@dataclasses.dataclass(frozen=True, eq=False)
class RecordedFuture:
    """Where this branch can go without new input, and what it replaced."""
    tick: int                     # acknowledged current frame
    valid_end: int                # last frame whose result is known (greenzone)
    furthest: int                 # furthest frame recorded on this lineage
    invalid_from: int | None      # first frame whose input changed (or valid_end + 1)
    stale_take: str | None        # take that still holds the earlier attempt
    source_take: str | None       # take that supplies the kept inputs
    segments: tuple = ()          # (low, high, RowIndex, take_path): frames low+1..high
    stale_index: Any = None       # the earlier attempt's inputs (has .note(tick))
    inputs_end: int | None = None  # last future frame an ordinary step can replay

    @property
    def replay_end(self):
        return self.valid_end if self.inputs_end is None else self.inputs_end

    @property
    def replayable(self):
        return self.replay_end > self.tick

    def note(self, tick):
        """Kept input of a future frame, or None outside the replayable span."""
        if not self.tick < tick <= self.replay_end:
            return None
        for low, high, index, _ in self.segments:
            if low < tick <= high:
                return index.note(tick)
        return None

    def take_for(self, tick):
        for low, high, _, path in self.segments:
            if low < tick <= high:
                return path
        return None


class _Cache:
    """Bounded LRU memo for sealed-take documents and final results."""
    def __init__(self):
        from collections import OrderedDict
        self.documents = OrderedDict()
        self.results = OrderedDict()

    @staticmethod
    def get(table, key):
        if key in table:
            table.move_to_end(key)
            return True, table[key]
        return False, None

    @staticmethod
    def put(table, key, value, limit):
        table[key] = value
        table.move_to_end(key)
        while len(table) > limit:
            table.popitem(last=False)


def _limit(take):
    last, durable = take.get('last'), take.get('durable_tick')
    if type(last) is not int or type(durable) is not int:
        return None
    return min(last, durable)   # only frames the owner made durable


def _take_key(take):
    return (take.get('path'), take.get('score_sha256'), take.get('last'), take.get('durable_tick'),
            take.get('branch_tick'), take.get('parent'))


def _load_index(controller, take, limit):
    digest, path = take.get('score_sha256'), take.get('path')
    if not isinstance(path, str) or not isinstance(digest, str):
        return None
    loader = getattr(controller, '_score_loader', None)
    if loader is not None:
        text = loader(path)
        if hashlib.sha256(text.encode('utf-8')).hexdigest() != digest:
            return None
    else:
        # The reader's durable check guards the current take's boundary; an
        # ancestor's own frames are capped at its durable frame instead.
        text = controller._load_score({**take, 'durable_tick': take.get('last')})
    return RowIndex(score_rows(text), limit)


def _document(controller, cache, chain):
    """Document of chain[0] (a sealed take); chain continues with its ancestors."""
    # Walk up to the first memoized ancestor (or the root), then build down.
    pending, parent_doc = [], None
    for take in chain:
        found, doc = cache.get(cache.documents, _take_key(take))
        if found:
            parent_doc = doc
            break
        pending.append(take)
    for i in range(len(pending) - 1, -1, -1):
        take = pending[i]
        limit = _limit(take)
        doc = None
        if limit is not None:
            index = _load_index(controller, take, limit)
            if index is not None:
                segments = [(0, index.limit, index, take.get('path'))]
                branch_tick = take.get('branch_tick')
                changed = None
                if parent_doc is not None and type(branch_tick) is int and branch_tick < limit:
                    top = min(limit, parent_doc.end)
                    changed = (_first_difference(index.runs(branch_tick, top), parent_doc.runs(branch_tick, top),
                                                 branch_tick, top) if branch_tick < top else None)
                    if changed is None and limit > parent_doc.end:
                        changed = parent_doc.end + 1   # recorded past the parent: new input
                verified = index.limit
                if (parent_doc is not None and changed is None and index.limit == limit
                        and parent_doc.end > limit):
                    # Nothing changed: the parent's later frames stay this take's
                    # future. After a change they stay only in the parent.
                    segments.extend(parent_doc.beyond(limit))
                    verified = max(limit, parent_doc.verified_end)
                base = parent_doc.rerecords if parent_doc is not None else 0
                # libTAS rule: a restore counts once input changed after it.
                doc = _Document(segments, base + (1 if changed is not None and parent_doc is not None else 0),
                                changed, verified)
        cache.put(cache.documents, _take_key(take), doc, 2048)
        parent_doc = doc
    return parent_doc


@dataclasses.dataclass(frozen=True, eq=False)
class TakeInfo:
    """One take of this owner session, for the branch tree."""
    path: str
    parent: str | None
    branch_tick: int | None
    last: int | None
    changed_at: int | None      # first frame whose input differs from its parent
    marks: dict                 # score @mark name -> frame
    current: bool
    rerecords: int


@dataclasses.dataclass(frozen=True, eq=False)
class Lineage:
    future: RecordedFuture | None
    rerecords: int              # restores followed by a changed input (libTAS rule)
    takes: tuple                # TakeInfo, oldest first, current last


def _ancestors(by_path, take):
    chain, seen, child = [], {take.get('path')}, take
    while True:
        parent = by_path.get(child.get('parent'))
        if parent is None or parent.get('path') in seen:
            return chain
        seen.add(parent.get('path'))
        chain.append(parent)
        child = parent


def _changed(index, doc, branch_tick, last):
    """First frame where a take's inputs leave its parent's document, else None."""
    if doc is None or type(branch_tick) is not int or branch_tick >= last:
        return None
    top = min(last, doc.end)
    changed = (_first_difference(index.runs(branch_tick, top), doc.runs(branch_tick, top), branch_tick, top)
               if branch_tick < top else None)
    if changed is None and last > doc.end:
        changed = doc.end + 1
    return changed


MAX_TREE_TAKES = 300


def lineage(controller, takes, take, rows, tick):
    """Recorded future, rerecord count and branch tree of a live owner state."""
    if not isinstance(takes, list) or not isinstance(take, dict) or type(tick) is not int:
        return None
    cache = getattr(controller, '_future_cache', None)
    if not isinstance(cache, _Cache):
        cache = _Cache()
        controller._future_cache = cache
    older = [t for t in takes[:-1] if isinstance(t, dict)]
    key = (take.get('path'), take.get('score_sha256'), tick, len(older),
           _take_key(older[-1]) if older else None)
    found, result = cache.get(cache.results, key)
    if found:
        return result
    by_path = {t.get('path'): t for t in older}
    chain = _ancestors(by_path, take)
    branch_tick = take.get('branch_tick')
    parent_doc = _document(controller, cache, chain) if chain else None
    current = RowIndex(rows, tick)
    future = None
    if parent_doc is not None and parent_doc.end > tick and type(branch_tick) is int:
        changed = (_first_difference(current.runs(branch_tick, tick), parent_doc.runs(branch_tick, tick),
                                     branch_tick, tick) if branch_tick < tick else None)
        inputs_end = max(tick, parent_doc.replay_end(tick))
        segments = tuple((low, min(high, inputs_end), index, path)
                         for low, high, index, path in parent_doc.beyond(tick) if low < inputs_end)
        source = segments[0][3] if segments else None
        if changed is None:
            valid_end = max(tick, min(inputs_end, parent_doc.verified_end))
            invalid = valid_end + 1 if valid_end < parent_doc.end else None
            future = RecordedFuture(tick, valid_end, parent_doc.end, invalid, None, source, segments,
                                    None, inputs_end)
        else:
            # Read-only tail ends at the change: later frames of the earlier
            # attempt are shown (striped) and kept on disk, never stepped.
            future = RecordedFuture(tick, tick, parent_doc.end, changed, chain[0].get('path'), None,
                                    (), parent_doc, tick)
    current_changed = _changed(current, parent_doc, branch_tick, tick)
    rerecords = ((parent_doc.rerecords if parent_doc is not None else 0)
                 + (1 if current_changed is not None else 0))
    tree = []
    for other in older[-MAX_TREE_TAKES:]:
        hit, doc = cache.get(cache.documents, _take_key(other))
        if not hit:
            try:
                doc = _document(controller, cache, [other] + _ancestors(by_path, other))
            except Exception:
                doc = None
        own = doc.segments[0][2] if doc is not None and doc.segments else None
        tree.append(TakeInfo(other.get('path'), other.get('parent'), other.get('branch_tick'), other.get('last'),
                             doc.changed_at if doc is not None else None,
                             dict(own.marks) if own is not None else {}, False,
                             doc.rerecords if doc is not None else 0))
    tree.append(TakeInfo(take.get('path'), take.get('parent'), branch_tick, tick, current_changed,
                         dict(current.marks), True, rerecords))
    result = Lineage(future, rerecords, tuple(tree))
    cache.put(cache.results, key, result, 64)
    return result


def recorded_future(controller, takes, take, rows, tick):
    """The recorded future of a live owner state, or None when there is none."""
    found = lineage(controller, takes, take, rows, tick)
    return found.future if found is not None else None
