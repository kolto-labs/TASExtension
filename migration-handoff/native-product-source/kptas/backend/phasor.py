"""Hyperstim's harness channel: loopback TCP, NDJSON, one client at a time.

Requests are {"id", "cmd", ...args}; replies are {"id", "ok", "result"} or
{"id", "ok": false, "error": "<text>"}; event frames are {"event", "tick", "seq", ...}.
Read-mostly: verbs that change game state need ``allow_writes=True``.

PhasorBackend is the optional ``phasor`` backend on top of the client. It attaches
to a game already running with Hyperstim loaded. It observes by default; explicit
lab semantic mode queues engine actions through NWScript. No keyboard input,
no tick gate, no virtual clock, no capture or restore. Its ceiling is lab.
"""

from __future__ import annotations

import dataclasses
import json
import math
import os
import socket
import time
from collections import deque
from collections.abc import Iterable, Iterator, Mapping
from typing import Any, Literal

from kptas.types import (WIRE_MAX_LINE, BackendError, Caps, ClockMode, EngineEvent, EnvPin, OsEvent,
                         PointReason, ProcOutcome, Receipt, SeekMode, StagingRecord, StepReport, Tier,
                         VerbClass)

READ_VERBS = frozenset({"ping", "status", "state", "events", "subscribe", "unsubscribe", "dialog",
                        "globals", "journal", "screenshot"})
WRITE_VERBS = frozenset({
    "lua", "reload", "load_save", "new_game", "chargen_select_class", "chargen_begin_play", "save",
    "warp", "reply", "dialog_continue", "dialog_skip", "dialog_abort", "dialog_clear_sound_wait",
    "clear_auto_pause", "speed", "fixed_step", "unpause_timers", "advance_tutorial",
    "finish_hidden_tutorial", "dismiss_status_summary", "dismiss_messagebox", "equipment_screen",
    "door_use", "door_commit_open", "door_security", "attack", "dismiss_container",
    "take_all_container", "accept_party_selection", "auto_levelup", "set_party_leader", "use_item",
    "equip_item", "movies", "quit"})
CONSOLE_READ_OPS = frozenset({"status", "outcome", "output", "complete"})
# these reply after the frame, not at once
DEFERRED = frozenset({"screenshot", "auto_levelup", "set_party_leader", "use_item", "equip_item",
                      "door_security"})


class PhasorError(BackendError):
    """Hyperstim answered ok=false, or the channel failed."""


class PhasorClient:
    """One connection to Hyperstim. Port: argument, else KOTORPHASOR_HARNESS_PORT."""

    def __init__(self, port: int | None = None, host: str = "127.0.0.1", timeout_s: float = 10.0,
                 allow_writes: bool = False):
        if port is None:
            port = int(os.environ.get("KOTORPHASOR_HARNESS_PORT", "0"))
        if not port:
            raise PhasorError("no Hyperstim port: pass one or set KOTORPHASOR_HARNESS_PORT")
        if not host.startswith("127."):
            raise PhasorError("Hyperstim listens on loopback only")
        self.host, self.port = host, port
        self.timeout_s = timeout_s
        self.allow_writes = allow_writes
        self.sock: socket.socket | None = None
        self._buf = bytearray()
        self._next = 1
        self._early: dict[Any, dict[str, Any]] = {}
        self.event_frames: deque[dict[str, Any]] = deque(maxlen=65536)
        self.gaps = 0

    def connect(self) -> PhasorClient:
        self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout_s)
        return self

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            finally:
                self.sock = None

    def __enter__(self) -> PhasorClient:
        return self.connect() if self.sock is None else self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ transport

    def _recv(self, timeout_s: float | None) -> dict[str, Any] | None:
        assert self.sock is not None
        deadline = None if timeout_s is None else time.monotonic() + timeout_s
        while True:
            nl = self._buf.find(b"\n")
            if nl >= 0:
                line = bytes(self._buf[:nl]).rstrip(b"\r")
                del self._buf[:nl + 1]
                if line:
                    try:
                        msg = json.loads(line)
                    except ValueError as e:
                        raise PhasorError(f"bad Hyperstim line: {e}") from None
                    if not isinstance(msg, dict):
                        raise PhasorError("Hyperstim message is not an object")
                    return msg
                continue
            if len(self._buf) > WIRE_MAX_LINE:
                raise PhasorError("reply line exceeds 1 MiB")
            # once the deadline has passed, still take what is already queued: timeout 0 is a
            # non-blocking recv, so poll_events(0.0) reads frames instead of returning at once
            left = None if deadline is None else max(0.0, deadline - time.monotonic())
            self.sock.settimeout(left)
            try:
                chunk = self.sock.recv(65536)
            except (socket.timeout, BlockingIOError):
                return None
            finally:
                self.sock.settimeout(self.timeout_s)
            if not chunk:
                raise PhasorError("Hyperstim closed the connection")
            self._buf += chunk

    def _route(self, msg: dict[str, Any]) -> None:
        if "event" in msg and "id" not in msg:
            if msg["event"] == "harness.gap":
                self.gaps += int(msg.get("dropped", 0))
            self.event_frames.append(msg)
        else:
            self._early[msg.get("id")] = msg

    def call(self, cmd: str, timeout_s: float | None = None, **args: Any) -> Any:
        """Send one command and return its result. Write verbs need allow_writes."""
        if cmd in WRITE_VERBS and not self.allow_writes:
            raise PhasorError(f"{cmd} changes game state; construct with allow_writes=True")
        if cmd == "devtools" and args.get("op") not in CONSOLE_READ_OPS and not self.allow_writes:
            raise PhasorError(f"devtools {args.get('op')!r} changes game state; construct with allow_writes=True")
        if cmd not in READ_VERBS | WRITE_VERBS | {"devtools"}:
            raise PhasorError(f"unknown Hyperstim verb {cmd!r}")
        if self.sock is None:
            self.connect()
        assert self.sock is not None
        rid = self._next
        self._next += 1
        line = json.dumps({"id": rid, "cmd": cmd, **args}, separators=(",", ":")).encode() + b"\n"
        self.sock.sendall(line)
        limit = timeout_s if timeout_s is not None else (self.timeout_s * 6 if cmd in DEFERRED else self.timeout_s)
        deadline = time.monotonic() + limit
        while True:
            msg = self._early.pop(rid, None)
            if msg is None:
                msg = self._recv(max(0.0, deadline - time.monotonic()))
                if msg is None:
                    raise PhasorError(f"{cmd}: no reply within {limit} s")
                if msg.get("id") != rid:
                    self._route(msg)
                    continue
            if msg.get("ok"):
                return msg.get("result")
            raise PhasorError(f"{cmd}: {msg.get('error')}")

    def poll_events(self, timeout_s: float = 0.0) -> Iterator[dict[str, Any]]:
        """Event frames received so far (after ``subscribe``). Waits up to timeout_s for the first new
        frame, then takes whatever else is already queued."""
        if self.sock is not None:
            while True:
                msg = self._recv(timeout_s)
                if msg is None:
                    break
                self._route(msg)
                timeout_s = 0.0
        while self.event_frames:
            yield self.event_frames.popleft()

    # ------------------------------------------------------------------ read verbs

    def ping(self) -> Any:
        return self.call("ping")

    def status(self) -> Mapping[str, Any]:
        """client_tick, server_tick, hooks, poisoned, missing_sites, events_seq."""
        return self.call("status")

    def state(self) -> Mapping[str, Any]:
        return self.call("state")

    def events(self, **args: Any) -> Any:
        return self.call("events", **args)

    def subscribe(self, **args: Any) -> Any:
        return self.call("subscribe", **args)

    def unsubscribe(self, **args: Any) -> Any:
        return self.call("unsubscribe", **args)

    def dialog(self) -> Any:
        return self.call("dialog")

    def globals(self, **args: Any) -> Any:
        return self.call("globals", **args)

    def journal(self, **args: Any) -> Any:
        return self.call("journal", **args)

    def screenshot(self, **args: Any) -> Any:
        return self.call("screenshot", **args)

    def console(self, op: str, runtime: str, **args: Any) -> Any:
        """kp.console.v1 sub-protocol. submit and cancel are writes."""
        if op not in CONSOLE_READ_OPS and not self.allow_writes:
            raise PhasorError(f"console {op} changes game state; construct with allow_writes=True")
        return self.call("devtools", protocol="kp.console.v1", op=op, runtime=runtime, **args)

    def wait_event(self, name: str, timeout_s: float) -> dict[str, Any] | None:
        """First event frame named ``name`` within timeout_s; earlier frames stay queued."""
        deadline = time.monotonic() + timeout_s
        held: list[dict[str, Any]] = []
        try:
            while True:
                for ev in self.poll_events(min(0.05, max(0.0, deadline - time.monotonic()))):
                    if ev.get("event") == name:
                        return ev
                    held.append(ev)
                if time.monotonic() >= deadline:
                    return None
        finally:
            self.event_frames.extendleft(reversed(held))


class PhasorBackend:
    """Backend ``phasor``: observe or explicitly request engine actions (tier lab only).

    This adapter has no keyboard-input, pause or frame-step verb and no clock hooks, so a
    tick here is 1/hz of wall time, as in hands-off. Typed engine commands are opt-in.
    A score note must hold nothing: step()
    waits and wait() watches the event stream. The DLL sits in the game process outside
    the tas module allowlist (C9), so nothing recorded through it counts above lab.
    """

    name = "phasor"

    def __init__(self, port: int | None = None, client: PhasorClient | None = None):
        self.port = port
        self.client = client
        self.hz = 60
        self.status0: Mapping[str, Any] = {}
        self._t0 = 0.0
        self._tick = 0
        self._started = False
        self._attached = False
        self._closed = False
        self._exited = False
        self._events: deque[EngineEvent] = deque()
        self._semantic_enabled = False
        self._lab_locked = False
        self._semantic_failed = False
        self._commands: list[dict[str, Any]] = []
        self._command_index = 0
        self._semantic_vc = VerbClass(0)
        self.command_results: list[dict[str, Any]] = []
        self._caps = Caps(backend="phasor", tier_ceiling=Tier.LAB, clock_modes=frozenset({ClockMode.WALL}),
                          seek_modes=frozenset({SeekMode.REPLAY}), input_paths=frozenset(),
                          thread_models=frozenset({"free"}), tick_source="none")

    @property
    def caps(self) -> Caps:
        return self._caps

    def attach(self, cfg: Mapping[str, Any], env: EnvPin | None = None) -> Caps:
        from kptas.engine_control import validate_schedule
        if self._closed:
            raise BackendError("backend is closed; create a fresh backend/session")
        self._attached = False
        if self.client is not None:
            self.client.allow_writes = False
        self._semantic_enabled = cfg.get("input", {}).get("path") == "semantic"
        if self._semantic_enabled and Tier.parse(cfg.get("run", {}).get("tier", "tas")) is not Tier.LAB:
            raise BackendError("semantic engine commands require explicit lab tier")
        self._commands = validate_schedule(cfg.get("engine", {}).get("commands", []))
        if self._commands and not self._semantic_enabled:
            raise BackendError("engine.commands requires input.path=semantic")
        port = self.port or cfg.get("phasor", {}).get("port") or None
        self.hz = int(cfg.get("clock", {}).get("hz", 60))
        if self.client is None:
            self.client = PhasorClient(port=port, allow_writes=False)
        self.client.allow_writes = False
        if self.client.sock is None:
            try:
                self.client.connect()
            except OSError as e:
                raise PhasorError(f"no Hyperstim on {self.client.host}:{self.client.port}: {e}") from None
        # Status admission must not leave a rejected attachment write-enabled.
        self.status0 = dict(self.client.status())
        if self._semantic_enabled:
            wanted = cfg.get("run", {}).get("game", "k1")
            reported = self.status0.get("game")
            if not isinstance(reported, str) or reported.lower() not in {"k1", "k2"}:
                raise PhasorError("semantic commands require a harness status with game K1 or K2; "
                                  "the attached game identity is unavailable")
            if reported.lower() != wanted:
                raise PhasorError(f"semantic game mismatch: requested {wanted}, attached harness reports {reported}")
        hooks = self.status0.get("hooks", ())
        # Existing harness releases report a hook count, not hook names.
        # Keep the count in status0 without fabricating capability identities.
        if type(hooks) is int and hooks >= 0:
            hook_names = ()
        elif isinstance(hooks, (list, tuple)) and all(isinstance(h, str) for h in hooks):
            hook_names = tuple(hooks)
        else:
            raise PhasorError("Hyperstim status hooks must be a count or a list of names")
        self._caps = dataclasses.replace(self._caps, hooks=hook_names,
                                         input_paths=frozenset({"semantic"}) if self._semantic_enabled else frozenset())
        self._attached = True
        self.client.allow_writes = self._semantic_enabled and self._lab_locked and not self._semantic_failed
        return self._caps

    def lock_tier(self, tier: Tier) -> None:
        if Tier.parse(tier) is not Tier.LAB:
            raise BackendError("the phasor backend runs tier lab only: Hyperstim cannot gate ticks "
                               "and is outside the tas module allowlist")

        self._lab_locked = True
        if self.client is not None:
            self.client.allow_writes = self._attached and self._semantic_enabled and not self._semantic_failed and not self._closed

    def clock(self, mode: ClockMode, hz: int, scale: int = 100) -> None:
        if type(hz) is not int or hz <= 0:
            raise BackendError("wall clock hz must be a positive integer")
        if ClockMode(mode) is not ClockMode.WALL or scale != 100:
            raise BackendError("Hyperstim does not virtualize time; the phasor backend runs on the wall clock")
        if self._started:       # a mid-run @segment hz= keeps the current tick's time; later ticks use the new rate
            self._t0 += self._tick / self.hz - self._tick / hz
        self.hz = hz

    def start(self) -> StepReport:
        self._connection_ready()
        if self._semantic_enabled and not self._lab_locked:
            raise BackendError("semantic start requires locked lab tier")
        if self._started:
            raise BackendError("already started")
        assert self.client is not None, "attach first"
        self.client.subscribe()
        self._started = True
        self._t0 = time.monotonic()
        self._tick = 0
        return self._report(VerbClass.READ, self._dispatch_commands())

    def close(self) -> None:
        self._closed = True
        self._attached = False
        self._started = False
        if self.client is not None:
            self.client.allow_writes = False
            self.client.close()

    # ------------------------------------------------------------------ ticks

    def _no_input(self, note: Any) -> None:
        if note is not None and (note.held or note.buttons or note.mouse_abs is not None
                                 or note.mouse_rel is not None or note.wheel or note.osev):
            raise BackendError("The phasor backend does not inject keyboard or mouse input; every score note must "
                               "hold nothing (\".\")")

    def _poll(self) -> list[EngineEvent]:
        """Event frames that arrived so far, at the current local tick."""
        assert self.client is not None
        try:
            frames = list(self.client.poll_events(0.0))
        except (PhasorError, OSError) as e:     # no automatic reconnection after an observation gap
            self._exited = True
            self.client.allow_writes = False
            new = [EngineEvent("proc.down", self._tick, str(e))]
        else:
            new = [EngineEvent(f["event"], self._tick,
                               str(f.get("dropped", 0)) if f["event"] == "harness.gap" else None)
                   for f in frames if isinstance(f.get("event"), str)]
        self._events.extend(new)
        return new

    def _run(self, n: int, until: str | None = None) -> tuple[EngineEvent, ...]:
        seen: list[EngineEvent] = []
        for _i in range(n):
            self._tick += 1
            delay = self._t0 + self._tick / self.hz - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            seen += self._poll()
            if not self._exited:
                seen += self._dispatch_commands()
            if self._exited or (until is not None and any(e.name == until for e in seen)):
                break
        return tuple(seen)

    def step(self, n: int, note: Any = None) -> StepReport:
        self._alive()
        self._no_input(note)
        return self._report(VerbClass.READ, self._run(n))

    def wait(self, event: str, arg: str | None, timeout_ticks: int, note: Any = None) -> StepReport:
        """Watch the event stream until ``event`` or the timeout. Frames carry no agreed arg field,
        so a wait on an arg is refused rather than guessed."""
        self._alive()
        self._no_input(note)
        if arg is not None:
            raise BackendError(f"@sync {event} {arg}: Hyperstim event frames have no arg to match; "
                               "sync on the name only")
        return self._report(VerbClass.READ | VerbClass.WAIT, self._run(max(1, timeout_ticks), until=event))

    def _connection_ready(self) -> None:
        if self._closed:
            raise BackendError("backend is closed; create a fresh backend/session")
        if not self._attached:
            raise BackendError("attach successfully before using this backend")
        if self._exited:
            raise BackendError("Hyperstim connection ended; create a fresh backend/session")
        if self._semantic_failed:
            raise BackendError("engine request outcome is indeterminate; create a fresh backend/session")

    def _alive(self) -> None:
        self._connection_ready()
        if not self._started:
            raise BackendError("not started")
        if self._exited:
            raise BackendError("Hyperstim closed the connection")

    def _report(self, vc: VerbClass, events: tuple[EngineEvent, ...]) -> StepReport:
        vc |= self._semantic_vc
        self._semantic_vc = VerbClass(0)
        return StepReport(tick=self._tick, epoch=0, server_passes=0, subticks=0, verbclass=vc,
                          reason=PointReason.NOT_CAPTURED, events=events,
                          outcome=ProcOutcome.EXITED if self._exited else ProcOutcome.ALIVE)

    def events(self) -> Iterator[EngineEvent]:
        while self._events:
            yield self._events.popleft()

    def records(self) -> Iterator[StagingRecord]:
        return iter(())

    # ------------------------------------------------------------------ what it cannot do

    def _cannot(self, what: str) -> BackendError:
        return BackendError(f"the phasor backend is read-mostly and cannot {what}")

    def os_event(self, ev: OsEvent) -> ProcOutcome:
        raise self._cannot(f"send os event {OsEvent(ev).value}: Hyperstim has no input path")

    def capture(self, kind: Literal["dirty", "full"]) -> None:
        raise self._cannot("capture pages")

    def restore(self, tick: int, root: bytes, pages: Iterable[StagingRecord]) -> Receipt:
        raise self._cannot("restore a tick")

    def read(self, addr: int, length: int) -> bytes:
        raise self._cannot("read raw memory; use PhasorClient.state() or globals()")

    def write(self, addr: int, data: bytes) -> None:
        raise self._cannot("write memory")

    def semantic(self, op: Literal["read", "apply"], fields: Any) -> Mapping[str, Any]:
        if op != "read":
            raise self._cannot("apply arbitrary fields; use typed engine_action requests in semantic mode")
        self._connection_ready()
        if not isinstance(fields, (list, tuple)) or any(not isinstance(name, str) for name in fields):
            raise BackendError("semantic read fields must be a list of field names")
        names = list(fields)
        assert self.client is not None
        try:
            reply = self.client.state()
            if not isinstance(reply, Mapping):
                raise BackendError("semantic read returned no structured state")
            state = dict(reply)
        except (BackendError, OSError, ValueError, TypeError):
            self._semantic_failed = True
            self.client.allow_writes = False
            raise
        if not names:
            return state
        missing = set(names) - state.keys()
        if missing:
            raise BackendError(f"unknown live state fields: {sorted(missing)}")
        return {name: state[name] for name in names}

    def engine_action(self, command: Mapping[str, Any]) -> Mapping[str, Any]:
        """Queue a typed engine request. Acceptance is never an arrival/combat claim."""
        from kptas.engine_control import lua_request, validate_command
        self._alive()
        if not self._semantic_enabled or not self._lab_locked:
            raise BackendError("engine_action requires semantic input and locked lab tier")
        if self._semantic_failed:
            raise BackendError("engine request outcome is indeterminate; create a fresh backend/session before further requests")
        assert self.client is not None
        request = validate_command(command)
        try:
            reply = self.client.call("lua", code=lua_request(request))
            values = reply.get("values") if isinstance(reply, Mapping) else None
            if not isinstance(values, list) or len(values) != 1 or not isinstance(values[0], Mapping):
                raise BackendError("engine action returned no structured engine observation")
            observation = dict(values[0])
            position = observation.get("position")
            if (observation.get("request_accepted") is not True or observation.get("completed") is not False
                    or type(observation.get("actor")) is not int
                    or type(observation.get("current_action")) is not int
                    or not isinstance(position, Mapping)
                    or any(type(position.get(k)) not in (int, float) or abs(position[k]) > 1e6
                           or not math.isfinite(position[k]) for k in ("x", "y", "z"))):
                raise BackendError("engine action returned an incomplete live observation")
            if "target" in request:
                distance = observation.get("distance")
                if (type(observation.get("target")) is not int or type(distance) not in (int, float)
                        or not 0 <= distance <= 2e6 or not math.isfinite(distance)):
                    raise BackendError("engine action returned no valid target distance")
        except BaseException:
            # The engine may have queued an action before a transport/observation
            # failure. Never retry or issue another request on this session.
            self._semantic_failed = True
            self.client.allow_writes = False
            raise
        result = {"tick": self._tick, "request": request, "observation": observation,
                  "provider": "phasor", "timing": "wall", "tier": "lab",
                  "game": str(self.status0["game"]).lower(),
                  "provider_build": self.status0.get("build", "")}
        self.command_results.append(result)
        self._semantic_vc |= VerbClass.READ if request["action"] == "observe" else VerbClass.SEMANTIC
        return result

    def _dispatch_commands(self) -> tuple[EngineEvent, ...]:
        if self._semantic_failed:
            raise BackendError("engine dispatch halted after an indeterminate request; create a fresh backend/session")
        events = []
        while self._command_index < len(self._commands):
            command = self._commands[self._command_index]
            if command["at"] > self._tick:
                break
            result = self.engine_action(command)
            self._command_index += 1
            event = EngineEvent("engine.request", self._tick, json.dumps(result, separators=(",", ":")))
            events.append(event)
            self._events.append(event)
        return tuple(events)

    def files(self, op: Literal["snap", "put"], blob: bytes | None = None) -> bytes | None:
        raise self._cannot("snapshot the files overlay")

    def watch(self, wid: str, addr: int, length: int, cond: str) -> None:
        raise self._cannot("watch memory")

    def unwatch(self, wid: str) -> None:
        raise self._cannot("watch memory")

    def frame(self) -> bytes | None:
        raise self._cannot("return a frame yet: the screenshot reply is not wired in")
