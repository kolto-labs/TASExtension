"""Branches (contract 8.1): a new bundle that continues a parent after one of its ticks and shares its pages."""

from __future__ import annotations

import dataclasses
import os
from collections.abc import Mapping
from typing import Any

from kptas.holo.bundle import Bundle, meta_append
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
    try:
        if par.writable and par._ready:
            par._flush_pending()  # held payloads of the branch-point map must be in the store to be shared
        link = par.link_at(tick)
        base = par.header if header is None else header
        hdr = dataclasses.replace(base, parent=ParentRef(par.bundle_id, tick, link.hex()))
        child = Bundle.create(path, hdr, par.score if score is None else score, par.env if env is None else env,
                              config=config, budget_bytes=budget_bytes, parent=par)
    except BaseException:
        if owns:
            par.close()
        raise
    child._owns_parent = owns
    meta_append(par.root, "branches", {"path": str(child.root.resolve()), "tick": tick, "holo": child.bundle_id})
    par._pins = None
    return child
