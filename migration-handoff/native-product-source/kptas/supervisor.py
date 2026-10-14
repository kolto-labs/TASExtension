"""Launch or attach a backend, drive a compiled score through it, and survive relaunches.

Each game process is one epoch (contract §3). Python listens on loopback and the
shim connects at boot with KPTAS_PORT and KPTAS_SESSION (C1); KPTAS_CLOCK carries
the clock origin DllMain needs before any connection exists. An epoch ends when
the process exits (Alt-F4, close, crash) or the score asks for @relaunch; the
supervisor reports it to the sink, which owns the journal.
"""

from __future__ import annotations

import base64
import binascii
import dataclasses
try:
    import fcntl
except ImportError:            # native Windows: msvcrt byte-range locks below
    fcntl = None
    import msvcrt
import fnmatch
import functools
import importlib
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any, Protocol

from kptas.types import (BackendError, Caps, ClockMode, EnvPin, OsEvent, ProcOutcome,
                         StagingRecord, StepReport, Tier, VerbClass)

PROCESS_BACKENDS = frozenset({"simgame", "shim"})
PKG_ROOT = Path(__file__).resolve().parent.parent       # KPTASInterface
_FILETIME_EPOCH = datetime(1601, 1, 1, tzinfo=timezone.utc)


class TelosError(Exception):
    """A clear, user-facing failure. The CLI prints it without a traceback."""


class RunError(TelosError):
    """The score could not be carried out as written (halt, timeout, unexpected exit)."""


def require(modname: str, *attrs: str) -> ModuleType:
    """Import a sibling module lazily; a missing or broken one becomes a TelosError."""
    try:
        mod = importlib.import_module(modname)
    except ModuleNotFoundError as e:
        if e.name and (modname == e.name or modname.startswith(e.name + ".")):
            raise TelosError(f"{modname} is not available in this checkout; "
                             "this command needs it") from None
        raise TelosError(f"{modname} failed to import: {e}") from None
    except Exception as e:  # a half-written module must not crash the CLI
        raise TelosError(f"{modname} failed to import: {type(e).__name__}: {e}") from None
    missing = [a for a in attrs if not hasattr(mod, a)]
    if missing:
        raise TelosError(f"{modname} has no {', '.join(missing)}; this command needs it")
    return mod


# ---------------------------------------------------------------- process launch

def filetime(iso: str) -> int:
    """ISO-8601 UTC time as a Windows FILETIME (100 ns since 1601)."""
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    d = dt - _FILETIME_EPOCH
    return (d.days * 86400 + d.seconds) * 10_000_000 + d.microseconds * 10


def clock_env(cfg: Mapping[str, Any], hz: int | None = None) -> str:
    """KPTAS_CLOCK value: semicolon-separated key=decimal pairs, trivial to parse without a CRT."""
    c = cfg["clock"]
    parts = {"mode": c["mode"], "hz": hz or c["hz"], "qpf_hz": c["qpf_hz"],
             "uptime0_ms": c["uptime0_ms"], "initial_ft": filetime(c["initial_realtime"])}
    return ";".join(f"{k}={v}" for k, v in parts.items())


def audio_env(cfg: Mapping[str, Any]) -> str:
    """KPTAS_AUDIO value, or "" for audio.device = "none": that game takes the sound-off path with no
    Miles hooks, and its environment stays the one older runs had."""
    a = cfg.get("audio", {})
    device = a.get("device", "none")
    if device in ("none", "auto"):
        return ""
    parts = {"device": device, "provider": a.get("provider", "software"), "decode": a.get("decode", "canonical"),
             "output": a.get("output", "null"), "shadow": int(bool(a.get("shadow", False)))}
    return ";".join(f"{k}={v}" for k, v in parts.items())


def extra_env(cfg: Mapping[str, Any]) -> dict[str, str]:
    """game.env as a dict: extra NAME=value entries for the game's environment."""
    return dict(e.split("=", 1) for e in cfg.get("game", {}).get("env", ()))


def portable_values(cfg: Mapping[str, Any]) -> dict[str, Any]:
    """math, x87_cw and mxcsr as the determinism key records them: [portable] under the profile file."""
    if "portable" not in cfg:
        return {"math": "reference", "x87_cw": 0x027F, "mxcsr": 0x1F80}
    prof = require("kptas.tier.envpin", "profile_obj").profile_obj(cfg)
    return {k: prof[k] for k in ("math", "x87_cw", "mxcsr")}


def portable_env(cfg: Mapping[str, Any]) -> str:
    """KPTAS_PORTABLE value (§7 item 3). The shim reads it in DllMain and drops it from the environment
    before the CRT copies it. Wine has already allocated the environment by then, so the value has one
    length in every mode: a longer one shifts every later Wine heap pointer the game stores."""
    v = portable_values(cfg)
    return f"math={'ref' if v['math'] == 'reference' else 'nat'};x87_cw={v['x87_cw']:#06x};mxcsr={v['mxcsr']:#06x}"


_SESSION_LOCKS: dict[Path, int] = {}


def _lock_nb(fd: int) -> None:
    """Exclusive non-blocking lock on fd; OSError when another holder has it (flock, or msvcrt on Windows)."""
    if fcntl is not None:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    else:
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)


def _session_base() -> Path:
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "Stasis"
    return Path(os.environ.get("XDG_RUNTIME_DIR") or os.path.expanduser("~/.cache")) / "kptas"


def auto_session_dir() -> Path:
    """shim.session_dir "auto": $XDG_RUNTIME_DIR/kptas/session-0, the same for every run, seek and judge.

    The path reaches Wine's environment (KPTAS_SESSION), and with it Wine's own heap. The shim keeps it out of
    game memory; one name everywhere is the second guard. A directory another live process holds (a lock file
    beside it) gives way to session-1 .. session-9, which have the same length. This process keeps its lock."""
    base = _session_base()
    for d in list(_SESSION_LOCKS):
        if d.parent == base:
            return d
    base.mkdir(parents=True, exist_ok=True)
    for i in range(10):
        d = base / f"session-{i}"
        fd = os.open(base / f"session-{i}.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            _lock_nb(fd)
        except OSError:
            os.close(fd)
            continue
        _SESSION_LOCKS[d] = fd
        return d
    raise TelosError(f"every session directory under {base} is in use; set shim.session_dir")


_STALE_NAMES = ("admitted.roots",)
_STALE_GLOBS = ("snap-*.bin", "files-*.bin", "restore-*.rec", "gl-*.set", "frame-*.bgra", "*.tmp")
_KEEP_LOGS = ("kptas_shim.log", "game.out", "process-*.log")     # evidence: moved aside, never deleted
_PREVIOUS_KEEP = 3
_PREVIOUS_RE = re.compile(r"previous-\d{8}-\d{6}(-\d+)?")
_PREVIOUS_DIRS: dict[Path, Path] = {}       # session dir -> this process's previous-<timestamp> directory


def _held_session_root(target: Path) -> Path | None:
    """The auto session dir (session-N) that target is, or is directly inside, when this process holds its
    flock; else None. The lock file is probed through a second descriptor, so a lock nobody holds is not
    mistaken for ours."""
    t = os.path.realpath(target)
    for d in list(_SESSION_LOCKS):
        r = os.path.realpath(d)
        if t != r and os.path.dirname(t) != r:
            continue
        try:
            probe = os.open(d.parent / f"{d.name}.lock", os.O_RDWR)
        except OSError:
            return None
        try:
            _lock_nb(probe)
            if fcntl is None:
                msvcrt.locking(probe, msvcrt.LK_UNLCK, 1)
        except OSError:
            return d                        # held, and by this process: only it keeps a descriptor in _SESSION_LOCKS
        finally:
            os.close(probe)
        return None
    return None


def clear_stale_session(target: Path, log: Callable[[str], None] | None = None) -> bool:
    """Before a NEW game process is launched into target (an epoch-N or probe directory, or the session
    directory itself): remove what a dead previous owner left there. A crashed owner leaves the shim's
    GL frame spool (gl/frame-*.kpgs, *.tmp), ring.bin and other per-epoch files; the new shim then refuses to
    publish its first spool (CREATE_NEW / sequence 1) and the host rejects the old file's header.

    Only under an auto session directory whose lock this process holds (otherwise nothing is touched and
    False is returned), only inside target, and only known debris names. The previous kptas_shim.log,
    game.out and process-*.log move to <session>/previous-<timestamp>/<name of target>/ first (the newest
    three previous-* directories are kept). A target with no debris is left alone, logs included.
    Never call this when attaching to a live process. Returns True when debris was removed."""
    target = Path(target)
    root = _held_session_root(target)
    if root is None or target.is_symlink() or not target.is_dir():
        return False
    stale: list[os.DirEntry[str]] = []
    logs: list[os.DirEntry[str]] = []
    ring: os.DirEntry[str] | None = None
    with os.scandir(target) as it:
        for e in it:
            if e.name == "gl":
                if not e.is_dir(follow_symlinks=False) or any(True for _ in os.scandir(e.path)):
                    stale.append(e)
            elif e.name == "ring.bin":
                ring = e
            elif e.is_dir(follow_symlinks=False):
                continue
            elif e.name in _STALE_NAMES or any(fnmatch.fnmatchcase(e.name, g) for g in _STALE_GLOBS):
                stale.append(e)
            elif any(fnmatch.fnmatchcase(e.name, g) for g in _KEEP_LOGS) and not e.is_symlink():
                logs.append(e)
    if not stale:
        return False                        # a clean shutdown leaves at most ring.bin and an empty gl/
    if ring is not None:
        stale.append(ring)
    try:
        prev = _PREVIOUS_DIRS.get(root)
        if prev is None:
            stamp = time.strftime("%Y%m%d-%H%M%S")
            prev, n = root / f"previous-{stamp}", 0
            while prev.exists():
                n += 1
                prev = root / f"previous-{stamp}-{n}"
            _PREVIOUS_DIRS[root] = prev
        if logs:
            dest = prev / (target.name if target != root else "session")
            dest.mkdir(parents=True, exist_ok=True)
            for e in logs:
                to, n = dest / e.name, 0
                while to.exists():
                    n += 1
                    to = dest / f"{e.name}.{n}"
                os.replace(e.path, to)      # same filesystem; evidence is safe before anything is deleted
        for e in stale:
            if e.is_dir(follow_symlinks=False):
                shutil.rmtree(e.path)
            else:
                os.unlink(e.path)
        keep = sorted(c.name for c in root.iterdir()
                      if _PREVIOUS_RE.fullmatch(c.name) and c.is_dir() and not c.is_symlink())
        for name in keep[:-_PREVIOUS_KEEP]:
            if root / name != prev:
                shutil.rmtree(root / name, ignore_errors=True)
    except OSError as e:
        raise TelosError(f"cannot clear stale game files in {target}: {e}") from None
    if log:
        log(f"cleared {len(stale)} stale item(s) a previous owner left in {target}"
            + (f"; its logs moved to {prev}" if logs else ""))
    return True


def session_dir(cfg: Mapping[str, Any], run: str) -> Path:
    """The run's session directory: shim.session_dir, or auto_session_dir() whatever the run is called."""
    sd = cfg["shim"]["session_dir"]
    if sd != "auto":
        return Path(os.path.expanduser(sd))
    return auto_session_dir()


def game_command(cfg: Mapping[str, Any], backend: str) -> tuple[list[str], Path]:
    """argv and working directory of the game. The exe is named relative to its directory, as a player's
    shortcut does, so its command line does not carry the install path."""
    g = cfg["game"]
    if backend == "simgame":
        exe = cfg["sim"]["simgame_exe"]
        exe_path = (PKG_ROOT / "native/simgame/build/SimGame.exe" if exe == "auto"
                    else Path(os.path.expanduser(exe)))
        cwd = exe_path.parent
    else:
        if not g["dir"]:
            raise TelosError("game.dir is not set; point it at the directory holding the exe")
        cwd = Path(os.path.expanduser(g["dir"]))
        exe_path = cwd / g["exe"]
        if not (cwd / "dinput8.dll").is_file():
            raise TelosError(f"{cwd / 'dinput8.dll'} is missing; place the shim build "
                             "(native/shim/build/dinput8.dll) in the game directory or set shim.dll")
    if not exe_path.is_file():
        raise TelosError(f"{exe_path} does not exist")
    if os.name == "nt":
        return [str(exe_path)], cwd           # native Windows: no wine, sh, env or setarch wrapper
    argv = [g["wine"], exe_path.name] if g["wine"] else [str(exe_path)]
    if g["launch_wrapper"] == "setarch_R":
        argv = ["setarch", os.uname().machine, "-R", *argv]
    return argv, cwd


_NT_PINNED_ENV = ("SYSTEMROOT", "SystemRoot", "WINDIR", "PATH", "USERPROFILE", "TEMP", "TMP", "APPDATA",
                  "LOCALAPPDATA", "COMPUTERNAME", "USERNAME")


def game_environ(cfg: Mapping[str, Any], port: int, session: Path, hz: int | None = None
                 ) -> dict[str, str]:
    """The game's whole environment. Pinned, it holds only HOME, USER and fixed values; no Wayland or
    session variables reach Wine, and XAUTHORITY only when the game uses the caller's own display."""
    g = cfg["game"]
    if g["pinned_env"] and os.name == "nt":
        env = {k: os.environ[k] for k in _NT_PINNED_ENV if k in os.environ}
    elif g["pinned_env"]:
        env = {k: os.environ[k] for k in ("HOME", "USER") if k in os.environ}
        env.update(LANG="C", PATH="/usr/bin:/bin", WINEDEBUG="-all")
        if g["display"] == "auto" and "XAUTHORITY" in os.environ:
            env["XAUTHORITY"] = os.environ["XAUTHORITY"]
    else:
        env = dict(os.environ)
    display = os.environ.get("DISPLAY", "") if g["display"] == "auto" else g["display"]
    if display:
        env["DISPLAY"] = display
    if g["wineprefix"]:
        env["WINEPREFIX"] = os.path.expanduser(g["wineprefix"])
    if g["dll_overrides"]:
        env["WINEDLLOVERRIDES"] = g["dll_overrides"]
    env.update(extra_env(cfg))
    if audio_env(cfg):
        env["KPTAS_AUDIO"] = audio_env(cfg)
    env["KPTAS_PORT"] = str(port)
    env["KPTAS_SESSION"] = ("Z:" + str(session.resolve()).replace("/", "\\") if g["wine"] and os.name != "nt"
                            else str(session))
    env["KPTAS_CLOCK"] = clock_env(cfg, hz)
    env["KPTAS_PRESENT"] = cfg["replay"]["present"]
    env.pop("KPTAS_GL_PROFILE", None)
    if cfg.get("capture", {}).get("gl_profile") is True:
        env["KPTAS_GL_PROFILE"] = "1"
    env["KPTAS_PORTABLE"] = portable_env(cfg)
    from kptas.tier.envpin import boundary_env     # kptas.tier imports this module
    env["KPTAS_BOUNDARY"] = boundary_env(cfg)
    return env


def _kpm_why(session: Path, mark: int | None, backend: str, returncode: int | None = None) -> str:
    """The KPM refusal when the shim refused to run at load (its log line or exit code), else "".
    A launch that refused for KPM raises this message alone."""
    if backend != "shim" or mark is None:
        return ""
    from kptas.backend import rig
    return rig.kpm_exit_diagnosis(session, mark, returncode)


@dataclasses.dataclass
class GameLink:
    """One game process (None when attached to an external one) and its shim connection.
    `cleanup` runs once the process is gone (it stops the wineprefix's wineserver)."""
    sock: socket.socket
    proc: subprocess.Popen | None
    session: Path
    port: int
    cleanup: Callable[[], None] | None = None
    # Set only by our controlled local launcher, never constructor/adoption.
    _ui_focus_anchor: Any = dataclasses.field(default=None, init=False, repr=False)

    def exit_status(self, timeout: float = 5.0) -> int | None:
        if self.proc is None:
            return None
        try:
            return self.proc.wait(timeout)
        except subprocess.TimeoutExpired:
            return None

    def close(self, grace: float = 5.0) -> int | None:
        try:
            self.sock.close()
        except OSError:
            pass
        if self.proc is None:
            return None
        status = self.exit_status(grace)
        if status is None:
            self.proc.kill()
            status = self.proc.wait()
        if self.cleanup is not None:
            self.cleanup()
        return status


def listen_socket(host: str, port: int, hint: str = "") -> socket.socket:
    """A loopback listener on exactly this port; a port in use is a TelosError.

    SO_REUSEADDR (POSIX) lets a fixed port be bound again while the previous
    epoch's connection sits in TIME_WAIT. It never admits a second listener.
    """
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if os.name == "posix":
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((host, port))
        srv.listen(1)
    except OSError as e:
        srv.close()
        raise TelosError(f"cannot listen on {host}:{port}: {e.strerror or e}"
                         + (f"; {hint}" if hint else "")) from None
    return srv


def open_link(cfg: Mapping[str, Any], backend: str, session: Path, *, attach: bool = False,
              hz: int | None = None, log: Callable[[str], None] | None = None) -> GameLink:
    """Listen, launch the game (unless attaching), and accept the shim's connection.

    Attach waits for an externally started game that already has KPTAS_PORT, so it
    needs a fixed shim.port. A port in use is an error; the supervisor never moves
    to another port.
    """
    sh = cfg["shim"]
    port = sh["port"]
    if attach and not port:
        raise TelosError("attach needs a fixed shim.port (0 picks a port the game cannot know)")
    session.mkdir(parents=True, exist_ok=True)
    srv = listen_socket(sh["listen"], port, "another session may own it")
    port = srv.getsockname()[1]
    proc = None
    cleanup = None
    focus_anchor = None
    log_mark = None                 # the shim log's size before this launch; None when attaching
    try:
        if not attach:
            if backend in PROCESS_BACKENDS:
                clear_stale_session(session, log)      # before log_mark: a moved shim log restarts at 0
            from kptas.backend import rig
            if backend in ("handsoff", "phasor"):   # the shim backend refuses in rig.prepare_launch
                try:
                    rig.refuse_kpm(cfg)              # read-only: KPTAS does not run with KPM installed
                except BackendError as e:
                    raise TelosError(str(e)) from None
            if backend == "shim":
                log_mark = rig.shim_log_size(session)
                try:
                    rig.prepare_launch(cfg)          # cold start from the pins, with the pinned shim
                except (BackendError, OSError) as e:
                    raise TelosError(f"cannot prepare the game directory: {e}") from None
                cleanup = functools.partial(rig.stop_wineserver, cfg)
            argv, cwd = game_command(cfg, backend)
            env = game_environ(cfg, port, session, hz)
            local_focus = backend == 'shim' and not rig.host_prefix(cfg)
            if local_focus:
                env['KPTAS_FOCUS_NONCE'] = secrets.token_hex(32)
            launched_env = dict(env)
            if backend in PROCESS_BACKENDS and os.name != "nt":
                from kptas.backend.rig import launch_argv
                argv = launch_argv(cfg, argv, cwd, env, session / "game.out")
                env = dict(os.environ)             # the command line carries the game's environment
            if not shutil.which(argv[0]) and not Path(argv[0]).is_file():
                raise TelosError(f"{argv[0]} not found on PATH")
            if os.name == "nt":
                with open(session / "game.out", "ab") as game_out:
                    proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=game_out,
                                            stderr=subprocess.STDOUT,
                                            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
            else:
                proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL, start_new_session=True)
            if local_focus:
                from kptas.telos.game_focus import launch_anchor
                try:
                    focus_anchor = launch_anchor(proc.pid, cfg, launched_env)
                except (BackendError, OSError, KeyError, ValueError, IndexError):
                    pass  # Unsupported shapes do not advertise focus admission.
            if log:
                log(f"launched pid {proc.pid}: {' '.join(argv[-2:])} in {cwd}; waiting on port {port}")
        elif log:
            log(f"waiting for the game on {sh['listen']}:{port}")
        deadline = time.monotonic() + sh["connect_timeout_s"]
        srv.settimeout(0.25)
        while True:
            try:
                conn, _ = srv.accept()
                break
            except TimeoutError:
                pass
            if proc is not None and proc.poll() is not None:
                raise TelosError(_kpm_why(session, log_mark, backend, proc.returncode) or
                                 f"the game exited with status {proc.returncode} before connecting")
            if time.monotonic() > deadline:
                raise TelosError(_kpm_why(session, log_mark, backend) or
                                 f"no connection within {sh['connect_timeout_s']} s")
        conn.settimeout(None)
        link = GameLink(conn, proc, session, port, cleanup)
        link._ui_focus_anchor = focus_anchor
        return link
    except BaseException:
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait()
        if cleanup is not None:
            cleanup()
        raise
    finally:
        srv.close()


# ---------------------------------------------------------------- score driving

class Sink(Protocol):
    """Receives what a run produces. The holorecord writer implements it."""

    def tick(self, report: StepReport, note: Any, records: list[StagingRecord],
             files: list[tuple[int, Any]] | None = None, blobs: list[tuple[str, bytes]] | None = None) -> None: ...

    def mark(self, name: str, tick: int) -> None: ...

    def epoch(self, epoch: int, tick: int, reason: str, exit_status: int | None,
              files: bytes | None = None) -> None: ...


def _captured_before(bundle: Any, tick: int, epoch: int) -> bool:
    """Whether a captured tick of `epoch` precedes `tick` on bundle's ancestry."""
    from kptas.types import TickFlag
    b, limit = bundle, tick
    while b is not None:
        for r in reversed(b.rows):
            if r.tick > limit:
                continue
            if r.epoch != epoch:
                return False
            if r.flags & TickFlag.CAPTURED:
                return True
        limit, b = min(limit, b.chain.tick_floor), b.parent
    return False


class BundleSink:
    """Turns staging batches into TICK records on a kptas.holo Bundle opened for writing.

    One TickRecord per TICK_END batch. Per-tick server_passes, subticks and input
    come from the semantic journal when it carries them; verbclass is the union
    the step reported. With no records (no ring), the report alone makes one
    uncaptured TICK per step. When the backend tracks its files, every tick from
    the first files tree on carries the tree in force at its gate (files_root).
    """

    def __init__(self, bundle: Any, declared: Tier, *, on_durable=None):
        self.bundle, self.declared = bundle, declared
        self.on_durable = on_durable
        self.last_durable = None
        self._durable_deadline = time.monotonic() + 15.0
        self.pmap: Any = None
        self.epoch_seen = -1
        self.ceiling = declared
        self.ticks = 0
        self.last_tick = -1
        self.last_virtual_clock = None
        self._tick_phase_profile = None
        self.first_vc = VerbClass.NONE  # bits for the next tick only (see resume)
        self.files_root: bytes | None = None
        self._stored: set[str] = set()   # file blobs this sink has put

    def resume(self, tick: int, restored: bool = False) -> None:
        """Continue a branch after its branch tick: the next TICK's refs apply to map(tick), in tick's epoch.
        `restored`: the seek wrote state back (restore, hybrid, semantic), so the first tick carries RESTORE
        even when a hybrid seek's replay steps already reported the bit.
        A branch tick the recording did not capture (capture cadence) has no map(tick), and the payloads of
        the live state there were never stored, so such a branch is refused here, before any write.
        Branch at a captured restore point K <= T and re-record K..T into the branch instead."""
        from kptas.types import NULL_DIGEST, FormatError, TickFlag
        self.first_vc = VerbClass.RESTORE if restored else VerbClass.NONE
        self.last_tick = tick
        self.last_virtual_clock = None
        self.epoch_seen = self.bundle.epoch_at(tick)
        try:
            self.pmap = self.bundle.map_at(tick)
        except (FormatError, KeyError):
            self.pmap = None          # uncaptured: the next captured tick must be a full sweep
        rec = self.bundle.record(tick)
        if self.pmap is None and not rec.flags & TickFlag.CAPTURED and _captured_before(self.bundle, tick, rec.epoch):
            # capture cadence skipped this tick inside a captured epoch: the next capture is a delta
            u = _unit(getattr(self.bundle, "cfg", None))
            raise RunError(f"{u} {tick} was not captured (capture cadence), so a branch cannot start there; "
                           "branch at the latest captured restore point at or before it and re-record "
                           f"up to the wanted {u}")
        self.files_root = rec.files_root if rec.flags & TickFlag.FILES and rec.files_root != NULL_DIGEST else None

    def _phase_call(self, name: str, fn, *args):
        profile = self._tick_phase_profile
        if profile is None:
            return fn(*args)
        started = time.perf_counter_ns()
        try:
            return fn(*args)
        finally:
            profile[name] = profile.get(name, 0) + time.perf_counter_ns() - started
            profile[name + '_calls'] = profile.get(name + '_calls', 0) + 1

    def tick(self, report: StepReport, note: Any, records: list[StagingRecord],
             files: list[tuple[int, Any]] | None = None, blobs: list[tuple[str, bytes]] | None = None) -> None:
        from kptas.backend.staging import batches
        from kptas.holo.bundle import EVENT_MARK
        from kptas.holo.pages import PageMap
        from kptas.types import NULL_DIGEST, PointReason, RecordFlag, TickFlag, TickRecord, sha256

        once = VerbClass.RESTORE | VerbClass.FILES_RESTORE   # written back at the gate before the step's first tick
        vc0, self.first_vc = report.verbclass | self.first_vc, VerbClass.NONE
        self.ceiling = min(self.ceiling, vc0.ceiling())
        text = note_text(note) if note is not None else "."
        held = note_text(dataclasses.replace(note, wheel=0, osev=None)) if note is not None else "."
        if blobs and self.bundle.store_cfg.get("pages") != "digests":
            # GL objects are fully admitted/stored before the ticks naming them (8.8).
            self._phase_call('put_files_ns', self.bundle.put_files, blobs)
            if self._tick_phase_profile is not None:
                detail = getattr(self.bundle, "last_put_files_profile", None)
                if detail is not None:
                    self._tick_phase_profile["put_files_detail"] = dict(detail)
        groups = list(batches(records))
        # An exit can arrive after the final recorded gate without another TICK_END.
        # Preserve its event/EPOC, but never append the branch point a second time.
        if not groups and not (report.outcome is ProcOutcome.EXITED and report.tick == self.last_tick):
            groups = [(report.tick, [], None, None)]
        trees = sorted(files or (), key=lambda f: f[0])
        for i, (t, pages, jrec, end) in enumerate(groups):
            flags = TickFlag.NONE
            while trees and trees[0][0] <= t:
                self.files_root = self._store_tree(trees.pop(0)[1])
            if self.files_root is not None:
                flags |= TickFlag.FILES
            if report.epoch != self.epoch_seen:
                flags |= TickFlag.EPOCH_START
                self.epoch_seen, self.pmap = report.epoch, None
            journal = jrec.data if jrec is not None else b""
            meta = _journal_fields(journal)
            refs = tuple(sorted((r.page_ref() for r in pages), key=lambda r: r.vaddr))
            payload = {r.vaddr: r.data for r in pages if r.data}
            root = NULL_DIGEST
            # capture cadence: a played gate the shim did not capture. Its pages arrive with the next
            # captured tick (a delta since the last capture), so the map is left as it is.
            skipped = end is not None and bool(end.flags & RecordFlag.UNCAPTURED)
            if skipped and refs:
                raise RunError(f"{_unit(getattr(self.bundle, 'cfg', None))} {t}: an uncaptured batch carries pages")
            if end is not None and not skipped:
                flags |= TickFlag.CAPTURED
                if self.pmap is None and not flags & TickFlag.EPOCH_START:
                    u = _unit(getattr(self.bundle, "cfg", None))
                    raise RunError(f"{u} {t}: a captured delta with no map before it; the branch {u} "
                                   f"{self.last_tick} was not captured (capture cadence): branch at a "
                                   f"captured restore point and re-record up to the wanted {u}")
                if self.pmap is None:
                    self.pmap = PageMap(refs)
                else:
                    self._phase_call('sink_map_apply_ns', self.pmap.apply, refs)
                root = self._phase_call('sink_map_root_ns', self.pmap.root)
            rp = end is not None and not skipped and bool(end.flags & RecordFlag.RESTORE_POINT)
            if rp:
                flags |= TickFlag.RESTORE_POINT
            last = t == report.tick
            sub = meta.get("subticks", report.subticks if last else 0)
            if sub:
                flags |= TickFlag.HOLD
            if not rp:
                reason = (PointReason.NOT_CAPTURED if end is None or skipped else
                          report.reason if last and report.reason != PointReason.NONE else
                          PointReason.OTHER)
            else:
                reason = PointReason.NONE
            n = text if i == 0 else held
            inp = meta.get("input")
            rec = TickRecord(
                tick=t, epoch=report.epoch, note=n, verbclass=vc0 if i == 0 else report.verbclass & ~once,
                server_passes=meta.get("server_passes", report.server_passes if last else 1),
                subticks=sub, flags=flags, reason=reason, state_root=root,
                files_root=self.files_root if self.files_root is not None else NULL_DIGEST,
                input_sha256=(bytes.fromhex(inp) if isinstance(inp, str) and len(inp) == 64
                              else sha256(n.encode())),
                journal_sha256=(sha256(journal) if journal else
                                report.journal_sha256 if last else NULL_DIGEST),
                pages=refs, journal=journal)
            if last and end is not None and not skipped and report.state_root not in (NULL_DIGEST, root):
                raise RunError(f"{_unit(getattr(self.bundle, 'cfg', None))} {t}: the backend reported state_root {report.state_root.hex()[:16]}"
                               f" but its records give {root.hex()[:16]}")
            self._phase_call('append_tick_ns', self.bundle.append_tick, rec, payload)
            if self._tick_phase_profile is not None:
                detail = getattr(self.bundle, "last_append_tick_profile", None)
                if detail is not None:
                    self._tick_phase_profile["append_tick_detail"] = dict(detail)
            self.last_tick = t
            virtual_us = meta.get('virtual_us')
            self.last_virtual_clock = ((t, virtual_us) if type(virtual_us) is int
                                       and 0 <= virtual_us < 1 << 64 else None)
            self.ticks += 1
            # Preserve report event ordering: an event-bearing batch becomes a
            # recoverable boundary only after its MARK records are appended.
            # capture cadence: timed checkpoints wait for a captured tick, so a durable boundary is a
            # restore point (an explicit or closing checkpoint is forced and is taken where it is)
            if not report.events and not skipped:
                self._phase_call('checkpoint_ns', self.checkpoint_if_due)
        terminal_attempt = any(e.name == "input.attempted_exit" for e in report.events)
        for e in report.events:     # a seek that resumes inside a note gets the events of its first ticks here
            self.bundle.mark(EVENT_MARK + (e.name if e.arg is None else f"{e.name}:{e.arg}"), e.tick)

        if terminal_attempt:
            # This marker is the only durable evidence for an input whose game
            # tick exited before producing a gate journal/root. Fence it now;
            # ordinary event marks retain the normal autosave cadence.
            self._phase_call('checkpoint_ns', self.checkpoint_if_due, force=True)
        elif not groups or not skipped:
            self._phase_call('checkpoint_ns', self.checkpoint_if_due)

    def checkpoint_if_due(self, force: bool = False, *, reuse_current: bool = False):
        """Serialized wall-time autosave; successful persistence is not native verification.

        reuse_current: a forced call (close) that finds the last successful checkpoint still current
        (same bundle and chain link, nothing pending, store clean) returns None instead of fencing
        the disk again; on_durable already fired for that checkpoint. A failed checkpoint never sets
        last_durable, so it is retried, not skipped."""
        if self.last_tick < 0 or (not force and time.monotonic() < self._durable_deadline):
            return None
        # Every journal record, including same-tick MARK/EPOC, advances the
        # chain. A paused writer with no new data need not repeat disk barriers
        # or issue another saved notification. Explicit saves still fence disk.
        if ((reuse_current or not force) and self.last_durable is not None
                and self.last_durable["bundle_id"] == self.bundle.bundle_id
                and self.last_durable["chain_link"] == self.bundle.chain.link.hex()
                and not self.bundle.pending and not self.bundle.store._dirty):
            self._durable_deadline = time.monotonic() + 15.0
            return None
        from kptas.durability import checkpoint
        result = checkpoint(self.bundle)
        self.last_durable = result
        self._durable_deadline = time.monotonic() + 15.0
        consume = getattr(self, '_gl_spool_consumer', None)
        if consume is not None:
            consume(result)
        if self.on_durable is not None:
            self.on_durable(result)
        return result

    def mark(self, name: str, tick: int) -> None:
        self.bundle.mark(name, max(tick, 0))

    def epoch(self, epoch: int, tick: int, reason: str, exit_status: int | None,
              files: bytes | None = None) -> None:
        """EPOC; files is the backend's files("snap") blob, stored so files_root resolves (8.3, 8.7)."""
        from kptas.types import NULL_DIGEST
        root = NULL_DIGEST if files is None else self._store_files(files)
        self.bundle.end_epoch(epoch, max(tick, 0), reason, exit_status, files_root=root)

    def _store_tree(self, tree: Any) -> bytes:
        """A backend files tree: (rows of (path, sha256, size, vtime), {sha256: bytes}), or a files("snap") blob."""
        if isinstance(tree, (bytes, bytearray)):
            return self._store_files(bytes(tree))
        rows, blobs = tree
        for path, digest, _s, _v in rows:
            if digest in self._stored:
                continue
            if self.bundle.put_file(blobs[digest]).hex() != digest:
                raise RunError(f"files tree: the bytes for {path} do not match their sha256")
            self._stored.add(digest)
        return self.bundle.put_file_tree(rows)

    def _store_files(self, blob: bytes) -> bytes:
        try:
            entries = json.loads(blob)["entries"]
            rows = []
            for path, digest, size, vtime, b64 in entries:
                data = base64.b64decode(b64, validate=True)
                if self.bundle.put_file(data).hex() != digest or len(data) != size:
                    raise ValueError(f"entry {path!r} does not match its sha256 or size")
                rows.append((path, digest, size, vtime))
        except (ValueError, KeyError, TypeError, binascii.Error) as e:
            raise RunError(f"the backend's files snapshot is malformed: {e}") from None
        return self.bundle.put_file_tree(rows)

    def close(self, seal: bool, backend_ceiling: Tier | None = None) -> None:
        """Seal with the tier computed so far. Publishing needs a judge receipt, so not yet."""
        try:
            self.checkpoint_if_due(force=True, reuse_current=True)
            if seal:
                cap = self.declared if backend_ceiling is None else backend_ceiling
                self.bundle.seal(min(self.ceiling, cap), False)
        finally:
            self.bundle.close()


def _journal_fields(journal: bytes) -> dict[str, Any]:
    if not journal.startswith(b"{"):
        return {}
    try:
        obj = json.loads(journal)
    except ValueError:
        return {}
    if not isinstance(obj, dict):
        return {}
    out = {k: obj[k] for k in ("server_passes", "subticks", "input") if k in obj}
    virtual_us = obj.get('v')
    if type(virtual_us) is int and 0 <= virtual_us < 1 << 64:
        out['virtual_us'] = virtual_us
    for short, name in (("sp", "server_passes"), ("st", "subticks")):   # the shim's short names
        if name not in out and isinstance(obj.get(short), int):
            out[name] = obj[short]
    return out


BackendFactory = Callable[[str, Mapping[str, Any], "GameLink | None"], Any]


def default_factory(name: str, cfg: Mapping[str, Any], link: GameLink | None) -> Any:
    return require("kptas.backend", "create").create(name, cfg, link=link)


@dataclasses.dataclass
class RunResult:
    ticks: int = 0              # last global tick, -1 when nothing ran
    epochs: int = 0
    reports: int = 0
    stopped: str = "end"        # end | max_ticks | halted | exit
    cursor: Any = None          # next score action at the stopped gate (runtime continuation)


def _unit(cfg: Mapping[str, Any] | None, plural: bool = False) -> str:
    """The word for one gate step in user-facing messages: frame unless clock.precision = "tick"."""
    from kptas.config import unit_name
    return unit_name((cfg or {}).get("clock", {}).get("precision"), plural)


def merge_oneshots(pending: Any, note: Any) -> Any:
    """Carry a count-0 note's one-shots (wheel, os event) onto the next note's first tick."""
    if pending is None:
        return note
    if pending.osev and note.osev:     # the compiler refuses this; never drop one silently
        raise RunError(f"!{pending.osev} and !{note.osev} land on the same position; one os event per position")
    return dataclasses.replace(note, wheel=pending.wheel + note.wheel,
                               osev=note.osev or pending.osev)


class Supervisor:
    """Owns the backend across epochs and keeps a global tick count over them."""

    def __init__(self, cfg: Mapping[str, Any], *, env: EnvPin | None = None,
                 backend: str | None = None, attach: bool = False,
                 factory: BackendFactory | None = None, sink: Sink | None = None,
                 run_name: str = "session", log: Callable[[str], None] | None = None):
        self.cfg = cfg
        self.env = env or EnvPin()
        self.name = backend or cfg["run"]["backend"]
        self.attach_mode = attach
        self.factory = factory or default_factory
        self.sink = sink
        self.run_name = run_name
        self.log = log or (lambda s: print(s, file=sys.stderr))
        self.tier = Tier.parse(cfg["run"]["tier"])
        self.mode = ClockMode(cfg["clock"]["mode"])
        self.hz = cfg["clock"]["hz"]
        self.scale = cfg["clock"]["scale"]
        self.backend: Any = None
        self.link: GameLink | None = None
        self.caps: Caps | None = None
        self.epoch = -1
        self.tick = -1
        self.offset = 0
        self.force_capture_every_tick = False   # capture cadence off for this drive (seek canary)
        self.alive = False
        self.epoch_closed = True
        self.last_osev: str | None = None
        self.carry: list[Any] = []       # events of the last report (of the whole note when resumed inside it)
        self.frames_dir: Path | None = None   # capture.frames; default the sink's bundle/frames
        self.next_save_ms: int | None = None
        self.save_ms: int | None = None  # the score header's save length; every new process gets it again
        self.last_journal: tuple[int, bytes] | None = None   # (global tick, entry) of the sink's latest journal

    # lifecycle
    def open(self, *, origin_tick: int | None = None, origin_epoch: int | None = None,
             files: bytes | None = None) -> StepReport:
        """Start a new epoch: launch or attach, hello, tier.lock, clock, start."""
        if self.backend is not None:
            raise TelosError("a backend is already open")
        if origin_tick is not None:
            self.tick = origin_tick - 1
        if origin_epoch is not None:
            self.epoch = origin_epoch - 1
        if self.name in PROCESS_BACKENDS:
            sess = session_dir(self.cfg, self.run_name) / f"epoch-{self.epoch + 1}"
            self.link = open_link(self.cfg, self.name, sess, attach=self.attach_mode,
                                  hz=self.hz, log=self.log)
        try:
            self.backend = self.factory(self.name, self.cfg, self.link)
            self.caps = self.backend.attach(self.cfg, self.env)
            if self.caps is not None and self.caps.tier_ceiling < self.tier:
                self.log(f"backend {self.name} tops out at {self.caps.tier_ceiling}; "
                         f"the run cannot count above it (declared {self.tier})")
            if files is not None:
                self.backend.files("put", files)
            origin = getattr(self.backend, "set_epoch_origin", None)
            if origin is not None and origin_tick is not None:
                origin(origin_tick, self.epoch + 1)
                if self.epoch >= 0:
                    if self.save_ms is not None:
                        self.backend.set_save_ms(self.save_ms)
                    if self.next_save_ms is not None:
                        self.backend.set_next_save_ms(self.next_save_ms)
            self.backend.lock_tier(self.tier)
            if self.name == "shim":
                self.backend.hud(self.cfg["hud"]["enabled"],
                                 watermark=self.cfg["hud"]["watermark"], offset=self.tick + 1)
            self.backend.clock(self.mode, self.hz, self.scale)
            framed = self._frames_setup()
            rep = self.backend.start()
            if self.name == "shim" and rep.tick != 0:
                raise TelosError(f"native HUD startup requires the initial gate at local {_unit(self.cfg)} 0")
        except BaseException:
            self._drop()
            raise
        self.epoch += 1
        self.offset = (self.tick + 1) - rep.tick
        if self.name == "shim":
            self.backend.hud(self.cfg["hud"]["enabled"], watermark=self.cfg["hud"]["watermark"],
                             offset=self.offset)
        if framed:
            self.backend.frames_note(offset=self.offset)
        self.alive, self.epoch_closed, self.last_osev = True, False, None
        return self._emit(rep, None)

    def frames_out(self) -> Path:
        """Where capture.frames goes: frames_dir, else the bundle's frames/, else the session's."""
        if self.frames_dir is not None:
            return self.frames_dir
        bundle = getattr(self.sink, "bundle", None)
        if bundle is not None and getattr(bundle, "root", None) is not None:
            return Path(bundle.root) / "frames"
        return session_dir(self.cfg, self.run_name) / "frames"

    def _frames_setup(self) -> bool:
        if (self.cfg["capture"]["frames"] == "off" and not self.cfg["capture"].get("frames_view", False)) or not hasattr(self.backend, "frames_dir"):
            return False
        if self.cfg["capture"]["frames"] != "off":
            self.backend.frames_dir = self.frames_out()
        self.backend.frames_epoch = self.epoch + 1
        return True

    def frames_to(self, d: Path) -> None:
        """Send frames from the next tick on to d (run --from: the branch bundle's frames/)."""
        self.frames_dir = d
        if self.backend is not None and hasattr(self.backend, "frames_retarget"):
            self.backend.frames_retarget(d)

    def probe(self) -> Caps:
        """hello only: launch or attach, read caps, then shut down without starting."""
        if self.name in PROCESS_BACKENDS:
            sess = session_dir(self.cfg, self.run_name) / "probe"
            self.link = open_link(self.cfg, self.name, sess, attach=self.attach_mode,
                                  hz=self.hz, log=self.log)
        try:
            self.backend = self.factory(self.name, self.cfg, self.link)
            return self.backend.attach(self.cfg, self.env)
        finally:
            self._drop()

    def close(self, reason: str = "close") -> None:
        """End the current epoch cleanly (bye) and report it."""
        if self.backend is None:
            return
        files = None if self.epoch_closed else self._files_snap()
        status = self._drop()
        if not self.epoch_closed:
            self._end_epoch(reason if self.alive else self._exit_reason(), status, files)

    def relaunch(self, reason: str = "relaunch") -> StepReport | None:
        """New epoch. A process backend gets a new process; an in-process backend (pysim)
        reboots in place through os_event(relaunch), which keeps its files overlay and tick
        count, and reports its boot tick with the next step."""
        if self.backend is None:
            rep = self.open()
            self._resave()
            return rep
        if self.name in PROCESS_BACKENDS:
            self.close(reason)
            rep = self.open()
            self._resave()
            return rep
        if not self.epoch_closed:
            self._end_epoch(reason if self.alive else self._exit_reason(), None, self._files_snap())
        before = self.tick
        self.backend.os_event(OsEvent.RELAUNCH)
        self.epoch += 1
        self.alive, self.epoch_closed, self.last_osev = True, False, None
        self._resave()
        try:
            self.carry = [e for e in self._shift(self.backend.events()) if e.tick > before]
        except BackendError:
            self.carry = []
        return None

    def _resave(self) -> None:
        """A new process starts with the default save length: give it the header's again, as at the first boot."""
        if self.save_ms is not None:
            self.save_length(self.save_ms, once=False)
        if self.next_save_ms is not None:
            self.save_length(self.next_save_ms, once=True)

    def _drop(self) -> int | None:
        b, self.backend = self.backend, None
        if b is not None:
            try:
                b.close()
            except Exception as e:
                self.log(f"backend close: {e}")
            if getattr(b, "frames_summary", ""):
                self.log(b.frames_summary)
        link, self.link = self.link, None
        return link.close() if link is not None else None

    def _files_snap(self) -> bytes | None:
        """The files overlay at epoch end, for EPOC files_root; None (logged) when unavailable."""
        if self.backend is None or self.sink is None:
            return None
        try:
            return self.backend.files("snap")
        except Exception as e:   # process already gone, handsoff, no session dir
            self.log(f"epoch {self.epoch}: no files snapshot ({type(e).__name__}: {e}); "
                     "EPOC files_root stays zero")
            return None

    def _exit_reason(self) -> str:
        return {"altf4": "altf4", "altf4@save": "altf4", "close": "close"}.get(self.last_osev or "", "crash")

    def _end_epoch(self, reason: str, status: int | None, files: bytes | None = None) -> None:
        self.epoch_closed = True
        if self.sink is not None:
            if files is None:
                self.sink.epoch(self.epoch, self.tick, reason, status)
            else:
                self.sink.epoch(self.epoch, self.tick, reason, status, files=files)

    def _emit(self, rep: StepReport, note: Any, keep_carry: bool = False) -> StepReport:
        native_tick, native_epoch = rep.tick, rep.epoch
        rep = dataclasses.replace(rep, tick=rep.tick + self.offset, epoch=self.epoch,
                                  events=self._shift(rep.events))
        self.tick = rep.tick
        if any(e.name == "save.begin" for e in rep.events):
            self.next_save_ms = None
        self.carry = (self.carry if keep_carry else []) + list(rep.events)
        if rep.outcome is ProcOutcome.EXITED:
            self.alive = False
        files = self._file_changes()
        gl = getattr(self.backend, "gl_blobs", None)
        profile = None
        if getattr(self.backend, '_tick_profile', None) is not None:
            profile = {'tick': rep.tick, 'epoch': rep.epoch,
                       'native_tick': native_tick, 'native_epoch': native_epoch,
                       'gl_blobs_ns': 0, 'gl_blobs_ns_calls': 0,
                       'put_files_ns': 0, 'put_files_ns_calls': 0,
                       'append_tick_ns': 0, 'append_tick_ns_calls': 0,
                       'checkpoint_ns': 0, 'checkpoint_ns_calls': 0}
            self.backend._profile_sink_phases = profile
        timed_sink = self.sink if isinstance(self.sink, BundleSink) else None
        if timed_sink is not None:
            timed_sink._tick_phase_profile = profile
        try:
            if profile is None or gl is None:
                blobs = gl() if gl is not None else []
            else:
                started = time.perf_counter_ns()
                try:
                    blobs = gl()
                finally:
                    profile['gl_blobs_ns'] += time.perf_counter_ns() - started
                    profile['gl_blobs_ns_calls'] += 1
            retain=getattr(self,'canary_frame_evidence',None)
            if retain is not None:retain(rep,blobs)
            if self.sink is not None:
                # Negotiate only the native staging transport. The sink remains
                # the sole owner of immutable admission and durable barriers.
                spool_consumer = getattr(self.backend, 'gl_spools_consumed', None)
                if isinstance(self.sink, BundleSink) and spool_consumer is not None:
                    target, offset = self.sink, self.offset
                    target._gl_spool_consumer = lambda durable, target=target, offset=offset, consumer=spool_consumer: consumer(
                        durable['tick'] - offset, target.bundle.store.has)
                extra: dict[str, Any] = {}
                if files:
                    extra["files"] = files
                if blobs:
                    extra["blobs"] = blobs
                recs = self._records()
                self._note_journal(recs)
                self.sink.tick(rep, note, recs, **extra)
                admitted = getattr(self.backend, 'gl_spools_admitted', None)
                if isinstance(self.sink, BundleSink) and admitted is not None:
                    admitted(self.sink.bundle.store.has)
        finally:
            if timed_sink is not None:
                timed_sink._tick_phase_profile = None
        presentation = getattr(self, 'presentation_report', None)
        if presentation is not None:
            try:
                presentation(self, rep, note)
            except Exception:
                # Presentation is never allowed to fail a committed native gate.
                channel = getattr(presentation, '__self__', None)
                if channel is not None:
                    channel.invalidate()
        return rep

    def _note_journal(self, recs: list[StagingRecord]) -> None:
        """Keep the newest journal entry handed to the sink, for @assert. Reads nothing from the game."""
        from kptas.types import RecordKind
        for r in reversed(recs):
            if r.kind is RecordKind.JOURNAL:
                self.last_journal = (r.tick, bytes(r.data))
                return

    def _assert(self, item: Any) -> None:
        """Evaluate @assert against the journal entry of the tick just played (the gate at self.tick).

        Only bytes the sink already received are read, so a recording and its judge replay see the same
        journal and the assert changes nothing in either. Without a sink (telos play) there is no journal:
        the assert is logged as not evaluated."""
        from kptas.score.assertion import evaluate, parse
        u = _unit(self.cfg)
        try:
            check = parse(item.expr)
        except ValueError as e:
            self.log(f"{_where(item)}: @assert {item.expr} not evaluated at {u} {self.tick}: "
                     f"it is a free-text note, not FIELD OP VALUE ({e})")
            return
        if self.sink is None:
            self.log(f"{_where(item)}: @assert {item.expr} not evaluated at {u} {self.tick}: "
                     "this run keeps no journal (use telos run)")
            return
        j = self.last_journal
        try:
            if j is None or j[0] != self.tick:
                raise ValueError("no journal entry was recorded for it")
            ok, actual = evaluate(check, j[1], self.tick)
        except ValueError as e:
            ok, msg = False, f"cannot be checked at {u} {self.tick}: {e}"
        else:
            msg = (f"failed at {u} {self.tick}: {check.field} is {actual}, "
                   f"expected {check.op} {check.value}" + (f" (tol {check.tol})" if check.tol else ""))
        if ok:
            return
        text = f"{_where(item)}: @assert {item.expr} {msg}"
        if check.on_fail == "report":
            self.log(text)
            return
        raise RunError(text)

    def _file_changes(self) -> list[tuple[int, Any]]:
        """(global tick, files tree) for every tick where the backend saw its files change."""
        fn = getattr(self.backend, "file_changes", None)
        if fn is None:
            return []
        return [(t + self.offset, tree) for t, tree in fn()]

    def _shift(self, events: Any) -> tuple[Any, ...]:
        """Engine events with backend-local ticks moved to global ticks."""
        return tuple(dataclasses.replace(e, tick=e.tick + self.offset) for e in events)

    def _records(self) -> list[StagingRecord]:
        try:
            recs = list(self.backend.records())
        except BackendError:
            return []
        if self.offset:
            recs = [dataclasses.replace(r, tick=r.tick + self.offset) for r in recs]
        return recs

    def _live(self) -> Any:
        if self.backend is None:
            raise TelosError("no backend is open")
        if not self.alive:
            raise RunError(f"the game exited at {_unit(self.cfg)} {self.tick}; the score needs @expect exit "
                           "and @relaunch before more input")
        return self.backend

    # ticks
    def step(self, n: int, note: Any, keep_carry: bool = False, capture_last: bool = True) -> StepReport:
        """keep_carry: this step finishes a note resumed inside it, so the note's earlier events still count.
        A backend with `note_first` is told that the note's first tick has already been played.
        capture_last=False (capture.every > 1, shim): the caller steps on at once, so the last tick is
        captured only if it is on the capture.every grid. Pause points must step with capture_last=True."""
        b = self._live()
        if note.osev:
            self.last_osev = note.osev
        self._cadence_prepare(b)
        kw = {} if capture_last or not getattr(b, "capture_cadence", False) else {"capture_last": False}
        if self.sink is not None and getattr(b, "streams_reports", False):
            rep=b.step(n,note,first=not keep_carry,on_report=self._stream_reports(note,keep_carry),**kw)
            return self._global_report(rep)
        rep = (b.step(n, note, first=False, **kw) if keep_carry and getattr(b, "note_first", False)
               else b.step(n, note, **kw))
        return self._emit(rep, note, keep_carry)

    def _cadence_prepare(self, b: Any) -> None:
        """Capture cadence: the grid is on global ticks (tick + offset), as keyframes are, and every grid
        point must be able to carry the recording bundle's keyframes."""
        if not getattr(b, "capture_cadence", False):
            return
        b.capture_offset = self.offset
        b.capture_force_every_tick = self.force_capture_every_tick
        every = int(self.cfg["capture"]["every"])
        bundle = getattr(self.sink, "bundle", None)
        if every > 1 and bundle is not None and getattr(self, "_cadence_checked", None) is not bundle:
            ke = bundle.header.keyframe_every
            if ke > 0 and ke % every:
                raise RunError(f"capture.every {every} does not divide this recording's keyframe_every {ke}")
            self._cadence_checked = bundle

    def _global_report(self, rep: StepReport) -> StepReport:
        return dataclasses.replace(rep,tick=rep.tick+self.offset,epoch=self.epoch,events=self._shift(rep.events))

    def _stream_reports(self, note: Any, keep_carry: bool = False):
        """Drain pages/files/GL and checkpoint at each stopped gate of one logical action."""
        seen=False
        held=dataclasses.replace(note,wheel=0,osev=None) if note is not None else None
        def emit(rep: StepReport, first: bool) -> None:
            nonlocal seen
            self._emit(rep,note if first else held,keep_carry or seen)
            seen=True
        return emit

    def wait(self, event: str, arg: str | None, timeout: int, note: Any = None) -> StepReport:
        backend=self._live()
        self._cadence_prepare(backend)
        if self.sink is not None and getattr(backend,"streams_reports",False):
            rep=backend.wait(event,arg,timeout,note,on_report=self._stream_reports(note))
            return self._global_report(rep)
        return self._emit(backend.wait(event,arg,timeout,note),note)

    def os_event(self, ev: OsEvent) -> ProcOutcome:
        out = self._live().os_event(ev)
        self.last_osev = ev.value
        if out is ProcOutcome.EXITED:
            self.alive = False
        return out

    def save_length(self, ms: int, once: bool) -> None:
        """How much virtual time an in-game save takes: every save, or only the next one (`@save ms=`)."""
        fn = getattr(self._live(), "set_next_save_ms" if once else "set_save_ms", None)
        if fn is None:
            raise RunError(f"the {self.name} backend cannot set how long a save takes yet")
        fn(ms)
        if once:
            self.next_save_ms = ms

    def set_clock(self, hz: int, scale: int) -> None:
        if (hz, scale) != (self.hz, self.scale):
            self.hz, self.scale = hz, scale
            if self.backend is not None:
                self.backend.clock(self.mode, hz, scale)

    # scores
    def run_score(self, compiled: Any, *, max_ticks: int = 0,
                  on_report: Callable[[StepReport, Any], None] | None = None,
                  start: Any = None) -> RunResult:
        """Drive every item of a CompiledScore. Opens the first epoch if needed.

        `start` (a kptas.holo.seek Cursor) resumes a live game that a seek left at the cursor's tick:
        from item start.i with start.j of its ticks already played, at the cursor's clock rate.
        """
        from kptas.score import Assert, Expect, Mark, Note, Relaunch, SaveMs, Segment, Sync
        from kptas.holo.seek import Cursor

        res = RunResult()
        default_timeout = self.cfg["score"]["default_timeout_ticks"]
        header_hz = compiled.header.get("hz")
        base_hz = int(header_hz) if header_hz else self.cfg["clock"]["hz"]   # 5.5 precedence
        if start is not None:
            # the seek left the game at this rate; a clock verb here would start a new segment at tick+1
            self.hz, self.scale = start.hz, start.scale
        elif header_hz:
            self.set_clock(base_hz, self.scale)
        if self.backend is None:
            rep = self.open()
            if on_report:
                on_report(rep, None)
        # A resumed run already has the header's save length when the backend keeps it in the rewound state
        # (the seek set it, or the restored tick holds it); setting it again would journal a new input.
        resumed = start is not None and (start.i, start.j) != (0, 0)
        hs = compiled.header.get("save_ms")
        self.save_ms = int(hs) if hs is not None else None
        if hs is not None and not (resumed and getattr(self.backend, "save_ms_rewound", False)):
            self.save_length(int(hs), once=False)
        pace, wall0, pending = "gate", None, None
        first, played = (start.i, start.j) if start is not None else (0, 0)
        if start is not None:
            pending = start.pending
            seg = next((it for it in reversed(compiled.items[:first]) if isinstance(it, Segment)), None)
            pace = (seg.pace if seg is not None else None) or "gate"

        def emit(rep: StepReport, note: Any) -> None:
            res.reports += 1
            if on_report:
                on_report(rep, note)

        def cursor(index, played_ticks=0):
            return Cursor(index, played_ticks, pending, self.hz, self.scale,
                          tuple((e.name, e.arg) for e in self.carry), self.next_save_ms, self.last_osev)

        for idx in range(first, len(compiled.items)):
            res.cursor = cursor(idx, played if idx == first else 0)
            item = compiled.items[idx]
            if max_ticks and self.tick >= max_ticks:
                res.stopped = "max_ticks"
                break
            if isinstance(item, Segment):
                # an omitted hz falls back to the header, then [clock] hz; never the previous segment
                self.set_clock(item.hz or base_hz, item.scale or self.cfg["clock"]["scale"])
                pace = item.pace or "gate"
                wall0 = None
            elif isinstance(item, Note):
                if item.count == 0:
                    pending = merge_oneshots(pending, item)
                    continue
                note, pending = merge_oneshots(pending, item), None
                n = item.count
                if idx == first and played:
                    # resumed inside this note: its one-shots fired on its first tick, before the seek target
                    note, n = dataclasses.replace(note, wheel=0, osev=None), n - played
                if max_ticks:
                    n = min(n, max_ticks - self.tick)
                if pace == "realtime":
                    wall0 = wall0 or (time.monotonic(), self.tick)
                emit(self.step(n, note, keep_carry=idx == first and played > 0), note)
                consumed = (played if idx == first else 0) + n
                if pace == "realtime":
                    ahead = wall0[0] + (self.tick - wall0[1]) / self.hz - time.monotonic()
                    if ahead > 0:
                        time.sleep(ahead)
                if self.alive and consumed < item.count:
                    res.cursor = cursor(idx, consumed)
                    res.stopped = "max_ticks"
                    break
            elif isinstance(item, Sync):
                def hit(evs: Any) -> bool:
                    return any(e.name == item.event and (item.arg is None or e.arg == item.arg)
                               for e in evs)
                # the event may already have fired in the ticks just run (a boot's main_menu)
                if hit(self.carry):
                    self.carry = []
                    continue
                rep = self.wait(item.event, item.arg, item.timeout or default_timeout)
                emit(rep, None)
                self.carry = []
                if not hit(rep.events):
                    what = item.event + (f" {item.arg}" if item.arg else "")
                    raise RunError(f"{_where(item)}: @sync {what} timed out at {_unit(self.cfg)} {self.tick}")
            elif isinstance(item, Expect):
                self._expect(item, item.timeout or default_timeout, emit)
            elif isinstance(item, Relaunch):
                if item.cond == "exit" and self.alive:
                    continue
                if not self.cfg["game"]["relaunch"]:
                    res.stopped = "halted"
                    self.log(f"{_where(item)}: game.relaunch is false; stopping here")
                    break
                rep = self.relaunch("relaunch")
                if rep is not None:
                    emit(rep, None)
            elif isinstance(item, Mark):
                if self.sink is not None:
                    self.sink.mark(item.name, self.tick)
            elif isinstance(item, SaveMs):
                self.save_length(item.ms, once=True)
            elif isinstance(item, Assert):
                self._assert(item)
        else:
            res.cursor = cursor(len(compiled.items))
        if pending is not None and res.stopped == "end":
            self.log("a trailing count-0 note had nothing to attach to; its one-shots were dropped")
        if not self.alive and res.stopped == "end":
            res.stopped = "exit"            # the game quit (!close, !altf4 or a crash) and nothing relaunched it
        res.ticks, res.epochs = self.tick, self.epoch + 1
        return res

    def _expect(self, item: Any, timeout: int, emit: Callable[[StepReport, Any], None]) -> None:
        want = set(item.outcome.split("|"))
        if self.alive and "exit" in want:
            emit(self.wait("proc.down", None, timeout, None), None)
        if not self.alive:
            if "exit" not in want:
                raise RunError(f"{_where(item)}: expected the game alive, it exited at {_unit(self.cfg)} {self.tick}")
            if not self.epoch_closed:
                files = self._files_snap()
                status = self._drop() if self.name in PROCESS_BACKENDS else None
                self._end_epoch(self._exit_reason(), status, files)
        elif "alive" not in want:
            raise RunError(f"{_where(item)}: expected the game to exit within {timeout} {_unit(self.cfg, True)}")


def _where(item: Any) -> str:
    return f"{item.source}:{item.line}"


def note_text(note: Any) -> str:
    """Canonical note text without its count, e.g. 'w lmb'."""
    from kptas.score.fmt import render
    return render(dataclasses.replace(note, count=1)).split(None, 1)[1]


def parse_note(text: str, count: int = 1) -> Any:
    """Parse one canonical note (keys, buttons, m@x,y, md, wh, !event) into a Note."""
    from kptas.score import Note, compile_score
    items = compile_score(f"#!score v1\n{count} {text.strip() or '.'}\n").items
    if len(items) != 1 or not isinstance(items[0], Note):
        raise TelosError(f"not a single note: {text!r}")
    return items[0]


__all__ = ["BundleSink", "GameLink", "RunError", "RunResult", "Sink", "Supervisor", "TelosError", "audio_env",
           "auto_session_dir", "clear_stale_session", "clock_env", "default_factory", "extra_env", "filetime", "game_command", "game_environ",
           "merge_oneshots", "note_text", "open_link", "parse_note", "portable_env", "portable_values", "require",
           "session_dir"]
