"""Small file-backed recent recording list; loading remains the owner's decision."""
from __future__ import annotations
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import tempfile
import stat
import uuid


@dataclass(frozen=True)
class RecordingEntry:
    path: str
    title: str
    current: bool = False


def _atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.'+path.name, delete=False) as stream:
            name = stream.name
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
        os.replace(name, path); name = None
        if os.name != 'nt':  # Windows cannot open or fsync a directory
            fd = os.open(path.parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
            try: os.fsync(fd)
            finally: os.close(fd)
    finally:
        if name is not None: Path(name).unlink(missing_ok=True)


def _read(path, limit):
    fd=os.open(path,os.O_RDONLY|getattr(os,'O_NONBLOCK',0))
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode): raise ValueError('recording metadata must be a regular file')
        with os.fdopen(fd,'rb') as stream:
            fd=None; data=stream.read(limit+1)
        if len(data)>limit: raise ValueError('recording metadata exceeds size limit')
        return data
    finally:
        if fd is not None: os.close(fd)


class RecordingLibrary:
    def __init__(self, path=None):
        self.path = Path(path).expanduser() if path else Path.home()/'.local/state/kptas/recordings.json'

    @staticmethod
    def _entry(path, current=False):
        root = Path(path).expanduser().resolve()
        if root.suffix != '.holo' or not root.is_dir() or not (root/'journal').is_file():
            raise ValueError('choose an existing .holo recording directory containing its journal')
        return RecordingEntry(str(root), root.name, current)

    def _recent(self):
        if not self.path.exists(): return []
        data = _read(self.path,128*1024)
        document = json.loads(data)
        if document.get('schema') != 'kptas-recording-library/1' or not isinstance(document.get('paths'), list):
            raise ValueError('unsupported recording library')
        if len(document['paths']) > 100 or any(not isinstance(p,str) or len(p)>4096 for p in document['paths']):
            raise ValueError('invalid recording library entries')
        return document['paths']

    def list(self, current_path=None):
        paths = ([str(current_path)] if current_path else []) + self._recent()
        entries, seen = [], set()
        current = str(Path(current_path).expanduser().resolve()) if current_path else None
        for path in paths:
            try: entry = self._entry(path)
            except (OSError, ValueError): continue
            if entry.path not in seen:
                entries.append(RecordingEntry(entry.path, entry.title, entry.path == current)); seen.add(entry.path)
        return tuple(entries)

    def add(self, path):
        entry = self._entry(path)
        paths = [entry.path] + [p for p in self._recent() if p != entry.path]
        _atomic(self.path, json.dumps({'schema':'kptas-recording-library/1','paths':paths[:100]}, indent=2).encode())
        return entry


def archive_draft(path, identity=None):
    """Preserve exact draft bytes before replacing its recording; never overwrite."""
    source = Path(path)
    if not source.exists(): return None
    data = _read(source,4*1024*1024)
    document = json.loads(data)
    if document.get('schema') != 'kptas-input-draft/2': raise ValueError('unsupported input draft')
    if identity is not None and tuple(document.get('identity',())) != tuple(identity):
        raise ValueError('draft identity changed before preservation')
    destination = source.with_name(source.stem+'.recovery-'+uuid.uuid4().hex+source.suffix)
    with destination.open('xb') as stream:
        stream.write(data); stream.flush(); os.fsync(stream.fileno())
    if hashlib.sha256(destination.read_bytes()).digest() != hashlib.sha256(data).digest():
        raise OSError('draft recovery copy failed verification')
    if os.name!='nt':  # Windows cannot open or fsync a directory
        fd=os.open(destination.parent,os.O_RDONLY|getattr(os,'O_DIRECTORY',0))
        try: os.fsync(fd)
        finally: os.close(fd)
    return destination
