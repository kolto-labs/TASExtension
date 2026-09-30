"""Content-addressed object store (contract 8.6-8.8): packs, dedup, XOR deltas, tree objects, keyframe packs."""

from __future__ import annotations

import os
import struct
from collections import OrderedDict
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from typing import BinaryIO, NamedTuple

from kptas.types import (PAGE_KEY_LEN, PAGE_SIZE, ZERO_PAGE_KEY, Codec, FormatError, ObjKind,
                         PageFlag, PageRef, frame_object, page_key, read_object, sha256)

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
    """The object is missing and the bundle has recorded budget evictions."""


class Loc(NamedTuple):
    area: int
    pack: int          # pack number, or the tick of a keyframe pack
    offset: int
    kind: int
    anchored: bool     # its XOR chain ends at a page in a materialized pack
    depth: int         # PAGE_X frames between it and its anchor


def _need(cond: bool, msg: str) -> None:
    if not cond:
        raise FormatError(msg)


def xor_pages(a: bytes, b: bytes) -> bytes:
    return (int.from_bytes(a, "little") ^ int.from_bytes(b, "little")).to_bytes(PAGE_SIZE, "little")


def pack_path(root: Path, area: int, pack: int) -> Path:
    if area == AREA_KF:
        return root / "keyframes" / f"kf-{pack:012d}.pack"
    return root / AREA_DIRS[area] / f"pack-{pack:06d}"


def list_packs(root: Path) -> dict[tuple[int, int], int]:
    """(area, pack) -> size in bytes, following spill symlinks."""
    out: dict[tuple[int, int], int] = {}
    for area, d in AREA_DIRS.items():
        p = root / d
        if not p.is_dir():
            continue
        for e in os.scandir(p):
            name = e.name
            if area == AREA_KF:
                if not (name.startswith("kf-") and name.endswith(".pack") and name[3:-5].isdigit()):
                    continue
                n = int(name[3:-5])
            else:
                if not (name.startswith("pack-") and name[5:].isdigit()):
                    continue
                n = int(name[5:])
            try:
                out[(area, n)] = os.stat(e.path).st_size
            except FileNotFoundError:
                out[(area, n)] = -1   # dangling spill link
    return out


def _frame_at(f: BinaryIO, pos: int, size: int) -> tuple[ObjKind, bytes, bytes | None, bytes, int] | None:
    f.seek(pos)
    head = f.read(_OBJ_HEAD.size)
    if len(head) < _OBJ_HEAD.size:
        return None
    magic, kind, _codec, klen, _res, slen, _rlen = _OBJ_HEAD.unpack(head)
    if magic != b"KPOB" or kind not in (1, 2, 3, 4, 5, 6):
        return None
    total = _OBJ_HEAD.size + klen + (PAGE_KEY_LEN if kind == ObjKind.PAGE_X else 0) + slen + 4
    if pos + total > size:
        return None
    try:
        return read_object(head + f.read(total - _OBJ_HEAD.size))
    except FormatError:
        return None


def iter_frames(f: BinaryIO, size: int, resync: bool = False
                ) -> Iterator[tuple[int, ObjKind, bytes, bytes | None, bytes, int]]:
    """Yield (offset, kind, key, base, raw, end) per valid frame.

    Without resync, stop at the first bad or torn frame. With resync, skip to the next
    KPOB whose frame checks out, so one bad frame does not hide later ones.
    """
    pos = 0
    while pos < size:
        got = _frame_at(f, pos, size)
        if got is None:
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
                got = _frame_at(f, nxt, size)
            pos = nxt
        k, key, base, raw, rel = got
        yield pos, k, key, base, raw, pos + rel
        pos += rel


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
            self._payload.pop(hi, None)
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
            self._payload.pop(hi, None)

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
            p = leaf_payload(hi, (leaf[v] for v in sorted(leaf)))
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

class Store:
    """Content-addressed frames under pages/, files/ and keyframes/, with a parent fallback."""

    def __init__(self, root: str | os.PathLike[str], *, codec: str = "zlib", level: int = 1,
                 max_chain: int = 32, xor_delta: bool = True, cache_bytes: int = 64 << 20,
                 parent: Store | None = None, writable: bool = False) -> None:
        self.root = Path(root)
        self.codec = CODECS[codec]
        self.level = level
        self.max_chain = max_chain
        self.xor_delta = xor_delta
        self.parent = parent
        self.writable = writable
        self.locs: dict[bytes, Loc] = {}
        self.valid: dict[tuple[int, int], int] = {}
        self.evictions = False
        self._read: dict[tuple[int, int], BinaryIO] = {}
        self._cur: dict[int, tuple[int, BinaryIO]] = {}
        self._dirty: set[int] = set()
        self._cache: OrderedDict[bytes, bytes] = OrderedDict()
        self._cache_bytes = cache_bytes
        self._cached = 0

    # -- index state

    def rebuild(self, kf_ticks: set[int] | None = None) -> None:
        """Scan every pack and rebuild the object locations; record where each pack's last valid frame ends.

        Keyframe packs are indexed only for ticks in kf_ticks (committed MATERIALIZED ticks) when given.
        """
        self.close_handles()
        self.locs.clear()
        self.valid.clear()
        bases: dict[bytes, bytes] = {}
        for (area, pack), size in sorted(list_packs(self.root).items()):
            if size < 0 or (area == AREA_KF and kf_ticks is not None and pack not in kf_ticks):
                continue
            with open(pack_path(self.root, area, pack), "rb") as f:
                end = 0
                for off, kind, key, base, _raw, end in iter_frames(f, size, resync=True):
                    old = self.locs.get(key)
                    if old is not None and (old.area == AREA_KF or area != AREA_KF):
                        continue  # keep the first copy, but prefer a keyframe-pack copy (an anchor)
                    bases.pop(key, None)
                    anchored = area == AREA_KF and kind == ObjKind.PAGE_I
                    self.locs[key] = Loc(area, pack, off, int(kind), anchored, 0)
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
            if key in memo or self.locs[key].kind != ObjKind.PAGE_X:
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
        """Bytes of packs stored inside the bundle directory (spilled packs excluded)."""
        total = 0
        for (area, pack), size in list_packs(self.root).items():
            if size > 0 and not pack_path(self.root, area, pack).is_symlink():
                total += size
        return total

    def current_spilled(self, area: int) -> bool:
        cur = self._current(area, open_it=False)
        return cur is not None and pack_path(self.root, area, cur).is_symlink()

    # -- reading

    def _reader(self, area: int, pack: int) -> BinaryIO:
        f = self._read.get((area, pack))
        if f is None:
            f = self._read[(area, pack)] = open(pack_path(self.root, area, pack), "rb")
        return f

    def read_frame(self, loc: Loc) -> tuple[ObjKind, bytes, bytes | None, bytes]:
        cur = self._cur.get(loc.area)
        if cur is not None and cur[0] == loc.pack:
            cur[1].flush()
        f = self._reader(loc.area, loc.pack)
        f.seek(loc.offset)
        head = f.read(_OBJ_HEAD.size)
        _need(len(head) == _OBJ_HEAD.size, f"no frame at {loc}")
        _m, kind, _c, klen, _r, slen, _rl = _OBJ_HEAD.unpack(head)
        total = _OBJ_HEAD.size + klen + (PAGE_KEY_LEN if kind == ObjKind.PAGE_X else 0) + slen + 4
        kind, key, base, raw, _ = read_object(head + f.read(total - _OBJ_HEAD.size))
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
            if self.evictions:
                raise EvictedError(f"{key.hex()} is not stored; this bundle evicted payloads under its budget "
                                   "(see meta.json evictions and the evicted: marks)")
            raise MissingObject(key.hex())
        kind, k, base, raw = self.read_frame(loc)
        _need(k == key, f"index points {key.hex()} at a frame keyed {k.hex()}")
        if kind == ObjKind.PAGE_X:
            raw = xor_pages(raw, self.get(base))  # type: ignore[arg-type]
        ok = page_key(raw) == key if len(key) == PAGE_KEY_LEN else sha256(raw) == key
        _need(ok, f"object {key.hex()} does not match its content key")
        self._remember(key, raw)
        return raw

    def _remember(self, key: bytes, raw: bytes) -> None:
        if len(raw) > self._cache_bytes:
            return
        self._cache[key] = raw
        self._cached += len(raw)
        while self._cached > self._cache_bytes:
            _k, v = self._cache.popitem(last=False)
            self._cached -= len(v)

    # -- writing

    def encode_page(self, key: bytes, data: bytes, bases: Iterable[bytes | None],
                    durable: Callable[[bytes], bool] | None = None) -> tuple[bytes, int, bool, int]:
        """Frame one page: PAGE_X against the first eligible base when smaller, else PAGE_I.

        A base is eligible when its chain is anchored at a materialized pack and shorter
        than max_chain, so every XOR chain obeys 8.7, and when `durable` accepts it.
        """
        intact = frame_object(ObjKind.PAGE_I, key, data, self.codec, self.level)
        if self.xor_delta:
            for b in bases:
                if b is None or b == key or b == ZERO_PAGE_KEY:
                    continue
                inf = self.info(b)
                if inf is None or not inf[0] or inf[1] >= self.max_chain or (durable and not durable(b)):
                    continue
                x = frame_object(ObjKind.PAGE_X, key, xor_pages(data, self.get(b)), self.codec, self.level, base=b)
                if len(x) < len(intact):
                    return x, ObjKind.PAGE_X, True, inf[1] + 1
                break
        return intact, ObjKind.PAGE_I, False, 0

    def frame(self, kind: ObjKind, key: bytes, raw: bytes) -> bytes:
        return frame_object(kind, key, raw, self.codec, self.level)

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

    def append(self, area: int, items: Iterable[tuple[bytes, bytes, int, bool, int]]) -> int:
        """Append (key, frame, kind, anchored, depth) items, skipping keys already stored. Returns bytes written."""
        written = 0
        for key, frame, kind, anchored, depth in items:
            if key in self.locs:
                continue
            n = self._current(area)
            f = self._cur[area][1]
            off = f.tell()
            if off and off + len(frame) > PACK_LIMIT:
                n = self.roll(area)
                f = self._cur[area][1]
                off = 0
            f.write(frame)
            self.locs[key] = Loc(area, n, off, int(kind), anchored, depth)
            self.valid[(area, n)] = off + len(frame)  # type: ignore[index]
            written += len(frame)
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
        """A materialized pack: ROOT, the LEAFs, then PAGE_I for every non-zero page in ascending vaddr."""
        root, rootp, leaves = pmap.tree()
        parts = [self.frame(ObjKind.ROOT, root, rootp)]
        parts.extend(self.frame(ObjKind.LEAF, k, p) for k, p in leaves)
        for r in pmap.refs():
            if r.key != ZERO_PAGE_KEY:
                parts.append(frame_object(ObjKind.PAGE_I, r.key, payload(r.key), self.codec, self.level))
        return b"".join(parts)

    def write_kf(self, tick: int, blob: bytes, fsync: bool) -> None:
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
        self._read.pop((AREA_KF, tick), None)
        with open(path, "rb") as f:
            end = 0
            for off, kind, key, _b, _raw, end in iter_frames(f, len(blob)):
                old = self.locs.get(key)
                if old is None or (kind == ObjKind.PAGE_I and old.area != AREA_KF):
                    self.locs[key] = Loc(AREA_KF, tick, off, int(kind), kind == ObjKind.PAGE_I, 0)
            _need(end == len(blob), "keyframe pack did not read back whole")
        self.valid[(AREA_KF, tick)] = len(blob)

    def read_kf(self, tick: int) -> tuple[bytes, list[PageRef], dict[bytes, bytes]]:
        """Read a materialized pack sequentially: (state root, page refs, key -> page bytes)."""
        path = pack_path(self.root, AREA_KF, tick)
        with open(path, "rb") as f:
            frames = list(iter_frames(f, os.fstat(f.fileno()).st_size))
        _need(frames and frames[0][1] == ObjKind.ROOT, f"{path.name} does not start with ROOT")
        root = frames[0][2]
        leaf_keys = [k for _hi, k in parse_root(frames[0][4])]
        _need(len(frames) > len(leaf_keys), f"{path.name}: {len(frames) - 1} frames for {len(leaf_keys)} LEAFs")
        refs: list[PageRef] = []
        for i, k in enumerate(leaf_keys, 1):
            _need(frames[i][1] == ObjKind.LEAF and frames[i][2] == k, f"{path.name}: LEAF {i} out of order")
            refs.extend(parse_leaf(frames[i][4]))
        pages = frames[1 + len(leaf_keys):]
        nonzero = [r for r in refs if r.key != ZERO_PAGE_KEY]
        _need(len(pages) == len(nonzero), f"{path.name}: {len(pages)} pages for {len(nonzero)} non-zero refs")
        data: dict[bytes, bytes] = {}
        for r, (_o, kind, key, _b, raw, _e) in zip(nonzero, pages):
            _need(kind == ObjKind.PAGE_I and key == r.key and page_key(raw) == key,
                  f"{path.name}: page {r.vaddr:#x} does not match its ref")
            data[key] = raw
        return root, refs, data

    # -- eviction

    def bases(self) -> dict[bytes, bytes]:
        """key -> base key of every PAGE_X frame stored in this bundle."""
        return {k: self.read_frame(l)[2] for k, l in self.locs.items()  # type: ignore[misc]
                if l.kind == ObjKind.PAGE_X}

    def compact(self, drop: set[bytes], packs: Iterable[tuple[int, int]],
                base_of: dict[bytes, bytes] | None = None) -> tuple[int, int]:
        """Rewrite the given packs without `drop` keys and unindexed frames.

        A kept PAGE_X whose chain passes through a dropped or re-encoded key is re-encoded
        as PAGE_I first, so every remaining chain still ends at its anchor. Each pack is rewritten as tmp, fsynced, then replaced. Returns (bytes freed, frames dropped).
        """
        packs = sorted(set(packs))
        inpack = set(packs)
        if base_of is None:
            base_of = self.bases()
        redo: dict[bytes, bytes] = {}
        grew = True
        while grew:  # a chain through a re-encoded (unanchored) PAGE_I must be re-encoded too
            grew = False
            for key, loc in self.locs.items():
                if key in drop or key in redo or loc.kind != ObjKind.PAGE_X or (loc.area, loc.pack) not in inpack:
                    continue
                b = base_of.get(key)
                while b is not None:
                    if b in drop or b in redo:
                        redo[key] = self.get(key)
                        grew = True
                        break
                    b = base_of.get(b)
        self.close_handles()
        freed = dropped = 0
        for area, pack in packs:
            path = pack_path(self.root, area, pack)
            real = Path(os.path.realpath(path))
            size = real.stat().st_size
            tmp = real.with_name(real.name + ".tmp")
            newlocs: dict[bytes, Loc] = {}
            with open(real, "rb") as f, open(tmp, "wb") as out:
                for off, _kind, key, _b, _raw, end in iter_frames(f, size, resync=True):
                    loc = self.locs.get(key)
                    if key in drop or loc is None or (loc.area, loc.pack, loc.offset) != (area, pack, off):
                        dropped += key in drop
                        continue
                    if key in redo:
                        frame = frame_object(ObjKind.PAGE_I, key, redo[key], self.codec, self.level)
                        loc = loc._replace(kind=int(ObjKind.PAGE_I), anchored=False, depth=0)
                    else:
                        f.seek(off)
                        frame = f.read(end - off)
                        f.seek(end)
                    newlocs[key] = loc._replace(offset=out.tell())
                    out.write(frame)
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
        self.evictions = True
        return freed, dropped

    def close_handles(self) -> None:
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
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


__all__ = ["AREA_FILES", "AREA_KF", "AREA_PAGES", "EvictedError", "Loc", "MissingObject",
           "PageMap", "Store", "leaf_payload", "parse_leaf", "parse_root", "xor_pages"]
