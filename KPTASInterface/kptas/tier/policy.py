"""Computed tier, publishability and judge replay (contract 4.1, 4.3).

The effective tier is the minimum of the declared tier, the backend ceiling, the
module-set ceiling, VerbClass.ceiling() over every tick, and the last judge
receipt. It is a one-way ratchet over the tick log: a tick can only lower it,
and only a new passing judge receipt can lift a failed judge's cap.
"""

from __future__ import annotations

import base64
import dataclasses
import sys
import time
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any

from kptas.holo import verify as hv
from kptas.holo.bundle import Bundle, read_meta
from kptas.holo.pages import PageMap
from kptas.tier.envpin import PYSIM_EXE, pin_env
from kptas.types import (AUTHORING_VERBS, NULL_DIGEST, EnvPin, FormatError, Receipt, SeekMode, Tier, TickFlag,
                         TickRecord, VerbClass, VerifyLevel, canonical_json, sha256)

BACKEND_CEILING = {"pysim": Tier.TAS, "simgame": Tier.TAS, "shim": Tier.TAS, "handsoff": Tier.HANDS_OFF}


def _label(t: Tier | None) -> str:
    return "-" if t is None else t.label


def backend_of(env: EnvPin) -> str:
    return env.extra.get("backend") or ("pysim" if env.exe_sha256 == PYSIM_EXE else "shim")


def _module_class(name: str) -> str:
    n = name.rsplit("/", 1)[-1]
    if n in ("mss32.dll",):
        return "mss32"
    if n in ("binkw32.dll", "binkw32_real.dll"):
        return "binkw32"
    if n.endswith((".asi", ".m3d", ".flt", ".mix")):
        return "miles_providers"
    if n == "dinput8.dll":
        return "kptas_shim"
    return "other"


def module_ceiling(env: EnvPin, allowlist: list[str]) -> tuple[Tier, list[str]]:
    """lab when a game-side module falls outside shim.module_allowlist (C9), else tas-or-better."""
    foreign = [m for m in env.modules if _module_class(m.split("@", 1)[0]) not in allowlist]
    return (Tier.LAB if foreign else Tier.HANDS_OFF), foreign


def all_records(b: Bundle) -> Iterator[TickRecord]:
    """Every tick on the bundle's ancestry, inherited ones first."""
    if b.parent is not None:
        floor = b.chain.tick_floor
        for r in all_records(b.parent):
            if r.tick > floor:
                break
            yield r
    for t in b.ticks():
        yield b.record(t)


def judge_receipts(b: Bundle) -> list[tuple[str, Receipt]]:
    """Judge receipts in the bundle and in the branches its meta.json lists, oldest first."""
    out = [("self", r) for r in b.receipts() if r.kind == "judge"]
    for br in read_meta(b.root).get("branches", []):
        p = Path(br.get("path", ""))
        if not (p / "journal").is_file():
            continue
        try:
            with Bundle.open(p) as child:
                out += [(p.name, r) for r in child.receipts() if r.kind == "judge"]
        except (FormatError, OSError, KeyError):
            continue
    return out


@dataclasses.dataclass
class Assessment:
    rows: list[tuple[str, Tier | None, str]]     # (input, ceiling or None for no cap, detail)
    effective: Tier
    publishable: bool
    why_not: list[str]
    checks: list[str]


def assess(b: Bundle, cfg: Mapping[str, Any] | None = None) -> Assessment:
    cfg = cfg or b.cfg
    declared = b.header.tier
    backend = backend_of(b.env)
    rows: list[tuple[str, Tier | None, str]] = [
        ("declared", declared, "HOLO header (score header first, then run.tier)"),
        ("backend", BACKEND_CEILING.get(backend, Tier.LAB), backend)]
    mod, foreign = module_ceiling(b.env, list(cfg["shim"]["module_allowlist"]))
    rows.append(("modules", mod if foreign else None,
                 f"outside the allowlist: {', '.join(foreign)}" if foreign else "all allowlisted"))
    vc_tier, n, drops, authored = Tier.HANDS_OFF, 0, [], []
    for r in all_records(b):
        n += 1
        c = r.verbclass.ceiling()
        if c < vc_tier or not drops:
            drops.append(f"{c.label} from tick {r.tick} ({r.verbclass.name or 'NONE'})")
            vc_tier = min(vc_tier, c)
        if r.verbclass & AUTHORING_VERBS:
            authored.append(r.tick)
    rows.append(("verbclass", vc_tier, f"{n} ticks; " + "; ".join(drops) if drops else "no ticks"))
    key = b.env.key()
    js = [(w, r) for w, r in judge_receipts(b) if r.env_key == key]
    last = js[-1] if js else None
    if last is None:
        rows.append(("judge", None, "no judge receipt for this key; run telos verify --fresh"))
    else:
        cap = None if last[1].result in ("pass", "best_effort") else Tier.LAB
        rows.append(("judge", cap, f"last judge {last[1].result} in {last[0]}"
                     + (f", first divergence at tick {last[1].first_divergence}"
                        if last[1].first_divergence is not None else "")))
    seal = b.sealed
    if seal:
        rows.append(("seal", Tier.parse(seal["computed_tier"]), f"SEAL computed_tier over {seal['ticks']} ticks"))
    eff = min(t for _n, t, _d in rows if t is not None)
    checks = []
    computed = min(t for name, t, _d in rows if t is not None and name not in ("declared", "seal"))
    if computed < declared:
        checks.append(f"header declares {declared.label} but the run computes {computed.label}")
    else:
        checks.append(f"header {declared.label} is at or below the computed {computed.label}")
    recomputed = min(declared, BACKEND_CEILING.get(backend, Tier.LAB), vc_tier)
    if seal and Tier.parse(seal["computed_tier"]) > recomputed:
        checks.append(f"SEAL claims {seal['computed_tier']} but the log gives {recomputed.label}")
    if not seal:
        checks.append("unsealed: the tier is final only when the run is sealed")
    why = []
    if eff < Tier.TAS:
        why.append(f"effective tier {eff.label} is below tas")
    if authored:
        why.append(f"{len(authored)} tick(s) carry RESTORE or FILES_RESTORE, first {authored[0]}")
    if last is None or last[1].result not in ("pass", "best_effort"):
        why.append("no passing judge receipt")
    return Assessment(rows, eff, not why, why, checks)


def effective(b: Bundle) -> Tier:
    return assess(b).effective


def publishable(b: Bundle) -> bool:
    return assess(b).publishable


def explain(b: Bundle) -> list[str]:
    a = assess(b)
    lines = [f"{'input':<11} {'ceiling':<10} detail"]
    lines += [f"{n:<11} {_label(t):<10} {d}" for n, t, d in a.rows]
    limiting = [n for n, t, _d in a.rows if t == a.effective]
    lines.append(f"{'effective':<11} {a.effective.label:<10} set by {', '.join(limiting)}")
    lines += [f"{'check':<11} {'':<10} {c}" for c in a.checks]
    lines.append(f"{'publishable':<11} {'yes' if a.publishable else 'no':<10} {'; '.join(a.why_not)}".rstrip())
    return lines


# ---------------------------------------------------------------- judge replay

class _Desync(Exception):
    pass


class _Collect:
    """Stands in for a writable Bundle under BundleSink, so the judge builds TickRecords the same way."""

    def __init__(self, on_tick: Callable[[TickRecord], None]) -> None:
        self.on_tick, self.epochs = on_tick, []

    def append_tick(self, rec: TickRecord, pages: Any = None) -> None:
        self.on_tick(rec)

    def mark(self, name: str, tick: int | None = None) -> None:
        pass

    def end_epoch(self, epoch: int, tick: int, reason: str, exit_status: int | None = None,
                  files_root: bytes = NULL_DIGEST) -> None:
        self.epochs.append({"epoch": epoch, "tick": tick, "reason": reason,
                            "files_root": files_root.hex() if files_root != NULL_DIGEST else ""})

    def put_file(self, data: bytes) -> bytes:
        return sha256(data)

    def put_file_tree(self, rows: Any) -> bytes:
        return sha256(canonical_json({"entries": sorted([p, h if isinstance(h, str) else h.hex(), int(s), int(v)]
                                                        for p, h, s, v in rows)}))

    def seal(self, *_a: Any) -> None:
        pass

    def close(self) -> None:
        pass


class _PlaceStart:
    """Wraps a backend so the start save is placed by file after hello and before tick 0 (4.3 step 2)."""

    def __init__(self, inner: Any, blob: bytes) -> None:
        self._inner, self._blob = inner, blob

    def attach(self, cfg: Any, env: Any) -> Any:
        caps = self._inner.attach(cfg, env)
        self._inner.files("put", self._blob)
        return caps

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def _start_blob(b: Bundle) -> bytes | None:
    """files blob for a `save:<files_root>` start, from the bundle's files store."""
    start = b.header.start
    if not start.startswith("save:"):
        return None
    root = bytes.fromhex(start[5:])
    ents = [[p, h, s, v, base64.b64encode(b.file(bytes.fromhex(h))).decode()] for p, h, s, v in b.file_tree(root)]
    return canonical_json({"entries": ents})


def _fresh_path(base: Path) -> Path:
    p, i = base, 2
    while p.exists():
        p = base.with_name(f"{base.name[:-5]}-{i}.holo")
        i += 1
    return p


def judge(b: Bundle, cfg: Mapping[str, Any], log: Callable[[str], None] | None = None) -> Receipt:
    """Replay the embedded SCOR in a fresh backend under the pin, compare, and append an RCPT on a new branch."""
    from kptas.holo.branch import create_branch
    from kptas.score import compile_score
    from kptas.supervisor import PROCESS_BACKENDS, BundleSink, RunError, Supervisor, TelosError, default_factory
    from kptas.telos.textview import tick_differs

    log = log or (lambda s: print(f"telos: {s}", file=sys.stderr, flush=True))
    have = pin_env(cfg)
    key = b.env.key()
    if have.key() != key:
        w, h = b.env.to_obj(), have.to_obj()
        msg = f"the environment differs from the pin in {', '.join(k for k in w if w[k] != h.get(k))}"
        if cfg["env"]["guard"] == "refuse":
            raise TelosError(msg + " (env.guard is refuse)")
        log(msg + "; env.guard is warn, judging anyway")
    recorded = {r.tick: r for r in all_records(b)}
    if not recorded:
        raise TelosError("the bundle has no ticks to judge")
    last = max(recorded)
    compare = cfg["verify"]["compare"]
    if compare == "end":
        points = {last}
    elif compare == "syncs":
        points = {t for t, r in recorded.items() if r.verbclass & VerbClass.WAIT
                  and not (t + 1 in recorded and recorded[t + 1].verbclass & VerbClass.WAIT)} | {last}
    else:
        points = set(recorded)
    roots = have.key() == key and bool(b.env.mask_registry)
    stop = cfg["verify"]["stop_on_desync"]
    fresh: dict[int, TickRecord] = {}
    first: list[Any] = []
    holder: list[Any] = []

    def on_tick(rec: TickRecord) -> None:
        fresh[rec.tick] = rec
        if first or rec.tick not in points:
            return
        want = recorded.get(rec.tick)
        field = "missing" if want is None else tick_differs(want, rec, roots)
        if field:
            pm = holder[0].pmap if holder and holder[0].pmap is not None else None
            first.append((rec.tick, field, want, rec.journal, PageMap(pm.refs()) if pm is not None else None))
            if stop:
                raise _Desync

    sink = BundleSink(_Collect(on_tick), Tier.parse(cfg["run"]["tier"]))
    holder.append(sink)
    try:
        blob = _start_blob(b)
    except (FormatError, KeyError, ValueError) as e:
        raise TelosError(f"the start save {b.header.start} is not in the files store: {e}") from None
    factory = None if blob is None else (lambda n, c, link: _PlaceStart(default_factory(n, c, link), blob))
    sup = Supervisor(cfg, env=b.env, sink=sink, factory=factory, run_name="judge", log=log)
    t0 = time.perf_counter_ns()
    halted = ""
    try:
        sup.run_score(compile_score(b.score), max_ticks=last)
    except _Desync:
        pass
    except RunError as e:
        halted = str(e)
    finally:
        sup.close()
    us = (time.perf_counter_ns() - t0) // 1000
    if not first:
        miss = sorted(t for t in points if t not in fresh)
        if miss:
            first.append((miss[0], "missing", recorded[miss[0]], None, None))
        elif halted:
            first.append((sup.tick, f"run halted: {halted}", recorded.get(sup.tick), None, None))
    if not first and b.parent is None:
        want_ep = [{k: e[k] for k in ("epoch", "tick", "reason", "files_root")} for e in b.epochs]
        got_ep = sink.bundle.epochs[:len(want_ep)]
        got_ep += [None] * (len(want_ep) - len(got_ep))
        bad = next(((w, g) for w, g in zip(want_ep, got_ep, strict=True) if w != g), None)
        if bad:
            first.append((bad[0]["tick"], f"EPOC {bad[0]['epoch']}: recorded {bad[0]}, fresh {bad[1]}",
                          None, None, None))
    if first:
        t, field, want, jr, pm = first[0]
        wmap = None
        if pm is not None and want is not None and want.flags & TickFlag.CAPTURED:
            try:
                wmap = b.map_at(t)
            except (FormatError, KeyError):
                wmap = None
        for line in hv.report(t, field, want, jr, wmap, pm if wmap is not None else None):
            log(line)
    backend = cfg["run"]["backend"]
    tail = fresh.get(max(fresh)) if fresh else None
    ok = not first
    r = Receipt(
        kind="judge", result=("best_effort" if backend == "handsoff" else "pass") if ok else "fail",
        from_tick=min(recorded), to_tick=last, mode=SeekMode.REPLAY,
        verify_level=(VerifyLevel.FRAMES if backend == "handsoff" else
                      VerifyLevel.SIM_ROOT if roots else VerifyLevel.JOURNAL),
        micros=us, state_root=hv.root_hex(tail.state_root) if tail else "",
        journal_sha256=hv.root_hex(tail.journal_sha256) if tail else "",
        first_divergence=first[0][0] if first else None,
        restore_points=hv.restore_ranges((t, bool(x.flags & TickFlag.RESTORE_POINT)) for t, x in fresh.items()),
        verifier=(f"judge {backend} fresh {'process' if backend in PROCESS_BACKENDS else 'instance'}; "
                  f"compare {compare}; {len(points & set(fresh))} of {len(points)} ticks compared"),
        env_key=have.key())
    dest = _fresh_path(b.root.with_name(f"{b.root.name[:-5]}@judge.holo"))
    child = create_branch(b, b.last_tick if b.last_tick >= 0 else last, dest)
    try:
        child.receipt(r)
    finally:
        child.close()
    log(f"judge receipt appended on branch {dest}")
    return r


__all__ = ["Assessment", "all_records", "assess", "backend_of", "effective", "explain", "judge", "judge_receipts",
           "module_ceiling", "publishable"]
