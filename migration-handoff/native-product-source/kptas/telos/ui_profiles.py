"""Named, editable TOML layouts. No recording or native-session operations."""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

from kptas.platform_sync import sync_dir
from .ui_preferences import UIPreferences, PreferenceError, load_preferences, _read_bytes


class ProfileDurabilityError(OSError):
    """Publication succeeded, but confirming the directory barrier did not."""
    def __init__(self, path, cause):
        self.path = path
        self.published = True
        super().__init__(f'Layout was written to {path}, but disk synchronization failed: {cause}')


def profile_directory() -> Path:
    base = os.environ.get('XDG_CONFIG_HOME')
    return (Path(base) if base else Path.home() / '.config') / 'kptas' / 'layouts'


def _name(name: str) -> str:
    if (not isinstance(name, str) or name != name.strip() or
            not re.fullmatch(r'[\w][\w .-]{0,63}', name) or name.endswith('.') or
            name.split('.')[0].upper() in {'CON', 'PRN', 'AUX', 'NUL',
                                         *('COM'+str(i) for i in range(1,10)),
                                         *('LPT'+str(i) for i in range(1,10))}):
        raise PreferenceError('Use a layout name of 1–64 letters, numbers, spaces, dots, dashes or underscores.')
    return name


def _publish(path: Path, data: bytes, *, overwrite=True):
    """An interrupted write retains the previous complete profile."""
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if overwrite:
            os.replace(temporary, path)
        else:
            os.link(temporary, path)
        try:
            sync_dir(path.parent)
        except OSError as exc:
            raise ProfileDurabilityError(path, exc) from exc
    finally:
        Path(temporary).unlink(missing_ok=True)


publish_file = _publish  # Public name for keymap and ui_settings.


def preferences_text(preferences: UIPreferences, *, theme_name=None, panel_states=None):
    from .panel_controls import validate_panel_states
    states = validate_panel_states(preferences.panel_states if panel_states is None else panel_states)
    quote = lambda value: json.dumps(value, ensure_ascii=False)
    lines = ['version = 1', '', '[ui]', f'layout = {quote(preferences.layout)}',
             f'game_surface = {quote(preferences.game_surface)}', f'density = {quote(preferences.density)}',
             f'timeline_edge = {quote(preferences.timeline_edge)}',
             f'follow_playhead = {quote(preferences.follow_playhead)}',
             f'pause_on_engage = {quote(preferences.pause_on_engage)}']
    if theme_name:
        lines.append('theme = ' + quote(theme_name))
    sections = [('ui.panels', asdict(preferences.panels)),
                ('ui.sizing', asdict(preferences.sizing)),
                ('ui.shortcuts', dict(preferences.shortcuts))]
    sections += [('ui.panel_states.' + quote(name), values) for name, values in sorted(states.items())]
    for section, values in sections:
        lines.extend(('', '[' + section + ']'))
        for key, value in values.items():
            if value is not None:
                lines.append(f'{key} = {quote(value)}')
    return '\n'.join(lines) + '\n'


class LayoutProfiles:
    def __init__(self, directory=None):
        self.directory = Path(directory) if directory is not None else profile_directory()
        self.errors: dict[str, str] = {}

    def path(self, name):
        return self.directory / (_name(name) + '.toml')

    def names(self):
        """Bad external profiles stay on disk and are reported separately."""
        self.errors = {}
        names = []
        if not self.directory.exists():
            return names
        for path in sorted(self.directory.glob('*.toml'), key=lambda p: p.name.casefold()):
            try:
                self.load(path.stem)
                names.append(path.stem)
            except (ValueError, OSError) as exc:
                self.errors[path.stem] = str(exc)
        return names

    def load(self, name):
        return load_preferences(self.path(name))

    def save(self, name, preferences, *, panel_states=None, overwrite=False):
        path = self.path(name)
        self.directory.mkdir(parents=True, exist_ok=True)
        if path.exists() and not overwrite:
            raise PreferenceError('A layout with that name already exists.')
        # Validate state before creating any artifacts or touching an existing theme.
        preferences_text(preferences, panel_states=panel_states)
        # Theme objects are immutable. An external edit is preserved, not overwritten.
        theme_name = None
        if preferences.stylesheet:
            data = preferences.stylesheet.encode('utf-8')
            theme_name = 'theme-' + hashlib.sha256(data).hexdigest() + '.qss'
            theme_path = self.directory / theme_name
            if theme_path.exists():
                if _read_bytes(theme_path, 256 * 1024) != data:
                    raise PreferenceError('The saved theme was edited externally; reload it before saving this layout.')
            else:
                _publish(theme_path, data, overwrite=False)
        data = preferences_text(preferences, theme_name=theme_name, panel_states=panel_states).encode('utf-8')
        # Exercise the same reader as user-edited files before replacing any profile.
        fd, candidate = tempfile.mkstemp(prefix='.validate-', suffix='.toml', dir=self.directory)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(data)
            load_preferences(candidate)
            if overwrite and path.exists():
                previous = _read_bytes(path, 64 * 1024)
                backup = self.directory / '.recovery'
                backup.mkdir(exist_ok=True)
                recovery = backup / (name + '-' + hashlib.sha256(previous).hexdigest() + '.toml')
                if not recovery.exists():
                    _publish(recovery, previous, overwrite=False)
            _publish(path, data, overwrite=overwrite)
        finally:
            Path(candidate).unlink(missing_ok=True)
        return self.load(name)

    def rename(self, old, new):
        old_path, new_path = self.path(old), self.path(new)
        self.load(old)
        if old_path == new_path:
            return self.load(old)
        if new_path.exists():
            raise PreferenceError('A layout with that name already exists.')
        # Link then unlink: refusing an existing destination never overwrites its data.
        os.link(old_path, new_path)
        sync_dir(self.directory)
        old_path.unlink()
        sync_dir(self.directory)
        return self.load(new)

    def delete(self, name):
        self.path(name).unlink()
        sync_dir(self.directory)
