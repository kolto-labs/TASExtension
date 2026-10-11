"""Trusted native TAS package adapter for Telos Core.

Core owns the game session and workspace. This adapter contributes TAS
lifecycle policy through the narrow ``tas.runtime`` service, mirroring the
browser package contract without importing a UI toolkit or taking ownership of
the supervisor, score store, process, or input transport.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Protocol

from .package_host import ExtensionHost, HostContext, HostError


TAS_PACKAGE_ID = "telos.tas"
TAS_RUNTIME_SERVICE = "tas.runtime"

# Keep identity and feature IDs aligned with the browser package manifest.
# Registration remains explicit: this metadata never names an import path.
TAS_MANIFEST: Mapping[str, Any] = {
    "schema": "telos.extension/v1",
    "id": TAS_PACKAGE_ID,
    "name": "TAS",
    "version": "1.0.0",
    "api": {"id": "telos.core.extension", "major": 1, "minor": 0},
    "defaultState": {"installed": False, "enabled": False},
    "requires": [],
    "permissions": [TAS_RUNTIME_SERVICE],
    "provides": ["tas.authoring", "tas.playback", "tas.input-recording", "tas.branch-workflow"],
    "description": "Author inputs, record scores, edit the piano roll, and manage run branches.",
}


class TasRuntimeService(Protocol):
    """Core-owned bridge into the existing native TAS workspace."""

    def setEnabled(self, enabled: bool) -> object: ...
    def setPanelsAvailable(self, available: bool) -> object: ...
    def refreshKeyboard(self) -> object: ...
    def invalidate(self) -> object: ...
    def start(self) -> object: ...
    def stop(self) -> object: ...
    def invalidatePendingWork(self) -> object: ...
    def flushEdits(self) -> object: ...
    def pause(self) -> object: ...
    def stopRecording(self) -> object: ...
    def clearRecording(self) -> object: ...
    def releaseInputs(self, reason: str) -> object: ...
    def cancelCapture(self) -> object: ...
    def resetWorkspace(self) -> object: ...


_ACTIVATE = ("setEnabled", "setPanelsAvailable", "refreshKeyboard", "invalidate", "start")
_DEACTIVATE = (
    "invalidatePendingWork", "flushEdits", "pause", "stopRecording", "clearRecording",
    "releaseInputs", "cancelCapture", "resetWorkspace", "setEnabled", "setPanelsAvailable", "stop",
)


def _runtime(context: HostContext) -> TasRuntimeService:
    service = context.service(TAS_RUNTIME_SERVICE)
    if any(not callable(getattr(service, name, None)) for name in (*_ACTIVATE, *_DEACTIVATE)):
        raise HostError("TAS_RUNTIME_CONTRACT_INVALID",
                        "Core service tas.runtime is missing one or more lifecycle operations")
    return service


class TasPackageLifecycle:
    """Optional TAS package lifecycle; the host remains owner of execution."""

    def __init__(self) -> None:
        # Retain the exact service used during activation so retry_cleanup can
        # still unwind it if Core temporarily withdraws service discovery.
        self._bound_runtime: TasRuntimeService | None = None

    def activate(self, context: HostContext) -> Callable[[], object] | None:
        runtime = _runtime(context)
        self._bound_runtime = runtime
        runtime.setEnabled(True)
        runtime.setPanelsAvailable(True)
        runtime.refreshKeyboard()
        runtime.invalidate()
        runtime.start()
        return None

    def deactivate(self, context: HostContext) -> None:
        # A failed lookup before activation had no side effects to clean up.
        # This also avoids poisoning Core's retry state when tas.runtime was
        # simply not provided at registration time.
        runtime = self._bound_runtime
        if runtime is None:
            return
        failures: list[str] = []
        for name in _DEACTIVATE:
            try:
                method = getattr(runtime, name)
                if name == "releaseInputs":
                    method("tas-extension-disabled")
                elif name == "setEnabled":
                    method(False)
                elif name == "setPanelsAvailable":
                    method(False)
                else:
                    method()
            except Exception as exc:
                failures.append(f"{name}: {type(exc).__name__}: {exc}")
        if failures:
            raise HostError("TAS_CLEANUP_FAILED", "; ".join(failures))
        self._bound_runtime = None


def register_tas_package(host: ExtensionHost) -> None:
    """Register the trusted first-party adapter before ``host.start()``.

    The Core application must first provide ``tas.runtime``. Enrollment tools
    that do not own a live workspace should register their own non-activating
    lifecycle instead.
    """
    host.register(TAS_MANIFEST, TasPackageLifecycle())
