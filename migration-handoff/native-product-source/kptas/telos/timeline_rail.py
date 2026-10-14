"""Seek rail support for the recorded future of a live take.

After a rewind, the rail keeps the whole recorded range of the branch: the
current frame, the furthest frame reached and the point where inputs changed.
Dragging forward replays recorded inputs through ordinary owner steps (the
workspace's Play path, stopped at the target). It never adds an input: the
step input for each frame is the one Timeline.note already resolves.

Installed by WorkspaceWindow; the window keeps every owner admission rule.
"""
from PySide6 import QtCore as C, QtGui as G

from .timeline_future import input_key

_REPLAY = G.QColor(25, 227, 193, 120)      # saved future, reachable
_STALE = G.QColor(229, 193, 91, 150)       # earlier attempt, not reachable
_CHANGED = G.QColor('#FF6B6B')             # invalidation point
_FURTHEST = G.QColor('#D7E3EA')            # furthest recorded frame
_BOOKMARK = G.QColor('#E5C15B')            # user bookmark
_GREEN = G.QColor('#65D6AD')               # return point (green arrow)


class FutureRail:
    """Rail extent, marks and forward replay for one workspace window."""

    def __init__(self, window):
        self.w = window
        self.target = None          # frame a running forward replay stops at
        self.bookmarks = None       # callable -> bookmark frames, set by TimelineTools
        self.green = None           # frame to return to after a jump back (green arrow)
        self.started_from = None    # frame a running forward replay started at
        overlay = window.overlay
        self._base_extent = type(overlay).seek_extent
        # Every rail range, enable check and seek bound reads this extent.
        overlay.seek_extent = self.extent
        window.seek.timeline_marks = self.paint
        window.seek.cursor_hint = self.cursor_hint

    # -- model -------------------------------------------------------------
    def future(self, snapshot=None):
        s = self.w.snapshot if snapshot is None else snapshot
        f = getattr(s, 'future', None)
        if (f is None or getattr(s, 'mode', 'live') != 'live' or f.tick != s.tick
                or self.w.timeline.foreign):
            return None
        return f

    def extent(self, snapshot):
        base = self._base_extent(snapshot)
        f = self.future(snapshot)
        return max(base, f.furthest) if f is not None else base

    def pending_edit(self, f=None):
        """First frame whose draft differs from the recorded input there.

        Past frames count too: an undo after execution or a range edit can
        leave a pending edit at or before the current frame.
        """
        timeline = self.w.timeline
        if timeline.foreign or not timeline.drafts:
            return None
        # Painted many times per second: recompute only when drafts, rows or
        # the recorded future change (drafts are replaced, not mutated, on edit).
        key = (id(timeline.drafts), len(timeline.drafts), id(timeline.rows), id(timeline.future),
               timeline.blocked_from)
        if getattr(self, '_pending_key', None) == key:
            return self._pending_value
        self._pending_key, self._pending_value = key, self._first_pending(timeline)
        return self._pending_value

    @staticmethod
    def _first_pending(timeline):
        for tick in sorted(timeline.drafts):
            try:
                base = timeline.base_note(tick)
            except ValueError:
                continue
            if input_key(timeline.drafts[tick]) != input_key(base):
                return tick
        return None

    def reach(self, f=None):
        """Last frame a forward seek may replay to, and why it stops there."""
        f = f or self.future()
        if f is None:
            return None, ''
        unit = self.w._unit()
        end, why = f.replay_end, ''
        if end < f.furthest:
            why = ('inputs changed at {} {}; use "Bring back the earlier inputs" to keep them'.format(unit, f.invalid_from)
                   if f.invalid_from is not None and f.stale_index is not None
                   else 'the next {} needs score playback'.format(unit))
        edit = self.pending_edit(f)
        if edit is not None and f.tick < edit <= end:
            end, why = edit - 1, 'you edited the input at {} {}; step or play to run it'.format(unit, edit)
        return end, why

    def striped_from(self, f=None):
        """First future frame the replay cannot reach (striped), else None."""
        f = f or self.future()
        if f is None:
            return None
        end, _ = self.reach(f)
        return end + 1 if end < f.furthest else None

    # -- forward seek ------------------------------------------------------
    def handles(self, value):
        """True for seeks this rail owns: forward in a live take, or back during a replay."""
        s = self.w.snapshot
        if type(value) is not int or s is None or getattr(s, 'mode', 'live') != 'live':
            return False
        return value > s.tick or self.target is not None

    def seek(self, value):
        if value > self.w.snapshot.tick:
            return self.request(value)
        # Back during a forward replay: stop it, then seek once the running
        # batch is acknowledged (or now, when nothing is in flight).
        w = self.w
        self.target = None
        w.playing = False
        w.play.setText('▶ Play')
        w.playback_timer.stop()
        if w.pending and w.operation != 'refresh':
            w.deferred_command = ('seek', value, None, True, False)
            w.present()
            return True
        return w.submit('seek', value)

    def admitted(self, tick):
        """A rail seek this rail accepted: a running replay toward `tick`."""
        return self.target is not None and self.w.snapshot is not None and tick > self.w.snapshot.tick

    def request(self, target):
        """Replay recorded inputs up to `target`. True when admitted."""
        w, unit = self.w, self.w._unit()
        s = w.snapshot
        f = self.future()
        end, why = self.reach(f)
        if f is None or end is None or end <= s.tick:
            reason = why or 'nothing is recorded after {} {}'.format(unit, s.tick)
            w.error.setText('Seek refused: ' + reason)
            return False
        if (w.mutation_pending() and not w.playing) or w.owner_error or w.draft_failure:
            w.error.setText('Seek refused: wait for the current command to finish')
            return False
        if getattr(s, 'engaged', None) is False:
            w.error.setText('Seek refused: engage TAS first')
            return False
        if target > end:
            w.show_notice('Seek stopped early', 'Replay stops at {} {}: {}.'.format(unit, end, why))
            target = end
        self.target = target
        self.started_from = s.tick
        if w.playing:
            return True     # the running Play loop stops at the target
        w.playing = True
        w.play.setText('Ⅱ Pause')
        if not w.step_next(ui_execution=True, play=True):
            self.target = None
            w.playing = False
            w.play.setText('▶ Play')
            w.present()
            return False
        return True

    def play_to(self, target):
        """Play the document (recorded future and your edits) up to `target`.

        Used to return to an earlier position after "Bring back the earlier
        inputs": every frame on the way has a recorded input or an edit.
        """
        w = self.w
        if (w.mutation_pending() and not w.playing) or w.owner_error or w.draft_failure:
            w.error.setText('Return refused: wait for the current command to finish')
            return False
        self.target = target
        self.started_from = w.snapshot.tick
        if w.playing:
            return True
        w.playing = True
        w.play.setText('Ⅱ Pause')
        if not w.step_next(ui_execution=True, play=True):
            self.target = None
            w.playing = False
            w.play.setText('▶ Play')
            w.present()
            return False
        return True

    def cap(self, start, limit):
        """Largest Play batch from `start` that does not pass the target or, in
        read-only mode, the last kept input."""
        stop = self.target
        if self.w.timeline.read_only:
            f = self.future()
            end = f.replay_end if f is not None else start - 1
            stop = end if stop is None else min(stop, end)
        if stop is None:
            return limit
        return max(1, min(limit, stop - start + 1))

    def cancel(self):
        self.target = None

    def after_result(self, snapshot, error):
        """Stop the Play loop at the target; any refusal ends the replay."""
        if self.target is None:
            return
        w = self.w
        reached = snapshot is not None and snapshot.tick >= self.target
        if error or snapshot is None or not w.playing or reached:
            target, self.target = self.target, None
            if reached and w.playing:
                w.playing = False
                w.play.setText('▶ Play')
                w.playback_timer.stop()
                w.show_notice('Replayed to {} {}'.format(w._unit(), snapshot.tick),
                              'Replayed recorded inputs to {} {}.'.format(w._unit(), snapshot.tick))
                w.present()
            elif error:
                w.show_notice('Replay stopped', 'Replay to {} {} stopped: {}'.format(w._unit(), target, error),
                              error=True)

    # -- presentation -------------------------------------------------------
    def cursor_hint(self, value):
        s, f = self.w.snapshot, self.future()
        if s is None or f is None or value <= s.tick:
            return None
        unit = self.w._unit(title=True)
        end, why = self.reach(f)
        if value <= end:
            return '{} {} · release to replay recorded inputs'.format(unit, value)
        return '{} {} · not reachable: {}'.format(unit, value, why)

    def present(self):
        """Counter and rail text for the recorded future; called after present()."""
        tools = getattr(self.w, 'timeline_tools', None)
        if tools is not None:
            tools.present()    # counter with frame, furthest, rewinds and modes
            return
        w, f = self.w, self.future()
        if f is None or w.snapshot is None:
            return
        unit = w._unit(title=True)
        w.counter.setText('{} {} of {}'.format(unit, w.snapshot.tick, f.furthest))
        text = self.describe()
        w.counter.setToolTip(text)
        w.seek.setAccessibleDescription(text)

    def describe(self):
        f, unit = self.future(), self.w._unit()
        if f is None:
            return ''
        end, why = self.reach(f)
        parts = ['Recorded to {} {}.'.format(unit, f.furthest)]
        if end > f.tick:
            parts.append('Drag forward to replay the kept inputs up to {} {}.'.format(unit, end))
        if f.invalid_from is not None and f.stale_index is not None:
            parts.append('Inputs changed at {} {}. The earlier attempt after it is striped on the bar and '
                         'kept in its take; it is not replayed.'.format(unit, f.invalid_from))
        if why:
            parts.append('Replay stops at {} {}: {}.'.format(unit, end, why))
        if self.green is not None:
            parts.append('Return to {} {} with Ctrl+R.'.format(unit, self.green))
        return ' '.join(parts)

    def paint(self, slider, painter, bounds):
        f = self.future()
        pending = self.pending_edit()
        bookmarks = self.bookmarks() if self.bookmarks is not None else ()
        low, high = slider.minimum(), slider.maximum()
        if high <= low or (f is None and pending is None and not bookmarks):
            return
        painter.save()
        try:
            vertical = slider.orientation() == C.Qt.Vertical
            if vertical:
                painter.translate(0, slider.height())
                painter.rotate(-90)
            thickness = slider.width() if vertical else slider.height()
            left, right = bounds
            span = high - low

            def x(tick):
                tick = max(low, min(high, tick))
                return left + round((tick - low) / span * (right - left))
            band_top, band_height = thickness - 6, 4
            if f is not None:
                solid, _ = self.reach(f)
                if solid > f.tick:
                    painter.fillRect(C.QRectF(x(f.tick), band_top, max(2, x(solid) - x(f.tick)), band_height), _REPLAY)
                if f.furthest > max(solid, f.tick):
                    start = max(solid, f.tick)
                    rect = C.QRectF(x(start), band_top, max(2, x(f.furthest) - x(start)), band_height)
                    painter.fillRect(rect, G.QColor(11, 16, 22, 160))
                    painter.fillRect(rect, G.QBrush(_STALE, C.Qt.BDiagPattern))
                painter.setPen(G.QPen(_FURTHEST, 1))
                painter.drawLine(C.QPointF(x(f.furthest) + .5, thickness - 10), C.QPointF(x(f.furthest) + .5, thickness - 1))
            if self.target is not None:
                pen = G.QPen(_REPLAY.lighter(150), 1, C.Qt.DashLine)
                painter.setPen(pen)
                painter.drawLine(C.QPointF(x(self.target) + .5, 0), C.QPointF(x(self.target) + .5, thickness))
            if self.green is not None:
                at = x(self.green)
                painter.setPen(C.Qt.NoPen)
                painter.setBrush(_GREEN)
                painter.drawPolygon(G.QPolygonF([C.QPointF(at - 4, 0), C.QPointF(at + 4, 0), C.QPointF(at, 6)]))
            for tick in bookmarks:
                at = x(tick)
                painter.setPen(C.Qt.NoPen)
                painter.setBrush(_BOOKMARK)
                painter.drawPolygon(G.QPolygonF([C.QPointF(at, 0), C.QPointF(at + 5, 0), C.QPointF(at, 5)]))
            changed = pending if pending is not None else (f.invalid_from if f is not None else None)
            if changed is not None:
                at = x(changed)
                painter.setPen(G.QPen(_CHANGED, 2))
                painter.drawLine(C.QPointF(at, 7), C.QPointF(at, thickness - 1))
                painter.setBrush(_CHANGED)
                painter.setPen(C.Qt.NoPen)
                painter.drawPolygon(G.QPolygonF([C.QPointF(at - 3, thickness - 1), C.QPointF(at + 3, thickness - 1),
                                                 C.QPointF(at, thickness - 5)]))
        finally:
            painter.restore()
