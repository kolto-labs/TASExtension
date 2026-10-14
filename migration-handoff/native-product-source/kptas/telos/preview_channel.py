"""Read-only presentation of committed gates, independent of command admission.

This channel never calls the backend or supplies an authoring/restore token.
"""
import base64
from collections import deque
import dataclasses
import json
import os
import select
import socket
import threading
import time

from kptas.types import NULL_DIGEST


class PreviewChannel:
    def __init__(self, session, host, port):
        self.session = session
        self.lock = threading.Lock()
        self.generation = 0
        self.candidate = self.packet = self.packet_bytes = self.packet_candidate = None
        self.metrics = dict(encoded=0, expired=0, advanced_during_encode=0, png_ns=0, png_max_ns=0, serialization_ns=0)
        # Committed per-tick inputs (authored, played or physical) for the input
        # trainer: (seq, session_id, take, epoch, tick, held tokens, buttons).
        self.inputs = deque(maxlen=4096)
        self.input_seq = 0
        self.input_stream = os.urandom(8).hex()
        self.stop = threading.Event()
        self.socket = socket.socket()
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.bind((host, port))
        self.socket.listen(8)
        self.socket.settimeout(.2)
        threading.Thread(target=self.encode, daemon=True, name='telos-preview-encode').start()
        threading.Thread(target=self.serve, daemon=True, name='telos-preview-read').start()

    def invalidate(self, *, inputs=True):
        with self.lock:
            self.generation += 1
            self.candidate = self.packet = self.packet_bytes = self.packet_candidate = None
            if inputs:
                self.inputs.clear()

    def _log_input(self, session_id, take, epoch, tick, note):
        held = tuple(str(token) for token in (getattr(note, 'held', ()) or ()))
        buttons = tuple(str(button) for button in (getattr(note, 'buttons', ()) or ()))
        with self.lock:
            log = self.inputs
            # A restore re-commits earlier ticks: drop the abandoned future.
            while log and log[-1][1] == session_id and log[-1][2] == take and log[-1][4] >= tick:
                log.pop()
            self.input_seq += 1
            log.append((self.input_seq, session_id, take, epoch, tick, held, buttons))

    def attach(self):
        if self.session.sup is not None:
            self.session.sup.presentation_report = self.publish

    def publish(self, sup, report, note):
        s = self.session
        backend = sup.backend
        sink = getattr(backend, 'frames', None)
        bundle = getattr(sup.sink, 'bundle', None)
        identity = sup is s.sup and not s.closed and not s.broken and bundle is not None
        if identity and type(report.tick) is int and note is not None:  # skip epoch-origin gates
            self._log_input(s.session_id, str(bundle.root), sup.epoch, report.tick, note)
        if (not identity or sink is None or getattr(sup.sink, 'last_tick', None) != report.tick):
            self.invalidate(inputs=not identity)
            return
        # sink.tick has returned: the complete checkpoint exists. Disk autosave
        # and action acknowledgement remain separate, unchanged contracts.
        clock = getattr(sup.sink, 'last_virtual_clock', None)
        virtual_us = (clock[1] if type(clock) is tuple and len(clock) == 2
                      and type(clock[0]) is int and clock[0] == report.tick
                      and type(clock[1]) is int and 0 <= clock[1] < 1 << 64 else None)
        # Capture cadence: an uncaptured gate's report has the null root. Say "not captured"
        # rather than present the null digest as a state hash. captured_tick and source
        # describe the frame's pixels, not the state capture, and stay as they are.
        captured = report.state_root != NULL_DIGEST
        descriptor = dict(session_id=s.session_id, take=str(bundle.root), tick=report.tick,
                          epoch=sup.epoch, offset=sup.offset,
                          inputs=dataclasses.asdict(note) if note is not None else {},
                          state_root=report.state_root.hex() if captured else None,
                          state_captured=captured, state='captured' if captured else 'not captured',
                          gate_tick=report.tick,
                          captured_tick=report.tick, source='captured_back_buffer',
                          virtual_us=virtual_us)
        with self.lock:
            self.candidate = (self.generation, sup, backend, sink,
                              getattr(backend, 'frames_epoch', None), descriptor)

    def valid(self, candidate):
        gen, sup, backend, sink, epoch, desc = candidate
        s = self.session
        return (not s.closed and not s.broken and gen == self.generation
                and sup is s.sup and backend is sup.backend
                and sink is getattr(backend, 'frames', None)
                and epoch == getattr(backend, 'frames_epoch', None)
                and sup.epoch == desc['epoch'] and sup.offset == desc['offset']
                and str(getattr(getattr(sup.sink, 'bundle', None), 'root', None)) == desc['take']
                and sink.consumer_health() == 'healthy')

    def encode(self):
        from kptas.frames import png_bytes
        encoded = None
        while not self.stop.wait(.004):
            with self.lock:
                candidate = self.candidate
            if candidate is None or candidate is encoded or not self.valid(candidate):
                continue
            gen, sup, backend, sink, epoch, desc = candidate
            frame = sink.presentation_frame(epoch, desc['tick'] - desc['offset'])
            if frame is None:
                continue
            _seq, meta, pixels = frame
            # The native slot supplies its local capture tick; the consumer
            # supplies the host epoch it owns. Require both before labeling pixels.
            if (type(meta.get('epoch')) is not int or meta['epoch'] != epoch
                    or type(meta.get('tick')) is not int
                    or meta['tick'] + desc['offset'] != desc['tick']):
                continue
            started = time.perf_counter_ns()
            try:
                png = png_bytes(pixels, meta['w'], meta['h'])
            except (ValueError, OSError):
                with self.lock:
                    if self.candidate is candidate:
                        self.packet = self.packet_bytes = self.packet_candidate = None
                encoded = candidate
                continue
            png_ns = time.perf_counter_ns() - started
            serialized = time.perf_counter_ns()
            packet = {**desc, 'generation': gen, 'png': base64.b64encode(png).decode('ascii')}
            wire = json.dumps(packet, separators=(',', ':')).encode() + b'\n'
            serialization_ns = time.perf_counter_ns() - serialized
            with self.lock:
                self.metrics['png_ns'] += png_ns
                self.metrics['png_max_ns'] = max(self.metrics['png_max_ns'], png_ns)
                self.metrics['serialization_ns'] += serialization_ns
                # A newer committed gate may arrive during encoding. The
                # completed image is still its real captured tick, and must not
                # be starved forever when encoding exceeds a gate interval.
                if self.valid(candidate):
                    self.metrics['encoded'] += 1
                    self.metrics['advanced_during_encode'] += self.candidate is not candidate
                    self.packet = packet
                    self.packet_bytes = wire
                    self.packet_candidate = candidate
                else:
                    self.metrics['expired'] += 1
                encoded = candidate

    def serve(self):
        while not self.stop.is_set():
            try:
                conn, _ = self.socket.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            try:
                with conn:
                    conn.settimeout(.5)
                    if self._serve_inputs(conn):
                        continue
                    with self.lock:
                        packet = (self.packet_bytes if self.candidate and self.packet_candidate
                                  and self.valid(self.candidate) and self.valid(self.packet_candidate) else None)
                    conn.sendall(packet if packet is not None else b'null\n')
            except OSError:
                pass

    def _serve_inputs(self, conn):
        """Answer {"inputs_since": seq}; preview readers send nothing and get the packet."""
        try:
            ready, _, _ = select.select([conn], [], [], .01)
            if not ready:
                return False
            raw = conn.recv(256).split(b'\n', 1)[0]
            request = json.loads(raw) if raw else None
        except (OSError, ValueError):
            return False
        since = request.get('inputs_since') if isinstance(request, dict) else None
        if type(since) is not int:
            return False
        with self.lock:
            log, seq = list(self.inputs), self.input_seq
        entries = [list(entry[:5]) + [list(entry[5]), list(entry[6])] for entry in log if entry[0] > since]
        reply = {'stream': self.input_stream, 'seq': seq, 'entries': entries}
        conn.sendall(json.dumps(reply, separators=(',', ':')).encode() + b'\n')
        return True

    def close(self):
        self.invalidate()
        self.stop.set()
        self.socket.close()


class PreviewReadCache:
    """One parsed wire packet; callers receive their own metadata containers."""
    def __init__(self):
        self.raw = self.packet = None

    def decode(self, raw):
        from copy import deepcopy
        if raw != self.raw:
            packet = json.loads(raw)
            self.raw, self.packet = raw, packet
        # PNG strings are immutable and shared; nested inputs/metadata are not.
        return deepcopy(self.packet)


def read_preview(host, port, *, cache=None):
    with socket.create_connection((host, port), .5) as conn:
        conn.settimeout(.5)
        with conn.makefile('rb') as stream:
            raw = stream.readline(8 * 1024 * 1024)
    return cache.decode(raw) if cache is not None else json.loads(raw)


def read_inputs(host, port, since):
    """Committed per-tick inputs after `since`; None from an owner without the log."""
    with socket.create_connection((host, port), .5) as conn:
        conn.settimeout(.5)
        conn.sendall(json.dumps({'inputs_since': since}).encode() + b'\n')
        with conn.makefile('rb') as stream:
            raw = stream.readline(8 * 1024 * 1024)
    packet = json.loads(raw) if raw else None
    if not isinstance(packet, dict) or not isinstance(packet.get('entries'), list) or 'stream' not in packet:
        return None
    return packet
