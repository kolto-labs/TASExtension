"""Session-bound debugger capabilities for Telos Core provider admission.

The runtime host supplies one report per transport, backend, target and policy
layer. This module intersects those reports and builds the existing Core
``ProviderSpec``; ``Registry.negotiate`` still performs extension/API/grant
admission and owns activation disposal. No package code is loaded and no
native hooks are injected; status and memory operations are delegated to a
trusted host backend.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from threading import RLock
from types import MappingProxyType
from typing import Protocol

from .contracts import (
    ApiVersion, Capability, ExtensionId, Outcome, OutcomeState, Permission,
    ProviderId, RefusalCode,
)
from .registry import Activation, ProviderSpec, Registry


class DebuggerPolicyError(ValueError):
    """Debugger evidence or provider identity violates host policy."""


class Layer(StrEnum):
    TRANSPORT = "transport"
    BACKEND = "backend"
    TARGET = "target"
    POLICY = "policy"


class Availability(StrEnum):
    AVAILABLE = "available"
    UNSUPPORTED = "unsupported"
    NOT_QUALIFIED = "not-qualified"
    UNAVAILABLE = "unavailable"


_REQUIRED_LAYERS = frozenset(Layer)
MAX_STATUS_BYTES = 64 * 1024
MAX_MEMORY_READ_BYTES = 64 * 1024

# These names are the stable bridge between debugger operation reports and
# Telos Core's generic capability/permission contract. They do not imply that
# any backend currently implements the operation.
_OPERATION_CAPABILITIES: Mapping[str, Capability] = MappingProxyType({
    "session.status": Capability("debugger.session.status", Permission.OBSERVE),
    "memory.read": Capability("debugger.memory.read", Permission.OBSERVE),
})


class DebuggerBackend(Protocol):
    """Host-owned, session-scoped backend surface required by the Core bridge."""

    @property
    def identity(self) -> tuple[str, str, int]:
        """Current (session ID, target ID, monotonically increasing revision)."""
        ...

    def read_status(self, expected_identity: tuple[str, str, int]) -> bytes:
        """Refuse stale identity and return bounded status for the exact revision."""
        ...

    def read_memory(self, process_id: int, address: int, size: int, *,
                    expected_identity: tuple[str, str, int]) -> bytes:
        """Bind exact identity and revalidate the safe pause/barrier per capture."""
        ...


class DebuggerStaleSession(RuntimeError):
    """The target/session changed while a debugger request was being served."""


class DebuggerPermissionError(PermissionError):
    """The negotiated extension activation lacks the requested debugger capability."""


@dataclass(frozen=True)
class StatusObservation:
    session_id: str
    target_id: str
    revision: int
    payload: bytes


@dataclass(frozen=True)
class MemoryObservation:
    session_id: str
    target_id: str
    revision: int
    process_id: int
    address: int
    data: bytes


class DebuggerService:
    """Small executable read/status facade owned by one Core activation."""

    def __init__(self, backend: DebuggerBackend, evidence: DebuggerEvidence,
                 activation: Activation, on_close) -> None:
        self._backend = backend
        self._evidence = evidence
        self._activation = activation
        self._on_close = on_close
        self._allowed: frozenset[Capability] = frozenset()
        self._closed = False

    def _bind_permissions(self, allowed: frozenset[Capability]) -> None:
        self._allowed = allowed

    @property
    def closed(self) -> bool:
        return self._closed or self._activation.closed

    def status(self) -> StatusObservation:
        self._require("session.status")
        self._require_current()
        result = self._backend.read_status(self._expected_identity)
        self._require("session.status")
        self._require_current()
        if type(result) is not bytes or len(result) > MAX_STATUS_BYTES:
            raise ValueError("debugger status must be bounded immutable bytes")
        return StatusObservation(
            self._evidence.session_id, self._evidence.target_id,
            self._evidence.revision, result,
        )

    def read_memory(self, process_id: int, address: int, size: int) -> MemoryObservation:
        self._require("memory.read")
        self._require_current()
        if type(process_id) is not int or process_id <= 0:
            raise ValueError("process ID must be a positive integer")
        if type(address) is not int or address < 0:
            raise ValueError("memory address must be a nonnegative integer")
        if type(size) is not int or not 1 <= size <= MAX_MEMORY_READ_BYTES:
            raise ValueError(f"memory read size must be in [1, {MAX_MEMORY_READ_BYTES}]")
        pointer_bits = self._evidence.pointer_bits
        if pointer_bits is None:
            raise DebuggerPermissionError("memory reads require a known target pointer width")
        if address >= 1 << pointer_bits or address + size > 1 << pointer_bits:
            raise ValueError(f"memory address range exceeds the {pointer_bits}-bit target address space")
        result = self._backend.read_memory(
            process_id, address, size, expected_identity=self._expected_identity,
        )
        self._require("memory.read")
        self._require_current()
        if type(result) is not bytes or len(result) != size:
            raise ValueError("backend memory read did not return the exact requested byte count")
        return MemoryObservation(
            self._evidence.session_id, self._evidence.target_id,
            self._evidence.revision, process_id, address, result,
        )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._on_close(self._activation)
        if not self._activation.closed:
            self._activation.close()

    def _require(self, operation: str) -> None:
        if self.closed:
            raise DebuggerStaleSession("debugger activation is closed")
        capability = _OPERATION_CAPABILITIES[operation]
        if capability not in self._allowed:
            raise DebuggerPermissionError(f"activation lacks {capability.name}")

    def _require_current(self) -> None:
        identity = self._backend.identity
        if identity != self._expected_identity:
            raise DebuggerStaleSession("backend session, target or revision changed")

    @property
    def _expected_identity(self) -> tuple[str, str, int]:
        return self._evidence.session_id, self._evidence.target_id, self._evidence.revision


class DebuggerProvider:
    """Session-bound provider using Core's existing registry and grant admission."""

    def __init__(self, *, identity: ProviderId, api: ApiVersion,
                 evidence: DebuggerEvidence, backend: DebuggerBackend) -> None:
        if type(identity) is not ProviderId or type(api) is not ApiVersion:
            raise TypeError("provider identity and API version have required contract types")
        if type(evidence) is not DebuggerEvidence:
            raise TypeError("evidence must be DebuggerEvidence")
        _reject_kpm_identity(identity.value)
        capabilities = evidence.capabilities
        if (Capability("debugger.session.status", Permission.OBSERVE) in capabilities
                and not callable(getattr(backend, "read_status", None))):
            raise TypeError("backend evidence advertises status but backend has no status reader")
        if (Capability("debugger.memory.read", Permission.OBSERVE) in capabilities
                and not callable(getattr(backend, "read_memory", None))):
            raise TypeError("backend evidence advertises memory reads but backend has no memory reader")
        if getattr(backend, "identity", None) != (
            evidence.session_id, evidence.target_id, evidence.revision,
        ):
            raise DebuggerStaleSession("runtime evidence does not match the backend's current session")
        self.identity = identity
        self.api = api
        self.evidence = evidence
        self.backend = backend
        self._services: dict[Activation, DebuggerService] = {}
        self._lock = RLock()

    def register(self, registry: Registry) -> ProviderSpec:
        """Register only this host-owned provider factory with Core."""
        if type(registry) is not Registry:
            raise TypeError("registry must be a Core Registry")
        spec = self.evidence.provider_spec(self.identity, self.api)
        registry.register(spec, self._activate)
        return spec

    def negotiate(self, registry: Registry, *, request_id: str, api: ApiVersion,
                  extension: ExtensionId, required: frozenset[Capability],
                  granted: frozenset[Capability]) -> Outcome[DebuggerService]:
        """Admit an extension through Core and return its scoped read/status service."""
        activation_result = negotiate_debugger(
            registry,
            request_id=request_id,
            extension=extension,
            provider=self.identity,
            api=api,
            required=required,
            granted=granted,
        )
        if activation_result.state is not OutcomeState.COMPLETED:
            return Outcome(request_id, activation_result.state, refusal=activation_result.refusal)
        activation = activation_result.value
        with self._lock:
            service = self._services.get(activation)
            if service is None or service.closed:
                activation.close()
                return Outcome(request_id, OutcomeState.REFUSED, refusal=RefusalCode.PROVIDER_FAILED)
            service._bind_permissions(required & granted)
        return Outcome(request_id, OutcomeState.COMPLETED, value=service)

    def _activate(self, activation: Activation) -> None:
        service = DebuggerService(self.backend, self.evidence, activation, self._dispose)
        with self._lock:
            self._services[activation] = service
        try:
            activation.own(service)
        except Exception:
            self._dispose(activation)
            raise

    def _dispose(self, activation: Activation) -> None:
        with self._lock:
            self._services.pop(activation, None)


@dataclass(frozen=True)
class LayerReport:
    """One layer's evidence for one operation in one exact session revision."""

    layer: Layer
    state: Availability
    session_id: str
    target_id: str
    revision: int
    evidence_id: str | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if type(self.layer) is not Layer or type(self.state) is not Availability:
            raise TypeError("layer and state must use debugger contract enums")
        _identity(self.session_id, "session_id")
        _identity(self.target_id, "target_id")
        if type(self.revision) is not int or self.revision < 0:
            raise ValueError("revision must be a nonnegative integer")
        if self.state is Availability.AVAILABLE:
            _identity(self.evidence_id, "evidence_id")
        elif self.evidence_id is not None and type(self.evidence_id) is not str:
            raise TypeError("evidence_id must be a string or None")
        if self.reason is not None and (
            type(self.reason) is not str or not self.reason or len(self.reason) > 1024
        ):
            raise ValueError("reason must be a nonempty string or None")


@dataclass(frozen=True)
class DebuggerEvidence:
    """Immutable, live-session capability evidence supplied by the runtime host."""

    session_id: str
    target_id: str
    backend_id: str
    revision: int
    source: str
    operations: Mapping[str, tuple[LayerReport, ...]]
    pointer_bits: int | None = None

    def __post_init__(self) -> None:
        _identity(self.session_id, "session_id")
        _identity(self.target_id, "target_id")
        _identity(self.backend_id, "backend_id")
        _reject_kpm_identity(self.target_id)
        _reject_kpm_identity(self.backend_id)
        if type(self.revision) is not int or self.revision < 0:
            raise ValueError("revision must be a nonnegative integer")
        if self.source != "live-runtime":
            raise DebuggerPolicyError("debugger providers require live-runtime evidence")
        if self.pointer_bits is not None and (
            type(self.pointer_bits) is not int or self.pointer_bits not in (32, 64)
        ):
            raise ValueError("pointer_bits must be 32, 64 or None")
        if not isinstance(self.operations, Mapping):
            raise TypeError("operations must map known operation IDs to immutable report tuples")
        copied: dict[str, tuple[LayerReport, ...]] = {}
        for operation, reports in self.operations.items():
            if operation not in _OPERATION_CAPABILITIES:
                raise DebuggerPolicyError(f"unknown debugger operation: {operation!r}")
            if type(reports) is not tuple or any(type(report) is not LayerReport for report in reports):
                raise TypeError("operation reports must be tuples of LayerReport")
            layers = [report.layer for report in reports]
            if len(set(layers)) != len(layers):
                raise DebuggerPolicyError(f"duplicate layer report for {operation}")
            for report in reports:
                if (report.session_id, report.target_id, report.revision) != (
                    self.session_id, self.target_id, self.revision,
                ):
                    raise DebuggerPolicyError(f"stale or cross-target report for {operation}")
            copied[operation] = reports
        object.__setattr__(self, "operations", MappingProxyType(copied))

    @property
    def capabilities(self) -> frozenset[Capability]:
        """Capabilities proven available by every required layer in this revision."""
        result = set()
        for operation, reports in self.operations.items():
            if {report.layer for report in reports} != _REQUIRED_LAYERS:
                continue
            if all(report.state is Availability.AVAILABLE for report in reports):
                if operation == "memory.read" and self.pointer_bits is None:
                    continue
                result.add(_OPERATION_CAPABILITIES[operation])
        return frozenset(result)

    def provider_spec(self, identity: ProviderId, api: ApiVersion) -> ProviderSpec:
        """Create a Core provider declaration; host must own its activation factory."""
        if type(identity) is not ProviderId or type(api) is not ApiVersion:
            raise TypeError("provider identity and API version have required contract types")
        _reject_kpm_identity(identity.value)
        return ProviderSpec(identity, api, self.capabilities)


def negotiate_debugger(
    registry: Registry,
    *,
    request_id: str,
    extension: ExtensionId,
    provider: ProviderId,
    api: ApiVersion,
    required: frozenset[Capability],
    granted: frozenset[Capability],
) -> Outcome[Activation]:
    """Apply the Core registry's API/capability/grant admission to a debugger.

    Extensions cannot refer to KPM as either their identity or provider, and
    cannot ask this API to load an adapter or hook implementation.
    """
    if type(registry) is not Registry or type(extension) is not ExtensionId:
        raise TypeError("registry and extension identity have required contract types")
    if type(provider) is not ProviderId:
        raise TypeError("provider identity has required contract type")
    _reject_kpm_identity(extension.value)
    _reject_kpm_identity(provider.value)
    return registry.negotiate(
        request_id=request_id,
        extension=extension,
        provider=provider,
        api=api,
        required=required,
        granted=granted,
    )


def _identity(value: object, name: str) -> None:
    if type(value) is not str or not value or len(value) > 256 or not value.isascii():
        raise ValueError(f"{name} must be a bounded, nonempty ASCII identifier")


def _reject_kpm_identity(value: str) -> None:
    # KPM is excluded by host policy; package metadata cannot turn it into a
    # supported debugger provider or capability source.
    if any(part == "kpm" for part in value.casefold().replace("_", ".").replace("-", ".").split(".")):
        raise DebuggerPolicyError("KPM provider identities are forbidden by host policy")
