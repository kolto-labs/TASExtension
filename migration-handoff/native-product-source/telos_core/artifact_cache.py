"""Bounded local artifact integrity cache; no authenticity or execution admission.

Requires a pre-existing trusted POSIX directory and filesystem supporting flock,
atomic same-directory replace and file/directory fsync. Cooperating writers must
use this cache. A hostile directory owner, ancestor replacement, and privileged
mutation are outside that boundary. Symlink/nonregular entries still refuse.
No URL is fetched; release identity and size are caller-supplied declarations.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import io
import os
from pathlib import Path
import stat
import tempfile
from typing import BinaryIO

from .profile import ReleaseRef
from .release_manifest import MAX_ARTIFACT_BYTES, ReleaseManifest

_CHUNK = 64 * 1024


@dataclass(frozen=True)
class ArtifactSpec:
    release: ReleaseRef
    size_bytes: int

    def __post_init__(self):
        if type(self.release) is not ReleaseRef:
            raise TypeError("release must be ReleaseRef")
        if type(self.size_bytes) is not int or not 1 <= self.size_bytes <= MAX_ARTIFACT_BYTES:
            raise ValueError("invalid declared artifact size")

    @classmethod
    def from_manifest(cls, manifest: ReleaseManifest) -> ArtifactSpec:
        if type(manifest) is not ReleaseManifest:
            raise TypeError("manifest must be ReleaseManifest")
        return cls(manifest.release, manifest.to_dict()["artifact"]["size_bytes"])


@dataclass(frozen=True)
class ArtifactVerification:
    """Point-in-time byte evidence, not a reusable admission token or safe path."""
    spec: ArtifactSpec
    sha256: str
    size_bytes: int


class ArtifactCache:
    """Digest-addressed bytes with bounded staging and verified consumer reads.

    Limits are per operation, not a total-cache quota. No garbage collection or
    reference catalog is provided. Identity/version mappings remain the caller's
    responsibility; equal digest/size declarations share bytes. read_verified
    returns the exact verified snapshot, avoiding a verify-then-reopen race.
    A put error after replace may already have published complete bytes. Retry
    verifies that entry and fsyncs its directory before returning success.
    """
    def __init__(self, directory: str | Path, *, max_artifact_bytes: int = 256 * 1024 * 1024,
                 max_read_bytes: int = 16 * 1024 * 1024):
        for limit in (max_artifact_bytes, max_read_bytes):
            if type(limit) is not int or not 1 <= limit <= MAX_ARTIFACT_BYTES:
                raise ValueError("invalid resource limit")
        self.directory = Path(directory).absolute()
        self.max_artifact_bytes = max_artifact_bytes
        self.max_read_bytes = max_read_bytes
        fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            self._identity = info.st_dev, info.st_ino
        finally:
            os.close(fd)

    def _check(self, spec):
        if type(spec) is not ArtifactSpec:
            raise TypeError("spec must be ArtifactSpec")
        if spec.size_bytes > self.max_artifact_bytes:
            raise ValueError("artifact exceeds local resource limit")

    @contextmanager
    def _locked(self):
        fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            if (info.st_dev, info.st_ino) != self._identity:
                raise ValueError("cache directory changed")
            lock_fd = os.open(".artifact-lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                              0o600, dir_fd=fd)
            try:
                self._regular(lock_fd)
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
                yield fd
            finally:
                os.close(lock_fd)
        finally:
            os.close(fd)

    @staticmethod
    def _regular(fd):
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("expected singly linked regular file")
        return info

    @staticmethod
    def _name(spec):
        return spec.release.digest + ".artifact"

    @staticmethod
    def _consume(stream, spec, sink=None):
        total = 0
        digest = hashlib.sha256()
        while True:
            request = min(_CHUNK, spec.size_bytes - total + 1)
            block = stream.read(request)
            if type(block) is not bytes or len(block) > request:
                raise ValueError("source must honor bounded binary reads")
            if not block:
                break
            total += len(block)
            if total > spec.size_bytes:
                raise ValueError("artifact size mismatch")
            digest.update(block)
            if sink is not None:
                sink.write(block)
        if total != spec.size_bytes:
            raise ValueError("artifact size mismatch")
        actual = digest.hexdigest()
        if actual != spec.release.digest:
            raise ValueError("artifact digest mismatch")
        return ArtifactVerification(spec, actual, total)

    def _existing(self, fd, spec, sink=None):
        artifact_fd = os.open(self._name(spec), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        with os.fdopen(artifact_fd, "rb") as stream:
            before = self._regular(stream.fileno())
            if before.st_size != spec.size_bytes:
                raise ValueError("artifact size mismatch")
            evidence = self._consume(stream, spec, sink)
            after = os.fstat(stream.fileno())
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise ValueError("artifact changed during verification")
            return evidence

    def put_stream(self, spec: ArtifactSpec, source: BinaryIO) -> ArtifactVerification:
        """Read from the stream's current position through EOF; caller owns it.

        Even on a cache hit the supplied stream must match the declaration.
        Streams must honor read(n); arbitrary blocking stream code has no timeout.
        """
        self._check(spec)
        with self._locked() as directory_fd:
            # Stage before publication. Temporary file shares the destination FS.
            temporary_fd, temporary = tempfile.mkstemp(prefix=".artifact-stage-", dir=self.directory)
            try:
                with os.fdopen(temporary_fd, "wb") as staged:
                    evidence = self._consume(source, spec, staged)
                    staged.flush()
                    os.fchmod(staged.fileno(), 0o444)
                    os.fsync(staged.fileno())
                try:
                    self._existing(directory_fd, spec)
                except FileNotFoundError:
                    os.replace(temporary, self._name(spec), dst_dir_fd=directory_fd)
                os.fsync(directory_fd)
                return evidence
            finally:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass

    def put_bytes(self, spec: ArtifactSpec, data: bytes) -> ArtifactVerification:
        self._check(spec)
        if type(data) is not bytes:
            raise TypeError("data must be bytes")
        if len(data) != spec.size_bytes:
            raise ValueError("artifact size mismatch")
        return self.put_stream(spec, io.BytesIO(data))

    def put_file(self, spec: ArtifactSpec, path: str | Path) -> ArtifactVerification:
        self._check(spec)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            if self._regular(fd).st_size != spec.size_bytes:
                raise ValueError("artifact size mismatch")
            return self.put_stream(spec, stream)

    def verify(self, spec: ArtifactSpec) -> ArtifactVerification:
        self._check(spec)
        with self._locked() as fd:
            return self._existing(fd, spec)

    def read_verified(self, spec: ArtifactSpec) -> bytes:
        """Return only bytes verified now, bounded by the separate read limit."""
        self._check(spec)
        if spec.size_bytes > self.max_read_bytes:
            raise ValueError("artifact exceeds in-memory read limit")
        with self._locked() as fd:
            snapshot = io.BytesIO()
            self._existing(fd, spec, snapshot)
            return snapshot.getvalue()
