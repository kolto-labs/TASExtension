"""Structured piano-roll view and independent durable future input drafts."""
from dataclasses import asdict, replace
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from kptas.score import Note


class Timeline:
    def __init__(self, draft_path=None):
        self.rows = ()
        self.tick = 0
        self.first = self.last = 0
        self.drafts = {}
        self.identity = None
        self.foreign = False
        self.blocked_from = None
        # Recorded future after a live rewind (timeline_future.RecordedFuture):
        # later frames keep their recorded inputs until the user changes one.
        self.future = None
        # Read-only: inputs are locked; Step and Play only replay recorded input.
        self.read_only = False
        self._undo = []
        self._redo = []
        self._history_identity = None
        self._history_drafts = {}
        self._ack_identity = None
        self.draft_path = Path(draft_path) if draft_path else None
        self._draft_store = None
        self._draft_revision = None
        if self.draft_path and self.draft_path.exists():
            from .draft_store import draft_revision
            data = self.draft_path.read_bytes()
            document = json.loads(data)
            self._draft_revision = draft_revision(data)
            if document.get('schema') != 'kptas-input-draft/2':
                raise ValueError('unsupported input draft')
            self.identity = tuple(document['identity'])
            for tick, values in document['notes'].items():
                values = dict(values)
                for key in ('held', 'buttons', 'mouse_abs', 'mouse_rel'):
                    if values.get(key) is not None:
                        values[key] = tuple(values[key])
                self.drafts[int(tick)] = Note(**values)

    @staticmethod
    def snapshot_identity(snapshot):
        return (snapshot.take_path, snapshot.score_sha256)

    def clear_edit_history(self):
        """Retire process-local history without altering published drafts."""
        self._undo.clear()
        self._redo.clear()
        self._history_identity = self.identity
        self._history_drafts = dict(self.drafts)
        self.history_changed()

    def history_changed(self):
        """Hook for the history list; replaced by the window."""

    # Each transaction is (label, ((tick, before, after), ...)). before/after
    # are the effective inputs (Timeline.note) at that frame, so a transaction
    # still applies after its edits were executed or carried to a new take:
    # undo writes the earlier input back as a draft. It never restores, seeks
    # or executes the game.
    def _effective_matches(self, changes, *, undo):
        if self.foreign or self.identity is None:
            return False
        from .timeline_future import input_key
        try:
            return all(input_key(self.note(tick)) == input_key(after if undo else before)
                       for tick, before, after in changes)
        except ValueError:
            return False

    def _can_history(self, stack, *, undo):
        return bool(stack) and self._effective_matches(stack[-1][1], undo=undo)

    @property
    def can_undo(self):
        return self._can_history(self._undo, undo=True)

    @property
    def can_redo(self):
        return self._can_history(self._redo, undo=False)

    def _write_effective(self, values):
        """Set effective inputs {tick: Note}; a value equal to the recorded input drops its draft."""
        from .timeline_future import input_key
        previous = self.drafts
        drafts = dict(previous)
        for tick, note in values.items():
            base = self.base_note(tick)
            if input_key(note) == input_key(base):
                drafts.pop(tick, None)
            else:
                drafts[tick] = replace(note, count=1)
        self.drafts = drafts
        try:
            self.save()
        except Exception:
            self.drafts = previous
            raise

    def _restore_edit(self, *, undo):
        source, destination = (self._undo, self._redo) if undo else (self._redo, self._undo)
        if self.read_only:
            raise ValueError('read-only is on; turn it off to change inputs')
        if not self._can_history(source, undo=undo):
            return None
        transaction = source[-1]
        _, changes = transaction
        self._write_effective({tick: before if undo else after for tick, before, after in changes})
        source.pop()
        destination.append(transaction)
        self._history_drafts = dict(self.drafts)
        self.last_restored = tuple(tick for tick, _, _ in changes)   # for the undo flash
        self.history_changed()
        return min(tick for tick, _, _ in changes)

    def undo_edit(self):
        """Publish the prior input as a draft; never restore or execute the game."""
        return self._restore_edit(undo=True)

    def redo_edit(self):
        return self._restore_edit(undo=False)

    def history(self):
        """(undo labels oldest first, redo labels next first) for the history list."""
        return ([label for label, _ in self._undo], [label for label, _ in reversed(self._redo)])

    def apply_inputs(self, values, label):
        """One undoable transaction setting effective inputs {tick: Note}.

        Returns the number of frames that changed. Every tick is checked
        against barriers and foreign drafts before anything is written.
        """
        from .timeline_future import input_key
        if self.read_only:
            raise ValueError('read-only is on; turn it off to change inputs')
        if self.foreign or self.identity is None:
            raise ValueError('inputs belong to another recording')
        changes = []
        for tick in sorted(values):
            if type(tick) is not int or tick < 1:
                raise ValueError('input position must be positive')
            before = self.note(tick)
            after = replace(values[tick], count=1)
            if input_key(before) != input_key(after):
                changes.append((tick, before, after))
        if not changes:
            return 0
        self._write_effective({tick: after for tick, _, after in changes})
        self._undo.append((label, tuple(changes)))
        del self._undo[:-256]
        self._redo.clear()
        self._history_identity = self.identity
        self._history_drafts = dict(self.drafts)
        self.history_changed()
        return len(changes)

    def transfer(self, snapshot, expected_identity):
        if self.foreign or self.identity != expected_identity:
            raise ValueError('draft identity changed during execution')
        candidate = self.snapshot_identity(snapshot)
        self.save(identity=candidate)
        self.identity = candidate
        self._ack_identity = candidate
        self._history_identity = candidate
        # Input history follows its drafts to the new take: entries hold
        # effective inputs, so they stay valid after execution.

    def update(self, snapshot):
        identity = self.snapshot_identity(snapshot)
        lineage = (getattr(snapshot, 'mode', 'live'), (getattr(snapshot, 'owner_token', None) or {}).get('session_id'))
        if self._ack_identity != identity and (lineage != getattr(self, '_lineage', None)
                                               or lineage[0] != 'live' or lineage[1] is None):
            # A live owner session keeps one input history across its takes.
            self.clear_edit_history()
        self._lineage = lineage
        self._ack_identity = identity
        self.blocked_from = getattr(snapshot, 'blocked_from', None)
        if self.identity is None or not self.drafts:
            self.identity = identity
            self.foreign = False
        else:
            self.foreign = self.identity != identity
        if self._history_identity != self.identity and (self.foreign or lineage[0] != 'live'):
            self.clear_edit_history()
        self._history_identity = self.identity
        self.rows = snapshot.rows
        self.tick, self.first, self.last = snapshot.tick, snapshot.first, snapshot.last
        future = getattr(snapshot, 'future', None)
        self.future = future if future is not None and future.tick == snapshot.tick else None

    def note(self, tick):
        if self.blocked_from is not None and tick >= self.blocked_from:
            raise ValueError('timeline barrier requires score playback')
        if self.foreign:
            raise ValueError('draft belongs to another take or authoring revision')
        if tick in self.drafts:
            return self.drafts[tick]
        return self.base_note(tick)

    def base_note(self, tick):
        """The recorded input at a frame, without drafts (current take, then recorded future)."""
        if self.blocked_from is not None and tick >= self.blocked_from:
            raise ValueError('timeline barrier requires score playback')
        if 1 <= tick <= self.last:
            # Fast path for range edits over long takes; the loop below keeps
            # the barrier and mark refusals for frames the index cannot answer.
            cached = getattr(self, '_row_index', None)
            if cached is None or cached[0] is not self.rows or cached[1] != self.last:
                from .timeline_future import RowIndex
                cached = (self.rows, self.last, RowIndex(self.rows, self.last))
                self._row_index = cached
            found = cached[2].note(tick)
            if found is not None:
                return found
        for row in self.rows:
            if (isinstance(row.item, Note) and row.item.count == 0
                    and (row.item.wheel or row.item.osev) and row.start is not None
                    and tick == row.start + 1):
                raise ValueError('zero-time one-shot requires score playback')
            if row.start is not None and row.end is not None and row.start <= tick <= row.end:
                if isinstance(row.item, Note):
                    return replace(row.item, count=1,
                                   wheel=row.item.wheel if tick == row.start else 0,
                                   osev=row.item.osev if tick == row.start else None)
                raise ValueError('timeline barrier cannot be edited as input')
        if self.future is not None and tick > self.last:
            # A frame an earlier attempt recorded: replay keeps its input.
            recorded = self.future.note(tick)
            if recorded is not None:
                return recorded
        return Note(count=1)

    def edit(self, tick, label=None, **values):
        if type(tick) is not int or tick < 1:
            raise ValueError('input position must be positive')
        current = self.note(tick)
        candidate = replace(current, **values, count=1)
        if candidate == current:
            return candidate
        self.apply_inputs({tick: candidate}, label or f'Edit input at {tick}')
        return candidate

    def edited_score(self, snapshot, *, branch_tick=None):
        """Commit future drafts into full authoring without changing its prefix."""
        from kptas.score import compile_score, Sync, Relaunch
        from kptas.score.compile import CompiledScore
        boundary=snapshot.tick if branch_tick is None else branch_tick
        if (self.foreign or self.identity!=self.snapshot_identity(snapshot)
                or type(boundary) is not int or not 0<=boundary<=snapshot.tick):
            raise ValueError('branch needs the acknowledged draft identity and gate')
        if any(tick<=boundary for tick in self.drafts):
            raise ValueError('select a branch gate before the earliest draft edit')
        score=compile_score(snapshot.score_text)
        pending=dict(self.drafts);items=[];at=0
        for item in score.items:
            if not isinstance(item,Note):
                items.append(item)
                if isinstance(item,(Sync,Relaunch)):
                    if pending:raise ValueError('draft after a timeline barrier requires full score authoring')
                    at=None
                continue
            if at is None or item.count==0:
                items.append(item)
                continue
            end=at+item.count;cursor=at+1
            for tick in sorted(t for t in pending if at<t<=end):
                self.note(tick)  # Retain barrier and zero-time one-shot refusal.
                if cursor<tick:
                    items.append(replace(item,count=tick-cursor,
                        wheel=item.wheel if cursor==at+1 else 0,
                        osev=item.osev if cursor==at+1 else None))
                items.append(replace(pending.pop(tick),count=1));cursor=tick+1
            if cursor<=end:
                items.append(replace(item,count=end-cursor+1,
                    wheel=item.wheel if cursor==at+1 else 0,
                    osev=item.osev if cursor==at+1 else None))
            at=end
        for tick in sorted(pending):
            self.note(tick)
            if at is None:raise ValueError('draft after a timeline barrier requires full score authoring')
            if tick>at+1:items.append(Note(tick-at-1))
            items.append(replace(pending[tick],count=1));at=tick
        result=CompiledScore(dict(score.header),items,dict(score.aliases),list(score.files)).text()
        compile_score(result)  # Validate new one-shot adjacency and all directives.
        return result

    def commit_branch(self, snapshot, expected_identity, committed):
        """Retire only the exact drafts acknowledged in a durable child."""
        if self.foreign or self.identity!=expected_identity:
            raise ValueError('draft identity changed during branch acknowledgement')
        before=self.drafts
        self.drafts={tick:note for tick,note in before.items() if committed.get(tick)!=note}
        candidate=self.snapshot_identity(snapshot)
        try:self.save(identity=candidate)
        except Exception:
            self.drafts=before
            raise
        self.identity=candidate;self.foreign=False
        self._ack_identity=candidate
        self.clear_edit_history()

    def save(self, *, identity=None):
        if self.draft_path is None:
            return
        if self.identity is None:
            raise ValueError('draft needs acknowledged take identity')
        from .draft_store import InputDraftStore
        if self._draft_store is None:
            self._draft_store = InputDraftStore(self.draft_path,
                                                expected_revision=self._draft_revision)
        elif self._draft_store.path != self.draft_path:
            raise ValueError('draft path changed; reopen its document before saving')
        document = {'schema': 'kptas-input-draft/2',
                    'identity': list(identity if identity is not None else self.identity),
                    'notes': {str(tick): asdict(note)
                              for tick, note in sorted(self.drafts.items())}}
        self._draft_revision = self._draft_store.publish(
            document, source_identity=tuple(self.identity))



def note_action(note):
    """Lossless input action; unsupported OS events require the full score path."""
    if note.osev:
        raise ValueError('OS event requires score playback')
    return {'count': 1, 'held': list(note.held), 'buttons': list(note.buttons),
            'mouse_abs': note.mouse_abs, 'mouse_rel': note.mouse_rel, 'wheel': note.wheel}
