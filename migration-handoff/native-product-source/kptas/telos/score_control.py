"""Live score control: pause, resume, unload and load a score at the current frame.

The owner's run queue executes and records every scripted frame. This module
holds the client-side pieces: the remappable action table, conversion of a
.score file to the plain run text the owner admits, and remainder drafts.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import uuid

# Action id, default key, label, description. Registered in the central action
# registry (actions.register_action, category 'Live score'), which binds the
# keys, applies keymap overrides and resolves conflicts.
SCORE_ACTIONS = (
    ('score_pause_resume', 'Ctrl+Alt+P', 'Pause or resume the score',
     'Stop the loaded score at the current frame, or continue it from there.'),
    ('score_unload', 'Ctrl+Alt+U', 'Unload the score',
     'Stop the score here. Its other inputs are saved as a draft.'),
    ('score_load_here', 'Ctrl+Alt+O', 'Load a score here',
     'Start a score file at the current frame. A loaded score is replaced.'),
    ('score_load_at', '', 'Load a score at an earlier frame',
     'Go back to a frame you choose, then start a score file there.'),
)
SCORE_ACTION_IDS = tuple(action for action, *_rest in SCORE_ACTIONS)
# Workspace operations that act on a loaded score. They bypass the
# "a run is active" gate because they are how the user controls that run.
RUN_CONTROL_OPS = ('run_pause', 'run_resume', 'run_unload', 'score_load')


BUNDLED_SCORE_DIR = Path(__file__).resolve().parents[1] / 'data'  # kptas/data, e.g. k1-keymap.score


def score_include_dirs(configured=()):
    """Include folders searched after the score's own folder, as `score check` does:
    the configured score.include_dirs, then the bundled kptas/data folder."""
    dirs = [Path(os.path.expanduser(str(d))) for d in configured or ()]
    if BUNDLED_SCORE_DIR not in dirs:
        dirs.append(BUNDLED_SCORE_DIR)
    return dirs


def configured_include_dirs():
    """score.include_dirs from telos.toml in the working folder, resolved against
    the config's folder exactly as the telos CLI does (cli.Ctx.include_dirs).
    A missing or unreadable config falls back to the bundled folder only."""
    try:
        from .cli import Ctx
        ctx = Ctx()
        return ctx.include_dirs(ctx.cfg())
    except Exception:
        return []


def compile_score_file(path, configured=()):
    """compile_score with `score check` include resolution and a plain missing-include error."""
    from kptas.score import ScoreError, compile_score
    path = Path(path)
    try:
        return compile_score(path, include_dirs=score_include_dirs(configured))
    except ScoreError as exc:
        found = re.fullmatch(r"cannot find include '(.+?)' \(looked for (.*)\)", exc.message)
        if not found:
            raise
        folders = list(dict.fromkeys(str(Path(c).parent) for c in found[2].split(', ')))
        raise ScoreError(f"@include {found[1]} was not found. Folders searched: " + '; '.join(folders),
                         exc.source, exc.line, exc.col) from None


@dataclass(frozen=True)
class LoadedScore:
    text: str        # plain run score: '#!score v1' header and Notes only
    name: str        # file name shown in the UI and the owner's history
    frames: int
    source: str


def load_run_score(path, configured=()):
    """Compile a .score file into the plain run text the owner admits.

    Includes resolve as in `score check`: beside the file, then the configured
    include folders, then the bundled kptas/data. Marks and option-free segments are
    dropped; anything that changes timing or sends OS events is refused.
    Raises ValueError with a plain message; nothing is sent on failure.
    """
    from kptas.score import ScoreError
    from kptas.score.ast import Mark, Note, Segment
    from kptas.score.fmt import render
    from kptas.supervisor import TelosError
    from .run_queue import compile_run
    path = Path(path)
    try:
        compiled = compile_score_file(path, configured)
    except ScoreError as exc:
        raise ValueError(f'Could not read {path.name}: {exc.message} ({Path(exc.source).name} line {exc.line})') from None
    except OSError as exc:
        raise ValueError(f'Could not read {path.name}: {exc}') from None
    hz = compiled.header.get('hz')
    if hz not in (None, 60):
        raise ValueError(f'{path.name} is written for {hz} Hz. Live loading runs at 60 frames per second.')
    # Header settings the live run cannot apply are refused, never dropped.
    for key in ('save_ms', 'tier', 'render', 'keymap'):
        if compiled.header.get(key) is not None:
            raise ValueError(f'{path.name} sets {key}= in its header. Live loading cannot apply it. '
                             'Remove it from the header to load the inputs.')
    if compiled.header.get('start') not in (None, 'boot'):
        raise ValueError(f'{path.name} starts from a save. Live loading starts at the current frame. '
                         'Remove start= from the header to load the inputs.')
    lines = []
    for item in compiled.items:
        if isinstance(item, Mark):
            continue
        if isinstance(item, Segment):
            from kptas.score.compile import DEFAULT_PACE, DEFAULT_SCALE
            # Compiled segments carry the defaults explicitly; only real changes refuse.
            if item.hz in (None, 60) and item.scale in (None, DEFAULT_SCALE) and item.pace in (None, DEFAULT_PACE):
                continue
            raise ValueError(f'{path.name} changes timing in @segment {item.name}. Live loading needs plain inputs.')
        if not isinstance(item, Note):
            kind = {'SaveMs': 'save'}.get(type(item).__name__, type(item).__name__.lower())
            raise ValueError(f'{path.name} uses @{kind}. Live loading supports plain inputs only.')
        if item.osev is not None:
            raise ValueError(f'{path.name} sends an OS event ({render(item).split()[-1]}). Live loading cannot send it.')
        if item.count < 1:
            raise ValueError(f'{path.name} has an input line with no frames. Remove it to load live.')
        lines.append(render(item))
    if not lines:
        raise ValueError(f'{path.name} has no inputs to play.')
    text = '#!score v1\n' + ''.join(line + '\n' for line in lines)
    try:
        _notes, frames, _digest = compile_run(text)
    except (TelosError, ScoreError) as exc:
        raise ValueError(f'{path.name} cannot load live: {exc}') from None
    return LoadedScore(text, path.name, frames, str(path))


def remainder_directory(draft_path):
    base = Path(draft_path).parent if draft_path else Path.home() / '.local/state/kptas'
    return base / 'score-drafts'


def save_remainder(directory, name, run_id, tick, text, unit='frame'):
    """Keep a score's unexecuted inputs as a loadable .score draft; returns its path."""
    stem = re.sub(r'[^A-Za-z0-9._-]+', '-', Path(name or 'score').stem).strip('-.') or 'score'
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f'{stem[:60]}-rest-from-{unit}-{tick}-{(run_id or uuid.uuid4().hex)[:8]}.score'
    if path.exists():
        path = path.with_name(path.stem + '-' + uuid.uuid4().hex[:6] + '.score')
    temporary = path.with_name(path.name + '.tmp-' + uuid.uuid4().hex)
    with temporary.open('x', encoding='utf-8') as stream:
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    return path


def history_lines(history, unit='frame'):
    """Plain lines for the score starts kept in this recording, oldest first."""
    lines = []
    for tick, name, kind in history:
        label = name or 'a score'
        lines.append(f'{unit.capitalize()} {tick}: ' + (f'replaced with {label}' if kind == 'splice' else f'loaded {label}'))
    return lines
