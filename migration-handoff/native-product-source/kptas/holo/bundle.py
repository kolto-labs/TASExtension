"""Holorecord bundle `<name>.holo/` (contract 8): create, open, commit ticks, read maps, audit, budget."""

from __future__ import annotations

import dataclasses
import copy
import datetime
import json
import os
import platform
import shutil
import sys
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable, Iterator, Mapping
from pathlib import Path
from typing import Any

from kptas import config as kconfig
from kptas.filetree import decode_tree, validate_rows
from kptas.holo import index as kindex
from kptas.holo.index import TickRow
from kptas.holo.journal import ChainState, Journal, audit_journal, epoch_body, mark_body, seal_body
from kptas.holo.pages import (AREA_DIRS, AREA_FILES, AREA_PAGES, PAGE_KINDS, EvictedError, PageMap, Store, Unit,
                              fsync_dir, list_packs, pack_path, parse_leaf, parse_root)
from kptas.types import (CHAINED_TYPES, CHUNK_HEAD_SIZE, CHUNK_TRAILER_SIZE, NULL_DIGEST, PAGE_SIZE,
                         ZERO_PAGE_KEY, EnvPin, FormatError, Header, ObjKind, PageFlag, Reached, Receipt, Tier,
                         PageRef, TickFlag, TickRecord, canonical_json, chained_payload, page_key, resolve_codec, sha256, split_chained)

try:
    import fcntl
except ImportError:  # not POSIX; meta.json updates are then unlocked
    fcntl = None  # type: ignore[assignment]

META_NAME = "meta.json"
EVICT_MARK = "evicted:"
EVENT_MARK = "event:"       # one MARK per engine event; @mark labels cannot contain ':'
_CAP, _KF, _MAT = TickFlag.CAPTURED, TickFlag.KEYFRAME, TickFlag.MATERIALIZED
_RECORD_CACHE_BYTES = 4 << 20
_RECORD_CACHE_ENTRIES = 512
_RECEIPT_SUFFIX_BYTES = 8 << 20
_RECEIPT_SUFFIX_CHUNKS = 4096


class MapUnavailableError(FormatError):
    """The record has no captured map for this tick; replay can supply it."""


class BudgetError(RuntimeError):
    """The store budget cannot take the next commit and no configured step made room."""


def _need(cond: Any, msg: str) -> None:
    if not cond:
        raise FormatError(msg)


def _canonical_tick(rec: Any) -> bool:
    """A canonical immutable TickRecord: exact types throughout, so its encode() is
    the TICK body TickRecord.decode would rebuild it from. Public subclasses or
    custom scalar/bytes types keep the full encode and decode path."""
    return (type(rec) is TickRecord and type(rec.note) is str
            and all(type(getattr(rec, name)) is int for name in
                    ('tick', 'epoch', 'server_passes', 'subticks'))
            and all(type(getattr(rec, name)) is bytes for name in
                    ('state_root', 'sim_root', 'files_root', 'input_sha256',
                     'journal_sha256', 'journal'))
            and all(type(ref) is PageRef and type(ref.key) is bytes
                    and type(ref.vaddr) is int and type(ref.prot) is int
                    for ref in rec.pages))


def read_meta(root: str | os.PathLike[str]) -> dict[str, Any]:
    try:
        with open(Path(root) / META_NAME, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def _write_meta(root: Path, meta: dict[str, Any]) -> None:
    from kptas.durability import atomic_json
    atomic_json(root / META_NAME, meta)


def meta_append(root: str | os.PathLike[str], key: str, entry: dict[str, Any]) -> None:
    """Read-modify-write one list in meta.json, so concurrent notes from a branch are kept."""
    root = Path(root)
    fd = os.open(root, os.O_RDONLY) if fcntl is not None else None
    try:
        if fd is not None:
            fcntl.flock(fd, fcntl.LOCK_EX)  # a parent writer and a branch creator may update it at once
        meta = read_meta(root)
        meta.setdefault(key, []).append(entry)
        _write_meta(root, meta)
    finally:
        if fd is not None:
            os.close(fd)


class Bundle:
    """One holorecord. Use Bundle.create, Bundle.open or kptas.holo.create_branch."""

    def __init__(self, root: Path, writable: bool, config: Mapping[str, Any] | None,
                 budget_bytes: int | None) -> None:
        self.root = root
        self.writable = writable
        self.cfg = kconfig.resolve({}, "<defaults>") if config is None else config
        self.store_cfg = self.cfg["store"]
        self.budget = int(self.store_cfg["budget_gb"] * (1 << 30)) if budget_bytes is None else budget_bytes
        self.ram_limit = self.store_cfg["ram_budget_mb"] << 20
        self.materialize_every = self.cfg["seek"]["materialize_every"]
        self.chain = ChainState()
        self.rows: list[TickRow] = []
        self._pos: dict[int, int] = {}
        self.marks: list[dict[str, Any]] = []
        self.epochs: list[dict[str, Any]] = []
        self.receipt_offsets: list[int] = []
        self.parent: Bundle | None = None
        self._owns_parent = False
        self.journal: Journal = None  # type: ignore[assignment]
        self.store: Store = None      # type: ignore[assignment]
        self.live: PageMap | None = None
        self.live_tick = -1
        self.live_epoch = -1
        self.live_through = -1      # last tick the live map still holds for (only uncaptured ticks since live_tick)
        self.anchors: dict[int, bytes] | None = None
        self.pending: dict[bytes, tuple[int, bytes]] = {}   # held payloads: key -> (vaddr, page)
        self.thinning = False
        self.budget_log: list[dict[str, Any]] = []
        self.torn_bytes = 0
        self.valid_len = 0
        self.tail_offset = 0
        self._ready = False
        self._pins: set[bytes] | None = None
        self._compactions = 0   # budget evictions that rewrote packs in this session
        self._records: OrderedDict[int, tuple[bytes, TickRecord]] = OrderedDict()
        self._record_bytes = 0
        self._admitted_files = None
        self._closed_admission = None
        self._file_encoder = None
        self._tick_prof: dict | None = None             # capture.gl_profile phase sample in flight
        self.last_append_tick_profile: dict | None = None

    # ---------------------------------------------------------------- lifecycle

    @classmethod
    def create(cls, path: str | os.PathLike[str], header: Header, score: str, env: EnvPin, *,
               config: Mapping[str, Any] | None = None, budget_bytes: int | None = None,
               parent: Bundle | None = None) -> Bundle:
        """Create `<name>.holo/` and commit HOLO, SCOR and ENVP (the chain genesis)."""
        root = Path(path)
        _need(root.name.endswith(".holo") and len(root.name) > 5, "a holorecord directory is named <name>.holo")
        _need((header.parent is None) == (parent is None), "a branch header needs its parent bundle, and only then")
        header = dataclasses.replace(header, codec=resolve_codec(header.codec))
        _need(header.codec != "zstd" or sys.version_info >= (3, 14), "codec zstd needs Python 3.14 or newer")
        root.mkdir()
        for d in ("pages", "keyframes", "files", "index"):
            (root / d).mkdir()
        b = cls(root, True, config, budget_bytes)
        b.journal = Journal(root / "journal", True, create=True)
        for fourcc, payload in ((b"HOLO", header.to_payload()), (b"SCOR", score.encode("utf-8")),
                                (b"ENVP", env.to_payload())):
            b.chain.feed(fourcc, payload)
            b.tail_offset = b.journal.append_raw(fourcc, payload)[0]
        b.journal.fsync()
        meta: dict[str, Any] = {
            "format": "holo-meta/1",
            "created": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
            "host": {"node": platform.node(), "platform": platform.platform(), "python": sys.version.split()[0]},
            "parent_path": None, "parent_abspath": None,
            "branches": [], "spills": [], "evictions": [], "thinning": [],
        }
        if parent is not None:
            meta["parent_path"] = os.path.relpath(parent.root.resolve(), root.resolve())
            meta["parent_abspath"] = str(parent.root.resolve())
        _write_meta(root, meta)
        fsync_dir(root)
        b._attach(parent, owns=False)
        b.store.rebuild()
        b._ready = True
        return b

    @classmethod
    def open(cls, path: str | os.PathLike[str], *, writable: bool = False,
             config: Mapping[str, Any] | None = None, budget_bytes: int | None = None) -> Bundle:
        """Open a bundle. A writer cuts a torn journal tail and torn pack tails; a reader ignores them.

        A valid frame that breaks the chain is refused (use audit_journal to inspect it).
        """
        root = Path(path)
        _need((root / "journal").is_file(), f"{root} is not a holorecord (no journal)")
        b = cls(root, writable, config, budget_bytes)
        b.journal = Journal(root / "journal", writable)
        try:
            idx = kindex.load(root)
            fresh = idx is not None and b._restore(idx[0], idx[2])
            if not fresh:
                b._scan()
            parent = None
            hp = b.header.parent
            if hp is not None:
                meta = read_meta(root)
                cands = [root / meta["parent_path"]] if meta.get("parent_path") else []
                if meta.get("parent_abspath"):
                    cands.append(Path(meta["parent_abspath"]))
                found = [c for c in cands if (c / "journal").is_file()]
                _need(found, f"branch parent {hp.holo} not found (meta.json parent_path)")
                parent = Bundle.open(found[0], config=config)
            b._attach(parent, owns=True)
            if fresh:
                b.store.locs = idx[1]  # type: ignore[index]
                b.store.valid = {tuple(map(int, k.split(":"))): v  # type: ignore[misc]
                                 for k, v in idx[0]["valid"].items()}  # type: ignore[index]
            else:
                b.store.rebuild({r.tick for r in b.rows if r.flags & _MAT})
            if writable:
                if b.torn_bytes:
                    b.journal.truncate(b.valid_len)
                    b.budget_log.append({"step": "recover", "journal_bytes_cut": b.torn_bytes})
                for area, pack, n in b.store.truncate_tails():
                    b.budget_log.append({"step": "recover", "pack": f"{AREA_DIRS[area]}/{pack}", "bytes_cut": n})
            b._ready = True
            if not fresh and writable:
                b.save_index()
            if not writable:
                b._admitted_files = b._reuse_snapshot()
        except BaseException:
            b.close()
            raise
        return b

    def _attach(self, parent: Bundle | None, owns: bool) -> None:
        hp = self.header.parent
        if hp is not None:
            _need(parent is not None, "branch needs its parent")
            _need(parent.bundle_id == hp.holo, f"parent is {parent.bundle_id}, HOLO names {hp.holo}")  # type: ignore[union-attr]
            _need(parent.link_at(hp.tick).hex() == hp.link,  # type: ignore[union-attr]
                  f"parent link at tick {hp.tick} does not match HOLO")
            if not self.rows:
                self.chain.last_epoch = max(self.chain.last_epoch, parent.epoch_at(hp.tick))  # type: ignore[union-attr]
        self.parent, self._owns_parent = parent, owns
        st = self.store_cfg
        self.store = Store(self.root, codec=self.header.codec, level=st["level"], max_chain=st["max_delta_chain"],
                           xor_delta=st["xor_delta"], batch_pages=st["batch_pages"],
                           cache_bytes=st["hot_ring_mb"] << 20, parent=parent.store if parent else None,
                           writable=self.writable)
        meta = read_meta(self.root)
        self.store.evictions = bool(meta.get("evictions") or meta.get("thinning"))
        # Old eviction counts/ranges cannot identify missing objects. Only this
        # ordered, exact-key ledger establishes absence; reintroduction revokes it.
        for entry in meta.get("availability", []):
            _need(isinstance(entry, dict) and type(entry.get("missing")) is bool
                  and isinstance(entry.get("keys"), list), "invalid availability entry")
            keys = set()
            for key in entry["keys"]:
                _need(isinstance(key, str) and len(key) == 32, "invalid availability page key")
                try:
                    raw = bytes.fromhex(key)
                except ValueError:
                    raise FormatError("invalid availability page key") from None
                _need(len(raw) == 16, "invalid availability page key")
                keys.add(raw)
            if entry["missing"]:
                self.store.evicted_keys.update(keys)
            else:
                self.store.evicted_keys.difference_update(keys)
        # Keep the callback independent of its owner so closed bundles release immediately.
        root = self.root
        def note(keys: set[bytes], missing: bool) -> None:
            meta_append(root, "availability", {"missing": missing, "keys": sorted(k.hex() for k in keys)})
            with open(root / META_NAME, "rb") as f:
                os.fsync(f.fileno())
            fsync_dir(root)
        self.store.note_availability = note

    def _reuse_snapshot(self):
        """Filesystem identity for this admitted object, excluding derived indexes.

        Branch annotations and receipt timings do not alter admitted content.
        Journal, payload packs, authoring authority and availability metadata do.
        This is private per-object freshness evidence, never a global cache.
        """
        def stamp(path):
            st = path.stat()
            return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
        root = self.root.resolve(strict=True)
        own = stamp(root / 'journal')
        opened = os.fstat(self.journal.f.fileno())
        _need((opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns) == own,
              'admitted journal was replaced or changed')
        meta = read_meta(root)
        for harmless in ('branches', 'receipt_micros'):
            meta.pop(harmless, None)
        packs = tuple((area, pack, stamp(pack_path(root, area, pack)))
                      for area, pack in sorted(list_packs(root)))
        authority = [root / name for name in ('live.score', 'live.pending.json', 'durable.json')]
        authority += list(root.glob('authoring-*.score'))
        authority.append(root.with_name(root.stem.split('@', 1)[0] + '.session.json'))
        files = tuple((str(path), stamp(path) if path.exists() else None)
                      for path in sorted(authority))
        directory = root.stat()
        return (str(root), directory.st_dev, directory.st_ino, own, packs, canonical_json(meta), files)

    def _check_admitted_tail(self):
        """Check concrete genesis and live tail, without reloading a large index."""
        chain = ChainState()
        for i, (_off, kind, payload, _end) in enumerate(self.journal.scan()):
            chain.feed(kind, payload)
            if i == 2:
                break
        _need(chain.h0 == self.chain.h0 and chain.env == self.env and chain.header == self.header,
              'admitted genesis/environment changed')
        kind, payload = self.journal.read(self.tail_offset)
        link = split_chained(payload)[1] if kind in CHAINED_TYPES else chain.h0
        end = self.tail_offset + CHUNK_HEAD_SIZE + len(payload) + CHUNK_TRAILER_SIZE
        _need(link == self.chain.link and end == self.valid_len == self.journal.size(),
              'admitted journal tail changed')

    def _check_borrowed_ancestry(self):
        parent, child, seen = self.parent, self, set()
        while parent is not None:
            _need(id(parent) not in seen, 'cyclic admitted ancestry')
            seen.add(id(parent))
            _need(parent._ready and not parent.writable and not parent.journal.f.closed,
                  'admitted parent reader is closed or writable')
            _need(parent._admitted_files is not None and parent._reuse_snapshot() == parent._admitted_files,
                  'admitted parent files changed; reopen recording')
            parent._check_admitted_tail()
            hp = child.header.parent
            _need(hp is not None and parent.bundle_id == hp.holo
                  and parent.link_at(hp.tick).hex() == hp.link and child.env.key() == parent.env.key(),
                  'admitted parent header/link/environment mismatch')
            meta = read_meta(child.root)
            _need(isinstance(meta.get('parent_path'), str)
                  and (child.root / meta['parent_path']).resolve() == parent.root.resolve()
                  and Path(meta.get('parent_abspath', '')).resolve() == parent.root.resolve(),
                  'admitted parent path mismatch')
            child, parent = parent, parent.parent

    def _check_closed_content(self):
        """Validate this writer's own bytes, never recursively scan ancestors."""
        from kptas.holo.pages import iter_units
        candidate = ChainState()
        scan = self.journal.scan()
        for _off, kind, payload, _end in scan:
            candidate.feed(kind, payload)
        if not self.rows and self.parent is not None:
            candidate.last_epoch = max(candidate.last_epoch, self.parent.epoch_at(self.chain.tick_floor))
        _need(scan.valid_len == self.journal.size() and candidate == self.chain,
              'closed writer journal differs from its admitted chain')
        observed = set()
        for (area, pack), size in list_packs(self.root).items():
            _need(size >= 0 and self.store.valid.get((area, pack)) == size,
                  'closed writer pack length differs from admission')
            with open(pack_path(self.root, area, pack), 'rb') as stream:
                for off, _end, members in iter_units(stream, size, strict=True):
                    observed.update((key, area, pack, off, int(kind), slot)
                                    for kind, key, _base, _raw, slot in members)
        _need(all((key, loc.area, loc.pack, loc.offset, loc.kind, loc.slot) in observed
                  for key, loc in self.store.locs.items()),
              'closed writer payload locations differ from admission')

    def reopen_reader(self):
        """Reuse a just-cleanly-closed writer, with its still-open borrowed parent.

        No journal/store bytes or indexes are written. Failure retains a closed
        object; callers must recover explicitly rather than adopting stale state.
        """
        _need(not self._ready and self.journal.f.closed and self.writable
              and self._closed_admission is not None and not self._owns_parent,
              'reader reuse requires a cleanly closed writer with borrowed ancestry')
        journal = Journal(self.root / 'journal', writable=False)
        self.journal = journal
        try:
            _need(self._reuse_snapshot() == self._closed_admission, 'closed writer files changed')
            self._check_admitted_tail()
            self._check_closed_content()
            self._check_borrowed_ancestry()
            _need(self._reuse_snapshot() == self._closed_admission, 'closed writer changed during validation')
            if self.live is not None:
                rec = self.record(self.live_tick)
                _need(rec.epoch == self.live_epoch and self.live.root() == rec.state_root,
                      'closed writer live map differs from its recorded root')
            # Keep location admission, not this writer's decoded payload bytes:
            # its future reads still exercise content hashes against disk payloads.
            # Ancestors were just proven read-only readers whose files are unchanged
            # since admission (_check_borrowed_ancestry); their hot caches hold only
            # bytes Store.get verified against content keys, so they stay warm.
            self.store._cache.clear()
            self.store._cached = 0
            self.writable = self.store.writable = False
            self._admitted_files = self._closed_admission
            self._closed_admission = None
            self._ready = True
            return self
        except BaseException:
            journal.close()
            self._records.clear()
            self._record_bytes = 0
            raise

    def close(self) -> None:
        """Store pending payloads, fsync, write the index and release the lock."""
        self._closed_admission = None
        try:
            if self.writable and self._ready and not self.journal.f.closed and self.journal.write_failure is None:
                try:
                    self._flush_pending()
                except BudgetError:
                    # already marked EVICTED on their ticks; note that they never reached the store
                    entry = {"dropped_at_close": len(self.pending), "tick": self.chain.last_tick}
                    meta_append(self.root, "thinning", entry)
                    self.budget_log.append({"step": "thin", **entry})
                    self.store.availability({k for k in self.pending if k not in self.store.locs}, True)
                    self.pending.clear()
                self.store.fsync()
                self.journal.fsync()
                self.save_index()
                try:
                    self.valid_len = self.journal.size()
                    self._check_admitted_tail()
                    self._closed_admission = self._reuse_snapshot()
                except (FormatError, OSError):
                    # Optional reuse evidence must not change close semantics.
                    # Reopen refuses when this witness could not be established.
                    self._closed_admission = None
        finally:
            pool, self._file_encoder = self._file_encoder, None
            if pool is not None:
                pool.shutdown(wait=True)
            self._ready = False
            self._records.clear()
            self._record_bytes = 0
            if self.store is not None:
                self.store.close_handles()
            if self.journal is not None:
                self.journal.close()
            if self.parent is not None and self._owns_parent:
                self.parent.close()

    def __enter__(self) -> Bundle:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---------------------------------------------------------------- journal state

    def _note(self, off: int, fourcc: bytes, dec: Any, link: bytes) -> None:
        self.tail_offset = off
        if fourcc == b"TICK":
            self._pos[dec.tick] = len(self.rows)
            self.rows.append(TickRow(dec.tick, dec.epoch, int(dec.flags), off, link))
        elif fourcc == b"MARK":
            self.marks.append(dec)
        elif fourcc == b"EPOC":
            self.epochs.append(dec)
        elif fourcc == b"RCPT":
            self.receipt_offsets.append(off)

    def _scan(self) -> None:
        sc = self.journal.scan()
        for off, fourcc, payload, _end in sc:
            _b, link, dec = self.chain.feed(fourcc, payload)
            self._note(off, fourcc, dec, link)
        _need(self.chain.h0 is not None, "journal has no complete HOLO, SCOR, ENVP prefix")
        self.valid_len = sc.valid_len
        self.torn_bytes = sc.size - sc.valid_len

    def _index_state(self) -> dict[str, Any]:
        c = self.chain
        return {
            "journal_len": self.journal.size(), "tail": self.tail_offset,
            "packs": {f"{a}:{p}": s for (a, p), s in list_packs(self.root).items()},
            "valid": {f"{a}:{p}": v for (a, p), v in self.store.valid.items()},
            "chain": {"link": c.link.hex(), "ticks": c.ticks, "last_tick": c.last_tick,
                      "last_epoch": c.last_epoch, "seal": c.seal, "counts": c.counts},
            "marks": self.marks, "epochs": self.epochs, "receipts": self.receipt_offsets,
        }

    @staticmethod
    def _receipt_suffix(journal: Journal, chain: ChainState, start: int) -> tuple[ChainState, list[int], int, int]:
        """Validate a bounded, complete RCPT-only suffix without changing admitted state."""
        size = journal.size()
        _need(0 <= size - start <= _RECEIPT_SUFFIX_BYTES, "receipt suffix exceeds admission bound")
        candidate = dataclasses.replace(chain, counts=dict(chain.counts), genesis=list(chain.genesis))
        offsets: list[int] = []
        tail = -1
        scan = journal.scan(start)
        for off, fourcc, payload, _end in scan:
            _need(fourcc == b"RCPT", "journal changed beyond receipts; reopen recording")
            _need(len(offsets) < _RECEIPT_SUFFIX_CHUNKS, "too many receipt suffix chunks")
            candidate.feed(fourcc, payload)
            offsets.append(off)
            tail = off
        _need(scan.valid_len == size, "incomplete or corrupt receipt suffix")
        return candidate, offsets, tail, scan.valid_len

    def _restore(self, state: dict[str, Any], rows: list[TickRow]) -> bool:
        """Adopt an index prefix plus a bounded verified receipt-only suffix."""
        try:
            _need(state["packs"] == {f"{a}:{p}": s for (a, p), s in list_packs(self.root).items()},
                  "indexed packs changed")
            prefix = state["journal_len"]
            _need(prefix <= self.journal.size(), "indexed journal truncated")
            c = ChainState()
            for i, (_off, fourcc, payload, _end) in enumerate(self.journal.scan()):
                c.feed(fourcc, payload)
                if i == 2:
                    break
            fourcc, payload = self.journal.read(state["tail"])
            ch = state["chain"]
            link = split_chained(payload)[1] if fourcc in CHAINED_TYPES else c.h0
            end = state["tail"] + CHUNK_HEAD_SIZE + len(payload) + CHUNK_TRAILER_SIZE
            _need(c.h0 is not None and link is not None and link.hex() == ch["link"] and end == prefix,
                  "indexed tail does not match journal prefix")
            c.link, c.ticks, c.last_tick, c.last_epoch = bytes.fromhex(ch["link"]), ch["ticks"], ch["last_tick"], ch["last_epoch"]
            c.seal, c.counts = ch["seal"], dict(ch["counts"])
            c, offsets, tail, validated_end = self._receipt_suffix(self.journal, c, prefix)
        except (FormatError, KeyError, TypeError, ValueError):
            self.chain = ChainState()
            return False
        self.chain = c
        self.rows = rows
        self._pos = {r.tick: i for i, r in enumerate(rows)}
        self.marks, self.epochs = state["marks"], state["epochs"]
        self.receipt_offsets = list(state["receipts"]) + offsets
        self.valid_len = validated_end
        self.tail_offset = tail if offsets else state["tail"]
        return True

    def save_index(self) -> None:
        _need(self.journal.write_failure is None, "cannot publish an index after a journal writer failure")
        try:
            kindex.save(self.root, self._index_state(), self.store.locs, self.rows)
        except OSError:
            if self.writable:
                raise

    # ---------------------------------------------------------------- reading

    @property
    def header(self) -> Header:
        return self.chain.header  # type: ignore[return-value]

    @property
    def score(self) -> str:
        if self.writable:
            return self.chain.score  # type: ignore[return-value]
        from .authoring import resolved_score
        if not hasattr(self, '_resolved_score'):
            self._resolved_score = resolved_score(self)
        return self._resolved_score

    @property
    def env(self) -> EnvPin:
        return self.chain.env  # type: ignore[return-value]

    @property
    def bundle_id(self) -> str:
        return self.chain.bundle_id

    @property
    def link(self) -> bytes:
        return self.chain.link

    @property
    def sealed(self) -> dict[str, Any] | None:
        return self.chain.seal

    @property
    def last_tick(self) -> int:
        return self.chain.last_tick

    def ticks(self) -> list[int]:
        return [r.tick for r in self.rows]

    def _row(self, tick: int) -> TickRow | None:
        i = self._pos.get(tick)
        return None if i is None else self.rows[i]

    def _inherited(self, tick: int) -> bool:
        return self.parent is not None and tick <= self.chain.tick_floor

    def record(self, tick: int) -> TickRecord:
        row = self._row(tick)
        if row is None:
            if self._inherited(tick):
                return self.parent.record(tick)  # type: ignore[union-attr]
            raise KeyError(f"no tick {tick}")
        fourcc, payload = self.journal.read(row.offset)
        _need(fourcc == b"TICK", f"index points tick {tick} at a {fourcc!r} chunk")
        # Re-read and CRC-check the journal even on a hit. Cache only decoding:
        # a replaced valid chunk or corrupt frame must never return stale state.
        hit = self._records.get(tick)
        if hit is not None and hit[0] == payload:
            self._records.move_to_end(tick)
            return hit[1]
        record = TickRecord.from_chunk(payload)[0]
        if hit is not None:
            self._record_bytes -= len(self._records.pop(tick)[0])
        if len(payload) <= _RECORD_CACHE_BYTES:
            self._records[tick] = (payload, record)
            self._record_bytes += len(payload)
            while (self._record_bytes > _RECORD_CACHE_BYTES or
                   len(self._records) > _RECORD_CACHE_ENTRIES):
                _old_tick, (old_payload, _old_record) = self._records.popitem(last=False)
                self._record_bytes -= len(old_payload)
        return record

    def link_at(self, tick: int) -> bytes:
        row = self._row(tick)
        if row is not None:
            return row.link
        if self._inherited(tick):
            return self.parent.link_at(tick)  # type: ignore[union-attr]
        raise KeyError(f"no tick {tick}")

    def epoch_at(self, tick: int) -> int:
        row = self._row(tick)
        if row is not None:
            return row.epoch
        if self._inherited(tick):
            return self.parent.epoch_at(tick)  # type: ignore[union-attr]
        raise KeyError(f"no tick {tick}")

    def receipts(self) -> list[Receipt]:
        return [Receipt.from_payload(split_chained(self.journal.read(o)[1])[0]) for o in self.receipt_offsets]

    def page(self, key: bytes) -> bytes:
        return self.store.get(key)

    def _pinned(self) -> set[bytes]:
        """Page keys of every branch-point map recorded in meta.json; budget eviction keeps them."""
        if self._pins is None:
            pins: set[bytes] = set()
            for pin in read_meta(self.root).get("branches", []):
                try:
                    pins |= self.map_at(pin["tick"]).keys()
                except (FormatError, KeyError):
                    pass  # an uncaptured branch point has no map
            self._pins = pins
        return self._pins

    def keeps(self, key: bytes) -> bool:
        """True when this bundle or an ancestor holds `key` in a form budget eviction never drops:
        keyframe-pack pages, tree and file objects, and pages of a branch-point map."""
        loc = self.store.locs.get(key)
        if loc is not None and (loc.area != AREA_PAGES or loc.kind not in PAGE_KINDS or key in self._pinned()):
            return True
        return self.parent is not None and self.parent.keeps(key)

    def _have(self, key: bytes) -> bool:
        """Stored here, or durably in an ancestor; a branch copies anything else it needs."""
        return key == ZERO_PAGE_KEY or key in self.store.locs or (self.parent is not None and self.parent.keeps(key))

    def _tree_map(self, rec_root: bytes) -> PageMap:
        m = PageMap()
        for _hi, lk in parse_root(self.store.get(rec_root)):
            m.apply(parse_leaf(self.store.get(lk)))
        _need(m.root() == rec_root, "keyframe tree objects do not rebuild their root")
        return m

    def map_at(self, tick: int) -> PageMap:
        """map(T) of 8.5: the nearest KEYFRAME K <= T in T's epoch plus the refs of K+1..T."""
        if self.live is not None and tick == self.live_tick:
            return PageMap(self.live.refs())
        row = self._row(tick)
        if row is None:
            if not self._inherited(tick):
                raise MapUnavailableError(f"no tick {tick}")
            return self.parent.map_at(tick)  # type: ignore[union-attr]
        i, todo = self._pos[tick], []
        while True:
            r = self.rows[i]
            if not r.flags & _CAP and r.tick == tick:
                raise MapUnavailableError(f"tick {r.tick} is not captured, so map({tick}) is unknown")
            _need(r.epoch == row.epoch, f"no keyframe in epoch {row.epoch} at or before tick {tick}")
            if r.flags & _KF:
                base = self._tree_map(self.record(r.tick).state_root)
                break
            # An uncaptured tick inside an epoch (capture cadence) has no pages: the next captured
            # tick's pages are the delta since the previous capture, so the walk passes over it.
            if r.flags & _CAP:
                todo.append(r.tick)
            i -= 1
            if i < 0:
                _need(self.parent is not None, f"no keyframe at or before tick {tick}")
                floor = self.chain.tick_floor
                _need(self.parent.epoch_at(floor) == row.epoch, "branch point is in another epoch")  # type: ignore[union-attr]
                base = self.parent.map_at(floor)  # type: ignore[union-attr]
                break
        for t in reversed(todo):
            base.apply(self.record(t).pages)
        return base

    def image(self, tick: int) -> Iterator[tuple[Any, bytes]]:
        """(PageRef, page bytes) for every page of map(T), ascending; one sequential read when materialized."""
        row = self._row(tick)
        if row is not None and row.flags & _MAT:
            _root, refs, data = self.store.read_kf(tick)
            for r in refs:
                yield r, data.get(r.key) or self.store.get(r.key)
            return
        for r in self.map_at(tick).refs():
            yield r, self.store.get(r.key)

    def usage(self) -> int:
        """Bytes the budget counts: the journal and every pack inside the bundle directory."""
        return self.store.local_bytes() + self.journal.size()

    # ---------------------------------------------------------------- writing

    def _check_writable(self, allow_sealed: bool = False) -> None:
        _need(self.writable, "bundle is open read-only")
        _need(self.journal.write_failure is None, "bundle journal writer failed; close and recover before writing")
        _need(allow_sealed or self.chain.seal is None, "bundle is sealed; only RCPT may follow SEAL")

    def _append(self, fourcc: bytes, body: bytes, sync: bool = False, *,
                tick_record: TickRecord | None = None) -> bytes:
        """tick_record: the canonical TickRecord whose encode() produced `body` in this
        call path. Its TICK chain rules are checked here instead of decoding `body` again
        (TickRecord.decode rebuilds and revalidates every PageRef the record just encoded)."""
        payload, link = chained_payload(self.chain.link, fourcc, body)
        # Validate against a detached candidate. The visible chain remains at
        # its accepted boundary if writing or the required durability barrier
        # fails; the poisoned journal prevents an ambiguous retry.
        candidate = copy.copy(self.chain)
        candidate.genesis = list(self.chain.genesis)
        candidate.counts = dict(self.chain.counts)
        if tick_record is not None and fourcc == b"TICK" and _canonical_tick(tick_record):
            dec = tick_record
            # The TICK branch of ChainState.feed, minus the decode and the link
            # recomputation (chained_payload just produced `link` from this body).
            _need(len(candidate.genesis) >= 3 and candidate.h0 is not None,
                  "journal has no complete HOLO, SCOR, ENVP prefix")
            _need(candidate.seal is None, "TICK after SEAL; only RCPT may follow it")
            _need(dec.tick > max(candidate.last_tick, candidate.tick_floor),
                  f"tick {dec.tick} does not follow tick {max(candidate.last_tick, candidate.tick_floor)}")
            _need(dec.epoch >= candidate.last_epoch, f"epoch {dec.epoch} goes backwards")
            candidate.last_tick, candidate.last_epoch = dec.tick, dec.epoch
            candidate.ticks += 1
            candidate.link = link
            candidate.counts["TICK"] = candidate.counts.get("TICK", 0) + 1
        else:
            _b, _l, dec = candidate.feed(fourcc, payload)
        off, _end = self.journal.append_raw(fourcc, payload)
        if sync:
            self.journal.fsync()
        self.chain = candidate
        self._note(off, fourcc, dec, link)
        return link

    def _sync_now(self, flags: TickFlag) -> bool:
        mode = self.store_cfg["fsync"]
        return mode == "tick" or (mode == "keyframe" and bool(flags & _KF))

    def _live_base(self, epoch: int) -> PageMap:
        prev = max(self.chain.last_tick, self.chain.tick_floor)
        if (self.live is not None and prev in (self.live_tick, self.live_through)
                and self.live_epoch == epoch):
            return self.live
        _need(prev >= 0, "the first captured tick must carry EPOCH_START (a full sweep)")
        # capture cadence: the base is the last captured tick <= prev; uncaptured ticks after it hold no pages
        base = prev
        i = self._pos.get(prev)
        while i is not None and i >= 0 and not self.rows[i].flags & _CAP and self.rows[i].epoch == epoch:
            i -= 1
            base = self.rows[i].tick if i >= 0 else self.chain.tick_floor
        try:
            _need(base >= 0 and self.epoch_at(base) == epoch, "a new epoch starts with EPOCH_START (a full sweep)")
            m = self.map_at(base)
        except (FormatError, KeyError) as e:
            raise FormatError(f"map at tick {base} is unknown ({e}); this tick must carry EPOCH_START") from None
        self.live, self.live_tick, self.live_epoch, self.live_through = m, base, epoch, prev
        return m

    def _load_anchors(self) -> dict[int, bytes]:
        if self.anchors is None:
            self.anchors = {}
            b: Bundle | None = self
            limit = None
            while b is not None and not self.anchors:
                for r in reversed(b.rows):
                    if r.flags & _MAT and (limit is None or r.tick <= limit):
                        self.anchors = {p.vaddr: p.key for p in b._tree_map(b.record(r.tick).state_root).refs()}
                        break
                limit, b = b.chain.tick_floor, b.parent
        return self.anchors

    def _page_frames(self, items: Mapping[bytes, tuple[int, bytes]], prev: Mapping[int, bytes]) -> list[Unit]:
        """Frames for new pages. A page's delta base is its previous version at the same vaddr,
        else its page in the last materialized keyframe."""
        anchors = self._load_anchors()
        return self.store.encode_pages(((key, data, (prev.get(vaddr), anchors.get(vaddr)))
                                        for key, (vaddr, data) in items.items()), durable=self._have)

    def _fit(self, need: Callable[[], int], areas: set[int], thin: Callable[[], bool] | None = None,
             keep: Iterable[bytes] = ()) -> None:
        """Apply store.on_budget_steps in order until the next write fits; never write past the budget."""
        if self.usage() + need() <= self.budget:
            return
        steps = list(self.store_cfg["on_budget_steps"])
        for step in steps:
            if step == "halt":
                break
            if step == "spill":
                self._spill(areas, need())
            elif step == "evict_detached":
                self._evict(set(keep))
            elif step == "thin" and thin is not None:
                thin()
            if self.usage() + need() <= self.budget:
                return
        entry = {"step": "halt", "tick": self.chain.last_tick, "usage": self.usage(), "need": need(),
                 "budget": self.budget, "steps": steps}
        self.budget_log.append(entry)
        raise BudgetError(f"store budget of {self.budget} bytes: {entry['usage']} used, {entry['need']} more "
                          f"needed, and the steps {steps} did not make room")

    def _spill(self, areas: set[int], need: int) -> None:
        areas = {a for a in areas if not self.store.current_spilled(a)}
        for p in self.store_cfg["spill_paths"]:
            base = Path(os.path.expanduser(p))
            try:
                if shutil.disk_usage(base).free < need + (64 << 20):
                    continue
            except OSError:
                continue
            for area in sorted(areas):
                target = base / self.bundle_id / AREA_DIRS[area]
                n = self.store.roll(area, target)
                entry = {"area": AREA_DIRS[area], "pack": n, "path": str(target.resolve()), "tick": self.chain.last_tick}
                meta_append(self.root, "spills", entry)
                self.budget_log.append({"step": "spill", **entry})
            return
        self.budget_log.append({"step": "spill", "skipped": "no spill path with room"})

    def _evict(self, extra: set[bytes]) -> None:
        """Drop page payloads no keyframe map, live map, branch point or pending write needs.

        Records a chained MARK `evicted:<lo>-<hi>` naming the ticks that lost payloads, and a meta.json entry.
        """
        if self.store_cfg["detached_payload"] != "evictable":
            self.budget_log.append({"step": "evict_detached", "skipped": "store.detached_payload is keep"})
            return
        keep = set(self.pending) | extra
        if self.live is not None:
            keep |= self.live.keys()
        for r in self.rows:
            if r.flags & _KF:
                keep |= self._tree_map(self.record(r.tick).state_root).keys()
        self._pins = None
        keep |= self._pinned()
        local = {(a, p) for (a, p) in list_packs(self.root)
                 if a == AREA_PAGES and not pack_path(self.root, a, p).is_symlink()}
        base_of = self.store.bases()
        for k, l in self.store.locs.items():  # frames that stay as they are keep their whole XOR chains
            if l.kind == ObjKind.PAGE_X and (l.area, l.pack) not in local:
                b = base_of.get(k)
                while b is not None:
                    keep.add(b)
                    b = base_of.get(b)
        drop = {k for k, l in self.store.locs.items()
                if l.kind in PAGE_KINDS and (l.area, l.pack) in local and k not in keep}
        if not drop:
            self.budget_log.append({"step": "evict_detached", "skipped": "nothing detached"})
            return
        hit = [r.tick for r in self.rows if r.flags & _CAP and any(p.key in drop for p in self.record(r.tick).pages)]
        freed, frames = self.store.compact(drop, local, base_of)
        self._compactions += 1
        name = f"{EVICT_MARK}{hit[0]}-{hit[-1]}" if hit else f"{EVICT_MARK}unreferenced"
        at = max(self.chain.last_tick, 0)
        self._append(b"MARK", mark_body(name, at), sync=True)
        entry = {"mark": name, "tick": at, "ticks": [hit[0], hit[-1]] if hit else None,
                 "objects": len(drop), "frames": frames, "bytes": freed}
        meta_append(self.root, "evictions", entry)
        self.budget_log.append({"step": "evict_detached", **entry})

    def append_tick(self, rec: TickRecord, pages: Mapping[int, bytes] | None = None) -> tuple[TickRecord, bytes]:
        """Commit one tick. `pages` maps vaddr -> 4096 bytes for refs whose payload is not stored yet.

        Checks state_root against map(T); adds KEYFRAME and MATERIALIZED where 8.5 and 8.8 require
        them, and EVICTED when the budget's `thin` step withheld this tick's payloads. Objects are
        written (and fsynced per store.fsync) before the TICK chunk, which is the commit point.
        Returns the committed record and its chain link.

        The existing capture.gl_profile switch also exposes one bounded host sample of this
        call's phases as last_append_tick_profile; timings never change admission or barriers.
        """
        if not self.cfg.get("capture", {}).get("gl_profile", False):
            self._tick_prof = self.last_append_tick_profile = self.store.encode_profile = None
            return self._append_tick(rec, pages)
        prof = {"schema": "kptas-append-tick-phases/1", "complete": False, "refs": len(rec.pages),
                "new_pages": 0, "_t": time.perf_counter_ns()}
        started = prof["_t"]
        self._tick_prof, self.last_append_tick_profile, self.store.encode_profile = prof, prof, {}
        try:
            out = self._append_tick(rec, pages)
            prof["complete"] = True
            return out
        finally:
            prof["total_ns"] = time.perf_counter_ns() - started
            prof["encode_pages"] = dict(self.store.encode_profile or {})
            del prof["_t"]
            self._tick_prof = self.store.encode_profile = None

    def _lap(self, name: str) -> None:
        p = self._tick_prof
        if p is not None:
            now = time.perf_counter_ns()
            p[name] = p.get(name, 0) + now - p["_t"]
            p["_t"] = now

    def _append_tick(self, rec: TickRecord, pages: Mapping[int, bytes] | None) -> tuple[TickRecord, bytes]:
        self._check_writable()
        rec.validate()
        prev_tick = max(self.chain.last_tick, self.chain.tick_floor)
        _need(rec.tick > prev_tick, f"tick {rec.tick} does not follow tick {prev_tick}; continue in a branch")
        _need(rec.epoch >= self.chain.last_epoch, f"epoch {rec.epoch} goes backwards")
        flags = TickFlag(rec.flags)
        pages = dict(pages or {})
        if rec.flags & TickFlag.FILES:
            _need(self.store.has(rec.files_root), "files_root is not in the files store; put the tree first")
        self._lap("check_ns")
        if not flags & _CAP:
            _need(not pages and not flags & (_KF | _MAT), "an uncaptured tick has no pages or keyframe")
            body = rec.encode()
            self._fit(lambda: len(body) + 74, set())
            link = self._append(b"TICK", body, sync=self._sync_now(flags), tick_record=rec)
            self._lap("journal_ns")
            # Capture cadence: the live map still holds for the next captured tick of this epoch.
            if (self.live is not None and prev_tick in (self.live_tick, self.live_through)
                    and rec.epoch == self.live_epoch and not flags & TickFlag.EPOCH_START):
                self.live_through = rec.tick
            else:
                self.live = None
            return rec, link

        epoch_start = bool(flags & TickFlag.EPOCH_START)
        live = None if epoch_start else self._live_base(rec.epoch)
        if live is None:
            new_map, undo = PageMap(r for r in rec.pages if not r.flags & PageFlag.DECOMMIT), None
        else:
            new_map, undo = live, live.apply(rec.pages)
        self._lap("map_apply_ns")
        try:
            return self._commit_captured(rec, flags, pages, new_map, undo, epoch_start)
        except BaseException:
            if undo is not None:
                live.revert(undo)  # type: ignore[union-attr]
            raise

    def _commit_captured(self, rec: TickRecord, flags: TickFlag, pages: dict[int, bytes], new_map: PageMap,
                         undo: list | None, epoch_start: bool) -> tuple[TickRecord, bytes]:
        from kptas._verified_page import _verified_page_payload
        root, rootp, leaves = new_map.tree()
        _need(root == rec.state_root, f"tick {rec.tick}: state_root does not match map(T)")
        self._lap("tree_ns")
        mode = self.store_cfg["pages"]
        ke = self.header.keyframe_every
        if epoch_start or rec.reached in (Reached.RESTORE, Reached.HYBRID) or (ke > 0 and rec.tick % ke == 0):
            flags |= _KF  # 8.5: every keyframe_every ticks, every EPOCH_START and after every restore
        if flags & _MAT or ((epoch_start or rec.tick % self.materialize_every == 0) and mode != "digests"):
            flags |= _MAT | _KF
        _need(not (flags & _MAT and mode == "digests"), "store.pages = digests keeps no payloads to materialize")
        prev = {v: old.key for v, old in (undo or ()) if old is not None}

        new: dict[bytes, tuple[int, bytes]] = {}
        for r in rec.pages:
            if r.flags & PageFlag.DECOMMIT or r.key == ZERO_PAGE_KEY:
                discarded = pages.pop(r.vaddr, None)
                if discarded is not None:
                    try:
                        _verified_page_payload(discarded, r.vaddr, r.key)
                    except ValueError as exc:
                        raise FormatError(str(exc)) from None
                continue
            data = pages.pop(r.vaddr, None)
            if data is not None:
                try:
                    verified = _verified_page_payload(data, r.vaddr, r.key)
                except ValueError as exc:
                    raise FormatError(str(exc)) from None
                if verified is None:
                    _need(len(data) == PAGE_SIZE and page_key(data) == r.key, f"payload for {r.vaddr:#x} does not match its key")
                else:
                    data = verified
            if mode == "digests" or r.key in new or r.key in self.pending or self._have(r.key):
                continue
            if data is None:
                _need(self.store.has(r.key), f"tick {rec.tick}: no payload for page {r.vaddr:#x}")
                data = self.store.get(r.key)  # held by a parent that may evict it: keep a copy here
            new[r.key] = (r.vaddr, data)
        _need(not pages, f"payloads given for pages without refs: {sorted(hex(v) for v in pages)}")
        if self._tick_prof is not None:
            self._tick_prof["new_pages"] = len(new)
        self._lap("admit_ns")

        defer = mode == "keyframes" or self.thinning
        now: dict[bytes, tuple[int, bytes]] = {} if defer else dict(new)
        held: dict[bytes, tuple[int, bytes]] = dict(new) if defer else {}
        pend_bytes = len(self.pending) * PAGE_SIZE
        if flags & _KF or (held and pend_bytes > self.ram_limit):
            need_keys = new_map.keys()
            for k, (v, d) in list(held.items()):
                if k in need_keys:
                    now[k] = held.pop(k)
            for k, vd in self.pending.items():
                if k in need_keys and k not in now:
                    now[k] = vd
        if self.thinning and held:
            flags |= TickFlag.EVICTED  # held payloads that a later tick supersedes never reach the store

        def frames() -> list[Unit]:
            # a materialized pack holds the tree and every page of the map, so its tick writes neither to pages/
            return [] if flags & _MAT else self._page_frames(now, prev)

        state = {"flags": flags, "frames": frames()}
        self._lap("frames_ns")
        tree = [] if not flags & _KF or flags & _MAT else \
            [self.store.unit(ObjKind.LEAF, k, p) for k, p in leaves if not self._have(k)]
        if flags & _KF and not flags & _MAT and not self._have(root):
            tree.append(self.store.unit(ObjKind.ROOT, root, rootp))

        def payload(k: bytes) -> bytes:
            hit = new.get(k) or now.get(k) or self.pending.get(k)
            return hit[1] if hit else self.store.get(k)

        kf = self.store.kf_bytes(new_map, payload) if flags & _MAT else b""
        encoded_tick = rec.encode()
        tick_len = len(encoded_tick) + 74
        # Keep reuse private to canonical immutable records. Public subclasses
        # or custom scalar/bytes types retain their original second encode.
        reusable_tick = _canonical_tick(rec)
        self._lap("tree_units_kf_encode_ns")

        def need() -> int:
            spilled = self.store.current_spilled(AREA_PAGES)
            objs = 0 if spilled else sum(len(u.blob) for u in state["frames"]) + sum(len(u.blob) for u in tree)
            return objs + len(kf) + tick_len

        def thin() -> bool:
            if flags & _KF or not state["frames"]:
                return False
            for k, (v, d) in now.items():
                held[k] = (v, d)
            now.clear()
            state["frames"] = []
            state["flags"] |= TickFlag.EVICTED
            if not self.thinning:
                self.thinning = self.store.evictions = True
                meta_append(self.root, "thinning", {"from_tick": rec.tick})
            self.store.availability(set(held), True)
            self.budget_log.append({"step": "thin", "tick": rec.tick, "withheld": len(held)})
            return True

        n = self._compactions
        self._fit(need, {AREA_PAGES}, thin, keep=prev.values())
        while n != self._compactions:  # eviction may have dropped or re-encoded the bases chosen above
            n = self._compactions
            state["frames"] = frames()
            self._fit(need, {AREA_PAGES}, thin, keep=prev.values())
        self._lap("fit_ns")
        flags = state["flags"]
        sync = self._sync_now(flags)
        self.store.append(AREA_PAGES, state["frames"] + tree)
        if kf:
            self.store.write_kf(rec.tick, kf, fsync=sync)
        if sync:
            self.store.fsync()
        # Covers both the first budget transition and all later deferred ticks.
        # A later flush revokes these exact keys before writing their bytes.
        self.store.availability({k for k in held if k not in self.store.locs}, True)
        self._lap("store_write_ns")
        final = dataclasses.replace(rec, flags=flags) if flags != rec.flags else rec
        # Budget policy may change the final flags. That case still validates
        # and encodes the final record after all original store barriers.
        body = encoded_tick if reusable_tick and final is rec else final.encode()
        link = self._append(b"TICK", body, sync=sync, tick_record=final)
        self._lap("journal_ns")

        self.live, self.live_tick, self.live_epoch, self.live_through = new_map, rec.tick, rec.epoch, rec.tick
        if flags & _MAT:
            self.anchors = {r.vaddr: r.key for r in new_map.refs() if r.key != ZERO_PAGE_KEY}
        self.pending.update(held)
        if self.pending:
            live_keys = new_map.keys()
            for k in [k for k in self.pending if k not in live_keys or k in self.store.locs]:
                del self.pending[k]
        return final, link

    def _flush_pending(self) -> None:
        """Store held payloads that the live map still needs (on close and seal)."""
        if not self.pending:
            return
        items = {k: vd for k, vd in self.pending.items() if not self._have(k)}
        frames = self._page_frames(items, {})
        self._fit(lambda: sum(len(u.blob) for u in frames), {AREA_PAGES})
        self.store.append(AREA_PAGES, frames)
        self.store.fsync()
        self.pending.clear()

    # ---------------------------------------------------------------- files store

    def put_file(self, data: bytes) -> bytes:
        """Store one file blob; returns its key, sha256 of the content."""
        self._check_writable()
        key = sha256(data)
        if not self.store.has(key):
            unit = self.store.unit(ObjKind.FILE_BLOB, key, data)
            self._fit(lambda: len(unit.blob), {AREA_FILES})
            self.store.append(AREA_FILES, [unit])
        return key

    def put_files(self, blobs: Iterable[tuple[str, bytes]]) -> None:
        """Store immutable blobs; optional timings do not change admission or barriers.

        The existing capture.gl_profile switch also exposes one bounded host
        sample as last_put_files_profile. Worker wall times overlap and must
        not be added to the enclosing total_wall_ns.
        """
        profile = None
        if self.cfg.get("capture", {}).get("gl_profile", False):
            import time
            profile = {"schema": "kptas-put-files-phases/1", "complete": False,
                       "input_blobs": 0, "input_bytes": 0,
                       "pending_blobs": 0, "pending_bytes": 0,
                       "encoded_blobs": 0, "encoded_bytes": 0,
                       "admit_wall_ns": 0, "admit_cpu_ns": 0,
                       "encode_worker_wall_ns": 0, "encode_worker_cpu_ns": 0,
                       "encode_wait_wall_ns": 0,
                       "budget_wall_ns": 0, "budget_cpu_ns": 0,
                       "append_wall_ns": 0, "append_cpu_ns": 0,
                       "total_wall_ns": 0, "total_caller_cpu_ns": 0}
            started, cpu_started = time.perf_counter_ns(), time.thread_time_ns()
        self.last_put_files_profile = profile
        try:
            self._put_files(blobs, profile)
            if profile is not None:
                profile["complete"] = True
        finally:
            if profile is not None:
                profile["total_wall_ns"] = time.perf_counter_ns() - started
                profile["total_caller_cpu_ns"] = time.thread_time_ns() - cpu_started

    def _put_files(self, blobs: Iterable[tuple[str, bytes]], profile: dict | None) -> None:
        """Admit content-addressed blobs, compress in bounded groups, append in order.

        Every digest is checked against fresh input before any write. Only pure
        frame encoding runs on workers; budget decisions and pack writes remain
        serial. A failure propagates before the caller can commit its tick.
        """
        from concurrent.futures import ThreadPoolExecutor
        from kptas._verified_blob import _verified_payload
        if profile is not None:
            import time
            from threading import Lock
            encode_profile_lock = Lock()

        def timed(phase, fn, *args):
            if profile is None:
                return fn(*args)
            stamp, cpu = time.perf_counter_ns(), time.thread_time_ns()
            try:
                return fn(*args)
            finally:
                profile[phase + "_wall_ns"] += time.perf_counter_ns() - stamp
                profile[phase + "_cpu_ns"] += time.thread_time_ns() - cpu

        self._check_writable()
        pending = []
        seen = set()
        if profile is not None:
            admit_started, admit_cpu = time.perf_counter_ns(), time.thread_time_ns()
        for item in blobs:
            try:
                verified = _verified_payload(item)
            except ValueError as exc:
                raise FormatError(str(exc)) from None
            if verified is None:
                digest, data = item
                key = sha256(data)
                _need(key.hex() == digest, f"blob {digest[:16]} does not match its sha256")
            else:
                key, data = verified
            if profile is not None:
                profile["input_blobs"] += 1
                profile["input_bytes"] += len(data)
            if key not in seen and not self.store.has(key):
                pending.append((key, data))
                seen.add(key)
        if profile is not None:
            profile["admit_wall_ns"] = time.perf_counter_ns() - admit_started
            profile["admit_cpu_ns"] = time.thread_time_ns() - admit_cpu
            profile["pending_blobs"] = len(pending)
            profile["pending_bytes"] = sum(len(data) for _, data in pending)

        def encode(item):
            key, data = item
            if profile is None:
                return self.store.unit(ObjKind.FILE_BLOB, key, data)
            stamp, cpu = time.perf_counter_ns(), time.thread_time_ns()
            try:
                unit = self.store.unit(ObjKind.FILE_BLOB, key, data)
                with encode_profile_lock:
                    profile["encoded_blobs"] += 1
                    profile["encoded_bytes"] += len(unit.blob)
                return unit
            finally:
                elapsed, cpu_elapsed = time.perf_counter_ns() - stamp, time.thread_time_ns() - cpu
                with encode_profile_lock:
                    profile["encode_worker_wall_ns"] += elapsed
                    profile["encode_worker_cpu_ns"] += cpu_elapsed

        def append(unit):
            timed("budget", self._fit, lambda: len(unit.blob), {AREA_FILES})
            timed("append", self.store.append, AREA_FILES, [unit])

        def append_group(units):
            # Reserve the complete bounded encoding group against one fresh
            # usage observation, then flush its ordered frames together. The
            # exclusive bundle writer cannot append between this admission and
            # Store.append. Keep the original per-object budget/spill path when
            # the group does not fit: it may admit the first object and spill or
            # refuse the next, and that failure ordering must remain unchanged.
            need = sum(len(unit.blob) for unit in units)
            if timed("budget", self.usage) + need <= self.budget:
                timed("append", self.store.append, AREA_FILES, units)
            else:
                for unit in units:
                    append(unit)

        # Do not dispatch tiny records or one oversized object. The two workers
        # bound encodings in flight; a group holds at most 32 MiB of source, with
        # oversized blobs handled synchronously. Groups are bounded by bytes, not
        # by count: the GL spool interleaves [image, descriptor] pairs, and a
        # two-item group then held one large image, so images never overlapped.
        # Units are still appended in pending order; every frame is independent
        # (FLUSH_FRAME codecs), so the pack bytes are unchanged.
        if len(pending) < 2 or sum(len(data) for _, data in pending) < 256 * 1024:
            for item in pending:
                append(encode(item))
            return
        if self._file_encoder is None:
            self._file_encoder = ThreadPoolExecutor(max_workers=2, thread_name_prefix="holo-compress")
        pool = self._file_encoder
        group = []
        size = 0

        def flush():
            # Resolve the entire group before making a write. Never touch
            # the store from a worker or acknowledge a failed group.
            stamp = time.perf_counter_ns() if profile is not None else None
            try:
                units = list(pool.map(encode, group))
            except BaseException:
                # Keep the original per-call failure boundary: no encoder from
                # this failed group may outlive its refusal or store closure.
                self._file_encoder = None
                pool.shutdown(wait=True)
                raise
            finally:
                if profile is not None:
                    profile["encode_wait_wall_ns"] += time.perf_counter_ns() - stamp
            append_group(units)

        for item in pending:
            n = len(item[1])
            if group and size + n > 32 * 1024 * 1024:
                flush()
                group, size = [], 0
            if n > 32 * 1024 * 1024:
                append(encode(item))
            else:
                group.append(item)
                size += n
        if group:
            flush()

    def put_file_tree(self, entries: Iterable[tuple[str, bytes | str, int, int]]) -> bytes:
        """Store a FILE_TREE of (path, sha256, size, vtime_100ns); returns its key, the files_root."""
        self._check_writable()
        rows = []
        for path, digest, size, vtime in entries:
            h = digest.hex() if isinstance(digest, bytes) else digest
            rows.append([path, h, size, vtime])
        rows, _ = validate_rows(rows, lambda h: self.file(bytes.fromhex(h)))
        payload = canonical_json({"entries": rows})
        key = sha256(payload)
        if not self.store.has(key):
            unit = self.store.unit(ObjKind.FILE_TREE, key, payload)
            self._fit(lambda: len(unit.blob), {AREA_FILES})
            self.store.append(AREA_FILES, [unit])
        return key

    def file(self, key: bytes) -> bytes:
        return self.store.get(key)

    def file_tree(self, root: bytes) -> list[list[Any]]:
        return decode_tree(self.store.get(root))

    # ---------------------------------------------------------------- other chunks

    def mark(self, name: str, tick: int | None = None) -> bytes:
        self._check_writable()
        _need(not name.startswith(EVICT_MARK), f"mark names starting {EVICT_MARK!r} are reserved for eviction")
        return self._append(b"MARK", mark_body(name, max(self.chain.last_tick, 0) if tick is None else tick))

    def end_epoch(self, epoch: int, tick: int, reason: str, exit_status: int | None = None,
                  files_root: bytes = NULL_DIGEST) -> bytes:
        self._check_writable()
        if files_root != NULL_DIGEST:
            _need(self.store.has(files_root), "EPOC files_root is not in the files store")
        self.store.fsync()
        return self._append(b"EPOC", epoch_body(epoch, tick, reason, exit_status, files_root), sync=True)

    def receipt(self, r: Receipt) -> bytes:
        """RCPT is the one chunk allowed after SEAL (seeks and verification of a sealed run)."""
        self._check_writable(allow_sealed=True)
        link = self._append(b"RCPT", r.to_payload(), sync=True)
        if r.micros:
            meta_append(self.root, "receipt_micros", {"link": link.hex(), "micros": r.micros})
        return link

    def append_seek_receipt(self, r: Receipt) -> bytes:
        """Append to an admitted reader under the journal's normal exclusive writer lock.

        No store rebuild or index rewrite: the old index remains a valid prefix.
        Any non-receipt change since admission refuses before writing.
        """
        if self.writable:
            return self.receipt(r)
        _need(self._ready and not self.journal.f.closed, "recording reader is closed")
        _need(fcntl is not None, "receipt append requires an exclusive journal lock")
        before = self._reuse_snapshot()
        prior = self._admitted_files
        unchanged_payloads = (prior is not None and before[:3] + before[4:] == prior[:3] + prior[4:])
        with_writer = Journal(self.root / "journal", writable=True)
        try:
            old = os.fstat(self.journal.f.fileno())
            new = os.fstat(with_writer.f.fileno())
            _need((old.st_dev, old.st_ino) == (new.st_dev, new.st_ino), "recording journal was replaced")
            fourcc, payload = with_writer.read(self.tail_offset)
            link = split_chained(payload)[1] if fourcc in CHAINED_TYPES else self.chain.h0
            end = self.tail_offset + CHUNK_HEAD_SIZE + len(payload) + CHUNK_TRAILER_SIZE
            _need(link == self.chain.link and end == self.valid_len, "admitted journal tail changed")
            candidate, offsets, _tail, _validated_end = self._receipt_suffix(with_writer, self.chain, self.valid_len)
            payload, link = chained_payload(candidate.link, b"RCPT", r.to_payload())
            candidate.feed(b"RCPT", payload)
            off, end = with_writer.append_raw(b"RCPT", payload)
            with_writer.fsync()
            self.chain = candidate
            self.receipt_offsets.extend(offsets + [off])
            self.tail_offset, self.valid_len = off, end
            if r.micros:
                meta_append(self.root, "receipt_micros", {"link": link.hex(), "micros": r.micros})
            self._admitted_files = self._reuse_snapshot() if unchanged_payloads else None
            return link
        finally:
            with_writer.close()

    def seal(self, computed_tier: str | Tier, publishable: bool) -> bytes:
        self._check_writable()
        self._flush_pending()
        self.store.fsync()
        return self._append(b"SEAL", seal_body(computed_tier, publishable, self.chain.ticks, self.chain.link), sync=True)

    # ---------------------------------------------------------------- audit

    def audit(self, deep: bool = False) -> dict[str, Any]:
        """Chain audit of the journal; with deep=True also every captured root, keyframe and XOR chain."""
        rep = audit_journal(self.root / "journal")
        out: dict[str, Any] = {"ok": rep.ok, "journal": dataclasses.asdict(rep), "problems": []}
        if not rep.ok:
            out["problems"].append(f"journal: {rep.error} at {rep.error_offset}")
        if not deep:
            return out
        probs = out["problems"]
        m: PageMap | None = None
        for i, r in enumerate(self.rows):
            if not r.flags & _CAP:
                m = None
                continue
            rec = self.record(r.tick)
            try:
                if r.flags & TickFlag.EPOCH_START:
                    m = PageMap(p for p in rec.pages if not p.flags & PageFlag.DECOMMIT)
                elif m is None:
                    m = self.map_at(r.tick)
                else:
                    m.apply(rec.pages)
                if m.root() != rec.state_root:
                    probs.append(f"tick {r.tick}: state_root does not match map(T)")
                if r.flags & _KF and self._tree_map(rec.state_root).root() != m.root():
                    probs.append(f"tick {r.tick}: keyframe tree differs from the incremental map")
                if r.flags & _MAT:
                    kroot, refs, _d = self.store.read_kf(r.tick)
                    if kroot != rec.state_root or refs != list(m.refs()):
                        probs.append(f"tick {r.tick}: materialized pack differs from map(T)")
            except (FormatError, KeyError) as e:
                probs.append(f"tick {r.tick}: {e}")
                m = None
        for k, l in list(self.store.locs.items()):
            if l.kind == ObjKind.PAGE_X and (not l.anchored or l.depth > self.store.max_chain):
                probs.append(f"object {k.hex()}: XOR chain anchored={l.anchored} depth={l.depth}")
            try:
                self.store.get(k)
            except (FormatError, KeyError) as e:
                probs.append(f"object {k.hex()}: unreadable ({e})")
        evicted: list[str] = []
        if self.store_cfg["pages"] != "digests":
            for r in self.rows:
                if not r.flags & _KF:
                    continue
                for ref in self._tree_map(self.record(r.tick).state_root).refs():
                    try:
                        self.store.get(ref.key)
                    except EvictedError:
                        evicted.append(f"tick {r.tick}: page {ref.vaddr:#x}")
                    except (FormatError, KeyError) as e:
                        probs.append(f"keyframe {r.tick}: page {ref.vaddr:#x} unreadable ({e})")
        out["evicted"] = evicted
        out["ok"] = not probs
        return out
