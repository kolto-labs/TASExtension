"""Read a lossless, explicitly historical frame for a selected holorecord tick."""
from pathlib import Path
from collections import OrderedDict
import hashlib
import subprocess
import threading

from kptas.frames import FPS, load_index, latest, ffmpeg_argv
from kptas.types import BackendError


_INDEX_CACHE = OrderedDict()
_INDEX_LOCK = threading.Lock()
_MAX_INDEXES = 8
_MAX_INDEX_ROWS = 100_000
_MAX_INDEX_BYTES = 32 * 1024 * 1024


def _signature(path):
    st = path.stat()
    return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns


def _indexed_frame(directory, tick, epoch=None):
    """Cache only unchanged indexes; bound retained rows and source bytes across archives."""
    path = (directory / 'index.jsonl').resolve()
    signature = _signature(path)
    cache_key = (path, epoch)
    with _INDEX_LOCK:
        cached = _INDEX_CACHE.get(cache_key)
        if cached is not None and cached[0] == signature:
            _INDEX_CACHE.move_to_end(cache_key)
            return cached[1].get(tick)
        _INDEX_CACHE.pop(cache_key, None)
    frames, offsets = load_index(directory)
    if epoch is not None:
        frames = [fr for fr in frames if type(fr.get("epoch")) is int and fr["epoch"] == epoch]
    by_tick = latest(frames, offsets)
    # Never retain a parse raced by append, truncate, replacement or an offset edit.
    if (_signature(path) == signature and len(by_tick) <= _MAX_INDEX_ROWS
            and signature[2] <= _MAX_INDEX_BYTES):
        with _INDEX_LOCK:
            _INDEX_CACHE[cache_key] = (signature, by_tick)
            _INDEX_CACHE.move_to_end(cache_key)
            while (len(_INDEX_CACHE) > _MAX_INDEXES
                   or sum(len(v[1]) for v in _INDEX_CACHE.values()) > _MAX_INDEX_ROWS
                   or sum(v[0][2] for v in _INDEX_CACHE.values()) > _MAX_INDEX_BYTES):
                _INDEX_CACHE.popitem(last=False)
    return by_tick.get(tick)


def _decode(path, frame):
    # _Mkv captures at FPS with -g 1: every FFV1 frame is a seek point. Matroska
    # rounds these timestamps to milliseconds; seek just before that timestamp
    # so accurate seeking cannot skip a rounded-down frame (e.g. frame 2).
    number = frame['frame']
    if type(number) is not int or number < 0:
        raise BackendError('recorded frame index must be a nonnegative integer')
    seek = []
    if path.suffix.lower() == '.mkv':
        milliseconds = (number * 1000 + FPS // 2) // FPS
        seek = ['-ss', f'{max(0, milliseconds * 1000 - 1) / 1_000_000:.6f}']
    elif number != 0:
        raise BackendError('single-image capture has a nonzero frame index')
    return subprocess.run(ffmpeg_argv('-v', 'error', *seek, '-i', str(path),
        '-vf', 'vflip', '-frames:v', '1', '-f', 'rawvideo', '-pix_fmt', 'bgra',
        'pipe:1'), capture_output=True, timeout=30)


def recorded_preview(bundle, tick, *, exact=False, epoch=None):
    """Return bottom-up BGRA + provenance, or None. Never step or alter the game."""
    source = bundle
    while source is not None:
        directory = Path(source.root) / 'frames'
        if (directory / 'index.jsonl').is_file():
            frame = _indexed_frame(directory, tick, epoch)
            if exact and frame is not None and ('repeat' in frame.get('flags', [])
                    or type(frame.get('reads')) is not int or frame['reads'] < 1):
                frame = None
            if frame is not None and 'blank' not in frame.get('flags', []):
                name = Path(frame['file'])
                if name.is_absolute() or '..' in name.parts:
                    raise BackendError('recorded frame path escapes capture directory')
                path = directory / name
                w, h = frame['w'], frame['h']
                result = _decode(path, frame)
                if result.returncode or len(result.stdout) != w*h*4:
                    raise BackendError('recorded frame could not be decoded at its indexed position')
                px = result.stdout
                expected = frame.get('digest')
                # FFV1 bgr0 stores RGB losslessly but not the original unused alpha byte.
                # Accept only a reconstruction matching the indexed raw-byte digest.
                for alpha in (None, 0, 255):
                    candidate = bytearray(px)
                    if alpha is not None:
                        candidate[3::4] = bytes([alpha]) * (w*h)
                    digest = hashlib.blake2b(candidate, digest_size=16).hexdigest()
                    if digest == expected or (alpha == 0 and digest == frame.get('bgr0_digest')):
                        # Alpha is not archived by bgr0; make the verified RGB opaque for display.
                        candidate[3::4] = b'\xff' * (w*h)
                        return bytes(candidate), w, h, {
                            'source': 'recorded_preview', 'archive_tick': tick,
                            'captured_tick': None if 'repeat' in frame.get('flags', []) else tick,
                            'gate_tick': tick, 'blank': False,
                            'repeat': 'repeat' in frame.get('flags', []),
                            'cropped': 'cropped' in frame.get('flags', []),
                            'archive': str(path), 'archive_frame': frame['frame'],
                            'archive_digest_verified': True, 'archive_epoch': frame.get('epoch'),
                            'presentation_phase': 'recorded_pre_swap',
                            'format': 'bgra-bottom-up',
                            'sha256': hashlib.sha256(candidate).hexdigest()}
                if frame.get('bgr0_digest'):
                    raise BackendError('recorded frame bytes do not match the capture index')
                # Older FFV1 archives discarded alpha without an RGB-only hash.
                # Do not present an unverified historical image as an exact selected frame.
                return None
        parent = source.parent
        # Never show a parent's frame beyond the child's fork point.
        if parent is None or tick > source.header.parent.tick:
            break
        source = parent
    return None
