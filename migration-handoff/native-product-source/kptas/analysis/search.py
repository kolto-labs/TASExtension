"""Bounded brute-force input search (gap 11, BizHawk BasicBot style).

A candidate adds one press (a token set from `keys`) at start+offset for `duration` frames on top
of the recorded inputs. The space is keys x offsets x durations, explicit and capped; when it
is larger than max_attempts a seeded, deterministic sample is tried. The objective is read from
the recorded journal at frame G: maximize or minimize a value, or reach a predicate earliest.
"""
from __future__ import annotations

import itertools
import random

from kptas.score.keys import BUTTONS, canonical_token
from kptas.supervisor import TelosError
from . import predicate as P
from . import span as S

MAX_WINDOW = 120
MAX_DURATIONS = 16
MAX_KEYS = 16
MAX_ATTEMPTS = 150


def _press(text):
    out = []
    for tok in str(text).split():
        out.append(tok if tok in BUTTONS else canonical_token(tok))
    if not out:
        raise TelosError('each key entry needs at least one key or button')
    return frozenset(out)


def build(s, spec, prepare):
    try:
        start = int(spec['start'])
        window = int(spec['window'])
        until = int(spec['until'])
        durations = sorted({int(d) for d in spec['durations']})
        presses = [_press(k) for k in spec['keys']]
        attempts = int(spec['max_attempts'])
        seed = int(spec.get('seed', 0))
    except KeyError as exc:
        raise TelosError(f'search needs {exc.args[0]}; unbounded searches are refused') from None
    except (TypeError, ValueError) as exc:
        raise TelosError(f'bad search value: {exc}') from None
    if not 1 <= window <= MAX_WINDOW:
        raise TelosError(f'window must be 1..{MAX_WINDOW} frames')
    if not durations or len(durations) > MAX_DURATIONS or durations[0] < 1 or durations[-1] > 240:
        raise TelosError(f'give 1..{MAX_DURATIONS} durations of 1..240 frames')
    if not presses or len(presses) > MAX_KEYS:
        raise TelosError(f'give 1..{MAX_KEYS} keys or chords')
    if not 1 <= attempts <= MAX_ATTEMPTS:
        raise TelosError(f'max_attempts must be 1..{MAX_ATTEMPTS}')
    goal = spec.get('goal')
    if goal in ('maximize', 'minimize'):
        objective = P.parse_objective(spec.get('objective', ''))
        pred = None
    elif goal == 'earliest':
        objective, pred = None, P.parse_predicate(spec.get('objective', ''))
        if pred.needs_root or pred.uses_orig:
            raise TelosError('`same` and `orig` hold before any change, so they cannot be reached earliest; '
                             'use fixed values')
    else:
        raise TelosError('goal must be maximize, minimize or earliest')
    require = P.parse_predicate(spec['require']) if spec.get('require') else None

    ctx = prepare(s, first_modified=start, until=until, need_root=bool(require and require.needs_root))
    tail, A, G = ctx.tail, ctx.anchor, ctx.until
    if start + window - 1 > G:
        raise TelosError(f'the search window ends after frame G ({G})')
    space = list(itertools.product(range(len(presses)), range(window), durations))
    picked = range(len(space))
    if len(space) > attempts:
        picked = sorted(random.Random(seed).sample(range(len(space)), attempts))
    trials = [{'label': 'baseline', 'control': True, 'notes': S.cut(tail, G)}]
    for index in picked:
        p, off, dur = space[index]
        press = presses[p]
        lo, hi = start + off, min(start + off + dur - 1, tail.end)
        item = {'label': f'c{index}', 'index': index, 'keys': sorted(press), 'offset': off, 'duration': dur,
                'frame': lo}
        add = {t: press for t in range(lo, hi + 1) if not press <= tail.toks(t)}
        if not add:
            item['skip'] = 'the recording already holds these keys there'
        else:
            item['edits'] = (add, {})
            item['notes'] = S.compress(tail, add, None, upto=G)
        trials.append(item)

    def evaluate(trial, recs):
        if require is not None:
            ok = ctx.judge(require, 'at', recs, start=start)
            if not ok['ok']:
                return {'ok': False, 'score': None, 'notes': ['require failed'] + ok.get('notes', [])}
        if pred is not None:
            got = ctx.judge(pred, 'by', recs, start=start)
            return {'ok': got['ok'], 'score': got.get('frame'), 'notes': got.get('notes', [])}
        journal, orig = ctx.journals(recs, G)
        value = P.score(objective, journal, orig)
        return {'ok': value is not None, 'score': value,
                'notes': [] if value is not None else [f'{objective.path}: missing or not a number']}

    def summarize(results):
        better = (lambda x: -x) if goal == 'maximize' else (lambda x: x)
        base = next((t for t in results if t.get('control')), {})
        ranked = sorted((t for t in results if not t.get('control') and t.get('ok') and t.get('score') is not None),
                        key=lambda t: (better(t['score']), t['duration'], t['offset'], t['index']))
        top = ranked[:10]
        out = {'goal': goal, 'baseline': base.get('score'), 'tried': sum(1 for t in results if not t.get('control')
                                                                       and 'skip' not in t),
               'space': len(space), 'sampled': len(space) > attempts, 'seed': seed,
               'best': [{k: t.get(k) for k in ('label', 'keys', 'frame', 'offset', 'duration', 'score', 'take',
                                                 'score_file')} for t in top]}
        if top and base.get('score') is not None:
            out['improves'] = better(top[0]['score']) < better(base['score'])
        out['text'] = (f'Best: {"+".join(top[0]["keys"])} at frame {top[0]["frame"]} for {top[0]["duration"]} '
                       f'frames, score {top[0]["score"]} (baseline {base.get("score")}).' if top
                       else 'No candidate produced a score.')
        return out

    return dict(ctx=ctx, trials=trials, evaluate=evaluate, summarize=summarize, keep_best=5,
                info={'anchor': A, 'until': G, 'goal': goal, 'objective': spec.get('objective'),
                      'space': len(space), 'hz': tail.hz})
