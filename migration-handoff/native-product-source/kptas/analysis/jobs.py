"""Analysis jobs inside the live owner: admission, sliced trials, the return trip, cache, ops.

A job runs one owner operation per slice (kptas.telos.live.serve calls advance() between
commands), so status and cancel stay answerable. Each trial:

    rewind(anchor)             strict restore of the latest captured restore point before the
                               first changed frame; the owner seals its take and opens a scratch take
    step(note) ...             the trial's inputs up to frame G, recorded into that scratch take
    read records anchor+1..G   journal and state_root from the scratch take; judge the predicate

The first trial is a control: the recorded inputs verbatim. Its journals and roots must equal
the original take's, or the job stops and says the replay is not deterministic.

Every job ends with the return trip, also after cancel or error: rewind(anchor), then replay
the original authored notes and marks to the user's frame, then check that frame's state_root
equals the one before the job. The user's own take is sealed by the first rewind and never
written again. Scratch takes stay on disk: each later take names the previous take as its
parent, so deleting one would break the takes after it. "Discard" means listed, not adopted.

Recording-mode owners only (telos live). Frame clock precision only.
"""
from __future__ import annotations

import hashlib
import json
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kptas.score import Mark, Note
from kptas.supervisor import TelosError
from kptas.types import TickFlag
from . import predicate as P
from . import span as S

MAX_BACK = 1800          # frames searched backwards for a restore point
MAX_TRIAL_FRAMES = 3600  # anchor..G
MAX_RETURN_FRAMES = 7200 # anchor..user frame, replayed once per job
KEEP_JOBS = 8
READ_OPS = frozenset({'takes', 'inspect', 'state', 'render', 'view'})
# Same text as the owner's compare-and-swap refusal: nothing ran, the client must refresh.
# The workspace reads it as "stale", not as an unknown outcome needing Reconnect.
BUSY = 'session, take, frame or revision changed; refresh required'


# ------------------------------------------------------------------ admission

@dataclass
class Context:
    session: Any
    anchor: int
    user_tick: int
    user_root: str
    user_text: str
    until: int
    tail: S.Tail
    prefix_items: list
    orig: dict = field(default_factory=dict)       # tick -> (journal dict, root hex or None, journal digest)
    identity: str = ''

    def journals(self, recs, t):
        have = recs.get(t)
        orig = self.orig.get(t)
        return (have[0] if have else {}), (orig[0] if orig else None)

    def judge(self, pred, mode, recs, *, start):
        if mode == 'at':
            have, orig = recs.get(self.until), self.orig.get(self.until)
            if have is None:
                return {'ok': False, 'notes': [f'frame {self.until} was not reached']}
            ok, notes = P.evaluate(pred, have[0], orig[0] if orig else None, root=have[1],
                                   orig_root=orig[1] if orig else None)
            return {'ok': ok, 'notes': notes, 'frame': self.until if ok else None}
        notes = []
        for t in range(max(start, self.anchor + 1), self.until + 1):
            have, orig = recs.get(t), self.orig.get(t)
            if have is None:
                break
            ok, n = P.evaluate(pred, have[0], orig[0] if orig else None)
            notes = n or notes
            if ok:
                return {'ok': True, 'frame': t, 'notes': []}
        return {'ok': False, 'frame': None, 'notes': notes}


def _record(bundle, t):
    from kptas.holo import verify as hv
    rec = bundle.record(t)
    try:
        journal = json.loads(rec.journal) if rec.journal else {}
    except ValueError:
        journal = {}
    root = rec.state_root.hex() if rec.flags & TickFlag.CAPTURED else None
    return journal, root, hv.journal_digest(rec).hex(), rec.flags


def _core(journal):
    """A journal without its note text (chunking-dependent, count-free) and restore events."""
    out = {k: v for k, v in journal.items() if k != 'note'}
    if isinstance(out.get('ev'), list):
        out['ev'] = [e for e in out['ev'] if not (isinstance(e, dict) and e.get('name') == 'restore')]
    return out


def admit(s):
    if getattr(s, 'mode', None) != 'recording':
        raise TelosError('analysis runs on a live recording owner (telos live); archive playback is not supported')
    if s.closed or s.broken or s.sup is None or s.sink is None:
        raise TelosError('the owner needs recovery before analysis')
    if s.run_loaded:
        raise TelosError('a score is loaded; stop or unload it before analysis')
    if s.input_scheduler_running or s.playing or not getattr(s, 'engaged', True):
        raise TelosError('pause playback and engage TAS before analysis')
    if s.cfg.get('clock', {}).get('precision') != 'frame':
        raise TelosError('analysis needs frame clock precision')


def make_prepare():
    def prepare(s, *, first_modified, until=None, need_root=False, probe_frame=None, probe_back=240):
        admit(s)
        here = s.inspect()            # raises unless the current frame is durable
        T = here['tick']
        if not here.get('captured'):
            raise TelosError('the current frame is not a restore point; step once, then start the analysis')
        at = S.placement(s, T)
        items = list(s.items)
        bundle = s.sink.bundle
        if probe_frame is not None:
            # Provisional anchor only to locate a press; nothing runs.
            cands = [t for t in at if t <= probe_frame - 1 - probe_back]
            if not cands:
                raise TelosError(f'no frame {probe_back} frames before frame {probe_frame} to read the press from')
            A0 = max(cands)
            return Context(s, A0, T, here['state_root'], s.text(), T, S.build_tail(items, at, A0, T), [])
        if until is None or not first_modified - 1 < until <= T:
            raise TelosError(f'frame G must be after frame {first_modified - 1} and no later than the current frame {T}')
        A = fallback = None
        for t in range(first_modified - 1, max(-1, first_modified - 1 - MAX_BACK), -1):
            cur = at.get(t)
            if cur is None:
                continue
            flags = bundle.record(t).flags
            if flags & TickFlag.CAPTURED and flags & TickFlag.RESTORE_POINT:
                if cur.j == 0:
                    A = t
                    break
                fallback = fallback if fallback is not None else t
        A = A if A is not None else fallback
        if A is None:
            raise TelosError(f'no restore point in the {MAX_BACK} frames before frame {first_modified}')
        if until - A > MAX_TRIAL_FRAMES or T - A > MAX_RETURN_FRAMES:
            raise TelosError(f'the anchor at frame {A} is too far back (trials {until - A} frames, return trip '
                             f'{T - A}; limits {MAX_TRIAL_FRAMES}/{MAX_RETURN_FRAMES}). Rewind closer to the press first')
        tail = S.build_tail(items, at, A, T)
        G = until
        if need_root and not bundle.record(G).flags & TickFlag.CAPTURED:
            later = [t for t in range(G + 1, T + 1) if bundle.record(t).flags & TickFlag.CAPTURED]
            if not later:
                raise TelosError(f'frame {G} has no captured state to compare; choose a captured frame')
            G = later[0]
        cur = at[A]
        prefix = items[:cur.i] + ([__import__('dataclasses').replace(items[cur.i], count=cur.j)] if cur.j else [])
        ctx = Context(s, A, T, here['state_root'], s.text(), G, tail, prefix)
        for t in range(A, G + 1):
            j, root, digest, _ = _record(bundle, t)
            ctx.orig[t] = (j, root, digest)
        ctx.identity = hashlib.sha256(json.dumps({
            'env': bundle.env.key(), 'anchor_root': bundle.record(A).state_root.hex(),
            'anchor': A, 'tail': s.text(tail.items)}, sort_keys=True).encode()).hexdigest()
        return ctx
    return prepare


# ------------------------------------------------------------------ jobs

@dataclass
class Job:
    id: str
    kind: str
    spec: dict
    plan: dict
    dir: Path
    status: str = 'running'
    phase: str = 'trials'
    done: int = 0
    results: list = field(default_factory=list)
    summary: dict | None = None
    error: str | None = None
    returned: dict | None = None
    cancel: bool = False
    cached: bool = False
    user_take: str | None = None
    started: float = field(default_factory=time.time)
    gen: Any = None

    def snapshot(self, full=True):
        out = {'job_id': self.id, 'kind': self.kind, 'status': self.status, 'phase': self.phase,
               'done': self.done, 'total': len(self.plan.get('trials', ())) if self.plan else len(self.results),
               'info': self.plan.get('info') if self.plan else None, 'error': self.error,
               'summary': self.summary, 'returned': self.returned, 'cached': self.cached, 'dir': str(self.dir),
               'elapsed_s': round(time.time() - self.started, 1), 'spec': self.spec}
        if full:
            out['trials'] = self.results
        return out


def _registry(s):
    reg = getattr(s, '_analysis_jobs', None)
    if reg is None:
        reg = s._analysis_jobs = {'jobs': {}, 'active': None}
    return reg


def active(s):
    reg = getattr(s, '_analysis_jobs', None)
    job = reg and reg['active']
    return job if job is not None and job.status == 'running' else None


def guard(s, op):
    """Refuse every owner command except reads while a job runs."""
    if active(s) is not None and op not in READ_OPS:
        print(f'telos analysis: refused {op!r} while analysis job {active(s).id} runs', file=sys.stderr, flush=True)
        raise TelosError(BUSY)


def _write(job):
    from kptas.durability import atomic_json
    job.dir.mkdir(parents=True, exist_ok=True)
    atomic_json(job.dir / 'job.json', {'format': 'kptas-analysis/1', **job.snapshot(),
                                       'identity': job.plan['ctx'].identity if job.plan else None})


def _cache_path(s, kind, spec, identity):
    key = hashlib.sha256(json.dumps({'kind': kind, 'spec': spec, 'identity': identity},
                                    sort_keys=True).encode()).hexdigest()
    return Path(s.cur.path).parent / 'analysis' / 'cache' / f'{key}.json'


def start(s, kind, spec, resume=None):
    reg = _registry(s)
    if active(s) is not None:
        raise TelosError(f'analysis job {active(s).id} is running; cancel it first')
    if kind == 'window':
        from .window import build
    elif kind == 'search':
        from .search import build
    else:
        raise TelosError('kind must be window or search')
    if not isinstance(spec, dict):
        raise TelosError('spec must be an object')
    plan = build(s, spec, make_prepare())
    ctx = plan['ctx']
    job_id = uuid.uuid4().hex[:12]
    root = Path(s.cur.path).parent / 'analysis'
    job = Job(job_id, kind, spec, plan, root / f'{kind}-{job_id}')
    cache = _cache_path(s, kind, spec, ctx.identity)
    if cache.exists() and not spec.get('no_cache'):
        stored = json.loads(cache.read_text())
        job.results, job.summary = stored['trials'], stored['summary']
        job.status, job.phase, job.cached, job.done = 'completed', 'done', True, len(job.results)
        job.returned = {'ok': True, 'note': 'cached result; the game was not touched'}
        reg['jobs'][job_id] = job
        return job.snapshot()
    if resume:
        old = root / f'{kind}-{resume}'
        prior = json.loads((old / 'job.json').read_text()) if (old / 'job.json').is_file() else None
        if not prior or prior.get('identity') != ctx.identity or prior.get('spec') != spec:
            raise TelosError('resume needs the same spec on the same recorded inputs')
        job.results = [t for t in prior.get('trials', []) if 'error' not in t]
    job.gen = _run(job, s)
    reg['jobs'][job_id] = job
    reg['active'] = job
    while len(reg['jobs']) > KEEP_JOBS:
        oldest = next(iter(reg['jobs']))
        if reg['jobs'][oldest] is job or reg['jobs'][oldest].status == 'running':
            break
        reg['jobs'].pop(oldest)
    _write(job)
    return job.snapshot()


def advance(s):
    """Run one slice of the active job. Never raises."""
    job = active(s)
    if job is None:
        return
    try:
        next(job.gen)
    except StopIteration:
        pass
    except Exception as exc:   # noqa: BLE001 - recorded in the job; the owner keeps serving
        job.status, job.error = 'failed', f'{type(exc).__name__}: {exc}'
    if job.status != 'running':
        try:
            _write(job)
        except Exception as exc:  # noqa: BLE001
            print(f'telos analysis: job record not written: {exc}', file=sys.stderr, flush=True)


def _trial(job, s, trial):
    ctx = job.plan['ctx']
    A = ctx.anchor
    began = time.perf_counter()
    r = s.rewind(A)
    if not isinstance(r, dict) or r.get('rewound') is not True or s.sup.tick != A:
        raise TelosError(f'trial rewind to frame {A} did not land: {r.get("reason") if isinstance(r, dict) else r}')
    phases = (r.get('receipt') or {}).get('phases')
    take = r.get('take')
    restore_s = time.perf_counter() - began
    yield
    t = A
    for note in trial['notes']:
        out = s.step(note)
        t += note.count
        if out.get('tick') != t:
            raise TelosError(f'trial step ended at frame {out.get("tick")}, expected {t}')
        yield
    bundle = s.sink.bundle
    recs = {}
    for k in range(A + 1, t + 1):
        j, root, digest, _ = _record(bundle, k)
        recs[k] = (j, root, digest)
    return recs, {'take': take, 'restore_s': round(restore_s, 3), 'wall_s': round(time.perf_counter() - began, 3),
                  'phases': phases, 'frames': t - A}


def _run(job, s):
    plan, ctx = job.plan, job.plan['ctx']
    done = {t['label'] for t in job.results}
    try:
        for trial in plan['trials']:
            if job.cancel:
                break
            if trial['label'] in done:
                job.done += 1
                continue
            row = {k: v for k, v in trial.items() if k not in ('notes', 'edits')}
            if 'skip' in trial:
                row['ok'] = None
            else:
                try:
                    recs, cost = yield from _trial(job, s, trial)
                except Exception as exc:   # noqa: BLE001
                    row.update(ok=None, error=f'{type(exc).__name__}: {exc}')
                    job.results.append(row)
                    job.done += 1
                    raise
                row.update(cost)
                if trial.get('control'):
                    for k in range(ctx.anchor + 1, ctx.until + 1):
                        have, want = recs.get(k), ctx.orig.get(k)
                        if have is None or want is None or (have[2] != want[2] and _core(have[0]) != _core(want[0])) or (
                                have[1] and want[1] and have[1] != want[1]):
                            job.error = (f'not deterministic: replaying the recorded inputs differs from the '
                                         f'recording at frame {k}; no window or search result is reported')
                            raise TelosError(job.error)
                row.update(plan['evaluate'](trial, recs))
                if trial.get('control') and job.kind == 'window' and not row.get('ok'):
                    # Not an error: the summary says there is no window. Shifted trials cannot pass
                    # a check the recorded input fails, so they are not run.
                    job.results.append(row)
                    job.done += 1
                    break
                if trial.get('edits') and (job.kind == 'search'):
                    row['score_text'] = _score_text(ctx, trial)
            job.results.append(row)
            job.done += 1
            _write(job)
    except Exception as exc:   # noqa: BLE001 - the return trip still runs
        job.error = job.error or f'{type(exc).__name__}: {exc}'
    job.phase = 'return'
    yield
    try:
        job.returned = yield from _return(job, s)
    except Exception as exc:   # noqa: BLE001
        job.returned = {'ok': False, 'error': f'{type(exc).__name__}: {exc}',
                        'recover': f'reopen take {job.user_take} at frame {ctx.user_tick}'}
    try:
        job.summary = plan['summarize'](job.results)
        _keep(job)
    except Exception as exc:   # noqa: BLE001
        job.error = job.error or f'summary failed: {exc}'
    job.phase = 'done'
    if not job.returned or not job.returned.get('ok'):
        job.status = 'failed'
    elif job.error:
        job.status = 'failed'
    else:
        job.status = 'cancelled' if job.cancel else 'completed'
    if job.status == 'completed':
        try:
            cache = _cache_path(s, job.kind, job.spec, ctx.identity)
            cache.parent.mkdir(parents=True, exist_ok=True)
            from kptas.durability import atomic_json
            atomic_json(cache, {'trials': job.results, 'summary': job.summary})
        except Exception as exc:   # noqa: BLE001
            print(f'telos analysis: cache not written: {exc}', file=sys.stderr, flush=True)


def _return(job, s):
    """Replay the original inputs from the anchor to the user's frame and prove the state."""
    ctx = job.plan['ctx']
    r = s.rewind(ctx.anchor)
    if not isinstance(r, dict) or r.get('rewound') is not True or s.sup.tick != ctx.anchor:
        raise TelosError(f'return rewind to frame {ctx.anchor} did not land')
    yield
    for item in ctx.tail.items:
        if isinstance(item, Mark):
            s.mark(item.name)
        elif isinstance(item, Note):
            s.step(item)
            yield
    here = s.inspect()
    ok = here['tick'] == ctx.user_tick and here['state_root'] == ctx.user_root
    return {'ok': ok, 'tick': here['tick'], 'take': here['take'], 'state_root_matches': ok,
            'authored_text_matches': s.text() == ctx.user_text,
            **({} if ok else {'error': f'state at frame {here["tick"]} differs from before the job'})}


def _score_text(ctx, trial):
    add, remove = trial['edits']
    notes = S.compress(ctx.tail, add, remove)
    return ctx.session.text(list(ctx.prefix_items) + notes)


def _keep(job):
    """Write the best search candidates (or kept window trials) as replayable score files."""
    keep = job.plan.get('keep_best', 0)
    rows = job.results
    if job.kind == 'search' and job.summary:
        labels = [b['label'] for b in job.summary.get('best', [])[:keep]]
        (job.dir / 'candidates').mkdir(parents=True, exist_ok=True)
        for rank, label in enumerate(labels, 1):
            row = next(t for t in rows if t['label'] == label)
            path = job.dir / 'candidates' / f'rank-{rank:02d}-{label}.score'
            path.write_text(row.get('score_text', ''), encoding='utf-8')
            for b in job.summary['best']:
                if b['label'] == label:
                    b['score_file'] = str(path)
    for row in rows:
        row.pop('score_text', None)
        if row.get('take'):
            row['scratch'] = True    # listed, not adopted; see module docstring


# ------------------------------------------------------------------ ops

def handle_op(s, req):
    op = req.get('op')
    reg = _registry(s)
    with s._lock:
        if op == 'analysis_start':
            if s._operation_depth:
                raise TelosError('analysis start requires a completed owner boundary')
            out = start(s, req.get('kind'), dict(req.get('spec') or {}), resume=req.get('resume'))
            reg['jobs'][out['job_id']].user_take = out['user_take'] = s.cur.path if s.takes else None
            return out
        if op == 'analysis_status':
            job_id = req.get('job_id')
            job = reg['jobs'].get(job_id) if job_id else (reg['active'] or (list(reg['jobs'].values())[-1]
                                                                           if reg['jobs'] else None))
            if job is None:
                raise TelosError('no analysis job')
            return job.snapshot(full=req.get('full', True) is not False)
        if op == 'analysis_cancel':
            job = reg['jobs'].get(req.get('job_id'))
            if job is None:
                raise TelosError('no such analysis job')
            job.cancel = True
            return job.snapshot(full=False)
        if op == 'analysis_list':
            return {'jobs': [j.snapshot(full=False) for j in reg['jobs'].values()]}
    raise TelosError(f'unknown analysis op {op!r}')
