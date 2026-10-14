"""Client for the native shim (contract sections 6, 9 and 10).

Python listens on loopback; the shim (or SimGame.exe) connects from its entry
detour using KPTAS_PORT and KPTAS_SESSION, then answers hello. Bulk bytes go
through files in the session directory; page records come through the staging ring.

A savestate is the tick's pages plus the stage's writable files. The pages come
through the ring every tick. The files are scanned at every gate, so a step of
n ticks runs as n one-tick steps; only the first carries the note's first-tick
edges (first=true), so the game sees the same input as one n-tick step.
"""

from __future__ import annotations

import base64
import dataclasses
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

from kptas.config import DEFAULTS
from kptas.supervisor import clock_env, filetime
from kptas.types import (VERB_ALLOW, WIRE_MAX_LINE, WIRE_PROTO, BackendError, Caps, ClockMode,
                         EngineEvent, EnvPin, OsEvent, ProcOutcome, Receipt, RecordFlag, RecordKind, RefusedError,
                         SeekMode, StagingRecord, StepReport, Tier, VerbClass, VerifyLevel)

from .rig import StageFiles
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
        gl_names=bool(c.get("gl_names", False)), build=str(hello.get("build", "")),
        exe_sha256=str(hello.get("exe_sha256", "")), shim_version=str(hello.get("shim", "")),
        modules=tuple(hello.get("modules", ())), hooks=tuple(hello.get("hooks", ())))


def _gone(e: BaseException) -> bool:
    """The connection ended (the game exited), as opposed to the shim refusing or failing a request."""
    if isinstance(e, (WireError, RefusedError)):
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
        self._restore_poisoned = False
        self._relaunching = False
        self._epoch = 0
        self._wine = True
        self._events: deque[EngineEvent] = deque()
        self._seq = 0
        self._tick = 0
        self._pending: deque[StagingRecord] = deque()     # records of complete tick batches
        self._open: list[StagingRecord] = []              # records of a batch whose TICK_END is still to come
        self._last_end = -1                               # tick of the last TICK_END seen
        self.restore_batches = 0
        self._ring_attached = False
        self._clock_args: tuple[ClockMode, int, int] | None = None
        self._files: StageFiles | None = None
        self._file_log: list[tuple[int, Any]] = []
        self._msgs: list[str] = []                        # os events for the next tick
        self.logs: deque[dict[str, Any]] = deque(maxlen=1000)
        self.ticks: deque[dict[str, Any]] = deque(maxlen=1000)
        self._input_sha256_by_tick: dict[tuple[int, int], bytes] = {}
        self._restore_ticks: set[tuple[int, int]] = set()
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
        env = {"KPTAS_PORT": str(port), "KPTAS_SESSION": win_path(self.session_dir, bool(game.get("wine", "wine"))),
               "KPTAS_CLOCK": clock_env({"clock": {**DEFAULTS["clock"], "mode": "virtual", **clock}}),
               "KPTAS_PRESENT": str(self.cfg.get("replay", {}).get("present", "skip"))}
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
        # Wire epochs are scoped to one native process. A relaunch may reuse
        # the same numeric epoch/tick pair, so discard any unconsumed receipts
        # from the prior connection rather than risking a false match.
        self._input_sha256_by_tick.clear()
        self._restore_ticks.clear()
        hello = self.wire.request("hello", {"proto": WIRE_PROTO})
        if hello.get("proto") != WIRE_PROTO:
            raise BackendError(f"shim speaks {hello.get('proto')!r}, not {WIRE_PROTO}")
        # A configured build pin is an integrity boundary. Accept the complete
        # SHA-256 when available, otherwise the documented 16-hex build prefix;
        # never silently downgrade a missing or malformed receipt to unpinned.
        want = str(self.cfg.get("shim", {}).get("dll_sha256", "")).lower()
        if self.name == "shim" and want:
            if not (1 <= len(want) <= 64 and all(c in "0123456789abcdef" for c in want)):
                raise BackendError("shim.dll_sha256 must be a 1-to-64 character hexadecimal SHA-256 prefix")
            full = str(hello.get("shim_sha256", "")).lower()
            prefix = str(hello.get("build", "")).lower()
            if full:
                if len(full) != 64 or not all(c in "0123456789abcdef" for c in full):
                    raise BackendError("the shim returned a malformed full SHA-256 required by shim.dll_sha256")
                candidate = full
            elif len(prefix) == 16 and all(c in "0123456789abcdef" for c in prefix):
                candidate = prefix
            else:
                raise BackendError("the shim did not provide a verifiable build hash required by shim.dll_sha256")
            if len(want) > len(candidate) or not candidate.startswith(want):
                raise BackendError(f"the game loaded shim build {candidate}, not the pinned {want}")
        self.hello = hello
        self._epoch = int(hello.get("epoch", self._epoch))
        self._caps = _caps(hello, self.name)
        # section 10 order is hello, tier.lock, clock, ring.attach: attach before capture or start
        self._ring_attached = False

    def _ensure_ring(self) -> None:
        if self._ring_attached:
            return
        assert self.ring is not None and self.session_dir is not None
        self.ring_header = self._call("ring.attach", {"path": win_path(self.ring.path, self._wine),
                                                      "mb": self.ring.capacity >> 20,
                                                      "bytes": 4096 + self.ring.capacity}).get("header")
        self.ring.set_consumer()
        self._ring_attached = True

    def _call(self, verb: str, args: Mapping[str, Any] | None = None, timeout_s: float | None = None
              ) -> dict[str, Any]:
        recovery_setup = self._relaunching and verb in {"tier.lock", "clock"}
        if self._restore_poisoned and verb not in {"restore", "verify"} and not recovery_setup:
            raise BackendError(f"{verb} refused after an unverified restore; perform a verified restore or relaunch")
        if self.wire is None:
            raise BackendError("not attached")
        allowed = VERB_ALLOW.get(verb, frozenset())
        if self._tier is None and Tier.TAS not in allowed:
            raise RefusedError(f"{verb} is lab only; lock the tier at lab first")
        if self._tier is not None and self._tier not in allowed:
            raise RefusedError(f"{verb} is refused in tier {self._tier.label}")
        # drain the ring while waiting: the shim's gate waits for space, never the reverse
        res = self.wire.request(verb, args, timeout_s, idle=self._drain_ring)
        self._drain_ring()
        self._drain_pushes()
        return res

    def _drain_ring(self) -> None:
        """Move complete tick batches to the queue. A restore's batch is dropped: it holds the pages the
        restore wrote, which the caller already knows, and no tick was played."""
        if self.ring is None:
            return
        for rec in self.ring.read():
            self._open.append(rec)
            if rec.kind is not RecordKind.TICK_END:
                continue
            batch, self._open = self._open, []
            if rec.flags & RecordFlag.RESTORE:
                self.restore_batches += 1
                self._restore_ticks.add((self._epoch, rec.tick))
                continue
            self._last_end = rec.tick
            self._pending.extend(batch)

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
            "budget_guard": dict(c.get("budget_guard", {})), "spin": spin})
        self._clock_args = (mode, hz, scale)

    def start(self) -> StepReport:
        """Attach the ring, turn capture on (the shim takes it only before start), and run to tick 0."""
        self._ensure_ring()
        if self.cfg.get("capture", {}).get("enabled", "auto") is not False:
            self._call("capture", {"enabled": True})
        self.subscribe(("tick",))
        rep = self._report(self._call("start"))
        self._started = True
        self._scan_files(rep.tick)
        return rep

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

    def _report(self, res: Mapping[str, Any]) -> StepReport:
        """A step reply as a StepReport, with the events pushed while it ran."""
        rep = StepReport.from_wire(res)
        pushed = []
        while self._events:
            pushed.append(self._events.popleft())
        if pushed:
            rep = dataclasses.replace(rep, events=(*rep.events, *pushed))
        rep = dataclasses.replace(rep, input_sha256_by_tick=self._take_input_sha256(rep.epoch, rep.tick))
        self._tick = rep.tick
        return rep

    def _take_input_sha256(self, epoch: int, through_tick: int) -> tuple[tuple[int, int, bytes], ...]:
        """Consume only observed digests at or before this report's exact gate.

        The wire push is advisory and may be absent on older shims. RESTORE
        batches are deliberately excluded; replaying a restored tick will
        produce a fresh, separately keyed receipt.
        """
        keys = sorted(key for key in self._input_sha256_by_tick
                      if key[0] == epoch and key[1] <= through_tick)
        rows = []
        for key in keys:
            digest = self._input_sha256_by_tick.pop(key)
            if key not in self._restore_ticks:
                rows.append((key[0], key[1], digest))
            self._restore_ticks.discard(key)
        self._restore_ticks = {
            key for key in self._restore_ticks
            if key[0] != epoch or key[1] > through_tick
        }
        # A stale candidate must never be retained indefinitely or reused in
        # a later epoch. It is safe to discard only after its epoch ended.
        if len(self._input_sha256_by_tick) > 8192:
            self._input_sha256_by_tick = {
                key: value for key, value in self._input_sha256_by_tick.items()
                if key[0] == epoch and key[1] > through_tick
            }
        return tuple(rows)

    def _scan_files(self, tick: int) -> None:
        if self._files is None:
            return
        tree = self._files.changed()
        if tree is not None:
            self._file_log.append((tick, tree))

    def file_changes(self) -> list[tuple[int, Any]]:
        """(tick, (rows, {sha256: bytes})) for each gate where the stage's files differed from the gate
        before; the first entry is the tree at tick 0."""
        out, self._file_log = self._file_log, []
        return out

    def _exited(self) -> StepReport:
        """The connection ended during a step: the process is gone. Ticks it finished are in the ring."""
        if self.wire is not None:
            # Preserve complete pushes already decoded before EOF; the ring is
            # drained first so any restore markers are known before pairing.
            self._drain_ring()
            self._drain_pushes()
            self.wire.close()
            self.wire = None
        else:
            self._drain_ring()
        self._open = []
        tick = max(self._last_end, self._tick)
        self._tick = tick
        return StepReport(tick=tick, epoch=self._epoch, server_passes=1, subticks=0,
                          verbclass=VerbClass.GATE | VerbClass.CLOCK | VerbClass.INPUT,
                          outcome=ProcOutcome.EXITED,
                          input_sha256_by_tick=self._take_input_sha256(self._epoch, tick))

    def _one(self, verb: str, args: Mapping[str, Any]) -> StepReport:
        if self._restore_poisoned:
            raise BackendError(f"{verb} refused after an unverified restore; perform a verified restore or relaunch")
        try:
            res = self._call(verb, args, timeout_s=0)
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
        self._scan_files(rep.tick)
        return rep

    @staticmethod
    def _merge(reps: Sequence[StepReport]) -> StepReport:
        vc, evs, input_sha256_by_tick = VerbClass.NONE, [], {}
        for r in reps:
            vc |= r.verbclass
            evs.extend(r.events)
            for epoch, tick, digest in r.input_sha256_by_tick:
                input_sha256_by_tick[(epoch, tick)] = digest
        hashes = tuple((epoch, tick, digest)
                       for (epoch, tick), digest in sorted(input_sha256_by_tick.items()))
        return dataclasses.replace(reps[-1], verbclass=vc, events=tuple(evs),
                                   input_sha256_by_tick=hashes)

    def step(self, n: int, note: Any = None, first: bool = True) -> StepReport:
        """Run n ticks holding note. first=False: the note's first tick already ran (a resumed note)."""
        args = note_args(note)
        msgs = [*self._msgs, *([str(note.osev)] if note is not None and note.osev and first else [])]
        self._msgs = []
        if n <= 0:
            return self._one("step", {**args, "n": 0})
        per_tick = self._files is not None
        reps: list[StepReport] = []
        left = n
        while left > 0:
            k = 1 if per_tick else left
            a = {**args, "n": k, "first": first and not reps}
            if msgs and not reps:
                a["msgs"] = msgs
            rep = self._one("step", a)
            reps.append(rep)
            left -= k
            if rep.outcome is ProcOutcome.EXITED:
                break
        return self._merge(reps)

    def wait(self, event: str, arg: str | None, timeout_ticks: int, note: Any = None) -> StepReport:
        """Run ticks holding note (nothing by default) until event fires or timeout_ticks pass. Every tick
        of a wait carries WAIT. proc.down waits for the process to exit."""
        args = note_args(note)
        if self._files is None and event != "proc.down":
            res = self._one("wait", {**args, "event": event, "arg": arg, "timeout": timeout_ticks})
            self.last_waited = res.outcome is ProcOutcome.ALIVE and any(e.name == event for e in res.events)
            return dataclasses.replace(res, verbclass=res.verbclass | VerbClass.WAIT)
        reps: list[StepReport] = []
        hit = False
        for i in range(max(0, timeout_ticks)):
            rep = self._one("step", {**args, "n": 1, "first": i == 0})
            reps.append(rep)
            if rep.outcome is ProcOutcome.EXITED:
                hit = event == "proc.down"
                break
            if any(e.name == event and (arg is None or e.arg == arg) for e in rep.events):
                hit = True
                break
        if not reps:
            reps.append(self._one("step", {**args, "n": 0}))
        self.last_waited = hit
        rep = self._merge(reps)
        return dataclasses.replace(rep, verbclass=rep.verbclass | VerbClass.WAIT)

    def input(self, note: Any) -> int:
        if self._restore_poisoned:
            raise BackendError("input refused after an unverified restore; perform a verified restore or relaunch")
        result = self._call("input", note_args(note))
        tick = result.get("for_tick")
        if type(tick) is not int or tick < 0:
            raise BackendError("input acknowledgement has an invalid for_tick value")
        return tick

    def gate(self, on: bool) -> dict[str, Any]:
        return self._call("gate", {"on": on})

    # ------------------------------------------------------------------ capture and restore

    def capture(self, kind: Literal["dirty", "full"], enabled: bool = True) -> dict[str, Any]:
        if self._restore_poisoned:
            raise BackendError("capture refused after an unverified restore; perform a verified restore or relaunch")
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
        """Write the page set W (a restore set file in the session directory) and let the shim verify the
        whole sim set against root. Put the tick's files first (put_files)."""
        assert self.session_dir is not None
        if self.wire is None:
            raise BackendError("not attached")
        if type(tick) is not int or tick < 0 or tick > 0xFFFFFFFFFFFFFFFF:
            raise BackendError("restore tick must be an unsigned 64-bit integer")
        if type(root) is not bytes or len(root) != 32:
            raise BackendError("restore root must be exactly 32 bytes")
        self._seq += 1
        path = self.session_dir / f"restore-{self._seq:06d}.rec"
        wire_path = win_path(path, self._wine)
        args = {"tick": tick, "root": root.hex(), "path": wire_path}

        def restore_pages() -> Iterator[StagingRecord]:
            for record in pages:
                if not isinstance(record, StagingRecord):
                    raise BackendError("restore set contains a non-staging record")
                if record.kind is not RecordKind.PAGE:
                    raise BackendError("restore set may contain only PAGE records")
                if record.tick != tick:
                    raise BackendError("restore PAGE tick does not match the requested target")
                yield record
        try:
            n = write_records(path, restore_pages())
        except BaseException:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        t0 = time.perf_counter_ns()
        # Once the write-set is about to be applied, assume the live process may
        # be partially changed until the shim proves the requested root and tick.
        self._restore_poisoned = True
        try:
            res = self._call("restore", args, timeout_s=0)
        finally:
            path.unlink(missing_ok=True)
        reported_tick, reported_root, verified = res.get("tick"), res.get("root"), res.get("verified")
        if type(reported_tick) is not int or reported_tick < 0 or type(reported_root) is not str or type(verified) is not bool:
            raise BackendError("restore reply is missing a valid tick, root, or boolean verification result")
        us = res.get("us", (time.perf_counter_ns() - t0) // 1000)
        write_pages = res.get("pages", n)
        if type(us) is not int or us < 0 or type(write_pages) is not int or write_pages < 0:
            raise BackendError("restore reply has invalid duration or page count")
        ok = verified is True and reported_root == root.hex() and reported_tick == tick
        from_tick = self._tick
        receipt = Receipt(kind="seek", result="pass" if ok else "fail", from_tick=from_tick,
                          to_tick=reported_tick, mode=SeekMode.RESTORE,
                          verify_level=VerifyLevel.FULL_ROOT, write_pages=write_pages, micros=us,
                          state_root=reported_root, verifier=self.name)
        if ok:
            self._tick = reported_tick
            self._restore_poisoned = False
        return receipt

    def put_files(self, rows: Sequence[Sequence[Any]], blob: Callable[[str], bytes]) -> None:
        """Replay the recorded file tree as the first phase of a page restore.

        Poison before writing because a partial file-tree failure leaves the
        live process inconsistent. A later exact, verified page restore clears
        poison; allowing this method while poisoned is required for that retry.
        """
        if self._files is None:
            raise BackendError("this backend does not track the game's files (game.dir or files.store)")
        self._restore_poisoned = True
        self._files.write(rows, blob)

    def digest(self, regions: Sequence[str]) -> dict[str, str]:
        if self._restore_poisoned:
            raise BackendError("digest refused after an unverified restore; perform a verified restore or relaunch")
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
        if self._restore_poisoned and op == "put":
            raise BackendError("file write refused after an unverified restore; perform a verified restore or relaunch")
        if self._files is not None:
            if op == "snap":
                return self._files.snapshot()
            if op == "put":
                if self._started and self._tier is not Tier.LAB:
                    raise RefusedError("files put after start is lab only")
                ents = json.loads(blob or b'{"entries": []}')["entries"]
                data = {h: base64.b64decode(b64) for _p, h, _s, _v, b64 in ents}
                self._files.write([e[:4] for e in ents], data.__getitem__)
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
                raw_digest = p.get("input_sha256")
                if raw_digest is not None:
                    if not isinstance(raw_digest, str) or len(raw_digest) != 64:
                        raise BackendError("tick push input_sha256 must be 64 hexadecimal characters")
                    try:
                        digest = bytes.fromhex(raw_digest)
                    except ValueError:
                        raise BackendError("tick push input_sha256 is not hexadecimal") from None
                    if len(digest) != 32:
                        raise BackendError("tick push input_sha256 must decode to 32 bytes")
                    tick = p.get("tick")
                    epoch = p.get("epoch", self._epoch)
                    if type(tick) is not int or tick < 0 or type(epoch) is not int or epoch < 0:
                        raise BackendError("tick push input_sha256 has an invalid epoch/tick key")
                    key = (epoch, tick)
                    # Restore flags are learned from the staging ring before
                    # pushes are drained. Never attach a restore-time digest.
                    if key in self._restore_ticks:
                        self._restore_ticks.discard(key)
                    else:
                        previous = self._input_sha256_by_tick.get(key)
                        if previous is not None and previous != digest:
                            raise BackendError(f"conflicting input_sha256 receipts for epoch {epoch}, tick {tick}")
                        self._input_sha256_by_tick[key] = digest
                self.ticks.append(p)

    def events(self) -> Iterator[EngineEvent]:
        if self.wire is not None:
            self.wire.poll(0.0)
            self._drain_pushes()
        while self._events:
            yield self._events.popleft()

    def os_event(self, ev: OsEvent) -> ProcOutcome:
        """close: the shim posts WM_CLOSE to the game window at the start of the next tick (os_event verb).
        Other window events ride on the next step's msgs, which the shim refuses when it lacks them.
        relaunch starts a new process."""
        ev = OsEvent(ev)
        if ev is OsEvent.RELAUNCH:
            if self.link is not None:
                raise BackendError("relaunch belongs to the link owner")
            return self.relaunch()
        if ev is OsEvent.CLOSE:
            return ProcOutcome(self._call("os_event", {"ev": str(ev)}).get("outcome", "alive"))
        self._msgs.append(str(ev))
        return ProcOutcome.ALIVE

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
        self._relaunching = True
        try:
            self._connect(self._listener.getsockname()[1], int(self.cfg.get("shim", {}).get("connect_timeout_s", 30)))
            if tier is not None:
                self.lock_tier(tier)
            if self._clock_args is not None:
                self.clock(*self._clock_args)   # the new process needs its clock request again
            self._restore_poisoned = False
        finally:
            self._relaunching = False
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
