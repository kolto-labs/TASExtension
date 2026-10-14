"""Repack a bundle into the current store format (contract 8.7), then prove the copy holds the same states.

The journal is copied byte for byte, so the bundle id, every chain link and every state root stay
as recorded. Every object the source stores is written again with the destination's codec, batch
size and delta bases. The source is only read.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from kptas.holo.bundle import META_NAME, Bundle, _write_meta, read_meta
from kptas.holo.index import INDEX_NAME, OLD_NAMES
from kptas.holo.pages import (AREA_FILES, AREA_KF, AREA_PAGES, CODECS, PageMap, iter_units, list_packs,
                              pack_path, parse_root, xor_pages)
from kptas.types import ZERO_PAGE_KEY, FormatError, ObjKind, PageFlag, PageRef, TickFlag, page_key, resolve_codec, sha256

_CAP, _KF, _MAT, _START = TickFlag.CAPTURED, TickFlag.KEYFRAME, TickFlag.MATERIALIZED, TickFlag.EPOCH_START


def _need(cond: Any, msg: str) -> None:
    if not cond:
        raise FormatError(msg)


def bundle_bytes(root: str | os.PathLike[str]) -> dict[str, int]:
    """Bytes on disk per part of a bundle; spilled packs count where they live."""
    root = Path(root)
    out = {"journal": (root / "journal").stat().st_size, "pages": 0, "keyframes": 0, "files": 0, "index": 0}
    for (area, pack), size in list_packs(root).items():
        out[{AREA_PAGES: "pages", AREA_FILES: "files", AREA_KF: "keyframes"}[area]] += max(size, 0)
    idx = root / "index"
    if idx.is_dir():
        out["index"] = sum(e.stat().st_size for e in os.scandir(idx) if e.is_file())
    meta = root / META_NAME
    out["meta"] = meta.stat().st_size if meta.is_file() else 0
    out["total"] = sum(out.values())
    return out


def repack(src_path: str | os.PathLike[str], dst_path: str | os.PathLike[str], config: Mapping[str, Any],
           progress: Callable[[str], None] | None = None) -> dict[str, Any]:
    """Write a new-format copy of SRC at DST, then verify it. Returns a report; report["ok"] says
    whether every check passed."""
    dst_root = Path(dst_path)
    _need(dst_root.name.endswith(".holo") and len(dst_root.name) > 5, "a holorecord directory is named <name>.holo")
    _need(not dst_root.exists(), f"{dst_root} exists; repack writes a new bundle")
    src = Bundle.open(src_path, config=config)
    try:
        _need(Path(src_path).resolve() != dst_root.resolve(), "repack never writes over its source")
        if progress:
            progress("verify: source packs and materialized keyframes")
        _validate_source(src)
        t0 = time.perf_counter()
        rep = _copy(src, dst_root, config, progress)
        rep["write_s"] = time.perf_counter() - t0
        t1 = time.perf_counter()
        rep.update(verify_copy(src, dst_root, config, progress))
        rep["verify_s"] = time.perf_counter() - t1
    finally:
        src.close()
    rep["src_bytes"] = bundle_bytes(src_path)
    rep["dst_bytes"] = bundle_bytes(dst_root)
    return rep


def _validate_source(src: Bundle) -> None:
    """Validate every physical frame, including duplicate objects hidden by the index.

    Rebuilding a keyframe from journal refs must not silently repair a corrupt source.
    """
    for (area, pack), size in list_packs(src.root).items():
        path = pack_path(src.root, area, pack)
        _need(size >= 0, f"source pack is missing: {path.name}")
        with open(path, "rb") as f:
            for _off, _end, members in iter_units(f, size, strict=True):
                for kind, key, base, raw, _slot in members:
                    if kind == ObjKind.PAGE_X:
                        raw = xor_pages(raw, src.store.get(base))
                    actual = page_key(raw) if kind in (ObjKind.PAGE_I, ObjKind.PAGE_X) else sha256(raw)
                    _need(actual == key, f"source object {key.hex()} does not match its key")
    for row in src.rows:
        if row.flags & _MAT:
            root, refs, _data = src.store.read_kf(row.tick)
            _need(root == src.record(row.tick).state_root and PageMap(refs).root() == root,
                  f"source keyframe {row.tick} does not match its state_root")


def _copy(src: Bundle, dst_root: Path, config: Mapping[str, Any],
          progress: Callable[[str], None] | None) -> dict[str, Any]:
    dst_root.mkdir()
    for d in ("pages", "keyframes", "files", "index"):
        (dst_root / d).mkdir()
    with open(src.root / "journal", "rb") as f, open(dst_root / "journal", "wb") as out:
        left = src.valid_len   # a torn tail is not part of the record
        while left:
            chunk = f.read(min(left, 1 << 24))
            _need(chunk, "source journal is shorter than its valid prefix")
            out.write(chunk)
            left -= len(chunk)
    meta = read_meta(src.root)
    if meta.get("parent_abspath"):
        meta["parent_path"] = os.path.relpath(meta["parent_abspath"], dst_root.resolve())
    if meta.get("spills"):
        meta["spills_in_source"] = meta["spills"]
    meta["spills"] = []
    meta["repacked_from"] = {"bundle": src.bundle_id, "path": str(src.root.resolve()),
                             "torn_bytes_left_out": src.torn_bytes}
    _write_meta(dst_root, meta)

    dst = Bundle.open(dst_root, writable=True, config=config)
    try:
        st, own = dst.store, src.store.locs
        st.codec = CODECS[resolve_codec(config["store"]["codec"])]   # the header names the codec it was recorded with
        enc = dec = 0.0
        ticks = 0
        live: dict[int, PageRef] | None = None
        anchors = _parent_anchors(src)
        prev_tick = src.chain.tick_floor
        for row in src.rows:
            if not row.flags & _CAP:
                # capture cadence: an uncaptured tick has no pages; the live map (or prev_tick, the last
                # captured tick) carries over to the next captured tick (an EPOCH_START resets it below)
                continue
            rec = src.record(row.tick)
            if rec.flags & _START:
                live = {}
            elif live is None:
                _need(prev_tick >= 0, f"tick {row.tick}: no map before it and no EPOCH_START")
                live = {r.vaddr: r for r in src.map_at(prev_tick).refs()}
            t = time.perf_counter()
            items, seen = [], set()
            for r in rec.pages:
                if r.flags & PageFlag.DECOMMIT or r.key == ZERO_PAGE_KEY or r.key in seen \
                        or r.key in st.locs or r.key not in own:
                    continue
                seen.add(r.key)
                old = live.get(r.vaddr)
                items.append((r.key, src.store.get(r.key), (old.key if old else None, anchors.get(r.vaddr))))
            for r in rec.pages:
                if r.flags & PageFlag.DECOMMIT:
                    live.pop(r.vaddr, None)
                else:
                    live[r.vaddr] = r
            tree = []
            if row.flags & _KF and not row.flags & _MAT and rec.state_root in own:
                for _hi, lk in parse_root(src.store.get(rec.state_root)):
                    if lk in own and lk not in st.locs:
                        tree.append(st.unit(ObjKind.LEAF, lk, src.store.get(lk)))
                if rec.state_root not in st.locs:
                    tree.append(st.unit(ObjKind.ROOT, rec.state_root, src.store.get(rec.state_root)))
            t1 = time.perf_counter()
            dec += t1 - t
            if row.flags & _MAT:
                pmap = PageMap(live.values())
                _need(pmap.root() == rec.state_root, f"tick {row.tick}: rebuilt map does not match its state_root")
                st.write_kf(row.tick, st.kf_bytes(pmap, src.store.get), fsync=False)
                anchors = {r.vaddr: r.key for r in live.values() if r.key != ZERO_PAGE_KEY}
            else:
                st.append(AREA_PAGES, st.encode_pages(items, durable=dst._have) + tree)
            enc += time.perf_counter() - t1
            prev_tick = row.tick
            ticks += 1
            if progress and ticks % 500 == 0:
                progress(f"repack: tick {row.tick}, {len(st.locs)} objects")
        t = time.perf_counter()
        # what no tick reached: file blobs and trees, and anything else the source holds
        rest = sorted((l.area, l.pack, l.offset, l.slot, k) for k, l in own.items() if k not in st.locs)
        pages = [(k, src.store.get(k), ()) for a, _p, _o, _s, k in rest
                 if a != AREA_FILES and own[k].kind in (ObjKind.PAGE_I, ObjKind.PAGE_X)]
        st.append(AREA_PAGES, st.encode_pages(pages, durable=dst._have))
        for a, _p, _o, _s, k in rest:
            kind = ObjKind(own[k].kind)
            if kind not in (ObjKind.PAGE_I, ObjKind.PAGE_X):
                st.append(AREA_FILES if a == AREA_FILES else AREA_PAGES, [st.unit(kind, k, src.store.get(k))])
        enc += time.perf_counter() - t
        st.fsync()
        return {"ticks_written": ticks, "objects": len(own), "unreached": len(rest),
                "encode_s": enc, "decode_s": dec}
    finally:
        dst.close()


def _parent_anchors(b: Bundle) -> dict[int, bytes]:
    """Pages of the last materialized keyframe a branch inherits, as the writer's anchors."""
    p, limit = b.parent, b.chain.tick_floor
    while p is not None:
        for r in reversed(p.rows):
            if r.flags & _MAT and r.tick <= limit:
                return {x.vaddr: x.key for x in p._tree_map(p.record(r.tick).state_root).refs()
                        if x.key != ZERO_PAGE_KEY}
        limit, p = p.chain.tick_floor, p.parent
    return {}


def verify_copy(src: Bundle, dst_root: Path, config: Mapping[str, Any],
                progress: Callable[[str], None] | None = None) -> dict[str, Any]:
    """Check DST against SRC: the same journal bytes, the same object keys, every object decoding
    to its key, and (deep audit) every captured tick's map rebuilding its state root."""
    problems: list[str] = []
    n = src.valid_len
    with open(src.root / "journal", "rb") as a, open(dst_root / "journal", "rb") as b:
        same = True
        while n:
            x, y = a.read(min(n, 1 << 24)), b.read(min(n, 1 << 24))
            if x != y:
                same = False
                break
            n -= len(x)
        same = same and not b.read(1)
    if not same:
        problems.append("journal bytes differ from the source")
    for name in (INDEX_NAME, *OLD_NAMES):   # read the copy from its packs alone
        try:
            (dst_root / "index" / name).unlink()
        except FileNotFoundError:
            pass
    dst = Bundle.open(dst_root, config=config)
    try:
        src_keys, dst_keys = set(src.store.locs), set(dst.store.locs)
        if src_keys != dst_keys:
            problems.append(f"object keys differ: {len(src_keys - dst_keys)} only in the source, "
                            f"{len(dst_keys - src_keys)} only in the copy")
        if progress:
            progress(f"verify: deep audit of {len(dst.rows)} ticks and {len(dst_keys)} objects")
        audit = dst.audit(deep=True)
        problems.extend(audit["problems"][:20])
        roots = sum(1 for r in dst.rows if r.flags & _CAP)
        same_roots = all(src.record(r.tick).state_root == dst.record(r.tick).state_root for r in dst.rows)
        if not same_roots:
            problems.append("a TICK state_root differs from the source")
        if dst.bundle_id != src.bundle_id:
            problems.append("bundle id differs from the source")
        kinds = {"batched": sum(1 for l in dst.store.locs.values() if l.slot >= 0),
                 "xor": sum(1 for l in dst.store.locs.values() if l.kind == ObjKind.PAGE_X),
                 "max_depth": max((l.depth for l in dst.store.locs.values()), default=0)}
    finally:
        dst.close()
    # leave the copy with an index, as any writer would
    Bundle.open(dst_root, writable=True, config=config).close()
    return {"ok": not problems, "problems": problems, "journal_identical": same, "ticks": len(dst.rows),
            "roots_checked": roots, "objects_checked": len(dst_keys), "audit_ok": audit["ok"],
            "evicted": len(audit.get("evicted", [])), **kinds}


__all__ = ["bundle_bytes", "repack", "verify_copy"]
