"""Frame capture: the driver's side of the frame ring (contract section 9), and labelled videos.

The shim reads the back buffer at every present and, at gate n, writes tick n's
frame into a ring file that the driver creates in the session directory.
FrameSink drains the ring on its own thread and keeps every frame:

- ``mkv``: lossless FFV1 in Matroska, one file per epoch and frame size;
- ``png``: one PNG per capture. Repeated ticks retain separate occurrence files.

Either way ``index.jsonl`` gets one line per frame. ``video`` turns a capture into an H.264 file with each
frame's tick drawn on it.
"""

from __future__ import annotations

import hashlib
import json
import mmap
import os
import shutil
import struct
import subprocess
import tempfile
import threading
import time

from kptas.config import unit_name
import zlib
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from kptas.types import BackendError

MAGIC = b"KPFR"
VERSION = 1
HEADER = 4096
SLOT_HEADER = 4096
_HDR = struct.Struct("<4sIII")          # magic, version, slots, slot_bytes
_Q = struct.Struct("<Q")
_I = struct.Struct("<I")
OFF_WRITTEN, OFF_READ, OFF_PPID, OFF_STATE = 16, 24, 32, 36
_SLOT = struct.Struct("<QQIIIIIIIII")   # seq, tick, w, h, format, swaps, reads, flags, read_us, skipped, finish_us
REPEAT, BLANK, CROPPED = 1, 2, 4
FLAG_NAMES = ((REPEAT, "repeat"), (BLANK, "blank"), (CROPPED, "cropped"))
INDEX = "index.jsonl"
FPS = 60


def slot_bytes_for(width: int, height: int) -> int:
    """Pixel capacity of one slot for a BGRA frame, rounded up to a page."""
    return (width * height * 4 + 4095) // 4096 * 4096


def render_size(game_dir: str | os.PathLike[str] | None, ini: str = "swkotor.ini") -> tuple[int, int]:
    """Width and Height from the game's ini (the render size), else the 1024x768 virtual display."""
    w = h = 0
    if game_dir:
        try:
            for line in (Path(os.path.expanduser(game_dir)) / ini).read_text(errors="replace").splitlines():
                k, _, v = line.partition("=")
                k = k.strip().lower()
                if k in ("width", "height") and v.strip().isdigit():
                    if k == "width" and not w:
                        w = int(v)
                    elif k == "height" and not h:
                        h = int(v)
        except OSError:
            pass
    return (w, h) if w and h else (1024, 768)


class FrameRing:
    """The ring file: a 4096-byte header, then `slots` slots of a 4096-byte header page plus `slot_bytes`."""

    def __init__(self, path: Path, fd: int, mm: mmap.mmap, slots: int, slot_bytes: int):
        self.path, self._fd, self._mm = path, fd, mm
        self.slots, self.slot_bytes = slots, slot_bytes

    @classmethod
    def create(cls, path: str | os.PathLike[str], slots: int, slot_bytes: int) -> FrameRing:
        if slots < 1 or slot_bytes <= 0 or slot_bytes % 4096:
            raise BackendError("a frame ring needs slots >= 1 and slot_bytes a multiple of 4096")
        size = HEADER + slots * (SLOT_HEADER + slot_bytes)
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.ftruncate(fd, size)
            mm = mmap.mmap(fd, size)
        except BaseException:
            os.close(fd)
            raise
        mm[:_HDR.size] = _HDR.pack(MAGIC, VERSION, slots, slot_bytes)
        _I.pack_into(mm, OFF_STATE, 1)
        return cls(Path(path), fd, mm, slots, slot_bytes)

    @property
    def written(self) -> int:
        return _Q.unpack_from(self._mm, OFF_WRITTEN)[0]

    @property
    def read(self) -> int:
        return _Q.unpack_from(self._mm, OFF_READ)[0]

    def take(self, seq: int) -> tuple[dict[str, Any], bytes]:
        """Slot header and pixels of frame `seq` (still unconsumed). Call consumed() after."""
        off = HEADER + (seq % self.slots) * (SLOT_HEADER + self.slot_bytes)
        s, tick, w, h, fmt, swaps, reads, flags, read_us, skipped, finish_us = _SLOT.unpack_from(self._mm, off)
        if s != seq:
            raise BackendError(f"frame ring slot holds frame {s}, expected {seq}")
        n = w * h * 4
        if fmt != 1 or n > self.slot_bytes:
            raise BackendError(f"frame {seq}: format {fmt}, {w}x{h} does not fit the ring")
        px = self._mm[off + SLOT_HEADER:off + SLOT_HEADER + n]
        return {"tick": tick, "w": w, "h": h, "swaps": swaps, "reads": reads, "flags": flags,
                "read_us": read_us, "finish_us": finish_us, "skipped": skipped}, px

    def consumed(self, n: int) -> None:
        _Q.pack_into(self._mm, OFF_READ, n)

    def consumer_failed(self) -> None:
        """Signal consumer failure without advancing read or claiming storage."""
        _I.pack_into(self._mm, OFF_STATE, 3)

    @property
    def closed(self) -> bool:
        return self._mm.closed

    def close(self, unlink: bool = True) -> None:
        if self._mm.closed:
            if self._fd is not None:
                fd, self._fd = self._fd, None
                os.close(fd)
            return  # Never unlink a newer ring that reused this pathname.
        try:
            if _I.unpack_from(self._mm, OFF_STATE)[0] != 3:
                _I.pack_into(self._mm, OFF_STATE, 2)
            self._mm.close()
        finally:
            if self._fd is not None:
                fd, self._fd = self._fd, None
                os.close(fd)
            if unlink:
                self.path.unlink(missing_ok=True)


# ---------------------------------------------------------------- stores

def png_bytes(bgra: bytes, w: int, h: int, level: int = 1) -> bytes:
    """An RGB PNG from bottom-up BGRA rows."""
    rgb = bytearray(w * h * 3)
    rgb[0::3], rgb[1::3], rgb[2::3] = bgra[2::4], bgra[1::4], bgra[0::4]
    stride = w * 3
    raw = b"".join(b"\x00" + rgb[(h - 1 - y) * stride:(h - y) * stride] for y in range(h))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, level)) + chunk(b"IEND", b""))


def ffmpeg_argv(*args: str) -> list[str]:
    """ffmpeg here when it is on PATH, else the host's through host-spawn."""
    if shutil.which("ffmpeg") or not shutil.which("host-spawn"):
        return ["ffmpeg", *args]
    return ["host-spawn", "-no-pty", "ffmpeg", *args]


class _Mkv:
    """One FFV1 file per epoch and size, fed raw frames on stdin."""

    def __init__(self, out: Path, epoch: int):
        self.out, self.epoch = out, epoch
        self.proc: subprocess.Popen[bytes] | None = None
        self.size = (0, 0)
        self.name = ""
        self.n = 0
        self.part = 0
        self.files: list[str] = []

    def _open(self, w: int, h: int) -> None:
        self.close()
        while True:
            self.name = f"e{self.epoch}-{self.part}.mkv" if self.part else f"e{self.epoch}.mkv"
            self.part += 1
            outputs = (self.out / self.name, self.out / f"{self.name}.log")
            if not any(path.exists() or path.is_symlink() for path in outputs):
                break
        argv = ffmpeg_argv("-hide_banner", "-loglevel", "error", "-n", "-f", "rawvideo", "-pix_fmt", "bgr0",
                           "-s", f"{w}x{h}", "-framerate", str(FPS), "-i", "pipe:0", "-vf", "vflip",
                           "-c:v", "ffv1", "-level", "3", "-g", "1", "-slices", "4", "-slicecrc", "1",
                           str(self.out / self.name))
        self.err = open(self.out / f"{self.name}.log", "xb")
        try:
            self.proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=self.err)
        except BaseException:
            self.err.close()
            raise
        self.size, self.n = (w, h), 0
        self.files.append(self.name)

    def put(self, meta: dict[str, Any], px: bytes) -> dict[str, Any]:
        if (meta["w"], meta["h"]) != self.size:
            self._open(meta["w"], meta["h"])
        assert self.proc is not None and self.proc.stdin is not None
        self.proc.stdin.write(px)
        self.n += 1
        return {"file": self.name, "frame": self.n - 1}

    def close(self, timeout_s: float = 30.0) -> None:
        if self.proc is None:
            return
        if not isinstance(timeout_s, (int, float)) or not 0 < timeout_s <= 300:
            raise BackendError('encoder finalization timeout must be between 0 and 300 seconds')
        proc, self.proc = self.proc, None
        assert proc.stdin is not None
        expired = threading.Event()
        def stop_encoder():
            if proc.poll() is None:
                expired.set()
                try:
                    proc.kill()
                except OSError:
                    pass
        deadline = threading.Timer(timeout_s, stop_encoder)
        deadline.daemon = True
        deadline.start()
        failure = None
        try:
            try:
                proc.stdin.close()
            except OSError as error:
                failure = error
            try:
                rc = proc.wait(timeout=timeout_s)
            except subprocess.TimeoutExpired as error:
                failure = failure or error
                stop_encoder()
                try:
                    rc = proc.wait(timeout=5.0)
                except subprocess.TimeoutExpired:
                    raise BackendError(f'encoder did not exit after finalization timeout: {self.out / self.name}') from error
        finally:
            deadline.cancel()
            self.err.close()
        if expired.is_set() or failure is not None:
            raise BackendError(f'encoder finalization incomplete: {self.out / self.name}') from failure
        log = self.out / f"{self.name}.log"
        if log.stat().st_size == 0:
            log.unlink()
        if rc != 0:
            raise BackendError(f"ffmpeg ended with status {rc} writing {self.out / self.name}")


class _Png:
    def __init__(self, out: Path, epoch: int):
        self.out, self.epoch = out, epoch
        self.files: list[str] = []

    def put(self, meta: dict[str, Any], px: bytes) -> dict[str, Any]:
        stem = f"e{self.epoch}-t{meta['tick']:06d}"
        fd, temporary = tempfile.mkstemp(prefix=f".{stem}-", suffix=".tmp", dir=self.out)
        tmp = Path(temporary)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(png_bytes(px, meta["w"], meta["h"]))
        occurrence = 0
        while True:
            name = f"{stem}{f'-c{occurrence}' if occurrence else ''}.png"
            try:
                # Publish complete bytes atomically without replacing an older
                # capture. Failed publication retains the private temp file.
                os.link(tmp, self.out / name)
                break
            except FileExistsError:
                occurrence += 1
        tmp.unlink()  # The successfully published file retains these bytes.
        if not self.files or self.files[-1] != name:
            self.files.append(name)
        return {"file": name, "frame": 0}

    def close(self) -> None:
        pass




class FrameSink:
    """Drains a FrameRing on a thread into a directory (mkv or png) and its index.jsonl.

    ``retarget(dir)`` sends every frame published after the call to another directory; the shim is held at a
    gate then, so the split falls exactly between two ticks. ``note(**fields)`` adds an index line in frame
    order. Epoch 0 creates a new index exclusively; later epochs and returns to
    an already owned directory append without discarding earlier records.
    """

    def __init__(self, ring: FrameRing, out_dir: Path, epoch: int, kind: str = "mkv"):
        if kind not in ("mkv", "png"):
            raise BackendError(f"capture.frames_sink must be mkv or png, not {kind!r}")
        self.ring, self.epoch, self.kind = ring, epoch, kind
        self._dirs: list[tuple[int, Path]] = [(ring.read, Path(out_dir))]
        self._notes: list[tuple[int, dict[str, Any]]] = []
        self._sticky: dict[str, Any] = {}      # repeated at the top of every index this sink starts
        self._precision: str | None = None     # from a precision_effective note; wording only
        self._store: Any = None
        self._index: Any = None
        self._dir: Path | None = None
        self._started_dirs: set[Path] = set()
        self._seq = ring.read
        self._latest_lock = threading.Lock()
        self._latest = None
        self._presentation_frames = {}
        self._latest_floor = ring.read
        self._stop = threading.Event()
        self._error: BaseException | None = None
        self._closed = False
        self._close_summary: str | None = None
        self.frames = 0
        self.ticks: list[int] = []
        self.flags = {name: 0 for _bit, name in FLAG_NAMES}
        self.thread = threading.Thread(target=self._run, name="kptas-frames", daemon=True)
        self.thread.start()

    def consumer_health(self) -> str:
        """Passive draining-thread evidence; never repair or wait for the sink."""
        with self._latest_lock:
            if self._error is not None or self._stop.is_set() or not self.thread.is_alive():
                return "dead"
            return "healthy"

    def invalidate_preview(self) -> None:
        with self._latest_lock:
            self._latest = None
            self._presentation_frames.clear()
            self._latest_floor = self.ring.written

    def latest_frame(self, epoch: int, tick: int):
        """Nonblocking immutable fresh capture only; no persistence/readiness claim."""
        with self._latest_lock:
            if self._stop.is_set() or self._error is not None or self._latest is None:
                return None
            seq, meta, px = self._latest
            if (epoch != self.epoch or seq < self._latest_floor or seq != self.ring.written - 1
                    or meta['tick'] != tick or meta['flags'] & (REPEAT | BLANK)
                    or not meta['reads'] or not meta['w'] or not meta['h']):
                return None
            return seq, dict(meta), px

    def presentation_frame(self, epoch: int, tick: int):
        """Immutable recent capture; presentation never claims an owner ACK."""
        with self._latest_lock:
            if epoch != self.epoch or self._stop.is_set() or self._error is not None:
                return None
            frame = self._presentation_frames.get(tick)
            if frame is None:
                return None
            seq, meta, pixels = frame
            if (seq < self._latest_floor or meta['flags'] & (REPEAT | BLANK)
                    or not meta['reads'] or not meta['w'] or not meta['h']):
                return None
            # Epoch belongs to this consumer/ring instance, not an inferred
            # field in the native slot. Keep it explicit for presentation joins.
            return seq, {**meta, 'epoch': self.epoch}, pixels

    def retarget(self, out_dir: Path) -> None:
        self.invalidate_preview()
        self._dirs.append((self.ring.written, Path(out_dir)))

    def note(self, sticky: bool = False, **fields: Any) -> None:
        """An index line after the frames published so far. A sticky one (the epoch's tick offset) also
        heads every index this sink starts later."""
        if sticky:
            self._sticky.update(fields)
        pe = fields.get("precision_effective")
        if isinstance(pe, Mapping):
            self._precision = pe.get("precision")
        self._notes.append((self.ring.written, fields))

    def _target(self, seq: int) -> Path:
        return [d for start, d in self._dirs if seq >= start][-1]

    def _switch(self, d: Path) -> None:
        if self._dir == d:
            return
        self._close_store()
        d.mkdir(parents=True, exist_ok=True)
        self._dir = d
        identity = d.resolve()
        self._index = open(d / INDEX, "x" if self.epoch == 0 and identity not in self._started_dirs else "a", encoding="utf-8")
        self._started_dirs.add(identity)
        self._store = (_Mkv if self.kind == "mkv" else _Png)(d, self.epoch)
        self._line({"format": "kptas-frames/1", "epoch": self.epoch, "sink": self.kind, "fps": FPS})
        if self._sticky:
            self._line({"epoch": self.epoch, **self._sticky})

    def _line(self, obj: Mapping[str, Any]) -> None:
        self._index.write(json.dumps(obj, separators=(",", ":")) + "\n")

    def _flush_notes(self, upto: int) -> None:
        while self._notes and self._notes[0][0] <= upto:
            _at, fields = self._notes.pop(0)
            self._switch(self._target(self._seq))
            self._line({"epoch": self.epoch, **fields})

    def _one(self) -> None:
        seq = self._seq
        self._flush_notes(seq)
        self._switch(self._target(seq))
        meta, px = self.ring.take(seq)
        with self._latest_lock:
            if seq >= self._latest_floor:
                self._latest = (seq, dict(meta), px)
                # Presentation uses captured immutable bytes, not completion of
                # the independent FFV1/index writer. Keep that writer's order
                # and failures unchanged; this cache never claims archive durability.
                self._presentation_frames[meta['tick']] = (seq, dict(meta), px)
                while len(self._presentation_frames) > 4:
                    del self._presentation_frames[next(iter(self._presentation_frames))]
        self.ring.consumed(seq + 1)
        where = self._store.put(meta, px)
        flags = [name for bit, name in FLAG_NAMES if meta["flags"] & bit]
        for f in flags:
            self.flags[f] += 1
        rgb = bytearray(px)
        rgb[3::4] = bytes(len(rgb) // 4)
        self._line({"seq": seq, "epoch": self.epoch, "tick": meta["tick"], "w": meta["w"], "h": meta["h"],
                    "swaps": meta["swaps"], "reads": meta["reads"], "finish_us": meta["finish_us"],
                    "read_us": meta["read_us"], "flags": flags,
                    "digest": hashlib.blake2b(px, digest_size=16).hexdigest(),
                    "bgr0_digest": hashlib.blake2b(rgb, digest_size=16).hexdigest(), **where})
        self._seq = seq + 1
        self.frames += 1
        self.ticks.append(meta["tick"])

    def _run(self) -> None:
        try:
            while True:
                if self._seq < self.ring.written:
                    self._one()
                elif self._stop.is_set():
                    break
                else:
                    time.sleep(0.002)
            self._flush_notes(1 << 62)
        except BaseException as e:      # close() reports the original failure
            with self._latest_lock:
                self._error = self._error or e
                try:
                    self.ring.consumer_failed()
                except BaseException:
                    pass  # Signaling failure cannot replace the captured cause.
        finally:
            try:
                self._close_store()
            except BaseException as error:
                with self._latest_lock:
                    self._error = self._error or error
                    try:
                        self.ring.consumer_failed()
                    except BaseException:
                        pass

    def drain(self, timeout_s: float = 300.0) -> None:
        """Wait for published frames to reach the writer; close finalizes encoders."""
        with self._latest_lock:
            failure = self._error
        if failure is not None:
            raise BackendError(f'frames: capture writer failed: {failure}') from failure
        if self._closed:
            return
        if self.ring.closed:
            raise BackendError('frames: frame ring closed before drain completion')
        deadline = time.monotonic() + timeout_s
        while self._seq < self.ring.written and self._error is None and self.thread.is_alive():
            if time.monotonic() > deadline:
                raise BackendError(f"frames: {self.ring.written - self._seq} frames still unstored after "
                                   f"{timeout_s} s")
            time.sleep(0.005)
        with self._latest_lock:
            failure = self._error
        if failure is not None:
            raise BackendError(f"frames: capture writer failed: {failure}") from failure
        if self.ring.closed:
            raise BackendError('frames: frame ring closed before drain completion')
        remaining = self.ring.written - self._seq
        if remaining > 0:
            raise BackendError(f"frames: capture writer stopped with {remaining} frames unstored")

    def _close_store(self) -> None:
        try:
            if self._store is not None:
                self._store.close()
        finally:
            self._store = None
            if self._index is not None:
                self._index.close()
                self._index = None

    def close(self, timeout_s: float = 300.0) -> str:
        """Store what is left, finish the files, and return a one-line summary."""
        if not isinstance(timeout_s, (int, float)) or not 0 < timeout_s <= 300:
            raise BackendError('capture shutdown timeout must be between 0 and 300 seconds')
        if self._closed:
            if self._error is not None:
                raise BackendError(f'frames: {self._error}') from self._error
            return self._close_summary
        with self._latest_lock:
            self._stop.set()
            self._latest = None
        self.thread.join(timeout_s)
        if self.thread.is_alive():
            with self._latest_lock:
                self._error = self._error or TimeoutError('capture writer did not stop; recording is incomplete')
                failure = self._error
            try:
                self.ring.consumer_failed()
            except BaseException:
                pass
            # The writer may still own the store/index and read its mapping.
            # Retain those resources for a later close; never unmap under it.
            raise BackendError(f'frames: {failure}') from failure
        if self.ring.closed:
            with self._latest_lock:
                self._error = self._error or BackendError('frame ring closed before sink finalization was acknowledged')
        remaining = 0 if self.ring.closed else self.ring.written - self._seq
        if remaining > 0:
            with self._latest_lock:
                self._error = self._error or BackendError(f'{remaining} published frames remained unstored at shutdown')
        try:
            self.ring.close()
        except BaseException as error:
            with self._latest_lock:
                self._error = self._error or error
            raise BackendError(f'frames: {self._error}') from self._error
        if self._error is not None:
            self._closed = True
            raise BackendError(f"frames: {self._error}") from self._error
        units = unit_name(self._precision, plural=True)
        span = f"{units} {min(self.ticks)}..{max(self.ticks)}" if self.ticks else f"no {units}"
        extra = "".join(f", {n} {k}" for k, n in self.flags.items() if n)
        dirs = ", ".join(dict.fromkeys(str(d) for _s, d in self._dirs))
        self._close_summary = f"frames: {self.frames} ({span}{extra}) in {dirs}"
        self._closed = True
        return self._close_summary


class FrameViewSink(FrameSink):
    """Drain native pixels for current viewing only, without archival output.

    Uses the same immutable, provenance-checked latest-frame adapter. No frame
    files/index or notes are retained or acknowledged durable. Only four recent
    immutable captures are held for presentation.
    """
    def __init__(self, ring: FrameRing, epoch: int):
        super().__init__(ring, Path('.'), epoch)

    def _one(self) -> None:
        seq = self._seq
        meta, px = self.ring.take(seq)
        with self._latest_lock:
            if seq >= self._latest_floor:
                self._latest = (seq, dict(meta), px)
                self._presentation_frames[meta['tick']] = self._latest
                while len(self._presentation_frames) > 4:
                    del self._presentation_frames[next(iter(self._presentation_frames))]
        self.ring.consumed(seq + 1)
        self._seq = seq + 1
        self.frames += 1

    def note(self, sticky: bool = False, **fields: Any) -> None:
        pass  # There is deliberately no archival index or queued note history.

    def retarget(self, out_dir: Path) -> None:
        self.invalidate_preview()

    def close(self) -> str:
        super().close()
        return f"frame preview: {self.frames} drained; no archival output"


# ---------------------------------------------------------------- reading a capture

def frames_dir(path: str | os.PathLike[str]) -> Path:
    """The capture directory for a bundle (its frames/) or a capture directory itself."""
    p = Path(path)
    for d in (p / "frames", p):
        if (d / INDEX).is_file():
            return d
    raise BackendError(f"{path}: no {INDEX} here or in frames/")


def load_index(d: Path) -> tuple[list[dict[str, Any]], dict[int, int]]:
    """Frame lines in write order, and each epoch's tick offset (global tick = tick + offset)."""
    frames: list[dict[str, Any]] = []
    offsets: dict[int, int] = {}
    with open(d / INDEX, encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                raise BackendError(f"{d / INDEX}:{n}: not JSON") from None
            if "seq" in obj:
                frames.append(obj)
            elif "offset" in obj:
                offsets[int(obj["epoch"])] = int(obj["offset"])
    return frames, offsets


def index_precision(d: Path) -> str:
    """The clock precision an index records (a precision_effective line), else "frame", the default.
    Indexes written before the field existed read as frame."""
    found = None
    try:
        with open(d / INDEX, encoding="utf-8") as f:
            for line in f:
                if '"precision_effective"' not in line:
                    continue
                try:
                    pe = json.loads(line).get("precision_effective")
                except ValueError:
                    continue
                if isinstance(pe, dict) and pe.get("precision") in ("frame", "tick"):
                    found = pe["precision"]
    except OSError:
        pass
    return found or "frame"


def latest(frames: Iterable[Mapping[str, Any]], offsets: Mapping[int, int]) -> dict[int, Mapping[str, Any]]:
    """Global tick -> the frame written last for it (a tick played again after a restore replaces it)."""
    out: dict[int, Mapping[str, Any]] = {}
    for fr in frames:
        out[int(fr["tick"]) + offsets.get(int(fr["epoch"]), 0)] = fr
    return out


def info(path: str | os.PathLike[str]) -> list[str]:
    d = frames_dir(path)
    frames, offsets = load_index(d)
    by_tick = latest(frames, offsets)
    units = unit_name(index_precision(d), plural=True)
    lines = [f"{d}: {len(frames)} images written, {len(by_tick)} {units}"]
    if by_tick:
        ticks = sorted(by_tick)
        missing = (ticks[-1] - ticks[0] + 1) - len(ticks)
        lines.append(f"{units} {ticks[0]}..{ticks[-1]}" + (f", {missing} missing" if missing else ", none missing"))
    for name in ("repeat", "blank", "cropped"):
        n = sum(1 for fr in by_tick.values() if name in fr.get("flags", ()))
        if n:
            lines.append(f"{name}: {n} {units}")
    sizes = sorted({(fr["w"], fr["h"]) for fr in frames})
    lines.append("sizes " + ", ".join(f"{w}x{h}" for w, h in sizes))
    files = list(dict.fromkeys(fr["file"] for fr in frames))
    lines.append(f"files: {len(files)}" + (f" ({', '.join(files[:4])}{', ...' if len(files) > 4 else ''})"))
    return lines


def _label_expr(runs: Sequence[tuple[int, int, int]]) -> str:
    """drawtext expression for an output frame n: runs are (first output frame, length, first tick)."""
    expr = ""
    for i, (s, _n, t) in enumerate(reversed(runs)):
        term = f"n+{t - s}" if t >= s else f"n-{s - t}"
        expr = term if i == 0 else f"if(lt(n\\,{runs[len(runs) - i][0]})\\,{term}\\,{expr})"
    return expr


def video(path: str | os.PathLike[str], out: str | os.PathLike[str], *, start: int | None = None,
          end: int | None = None, fps: int = FPS, crf: int = 18, font: str = "monospace",
          run: Callable[[list[str]], int] | None = None) -> str:
    """Encode the capture's positions start..end as H.264 with "<unit> N" drawn on every image; the unit
    is the recording's precision ("frame" unless it recorded "tick")."""
    d = frames_dir(path)
    frames, offsets = load_index(d)
    by_tick = latest(frames, offsets)
    unit = unit_name(index_precision(d))
    ticks = [t for t in sorted(by_tick) if (start is None or t >= start) and (end is None or t <= end)]
    if not ticks:
        raise BackendError(f"{d}: no images in the requested {unit}s")
    sizes = {(by_tick[t]["w"], by_tick[t]["h"]) for t in ticks}
    if len(sizes) != 1:
        raise BackendError(f"{d}: images of several sizes ({sorted(sizes)}); pick a {unit} range of one size")
    # runs of consecutive ticks; for mkv also consecutive frames of one file
    segs: list[list[int]] = []
    for t in ticks:
        fr = by_tick[t]
        if segs:
            p = by_tick[segs[-1][-1]]
            if t == segs[-1][-1] + 1 and (fr["file"] == p["file"] and fr["frame"] == p["frame"] + 1
                                          or fr["file"].endswith(".png")):
                segs[-1].append(t)
                continue
        segs.append([t])
    argv = ["-hide_banner", "-loglevel", "error", "-n"]
    chains, labels, runs, pos = [], [], [], 0
    if by_tick[ticks[0]]["file"].endswith(".png"):
        lst = Path(out).with_suffix(".ffconcat")
        lst.write_text("ffconcat version 1.0\n" + "".join(
            f"file '{(d / by_tick[t]['file']).resolve()}'\nduration {1 / fps:.9f}\n" for t in ticks))
        argv += ["-f", "concat", "-safe", "0", "-i", str(lst)]
        chains.append(f"[0:v]setpts=N/({fps}*TB)[c]")
        for s in segs:
            runs.append((pos, len(s), s[0]))
            pos += len(s)
    else:
        lst = None
        for i, s in enumerate(segs):
            fr = by_tick[s[0]]
            argv += ["-i", str(d / fr["file"])]
            chains.append(f"[{i}:v]trim=start_frame={fr['frame']}:end_frame={fr['frame'] + len(s)},"
                          f"setpts=N/({fps}*TB)[s{i}]")
            labels.append(f"[s{i}]")
            runs.append((pos, len(s), s[0]))
            pos += len(s)
        chains.append("".join(labels) + f"concat=n={len(segs)}:v=1:a=0[c]" if len(segs) > 1
                      else "[s0]null[c]")
    text = f"{unit} %{{eif\\:{_label_expr(runs)}\\:d}}"
    chains.append(f"[c]drawtext=font={font}:fontsize=28:fontcolor=white:box=1:boxcolor=black@0.6:boxborderw=6:"
                  f"x=10:y=10:text='{text}',format=yuv420p[v]")
    argv += ["-filter_complex", ";".join(chains), "-map", "[v]", "-r", str(fps), "-c:v", "libx264",
             "-preset", "veryfast", "-crf", str(crf), "-movflags", "+faststart", str(out)]
    cmd = ffmpeg_argv(*argv)
    try:
        rc = (run or (lambda a: subprocess.run(a, stdin=subprocess.DEVNULL, check=False).returncode))(cmd)
    finally:
        if lst is not None:
            lst.unlink(missing_ok=True)
    if rc != 0:
        raise BackendError(f"ffmpeg ended with status {rc}")
    gaps = (ticks[-1] - ticks[0] + 1) - len(ticks)
    return (f"{out}: {len(ticks)} images, {unit}s {ticks[0]}..{ticks[-1]}"
            + (f" ({gaps} {unit}s have no image)" if gaps else "") + f", {len(segs)} run(s)")
