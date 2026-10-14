"""`telos window ...` and `telos search ...`: clients of the running live owner's analysis jobs."""
from __future__ import annotations

import json
import socket
import sys
import time
import uuid
from pathlib import Path

from kptas.supervisor import TelosError

TERMINAL = frozenset({'completed', 'cancelled', 'failed'})


def request(port, op, timeout=10.0, **args):
    """One NDJSON request to the owner. A slow reply while a slice runs is reported as busy."""
    rid = uuid.uuid4().hex
    payload = json.dumps({'id': rid, 'op': op, **args}, allow_nan=False).encode() + b'\n'
    with socket.create_connection(('127.0.0.1', port), timeout) as sock:
        sock.sendall(payload)
        buf = bytearray()
        deadline = time.monotonic() + timeout
        while b'\n' not in buf:
            if time.monotonic() > deadline:
                raise TimeoutError('owner busy')
            part = sock.recv(65536)
            if not part:
                raise TelosError('owner closed the connection')
            buf.extend(part)
    reply = json.loads(bytes(buf).split(b'\n', 1)[0])
    if reply.get('id') != rid:
        raise TelosError('mismatched owner reply')
    if not reply.get('ok'):
        err = reply.get('error') or {}
        raise TelosError(err.get('message') if isinstance(err, dict) else str(err))
    return reply['result']


def emit(value):
    print(json.dumps(value, separators=(',', ':'), default=str), flush=True)


def add_commands(sub):
    for name, help in (('window', 'measure how many frames earlier or later a press still works'),
                       ('search', 'try a bounded set of extra presses and rank them by an objective')):
        p = sub.add_parser(name, help=help, description=help)
        acts = p.add_subparsers(dest='analysis_action', required=True)
        for action in ('start', 'status', 'cancel', 'list', 'export'):
            if name == 'search' and action == 'export':
                continue
            a = acts.add_parser(action)
            a.set_defaults(fn=command, analysis_kind=name)
            a.add_argument('--port', type=int, default=47610)
            if action == 'start':
                a.add_argument('--until', '-g', type=int, required=True, help='frame G where the result is read')
                a.add_argument('--no-cache', action='store_true')
                a.add_argument('--resume', help='job id to continue (same spec, same recorded inputs)')
                a.add_argument('--wait', action='store_true', help='print progress until the job ends')
                if name == 'window':
                    a.add_argument('frame', type=int, help='a frame inside the press')
                    a.add_argument('--shift', '-k', type=int, default=3, help='try -k..+k frames (default 3)')
                    a.add_argument('--hold', type=int, default=0, help='also hold -h..+h frames longer')
                    a.add_argument('--keys', nargs='*', help='only these held tokens form the press')
                    a.add_argument('--predicate', '-p', required=True, help='e.g. "sem.pc.area == orig" or same')
                    a.add_argument('--by', action='store_true', help='pass if it holds at any frame up to G')
                    a.add_argument('--export', type=Path, help='write a trainer trial file when done (needs --wait)')
                else:
                    a.add_argument('--start', type=int, required=True, help='first frame a press may start')
                    a.add_argument('--window', type=int, required=True, help='offsets 0..N-1 after --start')
                    a.add_argument('--keys', nargs='+', required=True, help='presses to try; "w e" is a chord')
                    a.add_argument('--durations', nargs='+', type=int, required=True)
                    a.add_argument('--max-attempts', type=int, required=True)
                    a.add_argument('--seed', type=int, default=0)
                    g = a.add_mutually_exclusive_group(required=True)
                    g.add_argument('--maximize', metavar='EXPR')
                    g.add_argument('--minimize', metavar='EXPR')
                    g.add_argument('--earliest', metavar='PRED')
                    a.add_argument('--require', metavar='PRED', help='a candidate counts only if this holds at G')
            elif action in ('status', 'cancel'):
                a.add_argument('job_id', nargs='?' if action == 'status' else None)
            elif action == 'export':
                a.add_argument('job_id')
                a.add_argument('out', type=Path, help='new .sequence.toml trial file')
                a.add_argument('--title')


def _spec(args):
    if args.analysis_kind == 'window':
        spec = {'frame': args.frame, 'shift': args.shift, 'hold': args.hold, 'until': args.until,
                'mode': 'by' if args.by else 'at', 'predicate': args.predicate}
        if args.keys:
            spec['keys'] = list(args.keys)
    else:
        goal = 'maximize' if args.maximize else 'minimize' if args.minimize else 'earliest'
        spec = {'start': args.start, 'window': args.window, 'until': args.until, 'keys': list(args.keys),
                'durations': list(args.durations), 'max_attempts': args.max_attempts, 'seed': args.seed,
                'goal': goal, 'objective': args.maximize or args.minimize or args.earliest}
        if args.require:
            spec['require'] = args.require
    if args.no_cache:
        spec['no_cache'] = True
    return spec


def _wait(port, job_id):
    last = None
    while True:
        try:
            job = request(port, 'analysis_status', job_id=job_id, full=False)
        except TimeoutError:
            time.sleep(0.5)
            continue
        line = (job['status'], job['phase'], job['done'])
        if line != last:
            print(f"{job['status']} {job['phase']}: {job['done']}/{job['total']} trials, {job['elapsed_s']} s",
                  file=sys.stderr, flush=True)
            last = line
        if job['status'] in TERMINAL:
            return request(port, 'analysis_status', job_id=job_id, timeout=30)
        time.sleep(0.5)


def command(_ctx, args):
    port, kind = args.port, args.analysis_kind
    action = args.analysis_action
    if action == 'list':
        emit(request(port, 'analysis_list'))
        return 0
    if action == 'status':
        emit(request(port, 'analysis_status', **({'job_id': args.job_id} if args.job_id else {}), timeout=30))
        return 0
    if action == 'cancel':
        emit(request(port, 'analysis_cancel', job_id=args.job_id))
        return 0
    if action == 'export':
        from .window import export_trial
        job = request(port, 'analysis_status', job_id=args.job_id, timeout=30)
        emit(export_trial(job, args.out, title=args.title))
        return 0
    args_ = {'kind': kind, 'spec': _spec(args)}
    if args.resume:
        args_['resume'] = args.resume
    job = request(port, 'analysis_start', timeout=120, **args_)
    if not args.wait:
        emit({k: job[k] for k in ('job_id', 'status', 'total', 'info', 'cached', 'dir')})
        return 0
    try:
        job = _wait(port, job['job_id'])
    except KeyboardInterrupt:
        print(f"Job {job['job_id']} continues in the owner. Cancel it with telos {kind} cancel {job['job_id']}",
              file=sys.stderr)
        return 130
    emit(job)
    if job.get('summary', {}) and job['summary'].get('text'):
        print(job['summary']['text'], file=sys.stderr)
    if kind == 'window' and getattr(args, 'export', None) and job['status'] == 'completed':
        from .window import export_trial
        emit(export_trial(job, args.export))
    return 0 if job['status'] == 'completed' else 1
