"""Holorecord bundle `<name>.holo/` (contract 8): create, open, commit ticks, read maps, audit, budget."""

from __future__ import annotations

import dataclasses
import datetime
import json
import os
import platform
import shutil
import sys
from collections.abc import Callable, Iterable, Iterator, Mapping
from pathlib import Path
from typing import Any

from kptas import config as kconfig
from kptas.holo import index as kindex
from kptas.holo.index import TickRow
from kptas.holo.journal import ChainState, Journal, audit_journal, epoch_body, mark_body, seal_body
from kptas.holo.pages import (AREA_DIRS, AREA_FILES, AREA_PAGES, PAGE_KINDS, EvictedError, PageMap, Store,
                              fsync_dir, list_packs, pack_path, parse_leaf, parse_root)
from kptas.types import (CHAINED_TYPES, CHUNK_HEAD_SIZE, CHUNK_TRAILER_SIZE, NULL_DIGEST, PAGE_SIZE,
                         ZERO_PAGE_KEY, EnvPin, FormatError, Header, ObjKind, PageFlag, Reached, Receipt, Tier,
                         TickFlag, TickRecord, canonical_json, chained_payload, page_key, sha256, split_chained)

try:
    import fcntl
except ImportError:  # not POSIX; meta.json updates are then unlocked
    fcntl = None  # type: ignore[assignment]

META_NAME = "meta.json"
EVICT_MARK = "evicted:"
EVENT_MARK = "event:"       # one MARK per engine event; @mark labels cannot contain ':'
_CAP, _KF, _MAT = TickFlag.CAPTURED, TickFlag.KEYFRAME, TickFlag.MATERIALIZED


class BudgetError(RuntimeError):
    """The store budget cannot take the next commit and no configured step made room."""


def _need(cond: Any, msg: str) -> None:
    if not cond:
        raise FormatError(msg)


def read_meta(root: str | os.PathLike[str]) -> dict[str, Any]:
    try:
        with open(Path(root) / META_NAME, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def _write_meta(root: Path, meta: dict[str, Any]) -> None:
    tmp = root / f"{META_NAME}.tmp{os.getpid()}"
    tmp.write_text(json.dumps(meta, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(tmp, root / META_NAME)


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

    # ---------------------------------------------------------------- lifecycle

    @classmethod
    def create(cls, path: str | os.PathLike[str], header: Header, score: str, env: EnvPin, *,
               config: Mapping[str, Any] | None = None, budget_bytes: int | None = None,
               parent: Bundle | None = None) -> Bundle:
        """Create `<name>.holo/` and commit HOLO, SCOR and ENVP (the chain genesis)."""
        root = Path(path)
        _need(root.name.endswith(".holo") and len(root.name) > 5, "a holorecord directory is named <name>.holo")
        _need((header.parent is None) == (parent is None), "a branch header needs its parent bundle, and only then")
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
                           xor_delta=st["xor_delta"], cache_bytes=st["hot_ring_mb"] << 20,
                           parent=parent.store if parent else None, writable=self.writable)
        meta = read_meta(self.root)
        self.store.evictions = bool(meta.get("evictions") or meta.get("thinning"))

    def close(self) -> None:
        """Store pending payloads, fsync, write the index and release the lock."""
        try:
            if self.writable and self._ready and not self.journal.f.closed:
                try:
                    self._flush_pending()
                except BudgetError:
                    # already marked EVICTED on their ticks; note that they never reached the store
                    entry = {"dropped_at_close": len(self.pending), "tick": self.chain.last_tick}
                    meta_append(self.root, "thinning", entry)
                    self.budget_log.append({"step": "thin", **entry})
                    self.pending.clear()
                self.store.fsync()
                self.journal.fsync()
                self.save_index()
        finally:
            self._ready = False
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

    def _restore(self, state: dict[str, Any], rows: list[TickRow]) -> bool:
        """Adopt an index whose journal and pack sizes still match; False means rebuild."""
        try:
            ok = (state["journal_len"] == self.journal.size()
                  and state["packs"] == {f"{a}:{p}": s for (a, p), s in list_packs(self.root).items()})
            if ok:
                for i, (_off, fourcc, payload, _end) in enumerate(self.journal.scan()):
                    self.chain.feed(fourcc, payload)
                    if i == 2:
                        break
                fourcc, payload = self.journal.read(state["tail"])
                ch = state["chain"]
                link = split_chained(payload)[1] if fourcc in CHAINED_TYPES else self.chain.h0
                # the tail chunk must end the file: an index saved over a torn tail would hide it from the writer
                end = state["tail"] + CHUNK_HEAD_SIZE + len(payload) + CHUNK_TRAILER_SIZE
                ok = (self.chain.h0 is not None and link is not None and link.hex() == ch["link"]
                      and end == self.journal.size())
        except (FormatError, KeyError, TypeError, ValueError):
            ok = False
        if not ok:
            self.chain = ChainState()
            return False
        c = self.chain
        c.link, c.ticks, c.last_tick, c.last_epoch = bytes.fromhex(ch["link"]), ch["ticks"], ch["last_tick"], ch["last_epoch"]
        c.seal, c.counts = ch["seal"], ch["counts"]
        self.rows = rows
        self._pos = {r.tick: i for i, r in enumerate(rows)}
        self.marks, self.epochs, self.receipt_offsets = state["marks"], state["epochs"], state["receipts"]
        self.valid_len = state["journal_len"]
        self.tail_offset = state["tail"]
        return True

    def save_index(self) -> None:
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
        return self.chain.score  # type: ignore[return-value]

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
        return TickRecord.from_chunk(payload)[0]

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
            _need(self._inherited(tick), f"no tick {tick}")
            return self.parent.map_at(tick)  # type: ignore[union-attr]
        i, todo = self._pos[tick], []
        while True:
            r = self.rows[i]
            _need(r.flags & _CAP, f"tick {r.tick} is not captured, so map({tick}) is unknown")
            _need(r.epoch == row.epoch, f"no keyframe in epoch {row.epoch} at or before tick {tick}")
            if r.flags & _KF:
                base = self._tree_map(self.record(r.tick).state_root)
                break
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
        _need(allow_sealed or self.chain.seal is None, "bundle is sealed; only RCPT may follow SEAL")

    def _append(self, fourcc: bytes, body: bytes, sync: bool = False) -> bytes:
        payload, link = chained_payload(self.chain.link, fourcc, body)
        _b, _l, dec = self.chain.feed(fourcc, payload)
        off, _end = self.journal.append_raw(fourcc, payload)
        self._note(off, fourcc, dec, link)
        if sync:
            self.journal.fsync()
        return link

    def _sync_now(self, flags: TickFlag) -> bool:
        mode = self.store_cfg["fsync"]
        return mode == "tick" or (mode == "keyframe" and bool(flags & _KF))

    def _live_base(self, epoch: int) -> PageMap:
        prev = max(self.chain.last_tick, self.chain.tick_floor)
        if self.live is not None and self.live_tick == prev and self.live_epoch == epoch:
            return self.live
        _need(prev >= 0, "the first captured tick must carry EPOCH_START (a full sweep)")
        try:
            _need(self.epoch_at(prev) == epoch, "a new epoch starts with EPOCH_START (a full sweep)")
            m = self.map_at(prev)
        except (FormatError, KeyError) as e:
            raise FormatError(f"map at tick {prev} is unknown ({e}); this tick must carry EPOCH_START") from None
        self.live, self.live_tick, self.live_epoch = m, prev, epoch
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

    def _page_frames(self, items: Mapping[bytes, tuple[int, bytes]], prev: Mapping[int, bytes]
                     ) -> list[tuple[bytes, bytes, int, bool, int]]:
        anchors = self._load_anchors()
        out = []
        for key, (vaddr, data) in items.items():
            frame, kind, anchored, depth = self.store.encode_page(key, data, (prev.get(vaddr), anchors.get(vaddr)),
                                                                  durable=self._have)
            out.append((key, frame, kind, anchored, depth))
        return out

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
        """
        self._check_writable()
        rec.validate()
        prev_tick = max(self.chain.last_tick, self.chain.tick_floor)
        _need(rec.tick > prev_tick, f"tick {rec.tick} does not follow tick {prev_tick}; continue in a branch")
        _need(rec.epoch >= self.chain.last_epoch, f"epoch {rec.epoch} goes backwards")
        flags = TickFlag(rec.flags)
        pages = dict(pages or {})
        if rec.flags & TickFlag.FILES:
            _need(self.store.has(rec.files_root), "files_root is not in the files store; put the tree first")
        if not flags & _CAP:
            _need(not pages and not flags & (_KF | _MAT), "an uncaptured tick has no pages or keyframe")
            body = rec.encode()
            self._fit(lambda: len(body) + 74, set())
            link = self._append(b"TICK", body, sync=self._sync_now(flags))
            self.live = None
            return rec, link

        epoch_start = bool(flags & TickFlag.EPOCH_START)
        live = None if epoch_start else self._live_base(rec.epoch)
        if live is None:
            new_map, undo = PageMap(r for r in rec.pages if not r.flags & PageFlag.DECOMMIT), None
        else:
            new_map, undo = live, live.apply(rec.pages)
        try:
            return self._commit_captured(rec, flags, pages, new_map, undo, epoch_start)
        except BaseException:
            if undo is not None:
                live.revert(undo)  # type: ignore[union-attr]
            raise

    def _commit_captured(self, rec: TickRecord, flags: TickFlag, pages: dict[int, bytes], new_map: PageMap,
                         undo: list | None, epoch_start: bool) -> tuple[TickRecord, bytes]:
        root, rootp, leaves = new_map.tree()
        _need(root == rec.state_root, f"tick {rec.tick}: state_root does not match map(T)")
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
                pages.pop(r.vaddr, None)
                continue
            data = pages.pop(r.vaddr, None)
            if data is not None:
                _need(len(data) == PAGE_SIZE and page_key(data) == r.key, f"payload for {r.vaddr:#x} does not match its key")
            if mode == "digests" or r.key in new or r.key in self.pending or self._have(r.key):
                continue
            if data is None:
                _need(self.store.has(r.key), f"tick {rec.tick}: no payload for page {r.vaddr:#x}")
                data = self.store.get(r.key)  # held by a parent that may evict it: keep a copy here
            new[r.key] = (r.vaddr, data)
        _need(not pages, f"payloads given for pages without refs: {sorted(hex(v) for v in pages)}")

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
        state = {"flags": flags, "frames": self._page_frames(now, prev)}
        tree = [(k, self.store.frame(ObjKind.LEAF, k, p), ObjKind.LEAF, False, 0)
                for k, p in leaves if not self._have(k)] if flags & _KF else []
        if flags & _KF and not self._have(root):
            tree.append((root, self.store.frame(ObjKind.ROOT, root, rootp), ObjKind.ROOT, False, 0))

        def payload(k: bytes) -> bytes:
            hit = new.get(k) or now.get(k) or self.pending.get(k)
            return hit[1] if hit else self.store.get(k)

        kf = self.store.kf_bytes(new_map, payload) if flags & _MAT else b""
        tick_len = len(rec.encode()) + 74

        def need() -> int:
            spilled = self.store.current_spilled(AREA_PAGES)
            objs = 0 if spilled else sum(len(f[1]) for f in state["frames"]) + sum(len(f[1]) for f in tree)
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
            self.budget_log.append({"step": "thin", "tick": rec.tick, "withheld": len(held)})
            return True

        n = self._compactions
        self._fit(need, {AREA_PAGES}, thin, keep=prev.values())
        while n != self._compactions:  # eviction may have dropped or re-encoded the bases chosen above
            n = self._compactions
            state["frames"] = self._page_frames(now, prev)
            self._fit(need, {AREA_PAGES}, thin, keep=prev.values())
        flags = state["flags"]
        sync = self._sync_now(flags)
        self.store.append(AREA_PAGES, state["frames"] + tree)
        if kf:
            self.store.write_kf(rec.tick, kf, fsync=sync)
        if sync:
            self.store.fsync()
        final = dataclasses.replace(rec, flags=flags) if flags != rec.flags else rec
        link = self._append(b"TICK", final.encode(), sync=sync)

        self.live, self.live_tick, self.live_epoch = new_map, rec.tick, rec.epoch
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
        self._fit(lambda: sum(len(f[1]) for f in frames), {AREA_PAGES})
        self.store.append(AREA_PAGES, frames)
        self.store.fsync()
        self.pending.clear()

    # ---------------------------------------------------------------- files store

    def put_file(self, data: bytes) -> bytes:
        """Store one file blob; returns its key, sha256 of the content."""
        self._check_writable()
        key = sha256(data)
        if not self.store.has(key):
            frame = self.store.frame(ObjKind.FILE_BLOB, key, data)
            self._fit(lambda: len(frame), {AREA_FILES})
            self.store.append(AREA_FILES, [(key, frame, ObjKind.FILE_BLOB, False, 0)])
        return key

    def put_file_tree(self, entries: Iterable[tuple[str, bytes | str, int, int]]) -> bytes:
        """Store a FILE_TREE of (path, sha256, size, vtime_100ns); returns its key, the files_root."""
        self._check_writable()
        rows = []
        for path, digest, size, vtime in entries:
            h = digest if isinstance(digest, str) else digest.hex()
            _need(self.store.has(bytes.fromhex(h)), f"file tree names {path} but its blob is not stored")
            rows.append([path, h, int(size), int(vtime)])
        rows.sort(key=lambda e: e[0])
        _need(len({e[0] for e in rows}) == len(rows), "duplicate path in file tree")
        payload = canonical_json({"entries": rows})
        key = sha256(payload)
        if not self.store.has(key):
            frame = self.store.frame(ObjKind.FILE_TREE, key, payload)
            self._fit(lambda: len(frame), {AREA_FILES})
            self.store.append(AREA_FILES, [(key, frame, ObjKind.FILE_TREE, False, 0)])
        return key

    def file(self, key: bytes) -> bytes:
        return self.store.get(key)

    def file_tree(self, root: bytes) -> list[list[Any]]:
        return json.loads(self.store.get(root))["entries"]

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
