"""Disposable index (contract 8.8): a private, versioned cache of object locations and the tick table.

Nothing lives only here. A missing, unreadable, other-version or stale index is rebuilt
from the journal and the packs. Version 2 is written; version 1 is still read.
"""

from __future__ import annotations

import json
import os
import struct
import zlib
from pathlib import Path
from typing import Any, NamedTuple

from kptas.holo.pages import Loc
from kptas.types import ObjKind

INDEX_VERSION = 2
INDEX_NAME = f"index-v{INDEX_VERSION}.bin"
OLD_NAMES = ("index-v1.bin",)
_MAGIC = b"KPIX"
_HEAD = struct.Struct("<4sIIQQ")          # magic, version, state length, objects, ticks
_OBJ1 = struct.Struct("<32sBBBBHxxQQ")    # v1: key (zero padded), key length, kind, area, anchored, depth, pack, offset
_OBJ2 = struct.Struct("<BBBBHIQQ")        # v2: key length, kind, area, anchored, depth, slot + 1, pack, offset; key
_TICK = struct.Struct("<QIIQ32s")         # tick, epoch, flags, journal offset, link


class TickRow(NamedTuple):
    tick: int
    epoch: int
    flags: int
    offset: int
    link: bytes


def save(root: str | os.PathLike[str], state: dict[str, Any], locs: dict[bytes, Loc], rows: list[TickRow]) -> None:
    """Write index-v2.bin: header, state JSON, then the objects and tick rows as one zlib stream."""
    d = Path(root) / "index"
    d.mkdir(exist_ok=True)
    blob = json.dumps(state, sort_keys=True, separators=(",", ":")).encode()
    body = [_OBJ2.pack(len(k), l.kind, l.area, int(l.anchored), min(l.depth, 0xFFFF), l.slot + 1, l.pack, l.offset) + k
            for k, l in locs.items()]
    body.extend(_TICK.pack(*r) for r in rows)
    parts = [_HEAD.pack(_MAGIC, INDEX_VERSION, len(blob), len(locs), len(rows)), blob, zlib.compress(b"".join(body), 1)]
    tmp = d / f"{INDEX_NAME}.tmp{os.getpid()}"
    tmp.write_bytes(b"".join(parts))
    os.replace(tmp, d / INDEX_NAME)
    for name in OLD_NAMES:
        try:
            (d / name).unlink()
        except FileNotFoundError:
            pass


def load(root: str | os.PathLike[str]) -> tuple[dict[str, Any], dict[bytes, Loc], list[TickRow]] | None:
    """Return (state, locations, tick rows), or None when the index is missing or unusable."""
    d = Path(root) / "index"
    for name, reader in ((INDEX_NAME, _load_v2), (OLD_NAMES[0], _load_v1)):
        try:
            buf = (d / name).read_bytes()
        except OSError:
            continue
        try:
            return reader(buf)
        except (ValueError, KeyError, struct.error, zlib.error):
            return None
    return None


def _load_v2(buf: bytes) -> tuple[dict[str, Any], dict[bytes, Loc], list[TickRow]] | None:
    magic, ver, slen, nobj, nticks = _HEAD.unpack_from(buf, 0)
    if magic != _MAGIC or ver != 2:
        return None
    off = _HEAD.size
    state = json.loads(buf[off:off + slen])
    body = zlib.decompress(buf[off + slen:])
    locs: dict[bytes, Loc] = {}
    p = 0
    for _ in range(nobj):
        klen, kind, area, anchored, depth, slot, pack, o = _OBJ2.unpack_from(body, p)
        p += _OBJ2.size
        locs[body[p:p + klen]] = Loc(area, pack, o, kind, bool(anchored), depth, slot - 1)
        p += klen
    if len(body) - p != nticks * _TICK.size:
        return None
    rows = [TickRow(*t) for t in _TICK.iter_unpack(body[p:])]
    return state, locs, rows


def _load_v1(buf: bytes) -> tuple[dict[str, Any], dict[bytes, Loc], list[TickRow]] | None:
    """Version 1 marked only keyframe-pack pages as anchors; every intact page is one now."""
    magic, ver, slen, nobj, nticks = _HEAD.unpack_from(buf, 0)
    if magic != _MAGIC or ver != 1:
        return None
    off = _HEAD.size
    if len(buf) != off + slen + nobj * _OBJ1.size + nticks * _TICK.size:
        return None
    state = json.loads(buf[off:off + slen])
    off += slen
    locs: dict[bytes, Loc] = {}
    for key, klen, kind, area, anchored, depth, pack, o in _OBJ1.iter_unpack(buf[off:off + nobj * _OBJ1.size]):
        locs[key[:klen]] = Loc(area, pack, o, kind, bool(anchored) or kind == ObjKind.PAGE_I, depth)
    off += nobj * _OBJ1.size
    rows = [TickRow(*t) for t in _TICK.iter_unpack(buf[off:])]
    return state, locs, rows
