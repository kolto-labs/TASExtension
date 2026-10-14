"""Shared file-tree syntax/payload validation; stage ownership is checked separately."""
import base64
import hashlib
import json
import re
from kptas.types import FormatError

EMPTY_SHA = hashlib.sha256(b'').hexdigest()


def canonical_path(path, *, virtual=True):
    if not isinstance(path, str) or not path or '\\' in path or '\0' in path:
        raise FormatError('file tree path must be a canonical relative path')
    value = path
    # Virtual backends use e.g. SAVES:/file; these never become stage paths.
    if virtual and re.match(r'^[A-Z][A-Z0-9_]*:/', value):
        value = value.split(':/', 1)[1]
        if not value:
            return path
    value = value[:-1] if value.endswith('/') else value
    if not value or ':' in value or any(p in ('', '.', '..') for p in value.split('/')):
        raise FormatError(f'noncanonical file tree path: {path!r}')
    return path


def validate_rows(rows, blob=None):
    """Return canonical rows and frozen payloads, without changing any destination."""
    if not isinstance(rows, (list, tuple)):
        raise FormatError('file tree entries must be an array')
    result, paths, data = [], {}, {}
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) != 4:
            raise FormatError('file tree entry must have exactly four fields')
        path, digest, size, vtime = row
        canonical_path(path)
        if not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest):
            raise FormatError('file tree digest must be lowercase SHA256')
        if type(size) is not int or not 0 <= size <= 0xffffffff or type(vtime) is not int or not 0 <= vtime <= 0xffffffffffffffff:
            raise FormatError('file tree size/time outside unsigned format bounds')
        name = path.rstrip('/')
        if name.casefold() in {p.casefold() for p in paths}:
            raise FormatError(f'duplicate or conflicting file tree path: {path}')
        paths[name] = path.endswith('/')
        if path.endswith('/') and (digest != EMPTY_SHA or size != 0):
            raise FormatError(f'directory has a nonempty payload: {path}')
        if blob is not None:
            if digest not in data:
                raw = blob(digest)
                if not isinstance(raw, (bytes, bytearray, memoryview)):
                    raise FormatError('file payload is not bytes')
                data[digest] = bytes(raw)
            if len(data[digest]) != size or hashlib.sha256(data[digest]).hexdigest() != digest:
                raise FormatError(f'file payload does not match size/digest: {path}')
        result.append((path, digest, size, vtime))
    folded = {p.casefold(): directory for p, directory in paths.items()}
    for name in paths:
        parts = name.casefold().split('/')
        for i in range(1, len(parts)):
            if folded.get('/'.join(parts[:i])) is False:
                raise FormatError(f'file is an ancestor of another entry: {name}')
    return sorted(result), data


def decode_tree(raw):
    def unique(pairs):
        obj = {}
        for key, value in pairs:
            if key in obj:
                raise FormatError('duplicate file tree object field')
            obj[key] = value
        return obj
    try:
        obj = json.loads(raw, object_pairs_hook=unique)
    except (ValueError, UnicodeError) as exc:
        raise FormatError(f'invalid file tree JSON: {exc}') from exc
    if not isinstance(obj, dict) or set(obj) != {'entries'}:
        raise FormatError('file tree must contain only entries')
    return [list(row) for row in validate_rows(obj['entries'])[0]]


def decode_snapshot(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise FormatError('duplicate snapshot field')
            result[key] = value
        return result
    try:
        obj = json.loads(raw, object_pairs_hook=unique)
        if not isinstance(obj, dict) or set(obj) != {'entries'} or not isinstance(obj['entries'], list):
            raise FormatError('snapshot must contain only an entries array')
        rows, data = [], {}
        for row in obj['entries']:
            if not isinstance(row, list) or len(row) != 5 or not isinstance(row[4], str):
                raise FormatError('snapshot entry must have exactly five fields')
            payload = base64.b64decode(row[4], validate=True)
            valid, _ = validate_rows([row[:4]], lambda _h: payload)
            rows.extend(valid)
            data[row[1]] = payload
        return validate_rows(rows, data.__getitem__)
    except (ValueError, TypeError, UnicodeError) as exc:
        raise FormatError(f'invalid files snapshot: {exc}') from exc
