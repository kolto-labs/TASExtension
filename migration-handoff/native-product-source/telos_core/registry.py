"""Local provider discovery, admission, and bounded extension-owned events."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from threading import RLock
from typing import Callable, Protocol

from .contracts import (
    ApiVersion, Capability, ExtensionId, Outcome, OutcomeState, ProviderId,
    RefusalCode,
)


class Disposable(Protocol):
    def close(self) -> None: ...


@dataclass(frozen=True)
class ProviderSpec:
    identity: ProviderId
    api: ApiVersion
    capabilities: frozenset[Capability]

    def __post_init__(self) -> None:
        if type(self.identity) is not ProviderId or type(self.api) is not ApiVersion:
            raise TypeError("provider identity and API version have required contract types")
        if type(self.capabilities) is not frozenset or any(
            type(capability) is not Capability for capability in self.capabilities
        ):
            raise TypeError("provider capabilities must be a frozenset of Capability")


@dataclass(frozen=True)
class Event:
    topic: str
    payload: bytes


class Subscription:
    """FIFO with drop-oldest overflow; poll reports the number dropped."""

    def __init__(self, owner: Activation, capacity: int, max_event_bytes: int) -> None:
        if type(capacity) is not int or type(max_event_bytes) is not int:
            raise TypeError("event limits must be integers")
        if capacity < 1 or max_event_bytes < 1:
            raise ValueError("event limits must be positive")
        self._owner = owner
        self._capacity = capacity
        self._max_event_bytes = max_event_bytes
        self._events: deque[Event] = deque()
        self._dropped = 0
        self._closed = False

    @property
    def closed(self) -> bool:
        with self._owner._lock:
            return self._closed

    def _push(self, event: Event) -> None:
        if len(event.topic.encode("utf-8")) + len(event.payload) > self._max_event_bytes:
            self._dropped += 1
            return
        if len(self._events) >= self._capacity:
            self._events.popleft()
            self._dropped += 1
        self._events.append(event)

    def poll(self) -> tuple[tuple[Event, ...], int]:
        with self._owner._lock:
            events = tuple(self._events)
            dropped = self._dropped
            self._events.clear()
            self._dropped = 0
            return events, dropped

    def close(self) -> None:
        with self._owner._lock:
            self._closed = True
            self._events.clear()
            self._owner._subscriptions.discard(self)


class Activation:
    """An admitted extension's local resources. Closing is idempotent."""

    def __init__(self, extension: ExtensionId, spec: ProviderSpec) -> None:
        self.extension = extension
        self.provider = spec
        self._lock = RLock()
        self._resources: list[Disposable] = []
        self._subscriptions: set[Subscription] = set()
        self._closed = False

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    def own(self, resource: Disposable) -> Disposable:
        with self._lock:
            if self._closed:
                raise RuntimeError("activation is closed")
            self._resources.append(resource)
            return resource

    def subscribe(self, *, capacity: int = 32, max_event_bytes: int = 4096) -> Subscription:
        with self._lock:
            if self._closed:
                raise RuntimeError("activation is closed")
            subscription = Subscription(self, capacity, max_event_bytes)
            self._subscriptions.add(subscription)
            return subscription

    def emit(self, topic: str, payload: bytes) -> None:
        if type(topic) is not str or not topic or type(payload) is not bytes:
            raise ValueError("event requires a topic and immutable bytes payload")
        event = Event(topic, payload)
        with self._lock:
            if self._closed:
                raise RuntimeError("activation is closed")
            for subscription in tuple(self._subscriptions):
                subscription._push(event)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            for subscription in tuple(self._subscriptions):
                subscription.close()
            resources = self._resources[::-1]
            self._resources.clear()
        for resource in resources:
            try:
                resource.close()
            except Exception:
                # Continue releasing other owned resources after one close fails.
                pass


ProviderFactory = Callable[[Activation], None]


class Registry:
    def __init__(self) -> None:
        self._providers: dict[ProviderId, tuple[ProviderSpec, ProviderFactory]] = {}
        self._activations: list[Activation] = []
        self._lock = RLock()
        self._closed = False

    def register(self, spec: ProviderSpec, activate: ProviderFactory) -> None:
        with self._lock:
            if self._closed:
                raise RuntimeError("registry is closed")
            if spec.identity in self._providers:
                raise ValueError("provider already registered")
            self._providers[spec.identity] = (spec, activate)

    def providers(self) -> tuple[ProviderSpec, ...]:
        with self._lock:
            return tuple(spec for spec, _ in self._providers.values())

    def negotiate(
        self, *, request_id: str, extension: ExtensionId, provider: ProviderId,
        api: ApiVersion, required: frozenset[Capability],
        granted: frozenset[Capability],
    ) -> Outcome[Activation]:
        # Validate all caller-owned fields before a provider factory can run.
        if type(request_id) is not str or not request_id:
            raise ValueError("request identity is required")
        if type(extension) is not ExtensionId or type(provider) is not ProviderId:
            raise TypeError("extension and provider identities have required contract types")
        if type(api) is not ApiVersion:
            raise TypeError("API version must be an ApiVersion")
        if type(required) is not frozenset or type(granted) is not frozenset or any(
            type(capability) is not Capability for capability in required | granted
        ):
            raise TypeError("required and granted capabilities must be frozensets of Capability")
        with self._lock:
            if self._closed:
                return Outcome(request_id, OutcomeState.REFUSED, refusal=RefusalCode.REGISTRY_CLOSED)
            entry = self._providers.get(provider)
            if entry is None:
                return Outcome(request_id, OutcomeState.REFUSED, refusal=RefusalCode.PROVIDER_MISSING)
            spec, factory = entry
            if api.major != spec.api.major:
                code = RefusalCode.API_MAJOR_UNSUPPORTED
            elif api.minor > spec.api.minor:
                code = RefusalCode.API_MINOR_UNSUPPORTED
            elif not required <= spec.capabilities:
                code = RefusalCode.CAPABILITY_MISSING
            elif not required <= granted:
                code = RefusalCode.PERMISSION_DENIED
            else:
                code = None
            if code is not None:
                return Outcome(request_id, OutcomeState.REFUSED, refusal=code)
            activation = Activation(extension, spec)
            try:
                factory(activation)
            except Exception:
                activation.close()
                return Outcome(request_id, OutcomeState.REFUSED, refusal=RefusalCode.PROVIDER_FAILED)
            if self._closed or activation.closed:
                activation.close()
                return Outcome(request_id, OutcomeState.REFUSED, refusal=RefusalCode.PROVIDER_FAILED)
            self._activations.append(activation)
            return Outcome(request_id, OutcomeState.COMPLETED, value=activation)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            activations = self._activations[::-1]
            self._activations.clear()
        for activation in activations:
            activation.close()
