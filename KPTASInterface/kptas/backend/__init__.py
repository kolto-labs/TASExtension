"""Backends behind kptas.types.Backend: pysim, simgame, shim and handsoff."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from kptas.types import Backend, BackendError

from .staging import BatchAssembler, RingFull, StagingRing, batches, read_records, write_records

BACKENDS = ("pysim", "simgame", "shim", "handsoff")


def make_backend(cfg: Mapping[str, Any], **kwargs: Any) -> Backend:
    """Instantiate the backend named by a resolved config's run.backend (not attached yet)."""
    name = cfg.get("run", {}).get("backend", "pysim")
    if name == "pysim":
        from .pysim import PySim
        return PySim(**kwargs)
    if name in ("shim", "simgame"):
        from .shim import ShimBackend
        return ShimBackend(simgame=name == "simgame", **kwargs)
    if name == "handsoff":
        from .handsoff import HandsOff
        return HandsOff(**kwargs)
    raise BackendError(f"unknown backend {name!r}; expected one of {', '.join(BACKENDS)}")


def create(name: str, cfg: Mapping[str, Any], link: Any = None) -> Backend:
    """Backend by name; shim and simgame adopt ``link`` (an accepted shim connection) when given."""
    if name in ("shim", "simgame") and link is not None:
        from .shim import ShimBackend
        return ShimBackend(simgame=name == "simgame", launch=False, link=link)
    return make_backend({**cfg, "run": {**cfg.get("run", {}), "backend": name}})


__all__ = ["BACKENDS", "BatchAssembler", "RingFull", "StagingRing", "batches", "create", "make_backend",
           "read_records", "write_records"]
