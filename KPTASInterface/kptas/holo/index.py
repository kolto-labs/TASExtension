"""Disposable index (contract 8.8): a private, versioned cache of object locations and the tick table.

Nothing lives only here. A missing, unreadable, other-version or stale index is rebuilt
from the journal and the packs.
"""

from __future__ import annotations

import json
import os
import struct
from pathlib import Path
from typing import Any, NamedTuple

from kptas.holo.pages import Loc

INDEX_VERSION = 1
INDEX_NAME = f"index-v{INDEX_VERSION}.bin"
_MAGIC = b"KPIX"
_HEAD = struct.Struct("<4sIIQQ")          # magic, version, state length, objects, ticks
_OBJ = struct.Struct("<32sBBBBHxxQQ")      # key (zero padded), key length, kind, area, anchored, depth, pack, offset
_TICK = struct.Struct("<QIIQ32s")         # tick, epoch, flags, journal offset, link


class TickRow(NamedTuple):
    tick: int
    epoch: int
    flags: int
    offset: int
    link: bytes


def save(root: str | os.PathLike[str], state: dict[str, Any], locs: dict[bytes, Loc], rows: list[TickRow]) -> None:
    d = Path(root) / "index"
    d.mkdir(exist_ok=True)
    blob = json.dumps(state, sort_keys=True, separators=(",", ":")).encode()
    parts = [_HEAD.pack(_MAGIC, INDEX_VERSION, len(blob), len(locs), len(rows)), blob]
    parts.extend(_OBJ.pack(k, len(k), l.kind, l.area, int(l.anchored), min(l.depth, 0xFFFF), l.pack, l.offset)
                 for k, l in locs.items())
    parts.extend(_TICK.pack(*r) for r in rows)
    tmp = d / f"{INDEX_NAME}.tmp{os.getpid()}"
    tmp.write_bytes(b"".join(parts))
    os.replace(tmp, d / INDEX_NAME)


def load(root: str | os.PathLike[str]) -> tuple[dict[str, Any], dict[bytes, Loc], list[TickRow]] | None:
    """Return (state, locations, tick rows), or None when the index is missing or unusable."""
    try:
        buf = (Path(root) / "index" / INDEX_NAME).read_bytes()
        magic, ver, slen, nobj, nticks = _HEAD.unpack_from(buf, 0)
        if magic != _MAGIC or ver != INDEX_VERSION:
            return None
        off = _HEAD.size
        if len(buf) != off + slen + nobj * _OBJ.size + nticks * _TICK.size:
            return None
        state = json.loads(buf[off:off + slen])
        off += slen
        locs: dict[bytes, Loc] = {}
        for key, klen, kind, area, anchored, depth, pack, o in _OBJ.iter_unpack(buf[off:off + nobj * _OBJ.size]):
            locs[key[:klen]] = Loc(area, pack, o, kind, bool(anchored), depth)
        off += nobj * _OBJ.size
        rows = [TickRow(*t) for t in _TICK.iter_unpack(buf[off:])]
        return state, locs, rows
    except (OSError, ValueError, struct.error):
        return None


def drop(root: str | os.PathLike[str]) -> None:
    try:
        os.unlink(Path(root) / "index" / INDEX_NAME)
    except FileNotFoundError:
        pass
