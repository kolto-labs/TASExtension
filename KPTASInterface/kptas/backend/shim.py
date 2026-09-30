"""Client for the native shim (contract sections 6, 9 and 10).

Python listens on loopback; the shim (or SimGame.exe) connects from its entry
detour using KPTAS_PORT and KPTAS_SESSION, then answers hello. Bulk bytes go
through files in the session directory; page records come through the staging ring.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import subprocess
import time
from collections import deque
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

from kptas.types import (VERB_ALLOW, WIRE_MAX_LINE, WIRE_PROTO, BackendError, Caps, ClockMode,
                         EngineEvent, EnvPin, OsEvent, ProcOutcome, Receipt, RefusedError, SeekMode,
                         StagingRecord, StepReport, Tier, VerifyLevel)

from .staging import StagingRing, write_records

_ROOT = Path(__file__).resolve().parents[2]


class WireError(BackendError):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code


class Wire:
    """NDJSON request/reply over one socket. Pushes that arrive while waiting are queued."""

    def __init__(self, sock: socket.socket, timeout_s: float = 60.0):
        self.sock = sock
        self.timeout_s = timeout_s
        self._buf = bytearray()
        self._next_id = 1
        self.pushes: deque[dict[str, Any]] = deque()
        self._early: dict[int, dict[str, Any]] = {}

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
                    obj = json.loads(line)
                except ValueError as e:
                    raise BackendError(f"bad wire line: {e}") from None
                if not isinstance(obj, dict):
                    raise BackendError("wire message is not an object")
                return obj
            if len(self._buf) > WIRE_MAX_LINE:
                raise BackendError("wire line exceeds 1 MiB")
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
        rid = self._next_id
        self._next_id += 1
        self.send({"id": rid, "verb": verb, "args": dict(args or {})})
        limit = self.timeout_s if timeout_s is None else timeout_s
        deadline = time.monotonic() + limit if limit else None
        while True:
            msg = self._early.pop(rid, None)
            if msg is None:
                left = None if deadline is None else max(0.0, deadline - time.monotonic())
                if idle is not None:
                    msg = self.recv(0.005 if left is None else min(left, 0.005))
                    if msg is None:
                        idle()
                        if deadline is not None and time.monotonic() >= deadline:
                            raise BackendError(f"{verb}: no reply within {limit} s")
                        continue
                else:
                    msg = self.recv(left)
                if msg is None:
                    raise BackendError(f"{verb}: no reply within {limit} s")
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

    def poll(self, timeout_s: float = 0.0) -> None:
        """Move any pushes already on the socket into the queue."""
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


def filetime(iso: str) -> int:
    from .pysim import _filetime
    return _filetime(iso)


def clock_env(clock: Mapping[str, Any], mode: str | None = None) -> str:
    """KPTAS_CLOCK: semicolon-separated key=decimal pairs (same keys as kptas.supervisor)."""
    parts = {
        "mode": mode or clock.get("mode", "virtual"), "hz": int(clock.get("hz", 60)),
        "qpf_hz": int(clock.get("qpf_hz", 10_000_000)),
        "uptime0_ms": int(clock.get("uptime0_ms", 3_600_000)),
        "initial_realtime": filetime(str(clock.get("initial_realtime", "2004-02-12T00:00:00Z"))),
    }
    return ";".join(f"{k}={v}" for k, v in parts.items())


def note_to_wire(note: Any) -> dict[str, Any]:
    """A score Note in the input verb's shape: DIK codes, buttons, mouse, wheel, msgs."""
    from kptas.score.keys import dik
    if note is None:
        return {"keys": [], "buttons": [], "mouse": {}, "wheel": 0, "msgs": []}
    keys = sorted({dik(k) for tok in note.held for k in tok.split("+")})
    mouse: dict[str, list[int]] = {}
    if note.mouse_abs is not None:
        mouse["abs"] = list(note.mouse_abs)
    if note.mouse_rel is not None:
        mouse["rel"] = list(note.mouse_rel)
    return {"keys": keys, "buttons": list(note.buttons), "mouse": mouse, "wheel": note.wheel,
            "msgs": [note.osev] if note.osev else []}


def _hex(b: bytes | None) -> str | None:
    return None if b is None else b.hex()


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
        self._pending: deque[StagingRecord] = deque()
        self._ring_attached = False
        self._clock_args: tuple[ClockMode, int, int] | None = None
        self.logs: deque[dict[str, Any]] = deque(maxlen=1000)
        self.ticks: deque[dict[str, Any]] = deque(maxlen=1000)
        self.last_waited: Any = None

    # ------------------------------------------------------------------ lifecycle

    @property
    def caps(self) -> Caps:
        if self._caps is None:
            raise BackendError("not attached")
        return self._caps

    def _session(self) -> Path:
        sd = self.cfg.get("shim", {}).get("session_dir", "auto")
        if sd == "auto":
            base = os.environ.get("XDG_RUNTIME_DIR") or os.path.join(
                os.environ.get("TMPDIR", "/tmp"), f"kptas-{os.getuid()}")
            sd = os.path.join(base, "kptas", f"run-{os.getpid()}-{int(time.time() * 1000)}")
        p = Path(os.path.expanduser(sd))
        p.mkdir(parents=True, exist_ok=True, mode=0o700)
        return p

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
        if game.get("launch_wrapper") == "setarch_R":
            argv = ["setarch", "-R", *argv]
        pinned = bool(game.get("pinned_env", True))
        envargs = ["env", "-i"] if pinned else ["env"]
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
        env = {"KPTAS_PORT": str(port), "KPTAS_SESSION": str(self.session_dir),
               "KPTAS_CLOCK": clock_env(clock)}
        if game.get("pinned_env", True):
            for k in ("PATH", "HOME", "USER", "XAUTHORITY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR"):
                if k in os.environ:
                    env[k] = os.environ[k]
            env.update(LANG="C", LC_ALL="C", TZ="UTC", WINEDEBUG="-all")
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
        return env

    def attach(self, cfg: Mapping[str, Any], env: EnvPin | None = None) -> Caps:
        """Listen, launch, accept the shim's connection, hello, and attach the staging ring."""
        self.cfg = cfg
        shim = cfg.get("shim", {})
        self._wine = bool(cfg.get("game", {}).get("wine", "wine"))
        self.session_dir = Path(self.link.session) if self.link is not None else self._session()
        self.session_dir.mkdir(parents=True, exist_ok=True)
        cap = int(shim.get("ring_mb", 64)) << 20
        self.ring = StagingRing.create(self.session_dir / "ring.bin", cap)
        self.ring.set_consumer(state=0)
        if self.link is not None:
            self._handshake(self.link.sock)
            return self.caps
        self._listener = socket.create_server((shim.get("listen", "127.0.0.1"), int(shim.get("port", 0))),
                                              backlog=1)
        port = self._listener.getsockname()[1]
        self._connect(port, int(shim.get("connect_timeout_s", 30)))
        return self.caps

    def _connect(self, port: int, timeout_s: int) -> None:
        env = self._child_env(port)
        if self.on_listen is not None:
            self.on_listen(port, env)
        if self.launch:
            argv, _ = self._launch_argv(env)
            log = open(self.session_dir / f"process-{self._epoch}.log", "wb")  # type: ignore[operator]
            self.proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                         start_new_session=True)
            log.close()
        assert self._listener is not None
        self._listener.settimeout(timeout_s)
        try:
            conn, _ = self._listener.accept()
        except socket.timeout:
            raise BackendError(f"the shim did not connect within {timeout_s} s") from None
        self._handshake(conn)

    def _handshake(self, conn: socket.socket) -> None:
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.wire = Wire(conn)
        hello = self.wire.request("hello", {"proto": WIRE_PROTO})
        if hello.get("proto") != WIRE_PROTO:
            raise BackendError(f"shim speaks {hello.get('proto')!r}, not {WIRE_PROTO}")
        self.hello = hello
        self._epoch = int(hello.get("epoch", self._epoch))
        self._caps = Caps.from_hello({**hello, "backend": self.name})
        # section 10 order is hello, tier.lock, clock, ring.attach: attach before capture or start
        self._ring_attached = False

    def _ensure_ring(self) -> None:
        if self._ring_attached:
            return
        assert self.ring is not None and self.session_dir is not None
        self.ring_header = self._call("ring.attach", {"path": win_path(self.ring.path, self._wine),
                                                      "bytes": 4096 + self.ring.capacity}).get("header")
        self.ring.set_consumer()
        self._ring_attached = True

    def _call(self, verb: str, args: Mapping[str, Any] | None = None, timeout_s: float | None = None
              ) -> dict[str, Any]:
        if self.wire is None:
            raise BackendError("not attached")
        if self._tier is not None and self._tier not in VERB_ALLOW.get(verb, frozenset()):
            raise RefusedError(f"{verb} is refused in tier {self._tier.label}")
        # drain the ring while waiting: the shim's gate waits for space, never the reverse
        res = self.wire.request(verb, args, timeout_s, idle=self._drain_ring)
        self._drain_ring()
        self._drain_pushes()
        return res

    def _drain_ring(self) -> None:
        if self.ring is not None:
            self._pending.extend(self.ring.read())

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
        self._call("clock", {
            "mode": str(mode), "hz": hz, "scale": scale, "qpf_hz": int(c.get("qpf_hz", 10_000_000)),
            "uptime0_ms": int(c.get("uptime0_ms", 3_600_000)),
            "initial_realtime": filetime(str(c.get("initial_realtime", "2004-02-12T00:00:00Z"))),
            "hooked_modules": list(c.get("hooked_modules", ())),
            "budget_guard": dict(c.get("budget_guard", {})), "spin": spin})
        self._clock_args = (mode, hz, scale)

    def start(self) -> StepReport:
        self._ensure_ring()
        rep = StepReport.from_wire(self._call("start"))
        self._started = True
        return self._note_events(rep)

    def close(self) -> None:
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
        if self.ring is not None:
            self.ring.set_state(2)
            self.ring.close()
            self.ring = None

    # ------------------------------------------------------------------ ticks

    def _note_events(self, rep: StepReport) -> StepReport:
        self._events.extend(rep.events)
        self._tick = rep.tick
        return rep

    def step(self, n: int, note: Any = None) -> StepReport:
        return self._note_events(StepReport.from_wire(
            self._call("step", {"n": n, "note": note_to_wire(note)}, timeout_s=0)))

    def wait(self, event: str, arg: str | None, timeout_ticks: int, note: Any = None) -> StepReport:
        res = self._call("wait", {"event": event, "arg": arg, "timeout": timeout_ticks,
                                  "note": note_to_wire(note)}, timeout_s=0)
        self.last_waited = res.get("waited")
        return self._note_events(StepReport.from_wire(res))

    def input(self, note: Any) -> int:
        return int(self._call("input", note_to_wire(note))["for_tick"])

    def gate(self, on: bool) -> dict[str, Any]:
        return self._call("gate", {"on": on})

    # ------------------------------------------------------------------ capture and restore

    def capture(self, kind: Literal["dirty", "full"], enabled: bool = True) -> dict[str, Any]:
        cap = self.cfg.get("capture", {})
        self._ensure_ring()
        return self._call("capture", {"enabled": enabled, "tracker": cap.get("tracker", "auto"),
                                      "classes": list(cap.get("sim_classes", ())), "sweep": kind == "full"})

    def records(self) -> Iterator[StagingRecord]:
        if self.ring is None:
            raise BackendError("not attached")
        self._drain_ring()
        while self._pending:
            yield self._pending.popleft()

    def restore(self, tick: int, root: bytes, pages: Iterable[StagingRecord]) -> Receipt:
        assert self.session_dir is not None
        self._seq += 1
        path = self.session_dir / f"restore-{self._seq:06d}.rec"
        n = write_records(path, pages)
        t0 = time.perf_counter_ns()
        res = self._call("restore", {"tick": tick, "root": root.hex(), "path": win_path(path, self._wine)},
                         timeout_s=0)
        us = int(res.get("us", (time.perf_counter_ns() - t0) // 1000))
        path.unlink(missing_ok=True)
        ok = (bool(res.get("verified")) and res.get("root") == root.hex()
              and int(res.get("tick", tick)) == tick)
        from_tick, self._tick = self._tick, int(res.get("tick", tick))
        return Receipt(kind="seek", result="pass" if ok else "fail", from_tick=from_tick,
                       to_tick=int(res.get("tick", tick)), mode=SeekMode.RESTORE,
                       verify_level=VerifyLevel.FULL_ROOT, write_pages=int(res.get("pages", n)), micros=us,
                       state_root=str(res.get("root", "")), verifier=self.name)

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
        assert self.session_dir is not None
        self._seq += 1
        path = self.session_dir / f"files-{self._seq:06d}.bin"
        if op == "put":
            if self._started and self._tier is not None and self._tier is not Tier.LAB:
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

    # ------------------------------------------------------------------ events, os, frame, hud

    def _drain_pushes(self) -> None:
        if self.wire is None:
            return
        while self.wire.pushes:
            p = self.wire.pushes.popleft()
            kind = p.get("push")
            if kind == "event":
                self._events.append(EngineEvent(p["name"], int(p["tick"]), p.get("arg")))
            elif kind == "watch.hit":
                self._events.append(EngineEvent("watch.hit", int(p.get("tick", 0)), p.get("id")))
            elif kind == "desync":
                self._events.append(EngineEvent("desync", int(p.get("tick", 0)), json.dumps(p, sort_keys=True)))
            elif kind == "log":
                self.logs.append(p)
            elif kind == "tick":
                self.ticks.append(p)

    def events(self) -> Iterator[EngineEvent]:
        if self.wire is not None:
            self.wire.poll(0.0)
            self._drain_pushes()
        while self._events:
            yield self._events.popleft()

    def os_event(self, ev: OsEvent) -> ProcOutcome:
        ev = OsEvent(ev)
        if ev is OsEvent.RELAUNCH:
            if self.link is not None:
                raise BackendError("relaunch belongs to the link owner")
            return self.relaunch()
        return ProcOutcome(self._call("os_event", {"ev": str(ev)}).get("outcome", "alive"))

    def relaunch(self) -> ProcOutcome:
        """End this process and start the next epoch on the same listener and ring."""
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
        assert self.session_dir is not None
        self._seq += 1
        path = self.session_dir / f"frame-{self._seq:06d}.png"
        self._call("frame", {"path": win_path(path, self._wine)})
        data = path.read_bytes()
        path.unlink(missing_ok=True)
        return data

    def hud(self, on: bool, text: str = "") -> None:
        self._call("hud", {"on": on, "text": text})
