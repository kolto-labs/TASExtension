"""Input trainer ("combo trial") logic: trials, frame matching, stats, history.

Pure Python, no Qt. It consumes committed per-frame inputs (the same
InputReceipt values the mechanic sequence guide receives); it never previews,
injects or polls the game.

Trial files are a compatible superset of the mechanic sequence format
(version 1, ``*.sequence.toml``). Old files load unchanged and keep their
inclusive ``window_min``/``window_max`` meaning. New optional step fields:

    target        frames after the previous step's landing frame
    window_early  frames allowed before target (default from settings)
    window_late   frames allowed after target (default from settings)
    hold_min      press steps: hold at least this many frames
    hold_max      press steps: release within this many frames

Frame numbering follows the workspace timeline: frame N is the input applied
at boundary N (the note covering ``(at, at+count]``).
"""
from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass, field, fields, replace as _copy
import hashlib
import json
import os
from pathlib import Path
import stat
import tomllib
from typing import Iterable, Mapping

from kptas.score.keys import BUTTONS, token_keys
from .mechanic_sequences import _fields, _integer, _names, _text


class TrainerError(ValueError):
    pass


MAX_STEPS = 256
NO_LIMIT = 1_000_000  # window_max = 1000000 means the step has no time limit
MAX_SPAN = 36_000  # ten minutes of frames for "make a trial from frames A..B"


# --------------------------------------------------------------------------
# Glyphs

_NAMES = {
    'lshift': 'Shift', 'rshift': 'R Shift', 'lctrl': 'Ctrl', 'rctrl': 'R Ctrl',
    'lalt': 'Alt', 'ralt': 'R Alt', 'esc': 'Esc', 'tab': 'Tab', 'space': 'Space',
    'enter': 'Enter', 'backspace': 'Backspace', 'numenter': 'Num Enter',
    'up': 'Up', 'down': 'Down', 'left': 'Left', 'right': 'Right',
    'pgup': 'Page Up', 'pgdn': 'Page Down', 'ins': 'Insert', 'del': 'Delete',
    'home': 'Home', 'end': 'End', 'capslock': 'Caps Lock', 'scrolllock': 'Scroll Lock',
    'numlock': 'Num Lock', 'pause': 'Pause', 'prtsc': 'Print Screen',
    'minus': '-', 'equals': '=', 'lbracket': '[', 'rbracket': ']', 'backslash': '\\',
    'semicolon': ';', 'apostrophe': "'", 'grave': '`', 'comma': ',', 'period': '.', 'slash': '/',
    'nummul': 'Num *', 'numadd': 'Num +', 'numsub': 'Num -', 'numdec': 'Num .', 'numdiv': 'Num /',
    'lmb': 'Left click', 'rmb': 'Right click', 'mmb': 'Middle click',
}
_COMPACT = {
    'lshift': '⇧', 'rshift': 'R⇧', 'lctrl': 'Ctl', 'rctrl': 'RCtl', 'lalt': 'Alt', 'ralt': 'RAlt',
    'space': '␣', 'enter': '⏎', 'backspace': '⌫', 'tab': '⇥', 'esc': 'Esc',
    'up': '↑', 'down': '↓', 'left': '←', 'right': '→',
    'lmb': 'M1', 'rmb': 'M2', 'mmb': 'M3',
}
_MODIFIER_ORDER = ('lctrl', 'rctrl', 'lshift', 'rshift', 'lalt', 'ralt')


def glyph(name: str, style: str = 'keycap') -> str:
    """Short display text for one canonical key or mouse button."""
    if style == 'compact' and name in _COMPACT:
        return _COMPACT[name]
    if style != 'text' and name in BUTTONS:
        return name.upper()
    if name in _NAMES:
        return _NAMES[name]
    if name.startswith('num') and name[3:].isdigit():
        return 'Num ' + name[3:]
    return name.upper() if len(name) <= 3 else name.title()


def ordered_inputs(inputs: Iterable[str]) -> list[str]:
    """Modifiers first, then keys, then mouse buttons."""
    def rank(name):
        if name in _MODIFIER_ORDER:
            return (0, _MODIFIER_ORDER.index(name), name)
        return (2 if name in BUTTONS else 1, 0, name)
    return sorted(inputs, key=rank)


def chord_label(inputs: Iterable[str], style: str = 'text') -> str:
    return (' + ' if style != 'compact' else '+').join(glyph(n, style) for n in ordered_inputs(inputs))


# --------------------------------------------------------------------------
# Settings

DEFAULT_HOTKEYS = (
    ('trainer.start_stop', 'Ctrl+Shift+F5'),
    ('trainer.reset', 'Ctrl+Shift+F6'),
    ('trainer.previous', 'Ctrl+Shift+F7'),
    ('trainer.next', 'Ctrl+Shift+F8'),
    ('trainer.toggle_panel', 'Ctrl+Shift+F9'),
)
HOTKEY_LABELS = {
    'trainer.start_stop': 'Start or stop the trial',
    'trainer.reset': 'Reset the trial',
    'trainer.previous': 'Previous trial',
    'trainer.next': 'Next trial',
    'trainer.toggle_panel': 'Show or hide the trainer',
}
DEFAULT_COLORS = (
    ('panel', '#12161c'), ('text', '#dfe6ee'), ('muted', '#8b95a5'),
    ('current', '#39a9db'), ('hit', '#4cc38a'), ('perfect', '#ffd166'),
    ('miss', '#ef5b5b'), ('window', '#2b3a4c'), ('target', '#ffd166'), ('marker', '#ffffff'),
)
_CHOICES = {
    'mode': ('practice', 'strict'),
    'reduced_motion': ('system', 'reduce', 'full'),
    'glyph_style': ('keycap', 'text', 'compact'),
    'panel_corner': ('top-right', 'top-left', 'bottom-right', 'bottom-left', 'custom'),
}
_RANGES = {
    'show_next': (0, 64), 'show_done': (0, 64), 'window_early': (0, 600), 'window_late': (0, 600),
    'hold_window': (0, 600), 'perfect_window': (0, 30), 'late_grace': (0, 600), 'frame_rate': (1, 1000),
    'history_entries': (1, 64), 'history_frames': (10, 1200), 'panel_x': (-100000, 100000),
    'panel_y': (-100000, 100000), 'panel_width': (240, 1200), 'reset_delay_ms': (0, 5000),
}
_FLOATS = {'animation_speed': (0.25, 4.0), 'sound_volume': (0.0, 1.0), 'panel_opacity': (0.2, 1.0)}


@dataclass(frozen=True)
class TrainerSettings:
    """Every trainer option with its default. from_dict/to_dict round-trip."""
    mode: str = 'practice'           # practice: loops; strict: one try
    auto_reset: bool = True          # practice: start over after a miss
    loop: bool = True                # practice: start again after finishing
    show_next: int = 4               # upcoming steps listed; 0 lists all
    show_done: int = 2               # finished steps kept in view
    window_early: int = 2            # default frames allowed before target
    window_late: int = 2             # default frames allowed after target
    perfect_window: int = 0          # |offset| at or under this counts as perfect
    hold_window: int = 2             # generated trials: hold length tolerance
    check_holds: bool = True         # generated trials: check hold lengths
    late_grace: int = 20             # frames to wait after a window closes to report lateness
    reset_delay_ms: int = 700        # how long a miss stays on screen before the reset shows
    frame_rate: int = 60
    history_entries: int = 12
    history_frames: int = 120
    reduced_motion: str = 'system'   # system | reduce | full
    animation_speed: float = 1.0
    sound: bool = False
    sound_volume: float = 0.5
    glyph_style: str = 'keycap'      # keycap | text | compact
    panel_corner: str = 'top-right'
    panel_x: int = 16                # offset from the corner (or position when custom)
    panel_y: int = 40
    panel_width: int = 360
    panel_opacity: float = 0.94
    panel_visible: bool = False
    colors: Mapping[str, str] = field(default_factory=lambda: dict(DEFAULT_COLORS))
    hotkeys: Mapping[str, str] = field(default_factory=lambda: dict(DEFAULT_HOTKEYS))

    def to_dict(self) -> dict:
        data = asdict(self)
        data['colors'] = dict(self.colors)
        data['hotkeys'] = dict(self.hotkeys)
        return data

    @classmethod
    def from_dict(cls, data: Mapping) -> 'TrainerSettings':
        """Validate a partial mapping; missing keys keep their defaults."""
        if not isinstance(data, Mapping):
            raise TrainerError('Trainer settings must be a table')
        known = {f.name for f in fields(cls)}
        unknown = set(data) - known
        if unknown:
            raise TrainerError(f'Unknown trainer setting: {sorted(unknown)[0]}')
        values = {}
        for name, value in data.items():
            if name in _CHOICES:
                if value not in _CHOICES[name]:
                    raise TrainerError(f'{name} must be one of: {", ".join(_CHOICES[name])}')
            elif name in _RANGES:
                low, high = _RANGES[name]
                if type(value) is not int or not low <= value <= high:
                    raise TrainerError(f'{name} must be a whole number from {low} to {high}')
            elif name in _FLOATS:
                low, high = _FLOATS[name]
                if type(value) not in (int, float) or not low <= value <= high:
                    raise TrainerError(f'{name} must be a number from {low} to {high}')
                value = float(value)
            elif name == 'colors':
                value = _colors(value)
            elif name == 'hotkeys':
                value = _hotkeys(value)
            elif type(value) is not bool:
                raise TrainerError(f'{name} must be true or false')
            values[name] = value
        return cls(**values)


def _colors(value):
    if not isinstance(value, Mapping):
        raise TrainerError('colors must be a table')
    merged = dict(DEFAULT_COLORS)
    for key, color in value.items():
        if key not in merged:
            raise TrainerError(f'Unknown color: {key}')
        if (not isinstance(color, str) or len(color) not in (4, 7, 9) or color[0] != '#'
                or any(c not in '0123456789abcdefABCDEF' for c in color[1:])):
            raise TrainerError(f'colors.{key} must look like #rrggbb')
        merged[key] = color
    return merged


def _hotkeys(value):
    if not isinstance(value, Mapping):
        raise TrainerError('hotkeys must be a table')
    merged = dict(DEFAULT_HOTKEYS)
    for action, text in value.items():
        if action not in merged:
            raise TrainerError(f'Unknown trainer action: {action}')
        if not isinstance(text, str) or len(text) > 40 or any(ord(c) < 32 for c in text):
            raise TrainerError(f'hotkeys.{action} must be a short key sequence, or empty to turn it off')
        merged[action] = text
    return merged


def settings_path() -> Path:
    base = os.environ.get('XDG_CONFIG_HOME')
    return (Path(base) if base else Path.home() / '.config') / 'kptas' / 'trainer.toml'


def _read_bounded(path, limit=65536) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, 'O_NOFOLLOW', 0))
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise TrainerError(f'{Path(path).name} must be a regular file no larger than {limit // 1024} KiB')
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            raw = stream.read(limit + 1)
        if len(raw) > limit:
            raise TrainerError(f'{Path(path).name} is larger than {limit // 1024} KiB')
        return raw
    finally:
        os.close(fd)


def load_settings(path=None) -> TrainerSettings:
    """Missing file gives defaults. A bad file raises; the caller keeps defaults."""
    path = Path(path) if path else settings_path()
    if not path.exists():
        return TrainerSettings()
    try:
        document = tomllib.loads(_read_bounded(path).decode('utf-8'))
    except (UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise TrainerError(f'Trainer settings: {exc}') from exc
    if set(document) - {'version', 'trainer'} or document.get('version') != 1:
        raise TrainerError('Trainer settings need version = 1 and a [trainer] table')
    return TrainerSettings.from_dict(document.get('trainer', {}))


def _toml_value(value):
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, (int, float)):
        return repr(value)
    return _toml_str(value)


def settings_text(settings: TrainerSettings) -> str:
    data = settings.to_dict()
    lines = ['version = 1', '', '[trainer]']
    for name, value in data.items():
        if not isinstance(value, dict):
            lines.append(f'{name} = {_toml_value(value)}')
    for table in ('colors', 'hotkeys'):
        lines += ['', f'[trainer.{table}]']
        lines += [f'{_toml_str(k)} = {_toml_value(v)}' for k, v in data[table].items()]
    return '\n'.join(lines) + '\n'


def save_settings(settings: TrainerSettings, path=None) -> Path:
    """Atomic write through the layout profile publisher."""
    from .ui_profiles import _publish
    path = Path(path) if path else settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    _publish(path, settings_text(settings).encode('utf-8'))
    return path


# --------------------------------------------------------------------------
# Trials

@dataclass(frozen=True)
class TrialStep:
    id: str
    label: str
    inputs: frozenset[str]
    match: str = 'press'             # press | release | hold
    exact: bool = False
    duration: int = 1                # hold match: frames to keep held
    target: int | None = None        # timed step: frames after the previous step
    early: int | None = None         # None: settings default, clamped to target
    late: int | None = None          # None: settings default
    minimum: int = 0                 # untimed step: inclusive window (old format)
    maximum: int | None = 120        # None: no limit
    hold_min: int | None = None
    hold_max: int | None = None

    @property
    def timed(self) -> bool:
        return self.target is not None

    def window(self, defaults: tuple[int, int] = (2, 2)) -> tuple[int, int | None]:
        """Inclusive frames after the previous step, (low, high or None).

        defaults = (window_early, window_late) from settings, used when the
        step does not set its own.
        """
        if self.timed:
            early = self.early if self.early is not None else min(defaults[0], self.target)
            late = self.late if self.late is not None else defaults[1]
            return self.target - early, self.target + late
        return self.minimum, self.maximum

    def window_frames(self, defaults: tuple[int, int] = (2, 2)) -> int | None:
        low, high = self.window(defaults)
        return None if high is None else high - low + 1


@dataclass(frozen=True)
class Trial:
    id: str
    title: str
    steps: tuple[TrialStep, ...]
    precision: str | None = 'frame'  # None: accept the session's clock
    reject_unrelated: bool = False
    motion: str = 'respect-system'
    source: str = ''
    sha256: str = ''
    sound: bool | None = None        # feedback.sound: False mutes this trial


_STEP_FIELDS = {'id', 'label', 'kind', 'match', 'keys', 'buttons', 'chord', 'window_min', 'window_max',
                'duration', 'predicate', 'equals', 'target', 'window_early', 'window_late', 'hold_min', 'hold_max'}


def _optional_int(row, name, context, default=None, minimum=0):
    if name not in row:
        return default
    value = _integer(row[name], f'{context}.{name}')
    if value < minimum:
        raise TrainerError(f'{context}.{name}: must be at least {minimum}')
    return value


def load_trial(path, **_ignored) -> Trial:
    """Load a trial or an old mechanic sequence file. Raises TrainerError.

    Steps without window_early/window_late follow the trainer settings when run.
    """
    raw = _read_bounded(path)
    try:
        data = tomllib.loads(raw.decode('utf-8'))
        _fields(data, {'version', 'id', 'title', 'precision', 'start', 'unrelated_inputs', 'feedback', 'steps'}, 'trial')
        if type(data.get('version')) is not int or data['version'] != 1:
            raise TrainerError('Unsupported trial version. Use version = 1.')
        precision = data.get('precision', 'frame')
        if precision not in {'frame', 'tick'} or data.get('start', 'explicit-boundary') != 'explicit-boundary':
            raise TrainerError('precision must be "frame" and start must be "explicit-boundary"')
        unrelated = data.get('unrelated_inputs', 'ignore')
        if unrelated not in {'ignore', 'reject'}:
            raise TrainerError('unrelated_inputs must be "ignore" or "reject"')
        feedback = data.get('feedback', {})
        _fields(feedback, {'motion', 'sound'}, 'feedback')
        motion = feedback.get('motion', 'respect-system')
        if motion not in {'respect-system', 'none'} or type(feedback.get('sound', False)) is not bool:
            raise TrainerError('feedback.motion must be "respect-system" or "none"; feedback.sound must be true or false')
        rows = data.get('steps')
        if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_STEPS:
            raise TrainerError(f'A trial needs 1 to {MAX_STEPS} steps')
        steps, ids = [], set()
        for i, row in enumerate(rows):
            context = f'steps[{i}]'
            _fields(row, _STEP_FIELDS, context)
            kind = row.get('kind', 'input')
            if kind == 'outcome':
                raise TrainerError(f'{context}: outcome steps need game data. The trainer checks inputs only.')
            if kind != 'input':
                raise TrainerError(f'{context}: unsupported step kind')
            ident = _text(row.get('id', f'step-{i + 1}'), context + '.id', 80)
            if ident in ids:
                raise TrainerError(f'{context}: duplicate step id')
            ids.add(ident)
            match = row.get('match', 'press')
            chord = row.get('chord', 'exact')
            if match not in {'press', 'release', 'hold'} or chord not in {'exact', 'contains'}:
                raise TrainerError(f'{context}: match must be press, release or hold; chord must be exact or contains')
            if match != 'hold' and 'duration' in row:
                raise TrainerError(f'{context}: duration is only valid for hold')
            duration = _integer(row.get('duration', 1), context + '.duration')
            if duration < 1:
                raise TrainerError(f'{context}: duration must be at least 1')
            inputs = _names(row.get('keys', []), context + '.keys') | _names(row.get('buttons', []), context + '.buttons', True)
            if not inputs:
                raise TrainerError(f'{context}: specify a key or button')
            label = _text(row['label'], context + '.label') if 'label' in row else chord_label(inputs)
            target = _optional_int(row, 'target', context)
            if target is None and ({'window_early', 'window_late'} & set(row)):
                raise TrainerError(f'{context}: window_early and window_late need a target')
            if target is not None and ({'window_min', 'window_max'} & set(row)):
                raise TrainerError(f'{context}: use target with window_early/window_late, or window_min/window_max, not both')
            early = _optional_int(row, 'window_early', context)
            late = _optional_int(row, 'window_late', context)
            if early is not None and early > target:
                raise TrainerError(f'{context}: window_early is larger than target')
            minimum = _integer(row.get('window_min', 0), context + '.window_min')
            maximum = _integer(row.get('window_max', 120), context + '.window_max')
            if maximum < minimum:
                raise TrainerError(f'{context}: timing window is reversed')
            hold_min = _optional_int(row, 'hold_min', context, minimum=1)
            hold_max = _optional_int(row, 'hold_max', context, minimum=1)
            if (hold_min is not None or hold_max is not None) and match != 'press':
                raise TrainerError(f'{context}: hold_min and hold_max are only valid for press steps')
            if hold_min is not None and hold_max is not None and hold_max < hold_min:
                raise TrainerError(f'{context}: hold_max is smaller than hold_min')
            steps.append(TrialStep(ident, label, inputs, match, chord == 'exact', duration, target, early, late,
                                   minimum, None if maximum >= NO_LIMIT else maximum, hold_min, hold_max))
        return Trial(_text(data.get('id'), 'id', 80), _text(data.get('title'), 'title'), tuple(steps), precision,
                     unrelated == 'reject', motion, str(path), hashlib.sha256(raw).hexdigest(),
                     feedback.get('sound'))
    except TrainerError:
        raise
    except (ValueError, TypeError, KeyError) as exc:
        # SequenceError, unknown key names, TOML and UTF-8 errors.
        raise TrainerError(str(exc)) from exc


def _toml_str(text) -> str:
    # ensure_ascii=False: JSON surrogate escapes for non-BMP text are invalid TOML.
    return json.dumps(str(text), ensure_ascii=False).replace('\x7f', '\\u007f')


def trial_text(trial: Trial) -> str:
    """TOML that load_trial reads back to the same steps."""
    out = ['version = 1', f'id = {_toml_str(trial.id)}', f'title = {_toml_str(trial.title)}',
           'precision = "frame"', 'start = "explicit-boundary"',
           f'unrelated_inputs = "{"reject" if trial.reject_unrelated else "ignore"}"']
    if trial.motion != 'respect-system' or trial.sound is not None:
        out += ['', '[feedback]', f'motion = "{trial.motion}"']
        if trial.sound is not None:
            out.append(f'sound = {"true" if trial.sound else "false"}')
    for step in trial.steps:
        keys = [n for n in ordered_inputs(step.inputs) if n not in BUTTONS]
        buttons = [n for n in ordered_inputs(step.inputs) if n in BUTTONS]
        out += ['', '[[steps]]', f'id = {_toml_str(step.id)}', f'label = {_toml_str(step.label)}', 'kind = "input"']
        if step.match != 'press':
            out.append(f'match = "{step.match}"')
        if keys:
            out.append(f'keys = {json.dumps(keys)}')
        if buttons:
            out.append(f'buttons = {json.dumps(buttons)}')
        out.append(f'chord = "{"exact" if step.exact else "contains"}"')
        if step.match == 'hold':
            out.append(f'duration = {step.duration}')
        if step.timed:
            out.append(f'target = {step.target}')
            if step.early is not None:
                out.append(f'window_early = {step.early}')
            if step.late is not None:
                out.append(f'window_late = {step.late}')
        else:
            out += [f'window_min = {step.minimum}', f'window_max = {step.maximum if step.maximum is not None else NO_LIMIT}']
        if step.hold_min is not None:
            out.append(f'hold_min = {step.hold_min}')
        if step.hold_max is not None:
            out.append(f'hold_max = {step.hold_max}')
    return '\n'.join(out) + '\n'


def note_inputs(note) -> frozenset[str]:
    """Held keys and buttons of a score Note, as committed receipts report them."""
    return frozenset(k for token in note.held for k in token_keys(token)) | frozenset(note.buttons)


def score_frames(text: str, first: int, last: int, *, include_dirs=()) -> list[tuple[int, frozenset[str]]]:
    """Per-frame held inputs of a score for frames first..last."""
    from kptas.score import Note, Relaunch, ScoreError, Sync, compile_score
    if not text or not text.strip():
        raise TrainerError('No score is loaded')
    if '\n' not in text:
        text += '\n'      # a one-line string would be read as a file path
    try:
        compiled = compile_score(text, include_dirs=[Path(p) for p in include_dirs])
    except ScoreError as exc:
        raise TrainerError(f'The score does not compile: {exc}') from exc
    out, at = [], 0
    for item in compiled.items:
        if at >= last:
            break
        if isinstance(item, (Sync, Relaunch)):
            raise TrainerError(f'Frames after {at} follow a sync point, so they have no fixed number in the score. '
                               f'Select them on the timeline instead, or pick frames up to {at}.')
        if isinstance(item, Note) and item.count > 0:
            held = note_inputs(item)
            out += [(t, held) for t in range(max(first, at + 1), min(last, at + item.count) + 1)]
            at += item.count
    if not out or out[-1][0] < last:
        raise TrainerError(f'The score ends at frame {at}.')
    return out


def trial_from_frames(frames: Iterable[tuple[int, frozenset[str] | None]], *, title: str,
                      settings: TrainerSettings | None = None, ident: str | None = None) -> Trial:
    """One step per frame with new presses; the chord is every key pressed that frame.

    A None held set marks a barrier or unavailable frame: the next step gets
    free timing. The trial assumes every key starts released.
    """
    settings = settings or TrainerSettings()
    frames = list(frames)
    if not frames:
        raise TrainerError('Select at least one frame')
    if len(frames) > MAX_SPAN:
        raise TrainerError(f'Pick at most {MAX_SPAN} frames')
    if any(b[0] != a[0] + 1 for a, b in zip(frames, frames[1:])):
        raise TrainerError('Frames must be consecutive')
    presses, prev, barrier = [], frozenset(), True
    for index, (frame, held) in enumerate(frames):
        if held is None:
            prev, barrier = frozenset(), True
            continue
        pressed = held - prev
        if pressed:
            presses.append((index, frame, frozenset(pressed), barrier))
            barrier = False
        prev = held
    first, last = frames[0][0], frames[-1][0]
    if not presses:
        raise TrainerError(f'No key presses between frames {first} and {last}')
    if len(presses) > MAX_STEPS:
        raise TrainerError(f'Frames {first}–{last} have {len(presses)} presses. The limit is {MAX_STEPS}; pick a shorter span.')
    steps, previous = [], None
    for number, (index, frame, chord, after_barrier) in enumerate(presses, 1):
        length, open_end = 0, True
        for _, held in frames[index:]:
            if held is None or not chord <= held:
                open_end = False
                break
            length += 1
        hold_min = hold_max = None
        if settings.check_holds:
            hold_min = max(1, length - settings.hold_window)
            hold_max = None if open_end else length + settings.hold_window
        if previous is None or after_barrier:
            step = TrialStep(f'f{frame}', chord_label(chord), chord, minimum=0, maximum=None,
                             hold_min=hold_min, hold_max=hold_max)
        else:
            target = frame - previous
            step = TrialStep(f'f{frame}', chord_label(chord), chord, target=target,
                             early=min(settings.window_early, target - 1), late=settings.window_late,
                             hold_min=hold_min, hold_max=hold_max)
        steps.append(step)
        previous = frame
    digest = hashlib.sha256(repr([(s.id, sorted(s.inputs), s.target, s.hold_min, s.hold_max) for s in steps]).encode()).hexdigest()
    return Trial(ident or f'frames-{first}-{last}', title, tuple(steps), None, False, 'respect-system',
                 f'frames {first}–{last}', digest)


# --------------------------------------------------------------------------
# Matching

@dataclass
class StepResult:
    state: str = 'pending'           # pending | current | holding | hit | miss
    frame: int | None = None         # landing frame
    elapsed: int | None = None       # frames after the previous step
    offset: int | None = None        # elapsed - target (timed steps)
    verdict: str = ''                # perfect | early | late | in window | free
    hold: int | None = None          # frames held (hold checks)
    reason: str = ''


@dataclass(frozen=True)
class Event:
    kind: str                        # reset | landed | hit | miss | closed | complete | status
    index: int | None = None
    text: str = ''
    result: StepResult | None = None # copy taken when the event happened


@dataclass
class Stats:
    hits: int = 0
    misses: int = 0
    perfect: int = 0
    streak: int = 0
    best_streak: int = 0
    completions: int = 0
    attempts: int = 0
    offsets: list = field(default_factory=list)

    def add_hit(self, offset, perfect):
        self.hits += 1
        self.streak += 1
        self.best_streak = max(self.best_streak, self.streak)
        if perfect:
            self.perfect += 1
        if offset is not None:
            self.offsets.append(offset)
            del self.offsets[:-10_000]

    def add_miss(self):
        self.misses += 1
        self.streak = 0

    @property
    def average_offset(self) -> float | None:
        return sum(self.offsets) / len(self.offsets) if self.offsets else None

    @property
    def average_distance(self) -> float | None:
        return sum(abs(o) for o in self.offsets) / len(self.offsets) if self.offsets else None


def _frames(n: int) -> str:
    return f'{n} frame' + ('' if n == 1 else 's')


class TrialRun:
    """Feeds committed per-frame held sets through one trial.

    States: idle, running, failed, complete, suspended. Untimed steps keep the
    mechanic sequence tracker's exact meaning; timed steps judge the landing
    frame against target ± window and report early/late offsets.
    """
    ACTIVE = ('running',)

    def __init__(self, trial: Trial, settings: TrainerSettings | None = None):
        self.trial = trial
        self.settings = settings or TrainerSettings()
        self.stats = Stats()
        self.state = 'idle'
        self.status = 'Press Start, then play the steps'
        self.lineage = None
        self.last = None
        self.held = frozenset()
        self._seen = set()
        self._fresh = False
        self._reset_progress(None)

    @property
    def defaults(self) -> tuple[int, int]:
        return self.settings.window_early, self.settings.window_late

    def window(self, step: TrialStep) -> tuple[int, int | None]:
        return step.window(self.defaults)

    # -- control --------------------------------------------------------
    def _reset_progress(self, anchor):
        self.results = [StepResult() for _ in self.trial.steps]
        self.index = 0
        self.anchor = anchor
        self._hold_count = 0         # untimed hold steps (old semantics)
        self._run_start = None       # timed hold steps: first frame of the current run
        self._holds = {}             # step index -> landing frame, awaiting hold verdict
        self._closed = None          # frame the current timed window closed
        self.generation = getattr(self, 'generation', 0) + 1
        if self.results:
            self.results[0].state = 'current'

    def start(self, boundary: int, held=frozenset(), lineage=None, *, text='Go') -> list[Event]:
        self.lineage, self.last, self.held = lineage, boundary, frozenset(held)
        self._reset_progress(boundary)
        self.state = 'running'
        self._fresh = True
        self.stats.attempts += 1
        self.status = text
        return [Event('reset', None, text)]

    def stop(self, text='Stopped'):
        self.state = 'idle'
        self.status = text

    def reset(self) -> list[Event]:
        if self.last is None:
            self._reset_progress(None)
            self.state = 'idle'
            self.status = 'Waiting for committed input. Step one frame to begin.'
            return [Event('reset', None, self.status)]
        return self.start(self.last, self.held, self.lineage)

    def suspend(self, text):
        if self.state == 'running':
            self.state = 'suspended'
        self.status = text

    def check_precision(self, precision) -> str | None:
        if self.trial.precision is not None and precision != self.trial.precision:
            return 'This trial file uses a different clock than the session. Make the trial again from frames.'
        return None

    def _interrupt(self, boundary, held, lineage, text) -> list[Event]:
        if self.settings.mode == 'practice':
            # Judge this frame too: restart one frame earlier with every key up.
            events = self.start(boundary - 1, frozenset(), lineage, text=text + ' Trial restarted.')
            self._fresh = False
            self.last, self.held = boundary, frozenset(held)
            return events + self._advance(boundary, frozenset(), frozenset(held))
        self.last, self.held, self.lineage = boundary, frozenset(held), lineage
        self.state = 'suspended'
        self.status = text + ' Press Reset to try again.'
        return [Event('status', None, self.status)]

    # -- feeding --------------------------------------------------------
    def feed(self, boundary: int, held, lineage=None, identity=None) -> list[Event]:
        """One committed frame. Returns presentation events, oldest first."""
        held = frozenset(held)
        if identity is not None:
            if identity in self._seen:
                return []
            if len(self._seen) > 100_000:
                self._seen.clear()
            self._seen.add(identity)
        if self.state not in self.ACTIVE:
            self.last, self.held = boundary, held
            if lineage is not None:
                self.lineage = lineage
            return []
        fresh, self._fresh = self._fresh, False
        if fresh and (boundary != self.last + 1 or (lineage is not None and lineage != self.lineage)):
            # Start used a stale frame (a seek or rewind since). Re-anchor quietly.
            text = self.status
            events = self.start(boundary - 1, frozenset(), lineage or self.lineage, text=text)
            self._fresh = False
            self.last, self.held = boundary, held
            return events + self._advance(boundary, frozenset(), held)
        if lineage is not None and self.lineage is not None and lineage != self.lineage:
            return self._interrupt(boundary, held, lineage, 'The recording changed.')
        if boundary <= self.last:
            return self._interrupt(boundary, held, lineage or self.lineage, f'Rewound to frame {boundary}.')
        if boundary != self.last + 1:
            return self._interrupt(boundary, held, lineage or self.lineage,
                                   'Frames were skipped. The trainer needs one frame at a time.')
        prior, self.last, self.held = self.held, boundary, held
        return self._advance(boundary, prior, held)

    def _advance(self, f, prior, held) -> list[Event]:
        events = []
        pressed, released = held - prior, prior - held
        generation = self.generation
        for index in sorted(self._holds):
            if index in self._holds:
                events += self._check_hold(index, f, held)
            if self.state != 'running' or self.generation != generation:
                return events
        chained = False
        while self.state == 'running' and self.index < len(self.trial.steps):
            landed, step_events = self._match(f, prior, held, pressed, released, chained)
            events += step_events
            if not landed or self.generation != generation:
                break
            chained = True
        if self.state == 'running' and self.index >= len(self.trial.steps) and not self._holds:
            events += self._complete(f)
        return events

    def _match(self, f, prior, held, pressed, released, chained):
        index = self.index
        step = self.trial.steps[index]
        elapsed = f - self.anchor
        if chained and (not step.timed or self.window(step)[0] > 0):
            return False, []      # one step per frame, unless a timed step can land at elapsed 0
        if step.timed:
            return self._match_timed(index, step, f, elapsed, held, pressed, released)
        # Untimed step: same predicates and window as SequenceTracker.consume.
        if step.maximum is not None and elapsed > step.maximum:
            return False, self._miss(index, f, f'Window closed. Allowed {step.minimum}–{step.maximum} frames after the previous step.')
        values = pressed if step.match == 'press' else released if step.match == 'release' else held
        matched = (step.inputs <= held and bool(pressed & step.inputs)) if step.match == 'press' else step.inputs <= values
        if step.exact:
            matched = matched and (values == step.inputs if step.match == 'release' else held == step.inputs)
        if step.match == 'hold':
            self._hold_count = self._hold_count + 1 if matched else 0
            matched = self._hold_count >= step.duration
        unexpected = pressed - step.inputs
        if self.trial.reject_unrelated and unexpected:
            return False, self._miss(index, f, f'Unexpected input: {chord_label(unexpected)}.')
        if matched and step.minimum <= elapsed:
            self._hold_count = 0
            verdict = 'free' if step.maximum is None and step.minimum == 0 else 'in window'
            return True, self._land(index, f, elapsed, None, verdict, held)
        if matched:
            self.status = f'Too early. Wait {_frames(step.minimum - elapsed)} more.'
            return False, [Event('status', index, self.status)]
        return False, []

    def _match_timed(self, index, step, f, elapsed, held, pressed, released):
        low, high = self.window(step)
        if step.match == 'hold':
            ok = held == step.inputs if step.exact else step.inputs <= held
            if ok:
                if self._run_start is None:
                    self._run_start = f
            else:
                self._run_start = None
            landed = ok and f - self._run_start + 1 >= step.duration
            land_frame = self._run_start if landed else None
        else:
            if step.match == 'press':
                landed = step.inputs <= held and bool(pressed & step.inputs)
                if step.exact:
                    landed = landed and held == step.inputs
            else:
                landed = step.inputs <= released and (not step.exact or released == step.inputs)
            land_frame = f if landed else None
        unexpected = pressed - step.inputs
        if self.trial.reject_unrelated and unexpected:
            return False, self._miss(index, f, f'Unexpected input: {chord_label(unexpected)}.')
        if landed:
            e = land_frame - self.anchor
            offset = e - step.target
            if e < low:
                return False, self._miss(index, f, f'Too early by {_frames(low - e)}.', offset=offset, elapsed=e)
            if e > high:
                return False, self._miss(index, f, f'Too late by {_frames(e - high)}.', offset=offset, elapsed=e)
            perfect = abs(offset) <= self.settings.perfect_window
            verdict = 'perfect' if perfect else 'early' if offset < 0 else 'late'
            self._run_start = None
            return True, self._land(index, land_frame, e, offset, verdict, held, at=f)
        started = step.match == 'hold' and self._run_start is not None and self._run_start - self.anchor <= high
        if elapsed > high and not started:
            events = []
            if self._closed is None:
                self._closed = f
                self.status = 'Window closed'
                events.append(Event('closed', index, self.status, _copy(self.results[index])))
            if f - self._closed >= self.settings.late_grace:
                events += self._miss(index, f, f'No input in the window ({low}–{high} frames after the previous step).')
            return False, events
        return False, []

    def _land(self, index, frame, elapsed, offset, verdict, held, at=None) -> list[Event]:
        step = self.trial.steps[index]
        result = self.results[index]
        result.frame, result.elapsed, result.offset, result.verdict = frame, elapsed, offset, verdict
        self.anchor = frame
        self.index += 1
        self._closed = None
        self._run_start = None
        if self.index < len(self.trial.steps):
            self.results[self.index].state = 'current'
        events = []
        if step.hold_min is not None or step.hold_max is not None:
            result.state = 'holding'
            self._holds[index] = frame
            events.append(Event('landed', index, 'Hold', _copy(result)))
            events += self._check_hold(index, at if at is not None else frame, held)
        else:
            events += self._pass(index)
        return events

    def _check_hold(self, index, f, held) -> list[Event]:
        step = self.trial.steps[index]
        start = self._holds[index]
        result = self.results[index]
        if step.inputs <= held:
            length = f - start + 1
            result.hold = length
            if step.hold_max is not None and length > step.hold_max:
                del self._holds[index]
                return self._miss(index, f, f'Held too long. Release within {_frames(step.hold_max)}.', keep_landing=True)
            if step.hold_max is None and length >= (step.hold_min or 1):
                del self._holds[index]
                return self._pass(index)
            return []
        length = f - start
        result.hold = length
        del self._holds[index]
        if step.hold_min is not None and length < step.hold_min:
            return self._miss(index, f, f'Released too soon. Held {length} of {_frames(step.hold_min)}.', keep_landing=True)
        return self._pass(index)

    def _pass(self, index) -> list[Event]:
        result = self.results[index]
        result.state = 'hit'
        self.stats.add_hit(result.offset, result.verdict == 'perfect')
        self.status = {'perfect': 'Perfect', 'early': f'Early by {_frames(-(result.offset or 0))}',
                       'late': f'Late by {_frames(result.offset or 0)}'}.get(result.verdict, 'Hit')
        return [Event('hit', index, self.status, _copy(result))]

    def _miss(self, index, f, reason, *, offset=None, elapsed=None, keep_landing=False) -> list[Event]:
        result = self.results[index]
        result.state = 'miss'
        result.reason = reason
        if not keep_landing:
            result.offset, result.elapsed = offset, elapsed
            result.verdict = 'early' if offset is not None and offset < 0 else 'late' if offset is not None else ''
        self.stats.add_miss()
        events = [Event('miss', index, reason, _copy(result))]
        if self.settings.mode == 'practice' and self.settings.auto_reset:
            self._reset_progress(f)
            self.stats.attempts += 1
            self.status = reason + ' Starting over.'
            events.append(Event('reset', None, self.status))
        else:
            self.state = 'failed'
            self.status = reason + ' Press Reset to try again.'
        return events

    def _complete(self, f) -> list[Event]:
        self.stats.completions += 1
        events = [Event('complete', None, 'Trial complete')]
        if self.settings.mode == 'practice' and self.settings.loop:
            self._reset_progress(f)
            self.stats.attempts += 1
            self.status = 'Trial complete. Starting again.'
            events.append(Event('reset', None, self.status))
        else:
            self.state = 'complete'
            self.status = 'Trial complete'
        return events


@dataclass(frozen=True)
class LiveFrame:
    """One committed frame from the owner's input log, a recording or a receipt.

    Lighter than InputReceipt: no journal digest. Judged in committed game frames.
    """
    session: str
    take: str
    boundary: int
    held: frozenset[str]
    precision: str | None = None
    identity: object = None


def frame_from_tokens(session, take, tick, held_tokens, buttons=(), *, identity=None, precision=None) -> LiveFrame:
    held = frozenset(k for token in held_tokens for k in token_keys(str(token))) | frozenset(str(b) for b in buttons)
    return LiveFrame(str(session), str(take), int(tick), held, precision, identity)


# --------------------------------------------------------------------------
# Input history

@dataclass
class HistoryEntry:
    held: frozenset[str]
    start: int
    frames: int
    gap: bool = False


class InputHistory:
    """Run-length list of committed held sets plus the last span frames."""

    def __init__(self, entries: int = 12, span: int = 120):
        self.entries: deque[HistoryEntry] = deque(maxlen=entries)
        self.recent: deque[tuple[int, frozenset[str]]] = deque(maxlen=span)
        self.last = None

    def resize(self, entries: int, span: int):
        self.entries = deque(self.entries, maxlen=entries)
        self.recent = deque(self.recent, maxlen=span)

    def clear(self):
        self.entries.clear()
        self.recent.clear()
        self.last = None

    def feed(self, boundary: int, held) -> None:
        held = frozenset(held)
        if self.last is not None and boundary <= self.last:
            self.clear()          # rewound: the shown history no longer happened
        gap = self.last is not None and boundary != self.last + 1
        if self.entries and not gap and self.entries[-1].held == held:
            self.entries[-1].frames += 1
        else:
            self.entries.append(HistoryEntry(held, boundary, 1, gap))
        if gap:
            self.recent.clear()
        self.recent.append((boundary, held))
        self.last = boundary

    def lanes(self) -> list[tuple[str, list[tuple[int, int]]]]:
        """Per input, (first, last) frame runs held within the recent span."""
        runs: dict[str, list[list[int]]] = {}
        for frame, held in self.recent:
            for name in held:
                spans = runs.setdefault(name, [])
                if spans and spans[-1][1] == frame - 1:
                    spans[-1][1] = frame
                else:
                    spans.append([frame, frame])
        return [(name, [tuple(s) for s in runs[name]]) for name in ordered_inputs(runs)]


def replace_settings(settings: TrainerSettings, **changes) -> TrainerSettings:
    """Validated copy with changes applied."""
    data = settings.to_dict()
    data.update(changes)
    return TrainerSettings.from_dict(data)


__all__ = ['TrainerError', 'TrainerSettings', 'Trial', 'TrialStep', 'TrialRun', 'StepResult', 'Event', 'Stats',
           'InputHistory', 'HistoryEntry', 'DEFAULT_HOTKEYS', 'HOTKEY_LABELS', 'DEFAULT_COLORS', 'load_trial',
           'trial_text', 'trial_from_frames', 'score_frames', 'note_inputs', 'load_settings', 'save_settings',
           'settings_path', 'settings_text', 'glyph', 'chord_label', 'ordered_inputs', 'replace_settings',
           'LiveFrame', 'frame_from_tokens']
