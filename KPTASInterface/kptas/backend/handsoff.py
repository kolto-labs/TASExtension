"""Hands-off backend: OS-level input through xdotool and frames through ffmpeg x11grab.

No in-process code, no wire, wall clock. Inside a distrobox the host tools may
be missing; each tool then runs behind a command prefix (``host-spawn`` by
default when it exists). KPTAS_HOST_PREFIX or the ``prefix`` argument override
the choice for every tool.
"""

from __future__ import annotations

import hashlib
import os
import shlex
import shutil
import subprocess
import time
from collections import deque
from collections.abc import Iterable, Iterator, Mapping, Sequence
from typing import Any, Literal

from kptas.types import (BackendError, Caps, ClockMode, EngineEvent, EnvPin, OsEvent, PointReason,
                         ProcOutcome, Receipt, SeekMode, StagingRecord, StepReport, Tier, VerbClass)

# canonical kptas key name -> X keysym
_SPECIAL = {
    "up": "Up", "down": "Down", "left": "Left", "right": "Right",
    "lshift": "Shift_L", "rshift": "Shift_R", "lctrl": "Control_L", "rctrl": "Control_R",
    "lalt": "Alt_L", "ralt": "Alt_R", "esc": "Escape", "tab": "Tab", "space": "space",
    "enter": "Return", "backspace": "BackSpace", "nummul": "KP_Multiply", "numadd": "KP_Add",
    "numsub": "KP_Subtract", "numdec": "KP_Decimal", "numdiv": "KP_Divide", "numenter": "KP_Enter",
    "numlock": "Num_Lock", "minus": "minus", "equals": "equal", "lbracket": "bracketleft",
    "rbracket": "bracketright", "backslash": "backslash", "semicolon": "semicolon",
    "apostrophe": "apostrophe", "grave": "grave", "comma": "comma", "period": "period",
    "slash": "slash", "home": "Home", "end": "End", "pgup": "Prior", "pgdn": "Next",
    "ins": "Insert", "del": "Delete", "capslock": "Caps_Lock", "scrolllock": "Scroll_Lock",
    "pause": "Pause", "prtsc": "Print",
}
_BUTTON = {"lmb": "1", "mmb": "2", "rmb": "3"}


def keysym(name: str) -> str:
    if name in _SPECIAL:
        return _SPECIAL[name]
    if name.startswith("num") and name[3:].isdigit():
        return "KP_" + name[3:]
    if len(name) >= 2 and name[0] == "f" and name[1:].isdigit():
        return "F" + name[1:]
    if len(name) == 1 and name.isalnum():
        return name
    raise BackendError(f"no X keysym for key {name!r}")


class HandsOff:
    """Backend ``handsoff`` (tier hands-off only)."""

    name = "handsoff"

    def __init__(self, prefix: Sequence[str] | None = None, display: str | None = None,
                 window_name: str = "Star Wars", launch: bool = True, command: Sequence[str] | None = None,
                 dry_run: bool = False):
        env_prefix = os.environ.get("KPTAS_HOST_PREFIX")
        self.prefix = list(prefix) if prefix is not None else (
            shlex.split(env_prefix) if env_prefix is not None else None)
        self.display = display or os.environ.get("DISPLAY", ":0")
        self.window_name = window_name
        self.launch = launch
        self.command = list(command) if command else None
        self.dry_run = dry_run            # build commands without running them
        self.issued: list[list[str]] = []
        self.cfg: Mapping[str, Any] = {}
        self.hz = 60
        self.fps = 60
        self.wid: str | None = None
        self.proc: subprocess.Popen[bytes] | None = None
        self._rec: subprocess.Popen[bytes] | None = None
        self._t0 = 0.0
        self._tick = 0
        self._epoch = 0
        self._held: set[str] = set()
        self._buttons: set[str] = set()
        self._mouse: tuple[int, int] | None = None
        self._events: deque[EngineEvent] = deque()
        self._started = False
        self._exited = False
        self._caps = Caps(backend="handsoff", tier_ceiling=Tier.HANDS_OFF,
                          clock_modes=frozenset({ClockMode.WALL}), seek_modes=frozenset({SeekMode.REPLAY}),
                          input_paths=frozenset({"os"}), thread_models=frozenset({"free"}), tick_source="none")

    # ------------------------------------------------------------------ commands

    def argv(self, tool: str, *args: str) -> list[str]:
        """Full argv for one host tool, prefix included when the tool is not on PATH here."""
        prefix = self.prefix
        if prefix is None:
            prefix = [] if shutil.which(tool) else (["host-spawn"] if shutil.which("host-spawn") else [])
        env = ["env", f"DISPLAY={self.display}"] if prefix else []
        return [*prefix, *env, tool, *args]

    def _run(self, tool: str, *args: str, capture: bool = False, timeout: float = 10) -> bytes:
        cmd = self.argv(tool, *args)
        self.issued.append(cmd)
        if self.dry_run:
            return b""
        env = {**os.environ, "DISPLAY": self.display}
        try:
            res = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout, env=env)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise BackendError(f"{tool} failed: {e}") from None
        if res.returncode != 0:
            raise BackendError(f"{tool} exited {res.returncode}: {res.stderr.decode(errors='replace').strip()}")
        return res.stdout if capture else b""

    def xdo(self, *args: str, capture: bool = False) -> bytes:
        return self._run(self.cfg.get("handsoff", {}).get("input_tool", "xdotool"), *args, capture=capture)

    # ------------------------------------------------------------------ lifecycle

    @property
    def caps(self) -> Caps:
        return self._caps

    def attach(self, cfg: Mapping[str, Any], env: EnvPin | None = None) -> Caps:
        self.cfg = cfg
        ho = cfg.get("handsoff", {})
        if ho.get("input_tool", "xdotool") != "xdotool" or ho.get("capture", "x11grab") != "x11grab":
            raise BackendError("handsoff supports input_tool xdotool and capture x11grab only")
        self.fps = int(ho.get("capture_fps", 60))
        self.hz = int(cfg.get("clock", {}).get("hz", 60))
        if self.launch:
            self._launch()
        self.wid = self._find_window()
        return self._caps

    def _launch(self) -> None:
        game = self.cfg.get("game", {})
        if self.command:
            argv = self.command
        else:
            if not game.get("dir"):
                raise BackendError("game.dir is not set; nothing to launch")
            gdir = os.path.expanduser(game["dir"])
            wine = game.get("wine", "wine")
            envs = [f"DISPLAY={self.display}"]
            if game.get("wineprefix"):
                envs.append(f"WINEPREFIX={os.path.expanduser(game['wineprefix'])}")
            # hands-off runs Wine's builtin dinput8: no override, no proxy
            argv = ["sh", "-c", 'cd "$1" && shift && exec "$@"', "sh", gdir, "env", "-u", "WINEDLLOVERRIDES",
                    *envs, *([wine] if wine else []), os.path.join(gdir, game.get("exe", "swkotor.exe"))]
        prefix = self.prefix if self.prefix is not None else (
            ["host-spawn"] if not shutil.which(argv[0]) and shutil.which("host-spawn") else [])
        cmd = [*prefix, *argv]
        self.issued.append(cmd)
        if not self.dry_run:
            self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL, start_new_session=True)

    def _find_window(self, timeout_s: float = 60) -> str | None:
        if self.dry_run:
            return "0"
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                out = self.xdo("search", "--onlyvisible", "--name", self.window_name, capture=True)
                ids = out.split()
                if ids:
                    return ids[-1].decode()
            except BackendError:
                pass
            time.sleep(0.5)
        raise BackendError(f"no window named {self.window_name!r} within {timeout_s} s")

    def lock_tier(self, tier: Tier) -> None:
        if Tier.parse(tier) is not Tier.HANDS_OFF:
            raise BackendError("the handsoff backend runs tier hands-off only")

    def clock(self, mode: ClockMode, hz: int, scale: int = 100) -> None:
        if ClockMode(mode) is not ClockMode.WALL:
            raise BackendError("hands-off uses the wall clock only")
        if self._started:       # a mid-run @segment hz= keeps the current tick's time; later ticks use the new rate
            self._t0 += self._tick / self.hz - self._tick / hz
        self.hz = hz

    def start(self) -> StepReport:
        if self._started:
            raise BackendError("already started")
        if self.wid and not self.dry_run:
            self.xdo("windowactivate", "--sync", self.wid)
        self._started = True
        self._t0 = time.monotonic()
        self._tick = 0
        return self._report(VerbClass.NONE, ())

    def close(self) -> None:
        if self._started and not self._exited:
            try:
                self._apply(None)
            except BackendError:
                pass
        self.record_stop()

    # ------------------------------------------------------------------ input

    def _apply(self, note: Any, first: bool = True) -> tuple[list[str], VerbClass]:
        """xdotool command chain moving the held set to ``note``."""
        held = {k for tok in (note.held if note else ()) for k in tok.split("+")}
        buttons = set(note.buttons) if note else set()
        chain: list[str] = []
        for k in sorted(self._held - held):
            chain += ["keyup", keysym(k)]
        for tok in (note.held if note else ()):
            for k in tok.split("+"):
                if k not in self._held and k in held:
                    chain += ["keydown", keysym(k)]
                    self._held.add(k)
        self._held &= held
        if note is not None and note.mouse_abs is not None and (first or note.mouse_abs != self._mouse):
            chain += ["mousemove", "--window", self.wid or "0", str(note.mouse_abs[0]), str(note.mouse_abs[1])]
            self._mouse = tuple(note.mouse_abs)
        for b in sorted(self._buttons - buttons):
            chain += ["mouseup", _BUTTON[b]]
        for b in sorted(buttons - self._buttons):
            chain += ["mousedown", _BUTTON[b]]
        self._buttons = buttons
        vc = VerbClass.OS_INPUT if chain else VerbClass.NONE
        if first and note is not None and note.wheel:
            chain += ["click", "--repeat", str(abs(note.wheel)), "4" if note.wheel > 0 else "5"]
            vc |= VerbClass.OS_INPUT
        if chain:
            self.xdo(*chain)
        if first and note is not None and note.osev:
            self.os_event(OsEvent(note.osev))
            vc |= VerbClass.OS_EVENT
        return chain, vc

    def _sleep_to(self, tick: int) -> None:
        if self.dry_run:
            return
        delay = self._t0 + tick / self.hz - time.monotonic()
        if delay > 0:
            time.sleep(delay)

    def _ticks(self, n: int, note: Any) -> VerbClass:
        """n ticks of wall time. A relative mouse move repeats every tick, as the Note contract says."""
        _, vc = self._apply(note, first=True)
        for _i in range(n):
            if note is not None and note.mouse_rel is not None:
                self.xdo("mousemove_relative", "--", str(note.mouse_rel[0]), str(note.mouse_rel[1]))
                vc |= VerbClass.OS_INPUT
            self._tick += 1
            self._sleep_to(self._tick)
        return vc

    def step(self, n: int, note: Any = None) -> StepReport:
        self._alive()
        return self._report(self._ticks(n, note), ())

    def wait(self, event: str, arg: str | None, timeout_ticks: int, note: Any = None) -> StepReport:
        """No engine events are observable: hold for the timeout, report the wait as met, and hash a frame
        (best effort) into a separate "frame" event."""
        self._alive()
        vc = self._ticks(max(1, timeout_ticks), note)
        png = self.frame()
        evs = (EngineEvent(event, self._tick, arg),
               EngineEvent("frame", self._tick, hashlib.sha256(png or b"").hexdigest()))
        self._events.extend(evs)
        return self._report(vc | VerbClass.WAIT | VerbClass.SCREEN, evs)

    def _alive(self) -> None:
        if not self._started:
            raise BackendError("not started")
        if self._exited:
            raise BackendError("the game has exited")

    def _report(self, vc: VerbClass, events: tuple[EngineEvent, ...]) -> StepReport:
        return StepReport(tick=self._tick, epoch=self._epoch, server_passes=0, subticks=0, verbclass=vc,
                          reason=PointReason.NOT_CAPTURED, events=events,
                          outcome=ProcOutcome.EXITED if self._exited else ProcOutcome.ALIVE)

    def os_event(self, ev: OsEvent) -> ProcOutcome:
        ev = OsEvent(ev)
        if ev is OsEvent.ALTF4:
            self.xdo("key", "alt+F4")
        elif ev is OsEvent.FOCUS:
            self.xdo("windowactivate", self.wid or "0")
        elif ev is OsEvent.BLUR:
            self.xdo("windowminimize", self.wid or "0")
        elif ev is OsEvent.CLOSE:
            self.xdo("windowclose", self.wid or "0")
        elif ev is OsEvent.RELAUNCH:
            if self.proc is not None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait(timeout=5)
            self._epoch += 1
            self._exited = False
            self._held.clear()
            self._buttons.clear()
            self._launch()
            self.wid = self._find_window()
            return ProcOutcome.ALIVE
        if ev in (OsEvent.ALTF4, OsEvent.CLOSE):
            self._exited = self._wait_exit(5.0)
            if self._exited:
                self._events.append(EngineEvent("proc.down", self._tick, str(ev)))
        return ProcOutcome.EXITED if self._exited else ProcOutcome.ALIVE

    def _wait_exit(self, timeout_s: float) -> bool:
        if self.dry_run:
            return True
        if self.proc is None:
            return False
        try:
            self.proc.wait(timeout=timeout_s)
            return True
        except subprocess.TimeoutExpired:
            return False

    # ------------------------------------------------------------------ frames

    def _geometry(self) -> tuple[int, int, int, int]:
        if self.dry_run:
            return 0, 0, 800, 600
        out = self.xdo("getwindowgeometry", "--shell", self.wid or "0", capture=True).decode()
        g = dict(line.split("=", 1) for line in out.split() if "=" in line)
        return int(g["X"]), int(g["Y"]), int(g["WIDTH"]), int(g["HEIGHT"])

    def grab_argv(self, out: str = "-", frames: int = 1) -> list[str]:
        x, y, w, h = self._geometry()
        args = ["-hide_banner", "-loglevel", "error", "-f", "x11grab", "-framerate", str(self.fps),
                "-video_size", f"{w}x{h}", "-i", f"{self.display}+{x},{y}"]
        if frames:
            args += ["-frames:v", str(frames), "-f", "image2pipe", "-vcodec", "png", out]
        else:
            args += ["-c:v", "libx264rgb", "-crf", "0", "-preset", "ultrafast", "-y", out]
        return self.argv("ffmpeg", *args)

    def frame(self) -> bytes | None:
        """One PNG of the game window."""
        cmd = self.grab_argv()
        self.issued.append(cmd)
        if self.dry_run:
            return None
        res = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, timeout=15,
                             env={**os.environ, "DISPLAY": self.display})
        if res.returncode != 0 or not res.stdout.startswith(b"\x89PNG"):
            raise BackendError(f"x11grab failed: {res.stderr.decode(errors='replace').strip()}")
        return res.stdout

    def record_start(self, path: str) -> None:
        """Continuous lossless capture of the window for human review."""
        if self._rec is not None:
            raise BackendError("already recording")
        cmd = self.grab_argv(path, frames=0)
        self.issued.append(cmd)
        if not self.dry_run:
            self._rec = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL)

    def record_stop(self) -> None:
        if self._rec is not None:
            try:
                self._rec.communicate(b"q", timeout=10)
            except subprocess.TimeoutExpired:
                self._rec.kill()
            self._rec = None

    # ------------------------------------------------------------------ nothing in process

    def events(self) -> Iterator[EngineEvent]:
        while self._events:
            yield self._events.popleft()

    def _none(self, what: str) -> BackendError:
        return BackendError(f"hands-off has no in-process access: {what}")

    def capture(self, kind: Literal["dirty", "full"]) -> None:
        raise self._none("capture")

    def records(self) -> Iterator[StagingRecord]:
        return iter(())

    def restore(self, tick: int, root: bytes, pages: Iterable[StagingRecord]) -> Receipt:
        raise self._none("restore")

    def read(self, addr: int, length: int) -> bytes:
        raise self._none("memory read")

    def write(self, addr: int, data: bytes) -> None:
        raise self._none("memory write")

    def semantic(self, op: Literal["read", "apply"], fields: Any) -> Mapping[str, Any]:
        raise self._none("semantic fields")

    def files(self, op: Literal["snap", "put"], blob: bytes | None = None) -> bytes | None:
        raise self._none("files overlay")

    def watch(self, wid: str, addr: int, length: int, cond: str) -> None:
        raise self._none("watch")

    def unwatch(self, wid: str) -> None:
        raise self._none("watch")
