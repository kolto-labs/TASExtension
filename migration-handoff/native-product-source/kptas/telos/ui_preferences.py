"""Versioned presentation preferences, separate from the run plan and environment.

Parse and validate the entire file and optional theme before changing any widget.
There are no owner, game, draft, executable or Python configuration fields.
"""
from dataclasses import dataclass, field, replace
import os
from pathlib import Path
import re
import stat
import tomllib


class PreferenceError(ValueError):
    pass


@dataclass(frozen=True)
class Panels:
    editor: bool | None = False  # Editing starts collapsed unless a saved layout enables it.
    inputs: bool = True
    console: bool = True
    score: bool = True
    history: bool = True
    journal: bool = True
    branches: bool = True
    preview: bool = True


@dataclass(frozen=True)
class Sizing:
    window_width: int = 800
    window_height: int = 600
    panel_width: int = 760
    piano_height: int = 100
    column_width: int = 28
    row_height: int = 16


DEFAULT_SHORTCUTS = (
    ('play_pause', 'Ctrl+F7'), ('step', 'Ctrl+F8'),
    ('rewind', 'Ctrl+F9'), ('apply', 'Ctrl+Alt+Return'),
    ('save', 'Ctrl+Alt+S'), ('toggle_editor', 'Ctrl+F6'),
    ('switch_layout', 'Ctrl+Alt+L'), ('reload_ui', 'Ctrl+Alt+R'),
    ('toggle_engagement', 'Ctrl+Shift+F11'), ('toggle_visibility', 'Ctrl+Shift+F12'),
)

# [ui.sizing] ranges in pixels, shared with the settings dialog.
SIZING_LIMITS = {'window_width': (640, 3840), 'window_height': (480, 2160),
                 'panel_width': (280, 1200), 'piano_height': (75, 600),
                 'column_width': (20, 120), 'row_height': (14, 64)}


@dataclass(frozen=True)
class UIPreferences:
    layout: str = 'overlay'
    game_surface: str = 'owner_frame'
    panels: Panels = field(default_factory=Panels)
    sizing: Sizing = field(default_factory=Sizing)
    shortcuts: tuple[tuple[str, str], ...] = DEFAULT_SHORTCUTS
    stylesheet: str = ''
    source: Path | None = None
    panel_states: dict = field(default_factory=dict)
    density: str = 'timeline'
    timeline_edge: str = 'bottom'
    follow_playhead: bool = True
    window_position: tuple[int, int] | None = None
    selected_layout: str | None = None
    theme_source: Path | None = None
    pause_on_engage: bool = True


def _read_bytes(path, limit):
    """Regular files only, finite reads; FIFOs and devices cannot block startup."""
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NONBLOCK', 0))
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise PreferenceError(f'{path}: expected a regular file')
        if info.st_size > limit:
            raise PreferenceError(f'{path}: exceeds {limit} byte limit')
        with os.fdopen(fd, 'rb') as stream:
            fd = None
            data = stream.read(limit + 1)
        if len(data) > limit:
            raise PreferenceError(f'{path}: exceeds {limit} byte limit')
        return data
    finally:
        if fd is not None:
            os.close(fd)


def _read(path, limit):
    return _read_bytes(path, limit).decode('utf-8')

def _table(value, allowed, label):
    if not isinstance(value, dict):
        raise PreferenceError(f'{label} must be a table')
    unknown = set(value) - set(allowed)
    if unknown:
        raise PreferenceError(f'{label}: unknown keys: {", ".join(sorted(unknown))}')
    return value


def _shortcuts(values):
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QKeySequence
    values = _table(values, dict(DEFAULT_SHORTCUTS), 'ui.shortcuts')
    merged = dict(DEFAULT_SHORTCUTS)
    merged.update(values)
    seen = {}
    for action, value in merged.items():
        if not isinstance(value, str) or len(value) > 80:
            raise PreferenceError(f'ui.shortcuts.{action}: expected a short PortableText sequence')
        if not value:
            continue  # Explicit empty string disables this shortcut.
        sequence = QKeySequence.fromString(value, QKeySequence.PortableText)
        if sequence.isEmpty() or sequence.count() != 1 or sequence[0].key() == Qt.Key_unknown:
            raise PreferenceError(f'ui.shortcuts.{action}: invalid single-key sequence')
        key = sequence[0]
        if key.key() in (Qt.Key_Control, Qt.Key_Shift, Qt.Key_Alt, Qt.Key_Meta):
            raise PreferenceError(f'ui.shortcuts.{action}: expected a key, not a modifier alone')
        canonical = sequence.toString(QKeySequence.PortableText)
        if canonical in seen:
            raise PreferenceError(f'shortcut collision: {action} and {seen[canonical]} use {canonical}')
        seen[canonical] = action
        merged[action] = canonical
    return tuple(merged.items())


def _validate_qss_structure(text):
    """Check rule/declaration structure; Qt remains the property/value parser."""
    inside = False
    quote = None
    escaped = False
    depth = 0
    token = []
    def declaration():
        value = ''.join(token).strip()
        if value:
            name, colon, body = value.partition(':')
            if not colon or not re.fullmatch(r'[-a-zA-Z][\w-]*', name.strip()) or not body.strip():
                raise PreferenceError('malformed theme declaration: expected property: value')
        token.clear()
    for char in text:
        if quote:
            token.append(char)
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in ('"', "'"):
            quote = char;token.append(char)
        elif char == '(':
            depth += 1;token.append(char)
        elif char == ')':
            depth -= 1;token.append(char)
            if depth < 0:raise PreferenceError('malformed theme parentheses')
        elif not depth and char == '{':
            if inside or not ''.join(token).strip():raise PreferenceError('malformed theme rule')
            inside = True;token.clear()
        elif not depth and char == '}':
            if not inside:raise PreferenceError('malformed theme rule')
            declaration();inside = False
        elif not depth and inside and char == ';':
            declaration()
        else:
            token.append(char)
    if quote or depth or inside or ''.join(token).strip():
        raise PreferenceError('incomplete theme rule')


def _stylesheet(text, theme_directory):
    # QSS remains native Qt styling. Resolve local asset paths relative to the
    # theme file so moving the launch directory cannot change its appearance.
    clean = re.sub(r'/\*.*?\*/', '', text, flags=re.S)
    if re.search(r'\bqproperty-|@import', clean, re.I):
        raise PreferenceError('theme may not set widget properties or import stylesheets')
    if clean.count('{') != clean.count('}') or '\x00' in clean:
        raise PreferenceError('malformed theme stylesheet')
    _validate_qss_structure(clean)
    def asset(match):
        value = match.group(1).strip().strip('"\'')
        if value.startswith(':/'):
            return match.group(0)  # Existing application Qt resource.
        if '://' in value or value.startswith('data:'):
            raise PreferenceError('theme assets must be local files or Qt resources')
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = theme_directory / path
        _read_bytes(path, 8 * 1024 * 1024)
        escaped = path.absolute().as_posix().replace('"', '\\"')
        return 'url("' + escaped + '")'
    return re.sub(r'url\s*\(\s*([^)]*)\s*\)', asset, text, flags=re.I)


def load_preferences(path=None, *, layout=None):
    """Return a fully validated candidate or raise; never mutate current UI."""
    try:
        source = Path(path).expanduser().absolute() if path else None
        document = tomllib.loads(_read(source, 64 * 1024)) if source else {'version': 1}
        _table(document, {'version', 'ui'}, 'preferences')
        if type(document.get('version')) is not int or document['version'] != 1:
            raise PreferenceError('preferences version must be 1')
        ui = _table(document.get('ui', {}), {'layout', 'game_surface', 'theme', 'panels', 'sizing', 'shortcuts', 'panel_states', 'density', 'timeline_edge', 'follow_playhead', 'window_position', 'selected_layout', 'pause_on_engage'}, 'ui')
        pause_on_engage = ui.get('pause_on_engage', True)
        if type(pause_on_engage) is not bool:
            raise PreferenceError('ui.pause_on_engage must be a boolean')
        position = ui.get('window_position')
        if position is not None:
            if (not isinstance(position, list) or len(position) != 2 or
                    any(type(value) is not int or not -100000 <= value <= 100000 for value in position)):
                raise PreferenceError('ui.window_position must contain two bounded integer coordinates')
            position = tuple(position)
        selected = ui.get('selected_layout')
        if selected is not None:
            from .ui_profiles import _name
            selected = _name(selected)
        preset = layout if layout is not None else ui.get('layout', 'overlay')
        if preset not in ('overlay', 'workspace'):
            raise PreferenceError('ui.layout must be overlay or workspace')
        surface = ui.get('game_surface', 'owner_frame')
        if surface not in ('owner_frame', 'native'):
            raise PreferenceError('ui.game_surface must be owner_frame or native')
        panel_values = _table(ui.get('panels', {}), Panels.__dataclass_fields__, 'ui.panels')
        if any(type(v) is not bool for v in panel_values.values()):
            raise PreferenceError('ui.panels values must be booleans')
        density=ui.get('density', 'editor' if panel_values.get('editor') else 'timeline')
        if density not in ('minimal','timeline','editor'):
            raise PreferenceError('ui.density must be minimal, timeline or editor')
        edge=ui.get('timeline_edge','bottom')
        if edge not in ('bottom','left'):raise PreferenceError('ui.timeline_edge must be bottom or left')
        follow = ui.get('follow_playhead', True)
        if type(follow) is not bool:
            raise PreferenceError('ui.follow_playhead must be a boolean')
        limits = SIZING_LIMITS
        sizes = _table(ui.get('sizing', {}), limits, 'ui.sizing')
        for key, value in sizes.items():
            low, high = limits[key]
            if type(value) is not int or not low <= value <= high:
                raise PreferenceError(f'ui.sizing.{key} must be an integer from {low} to {high}')
        if preset=='workspace':
            sizes={'window_width':1024,'window_height':768,**sizes}
        sizing = Sizing(**sizes)
        if preset=='workspace' and sizing.window_height<600:
            raise PreferenceError('workspace window_height must be at least 600')
        if sizing.piano_height > sizing.window_height - 280:
            raise PreferenceError('piano_height leaves no room for transport and inputs')
        stylesheet = ''
        theme = None
        if 'theme' in ui:
            if source is None or not isinstance(ui['theme'], str) or not ui['theme'] or len(ui['theme']) > 4096:
                raise PreferenceError('ui.theme must name a stylesheet file')
            theme = Path(ui['theme']).expanduser()
            if not theme.is_absolute():
                theme = source.parent / theme
            stylesheet = _stylesheet(_read(theme, 256 * 1024), theme.parent)
        from .panel_controls import validate_panel_states
        try:
            panel_states = validate_panel_states(ui.get('panel_states', {}))
        except ValueError as exc:
            raise PreferenceError(str(exc)) from exc
        return UIPreferences(preset, surface, Panels(**panel_values), sizing,
                             _shortcuts(ui.get('shortcuts', {})), stylesheet, source, panel_states, density, edge, follow,
                             position, selected, theme.absolute() if theme is not None else None, pause_on_engage)
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise PreferenceError(str(exc)) from exc


def preference_watch_paths(path, previous_theme=None):
    """Finite dependency discovery even while an edited config is incomplete.

    This discovers presentation files only; no imports, executable fields or
    owner operations. Keep the last valid theme watched until a new one parses.
    """
    source = Path(path).expanduser().absolute()
    paths = {source}
    if previous_theme is not None:paths.add(Path(previous_theme))
    try:
        document = tomllib.loads(_read(source,64 * 1024))
        ui = document.get('ui',{})
        theme = ui.get('theme') if isinstance(ui,dict) else None
        if isinstance(theme,str) and theme and len(theme)<=4096 and '\x00' not in theme:
            dependency = Path(theme).expanduser()
            paths.add(dependency.absolute() if dependency.is_absolute() else (source.parent / dependency).absolute())
    except (OSError,UnicodeError,ValueError):
        pass
    return paths


# Public names for helpers other modules use (keymap, ui_settings, settings
# dialog). The underscore names stay for existing callers.
read_bytes = _read_bytes
read_text = _read
validate_shortcuts = _shortcuts
load_stylesheet = _stylesheet


class SessionPreferencesWriter:
    """Latest-only background publication, independent of explicitly saved layouts.

    Payloads contain plain Python values, never Qt objects. One worker serializes
    publications; rapid dragging replaces the pending snapshot instead of queuing
    writes. An interrupted publication retains the previous complete TOML file.
    """
    def __init__(self):
        import threading
        from .ui_profiles import profile_directory
        self.path = profile_directory().parent / 'last-session.toml'
        try:
            self._expected_bytes = _read_bytes(self.path, 64 * 1024)
            self._initial_read_error = None
        except FileNotFoundError:
            self._expected_bytes = None
            self._initial_read_error = None
        except (OSError, ValueError) as exc:
            self._expected_bytes = None
            self._initial_read_error = str(exc)
        self._condition = threading.Condition()
        self._pending = None
        self._stopping = False
        self._result = None
        self._generation = 0
        self._previous_preserved = False
        self._thread = threading.Thread(target=self._run, name='telos-ui-preferences', daemon=True)
        self._thread.start()

    def submit(self, preferences):
        from copy import deepcopy
        snapshot = deepcopy(preferences)
        with self._condition:
            if self._stopping:
                return
            self._generation += 1
            self._pending = (self._generation, snapshot)
            self._condition.notify()

    def take_result(self):
        with self._condition:
            result, self._result = self._result, None
            return result

    def finish(self, timeout=.25):
        # Orderly exit gets a bounded opportunity to finish the latest snapshot.
        # A stalled disk cannot hold gameplay/UI shutdown indefinitely; the last
        # atomically published layout remains recoverable in that case.
        with self._condition:
            self._stopping = True
            self._condition.notify()
        self._thread.join(timeout)
        return not self._thread.is_alive()

    def _run(self):
        import hashlib
        import json
        from kptas.durability import _lock_nb
        from kptas.platform_sync import require_directory_barrier
        from .ui_profiles import _publish, preferences_text
        lock_fd = None
        while True:
            with self._condition:
                while self._pending is None and not self._stopping:
                    self._condition.wait()
                if self._pending is None:
                    if lock_fd is not None:os.close(lock_fd)
                    return
                generation, preferences = self._pending
                self._pending = None
            error = ''
            try:
                if self._initial_read_error:
                    raise PreferenceError('Existing session layout was preserved: ' + self._initial_read_error)
                require_directory_barrier()
                self.path.parent.mkdir(parents=True, exist_ok=True)
                if lock_fd is None:
                    fd = os.open(self.path.with_suffix('.lock'), os.O_RDWR | os.O_CREAT |
                                 getattr(os,'O_NOFOLLOW',0) | getattr(os,'O_NONBLOCK',0),0o600)
                    try:
                        if not stat.S_ISREG(os.fstat(fd).st_mode):
                            raise PreferenceError('Session layout lock must be a regular file')
                        _lock_nb(fd)
                    except (OSError,ValueError):
                        os.close(fd)
                        raise
                    lock_fd = fd
                try:
                    current = _read_bytes(self.path, 64 * 1024)
                except FileNotFoundError:
                    current = None
                if current != self._expected_bytes:
                    raise PreferenceError('Session layout changed in another window or editor; it was preserved. Save a named layout for this window.')
                theme_name = None
                if preferences.stylesheet:
                    theme = preferences.stylesheet.encode('utf-8')
                    theme_name = 'session-theme-' + hashlib.sha256(theme).hexdigest() + '.qss'
                    theme_path = self.path.parent / theme_name
                    try:
                        _publish(theme_path, theme, overwrite=False)
                    except FileExistsError:
                        if _read_bytes(theme_path, 256 * 1024) != theme:
                            raise PreferenceError('The session theme was edited externally; it was preserved.')
                text = preferences_text(preferences, theme_name=theme_name)
                extra = ''
                if preferences.window_position is not None:
                    extra += 'window_position = ' + json.dumps(preferences.window_position) + '\n'
                if preferences.selected_layout is not None:
                    extra += 'selected_layout = ' + json.dumps(preferences.selected_layout) + '\n'
                data = text.replace('[ui]\n', '[ui]\n' + extra, 1).encode('utf-8')
                if len(data) > 64 * 1024:
                    raise PreferenceError('Session layout exceeds the 64 KiB preference limit')
                # Check again after theme publication, before replacing the
                # session file. Named layouts are never part of this operation.
                try:
                    current = _read_bytes(self.path, 64 * 1024)
                except FileNotFoundError:
                    current = None
                if current != self._expected_bytes:
                    raise PreferenceError('Session layout changed externally; it was preserved.')
                if not self._previous_preserved and current is not None:
                    # One bounded recovery file per session, including malformed
                    # external TOML. Do not accumulate a file for every drag.
                    _publish(self.path.with_name('last-session.previous.toml'),current)
                    self._previous_preserved = True
                try:
                    _publish(self.path, data)
                except OSError as exc:
                    if getattr(exc,'published',False):self._expected_bytes = data
                    raise
                self._expected_bytes = data
            except (OSError, ValueError, TypeError) as exc:
                error = str(exc)
            with self._condition:
                # A result from an older in-flight write cannot hide a newer
                # failure/success, or mark an as-yet unwritten snapshot saved.
                if generation == self._generation:
                    self._result = (generation, error)
