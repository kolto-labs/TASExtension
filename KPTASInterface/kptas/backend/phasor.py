"""Client for KotorPhasor's harness channel: loopback TCP, NDJSON, one client at a time.

Requests are {"id", "cmd", ...args}; replies are {"id", "ok", "result"} or
{"id", "ok": false, "error": "<text>"}; event frames are {"event", "tick", "seq", ...}.
Read-mostly: verbs that change game state need ``allow_writes=True``.
"""

from __future__ import annotations

import json
import os
import socket
import time
from collections import deque
from collections.abc import Iterator, Mapping
from typing import Any

from kptas.types import WIRE_MAX_LINE, BackendError

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
    """KotorPhasor answered ok=false, or the channel failed."""


class PhasorClient:
    """One connection to KotorPhasor. Port: argument, else KOTORPHASOR_HARNESS_PORT."""

    def __init__(self, port: int | None = None, host: str = "127.0.0.1", timeout_s: float = 10.0,
                 allow_writes: bool = False):
        if port is None:
            port = int(os.environ.get("KOTORPHASOR_HARNESS_PORT", "0"))
        if not port:
            raise PhasorError("no KotorPhasor port: pass one or set KOTORPHASOR_HARNESS_PORT")
        if not host.startswith("127."):
            raise PhasorError("KotorPhasor listens on loopback only")
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
                        raise PhasorError(f"bad KotorPhasor line: {e}") from None
                    if not isinstance(msg, dict):
                        raise PhasorError("KotorPhasor message is not an object")
                    return msg
                continue
            if len(self._buf) > WIRE_MAX_LINE:
                raise PhasorError("reply line exceeds 1 MiB")
            left = None if deadline is None else deadline - time.monotonic()
            if left is not None and left <= 0:
                return None
            self.sock.settimeout(left)
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout:
                return None
            if not chunk:
                raise PhasorError("KotorPhasor closed the connection")
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
            raise PhasorError(f"unknown KotorPhasor verb {cmd!r}")
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
        """Event frames received so far (after ``subscribe``), plus any that arrive within timeout_s."""
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
