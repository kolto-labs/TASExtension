"""Client for the native shim (contract sections 6, 9 and 10).

Python listens on loopback; the shim (or SimGame.exe) connects from its entry
detour using KPTAS_PORT and KPTAS_SESSION, then answers hello. Bulk bytes go
through files in the session directory; page records come through the staging ring.

A savestate is the tick's pages plus the stage's writable files. The pages come
through the ring every tick. The files are read at a held gate. A shim with
caps.step_stream runs a step of n ticks as one native request: every gate pushes
its report (step.tick), the host reports each tick from the ring while the game
runs on, and a tick that made a file-mutating call ends the request at its own
gate so the files are read there (see _step_stream). Without the cap, a step of
n ticks runs as n one-tick steps; only the first carries the note's first-tick
edges (first=true), so the game sees the same input as one n-tick step.
"""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import json
import os
import re
import secrets
import shutil
import socket
import sys
import struct
import stat
import subprocess
import time
from collections import deque
from functools import lru_cache
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

from kptas.config import DEFAULTS
from kptas.input_source import SOURCE_PROTOCOL, SourceAcknowledgement, PhysicalCandidate
from kptas.supervisor import (audio_env, auto_session_dir, clock_env, extra_env, filetime, portable_env,
                              portable_values)
from kptas.types import (VERB_ALLOW, WIRE_MAX_LINE, WIRE_PROTO, BackendError, Caps, ClockMode,
                         EngineEvent, EnvPin, FormatError, OsEvent, ProcOutcome, Receipt, RecordFlag, RecordKind, RefusedError,
                         SeekMode, StagingRecord, StepReport, Tier, VerbClass, VerifyLevel, sha256)

from kptas.frames import FrameRing, FrameSink, FrameViewSink, render_size, slot_bytes_for

from . import rig
from .rig import StageFiles, stage_audio
from kptas.filetree import decode_snapshot
from kptas._verified_blob import _verified_file_blob
from .staging import StagingRing, write_records

_ROOT = Path(__file__).resolve().parents[2]


class WireError(BackendError):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code


class WireRequestTimeout(BackendError):
    """A sent request has no admitted reply; its native outcome is uncertain."""


class OwnedLaunchCrash(BackendError):
    """A fresh fatal Wine line belongs to this connection's owned launch."""


def _wire_object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Build a protocol object only when every field occurs exactly once.

    ``json.loads`` otherwise keeps the last duplicate key. That is unsafe for
    receipts whose exact field set and values form a provenance contract: a
    malformed reply could otherwise hide a second, conflicting value.
    """
    obj: dict[str, Any] = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError(f"duplicate JSON object key {key!r}")
        obj[key] = value
    return obj


def _terminal_input_event(report: Mapping[str, Any], default_tick: int) -> EngineEvent | None:
    """Validate the shim's exited-before-gate receipt and preserve it as an event MARK.

    This is an attempted staged input only. It deliberately carries no guest-consumption
    acknowledgement and no state root, because the game exited before the capture gate.
    """
    if "terminal_input" not in report:
        return None
    raw = report["terminal_input"]
    if not isinstance(raw, Mapping):
        raise BackendError("terminal input receipt is not an object")
    expected = {"schema", "boundary", "acknowledgement", "guest_consumption", "captured_root", "native_tick",
                "source", "note", "first_tick", "keys", "buttons", "mouse", "wheel",
                "os_events", "staged_state_blake2s128"}
    if set(raw) != expected:
        raise BackendError("terminal input receipt has an unsupported field set")
    if (raw["schema"] != "kptas-terminal-input/1" or raw["boundary"] != "exited-before-gate"
            or raw["acknowledgement"] != "none"
            or raw["guest_consumption"] != "unproven" or raw["captured_root"] is not None):
        raise BackendError("terminal input receipt claims an unsupported boundary or proof")
    tick = raw["native_tick"]
    if type(tick) is not int or tick < 1 or tick != default_tick:
        raise BackendError("terminal input receipt has an invalid native tick")
    source = raw["source"]
    if source not in ("score", "physical"):
        raise BackendError("terminal input receipt has an invalid source")
    note = raw["note"]
    if not isinstance(note, str) or len(note) > 255:
        raise BackendError("terminal input receipt has an invalid note")
    if type(raw["first_tick"]) is not bool:
        raise BackendError("terminal input receipt has an invalid first-tick flag")
    keys = raw["keys"]
    if (not isinstance(keys, list) or any(type(key) is not int or not 1 <= key <= 255 for key in keys)
            or keys != sorted(set(keys))):
        raise BackendError("terminal input receipt keys are not canonical DIK codes")
    buttons = raw["buttons"]
    button_order = ("lmb", "rmb", "mmb")
    if (not isinstance(buttons, list) or any(button not in button_order for button in buttons)
            or buttons != [button for button in button_order if button in buttons]):
        raise BackendError("terminal input receipt buttons are not canonical")
    mouse = raw["mouse"]
    if not isinstance(mouse, Mapping) or set(mouse) != {"absolute", "relative"}:
        raise BackendError("terminal input receipt mouse state is malformed")
    for name, nullable in (("absolute", True), ("relative", False)):
        pair = mouse[name]
        if pair is None and nullable:
            continue
        if (not isinstance(pair, list) or len(pair) != 2
                or any(type(value) is not int or not -(1 << 31) <= value < (1 << 31) for value in pair)):
            raise BackendError(f"terminal input receipt mouse.{name} is malformed")
    wheel = raw["wheel"]
    if type(wheel) is not int or not -(1 << 31) <= wheel < (1 << 31):
        raise BackendError("terminal input receipt wheel is malformed")
    os_events = raw["os_events"]
    os_order = ("close", "altf4", "altf4@save")
    if (not isinstance(os_events, list) or any(event not in os_order for event in os_events)
            or os_events != [event for event in os_order if event in os_events]):
        raise BackendError("terminal input receipt OS events are not canonical")
    digest = raw["staged_state_blake2s128"]
    if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{32}", digest) is None:
        raise BackendError("terminal input receipt staged-state digest is malformed")
    arg = json.dumps(dict(raw), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return EngineEvent("input.attempted_exit", tick, arg)


@lru_cache(maxsize=1)
def _windows_tcp_api():
    import ctypes
    ws = ctypes.WinDLL("Ws2_32.dll")
    ioctl = ws.WSAIoctl
    ioctl.argtypes = [ctypes.c_size_t, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32,
                      ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32),
                      ctypes.c_void_p, ctypes.c_void_p]
    ioctl.restype = ctypes.c_int
    error = ws.WSAGetLastError
    error.argtypes = []
    error.restype = ctypes.c_int
    return ws, ioctl, error  # Retain the library and fixed ABI across snapshots.


def _windows_tcp_health(sock) -> str:
    """Query the owned socket with Windows 10+ SIO_TCP_INFO version zero.

    The SDK's TCP_INFO_v0 is 88 bytes; State is its first DWORD. Win7
    compatibility is explicitly unverified, not positive health evidence.
    """
    import ctypes
    try:
        _, ioctl, error = _windows_tcp_api()
        version, returned = ctypes.c_uint32(0), ctypes.c_uint32(0)
        info = ctypes.create_string_buffer(88)
        result = ioctl(sock.fileno(), 0xd8000027, ctypes.byref(version), 4,
                       info, len(info), ctypes.byref(returned), None, None)
        if result != 0:
            code = error()
            if code in (10022, 10042, 10045):  # invalid/unsupported control on older providers
                return "unverified"
            if code in (10038, 10050, 10054, 10057, 10058):
                return "dead"
            return "unknown"
        if returned.value != len(info):
            return "unknown"
        state = struct.unpack_from("<I", info.raw)[0]
        if not 0 <= state < 11:  # TCPSTATE_MAX, not a connection state
            return "unknown"
        return "healthy" if state == 4 else "dead"  # Windows TCPSTATE_ESTABLISHED
    except (AttributeError, OSError):
        return "unknown"


class Wire:
    """NDJSON request/reply over one socket. Pushes that arrive while waiting are queued."""

    def __init__(self, sock: socket.socket, timeout_s: float = 60.0):
        self.sock = sock
        self.timeout_s = timeout_s
        self._buf = bytearray()
        self._next_id = 1
        self.pushes: deque[dict[str, Any]] = deque()
        self._early: dict[int, dict[str, Any]] = {}
        self.stream_id: int | None = None      # a streamed step owns the socket until its reply

    def connection_health(self) -> str:
        """Passive owned transport state; never read/send NDJSON or poll a launcher.

        Linux TCP_INFO reports FIN even with unread bytes queued. Other hosts
        need their own positive evidence; unknown must not mean healthy. Older
        Windows providers retain explicitly unverified legacy compatibility.
        """
        if self.sock.fileno() < 0:
            return "dead"
        if self.sock.family not in (socket.AF_INET, socket.AF_INET6):
            return "unknown"
        if sys.platform == "win32":
            return _windows_tcp_health(self.sock)
        if not sys.platform.startswith("linux") or not hasattr(socket, "TCP_INFO"):
            return "unknown"
        try:
            info = self.sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_INFO, 1)
        except OSError:
            return "unknown"
        if not info:
            return "unknown"
        return "healthy" if info[0] == 1 else "dead"  # Linux TCP_ESTABLISHED

    def send(self, obj: Mapping[str, Any]) -> None:
        line = json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8") + b"\n"
        if len(line) > WIRE_MAX_LINE:
            raise BackendError(f"request line of {len(line)} bytes exceeds the 1 MiB limit")
        self.sock.sendall(line)

    def recv(self, timeout_s: float | None) -> dict[str, Any] | None:
        """One message, or None when timeout_s elapses first (timeout_s=None blocks)."""
        deadline = None if timeout_s is None else time.monotonic() + timeout_s
        while True:
            nl = self._buf.find(b"\n")
            if nl >= 0:
                line = bytes(self._buf[:nl]).rstrip(b"\r")
                del self._buf[:nl + 1]
                if not line:
                    continue
                try:
                    obj = json.loads(line, object_pairs_hook=_wire_object_without_duplicate_keys)
                except ValueError as e:
                    # A consumed malformed reply cannot safely be skipped: the
                    # caller no longer knows which protocol outcome it missed.
                    raise WireError("protocol", f"bad wire line: {e}") from None
                if not isinstance(obj, dict):
                    raise WireError("protocol", "wire message is not an object")
                return obj
            if len(self._buf) > WIRE_MAX_LINE:
                raise WireError("protocol", "wire line exceeds 1 MiB")
            left = None if deadline is None else deadline - time.monotonic()
            if left is not None and left <= 0:
                return None
            self.sock.settimeout(left)
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout:
                return None
            if not chunk:
                raise BackendError("the shim closed the connection")
            self._buf += chunk

    def request(self, verb: str, args: Mapping[str, Any] | None = None, timeout_s: float | None = None,
                idle: Callable[[], None] | None = None) -> dict[str, Any]:
        """Send one request and wait for its reply. ``idle`` runs every few ms while waiting."""
        if self.stream_id is not None:
            raise BackendError(f"{verb}: refused while streamed step {self.stream_id} owns the connection")
        rid = self._next_id
        self._next_id += 1
        self.send({"id": rid, "verb": verb, "args": dict(args or {})})
        limit = self.timeout_s if timeout_s is None else timeout_s
        deadline = time.monotonic() + limit if limit else None
        while True:
            # Step liveness is bounded even if unrelated pushes keep arriving.
            if verb == "step" and deadline is not None and time.monotonic() >= deadline:
                raise WireRequestTimeout(f"{verb}: no reply within {limit} s")
            msg = self._early.pop(rid, None)
            if msg is None:
                left = None if deadline is None else max(0.0, deadline - time.monotonic())
                if idle is not None:
                    msg = self.recv(0.005 if left is None else min(left, 0.005))
                    if msg is None:
                        idle()
                        if deadline is not None and time.monotonic() >= deadline:
                            raise WireRequestTimeout(f"{verb}: no reply within {limit} s")
                        continue
                else:
                    msg = self.recv(left)
                if msg is None:
                    raise WireRequestTimeout(f"{verb}: no reply within {limit} s")
            if "push" in msg:
                self.pushes.append(msg)
                continue
            if msg.get("id") != rid:
                if isinstance(msg.get("id"), int):
                    self._early[msg["id"]] = msg
                continue
            if msg.get("ok"):
                return msg.get("result") or {}
            err = msg.get("error") or {}
            code, text = str(err.get("code", "internal")), str(err.get("message", ""))
            if code == "refused":
                raise RefusedError(f"{verb}: {text}")
            raise WireError(code, f"{verb}: {text}")

    def begin(self, verb: str, args: Mapping[str, Any] | None = None) -> int:
        """Send one request and return its id; the caller reads the reply (a streamed step)."""
        rid = self._next_id
        self._next_id += 1
        self.send({"id": rid, "verb": verb, "args": dict(args or {})})
        return rid

    def poll(self, timeout_s: float = 0.0) -> None:
        """Move any pushes already on the socket into the queue."""
        if self.stream_id is not None:
            raise BackendError(f"poll: refused while streamed step {self.stream_id} owns the connection")
        while True:
            msg = self.recv(timeout_s)
            if msg is None:
                return
            if "push" in msg:
                self.pushes.append(msg)
            elif isinstance(msg.get("id"), int):
                self._early[msg["id"]] = msg
            timeout_s = 0.0

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


# ---------------------------------------------------------------- helpers

def win_path(path: str | os.PathLike[str], wine: bool = True) -> str:
    """A Linux path as the Windows process sees it (Wine maps / to Z:)."""
    p = str(Path(path).resolve())
    return "Z:" + p.replace("/", "\\") if wine else p


GL_GROUP, GL_RECORD = 0x4747504B, 0x4247504B      # "KPGG", "KPGB": a gl.apply set
GL_CTX_MAGIC = b"KPGC"
GL_FRAMEBUFFER_MAGIC = b"KPGF"


def gl_children(desc: bytes) -> list[bytes]:
    """The blob keys a GL descriptor names: {magic, version, n, n x sha256, ...}."""
    if len(desc) < 12:
        raise BackendError("GL descriptor too short")
    n = struct.unpack_from("<I", desc, 8)[0]
    if len(desc) < 12 + 32 * n:
        raise BackendError("GL descriptor truncated")
    return [desc[12 + 32 * i:44 + 32 * i] for i in range(n)]


def validate_framebuffer(desc: bytes, planes: list[bytes]) -> None:
    if len(desc) != 156 or struct.unpack_from("<II", desc, 4) != (1, 4):
        raise BackendError("unsupported framebuffer descriptor")
    width, height, depth, stencil = struct.unpack_from("<IIII", desc, 140)
    if not width or not height or width * height > 16777216 or not 1 <= depth <= 32 or not 1 <= stencil <= 8:
        raise BackendError("unsupported framebuffer dimensions or precision")
    sizes = [width * height * n for n in (4, 4, 4, 1)]
    if len(planes) != 4 or [len(p) for p in planes] != sizes:
        raise BackendError("framebuffer plane size does not match its descriptor")


def note_args(note: Any) -> dict[str, Any]:
    """A score Note as the step verb's input fields: the note text the shim journals, DIK codes, buttons,
    and mouse and wheel when the note has them. One-shot os events travel separately (msgs)."""
    from kptas.score.keys import dik, token_keys
    from kptas.supervisor import note_text
    if note is None:
        return {"note": ".", "keys": [], "buttons": []}
    args: dict[str, Any] = {"note": note_text(note), "buttons": list(note.buttons),
                            "keys": sorted({dik(k) for tok in note.held for k in token_keys(tok)})}
    mouse: dict[str, list[int]] = {}
    if note.mouse_abs is not None:
        mouse["abs"] = list(note.mouse_abs)
    if note.mouse_rel is not None:
        mouse["rel"] = list(note.mouse_rel)
    if mouse:
        args["mouse"] = mouse
    if note.wheel:
        args["wheel"] = note.wheel
    return args


def _hex(b: bytes | None) -> str | None:
    return None if b is None else b.hex()


def _caps(hello: Mapping[str, Any], backend: str) -> Caps:
    """Caps from a hello reply. The contract's shape names tier_ceiling; the shim's own caps object says
    what it virtualizes, and restore plus capture make restore and hybrid seeks possible."""
    c = hello.get("caps", {})
    if "tier_ceiling" in c:
        return Caps.from_hello({**hello, "backend": backend})
    seek = {SeekMode.REPLAY}
    if c.get("restore") and c.get("capture"):
        seek |= {SeekMode.RESTORE, SeekMode.HYBRID}
    threads = str(c.get("threads", ""))
    return Caps(
        backend=backend, tier_ceiling=Tier.TAS,
        clock_modes=frozenset(ClockMode(m) for m in c.get("clock", ())), seek_modes=frozenset(seek),
        input_paths=frozenset({"dinput"} if c.get("dinput") else ()),
        thread_models=frozenset({threads.split("_")[0]} if threads else ()),
        tick_source="winmain_loop_head", seed_hooked=bool(hello.get("seed_hooked", False)),
        capture=bool(c.get("capture", False)), fiber=c.get("context") == "fiber", ring=True,
        gl_names=bool(c.get("gl_names", False)), miles_shim=(c.get("audio") or {}).get("device") == "virtual",
        portable=frozenset(c.get("portable", ())),
        build=str(hello.get("build", "")),
        exe_sha256=str(hello.get("exe_sha256", "")), shim_version=str(hello.get("shim", "")),
        modules=tuple(hello.get("modules", ())), hooks=tuple(hello.get("hooks", ())))


def _integrity_detail(p: Mapping[str, Any]) -> str:
    """The address, expected and observed bytes of an integrity push, whatever the native lane called them."""
    def pick(*keys: str) -> Any:
        return next((p[k] for k in keys if p.get(k) is not None), None)
    parts = [f"{label} {value}" for label, value in
             (("address", pick("address", "addr", "va", "at")), ("expected", pick("expected", "want", "original")),
              ("observed", pick("observed", "actual", "got", "found"))) if value is not None]
    return ", ".join(parts) or json.dumps(p, sort_keys=True)


def _gone(e: BaseException) -> bool:
    """The connection ended (the game exited), as opposed to the shim refusing or failing a request."""
    if isinstance(e, (WireError, RefusedError, OwnedLaunchCrash)):
        return False
    return isinstance(e, OSError) or "closed the connection" in str(e)


# ---------------------------------------------------------------- backend

class ShimBackend:
    """The game (or SimGame.exe with ``simgame=True``) driven through the native shim.

    ``command`` overrides the launch argv. ``launch=False`` waits for an external
    process; ``on_listen(port, env)`` is called once the listener is up, before accept.
    ``host_prefix`` prefixes the launch argv (for example ["host-spawn"] inside distrobox).
    ``link`` adopts a connection someone else accepted (an object with ``sock`` and
    ``session``, like kptas.supervisor.GameLink); that owner keeps the process and relaunch.
    """

    name = "shim"
    streams_reports = True      # on_report gets one report per tick, in tick order (streamed or one-tick steps)
    capture_cadence = True      # step(capture_last=False) with capture.every > 1 (see _capture_every)
    capture_offset = 0          # global tick - local tick (Supervisor.offset): the grid is on global ticks
    capture_force_every_tick = False   # Supervisor.force_capture_every_tick (seek canary)
    note_first = True            # step(n, note, first=False) goes on with a note whose first tick has run
    save_ms_rewound = True       # save lengths are journalled inputs in the shim's state; a restore rewinds them

    def __init__(self, simgame: bool = False, launch: bool = True, command: Sequence[str] | None = None,
                 host_prefix: Sequence[str] | None = None,
                 on_listen: Callable[[int, dict[str, str]], None] | None = None, link: Any = None):
        self.name = "simgame" if simgame else "shim"
        self.link = link
        self.simgame = simgame
        self.launch = launch
        self.command = list(command) if command else None
        self.host_prefix = list(host_prefix) if host_prefix is not None else None
        self.on_listen = on_listen
        self.cfg: Mapping[str, Any] = {}
        self.hello: dict[str, Any] = {}
        self.session_dir: Path | None = None
        self.ring: StagingRing | None = None
        self.proc: subprocess.Popen[bytes] | None = None
        self.wire: Wire | None = None
        self._listener: socket.socket | None = None
        self._caps: Caps | None = None
        self._tier: Tier | None = None
        self._started = False
        self._epoch = 0
        self._wine = True
        self._events: deque[EngineEvent] = deque()
        self._seq = 0
        self._tick = 0
        self.input_source_engaged = True
        self._physical_prepared = None
        self._gl_transport = None
        self._gl_spools_pending = {}
        # frame-*.kpgs names already published when the last restore returned. The
        # native spool sequence and its emitted-key set live in the shim image, which
        # a restore never rewinds: these files may carry ticks past the restored gate,
        # and their blobs are never emitted again, so they must still be admitted.
        self._gl_restore_spools: frozenset[str] = frozenset()
        self._pending: deque[StagingRecord] = deque()     # records of complete tick batches
        self._open: list[StagingRecord] = []              # records of a batch whose TICK_END is still to come
        self._last_end = -1                               # tick of the last TICK_END seen
        self._journal_incomplete = False                  # first incomplete ring batch permanently fences later ticks
        self._journal_incomplete_tick: int | None = None  # earlier complete prefix reports remain admissible
        self.restore_batches = 0
        self._ring_attached = False
        self._clock_args: tuple[ClockMode, int, int] | None = None
        self._files: StageFiles | None = None
        self._file_log: list[tuple[int, Any]] = []
        self._files_dirty = False                         # put_files wrote the stage after the last read
        self._emit_limit: int | None = None               # streamed step: the tick being reported
        self._streaming = False                           # a streamed step request is in flight
        self._drain_upto: int | None = None               # streamed step: read the ring up to batch upto+1
        self._last_reported = -1                          # last tick on_report took without failing
        self._gl_announced: set[str] = set()              # per-digest GL blobs a reported gate listed
        self._gl_admit_all = False                        # the reported gate could not list them (gl_all)
        self._beat = None                                 # (thread, stop event): the ring heartbeat
        self._msgs: list[str] = []                        # os events for the next tick
        self.logs: deque[dict[str, Any]] = deque(maxlen=1000)
        self.ticks: deque[dict[str, Any]] = deque(maxlen=1000)
        self.last_waited: Any = None
        self.frames_dir: Path | None = None              # capture.frames: where the sink stores frames
        self.frames_epoch = 0
        self._frame_mirror_valid = False
        self._frames_expected = False
        self.frames: FrameSink | None = None
        self.frames_summary = ""
        self.admitted: bytes | None = None     # sha256 of the roots this process admitted (restore.admit)
        self.poisoned = False                  # a tas restore failed to verify; the shim refuses ticks
        self._native_step_unknown = None       # latched until a qualified new process handshake
        self._owned_crash_watch = None         # bounded current-launch log cursor; never an external attachment
        self._tick_profile = None              # opt-in host clocks; no files or native calls
        self._profile_file_scan_ns = 0
        self._profile_ring_ns = 0
        self._profile_ring_calls = 0
        self._profile_ring_records = 0
        self._profile_ring_post_ns = 0         # the drain after the reply: on the critical path
        self._profile_ring_post_records = 0
        self._profile_native_tick = None

    # ------------------------------------------------------------------ lifecycle

    @property
    def caps(self) -> Caps:
        if self._caps is None:
            raise BackendError("not attached")
        return self._caps

    def _session(self) -> Path:
        sd = self.cfg.get("shim", {}).get("session_dir", "auto")
        p = auto_session_dir() if sd == "auto" else Path(os.path.expanduser(sd))
        p.mkdir(parents=True, exist_ok=True, mode=0o700)
        return p

    def _boundary(self) -> str | None:
        """The KPTAS_BOUNDARY this config serves, or None when the config is too partial to say."""
        try:
            from kptas.tier.envpin import boundary_env
            return boundary_env(self.cfg)
        except (KeyError, TypeError):
            return None

    def _launch_argv(self, env: dict[str, str]) -> tuple[list[str], str | None]:
        game = self.cfg.get("game", {})
        wine = game.get("wine", "wine")
        if self.command:
            argv, cwd = list(self.command), None
        else:
            if self.simgame:
                exe = self.cfg.get("sim", {}).get("simgame_exe", "auto")
                if exe == "auto":
                    exe = str(_ROOT / "native" / "simgame" / "build" / "SimGame.exe")
                cwd = str(Path(exe).parent)
            else:
                if not game.get("dir"):
                    raise BackendError("game.dir is not set; nothing to launch")
                cwd = os.path.expanduser(game["dir"])
                exe = os.path.join(cwd, game.get("exe", "swkotor.exe"))
            argv = ([wine] if wine else []) + [exe]
        if os.name == "nt":
            # Native Windows: no Wine, sh, env or setarch. Popen gets cwd and env directly (see launch()).
            return (argv if self.command else [exe]), cwd
        if game.get("launch_wrapper") == "setarch_R":
            argv = ["setarch", "-R", *argv]
        pinned = bool(game.get("pinned_env", True))
        envargs = ["env", "-i"] if pinned else ["env", "-u", "KPTAS_GL_PROFILE"]
        envargs += [f"{k}={v}" for k, v in sorted(env.items())]
        if cwd:
            argv = ["sh", "-c", 'cd "$1" && shift && exec "$@"', "sh", cwd, *envargs, *argv]
        else:
            argv = [*envargs, *argv]
        prefix = self.host_prefix
        if prefix is None:
            prefix = ["host-spawn"] if wine and not shutil.which(wine) and shutil.which("host-spawn") else []
        return [*prefix, *argv], cwd

    def _child_env(self, port: int) -> dict[str, str]:
        """The launch environment; matches kptas.supervisor.game_environ."""
        game, clock = self.cfg.get("game", {}), self.cfg.get("clock", {})
        env = {"KPTAS_PORT": str(port), "KPTAS_SESSION": win_path(self.session_dir, bool(game.get("wine", "wine"))),
               "KPTAS_CLOCK": clock_env({"clock": {**DEFAULTS["clock"], "mode": "virtual", **clock}}),
               "KPTAS_PRESENT": str(self.cfg.get("replay", {}).get("present", "skip")),
               "KPTAS_PORTABLE": portable_env(self.cfg)}
        if self.cfg.get("capture", {}).get("gl_profile") is True:
            env["KPTAS_GL_PROFILE"] = "1"
        bnd = self._boundary()
        if bnd is not None:
            env["KPTAS_BOUNDARY"] = bnd
        if game.get("pinned_env", True):
            for k in ("HOME", "USER"):
                if k in os.environ:
                    env[k] = os.environ[k]
            env.update(LANG="C", PATH="/usr/bin:/bin", WINEDEBUG="-all")
        display = game.get("display", "auto")
        display = os.environ.get("DISPLAY", "") if display == "auto" else display
        if display:
            env["DISPLAY"] = display
        if game.get("wineprefix"):
            env["WINEPREFIX"] = os.path.expanduser(game["wineprefix"])
        ov = game.get("dll_overrides", "auto")
        if ov == "auto":
            ov = "dinput8=n,b"
        if ov:
            env["WINEDLLOVERRIDES"] = ov
        env.update(extra_env(self.cfg))
        if audio_env(self.cfg):
            env["KPTAS_AUDIO"] = audio_env(self.cfg)
        return env

    def attach(self, cfg: Mapping[str, Any], env: EnvPin | None = None) -> Caps:
        """Listen, launch, accept the shim's connection, hello, and attach the staging ring."""
        self.cfg = cfg
        shim = cfg.get("shim", {})
        game = cfg.get("game", {})
        self._wine = bool(game.get("wine", "wine"))
        self.session_dir = Path(self.link.session) if self.link is not None else self._session()
        self.session_dir.mkdir(parents=True, exist_ok=True)
        if self.name == "shim" and game.get("dir") and cfg.get("files", {}).get("store", True):
            self._files = StageFiles(Path(os.path.expanduser(game["dir"])),
                                     cfg.get("files", {}).get("stage_paths", ()))
        cap = int(shim.get("ring_mb", 64)) << 20
        self.ring = StagingRing.create(self.session_dir / "ring.bin", cap)
        self.ring.set_consumer(state=0)
        if self.link is not None:
            from kptas.supervisor import GameLink
            from kptas.telos.game_focus import GameFocus, LaunchAnchor
            self.ui_game_focus = None
            if type(self.link) is GameLink and self.link.proc is not None and not self.simgame:
                anchor = self.link._ui_focus_anchor
                if (type(anchor) is LaunchAnchor and anchor.pid == self.link.proc.pid
                        and anchor.session == win_path(self.session_dir, self._wine)):
                    self.ui_game_focus = GameFocus(anchor)
            self._handshake(self.link.sock)
            return self.caps
        self._listener = socket.create_server((shim.get("listen", "127.0.0.1"), int(shim.get("port", 0))),
                                              backlog=1)
        port = self._listener.getsockname()[1]
        self._connect(port, int(shim.get("connect_timeout_s", 30)))
        return self.caps

    def _connect(self, port: int, timeout_s: int) -> None:
        env = self._child_env(port)
        self.ui_game_focus = None
        log_mark = rig.shim_log_size(self.session_dir)
        local_focus = self.launch and not self.simgame and not self.host_prefix
        if local_focus:
            env['KPTAS_FOCUS_NONCE'] = secrets.token_hex(32)
        if self.on_listen is not None:
            self.on_listen(port, env)
        if self.launch:
            if not self.simgame and not self.command:
                rig.refuse_kpm(self.cfg)    # read-only, before stage_audio writes into game.dir
                stage_audio(self.cfg)       # the ini must agree with KPTAS_AUDIO, as rig.prepare_launch makes it
            argv, cwd = self._launch_argv(env)
            log = open(self.session_dir / f"process-{self._epoch}.log", "wb")  # type: ignore[operator]
            if os.name == "nt":
                # Windows needs SystemRoot and friends, so the pinned values go on top of the real environment.
                self.proc = subprocess.Popen(argv, cwd=cwd, env={**os.environ, **env}, stdin=subprocess.DEVNULL,
                                             stdout=log, stderr=subprocess.STDOUT,
                                             creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
            else:
                self.proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                             start_new_session=True)
            log.close()
            if local_focus and argv[0] != 'host-spawn':
                from kptas.telos.game_focus import GameFocus, launch_anchor
                try:
                    self.ui_game_focus = GameFocus(launch_anchor(self.proc.pid, self.cfg, env))
                except (BackendError, OSError, KeyError, ValueError, IndexError):
                    pass  # Unsupported launch shapes do not gain UI focus capability.
        assert self._listener is not None
        # Short accepts, so a game that exits first (the shim's KPM refusal ends it at load) is reported now
        self._listener.settimeout(0.25)
        deadline = time.monotonic() + timeout_s
        while True:
            try:
                conn, _ = self._listener.accept()
                break
            except TimeoutError:
                pass
            proc = self.proc if self.launch else None
            done = proc is not None and proc.poll() is not None
            if done or time.monotonic() > deadline:
                why = rig.kpm_exit_diagnosis(self.session_dir, log_mark, proc.returncode if done else None)
                if why:
                    raise BackendError(why)
                if done:
                    raise BackendError(f"the game exited with status {proc.returncode} before the shim connected")
                raise BackendError(f"the shim did not connect within {timeout_s} s")
        self._listener.settimeout(None)
        self._handshake(conn)

    def request_ui_execution_focus(self, display):
        focus = getattr(self, 'ui_game_focus', None)
        if focus is None:
            raise BackendError('this game owner has no qualified local UI focus binding')
        _, width, height = self.view_raw()
        focus.focus(display, (width, height))

    def _handshake(self, conn: socket.socket) -> None:
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.wire = Wire(conn)
        hello = self.wire.request("hello", {"proto": WIRE_PROTO})
        if hello.get("proto") != WIRE_PROTO:
            raise BackendError(f"shim speaks {hello.get('proto')!r}, not {WIRE_PROTO}")
        if self.name == "shim" and (hello.get("caps") or {}).get("journal_event_completeness") != 1:
            raise BackendError("the loaded shim cannot prove complete per-tick event provenance; update the shim before starting")
        # build is the loaded DLL's sha256 prefix (16 hex). Older builds sent a game name there, so with a pin
        # set, a build that is not 16 or more hex digits is refused like a wrong one; a 64-digit pin is held
        # against the full shim_sha256.
        want = str(self.cfg.get("shim", {}).get("dll_sha256", "")).lower()
        got = str(hello.get("build", "")).lower()
        if self.name == "shim" and want:
            full = str(hello.get("shim_sha256", "")).lower()
            hexd = set("0123456789abcdef")
            if len(got) < 16 or not set(got) <= hexd:
                raise BackendError(f"the game loaded a shim that reports build {got or '(none)'!r}, not a build id; "
                                   f"the pin is {want[:16]}")
            n = min(len(want), len(got))
            if want[:n] != got[:n]:
                raise BackendError(f"the game loaded shim build {got}, not the pinned {want[:16]}")
            if len(want) == 64 and full != want:
                raise BackendError(f"the game loaded a shim with sha256 {full or '(none)'}, not the pinned {want}")
        if self.name == "shim":
            self._check_portable(hello.get("caps", {}))
            # the loader that put the shim in the game; a hello without the field is a build from before it
            # Vestigial since KPM support was removed: dinput8 is the only loader and every hello says so.
            # Kept so a stale shim that reports another loader is refused rather than trusted.
            loader = hello.get("loader")
            want_loader = self.cfg.get("shim", {}).get("loader", "dinput8")
            if loader is not None and loader != want_loader:
                raise BackendError(f"the shim was loaded by {loader!r}, but shim.loader is {want_loader!r}; "
                                   f"the stage and the config disagree about who injects the shim")
        # the portable profile is served: the shim reports a digest of the boundary text it parsed
        bnd, want_b = hello.get("boundary"), self._boundary() if self.name == "shim" else None
        if isinstance(bnd, dict) and want_b is not None:
            if bnd.get("sha256") != hashlib.sha256(want_b.encode()).hexdigest():
                raise BackendError(f"the shim serves a {bnd.get('mode')} boundary that is not this config's "
                                   f"portable profile ({self.cfg['portable']['profile']})")
        self.hello = hello
        self._epoch = int(hello.get("epoch", self._epoch))
        self._caps = _caps(hello, self.name)
        integrity = hello.get("integrity")
        entries = integrity.get("entries") if isinstance(integrity, Mapping) else None
        self._caps = dataclasses.replace(
            self._caps, loader=str(hello.get("loader", "")),
            integrity_entries=entries if type(entries) is int else None)
        # a virtual device that did not install would leave the real Miles, and the host's hardware, answering
        if self.name == "shim" and self.cfg.get("audio", {}).get("device") == "virtual" and not self._caps.miles_shim:
            got_audio = (hello.get("caps") or {}).get("audio")
            raise BackendError(f"audio.device is \"virtual\" but the shim reports {got_audio!r}: the loaded build "
                               f"does not answer every Miles call, so the host's audio hardware would decide state")
        self.admitted, self.poisoned = None, False      # a new connection is a new process
        self._integrity_halt = None
        self._native_step_unknown = None
        self._journal_incomplete = False
        self._journal_incomplete_tick = None
        self._arm_owned_crash_watch()
        # section 10 order is hello, tier.lock, clock, ring.attach: attach before capture or start
        self._ring_attached = False

    def _check_portable(self, caps: Mapping[str, Any]) -> None:
        """The shim must serve the configured math mode and control words. A shim without these caps
        is older than reference math: it serves native math with 0x027F and 0x1F80."""
        want = portable_values(self.cfg)
        got = {"math": str(caps.get("math", "native")), "x87_cw": int(caps.get("x87_cw", 0x027F)),
               "mxcsr": int(caps.get("mxcsr", 0x1F80))}
        bad = [f"{k} {got[k] if k == 'math' else hex(got[k])}, not {want[k] if k == 'math' else hex(want[k])}"
               for k in ("math", "x87_cw", "mxcsr") if got[k] != want[k]]
        if bad:
            why = f" ({caps['math_error']})" if caps.get("math_error") else ""
            raise BackendError(f"the shim serves portable {', '.join(bad)}{why}")

    def _ensure_ring(self) -> None:
        if self._ring_attached:
            return
        assert self.ring is not None and self.session_dir is not None
        self.ring_header = self._call("ring.attach", {"path": win_path(self.ring.path, self._wine),
                                                      "mb": self.ring.capacity >> 20,
                                                      "bytes": 4096 + self.ring.capacity}).get("header")
        self.ring.set_consumer()
        self._ring_attached = True

    def execution_health(self) -> str:
        if self.poisoned:
            return "dead"
        if self.wire is None:
            return "dead"
        sink = getattr(self, 'frames', None)
        if sink is None and getattr(self, '_frames_expected', False):
            return "dead"
        if sink is not None:
            try:
                if sink.consumer_health() != "healthy":
                    return "dead"
            except Exception:
                return "unknown"
        return self.wire.connection_health()

    def _call(self, verb: str, args: Mapping[str, Any] | None = None, timeout_s: float | None = None
              ) -> dict[str, Any]:
        cleanup = self._admit(verb, args)
        # drain the ring while waiting: the shim's gate waits for space, never the reverse
        try:
            res = self.wire.request(verb, args, timeout_s,
                                    idle=self._drain_ring if cleanup else self._wire_wait_idle)
        except WireError as exc:
            if exc.code in ("journal_incomplete", "protocol"):
                self._poison_journal(exc)
            raise
        if self._tick_profile is None:
            self._drain_ring()
        else:
            # Idle drains overlap the native run; only this one adds gate latency.
            ns, records = self._profile_ring_ns, self._profile_ring_records
            self._drain_ring()
            self._profile_ring_post_ns += self._profile_ring_ns - ns
            self._profile_ring_post_records += self._profile_ring_records - records
        self._drain_pushes()
        return res

    def _poison_journal(self, cause: BaseException, tick: int | None = None) -> None:
        """A native tick ran without a complete event record; never retry it."""
        detail = str(cause)
        self.poisoned = True
        self._journal_incomplete = True
        if type(tick) is int and (self._journal_incomplete_tick is None or tick < self._journal_incomplete_tick):
            self._journal_incomplete_tick = tick
        invalid_from = self._journal_incomplete_tick
        if invalid_from is not None:
            self._pending = deque(rec for rec in self._pending if rec.tick < invalid_from)
            self._open = [rec for rec in self._open if rec.tick < invalid_from]
            self._last_end = min(self._last_end, invalid_from - 1)
        self._native_step_unknown = f"event provenance is incomplete: {detail}; close or relaunch required"

    def _admit(self, verb: str, args: Mapping[str, Any] | None = None) -> bool:
        """Refuse a request this backend must not send now; True for a cleanup request."""
        if self.wire is None:
            raise BackendError("not attached")
        if getattr(self, '_streaming', False):
            # A tick report (sink, presentation) ran while the native step it reports is still
            # running; another request now would take that step's pushes and reply.
            raise BackendError(f"{verb}: refused while a streamed step is running")
        uncertain = getattr(self, '_native_step_unknown', None)
        cleanup = verb == 'bye' or (verb == 'frames' and args is not None and args.get('enabled') is False)
        halt = getattr(self, '_integrity_halt', None)
        if halt is not None and not cleanup:
            from kptas.supervisor import RunError
            raise RunError(f"shim integrity halt: {halt}; execution stopped, relaunch required")
        if uncertain is not None and not cleanup:
            raise BackendError(f"{verb}: native step outcome unknown; {uncertain}; execution stopped, close or relaunch required")
        if self.poisoned and verb in ("step", "wait", "digest"):
            raise BackendError(f"{verb}: restore is incomplete or failed; complete verified reconstruction or relaunch")
        allowed = VERB_ALLOW.get(verb, frozenset())
        if self._tier is None and Tier.TAS not in allowed:
            raise RefusedError(f"{verb} is lab only; lock the tier at lab first")
        if self._tier is not None and self._tier not in allowed:
            raise RefusedError(f"{verb} is refused in tier {self._tier.label}")
        return cleanup

    @staticmethod
    def _launch_start_stamp(pid: int) -> str | None:
        if not sys.platform.startswith('linux'):
            return None
        try:
            text = Path(f'/proc/{pid}/stat').read_text()
            return text[text.rfind(')')+2:].split()[19]
        except (OSError, IndexError):
            return None

    def _arm_owned_crash_watch(self) -> None:
        """Bind after handshake; historical bytes are excluded from this epoch."""
        self._owned_crash_watch = None
        if self.simgame or self.session_dir is None or self.wire is None:
            return
        proc = self.proc
        path = self.session_dir / f'process-{self._epoch}.log'
        if self.link is not None:
            from kptas.supervisor import GameLink
            if type(self.link) is not GameLink or self.link.proc is None or self.link.sock is not self.wire.sock:
                return
            proc = self.link.proc
            path = self.session_dir / 'game.out'
            # Only the supervisor's explicit redirect is a known diagnostic log.
            if not isinstance(proc.args, (list, tuple)) or str(path) not in proc.args:
                return
        if proc is None or (self.link is None and not self.launch):
            return
        stamp = self._launch_start_stamp(proc.pid)
        if sys.platform.startswith('linux') and stamp is None:
            return
        try:
            session = self.session_dir.resolve(strict=True)
            directory = session.stat()
            info = path.stat(follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode):
                return
        except OSError:
            return
        self._owned_crash_watch = {'proc':proc, 'pid':proc.pid, 'start':stamp,
            'epoch':self._epoch, 'socket':self.wire.sock, 'session':session,
            'directory':(directory.st_dev,directory.st_ino), 'path':path,
            'file':(info.st_dev,info.st_ino), 'offset':info.st_size,
            'tail':b'', 'next_poll':0.0}

    def _wire_wait_idle(self) -> None:
        self._check_owned_crash_log()
        self._drain_ring()

    def _check_owned_crash_log(self) -> None:
        watch = self._owned_crash_watch
        if watch is None or time.monotonic() < watch['next_poll']:
            return
        watch['next_poll'] = time.monotonic()+0.1
        proc = self.link.proc if self.link is not None else self.proc
        if (proc is not watch['proc'] or proc.pid != watch['pid'] or self._epoch != watch['epoch'] or
                self.wire is None or self.wire.sock is not watch['socket'] or self.session_dir is None):
            self._owned_crash_watch = None
            return
        if sys.platform.startswith('linux') and self._launch_start_stamp(proc.pid) != watch['start']:
            self._owned_crash_watch = None
            return
        try:
            session = self.session_dir.resolve(strict=True)
            directory = session.stat()
            if session != watch['session'] or (directory.st_dev,directory.st_ino) != watch['directory']:
                self._owned_crash_watch = None
                return
            fd = os.open(watch['path'],os.O_RDONLY | getattr(os,'O_NOFOLLOW',0) | getattr(os,'O_NONBLOCK',0))
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode):
                    return
                identity = (info.st_dev,info.st_ino)
                if identity != watch['file'] or info.st_size < watch['offset']:
                    # A replaced/truncated file may contain replayed old diagnostics.
                    # Rebaseline at EOF; never carry text across file generations.
                    watch.update(file=identity,offset=info.st_size,tail=b'')
                    return
                os.lseek(fd,watch['offset'],os.SEEK_SET)
                data = os.read(fd,16384)
            finally:
                os.close(fd)
        except OSError:
            return  # Absent/unreadable output is not positive crash evidence.
        watch['offset'] += len(data)
        lines = (watch['tail']+data).split(b'\n')
        watch['tail'] = lines.pop()[-2048:]
        for line in lines:
            if re.fullmatch(rb'wine: Unhandled page fault .{1,1024}, starting debugger\.\.\.\r?',line):
                fatal = line.rstrip(b'\r').decode('utf-8','replace')
                why = f'owned native epoch {self._epoch}: {fatal}'
                self.poisoned = True
                self._native_step_unknown = why
                raise OwnedLaunchCrash(f'{why}; execution poisoned; last reported tick {self._last_reported}; do not retry')

    def _drain_ring(self) -> None:
        if self._tick_profile is None:
            self._drain_ring_records()
            return
        started = time.perf_counter_ns()
        try:
            self._drain_ring_records()
        finally:
            self._profile_ring_ns += time.perf_counter_ns() - started
            self._profile_ring_calls += 1

    def _drain_ring_records(self) -> None:
        """Move complete tick batches to the queue. A restore's batch is dropped: it holds the pages the
        restore wrote, which the caller already knows, and no tick was played."""
        if self.ring is None:
            return
        # Streamed step: read no further than the batch after the newest pushed gate, so the
        # ring, not this process's memory, bounds how far the game runs ahead. A batch is on
        # the ring before its push, so the reader never waits on a push it holds back.
        upto = self._drain_upto
        if upto is not None and self._last_end > upto:
            return
        for rec in self.ring.read(_verified_pages=True):
            if self._tick_profile is not None:
                self._profile_ring_records += 1
            if self._journal_incomplete:
                if rec.kind is RecordKind.TICK_END:
                    self._open.clear()
                continue
            self._open.append(rec)
            if rec.kind is not RecordKind.TICK_END:
                continue
            batch, self._open = self._open, []
            if rec.flags & RecordFlag.RESTORE:
                self.restore_batches += 1
                continue
            # Validate the ring's source record before it enters the host's
            # pending batch. A wire acknowledgement alone cannot make an
            # incomplete tick eligible for recording or replay.
            journal = next((item.data for item in batch if item.kind is RecordKind.JOURNAL), None)
            try:
                from kptas.holo.verify import require_journal_complete
                require_journal_complete(journal)
            except FormatError as exc:
                self.poisoned = True
                self._journal_incomplete = True
                if self._journal_incomplete_tick is None or rec.tick < self._journal_incomplete_tick:
                    self._journal_incomplete_tick = rec.tick
                self._native_step_unknown = f"ring tick {rec.tick} has incomplete event provenance: {exc}; relaunch required"
                continue
            self._last_end = rec.tick
            self._pending.extend(batch)
            if upto is not None and self._last_end > upto:
                return

    def lock_tier(self, tier: Tier) -> None:
        tier = Tier.parse(tier)
        if tier is Tier.HANDS_OFF:
            raise BackendError("hands-off has no wire; use the handsoff backend")
        self._call("tier.lock", {"tier": tier.label})
        self._tier = tier

    def clock(self, mode: ClockMode, hz: int, scale: int = 100) -> None:
        mode = ClockMode(mode)
        if self._tier is Tier.TAS and mode is not ClockMode.VIRTUAL:
            raise RefusedError(f"clock {mode} is refused in tier tas")
        c = self.cfg.get("clock", {})
        spin = {"escape": c.get("spin_escape", "callsite"), "count": dict(c.get("spin_count", {}))}
        ft = filetime(str(c.get("initial_realtime", "2004-02-12T00:00:00Z")))
        self._call("clock", {
            "mode": str(mode), "hz": hz, "scale": scale, "qpf_hz": int(c.get("qpf_hz", 10_000_000)),
            "uptime0_ms": int(c.get("uptime0_ms", 3_600_000)), "initial_realtime": ft, "initial_ft": ft,
            "hooked_modules": list(c.get("hooked_modules", ())),
            "budget_guard": dict(c.get("budget_guard", {})), "spin": spin,
            # wording only (HUD label, refusal text); never part of the clock arithmetic
            "precision": "tick" if c.get("precision") == "tick" else "frame"})
        self._clock_args = (mode, hz, scale)

    def start(self) -> StepReport:
        """Attach the ring, turn capture on (the shim takes it only before start), and run to tick 0."""
        self._ensure_ring()
        if self.cfg.get("capture", {}).get("enabled", "auto") is not False:
            self.capture("dirty")
        self._frames_start()
        rep = self._report(self._call("start", timeout_s=float(self.cfg.get("shim", {}).get("start_timeout_s", 180))))
        self._started = True
        self._scan_files(rep.tick)
        return rep

    def close(self) -> None:
        try:
            self._frames_stop()
        finally:
            self._close()

    def _close(self) -> None:
        if self.wire is not None:
            try:
                self.wire.request("bye", {"exit": True}, timeout_s=5)
            except (BackendError, OSError):
                pass
            if self.link is None:
                self.wire.close()
            self.wire = None
        if self._listener is not None:
            self._listener.close()
            self._listener = None
        if self.proc is not None:
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
            self.proc = None
        if self._beat is not None:
            self._beat[1].set()
            self._beat[0].join(timeout=2)
            self._beat = None
        if self.ring is not None:
            self.ring.set_state(2)
            self.ring.close()
            self.ring = None

    # ------------------------------------------------------------------ frames

    def _frames_start(self) -> None:
        """capture.frames: create the frame ring, attach the shim to it, and start the sink thread. The
        shim reads the back buffer at every present and writes tick n's frame at gate n."""
        cap = self.cfg.get("capture", {})
        if self.frames is not None:
            if self.frames.consumer_health() != 'healthy':
                raise BackendError('previous frame capture requires shutdown or recovery; its owner is retained')
            return
        if cap.get("frames", "off") == "off" and not cap.get("frames_view", False):
            return
        assert self.session_dir is not None
        if not self.hello.get("caps", {}).get("frames"):
            raise BackendError(f"capture.frames needs a shim with the frames verb; build "
                               f"{self.hello.get('build', '?')} has none")
        w, h = render_size(self.cfg.get("game", {}).get("dir"))
        ring = FrameRing.create(self._frames_ring_dir() / f"frames-{os.getpid()}-{self.frames_epoch}-{secrets.token_hex(8)}.bin",
                                int(cap.get("frames_slots", 8)), slot_bytes_for(w, h))
        try:
            self._call("frames", {"enabled": True, "path": win_path(ring.path, self._wine), "slots": ring.slots,
                                  "slot_bytes": ring.slot_bytes})
        except BaseException:
            ring.close()
            raise
        self._frames_expected = True
        self.frames = (FrameViewSink(ring, self.frames_epoch) if cap.get("frames", "off") == "off"
                       else FrameSink(ring, self.frames_dir or self.session_dir / "frames", self.frames_epoch,
                                      str(cap.get("frames_sink", "mkv"))))
        # index.jsonl records the precision the run used, so labels and summaries name its unit later;
        # an index line only, never part of game state, the journal or the determinism key
        prec = "tick" if self.cfg.get("clock", {}).get("precision") == "tick" else "frame"
        self.frames.note(sticky=True, precision_effective={"precision": prec, "unit": prec})

    def _frames_ring_dir(self) -> Path:
        """The frame ring moves a frame (3 MiB at 1024x768) every tick, so it lives on tmpfs
        ($XDG_RUNTIME_DIR) when there is one: in a session directory on disk the gate's write stalled on
        writeback (11.6 ms mean, 310 ms max)."""
        run = os.environ.get("XDG_RUNTIME_DIR")
        if run and os.path.isdir(run):
            d = Path(run) / "kptas"
            d.mkdir(mode=0o700, exist_ok=True)
            return d
        assert self.session_dir is not None
        return self.session_dir

    def frames_note(self, **fields: Any) -> None:
        """A sticky index line for this epoch's frames (the supervisor's tick offset)."""
        if self.frames is not None:
            self.frames.note(sticky=True, **fields)

    def frames_retarget(self, out_dir: Path) -> None:
        """Frames published from now on go to out_dir (a branch bundle's frames/)."""
        self.frames_dir = out_dir
        if self.frames is not None:
            self.frames.retarget(out_dir)

    def _frames_stop(self) -> None:
        """Before bye, which ends the process at once: turn capture off, store every published frame and
        finish the files."""
        self._frame_mirror_valid = False
        self._frames_expected = False
        sink = self.frames
        if sink is None:
            return
        stats = None
        failure = None
        if self.wire is not None:
            try:
                stats = self._call("frames", {"enabled": False}, timeout_s=30)
            except (BackendError, OSError):
                pass
        try:
            sink.drain()
            if stats:
                sink.note(shim=stats)
        except BaseException as error:
            failure = error
        finally:
            try:
                self.frames_summary = sink.close()
            except BaseException as error:
                failure = failure or error
        if failure is not None:
            self.frames_summary = f'frames shutdown incomplete: {failure}'
            raise failure
        if self.frames is sink:
            self.frames = None

    # ------------------------------------------------------------------ ticks

    def _report(self, res: Mapping[str, Any]) -> StepReport:
        """A step reply as a StepReport, with the events pushed while it ran."""
        complete = (res.get("events_complete") is True
                    and res.get("journal_events_complete") is True
                    and type(res.get("events_dropped", 0)) is int
                    and res.get("events_dropped", 0) == 0
                    and type(res.get("journal_events_dropped", 0)) is int
                    and res.get("journal_events_dropped", 0) == 0
                    and not res.get("events_truncated"))
        rep = StepReport.from_wire(res)
        try:
            terminal = _terminal_input_event(res, rep.tick)
        except BackendError as exc:
            self._poison_journal(exc, rep.tick)
            raise
        tick = res.get("tick")
        invalid_from = self._journal_incomplete_tick
        terminal_only = False
        if self.name == "shim" and (not complete or
                                     (invalid_from is not None and type(tick) is int and tick >= invalid_from)):
            exc = BackendError("native tick report does not prove complete event provenance; execution stopped")
            self._poison_journal(exc, tick if type(tick) is int else None)
            # The terminal receipt is outside the missing gate journal. Preserve
            # that attempted-input fact while refusing to accept this as a tick.
            if rep.outcome is ProcOutcome.EXITED and terminal is not None:
                rep = dataclasses.replace(rep, events=(terminal,))
                terminal_only = True
            else:
                raise exc
        if terminal is not None:
            if rep.outcome is not ProcOutcome.EXITED:
                exc = BackendError("terminal input receipt appeared without an exited outcome")
                self._poison_journal(exc, rep.tick)
                raise exc
            if not terminal_only:
                rep = dataclasses.replace(rep, events=(*rep.events, terminal))
        if terminal_only:
            self._tick = rep.tick
            self._frame_mirror_valid = False
            return rep
        pushed = []
        while self._events:
            pushed.append(self._events.popleft())
        if pushed:
            rep = dataclasses.replace(rep, events=(*rep.events, *pushed))
        self._tick = rep.tick
        self._frame_mirror_valid = rep.outcome is ProcOutcome.ALIVE
        return rep

    def _scan_files(self, tick: int) -> bool:
        """Read the stage at the held gate of tick; True when the tree changed."""
        if self._files is None:
            return False
        tree = self._files.changed()
        self._files_dirty = False
        if tree is not None:
            self._file_log.append((tick, tree))
        return tree is not None

    def file_changes(self) -> list[tuple[int, Any]]:
        """(tick, (rows, {sha256: bytes})) for each gate where the stage's files differed from the gate
        before; the first entry is the tree at tick 0."""
        out, self._file_log = self._file_log, []
        return out

    def _exited(self) -> StepReport:
        """The connection ended during a step: the process is gone. Ticks it finished are in the ring."""
        if self.wire is not None:
            self.wire.close()
            self.wire = None
        self._drain_ring()
        self._open = []
        tick = max(self._last_end, self._tick)
        self._tick = tick
        return StepReport(tick=tick, epoch=self._epoch, server_passes=1, subticks=0,
                          verbclass=VerbClass.GATE | VerbClass.CLOCK | VerbClass.INPUT, outcome=ProcOutcome.EXITED)

    def start_tick_profile(self, limit: int = 4096) -> None:
        """Enable bounded host phase observations for root-owned actual execution."""
        if type(limit) is not int or not 1 <= limit <= 100000:
            raise ValueError("tick profile limit must be an integer from 1 to 100000")
        self._tick_profile = deque(maxlen=limit)

    def tick_profile(self) -> dict[str, Any]:
        """Read observed actual phases; never run a tick or write a receipt."""
        rows = self._tick_profile
        return {"schema": "kptas-host-tick-phases/1", "enabled": rows is not None,
                "rows": list(rows) if rows is not None else [],
                "scope": "gate includes native/wire/ring/report/file scan; ring_drain is nested in gate or sink as split below; file_scan is nested in gate; sink includes full capture/store; action publication is outside; native/frame clocks overlap gate and are diagnostic only"}

    def _gate_timeout(self) -> int:
        timeout = self.cfg.get('shim', {}).get('native_gate_timeout_s', 60)
        if type(timeout) is not int or not 1 <= timeout <= 3600:
            raise BackendError('shim.native_gate_timeout_s must be an integer from 1 to 3600')
        return timeout

    def _render_args(self, args: Mapping[str, Any]) -> Mapping[str, Any]:
        """capture.render_skip and capture.preview_every as step/wait arguments (caps.render_skip).

        Output and GPU work only: never part of ENVP, the journal or the determinism key. Archival
        frames (capture.frames) read every tick, so they force preview_every = 1. A shim without
        the capability gets nothing and renders every frame."""
        cap = self.cfg.get("capture", {})
        mode = str(cap.get("render_skip", "off"))
        every = 1 if cap.get("frames", "off") != "off" else int(cap.get("preview_every", 1))
        if (mode == "off" and every == 1) or not self.hello.get("caps", {}).get("render_skip"):
            return args
        return {**args, "render_skip": mode, "preview_every": every}

    def _one(self, verb: str, args: Mapping[str, Any]) -> StepReport:
        if verb in ("step", "wait"):
            args = self._render_args(args)
        timeout = self._gate_timeout() if verb == 'step' else 0
        try:
            res = self._call(verb, args, timeout_s=timeout)
        except WireRequestTimeout as e:
            if verb == 'step':
                self.poisoned = True
                self._native_step_unknown = str(e)
                raise BackendError(f'native step outcome unknown: {e}; execution poisoned; do not retry') from e
            raise
        except (BackendError, OSError) as e:
            if not _gone(e):
                raise
            return self._exited()
        rep = self._report(res)
        if rep.outcome is ProcOutcome.EXITED:
            # the reply names the tick that was running; the last one with a gate (and a record) is before it
            self._drain_ring()
            self._open = []
            rep = dataclasses.replace(rep, tick=self._last_end if self._last_end >= 0 else rep.tick)
            self._tick = rep.tick
            return rep
        if self._tick_profile is None:
            self._scan_files(rep.tick)
        else:
            started = time.perf_counter_ns()
            try:
                self._scan_files(rep.tick)
            finally:
                self._profile_file_scan_ns = time.perf_counter_ns() - started
        return rep

    @staticmethod
    def _merge(reps: Sequence[StepReport]) -> StepReport:
        vc, evs = VerbClass.NONE, []
        for r in reps:
            vc |= r.verbclass
            evs.extend(r.events)
        return dataclasses.replace(reps[-1], verbclass=vc, events=tuple(evs))

    def step(self, n: int, note: Any = None, first: bool = True, *,
             on_report: Callable[[StepReport, bool], None] | None = None,
             capture_last: bool = True) -> StepReport:
        """Run n ticks holding note. first=False: the note's first tick already ran (a resumed note).
        With capture.every > 1 the shim captures ticks on the every-tick grid and the step's last tick;
        capture_last=False leaves the last tick off unless the grid has it (a caller that steps on at
        once). A held gate that was not captured has no root and refuses restore."""
        args = note_args(note)
        if not self.input_source_engaged:
            prepared = self._physical_prepared
            if (n != 1 or first is not True or prepared is None or
                    args != prepared['args'] or self._msgs or
                    getattr(note, 'osev', None) != prepared['osev']):
                raise BackendError('physical source requires its exact prepared one-frame Note')
            args['physical_ticket'] = prepared['ticket']
            self._physical_prepared = None
        msgs = [*self._msgs, *([str(note.osev)] if note is not None and note.osev and first else [])]
        self._msgs = []
        if n <= 0:
            rep = self._one("step", {**args, "n": 0})
            if on_report is not None:on_report(rep, first)
            return rep
        per_tick = self._files is not None or on_report is not None
        if per_tick and self._stream_ok():
            return self._step_stream(n, args, msgs, first, on_report, capture_last)
        every = self._capture_every()
        reps: list[StepReport] = []
        left = n
        while left > 0:
            k = 1 if per_tick else left
            a = {**args, "n": k, "first": first and not reps}
            if msgs and not reps:
                a["msgs"] = msgs
            if every > 1:
                # Cadence: the shim captures tick % every == 0, and the last tick of this step only:
                # the per-tick sub-commands in between hold their gates uncaptured.
                a["capture_every"] = every
                a["capture_phase"] = self.capture_offset % every
                if left - k > 0 or not capture_last:
                    a["capture_last"] = False
            profile = self._tick_profile
            started = time.perf_counter_ns() if profile is not None else None
            if profile is not None:
                self._profile_file_scan_ns = 0
                self._profile_ring_ns = 0
                self._profile_ring_calls = 0
                self._profile_ring_records = 0
                self._profile_ring_post_ns = 0
                self._profile_ring_post_records = 0
                self._profile_native_tick = None
                self._profile_sink_phases = None
            rep = self._one("step", a)
            gate_end = time.perf_counter_ns() if profile is not None else None
            gate_ring_ns = self._profile_ring_ns if profile is not None else None
            failed = True
            try:
                if on_report is not None:on_report(rep, bool(a["first"]))
                self._last_reported = rep.tick
                failed = False
            finally:
                if profile is not None:
                    ended = time.perf_counter_ns()
                    native = self._profile_native_tick
                    native = native if native is not None and native.get("tick") == rep.tick else None
                    frame = None
                    sink = self.frames
                    if sink is not None:
                        present = sink.presentation_frame(self.frames_epoch, rep.tick)
                        if present is not None:
                            _seq, meta, _pixels = present
                            if meta.get("tick") == rep.tick and meta.get("epoch") == self.frames_epoch:
                                frame = {k: meta[k] for k in ("tick", "epoch", "reads", "read_us", "finish_us", "swaps", "flags") if k in meta}
                    profile.append({"tick": rep.tick, "epoch": rep.epoch,
                                    "gate_and_files_ns": gate_end - started,
                                    "file_scan_ns": self._profile_file_scan_ns,
                                    "ring_drain_ns": self._profile_ring_ns,
                                    "gate_ring_drain_ns": gate_ring_ns,
                                    "sink_ring_drain_ns": self._profile_ring_ns - gate_ring_ns,
                                    "ring_drain_calls": self._profile_ring_calls,
                                    "ring_records": self._profile_ring_records,
                                    "ring_drain_post_reply_ns": self._profile_ring_post_ns,
                                    "ring_records_post_reply": self._profile_ring_post_records,
                                    "native_tick": native, "presentation": frame,
                                    "sink_phases": (dict(self._profile_sink_phases)
                                        if self._profile_sink_phases is not None
                                        and self._profile_sink_phases.get('native_tick') == rep.tick
                                        and self._profile_sink_phases.get('native_epoch') == rep.epoch else None),
                                    "capture_sink_ns": ended - gate_end,
                                    "total_ns": ended - started, "capture_failed": failed})
            reps.append(rep)
            left -= k
            if rep.outcome is ProcOutcome.EXITED:
                break
        return self._merge(reps)

    # ------------------------------------------------------------------ streamed steps

    def _beat_start(self) -> None:
        """A thread bumps the ring heartbeat while a streamed step runs, also through a long
        sink call on this thread: the shim's ring wait holds the game instead of failing."""
        if self._beat is not None and self._beat[0].is_alive():
            return
        import threading
        stop = threading.Event()

        def beat() -> None:
            while not stop.wait(0.5):
                ring = self.ring
                if ring is None:
                    return
                if self._streaming:
                    try:
                        ring.beat()
                    except (ValueError, OSError, BackendError):
                        return
        thread = threading.Thread(target=beat, name="kptas-ring-beat", daemon=True)
        thread.start()
        self._beat = (thread, stop)

    def _stream_ok(self) -> bool:
        """The shim has the complete file signal (caps.step_stream), shim.stream_steps allows
        streaming, and the input is authored (a physical frame is one prepared tick anyway)."""
        return (self.hello.get("caps", {}).get("step_stream") == 1 and self.input_source_engaged
                and self.cfg.get("shim", {}).get("stream_steps", True) is not False)

    def _step_stream(self, n: int, args: Mapping[str, Any], msgs: list[str], first: bool,
                     on_report: Callable[[StepReport, bool], None] | None,
                     capture_last: bool) -> StepReport:
        """Run n ticks as streamed native steps (caps.step_stream).

        One request runs its ticks without holding a gate between them; every gate pushes
        its report (step.tick). The host reports a tick (on_report) once the next push or
        the reply shows its gate has passed, while the game runs on. Each report carries
        exactly what a one-tick step's would: the tick's own events and verbclass, its
        own ring batch (records stops at its TICK_END), and the GL spools published up
        to it, admitted before the tick is appended; a per-digest blob waits for the first
        reported gate whose push lists it.

        Run-ahead is bounded by the staging ring: the host reads it no further than the
        batch after the newest push it has taken, so a slow report fills the ring and the
        native waits at its next record (as the frame ring makes it wait for a slot).
        Nothing is dropped. The native's 60 s ring wait goes on while the connection is
        open and the heartbeat thread (_beat_start) still beats; a host that stops beating
        for 60 s still ends the game.

        The stage files are read only at a held gate. A tick that made a file-mutating
        call ends the request at its own gate (stopped "files"); the files are read there
        and the rest of n continues in a new request with first=false. The last gate is
        held and read as a one-tick step's is. A change no tick signalled (only a write
        from outside the game) is logged and dated at the held gate. A put_files before
        the step makes its first request one tick long, so that write is read where a
        one-tick step would read it. Capture cadence is unchanged: a files stop is not
        the step's end, so its gate is captured only on the capture.every grid."""
        every = self._capture_every()
        reps: list[StepReport] = []
        left = n
        while left > 0:
            k = 1 if (self._files is not None and self._files_dirty) else left
            a = {**args, "n": k, "first": first and not reps, "stream": True}
            if msgs and not reps:
                a["msgs"] = msgs
            if every > 1:
                a["capture_every"] = every
                a["capture_phase"] = self.capture_offset % every
                if left - k > 0 or not capture_last:
                    a["capture_last"] = False
            ran, ended = self._stream_request(a, k, first, on_report, reps)
            left -= ran
            if ended:
                break
        return self._merge(reps)

    def _stream_request(self, args: Mapping[str, Any], k: int, first: bool,
                        on_report: Callable[[StepReport, bool], None] | None,
                        reps: list[StepReport]) -> tuple[int, bool]:
        """One streamed native step of k ticks. Returns (ticks run, the process ended)."""
        args = self._render_args(args)
        self._admit("step", args)
        timeout = self._gate_timeout()
        wire = self.wire
        assert wire is not None
        was_dirty = self._files_dirty
        profile = self._tick_profile
        state: dict[str, Any] = {"failure": None, "failed_tick": None,
                                 "mark": time.perf_counter_ns() if profile is not None else 0,
                                 "scan_ns": 0, "pushed": -1}

        def report(rep: StepReport) -> None:
            """on_report for one tick, with records limited to that tick's batch."""
            if state["failure"] is not None:
                reps.append(rep)
                return
            started = 0
            if profile is not None:
                started = time.perf_counter_ns()
                self._profile_native_tick = None
                self._profile_sink_phases = None
            self._emit_limit = rep.tick if rep.outcome is ProcOutcome.ALIVE else None
            failed = True
            try:
                if on_report is not None:
                    on_report(rep, first and not reps)
                self._last_reported = rep.tick
                failed = False
            except Exception as e:      # keep draining: the native finishes the request regardless
                state["failure"], state["failed_tick"] = e, rep.tick
            finally:
                self._emit_limit = None
                if profile is not None:
                    ended = time.perf_counter_ns()
                    phases = self._profile_sink_phases
                    profile.append({"tick": rep.tick, "epoch": rep.epoch, "stream": True,
                                    "wait_ns": started - state["mark"],
                                    "file_scan_ns": state["scan_ns"],
                                    "ring_drain_ns": self._profile_ring_ns,
                                    "ring_drain_calls": self._profile_ring_calls,
                                    "ring_records": self._profile_ring_records,
                                    "lag_ticks": max(0, state["pushed"] - rep.tick),
                                    "sink_phases": (dict(phases) if phases is not None
                                                    and phases.get('native_tick') == rep.tick
                                                    and phases.get('native_epoch') == rep.epoch else None),
                                    "capture_sink_ns": ended - started,
                                    "total_ns": ended - state["mark"], "capture_failed": failed})
                    state["mark"], state["scan_ns"] = ended, 0
                    self._profile_ring_ns = self._profile_ring_calls = self._profile_ring_records = 0
            reps.append(rep)

        def emit(push: Mapping[str, Any]) -> None:
            self._drain_ring()                  # the gate's batch was on the ring before its push
            names = push.get("gl_names")
            if names:
                self._gl_announced.update(str(n) for n in names)
            self._gl_admit_all = bool(push.get("gl_all"))
            try:
                report(self._report(push["report"]))
            finally:
                self._gl_admit_all = False

        def finish() -> None:
            """Re-raise a reporting failure; ticks played past it were never recorded."""
            e = state["failure"]
            if e is None:
                return
            if self._tick > state["failed_tick"]:
                self.poisoned = True
                self._native_step_unknown = (f"reporting tick {state['failed_tick']} of a streamed step failed "
                                             f"while the game ran on to tick {self._tick}, unrecorded")
            raise e

        try:
            rid = wire.begin("step", args)
        except (BackendError, OSError) as e:
            if not _gone(e):
                raise
            report(self._exited())
            finish()
            return 0, True
        self._streaming = True
        wire.stream_id = rid
        self._drain_upto = self._last_end
        self._beat_start()
        pending: dict[str, Any] | None = None   # the last push: its gate may be the held one
        ran = 0
        reply: dict[str, Any] | None = None
        gone = False
        settled = False                          # the request ended where this host can account for it

        def advance(msg: Mapping[str, Any]) -> None:
            nonlocal pending, ran, deadline
            if pending is not None:
                if pending.get("held"):
                    raise BackendError("streamed step: a gate ran after the held gate")
                emit(pending)
            if msg.get("files") and not msg.get("held"):
                raise BackendError("streamed step: a gate with file-mutating calls was not held")
            pending, ran = dict(msg), ran + 1
            state["pushed"] = int(msg["report"]["tick"])
            self._drain_upto = max(self._drain_upto if self._drain_upto is not None else -1, state["pushed"])
            # progress: the gate deadline counts native time, not this host's reporting
            deadline = time.monotonic() + timeout

        deadline = time.monotonic() + timeout
        try:
            try:
                while True:
                    msg = wire._early.pop(rid, None)
                    if msg is None:
                        left_s = deadline - time.monotonic()
                        # past the deadline, still read what the socket holds before deciding
                        msg = wire.recv(min(left_s, 0.002) if left_s > 0 else 0.001)
                        if msg is None:
                            if left_s <= 0:
                                why = f"step: no gate within {timeout} s"
                                self._native_step_unknown = why
                                raise BackendError(f"native step outcome unknown: {why}; execution poisoned; "
                                                   "do not retry")
                            self._wire_wait_idle()
                            continue
                    if "push" in msg:
                        if msg.get("push") == "step.tick" and msg.get("id") == rid:
                            advance(msg)
                        else:
                            wire.pushes.append(msg)
                            self._drain_pushes()
                        self._wire_wait_idle()
                        continue
                    if msg.get("id") != rid:
                        if isinstance(msg.get("id"), int):
                            wire._early[msg["id"]] = msg
                        continue
                    reply = msg
                    break
            except (BackendError, OSError) as e:
                if not _gone(e):
                    raise
                gone = True
            self._streaming = False
            wire.stream_id = None
            self._drain_upto = None             # held or gone: nothing more is written
            self._drain_pushes()
            if gone:
                # Pushed gates were played and recorded on the ring; a last TICK_END without a
                # push is reported by the exit report, which takes every record left (as _one's).
                if pending is not None:
                    emit(pending)
                report(self._exited())
                settled = True
                finish()
                return ran, True
            assert reply is not None
            if not reply.get("ok"):
                # The gate pushed last was not held, so it made no file-mutating call: report
                # it as its one-tick step's reply would have been, then refuse as the shim did.
                if pending is not None:
                    emit(pending)
                settled = True
                finish()
                err = reply.get("error") or {}
                code, text = str(err.get("code", "internal")), str(err.get("message", ""))
                if code == "refused":
                    raise RefusedError(f"step: {text}")
                if code == "journal_incomplete":
                    self._poison_journal(WireError(code, f"step: {text}"))
                raise WireError(code, f"step: {text}")
            res = reply.get("result") or {}
            if res.get("outcome") == ProcOutcome.EXITED.value:
                if pending is not None:
                    emit(pending)
                self._drain_ring()
                rep = self._report(res)
                self._open = []
                rep = dataclasses.replace(rep, tick=self._last_end if self._last_end >= 0 else rep.tick)
                self._tick = rep.tick
                report(rep)
                settled = True
                finish()
                return ran, True
            if pending is None or not pending.get("held") or pending["report"].get("tick") != res.get("tick"):
                raise BackendError("streamed step: the reply is not for the held gate of its last push")
            stopped = res.get("stopped")
            left = res.get("left", 0)
            if (stopped not in (None, "files") or type(left) is not int or left < 0
                    or (stopped is None and left) or ran + left != k):
                raise BackendError(f"streamed step: {ran} gates pushed and {left} left do not make the {k} requested")
            # The game is held at this gate: read the stage files as a one-tick step does.
            started = time.perf_counter_ns() if profile is not None else 0
            changed = self._scan_files(int(res["tick"]))
            if profile is not None:
                state["scan_ns"] = time.perf_counter_ns() - started
            if changed and not pending.get("files") and not was_dirty:
                self.logs.append({"push": "log", "level": "warn", "tick": int(res["tick"]),
                                  "msg": "stage files changed with no file-mutating game call in the streamed "
                                         "ticks; the change is dated at this held gate"})
            emit(pending)
            settled = True
            finish()
            return ran, False
        finally:
            self._streaming = False
            wire.stream_id = None
            self._drain_upto = None
            if not settled:
                # Left before the native's ticks were accounted for: later batches would be
                # appended under the wrong reports. Only cleanup requests are admitted now.
                self.poisoned = True
                if getattr(self, '_native_step_unknown', None) is None:
                    self._native_step_unknown = (f"a streamed step stopped unaccounted after tick "
                                                 f"{state['pushed']}")

    def _capture_every(self) -> int:
        """capture.every when the shim advertises the cadence protocol, else 1 (capture every tick)."""
        every = int(self.cfg.get("capture", {}).get("every", 1))
        if every <= 1 or self.capture_force_every_tick:
            return 1
        if not self.hello.get("caps", {}).get("capture_cadence"):
            raise BackendError("capture.every > 1 needs a shim with caps.capture_cadence")
        return every

    def wait(self, event: str, arg: str | None, timeout_ticks: int, note: Any = None, *,
             on_report: Callable[[StepReport, bool], None] | None = None) -> StepReport:
        """Run ticks holding note (nothing by default) until event fires or timeout_ticks pass. Every tick
        of a wait carries WAIT. proc.down waits for the process to exit. The host-side per-tick loop
        below learns where the wait ends only after the tick, so it captures every tick; the native
        wait verb captures with the cadence and always at the gate where it ends."""
        args = note_args(note)
        if self._files is None and event != "proc.down" and on_report is None:
            every = self._capture_every()
            if every > 1:
                args = {**args, "capture_every": every, "capture_phase": self.capture_offset % every}
            res = self._one("wait", {**args, "event": event, "arg": arg, "timeout": timeout_ticks})
            self.last_waited = res.outcome is ProcOutcome.ALIVE and any(e.name == event for e in res.events)
            return dataclasses.replace(res, verbclass=res.verbclass | VerbClass.WAIT)
        reps: list[StepReport] = []
        hit = False
        for i in range(max(0, timeout_ticks)):
            rep = self._one("step", {**args, "n": 1, "first": i == 0})
            if on_report is not None:on_report(dataclasses.replace(rep,verbclass=rep.verbclass|VerbClass.WAIT),i==0)
            reps.append(rep)
            if rep.outcome is ProcOutcome.EXITED:
                hit = event == "proc.down"
                break
            if any(e.name == event and (arg is None or e.arg == arg) for e in rep.events):
                hit = True
                break
        if not reps:
            rep=self._one("step", {**args, "n": 0})
            if on_report is not None:on_report(dataclasses.replace(rep,verbclass=rep.verbclass|VerbClass.WAIT),True)
            reps.append(rep)
        self.last_waited = hit
        rep = self._merge(reps)
        return dataclasses.replace(rep, verbclass=rep.verbclass | VerbClass.WAIT)

    def input(self, note: Any) -> int:
        return int(self._call("input", note_args(note))["for_tick"])

    def supports_input_source(self) -> bool:
        return self.name == 'shim' and self.hello.get('input_source_protocol') == SOURCE_PROTOCOL

    def set_engaged(self, engaged: bool) -> dict[str, Any]:
        if type(engaged) is not bool or not self.supports_input_source():
            raise BackendError('acknowledged input source protocol unavailable')
        # A source request may take effect even if its reply is lost. Never
        # retain a candidate prepared under the previous source after dispatch.
        self._physical_prepared = None
        response = self._call('input.source', {'engaged': engaged, 'at_tick': self._tick})
        try:
            SourceAcknowledgement.decode(response, tick=self._tick, engaged=engaged)
        except ValueError as exc:
            raise BackendError(str(exc)) from exc
        self.input_source_engaged = engaged
        self._physical_prepared = None
        return response

    def prepare_physical_note(self):
        """Observe without releasing the gate; owner persists before normal step."""
        from kptas.score.ast import Note
        from kptas.score.keys import KEYS
        if self.input_source_engaged or not self.supports_input_source():
            raise BackendError('physical source is not active')
        # Preparation replaces the native ticket. A lost/malformed reply must
        # not leave an older host candidate looking reusable.
        self._physical_prepared = None
        response = self._call('input.physical.prepare', {'at_tick': self._tick})
        try:
            candidate = PhysicalCandidate.decode(response, tick=self._tick)
        except ValueError as exc:
            raise BackendError(str(exc)) from exc
        names = {scan: name for name, (_vk, scan) in KEYS.items()}
        if any(key not in names for key in candidate.keys):
            raise BackendError('physical input contains a key unsupported by the canonical score')
        note = Note(1, held=tuple(names[key] for key in candidate.keys),
                    buttons=candidate.buttons, mouse_abs=candidate.absolute,
                    wheel=candidate.wheel, osev=candidate.osev, source='<physical>')
        self._physical_prepared = {'ticket': candidate.ticket, 'args': note_args(note), 'osev': candidate.osev}
        return note

    def gate(self, on: bool) -> dict[str, Any]:
        return self._call("gate", {"on": on})

    # ------------------------------------------------------------------ capture and restore

    def capture(self, kind: Literal["dirty", "full"], enabled: bool = True) -> dict[str, Any]:
        cap = self.cfg.get("capture", {})
        self._ensure_ring()
        args = {"enabled": enabled, "tracker": cap.get("tracker", "auto"),
                "classes": list(cap.get("sim_classes", ())), "sweep": kind == "full"}
        advertised = self.hello.get("caps", {}).get("gl_transport")
        if advertised is not None:
            if advertised != "kptas-gl-spool/1":
                raise BackendError("unsupported native GL capture transport")
            args["gl_transport"] = advertised
        result = self._call("capture", args)
        if advertised is not None and result.get("gl_transport") != advertised:
            raise BackendError("native GL capture transport was not acknowledged")
        self._gl_transport = advertised
        return result

    def records(self) -> Iterator[StagingRecord]:
        if self.ring is None:
            raise BackendError("not attached")
        self._drain_ring()
        # A streamed step reports one tick at a time while later batches are already
        # here: stop after the reported tick's TICK_END.
        limit = self._emit_limit
        while self._pending:
            rec = self._pending.popleft()
            yield rec
            if limit is not None and rec.kind is RecordKind.TICK_END and rec.tick >= limit:
                return

    def admit_roots(self, roots: Sequence[bytes]) -> bytes:
        """Hand the shim the roots tas restores may target besides its own captures (restore.admit): the
        source bundle's recorded roots. Once per process; returns the set's sha256, which the shim journals
        with every restore it admits."""
        assert self.session_dir is not None
        if self.admitted is not None:
            raise BackendError("this process already has its admitted roots")
        rs = sorted(set(roots))
        if any(len(r) != 32 for r in rs):
            raise BackendError("admitted roots must be 32 bytes")
        blob = b"".join(rs)
        path = self.session_dir / "admitted.roots"
        path.write_bytes(blob)
        digest = sha256(blob)
        try:
            res = self._call("restore.admit", {"path": win_path(path, self._wine), "n": len(rs),
                                               "sha256": digest.hex()}, timeout_s=60)
        finally:
            path.unlink(missing_ok=True)
        if res.get("sha256") != digest.hex() or int(res.get("n", -1)) != len(rs):
            raise BackendError(f"the shim admitted {res.get('n')} roots as {res.get('sha256')}, "
                               f"not {len(rs)} as {digest.hex()}")
        self.admitted = digest
        return digest

    def can_admit(self) -> bool:
        return bool(self.hello.get("caps", {}).get("restore_admit"))

    def restore_admitted(self, tick: int, root: bytes) -> bool:
        """Whether the shim would take root as a restore target (nothing is read or written)."""
        if not self.can_admit():
            return True
        return bool(self._call("restore", {"tick": tick, "root": root.hex(), "check": True}).get("admitted"))

    def restore(self, tick: int, root: bytes, pages: Iterable[StagingRecord]) -> Receipt:
        """Write the page set W (a restore set file in the session directory) and let the shim verify the
        whole sim set against root. Put the tick's files first (put_files). In tas the shim refuses a root it
        neither captured nor admitted (RefusedError, nothing written); a restore that does not verify there
        leaves native execution poisoned until GL reconstruction and the final
        native audit commit. Its successful memory-stage receipt has level NONE;
        only the completed public seek can certify FULL_ROOT."""
        self._frame_mirror_valid = False
        if getattr(self, 'frames', None) is not None:
            self.frames.invalidate_preview()
        assert self.session_dir is not None
        self._seq += 1
        path = self.session_dir / f"restore-{self._seq:06d}.rec"
        n = write_records(path, pages)
        if not self.simgame:
            self.poisoned = True
        t0 = time.perf_counter_ns()
        try:
            res = self._call("restore", {"tick": tick, "root": root.hex(), "path": win_path(path, self._wine)},
                             timeout_s=0)
        finally:
            path.unlink(missing_ok=True)
        # The native gate is stopped here: every spool it published so far is
        # complete (.kpgs.tmp is renamed before its capture returns).
        self._gl_restore_spools = self._gl_spool_names()
        us = int(res.get("us", (time.perf_counter_ns() - t0) // 1000))
        memory_stage = not self.simgame
        self.poisoned = True if memory_stage else bool(res.get("poisoned", False))
        phase_ok = (res.get("memory_verified") is True and res.get("verified") is False
                    and res.get("external_pending") is True and res.get("poisoned") is True) if memory_stage else res.get("verified") is True
        ok = (phase_ok and res.get("root") == root.hex()
              and type(res.get("tick")) is int and res["tick"] == tick)
        from_tick, self._tick = self._tick, int(res.get("tick", tick))
        if ok:
            # Restore establishes a completed gate without a played TICK_END.
            # An immediate exit must report that gate, not the process's old boot tick.
            self._last_end = self._tick
        return Receipt(kind="seek", result="pass" if ok else "fail", from_tick=from_tick,
                       to_tick=int(res.get("tick", tick)), mode=SeekMode.RESTORE,
                       verify_level=VerifyLevel.NONE if memory_stage else VerifyLevel.FULL_ROOT,
                       write_pages=int(res.get("pages", n)), micros=us,
                       state_root=str(res.get("root", "")),
                       verifier=self.name + ("; memory verified; external reconstruction pending" if memory_stage and ok else ""))

    def gl_blobs(self) -> list[tuple[str, bytes]]:
        """GL descriptors and image data the shim wrote since the last call (<session>/gl/<sha256>). Each is
        content-addressed; the sink keeps them in the bundle so a restore can rebuild the driver's objects."""
        d = self.session_dir / "gl" if self.session_dir is not None else None
        if d is None or not d.is_dir():
            return []
        out = []
        # Admitted spools stay in the directory until a durable checkpoint consumes
        # them, so the listing carries that backlog every tick. Compare plain path
        # strings and build a Path only for files not yet admitted. Sorting names
        # within one directory is the order sorted(d.iterdir()) gave.
        base = str(d)
        admitted = {str(p) for p, (_tick, _keys, ok) in self._gl_spools_pending.items() if ok}
        with os.scandir(base) as entries:
            names = sorted(e.name for e in entries)
        for name in names:
            if os.path.join(base, name) in admitted:
                continue
            p = d / name
            if p.name.startswith('frame-') and p.name.endswith('.kpgs'):
                if self._gl_transport != 'kptas-gl-spool/1':
                    raise BackendError('GL frame spool was not negotiated')
                if (self._emit_limit is not None and name not in self._gl_restore_spools
                        and self._spool_tick(p) > self._emit_limit):
                    # Streamed step: a captured gate the host has not reported yet. Its
                    # spool is admitted with that tick, before the tick is appended.
                    continue
                tick, blobs = self._read_gl_spool(p, name in self._gl_restore_spools)
                out.extend(blobs)
                self._gl_spools_pending[p] = (tick, tuple(key for key, _ in blobs), False)
                continue
            if len(p.name) != 64:
                continue
            if self._emit_limit is not None and not self._gl_admit_all and name not in self._gl_announced:
                # Streamed step: published after the reported gate; its gate's push lists it.
                continue
            data = p.read_bytes()
            try:
                verified = _verified_file_blob(p.name, data)
            except (TypeError, ValueError):
                raise BackendError(f"GL blob {p.name} does not match its sha256") from None
            out.append(verified)
            if self._gl_transport == 'kptas-gl-spool/1':
                # Other GL producers and oversized textures retain individual
                # files. In the new transport they share its durable cleanup.
                self._gl_spools_pending[p] = (self._tick, (verified[0],), False)
            else:
                p.unlink()
        return out

    @staticmethod
    def _spool_tick(path: Path) -> int:
        """The tick in a published spool's header; -1 when it is unreadable (the full read reports why)."""
        try:
            with path.open('rb') as stream:
                header = stream.read(32)
        except OSError:
            return -1
        if len(header) != 32 or header[:8] != b'KPGS0001':
            return -1
        return struct.unpack_from('<Q', header, 24)[0]

    def _gl_spool_names(self) -> frozenset[str]:
        d = self.session_dir / "gl" if self.session_dir is not None else None
        if d is None or not d.is_dir():
            return frozenset()
        with os.scandir(d) as entries:
            return frozenset(e.name for e in entries
                             if e.name.startswith('frame-') and e.name.endswith('.kpgs'))

    def _read_gl_spool(self, path: Path, before_restore: bool = False) -> tuple[int, list[tuple[str, bytes]]]:
        """Validate a complete transport; descriptor and holorecord bytes stay unchanged.

        before_restore: the file was already published when the last restore returned.
        It may come from ticks the restore abandoned (a canary drive past the anchor).
        Its blobs are content-addressed and the native never emits them again, so they
        are admitted, and the file is filed at the restored gate for durable cleanup."""
        limit = 256 << 20
        if path.is_symlink() or not 100 <= path.stat().st_size <= limit:
            raise BackendError(f'invalid GL frame spool size or identity: {path.name}')
        out = []
        with path.open('rb') as stream:
            header = stream.read(32)
            if len(header) != 32:
                raise BackendError(f'invalid GL frame spool header: {path.name} is shorter than its header')
            magic, version, header_size, sequence, tick = struct.unpack('<8sIIQQ', header)
            if magic != b'KPGS0001' or version != 1 or header_size != 32:
                raise BackendError(f'invalid GL frame spool header: {path.name} has magic {magic!r}, '
                                   f'version {version}, header size {header_size}')
            if sequence == 0 or path.name != f'frame-{sequence:016x}.kpgs':
                raise BackendError(f'invalid GL frame spool header: {path.name} carries sequence {sequence:#x}')
            if tick > self._tick:
                if not before_restore:
                    raise BackendError(f'invalid GL frame spool header: {path.name} is for tick {tick}, '
                                       f'after the live tick {self._tick}')
                tick = self._tick
            size = os.fstat(stream.fileno()).st_size
            if not 100 <= size <= limit:
                raise BackendError('GL frame spool size changed before admission')
            stream.seek(size - 32)
            footer = stream.read(32)
            end, declared_size, count, reserved, end_sequence = struct.unpack('<8sQIIQ', footer)
            if (end != b'KPGSEND1' or declared_size != size or reserved != 0
                    or sequence != end_sequence or not 1 <= count <= 8192):
                raise BackendError('incomplete GL frame spool')
            stream.seek(32)
            keys = set()
            for _ in range(count):
                entry = stream.read(36)
                if len(entry) != 36:
                    raise BackendError('truncated GL frame spool entry')
                key, length = struct.unpack('<32sI', entry)
                if length > 64 << 20 or length > size - 32 - stream.tell() or key in keys:
                    raise BackendError('invalid GL frame spool entry length or duplicate')
                data = stream.read(length)
                if len(data) != length:
                    raise BackendError('GL frame spool payload does not match its sha256')
                # One fresh sha256 per payload. The private receipt lets the bundle
                # admit these exact bytes without hashing them a second time.
                try:
                    verified = _verified_file_blob(key.hex(), data)
                except (TypeError, ValueError):
                    raise BackendError('GL frame spool payload does not match its sha256') from None
                keys.add(key)
                out.append(verified)
            if stream.tell() != size - 32 or stream.read(32) != footer or stream.read(1):
                raise BackendError('GL frame spool framing changed or has trailing bytes')
        return tick, out

    def gl_spools_admitted(self, contains: Callable[[bytes], bool]) -> None:
        """Called after successful sink storage; failure leaves blobs retryable."""
        for path, (tick, keys, admitted) in tuple(self._gl_spools_pending.items()):
            if not admitted and all(contains(bytes.fromhex(key)) for key in keys):
                self._gl_spools_pending[path] = (tick, keys, True)

    def gl_spools_consumed(self, durable_tick: int, contains: Callable[[bytes], bool]) -> None:
        """Only the serialized sink calls this after its pack/journal/directory barrier."""
        for path, (tick, keys, _admitted) in tuple(self._gl_spools_pending.items()):
            if tick > durable_tick or not all(contains(bytes.fromhex(key)) for key in keys):
                continue
            try:
                path.unlink()
            except OSError:
                # Cleanup is retryable; stored evidence has already crossed the
                # barrier. Never turn a successful durable frame into failure.
                continue
            del self._gl_spools_pending[path]

    def gl_sync(self, blob: Callable[[bytes], bytes]) -> dict[str, Any]:
        """After a restore, at the same gate: the shim lists the GL descriptors its driver lacks for the
        restored tick (textures, ARB programs, context state); they go back with their blobs, context last,
        and the shim rebuilds, reads back and checks each one."""
        assert self.session_dir is not None
        # Any failed or incomplete native reconstruction must remain unusable.
        if not self.simgame:
            self.poisoned = True
        res = self._call("gl.need", {})
        if res.get("ok") is not True or "error" in res or not isinstance(res.get("need"), list):
            raise BackendError(f"gl.need did not verify: {res}")
        try:
            need = [bytes.fromhex(h) for h in res["need"]]
            if any(len(key) != 32 for key in need):
                raise ValueError("descriptor keys must contain 32 bytes")
        except (TypeError, ValueError) as e:
            raise BackendError(f"invalid gl.need descriptor keys: {e}") from None
        descs = [(k, blob(k)) for k in need]
        descs.sort(key=lambda d: {GL_CTX_MAGIC: 1, GL_FRAMEBUFFER_MAGIC: 2}.get(d[1][:4], 0))
        self._seq += 1
        path = self.session_dir / f"gl-{self._seq:06d}.set"
        try:
            with open(path, "wb") as f:
                for key, desc in descs:
                    recs = [(k, blob(k)) for k in gl_children(desc)] + [(key, desc)]
                    if any(hashlib.sha256(data).digest() != k for k, data in recs):
                        raise BackendError("GL restore blob does not match its sha256")
                    if desc[:4] == GL_FRAMEBUFFER_MAGIC:
                        validate_framebuffer(desc, [data for _, data in recs[:-1]])
                    f.write(struct.pack("<II", GL_GROUP, len(recs)))
                    for k, data in recs:
                        f.write(struct.pack("<II", GL_RECORD, len(data)) + k + data + bytes(-len(data) % 4))
            # An empty descriptor set still reconciles deleted/arena-backed objects.
            res = self._call("gl.apply", {"path": win_path(path, self._wine)}, timeout_s=0)
            fields = ("rebuilt", "deleted", "missing", "verify_bad", "groups")
            if (res.get("ok") is not True or "error" in res
                    or any(type(res.get(k)) is not int or res[k] < 0 for k in fields)
                    or res["missing"] or res["verify_bad"]
                    or (not self.simgame and (res.get("verified") is not True
                        or res.get("external_pending") is not False or res.get("poisoned") is not False))):
                raise BackendError(f"gl.apply did not verify: {res}")
            self.poisoned = False
            return res
        finally:
            path.unlink(missing_ok=True)

    def put_files(self, rows: Sequence[Sequence[Any]], blob: Callable[[str], bytes]) -> None:
        """Make the stage's writable files equal a recorded tree, at the gate, before a restore."""
        if self._files is None:
            raise BackendError("this backend does not track the game's files (game.dir or files.store)")
        # The game's file signal cannot see this write: the next step reads the stage
        # after its first tick, as a one-tick step would.
        self._files_dirty = True
        self._files.write(rows, blob)

    def digest(self, regions: Sequence[str]) -> dict[str, str]:
        return self._call("digest", {"regions": list(regions)})

    def snap(self, kind: Literal["digest", "full", "semantic"]) -> dict[str, Any]:
        assert self.session_dir is not None
        self._seq += 1
        return self._call("snap", {"kind": kind, "path": win_path(self.session_dir / f"snap-{self._seq:06d}.bin",
                                                                  self._wine)})

    def verify(self, level: VerifyLevel | str, expect_root: bytes | None = None) -> dict[str, Any]:
        return self._call("verify", {"level": str(level), "expect_root": _hex(expect_root)})

    # ------------------------------------------------------------------ memory, semantic, files

    def read(self, addr: int, length: int) -> bytes:
        if not 0 <= length <= 65536:
            raise BackendError("mem.read length must be 0..65536")
        return base64.b64decode(self._call("mem.read", {"addr": addr, "len": length})["data_b64"])

    def write(self, addr: int, data: bytes) -> None:
        self._call("mem.write", {"addr": addr, "data_b64": base64.b64encode(data).decode()})

    def semantic(self, op: Literal["read", "apply"], fields: Any) -> Mapping[str, Any]:
        if op == "read":
            return self._call("semantic.read", {"fields": list(fields)}).get("values", {})
        if op == "apply":
            self._call("semantic.apply", {"fields": dict(fields)})
            return {}
        raise BackendError(f"semantic op must be read or apply, not {op!r}")

    def files(self, op: Literal["snap", "put"], blob: bytes | None = None) -> bytes | None:
        """The stage's writable files. With the stage tracked here they are read and written directly;
        otherwise the shim's files verb carries them."""
        if self._files is not None:
            if op == "snap":
                return self._files.snapshot()
            if op == "put":
                if self._started and self._tier is not Tier.LAB:
                    raise RefusedError("files put after start is lab only")
                rows, data = decode_snapshot(blob or b'{"entries": []}')
                self._files.write(rows, data.__getitem__)
                return None
            raise BackendError(f"files op must be snap or put, not {op!r}")
        assert self.session_dir is not None
        self._seq += 1
        path = self.session_dir / f"files-{self._seq:06d}.bin"
        if op == "put":
            if self._started and self._tier is not Tier.LAB:
                raise RefusedError("files put after start is lab only")
            path.write_bytes(blob or b"")
            self._call("files", {"op": "put", "path": win_path(path, self._wine)})
            path.unlink(missing_ok=True)
            return None
        if op == "snap":
            self._call("files", {"op": "snap", "path": win_path(path, self._wine)})
            data = path.read_bytes()
            path.unlink(missing_ok=True)
            return data
        raise BackendError(f"files op must be snap or put, not {op!r}")

    # ------------------------------------------------------------------ save length

    def _save_verb(self, verb: str, ms: int) -> None:
        if ms < 0:
            raise BackendError("a save length is >= 0 ms")
        try:
            self._call(verb, {"ms": int(ms)})
        except WireError as e:
            if e.code == "unsupported":
                raise BackendError(f"this shim build ({self.hello.get('shim', '?')}, build "
                                   f"{self.hello.get('build', '?')}) has no {verb}; it needs a newer shim") from None
            raise

    def set_save_ms(self, ms: int) -> None:
        """Virtual length of every in-game save (score header save_ms)."""
        self._save_verb("clock.save_ms", ms)

    def set_next_save_ms(self, ms: int) -> None:
        """Virtual length of the next in-game save only (@save ms=N)."""
        self._save_verb("clock.save_ms_next", ms)

    # ------------------------------------------------------------------ events, os, frame, hud

    def _drain_pushes(self) -> None:
        if self.wire is None:
            return
        while self.wire.pushes:
            p = self.wire.pushes.popleft()
            kind = p.get("push")
            if kind == "integrity" or (kind == "event" and p.get("name") == "integrity"):
                # the shim's code-integrity check halted the game. Latch it now; the next non-cleanup call
                # raises, so a "bye" is never turned into an error. The native lane's push shape is not final.
                self._integrity_halt = _integrity_detail(p)
                self.poisoned = True
                self._native_step_unknown = f"shim integrity halt: {self._integrity_halt}"
                continue
            if kind == "step.tick":
                # Only a streamed step reads its gates; one here was played and never reported.
                self.poisoned = True
                self._native_step_unknown = f"a step.tick push for tick {(p.get('report') or {}).get('tick')} " \
                                            "arrived outside its streamed step"
                raise BackendError(self._native_step_unknown)
            if kind == "event":
                arg = p.get("arg")
                if arg is None and p.get("us") is not None:
                    arg = str(p["us"])
                self._events.append(EngineEvent(p["name"], int(p.get("tick", self._tick)), arg))
            elif kind == "watch.hit":
                self._events.append(EngineEvent("watch.hit", int(p.get("tick", 0)), p.get("id")))
            elif kind == "desync":
                self._events.append(EngineEvent("desync", int(p.get("tick", 0)), json.dumps(p, sort_keys=True)))
            elif kind == "log":
                self.logs.append(p)
            elif kind == "tick":
                self.ticks.append(p)
                if self._tick_profile is not None:
                    self._profile_native_tick = {k: p[k] for k in ("tick", "run_us", "journal_us", "cap", "captured", "skip") if k in p}

    def events(self) -> Iterator[EngineEvent]:
        if self.wire is not None:
            self.wire.poll(0.0)
            self._drain_pushes()
        while self._events:
            yield self._events.popleft()

    def os_event(self, ev: OsEvent) -> ProcOutcome:
        """close: the shim posts WM_CLOSE to the game window at the start of the next tick (os_event verb).
        altf4: the shim sends the keyboard messages of a real Alt+F4 at the start of the next tick, and
        the game's WndProc and DefWindowProc turn them into WM_SYSCOMMAND SC_CLOSE and WM_CLOSE.
        altf4@save: the same keys, held for the first message pump of the next save (UpdateScreen 0x401510).
        Other window events ride on the next step's msgs, which the shim refuses when it lacks them.
        relaunch starts a new process."""
        ev = OsEvent(ev)
        if ev is OsEvent.RELAUNCH:
            if self.link is not None:
                raise BackendError("relaunch belongs to the link owner")
            return self.relaunch()
        if ev in (OsEvent.CLOSE, OsEvent.ALTF4, OsEvent.ALTF4_SAVE):
            return ProcOutcome(self._call("os_event", {"ev": str(ev)}).get("outcome", "alive"))
        self._msgs.append(str(ev))
        return ProcOutcome.ALIVE

    def relaunch(self) -> ProcOutcome:
        """End this process and start the next epoch on the same listener and ring."""
        self._frames_stop()          # the next start() captures the new process into its own epoch
        self.frames_epoch += 1
        if self.wire is not None:
            try:
                self.wire.request("bye", {"exit": True}, timeout_s=5)
            except (BackendError, OSError):
                pass
            self.wire.close()
            self.wire = None
        if self.proc is not None:
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.terminate()
                self.proc.wait(timeout=5)
        tier, self._tier = self._tier, None
        self._started = False
        self._epoch += 1
        assert self._listener is not None
        self._connect(self._listener.getsockname()[1], int(self.cfg.get("shim", {}).get("connect_timeout_s", 30)))
        if tier is not None:
            self.lock_tier(tier)
        if self._clock_args is not None:
            self.clock(*self._clock_args)   # the new process needs its clock request again
        return ProcOutcome.ALIVE

    def watch(self, wid: str, addr: int, length: int, cond: str) -> None:
        self._call("watch", {"id": wid, "addr": addr, "len": length, "cond": cond})

    def unwatch(self, wid: str) -> None:
        self._call("watch.rm", {"id": wid})

    def subscribe(self, topics: Sequence[str]) -> None:
        self._call("subscribe", {"topics": list(topics)})

    def frame(self) -> bytes | None:
        from kptas.frames import png_bytes
        return png_bytes(*self.view_raw())

    def view_raw(self) -> tuple[bytes, int, int]:
        """Read the stopped gate's presented or verified restored frame without a game tick.

        Metadata identifies its source and capture tick. Pixels are BGRA, rows bottom-up.
        """
        sink = getattr(self, 'frames', None)
        if (not self.simgame and getattr(self, '_frame_mirror_valid', False)
                and sink is not None and self.execution_health() == 'healthy'):
            mirror = sink.latest_frame(self.frames_epoch, self._tick)
            if mirror is not None:
                seq, meta, data = mirror
                # Fresh ring frames carry a read at this exact local gate.
                # REPEAT has no original capture tick, so latest_frame refuses it.
                self.last_view_metadata = dict(w=meta['w'], h=meta['h'], format='bgra-bottom-up',
                    sha256=hashlib.sha256(data).hexdigest(), gate_tick=self._tick,
                    captured_tick=meta['tick'], blank=False, repeat=False,
                    cropped=bool(meta['flags'] & 4), source='captured_back_buffer')
                # Join diagnostics to the exact frame already admitted above.
                # These reads count graphics readbacks, not input samples.
                present = sink.presentation_frame(self.frames_epoch, self._tick)
                if present is not None:
                    present_seq, present_meta, present_data = present
                    if (present_seq == seq and present_meta.get('epoch') == self.frames_epoch
                            and present_meta.get('tick') == self._tick
                            and present_meta.get('w') == meta['w']
                            and present_meta.get('h') == meta['h']
                            and present_data == data):
                        self.last_view_metadata.update(
                            native_sequence=present_seq, native_epoch=present_meta['epoch'],
                            native_tick=present_meta['tick'])
                        for field in ('swaps', 'reads', 'flags', 'read_us', 'finish_us', 'skipped'):
                            if field in present_meta:
                                self.last_view_metadata[f'native_{field}'] = present_meta[field]
                return data, meta['w'], meta['h']
        assert self.session_dir is not None
        self._seq += 1
        path = self.session_dir / f"frame-{self._seq:06d}.bgra"
        owned = False
        try:
            meta = self._call("frame", {"path": win_path(path, self._wine)})
            owned = True
            data = path.read_bytes()
            w, h = meta.get("w"), meta.get("h")
            if (meta.get("format") != "bgra-bottom-up" or type(w) is not int or type(h) is not int
                    or w <= 0 or h <= 0 or len(data) != w * h * 4
                    or hashlib.sha256(data).hexdigest() != meta.get("sha256")):
                raise BackendError("native frame metadata or bytes do not verify")
            self.last_view_metadata = dict(meta)
            return data, w, h
        finally:
            if owned:
                path.unlink(missing_ok=True)

    def input_state(self) -> dict[str, Any]:
        """Virtual device state sampled at the stopped gate, not host physical keys."""
        state = self._call("input.state", {})
        if (state.get("source") != "virtual_devices" or state.get("sampled_at_gate") is not True
                or type(state.get("tick")) is not int
                or not isinstance(state.get("keys"), list)
                or any(type(k) is not int or not 0 <= k < 256 for k in state["keys"])
                or not isinstance(state.get("buttons"), list)
                or any(b not in ("lmb", "rmb", "mmb", "x1", "x2") for b in state["buttons"])
                or not isinstance(state.get("cursor"), list) or len(state["cursor"]) != 2
                or any(type(v) is not int for v in state["cursor"])
                or type(state.get("altf4_save_armed")) is not bool):
            raise BackendError("native input state response does not verify")
        return state

    def hud(self, on: bool, text: str = "", *, watermark: bool = True, offset: int = 0) -> None:
        """Configure output labels; native runtime owns tick, input and mode."""
        # Snapshot redraws these labels at the held gate; captured ring pixels
        # predate this mutation. Invalidate before the request, including faults.
        self._frame_mirror_valid = False
        sink = getattr(self, 'frames', None)
        if sink is not None:
            sink.invalidate_preview()
        self._call("hud", {"on": on, "text": text, "watermark": watermark, "offset": offset})

    def supports_tool_bindings(self) -> bool:
        return self.hello.get('hud_bindings_protocol') == 'kptas-tool-bindings/1'

    def hud_bindings(self, bindings) -> None:
        """Atomically configure the one native host dispatcher at a held gate."""
        if not self.supports_tool_bindings():
            raise BackendError('native configurable tool dispatcher unavailable')
        response = self._call('hud.bindings', {'bindings': list(bindings)})
        if (response.get('schema') != 'kptas-tool-bindings/1' or
                response.get('count') != len(bindings) or response.get('tick') != self._tick):
            raise BackendError('tool binding acknowledgement mismatch')

    def hud_poll(self) -> str:
        """Poll tool controls at a stopped gate; never inject a game input."""
        return str(self._call("hud.poll", {}).get("action", ""))
