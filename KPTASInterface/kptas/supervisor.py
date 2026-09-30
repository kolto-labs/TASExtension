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
import importlib
import json
import os
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
             "uptime0_ms": c["uptime0_ms"], "initial_realtime": filetime(c["initial_realtime"])}
    return ";".join(f"{k}={v}" for k, v in parts.items())


def session_dir(cfg: Mapping[str, Any], run: str) -> Path:
    sd = cfg["shim"]["session_dir"]
    if sd != "auto":
        return Path(os.path.expanduser(sd))
    base = os.environ.get("XDG_RUNTIME_DIR") or os.path.expanduser("~/.cache")
    return Path(base) / "kptas" / run


def game_command(cfg: Mapping[str, Any], backend: str) -> tuple[list[str], Path]:
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
                             "(native/shim/build/dinput8.dll) in the game directory")
    if not exe_path.is_file():
        raise TelosError(f"{exe_path} does not exist")
    argv = [g["wine"], str(exe_path)] if g["wine"] else [str(exe_path)]
    if g["launch_wrapper"] == "setarch_R":
        argv = ["setarch", os.uname().machine, "-R", *argv]
    return argv, cwd


def game_environ(cfg: Mapping[str, Any], port: int, session: Path, hz: int | None = None
                 ) -> dict[str, str]:
    g = cfg["game"]
    if g["pinned_env"]:
        keep = ("PATH", "HOME", "USER", "XAUTHORITY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR")
        env = {k: os.environ[k] for k in keep if k in os.environ}
        env.update(LANG="C", LC_ALL="C", TZ="UTC", WINEDEBUG="-all")
    else:
        env = dict(os.environ)
    display = os.environ.get("DISPLAY", "") if g["display"] == "auto" else g["display"]
    if display:
        env["DISPLAY"] = display
    if g["wineprefix"]:
        env["WINEPREFIX"] = os.path.expanduser(g["wineprefix"])
    if g["dll_overrides"]:
        env["WINEDLLOVERRIDES"] = g["dll_overrides"]
    env["KPTAS_PORT"] = str(port)
    env["KPTAS_SESSION"] = str(session)
    env["KPTAS_CLOCK"] = clock_env(cfg, hz)
    return env


@dataclasses.dataclass
class GameLink:
    """One game process (None when attached to an external one) and its shim connection."""
    sock: socket.socket
    proc: subprocess.Popen | None
    session: Path
    port: int

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
    try:
        if not attach:
            argv, cwd = game_command(cfg, backend)
            env = game_environ(cfg, port, session, hz)
            if not shutil.which(argv[0]) and not Path(argv[0]).is_file():
                raise TelosError(f"{argv[0]} not found on PATH")
            proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL)
            if log:
                log(f"launched pid {proc.pid}: {' '.join(argv)}; waiting on port {port}")
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
                raise TelosError(f"the game exited with status {proc.returncode} before connecting")
            if time.monotonic() > deadline:
                raise TelosError(f"no connection within {sh['connect_timeout_s']} s")
        conn.settimeout(None)
        return GameLink(conn, proc, session, port)
    except BaseException:
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait()
        raise
    finally:
        srv.close()


# ---------------------------------------------------------------- score driving

class Sink(Protocol):
    """Receives what a run produces. The holorecord writer implements it."""

    def tick(self, report: StepReport, note: Any, records: list[StagingRecord]) -> None: ...

    def mark(self, name: str, tick: int) -> None: ...

    def epoch(self, epoch: int, tick: int, reason: str, exit_status: int | None,
              files: bytes | None = None) -> None: ...


class BundleSink:
    """Turns staging batches into TICK records on a kptas.holo Bundle opened for writing.

    One TickRecord per TICK_END batch. Per-tick server_passes, subticks and input
    come from the semantic journal when it carries them; verbclass is the union
    the step reported. With no records (no ring), the report alone makes one
    uncaptured TICK per step.
    """

    def __init__(self, bundle: Any, declared: Tier):
        self.bundle, self.declared = bundle, declared
        self.pmap: Any = None
        self.epoch_seen = -1
        self.ceiling = declared
        self.ticks = 0
        self.first_vc = VerbClass.NONE  # bits for the next tick only (see resume)

    def resume(self, tick: int, restored: bool = False) -> None:
        """Continue a branch after its branch tick: the next TICK's refs apply to map(tick), in tick's epoch.
        `restored`: the seek wrote state back (restore, hybrid, semantic), so the first tick carries RESTORE
        even when a hybrid seek's replay steps already reported the bit."""
        from kptas.types import FormatError
        self.first_vc = VerbClass.RESTORE if restored else VerbClass.NONE
        self.epoch_seen = self.bundle.epoch_at(tick)
        try:
            self.pmap = self.bundle.map_at(tick)
        except (FormatError, KeyError):
            self.pmap = None          # uncaptured: the next captured tick must be a full sweep

    def tick(self, report: StepReport, note: Any, records: list[StagingRecord]) -> None:
        from kptas.backend.staging import batches
        from kptas.holo.bundle import EVENT_MARK
        from kptas.holo.pages import PageMap
        from kptas.types import NULL_DIGEST, PointReason, RecordFlag, TickFlag, TickRecord, sha256

        once = VerbClass.RESTORE | VerbClass.FILES_RESTORE   # written back at the gate before the step's first tick
        vc0, self.first_vc = report.verbclass | self.first_vc, VerbClass.NONE
        self.ceiling = min(self.ceiling, vc0.ceiling())
        text = note_text(note) if note is not None else "."
        held = note_text(dataclasses.replace(note, wheel=0, osev=None)) if note is not None else "."
        groups = list(batches(records)) or [(report.tick, [], None, None)]
        for i, (t, pages, jrec, end) in enumerate(groups):
            flags = TickFlag.NONE
            if report.epoch != self.epoch_seen:
                flags |= TickFlag.EPOCH_START
                self.epoch_seen, self.pmap = report.epoch, None
            journal = jrec.data if jrec is not None else b""
            meta = _journal_fields(journal)
            refs = tuple(sorted((r.page_ref() for r in pages), key=lambda r: r.vaddr))
            payload = {r.vaddr: r.data for r in pages if r.data}
            root = NULL_DIGEST
            if end is not None:
                flags |= TickFlag.CAPTURED
                if self.pmap is None:
                    self.pmap = PageMap(refs)
                else:
                    self.pmap.apply(refs)
                root = self.pmap.root()
            rp = end is not None and bool(end.flags & RecordFlag.RESTORE_POINT)
            if rp:
                flags |= TickFlag.RESTORE_POINT
            last = t == report.tick
            sub = meta.get("subticks", report.subticks if last else 0)
            if sub:
                flags |= TickFlag.HOLD
            if not rp:
                reason = (PointReason.NOT_CAPTURED if end is None else
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
                input_sha256=(bytes.fromhex(inp) if isinstance(inp, str) and len(inp) == 64
                              else sha256(n.encode())),
                journal_sha256=(sha256(journal) if journal else
                                report.journal_sha256 if last else NULL_DIGEST),
                pages=refs, journal=journal)
            if last and end is not None and report.state_root not in (NULL_DIGEST, root):
                raise RunError(f"tick {t}: the backend reported state_root {report.state_root.hex()[:16]}"
                               f" but its records give {root.hex()[:16]}")
            self.bundle.append_tick(rec, payload)
            self.ticks += 1
        for e in report.events:     # a seek that resumes inside a note gets the events of its first ticks here
            self.bundle.mark(EVENT_MARK + (e.name if e.arg is None else f"{e.name}:{e.arg}"), e.tick)

    def mark(self, name: str, tick: int) -> None:
        self.bundle.mark(name, max(tick, 0))

    def epoch(self, epoch: int, tick: int, reason: str, exit_status: int | None,
              files: bytes | None = None) -> None:
        """EPOC; files is the backend's files("snap") blob, stored so files_root resolves (8.3, 8.7)."""
        from kptas.types import NULL_DIGEST
        root = NULL_DIGEST if files is None else self._store_files(files)
        self.bundle.end_epoch(epoch, max(tick, 0), reason, exit_status, files_root=root)

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
    return {k: obj[k] for k in ("server_passes", "subticks", "input") if k in obj}


BackendFactory = Callable[[str, Mapping[str, Any], "GameLink | None"], Any]


def default_factory(name: str, cfg: Mapping[str, Any], link: GameLink | None) -> Any:
    return require("kptas.backend", "create").create(name, cfg, link=link)


@dataclasses.dataclass
class RunResult:
    ticks: int = 0              # last global tick, -1 when nothing ran
    epochs: int = 0
    reports: int = 0
    stopped: str = "end"        # end | max_ticks | halted


def merge_oneshots(pending: Any, note: Any) -> Any:
    """Carry a count-0 note's one-shots (wheel, os event) onto the next note's first tick."""
    if pending is None:
        return note
    if pending.osev and note.osev:     # the compiler refuses this; never drop one silently
        raise RunError(f"!{pending.osev} and !{note.osev} land on the same tick; one os event per tick")
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
        self.alive = False
        self.epoch_closed = True
        self.last_osev: str | None = None
        self.carry: list[Any] = []       # events of the last report (of the whole note when resumed inside it)

    # lifecycle
    def open(self) -> StepReport:
        """Start a new epoch: launch or attach, hello, tier.lock, clock, start."""
        if self.backend is not None:
            raise TelosError("a backend is already open")
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
            self.backend.lock_tier(self.tier)
            self.backend.clock(self.mode, self.hz, self.scale)
            rep = self.backend.start()
        except BaseException:
            self._drop()
            raise
        self.epoch += 1
        self.offset = (self.tick + 1) - rep.tick
        self.alive, self.epoch_closed, self.last_osev = True, False, None
        return self._emit(rep, None)

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
            return self.open()
        if self.name in PROCESS_BACKENDS:
            self.close(reason)
            return self.open()
        if not self.epoch_closed:
            self._end_epoch(reason if self.alive else self._exit_reason(), None, self._files_snap())
        before = self.tick
        self.backend.os_event(OsEvent.RELAUNCH)
        self.epoch += 1
        self.alive, self.epoch_closed, self.last_osev = True, False, None
        try:
            self.carry = [e for e in self._shift(self.backend.events()) if e.tick > before]
        except BackendError:
            self.carry = []
        return None

    def _drop(self) -> int | None:
        b, self.backend = self.backend, None
        if b is not None:
            try:
                b.close()
            except Exception as e:
                self.log(f"backend close: {e}")
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
        return {"altf4": "altf4", "close": "close"}.get(self.last_osev or "", "crash")

    def _end_epoch(self, reason: str, status: int | None, files: bytes | None = None) -> None:
        self.epoch_closed = True
        if self.sink is not None:
            if files is None:
                self.sink.epoch(self.epoch, self.tick, reason, status)
            else:
                self.sink.epoch(self.epoch, self.tick, reason, status, files=files)

    def _emit(self, rep: StepReport, note: Any, keep_carry: bool = False) -> StepReport:
        rep = dataclasses.replace(rep, tick=rep.tick + self.offset, epoch=self.epoch,
                                  events=self._shift(rep.events))
        self.tick = rep.tick
        self.carry = (self.carry if keep_carry else []) + list(rep.events)
        if rep.outcome is ProcOutcome.EXITED:
            self.alive = False
        if self.sink is not None:
            self.sink.tick(rep, note, self._records())
        return rep

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
            raise RunError(f"the game exited at tick {self.tick}; the score needs @expect exit "
                           "and @relaunch before more input")
        return self.backend

    # ticks
    def step(self, n: int, note: Any, keep_carry: bool = False) -> StepReport:
        """keep_carry: this step finishes a note resumed inside it, so the note's earlier events still count."""
        b = self._live()
        if note.osev:
            self.last_osev = note.osev
        return self._emit(b.step(n, note), note, keep_carry)

    def wait(self, event: str, arg: str | None, timeout: int, note: Any = None) -> StepReport:
        return self._emit(self._live().wait(event, arg, timeout, note), note)

    def os_event(self, ev: OsEvent) -> ProcOutcome:
        out = self._live().os_event(ev)
        self.last_osev = ev.value
        if out is ProcOutcome.EXITED:
            self.alive = False
        return out

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
        from kptas.score import Assert, Expect, Mark, Note, Relaunch, Segment, Sync

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

        for idx in range(first, len(compiled.items)):
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
                if pace == "realtime":
                    ahead = wall0[0] + (self.tick - wall0[1]) / self.hz - time.monotonic()
                    if ahead > 0:
                        time.sleep(ahead)
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
                    raise RunError(f"{_where(item)}: @sync {what} timed out at tick {self.tick}")
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
            elif isinstance(item, Assert):
                self.log(f"{_where(item)}: @assert {item.expr} is not evaluated by this runner")
        if pending is not None:
            self.log("a trailing count-0 note had nothing to attach to; its one-shots were dropped")
        res.ticks, res.epochs = self.tick, self.epoch + 1
        return res

    def _expect(self, item: Any, timeout: int, emit: Callable[[StepReport, Any], None]) -> None:
        want = set(item.outcome.split("|"))
        if self.alive and "exit" in want:
            emit(self._emit(self.backend.wait("proc.down", None, timeout, None), None), None)
        if not self.alive:
            if "exit" not in want:
                raise RunError(f"{_where(item)}: expected the game alive, it exited at tick {self.tick}")
            if not self.epoch_closed:
                files = self._files_snap()
                status = self._drop() if self.name in PROCESS_BACKENDS else None
                self._end_epoch(self._exit_reason(), status, files)
        elif "alive" not in want:
            raise RunError(f"{_where(item)}: expected the game to exit within {timeout} ticks")


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


__all__ = ["BundleSink", "GameLink", "RunError", "RunResult", "Sink", "Supervisor", "TelosError", "clock_env",
           "default_factory", "filetime", "game_command", "game_environ", "merge_oneshots",
           "note_text", "open_link", "parse_note", "require", "session_dir"]
