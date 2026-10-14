"""Content-addressed object store (contract 8.6-8.8): packs, dedup, XOR deltas, batch frames, tree objects,
keyframe packs."""

from __future__ import annotations

import os
import struct
from collections import OrderedDict
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from typing import BinaryIO, NamedTuple

from kptas.types import (PAGE_KEY_LEN, PAGE_SIZE, ZERO_PAGE_KEY, Codec, FormatError, ObjKind, PageFlag,
                         PageRef, frame_batch, frame_object, page_key, read_batch, read_object, resolve_codec,
                         sha256, _object_shape)

AREA_PAGES, AREA_FILES, AREA_KF = 0, 1, 2
AREA_DIRS = {AREA_PAGES: "pages", AREA_FILES: "files", AREA_KF: "keyframes"}
PACK_LIMIT = 1 << 28
CODECS = {"none": Codec.NONE, "zlib": Codec.ZLIB, "zstd": Codec.ZSTD}
ZERO_PAGE = bytes(PAGE_SIZE)
PAGE_KINDS = (ObjKind.PAGE_I, ObjKind.PAGE_X)

_OBJ_HEAD = struct.Struct("<4sBBBBII")
_LEAF_ENTRY = struct.Struct("<H16sBxH")
_ROOT_ENTRY = struct.Struct("<H32s")
_U16 = struct.Struct("<H")


class MissingObject(KeyError):
    """No stored object has this key, here or in any parent bundle."""


class EvictedError(MissingObject):
    """This exact object was intentionally withheld or removed by budget policy."""


class Loc(NamedTuple):
    area: int
    pack: int          # pack number, or the tick of a keyframe pack
    offset: int        # of the frame; a batch frame holds many objects
    kind: int
    anchored: bool     # an intact page, or a PAGE_X whose XOR chain ends at a stored intact page
    depth: int         # PAGE_X objects between it and its anchor
    slot: int = -1     # member number inside a batch frame; -1 for a frame of its own


class Unit(NamedTuple):
    """One frame ready to append, and the objects it holds as (key, kind, anchored, depth)."""
    blob: bytes
    members: tuple[tuple[bytes, int, bool, int], ...]
    batch: bool = False


def _need(cond: bool, msg: str) -> None:
    if not cond:
        raise FormatError(msg)


def xor_pages(a: bytes, b: bytes) -> bytes:
    return (int.from_bytes(a, "little") ^ int.from_bytes(b, "little")).to_bytes(PAGE_SIZE, "little")


def pack_path(root: Path, area: int, pack: int) -> Path:
    if area == AREA_KF:
        return root / "keyframes" / f"kf-{pack:012d}.pack"
    return root / AREA_DIRS[area] / f"pack-{pack:06d}"


def _pack_number(area: int, name: str) -> int | None:
    if area == AREA_KF:
        if name.startswith("kf-") and name.endswith(".pack") and name[3:-5].isdigit():
            return int(name[3:-5])
    elif name.startswith("pack-") and name[5:].isdigit():
        return int(name[5:])
    return None


def list_packs(root: Path) -> dict[tuple[int, int], int]:
    """(area, pack) -> size in bytes, following spill symlinks."""
    out: dict[tuple[int, int], int] = {}
    for area, d in AREA_DIRS.items():
        p = root / d
        if not p.is_dir():
            continue
        with os.scandir(p) as entries:
            for e in entries:
                n = _pack_number(area, e.name)
                if n is None:
                    continue
                try:
                    out[(area, n)] = e.stat().st_size
                except FileNotFoundError:
                    out[(area, n)] = -1   # dangling spill link
    return out


Member = tuple[ObjKind, bytes, bytes | None, bytes | None, int]   # kind, key, base, raw, slot


def _frame_size(head: bytes) -> int | None:
    magic, kind, _codec, klen, _res, slen, _rlen = _OBJ_HEAD.unpack(head)
    if magic != b"KPOB" or not 1 <= kind <= 7:
        return None
    if kind == ObjKind.BATCH:
        if klen or _res or not 0 < _rlen <= (1 << 16) * PAGE_SIZE or _rlen % PAGE_SIZE:
            return None
        return _OBJ_HEAD.size + slen + 4
    try:
        _object_shape(ObjKind(kind), klen, _rlen)
    except FormatError:
        return None
    return _OBJ_HEAD.size + klen + (PAGE_KEY_LEN if kind == ObjKind.PAGE_X else 0) + slen + 4


def _unit_at(f: BinaryIO, pos: int, size: int, decode: bool, strict: bool = False) -> tuple[int, list[Member]] | None:
    """The frame at pos as (end, members), or None when no valid frame starts there.

    A frame of its own has one member with slot -1. A batch has one member per page, with
    slots from 0; its raw bytes are left as None unless decode.
    """
    f.seek(pos)
    head = f.read(_OBJ_HEAD.size)
    if len(head) < _OBJ_HEAD.size:
        return None
    total = _frame_size(head)
    if total is None or pos + total > size:
        return None
    buf = head + f.read(total - _OBJ_HEAD.size)
    try:
        if head[4] == ObjKind.BATCH:
            members, raw, _end = read_batch(buf, 0, decode)
            return pos + total, [(k, key, base, None if raw is None else raw[i * PAGE_SIZE:(i + 1) * PAGE_SIZE], i)
                                 for i, (k, key, base) in enumerate(members)]
        k, key, base, raw, _end = read_object(buf)
    except FormatError:
        if strict:
            raise
        return None
    return pos + total, [(k, key, base, raw, -1)]


def iter_units(f: BinaryIO, size: int, resync: bool = False, decode: bool = True, strict: bool = False,
               on_invalid: Callable[[int], None] | None = None
               ) -> Iterator[tuple[int, int, list[Member]]]:
    """Yield (offset, end, members) per valid frame.

    Without resync, stop at the first bad or torn frame. With resync, skip to the next
    KPOB whose frame checks out, so one bad frame does not hide later ones. Strict
    reads raise on corruption, missing decoders and incomplete frames instead.
    """
    pos = 0
    while pos < size:
        got = _unit_at(f, pos, size, decode, strict)
        if got is None:
            if on_invalid is not None:
                on_invalid(pos)
            if strict:
                raise FormatError(f"invalid or truncated store frame at {pos}")
            if not resync:
                return
            nxt = pos
            while got is None:
                f.seek(nxt + 1)
                chunk = f.read(max(0, size - nxt - 1))
                i = chunk.find(b"KPOB")
                if i < 0:
                    return
                nxt += 1 + i
                got = _unit_at(f, nxt, size, decode)
            pos = nxt
        end, members = got
        yield pos, end, members
        pos = end


# ---------------------------------------------------------------- state tree (8.6)

def leaf_payload(hi: int, refs: Iterable[PageRef]) -> bytes:
    parts = [b"KPLF", _U16.pack(hi)]
    parts.extend(_LEAF_ENTRY.pack((r.vaddr >> 12) & 0x3FF, r.key, int(r.mclass), r.prot) for r in refs)
    return b"".join(parts)


def parse_root(payload: bytes) -> list[tuple[int, bytes]]:
    _need(payload[:4] == b"KPRT" and (len(payload) - 4) % _ROOT_ENTRY.size == 0, "bad ROOT payload")
    return [_ROOT_ENTRY.unpack_from(payload, o) for o in range(4, len(payload), _ROOT_ENTRY.size)]


def parse_leaf(payload: bytes) -> list[PageRef]:
    _need(payload[:4] == b"KPLF" and (len(payload) - 6) % _LEAF_ENTRY.size == 0, "bad LEAF payload")
    hi = _U16.unpack_from(payload, 4)[0]
    out = []
    for o in range(6, len(payload), _LEAF_ENTRY.size):
        lo, key, mclass, prot = _LEAF_ENTRY.unpack_from(payload, o)
        out.append(PageRef(((hi << 10) | lo) << 12, key, mclass, prot))
    return out


class PageMap:
    """The complete page map of one tick, with per-leaf hash caching so a root costs only dirty leaves."""

    def __init__(self, refs: Iterable[PageRef] = ()) -> None:
        self.leaves: dict[int, dict[int, PageRef]] = {}
        self._payload: dict[int, tuple[bytes, bytes]] = {}   # hi -> (LEAF payload, its hash)
        self._layout: dict[int, tuple[dict[int, int], bytearray]] = {}
        self.apply(refs)

    def __len__(self) -> int:
        return sum(len(v) for v in self.leaves.values())

    def get(self, vaddr: int) -> PageRef | None:
        leaf = self.leaves.get(vaddr >> 22)
        return None if leaf is None else leaf.get(vaddr)

    def apply(self, refs: Iterable[PageRef]) -> list[tuple[int, PageRef | None]]:
        """Apply page refs (DECOMMIT removes). Returns an undo list of (vaddr, previous ref)."""
        undo = []
        for r in refs:
            hi = r.vaddr >> 22
            leaf = self.leaves.setdefault(hi, {})
            undo.append((r.vaddr, leaf.get(r.vaddr)))
            if r.flags & PageFlag.DECOMMIT:
                leaf.pop(r.vaddr, None)
                if not leaf:
                    del self.leaves[hi]
            else:
                leaf[r.vaddr] = r
            self._update_layout(hi, r.vaddr, None if r.flags & PageFlag.DECOMMIT else r)
        return undo

    def revert(self, undo: list[tuple[int, PageRef | None]]) -> None:
        for vaddr, old in reversed(undo):
            hi = vaddr >> 22
            leaf = self.leaves.setdefault(hi, {})
            if old is None:
                leaf.pop(vaddr, None)
                if not leaf:
                    del self.leaves[hi]
            else:
                leaf[vaddr] = old
            self._update_layout(hi, vaddr, old)

    def _update_layout(self, hi: int, vaddr: int, ref: PageRef | None) -> None:
        self._payload.pop(hi, None)
        layout = self._layout.get(hi)
        if layout is None:
            return
        offsets, payload = layout
        offset = offsets.get(vaddr)
        if ref is None or offset is None:
            # Membership/order changes rebuild the canonical leaf once.
            self._layout.pop(hi, None)
        else:
            _LEAF_ENTRY.pack_into(payload, offset, (ref.vaddr >> 12) & 0x3FF,
                                  ref.key, int(ref.mclass), ref.prot)

    def refs(self) -> Iterator[PageRef]:
        for hi in sorted(self.leaves):
            leaf = self.leaves[hi]
            for v in sorted(leaf):
                yield leaf[v]

    def keys(self) -> set[bytes]:
        return {r.key for leaf in self.leaves.values() for r in leaf.values()}

    def _leaf(self, hi: int) -> tuple[bytes, bytes]:
        c = self._payload.get(hi)
        if c is None:
            leaf = self.leaves[hi]
            layout = self._layout.get(hi)
            if layout is None:
                addresses = sorted(leaf)
                payload = bytearray(leaf_payload(hi, (leaf[v] for v in addresses)))
                offsets = {v: 6 + i * _LEAF_ENTRY.size for i, v in enumerate(addresses)}
                layout = self._layout[hi] = (offsets, payload)
            p = bytes(layout[1])
            c = self._payload[hi] = (p, sha256(p))
        return c

    def tree(self) -> tuple[bytes, bytes, list[tuple[bytes, bytes]]]:
        """Return (root key, ROOT payload, [(leaf key, LEAF payload)]); root key is the state root."""
        his = sorted(self.leaves)
        cached = [self._leaf(hi) for hi in his]
        root = b"KPRT" + b"".join(_ROOT_ENTRY.pack(hi, h) for hi, (_p, h) in zip(his, cached))
        return sha256(root), root, [(h, p) for p, h in cached]

    def root(self) -> bytes:
        return self.tree()[0]


# ---------------------------------------------------------------- store

class _HotTier:
    """Process-wide LRU of verified object bytes, keyed by content key (seek.hot_tier_mb).

    Store caches belong to one Store object, and a rewind closes and reopens the whole bundle
    chain, so each rewind used to start cold and read pages, file blobs, GL blobs and tree
    objects from disk again. This tier outlives a reopen. Only bytes that Store.get has just
    checked against their own content key enter it, and Store.get consults it only after the
    store has located the key. So a hit returns the same bytes a disk read would, and
    missing or evicted objects still raise. Off (0 bytes) until a seek applies its config.
    """

    def __init__(self) -> None:
        import threading
        self._lock = threading.Lock()
        self._data: OrderedDict[bytes, bytes] = OrderedDict()
        self._bytes = 0
        self.budget = 0
        self.hits = self.misses = 0

    def configure(self, budget_bytes: int) -> None:
        with self._lock:
            self.budget = max(0, int(budget_bytes))
            self._trim()

    def get(self, key: bytes) -> bytes | None:
        if not self.budget:
            return None
        with self._lock:
            raw = self._data.get(key)
            if raw is None:
                self.misses += 1
                return None
            self._data.move_to_end(key)
            self.hits += 1
            return raw

    def put(self, key: bytes, raw: bytes) -> None:
        """raw must already be verified against key."""
        if not self.budget or len(raw) > self.budget:
            return
        with self._lock:
            if key in self._data:
                self._data.move_to_end(key)
                return
            self._data[key] = raw
            self._bytes += len(raw)
            self._trim()

    def _trim(self) -> None:
        while self._bytes > self.budget and self._data:
            _k, v = self._data.popitem(last=False)
            self._bytes -= len(v)

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {"budget": self.budget, "bytes": self._bytes, "objects": len(self._data),
                    "hits": self.hits, "misses": self.misses}


HOT = _HotTier()


class Store:
    """Content-addressed frames under pages/, files/ and keyframes/, with a parent fallback."""

    def __init__(self, root: str | os.PathLike[str], *, codec: str = "zlib", level: int = 1,
                 max_chain: int = 32, xor_delta: bool = True, batch_pages: int = 64,
                 cache_bytes: int = 64 << 20, parent: Store | None = None, writable: bool = False) -> None:
        self.root = Path(root)
        self.codec = CODECS[resolve_codec(codec)]
        self.level = level
        self.max_chain = max_chain
        self.xor_delta = xor_delta
        self.batch_pages = batch_pages
        self.parent = parent
        self.writable = writable
        self.locs: dict[bytes, Loc] = {}
        self.valid: dict[tuple[int, int], int] = {}
        self.evictions = False
        self.evicted_keys: set[bytes] = set()
        self.generation = 0     # monotonic; bumped whenever object locations or availability may change
        self.corrupt_packs: set[tuple[int, int]] = set()
        self.note_availability: Callable[[set[bytes], bool], None] | None = None
        self._read: dict[tuple[int, int], BinaryIO] = {}
        self._cur: dict[int, tuple[int, BinaryIO]] = {}
        self._dirty: set[int] = set()
        self._cache: OrderedDict[bytes, bytes] = OrderedDict()
        self._cache_bytes = cache_bytes
        self._cached = 0
        self._batches: OrderedDict[tuple[int, int, int], tuple[list, bytes]] = OrderedDict()
        self._batch_bytes = max(8 << 20, cache_bytes // 8)
        self._batched = 0
        self._page_encoder = None
        self.encode_profile: dict | None = None   # set by a profiling caller; counters only

    def availability(self, keys: set[bytes], missing: bool) -> None:
        """Persist exact removals; revoke absence intent before reintroducing bytes."""
        self.generation += 1
        keys = keys - self.evicted_keys if missing else keys & self.evicted_keys
        if not keys:
            return
        if self.note_availability is not None:
            self.note_availability(keys, missing)
        if missing:
            self.evicted_keys.update(keys)
        else:
            self.evicted_keys.difference_update(keys)

    # -- index state

    def rebuild(self, kf_ticks: set[int] | None = None) -> None:
        """Scan every pack and rebuild the object locations; record where each pack's last valid frame ends.

        Keyframe packs are indexed only for ticks in kf_ticks (committed MATERIALIZED ticks) when given.
        """
        self.generation += 1
        self.close_handles()
        self.locs.clear()
        self.valid.clear()
        self.corrupt_packs.clear()
        bases: dict[bytes, bytes] = {}
        for (area, pack), size in sorted(list_packs(self.root).items()):
            if size < 0 or (area == AREA_KF and kf_ticks is not None and pack not in kf_ticks):
                continue
            with open(pack_path(self.root, area, pack), "rb") as f:
                end = 0
                for off, end, members in iter_units(f, size, resync=True, decode=False,
                        on_invalid=lambda _off: self.corrupt_packs.add((area, pack))):
                    for kind, key, base, _raw, slot in members:
                        old = self.locs.get(key)
                        if old is not None and (old.area == AREA_KF or area != AREA_KF):
                            continue  # keep the first copy, but prefer a keyframe-pack copy
                        bases.pop(key, None)
                        self.locs[key] = Loc(area, pack, off, int(kind), kind == ObjKind.PAGE_I, 0, slot)
                        if base is not None:
                            bases[key] = base
                self.valid[(area, pack)] = end
        self._resolve_chains(bases)

    def _resolve_chains(self, bases: dict[bytes, bytes]) -> None:
        """Set anchored/depth on every PAGE_X by walking its bases to the first intact page."""
        memo: dict[bytes, tuple[bool, int]] = {}

        def known(k: bytes) -> tuple[bool, int] | None:
            if k in memo:
                return memo[k]
            loc = self.locs.get(k)
            if loc is None:
                return (self.parent.info(k) if self.parent else None) or (False, 1 << 16)
            return None if loc.kind == ObjKind.PAGE_X else (loc.anchored, 0)

        for key in bases:
            loc = self.locs.get(key)
            if key in memo or loc is None or loc.kind != ObjKind.PAGE_X:
                continue
            stack = [key]
            while (inf := known(bases[stack[-1]])) is None:
                stack.append(bases[stack[-1]])
                _need(len(stack) <= 1 << 16, "XOR chain does not terminate")
            for k in reversed(stack):
                inf = (inf[0], inf[1] + 1)
                memo[k] = inf
                self.locs[k] = self.locs[k]._replace(anchored=inf[0], depth=inf[1])

    def info(self, key: bytes) -> tuple[bool, int] | None:
        """(anchored, depth) of a stored page, or None."""
        loc = self.locs.get(key)
        if loc is not None:
            return loc.anchored, loc.depth
        return self.parent.info(key) if self.parent else None

    def has(self, key: bytes) -> bool:
        return key == ZERO_PAGE_KEY or key in self.locs or (self.parent is not None and self.parent.has(key))

    def local_bytes(self) -> int:
        """Fresh local pack sizes; use directory-entry metadata, never a stale size cache."""
        sizes: dict[tuple[int, int], int] = {}
        for area, directory in AREA_DIRS.items():
            path = self.root / directory
            if not path.is_dir():
                continue
            with os.scandir(path) as entries:
                for entry in entries:
                    pack = _pack_number(area, entry.name)
                    if pack is None:
                        continue
                    canonical = (f"kf-{pack:012d}.pack" if area == AREA_KF else f"pack-{pack:06d}")
                    # Preserve list_packs' number-keyed handling of legacy names.
                    # Normal packs need no separate Path construction or lstat.
                    spilled = (entry.is_symlink() if entry.name == canonical else
                               pack_path(self.root, area, pack).is_symlink())
                    try:
                        sizes[(area, pack)] = 0 if spilled else entry.stat().st_size
                    except FileNotFoundError:
                        sizes[(area, pack)] = 0
        return sum(sizes.values())

    def current_spilled(self, area: int) -> bool:
        cur = self._current(area, open_it=False)
        return cur is not None and pack_path(self.root, area, cur).is_symlink()

    # -- reading

    def _reader(self, area: int, pack: int) -> BinaryIO:
        cur = self._cur.get(area)
        if cur is not None and cur[0] == pack:
            cur[1].flush()
        f = self._read.get((area, pack))
        if f is None:
            f = self._read[(area, pack)] = open(pack_path(self.root, area, pack), "rb")
        return f

    def _frame_bytes(self, area: int, pack: int, offset: int) -> bytes:
        f = self._reader(area, pack)
        f.seek(offset)
        head = f.read(_OBJ_HEAD.size)
        _need(len(head) == _OBJ_HEAD.size, f"no frame at {AREA_DIRS[area]}/{pack}@{offset}")
        total = _frame_size(head)
        _need(total is not None, f"no frame at {AREA_DIRS[area]}/{pack}@{offset}")
        return head + f.read(total - _OBJ_HEAD.size)  # type: ignore[operator]

    def _batch(self, loc: Loc) -> tuple[list[tuple[ObjKind, bytes, bytes | None]], bytes]:
        """A batch frame's member table and decoded bytes, through a small cache of decoded batches."""
        at = (loc.area, loc.pack, loc.offset)
        hit = self._batches.get(at)
        if hit is not None:
            self._batches.move_to_end(at)
            return hit
        members, raw, _end = read_batch(self._frame_bytes(*at))
        assert raw is not None
        self._batches[at] = (members, raw)
        self._batched += len(raw)
        while self._batched > self._batch_bytes and len(self._batches) > 1:
            _k, (_m, r) = self._batches.popitem(last=False)
            self._batched -= len(r)
        return members, raw

    def read_frame(self, loc: Loc) -> tuple[ObjKind, bytes, bytes | None, bytes]:
        """(kind, key, base, stored bytes) of one object; for PAGE_X the bytes are the XOR against its base."""
        if loc.slot >= 0:
            members, raw = self._batch(loc)
            _need(loc.slot < len(members), f"no member {loc.slot} in the batch at {loc}")
            kind, key, base = members[loc.slot]
            return kind, key, base, raw[loc.slot * PAGE_SIZE:(loc.slot + 1) * PAGE_SIZE]
        kind, key, base, raw, _ = read_object(self._frame_bytes(loc.area, loc.pack, loc.offset))
        return kind, key, base, raw

    def get(self, key: bytes) -> bytes:
        """Decoded content of an object, verified against its key (BLAKE2s-128 or sha256)."""
        if key == ZERO_PAGE_KEY:
            return ZERO_PAGE
        hit = self._cache.get(key)
        if hit is not None:
            self._cache.move_to_end(key)
            return hit
        loc = self.locs.get(key)
        if loc is None:
            if self.parent is not None and self.parent.has(key):
                return self.parent.get(key)
            _need(not self.corrupt_packs, f"object {key.hex()} missing after corrupt pack scan: {sorted(self.corrupt_packs)}")
            if key in self.evicted_keys:
                raise EvictedError(f"{key.hex()} was intentionally removed or withheld under the store budget")
            raise MissingObject(key.hex())
        hot = HOT.get(key)       # located here: a hot hit skips the disk read, decode and re-hash
        if hot is not None:
            self._remember(key, hot)
            return hot
        kind, k, base, raw = self.read_frame(loc)
        _need(k == key, f"index points {key.hex()} at an object keyed {k.hex()}")
        if kind == ObjKind.PAGE_X:
            raw = xor_pages(raw, self.get(base))  # type: ignore[arg-type]
        ok = page_key(raw) == key if len(key) == PAGE_KEY_LEN else sha256(raw) == key
        _need(ok, f"object {key.hex()} does not match its content key")
        self._remember(key, raw)
        HOT.put(key, raw)
        return raw

    def _remember(self, key: bytes, raw: bytes) -> None:
        if len(raw) > self._cache_bytes or key in self._cache:
            return
        self._cache[key] = raw
        self._cached += len(raw)
        while self._cached > self._cache_bytes:
            _k, v = self._cache.popitem(last=False)
            self._cached -= len(v)

    # -- writing

    def _base_for(self, key: bytes, bases: Iterable[bytes | None],
                  durable: Callable[[bytes], bool] | None) -> tuple[bytes, int] | None:
        """The first eligible base and its depth: stored, its chain anchored at an intact page and
        shorter than max_chain, so every XOR chain obeys 8.7, and accepted by `durable`."""
        for b in bases:
            if b is None or b == key or b == ZERO_PAGE_KEY:
                continue
            inf = self.info(b)
            if inf is None or not inf[0] or inf[1] >= self.max_chain or (durable and not durable(b)):
                continue
            return b, inf[1]
        return None

    def encode_pages(self, items: Iterable[tuple[bytes, bytes, Iterable[bytes | None]]],
                     durable: Callable[[bytes], bool] | None = None) -> list[Unit]:
        """Frame pages given as (key, page bytes, candidate base keys), in order.

        Each page is stored intact, or as its XOR against the first eligible base. With batch_pages
        above 1 the pages go into batch frames of at most that many, and the XOR is taken when it
        has more zero bytes than the page. With batch_pages 1 every page is a frame of its own and
        the XOR is taken when its frame is smaller. Only immutable batch compression runs on
        workers, at most two frames and 32 MiB of source at once; admission and base reads stay
        serial, and no Unit reaches the caller until every frame succeeds.

        Batches are submitted as soon as they are prepared, so compression overlaps the serial
        preparation of the next batch; results are collected strictly in submission order.
        Frames are identical to encoding them inline.
        """
        from collections import deque
        units: list[Unit] = []
        batch: list[tuple[ObjKind, bytes, bytes | None, bytes]] = []
        meta: list[tuple[bytes, int, bool, int]] = []
        held: list[tuple[tuple, int]] = []      # prepared before the pool is warranted
        held_bytes = 0
        inflight: deque = deque()               # (future, source bytes), submission order
        inflight_bytes = 0
        pool = None
        prof = getattr(self, "encode_profile", None)
        if prof is not None:
            import time
            prof["pages"] = prof["jobs"] = prof["base_fetches"] = prof["base_misses"] = 0
            prof["wait_ns"] = prof["inline_ns"] = 0
            prof["pooled"] = False
            started = time.perf_counter_ns()

        def encode(job):
            entries, members, codec, level = job
            return Unit(frame_batch(entries, codec, level), members, True)

        def inline(job):
            if prof is None:
                return encode(job)
            stamp = time.perf_counter_ns()
            try:
                return encode(job)
            finally:
                prof["inline_ns"] += time.perf_counter_ns() - stamp

        def collect(max_jobs, max_bytes):
            # Resolve the oldest submissions, in order, until the bounds hold.
            nonlocal inflight_bytes
            while inflight and (len(inflight) > max_jobs or inflight_bytes > max_bytes):
                fut, size = inflight.popleft()
                inflight_bytes -= size
                stamp = time.perf_counter_ns() if prof is not None else 0
                try:
                    unit = fut.result()
                except BaseException:
                    # The first failure in submission order is the refusal; the
                    # rest of the group is abandoned, never resubmitted or returned.
                    self._page_encoder = None
                    pool.shutdown(wait=True)
                    inflight.clear()
                    inflight_bytes = 0
                    raise
                finally:
                    if prof is not None:
                        prof["wait_ns"] += time.perf_counter_ns() - stamp
                units.append(unit)

        def dispatch(job, size):
            nonlocal inflight_bytes
            # Workers only see immutable prepared frames; at most two frames and
            # 32 MiB of source are outstanding, counting this one.
            collect(1, 32 * 1024 * 1024 - size)
            inflight.append((pool.submit(encode, job), size))
            inflight_bytes += size

        def flush_jobs():
            # Every frame prepared so far reaches `units`, in order.
            nonlocal held, held_bytes
            collect(0, -1)
            ready, held, held_bytes = held, [], 0
            units.extend(inline(job) for job, _size in ready)

        def submit(job, size):
            nonlocal pool, held, held_bytes
            if prof is not None:
                prof["jobs"] += 1
            if pool is not None:
                dispatch(job, size)
                return
            held.append((job, size))
            held_bytes += size
            if len(held) < 2 or held_bytes < 256 * 1024 or job[2] is Codec.NONE:
                return
            from concurrent.futures import ThreadPoolExecutor
            if self._page_encoder is None:
                self._page_encoder = ThreadPoolExecutor(max_workers=2, thread_name_prefix="holo-pages")
            pool = self._page_encoder
            if prof is not None:
                prof["pooled"] = True
            ready, held, held_bytes = held, [], 0
            for j, n in ready:
                dispatch(j, n)

        def queue_batch():
            nonlocal batch, meta
            entries, members = tuple(batch), tuple(meta)
            batch, meta = [], []
            codec, level = self.codec, self.level
            size = sum(len(entry[3]) for entry in entries)
            immutable = (type(level) is int and type(codec) is Codec
                         and all(type(entry[1]) is bytes and type(entry[3]) is bytes
                                 and (entry[2] is None or type(entry[2]) is bytes)
                                 for entry in entries))
            if not immutable or size > 32 * 1024 * 1024:
                flush_jobs()
                units.append(inline((entries, members, codec, level)))
                return
            submit((entries, members, codec, level), size)

        def base(key: bytes) -> bytes:
            if prof is not None:
                prof["base_fetches"] += 1
                if key not in self._cache:
                    prof["base_misses"] += 1
            return self.get(key)

        def prepare():
            nonlocal batch, meta
            for key, data, bases in items:
                if prof is not None:
                    prof["pages"] += 1
                self._remember(key, data)
                got = self._base_for(key, bases, durable) if self.xor_delta else None
                if self.batch_pages <= 1:
                    flush_jobs()
                    frame = frame_object(ObjKind.PAGE_I, key, data, self.codec, self.level)
                    mem = (key, int(ObjKind.PAGE_I), True, 0)
                    if got is not None:
                        x = frame_object(ObjKind.PAGE_X, key, xor_pages(data, base(got[0])), self.codec,
                                         self.level, base=got[0])
                        if len(x) < len(frame):
                            frame, mem = x, (key, int(ObjKind.PAGE_X), True, got[1] + 1)
                    units.append(Unit(frame, (mem,)))
                    continue
                entry = (ObjKind.PAGE_I, key, None, data)
                mem = (key, int(ObjKind.PAGE_I), True, 0)
                if got is not None:
                    x = xor_pages(data, base(got[0]))
                    if x.count(0) > data.count(0):
                        entry, mem = (ObjKind.PAGE_X, key, got[0], x), (key, int(ObjKind.PAGE_X), True, got[1] + 1)
                batch.append(entry)
                meta.append(mem)
                if len(batch) >= self.batch_pages:
                    queue_batch()
            if batch:
                queue_batch()
        try:
            try:
                prepare()
            except BaseException:
                # Preserve earlier-frame refusal precedence if preparation of a
                # later page fails. No frame is appended by this method.
                flush_jobs()
                raise
            flush_jobs()
        finally:
            if prof is not None:
                prof["total_ns"] = time.perf_counter_ns() - started
        return units

    def unit(self, kind: ObjKind, key: bytes, raw: bytes) -> Unit:
        """A frame of its own for one object (tree objects, file blobs and trees)."""
        member = (key, int(kind), kind == ObjKind.PAGE_I, 0)
        return Unit(frame_object(kind, key, raw, self.codec, self.level), (member,))

    def _current(self, area: int, open_it: bool = True) -> int | None:
        cur = self._cur.get(area)
        if cur is not None:
            return cur[0]
        packs = [p for (a, p) in list_packs(self.root) if a == area]
        if not packs:
            return self.roll(area) if open_it else None
        n = max(packs)
        if open_it:
            f = open(pack_path(self.root, area, n), "r+b")
            f.seek(0, os.SEEK_END)
            self._cur[area] = (n, f)
        return n

    def roll(self, area: int, target_dir: str | os.PathLike[str] | None = None) -> int:
        """Start a new pack in `area`; with target_dir the pack lives there behind a symlink (spill)."""
        _need(area != AREA_KF, "keyframe packs are not rolled")
        old = self._cur.pop(area, None)
        if old is not None:
            old[1].flush()
            if area in self._dirty:
                os.fsync(old[1].fileno())
            old[1].close()
        packs = [p for (a, p) in list_packs(self.root) if a == area]
        n = max(packs) + 1 if packs else 0
        path = pack_path(self.root, area, n)
        path.parent.mkdir(parents=True, exist_ok=True)
        if target_dir is not None:
            real = Path(target_dir) / path.name
            real.parent.mkdir(parents=True, exist_ok=True)
            open(real, "xb").close()
            os.symlink(os.path.abspath(real), path)
        f = open(path, "x+b" if target_dir is None else "r+b")
        self._cur[area] = (n, f)
        self.valid[(area, n)] = 0
        return n

    def append(self, area: int, units: Iterable[Unit]) -> int:
        """Append frames, skipping a frame whose objects are all stored already. Returns bytes written."""
        written = 0
        for u in units:
            if all(m[0] in self.locs for m in u.members):
                continue
            self.availability({m[0] for m in u.members}, False)
            n = self._current(area)
            f = self._cur[area][1]
            off = f.tell()
            if off and off + len(u.blob) > PACK_LIMIT:
                n = self.roll(area)
                f = self._cur[area][1]
                off = 0
            f.write(u.blob)
            for slot, (key, kind, anchored, depth) in enumerate(u.members):
                if key not in self.locs:
                    self.locs[key] = Loc(area, n, off, kind, anchored, depth,  # type: ignore[arg-type]
                                         slot if u.batch else -1)
            self.valid[(area, n)] = off + len(u.blob)  # type: ignore[index]
            written += len(u.blob)
            self._dirty.add(area)
        cur = self._cur.get(area)
        if cur is not None:
            cur[1].flush()
        return written

    def fsync(self) -> None:
        for area in sorted(self._dirty):
            cur = self._cur.get(area)
            if cur is not None:
                cur[1].flush()
                os.fsync(cur[1].fileno())
        self._dirty.clear()

    def truncate_tails(self) -> list[tuple[int, int, int]]:
        """Writer only: cut each pack to its valid prefix. Returns (area, pack, bytes cut) per cut."""
        cuts = []
        for (area, pack), size in list_packs(self.root).items():
            v = self.valid.get((area, pack), 0)
            if size > v and area != AREA_KF:
                with open(pack_path(self.root, area, pack), "r+b") as f:
                    f.truncate(v)
                    os.fsync(f.fileno())
                cuts.append((area, pack, size - v))
        return cuts

    # -- keyframe packs (8.8)

    def kf_bytes(self, pmap: PageMap, payload) -> bytes:  # payload: key -> page bytes
        """A materialized pack: ROOT, the LEAFs, then every non-zero page intact in ascending vaddr,
        in batch frames when batch_pages is above 1."""
        root, rootp, leaves = pmap.tree()
        parts = [self.frame(ObjKind.ROOT, root, rootp)]
        parts.extend(self.frame(ObjKind.LEAF, k, p) for k, p in leaves)
        batch: list[tuple[ObjKind, bytes, bytes | None, bytes]] = []
        for r in pmap.refs():
            if r.key == ZERO_PAGE_KEY:
                continue
            data = payload(r.key)
            self._remember(r.key, data)
            if self.batch_pages <= 1:
                parts.append(frame_object(ObjKind.PAGE_I, r.key, data, self.codec, self.level))
                continue
            batch.append((ObjKind.PAGE_I, r.key, None, data))
            if len(batch) >= self.batch_pages:
                parts.append(frame_batch(batch, self.codec, self.level))
                batch = []
        if batch:
            parts.append(frame_batch(batch, self.codec, self.level))
        return b"".join(parts)

    def frame(self, kind: ObjKind, key: bytes, raw: bytes) -> bytes:
        return frame_object(kind, key, raw, self.codec, self.level)

    def write_kf(self, tick: int, blob: bytes, fsync: bool) -> None:
        if self.evicted_keys:
            import io
            keys = {key for _off, _end, members in iter_units(io.BytesIO(blob), len(blob), strict=True)
                    for _kind, key, _base, _raw, _slot in members}
            self.availability(keys, False)
        path = pack_path(self.root, AREA_KF, tick)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        with open(tmp, "wb") as f:
            f.write(blob)
            if fsync:
                f.flush()
                os.fsync(f.fileno())
        os.replace(tmp, path)
        if fsync:
            fsync_dir(path.parent)
        stale = self._read.pop((AREA_KF, tick), None)
        if stale is not None:
            stale.close()
        with open(path, "rb") as f:
            end = 0
            for off, end, members in iter_units(f, len(blob), decode=False):
                for kind, key, _b, _raw, slot in members:
                    old = self.locs.get(key)
                    if old is None or (kind == ObjKind.PAGE_I and old.area != AREA_KF):
                        self.locs[key] = Loc(AREA_KF, tick, off, int(kind), kind == ObjKind.PAGE_I, 0, slot)
            _need(end == len(blob), "keyframe pack did not read back whole")
        self.valid[(AREA_KF, tick)] = len(blob)

    def read_kf(self, tick: int) -> tuple[bytes, list[PageRef], dict[bytes, bytes]]:
        """Read a materialized pack sequentially: (state root, page refs, key -> page bytes)."""
        path = pack_path(self.root, AREA_KF, tick)
        with open(path, "rb") as f:
            objs = [(kind, key, raw) for _o, _e, members in iter_units(f, os.fstat(f.fileno()).st_size, strict=True)
                    for kind, key, _b, raw, _s in members]
        _need(objs and objs[0][0] == ObjKind.ROOT, f"{path.name} does not start with ROOT")
        root = objs[0][1]
        leaf_keys = [k for _hi, k in parse_root(objs[0][2])]  # type: ignore[arg-type]
        _need(len(objs) > len(leaf_keys), f"{path.name}: {len(objs) - 1} objects for {len(leaf_keys)} LEAFs")
        refs: list[PageRef] = []
        for i, k in enumerate(leaf_keys, 1):
            _need(objs[i][0] == ObjKind.LEAF and objs[i][1] == k, f"{path.name}: LEAF {i} out of order")
            refs.extend(parse_leaf(objs[i][2]))  # type: ignore[arg-type]
        pages = objs[1 + len(leaf_keys):]
        nonzero = [r for r in refs if r.key != ZERO_PAGE_KEY]
        _need(len(pages) == len(nonzero), f"{path.name}: {len(pages)} pages for {len(nonzero)} non-zero refs")
        data: dict[bytes, bytes] = {}
        for r, (kind, key, raw) in zip(nonzero, pages):
            _need(kind == ObjKind.PAGE_I and key == r.key and page_key(raw) == key,  # type: ignore[arg-type]
                  f"{path.name}: page {r.vaddr:#x} does not match its ref")
            data[key] = raw  # type: ignore[assignment]
        return root, refs, data

    # -- eviction

    def bases(self) -> dict[bytes, bytes]:
        """key -> base key of every PAGE_X object stored in this bundle."""
        out: dict[bytes, bytes] = {}
        tables: dict[tuple[int, int, int], list] = {}
        for k, l in self.locs.items():
            if l.kind != ObjKind.PAGE_X:
                continue
            if l.slot < 0:
                out[k] = self.read_frame(l)[2]  # type: ignore[assignment]
                continue
            at = (l.area, l.pack, l.offset)
            if at not in tables:
                tables[at] = read_batch(self._frame_bytes(*at), 0, decode=False)[0]
            out[k] = tables[at][l.slot][2]
        return out

    def compact(self, drop: set[bytes], packs: Iterable[tuple[int, int]],
                base_of: dict[bytes, bytes] | None = None) -> tuple[int, int]:
        """Rewrite the given packs without `drop` keys and unindexed objects.

        A kept PAGE_X whose base is dropped is stored intact instead, so every remaining chain
        still ends at an intact page. Each pack is rewritten as tmp, fsynced, then replaced.
        Returns (bytes freed, objects dropped).
        """
        self.generation += 1
        packs = sorted(set(packs))
        inpack = set(packs)
        if base_of is None:
            base_of = self.bases()
        redo: dict[bytes, bytes] = {}
        for key, loc in self.locs.items():
            if key not in drop and loc.kind == ObjKind.PAGE_X and (loc.area, loc.pack) in inpack \
                    and base_of.get(key) in drop:
                redo[key] = self.get(key)
        self.close_handles()
        self._batches.clear()
        self._batched = 0
        freed = dropped = 0
        for area, pack in packs:
            path = pack_path(self.root, area, pack)
            real = Path(os.path.realpath(path))
            size = real.stat().st_size
            tmp = real.with_name(real.name + ".tmp")
            newlocs: dict[bytes, Loc] = {}
            with open(real, "rb") as f, open(tmp, "wb") as out:
                for off, end, members in iter_units(f, size, strict=True):
                    here = [m for m in members if (l := self.locs.get(m[1])) is not None
                            and (l.area, l.pack, l.offset, l.slot) == (area, pack, off, m[4])]
                    keep = [m for m in here if m[1] not in drop]
                    dropped += len(here) - len(keep)
                    if not keep:
                        continue
                    pos = out.tell()
                    if members[0][4] < 0 and not any(m[1] in redo for m in keep):
                        f.seek(off)
                        out.write(f.read(end - off))
                        f.seek(end)
                        newlocs[keep[0][1]] = self.locs[keep[0][1]]._replace(offset=pos)
                        continue
                    if members[0][4] < 0:
                        key = keep[0][1]
                        out.write(frame_object(ObjKind.PAGE_I, key, redo[key], self.codec, self.level))
                        newlocs[key] = self.locs[key]._replace(offset=pos, kind=int(ObjKind.PAGE_I),
                                                               anchored=True, depth=0)
                        continue
                    if len(keep) == len(members) and not any(m[1] in redo for m in keep):
                        f.seek(off)
                        out.write(f.read(end - off))
                        f.seek(end)
                        for m in keep:
                            newlocs[m[1]] = self.locs[m[1]]._replace(offset=pos)
                        continue
                    batch = []
                    for slot, (kind, key, base, raw, _s) in enumerate(keep):
                        if key in redo:
                            batch.append((ObjKind.PAGE_I, key, None, redo[key]))
                            newlocs[key] = self.locs[key]._replace(offset=pos, slot=slot, kind=int(ObjKind.PAGE_I),
                                                                   anchored=True, depth=0)
                        else:
                            batch.append((kind, key, base, raw))
                            newlocs[key] = self.locs[key]._replace(offset=pos, slot=slot)
                    out.write(frame_batch(batch, self.codec, self.level))
                out.flush()
                os.fsync(out.fileno())
                newsize = out.tell()
            os.replace(tmp, real)
            fsync_dir(real.parent)
            self.locs.update(newlocs)
            self.valid[(area, pack)] = newsize
            freed += size - newsize
        for key in drop:
            loc = self.locs.get(key)
            if loc is not None and (loc.area, loc.pack) in inpack:
                del self.locs[key]
            if key in self._cache:
                self._cached -= len(self._cache.pop(key))
        # depths shrink where a chain now ends at a re-encoded page
        self._resolve_chains({k: b for k, b in base_of.items() if k in self.locs and k not in redo
                              and self.locs[k].kind == ObjKind.PAGE_X})
        self.evictions = True
        self.availability(drop, True)
        return freed, dropped

    def close_handles(self) -> None:
        pool, self._page_encoder = self._page_encoder, None
        if pool is not None:
            pool.shutdown(wait=True)
        for f in self._read.values():
            f.close()
        self._read.clear()
        for area, (_n, f) in list(self._cur.items()):
            f.flush()
            if area in self._dirty:
                os.fsync(f.fileno())
            f.close()
        self._cur.clear()
        self._dirty.clear()


def fsync_dir(path: str | os.PathLike[str]) -> None:
    # Metadata publication must not silently ignore unsupported barriers.
    from kptas.platform_sync import sync_dir
    sync_dir(path)


__all__ = ["AREA_FILES", "AREA_KF", "AREA_PAGES", "EvictedError", "Loc", "MissingObject",
           "PageMap", "Store", "Unit", "leaf_payload", "parse_leaf", "parse_root", "xor_pages"]
