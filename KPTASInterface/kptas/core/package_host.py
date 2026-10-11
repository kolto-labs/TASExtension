"""Small, host-owned package lifecycle for Telos Core.

Package metadata is declarative. Only a lifecycle explicitly passed to
``register`` can run; a manifest can never name a Python module or import path.
This is an application organization/capability boundary, not a Python sandbox:
registered native packages execute with the host process's privileges.

The host deliberately owns no process, supervisor, score, journal, Qt widget,
or game-runtime objects. Callers publish narrow services and keep their
existing owners authoritative.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Protocol


MANIFEST_SCHEMA = "telos.extension/v1"
STATE_SCHEMA = "telos.core.package-state/v1"
CORE_API = ("telos.core.extension", 1, 0)

_ID = re.compile(r"^[a-z][a-z0-9.-]{0,79}$")
_VERSION = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-[0-9A-Za-z.-]+)?$")
_STORAGE_KEY = re.compile(r"^[A-Za-z0-9._-]{1,80}$")
_MAX_STORAGE_VALUE = 65_536
_MAX_STATE_FILE = 4 * 1024 * 1024


class HostError(RuntimeError):
    """A stable, caller-displayable Core package error."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


@dataclass(frozen=True)
class Manifest:
    id: str
    name: str
    version: str
    description: str
    api_id: str
    api_major: int
    api_minor: int
    requires: tuple[str, ...] = ()
    permissions: tuple[str, ...] = ()
    provides: tuple[str, ...] = ()
    default_installed: bool = False
    default_enabled: bool = False

    @classmethod
    def parse(cls, value: Mapping[str, Any]) -> "Manifest":
        """Validate declarative metadata without importing or evaluating code."""
        if not isinstance(value, Mapping):
            raise HostError("CORE_MANIFEST_INVALID", "manifest must be an object")
        api = value.get("api")
        if (value.get("schema") != MANIFEST_SCHEMA
                or not isinstance(value.get("id"), str) or not _ID.fullmatch(value["id"])
                or not isinstance(value.get("name"), str) or not value["name"].strip()
                or len(value["name"]) > 100
                or not isinstance(value.get("version"), str) or not _VERSION.fullmatch(value["version"])
                or not isinstance(value.get("description"), str) or not value["description"].strip()
                or len(value["description"]) > 500
                or not isinstance(api, Mapping)
                or api.get("id") != CORE_API[0]
                or type(api.get("major")) is not int or api["major"] != CORE_API[1]
                or type(api.get("minor")) is not int or not 0 <= api["minor"] <= CORE_API[2]):
            raise HostError("CORE_MANIFEST_INVALID", "expected a compatible telos.extension/v1 manifest")

        def identifiers(field: str) -> tuple[str, ...]:
            items = value.get(field, ())
            if not isinstance(items, (list, tuple)):
                raise HostError("CORE_MANIFEST_INVALID", f"{field} must be a list")
            if any(not isinstance(item, str) or not _ID.fullmatch(item) for item in items):
                raise HostError("CORE_MANIFEST_INVALID", f"invalid identifier in {field}")
            if len(set(items)) != len(items):
                raise HostError("CORE_MANIFEST_INVALID", f"duplicate identifier in {field}")
            return tuple(items)

        requires = identifiers("requires")
        permissions = identifiers("permissions")
        provides = identifiers("provides")
        if value["id"] in requires:
            raise HostError("CORE_MANIFEST_INVALID", "a package cannot require itself")
        default = value.get("defaultState")
        if default is None:
            default = {}
        if (not isinstance(default, Mapping)
                or ("installed" in default and type(default["installed"]) is not bool)
                or ("enabled" in default and type(default["enabled"]) is not bool)):
            raise HostError("CORE_MANIFEST_INVALID", "defaultState must contain boolean flags")
        installed = default.get("installed", False)
        enabled = installed and default.get("enabled", False)
        return cls(value["id"], value["name"].strip(), value["version"],
                   value["description"].strip(), api["id"], api["major"], api["minor"],
                   requires, permissions, provides, installed, enabled)


@dataclass(frozen=True)
class PackageState:
    installed: bool = False
    enabled: bool = False
    active: bool = False
    cleanup_pending: bool = False
    error: str = ""


@dataclass(frozen=True)
class ChangeReceipt:
    package_ids: tuple[str, ...]
    action: str
    persisted: bool


class PackageLifecycle(Protocol):
    def activate(self, context: "HostContext") -> Callable[[], object] | None: ...
    def deactivate(self, context: "HostContext") -> object: ...


class PackageStateStore(Protocol):
    def load(self) -> Mapping[str, Any]: ...
    def save(self, state: Mapping[str, Any]) -> None: ...


class MemoryPackageStateStore:
    """Explicit visit-only store, useful until a Core profile path is wired."""

    def __init__(self, initial: Mapping[str, Any] | None = None) -> None:
        self._value = _json_copy(initial or {"schema": STATE_SCHEMA, "packages": {}, "data": {}})

    def load(self) -> Mapping[str, Any]:
        return _json_copy(self._value)

    def save(self, state: Mapping[str, Any]) -> None:
        self._value = _json_copy(state)


class JsonPackageStateStore:
    """Atomic per-profile persistence; callers choose and own the profile path."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)

    def load(self) -> Mapping[str, Any]:
        try:
            raw = self.path.read_bytes()
            if len(raw) > _MAX_STATE_FILE:
                raise HostError("CORE_STATE_INVALID", "package state exceeds the size limit")
            value = json.loads(raw)
        except FileNotFoundError:
            return {"schema": STATE_SCHEMA, "packages": {}, "data": {}}
        except HostError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError, RecursionError) as exc:
            raise HostError("CORE_STORAGE_UNAVAILABLE", str(exc)) from exc
        return _validate_state(value)

    def save(self, state: Mapping[str, Any]) -> None:
        value = _validate_state(state)
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        if len(raw) > _MAX_STATE_FILE:
            raise HostError("CORE_STATE_INVALID", "package state exceeds the size limit")
        temporary: str | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=self.path.parent, prefix=f".{self.path.name}.",
                                             suffix=".tmp", delete=False) as stream:
                temporary = stream.name
                os.chmod(temporary, 0o600)
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            temporary = None
            directory_fd = os.open(self.path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except (OSError, ValueError, TypeError) as exc:
            raise HostError("CORE_STORAGE_UNAVAILABLE", str(exc)) from exc
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass


def _json_copy(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError, json.JSONDecodeError, RecursionError) as exc:
        raise HostError("CORE_STATE_INVALID", "state must contain JSON values") from exc


def _validate_state(value: Any) -> dict[str, Any]:
    if (not isinstance(value, Mapping) or value.get("schema") != STATE_SCHEMA
            or not isinstance(value.get("packages"), Mapping)
            or not isinstance(value.get("data"), Mapping)):
        raise HostError("CORE_STATE_INVALID", "expected a telos.core.package-state/v1 object")
    packages: dict[str, dict[str, bool]] = {}
    for package_id, state in value["packages"].items():
        if (not isinstance(package_id, str) or not _ID.fullmatch(package_id)
                or not isinstance(state, Mapping)
                or type(state.get("installed")) is not bool
                or type(state.get("enabled")) is not bool):
            continue
        packages[package_id] = {"installed": state["installed"],
                                "enabled": state["installed"] and state["enabled"]}
    data: dict[str, dict[str, Any]] = {}
    for package_id, entries in value["data"].items():
        if not isinstance(package_id, str) or not _ID.fullmatch(package_id) or not isinstance(entries, Mapping):
            continue
        clean: dict[str, Any] = {}
        for key, item in entries.items():
            if isinstance(key, str) and _STORAGE_KEY.fullmatch(key):
                copied = _json_copy(item)
                encoded = json.dumps(copied, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
                if len(encoded) <= _MAX_STORAGE_VALUE:
                    clean[key] = copied
        data[package_id] = clean
    return {"schema": STATE_SCHEMA, "packages": packages, "data": data}


class _PackageStorage:
    def __init__(self, host: "ExtensionHost", package_id: str) -> None:
        self._host, self._package_id = host, package_id

    def get(self, key: str) -> Any:
        key = self._key(key)
        return _json_copy(self._host._state["data"].get(self._package_id, {}).get(key))

    def set(self, key: str, value: Any) -> bool:
        key = self._key(key)
        copied = _json_copy(value)
        encoded = json.dumps(copied, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
        if len(encoded) > _MAX_STORAGE_VALUE:
            raise HostError("CORE_STORAGE_VALUE_TOO_LARGE")
        entries = self._host._state["data"].setdefault(self._package_id, {})
        previous = entries.get(key, _MISSING)
        entries[key] = copied
        if self._host._persist():
            return True
        if previous is _MISSING:
            entries.pop(key, None)
        else:
            entries[key] = previous
        return False

    def remove(self, key: str) -> bool:
        key = self._key(key)
        entries = self._host._state["data"].setdefault(self._package_id, {})
        previous = entries.pop(key, _MISSING)
        if previous is _MISSING:
            return self._host._storage_ok
        if self._host._persist():
            return True
        entries[key] = previous
        return False

    @staticmethod
    def _key(key: str) -> str:
        if not isinstance(key, str) or not _STORAGE_KEY.fullmatch(key):
            raise HostError("CORE_STORAGE_KEY_INVALID")
        return key


_MISSING = object()


@dataclass(frozen=True)
class HostContext:
    """Narrow package context; Python privacy is organizational, not sandboxing."""
    package_id: str
    package_version: str
    core_api: tuple[str, int, int]
    _host: "ExtensionHost"

    @property
    def storage(self) -> _PackageStorage:
        return _PackageStorage(self._host, self.package_id)

    def service(self, service_id: str) -> Any:
        entry = self._host._packages[self.package_id]
        if service_id not in entry.manifest.permissions:
            raise HostError("CORE_CAPABILITY_NOT_DECLARED", f"{self.package_id} cannot use {service_id}")
        service = self._host._services.get(service_id)
        if service is None:
            raise HostError("CORE_SERVICE_UNAVAILABLE", service_id)
        return service


@dataclass
class _RegisteredPackage:
    manifest: Manifest
    lifecycle: PackageLifecycle
    disposer: Callable[[], object] | None = None


class ExtensionHost:
    """Registry and lifecycle coordinator for explicitly trusted package code."""

    def __init__(self, state_store: PackageStateStore | None = None) -> None:
        self._store = state_store or MemoryPackageStateStore()
        self._state = _validate_state(self._store.load())
        self._storage_ok = True
        self._services: dict[str, Any] = {}
        self._packages: dict[str, _RegisteredPackage] = {}
        self._active: set[str] = set()
        self._cleanup_pending: set[str] = set()
        self._changing: set[str] = set()
        self._errors: dict[str, str] = {}
        self._started = False

    @property
    def storage_available(self) -> bool:
        return self._storage_ok

    @property
    def started(self) -> bool:
        return self._started

    def provide_service(self, service_id: str, service: Any) -> Callable[[], None]:
        if not isinstance(service_id, str) or not _ID.fullmatch(service_id) or service is None:
            raise HostError("CORE_SERVICE_INVALID", str(service_id))
        if service_id in self._services:
            raise HostError("CORE_SERVICE_DUPLICATE", service_id)
        self._services[service_id] = service

        def revoke() -> None:
            if self._services.get(service_id) is not service:
                return
            users = [package_id for package_id in self._active
                     if service_id in self._packages[package_id].manifest.permissions]
            if users:
                raise HostError("CORE_SERVICE_IN_USE", ", ".join(sorted(users)))
            del self._services[service_id]

        return revoke

    def register(self, manifest_value: Mapping[str, Any], lifecycle: PackageLifecycle) -> Manifest:
        if self._started:
            raise HostError("CORE_REGISTRATION_CLOSED", "register trusted packages before start()")
        manifest = Manifest.parse(manifest_value)
        if manifest.id in self._packages:
            raise HostError("CORE_PACKAGE_DUPLICATE_OR_INVALID", manifest.id)
        if not callable(getattr(lifecycle, "activate", None)) or not callable(getattr(lifecycle, "deactivate", None)):
            raise HostError("CORE_PACKAGE_DUPLICATE_OR_INVALID", manifest.id)
        self._packages[manifest.id] = _RegisteredPackage(manifest, lifecycle)
        if manifest.id not in self._state["packages"]:
            self._state["packages"][manifest.id] = {
                "installed": manifest.default_installed,
                "enabled": manifest.default_enabled,
            }
            self._persist()
        return manifest

    def start(self) -> Mapping[str, str]:
        """Activate persisted enabled packages in dependency order; isolate failures."""
        if self._started:
            raise HostError("CORE_HOST_ALREADY_STARTED")
        self._started = True
        pending = [package_id for package_id in self._packages if self.state(package_id).enabled]
        for package_id in pending:
            try:
                for dependency in self._closure(package_id):
                    state = self.state(dependency)
                    if not state.installed:
                        raise HostError("CORE_DEPENDENCY_NOT_INSTALLED", dependency)
                    if not state.enabled:
                        raise HostError("CORE_DEPENDENCY_NOT_ENABLED", dependency)
                    if dependency not in self._active:
                        self._activate(dependency)
            except Exception as exc:
                self._record_start_failure(package_id, exc)
        return dict(self._errors)

    def manifests(self) -> tuple[Manifest, ...]:
        return tuple(entry.manifest for entry in self._packages.values())

    def state(self, package_id: str) -> PackageState:
        saved = self._state["packages"].get(package_id, {})
        return PackageState(saved.get("installed", False), saved.get("enabled", False),
                            package_id in self._active, package_id in self._cleanup_pending,
                            self._errors.get(package_id, ""))

    def dependents(self, package_id: str, *, installed_only: bool = True) -> tuple[str, ...]:
        return tuple(sorted(
            candidate for candidate, entry in self._packages.items()
            if package_id in entry.manifest.requires
            and (not installed_only or self.state(candidate).installed)
        ))

    def change(self, package_id: str, action: str) -> ChangeReceipt:
        if package_id not in self._packages:
            raise HostError("CORE_PACKAGE_UNKNOWN", package_id)
        if action not in {"install", "enable", "disable", "remove"}:
            raise HostError("CORE_ACTION_INVALID", action)
        if package_id in self._changing:
            raise HostError("CORE_CHANGE_IN_PROGRESS", package_id)
        current = self.state(package_id)
        enable_closure: tuple[str, ...] = ()
        enabled_before: dict[str, bool] = {}
        if action == "enable":
            if not current.installed:
                raise HostError("CORE_PACKAGE_NOT_INSTALLED", package_id)
            enable_closure = self._closure(package_id)
            # Preflight the entire dependency graph before mutating even one
            # profile flag or activating one package. A pending cleanup in a
            # dependency is just as unsafe as one on the requested package.
            pending_cleanup = tuple(item for item in enable_closure
                                    if item in self._cleanup_pending)
            if pending_cleanup:
                raise HostError("CORE_CLEANUP_RETRY_REQUIRED", ", ".join(pending_cleanup))
            missing = tuple(item for item in enable_closure
                            if not self._state["packages"].get(item, {}).get("installed", False))
            if missing:
                raise HostError("CORE_DEPENDENCY_NOT_INSTALLED", ", ".join(missing))
            enabled_before = {
                item: bool(self._state["packages"].get(item, {}).get("enabled", False))
                for item in enable_closure
            }
        installed_dependents = self.dependents(package_id)
        active_dependents = tuple(dependency for dependency in installed_dependents
                                  if self.state(dependency).enabled or self.state(dependency).active)
        if action in {"disable", "remove"} and active_dependents:
            raise HostError("CORE_PACKAGE_IN_USE", f"active dependents: {', '.join(active_dependents)}")
        if action == "remove" and installed_dependents:
            raise HostError("CORE_PACKAGE_IN_USE", f"installed dependents: {', '.join(installed_dependents)}")

        self._changing.add(package_id)
        affected = (package_id,)
        try:
            if action == "install" and not current.installed:
                affected = self._closure(package_id)
                for item in affected:
                    saved = self._state["packages"].setdefault(item, {"installed": False, "enabled": False})
                    saved["installed"] = True
            elif action == "enable":
                affected = enable_closure
                if not self._started:
                    for item in affected:
                        self._state["packages"][item]["enabled"] = True
                    persisted = self._persist()
                    return ChangeReceipt(tuple(affected), action, persisted)
                newly_active: list[str] = []
                try:
                    for item in affected:
                        if item not in self._active:
                            self._activate(item)
                            newly_active.append(item)
                        self._state["packages"][item]["enabled"] = True
                except Exception as activation_error:
                    cleanup_errors: list[str] = []
                    for item in reversed(newly_active):
                        try:
                            # Delay persistence until the full closure has
                            # been restored. Otherwise a cleanup failure could
                            # durably save a partially enabled transaction.
                            self._deactivate(item, persist_failure=False)
                        except Exception as cleanup_error:
                            cleanup_errors.append(f"{item}: {cleanup_error}")
                    # Roll the full dependency closure back to its prior
                    # desired state. A package whose cleanup failed must stay
                    # disabled despite the earlier snapshot, or it could be
                    # treated as safely enabled while still partially active.
                    for item, was_enabled in enabled_before.items():
                        self._state["packages"][item]["enabled"] = (
                            was_enabled and item not in self._cleanup_pending
                        )
                    rollback_persisted = self._persist()
                    details = []
                    if cleanup_errors:
                        details.append("cleanup failed: " + "; ".join(cleanup_errors))
                    if not rollback_persisted:
                        details.append("enable rollback is session-only because profile storage is unavailable")
                    if details:
                        detail = "; ".join(details)
                        if isinstance(activation_error, HostError):
                            raise HostError(activation_error.code,
                                            f"{activation_error.detail}; {detail}".strip("; ")) from activation_error
                        raise HostError("CORE_ACTIVATION_FAILED",
                                        f"{activation_error}; {detail}") from activation_error
                    raise
            elif action in {"disable", "remove"} and (current.enabled or current.active or current.cleanup_pending):
                self._deactivate(package_id)
                self._state["packages"].setdefault(package_id, {"installed": True, "enabled": False})["enabled"] = False

            if action == "disable":
                self._state["packages"].setdefault(package_id, {"installed": current.installed, "enabled": False})["enabled"] = False
            elif action == "remove":
                self._state["packages"].pop(package_id, None)
            persisted = self._persist()
            return ChangeReceipt(tuple(affected), action, persisted)
        except HostError as exc:
            self._errors[package_id] = str(exc)
            raise
        except Exception as exc:
            self._errors[package_id] = str(exc)
            raise HostError("CORE_PACKAGE_CHANGE_FAILED", str(exc)) from exc
        finally:
            self._changing.discard(package_id)

    def retry_cleanup(self, package_id: str) -> ChangeReceipt:
        if package_id not in self._cleanup_pending:
            raise HostError("CORE_CLEANUP_NOT_PENDING", package_id)
        self._deactivate(package_id)
        self._state["packages"].setdefault(package_id, {"installed": True, "enabled": False})["enabled"] = False
        return ChangeReceipt((package_id,), "cleanup", self._persist())

    def _closure(self, package_id: str) -> tuple[str, ...]:
        visiting: set[str] = set()
        complete: set[str] = set()
        ordered: list[str] = []

        def visit(current: str) -> None:
            if current not in self._packages:
                raise HostError("CORE_PACKAGE_UNKNOWN", current)
            if current in visiting:
                raise HostError("CORE_DEPENDENCY_CYCLE", current)
            if current in complete:
                return
            visiting.add(current)
            for dependency in self._packages[current].manifest.requires:
                visit(dependency)
            visiting.remove(current)
            complete.add(current)
            ordered.append(current)

        visit(package_id)
        return tuple(ordered)

    def _context(self, package_id: str) -> HostContext:
        manifest = self._packages[package_id].manifest
        return HostContext(package_id, manifest.version, CORE_API, self)

    def _activate(self, package_id: str) -> None:
        if package_id in self._cleanup_pending:
            raise HostError("CORE_CLEANUP_RETRY_REQUIRED", package_id)
        entry = self._packages[package_id]
        try:
            disposer = entry.lifecycle.activate(self._context(package_id))
            if disposer is not None and not callable(disposer):
                raise HostError("CORE_DISPOSER_INVALID", package_id)
            entry.disposer = disposer
            self._active.add(package_id)
            self._cleanup_pending.discard(package_id)
            self._errors.pop(package_id, None)
        except Exception as exc:
            message = str(exc)
            try:
                entry.lifecycle.deactivate(self._context(package_id))
            except Exception as cleanup_error:
                self._cleanup_pending.add(package_id)
                message += f"; CORE_CLEANUP_FAILED: {cleanup_error}"
            if entry.disposer is not None:
                try:
                    entry.disposer()
                    entry.disposer = None
                except Exception as cleanup_error:
                    self._cleanup_pending.add(package_id)
                    message += f"; CORE_CLEANUP_FAILED: {cleanup_error}"
            self._errors[package_id] = message
            if isinstance(exc, HostError):
                raise
            raise HostError("CORE_ACTIVATION_FAILED", message) from exc

    def _deactivate(self, package_id: str, *, persist_failure: bool = True) -> None:
        entry = self._packages[package_id]
        failures: list[str] = []
        try:
            entry.lifecycle.deactivate(self._context(package_id))
        except Exception as exc:
            failures.append(str(exc))
        if entry.disposer is not None:
            try:
                entry.disposer()
                entry.disposer = None
            except Exception as exc:
                failures.append(str(exc))
        if failures:
            self._cleanup_pending.add(package_id)
            self._state["packages"].setdefault(package_id, {"installed": True, "enabled": False})["enabled"] = False
            persisted = self._persist() if persist_failure else None
            detail = "; ".join(failures)
            if persisted is False:
                detail += "; CORE_STORAGE_UNAVAILABLE: disabled state applies for this session only"
            message = f"CORE_CLEANUP_FAILED: {detail}"
            self._errors[package_id] = message
            raise HostError("CORE_CLEANUP_FAILED", message)
        self._active.discard(package_id)
        self._cleanup_pending.discard(package_id)
        self._errors.pop(package_id, None)

    def _record_start_failure(self, package_id: str, exc: Exception) -> None:
        state = self._state["packages"].setdefault(package_id, {"installed": True, "enabled": False})
        state["enabled"] = False
        self._errors[package_id] = str(exc)
        self._persist()

    def _persist(self) -> bool:
        try:
            self._store.save(self._state)
            self._storage_ok = True
            return True
        except Exception:
            self._storage_ok = False
            return False
