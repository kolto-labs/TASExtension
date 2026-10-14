"""telos saves ... and telos runs ...: speedrun save packs and the speedrun catalog. No game control."""
from __future__ import annotations

from pathlib import Path

from kptas.supervisor import TelosError


def add_commands(sub):
    saves = sub.add_parser('saves', help='install or remove the published speedrun save packs')
    actions = saves.add_subparsers(dest='saves_action', required=True, metavar='action')
    for name, help in (('install', 'verify the packs and copy every slot into GAME/Saves'),
                       ('list', 'list GAME/Saves in the order the game shows it'),
                       ('remove', 'delete the slots this installer added and nothing else')):
        p = actions.add_parser(name, help=help, description=help)
        p.set_defaults(fn=saves_command)
        p.add_argument('--game', required=True, help='the folder that holds swkotor.exe and Saves')
        if name != 'list':
            p.add_argument('--pack', action='append', help='chaos or lane (default both)')
        if name == 'install':
            p.add_argument('--archives', help='folder with the pack zips (default ~/.local/share/kptas/save-packs)')
            p.add_argument('--no-download', action='store_true', help='fail instead of downloading a missing pack')
            p.add_argument('--keep-names', action='store_true', help='keep the in-game names as published')

    runs = sub.add_parser('runs', help='the speedrun catalog (~/.config/kptas/speedruns.toml)')
    actions = runs.add_subparsers(dest='runs_action', required=True, metavar='action')
    for name, help in (('list', 'list runs, checkpoints and scores'),
                       ('seed', 'add runs from installed save packs and the score library'),
                       ('add', 'add a run'), ('rename', 'rename a run'),
                       ('remove', 'remove a run from the list; files stay on disk'),
                       ('start', 'show how to start a recording from a run or checkpoint')):
        p = actions.add_parser(name, help=help, description=help)
        p.set_defaults(fn=runs_command)
        p.add_argument('--catalog', help='catalog file (default ~/.config/kptas/speedruns.toml)')
        if name == 'seed':
            p.add_argument('--game', help='game folder; also stored in the catalog')
            p.add_argument('--library', help='score-library.json from the imported speedrun inputs')
        elif name == 'add':
            p.add_argument('name')
            p.add_argument('--category', default='')
            p.add_argument('--runner', default='')
            p.add_argument('--source', default='')
            p.add_argument('--start-save', default='', help='a folder name in GAME/Saves')
        elif name in ('rename', 'remove', 'start'):
            p.add_argument('id')
            if name == 'rename':
                p.add_argument('name')
            if name == 'start':
                p.add_argument('--checkpoint', help='checkpoint name or save folder')


def saves_command(_ctx, a):
    from . import save_packs as sp
    try:
        if a.saves_action == 'list':
            rows = sp.listing(a.game)
            if not rows:
                print(f'No save slots in {Path(a.game) / "Saves"}.')
            for row in rows:
                pack = f'  [{row["pack"]} {row["source_folder"]}]' if row['pack'] else ''
                print(f'{row["row"]:>3}  {row["folder"]:<20} {row["name"]}{pack}')
            return 0
        packs = [sp.pack(key) for key in a.pack] if a.pack else list(sp.PACKS)
        if a.saves_action == 'install':
            results = sp.install(a.game, packs=packs, archives=a.archives, rename=not a.keep_names,
                                 download=not a.no_download)
            counts = {state: sum(r['state'] == state for r in results) for state in ('installed', 'kept', 'skipped')}
            print(f"{counts['installed']} installed, {counts['kept']} already there, {counts['skipped']} skipped. "
                  f'Manifest: {sp.manifest_path(a.game)}')
            return 1 if counts['skipped'] else 0
        results = sp.remove(a.game, packs=packs)
        changed = sum(r['state'] == 'changed' for r in results)
        print(f"{sum(r['state'] == 'removed' for r in results)} removed, {changed} kept because they changed.")
        return 1 if changed else 0
    except sp.SavePackError as exc:
        raise TelosError(str(exc)) from exc


def runs_command(_ctx, a):
    from . import speedrun_catalog as sc
    path = Path(a.catalog).expanduser() if a.catalog else sc.catalog_path()
    catalog, warning = sc.load_catalog(path)
    if warning:
        raise TelosError(warning)
    try:
        if a.runs_action == 'list':
            if not catalog.runs:
                print(f'No runs in {path}. Add them with telos runs seed --game GAME --library LIBRARY.')
            for run in catalog.runs:
                print(f'{run.id}\n  {run.name}  [{run.category or "no category"}]  {run.runner}')
                print(f'  Start save: {run.start_save or "none"}  ·  {run.status or "no status"}')
                if run.source:
                    print(f'  Source: {run.source}')
                for point in run.checkpoints:
                    print(f'    checkpoint  {point.save:<20} {point.name}')
                for score in run.scores:
                    flags = 'replay-qualified' if score.replay_qualified else 'not replay-qualified'
                    print(f'    score       {Path(score.path).name}  ({score.status or "no status"}, {flags})')
                for take in run.recordings:
                    print(f'    recording   {take}')
            return 0
        if a.runs_action == 'start':
            entry = catalog.get(a.id)
            if entry is None:
                raise TelosError(f'no run with id {a.id!r}')
            point = None
            if a.checkpoint:
                point = next((p for p in entry.checkpoints if a.checkpoint in (p.name, p.save)), None)
                if point is None:
                    raise TelosError(f'{entry.name} has no checkpoint {a.checkpoint!r}')
            plan = sc.start_plan(catalog, entry, point)
            if plan.problem:
                print(plan.problem)
                return 1
            for i, step in enumerate(plan.steps, 1):
                print(f'{i}. {step}')
            if plan.alternative:
                print(plan.alternative)
            print(sc.PIN_NOTE)
            return 0
        if a.runs_action == 'seed':
            added = sc.seed(catalog, a.game or '', a.library or '')
            for run in added:
                print(f'added  {run.id}  {run.name}')
            print(f'{len(added)} added, {len(catalog.runs)} in {path}.')
        elif a.runs_action == 'add':
            run = sc.add_run(catalog, a.name, category=a.category, runner=a.runner, source=a.source,
                             start_save=a.start_save)
            print(f'added  {run.id}  {run.name}')
        elif a.runs_action == 'rename':
            print(f'renamed  {sc.rename_run(catalog, a.id, a.name).id}')
        elif a.runs_action == 'remove':
            run = sc.remove_run(catalog, a.id)
            print(f'removed  {run.id}. Its saves and recordings stay on disk.')
        sc.save_catalog(catalog, path)
        return 0
    except sc.CatalogError as exc:
        raise TelosError(str(exc)) from exc
