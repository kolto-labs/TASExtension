"""Resolved desired profiles, not package resolution or runtime admission.

The store is for a trusted local POSIX directory/filesystem supporting flock,
atomic replace and directory fsync. It retains every committed revision. Grants
are supplied decisions, not proof of permission approval or artifact trust.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

from .contracts import Capability, Permission

_ID = re.compile(r"[a-z0-9][a-z0-9_-]*\.[a-z0-9][a-z0-9_.-]*\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_VERSION = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?\Z")
_MAX_BYTES = 1024 * 1024


def _match(pattern, value, label):
    if type(value) is not str or not pattern.fullmatch(value):
        raise ValueError(f"invalid {label}")


def _fields(value, names):
    if type(value) is not dict or set(value) != set(names.split()):
        raise ValueError(f"expected exact fields: {names}")


def _array(value):
    if type(value) is not list:
        raise ValueError("expected array")
    return value


def _bytes(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def _decode(data):
    if len(data) > _MAX_BYTES:
        raise ValueError("profile exceeds size limit")
    return json.loads(data, object_pairs_hook=_pairs)


@dataclass(frozen=True)
class ReleaseRef:
    id: str
    version: str
    digest: str

    def __post_init__(self):
        _match(_ID, self.id, "package identity")
        _match(_VERSION, self.version, "exact SemVer")
        prerelease = _VERSION.fullmatch(self.version).group(4)
        if prerelease and any(p.isdigit() and len(p) > 1 and p[0] == "0" for p in prerelease.split(".")):
            raise ValueError("numeric prerelease identifiers cannot have leading zeros")
        _match(_DIGEST, self.digest, "SHA-256")

    def to_dict(self):
        return dict(id=self.id, version=self.version, digest=self.digest)

    @classmethod
    def from_dict(cls, value):
        _fields(value, "id version digest")
        return cls(**value)


@dataclass(frozen=True)
class ResolvedExtension:
    release: ReleaseRef
    configuration_digest: str
    grants: tuple[Capability, ...] = ()
    requires: tuple[ReleaseRef, ...] = ()
    conflicts: tuple[str, ...] = ()

    def __post_init__(self):
        if type(self.release) is not ReleaseRef:
            raise TypeError("release must be ReleaseRef")
        _match(_DIGEST, self.configuration_digest, "configuration SHA-256")
        for values, kind in ((self.grants, Capability), (self.requires, ReleaseRef), (self.conflicts, str)):
            if type(values) is not tuple or any(type(v) is not kind for v in values):
                raise TypeError("profile collections must be immutable typed tuples")
        if len({g.name for g in self.grants}) != len(self.grants):
            raise ValueError("duplicate capability grant")
        if len({r.id for r in self.requires}) != len(self.requires):
            raise ValueError("duplicate dependency")
        if len(set(self.conflicts)) != len(self.conflicts):
            raise ValueError("duplicate conflict")
        for conflict in self.conflicts:
            _match(_ID, conflict, "conflict identity")

    def to_dict(self):
        return dict(release=self.release.to_dict(), configuration_digest=self.configuration_digest,
                    grants=[dict(name=g.name, permission=g.permission.value) for g in sorted(self.grants, key=lambda g: g.name)],
                    requires=[r.to_dict() for r in sorted(self.requires, key=lambda r: r.id)],
                    conflicts=sorted(self.conflicts))

    @classmethod
    def from_dict(cls, value):
        _fields(value, "release configuration_digest grants requires conflicts")
        grants = []
        for grant in _array(value["grants"]):
            _fields(grant, "name permission")
            grants.append(Capability(grant["name"], Permission(grant["permission"])))
        return cls(ReleaseRef.from_dict(value["release"]), value["configuration_digest"], tuple(grants),
                   tuple(ReleaseRef.from_dict(r) for r in _array(value["requires"])), tuple(_array(value["conflicts"])))


@dataclass(frozen=True)
class ResolvedProfile:
    id: str
    extensions: tuple[ResolvedExtension, ...]
    schema_version: int = 1

    def __post_init__(self):
        _match(re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z"), self.id, "profile identity")
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("unsupported profile schema version")
        if type(self.extensions) is not tuple or any(type(e) is not ResolvedExtension for e in self.extensions):
            raise TypeError("extensions must be immutable ResolvedExtension tuple")
        entries = {e.release.id: e for e in self.extensions}
        if len(entries) != len(self.extensions):
            raise ValueError("duplicate package identity")
        for extension in self.extensions:
            for dependency in extension.requires:
                if dependency.id not in entries or entries[dependency.id].release != dependency:
                    raise ValueError(f"unsatisfied exact dependency: {extension.release.id} -> {dependency.id}")
            for conflict in extension.conflicts:
                if conflict in entries:
                    raise ValueError(f"conflict: {extension.release.id} -> {conflict}")
        self.dependency_order()

    def dependency_order(self) -> tuple[str, ...]:
        remaining = {e.release.id: {r.id for r in e.requires} for e in self.extensions}
        result = []
        while remaining:
            ready = sorted(key for key, deps in remaining.items() if not deps)
            if not ready:
                raise ValueError("dependency cycle among: " + ", ".join(sorted(remaining)))
            result.extend(ready)
            for key in ready:
                del remaining[key]
            for deps in remaining.values():
                deps.difference_update(ready)
        return tuple(result)

    def to_dict(self):
        return dict(schema_version=self.schema_version, id=self.id,
                    extensions=[e.to_dict() for e in sorted(self.extensions, key=lambda e: e.release.id)])

    @classmethod
    def from_dict(cls, value):
        _fields(value, "schema_version id extensions")
        return cls(value["id"], tuple(ResolvedExtension.from_dict(e) for e in _array(value["extensions"])), value["schema_version"])


@dataclass(frozen=True)
class ProfileRevision:
    digest: str
    parent: str | None
    profile: ResolvedProfile


class ProfileStore:
    """One named profile per pre-existing trusted directory; append-only history.

    A failed save may have committed its head: reopen to reconcile. Successful
    return follows directory fsync. No revision is deleted, even after undo.
    """
    def __init__(self, directory: str | Path):
        self.directory = Path(directory)
        if not self.directory.is_dir():
            raise ValueError("profile store directory must already exist")

    def _read(self, name):
        with (self.directory / name).open("rb") as stream:
            return stream.read(_MAX_BYTES + 1)

    def _sync(self):
        fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _write(self, name, data):
        fd, temporary = tempfile.mkstemp(prefix=".profile-", dir=self.directory)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.directory / name)
            self._sync()
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _head(self):
        try:
            value = _decode(self._read("head.json"))
        except FileNotFoundError:
            return None
        _fields(value, "revision")
        _match(_DIGEST, value["revision"], "head revision")
        return value["revision"]

    def load(self, revision: str | None = None) -> ProfileRevision | None:
        if revision is None:
            revision = self._head()
        if revision is None:
            return None
        _match(_DIGEST, revision, "revision")
        data = self._read(revision + ".json")
        if hashlib.sha256(data).hexdigest() != revision:
            raise ValueError("revision digest mismatch")
        value = _decode(data)
        _fields(value, "schema_version parent profile")
        if type(value["schema_version"]) is not int or value["schema_version"] != 1:
            raise ValueError("unsupported revision schema")
        if value["parent"] is not None:
            _match(_DIGEST, value["parent"], "parent revision")
        return ProfileRevision(revision, value["parent"], ResolvedProfile.from_dict(value["profile"]))

    def history(self) -> tuple[ProfileRevision, ...]:
        current = self.load()
        result = []
        seen = set()
        while current is not None:
            if current.digest in seen:
                raise ValueError("revision history cycle")
            seen.add(current.digest)
            result.append(current)
            current = self.load(current.parent) if current.parent is not None else None
        return tuple(result)

    def save(self, profile: ResolvedProfile, *, expected_revision: str | None) -> ProfileRevision:
        import fcntl  # Store platform qualification is deliberately POSIX-only.
        if type(profile) is not ResolvedProfile:
            raise TypeError("profile must be ResolvedProfile")
        if expected_revision is not None:
            _match(_DIGEST, expected_revision, "expected revision")
        with (self.directory / ".lock").open("a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            current = self.load()
            if (current.digest if current else None) != expected_revision:
                raise ValueError("profile changed since preview")
            if current and current.profile.id != profile.id:
                raise ValueError("store belongs to a different profile")
            data = _bytes(dict(schema_version=1, parent=expected_revision, profile=profile.to_dict()))
            if len(data) > _MAX_BYTES:
                raise ValueError("profile exceeds size limit")
            revision = hashlib.sha256(data).hexdigest()
            path = self.directory / (revision + ".json")
            if path.exists():
                if self._read(path.name) != data:
                    raise ValueError("existing revision is corrupt")
                self._sync()
            else:
                self._write(path.name, data)
            self._write("head.json", _bytes(dict(revision=revision)))
            return ProfileRevision(revision, expected_revision, profile)
