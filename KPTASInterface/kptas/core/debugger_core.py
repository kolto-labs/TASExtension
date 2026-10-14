"""Core-owned debugger session identity and fail-closed capability reporting.

This module has no dependency on TAS, Lua, KPM, or the extension lifecycle.
Runtime layers report evidence for the current session; the debugger exposes an
operation only when transport, backend, target adapter, and policy all agree.
It reports capability state, but does not itself execute game operations.
"""
from __future__ import annotations

from collections.abc import Mapping
import json
from typing import Any
import re


DEBUG_SESSION_SCHEMA = "telos.debug-session/v2"
DEBUG_CAPABILITIES_SCHEMA = "telos.debug-capabilities/v1"
DEBUGGER_LAYERS = ("transport", "backend", "target", "policy")
CAPABILITY_STATES = frozenset({"available", "unsupported", "not-qualified", "unavailable"})

# Keep canonical operation IDs aligned with the static host's DEBUG_OPERATIONS
# in demo/public/debugger/debugger-contract.js. This is reporting vocabulary,
# not a command dispatcher or evidence that either host implements an operation.
CANONICAL_OPERATIONS = (
    "input.apply",
    "execution.pause",
    "execution.resume",
    "execution.step",
    "execution.seek",
    "execution.rewind",
    "processes.list",
    "threads.count",
    "threads.list",
    "modules.list",
    "memory.map",
    "memory.mapping",
    "memory.read",
    "memory.search",
    "memory.write",
    "memory.watch.list",
    "memory.watch.add",
    "memory.watch.remove",
    "registers.read",
    "stack.read",
    "code.disassemble",
    "code.decompile",
    "code.references",
    "code.functions",
    "symbols.list",
    "symbols.resolve",
    "source.resolve",
    "breakpoints.list",
    "breakpoints.add",
    "breakpoints.remove",
    "breakpoints.manage",
    "expressions.evaluate",
    "trace.read",
    "events.read",
    "input.timeline",
    "binary.identity",
    "binary.sections",
    "binary.dependencies",
    "binary.bytes",
    "state.snapshot",
    "state.restore",
)

# Retain the original native reporting IDs as separate entries. An owner must
# explicitly report a canonical ID; a legacy assertion must never grant an
# operation under a different name or pretend that semantics are equivalent.
LEGACY_OPERATIONS = (
    "disassembly.read",
    "breakpoints.software",
    "snapshots.capture",
    "snapshots.restore",
)
STANDARD_OPERATIONS = CANONICAL_OPERATIONS + LEGACY_OPERATIONS

_IDENTIFIER = re.compile(r"^[a-z][a-z0-9.-]{0,79}$")
_OPERATION = re.compile(r"^[a-z][a-z0-9_-]*(?:\.[a-z][a-z0-9_-]*)+$")
_SOURCES = frozenset({"live-runtime", "recorded-journal", "simulator"})


class DebuggerCoreError(RuntimeError):
    """Stable error for invalid, stale, or unavailable debugger evidence."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


class DebuggerCore:
    """Session-scoped capability intersection, independent of optional addons.

    `bind_session` invalidates prior layer evidence on a new session or
    revision. Each layer report must match the active identity. Missing or
    malformed evidence is exposed as unavailable, never inferred as support.
    """

    def __init__(self) -> None:
        self._session: dict[str, Any] | None = None
        self._reports: dict[str, dict[str, dict[str, Any]]] = {}
        self._layer_errors: dict[str, str] = {}

    @property
    def session(self) -> Mapping[str, Any] | None:
        return _copy(self._session) if self._session is not None else None

    def bind_session(self, descriptor: Mapping[str, Any]) -> None:
        """Bind explicit target/session identity and invalidate stale reports."""
        if not isinstance(descriptor, Mapping):
            raise DebuggerCoreError("DEBUG_SESSION_INVALID", "descriptor must be an object")
        session_id = descriptor.get("session")
        revision = descriptor.get("revision")
        source = descriptor.get("source")
        target = descriptor.get("target")
        boundary = descriptor.get("boundary")
        if (descriptor.get("schema") != DEBUG_SESSION_SCHEMA
                or not isinstance(session_id, str) or not session_id or len(session_id) > 160
                or type(revision) is not int or revision < 0
                or not isinstance(source, str) or source not in _SOURCES
                or not isinstance(target, Mapping)
                or not isinstance(boundary, Mapping)
                or not isinstance(boundary.get("kind"), str) or not boundary["kind"]
                or not isinstance(boundary.get("unit"), str) or not boundary["unit"]):
            raise DebuggerCoreError("DEBUG_SESSION_INVALID", "expected session/v2 identity, target, source, revision, and boundary")

        previous = self._session
        if previous and previous["session"] == session_id and revision < previous["revision"]:
            raise DebuggerCoreError("DEBUG_SESSION_STALE", f"revision {revision} precedes {previous['revision']}")
        if previous and previous["session"] == session_id and revision == previous["revision"]:
            candidate = _copy(dict(descriptor))
            if candidate != previous:
                raise DebuggerCoreError("DEBUG_SESSION_REVISION_REQUIRED", "identity changes require a newer revision")
            return

        self._session = _copy(dict(descriptor))
        self._reports.clear()
        self._layer_errors.clear()

    def report_layer(self, layer: str, *, session: str, revision: int,
                     operations: Mapping[str, Any]) -> None:
        """Replace a layer's report for the exact currently bound revision."""
        self._require_identity(session, revision)
        if layer not in DEBUGGER_LAYERS:
            raise DebuggerCoreError("DEBUG_LAYER_INVALID", str(layer))

        normalized: dict[str, dict[str, Any]] = {}
        error = ""
        if not isinstance(operations, Mapping):
            error = "operation report must be an object"
        else:
            for operation, report in operations.items():
                if not isinstance(operation, str) or not _OPERATION.fullmatch(operation):
                    error = "operation ID is malformed"
                    continue
                normalized[operation] = self._normalize_report(report)
        # Replace first, even when the report is malformed. A bad refresh must
        # not leave an older `available` receipt authoritative at this revision.
        self._reports[layer] = normalized
        if error:
            self._layer_errors[layer] = error
        else:
            self._layer_errors.pop(layer, None)

    def close_session(self, session: str) -> None:
        if self._session is None or session != self._session["session"]:
            raise DebuggerCoreError("DEBUG_SESSION_MISMATCH", str(session))
        self._session = None
        self._reports.clear()
        self._layer_errors.clear()

    def capabilities(self) -> dict[str, Any]:
        """Return effective operations plus per-layer evidence and gaps."""
        if self._session is None:
            raise DebuggerCoreError("DEBUG_SESSION_UNAVAILABLE")
        operations = set(STANDARD_OPERATIONS)
        for reports in self._reports.values():
            operations.update(reports)
        effective: dict[str, Any] = {}
        for operation in sorted(operations):
            layers: dict[str, dict[str, Any]] = {}
            for layer in DEBUGGER_LAYERS:
                report = self._reports.get(layer, {}).get(operation)
                if report is None:
                    detail = self._layer_errors.get(layer, "layer has not reported this operation")
                    layers[layer] = {"state": "unavailable", "reason": detail,
                                     "owner": layer, "evidenceId": None}
                else:
                    layers[layer] = dict(report)
            states = [layers[layer]["state"] for layer in DEBUGGER_LAYERS]
            if all(state == "available" for state in states):
                state = "available"
            elif "unsupported" in states:
                state = "unsupported"
            elif "not-qualified" in states:
                state = "not-qualified"
            else:
                state = "unavailable"
            effective[operation] = {"state": state, "layers": layers}

        return {
            "schema": DEBUG_CAPABILITIES_SCHEMA,
            "session": self._session["session"],
            "revision": self._session["revision"],
            "source": self._session["source"],
            "target": _copy(self._session["target"]),
            "boundary": _copy(self._session["boundary"]),
            "operations": effective,
        }

    def _require_identity(self, session: str, revision: int) -> None:
        if self._session is None:
            raise DebuggerCoreError("DEBUG_SESSION_UNAVAILABLE")
        if session != self._session["session"]:
            raise DebuggerCoreError("DEBUG_SESSION_MISMATCH", str(session))
        if type(revision) is not int or revision != self._session["revision"]:
            raise DebuggerCoreError("DEBUG_SESSION_REVISION_MISMATCH", str(revision))

    @staticmethod
    def _normalize_report(report: Any) -> dict[str, Any]:
        if not isinstance(report, Mapping):
            return {"state": "unavailable", "reason": "malformed layer evidence",
                    "owner": "unknown", "evidenceId": None}
        state = report.get("state")
        reason = report.get("reason")
        owner = report.get("owner")
        evidence_id = report.get("evidenceId")
        valid = (isinstance(state, str) and state in CAPABILITY_STATES
                 and isinstance(reason, str) and len(reason) <= 500
                 and isinstance(owner, str) and _IDENTIFIER.fullmatch(owner)
                 and (evidence_id is None or (isinstance(evidence_id, str) and len(evidence_id) <= 200))
                 and (state != "available" or (isinstance(evidence_id, str) and bool(evidence_id))))
        if not valid:
            return {"state": "unavailable", "reason": "malformed layer evidence",
                    "owner": "unknown", "evidenceId": None}
        return {"state": state, "reason": reason, "owner": owner,
                "evidenceId": evidence_id}


def _copy(value: Any) -> Any:
    """Copy JSON-compatible session metadata without exposing mutable state."""
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError, RecursionError) as exc:
        raise DebuggerCoreError("DEBUG_SESSION_INVALID", "metadata must be finite JSON values") from exc
