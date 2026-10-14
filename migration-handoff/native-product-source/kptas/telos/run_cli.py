"""Continuous-run client; the existing TAS session remains the sole game owner."""
from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

from kptas.supervisor import TelosError
from .workspace_session import LiveTransport, WorkspaceError, RemoteRefusal

TERMINAL = frozenset({'completed', 'cancelled', 'failed'})


def add_commands(sub):
    drive = sub.add_parser('drive', help='submit a continuous score to the running TAS')
    actions = drive.add_subparsers(dest='drive_action', required=True)
    for name in ('start', 'status', 'cancel', 'observe', 'capture'):
        p = actions.add_parser(name)
        p.set_defaults(fn=command)
        p.add_argument('--port', type=int, default=47610)
        if name == 'start':
            p.add_argument('score', type=Path)
            p.add_argument('--receipt', type=Path, required=True,
                           help='new local file preserving admission and run identity')
            p.add_argument('--request-id', default=None)
            p.add_argument('--wait', action='store_true', help='print progress until terminal; execution is independent')
            p.add_argument('--focus-display', default=None)
        elif name == 'status':
            p.add_argument('--run-id')
            p.add_argument('--request-id', help='recover the identity after a lost start acknowledgement')
        elif name == 'cancel':
            p.add_argument('run_id')
            p.add_argument('--session-id', required=True)
        elif name == 'observe':
            p.add_argument('--seconds', type=float, default=0, help='0 runs until interrupted')
            p.add_argument('--interval', type=float, default=.25)
        elif name == 'capture':
            p.add_argument('out', type=Path, help='new .mkv file; one continuous recording')
            p.add_argument('--display', default=':176')
            p.add_argument('--size', default='1024x768')
            p.add_argument('--fps', type=int, default=60)


def emit(value):
    print(json.dumps(value, separators=(',', ':'), allow_nan=False), flush=True)


def store(path, value, *, new=False):
    """Keep recovery identity even when a mutation response never arrives."""
    from kptas.durability import atomic_json
    if new:
        with path.open('x', encoding='utf-8') as stream:
            json.dump(value, stream)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        from kptas.platform_sync import sync_dir
        sync_dir(path.parent)
    else:
        atomic_json(path, value)


def command(_ctx, args):
    if not 1 <= args.port < 65535:
        raise TelosError('port must be 1..65534')
    try:
        return execute(args)
    except WorkspaceError as exc:
        raise TelosError(str(exc)) from exc


def execute(args):
    action = args.drive_action
    if action == 'capture':
        return capture(args)
    if action == 'observe':
        return observe(args)
    transport = LiveTransport(port=args.port, timeout=10)
    if action == 'status':
        values = {k: getattr(args, k) for k in ('run_id', 'request_id') if getattr(args, k)}
        emit(transport.request('run_status', **values))
        return 0
    if action == 'cancel':
        emit(transport.request('run_cancel', run_id=args.run_id, session_id=args.session_id))
        return 0

    # Bounded text only: never upload private game assets or resolve includes.
    with args.score.open('rb') as stream:
        raw = stream.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise TelosError('live score exceeds 1 MiB')
    try:
        score = raw.decode('utf-8')
    except UnicodeError as exc:
        raise TelosError('score must be UTF-8') from exc
    state = transport.request('takes')
    if 'authored_run_queue' not in state.get('capabilities', ()):
        raise TelosError('this owner predates continuous runs; save it and activate the updated owner first')
    expected = state.get('owner')
    if not isinstance(expected, dict) or not expected.get('session_id'):
        raise TelosError('owner did not supply an admission token')
    request_id = args.request_id or uuid.uuid4().hex
    receipt = {'schema': 'kptas-run-client/1', 'status': 'submitting',
               'request_id': request_id, 'session_id': expected['session_id'],
               'port': args.port, 'score': str(args.score.resolve()),
               'score_sha256': hashlib.sha256(raw).hexdigest(), 'expected': expected}
    store(args.receipt, receipt, new=True)
    values = {'score': score, 'expected': expected, 'request_id': request_id}
    if args.focus_display:
        values['ui_focus_display'] = args.focus_display
    try:
        run = transport.request('run_start', **values)
    except RemoteRefusal as exc:
        receipt.update(status='rejected', error=str(exc))
        store(args.receipt, receipt)
        raise
    except WorkspaceError as exc:
        receipt.update(status='unacknowledged', error=str(exc))
        store(args.receipt, receipt)
        print(f'Admission was not acknowledged. Inspect request {request_id} with '
              f'telos drive status --port {args.port} --request-id {request_id}; do not submit a new run.',
              file=sys.stderr, flush=True)
        raise
    receipt.update(status='acknowledged', run=run)
    store(args.receipt, receipt)
    emit(run)
    if not args.wait:
        return 0
    run_id = run['run_id']
    try:
        while run['status'] not in TERMINAL:
            time.sleep(1)
            run = transport.request('run_status', run_id=run_id)
            receipt['run'] = run
            store(args.receipt, receipt)
            emit(run)
    except KeyboardInterrupt:
        # Detach the observer; never turn an interrupted terminal into a game pause.
        print(f'Run {run_id} continues. Cancel explicitly with telos drive cancel '
              f'{run_id} --session-id {expected["session_id"]} --port {args.port}', file=sys.stderr)
        return 130
    return 0 if run['status'] == 'completed' else 1


def observe(args):
    from .preview_channel import read_preview
    if not .05 <= args.interval <= 60 or not 0 <= args.seconds <= 86400:
        raise TelosError('observation interval must be .05..60 seconds and duration 0..86400')
    started = time.monotonic()
    last = None
    while not args.seconds or time.monotonic() - started < args.seconds:
        packet = read_preview('127.0.0.1', args.port + 1)
        if packet:
            identity = (packet.get('session_id'), packet.get('take'), packet.get('generation'), packet.get('tick'))
            if identity != last:
                emit({k: v for k, v in packet.items() if k != 'png'})
                last = identity
        time.sleep(args.interval)
    return 0


def capture(args):
    import re
    if args.out.suffix.lower() != '.mkv':
        raise TelosError('use .mkv for crash-tolerant continuous capture; remux to MP4 afterward')
    if not re.fullmatch(r':\d+(?:\.\d+)?', args.display) or not re.fullmatch(r'\d+x\d+', args.size):
        raise TelosError('capture requires an X11 display and WIDTHxHEIGHT')
    if not 1 <= args.fps <= 240:
        raise TelosError('capture fps must be 1..240')
    # Input timestamps retain wall time. No setpts, deduplication, or speed changes.
    command = ['ffmpeg', '-hide_banner', '-nostdin', '-n', '-f', 'x11grab',
               '-framerate', str(args.fps), '-video_size', args.size, '-i', args.display,
               '-an', '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '18',
               '-pix_fmt', 'yuv420p', '-fps_mode', 'passthrough', str(args.out)]
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, start_new_session=True)
    try:
        return process.wait()
    except KeyboardInterrupt:
        import signal
        process.send_signal(signal.SIGINT)
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                print(f'Recorder PID {process.pid} has not finalized {args.out}; it was not killed.', file=sys.stderr)
        return 130
