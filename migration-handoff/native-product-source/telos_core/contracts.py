"""Small, dependency-free contracts for local Telos Core providers.

API versions here are Core facade versions. A TAS API or loader ABI has its own
version and must be negotiated separately by its owner.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Generic, TypeVar


@dataclass(frozen=True, order=True)
class ApiVersion:
    major: int
    minor: int

    def __post_init__(self) -> None:
        if type(self.major) is not int or type(self.minor) is not int:
            raise TypeError("API version components must be integers")
        if self.major < 1 or self.minor < 0:
            raise ValueError("API version requires positive major and nonnegative minor")


@dataclass(frozen=True)
class ExtensionId:
    value: str

    def __post_init__(self) -> None:
        if type(self.value) is not str or not self.value or not self.value.isascii() or any(
            c not in "abcdefghijklmnopqrstuvwxyz0123456789._-" for c in self.value
        ):
            raise ValueError("extension identity must be lowercase ASCII")


@dataclass(frozen=True)
class ProviderId:
    value: str

    def __post_init__(self) -> None:
        if type(self.value) is not str or not self.value or not self.value.isascii() or any(
            c not in "abcdefghijklmnopqrstuvwxyz0123456789._-" for c in self.value
        ):
            raise ValueError("provider identity must be lowercase ASCII")


class Permission(str, Enum):
    OBSERVE = "observe"
    MUTATE = "mutate"


@dataclass(frozen=True)
class Capability:
    name: str
    permission: Permission

    def __post_init__(self) -> None:
        if type(self.name) is not str or not self.name or not self.name.isascii() or any(
            c not in "abcdefghijklmnopqrstuvwxyz0123456789._-" for c in self.name
        ):
            raise ValueError("capability name must be lowercase ASCII")
        if type(self.permission) is not Permission:
            raise TypeError("capability permission must be a Permission")


class OutcomeState(str, Enum):
    ACCEPTED = "accepted"
    REFUSED = "refused"
    COMPLETED = "completed"
    UNKNOWN = "unknown"


class RefusalCode(str, Enum):
    PROVIDER_MISSING = "provider_missing"
    API_MAJOR_UNSUPPORTED = "api_major_unsupported"
    API_MINOR_UNSUPPORTED = "api_minor_unsupported"
    CAPABILITY_MISSING = "capability_missing"
    PERMISSION_DENIED = "permission_denied"
    PROVIDER_FAILED = "provider_failed"
    REGISTRY_CLOSED = "registry_closed"


T = TypeVar("T")


@dataclass(frozen=True)
class Outcome(Generic[T]):
    """A refusal has a stable code; unknown means completion was not established."""

    request_id: str
    state: OutcomeState
    value: T | None = None
    refusal: RefusalCode | None = None

    def __post_init__(self) -> None:
        if type(self.request_id) is not str or not self.request_id:
            raise ValueError("request identity is required")
        if type(self.state) is not OutcomeState:
            raise TypeError("outcome state must be an OutcomeState")
        if self.refusal is not None and type(self.refusal) is not RefusalCode:
            raise TypeError("refusal must be a RefusalCode")
        if (self.state is OutcomeState.REFUSED) != (self.refusal is not None):
            raise ValueError("refusal code is required only for refused outcomes")
        if self.state not in (OutcomeState.COMPLETED, OutcomeState.ACCEPTED) and self.value is not None:
            raise ValueError("value requires an accepted or completed outcome")
