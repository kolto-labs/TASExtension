"""Authenticated loopback commands for one existing controller.

HTTP threads only enqueue requests and read cached receipts. The game owner
executes commands through its normal UI dispatch, never on a server thread.
"""
from collections import OrderedDict
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from queue import Empty, Full, Queue
import secrets
import threading


class WorkspaceProtocol:
    def __init__(self, port=0):
        self.token = secrets.token_urlsafe(32)
        self.commands = Queue(maxsize=32)
        self.lock = threading.Lock()
        self.receipts = OrderedDict()
        self.snapshot = {"state": "opening"}
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def reply(self, status, value):
                payload = json.dumps(value).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(payload)))
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                self.wfile.write(payload)

            def authorized(self):
                self.connection.settimeout(2)
                if self.headers.get('Origin') or not secrets.compare_digest(
                        self.headers.get('Authorization', ''), 'Bearer ' + owner.token):
                    self.reply(403, {"error": "local bearer token required"})
                    return False
                return True

            def do_GET(self):
                if not self.authorized():
                    return
                with owner.lock:
                    if self.path == '/status':
                        result = deepcopy(owner.snapshot)
                    elif self.path.startswith('/commands/'):
                        result = deepcopy(owner.receipts.get(self.path.rsplit('/', 1)[-1]))
                    else:
                        result = None
                self.reply(200 if result is not None else 404, result or {"error": "not found"})

            def do_POST(self):
                if not self.authorized():
                    return
                if self.path != '/commands':
                    self.reply(404, {"error": "not found"})
                    return
                try:
                    length = int(self.headers.get('Content-Length', '0'))
                    if not 0 < length <= 8192:
                        raise ValueError('command body must be 1..8192 bytes')
                    request = json.loads(self.rfile.read(length))
                    action, values = validate(request)
                    command_id = secrets.token_hex(8)
                    with owner.lock:
                        owner.commands.put_nowait((command_id, action, values))
                        owner.receipts[command_id] = {"id": command_id, "state": "queued"}
                    self.reply(202, {"id": command_id, "state": "queued"})
                except Full:
                    self.reply(429, {"error": "command queue full; retry later"})
                except (ValueError, TypeError, UnicodeError) as exc:
                    self.reply(400, {"error": str(exc)})

        self.server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={'poll_interval': .05}, daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'

    def poll(self):
        try:
            item = self.commands.get_nowait()
        except Empty:
            return None, '', {}
        with self.lock:
            self.receipts[item[0]]['state'] = 'running'
        return item

    def update(self, snapshot):
        with self.lock:
            self.snapshot = deepcopy(snapshot)

    def finish(self, command_id, error=None):
        if command_id is None:
            return
        with self.lock:
            self.receipts[command_id] = {"id": command_id, "state": "failed" if error else "done",
                                         "error": error, "status": deepcopy(self.snapshot)}
            # Keep pending receipts even when many completed requests are retained.
            completed = [key for key, value in self.receipts.items() if value['state'] in ('done', 'failed')]
            for key in completed[:-128]:
                del self.receipts[key]

    def close(self):
        with self.lock:
            for receipt in self.receipts.values():
                if receipt['state'] in ('queued', 'running'):
                    receipt.update(state='failed', error='controller closed before execution')
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1)


def validate(request):
    if not isinstance(request, dict):
        raise ValueError('command must be an object')
    action = request.get('action')
    allowed = {
        'play_pause': {}, 'step': {}, 'rewind': {}, 'rewind_mark': {}, 'workspace': {}, 'stop': {},
        'seek': {'tick': int}, 'load_bundle': {'path': str}, 'save': {'path': str},
        'branch': {'out': str, 'score': str}, 'load_score': {'path': str},
    }
    if not isinstance(action, str) or action not in allowed:
        raise ValueError('unknown action')
    schema = allowed[action]
    if set(request) != {'action', *schema} and not (action == 'save' and set(request) == {'action'}):
        raise ValueError('command fields must be: ' + ', '.join(['action', *schema]))
    values = {key: request[key] for key in schema if key in request}
    for key, kind in schema.items():
        if key not in values:
            continue
        if type(values[key]) is not kind or kind is str and not values[key] or key == 'tick' and values[key] < 0:
            raise ValueError('invalid ' + key)
    return action, values
