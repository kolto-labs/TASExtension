"""The Settings dialog (Keys, Display, Playback, Shortcut list) and key capture.

Shortcut binding lives in shortcut_binder. Its public names are re-exported
here so older imports keep working.
"""
from __future__ import annotations

from dataclasses import fields, replace
import html
from pathlib import Path

from PySide6 import QtCore as C, QtGui as G, QtWidgets as W

from .actions import REGISTRY
from .keymap import (STORE, KeymapError, game_focus, overlaps, read_keymap_file, resolve,
                     unknown_message, write_keymap_file)
from . import ui_settings
from .shortcut_binder import (GLOBAL_NOTE, PORTABLE, ShortcutBinder, _live_preferences, _timeline_only,  # noqa: F401
                              attach_overlay, attach_workspace, external_keys)

# Re-exported for callers that import binding names from this module.
__all__ = ['SettingsDialog', 'KeyCaptureDialog', 'open_settings', 'ShortcutBinder',
           'attach_overlay', 'attach_workspace', 'external_keys', 'GLOBAL_NOTE',
           'TAB_KEYS', 'TAB_DISPLAY', 'TAB_PLAYBACK', 'TAB_SHORTCUTS']

class KeyCaptureDialog(W.QDialog):
    """Reads one key combination. Esc cancels. Backspace removes the key."""
    def __init__(self, parent, label):
        super().__init__(parent)
        self.setWindowTitle('Change key')
        self.setModal(True)
        self.result_key = None
        layout = W.QVBoxLayout(self)
        prompt = W.QLabel(f'Press the new key for “{label}”.')
        prompt.setWordWrap(True)
        layout.addWidget(prompt)
        hint = W.QLabel('Press Esc to cancel. Press Backspace to remove the key.')
        hint.setObjectName('muted')
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.setMinimumWidth(320)

    def event(self, event):
        # Tab and Backtab would otherwise move focus before keyPressEvent.
        if event.type() == C.QEvent.Type.ShortcutOverride:
            event.accept()  # Keep application shortcuts from firing while capturing.
            return True
        if event.type() == C.QEvent.Type.KeyPress:
            self.keyPressEvent(event)
            return True
        return super().event(event)

    def keyPressEvent(self, event):
        key = event.key()
        Key = C.Qt.Key
        if key in (Key.Key_Control, Key.Key_Shift, Key.Key_Alt, Key.Key_Meta, Key.Key_AltGr, Key.Key_unknown):
            return
        Mod = C.Qt.KeyboardModifier
        modifiers = event.modifiers() & (Mod.ControlModifier | Mod.ShiftModifier | Mod.AltModifier | Mod.MetaModifier)
        if modifiers == Mod.NoModifier and key == Key.Key_Escape:
            self.reject()
            return
        if modifiers == Mod.NoModifier and key == Key.Key_Backspace:
            self.result_key = ''
            self.accept()
            return
        try:
            sequence = G.QKeySequence(C.QKeyCombination(modifiers, Key(key)))
        except (TypeError, ValueError):
            sequence = G.QKeySequence(event.keyCombination())
        text = sequence.toString(PORTABLE)
        if not text:
            return
        self.result_key = text
        self.accept()


from .ui_preferences import SIZING_LIMITS as _LIMITS
_SIZE_LABELS = {'window_width': 'Window width', 'window_height': 'Window height',
                'panel_width': 'Panel width', 'piano_height': 'Input table height',
                'column_width': 'Column width', 'row_height': 'Row height'}
_PANEL_LABELS = {'inputs': 'Keyboard and mouse', 'console': 'Console', 'score': 'Score',
                 'history': 'History', 'journal': 'Journal', 'branches': 'Branches', 'preview': 'Game view'}


TAB_KEYS, TAB_DISPLAY, TAB_PLAYBACK, TAB_SHORTCUTS = range(4)


class SettingsDialog(W.QDialog):
    def __init__(self, overlay, binder):
        super().__init__(overlay.root)
        self.overlay = overlay
        self.binder = binder
        self.setWindowTitle('Stasis settings')
        self.setModal(True)
        self.resize(760, 560)
        STORE.ensure_loaded()
        try:
            self.unit = overlay._unit()
        except Exception:
            self.unit = 'frame'
        self.pending = dict(STORE.overrides)
        self.external = external_keys(overlay, binder)
        self.settings, settings_warning = ui_settings.load_settings()
        layout = W.QVBoxLayout(self)
        self.banner = W.QLabel()
        self.banner.setWordWrap(True)
        self.banner.setStyleSheet('color:#ffd17b;')
        layout.addWidget(self.banner)
        self.tabs = W.QTabWidget()
        self.tabs.addTab(self._keys_tab(), 'Keys')
        self.tabs.addTab(self._display_tab(), 'Display')
        self.tabs.addTab(self._playback_tab(), 'Playback')
        self.tabs.addTab(self._sheet_tab(), 'Shortcut list')
        self.tabs.currentChanged.connect(lambda index: self.refresh_sheet() if index == TAB_SHORTCUTS else None)
        layout.addWidget(self.tabs)
        Button = W.QDialogButtonBox.StandardButton
        buttons = W.QDialogButtonBox(Button.Save | Button.Apply | Button.Cancel)
        buttons.button(Button.Save).clicked.connect(self.save_and_close)
        buttons.button(Button.Apply).clicked.connect(self.apply_all)
        buttons.button(Button.Cancel).clicked.connect(self.reject)
        layout.addWidget(buttons)
        notes = [STORE.warning] if STORE.warning else []
        if settings_warning:
            notes.append(settings_warning)
        notes += REGISTRY.notes
        unknown = resolve(self.pending, layout=self._layout_keys(), external=self.external).unknown
        if unknown:
            notes.append(unknown_message(unknown))
        self.banner.setText('\n'.join(notes))
        self.banner.setVisible(bool(notes))
        self.refresh_keys()

    # Keys ---------------------------------------------------------------
    def _layout_keys(self):
        return dict(self.overlay.ui_preferences.shortcuts)

    def _keys_tab(self):
        page = W.QWidget()
        layout = W.QVBoxLayout(page)
        self.search = W.QLineEdit()
        self.search.setPlaceholderText('Search actions or keys')
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self.filter_keys)
        layout.addWidget(self.search)
        self.table = W.QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(['Action', 'Category', 'Key', 'Default'])
        self.table.setSelectionBehavior(W.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(W.QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(W.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().hide()
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, W.QHeaderView.ResizeMode.Stretch)
        for column in (1, 2, 3):
            header.setSectionResizeMode(column, W.QHeaderView.ResizeMode.ResizeToContents)
        self.table.cellClicked.connect(lambda row, column: self.capture(row) if column == 2 else None)
        self.table.cellDoubleClicked.connect(lambda row, column: self.capture(row))
        layout.addWidget(self.table)
        note = W.QLabel(GLOBAL_NOTE)
        note.setWordWrap(True)
        note.setObjectName('muted')
        layout.addWidget(note)
        self.key_status = W.QLabel('Click a key to change it.')
        self.key_status.setWordWrap(True)
        layout.addWidget(self.key_status)
        row = W.QHBoxLayout()
        for text, slot in (('Change key', lambda: self.capture(self.table.currentRow())),
                           ('Clear key', self.clear_key), ('Reset key', self.reset_key),
                           ('Reset all keys', self.reset_all), ('Import…', self.import_keys),
                           ('Export…', self.export_keys)):
            button = W.QPushButton(text)
            button.clicked.connect(slot)
            row.addWidget(button)
        row.addStretch()
        layout.addLayout(row)
        return page

    def _action_at(self, row):
        item = self.table.item(row, 0) if row >= 0 else None
        return REGISTRY.get(item.data(C.Qt.ItemDataRole.UserRole)) if item is not None else None

    def refresh_keys(self):
        resolution = resolve(self.pending, layout=self._layout_keys(), external=self.external)
        losers = {}
        for loser, key, holder in resolution.conflicts:
            losers.setdefault(loser, []).append((key, holder))
        current = self.table.currentRow()
        actions = REGISTRY.actions()
        self.table.setRowCount(len(actions))
        for row, action in enumerate(actions):
            keys = resolution.bindings.get(action.id, ())
            key_text = ', '.join(keys) or 'No key'
            tooltip = action.description
            if action.scope == 'global' and keys:
                if game_focus(action, resolution):
                    key_text += ' (Game focus too)'
                    tooltip = GLOBAL_NOTE
                else:
                    key_text += ' (Stasis focus only)'
                    tooltip = 'The game cannot read this key, so it works only while Stasis has focus.'
            if action.id in losers:
                key, holder = losers[action.id][0]
                key_text = f'No key ({key} is used by “{holder}”)'
                tooltip = f'{key} is already used by “{holder}”. Change one of them.'
            if not action.editable:
                tooltip = (action.description + ' ' if action.description else '') + 'This key is fixed.'
            cells = (action.title(self.unit), action.category, key_text, ', '.join(action.defaults) or 'No key')
            for column, text in enumerate(cells):
                item = W.QTableWidgetItem(text)
                item.setToolTip(tooltip or text)
                if column == 0:
                    item.setData(C.Qt.ItemDataRole.UserRole, action.id)
                if not action.editable:
                    item.setForeground(G.QBrush(G.QColor('#91a5b2')))
                elif column == 2 and action.id in losers:
                    item.setForeground(G.QBrush(G.QColor('#ffd17b')))
                self.table.setItem(row, column, item)
        if current >= 0:
            self.table.selectRow(min(current, len(actions) - 1))
        self.filter_keys(self.search.text())

    def filter_keys(self, text):
        needle = text.strip().casefold()
        for row in range(self.table.rowCount()):
            values = [self.table.item(row, column).text() for column in range(4) if self.table.item(row, column)]
            item = self.table.item(row, 0)
            if item is not None:
                values.append(item.data(C.Qt.ItemDataRole.UserRole) or '')
            self.table.setRowHidden(row, bool(needle) and not any(needle in value.casefold() for value in values))

    def capture(self, row):
        action = self._action_at(row)
        if action is None:
            self.key_status.setText('Select an action first.')
            return
        if not action.editable:
            self.key_status.setText(f'The key for “{action.title(self.unit)}” is fixed.')
            return
        dialog = KeyCaptureDialog(self, action.title(self.unit))
        if dialog.exec() != W.QDialog.DialogCode.Accepted or dialog.result_key is None:
            return
        if dialog.result_key == '':
            self.clear_key()
            return
        self.assign(action, dialog.result_key)

    def assign(self, action, key):
        current = resolve(self.pending, layout=self._layout_keys(), external=self.external)
        for other in REGISTRY.actions():
            if other.id != action.id and key in current.bindings.get(other.id, ()) and overlaps(other, action):
                self.key_status.setText(f'{key} is already used by “{other.title(self.unit)}”. Clear that key first, then try again.')
                return
        if key in self.external:
            self.key_status.setText(f'{key} is already used by “{self.external[key]}”. Choose another key.')
            return
        trial = dict(self.pending)
        trial[action.id] = (key,)
        result = resolve(trial, layout=self._layout_keys(), external=self.external)
        if key not in result.bindings.get(action.id, ()):
            reason = next((problem for problem in result.problems if action.title() in problem), '')
            self.key_status.setText(f'{key} cannot be used for “{action.title(self.unit)}”. {reason}'.strip())
            return
        self.pending = trial
        status = f'“{action.title(self.unit)}” now uses {key}. Select Apply to use it.'
        if key in result.not_game_focus.get(action.id, ()):
            # Plain keys stay on the timeline; some keys the game cannot read.
            status += ' ' + next((problem for problem in result.problems
                                  if action.title() in problem and key in problem), '')
        elif action.scope != 'global' and _timeline_only(key):
            status += f' {key} has no Ctrl, Alt or Meta, so it works only while the timeline has focus.'
        self.key_status.setText(status)
        self.refresh_keys()

    def clear_key(self):
        action = self._action_at(self.table.currentRow())
        if action is None or not action.editable:
            self.key_status.setText('Select an action you can change.')
            return
        self.pending[action.id] = ()
        self.key_status.setText(f'“{action.title(self.unit)}” has no key. Select Apply to use this.')
        self.refresh_keys()

    def reset_key(self):
        action = self._action_at(self.table.currentRow())
        if action is None or not action.editable:
            self.key_status.setText('Select an action you can change.')
            return
        # Layout files may hold an older key, so write the default explicitly.
        if action.layout_backed:
            self.pending[action.id] = action.defaults
        else:
            self.pending.pop(action.id, None)
        self.key_status.setText(f'“{action.title(self.unit)}” uses its default key again. Select Apply to use it.')
        self.refresh_keys()

    def reset_all(self):
        Button = W.QMessageBox.StandardButton
        answer = W.QMessageBox.question(self, 'Reset all keys', 'Reset every key to its default?',
                                        Button.Yes | Button.No, Button.No)
        if answer != Button.Yes:
            return
        # Keys for features that have not loaded yet are kept.
        kept = {action_id: keys for action_id, keys in self.pending.items() if action_id not in REGISTRY}
        self.pending = {**kept, **{action.id: action.defaults for action in REGISTRY.actions() if action.layout_backed}}
        self.key_status.setText('All keys use their defaults. Select Apply to use them.')
        self.refresh_keys()

    def import_keys(self):
        name, _ = W.QFileDialog.getOpenFileName(self, 'Import shortcuts', str(Path.home()), 'TOML files (*.toml)')
        if not name:
            return
        overrides, warning = read_keymap_file(Path(name))
        if warning and not overrides:
            self.key_status.setText(warning)
            return
        # A file without the ten layout keys would leave the layout's old keys
        # in place. Fill them with defaults, as Reset all keys does.
        for action in REGISTRY.actions():
            if action.layout_backed and action.id not in overrides:
                overrides[action.id] = action.defaults
        self.pending = overrides
        self.key_status.setText((warning + ' ' if warning else '')
                                + f'Imported {len(overrides)} keys. Select Apply to use them.')
        self.refresh_keys()

    def export_keys(self):
        name, _ = W.QFileDialog.getSaveFileName(self, 'Export shortcuts', str(Path.home() / 'stasis-keymap.toml'),
                                                'TOML files (*.toml)')
        if not name:
            return
        resolution = resolve(self.pending, layout=self._layout_keys(), external=self.external)
        data = {action.id: resolution.bindings.get(action.id, ()) for action in REGISTRY.actions()
                if action.editable}
        data.update({action_id: self.pending[action_id] for action_id in resolution.unknown})
        try:
            write_keymap_file(Path(name), data)
        except (OSError, KeymapError) as exc:
            self.key_status.setText(f'The shortcuts were not exported. {exc}')
            return
        self.key_status.setText(f'Exported the shortcuts to {name}.')

    # Display ------------------------------------------------------------
    def _display_tab(self):
        preferences = self.base = _live_preferences(self.overlay)
        scroll = W.QScrollArea()
        scroll.setWidgetResizable(True)
        page = W.QWidget()
        scroll.setWidget(page)
        form = W.QFormLayout(page)

        def combo(options, value):
            box = W.QComboBox()
            for data, text in options:
                box.addItem(text, data)
            box.setCurrentIndex(max(0, box.findData(value)))
            return box
        self.layout_box = combo((('overlay', 'Overlay'), ('workspace', 'Workspace')), preferences.layout)
        form.addRow('Layout', self.layout_box)
        self.surface_box = combo((('owner_frame', 'Owner frame'), ('native', 'Native game window')),
                                 preferences.game_surface)
        form.addRow('Game picture', self.surface_box)
        self.density_box = combo((('minimal', 'Minimal HUD'), ('timeline', 'Compact timeline'),
                                  ('editor', 'Editor')), preferences.density)
        form.addRow('Detail', self.density_box)
        self.edge_box = combo((('bottom', 'Bottom'), ('left', 'Left')), preferences.timeline_edge)
        form.addRow('Timeline edge', self.edge_box)
        self.follow_box = W.QCheckBox('Keep the playhead in view')
        self.follow_box.setChecked(preferences.follow_playhead)
        form.addRow('Follow playhead', self.follow_box)
        self.pause_box = W.QCheckBox('Pause when TAS input engages')
        self.pause_box.setChecked(preferences.pause_on_engage)
        form.addRow('Engage', self.pause_box)
        self.panel_boxes = {}
        panels = W.QWidget()
        panel_layout = W.QGridLayout(panels)
        panel_layout.setContentsMargins(0, 0, 0, 0)
        for index, (name, text) in enumerate(_PANEL_LABELS.items()):
            box = W.QCheckBox(text)
            box.setChecked(bool(getattr(preferences.panels, name)))
            self.panel_boxes[name] = box
            panel_layout.addWidget(box, index // 3, index % 3)
        form.addRow('Panels', panels)
        self.size_boxes = {}
        for name, (low, high) in _LIMITS.items():
            box = W.QSpinBox()
            box.setRange(low, high)
            box.setSuffix(' px')
            box.setValue(getattr(preferences.sizing, name))
            self.size_boxes[name] = box
            form.addRow(_SIZE_LABELS[name], box)
        self.opacity_box = W.QSpinBox()
        self.opacity_box.setRange(10, 100)
        self.opacity_box.setSuffix(' %')
        states = self.overlay.panel_manager.get_states() if hasattr(self.overlay, 'panel_manager') else {}
        opacities = [state.get('opacity') for name, state in states.items() if name != 'preview' and state.get('opacity')]
        self._initial_opacity = round(100 * (min(opacities) if opacities else 1.0))
        self.opacity_box.setValue(self._initial_opacity)
        self.opacity_box.setEnabled(hasattr(self.overlay, 'panel_manager'))
        form.addRow('Panel opacity', self.opacity_box)
        position = preferences.window_position or (self.overlay.root.x(), self.overlay.root.y())
        place = W.QWidget()
        place_layout = W.QHBoxLayout(place)
        place_layout.setContentsMargins(0, 0, 0, 0)
        self.x_box, self.y_box = W.QSpinBox(), W.QSpinBox()
        for box, value, prefix in ((self.x_box, position[0], 'X '), (self.y_box, position[1], 'Y ')):
            box.setRange(-100000, 100000)
            box.setPrefix(prefix)
            box.setValue(value)
            place_layout.addWidget(box)
        place_layout.addStretch()
        form.addRow('Window position', place)
        self._theme = (preferences.stylesheet, preferences.theme_source)
        theme = W.QWidget()
        theme_layout = W.QHBoxLayout(theme)
        theme_layout.setContentsMargins(0, 0, 0, 0)
        self.theme_label = W.QLabel()
        theme_layout.addWidget(self.theme_label, 1)
        choose = W.QPushButton('Choose theme file…')
        choose.clicked.connect(self.choose_theme)
        theme_layout.addWidget(choose)
        builtin = W.QPushButton('Use built-in colors')
        builtin.clicked.connect(lambda: self._set_theme('', None))
        theme_layout.addWidget(builtin)
        form.addRow('Colors', theme)
        self._set_theme(*self._theme)
        self.motion_box = W.QCheckBox('Reduce motion')
        self.motion_box.setChecked(self.settings.reduced_motion)
        self.motion_box.setToolTip('Features that animate read this setting.')
        form.addRow('Motion', self.motion_box)
        self.display_status = W.QLabel()
        self.display_status.setWordWrap(True)
        form.addRow(self.display_status)
        return scroll

    def _set_theme(self, stylesheet, source):
        self._theme = (stylesheet, source)
        self.theme_label.setText(str(source) if source else ('Session theme' if stylesheet else 'Built-in colors'))

    def choose_theme(self):
        name, _ = W.QFileDialog.getOpenFileName(self, 'Choose theme file', str(Path.home()),
                                                'Qt style sheets (*.qss);;All files (*)')
        if not name:
            return
        from .ui_preferences import PreferenceError, load_stylesheet, read_text
        path = Path(name).absolute()
        try:
            text = load_stylesheet(read_text(path, 256 * 1024), path.parent)
        except (OSError, UnicodeError, PreferenceError) as exc:
            self.display_status.setText(f'The theme was not loaded. {exc}')
            return
        self._set_theme(text, path)
        self.display_status.setText('Select Apply to use the theme.')

    def display_candidate(self):
        from .ui_preferences import PreferenceError
        preferences = self.base
        sizes = {name: box.value() for name, box in self.size_boxes.items()}
        layout = self.layout_box.currentData()
        if layout == 'workspace' and sizes['window_height'] < 600:
            raise PreferenceError('The workspace needs a window height of at least 600 px.')
        if sizes['piano_height'] > sizes['window_height'] - 280:
            raise PreferenceError('The input table is too tall for this window height. '
                                  'Leave at least 280 px for the other controls.')
        panel_names = {item.name for item in fields(preferences.panels)}
        panels = replace(preferences.panels, **{name: box.isChecked() for name, box in self.panel_boxes.items()
                                                if name in panel_names})
        position = (self.x_box.value(), self.y_box.value())
        candidate = replace(preferences, layout=layout, game_surface=self.surface_box.currentData(),
                            density=self.density_box.currentData(), timeline_edge=self.edge_box.currentData(),
                            follow_playhead=self.follow_box.isChecked(), pause_on_engage=self.pause_box.isChecked(),
                            panels=panels, sizing=replace(preferences.sizing, **sizes),
                            stylesheet=self._theme[0], theme_source=self._theme[1],
                            window_position=position)
        if layout != preferences.layout:
            candidate = replace(candidate, panel_states={})
        return candidate

    def apply_display(self):
        candidate = self.display_candidate()
        if candidate != self.base:
            self.overlay.apply_ui_preferences(candidate)
            self.base = _live_preferences(self.overlay)
        opacity = self.opacity_box.value()
        manager = getattr(self.overlay, 'panel_manager', None)
        if manager is not None and opacity != self._initial_opacity:
            manager.set_states({name: {'opacity': opacity / 100} for name in manager.panels if name != 'preview'})
            self._initial_opacity = opacity
        self.display_status.setText('')

    # Playback -----------------------------------------------------------
    def _playback_tab(self):
        page = W.QWidget()
        form = W.QFormLayout(page)
        workspace = self.binder.workspace
        sizer = getattr(workspace, 'play_sizer', None)
        self.batch_frames = W.QSpinBox()
        self.batch_frames.setRange(1, 240)
        self.batch_frames.setValue(getattr(sizer, 'max_ticks', 240))
        form.addRow('Largest Play batch (frames)', self.batch_frames)
        self.batch_seconds = W.QDoubleSpinBox()
        self.batch_seconds.setRange(0.1, 10.0)
        self.batch_seconds.setSingleStep(0.1)
        self.batch_seconds.setDecimals(1)
        self.batch_seconds.setSuffix(' s')
        self.batch_seconds.setValue(getattr(sizer, 'max_wall_s', 1.0))
        form.addRow('Longest Play batch', self.batch_seconds)
        self.preview_ms = W.QSpinBox()
        self.preview_ms.setRange(1, 1000)
        self.preview_ms.setSuffix(' ms')
        self.preview_ms.setValue(getattr(workspace, 'preview_refresh_ms', 16))
        form.addRow('Preview refresh', self.preview_ms)
        self._initial_playback = (self.batch_frames.value(), self.batch_seconds.value(), self.preview_ms.value())
        note = W.QLabel('Play grows each batch up to these limits. Pause takes effect after the current batch. '
                        'Smaller limits make Pause respond sooner.')
        note.setWordWrap(True)
        note.setObjectName('muted')
        form.addRow(note)
        if workspace is None:
            for box in (self.batch_frames, self.batch_seconds, self.preview_ms):
                box.setEnabled(False)
            missing = W.QLabel('These settings apply to the workspace. Open the workspace view to change them.')
            missing.setWordWrap(True)
            form.addRow(missing)
        self.playback_status = W.QLabel()
        self.playback_status.setWordWrap(True)
        form.addRow(self.playback_status)
        return page

    def settings_candidate(self):
        settings = replace(self.settings, reduced_motion=self.motion_box.isChecked())
        values = (self.batch_frames.value(), round(self.batch_seconds.value(), 1), self.preview_ms.value())
        if self.binder.workspace is not None:
            names = ('max_batch_frames', 'max_batch_seconds', 'preview_refresh_ms')
            for name, value, initial in zip(names, values, self._initial_playback):
                # Untouched values stay unset, so built-in pacing still applies.
                if value != initial or getattr(settings, name) is not None:
                    settings = replace(settings, **{name: value})
        return settings

    # Shortcut list ------------------------------------------------------
    def _sheet_tab(self):
        page = W.QWidget()
        layout = W.QVBoxLayout(page)
        self.sheet = W.QTextBrowser()
        layout.addWidget(self.sheet)
        row = W.QHBoxLayout()
        for text, slot in (('Print…', self.print_sheet), ('Copy as text', self.copy_sheet)):
            button = W.QPushButton(text)
            button.clicked.connect(slot)
            row.addWidget(button)
        row.addStretch()
        layout.addLayout(row)
        return page

    def _sheet_rows(self):
        resolution = resolve(self.pending, layout=self._layout_keys(), external=self.external)
        groups = {}
        for action in REGISTRY.actions():
            keys = resolution.bindings.get(action.id, ())
            if keys:
                note = action.description
                if game_focus(action, resolution):
                    note = (note + ' ' if note else '') + 'Also works while the game has focus.'
                groups.setdefault(action.category, []).append((action.title(self.unit), ' or '.join(keys), note))
        return groups

    def refresh_sheet(self):
        parts = ['<h2>Stasis keyboard shortcuts</h2>', f'<p>{html.escape(GLOBAL_NOTE)}</p>']
        for category, rows in sorted(self._sheet_rows().items()):
            parts.append(f'<h3>{html.escape(category)}</h3><table cellpadding="3">')
            for label, keys, note in rows:
                parts.append(f'<tr><td><b>{html.escape(keys)}</b></td><td>{html.escape(label)}'
                             + (f' <i>{html.escape(note)}</i>' if note else '') + '</td></tr>')
            parts.append('</table>')
        self.sheet.setHtml(''.join(parts))

    def copy_sheet(self):
        lines = ['Stasis keyboard shortcuts', GLOBAL_NOTE]
        for category, rows in sorted(self._sheet_rows().items()):
            lines += ['', category]
            lines += [f'  {keys:<24} {label}' for label, keys, _ in rows]
        W.QApplication.clipboard().setText('\n'.join(lines) + '\n')

    def print_sheet(self):
        try:
            from PySide6.QtPrintSupport import QPrintDialog, QPrinter
        except ImportError:
            self.copy_sheet()
            W.QMessageBox.information(self, 'Print', 'Printing is not available here. The list was copied instead.')
            return
        self.refresh_sheet()
        printer = QPrinter()
        dialog = QPrintDialog(printer, self)
        if dialog.exec() == W.QDialog.DialogCode.Accepted:
            self.sheet.document().print_(printer)

    # Apply --------------------------------------------------------------
    def apply_all(self):
        from .ui_preferences import PreferenceError
        ok = True
        if self.pending != STORE.overrides:
            try:
                STORE.save(self.pending)
            except (OSError, KeymapError) as exc:
                self.key_status.setText(f'The keys were not saved. {exc}')
                ok = False
        try:
            self.apply_display()
        except (PreferenceError, ValueError) as exc:
            self.display_status.setText(f'The display settings were not applied. {exc}')
            self.tabs.setCurrentIndex(TAB_DISPLAY)
            ok = False
        settings = self.settings_candidate()
        if settings != self.settings:
            try:
                ui_settings.save_settings(settings)
                self.settings = settings
                ui_settings.set_current(settings)
                if self.binder.workspace is not None:
                    ui_settings.apply_playback(self.binder.workspace, settings)
                self._initial_playback = (self.batch_frames.value(), self.batch_seconds.value(), self.preview_ms.value())
            except (OSError, ValueError) as exc:
                self.playback_status.setText(f'The settings were not saved. {exc}')
                ok = False
        self.refresh_keys()
        return ok

    def save_and_close(self):
        if self.apply_all():
            self.accept()


def open_settings(overlay, tab=TAB_KEYS):
    binder = getattr(overlay, '_shortcut_binder', None)
    if binder is None:
        return
    dialog = getattr(overlay, '_settings_dialog', None)
    try:
        if dialog is not None and dialog.isVisible():
            dialog.tabs.setCurrentIndex(tab)
            dialog.raise_()
            dialog.activateWindow()
            return
    except RuntimeError:
        pass
    dialog = SettingsDialog(overlay, binder)
    dialog.setAttribute(C.Qt.WidgetAttribute.WA_DeleteOnClose, True)
    overlay._settings_dialog = dialog
    dialog.tabs.setCurrentIndex(tab)
    if tab == TAB_SHORTCUTS:
        dialog.refresh_sheet()
    dialog.show()
    dialog.raise_()
    dialog.activateWindow()
