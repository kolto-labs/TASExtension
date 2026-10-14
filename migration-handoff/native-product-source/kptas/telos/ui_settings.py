"""Settings that are not part of a layout: reduced motion and playback pacing.

File: $XDG_CONFIG_HOME/kptas/settings.toml (normally ~/.config/kptas/settings.toml).

    version = 1

    [display]
    reduced_motion = false

    [playback]
    max_batch_frames = 240       # 1-240 frames per Play command
    max_batch_seconds = 1.0      # 0.1-10 seconds per Play command
    preview_refresh_ms = 16      # 1-1000 ms between preview reads

Layout files reject unknown keys, so these values live here. A playback value
that is absent keeps the built-in behavior, including KPTAS_PLAY_BATCH_MAX_S.
Loading never raises; a broken file gives defaults and one warning.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import tomllib

LIMIT = 16 * 1024
RANGES = {'max_batch_frames': (1, 240), 'max_batch_seconds': (0.1, 10.0), 'preview_refresh_ms': (1, 1000)}


@dataclass(frozen=True)
class UISettings:
    reduced_motion: bool = False
    max_batch_frames: int | None = None
    max_batch_seconds: float | None = None
    preview_refresh_ms: int | None = None
    first_run_hint_dismissed: bool = False


def settings_path():
    from .ui_profiles import profile_directory
    return profile_directory().parent / 'settings.toml'


def load_settings(path=None):
    """-> (UISettings, warning text). Never raises."""
    from .ui_preferences import read_bytes
    path = path or settings_path()
    try:
        document = tomllib.loads(read_bytes(path, LIMIT).decode('utf-8'))
    except FileNotFoundError:
        return UISettings(), ''
    except (OSError, ValueError, UnicodeError) as exc:
        return UISettings(), f'Default settings are in use because {path} could not be read: {exc}'
    problems = []
    version = document.get('version', 1)
    if type(version) is not int or version != 1:
        return UISettings(), f'Default settings are in use because {path} must have version = 1.'
    display = document.get('display', {})
    playback = document.get('playback', {})
    if not isinstance(display, dict) or not isinstance(playback, dict):
        return UISettings(), f'Default settings are in use because [display] and [playback] in {path} must be tables.'
    values = {}
    reduced = display.get('reduced_motion', False)
    if type(reduced) is bool:
        values['reduced_motion'] = reduced
    else:
        problems.append('reduced_motion must be true or false.')
    for name, (low, high) in RANGES.items():
        if name not in playback:
            continue
        value = playback[name]
        kind = float if isinstance(low, float) else int
        if type(value) is bool or not isinstance(value, (int, float)) or (kind is int and type(value) is not int):
            problems.append(f'{name} must be a number.')
        elif not low <= value <= high:
            problems.append(f'{name} must be from {low} to {high}.')
        else:
            values[name] = kind(value)
    hints = document.get('hints', {})
    if isinstance(hints, dict) and type(hints.get('first_run_dismissed', False)) is bool:
        values['first_run_hint_dismissed'] = hints.get('first_run_dismissed', False)
    warning = f'Some settings in {path} were skipped. ' + ' '.join(problems) if problems else ''
    return UISettings(**values), warning


def settings_text(settings):
    lines = ['# Stasis settings that are not part of a layout.', 'version = 1', '',
             '[display]', f'reduced_motion = {json.dumps(settings.reduced_motion)}', '', '[playback]']
    for name in RANGES:
        value = getattr(settings, name)
        if value is not None:
            lines.append(f'{name} = {json.dumps(value)}')
    if settings.first_run_hint_dismissed:
        lines += ['', '[hints]', 'first_run_dismissed = true']
    return ('\n'.join(lines) + '\n').encode('utf-8')


def save_settings(settings, path=None):
    from .ui_profiles import publish_file
    path = path or settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    publish_file(path, settings_text(settings))


_CURRENT = None


def current_settings():
    """Cached settings for features that read them, such as reduced motion."""
    global _CURRENT
    if _CURRENT is None:
        _CURRENT = load_settings()[0]
    return _CURRENT


def set_current(settings):
    global _CURRENT
    _CURRENT = settings


def dismiss_first_run_hint():
    """Remember that the first-run key tip was closed. Returns an error text or ''."""
    from dataclasses import replace
    settings, _warning = load_settings()
    try:
        save_settings(replace(settings, first_run_hint_dismissed=True))
    except (OSError, ValueError) as exc:
        return str(exc)
    if _CURRENT is not None:
        set_current(replace(_CURRENT, first_run_hint_dismissed=True))
    return ''


def reduced_motion() -> bool:
    """The one reduce-motion switch. Features that animate read this."""
    return current_settings().reduced_motion


def apply_playback(workspace, settings):
    """Set pacing on a live workspace; absent values keep current behavior."""
    sizer = getattr(workspace, 'play_sizer', None)
    if sizer is not None:
        sizer.configure(settings.max_batch_frames, settings.max_batch_seconds)
    if settings.preview_refresh_ms is not None and hasattr(workspace, 'preview_refresh_ms'):
        workspace.preview_refresh_ms = settings.preview_refresh_ms
        timer = getattr(workspace, 'timer', None)
        if timer is not None and timer.isActive():
            timer.setInterval(max(100, settings.preview_refresh_ms))
