"""User key overrides in keymap.toml, conflict checks and the resolved key table.

File: $XDG_CONFIG_HOME/kptas/keymap.toml (normally ~/.config/kptas/keymap.toml).

    version = 2
    alias_scope = "host-window"

    [keys]
    "play_pause" = "Ctrl+F7"                 # one key
    "redo_input_edit" = ["Ctrl+Shift+Z", "Ctrl+Y"]  # several keys
    "open_settings" = ""                         # no key

Precedence per action: keymap.toml, then the layout's [ui.shortcuts] value
(layout-backed actions only), then the registered default.

Conflicts: actions are checked in registration order. When two actions in
overlapping scopes ask for one key, the earlier one keeps it, the later one is
unbound, and one warning names both. Unknown ids are kept in the file, because
their feature may register later, and they are reported.

Loading never raises. A broken file gives default keys and one plain warning.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
import json
import sys
import threading
import tomllib

from .actions import REGISTRY, ACTION_ID as _ID

LIMIT = 64 * 1024


class KeymapError(ValueError):
    pass


def keymap_path():
    from .ui_profiles import profile_directory
    return profile_directory().parent / 'keymap.toml'


def canonical_key(text):
    """One Qt PortableText key combination, canonical form; raises ValueError."""
    if not isinstance(text, str) or not text or len(text) > 80:
        raise ValueError('expected a short key combination')
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QKeySequence
    sequence = QKeySequence.fromString(text, QKeySequence.PortableText)
    if sequence.isEmpty() or sequence.count() != 1 or sequence[0].key() == Qt.Key_unknown:
        raise ValueError(f'"{text}" is not a single key combination')
    if sequence[0].key() in (Qt.Key_Control, Qt.Key_Shift, Qt.Key_Alt, Qt.Key_Meta):
        raise ValueError(f'"{text}" is a modifier without a key')
    return sequence.toString(QKeySequence.PortableText)


def _keys(value, action_id):
    if isinstance(value, str):
        return (value,) if value else ()
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return tuple(dict.fromkeys(item for item in value if item))
    raise KeymapError('expected a key, a list of keys, or ""')


def parse_keymap(data):
    """Bytes -> ({id: keys}, [problem]). Raises KeymapError only for a broken file."""
    try:
        document = tomllib.loads(data.decode('utf-8'))
    except (UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise KeymapError(str(exc)) from exc
    problems = []
    version = document.get('version', 1)
    if type(version) is not int or version not in (1, 2):
        raise KeymapError('version must be 1 or 2')
    if version == 2 and document.get('alias_scope') != 'host-window':
        raise KeymapError('version 2 requires alias_scope = "host-window"')
    extra = sorted(set(document) - ({'version', 'keys', 'alias_scope'} if version == 2 else {'version', 'keys'}))
    if extra:
        problems.append('Ignored unknown sections: ' + ', '.join(extra) + '.')
    table = document.get('keys', {})
    if not isinstance(table, dict):
        raise KeymapError('[keys] must be a table')
    flat = {}

    def walk(prefix, values):
        # An unquoted dotted id such as score.pause parses as a nested table.
        for name, value in values.items():
            action_id = prefix + name
            if isinstance(value, dict):
                walk(action_id + '.', value)
            else:
                flat[action_id] = value
    walk('', table)
    overrides = {}
    for action_id, value in flat.items():
        if not _ID.fullmatch(action_id):
            problems.append(f'Ignored "{action_id}": not a valid action id.')
            continue
        try:
            keys = _keys(value, action_id)
            overrides[action_id] = tuple(canonical_key(key) for key in keys)
        except (KeymapError, ValueError) as exc:
            problems.append(f'Ignored {action_id}: {exc}.')
    return overrides, problems


def keymap_text(overrides):
    quote = lambda value: json.dumps(value, ensure_ascii=False)
    lines = ['# Stasis keyboard shortcuts. Change them in Settings or edit this file.',
             '# Each line maps an action id to a key, a list of keys, or "" for no key.',
             '# Only the primary binding may also run with native game focus.',
             '# Additional bindings apply only in the host interface window.',
             'version = 2', 'alias_scope = "host-window"', '', '[keys]']
    for action_id in sorted(overrides):
        keys = tuple(overrides[action_id])
        value = quote(keys[0]) if len(keys) == 1 else quote('') if not keys else quote(list(keys))
        lines.append(f'{quote(action_id)} = {value}')
    data = ('\n'.join(lines) + '\n').encode('utf-8')
    if len(data) > LIMIT:
        raise KeymapError('the shortcuts file would exceed 64 KiB')
    return data


def read_keymap_file(path):
    """Path -> ({id: keys}, warning text). Never raises."""
    from .ui_preferences import read_bytes
    try:
        data = read_bytes(path, LIMIT)
    except FileNotFoundError:
        return {}, ''
    except (OSError, ValueError) as exc:
        return {}, f'Default keys are in use because {path} could not be read: {exc}'
    try:
        overrides, problems = parse_keymap(data)
    except KeymapError as exc:
        return {}, f'Default keys are in use because {path} is not valid: {exc}'
    except Exception as exc:  # A Qt import failure must not stop the UI.
        return {}, f'Default keys are in use because {path} could not be checked: {exc}'
    if problems:
        return overrides, f'Some entries in {path} were skipped. ' + ' '.join(problems)
    return overrides, ''


def write_keymap_file(path, overrides):
    from .ui_profiles import publish_file
    data = keymap_text(overrides)
    path.parent.mkdir(parents=True, exist_ok=True)
    publish_file(path, data)


@dataclass
class Resolution:
    bindings: dict = field(default_factory=dict)  # id -> tuple of canonical keys
    conflicts: list = field(default_factory=list)  # (loser id, key, holder label)
    problems: list = field(default_factory=list)
    unknown: list = field(default_factory=list)
    not_game_focus: dict = field(default_factory=dict)  # global id -> keys that skip the game-focus table


def game_focus(action, resolution):
    """True when at least one key of this action also works while the game has focus."""
    if action.scope != 'global':
        return False
    skipped = set(resolution.not_game_focus.get(action.id, ()))
    return any(key not in skipped for key in resolution.bindings.get(action.id, ()))


def unknown_message(ids):
    """One wording for ids in the shortcuts file that no loaded feature knows."""
    return ('Your shortcuts file has keys for actions that are not loaded: ' + ', '.join(ids)
            + '. They are kept for when those features load.')


def overlaps(first, second):
    """Shortcut contexts that can both receive the same key press."""
    if first.scope == 'x11' or second.scope == 'x11' or not first.editable or not second.editable:
        return False
    if first.scope == 'widget' and second.scope == 'widget':
        return first.widget == second.widget
    return True


def _native_identity(key):
    """(vk, mask) for the native table, None for timeline-only keys; raises if unreadable."""
    from .xhotkeys import native_chord
    vk, mask, function = native_chord(key)
    return (vk, mask) if mask or function else None


def resolve(overrides, *, layout=None, external=None, registry=REGISTRY):
    """Resolve keys for every registered action.

    external maps canonical keys to a label for Qt shortcuts that are not in
    the registry. They hold their key first, so a registered action asking for
    it gets no key instead of an ambiguous shortcut that never fires.
    """
    layout = dict(layout or {})
    result = Resolution()
    claimed = {}
    native = {}
    external = dict(external or {})
    for action in registry.actions():
        if not action.editable or action.scope == 'x11':
            result.bindings[action.id] = action.defaults
            continue
        candidates = []
        if action.id in overrides:
            candidates.append(('your shortcuts file', tuple(overrides[action.id])))
        if action.layout_backed and action.id in layout:
            candidates.append(('the layout', (layout[action.id],) if layout[action.id] else ()))
        candidates.append(('the default', action.defaults))
        chosen = ()
        for origin, keys in candidates:
            try:
                chosen = [canonical_key(key) for key in keys]
                break
            except Exception as exc:
                result.problems.append(f'{action.title()}: the key from {origin} was not used. ' + str(exc).rstrip('.') + '.')
        keys = []
        for key in dict.fromkeys(chosen):  # One action never conflicts with itself.
            holder = next((other.title() for other in claimed.get(key, ()) if overlaps(other, action)), None)
            if holder is None and key in external:
                holder = external[key]
            if holder is not None:
                result.conflicts.append((action.id, key, holder))
                result.problems.append(f'{key} is set for both “{holder}” and “{action.title()}”. '
                                       f'This binding for “{action.title()}” was skipped.')
                if action.layout_backed and key == chosen[0]:
                    # Never promote a window-only alias into the native slot.
                    break
                continue
            keys.append(key)
            claimed.setdefault(key, []).append(action)
            if action.scope != 'global':
                continue
            # The layout/native protocol has one slot per action. The binder
            # owns subsequent aliases only while the interface has focus.
            if action.layout_backed and len(keys) > 1:
                result.not_game_focus.setdefault(action.id, []).append(key)
                result.problems.append(f'“{action.title()}” alias {key} works only while Stasis has focus.')
                continue
            # The Qt shortcut always stays. Only the game-focus binding is skipped.
            try:
                identity = _native_identity(key)
            except ValueError:
                result.not_game_focus.setdefault(action.id, []).append(key)
                result.problems.append(f'“{action.title()}” uses {key} only while Stasis has focus. '
                                       'The game cannot read this key.')
                continue
            if identity is None:
                # No Ctrl, Alt or Meta and not a function key: Qt keeps it on the timeline.
                result.not_game_focus.setdefault(action.id, []).append(key)
                result.problems.append(f'“{action.title()}” uses {key} only while the timeline has focus.')
                continue
            if identity in native:
                other_id, other_key = native[identity]
                other = registry.get(other_id)
                result.not_game_focus.setdefault(action.id, []).append(key)
                result.problems.append(f'“{action.title()}” uses {key} only while Stasis has focus. '
                                       f'The game reads it as {other_key} for “{other.title() if other else other_id}”.')
                continue
            native[identity] = (action.id, key)
        result.bindings[action.id] = tuple(keys)
    result.unknown = sorted(action_id for action_id in overrides if action_id not in registry)
    return result


class KeymapStore:
    """The loaded keymap.toml. One per process; listeners hear saves and reloads."""
    def __init__(self, path=None):
        self._lock = threading.RLock()
        self.path = path
        self.overrides = {}
        self.warning = ''
        self.loaded = False
        self._listeners = []
        self._messages = []
        self._reported = set()

    def ensure_loaded(self):
        with self._lock:
            if not self.loaded:
                self.reload(notify=False)

    def reload(self, *, notify=True):
        with self._lock:
            if self.path is None:
                self.path = keymap_path()
            self.overrides, self.warning = read_keymap_file(self.path)
            self.loaded = True
            if self.warning:
                self.report(self.warning)
        if notify:
            self._notify()

    def save(self, overrides):
        """Publish atomically; raises OSError or KeymapError, leaving the old file."""
        with self._lock:
            if self.path is None:
                self.path = keymap_path()
            clean = {action_id: tuple(keys) for action_id, keys in overrides.items()}
            try:
                write_keymap_file(self.path, clean)
            except OSError as exc:
                if not getattr(exc, 'published', False):
                    raise
                self.report(str(exc))
            self.overrides = clean
            self.warning = ''
            self.loaded = True
        self._notify()

    def resolve(self, *, layout=None, external=None):
        self.ensure_loaded()
        with self._lock:
            overrides = dict(self.overrides)
        result = resolve(overrides, layout=layout, external=external)
        for problem in result.problems:
            self.report(problem)
        # Unknown ids are reported by the binder after startup and in Settings;
        # features register late, so reporting here would warn falsely.
        return result

    def report_unknown(self, unknown):
        if unknown:
            self.report(unknown_message(unknown))

    def report(self, message):
        with self._lock:
            if message and message not in self._reported:
                self._reported.add(message)
                self._messages.append(message)

    def take_messages(self):
        with self._lock:
            messages, self._messages = self._messages, []
            return messages

    def subscribe(self, listener):
        with self._lock:
            if listener not in self._listeners:
                self._listeners.append(listener)

    def unsubscribe(self, listener):
        with self._lock:
            if listener in self._listeners:
                self._listeners.remove(listener)

    def _notify(self):
        with self._lock:
            listeners = list(self._listeners)
            # A later save may bring the same problem back; let it report again.
            self._reported.clear()
        for listener in listeners:
            listener()


STORE = KeymapStore()


def action_keys(action_id, *, layout=None):
    """Resolved keys for one action, for tooltips and menus."""
    return STORE.resolve(layout=layout).bindings.get(action_id, ())


_external_provider = None


def set_external_provider(provider):
    """Callable returning {key: label} for Qt shortcuts outside the registry."""
    global _external_provider
    _external_provider = provider


def _external():
    try:
        return _external_provider() if _external_provider is not None else None
    except Exception:  # The window may be gone.
        return None


def merge_layout_shortcuts(candidate, external=None):
    """UIPreferences with keymap.toml applied to its [ui.shortcuts]; never raises.

    The result keeps the same ids, order and canonical strings, and passes the
    layout validator. Keys the game cannot read stay as Qt shortcuts;
    xhotkeys.native_bindings leaves them out of the game-focus table. Anything
    else returns the candidate unchanged and reports why.
    """
    try:
        if external is None:
            external = _external()
        resolution = STORE.resolve(layout=dict(candidate.shortcuts), external=external)
        merged = []
        for action_id, value in candidate.shortcuts:
            if action_id in resolution.bindings:
                keys = resolution.bindings[action_id]
                value = keys[0] if keys else ''
            merged.append((action_id, value))
        merged = tuple(merged)
        if merged == tuple(candidate.shortcuts):
            return candidate
        from .ui_preferences import validate_shortcuts
        checked = validate_shortcuts(dict(merged))
        if checked == tuple(candidate.shortcuts):
            return candidate
        return replace(candidate, shortcuts=checked)
    except Exception as exc:
        print(f'kptas keymap: custom keys not applied: {exc!r}', file=sys.stderr)
        STORE.report('Your custom keys were not applied, so the layout keys are in use. '
                     'Open Settings to check them.')
        return candidate
