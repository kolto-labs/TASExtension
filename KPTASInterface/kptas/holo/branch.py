"""Branches (contract 8.1): a new bundle that continues a parent after one of its ticks and shares its pages."""

from __future__ import annotations

import dataclasses
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from kptas.holo.bundle import Bundle, meta_append, meta_remove
from kptas.types import EnvPin, Header, ParentRef


def create_branch(parent: Bundle | str | os.PathLike[str], tick: int, path: str | os.PathLike[str], *,
                  score: str | None = None, env: EnvPin | None = None, header: Header | None = None,
                  config: Mapping[str, Any] | None = None, budget_bytes: int | None = None) -> Bundle:
    """Create `path` as a branch of `parent` at `tick`, the last tick the two share.

    HOLO names parent = {holo: parent bundle id, tick, link: parent chain link at tick}. The
    branch stores only later ticks and resolves every other object through the parent. The
    parent's meta.json records the branch point, so its budget eviction keeps that map.
    Score, environment and header default to the parent's.
    """
    owns = not isinstance(parent, Bundle)
    par = Bundle.open(parent, config=config) if owns else parent
    pin = {"path": str(os.fspath(Path(path).resolve())), "tick": tick}
    child: Bundle | None = None
    pin_written = False
    try:
        if par.writable and par._ready:
            par._flush_pending()  # held payloads of the branch-point map must be in the store to be shared
        link = par.link_at(tick)
        base = par.header if header is None else header
        hdr = dataclasses.replace(base, parent=ParentRef(par.bundle_id, tick, link.hex()))
        # Persist the eviction pin before creating a child that may rely on these pages.
        # If the process dies during creation, the conservative pin survives and prevents
        # a later budget pass from evicting the child’s branch-point state.
        meta_append(par.root, "branch_pins", pin)
        pin_written = True
        par._pins = None
        child = Bundle.create(path, hdr, par.score if score is None else score, par.env if env is None else env,
                              config=config, budget_bytes=budget_bytes, parent=par)
    except BaseException:
        if child is not None:
            try:
                child.close()
            except BaseException:
                pass
        if pin_written:
            try:
                meta_remove(par.root, "branch_pins", pin)
            except BaseException:
                # A leftover pin only retains data; it must not hide the original failure.
                pass
        if owns:
            par.close()
        raise
    try:
        meta_append(par.root, "branches", {"path": str(child.root.resolve()), "tick": tick, "holo": child.bundle_id})
    except BaseException as exc:
        try:
            child.close()
        except BaseException as cleanup_error:
            exc.add_note(f"branch child close after registration failure also failed: {cleanup_error!r}")
        # Keep the pin: the child exists and may already have been observed by another process.
        if owns:
            try:
                par.close()
            except BaseException as cleanup_error:
                exc.add_note(f"branch parent close after registration failure also failed: {cleanup_error!r}")
        raise
    child._owns_parent = owns
    try:
        meta_remove(par.root, "branch_pins", pin)
    except Exception:
        # The committed branch entry also pins this map, so a stale temporary pin is safe.
        pass
    par._pins = None
    return child
