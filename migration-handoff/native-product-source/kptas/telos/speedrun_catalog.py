"""Speedrun catalog: the runs you can start, record or replay, in speedruns.toml.

File: $XDG_CONFIG_HOME/kptas/speedruns.toml (normally ~/.config/kptas/speedruns.toml).

    version = 1
    game = "/path/to/game"          # the folder with swkotor.exe and Saves
    library = "/path/to/score-library.json"

    [[run]]
    id = "chaosdrifter-anyu-10.1"
    name = "ChaosDrifter Any% Unrestricted 10.1 saves"
    category = "Any% Unrestricted"
    runner = "ChaosDrifter"
    source = "https://www.speedrun.com/kotor1/resources/kjs9s"
    start_save = "000106 - Game105"   # a folder in <game>/Saves
    status = "Route checkpoint saves. No input score in this pack."
    recordings = []                   # .holo takes you attached

    [[run.checkpoint]]
    name = "Skelly"
    save = "000106 - Game105"

    [[run.score]]
    path = "/path/to/segment.score"
    status = "score-format-candidate"
    replay_qualified = false

Statuses are copied from their source as written. Nothing here marks an input
replay-qualified; only replay evidence in the source library can.

Removing an entry removes these lines only. Saves and recordings stay on disk.
Installed saves come out through `telos saves remove`.

Loading never raises. A broken file gives an empty catalog and one warning.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import re
import tomllib

LIMIT = 512 * 1024
LOAD_GAME6 = '000007 - Game6'


class CatalogError(ValueError):
    pass


@dataclass
class Checkpoint:
    name: str
    save: str
    area: str = ''


@dataclass
class ScoreRef:
    path: str
    segment: str = ''
    variant: str = ''
    status: str = ''
    replay_qualified: bool = False
    timing_qualified: bool = False


@dataclass
class RunEntry:
    id: str
    name: str
    category: str = ''
    runner: str = ''
    runner_slug: str = ''
    submitted: str = ''
    run_id: str = ''
    source: str = ''
    attribution: str = ''
    start_save: str = ''
    status: str = ''
    notes: str = ''
    recordings: list = field(default_factory=list)
    checkpoints: list = field(default_factory=list)
    scores: list = field(default_factory=list)


@dataclass
class Catalog:
    game: str = ''
    library: str = ''
    runs: list = field(default_factory=list)

    def get(self, run_id):
        return next((run for run in self.runs if run.id == run_id), None)


RUN_TEXT = ('id', 'name', 'category', 'runner', 'runner_slug', 'submitted', 'run_id', 'source',
            'attribution', 'start_save', 'status', 'notes')


def catalog_path():
    from .ui_profiles import profile_directory
    return profile_directory().parent / 'speedruns.toml'


def _text(table, key, where):
    value = table.get(key, '')
    if not isinstance(value, str):
        raise CatalogError(f'{where}: {key} must be text')
    return value


def parse_catalog(data: bytes) -> Catalog:
    try:
        document = tomllib.loads(data.decode('utf-8'))
    except (UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise CatalogError(str(exc)) from exc
    if document.get('version', 1) != 1:
        raise CatalogError('version must be 1')
    catalog = Catalog(_text(document, 'game', 'top'), _text(document, 'library', 'top'))
    runs = document.get('run', [])
    if not isinstance(runs, list):
        raise CatalogError('[[run]] must be a list of tables')
    for i, table in enumerate(runs, 1):
        where = f'run {i}'
        if not isinstance(table, dict):
            raise CatalogError(f'{where} must be a table')
        entry = RunEntry(**{key: _text(table, key, where) for key in RUN_TEXT})
        if not entry.id or not entry.name:
            raise CatalogError(f'{where} needs an id and a name')
        if catalog.get(entry.id):
            raise CatalogError(f'{where}: id {entry.id!r} is used twice')
        recordings = table.get('recordings', [])
        if not isinstance(recordings, list) or not all(isinstance(r, str) for r in recordings):
            raise CatalogError(f'{where}: recordings must be a list of paths')
        entry.recordings = list(recordings)
        for row in table.get('checkpoint', []):
            if not isinstance(row, dict) or not row.get('save'):
                raise CatalogError(f'{where}: each checkpoint needs a save')
            entry.checkpoints.append(Checkpoint(_text(row, 'name', where) or row['save'],
                                                _text(row, 'save', where), _text(row, 'area', where)))
        for row in table.get('score', []):
            if not isinstance(row, dict) or not row.get('path'):
                raise CatalogError(f'{where}: each score needs a path')
            flags = {k: row.get(k, False) for k in ('replay_qualified', 'timing_qualified')}
            if not all(isinstance(v, bool) for v in flags.values()):
                raise CatalogError(f'{where}: replay_qualified and timing_qualified are true or false')
            entry.scores.append(ScoreRef(_text(row, 'path', where), _text(row, 'segment', where),
                                         _text(row, 'variant', where), _text(row, 'status', where), **flags))
        catalog.runs.append(entry)
    return catalog


def catalog_text(catalog: Catalog) -> str:
    quote = lambda value: json.dumps(value, ensure_ascii=False)
    lines = ['# Stasis speedrun catalog. Edit with More > Speedruns or `telos runs`.', 'version = 1',
             f'game = {quote(catalog.game)}', f'library = {quote(catalog.library)}']
    for run in catalog.runs:
        lines += ['', '[[run]]']
        lines += [f'{key} = {quote(getattr(run, key))}' for key in RUN_TEXT if getattr(run, key) or key in ('id', 'name')]
        lines.append(f'recordings = {quote(run.recordings)}')
        for point in run.checkpoints:
            lines += ['', '[[run.checkpoint]]', f'name = {quote(point.name)}', f'save = {quote(point.save)}']
            if point.area:
                lines.append(f'area = {quote(point.area)}')
        for score in run.scores:
            lines += ['', '[[run.score]]', f'path = {quote(score.path)}']
            lines += [f'{key} = {quote(getattr(score, key))}' for key in ('segment', 'variant', 'status')
                      if getattr(score, key)]
            lines += [f'replay_qualified = {quote(score.replay_qualified)}',
                      f'timing_qualified = {quote(score.timing_qualified)}']
    return '\n'.join(lines) + '\n'


def load_catalog(path=None):
    """-> (Catalog, warning text). Never raises."""
    from .ui_preferences import read_bytes
    path = Path(path) if path else catalog_path()
    try:
        return parse_catalog(read_bytes(path, LIMIT)), ''
    except FileNotFoundError:
        return Catalog(), ''
    except (OSError, ValueError) as exc:
        return Catalog(), f'The speedrun list is empty because {path} could not be read: {exc}'


def save_catalog(catalog: Catalog, path=None):
    from .ui_profiles import publish_file
    path = Path(path) if path else catalog_path()
    data = catalog_text(catalog).encode('utf-8')
    if len(data) > LIMIT:
        raise CatalogError('the speedrun list would exceed 512 KiB')
    parse_catalog(data)  # The same reader as user edits, before replacing anything.
    path.parent.mkdir(parents=True, exist_ok=True)
    publish_file(path, data)


# ---------------------------------------------------------------- edits

def _slug(text):
    return re.sub(r'[^a-z0-9]+', '-', text.casefold()).strip('-')[:48] or 'run'


def add_run(catalog: Catalog, name: str, **values) -> RunEntry:
    name = name.strip()
    if not name:
        raise CatalogError('Type a name for the run.')
    base = values.pop('id', '') or _slug(name)
    run_id, n = base, 2
    while catalog.get(run_id):
        run_id, n = f'{base}-{n}', n + 1
    entry = RunEntry(run_id, name, **values)
    catalog.runs.append(entry)
    return entry


def rename_run(catalog: Catalog, run_id: str, name: str) -> RunEntry:
    entry = catalog.get(run_id)
    if entry is None:
        raise CatalogError(f'No run with id {run_id!r}.')
    if not name.strip():
        raise CatalogError('Type a name for the run.')
    entry.name = name.strip()
    return entry


def remove_run(catalog: Catalog, run_id: str) -> RunEntry:
    """Drops the entry only. Its saves and recordings stay on disk."""
    entry = catalog.get(run_id)
    if entry is None:
        raise CatalogError(f'No run with id {run_id!r}.')
    catalog.runs.remove(entry)
    return entry


# ---------------------------------------------------------------- seeding

def seed(catalog: Catalog, game='', library='') -> list[RunEntry]:
    """Add runs from installed save packs and the score library. Existing ids are left alone."""
    added = []
    catalog.game = catalog.game or str(game or '')
    catalog.library = catalog.library or str(library or '')
    for entry in _pack_runs(catalog.game) + _library_runs(catalog.library):
        if catalog.get(entry.id) is None:
            catalog.runs.append(entry)
            added.append(entry)
    return added


def _pack_runs(game):
    if not game:
        return []
    from .save_packs import load_manifest, SavePackError
    try:
        manifest = load_manifest(game)
    except (OSError, ValueError, SavePackError):
        return []
    runs = []
    for key, pack in sorted(manifest['packs'].items()):
        slots = [r for r in manifest['slots'].values() if r['pack'] == key]
        slots.sort(key=lambda r: (r.get('time_played') is None, r.get('time_played') or 0, r['number']))
        points = [Checkpoint(r['name'].partition(': ')[2] or r['name'], r.get('same_as') or r['folder'],
                             r.get('area') or '') for r in slots]
        # The route starts at the earliest save by time played; Quick and Auto Saves are practice spots.
        route = [p for p, r in zip(points, slots) if not r.get('autosave') and r['source_folder'][:6] != '000000']
        runs.append(RunEntry(
            key, f"{pack['author']} {pack['version']} saves", pack['category'], pack['author'],
            pack['author'].casefold(), pack.get('published', ''), '', pack['resource'],
            f"speedrun.com resource, published by {pack['published_as']}, archive sha256 {pack['archive_sha256'][:12]}",
            route[0].save if route else '', 'Route checkpoint saves. No input score in this pack.',
            checkpoints=points))
    return runs


def _library_runs(library):
    if not library or not Path(library).is_file():
        return []
    root = Path(library).parent
    runs: dict[str, RunEntry] = {}

    def entry(run_id, runner, submitted, category, url):
        day = (submitted or '')[:10]
        if run_id not in runs:
            slug = _slug(runner).replace('-', '')
            runs[run_id] = RunEntry(f"{slug}__submitted-{day.replace('-', '')}__run-{run_id}",
                                    f'{runner} {category or "run"} ({run_id})', category or '', runner, slug, day,
                                    run_id, f'https://www.speedrun.com/kotor1/runs/{run_id}',
                                    f'{runner}, submitted {day}, run {run_id}')
        elif category and not runs[run_id].category:
            runs[run_id].category = category
        return runs[run_id]
    for path in sorted(root.glob('*.submission.json')):
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
            entry(data['id'], data['runner'], data.get('submitted'), data.get('category'), None)
        except (OSError, ValueError, KeyError, TypeError):
            continue
    try:
        segments = json.loads(Path(library).read_text(encoding='utf-8')).get('segments', [])
    except (OSError, ValueError):
        segments = []
    for segment in segments:
        if not isinstance(segment, dict) or not segment.get('run_id'):
            continue
        run = entry(segment['run_id'], segment.get('runner') or 'unknown', segment.get('submitted_utc'),
                    segment.get('category'), None)
        for variant in segment.get('variants', []):
            if variant.get('score_path'):
                run.scores.append(ScoreRef(variant['score_path'], segment.get('segment_id', ''),
                                           variant.get('id', ''), segment.get('input_projection_status') or '',
                                           variant.get('replay_qualified') is True,
                                           segment.get('timing_qualified') is True))
    for run in runs.values():
        if any(s.replay_qualified for s in run.scores):
            run.status = 'Some inputs are replay-qualified. See each score.'
        elif run.scores:
            run.status = 'Video-derived input candidates. None are replay-qualified.'
        else:
            run.status = 'Observed in video. No input score yet.'
        run.notes = ('A full run starts from New Game. Each score is one section of the run; '
                     'it needs a save at that exact point, which no pack provides.')
    return list(runs.values())


# ---------------------------------------------------------------- start a recording

@dataclass
class StartPlan:
    save: str
    name: str = ''
    row: int = 0
    total: int = 0
    command: str = ''
    steps: list = field(default_factory=list)
    alternative: str = ''
    problem: str = ''


def start_plan(catalog: Catalog, entry: RunEntry, checkpoint: Checkpoint | None = None) -> StartPlan:
    """What New (start recording) can do today, in plain steps.

    Stasis cannot yet start a recording straight from a save. Each session
    records from the main menu, so a recording starts with the load. Only the
    Game6 reference save loads by itself (--load-game6), and only while it is
    the first save in the list.
    """
    save = checkpoint.save if checkpoint else entry.start_save
    plan = StartPlan(save)
    if not save:
        if entry.run_id:
            plan.steps = ['Start a new session: ./stasis play stasis.toml. It records from the main menu.',
                          'Choose New Game. The take records the whole run from there.']
        else:
            plan.problem = 'This run has no start save. Pick a checkpoint, or set a start save.'
        return plan
    from .save_packs import listing
    rows = listing(catalog.game) if catalog.game else []
    row = next((r for r in rows if r['folder'] == save), None)
    if not catalog.game:
        plan.problem = 'Set the game folder first. It is the folder with swkotor.exe.'
        return plan
    if row is None:
        plan.problem = (f'"{save}" is not in {catalog.game}/Saves. Install the speedrun saves with '
                        f'telos saves install --game "{catalog.game}".')
        return plan
    plan.name, plan.row, plan.total = row['name'], row['row'], len(rows)
    if save == LOAD_GAME6 and row['row'] == 1:
        plan.command = './stasis play stasis.toml --load-game6'
        plan.steps = ['Start a new session with --load-game6. Stasis loads this save by itself, then records.']
        return plan
    plan.steps = [
        'Start a new session: ./stasis play stasis.toml. It records from the main menu.',
        'Choose Load Game.',
        f'Choose "{row["name"]}". It is row {row["row"]} of {len(rows)}, counted by folder name.',
        'Choose Load. The take records the load and everything after it.',
    ]
    plan.alternative = ('Already in a session? Quit to the main menu there, then do steps 2 to 4. '
                        'The open take keeps recording, so the quit is part of it.')
    plan.command = './stasis play stasis.toml'
    return plan


PIN_NOTE = ('If your config sets game.pin, each start copies the pin\'s Saves folder over the game\'s. '
            'Saves that are not in the pin are removed. Install them into the pin folder too.')
