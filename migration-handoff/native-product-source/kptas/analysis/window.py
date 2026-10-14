"""Frame-window finder (gap 9): replay a press shifted -k..+k frames (and held -h..+h longer).

The press is the set of held tokens that go down at the chosen frame. Only those tokens move;
every other input keeps its absolute frame. Each variant runs on a scratch take from the latest
captured restore point before the earliest changed frame, and is judged by a predicate at frame G
(`at`) or at any frame up to G (`by`). Results say which shifts still succeed.
"""
from __future__ import annotations

import dataclasses
from pathlib import Path

from kptas.supervisor import TelosError
from . import predicate as P
from . import span as S

MAX_SHIFT = 30
MAX_HOLD = 15
MAX_TRIALS = 121


def _order(k):
    out = [0]
    for d in range(1, k + 1):
        out += [-d, d]
    return out


def build(s, spec, prepare):
    """A plan for the engine. `prepare` is jobs.prepare (admission, anchor, tail, originals)."""
    try:
        frame = int(spec['frame'])
        k = int(spec.get('shift', 3))
        h = int(spec.get('hold', 0))
        mode = spec.get('mode', 'at')
        until = int(spec['until'])
    except (KeyError, TypeError, ValueError):
        raise TelosError('window needs integer frame, until, shift (and optional hold)') from None
    if not 0 <= k <= MAX_SHIFT or not 0 <= h <= MAX_HOLD:
        raise TelosError(f'shift must be 0..{MAX_SHIFT} and hold 0..{MAX_HOLD} frames')
    if mode not in ('at', 'by'):
        raise TelosError('mode must be "at" (holds at frame G) or "by" (holds at any frame up to G)')
    total = (2 * k + 1) * (2 * h + 1)
    if total > MAX_TRIALS:
        raise TelosError(f'{total} trials requested; the limit is {MAX_TRIALS}. Use a smaller shift or hold range')
    pred = P.parse_predicate(spec.get('predicate', ''))
    if (pred.needs_root or pred.uses_orig) and mode != 'at':
        raise TelosError('`same` and `orig` compare with the recording at one frame; use mode "at" '
                         '(with "by" they already hold before the press)')
    from kptas.score.keys import BUTTONS, canonical_token
    keys = frozenset(k if k in BUTTONS else canonical_token(k) for k in (spec.get('keys') or ())) or None

    found, last_error = None, None
    for back in (240, 120, 60, 30, k + 2):   # nearer anchors avoid a boot prefix with waits
        try:
            probe = prepare(s, first_modified=None, probe_frame=frame, probe_back=back)
            found = S.press_at(probe.tail, frame, keys)
            break
        except TelosError as exc:
            last_error = exc
    if found is None:
        raise last_error
    press, a, r = found
    ctx = prepare(s, first_modified=a - k, until=until, need_root=pred.needs_root)
    tail, A = ctx.tail, ctx.anchor
    G = ctx.until
    if G < r + k + h:
        raise TelosError(f'frame G ({G}) would cut the latest or longest press short; use G >= {r + k + h}')
    orig_frames = {t: S.tokens(tail.segs[tail.frames[t][0]].note) for t in tail.frames}

    def holds(t):
        return bool(press & orig_frames.get(t, frozenset()))

    trials = [{'label': 'control', 'shift': 0, 'hold': 0, 'control': True, 'notes': S.cut(tail, G)}]
    for dh in _order(h):
        for ds in _order(k):
            if ds == 0 and dh == 0:
                continue
            lo, hi = a + ds, r + ds + dh
            item = {'label': f's{ds:+d}h{dh:+d}', 'shift': ds, 'hold': dh}
            if hi < lo:
                item['skip'] = 'the press would be shorter than one frame'
            elif lo <= A or hi > tail.end:
                item['skip'] = 'outside the analysed frames'
            elif any(holds(t) for t in (lo - 1, hi + 1, *range(lo, hi + 1)) if not a <= t <= r):
                item['skip'] = 'merges with another press of the same key'
            else:
                remove = {t: press for t in range(a, r + 1)}
                add = {t: press for t in range(lo, hi + 1)}
                item['edits'] = (add, remove)
                item['notes'] = S.compress(tail, add, remove, upto=G)
            trials.append(item)

    def evaluate(trial, recs):
        return ctx.judge(pred, mode, recs, start=min(a, a + trial['shift']))

    def summarize(results):
        return summarize_window(results, a=a, r=r, frame=frame, hz=tail.hz, press=press, tail=tail, A=A)

    return dict(ctx=ctx, trials=trials, evaluate=evaluate, summarize=summarize,
                info={'press': sorted(press), 'press_first': a, 'press_last': r, 'anchor': A, 'until': G,
                      'mode': mode, 'predicate': pred.text, 'hz': tail.hz})


def _block(ok_by_value):
    """The contiguous run of successes around 0: (low, high) or None."""
    if not ok_by_value.get(0):
        return None
    low = high = 0
    while ok_by_value.get(low - 1):
        low -= 1
    while ok_by_value.get(high + 1):
        high += 1
    return low, high


def summarize_window(results, *, a, r, frame, hz, press, tail, A):
    shift_ok = {t['shift']: t.get('ok') for t in results if t.get('hold') == 0}
    hold_ok = {t['hold']: t.get('ok') for t in results if t.get('shift') == 0}
    block = _block(shift_ok)
    hblock = _block(hold_ok)
    out = {'press_frame': a, 'press_frames': r - a + 1, 'hz': hz}
    if block is None:
        out['text'] = 'The recorded press does not pass the check, so there is no window.'
    else:
        lo, hi = block
        n = hi - lo + 1
        out.update(early=-lo, late=hi, window_frames=n, window_ms=round(n * 1000 / hz, 1))
        out['text'] = (f'This press works from F{lo:+d} to F{hi:+d} (frames {a + lo}..{a + hi}): '
                       f'a {n}-frame window, {n * 1000 / hz:.0f} ms.')
        stray = sorted(v for v, ok in shift_ok.items() if ok and not lo <= v <= hi)
        if stray:
            out['also_works'] = stray
            out['text'] += ' Also works at ' + ', '.join(f'{v:+d}' for v in stray) + ' (not next to the window).'
    if hblock is not None and (len(hold_ok) > 1):
        out['hold_min'] = r - a + 1 + hblock[0]
        out['hold_max'] = r - a + 1 + hblock[1]
    # Per-frame inputs from up to 240 frames before the press, for the trainer export.
    first = max(A + 1, a - 240)
    out['frames_inputs'] = [[t, sorted(S.note_inputs_at(tail, t))] for t in range(first, r + 1)]
    return out


def export_trial(result: dict, path, *, title=None):
    """Write the measured window as an input-trainer trial through trainer's public functions."""
    from kptas.telos import trainer as T
    summary = result.get('summary') or {}
    if 'early' not in summary:
        raise TelosError('this result has no window to export')
    a = summary['press_frame']
    frames = [(t, frozenset(keys)) for t, keys in summary['frames_inputs']]
    trial = T.trial_from_frames(frames, title=title or f'Press at frame {a}')
    steps = list(trial.steps)
    index = next((i for i, st in enumerate(steps) if st.id == f'f{a}'), None)
    if index is None:
        raise TelosError(f'no key goes down at frame {a} in the exported inputs')
    step, notes = steps[index], []
    hold = {k: summary[k] for k in ('hold_min', 'hold_max') if k in summary}
    if step.timed:
        early = min(summary['early'], step.target)
        if early != summary['early']:
            notes.append(f'window_early clamped to {early} (the previous press is {step.target} frames earlier)')
        steps[index] = dataclasses.replace(step, early=early, late=summary['late'], **hold)
    else:
        notes.append('no earlier press in the exported frames; the step has no timing window')
        steps[index] = dataclasses.replace(step, **hold)
    trial = dataclasses.replace(trial, steps=tuple(steps))
    text = T.trial_text(trial)
    path = Path(path)
    with path.open('x', encoding='utf-8') as f:
        f.write(text)
    loaded = T.load_trial(path)        # round trip: the trainer must accept it
    return {'path': str(path), 'steps': len(loaded.steps), 'notes': notes}
