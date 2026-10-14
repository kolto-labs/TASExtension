"""Timeline editing tools for the workspace window.

Adds to the piano roll and seek rail:

* an Edits page: the undo history as a list (click a row to go back or
  forward to it) and the frames you jumped between (Back and Forward);
* bookmarks with notes, shown on the rail, with next and previous;
* range edits as single undoable transactions: copy, cut, paste, paste
  insert, clear, insert, delete and clone frames, hold and pattern fill;
* read-only mode, piano zoom, keyboard jumps and hold-to-advance.

Every edit writes drafts through Timeline.apply_inputs. Nothing here sends an
input to the game: drafts run only when you step, play or apply them, and
navigation uses the existing seek (restore back, replay recorded inputs
forward). Keys are registered in the central action registry, so the
keymap and the Settings dialog can change them.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from PySide6 import QtCore as C, QtGui as G, QtWidgets as W

from . import input_edits as E
from .timeline_future import input_key

TABLE = 'input table'

# (id, label, default keys, scope, method name, description)
ACTIONS = (
    ('timeline.copy', 'Copy frames', ('Ctrl+C',), 'widget', 'copy', 'Copy the inputs of the selected frames.'),
    ('timeline.cut', 'Cut frames', ('Ctrl+X',), 'widget', 'cut', 'Copy the selected frames, then delete them.'),
    ('timeline.paste', 'Paste frames', ('Ctrl+V',), 'widget', 'paste',
     'Paste at the selected frame. Insert mode pushes later inputs right.'),
    ('timeline.paste_insert', 'Paste and insert frames', ('Ctrl+Shift+V',), 'widget', 'paste_insert',
     'Paste at the selected frame and push later inputs right.'),
    ('timeline.clear', 'Release selected keys', ('Delete',), 'widget', 'clear',
     'Release the selected keys on the selected frames.'),
    ('timeline.insert_frames', 'Insert blank frames', ('Ctrl+Insert',), 'widget', 'insert_frames',
     'Insert as many blank frames as are selected. Later inputs move right.'),
    ('timeline.delete_frames', 'Delete frames', ('Ctrl+Delete',), 'widget', 'delete_frames',
     'Delete the selected frames. Later inputs move left.'),
    ('timeline.clone_frames', 'Clone frames', ('Ctrl+D',), 'widget', 'clone_frames',
     'Insert a copy of the selected frames after them.'),
    ('timeline.hold', 'Hold selected keys', ('Ctrl+H',), 'widget', 'hold',
     'Press the selected keys on every selected frame.'),
    ('timeline.pattern', 'Fill a press pattern', ('Ctrl+Shift+H',), 'widget', 'pattern',
     'Press the selected keys for some frames, release them for some, and repeat.'),
    ('timeline.insert_mode', 'Paste mode: insert or overwrite', ('Insert',), 'widget', 'toggle_insert_mode',
     'Insert pushes later inputs right when you paste. Overwrite replaces them.'),
    ('timeline.read_only', 'Read-only', ('Q',), 'widget', 'toggle_read_only',
     'Lock inputs. Step and Play then only replay recorded inputs.'),
    ('timeline.bookmark', 'Add or edit a bookmark', ('M',), 'widget', 'edit_bookmark',
     'Mark the selected frame with a note.'),
    ('timeline.remove_bookmark', 'Remove bookmark', ('Ctrl+M',), 'widget', 'remove_bookmark',
     'Remove the bookmark at the selected frame.'),
    ('timeline.zoom_in', 'Wider frame columns', ('Ctrl+=', 'Ctrl++'), 'widget', 'zoom_in', ''),
    ('timeline.zoom_out', 'Narrower frame columns (down to the configured width)', ('Ctrl+-',), 'widget', 'zoom_out', ''),
    ('timeline.show_start', 'Show frame 1', ('Ctrl+Home',), 'widget', 'show_start', ''),
    ('timeline.show_end', 'Show the last input', ('Ctrl+End',), 'widget', 'show_end', ''),
    ('timeline.show_current', 'Show the current frame', ('Ctrl+J',), 'widget', 'show_current', ''),
    ('timeline.back', 'Go back to the previous position', ('Alt+Left',), 'window', 'back',
     'Return to the frame you were at before the last jump.'),
    ('timeline.forward', 'Go forward to the next position', ('Alt+Right',), 'window', 'forward',
     'Undo a Back jump. Forward replays recorded inputs.'),
    ('timeline.prev_bookmark', 'Go to the previous bookmark', ('Shift+PgUp',), 'window', 'prev_bookmark', ''),
    ('timeline.next_bookmark', 'Go to the next bookmark', ('Shift+PgDown',), 'window', 'next_bookmark', ''),
    ('timeline.go_to', 'Go to frame', ('Ctrl+G',), 'window', 'go_to', 'Type a frame number and go there.'),
    ('timeline.restore_attempt', 'Bring back the earlier inputs', (), 'window', 'restore_attempt',
     'After you recorded a different input, add the earlier attempt\'s inputs back as edits.'),
    ('timeline.green_arrow', 'Return to where you were', ('Ctrl+R',), 'window', 'green_arrow',
     'Go back to the frame you were at before the last jump back. Replays kept inputs.'),
    ('timeline.cancel_seek', 'Stop the replay', ('Ctrl+.',), 'window', 'cancel_seek',
     'Stop a forward replay at the next acknowledged frame.'),
    ('timeline.turbo', 'Turbo', ('Ctrl+T',), 'window', 'toggle_turbo',
     'Play and replay in larger batches with fewer screen updates. Pause can take longer to land.'),
    ('timeline.lock_lanes', 'Lock or unlock selected keys', ('Ctrl+L',), 'widget', 'toggle_lock',
     'Locked keys keep their inputs during paste, clear, hold, pattern, insert and delete.'),
    ('timeline.history', 'Show the edit history', (), 'window', 'show_history', ''),
)

HOLD_DELAY_MS = 400     # hold Step this long before it repeats
HOLD_INTERVAL_MS = 16   # then try one frame per interval when the owner is idle


def install(window):
    """Attach the tools to a workspace window. Never raises."""
    try:
        return TimelineTools(window)
    except Exception as exc:  # optional tools must not stop the window
        try:
            window.overlay.console.appendPlainText(f'Timeline tools are unavailable: {exc}')
        except Exception:
            pass
        return None


class BookmarkStore:
    """Bookmarks with notes, per owner session, in one small JSON file."""

    def __init__(self, path):
        self.path = Path(path) if path else None
        self.data = {}
        self.take_notes = {}      # take path -> note
        if self.path is not None and self.path.is_file():
            try:
                raw = json.loads(self.path.read_text('utf-8'))
                if raw.get('schema') == 'stasis-bookmarks/1' and isinstance(raw.get('sessions'), dict):
                    self.data = {str(k): {int(t): str(n) for t, n in v.items()}
                                 for k, v in raw['sessions'].items() if isinstance(v, dict)}
                    notes = raw.get('take_notes')
                    if isinstance(notes, dict):
                        self.take_notes = {str(k): str(v) for k, v in notes.items()}
            except (OSError, ValueError, TypeError, AttributeError):
                self.data, self.take_notes = {}, {}

    def marks(self, key):
        return self.data.get(key, {}) if key else {}

    def save(self):
        if self.path is None:
            return
        document = {'schema': 'stasis-bookmarks/1',
                    'sessions': {k: {str(t): n for t, n in sorted(v.items())} for k, v in self.data.items() if v},
                    'take_notes': {k: v for k, v in self.take_notes.items() if v}}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(self.path.name + '.tmp')
        temp.write_text(json.dumps(document, indent=1), 'utf-8')
        os.replace(temp, self.path)

    def set(self, key, tick, note):
        self.data.setdefault(key, {})[tick] = note
        self.save()

    def remove(self, key, tick):
        if self.data.get(key, {}).pop(tick, None) is not None:
            self.save()

    def shift(self, key, at, delta):
        """Keep bookmarks with their inputs when frames are inserted (+) or deleted (-)."""
        marks = self.data.get(key)
        if not marks or not delta:
            return
        moved = {}
        for tick, note in marks.items():
            if tick < at:
                moved[tick] = note
            elif delta < 0 and tick < at - delta:
                moved.setdefault(at, note)     # a deleted frame's bookmark lands on the cut
            else:
                moved[tick + delta] = note
        self.data[key] = moved
        self.save()


class TimelineTools(C.QObject):
    def __init__(self, window):
        super().__init__(window.root)
        self.w = window
        self.insert_mode = False
        self.locked = set()           # lanes that range edits leave alone
        self.turbo = None             # TurboSizer layered over the user's sizer while Turbo is on
        self.back_stack, self.forward_stack = [], []
        self._navigating = None
        self._last = None             # (session_id, tick) last presented
        self._replay_from = None
        self.zoom = None
        draft = window.timeline.draft_path
        self.bookmarks = BookmarkStore(Path(draft).with_name('workspace-bookmarks.json') if draft else None)
        window.timeline.history_changed = self.refresh_history
        window.future_rail.bookmarks = lambda: sorted(self.current_marks())
        self._build_pages()
        self._build_takes_page()
        self.tint = PianoTint(self)
        self._build_actions()
        self._wrap_execution()
        self._hold = C.QTimer(self)
        self._hold.timeout.connect(self._hold_tick)
        self._hold_key = None
        self._hold_focus = None
        self._holding = False
        W.QApplication.instance().installEventFilter(self)
        window.table.viewport().installEventFilter(self)
        self.refresh_history()

    # -- helpers -----------------------------------------------------------
    def unit(self, title=False):
        return self.w._unit(title=title)

    def session_key(self):
        s = self.w.snapshot
        token = getattr(s, 'owner_token', None) or {}
        return token.get('session_id') or (getattr(s, 'take_path', None) if s is not None else None)

    def current_marks(self):
        return self.bookmarks.marks(self.session_key())

    def notice(self, label, detail='', error=False):
        self.w.show_notice(label, detail or label, error=error)

    def selection(self):
        """(first frame, last frame, lanes) of the piano selection, else the current cell."""
        table, model = self.w.table, self.w.model
        indexes = table.selectionModel().selectedIndexes() if table.selectionModel() else []
        if not indexes and table.currentIndex().isValid():
            indexes = [table.currentIndex()]
        if not indexes:
            return None
        columns = [i.column() for i in indexes]
        rows = sorted({i.row() for i in indexes})
        lanes = [model.keys[r] for r in rows if 0 <= r < len(model.keys)]
        return model.origin + min(columns), model.origin + max(columns), lanes

    def selected_frame(self):
        chosen = self.selection()
        return chosen[0] if chosen else (self.w.snapshot.tick if self.w.snapshot is not None else None)

    def editable(self):
        w = self.w
        if w.timeline.read_only:
            raise ValueError('read-only is on (Q in the input table turns it off)')
        if w.owner_run_active():
            raise ValueError('a score is loaded; stop or unload it before editing inputs')
        if w.model.busy or w.playing:
            raise ValueError('wait until playback and the current command finish')
        if w.timeline.foreign or w.draft_failure:
            raise ValueError('these inputs belong to another recording')

    def commit(self, values, label, *, shift=None):
        """Apply one transaction, keep bookmarks with their inputs, repaint."""
        if self.locked:
            timeline = self.w.timeline
            kept = {}
            for tick, note in values.items():
                before = timeline.note(tick)
                held = tuple(k for k in note.held if k not in self.locked)
                held += tuple(k for k in before.held if k in self.locked)
                kept[tick] = E.replace(note, held=held)
            values = kept
            label += f' (locked: {", ".join(sorted(self.locked))})'
        changed = self.w.timeline.apply_inputs(values, label)
        if changed and hasattr(self, 'tint'):
            self.tint.flash(sorted(values), undo=False)
        if shift is not None and changed:
            self.bookmarks.shift(self.session_key(), *shift)
        self.repaint()
        self.notice(label, f'{label}: {changed} {self.unit()}{"s" if changed != 1 else ""} changed.'
                    if changed else f'{label}: nothing changed.')
        return changed

    def repaint(self):
        w = self.w
        w.model.refresh(follow=False)
        if w.model.rowCount():
            w.model.dataChanged.emit(w.model.index(0, 0),
                                     w.model.index(w.model.rowCount() - 1, w.model.columnCount() - 1))
        w.table.viewport().update()
        w.seek.update()
        w.present()

    def guarded(self, method):
        def run():
            try:
                method()
            except ValueError as exc:
                self.notice('Not done', str(exc)[:1].upper() + str(exc)[1:] + '.', error=True)
            except Exception as exc:
                self.notice('Not done', f'{type(exc).__name__}: {exc}', error=True)
        return run

    # -- pages -------------------------------------------------------------
    def _build_pages(self):
        overlay = self.w.overlay
        page = W.QWidget()
        layout = W.QVBoxLayout(page)
        layout.setContentsMargins(4, 4, 4, 4)
        title = W.QLabel('Edit history. Select a row to go back or forward to it.')
        title.setWordWrap(True)
        layout.addWidget(title)
        self.history_list = W.QListWidget()
        self.history_list.setAccessibleName('Edit history')
        self.history_list.setAccessibleDescription('Each row is one input edit. Activate a row to undo or redo up to it.')
        self.history_list.itemActivated.connect(self._history_activated)
        self.history_list.itemClicked.connect(self._history_activated)
        layout.addWidget(self.history_list, 2)
        row = W.QHBoxLayout()
        for text, slot, tip in (('Undo', lambda: self.w.undo_draft(redo=False), 'Ctrl+Z in the input table'),
                                ('Redo', lambda: self.w.undo_draft(redo=True), 'Ctrl+Shift+Z or Ctrl+Y in the input table')):
            button = W.QPushButton(text)
            button.setToolTip(tip)
            button.clicked.connect(slot)
            row.addWidget(button)
        row.addStretch()
        layout.addLayout(row)
        visited = W.QLabel('Positions you jumped from')
        layout.addWidget(visited)
        self.visited_list = W.QListWidget()
        self.visited_list.setAccessibleName('Visited positions')
        self.visited_list.itemActivated.connect(self._visited_activated)
        layout.addWidget(self.visited_list, 1)
        row = W.QHBoxLayout()
        for text, slot, tip in (('◀ Back', self.guarded(self.back), 'Alt+Left'),
                                ('Forward ▶', self.guarded(self.forward), 'Alt+Right')):
            button = W.QPushButton(text)
            button.setToolTip(tip)
            button.clicked.connect(slot)
            row.addWidget(button)
        row.addStretch()
        layout.addLayout(row)
        overlay.add_editor_page(page, 'Edits')

        marks = W.QWidget()
        layout = W.QVBoxLayout(marks)
        layout.setContentsMargins(4, 4, 4, 4)
        self.mark_list = W.QListWidget()
        self.mark_list.setAccessibleName('Bookmarks')
        self.mark_list.setAccessibleDescription('Activate a bookmark to go to its frame.')
        self.mark_list.itemActivated.connect(self._mark_activated)
        layout.addWidget(self.mark_list)
        row = W.QHBoxLayout()
        for text, slot, tip in (('Add or edit…', self.guarded(self.edit_bookmark), 'M in the input table'),
                                ('Remove', self.guarded(self.remove_selected_mark), 'Ctrl+M in the input table'),
                                ('Go to', self.guarded(self.goto_selected_mark), 'Enter on a bookmark')):
            button = W.QPushButton(text)
            button.setToolTip(tip)
            button.clicked.connect(slot)
            row.addWidget(button)
        row.addStretch()
        layout.addLayout(row)
        overlay.add_editor_page(marks, 'Bookmarks')

    def refresh_history(self):
        restored = getattr(self.w.timeline, 'last_restored', None)
        if restored and hasattr(self, 'tint'):
            self.w.timeline.last_restored = None
            self.tint.flash(restored, undo=True)
        if not hasattr(self, 'history_list'):
            return
        undo, redo = self.w.timeline.history()
        widget = self.history_list
        widget.blockSignals(True)
        try:
            widget.clear()
            widget.addItem('Start')
            for label in undo:
                widget.addItem(label)
            for label in redo:
                item = W.QListWidgetItem(label + '  (undone)')
                item.setForeground(G.QBrush(G.QColor('#9AA8B4')))
                widget.addItem(item)
            widget.setCurrentRow(len(undo))
        finally:
            widget.blockSignals(False)

    def _history_activated(self, item):
        row = self.history_list.row(item)
        timeline = self.w.timeline
        try:
            while len(timeline.history()[0]) > row:
                if timeline.undo_edit() is None:
                    raise ValueError('an earlier edit no longer matches the inputs, so undo stopped there')
            while len(timeline.history()[0]) < row:
                if timeline.redo_edit() is None:
                    raise ValueError('a later edit no longer matches the inputs, so redo stopped there')
        except ValueError as exc:
            self.notice('History', str(exc)[:1].upper() + str(exc)[1:] + '.', error=True)
        self.refresh_history()
        self.repaint()

    def refresh_visited(self):
        if not hasattr(self, 'visited_list'):
            return
        self.visited_list.clear()
        for tick in reversed(self.back_stack[-50:]):
            self.visited_list.addItem(f'{self.unit(title=True)} {tick}')

    def _visited_activated(self, item):
        text = item.text().rsplit(' ', 1)[-1]
        if text.isdigit():
            self.go(int(text))

    def refresh_marks(self):
        if not hasattr(self, 'mark_list'):
            return
        self.mark_list.clear()
        for tick, note in sorted(self.current_marks().items()):
            item = W.QListWidgetItem(f'{self.unit(title=True)} {tick}' + (f' · {note}' if note else ''))
            item.setData(C.Qt.ItemDataRole.UserRole, tick)
            item.setToolTip(note or 'No note')
            self.mark_list.addItem(item)

    def _mark_activated(self, item):
        self.go(item.data(C.Qt.ItemDataRole.UserRole))

    def goto_selected_mark(self):
        item = self.mark_list.currentItem()
        if item is None:
            raise ValueError('choose a bookmark first')
        self.go(item.data(C.Qt.ItemDataRole.UserRole))

    def remove_selected_mark(self):
        item = self.mark_list.currentItem()
        if item is None:
            raise ValueError('choose a bookmark first')
        self.bookmarks.remove(self.session_key(), item.data(C.Qt.ItemDataRole.UserRole))
        self.refresh_marks()
        self.w.seek.update()

    # -- takes (branches) ----------------------------------------------------
    def _build_takes_page(self):
        page = W.QWidget()
        layout = W.QVBoxLayout(page)
        layout.setContentsMargins(4, 4, 4, 4)
        intro = W.QLabel('Takes in this session. Each rewind starts a take; its parent keeps the frames after it.')
        intro.setWordWrap(True)
        layout.addWidget(intro)
        split = W.QHBoxLayout()
        self.take_tree = W.QTreeWidget()
        self.take_tree.setHeaderLabels(['Take', 'Frames', 'Changed at', 'Rerecords', 'Note'])
        self.take_tree.setAccessibleName('Takes')
        self.take_tree.setAccessibleDescription('Takes as a tree. Select one to see its last image and its mark times.')
        self.take_tree.currentItemChanged.connect(lambda *_: self._take_selected())
        split.addWidget(self.take_tree, 3)
        side = W.QVBoxLayout()
        self.take_image = W.QLabel('No image')
        self.take_image.setFixedSize(176, 112)
        self.take_image.setAlignment(C.Qt.AlignmentFlag.AlignCenter)
        self.take_image.setAccessibleName('Last image of the selected take')
        side.addWidget(self.take_image)
        self.take_detail = W.QLabel('')
        self.take_detail.setWordWrap(True)
        self.take_detail.setTextInteractionFlags(C.Qt.TextInteractionFlag.TextSelectableByMouse)
        side.addWidget(self.take_detail, 1)
        split.addLayout(side, 2)
        layout.addLayout(split, 1)
        row = W.QHBoxLayout()
        note = W.QPushButton('Note…')
        note.setToolTip('Write a note for the selected take')
        note.clicked.connect(self.guarded(self.edit_take_note))
        row.addWidget(note)
        open_take = W.QPushButton('Open for playback')
        open_take.setToolTip('Load the selected take as a recording. Step and Play then replay it; '
                             'branch to change it.')
        open_take.clicked.connect(self.guarded(self.open_take))
        row.addWidget(open_take)
        row.addStretch()
        layout.addLayout(row)
        self.take_page = page
        self.w.overlay.editor_pages.currentChanged.connect(lambda *_: self._takes_page_changed())
        self.w.overlay.add_editor_page(page, 'Takes')

    def _takes_page_changed(self):
        if self.w.overlay.editor_pages.currentWidget() is self.take_page:
            takes = self._takes()
            self._tree_key = (len(takes), takes[-1].path if takes else None)
            self.refresh_takes()

    def _takes(self):
        lineage = getattr(self.w.snapshot, 'lineage', None)
        return tuple(lineage) if lineage else ()

    def refresh_takes(self):
        if not hasattr(self, 'take_tree'):
            return
        tree = self.take_tree
        selected = tree.currentItem().data(0, C.Qt.ItemDataRole.UserRole) if tree.currentItem() else None
        tree.blockSignals(True)
        tree.clear()
        items = {}
        for take in self._takes():
            name = Path(take.path).name if take.path else '?'
            start = take.branch_tick if take.branch_tick is not None else 0
            item = W.QTreeWidgetItem([name + (' (current)' if take.current else ''),
                                      f'{start}–{take.last}',
                                      '' if take.changed_at is None else str(take.changed_at),
                                      str(take.rerecords), self.bookmarks.take_notes.get(take.path, '')])
            item.setData(0, C.Qt.ItemDataRole.UserRole, take.path)
            item.setToolTip(0, take.path or '')
            if take.current:
                font = item.font(0)
                font.setBold(True)
                item.setFont(0, font)
            parent = items.get(take.parent)
            (parent.addChild(item) if parent is not None else tree.addTopLevelItem(item))
            items[take.path] = item
        tree.expandAll()
        for column in range(4):
            tree.resizeColumnToContents(column)
        if selected in items:
            tree.setCurrentItem(items[selected])
        tree.blockSignals(False)
        self._take_selected()

    def _selected_take(self):
        item = self.take_tree.currentItem()
        path = item.data(0, C.Qt.ItemDataRole.UserRole) if item is not None else None
        return next((t for t in self._takes() if t.path == path), None)

    def _take_selected(self):
        take = self._selected_take()
        if take is None:
            self._image_path = None
            self.take_image.setPixmap(G.QPixmap())
            self.take_image.setText('No image')
            self.take_detail.setText('')
            return
        load_image = getattr(self, '_image_path', None) != take.path
        self._image_path = take.path
        frames = []
        if load_image:
            self.take_image.setPixmap(G.QPixmap())
            self.take_image.setText('No image')
            try:
                frames = sorted(Path(take.path, 'frames').glob('*.png'))
            except OSError:
                frames = []
        if frames:
            pixmap = G.QPixmap(str(frames[-1]))
            if not pixmap.isNull():
                self.take_image.setPixmap(pixmap.scaled(self.take_image.size(), C.Qt.AspectRatioMode.KeepAspectRatio,
                                                        C.Qt.TransformationMode.SmoothTransformation))
        current = next((t for t in self._takes() if t.current), None)
        unit = self.unit()
        lines = [f'{unit.title()}s {take.branch_tick or 0}–{take.last}.']
        if take.changed_at is not None:
            lines.append(f'First changed input: {unit} {take.changed_at}.')
        if current is not None and current is not take:
            shared = sorted(set(take.marks) & set(current.marks), key=lambda n: current.marks[n])
            for name in shared:
                delta = take.marks[name] - current.marks[name]
                lines.append(f'@{name}: {take.marks[name]} there, {current.marks[name]} here '
                             f'({"+" if delta > 0 else ""}{delta} {unit}s).')
            if not shared and (take.marks or current.marks):
                lines.append('No score mark is in both takes.')
        note = self.bookmarks.take_notes.get(take.path)
        if note:
            lines.append('Note: ' + note)
        self.take_detail.setText('\n'.join(lines))

    def edit_take_note(self):
        take = self._selected_take()
        if take is None:
            raise ValueError('select a take first')
        note, ok = W.QInputDialog.getText(self.w.root, 'Take note', f'Note for {Path(take.path).name}:',
                                          text=self.bookmarks.take_notes.get(take.path, ''))
        if ok:
            self.bookmarks.take_notes[take.path] = note.strip()
            self.bookmarks.save()
            self.refresh_takes()

    def open_take(self):
        take = self._selected_take()
        if take is None:
            raise ValueError('select a take first')
        if take.current:
            raise ValueError('this is the current take')
        answer = W.QMessageBox.question(
            self.w.root, 'Open take',
            f'Open {Path(take.path).name} for playback? The current take stays saved. '
            'Recording continues only after you branch.')
        if answer != W.QMessageBox.StandardButton.Yes:
            return
        self.w.overlay_action('load_recording', path=take.path)

    # -- actions -----------------------------------------------------------
    def _build_actions(self):
        from .actions import register_action
        binder = getattr(self.w.overlay, '_shortcut_binder', None)
        menu = self.w.overlay.more_menu.addMenu('Timeline')
        self.qactions = {}
        for action_id, label, keys, scope, name, description in ACTIONS:
            callback = self.guarded(getattr(self, name))
            if scope == 'widget':
                register_action(action_id, label, category='Timeline', default=keys, scope='widget',
                                widget=TABLE, description=description or 'Works while the input table has focus.')
                qaction = G.QAction(label, self.w.table)
                qaction.setShortcutContext(C.Qt.WidgetWithChildrenShortcut)
                qaction.setToolTip(description or label)
                qaction.triggered.connect(callback)
                self.w.table.addAction(qaction)
                if binder is not None:
                    binder.attach_qaction(action_id, qaction)
                else:
                    qaction.setShortcuts([G.QKeySequence(k) for k in keys])
            else:
                register_action(action_id, label, category='Timeline', default=keys, scope='window',
                                callback=callback, description=description)
                qaction = G.QAction(label, menu)
                qaction.triggered.connect(callback)
                if binder is not None:
                    binder.show_in_menu(action_id, qaction, label)
            if name in ('toggle_insert_mode', 'toggle_read_only', 'toggle_turbo'):
                qaction.setCheckable(True)
            menu.addAction(qaction)
            self.qactions[action_id] = qaction

    def _wrap_execution(self):
        """Read-only: Step and Play stop where the recorded inputs end."""
        w = self.w
        step_next, apply_selected = w.step_next, w.apply_selected

        def guarded_step(*args, **kwargs):
            if w.timeline.read_only and getattr(w.snapshot, 'mode', 'live') == 'live':
                f = w.future_rail.future()
                if f is None or w.snapshot.tick >= f.replay_end:
                    w.error.setText(f'Read-only: no recorded input after {self.unit()} {w.snapshot.tick}')
                    return False
            return step_next(*args, **kwargs)

        def guarded_apply(*args, **kwargs):
            if w.timeline.read_only:
                w.error.setText('Read-only is on; turn it off to apply an edit')
                return False
            return apply_selected(*args, **kwargs)
        # Instance attributes: every caller in the window reaches these. The
        # Apply button was connected to the bound method before; route it here.
        w.step_next = guarded_step
        w.apply_selected = guarded_apply
        try:
            w.apply.clicked.disconnect(apply_selected)
        except (RuntimeError, TypeError):
            pass
        w.apply.clicked.connect(lambda *_: w.apply_selected())

    # -- edit commands -----------------------------------------------------
    def _range(self):
        chosen = self.selection()
        if chosen is None:
            raise ValueError(f'select one or more {self.unit()}s in the input table first')
        return chosen

    def copy(self):
        low, high, _ = self._range()
        text = E.notes_to_text(E.frame_notes(self.w.timeline, low, high))
        W.QApplication.clipboard().setText(text)
        self.notice('Copied', f'Copied {high - low + 1} {self.unit()}s ({low}–{high}).')

    def cut(self):
        self.editable()
        low, high, _ = self._range()
        self.copy()
        self.commit(E.delete_frames(self.w.timeline, low, high - low + 1),
                    f'Cut {self.unit()}s {low}–{high}', shift=(low, -(high - low + 1)))

    def _clipboard_notes(self):
        return E.text_to_notes(W.QApplication.clipboard().text())

    def paste(self, insert=None):
        self.editable()
        at = self._range()[0]
        notes = self._clipboard_notes()
        insert = self.insert_mode if insert is None else insert
        values = E.paste(self.w.timeline, at, notes, insert=insert)
        verb = 'Pasted and inserted' if insert else 'Pasted'
        self.commit(values, f'{verb} {len(notes)} {self.unit()}s at {at}',
                    shift=(at, len(notes)) if insert else None)

    def paste_insert(self):
        self.paste(insert=True)

    def clear(self):
        self.editable()
        low, high, lanes = self._range()
        self.commit(E.set_lanes(self.w.timeline, low, high, lanes, False),
                    f'Released {", ".join(lanes)} on {low}–{high}')

    def hold(self):
        self.editable()
        low, high, lanes = self._range()
        self.commit(E.set_lanes(self.w.timeline, low, high, lanes, True),
                    f'Held {", ".join(lanes)} on {low}–{high}')

    def pattern(self):
        self.editable()
        low, high, lanes = self._range()
        dialog = W.QDialog(self.w.root)
        dialog.setWindowTitle('Press pattern')
        form = W.QFormLayout(dialog)
        press, release = W.QSpinBox(), W.QSpinBox()
        for box in (press, release):
            box.setRange(1, 600)
            box.setValue(1)
        form.addRow(f'Press for ({self.unit()}s)', press)
        form.addRow(f'Release for ({self.unit()}s)', release)
        buttons = W.QDialogButtonBox(W.QDialogButtonBox.StandardButton.Ok | W.QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        if dialog.exec() != W.QDialog.DialogCode.Accepted:
            return
        on, off = press.value(), release.value()
        values = {}
        notes = E.frame_notes(self.w.timeline, low, high)
        for tick, note in zip(range(low, high + 1), notes):
            pressed = (tick - low) % (on + off) < on
            values[tick] = E.replace(note, held=E._held(note.held, lanes, pressed))
        self.commit(values, f'Pattern {on} on, {off} off for {", ".join(lanes)} on {low}–{high}')

    def _count(self, title, default):
        value, ok = W.QInputDialog.getInt(self.w.root, title, f'How many {self.unit()}s?', default, 1, E.MAX_FRAMES)
        return value if ok else None

    def insert_frames(self):
        self.editable()
        low, high, _ = self._range()
        count = high - low + 1
        if count == 1:
            count = self._count('Insert blank frames', 1)
            if count is None:
                return
        self.commit(E.insert_frames(self.w.timeline, low, count),
                    f'Inserted {count} blank {self.unit()}{"s" if count != 1 else ""} at {low}', shift=(low, count))

    def delete_frames(self):
        self.editable()
        low, high, _ = self._range()
        count = high - low + 1
        self.commit(E.delete_frames(self.w.timeline, low, count),
                    f'Deleted {self.unit()}s {low}–{high}', shift=(low, -count))

    def clone_frames(self):
        self.editable()
        low, high, _ = self._range()
        count = high - low + 1
        self.commit(E.clone_frames(self.w.timeline, low, high),
                    f'Cloned {self.unit()}s {low}–{high}', shift=(high + 1, count))

    def toggle_insert_mode(self):
        self.insert_mode = not self.insert_mode
        self.qactions['timeline.insert_mode'].setChecked(self.insert_mode)
        self.notice('Paste mode', 'Paste inserts frames.' if self.insert_mode else 'Paste overwrites frames.')
        self.w.present()

    def toggle_read_only(self):
        timeline = self.w.timeline
        timeline.read_only = not timeline.read_only
        self.qactions['timeline.read_only'].setChecked(timeline.read_only)
        self.notice('Read-only on' if timeline.read_only else 'Read-only off',
                    'Inputs are locked. Step and Play replay recorded inputs and stop where they end.'
                    if timeline.read_only else 'You can edit inputs again.')
        self.w.present()

    def restore_attempt(self):
        """Write the earlier attempt's inputs after the change point back as drafts."""
        self.editable()
        f = self.w.future_rail.future()
        if f is None or f.invalid_from is None or f.stale_index is None:
            raise ValueError('there is no earlier attempt to bring back here')
        low = max(f.invalid_from, 1)
        # Frames this branch recorded differently become pending past edits;
        # later frames become drafts that Step and Play will run.
        high = min(f.furthest, low + E.MAX_FRAMES - 1)
        values = {}
        for tick in range(low, high + 1):
            note = f.stale_index.note(tick)
            if note is None:
                break
            values[tick] = note
        if not values:
            raise ValueError('the earlier attempt has no inputs to bring back')
        self.commit(values, f'Brought back earlier inputs {low}–{max(values)}')

    def toggle_lock(self):
        chosen = self.selection()
        if chosen is None:
            raise ValueError('select a key lane in the input table first')
        lanes = set(chosen[2])
        if lanes <= self.locked:
            self.locked -= lanes
            self.notice('Keys unlocked', f'Unlocked {", ".join(sorted(lanes))}.')
        else:
            self.locked |= lanes
            self.notice('Keys locked', f'Locked {", ".join(sorted(lanes))}. Range edits leave them alone.')
        self.w.present()

    def green_arrow(self):
        green = self.w.future_rail.green
        s = self.w.snapshot
        if green is None or s is None or green <= s.tick:
            raise ValueError('there is no later frame to return to')
        rail, timeline = self.w.future_rail, self.w.timeline
        pending = rail.pending_edit()
        if pending is not None and pending <= s.tick:
            raise ValueError(f'go back to {self.unit()} {pending - 1} (just before the red mark) first, '
                             'then return with Ctrl+R')
        end, why = rail.reach()
        if end is not None and end >= green:
            self.go(green)
            return
        f = rail.future()
        covered = all(t in timeline.drafts or (f is not None and f.note(t) is not None)
                      for t in range(s.tick + 1, green + 1))
        if not covered:
            raise ValueError(f'{self.unit()} {green} cannot be replayed from here' + (f': {why}' if why else '')
                             + '. "Bring back the earlier inputs" in the Timeline menu adds them back as edits')
        if not rail.play_to(green):
            raise ValueError(self.w.error.text() or 'playback was not accepted')

    def cancel_seek(self):
        w = self.w
        if w.future_rail.target is None and not w.playing:
            raise ValueError('nothing is replaying')
        if w.playing:
            w.toggle_play()     # pauses and ends the target; the batch in flight lands first
        else:
            w.future_rail.cancel()
            w.present()

    def toggle_turbo(self):
        w = self.w
        if self.turbo is None:
            self.turbo = TurboSizer(w.play_sizer)
            w.play_sizer = self.turbo
            self.notice('Turbo on', 'Play and replay use larger batches. Pause lands at the end of a batch.')
        else:
            w.play_sizer = self.turbo.base   # the user's pacing, including any change made meanwhile
            self.turbo = None
            self.notice('Turbo off', 'Play uses your playback pacing again.')
        self.qactions['timeline.turbo'].setChecked(self.turbo is not None)
        w.present()

    # -- bookmarks -----------------------------------------------------------
    def edit_bookmark(self):
        tick = self.selected_frame()
        key = self.session_key()
        if tick is None or key is None:
            raise ValueError('wait for the owner to connect')
        current = self.current_marks().get(tick, '')
        note, ok = W.QInputDialog.getText(self.w.root, 'Bookmark',
                                          f'Note for {self.unit()} {tick} (can be empty):', text=current)
        if not ok:
            return
        self.bookmarks.set(key, tick, note.strip())
        self.refresh_marks()
        self.w.seek.update()
        self.notice('Bookmark saved', f'Bookmarked {self.unit()} {tick}.')

    def remove_bookmark(self):
        tick = self.selected_frame()
        if tick not in self.current_marks():
            raise ValueError(f'{self.unit()} {tick} has no bookmark')
        self.bookmarks.remove(self.session_key(), tick)
        self.refresh_marks()
        self.w.seek.update()

    def _bookmark_step(self, direction):
        s = self.w.snapshot
        if s is None:
            raise ValueError('wait for the owner to connect')
        marks = sorted(self.current_marks())
        targets = [t for t in marks if (t > s.tick if direction > 0 else t < s.tick)]
        if not targets:
            raise ValueError('no bookmark ' + ('after' if direction > 0 else 'before') + f' {self.unit()} {s.tick}')
        self.go(targets[0] if direction > 0 else targets[-1])

    def next_bookmark(self):
        self._bookmark_step(1)

    def prev_bookmark(self):
        self._bookmark_step(-1)

    # -- navigation ----------------------------------------------------------
    def go(self, tick):
        """Seek to a frame: restore back, replay recorded inputs forward.

        A forward frame the replay cannot reach is shown in the piano instead.
        """
        w = self.w
        s = w.snapshot
        if s is None or type(tick) is not int:
            raise ValueError('wait for the owner to connect')
        if tick == s.tick:
            self.show_frame(tick)
            return
        if tick > s.tick and getattr(s, 'mode', 'live') == 'live':
            end, why = w.future_rail.reach()
            if end is None or tick > end:
                self.show_frame(tick)
                raise ValueError(f'{self.unit()} {tick} cannot be replayed from here' + (f': {why}' if why else ''))
        if not w.submit('seek', tick):
            raise ValueError(w.seek_refusal_reason() or w.error.text() or 'the seek was not accepted')

    def back(self):
        if not self.back_stack:
            raise ValueError('there is no earlier position')
        target = self.back_stack[-1]
        self._navigating = target
        try:
            self.go(target)
        except ValueError:
            self._navigating = None
            raise
        self.back_stack.pop()
        self.forward_stack.append(self.w.snapshot.tick)
        self.refresh_visited()

    def forward(self):
        if not self.forward_stack:
            raise ValueError('there is no later position')
        target = self.forward_stack[-1]
        self._navigating = target
        try:
            self.go(target)
        except ValueError:
            self._navigating = None
            raise
        self.forward_stack.pop()
        self.back_stack.append(self.w.snapshot.tick)
        self.refresh_visited()

    def go_to(self):
        s = self.w.snapshot
        if s is None:
            raise ValueError('wait for the owner to connect')
        top = max(1, self.w.overlay.seek_extent(s))
        value, ok = W.QInputDialog.getInt(self.w.root, 'Go to frame', f'{self.unit(title=True)} (0–{top}):',
                                          s.tick, 0, top)
        if ok:
            self.go(value)

    def show_history(self):
        overlay = self.w.overlay
        if 'Edits' in overlay.editor_names:
            overlay.show_editor_page(overlay.editor_names.index('Edits'))

    def jumped(self, before, after):
        """Record a jump (not a single frame step) for Back and Forward."""
        if self._navigating is not None:
            self._navigating = None    # Back/Forward already moved the stacks
            return
        if before == after or abs(before - after) <= 1:
            return
        if after < before:
            self.w.future_rail.green = max(before, self.w.future_rail.green or 0)
        if not self.back_stack or self.back_stack[-1] != before:
            self.back_stack.append(before)
            del self.back_stack[:-100]
        self.forward_stack.clear()
        self.refresh_visited()

    # -- piano view ------------------------------------------------------------
    def show_frame(self, tick):
        w = self.w
        w.browse_inputs(vertical=False)
        w.show_grid_tick(max(1, tick))

    def show_start(self):
        self.show_frame(1)

    def show_end(self):
        self.show_frame(E.document_end(self.w.timeline))

    def show_current(self):
        if self.w.snapshot is not None:
            self.show_frame(self.w.snapshot.tick)

    def _apply_zoom(self):
        header = self.w.table.horizontalHeader()
        if self.zoom is not None and header.defaultSectionSize() < self.zoom:
            header.setDefaultSectionSize(self.zoom)

    def zoom_in(self):
        header = self.w.table.horizontalHeader()
        self.zoom = min(96, header.defaultSectionSize() + 4)
        header.setDefaultSectionSize(self.zoom)

    def zoom_out(self):
        header = self.w.table.horizontalHeader()
        current = header.defaultSectionSize()
        self.zoom = None
        self.w.overlay.fit_piano_headers()   # the configured width keeps labels readable
        base = header.defaultSectionSize()
        if current - 4 > base:
            self.zoom = current - 4
            header.setDefaultSectionSize(self.zoom)

    # -- presentation ----------------------------------------------------------
    def present(self):
        """Counter, jump tracking and the Paste/Read-only state. Called from present()."""
        w, s = self.w, self.w.snapshot
        if s is None:
            return
        key = self.session_key()
        if self._last is not None and self._last[0] == key and getattr(s, 'mode', 'live') == 'live':
            if s.tick < self._last[1]:
                self.jumped(self._last[1], s.tick)
            elif w.future_rail.target is None and self._replay_from is not None:
                self.jumped(self._replay_from, s.tick)
                self._replay_from = None
        if w.future_rail.target is not None and self._replay_from is None:
            self._replay_from = self._last[1] if self._last is not None else s.tick
        self._last = (key, s.tick)
        if getattr(self, '_marks_key', None) != key:
            self._marks_key = key
            self.refresh_marks()
        self._apply_zoom()
        rail = w.future_rail
        if rail.green is not None and s.tick >= rail.green:
            rail.green = None
        f = rail.future()
        parts = [f'{self.unit(title=True)} {s.tick}']
        if f is not None:
            parts[0] += f' of {f.furthest}'
        target = rail.target
        if target is not None:
            start = rail.started_from if rail.started_from is not None else s.tick
            done = 100 * max(0, s.tick - start) // max(1, target - start)
            parts.append(f'replaying to {target} ({done}%)')
        rerecords = getattr(s, 'rerecords', None)
        if type(rerecords) is int:
            parts.append(f'{rerecords} rerecord{"s" if rerecords != 1 else ""}')
        if w.timeline.read_only:
            parts.append('read-only')
        if self.insert_mode:
            parts.append('paste inserts')
        if self.turbo is not None:
            parts.append('turbo')
        if self.locked:
            parts.append('locked: ' + ', '.join(sorted(self.locked)))
        w.counter.setText(' · '.join(parts))
        detail = rail.describe()
        if type(rerecords) is int:
            count = getattr(s, 'take_count', None)
            detail += (' ' if detail else '') + (
                f'Rerecords: {rerecords} rewinds were followed by a changed input (the libTAS rule). '
                + (f'This owner session holds {count} takes.' if type(count) is int else ''))
        w.counter.setToolTip(detail)
        w.seek.setAccessibleDescription(detail or w.seek.accessibleDescription())
        takes = self._takes()
        tree_key = (len(takes), takes[-1].path if takes else None)
        page_shown = (hasattr(self, 'take_page') and self.w.overlay.editor_pages.currentWidget() is self.take_page
                      and self.take_page.isVisible())
        if page_shown and getattr(self, '_tree_key', None) != tree_key:
            self._tree_key = tree_key
            self.refresh_takes()

    # -- events ------------------------------------------------------------------
    def _step_keys(self):
        try:
            from .keymap import action_keys
            keys = action_keys('step', layout=dict(self.w.overlay.ui_preferences.shortcuts))
        except Exception:
            keys = ()
        if not keys:
            keys = tuple(k for a, k in self.w.overlay.ui_preferences.shortcuts if a == 'step' and k)
        return tuple(G.QKeySequence(k) for k in keys)

    def _matches_step(self, event):
        try:
            pressed = G.QKeySequence(event.keyCombination())
        except Exception:
            return False
        return any(pressed.matches(seq) == G.QKeySequence.SequenceMatch.ExactMatch for seq in self._step_keys())

    def _hold_tick(self):
        w = self.w
        if (not self._holding or w.closed or self._typing()
                or W.QApplication.activePopupWidget() is not None
                or W.QApplication.focusWidget() is not self._hold_focus
                or not self._hold_focus.isVisible() or not self._hold_focus.isEnabled()):
            self._stop_hold()
            return
        self._hold.setInterval(HOLD_INTERVAL_MS)
        if w.playing or w.owner_run_active():
            self._stop_hold()
            return
        # Busy: wait for the acknowledged frame instead of queueing or refusing.
        if w.pending or w.mutation_pending() or w.deferred_command is not None:
            return
        if not w.step.isEnabled() or not w.step_next():
            self._stop_hold()

    def _stop_hold(self):
        self._holding = False
        self._hold_key = None
        self._hold_focus = None
        self._hold.stop()

    @staticmethod
    def _typing():
        """A dialog or text field has the keyboard: holding a key must not step the game."""
        if W.QApplication.activeModalWidget() is not None:
            return True
        focus = W.QApplication.focusWidget()
        if isinstance(focus, (W.QLineEdit, W.QTextEdit, W.QPlainTextEdit, W.QAbstractSpinBox)):
            return True
        return isinstance(focus, W.QComboBox) and focus.isEditable()

    def eventFilter(self, watched, event):
        kind = event.type()
        if kind in (C.QEvent.Type.ShortcutOverride, C.QEvent.Type.KeyPress) and not event.isAutoRepeat():
            focus = W.QApplication.focusWidget()
            if (self._typing() or W.QApplication.activePopupWidget() is not None
                    or focus not in (self.w.seek, self.w.table)):
                self._stop_hold()
                return False
            if self._holding and event.key() != self._hold_key:
                self._stop_hold()
            if self._matches_step(event) and not self._holding:
                # Plain bindings belong to the rail's WidgetShortcut. A table
                # editing key must not acquire a delayed game-step side effect.
                sequence = G.QKeySequence(event.keyCombination())
                if self.w.overlay.timeline_scoped_shortcut(sequence) and focus is not self.w.seek:
                    return False
                self._holding = True
                self._hold_key = event.key()
                self._hold_focus = focus
                self._hold.start(HOLD_DELAY_MS)
        elif kind == C.QEvent.Type.KeyRelease and not event.isAutoRepeat() and self._holding:
            # Use the initiating key, even if bindings changed while held.
            # Releasing a modifier also ends the initiating chord.
            if event.key() in (self._hold_key, C.Qt.Key_Control, C.Qt.Key_Shift,
                               C.Qt.Key_Alt, C.Qt.Key_Meta, C.Qt.Key_AltGr):
                self._stop_hold()
        elif kind == C.QEvent.Type.WindowDeactivate and self._holding:
            self._stop_hold()
        elif kind in (C.QEvent.Type.FocusOut, C.QEvent.Type.Hide) and watched is self._hold_focus:
            self._stop_hold()
        elif (kind == C.QEvent.Type.Wheel and watched is self.w.table.viewport()
              and event.modifiers() & C.Qt.KeyboardModifier.ControlModifier):
            (self.zoom_in if event.angleDelta().y() > 0 else self.zoom_out)()
            return True
        return False


class TurboSizer:
    """Turbo as a layer over the user's PlayBatchSizer.

    Reads and writes of pacing settings (Settings > Playback) go to the
    user's sizer, so Turbo never overwrites them and turning Turbo off keeps
    any change made meanwhile. Only the batch size used for Play is Turbo's.
    """
    MAX_TICKS = 2000
    MAX_WALL_S = 10.0

    def __init__(self, base):
        object.__setattr__(self, 'base', base)
        object.__setattr__(self, 'turbo_ticks', max(1, int(getattr(base, 'ticks', 1))))

    def __getattr__(self, name):
        return getattr(self.base, name)

    def __setattr__(self, name, value):
        setattr(self.base, name, value)

    @property
    def ticks(self):
        return self.turbo_ticks

    def record(self, ticks, seconds):
        size = self.turbo_ticks
        if seconds > self.MAX_WALL_S and size > 1:
            size = max(1, size // 2)
        elif seconds * 2 <= self.MAX_WALL_S and ticks >= size:
            size = min(self.MAX_TICKS, size * 2)
        object.__setattr__(self, 'turbo_ticks', size)


class PianoTint(W.QWidget):
    """Paints over the piano: recent edits (orange, cooling), undone frames
    (purple flash), frames that re-run with kept inputs (stripes), bookmarks
    and the return point. Mouse events pass through."""
    HOT_S = 8.0
    FLASH_S = 1.2

    def __init__(self, tools):
        viewport = tools.w.table.viewport()
        super().__init__(viewport)
        self.tools = tools
        self.hot = {}       # tick -> monotonic time of the edit
        self.undone = {}
        self.setAttribute(C.Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(C.Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setGeometry(viewport.rect())
        viewport.installEventFilter(self)
        table = tools.w.table
        table.horizontalScrollBar().valueChanged.connect(self.update)
        tools.w.model.modelReset.connect(self.update)
        tools.w.model.dataChanged.connect(lambda *_: self.update())
        self.timer = C.QTimer(self)
        self.timer.setInterval(250)
        self.timer.timeout.connect(self._tick)
        self.show()

    def eventFilter(self, watched, event):
        if event.type() == C.QEvent.Type.Resize:
            self.setGeometry(watched.rect())
        return False

    def flash(self, ticks, *, undo):
        import time
        now = time.monotonic()
        table = self.undone if undo else self.hot
        for tick in list(ticks)[:2000]:
            table[tick] = now
        self.raise_()
        if not self.timer.isActive():
            self.timer.start()
        self.update()

    def _tick(self):
        import time
        now = time.monotonic()
        self.hot = {t: at for t, at in self.hot.items() if now - at < self.HOT_S}
        self.undone = {t: at for t, at in self.undone.items() if now - at < self.FLASH_S}
        if not self.hot and not self.undone:
            self.timer.stop()
        self.update()

    def paintEvent(self, event):
        import time
        w = self.tools.w
        model, table = w.model, w.table
        if model.columnCount() == 0:
            return
        rail = w.future_rail
        f = rail.future()
        rerun = rail.striped_from(f) if f is not None else None
        marks = self.tools.current_marks()
        now = time.monotonic()
        height = self.height()
        painter = G.QPainter(self)
        try:
            for column in range(model.columnCount()):
                x = table.columnViewportPosition(column)
                if x < -200 or x > self.width():
                    continue
                width = table.columnWidth(column)
                tick = model.origin + column
                rect = C.QRectF(x, 0, width, height)
                if f is not None and rerun is not None and rerun <= tick <= f.furthest:
                    painter.fillRect(rect, G.QBrush(G.QColor(229, 193, 91, 55), C.Qt.BDiagPattern))
                at = self.hot.get(tick)
                if at is not None:
                    alpha = int(90 * max(0.0, 1 - (now - at) / self.HOT_S))
                    painter.fillRect(rect, G.QColor(242, 166, 90, alpha))
                at = self.undone.get(tick)
                if at is not None:
                    alpha = int(120 * max(0.0, 1 - (now - at) / self.FLASH_S))
                    painter.fillRect(rect, G.QColor(180, 142, 240, alpha))
                if tick in marks:
                    painter.fillRect(C.QRectF(x, 0, width, 3), G.QColor('#E5C15B'))
                if rail.green == tick:
                    painter.setPen(G.QPen(G.QColor('#65D6AD'), 2, C.Qt.DashLine))
                    painter.drawLine(C.QPointF(x + width / 2, 0), C.QPointF(x + width / 2, height))
        finally:
            painter.end()
