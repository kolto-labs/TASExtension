"""Bind the action registry to Qt: window shortcuts, QAction keys, menu labels.

attach_overlay(overlay) runs once at the end of Overlay.__init__. It adds the
Settings menu item and a ShortcutBinder. attach_workspace(workspace) runs at
the end of the workspace window setup. It binds the input table actions and
applies saved playback pacing. Both never raise.

Layout-backed actions (the ten [ui.shortcuts] ids) keep their existing path:
Overlay.apply_ui_preferences builds their Qt shortcuts and sends them to the
native dispatcher. keymap.merge_layout_shortcuts feeds it the user overrides.
Extra layout-action aliases are window-only and bound here. Every other
action gets its Qt shortcut or QAction keys from this binder.
"""
from __future__ import annotations

import sys

from PySide6 import QtCore as C, QtGui as G

from .actions import REGISTRY, set_action_callback
from .keymap import STORE, merge_layout_shortcuts
from . import ui_settings

PORTABLE = G.QKeySequence.SequenceFormat.PortableText


def _sequence(key):
    return G.QKeySequence.fromString(key, PORTABLE)


def _timeline_only(key):
    """No Ctrl, Alt or Meta and not a function key: Qt keeps it on the timeline."""
    parts = key.split('+')
    return not (set(parts[:-1]) & {'Ctrl', 'Alt', 'Meta'}) and not (parts[-1][:1] == 'F' and parts[-1][1:].isdigit())


def external_keys(overlay, binder):
    """Keys of Qt shortcuts in the Stasis window that the registry does not manage."""
    own = {id(item) for item in binder.shortcuts} | {id(item) for item in getattr(overlay, 'ui_shortcuts', ())}
    own_actions = {id(item) for items in binder.qactions.values() for item in items}
    found = {}
    try:
        for shortcut in overlay.root.findChildren(G.QShortcut):
            if id(shortcut) in own or not shortcut.isEnabled():
                continue
            label = shortcut.objectName() or shortcut.whatsThis() or 'another Stasis control'
            for sequence in shortcut.keys():
                text = sequence.toString(PORTABLE)
                if text:
                    found.setdefault(text, label)
        for action in overlay.root.findChildren(G.QAction):
            label = action.text().replace('&', '').split('\t')[0] or 'another Stasis control'
            # attach_qaction tags registry-owned QActions.
            if id(action) in own_actions or action.property('stasisAction'):
                continue
            for sequence in action.shortcuts():
                text = sequence.toString(PORTABLE)
                if text:
                    found.setdefault(text, label)
    except RuntimeError:
        pass
    return found


GLOBAL_NOTE = ('For actions marked “Game focus too”, only the first binding works with game focus; '
               'additional aliases work while Stasis has focus. '
               'Older setups can’t read them there. '
               'They use the fixed keys listed under Older runtimes.')


def _live_preferences(overlay):
    """Preferences with the live window size, position and panels.

    overlay.ui_preferences does not follow window drags or resizes. Applying
    it as-is would snap the window back, so start from the session snapshot.
    """
    snapshot = getattr(overlay, '_session_preferences_snapshot', None)
    if callable(snapshot):
        try:
            return snapshot()
        except Exception:
            pass
    return overlay.ui_preferences


class ShortcutBinder(C.QObject):
    changed = C.Signal()

    def __init__(self, overlay):
        super().__init__(overlay.root)
        self.overlay = overlay
        self.workspace = None
        self.shortcuts = []
        self.qactions = {}
        self.menu_actions = {}
        self._signature = None
        self._keymap_dirty = False
        self._notes_shown = 0
        self._rebuild_pending = False
        self._slider_retries = 0
        self.changed.connect(self.rebuild, C.Qt.ConnectionType.QueuedConnection)
        REGISTRY.subscribe(self._registry_changed)
        STORE.subscribe(self._keymap_changed)
        overlay.root.destroyed.connect(self._detach)
        from .keymap import set_external_provider
        set_external_provider(lambda: external_keys(overlay, self))
        self._emit()
        # Features register their actions after the window is built. Check
        # for unknown ids once startup has settled, not at the first rebuild.
        C.QTimer.singleShot(2000, self, self._report_unknown)

    def _report_unknown(self):
        resolution = STORE.resolve(layout=dict(self.overlay.ui_preferences.shortcuts),
                                   external=external_keys(self.overlay, self))
        STORE.report_unknown(resolution.unknown)
        for message in STORE.take_messages():
            self.console(message)

    def _detach(self, *_):
        REGISTRY.unsubscribe(self._registry_changed)
        STORE.unsubscribe(self._keymap_changed)

    def _emit(self):
        if self._rebuild_pending:
            return  # One queued rebuild covers every change before it runs.
        self._rebuild_pending = True
        try:
            self.changed.emit()
        except RuntimeError:  # The window is gone.
            self._detach()

    def _registry_changed(self, _action_id):
        self._emit()

    def _keymap_changed(self):
        self._keymap_dirty = True
        self._emit()

    def attach_qaction(self, action_id, qaction):
        """Keep a QAction's keys equal to the resolved keys of action_id."""
        qaction.setProperty('stasisAction', action_id)
        self.qactions.setdefault(action_id, []).append(qaction)
        self._signature = None
        self._emit()

    def show_in_menu(self, action_id, qaction, text):
        """Show the resolved key next to a menu item without binding it twice."""
        self.menu_actions[action_id] = (qaction, text)
        self._signature = None
        self._emit()

    def console(self, message):
        console = getattr(self.overlay, 'console', None)
        if console is not None:
            console.appendPlainText('Keyboard shortcuts: ' + message)

    def rebuild(self):
        self._rebuild_pending = False
        overlay = self.overlay
        if not hasattr(overlay, 'slider'):
            self._slider_retries += 1
            if self._slider_retries <= 50:
                C.QTimer.singleShot(100, self, self.rebuild)
            return
        if self._keymap_dirty:
            self._keymap_dirty = False
            base = _live_preferences(overlay)
            merged = merge_layout_shortcuts(base, external_keys(overlay, self))
            if merged.shortcuts != base.shortcuts:
                try:
                    overlay.apply_ui_preferences(merged)
                except Exception as exc:
                    print(f'kptas shortcuts: layout keys not updated: {exc!r}', file=sys.stderr)
                    STORE.report('Layout keys were not updated. Open Settings to check them.')
        resolution = STORE.resolve(layout=dict(overlay.ui_preferences.shortcuts),
                                   external=external_keys(overlay, self))
        window = [action for action in REGISTRY.actions()
                  if action.editable and (action.layout_backed or action.scope == 'window')]
        signature = (tuple((action.id, resolution.bindings.get(action.id, ()), action.callback is not None)
                           for action in window),
                     tuple((action_id, resolution.bindings.get(action_id, ()), len(items))
                           for action_id, items in self.qactions.items()),
                     tuple(sorted(self.menu_actions)))
        if signature != self._signature:
            self._signature = signature
            for shortcut in self.shortcuts:
                shortcut.setEnabled(False)
                shortcut.deleteLater()
            self.shortcuts = []
            for action in window:
                if action.callback is None and not action.layout_backed:
                    continue  # Not available in this window; leave the key free.
                keys = resolution.bindings.get(action.id, ())
                # apply_ui_preferences owns the primary; never bind it twice.
                for key in (keys[1:] if action.layout_backed else keys):
                    sequence = _sequence(key)
                    scoped = overlay.timeline_scoped_shortcut(sequence)
                    shortcut = G.QShortcut(sequence, overlay.slider if scoped else overlay.root)
                    shortcut.setContext(C.Qt.ShortcutContext.WidgetShortcut if scoped
                                        else C.Qt.ShortcutContext.WindowShortcut)
                    shortcut.setAutoRepeat(False)
                    shortcut.activated.connect(lambda action_id=action.id: self.activate(action_id))
                    self.shortcuts.append(shortcut)
            for action_id, items in list(self.qactions.items()):
                sequences = [_sequence(key) for key in resolution.bindings.get(action_id, ())]
                alive = []
                for qaction in items:
                    try:
                        qaction.setShortcuts(sequences)
                        alive.append(qaction)
                    except RuntimeError:
                        pass
                self.qactions[action_id] = alive
            for action_id, (qaction, text) in list(self.menu_actions.items()):
                keys = resolution.bindings.get(action_id, ())
                try:
                    qaction.setText(text + ('\t' + keys[0] if keys else ''))
                except RuntimeError:
                    del self.menu_actions[action_id]
        notes = REGISTRY.notes[self._notes_shown:]
        self._notes_shown = len(REGISTRY.notes)
        for message in notes + STORE.take_messages():
            self.console(message)

    def activate(self, action_id):
        action = REGISTRY.get(action_id)
        callback = action.callback if action is not None else None
        if action is not None and action.layout_backed:
            overlay = self.overlay
            queued = {'play_pause': 'play_pause', 'step': 'step', 'rewind': 'rewind',
                      'save': 'save', 'apply': 'apply_selected'}
            if action_id in queued:
                if action_id == 'apply' and overlay.presentation is None:
                    return False
                callback = lambda: overlay.queue(queued[action_id])
            else:
                callback = {
                    'toggle_editor': overlay.toggle, 'switch_layout': overlay.switch_layout,
                    'reload_ui': overlay.reload_ui_preferences,
                    'toggle_engagement': overlay.interaction_controls.toggle_engagement,
                    'toggle_visibility': overlay.interaction_controls.toggle_visibility,
                }.get(action_id)
        if callback is None:
            return False

        def run():
            try:
                callback()
            except Exception as exc:
                print(f'kptas shortcuts: {action.id} failed: {exc!r}', file=sys.stderr)
                self.console(f'“{action.title()}” did not finish.')
        if getattr(self.overlay, 'interaction_controls', None) is not None:
            from .interaction_controls import activate_shortcut
            return activate_shortcut(self.overlay, action_id, run)
        run()
        return True


def attach_overlay(overlay):
    """Add the Settings menu item and bind registry shortcuts. Never raises."""
    from .settings_dialog import TAB_SHORTCUTS, open_settings
    try:
        binder = ShortcutBinder(overlay)
        overlay._shortcut_binder = binder
        menu_action = overlay.more_menu.addAction('Settings…')
        menu_action.triggered.connect(lambda: open_settings(overlay))
        binder.show_in_menu('open_settings', menu_action, 'Settings…')
        callbacks = {
            'open_settings': lambda: open_settings(overlay),
            'show_shortcuts': lambda: open_settings(overlay, tab=TAB_SHORTCUTS),
            'minimal_hud': lambda: overlay.set_density('minimal'),
            'compact_timeline': lambda: overlay.set_density('timeline'),
            'keyboard_details': lambda: overlay.keyboard_action.toggle() if overlay.keyboard_action.isEnabled() else None,
        }
        for action_id, callback in callbacks.items():
            set_action_callback(action_id, callback)
    except Exception as exc:
        console = getattr(overlay, 'console', None)
        if console is not None:
            print(f'kptas shortcuts: attach_overlay failed: {exc!r}', file=sys.stderr)
            console.appendPlainText('Keyboard shortcuts: Settings are unavailable in this window.')


def attach_workspace(workspace):
    """Bind the input table actions and apply saved playback pacing. Never raises."""
    binder = getattr(workspace.overlay, '_shortcut_binder', None)
    if binder is None:
        return
    try:
        binder.workspace = workspace
        for action_id, name in (('undo_input_edit', 'undo_action'), ('redo_input_edit', 'redo_action'),
                                ('save_input_edits', 'save_inputs_action')):
            qaction = getattr(workspace, name, None)
            if qaction is not None:
                binder.attach_qaction(action_id, qaction)
        follow = getattr(workspace, 'follow_action', None)
        if follow is not None:
            set_action_callback('follow_playhead', follow.toggle)
        settings, warning = ui_settings.load_settings()
        ui_settings.set_current(settings)
        if warning:
            binder.console(warning)
        ui_settings.apply_playback(workspace, settings)
    except Exception as exc:
        print(f'kptas shortcuts: attach_workspace failed: {exc!r}', file=sys.stderr)
        binder.console('Workspace shortcuts or playback settings were not applied.')
