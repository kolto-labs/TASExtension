"""Named user actions: stable id, label, category, default keys and scope.

This module has no Qt import. Key strings are Qt PortableText sequences such as
"Ctrl+Alt+F7". The keymap module resolves user overrides and conflicts;
shortcut_binder binds the resolved keys to Qt.

Use the registry from the GUI thread only. Listeners run synchronously inside
register() and set_callback(); a failing listener is skipped, not raised.

Scopes:
- "global": the ten built-in [ui.shortcuts] actions. They also run while the
  game has focus, through the native tool dispatcher. Any other id asking for
  "global" is registered as "window", with one note per action.
- "window": a Qt shortcut while the Stasis window has focus.
- "widget": a Qt shortcut owned by one widget, named by `widget`. It is bound by
  attaching a QAction (shortcut_binder.ShortcutBinder.attach_qaction).
- "x11": fixed chords read by the older X11 fallback. Shown in the shortcut
  list only; not rebindable and not part of conflict checks.

Other features register their own actions at any time:

    from kptas.telos.actions import register_action
    register_action('score.pause', 'Pause the live score', category='Live score',
                    default='Ctrl+Alt+P', callback=self.pause_score)

Registering the same id again updates it in place. It never adds a second
binding. A callback can also be attached later with set_action_callback().
"""
from __future__ import annotations

from dataclasses import dataclass
import re
import sys
from typing import Callable, Iterable

SCOPES = ('global', 'window', 'widget', 'x11')
ACTION_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}')
_ID = ACTION_ID  # Compatibility alias.


@dataclass
class Action:
    id: str
    label: str  # May contain "{unit}", filled by title().
    category: str
    defaults: tuple[str, ...]
    scope: str
    callback: Callable[[], object] | None
    description: str
    widget: str
    editable: bool
    layout_backed: bool  # Primary key in [ui.shortcuts]; extra aliases are window-only.
    order: int

    def title(self, unit='frame'):
        """Label with the time unit, such as frame or tick."""
        return self.label.replace('{unit}', unit)

    def note(self, unit='frame'):
        return self.description.replace('{unit}', unit)


class ActionRegistry:
    def __init__(self):
        self._actions: dict[str, Action] = {}
        self._listeners: list[Callable[[str], None]] = []
        self._counter = 0
        self.notes: list[str] = []

    def _note(self, message):
        if message not in self.notes:
            self.notes.append(message)

    def register(self, action_id, label, *, category='General', default=(), scope='window',
                 callback=None, description='', widget='', editable=True, layout_backed=False):
        if not isinstance(action_id, str) or not ACTION_ID.fullmatch(action_id):
            raise ValueError(f'action id {action_id!r}: use 1-64 letters, digits, dots, dashes, colons or underscores')
        if not isinstance(label, str) or not label.strip():
            raise ValueError(f'action {action_id}: label must be non-empty text')
        if scope not in SCOPES:
            raise ValueError(f'action {action_id}: scope must be one of {", ".join(SCOPES)}')
        if callback is not None and not callable(callback):
            raise ValueError(f'action {action_id}: callback must be callable')
        defaults = _key_tuple(default, action_id)
        existing = self._actions.get(action_id)
        if existing is not None and existing.layout_backed:
            # Built-in ids keep their scope, defaults and layout link. Another
            # caller may only attach a callback or update wording.
            layout_backed, scope, defaults = True, existing.scope, existing.defaults
        if scope == 'global' and not layout_backed:
            # Only [ui.shortcuts] reaches the native table. A global action
            # outside it would look rebindable but never fire with game focus.
            self._note(f'“{label}” works only while Stasis has focus.')
            scope = 'window'
        if scope == 'widget' and not widget and editable:
            widget = action_id
        if existing is not None:
            existing.label = label
            existing.category = category
            existing.defaults = defaults
            existing.scope = scope
            existing.description = description
            existing.widget = widget
            existing.editable = editable
            existing.layout_backed = layout_backed
            if callback is not None:
                existing.callback = callback
            action = existing
        else:
            self._counter += 1
            action = Action(action_id, label, category, defaults, scope, callback,
                            description, widget, editable, layout_backed, self._counter)
            self._actions[action_id] = action
        self._notify(action_id)
        return action

    def set_callback(self, action_id, callback):
        if callback is not None and not callable(callback):
            raise ValueError(f'action {action_id}: callback must be callable')
        action = self._actions.get(action_id)
        if action is None:
            raise KeyError(action_id)
        action.callback = callback
        self._notify(action_id)

    def _notify(self, action_id):
        for listener in list(self._listeners):
            try:
                listener(action_id)
            except Exception as exc:  # One broken listener must not stop registration.
                print(f'kptas actions: listener failed for {action_id}: {exc!r}', file=sys.stderr)

    def get(self, action_id):
        return self._actions.get(action_id)

    def actions(self):
        return sorted(self._actions.values(), key=lambda action: action.order)

    def __contains__(self, action_id):
        return action_id in self._actions

    def subscribe(self, listener):
        if listener not in self._listeners:
            self._listeners.append(listener)

    def unsubscribe(self, listener):
        if listener in self._listeners:
            self._listeners.remove(listener)


def _key_tuple(value, action_id):
    if value is None:
        return ()
    if isinstance(value, str):
        value = (value,)
    if not isinstance(value, Iterable):
        raise ValueError(f'action {action_id}: default must be a key string or a list of key strings')
    keys = []
    for item in value:
        if not isinstance(item, str) or len(item) > 80:
            raise ValueError(f'action {action_id}: each default key must be a short string')
        if item and item not in keys:
            keys.append(item)
    return tuple(keys)


REGISTRY = ActionRegistry()


def register_action(action_id: str, label: str, *, category: str = 'General',
                    default: str | Iterable[str] = (), scope: str = 'window',
                    callback: Callable[[], object] | None = None, description: str = '',
                    widget: str = '') -> Action:
    """Add or update one user action. Call from the GUI thread, before or after the UI exists."""
    return REGISTRY.register(action_id, label, category=category, default=default, scope=scope,
                             callback=callback, description=description, widget=widget)


def set_action_callback(action_id: str, callback: Callable[[], object] | None) -> None:
    REGISTRY.set_callback(action_id, callback)


def _register_builtins():
    from .ui_preferences import DEFAULT_SHORTCUTS
    defaults = dict(DEFAULT_SHORTCUTS)
    builtin = (
        ('play_pause', 'Play or pause', 'Playback', 'Start or stop playback.'),
        ('step', 'Advance one {unit}', 'Playback', 'Apply one {unit} of input, then stay paused.'),
        ('rewind', 'Go back one {unit}', 'Playback', 'Restore the previous recorded {unit}.'),
        ('apply', 'Apply selected draft', 'Editing', 'Commit the selected draft through the owner.'),
        ('save', 'Save inputs', 'File', 'Save the current input drafts.'),
        ('toggle_editor', 'Show or hide the input editor', 'View', ''),
        ('switch_layout', 'Switch layout', 'View', 'Switch between overlay and workspace.'),
        ('reload_ui', 'Reload UI preferences', 'View', 'Also reloads your shortcuts file.'),
        ('toggle_engagement', 'Engage or release TAS input', 'Game', 'Hand input to or from the game.'),
        ('toggle_visibility', 'Show or hide the TAS interface', 'View', ''),
    )
    for action_id, label, category, description in builtin:
        REGISTRY.register(action_id, label, category=category, default=defaults.get(action_id, ''),
                          scope='global', description=description, layout_backed=True)
    window = (
        ('open_settings', 'Open settings', 'View', 'Ctrl+,'),
        ('show_shortcuts', 'Show the shortcut list', 'View', 'F1'),
        ('minimal_hud', 'Show the minimal HUD', 'View', ''),
        ('compact_timeline', 'Show the compact timeline', 'View', ''),
        ('keyboard_details', 'Show or hide keyboard details', 'View', ''),
        ('follow_playhead', 'Turn follow playhead on or off', 'View', ''),
    )
    for action_id, label, category, key in window:
        REGISTRY.register(action_id, label, category=category, default=key, scope='window')
    table = (
        ('undo_input_edit', 'Undo input edit', ('Ctrl+Z',)),
        ('redo_input_edit', 'Redo input edit', ('Ctrl+Shift+Z', 'Ctrl+Y')),
        ('save_input_edits', 'Save input edits', ('Ctrl+S',)),
    )
    for action_id, label, keys in table:
        REGISTRY.register(action_id, label, category='Editing', default=keys, scope='widget',
                          widget='input table', description='Works while the input table has focus.')
    # Fixed keys, listed so the shortcut list is complete.
    older = 'Game focus, on older setups without the native dispatcher. These keys are fixed.'
    fixed = (
        ('fixed.toggle_cell', 'Toggle the selected input cell', 'Editing', ('Space', 'Return', 'Enter'), 'widget',
         'Works while the input table has focus. Enter includes the keypad key.'),
        ('fixed.seek_rail', 'Move along the timeline', 'Timeline',
         ('Left', 'Right', 'Up', 'Down', 'PgUp', 'PgDown', 'Home', 'End'), 'widget',
         'Works while the timeline has focus.'),
        ('fixed.seek_expand', 'Open the input editor from the timeline', 'Timeline', ('Space', 'Return', 'Enter'),
         'widget', 'Works while the timeline has focus.'),
        ('fixed.seek_cancel', 'Cancel a timeline drag', 'Timeline', ('Esc',), 'widget',
         'Works while you drag the timeline handle.'),
        ('legacy.workspace', 'Show the workspace', 'Older runtimes', ('Ctrl+Alt+F6',), 'x11', older),
        ('legacy.play_pause', 'Play or pause', 'Older runtimes', ('Ctrl+Alt+F7',), 'x11', older),
        ('legacy.step', 'Advance one {unit}', 'Older runtimes', ('Ctrl+Alt+F8',), 'x11', older),
        ('legacy.rewind', 'Go back one {unit}', 'Older runtimes', ('Ctrl+Alt+F9',), 'x11', older),
        ('legacy.rewind_mark', 'Go back to the previous bookmark', 'Older runtimes', ('Ctrl+Alt+Shift+F9',), 'x11', older),
        ('legacy.branch', 'Create a branch here', 'Older runtimes', ('Ctrl+Alt+F10',), 'x11', older),
        ('legacy.stop', 'Close the controller', 'Older runtimes', ('Ctrl+Alt+F11',), 'x11', older),
        ('legacy.toggle_visibility', 'Show or hide the TAS interface', 'Older runtimes', ('Ctrl+Shift+F12',), 'x11',
         older),
    )
    for action_id, label, category, keys, scope, description in fixed:
        REGISTRY.register(action_id, label, category=category, default=keys, scope=scope,
                          description=description, editable=False)


_register_builtins()
