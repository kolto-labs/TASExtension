"""Durable proposal journal for schema-2 input drafts; no game execution.

The existing draft file remains the published document. Full intent is flushed
first so a failed/ambiguous publication retains the proposed inputs separately.
Recovery is offline and explicit; an intent never acknowledges executed inputs.
Cooperating writers use a nonblocking lock and an exact previous-byte revision.
The containing directory must be trusted; hostile ancestor replacement is not
covered. No timer, refresh callback or background writer is started here.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import stat
import uuid

from kptas.durability import DraftAutosave, atomic_json
from kptas.platform_sync import require_directory_barrier

_MAX_BYTES = 4 * 1024 * 1024


def draft_revision(data: bytes) -> str:
    """Exact source-file generation, including its original serialization."""
    return hashlib.sha256(data).hexdigest()


def _document(value):
    if (type(value) is not dict or set(value) != {'schema', 'identity', 'notes'}
            or value['schema'] != 'kptas-input-draft/2'
            or type(value['identity']) is not list or len(value['identity']) != 2
            or any(type(part) is not str for part in value['identity'])
            or type(value['notes']) is not dict):
        raise ValueError('invalid input draft document')
    return value


def _read(path):
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
                     | getattr(os, 'O_NONBLOCK', 0))
    except FileNotFoundError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError('input draft must be a regular file')
        with os.fdopen(fd, 'rb') as stream:
            fd = None
            data = stream.read(_MAX_BYTES + 1)
        if len(data) > _MAX_BYTES:
            raise ValueError('input draft exceeds recovery bound')
        return data
    finally:
        if fd is not None:
            os.close(fd)


class InputDraftStore:
    """One editor's acknowledged file generation plus retained proposal intents."""
    def __init__(self, path: Path, *, expected_revision: str | None):
        self.path = Path(path)
        self.expected_revision = expected_revision
        self._events = None
        self._source_preserved = False

    def publish(self, document: dict, *, source_identity: tuple[str, str]) -> str:
        require_directory_barrier()
        # Copy before any barriers: subsequent caller mutation cannot alter intent.
        data = json.dumps(document, sort_keys=True, separators=(',', ':'),
                          ensure_ascii=False, allow_nan=False).encode() + b'\n'
        if len(data) > _MAX_BYTES:
            raise ValueError('input draft exceeds recovery bound')
        copied = _document(json.loads(data))
        if (type(source_identity) is not tuple or len(source_identity) != 2
                or any(type(part) is not str for part in source_identity)):
            raise ValueError('draft proposal needs its source take/revision identity')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            import fcntl
        except ImportError:  # Windows: msvcrt byte-range lock on the lock file instead of flock
            fcntl = None
        fd = os.open(self.path.with_name(self.path.name + '.lock'),
                     os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0)
                     | getattr(os, 'O_NONBLOCK', 0), 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError('input draft lock must be a regular file')
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:
                import msvcrt
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            previous, read_error = None, None
            try:
                previous = _read(self.path)
            except (OSError, ValueError) as exc:
                read_error = exc
            revision = draft_revision(previous) if previous is not None else None
            if self._events is None:
                self._events = DraftAutosave(self.path.with_name(self.path.name + '.recovery'))
            # Preserve the first observed file exactly, including legacy drafts
            # that predate this journal. Later proposals already have full events.
            if not self._source_preserved and previous is not None:
                self._events.event('input-draft-source', {
                    'source_revision': revision,
                    'bytes_base64': base64.b64encode(previous).decode('ascii'),
                    'executed_state_saved': False,
                })
                self._source_preserved = True
            # Keep even stale proposals; they may contain work from another editor.
            # The source identity is a caller-supplied draft witness, not native proof.
            self._events.event('input-draft-proposed', {
                'request_id': uuid.uuid4().hex,
                'source_identity': list(source_identity),
                'source_revision': self.expected_revision,
                'observed_revision': revision,
                'observed_read_error': repr(read_error) if read_error is not None else None,
                'proposed_revision': draft_revision(data),
                'document': copied,
                'executed_state_saved': False,
                'publication_acknowledged': False,
            })
            if read_error is not None:
                raise read_error  # Original file remains untouched; proposal is retained.
            if previous is not None:
                _document(json.loads(previous))  # Refuse corrupt/schema-invalid originals.
            if revision != self.expected_revision:
                raise ValueError('input draft changed since this editor acknowledged it; proposal retained')
            # atomic_json retains failed temporaries. An exception after replacement
            # leaves expected_revision unchanged, forcing explicit reconciliation.
            atomic_json(self.path, copied)
            self.expected_revision = draft_revision(data)
            return self.expected_revision
        finally:
            os.close(fd)
