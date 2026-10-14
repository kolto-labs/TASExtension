"""A remote client: Qt stays on its GUI thread; one worker owns all IO."""
from pathlib import Path
import os
import time
from types import SimpleNamespace
from .overlay import Overlay, RemotePresentation
from queue import Queue
from threading import Event, Lock, Thread
from PySide6 import QtCore as C, QtGui as G, QtWidgets as W
from .timeline import Timeline, note_action
from .workspace_session import ReadOnlyResponsePending
from .run_queue import BatchSizer as PlayBatchSizer
from .score_control import RUN_CONTROL_OPS
from .native_surface import NativeSurface
from kptas.score.keys import KEYS, SYNONYMS, BUTTONS, canonical_token
from kptas.config import unit_name


class Signals(C.QObject):
    result = C.Signal(object, object, object)
    refresh_view = C.Signal(object, object)
    preview = C.Signal(object)
    bootstrap = C.Signal(object)
    sequence = C.Signal(object)
    debugger_inspection = C.Signal(object)
    tool_action = C.Signal(str)


# Tool actions that change only this client's presentation. They never wait
# behind an owner command, so continuous Play cannot starve them.
PRESENTATION_ACTIONS = frozenset({'toggle_visibility', 'toggle_editor', 'switch_layout', 'reload_ui'})


class _RateLimitedLog:
    """stderr lines for a repeating failure: the first, then one per interval."""
    def __init__(self, name, interval=10.0):
        self.name, self.interval = name, interval
        self.last = None
        self.when = 0.0
        self.count = 0

    def log(self, exc, *, unexpected=False):
        import sys, traceback
        text = f'{type(exc).__name__}: {exc}'
        now = time.monotonic()
        self.count += 1
        if text != self.last or now - self.when >= self.interval:
            repeat = f' (x{self.count})' if self.count > 1 else ''
            print(f'Stasis: {self.name} failed{repeat}: {text}', file=sys.stderr, flush=True)
            if unexpected:
                traceback.print_exception(exc, file=sys.stderr)
                sys.stderr.flush()
            self.last, self.when, self.count = text, now, 0

    def ok(self):
        if self.last is not None:
            import sys
            print(f'Stasis: {self.name} recovered', file=sys.stderr, flush=True)
        self.last, self.count = None, 0


# PianoModel.data runs per visible cell and role on every paint. Resolve Qt
# enums once: each PySide attribute lookup such as C.Qt.DisplayRole costs ~5 us.
_DISPLAY = C.Qt.ItemDataRole.DisplayRole
_TOOLTIP = C.Qt.ItemDataRole.ToolTipRole
_ACCESSIBLE = C.Qt.ItemDataRole.AccessibleTextRole
_BACKGROUND = C.Qt.ItemDataRole.BackgroundRole
_FOREGROUND = C.Qt.ItemDataRole.ForegroundRole
_ALIGNMENT = C.Qt.ItemDataRole.TextAlignmentRole
_CELL_ROLES = frozenset((_DISPLAY, _TOOLTIP, _ACCESSIBLE, _BACKGROUND, _FOREGROUND, _ALIGNMENT))
_TEXT_ROLES = frozenset((_TOOLTIP, _ACCESSIBLE))
_HORIZONTAL = C.Qt.Orientation.Horizontal
_VERTICAL = C.Qt.Orientation.Vertical
_ALIGN_CENTER = C.Qt.AlignmentFlag.AlignCenter
_CELL_FLAGS = (C.Qt.ItemFlag.ItemIsSelectable | C.Qt.ItemFlag.ItemIsEnabled
               | C.Qt.ItemFlag.ItemNeverHasChildren)
_EDITABLE_CELL_FLAGS = _CELL_FLAGS | C.Qt.ItemFlag.ItemIsEditable
_DRAFT_BACKGROUND = G.QColor('#554b2b')
_CURRENT_BACKGROUND = G.QColor('#286574')
_FUTURE_BACKGROUND = G.QColor('#131f29')
_PAST_BACKGROUND = G.QColor('#080e14')
_DRAFT_FOREGROUND = G.QColor('#f0c46b')
_HELD_FOREGROUND = G.QColor('#8bdfd8')


class PianoModel(C.QAbstractTableModel):
    edited = C.Signal(int)
    editFailed = C.Signal(str)
    def __init__(self, timeline, parent=None):
        super().__init__(parent)
        self.timeline = timeline
        self.explicit_keys = []
        self.keys = self.lane_keys()
        self.origin = 1
        self.width = 64
        self.position_unit = 'Frame'   # 'Tick' only for clock.precision = "tick"
        self.rail_cursor = None  # seek-rail drag preview tick; presentation only
        self.busy = False
        self.acknowledged = None
        self._note_rows = None
        self._note_context = None
        self._note_cache = {}
        self._last_view = None
        self.modelReset.connect(self._forget_view)

    def rowCount(self, parent=C.QModelIndex()):
        return 0 if parent.isValid() else len(self.keys)

    def columnCount(self, parent=C.QModelIndex()):
        return 0 if parent.isValid() else self.width

    def headerData(self, section, orientation, role=_DISPLAY):
        if role == _DISPLAY:
            if orientation == _VERTICAL:
                return self.keys[section]
            tick=self.origin + section
            return ('▼ ' if tick == getattr(self,'presentation_tick',self.timeline.tick) else '') + str(tick)

    def cell_note(self, tick):
        """The note a column presents: draft, presentation, acknowledged or
        recorded. None marks an unavailable presentation column; ValueError a barrier."""
        drafts=self.timeline.drafts
        presentation=getattr(self,'presentation_tick',None)
        if tick not in drafts and tick == presentation and getattr(self,'presentation_unavailable',False):
            return None
        if tick in drafts:
            return self.timeline.note(tick)
        if tick == presentation and getattr(self,'presentation_note',None) is not None:
            return self.presentation_note
        if tick == self.timeline.tick and self.acknowledged is not None:
            return self.acknowledged
        return self.recorded_note(tick)

    def data(self, index, role=_DISPLAY):
        # Hot path: called per visible cell and role on every paint. Roles and
        # colors are module constants; PySide enum attribute lookups cost ~5 us each.
        if role not in _CELL_ROLES or not index.isValid():
            return None
        tick = self.origin + index.column()
        try:
            note = self.cell_note(tick)
        except ValueError:
            return '│' if role == _DISPLAY else None
        if note is None:
            if role in _TEXT_ROLES:
                return f'{self.position_unit} {tick}, {self.keys[index.row()]}: input unavailable'
            return '?' if role == _DISPLAY else None
        if role == _DISPLAY:
            return '●' if self.keys[index.row()] in note.held else ''
        if role == _ALIGNMENT:
            return _ALIGN_CENTER
        drafted = tick in self.timeline.drafts
        if role == _BACKGROUND:
            if drafted:
                return _DRAFT_BACKGROUND
            if tick == getattr(self,'presentation_tick',self.timeline.tick):
                return _CURRENT_BACKGROUND
            return _FUTURE_BACKGROUND if tick > self.timeline.tick else _PAST_BACKGROUND
        if role == _FOREGROUND:
            if drafted:
                return _DRAFT_FOREGROUND
            return _HELD_FOREGROUND if self.keys[index.row()] in note.held else None
        key = self.keys[index.row()]
        state = 'pressed' if key in note.held else 'released'
        return f'{self.position_unit} {tick}, {key}: {state} · {"draft" if drafted else "input"}'

    def recorded_note(self, tick):
        """Resolve each visible recorded column once, rather than for every Qt role.

        Drafts, applied inputs and independent presentation inputs bypass this
        cache. It never supplies an authoring or restore decision.
        """
        rows=self.timeline.rows
        context=(self.timeline.foreign,self.timeline.blocked_from,id(self.timeline.future))
        if rows is not self._note_rows or self._note_context!=context:
            # Every owner refresh delivers a new, usually equal, rows tuple.
            # Only a real change may drop the per-tick cache.
            if self._note_context!=context or self._note_rows is None or rows!=self._note_rows:
                self._note_cache.clear()
            self._note_rows=rows;self._note_context=context
        if tick not in self._note_cache:
            if len(self._note_cache)>=self.width*2:self._note_cache.clear()
            try:self._note_cache[tick]=self.timeline.note(tick)
            except ValueError:self._note_cache[tick]=None
        note=self._note_cache[tick]
        if note is None:raise ValueError('recorded input unavailable')
        return note

    def _column_state(self, tick):
        try:
            note = self.cell_note(tick)
        except ValueError:
            return '│'
        return '?' if note is None else (note.held, tick in self.timeline.drafts)

    def _view_state(self):
        head = (self.origin, tuple(self.keys), self.position_unit,
                getattr(self,'presentation_tick',self.timeline.tick), self.timeline.tick)
        return head, [self._column_state(self.origin + column) for column in range(self.width)]

    def sync_view(self):
        """Repaint only columns whose presented input or shading changed.

        Owner refreshes and preview packets arrive many times a second, mostly
        unchanged; a full dataChanged per arrival kept the grid repainting."""
        state = self._view_state()
        previous, self._last_view = self._last_view, state
        last_column = self.width - 1
        if previous is None or previous[0][:3] != state[0][:3]:
            if self.rowCount():
                self.dataChanged.emit(self.index(0, 0), self.index(self.rowCount() - 1, last_column))
            self.headerDataChanged.emit(_HORIZONTAL, 0, last_column)
            return
        (_, _, _, old_current, old_ack), old_columns = previous
        (_, _, _, current, ack), columns = state
        dirty = [column for column in range(self.width) if columns[column] != old_columns[column]]
        header = []
        if current != old_current:
            for tick in (old_current, current):
                if 0 <= tick - self.origin < self.width:
                    dirty.append(tick - self.origin);header.append(tick - self.origin)
        if ack != old_ack:
            # Past/future shading flips between the old and new acknowledged tick.
            low, high = sorted((old_ack, ack))
            dirty.extend(range(max(0, low - self.origin), min(self.width, high - self.origin + 1)))
        if dirty and self.rowCount():
            # One column of slack: the 2 px playhead line straddles a border.
            self.dataChanged.emit(self.index(0, max(0, min(dirty) - 1)),
                                  self.index(self.rowCount() - 1, min(last_column, max(dirty) + 1)))
        if header:
            self.headerDataChanged.emit(_HORIZONTAL, min(header), max(header))

    def _forget_view(self):
        # A reset repaints everything; record what it shows as the new baseline.
        self._last_view = self._view_state()

    def flags(self, index):
        if not index.isValid():
            return super().flags(index)
        return _CELL_FLAGS if self.busy else _EDITABLE_CELL_FLAGS

    def setData(self, index, value, role=C.Qt.EditRole):
        if role != C.Qt.EditRole or self.busy:
            return False
        tick = self.origin + index.column()
        try:
            held = tuple(self.timeline.note(tick).held)
            key = self.keys[index.row()]
            # Keep the recorded key order so toggling back restores the input.
            pressed = key not in held
            held = held + (key,) if pressed else tuple(k for k in held if k != key)
            self.timeline.edit(tick, label=f'{"Pressed" if pressed else "Released"} {key} at {tick}', held=held)
        except Exception as exc:
            self.editFailed.emit(str(exc))
            return False
        self.dataChanged.emit(index, index)
        self.edited.emit(tick)
        return True

    def lane_keys(self):
        # Score order makes first-use lanes visible without accumulating old branches.
        authored = [key for row in self.timeline.rows if hasattr(row.item, 'held')
                    for key in row.item.held]
        if not self.timeline.foreign:
            authored.extend(key for tick, note in sorted(self.timeline.drafts.items())
                            for key in note.held)
        keys = list(dict.fromkeys(authored + self.explicit_keys))
        return keys or ['w', 'a', 's', 'd', 'space', 'esc', 'enter', 'lshift']

    def add_lane(self, key):
        if key not in self.explicit_keys:
            self.explicit_keys.append(key)
        self.beginResetModel()
        self.keys = self.lane_keys()
        self.endResetModel()

    def refresh(self, *, follow=True):
        origin=max(1,self.timeline.tick-24) if follow else self.origin
        keys=self.lane_keys()
        if origin!=self.origin or keys!=self.keys:
            self.beginResetModel()
            self.origin,self.keys=origin,keys
            self.endResetModel()
        else:
            self.sync_view()


class _PianoHeader(W.QHeaderView):
    def setDefaultSectionSize(self, size):
        # fit_piano_headers runs on every owner refresh. Re-applying the same
        # size still relayouts and repaints the whole grid, so skip no-ops.
        if size != self.defaultSectionSize():
            super().setDefaultSectionSize(size)


class PianoView(W.QTableView):
    def __init__(self, *args):
        super().__init__(*args)
        header = _PianoHeader(C.Qt.Horizontal, self)
        header.setSectionsClickable(True)
        header.setHighlightSections(True)
        self.setHorizontalHeader(header)
        # The seek rail is the one visible horizontal control. The native bar
        # stays as the hidden scroll model for wheel, keyboard and scrollTo.
        self.setHorizontalScrollBarPolicy(C.Qt.ScrollBarAlwaysOff)

    def wheelEvent(self, event):
        # Qt treats Shift+wheel as a vertical page step. Here it browses ticks.
        delta = event.angleDelta()
        if event.modifiers() & C.Qt.ShiftModifier and abs(delta.y()) > abs(delta.x()):
            bar = self.horizontalScrollBar()
            action = (W.QAbstractSlider.SliderSingleStepSub if delta.y() > 0
                      else W.QAbstractSlider.SliderSingleStepAdd)
            for _ in range(max(1, round(abs(delta.y()) / 120 * W.QApplication.wheelScrollLines()))):
                bar.triggerAction(action)
            event.accept()
            return
        super().wheelEvent(event)

    def keyPressEvent(self, event):
        if event.key() in (C.Qt.Key_Space, C.Qt.Key_Return, C.Qt.Key_Enter) and not event.modifiers():
            index = self.currentIndex()
            if index.isValid() and not event.isAutoRepeat():
                self.model().setData(index, True)
            event.accept()
            return
        super().keyPressEvent(event)

    def paintEvent(self,event):
        super().paintEvent(event)
        model=self.model()
        if model is None:return
        painter=G.QPainter(self.viewport())
        try:
            cursor=getattr(model,'rail_cursor',None)
            if cursor is not None and 0<=cursor-model.origin<model.columnCount():
                # Rail drag target: not the playhead until the seek is admitted.
                x=self.columnViewportPosition(cursor-model.origin)+self.columnWidth(cursor-model.origin)//2
                painter.setPen(G.QPen(G.QColor('#ffc66d'),1,C.Qt.DashLine))
                painter.drawLine(x,0,x,self.viewport().height())
            column=getattr(model, 'presentation_tick', model.timeline.tick)-model.origin
            if 0<=column<model.columnCount():
                x=self.columnViewportPosition(column)
                painter.setPen(G.QPen(G.QColor('#19e3c1'),2))
                painter.drawLine(x,0,x,self.viewport().height())
        finally:painter.end()


class OverlayClient(C.QObject):
    """One client and input document with two presentation presets."""
    client_closed = C.Signal()
    def __init__(self, controller, draft_path=None, *, ui_config=None, layout=None, preview_refresh_ms=16):
        super().__init__()
        if type(preview_refresh_ms) is not int or not 1 <= preview_refresh_ms <= 1000:
            raise ValueError("preview_refresh_ms must be an integer from 1 to 1000")
        self.preview_refresh_ms = preview_refresh_ms
        self.controller = controller
        self.timeline = Timeline(draft_path or Path.home() / '.local/state/kptas/workspace-inputs.json')
        self.pending = False
        self.operation = None
        self.deferred_command = None
        self.playing = False
        self.play_sizer = PlayBatchSizer()
        self._play_timing = None  # (before_tick, after_tick, seconds), left by the worker
        self._preview_identity = None  # (session_id, take) of the last preview frame displayed
        self.closed = False
        self._preview_stop = Event()
        self._preview_lock = Lock()
        self._preview_mailbox = None
        self._preview_notification_pending = False
        self.snapshot = None
        self.transfer_from = None
        self.branch_commit = None
        self.draft_failure = False
        self.owner_error = False
        self.awaiting_ack = False
        self.mouse_target = None
        self.mouse_identity = None
        self.restoring_selection = False
        self.explicit_selection = False
        self.manual_scroll = False
        self.manual_vertical_scroll = False
        self.queue = Queue(maxsize=2)
        self.signals = Signals()
        self.signals.result.connect(self.completed)
        self.signals.refresh_view.connect(self.refresh_view_completed)
        self.signals.preview.connect(self.consume_preview)
        self.signals.bootstrap.connect(self.bootstrap_completed)
        self.overlay = Overlay(None, SimpleNamespace(display=None, branch_score=None, ui_config=ui_config, layout=layout),
                               presentation=RemotePresentation())
        self.root = self.overlay.root
        # A view/action adapter of this client, never another owner or window.
        # It reads controls only after construction and present() have finished.
        self.surface = NativeSurface(self)
        self.root.installEventFilter(self)
        self.overlay.remote_action = self.overlay_action
        self.signals.tool_action.connect(self.native_tool_action)
        self.play = self.overlay.play
        self.step = self.overlay.step
        self.counter = self.overlay.tick
        self.seek = self.overlay.slider
        # Recorded future after a live rewind: rail range, marks, forward replay.
        from .timeline_rail import FutureRail
        self.future_rail = FutureRail(self)
        self.follow_action = self.overlay.more_menu.addAction('Follow playhead')
        self.follow_action.setCheckable(True)
        self.follow_action.setChecked(self.overlay.ui_preferences.follow_playhead)
        self.follow_action.setToolTip(f'Keep the current {self._unit()} and its active input lanes in view. Browsing stops following.')
        self.follow_action.toggled.connect(self.set_follow_playhead)
        self.overlay.preferences_applied = self.sync_follow_preferences
        from .mechanic_sequence_widget import MechanicSequenceDialog
        self.sequence_dialog = MechanicSequenceDialog(self.root)
        self.sequence_enabled = False
        self.sequence_dialog.profile_loaded.connect(lambda: setattr(self, 'sequence_enabled', True))
        self.signals.sequence.connect(self.sequence_evidence)
        self.trainer_live = False  # Input trainer wants per-step receipts; never throttles Play.
        trainer = getattr(self.overlay, 'trainer_panel', None)
        if trainer is not None:  # Reads the owner's committed input log, plus receipts as a fallback.
            from .trainer_widget import table_span
            trainer.attach_live(enable=lambda on: setattr(self, 'trainer_live', bool(on)),
                                note_at=lambda tick: self.model.cell_note(tick),
                                selection=lambda: table_span(self.table, self.model.origin),
                                live_address=lambda: (getattr(self.controller.transport, 'host', None),
                                                      getattr(self.controller.transport, 'port', None)))
            self.signals.sequence.connect(trainer.sequence_evidence)
        sequence_action = self.overlay.more_menu.addAction('Sequence file guide…')
        sequence_action.setToolTip('Follow the steps of a mechanic sequence file. For timed combo practice, use Input trainer.')
        def show_sequence():
            self.sequence_dialog.show()
            self.sequence_dialog.raise_()
        sequence_action.triggered.connect(show_sequence)
        from .debugger_widget import DebuggerDialog
        self.debugger_dialog = DebuggerDialog(self.root, self.surface.activate)
        self._debugger_inspection_demand = Event()
        self.debugger_dialog.inspection_demand.connect(
            lambda enabled: self._debugger_inspection_demand.set() if enabled else self._debugger_inspection_demand.clear())
        self.signals.debugger_inspection.connect(self.debugger_dialog.accept_inspection)
        debugger_action = self.overlay.more_menu.addAction('TAS debugger…')
        debugger_action.setToolTip('Inspect confirmed inputs, frame boundaries and run speed')
        def show_debugger():
            self.debugger_dialog.observe(self.snapshot, surface=self.surface.view())
            self.debugger_dialog.show()
            self.debugger_dialog.raise_()
        debugger_action.triggered.connect(show_debugger)
        try:  # More > Memory: read-only watch and search over the take's recorded frames.
            from .memory_widget import install_memory_panel
            self.memory_panel = install_memory_panel(self)
        except Exception as exc:  # The rest of the window works without it.
            self.memory_panel = None
            self.overlay.console.appendPlainText(f'Memory: panel unavailable. {exc}')
        try:  # More > Speedruns: catalog of runs, their saves and scores. No owner ops of its own.
            from .speedrun_widget import install_speedrun_panel
            self.speedrun_panel = install_speedrun_panel(self)
        except Exception as exc:  # The rest of the window works without it.
            self.speedrun_panel = None
            self.overlay.console.appendPlainText(f'Speedruns: list unavailable. {exc}')
        # Live score control. Enablement and labels follow present(). Keys are
        # registered in the central action registry (category 'Live score'),
        # which binds them, applies keymap overrides and resolves conflicts.
        self._score_notice = None
        self.score_menu = self.overlay.more_menu.addMenu('Live score')
        self.score_menu.setToolTipsVisible(True)
        self.score_load_action = self.score_menu.addAction('Load score here…', lambda: self.overlay_action('score_load_here'))
        self.score_load_at_action = self.score_menu.addAction('Load score at…', lambda: self.overlay_action('score_load_at'))
        self.score_pause_action = self.score_menu.addAction('Pause score', lambda: self.overlay_action('score_pause_resume'))
        self.score_unload_action = self.score_menu.addAction('Unload score', lambda: self.overlay_action('score_unload'))
        self._score_menu_items = {'score_load_here': self.score_load_action, 'score_load_at': self.score_load_at_action,
                                  'score_pause_resume': self.score_pause_action, 'score_unload': self.score_unload_action}
        self._score_menu_texts = {}
        try:
            from .actions import register_action
            from .score_control import SCORE_ACTIONS
            for action_id, key, label, description in SCORE_ACTIONS:
                register_action(action_id, label, category='Live score', default=key,
                                callback=lambda action_id=action_id: self.overlay_action(action_id),
                                description=description + ' Works while the Stasis window has focus.')
        except Exception as exc:  # The menu still works without keys.
            self.overlay.console.appendPlainText(f'Live score: shortcuts unavailable. {exc}')
        piano = W.QWidget()
        piano.setObjectName('pianoEditor')
        piano.setAttribute(C.Qt.WA_StyledBackground)
        layout = W.QVBoxLayout(piano)
        layout.setContentsMargins(4,4,4,4)
        layout.setSpacing(4)
        follow_row = W.QHBoxLayout()
        follow_row.setContentsMargins(0,0,0,0)
        self.follow_checkbox = W.QCheckBox('Follow playhead')
        self.follow_checkbox.setChecked(self.follow_action.isChecked())
        self.follow_checkbox.setToolTip(self.follow_action.toolTip())
        self.follow_checkbox.toggled.connect(self.follow_action.setChecked)
        self.follow_action.toggled.connect(self.follow_checkbox.setChecked)
        follow_row.addWidget(self.follow_checkbox)
        follow_row.addStretch()
        layout.addLayout(follow_row)
        self.edit_controls=W.QWidget(piano)
        toolbar = W.QHBoxLayout(self.edit_controls)
        toolbar.setContentsMargins(0,0,0,0)
        toolbar.setSpacing(8)
        self.apply = W.QPushButton('Apply selected')
        toolbar.addWidget(self.apply)
        self.key_choice = W.QComboBox()
        self.key_choice.setEditable(True)
        self.key_choice.addItems(sorted(set(KEYS) | set(SYNONYMS)))
        self.key_choice.setMaximumWidth(110)
        self.key_choice.setCurrentIndex(-1)
        self.key_choice.lineEdit().setPlaceholderText("Key name")
        self.add_key_button = W.QPushButton('Add lane')
        self.add_key_button.clicked.connect(self.add_key)
        toolbar.addWidget(W.QLabel("Key"))
        toolbar.addWidget(self.key_choice)
        toolbar.addWidget(self.add_key_button)
        self.mouse_toggle=W.QToolButton()
        self.mouse_toggle.setText('Mouse edit ▸')
        self.mouse_toggle.setCheckable(True)
        toolbar.addWidget(self.mouse_toggle)
        toolbar.addStretch()
        self.selection_label = W.QLabel()
        self.selection_label.setObjectName("tickSelection")
        self.selection_label.setSizePolicy(W.QSizePolicy.Ignored,W.QSizePolicy.Preferred)
        toolbar.addWidget(self.selection_label)
        layout.addWidget(self.edit_controls)
        self.edit_controls.hide()
        controls_action=self.overlay.more_menu.addAction('Editing controls')
        controls_action.setCheckable(True)
        def show_edit_controls(visible):
            self.edit_controls.setVisible(visible)
            if not visible:self.mouse_toggle.setChecked(False)
            self.overlay._layout_surface(self.root.width(),self.root.height())
        controls_action.toggled.connect(show_edit_controls)
        self.model = PianoModel(self.timeline, self)
        self.table = PianoView()
        self.table.setObjectName("pianoRoll")
        self.table.setAccessibleName('TAS input timeline')
        self.table.setAccessibleDescription(f'{self._unit(plural=True, title=True)} run from left to right. Each row is a key. Arrow keys select a cell; Space, Enter or double-click toggles its draft input. Control Z undoes a draft edit.')
        self.table.setProperty("uiRole", "pianoRoll")
        self.table.setModel(self.model)
        self.table.horizontalHeader().setDefaultSectionSize(30)
        self.table.verticalHeader().setDefaultSectionSize(21)
        self.table.setMaximumHeight(16777215)
        self.table.setMinimumHeight(40)
        self.table.setSizePolicy(W.QSizePolicy.Expanding, W.QSizePolicy.Expanding)
        self.table.horizontalScrollBar().sliderPressed.connect(lambda: self.browse_inputs(vertical=False))
        self.table.horizontalScrollBar().actionTriggered.connect(lambda _: self.browse_inputs(vertical=False))
        self.table.verticalScrollBar().sliderPressed.connect(lambda: self.browse_inputs(vertical=True))
        self.table.verticalScrollBar().actionTriggered.connect(lambda _: self.browse_inputs(vertical=True))
        self.table.doubleClicked.connect(lambda index: self.model.setData(index, True))
        self.table.setEditTriggers(W.QAbstractItemView.NoEditTriggers)
        self.undo_action = G.QAction('Undo input edit', self.table)
        self.redo_action = G.QAction('Redo input edit', self.table)
        self.undo_action.setShortcut(G.QKeySequence(G.QKeySequence.StandardKey.Undo))
        self.redo_action.setShortcuts([G.QKeySequence('Ctrl+Shift+Z'), G.QKeySequence('Ctrl+Y')])
        for action in (self.undo_action, self.redo_action):
            action.setShortcutContext(C.Qt.WidgetWithChildrenShortcut)
            action.setEnabled(False)
            self.table.addAction(action)
            self.overlay.more_menu.addAction(action)
        self.undo_action.triggered.connect(lambda: self.undo_draft(redo=False))
        self.redo_action.triggered.connect(lambda: self.undo_draft(redo=True))
        self.save_inputs_action = G.QAction('Save input edits', self.table)
        self.save_inputs_action.setShortcut(G.QKeySequence(G.QKeySequence.StandardKey.Save))
        self.save_inputs_action.setShortcutContext(C.Qt.WidgetShortcut)
        self.save_inputs_action.setAutoRepeat(False)
        self.save_inputs_action.triggered.connect(self.overlay.save_button.click)
        self.table.addAction(self.save_inputs_action)
        self.model.edited.connect(lambda _: self.present())
        self.model.editFailed.connect(self.report_input_edit_failure)
        layout.addWidget(self.table)
        self.mouse_editor=W.QWidget(piano)
        mouse_layout=W.QVBoxLayout(self.mouse_editor);mouse_layout.setContentsMargins(0,0,0,0);mouse_layout.setSpacing(8)
        layout.addWidget(self.mouse_editor)
        mouse = W.QHBoxLayout()
        mouse.setSpacing(8)
        self.x = W.QSpinBox(); self.y = W.QSpinBox()
        for spin in (self.x, self.y):
            spin.setRange(-32768, 32767);spin.setMaximumWidth(76)
        self.x.setPrefix('X ');self.y.setPrefix('Y ')
        self.mouse_mode = W.QComboBox(); self.mouse_mode.addItems(['No movement', 'Relative', 'Position'])
        self.mouse_mode.setMaximumWidth(100)
        self.mouse_mode.setToolTip('Relative movement or absolute pointer position')
        self.mouse_mode.currentIndexChanged.connect(self.mouse_mode_changed)
        self.left = W.QCheckBox('Left'); self.right = W.QCheckBox('Right'); self.middle = W.QCheckBox('Middle')
        self.button_boxes = dict(zip(BUTTONS, (self.left, self.right, self.middle)))
        self.wheel = W.QSpinBox(); self.wheel.setRange(-32768, 32767); self.wheel.setPrefix('Wheel ');self.wheel.setMaximumWidth(90)
        for widget in (W.QLabel('Mouse'), self.mouse_mode, self.x, self.y, self.wheel):mouse.addWidget(widget)
        mouse.addStretch();mouse_layout.addLayout(mouse)
        buttons = W.QHBoxLayout();buttons.addWidget(W.QLabel('Buttons'))
        self.mouse_save = W.QPushButton('Save mouse edit')
        for widget in (self.left, self.middle, self.right):buttons.addWidget(widget)
        buttons.setSpacing(8)
        buttons.addStretch();buttons.addWidget(self.mouse_save);mouse_layout.addLayout(buttons)
        self.mouse_editor.hide()
        def reveal_mouse(visible):
            self.mouse_editor.setVisible(visible)
            self.mouse_toggle.setText('Mouse edit ▾' if visible else 'Mouse edit ▸')
            self.overlay._layout_surface(self.root.width(),self.root.height())
        self.mouse_toggle.toggled.connect(reveal_mouse)
        self.error = self.overlay.hint
        self.overlay.editor_pages.removeWidget(self.overlay.roll)
        self.overlay.roll.deleteLater()
        self.overlay.editor_pages.insertWidget(0,piano)
        self.overlay.editor_pages.setCurrentIndex(0)
        self.overlay.roll = self.table
        sizing = self.overlay.ui_preferences.sizing
        self.table.setMaximumHeight(16777215)
        self.overlay.fit_piano_headers()
        self.table.verticalHeader().setMinimumSectionSize(14)
        self.table.verticalHeader().setDefaultSectionSize(min(18,sizing.row_height))
        self.apply.clicked.connect(self.apply_selected)
        self.mouse_save.clicked.connect(self.edit_mouse)
        self.table.selectionModel().currentChanged.connect(self.load_selected_mouse)
        # The seek rail is the piano's horizontal navigator, synced both ways.
        self._rail_view_timer = C.QTimer(self)
        self._rail_view_timer.setSingleShot(True)
        self._rail_view_timer.timeout.connect(self.sync_rail_view)
        grid_bar = self.table.horizontalScrollBar()
        grid_bar.valueChanged.connect(self.schedule_rail_view)
        grid_bar.rangeChanged.connect(self.schedule_rail_view)
        self.table.horizontalHeader().sectionResized.connect(self.schedule_rail_view)
        self.model.modelReset.connect(self.schedule_rail_view)
        self.model.layoutChanged.connect(self.schedule_rail_view)
        self.table.installEventFilter(self)
        self.seek.cursorMoved.connect(self.rail_cursor_moved)
        self.seek.seekRefused.connect(self.rail_seek_refused)
        self.seek.sliderReleased.connect(self.clear_rail_cursor)
        # Must connect after the overlay's own seekRequested -> queue('seek')
        # slot: rail_seek_requested inspects whether that submit was admitted.
        self.seek.seekRequested.connect(self.rail_seek_requested)
        from .recording_library import RecordingLibrary
        self.recording_library=RecordingLibrary()
        self.recording_recovery=None
        self.overlay.recording_combo.currentIndexChanged.connect(lambda *_: self.present())
        self.overlay.initialize_panel_controls()
        self.refresh_recordings()
        self.worker_thread = Thread(target=self.worker, daemon=True)
        self.worker_thread.start()
        self._preview_requested = self.preview_requested(self.overlay.ui_preferences)
        self.preview_thread = Thread(target=self.preview_worker, daemon=True)
        self.preview_thread.start()
        self.timer = C.QTimer(self); self.timer.timeout.connect(self.poll_paused)
        # Presentation frames arrive separately. Idle canonical polls need not
        # repeatedly encode/download coherent PNGs at the presentation rate.
        # Playback and explicit controls still dispatch immediately through the
        # single worker and keep their existing acknowledgement checks.
        self.timer.start(max(100,self.preview_refresh_ms))
        self.playback_timer = C.QTimer(self)
        self.playback_timer.setSingleShot(True)
        self.playback_timer.timeout.connect(self.playback_advance)
        from .shortcut_binder import attach_workspace
        attach_workspace(self)  # Input table keys and saved playback pacing; never raises.
        from .timeline_tools import install as install_timeline_tools
        # Edit history, bookmarks, range edits, read-only, hold-to-advance; never raises.
        self.timeline_tools = install_timeline_tools(self)
        self.submit('refresh')

    def preview_worker(self):
        from .preview_channel import PreviewReadCache, read_preview
        import time
        transport = self.controller.transport
        host, port = getattr(transport, 'host', None), getattr(transport, 'port', None)
        if host is None or port is None:
            return
        previous = None
        cache = PreviewReadCache()
        last_change=last_emit=time.monotonic()
        failures = _RateLimitedLog('preview channel')
        while not self.closed:
            if not self._preview_requested:
                # A native/hidden surface needs canonical state, not PNGs.
                # Reset deduplication so reopening presents even unchanged data.
                previous = None
                cache = PreviewReadCache()
                if self._preview_stop.wait(.1):
                    return
                continue
            delay=max(.016,self.preview_refresh_ms/1000)
            try:
                packet = read_preview(host, port + 1, cache=cache)
                if packet is not None:
                    # Keep malformed responses off the GUI thread. Equality
                    # includes pixels and metadata, not only tick/generation.
                    if not isinstance(packet, dict):
                        raise ValueError('preview packet must be an object')
                    for key in ('session_id', 'take', 'generation', 'epoch', 'tick', 'png', 'inputs'):
                        packet[key]
                # While paused the owner keeps serving one unchanged packet. If
                # the GUI dropped its displayed frame (an error, a layout change),
                # offer the same packet again instead of leaving "Frame unavailable".
                resend = (packet is not None and getattr(self, '_display_packet', None) is None
                          and time.monotonic()-last_emit > .5)
                if packet != previous or resend:
                    with self._preview_lock:
                        self._preview_mailbox = packet
                        notify = not self._preview_notification_pending
                        self._preview_notification_pending = True
                    if notify:
                        self.signals.preview.emit(None)
                    if packet != previous:
                        last_change=time.monotonic()
                    previous = packet
                    last_emit=time.monotonic()
                elif time.monotonic()-last_change>.25:
                    delay=max(delay,.1)
                failures.ok()
            except (OSError, ValueError, KeyError) as exc:
                # Older owners keep their coherent command-result preview.
                failures.log(exc)
                delay=max(delay,.1)
            except Exception as exc:
                # Never let an unexpected packet kill the preview thread silently.
                failures.log(exc, unexpected=True)
                delay=max(delay,.5)
            if self._preview_stop.wait(delay):
                return

    @C.Slot(object)
    def consume_preview(self, _notification):
        # Only presentation is coalesced. Canonical ACK/error signals keep their
        # serialized path. A producer arriving after this lock queues a new wakeup.
        with self._preview_lock:
            packet = self._preview_mailbox
            self._preview_mailbox = None
            self._preview_notification_pending = False
        self.preview_completed(packet)

    def decoded_pixmap(self, png):
        """Share one exact-byte image decode across both presentation channels."""
        cached = getattr(self, '_decoded_pixmap', None)
        if cached is not None and cached[0] == png:
            return cached[1]
        pixmap = G.QPixmap()
        if not pixmap.loadFromData(png, 'PNG'):
            return None
        self._decoded_pixmap = (png, pixmap)
        return pixmap

    @C.Slot(object)
    def preview_completed(self, packet):
        import base64
        s = self.snapshot
        if self.closed or s is None or s.error or s.unknown_state or self.owner_error:
            return
        if packet is None:
            # Invalidation is an operation boundary, not an invalidation of the
            # previously captured image. Hold its existing tick/provenance until
            # the coherent owner result supplies the restored gate. In particular,
            # a refresh waiting behind an external rewind must not blank the game.
            return
        token = s.owner_token or {}
        if packet['session_id'] != token.get('session_id') or packet['take'] != s.take_path:
            return
        if not self.overlay.frame_provenance_matches(packet.get('tick'),packet):
            return
        epoch=(s.frame_metadata or {}).get('epoch')
        if type(epoch) is int and packet.get('epoch') != epoch:
            return
        # Queued presentation must never regress a newer canonical acknowledgement
        # or a later packet from this generation. A verified restore has a new
        # take/generation and is allowed to move the display backwards.
        identity = (packet['session_id'], packet['take'])
        generation, tick = packet['generation'], packet['tick']
        last = getattr(self, '_preview_fence', None)
        if tick < s.tick:
            return
        if last is not None and last[0] == identity:
            if generation < last[1] or (generation == last[1] and tick < last[2]):
                return
        if self.pending and self.operation in ('seek', 'branch', 'load_recording', 'reconcile'):
            return
        prefs = self.overlay.ui_preferences
        if not ((prefs.layout == 'workspace' and prefs.panels.preview)
                or (prefs.layout == 'overlay' and prefs.game_surface == 'owner_frame')):
            return
        pixmap=getattr(self,'_display_pixmap',None) if packet is getattr(self,'_display_packet',None) else None
        if pixmap is None:
            try:
                png = base64.b64decode(packet['png'], validate=True)
            except (TypeError, ValueError):
                return
            pixmap = self.decoded_pixmap(png)
            if pixmap is None:
                return
        tick = packet['tick']
        metadata = {k: v for k, v in packet.items() if k not in ('png', 'inputs')}
        if not self.overlay.set_workspace_frame(pixmap,tick,metadata):return
        self._preview_identity = (packet['session_id'], packet['take'])
        self._display_packet=packet
        self._display_pixmap=pixmap
        self._preview_fence = (identity, generation, tick)
        inputs = packet['inputs']
        keys, buttons = inputs.get('held', []), inputs.get('buttons', [])
        input_layout_changed=self.overlay._render_input_keys(True, tuple(dict.fromkeys(keys + buttons)))
        if input_layout_changed:self.overlay._layout_surface(self.root.width(),self.root.height())
        self.overlay._input_metadata = dict(tick=tick, highlight_source='owner_applied',
                                           highlight_keys=keys, highlight_buttons=buttons,
                                           live_input=None, view_info=metadata)
        mouse = inputs.get('mouse_abs') or inputs.get('mouse_rel')
        self.overlay.live_keys.setText('Mouse ' + ', '.join(map(str, mouse)) if mouse else '')
        self.overlay.show_input_motion(self.overlay.live_keys.text())
        self.overlay.tick.setText(f'{self._unit(title=True)} {tick}')
        self.counter.setText(f'{self._unit(title=True)} {tick}')
        self.selection_label.setText(f'Current {tick} · Selected {self.selected_tick()}')
        if not self.seek.isSliderDown():
            self.seek.setRange(0,max(self.overlay.seek_extent(s),tick))
            self.seek.setValue(tick)
        if tick>s.tick:
            # This captured gate is visible, but durable authoring authority
            # still waits for the command channel's canonical owner ACK.
            self.seek.set_seek_enabled(False)
        # The display playhead can advance during a Note. Authoring, seek limits,
        # and compare-and-swap admission continue to use the last canonical ACK.
        from kptas.score import Note
        self.model.presentation_tick = tick
        self.model.presentation_unavailable = False
        self.model.presentation_note = Note(1, held=tuple(keys), buttons=tuple(buttons))
        missing = [key for key in keys if key not in self.model.keys]
        if missing or (self.follow_action.isChecked() and not self.manual_scroll and tick >= self.model.origin + self.model.width - 8):
            vertical=self.table.verticalScrollBar().value()
            self.restoring_selection = True
            try:
                self.model.beginResetModel()
                self.model.keys = list(dict.fromkeys(self.model.keys + missing))
                if self.follow_action.isChecked() and not self.manual_scroll:
                    self.model.origin = max(1, tick - 24)
                self.model.endResetModel()
            finally:
                self.restoring_selection = False
            if missing:self.overlay._layout_surface(self.root.width(),self.root.height())
            if self.manual_vertical_scroll:self.table.verticalScrollBar().setValue(vertical)
        self.model.sync_view()  # repaint changed columns only, not per packet
        self.follow_active_keys(keys,tick)
        if self.awaiting_owner_ack():
            self.overlay.state.setText('Playing' if self.playing else 'Advancing…')
            self.overlay.state.setToolTip('Waiting for the game to confirm. Other commands wait until then.')
            self.block_owner_mutations()
            # Publish the first preview-induced admission change to an open
            # debugger; later preview frames do not become history/ACKs or
            # force per-frame debugger redraws. Canonical present clears it.
            latest = self.debugger_dialog.latest
            if self.debugger_dialog.isVisible() and (latest is None or not latest[1].awaiting_ack):
                self.debugger_dialog.observe(self.snapshot, surface=self.surface.view())

    def follow_active_keys(self,keys,tick):
        """Follow actual held lanes without changing their order or user selection."""
        if not self.follow_action.isChecked() or self.explicit_selection or self.manual_scroll:return
        column=tick-self.model.origin
        if not 0<=column<self.model.width:return
        # Horizontal follow must not accidentally return vertical position to row0.
        visible_row=self.table.rowAt(max(0,self.table.viewport().height()//2))
        visible_row=max(0,visible_row)
        vertical=self.table.verticalScrollBar().value()
        self.table.scrollTo(self.model.index(visible_row,column),W.QAbstractItemView.EnsureVisible)
        if self.manual_vertical_scroll:
            self.table.verticalScrollBar().setValue(vertical)
            return
        rows=sorted({self.model.keys.index(key) for key in keys if key in self.model.keys})
        if not rows:return
        height=self.table.viewport().height()
        top=self.table.rowViewportPosition(rows[0])
        bottom=self.table.rowViewportPosition(rows[-1])+self.table.rowHeight(rows[-1])
        if top>=0 and bottom<=height:return
        if sum(self.table.rowHeight(row) for row in range(rows[0],rows[-1]+1))<=height:
            self.table.scrollTo(self.model.index(rows[-1],column),W.QAbstractItemView.EnsureVisible)
            self.table.scrollTo(self.model.index(rows[0],column),W.QAbstractItemView.EnsureVisible)
        else:
            self.table.scrollTo(self.model.index(rows[0],column),W.QAbstractItemView.EnsureVisible)

    def sync_follow_preferences(self, preferences):
        self._preview_requested = self.preview_requested(preferences)
        self.follow_action.setChecked(preferences.follow_playhead)

    @staticmethod
    def preview_requested(preferences):
        return ((preferences.layout == 'workspace' and preferences.panels.preview)
                or (preferences.layout == 'overlay' and preferences.game_surface == 'owner_frame'))

    def set_follow_playhead(self, enabled):
        from dataclasses import replace
        self.overlay.ui_preferences = replace(self.overlay.ui_preferences, follow_playhead=enabled)
        if not enabled or not hasattr(self, 'model'):
            return
        self.explicit_selection = False
        self.manual_scroll = False
        self.manual_vertical_scroll = False
        tick = getattr(self.model, 'presentation_tick', self.timeline.tick)
        column = tick - self.model.origin
        if not 0 <= column < self.model.width:
            self.restoring_selection = True
            try:
                self.model.beginResetModel()
                self.model.origin = max(1, tick - 24)
                self.model.endResetModel()
            finally:
                self.restoring_selection = False
        note = getattr(self.model, 'presentation_note', None) or self.model.acknowledged
        self.follow_active_keys(getattr(note, 'held', ()), tick)

    def browse_inputs(self, *, vertical):
        if vertical:
            self.manual_vertical_scroll = True
        else:
            self.manual_scroll = True
        self.follow_action.setChecked(False)

    def schedule_rail_view(self, *_):
        if not self.closed:
            self._rail_view_timer.start(0)

    def visible_grid_columns(self):
        width = self.table.viewport().width()
        first = self.table.columnAt(0)
        if width <= 0 or first < 0:
            return None
        last = self.table.columnAt(width - 1)
        return first, last if last >= 0 else self.model.columnCount() - 1

    def sync_rail_view(self):
        """Mirror the piano viewport onto the seek rail as its view band."""
        if self.closed:
            return
        columns = self.visible_grid_columns() if self.table.isVisible() else None
        if columns is None:
            self.seek.set_view_window(None)
        else:
            self.seek.set_view_window(self.model.origin + columns[0], self.model.origin + columns[1])

    def show_grid_tick(self, tick, *, hint=W.QAbstractItemView.PositionAtCenter):
        """Window and scroll the piano so tick is visible. Lanes, vertical
        position and an in-window selection stay; nothing is sent to the owner."""
        model = self.model
        tick = max(1, int(tick))
        columns = self.visible_grid_columns()
        visible = (columns[1] - columns[0] + 1 if columns else
                   self.table.viewport().width() // max(1, self.table.horizontalHeader().defaultSectionSize()))
        visible = max(1, min(model.width, visible))
        start = max(1, tick - (visible // 2 if hint == W.QAbstractItemView.PositionAtCenter else 0))
        vertical = self.table.verticalScrollBar().value()
        if not (model.origin <= start and start + visible <= model.origin + model.width):
            selected = self.table.currentIndex()
            row = selected.row()
            selected_tick = model.origin + selected.column() if selected.isValid() else None
            origin = max(1, start - (model.width - visible) // 2)
            self.restoring_selection = True
            try:
                model.beginResetModel()
                model.origin = origin
                model.endResetModel()
                if selected_tick is not None and 0 <= selected_tick - origin < model.width:
                    self.table.setCurrentIndex(model.index(row, selected_tick - origin))
            finally:
                self.restoring_selection = False
            self.overlay.fit_piano_headers()
        if model.rowCount() and 0 <= tick - model.origin < model.width:
            row = max(0, self.table.rowAt(max(0, self.table.viewport().height() // 2)))
            self.table.scrollTo(model.index(min(row, model.rowCount() - 1), tick - model.origin), hint)
        self.table.verticalScrollBar().setValue(vertical)
        self.table.viewport().update()

    def rail_cursor_moved(self, tick):
        """Rail press/drag previews the piano position; the owner seeks on release."""
        self.model.rail_cursor = tick
        if self.table.isVisible():
            # Same rule as grid scrolling: manual browsing stops following.
            self.browse_inputs(vertical=False)
            self.show_grid_tick(tick)
        else:
            self.table.viewport().update()

    def clear_rail_cursor(self):
        self.model.rail_cursor = None
        self.table.viewport().update()

    def rail_seek_requested(self, tick):
        # Runs after the overlay slot that submitted queue('seek', tick).
        admitted = ((self.pending and self.operation == 'seek')
                    or (self.deferred_command is not None and self.deferred_command[0] == 'seek'
                        and self.deferred_command[1] == tick)
                    or self.future_rail.admitted(tick))  # forward replay
        if not admitted:
            self.rail_seek_refused(None)
            if self.snapshot is not None:
                self.seek.setValue(self.snapshot.tick)
            return
        # submit() -> present() released the handle and reset it to the old
        # playhead. Show the pending target; the seek result replaces it.
        self.seek.setValue(tick)
        if self.table.isVisible() and (self.manual_scroll or not self.follow_action.isChecked()):
            self.show_grid_tick(tick)

    def seek_refusal_reason(self):
        """The first unmet seek admission rule, in present()'s order."""
        s = self.snapshot
        if s is None:
            return 'wait for the game to connect'
        if self.owner_run_active():
            return 'stop or unload the score first'
        if self.mutation_pending():
            if self.awaiting_owner_ack():
                return 'wait for the game to confirm the last frame'
            return 'pause playback first' if self.playing else 'wait for the current command to finish'
        if self.owner_error or s.error or s.unknown_state:
            return 'Stasis lost track of the game. Try More > Reconnect. If it is greyed out, restart Stasis'
        if self.draft_failure:
            return 'resolve the input draft error first'
        if self.timeline.foreign:
            return 'these inputs belong to another recording'
        if getattr(s, 'engaged', None) is False:
            return 'engage TAS first'
        if self.overlay.seek_extent(s) <= 0:
            return 'nothing is recorded yet'
        return ''

    def disabled_reason(self):
        """One sentence for tooltips of controls disabled by the error or busy state."""
        reason = self.seek_refusal_reason()
        if not reason or reason == 'nothing is recorded yet':
            return ''
        reason = reason[0].upper() + reason[1:]
        return reason if reason.endswith('.') else reason + '.'

    def rail_seek_refused(self, position=None):
        reason = self.seek_refusal_reason() or self.error.text() or 'the game did not accept this jump'
        message = 'Seek unavailable: ' + reason
        if position is None:
            position = self.seek.mapToGlobal(self.seek.rect().center())
        W.QToolTip.showText(position, message, self.seek)
        # Repeated refused clicks: one console line per 3 s, repeats counted in the status line.
        now = time.monotonic()
        last = getattr(self, '_seek_refusal_last', None)
        if last is not None and last[0] == message and now - last[1] < 3.0:
            count = last[2] + 1
            self._seek_refusal_last = (message, last[1], count)
        else:
            count = 1
            self._seek_refusal_last = (message, now, count)
        shown = message if count == 1 else f'{message} (×{count})'
        notice = getattr(self.overlay, 'set_notice', None)
        if callable(notice):
            notice('Seek unavailable' if count == 1 else f'Seek unavailable (×{count})', shown,
                   ms=3000, console=count == 1)
        elif count == 1:
            self.overlay.console.appendPlainText(message)
        # Not self.error/overlay.hint: that label is the session-error channel.

    def undo_draft(self, *, redo):
        if self.model.busy or self.owner_error or self.draft_failure or self.timeline.foreign:
            return
        try:
            tick = self.timeline.redo_edit() if redo else self.timeline.undo_edit()
            if tick is not None:
                if tick == self.mouse_target:
                    self.refresh_selected_mouse()
                self.table.viewport().update()
                self.show_notice(f'Edit {"redone" if redo else "undone"}',
                                 f'Input edit {"redone" if redo else "undone"} at {self._unit()} {tick}')
            self.present()
        except Exception as exc:
            self.error.setText(str(exc))

    def report_input_edit_failure(self, message):
        self.playing = False
        self.playback_timer.stop()
        self.error.setText('Input edit refused: ' + message)
        self.overlay.console.appendPlainText(self.error.text())
        self.present()

    def show_notice(self, label, detail='', *, error=False):
        """Local action result. The error label is the owner-session error channel
        (it drives the "Session error" chip and is reset by every refresh), so
        successes and local failures go through the overlay notice instead."""
        notice = getattr(self.overlay, 'set_notice', None)
        if callable(notice):
            notice(label, detail, error=error, ms=10000 if error else 5000)
        else:
            self.overlay.console.appendPlainText(detail or label)

    def save_drafts(self):
        """Publish the input draft file. Report success only after it is on disk."""
        path = self.timeline.draft_path
        if path is None:
            self.show_notice('Inputs not saved', 'Inputs not saved: this window has no draft file.', error=True)
            return False
        try:
            self.timeline.save()
        except BlockingIOError:
            self.show_notice('Inputs not saved', 'Inputs not saved: another TAS window is saving '
                             f'{Path(path).name} right now. Try again.', error=True)
            return False
        except Exception as exc:
            self.show_notice('Inputs not saved', f'Inputs not saved: {exc}', error=True)
            return False
        finally:
            self.present()
        count = len(self.timeline.drafts)
        self.show_notice('Inputs saved', f'Saved {count} draft input{"s" if count != 1 else ""} to {path}')
        return True

    def retained_preview(self):
        packet=getattr(self,'_display_packet',None);s=self.snapshot
        if packet is None or s is None or self.owner_error or s.error or s.unknown_state:
            return None
        if not self.overlay.frame_provenance_matches(packet.get('tick'),packet):return None
        prefs=self.overlay.ui_preferences
        if not ((prefs.layout=='workspace' and prefs.panels.preview)
                or (prefs.layout=='overlay' and prefs.game_surface=='owner_frame')):
            return None
        token=s.owner_token or {};epoch=(s.frame_metadata or {}).get('epoch')
        if (packet['session_id']!=token.get('session_id') or packet['take']!=s.take_path
                or packet['tick']<s.tick or (type(epoch) is int and packet.get('epoch')!=epoch)
                or (self.pending and self.operation in ('seek','branch','load_recording','reconcile'))):
            return None
        return packet

    def toggle_play(self):
        self.future_rail.cancel()  # Play/Pause ends any forward replay target
        run=getattr(self.snapshot,'run',None)
        if run is not None and run.active and 'run_control' in self.snapshot.capabilities:
            # Play/Pause pauses or continues a loaded score; Stop stays separate.
            self.submit('run_resume' if run.paused else 'run_pause', run.run_id, ui_execution=False)
            return
        if self.can_stop_run():
            self.submit('run_cancel', ui_execution=False)
            return
        if self.playing:
            self.playing=False
            self.playback_timer.stop()
            if self.deferred_command is not None and self.deferred_command[0]=='step':
                self.deferred_command=None
            self.present()
            return
        if self.mutation_pending() or self.owner_error or self.draft_failure or self.timeline.foreign or self.snapshot is None or self.snapshot.unknown_state or self.snapshot.error:
            return
        self.playing = not self.playing
        self.play.setText('Ⅱ Pause' if self.playing else '▶ Play')
        if not self.step_next(ui_execution=True, play=True):
            self.playing = False
            self.present()

    # ---------------------------------------------------------------- live score control
    def _loaded_run(self, *, quiet=False):
        """The owner's loaded score, or None with a plain notice."""
        s = self.snapshot
        run = getattr(s, 'run', None)
        if s is None or 'run_control' not in s.capabilities:
            if not quiet:
                self.show_notice('Score control unavailable',
                                 'This session cannot pause or swap scores. Restart Stasis to use the current version.', error=True)
            return None
        if run is None or not run.active:
            if not quiet:
                self.show_notice('No score loaded', 'Load a score first.')
            return None
        return run

    def score_pause_resume(self):
        run = self._loaded_run()
        if run is None:
            return False
        return self.submit('run_resume' if run.paused else 'run_pause', run.run_id, ui_execution=False)

    def score_unload(self):
        s = self.snapshot
        run = getattr(s, 'run', None)
        if run is None or 'run_control' not in s.capabilities:
            return bool(self._loaded_run())
        return self.submit('run_unload', run.run_id, ui_execution=False)

    def score_load(self, *, choose_frame=False, path=None, tick=None):
        """Load a score at the current frame, or at a chosen earlier frame.

        A playing score pauses first, so the new one starts where the user
        asked, not where the game is after the file dialog closes.
        """
        s = self.snapshot
        if s is None or 'run_control' not in s.capabilities:
            self.show_notice('Score loading unavailable',
                             'Scores load live only into a live recording with frame precision.', error=True)
            return False
        if s.unknown_state or s.error:
            self.show_notice('Score not loaded', 'Reconnect before you load a score.', error=True)
            return False
        if self.playing:
            self.show_notice('Score not loaded', 'Pause playback before you load a score.', error=True)
            return False
        run = getattr(s, 'run', None)
        if run is not None and run.playing and path is None:
            self.submit('run_pause', run.run_id, ui_execution=False)
        unit = self._unit()
        if choose_frame and tick is None:
            last = self.snapshot.tick
            tick, ok = W.QInputDialog.getInt(self.root, f'Load score at {unit}',
                                             f'Start the score at {unit} 0 to {last}.', last, 0, last)
            if not ok:
                return False
        if tick is not None and (type(tick) is not int or not 0 <= tick <= self.snapshot.tick):
            self.show_notice('Score not loaded', f'Choose a {unit} from 0 to {self.snapshot.tick}.', error=True)
            return False
        if path is None:
            start = getattr(self, '_score_directory', None) or str(Path.home())
            path, _ = W.QFileDialog.getOpenFileName(self.root, 'Load score', start, 'Scores (*.score);;All files (*)')
            if not path:
                return False
        from .score_control import configured_include_dirs, load_run_score
        try:
            loaded = load_run_score(path, configured_include_dirs())
        except ValueError as exc:
            self.show_notice('Score not loaded', str(exc), error=True)
            return False
        self._score_directory = str(Path(path).parent)
        payload = {'score': loaded, 'at': tick}
        if self.pending and self.operation == 'run_pause' and self.deferred_command is None:
            # The pause sent before the file dialog is still in flight: queue the load behind it.
            self.deferred_command = ('score_load', payload, None, False, False)
            self.present()
            return True
        return self.submit('score_load', payload, ui_execution=False)

    def _keep_remainder(self, receipt):
        """Worker thread: save a score's unexecuted inputs; returns (sentence, failed)."""
        text, frames = receipt.get('remainder'), receipt.get('remaining_frames')
        if not text:
            return ' Nothing was left to play.', False
        from .score_control import remainder_directory, save_remainder
        try:
            path = save_remainder(remainder_directory(self.timeline.draft_path), receipt.get('name'),
                                  receipt.get('run_id'), receipt.get('tick'), text, self._unit())
        except OSError as exc:
            return f' Its remaining inputs could not be saved: {exc}.', True
        self._last_remainder_path = str(path)
        return f' Its other {frames} {self._unit(plural=frames != 1)} are saved in {path}.', False

    def _score_receipt_notice(self, operation, receipt):
        """Worker thread: describe a pause, resume or unload receipt for completed()."""
        unit = self._unit()
        name = receipt.get('name') or 'The score'
        status, at = receipt.get('status'), receipt.get('acknowledged_tick')
        done, total = receipt.get('executed_frames'), receipt.get('total_frames')
        if operation == 'run_pause':
            self._score_notice = (('Score paused', f'{name} paused at {unit} {at}, after {done} of {total}.', False)
                                  if status == 'paused' else
                                  ('Score not paused', f'{name} has already ended.', False))
        elif operation == 'run_resume':
            self._score_notice = (('Score resumed', f'{name} continues from {unit} {at}.', False)
                                  if status in ('queued', 'running') else
                                  ('Score not resumed', f'{name} is not paused.', False))
        else:
            sentence, failed = self._keep_remainder(receipt)
            self._score_notice = ('Score unloaded', f'{name} unloaded at {unit} {at}.' + sentence, failed)

    def _score_load(self, value):
        """Worker thread: [pause ->] [unload -> seek ->] start. Returns the final snapshot.

        Loading here replaces a paused score in one owner operation; a refusal
        leaves it paused. Loading at an earlier frame unloads it first (its
        remainder is saved), then seeks with the ordinary rewind machinery.
        """
        loaded, at = value['score'], value.get('at')
        unit = self._unit()
        parts, failed = [], False
        snap = self.controller.snapshot()
        run = snap.run if snap.run is not None and snap.run.active else None
        if run is not None and not run.paused:
            self.controller.run_command('run_pause', run.run_id, expected=snap)
            snap = self.controller.snapshot()
            run = snap.run if snap.run is not None and snap.run.active else None
        replace = None
        if run is not None:
            if not run.paused:
                raise ValueError('The loaded score did not pause. Try again.')
            if at is None or at == snap.tick:
                replace = run.run_id
            else:
                receipt = self.controller.run_command('run_unload', run.run_id, expected=snap)
                sentence, bad = self._keep_remainder(receipt)
                failed = failed or bad
                parts.append(f'{receipt.get("name") or "The previous score"} was unloaded at {unit} {receipt.get("tick")}.' + sentence)
                self._score_notice = ('Score unloaded', ' '.join(parts), failed)
                snap = self.controller.snapshot()
        if at is not None and at != snap.tick:
            snap = self.controller.seek(at, expected=snap)
        result = self.controller.start_run(loaded.text, expected=snap, name=loaded.name, replace=replace)
        parts.insert(0, f'Loaded {loaded.name} at {unit} {result.get("start_tick")}, '
                        f'{loaded.frames} {self._unit(plural=loaded.frames != 1)} long.')
        self._score_notice = ('Score loaded', ' '.join(parts), failed)
        replaced = result.get('replaced')
        if replace is not None and not isinstance(replaced, dict):
            try:  # A retried start returns no hand-over; unload is idempotent and returns it.
                replaced = self.controller.run_command('run_unload', replace, expected=snap)
            except Exception as exc:
                replaced = None
                parts.append(f'The replaced score\'s remaining inputs were not retrieved: {exc}.')
        if isinstance(replaced, dict):
            sentence, bad = self._keep_remainder(replaced)
            failed = failed or bad
            parts.append(f'It replaced {replaced.get("name") or "the previous score"}.' + sentence)
        self._score_notice = ('Score loaded', ' '.join(parts), failed)
        return self._score_snapshot()

    def _score_snapshot(self):
        """Worker thread: one atomic owner view after a score command.

        A playing score moves the gate between the two reads of snapshot(),
        so prefer the single-response view; fall back for legacy owners.
        """
        admitted = getattr(self.controller, '_atomic_admitted', None)
        if admitted is not None and 'atomic_view_observation' in admitted.capabilities:
            try:
                return self.controller.coherent_frame(None)[0]
            except ReadOnlyResponsePending:
                raise
            except Exception:
                pass
        return self.controller.snapshot()

    def _present_score_controls(self):
        if not hasattr(self, 'score_pause_action'):
            return
        s = self.snapshot
        control = s is not None and 'run_control' in s.capabilities and not s.error
        run = getattr(s, 'run', None)
        loaded = run is not None and run.active
        busy = (self.pending and self.operation in RUN_CONTROL_OPS) or bool(
            self.deferred_command and self.deferred_command[0] in RUN_CONTROL_OPS)
        texts = {'score_load_here': 'Load score here…', 'score_load_at': f'Load score at {self._unit()}…',
                 'score_pause_resume': 'Resume score' if loaded and run.paused else 'Pause score',
                 'score_unload': 'Unload score'}
        if texts != self._score_menu_texts:
            # The registry binder shows each resolved key beside its menu text.
            self._score_menu_texts = texts
            binder = getattr(self.overlay, '_shortcut_binder', None)
            for action_id, text in texts.items():
                self._score_menu_items[action_id].setText(text)
                if binder is not None:
                    binder.show_in_menu(action_id, self._score_menu_items[action_id], text)
        self.score_pause_action.setEnabled(control and loaded and not busy
                                           and not (run.paused and s.unknown_state))
        self.score_pause_action.setToolTip('Pause the loaded score, or continue it from here')
        self.score_unload_action.setEnabled(control and run is not None and not busy)
        self.score_unload_action.setToolTip('Stop the score here and keep its other inputs as a draft')
        ready = control and not s.unknown_state and not busy and not self.playing
        self.score_load_action.setEnabled(ready)
        self.score_load_action.setToolTip(f'Start a score at the current {self._unit()}')
        self.score_load_at_action.setEnabled(ready)
        self.score_load_at_action.setToolTip(f'Go back to an earlier {self._unit()} and start a score there')

    def poll_paused(self):
        if not self.playing:
            self.submit('refresh')

    def playback_advance(self):
        # One acknowledged batch per event-loop turn. Pause cancels the next
        # turn, so it lands on a recorded tick; one command stays in flight.
        if not self.playing or self.closed or self.pending:
            return
        if not self.step_next(ui_execution=False, play=True):
            self.playing = False
            self.present()

    def preview_live_for(self, snapshot):
        """The preview channel has shown a frame of this owner session and take."""
        return (snapshot is not None and self._preview_identity is not None
                and self._preview_identity == ((snapshot.owner_token or {}).get('session_id'), snapshot.take_path))

    def play_action(self):
        """The next run of identical authored ticks, at most one adaptive batch.

        Note(count=k) is the same input as k one-tick Notes: wheel applies to
        the first tick only, so later ticks must match it with wheel 0. A
        barrier or one-shot ends the group before it; at the first tick it
        is still refused exactly as a single step would be.
        """
        start = self.timeline.tick + 1
        first = note_action(self.timeline.note(start))
        # Mechanic sequence receipts are per acknowledged tick.
        limit = 1 if self.sequence_enabled else self.play_sizer.ticks
        limit = self.future_rail.cap(start, limit)  # forward replay stops at its target
        held = dict(first, wheel=0)
        count = 1
        while count < limit:
            try:
                following = note_action(self.timeline.note(start + count))
            except ValueError:
                break
            if following != held:
                break
            count += 1
        return dict(first, count=count)

    def advance(self):
        if self.playing:
            self.step_next(ui_execution=False)
        else:
            self.submit('refresh')

    def selected_tick(self):
        index = self.table.currentIndex()
        return self.model.origin + index.column() if index.isValid() else self.timeline.tick + 1

    def add_key(self):
        try:
            key = canonical_token(self.key_choice.currentText())
            selected = self.table.currentIndex()
            tick = self.selected_tick() if selected.isValid() else None
            selected_key = self.model.keys[selected.row()] if selected.isValid() else None
            self.restoring_selection = True
            try:
                self.model.add_lane(key)
                if tick is not None and self.model.keys:
                    row = self.model.keys.index(selected_key) if selected_key in self.model.keys else 0
                    column = tick - self.model.origin
                    if 0 <= column < self.model.columnCount():
                        self.table.setCurrentIndex(self.model.index(row, column))
            finally:
                self.restoring_selection = False
        except Exception as exc:
            self.error.setText(str(exc))

    def mouse_mode_changed(self, mode):
        self.x.setPrefix('ΔX ' if mode==1 else 'X ')
        self.y.setPrefix('ΔY ' if mode==1 else 'Y ')
        self.x.setEnabled(mode!=0);self.y.setEnabled(mode!=0)

    def load_selected_mouse(self, *_):
        if self.restoring_selection:
            return
        self.explicit_selection = True
        self.follow_action.setChecked(False)
        self.refresh_selected_mouse()

    def refresh_selected_mouse(self):
        # Retire the previous target before resolving the new selection. A
        # barrier/refusal must never leave Save mouse pointing at another tick.
        self.mouse_target = None
        self.mouse_identity = None
        try:
            target = self.selected_tick()
            note = self.timeline.note(target)
            self.selection_label.setText(f"Selected {target}")
            self.mouse_target = target
            self.mouse_identity = self.timeline.identity
            pair = note.mouse_abs or note.mouse_rel or (0, 0)
            self.mouse_mode.setCurrentIndex(2 if note.mouse_abs is not None else 1 if note.mouse_rel is not None else 0)
            self.x.setValue(pair[0]); self.y.setValue(pair[1])
            self.wheel.setValue(note.wheel)
            for key, box in self.button_boxes.items():
                box.setChecked(key in note.buttons)
        except Exception as exc:
            self.error.setText(str(exc))

    def edit_mouse(self):
        tick = self.mouse_target
        try:
            if self.snapshot is not None and (self.mutation_pending() or self.owner_error or self.draft_failure or self.snapshot.unknown_state or self.snapshot.error):
                raise ValueError('mouse editing is unavailable while the owner is busy or unknown')
            if tick is None or self.mouse_identity != self.timeline.identity or self.timeline.foreign:
                raise ValueError(f'mouse selection changed take or revision; select a {self._unit()} again')
            mode = self.mouse_mode.currentIndex()
            self.timeline.edit(tick, mouse_abs=(self.x.value(), self.y.value()) if mode == 2 else None,
                               mouse_rel=(self.x.value(), self.y.value()) if mode == 1 else None,
                               buttons=tuple(key for key, box in self.button_boxes.items() if box.isChecked()),
                               wheel=self.wheel.value())
            self.model.dataChanged.emit(self.model.index(0, 0), self.model.index(self.model.rowCount()-1, self.model.columnCount()-1))
            self.present()
        except Exception as exc:
            self.error.setText(str(exc))

    def step_next(self, *, ui_execution=True, play=False):
        try:
            options = {} if ui_execution else {'ui_execution': False}
            if self.snapshot is not None and getattr(self.snapshot, 'mode', 'live') == 'archive':
                return self.submit('step', None, play=play, **options)
            elif play:
                return self.submit('step', self.play_action(), play=True, **options)
            else:
                return self.submit('step', note_action(self.timeline.note(self.timeline.tick + 1)), **options)
        except Exception as exc:
            self.error.setText(str(exc))
            return False

    def branch_here(self, *, selected=None):
        try:
            if (self.mutation_pending() or self.owner_error or self.draft_failure
                    or self.timeline.foreign or self.closed or self.snapshot is None
                    or self.snapshot.unknown_state or self.snapshot.error):
                raise ValueError('Branch editing requires an idle, acknowledged owner state')
            if (self.snapshot is None or getattr(self.snapshot,'mode','live')!='archive'
                    or 'branch' not in self.snapshot.capabilities or not callable(getattr(self.controller,'branch',None))):
                raise ValueError('Branch editing requires an acknowledged archive owner')
            boundary=self.snapshot.tick if selected is None or selected>self.snapshot.tick else max(0,selected-1)
            text=self.timeline.edited_score(self.snapshot,branch_tick=boundary)
            destination=self.overlay.branch_destination()
            if not destination:return False
            value={'score':text,'destination':destination,'boundary':boundary,
                   'drafts':dict(self.timeline.drafts),'identity':self.timeline.identity}
            return self.submit('branch',value)
        except Exception as exc:self.error.setText(str(exc));return False

    def apply_selected(self):
        tick = self.selected_tick()
        if self.snapshot is not None and getattr(self.snapshot,'mode','live')=='archive':
            return self.branch_here(selected=tick)
        try:
            self.submit('apply', (tick, note_action(self.timeline.note(tick))))
        except Exception as exc:
            self.error.setText(str(exc))

    def awaiting_owner_ack(self):
        packet = self.retained_preview()
        return bool(self.awaiting_ack or (packet is not None and self.snapshot is not None
                                         and packet['tick'] > self.snapshot.tick))

    def block_owner_mutations(self):
        """Preview progress supplies pixels/inputs, never a new command token."""
        for widget in (self.step, self.apply, self.mouse_save, self.play, self.overlay.rewind,
                       self.overlay.branch, self.overlay.recording_load, self.overlay.recording_save,
                       self.overlay.save_button, self.overlay.entry):
            widget.setEnabled(False)
        self.seek.set_seek_enabled(False)
        self.undo_action.setEnabled(False)
        self.redo_action.setEnabled(False)
        self.play.setEnabled(self.play_action_enabled())
        self.model.busy = True
        self.overlay._sync_rail_controls()

    def mutation_pending(self):
        return bool(self.awaiting_owner_ack() or self.deferred_command
                    or (self.pending and self.operation != 'refresh'))

    def owner_run_active(self):
        run=getattr(self.snapshot,'run',None)
        return run is not None and run.active

    def play_action_enabled(self, ready=False):
        """Use the availability of the route toggle_play will actually take."""
        if self.owner_run_active() and 'run_control' in self.snapshot.capabilities:
            # The score menu owns pause/resume availability, including refusing
            # Resume for an unknown outcome. Cancellation is a separate action.
            return (not self.closed and self.score_pause_action.isEnabled()
                    and (not self.pending or self.operation == 'refresh')
                    and self.deferred_command is None)
        return ready or self.playing or self.can_stop_run()

    def can_stop_run(self):
        check=getattr(self.controller,'can_cancel_run',None)
        return (not self.closed and callable(check) and check(self.snapshot)
                and (not self.pending or self.operation=='refresh') and self.deferred_command is None)

    def stop_pending(self):
        return bool((self.pending and self.operation=='run_cancel')
                    or (self.deferred_command and self.deferred_command[0]=='run_cancel'))

    def can_reconcile(self):
        available=getattr(self.controller,'can_reconcile',None)
        return (not self.closed and self.snapshot is not None and not self.pending
                and self.deferred_command is None and callable(getattr(self.controller,'reconcile',None))
                and callable(available) and available())

    def submit(self, operation, value=None, *, expected_snapshot=None, ui_execution=True, play=False):
        if self.closed:return False
        if self.owner_run_active() and operation not in ('refresh','run_cancel',*RUN_CONTROL_OPS):
            self.overlay.state.setToolTip('A score is loaded. Stop or unload it before changing the recording.')
            return False
        if operation=='seek' and self.future_rail.handles(value):
            # A live owner restores backward only. Forward replays the recorded
            # future through ordinary steps, stopped at the target; a seek
            # back during that replay stops it first.
            return self.future_rail.seek(value)
        if operation=='run_cancel':
            check=getattr(self.controller,'can_cancel_run',None)
            if not callable(check) or not check(expected_snapshot or self.snapshot):
                self.error.setText('Stop requires an acknowledged run identity')
                return False
        # Score control is keyed by run identity: preview frames run ahead of the
        # last poll while a score plays, so it never waits for that ACK.
        if self.awaiting_owner_ack() and operation not in ('refresh', 'reconcile','run_cancel',*RUN_CONTROL_OPS):
            self.overlay.state.setToolTip('Command refused: waiting for the game to confirm the last command')
            return False
        if operation=='reconcile' and not self.can_reconcile():
            self.error.setText('Reconnect is only needed after Stasis loses track of a command. Nothing to reconnect now.')
            return False
        if (self.owner_error or self.draft_failure or self.timeline.foreign or self.snapshot is None
                or self.snapshot.unknown_state or self.snapshot.error) and operation not in ('refresh','reconcile','run_cancel','run_pause','run_unload'):
            self.error.setText('Command refused: wait until the game is paused and ready')
            return False
        if operation=='seek' and (type(value) is not int or self.snapshot is None
                or not 0<=value<=self.overlay.seek_extent(self.snapshot)):
            self.error.setText(f'Seek refused: choose a {self._unit()} in the acknowledged recording range')
            return False
        focus_display = None
        if ui_execution and operation in ('step', 'apply') and W.QApplication.instance().platformName() == 'xcb':
            focus_display = getattr(self.snapshot, 'ui_execution_display', None)
            if not focus_display and getattr(self.snapshot, 'mode', 'live') != 'archive':
                self.error.setText('Command refused: Stasis cannot see the game display')
                self.playing = False
                self.present()
                return False
        if self.pending:
            if self.operation == 'refresh' and operation != 'refresh' and self.deferred_command is None:
                if operation in ('seek','branch'):
                    self.playing=False
                    self.play.setText('▶ Play')
                self.deferred_command=(operation,value,expected_snapshot or self.snapshot,ui_execution,play)
                self.present()
                return True
            if operation != 'refresh':self.error.setText('Command already queued; wait for it to finish')
            return False
        if operation not in ('run_cancel',*RUN_CONTROL_OPS) and expected_snapshot is not None and self.snapshot is not None:
            if (Timeline.snapshot_identity(expected_snapshot)!=Timeline.snapshot_identity(self.snapshot)
                    or expected_snapshot.tick!=self.snapshot.tick
                    or getattr(expected_snapshot,'owner_token',None)!=getattr(self.snapshot,'owner_token',None)):
                self.error.setText(f'Owner changed while the command waited; select the {self._unit()} again')
                self.playing=False;self.present();return
        self.pending = True
        self.operation = operation
        if operation in ('seek','branch','load_recording','reconcile'):
            self._display_packet=None
        # A live seek back branches the take; future input edits stay with it.
        live_seek = operation == 'seek' and getattr(self.snapshot, 'mode', 'live') == 'live'
        self.transfer_from = self.timeline.identity if operation in ('step', 'apply') or live_seek else None
        self.branch_commit=value if operation=='branch' else None
        if operation in ('seek','branch'):
            self.playing=False
            self.play.setText('▶ Play')
        prefs=self.overlay.ui_preferences
        preview=(prefs.layout == 'workspace' and prefs.panels.preview) or (prefs.layout == 'overlay' and prefs.game_surface == 'owner_frame')
        # Play presents through the latest-only preview channel once it has
        # shown a frame; it never waits for a per-command PNG view.
        play = bool(play and operation == 'step')
        if play and self.preview_live_for(expected_snapshot or self.snapshot):
            preview = False
        self.queue.put_nowait((operation, value, expected_snapshot or self.snapshot, preview, focus_display, play))
        # A passive poll does not change editable/busy state. Keep error and
        # reconciliation presentation on the normal path.
        if (operation != 'refresh' or self.snapshot is None or self.owner_error
                or self.snapshot.error or self.snapshot.unknown_state or self.error.text()):
            self.present()
        return True

    @C.Slot(object)
    def bootstrap_completed(self,snapshot):
        # Initial command view can wait behind an external long step. Admit
        # owner authority first so the independent preview channel can present;
        # this does not complete or clear the outstanding command operation.
        if self.closed or self.snapshot is not None or snapshot.error or snapshot.unknown_state:return
        if snapshot.owner_token is None:return
        self.snapshot=snapshot
        self.timeline.update(snapshot)
        self.model.acknowledged=snapshot.last_inputs
        self.model.refresh()
        self.present()

    @C.Slot(object)
    def sequence_evidence(self, envelope):
        kind, value = envelope
        if kind == 'input':
            self.sequence_dialog.accept_receipt(value)
        elif kind == 'restore':
            self.sequence_dialog.boundary_changed(value)
        elif kind == 'recorded':
            return  # Input trainer only; the guide gets the 'unavailable' that follows.
        elif self.sequence_dialog.tracker is not None:
            self.sequence_dialog.tracker.suspend(value)
            self.sequence_dialog.latest = None
            self.sequence_dialog.refresh()

    def worker(self):
        while True:
            item = self.queue.get()
            if item is None:
                return
            operation, value, expected_snapshot, preview, focus_display, play = item
            started = time.perf_counter()
            focus_args = {'ui_focus_display': focus_display} if focus_display else {}
            sequence_before = sequence_action = None
            tool_action = ""
            sequence_requested = ((self.sequence_enabled or getattr(self, 'trainer_live', False))
                                  and operation in {'step', 'apply'})
            debugger_requested = (self._debugger_inspection_demand.is_set() and operation in {'step', 'apply'}
                                  and expected_snapshot is not None and expected_snapshot.mode == 'live'
                                  and 'atomic_step_observation' in expected_snapshot.capabilities)
            try:
                if operation == 'branch':
                    branch_expected=expected_snapshot
                    if value['boundary']!=expected_snapshot.tick:
                        branch_expected=self.controller.rewind(value['boundary'],expected=expected_snapshot)
                    snapshot=self.controller.branch(value['score'],value['destination'],expected=branch_expected)
                elif operation == 'configure_shortcuts':
                    snapshot = self.controller.configure_tool_bindings(value, expected=expected_snapshot)
                    self._native_tool_signature = tuple(tuple(pair) for pair in value)
                elif operation == 'set_engaged':
                    if getattr(expected_snapshot, 'engagement_qualified', False) is not True:
                        raise ValueError('normal input handoff is not qualified')
                    snapshot = self.controller.set_engaged(value['engaged'],
                        pause_on_engage=value.get('pause_on_engage', True), expected=expected_snapshot)
                elif operation == 'run_cancel':
                    target=getattr(self.controller,'cancel_target',lambda e:e)(expected_snapshot)
                    stopped_id=getattr(getattr(target,'run',None),'run_id',None)
                    snapshot=self.controller.cancel_run(expected=expected_snapshot)
                    if stopped_id and 'run_control' in getattr(target,'capabilities',()):
                        # Stop keeps the score's unexecuted inputs as a draft too.
                        try:
                            receipt=self.controller.run_remainder(stopped_id, expected=target)
                            if receipt.get('remainder'):
                                sentence, failed = self._keep_remainder(receipt)
                                self._score_notice = ('Score stopped', f'{receipt.get("name") or "The score"} stopped at '
                                                      f'{self._unit()} {receipt.get("tick")}.' + sentence, failed)
                        except Exception as exc:
                            self._score_notice = ('Score stopped', f'Its remaining inputs were not retrieved: {exc}.', True)
                elif operation in ('run_pause', 'run_resume', 'run_unload'):
                    receipt=self.controller.run_command(operation, value, expected=expected_snapshot)
                    self._score_receipt_notice(operation, receipt)
                    snapshot=self._score_snapshot()
                elif operation == 'score_load':
                    snapshot=self._score_load(value)
                elif operation == 'reconcile':
                    snapshot=self.controller.reconcile()
                elif operation == 'save_recording':
                    snapshot=self.controller.save_recording(expected=expected_snapshot)
                elif operation == 'load_recording':
                    snapshot=self.controller.load_recording(value,expected=expected_snapshot)
                elif operation == 'seek':
                    snapshot = self.controller.seek(value, expected=expected_snapshot)
                elif operation == 'apply':
                    if expected_snapshot is not None and getattr(expected_snapshot, 'mode', 'live') == 'archive':
                        raise ValueError('Archive input changes require an explicit branch; drafts are preserved')
                    tick, action = value
                    current = self.controller.snapshot()
                    if current.error:
                        raise ValueError(current.error)
                    if (expected_snapshot is None or Timeline.snapshot_identity(current) != Timeline.snapshot_identity(expected_snapshot) or current.tick != expected_snapshot.tick):
                        raise ValueError('take or authoring revision changed; refresh required')
                    if tick <= current.tick:
                        restored = self.controller.rewind(tick - 1, expected=expected_snapshot)
                        if restored.tick != tick - 1:
                            raise ValueError(f'rewind did not acknowledge requested {self._unit()}')
                        expected_snapshot = restored
                        if sequence_requested:
                            self.signals.sequence.emit(('restore', restored))
                    elif tick != current.tick + 1:
                        raise ValueError(f'select the next {self._unit()} to execute a future draft')
                    sequence_before, sequence_action = expected_snapshot, action
                    snapshot = self.controller.step(action, expected=expected_snapshot,
                                                    include_inspect=sequence_requested or debugger_requested, **focus_args)
                elif operation == 'step':
                    if play and expected_snapshot is not None:
                        # Play batches skip the extra 'takes' round trip. The
                        # owner's compare-and-swap on the expected token still
                        # refuses any change since the last acknowledgement.
                        current = expected_snapshot
                    else:
                        current = self.controller.snapshot()
                        if current.error:
                            raise ValueError(current.error)
                        if (expected_snapshot is None or Timeline.snapshot_identity(current) != Timeline.snapshot_identity(expected_snapshot) or current.tick != expected_snapshot.tick):
                            raise ValueError('take or authoring revision changed; refresh required')
                    if getattr(current, 'mode', 'live') == 'archive':
                        snapshot = self.controller.recorded_step(expected=expected_snapshot, **focus_args)
                    else:
                        sequence_before, sequence_action = expected_snapshot, value
                        snapshot = self.controller.step(value, expected=expected_snapshot,
                                                        include_inspect=sequence_requested or debugger_requested, **focus_args)
                else:
                    snapshot = self.controller.snapshot() if not preview else None
                    if preview and expected_snapshot is None:
                        initial=self.controller.snapshot()
                        self.signals.bootstrap.emit(initial)
                if debugger_requested:
                    try:
                        from .debugger_widget import recorded_state_projection
                        evidence = recorded_state_projection(snapshot, self.controller.last_step_observation)
                    except (ValueError, TypeError, AttributeError):
                        # Optional display refusal cannot invalidate a committed command.
                        evidence = None
                    self.signals.debugger_inspection.emit(evidence)
                if sequence_requested:
                    try:
                        from .mechanic_sequences import acknowledged_step_receipt
                        if sequence_before is None:
                            if snapshot is not None and getattr(self, 'trainer_live', False):
                                # Archive step: the trainer reads the recorded note at this frame.
                                self.signals.sequence.emit(('recorded', snapshot))
                            raise ValueError('Recorded playback needs per-boundary input evidence')
                        receipt = acknowledged_step_receipt(sequence_before, snapshot, sequence_action,
                            self.controller.last_step_observation, precision=snapshot.clock_precision)
                        self.signals.sequence.emit(('input', receipt))
                    except (ValueError, AttributeError) as exc:
                        # Guide refusal never changes a successful game command.
                        self.signals.sequence.emit(('unavailable', str(exc)))
                if play and snapshot is not None:
                    # Native tool edges queue in the game process during a
                    # batch; read one per batch so hotkeys work while playing.
                    # The poll's snapshot is the admitted one for the next batch.
                    if ('tool_dispatch' in snapshot.capabilities and getattr(self, '_native_tool_signature', None)
                            == tuple(self.overlay.ui_preferences.shortcuts)):
                        try:
                            snapshot, tool_action = self.controller.poll_tool_controls(expected=snapshot)
                        except Exception:
                            tool_action = ''  # Optional read; never fails a committed batch.
                    if expected_snapshot is not None:
                        self._play_timing = (expected_snapshot.tick, snapshot.tick, time.perf_counter() - started)
                png = None
                # A started or resumed score moves the gate at once; its frames
                # arrive through the preview channel and the next poll.
                if preview and operation not in ('reconcile','branch','run_cancel',*RUN_CONTROL_OPS):
                    # Refresh asks for the current coherent owner view. An
                    # action result still requires its exact acknowledged gate.
                    snapshot, png = self.controller.coherent_frame(snapshot)
                if operation == 'refresh' and snapshot is not None and 'tool_dispatch' in snapshot.capabilities:
                    # Deliver the already coherent owner boundary before optional
                    # tool IO can wait behind an external command or refuse its
                    # now-stale expected token. Keep this worker operation pending.
                    self.signals.refresh_view.emit(snapshot, png)
                    shortcuts = tuple(self.overlay.ui_preferences.shortcuts)
                    if getattr(self, '_native_tool_signature', None) != shortcuts:
                        snapshot = self.controller.configure_tool_bindings(shortcuts, expected=snapshot)
                        self._native_tool_signature = shortcuts
                    snapshot, tool_action = self.controller.poll_tool_controls(expected=snapshot)
                self.signals.result.emit(snapshot, png, None)
                if tool_action: self.signals.tool_action.emit(tool_action)
            except Exception as exc:
                self.signals.result.emit(None, None, exc if isinstance(exc, ReadOnlyResponsePending) else str(exc))

    @C.Slot(object, object)
    def refresh_view_completed(self, snapshot, png):
        if self.closed or not self.pending or self.operation != 'refresh':
            return
        self.completed(snapshot, png, None, observation_only=True)

    @C.Slot(object, object, object)
    def completed(self, snapshot, png, error, *, observation_only=False):
        if self.closed:
            return
        completed_operation=self.operation
        timing, self._play_timing = self._play_timing, None
        if (timing is not None and completed_operation == 'step' and not error
                and snapshot is not None and snapshot.tick == timing[1]):
            self.play_sizer.record(timing[1] - timing[0], timing[2])
        if not isinstance(error, ReadOnlyResponsePending):
            self.sequence_dialog.boundary_changed(snapshot, failed=bool(error))
        if (completed_operation == 'refresh' and isinstance(error, ReadOnlyResponsePending)
                and self.snapshot is not None and not self.owner_error
                and not self.snapshot.error and not self.snapshot.unknown_state):
            # The serial command server may be executing an external long Note.
            # Preserve already admitted preview provenance; do not infer a new
            # CAS/durable boundary from that independent presentation channel.
            self.pending = False
            self.operation = None
            self.awaiting_ack = True
            self.model.busy = False
            self._unchanged_refresh = None
            self.playing = False
            stop=self.deferred_command if self.deferred_command and self.deferred_command[0] in ('run_cancel',*RUN_CONTROL_OPS) else None
            self.deferred_command = None
            self.playback_timer.stop()
            self.present()
            if stop:
                operation,value,expected,ui_execution,play=stop
                self.submit(operation,value,expected_snapshot=expected,ui_execution=ui_execution,play=play)
            return
        if isinstance(error, ReadOnlyResponsePending):
            error = str(error)
        previous_tick = self.snapshot.tick if self.snapshot is not None else None
        if (completed_operation in ('seek','branch','load_recording','reconcile') or error
                or (snapshot is not None and previous_tick is not None and snapshot.tick<previous_tick)):
            self._display_packet=None
        if not observation_only:
            self.pending = False
            self.operation = None
        if (completed_operation == 'refresh' and not error and snapshot is not None
                and not snapshot.error and not snapshot.unknown_state and png
                and not self.playing and self.deferred_command is None
                and self.transfer_from is None and self.branch_commit is None
                and not self.owner_error and not self.draft_failure
                and not self.awaiting_ack
                and not self.timeline.foreign and not self.error.text()
                and getattr(self, '_unchanged_refresh', None)
                    == (snapshot, png, self._refresh_ui_identity())):
            # Full snapshot equality includes owner CAS, score, capabilities,
            # health/error and frame provenance; byte equality binds the image.
            # Local edits, selection and panel changes also force a full refresh.
            self.snapshot = snapshot
            self.playback_timer.stop()
            return
        self._unchanged_refresh = None
        self.model.busy = False
        self.error.setText(error or (snapshot.error if snapshot else '') or '')
        self.owner_error = bool(error)
        if error and completed_operation == 'save_recording':
            # The error label is replaced by the next refresh; keep the refusal visible.
            self.show_notice('Recording not saved', f'Recording not saved: {error}', error=True)
        notice = None
        if completed_operation in RUN_CONTROL_OPS or completed_operation == 'run_cancel':
            notice, self._score_notice = self._score_notice, None
        if notice is not None:
            # Set by the worker before any later step could fail, so a saved
            # remainder is reported even when the command itself was refused.
            self.show_notice(notice[0], notice[1], error=notice[2])
            self.overlay.console.appendPlainText(notice[1])
        elif error and completed_operation in RUN_CONTROL_OPS:
            self.show_notice('Score command refused', error, error=True)
        if not error and snapshot is not None and not snapshot.error and not snapshot.unknown_state:
            self.awaiting_ack = False
        if (completed_operation == 'step' and snapshot is not None and previous_tick is not None
                and snapshot.tick <= previous_tick):
            # A terminal recorded step can acknowledge zero-time tail work
            # without advancing. Preserve that result and stop the ACK chain.
            self.playing = False
        if error or (snapshot and (snapshot.unknown_state or snapshot.error)):
            self.playing = False
            self.play.setText('▶')
        if snapshot is not None:
            self.model.presentation_tick = snapshot.tick
            self.model.presentation_unavailable = False
            self.model.presentation_note = None
            self.snapshot = snapshot
            if self.owner_run_active():
                # The owner's admitted queue advances independently of this
                # client's legacy per-step play loop and stale deferred edits.
                self.playing=False
                self.playback_timer.stop()
                if self.deferred_command and self.deferred_command[0] not in ('run_cancel',*RUN_CONTROL_OPS):
                    self.deferred_command=None
            if getattr(self,"_recording_path",None)!=snapshot.take_path:self.refresh_recordings()
            if not error and not snapshot.error and not snapshot.unknown_state:
                # Results of a completed owner command; display only.
                if completed_operation=='save_recording':
                    from .workspace_session import save_summary
                    summary=save_summary(getattr(self.controller,'last_save',None))
                    if summary:self.show_notice('Recording saved',summary)
                elif completed_operation in ('seek','load_recording'):
                    landing=getattr(self.controller,'last_landing','')
                    if landing.startswith('Rewound via'):
                        # Restore/replay mechanics stay in the debug log, not the console.
                        import logging
                        logging.getLogger(__name__).debug(landing)
                        self.show_notice('Rewound', f'Rewound to {self._unit()} {snapshot.tick}.')
                # Capture cadence: an owner that had to record one extra frame to make the held
                # gate a restore point says so in the rewind receipt; show it once.
                receipt=snapshot.receipt if isinstance(snapshot.receipt,dict) else {}
                step=receipt.get('capture_step')
                if isinstance(step,list) and isinstance(receipt.get('notice'),str) and getattr(self,'_shown_capture',None)!=(snapshot.take_path,tuple(step)):
                    self._shown_capture=(snapshot.take_path,tuple(step))
                    self.show_notice('Extra frame captured',receipt['notice'])
            if completed_operation=='branch' and self.branch_commit is not None and not error and not snapshot.error and not snapshot.unknown_state:
                try:
                    commit=self.branch_commit
                    self.timeline.commit_branch(snapshot,commit['identity'],commit['drafts'])
                    self.show_notice('Branch saved','Drafts committed to paused branch; Step or Play executes the authored score')
                except Exception as exc:
                    self.draft_failure=True;self.error.setText(str(exc));self.playing=False
                self.branch_commit=None
            if completed_operation=='load_recording' and not error and not snapshot.error and not snapshot.unknown_state:
                old_drafts,old_identity=self.timeline.drafts,self.timeline.identity
                try:
                    self.timeline.drafts={}
                    self.timeline.save(identity=Timeline.snapshot_identity(snapshot))
                    self.timeline.identity=Timeline.snapshot_identity(snapshot);self.timeline.foreign=False
                    self.timeline.clear_edit_history()
                    self.explicit_selection=False;self.manual_scroll=False;self.manual_vertical_scroll=False
                except Exception as exc:
                    self.timeline.drafts=old_drafts;self.timeline.identity=old_identity
                    self.draft_failure=True;self.error.setText(str(exc))
                self.refresh_recordings()
            if self.transfer_from is not None and not error:
                try:
                    self.timeline.transfer(snapshot, self.transfer_from)
                except Exception as exc:
                    self.error.setText(str(exc))
                    self.draft_failure = True
                    self.playing = False
                    self.play.setText('▶')
            self.transfer_from = None
            self.timeline.update(snapshot)
            self.model.acknowledged = snapshot.last_inputs
            horizontal = self.table.horizontalScrollBar().value()
            vertical = self.table.verticalScrollBar().value()
            selected = self.table.currentIndex()
            selected_tick = self.model.origin + selected.column() if selected.isValid() else None
            selected_key = self.model.keys[selected.row()] if selected.isValid() else None
            self.restoring_selection = True
            try:
                self.model.refresh(follow=self.follow_action.isChecked() and not self.explicit_selection and not self.manual_scroll)
                if selected_tick is not None and (self.explicit_selection or self.manual_scroll or not self.follow_action.isChecked()):
                    column = selected_tick - self.model.origin
                    if not 0 <= column < self.model.width:
                        self.model.origin = max(1, selected_tick - 24)
                        column = selected_tick - self.model.origin
                    row = self.model.keys.index(selected_key) if selected_key in self.model.keys else 0
                    self.table.setCurrentIndex(self.model.index(row, column))
            finally:
                self.restoring_selection = False
            self.overlay.fit_piano_headers()
            if self.manual_vertical_scroll:self.table.verticalScrollBar().setValue(vertical)
            self.selection_label.setText(f"Current {snapshot.tick} · Selected {self.selected_tick()}")
            if self.follow_action.isChecked() and not self.explicit_selection and not self.manual_scroll:
                held=getattr(snapshot.last_inputs,'held',())
                self.follow_active_keys(held,snapshot.tick)
            elif self.manual_scroll:
                self.table.horizontalScrollBar().setValue(horizontal)
            self._apply_unit_wording()
            self.counter.setText(f'{self._unit(title=True)} {snapshot.tick}')
            self.counter.setToolTip(str(snapshot.last_inputs or ''))
            self.seek.setRange(0, self.overlay.seek_extent(snapshot))
            # A held rail handle is the user's cursor; idle refreshes must not
            # snap it back to the playhead before release commits the seek.
            if not self.seek.isSliderDown():self.seek.setValue(snapshot.tick)
        self.present()
        frame_replaced = self.retained_preview() is not None
        if not frame_replaced and png and snapshot is not None and not error and not snapshot.unknown_state and not snapshot.error:
            pixmap = self.decoded_pixmap(png)
            if pixmap is not None:
                frame_replaced=self.overlay.set_workspace_frame(pixmap,snapshot.tick,getattr(snapshot,'frame_metadata',None))
                if frame_replaced:
                    self.model.presentation_tick=snapshot.tick
                    self.model.presentation_note=None
                    self.model.presentation_unavailable=False
                    self.present()
                    self.model.sync_view()
        if not frame_replaced:
            frame_tick=self.overlay._workspace_frame_tick
            packet=getattr(self,'_display_packet',None)
            # During Play the latest preview frame normally trails the batch
            # ACK by one encode. Same session/take/epoch frames are only
            # behind, not of unknown provenance; the next packet repaints.
            trailing=(self.playing and png is None and self.preview_live_for(snapshot) and packet is not None
                      and snapshot is not None and not error
                      and packet.get('session_id')==(snapshot.owner_token or {}).get('session_id')
                      and packet.get('take')==snapshot.take_path
                      and packet.get('epoch')==(snapshot.frame_metadata or {}).get('epoch', packet.get('epoch'))
                      and packet.get('tick',snapshot.tick+1)<=snapshot.tick)
            if (frame_tick is not None and snapshot is not None and not trailing
                    and frame_tick != snapshot.tick):
                self.unavailable_frame_inputs()
            elif frame_tick is not None and snapshot is not None and frame_tick == snapshot.tick:
                # The retained image is the owner's current frame, so its
                # acknowledged inputs are valid: show them, not "unavailable".
                self.model.presentation_tick = snapshot.tick
                self.model.presentation_note = None
                self.model.presentation_unavailable = False
                self.model.sync_view()
        else:
            self.model.presentation_unavailable = False
        if frame_replaced and (not self.playing and not self.owner_error and not self.draft_failure
                and not self.timeline.foreign and not self.error.text()
                and self.deferred_command is None and self.transfer_from is None
                and self.branch_commit is None):
            self._unchanged_refresh = (snapshot, png, self._refresh_ui_identity())

        if observation_only:
            return  # Only the final worker result releases the queued operation.
        self.future_rail.after_result(snapshot, error)  # stop a forward replay at its target
        if self.deferred_command is not None:
            operation,value,expected,ui_execution,play=self.deferred_command;self.deferred_command=None
            self.submit(operation,value,expected_snapshot=expected,ui_execution=ui_execution,play=play)
        if self.playing and not self.pending:
            # Yield to GUI events instead of waiting for the idle refresh poll.
            self.playback_timer.start(0)
        elif not self.playing:
            self.playback_timer.stop()

    def _refresh_ui_identity(self):
        selected = self.table.currentIndex()
        return (repr(self.overlay.ui_preferences), repr(self.timeline.drafts),
                repr(self.timeline.identity), self.explicit_selection, self.manual_scroll,self.manual_vertical_scroll,
                self.root.width(), self.root.height(),
                tuple(self.model.explicit_keys), tuple(self.model.keys), self.model.origin,
                selected.row(), selected.column(),
                self.table.horizontalScrollBar().value(),
                self.table.verticalScrollBar().value(),
                self.overlay.recording_combo.currentData())

    def present(self):
        self.overlay.refresh_remote(RemotePresentation(self.snapshot, self.playing, self.mutation_pending(),
                                                       self.error.text(), self.awaiting_owner_ack(),
                                                       can_stop_run=self.can_stop_run(), stopping_run=self.stop_pending()))
        # Match toggle_play's existing loaded-score route. The generic overlay
        # uses Stop for owners without run_control; this host pauses/resumes.
        run = getattr(self.snapshot, 'run', None)
        if (run is not None and run.active and 'run_control' in self.snapshot.capabilities
                and not self.stop_pending()):
            self.play.setText('▶ Resume run' if run.paused else 'Ⅱ Pause run')
            self.play.setToolTip(self.overlay.shortcut_tooltip('play_pause',
                'Resume the loaded run from here.' if run.paused else 'Pause the loaded run.'))
        self.future_rail.present()
        self._present_score_controls()
        self.overlay.reconnect_action.setEnabled(self.can_reconcile())
        ready = (not self.mutation_pending() and not self.owner_error and not self.draft_failure and not self.timeline.foreign
                 and not self.owner_run_active() and self.snapshot is not None
                 and not self.snapshot.unknown_state and not self.snapshot.error)
        owner_ready = ready
        ready = ready and getattr(self.snapshot, 'engaged', None) is not False
        for widget in (self.step, self.apply, self.mouse_save, self.play):
            widget.setEnabled(ready)
        self.play.setEnabled(self.play_action_enabled(ready))
        self.model.busy = not ready or self.playing
        self.undo_action.setEnabled(ready and self.timeline.can_undo)
        self.redo_action.setEnabled(ready and self.timeline.can_redo)
        branch_ready=ready and self.snapshot is not None and getattr(self.snapshot,'mode','live')=='archive' and 'branch' in self.snapshot.capabilities and callable(getattr(self.controller,'branch',None))
        self.overlay.branch.setEnabled(branch_ready)
        self.overlay.branch.setToolTip('Commit future edits into a new paused recording branch' if branch_ready else 'Branches are not available in live play')
        if self.snapshot is not None and getattr(self.snapshot, 'mode', 'live') == 'archive':
            self.apply.setEnabled(branch_ready)
            self.apply.setToolTip(self.overlay.shortcut_tooltip('apply', f'Commit all future draft edits into a paused branch before the selected {self._unit()}'))
            self.step.setToolTip(self.overlay.shortcut_tooltip('step', 'Advance recorded inputs; drafts require an explicit branch'))
        else:
            self.apply.setToolTip(self.overlay.shortcut_tooltip('apply', 'Apply the selected draft'))
            self.step.setToolTip(self.overlay.shortcut_tooltip('step', 'Advance the next input from the shared draft document'))
        self.seek.set_seek_enabled(ready and self.overlay.seek_extent(self.snapshot) > 0)
        self.overlay.rewind.setEnabled(ready and self.snapshot.tick > 0)
        caps=self.snapshot.capabilities if self.snapshot is not None else ()
        for widget,capability in ((self.overlay.recording_load,'recording_load'),(self.overlay.recording_save,'recording_save')):
            selected=capability!='recording_load' or bool(self.overlay.recording_combo.currentData())
            recording_ready = owner_ready if capability == 'recording_save' else ready
            widget.setEnabled(recording_ready and capability in caps and selected)
            widget.setToolTip('' if capability in caps else 'This session cannot '+capability.split('_')[1]+' recordings')
        self.overlay.explain_disabled(self.disabled_reason())
        # A slow coherent refresh may acknowledge an earlier tick than an
        # already verified presentation packet. Reapply that packet's frame,
        # inputs, counter and piano together; never mutate the CAS snapshot.
        packet=self.retained_preview()
        if packet is not None:self.preview_completed(packet)
        elif self.overlay._workspace_frame_tick is not None and self.snapshot is not None:
            frame_epoch=self.overlay._owner_frame_metadata.get('epoch')
            owner_epoch=(self.snapshot.frame_metadata or {}).get('epoch')
            if (getattr(self.model,'presentation_unavailable',False)
                    or self.overlay._workspace_frame_tick != self.snapshot.tick
                    or (type(owner_epoch) is int and frame_epoch != owner_epoch)):
                self.unavailable_frame_inputs()

        # Preview handling above can disable authoring while the canonical ACK
        # is pending. Project only after those final control changes, while the
        # snapshot still supplies the committed position and evidence history.
        self.overlay._sync_rail_controls()
        self.debugger_dialog.observe(self.snapshot, surface=self.surface.view())

    def _unit(self, plural=False, title=False):
        """Time-unit word for labels: frame unless the owner reports clock.precision = "tick"."""
        return unit_name(getattr(getattr(self, 'snapshot', None), 'clock_precision', None), plural, title)

    def _apply_unit_wording(self):
        self.model.position_unit = self._unit(title=True)
        self.follow_action.setToolTip(f'Keep the current {self._unit()} and its active input lanes in view. Browsing stops following.')
        self.table.setAccessibleDescription(f'{self._unit(plural=True, title=True)} run from left to right. Each row is a key. Arrow keys select a cell; Space, Enter or double-click toggles its draft input. Control Z undoes a draft edit.')

    def unavailable_frame_inputs(self):
        """A retained image cannot borrow inputs from a different owner ACK."""
        tick=self.overlay._workspace_frame_tick
        if tick is None or self.snapshot is None:return
        self.overlay._render_input_keys(False, ())
        self.overlay._input_metadata=dict(tick=tick,highlight_source='unavailable',
                                         highlight_keys=[],highlight_buttons=[],live_input=None,
                                         view_info=dict(self.overlay._owner_frame_metadata))
        self.overlay.live_keys.setText('')
        self.overlay.current_input_summary.setText(f'{self._unit(title=True)} inputs unavailable')
        self.overlay.current_input_summary.setToolTip('No inputs are shown for this image.')
        self.counter.setText(f'Image {self._unit()} {tick} · Owner {self._unit()} {self.snapshot.tick}')
        self.counter.setToolTip(f'New image provenance is unavailable; the retained image keeps its original {self._unit()}.')
        self.model.presentation_tick=tick
        self.model.presentation_note=None
        self.model.presentation_unavailable=True
        self.selection_label.setText(f'Frame {tick} · Owner {self.snapshot.tick} · Selected {self.selected_tick()}')
        self.model.sync_view()

    def refresh_recordings(self,selected=None):
        current=self.snapshot.take_path if self.snapshot is not None else None
        self._recording_path=current
        combo=self.overlay.recording_combo
        selected=selected or combo.currentData() or current
        try:
            entries=self.recording_library.list(current)
            combo.clear()
            for entry in entries:combo.addItem(entry.title+(' · current' if entry.current else ''),entry.path)
            if selected:combo.setCurrentIndex(combo.findData(selected))
            if not entries:combo.addItem('Choose recording…',None)
        except Exception as exc:self.error.setText(str(exc))

    @C.Slot(str)
    def native_tool_action(self, action):
        # The canonical dispatcher already checked game focus. A concurrent UI
        # command can finish first; never silently discard the captured edge.
        pending = getattr(self, '_pending_tool_actions', None)
        if pending is None:
            from collections import deque
            pending = self._pending_tool_actions = deque()
        if len(pending) >= 32:
            self.playing = False
            self.error.setText('Tool shortcut queue overflow; pause and reconcile')
            return
        pending.append(action)
        self.drain_tool_actions()

    def drain_tool_actions(self):
        pending = getattr(self, '_pending_tool_actions', ())
        if self.closed or not pending:
            return
        # Presentation toggles and Pause/Stop of local Play never wait for the
        # in-flight batch: Play keeps a command pending almost continuously.
        immediate = (pending[0] in PRESENTATION_ACTIONS
                     or (pending[0] in ('play_pause', 'stop') and self.playing))
        if immediate:
            self.overlay.interaction_controls.dispatch(pending.popleft(), source='native')
            if pending:
                C.QTimer.singleShot(0, self.drain_tool_actions)
            return
        if self.pending or self.awaiting_owner_ack() or self.deferred_command:
            C.QTimer.singleShot(50, self.drain_tool_actions)
            return
        if self.owner_error or self.snapshot is None or self.snapshot.unknown_state or self.snapshot.error:
            self.error.setText('Wait for the game to confirm, then try the shortcut again')
            return
        action = pending.popleft()
        self.overlay.interaction_controls.dispatch(action, source='native')
        if pending:
            C.QTimer.singleShot(0, self.drain_tool_actions)

    def overlay_action(self, action, **args):
        if action == 'configure_shortcuts':
            if self.snapshot is None or 'tool_dispatch' not in self.snapshot.capabilities:
                return False
            # Every layout change (panel toggles, resets, reloads) re-applies
            # preferences. Unchanged shortcuts need no owner mutation; sending
            # one anyway raced Save/refresh and could refuse or queue-conflict.
            if tuple(tuple(pair) for pair in args['shortcuts']) == getattr(self, '_native_tool_signature', None):
                return True
            return self.submit('configure_shortcuts', args['shortcuts'], ui_execution=False)
        elif action == 'set_engaged':
            if self.snapshot is None or self.snapshot.engagement_qualified is not True:
                self.error.setText('Normal input handoff is not qualified')
                return False
            return self.submit('set_engaged', args, ui_execution=False)
        elif action == 'reconcile':
            return self.submit('reconcile')
        elif action == 'add_recording':
            try:
                entry=self.recording_library.add(args['path']);self.refresh_recordings(entry.path)
            except Exception as exc:self.error.setText(str(exc))
        elif action == 'save_recording':
            self.submit('save_recording')
        elif action == 'load_recording':
            try:
                if not args.get('path'):raise ValueError('Choose a recording first')
                if self.snapshot is None or 'recording_load' not in self.snapshot.capabilities:raise ValueError('This owner does not support recording load')
                from .recording_library import archive_draft
                if self.timeline.drafts:
                    self.timeline.save()
                    self.recording_recovery=archive_draft(self.timeline.draft_path,self.timeline.identity)
                self.playing=False
                self.submit('load_recording',args['path'])
            except Exception as exc:self.error.setText(str(exc))
        elif action == 'branch':
            return self.branch_here()
        elif action == 'score_pause_resume':
            return self.score_pause_resume()
        elif action == 'score_unload':
            return self.score_unload()
        elif action in ('score_load_here', 'score_load_at'):
            return self.score_load(choose_frame=action == 'score_load_at',
                                   path=args.get('path'), tick=args.get('tick'))
        elif action == 'play_pause':
            self.toggle_play()
        elif action == 'stop':
            if self.can_stop_run():
                return self.submit('run_cancel', ui_execution=False)
            if self.owner_run_active():
                self.error.setText('Stop requires an acknowledged run identity')
                return False
            if self.playing:
                self.toggle_play()
        elif action == 'step':
            self.step_next()
        elif action == 'rewind':
            self.submit('seek', self.timeline.tick - 1)
        elif action == 'seek':
            self.submit('seek', args['tick'])
        elif action == 'apply_selected':
            self.apply_selected()
        elif action == 'save':
            self.save_drafts()
        elif action == 'command':
            self.command(args['text'])
        else:
            self.error.setText('Unavailable for this owner')
            return False
        return True

    def command(self, text):
        """Commands and piano edits operate on this same input document."""
        import json
        from kptas.gym import to_note
        parts = text.split(maxsplit=2)
        if not parts:
            return
        try:
            run=getattr(self.snapshot,'run',None)
            if parts[0] in ('play', 'pause') and run is not None and run.active and 'run_control' in self.snapshot.capabilities:
                # A loaded score: pause holds it at this frame, play continues it.
                if (parts[0]=='pause') == run.playing:
                    self.submit('run_pause' if run.playing else 'run_resume', run.run_id, ui_execution=False)
            elif parts[0] in ('play', 'pause'):
                if parts[0]=='pause' and self.can_stop_run():
                    self.submit('run_cancel',ui_execution=False)
                elif not self.owner_run_active() and self.playing != (parts[0] == 'play'):
                    self.toggle_play()
            elif parts[0] in ('step', 'rewind', 'save', 'stop') and len(parts) == 1:
                self.overlay_action(parts[0])
            elif parts[0] == 'seek' and len(parts) == 2:
                self.submit('seek', int(parts[1]))
            elif parts[0] == 'input' and len(parts) == 3:
                if self.mutation_pending() or self.owner_error or self.draft_failure or self.snapshot is None or self.snapshot.unknown_state or self.snapshot.error:
                    raise ValueError('input editing is unavailable while the owner is busy or unknown')
                tick = int(parts[1])
                action = note_action(self.timeline.note(tick))
                values = json.loads(parts[2])
                if not isinstance(values, dict):
                    raise ValueError('input values must be a JSON object')
                if set(values) - {'held', 'buttons', 'mouse_abs', 'mouse_rel', 'wheel'}:
                    raise ValueError('unsupported input field')
                action.update(values)
                note = to_note(action)
                self.timeline.edit(tick, held=note.held, buttons=note.buttons,
                                   mouse_abs=note.mouse_abs, mouse_rel=note.mouse_rel, wheel=note.wheel)
                if tick == self.mouse_target:
                    self.refresh_selected_mouse()
                self.model.dataChanged.emit(self.model.index(0, 0), self.model.index(self.model.rowCount()-1, self.model.columnCount()-1))
            else:
                raise ValueError(f'Use play, pause, stop, step, rewind, seek {self._unit().upper()}, save, or input {self._unit().upper()} {{"held":["w"]}}')
        except Exception as exc:
            self.error.setText(str(exc))
            self.overlay.console.appendPlainText(str(exc))

    def show(self):
        self.root.show()

    def resize(self, width, height):
        self.overlay._layout_surface(width, height)

    def close(self):
        self.root.close()

    def eventFilter(self, watched, event):
        if watched is self.root and event.type() == C.QEvent.Close:
            self.closeEvent(event)
        elif watched is getattr(self, 'table', None) and event.type() in (
                C.QEvent.Show, C.QEvent.Hide, C.QEvent.Resize):
            # Editor open/close and resizes change the rail's view band.
            self.schedule_rail_view()
        return super().eventFilter(watched, event)

    def closeEvent(self, event):
        if self.closed:
            event.accept()
            return
        self.closed = True
        self._preview_stop.set()
        self.timer.stop()
        self.playback_timer.stop()
        self.queue.put_nowait(None)
        event.accept()
        self.client_closed.emit()


# Import compatibility only: there is no second window implementation.
WorkspaceWindow = OverlayClient

class _ExceptionNet(C.QObject):
    """Unexpected exceptions anywhere in the UI: full traceback to stderr, plus a
    visible notice on the GUI thread. The process keeps running; no session
    command is retried or acknowledged because of it."""
    raised = C.Signal(str)

    def __init__(self, window):
        super().__init__()
        self.window = window
        self.seen = {}
        self.raised.connect(self.show)
        import sys, threading
        self.previous = (sys.excepthook, threading.excepthook)
        sys.excepthook = self.excepthook
        threading.excepthook = lambda args: self.report(
            f'thread {getattr(args.thread, "name", "?")}', args.exc_type, args.exc_value, args.exc_traceback)

    def excepthook(self, exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            return self.previous[0](exc_type, exc, tb)
        self.report('Qt callback', exc_type, exc, tb)

    def report(self, where, exc_type, exc, tb):
        import sys, traceback
        if exc_type is SystemExit:
            return
        key = (where, exc_type.__name__, str(exc)[:200])
        now = time.monotonic()
        # A failing paint or timer slot repeats every frame: log each distinct
        # failure fully once per 30 s, and count the rest.
        last, count = self.seen.get(key, (None, 0))
        if last is not None and now - last < 30:
            self.seen[key] = (last, count + 1)
            return
        self.seen[key] = (now, 0)
        try:
            repeat = f' (repeated {count} more times)' if count else ''
            print(f'Stasis: unexpected error in {where}{repeat}:', file=sys.stderr)
            traceback.print_exception(exc_type, exc, tb, file=sys.stderr)
            sys.stderr.flush()
        except Exception:
            pass
        self.raised.emit(f'{exc_type.__name__}: {exc}')

    @C.Slot(str)
    def show(self, text):
        try:
            if not self.window.closed:
                self.window.show_notice('UI error', 'The interface hit an internal error and kept running. '
                                        'The game session was not changed. Details are in the UI log.\n\n' + text,
                                        error=True)
        except Exception:
            pass


def run_workspace(controller, draft_path=None, *, ui_config=None, layout=None, preview_refresh_ms=16):
    app = W.QApplication.instance() or W.QApplication(['stasis'])
    app.setApplicationName('Stasis'); app.setOrganizationName('Kolto Labs')
    window = OverlayClient(controller, draft_path, ui_config=ui_config, layout=layout,
                           preview_refresh_ms=preview_refresh_ms)
    net = _ExceptionNet(window)
    window.client_closed.connect(app.quit)
    window.show()
    try:
        return app.exec()
    finally:
        import sys, threading
        sys.excepthook, threading.excepthook = net.previous
