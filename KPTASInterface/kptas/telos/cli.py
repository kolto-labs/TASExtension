"""telos: the command line for InputInterfaceTelos.

Commands that need kptas.holo, kptas.tier or kptas.backend import them lazily
and fail with a plain message when they are missing. Exit codes: 0 ok, 1 a
check failed or a run halted, 2 usage, configuration or missing module.
"""

from __future__ import annotations

import argparse
import dataclasses
import os
import shlex
import subprocess
import sys
import tomllib
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from kptas import __version__
from kptas.config import ConfigError, resolve
from kptas.score import ScoreError, compile_score, fmt
from kptas.score.compile import CompiledScore
from kptas.supervisor import PKG_ROOT, BundleSink, RunError, Supervisor, TelosError, note_text, require
from kptas.telos import textview as tv
from kptas.types import (SEEK_ALIASES, BackendError, EngineEvent, EnvPin, FormatError, Header, Receipt, SeekMode,
                         Tier)

CONFIG_NAME = "telos.toml"
FRESH_JUDGE = "KPTAS_FRESH_JUDGE"      # set in the child that verify.fresh_process starts
SEEK_CHOICES = (*(m.value for m in SeekMode), *sorted(SEEK_ALIASES))
BACKEND_CHOICES = ("auto", "pysim", "simgame", "shim", "handsoff", "phasor")   # kptas.backend.BACKENDS


def out(line: str = "") -> None:
    print(line, flush=True)


def err(line: str) -> None:
    print(f"telos: {line}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------- configuration

@dataclasses.dataclass
class Ctx:
    config: str | None = None
    base: Path = dataclasses.field(default_factory=Path.cwd)

    def raw(self) -> tuple[dict[str, Any], str]:
        path = self.config
        if path is None and Path(CONFIG_NAME).is_file():
            path = CONFIG_NAME
        if path is None:
            return {}, "<defaults>"
        try:
            with open(path, "rb") as f:
                user = tomllib.load(f)
        except FileNotFoundError:
            raise ConfigError(f"{path}: no such file") from None
        except tomllib.TOMLDecodeError as e:
            raise ConfigError(f"{path}: {e}") from None
        self.base = Path(path).resolve().parent
        return user, path

    def cfg(self, header: Mapping[str, Any] | None = None, **over: Any) -> dict[str, Any]:
        """Resolved config. A score header's tier and hz win over run.tier and clock.hz (§5.5),
        and are applied before resolution so tier-dependent autos follow them."""
        user, src = self.raw()
        user = {k: dict(v) if isinstance(v, dict) else v for k, v in user.items()}
        if header:
            if header.get("tier"):
                user.setdefault("run", {})["tier"] = header["tier"]
            if header.get("hz"):
                user.setdefault("clock", {})["hz"] = int(header["hz"])
            if header.get("game"):
                user.setdefault("run", {})["game"] = header["game"]
        for dotted, v in over.items():
            if v is not None:
                table, key = dotted.split("__")
                user.setdefault(table, {})[key] = v
        return resolve(user, src)

    def include_dirs(self, cfg: Mapping[str, Any]) -> list[Path]:
        return [p if p.is_absolute() else self.base / p
                for p in (Path(os.path.expanduser(d)) for d in cfg["score"]["include_dirs"])]

    def compile(self, path: str) -> CompiledScore:
        self.raw()
        return compile_score(path, include_dirs=self.include_dirs(self.cfg()))


def holo(*attrs: str) -> Any:
    return require("kptas.holo", *attrs)


def tier_mod(*attrs: str) -> Any:
    return require("kptas.tier", *attrs)


def open_bundle(path: str) -> Any:
    if not Path(path).is_dir():
        raise TelosError(f"{path} is not a holorecord bundle directory")
    return holo("Bundle").Bundle.open(path)


def records(b: Any) -> Iterable[Any]:
    return (b.record(t) for t in b.ticks())


def lineage(b: Any) -> Iterable[Any]:
    """Every tick on the bundle's ancestry: a branch's inherited ticks, then its own."""
    return require("kptas.tier.policy", "all_records").all_records(b)


def is_bundle(path: str) -> bool:
    return Path(path).is_dir()


def pin_env(cfg: Mapping[str, Any]) -> EnvPin:
    return tier_mod("pin_env").pin_env(cfg)


def bundle_cfg(ctx: Ctx, bundle: Any, **over: Any) -> dict[str, Any]:
    h: Header = bundle.header
    return ctx.cfg({"tier": h.tier.label, "hz": h.hz, "game": h.game}, **over)


def build_header(cfg: Mapping[str, Any], score: CompiledScore, env: EnvPin) -> Header:
    threads = cfg["threads"]["model"]
    try:
        return Header(game=cfg["run"]["game"], build=cfg["run"]["build"], exe_sha256=env.exe_sha256,
                      tier=cfg["run"]["tier"], hz=cfg["clock"]["hz"], clock=cfg["clock"]["mode"],
                      scale=cfg["clock"]["scale"], seek=cfg["seek"]["mode"],
                      threads="join" if threads == "auto" else threads,
                      start=score.header.get("start") or "boot",
                      keyframe_every=cfg["seek"]["keyframe_every"], codec=cfg["store"]["codec"],
                      profile=cfg["portable"]["profile"], determinism_key=env.key())
    except FormatError as e:
        raise TelosError(f"cannot build the bundle header: {e}") from None


def fresh_path(base: Path) -> Path:
    if not base.exists():
        return base
    for i in range(2, 10_000):
        p = base.with_name(f"{base.stem}-{i}{base.suffix}")
        if not p.exists():
            return p
    raise TelosError(f"too many bundles named {base.name}")


def stream(note: Any = None) -> Any:
    printed = [False]

    def on_report(rep: Any, n: Any) -> None:
        if not printed[0]:
            out(tv.report_line(None))
            printed[0] = True
        out(tv.report_line(rep, note_text(n) if n is not None else ""))
    return on_report


# ---------------------------------------------------------------- score

def cmd_score_check(ctx: Ctx, a: argparse.Namespace) -> int:
    bad = 0
    for path in a.files:
        try:
            c = ctx.compile(path)
        except ScoreError as e:
            err(str(e))
            bad += 1
            continue
        ticks = sum(getattr(i, "count", 0) for i in c.items if type(i).__name__ == "Note")
        h = " ".join(f"{k}={v}" for k, v in c.header.items() if v is not None)
        out(f"ok {path}: {len(c.items)} items, {ticks} note ticks, {len(c.files)} files, "
            f"sha256 {c.sha256[:16]}  [{h}]")
    return 1 if bad else 0


def cmd_score_fmt(ctx: Ctx, a: argparse.Namespace) -> int:
    changed = 0
    for path in a.files:
        text = Path(path).read_text(encoding="utf-8")
        try:
            new = fmt(text, source=path)
        except ScoreError as e:
            err(str(e))
            return 1
        if new == text:
            continue
        changed += 1
        if a.check:
            out(f"would reformat {path}")
        elif a.write:
            Path(path).write_text(new, encoding="utf-8")
            out(f"reformatted {path}")
    if not a.check and not a.write:
        for path in a.files:
            sys.stdout.write(fmt(Path(path).read_text(encoding="utf-8"), source=path))
    return 1 if a.check and changed else 0


# ---------------------------------------------------------------- running

def cmd_run(ctx: Ctx, a: argparse.Namespace) -> int:
    score = ctx.compile(a.score)
    if a.start:
        return run_from(ctx, a, score)
    if a.mode:
        raise TelosError("--mode needs --from")
    cfg = ctx.cfg(score.header, run__backend=a.backend, run__max_ticks=a.max_ticks)
    env = pin_env(cfg)
    header = build_header(cfg, score, env)
    dest = Path(a.out) if a.out else fresh_path(
        Path(os.path.expanduser(cfg["run"]["out_dir"])) / f"{Path(a.score).stem}.holo")
    Bundle = holo("Bundle").Bundle
    dest.parent.mkdir(parents=True, exist_ok=True)
    sink = BundleSink(Bundle.create(dest, header, score.text(), env, config=cfg), header.tier)
    sup = Supervisor(cfg, env=env, attach=a.attach, sink=sink, run_name=dest.stem, log=err)
    ok = False
    try:
        res = sup.run_score(score, max_ticks=cfg["run"]["max_ticks"],
                            on_report=stream() if a.table else None)
        ok = True
    finally:
        try:
            sup.close()
        except BaseException:
            ok = False      # the last EPOC may be missing: leave the run unsealed
            raise
        finally:
            sink.close(seal=ok, backend_ceiling=sup.caps.tier_ceiling if sup.caps else None)
    out(f"{dest}: ticks 0..{res.ticks}, {res.epochs} epoch(s), stopped at {res.stopped}")
    return 0 if res.stopped != "halted" else 1


def run_from(ctx: Ctx, a: argparse.Namespace, score: CompiledScore) -> int:
    """telos run <score> --from <bundle>@<tick>: seek a live game to the tick, then record a branch
    that plays the rest of the score from there. The branch embeds the whole score, so the judge's
    replay from boot reproduces the parent's ticks up to the branch tick and the branch's after it."""
    src, sep, at = a.start.rpartition("@")
    if not sep or not at.isdigit():
        raise TelosError(f"--from wants <bundle>@<tick>, e.g. walk.holo@40, not {a.start!r}")
    tick = int(at)
    par = open_bundle(src)
    try:
        cfg = bundle_cfg(ctx, par, run__backend=a.backend, run__max_ticks=a.max_ticks)
        mode = SeekMode.parse(a.mode or cfg["seek"]["mode"])
        if not mode.allowed(Tier.parse(cfg["run"]["tier"])):
            raise TelosError(f"seek mode {mode} is not allowed in tier {cfg['run']['tier']}")
        cur = require("kptas.holo.seek", "resume_cursor").resume_cursor(par, tick, score, cfg)  # before any write
        dest = Path(a.out) if a.out else fresh_path(Path(src).with_name(f"{Path(src).stem}@{tick}.holo"))
        if dest.exists():
            raise TelosError(f"{dest} exists")
        sup = Supervisor(cfg, env=par.env, attach=a.attach, run_name=dest.stem, log=err)
        sink, ok = None, False
        try:
            r: Receipt = holo("seek").seek(par, tick, sup, mode=mode, cfg=cfg)
            out(f"seek {r.from_tick} -> {tick}: {r.mode} {r.result}; {r.verifier}; canary {r.canary}"
                + (f"; fallback {r.fallback}" if r.fallback else ""))
            if r.result != "pass":
                raise RunError(f"the seek to tick {tick} did not verify; no branch written")
            child = holo("create_branch").create_branch(par, tick, dest, score=score.text())
            child.receipt(r)            # the branch records how its first state was reached
            sink = BundleSink(child, child.header.tier)
            sink.resume(tick, restored=r.mode is not SeekMode.REPLAY)
            sup.sink = sink
            if r.mode in (SeekMode.RESTORE, SeekMode.SEMANTIC):
                sup.carry = [EngineEvent(e, tick, x) for e, x in cur.carry]
            res = sup.run_score(score, max_ticks=cfg["run"]["max_ticks"], start=cur,
                                on_report=stream() if a.table else None)
            ok = True
        finally:
            try:
                sup.close()
            except BaseException:
                ok = False
                raise
            finally:
                if sink is not None:
                    sink.close(seal=ok, backend_ceiling=sup.caps.tier_ceiling if sup.caps else None)
    finally:
        par.close()
    out(f"{dest}: branch of {src} at tick {tick}, ticks {tick + 1}..{res.ticks}, stopped at {res.stopped}")
    return 0 if res.stopped != "halted" else 1


def cmd_play(ctx: Ctx, a: argparse.Namespace) -> int:
    if is_bundle(a.target):
        b = open_bundle(a.target)
        score = compile_score(b.score)
        cfg = bundle_cfg(ctx, b, run__backend=a.backend, run__max_ticks=a.max_ticks)
    else:
        score = ctx.compile(a.target)
        cfg = ctx.cfg(score.header, run__backend=a.backend, run__max_ticks=a.max_ticks)
    sup = Supervisor(cfg, attach=a.attach, run_name="play", log=err)
    try:
        res = sup.run_score(score, max_ticks=cfg["run"]["max_ticks"], on_report=stream())
    finally:
        sup.close()
    out(f"played ticks 0..{res.ticks}, {res.epochs} epoch(s), stopped at {res.stopped}")
    return 0 if res.stopped != "halted" else 1


def cmd_seek(ctx: Ctx, a: argparse.Namespace) -> int:
    b = open_bundle(a.bundle)
    cfg = bundle_cfg(ctx, b, run__backend=a.backend)
    mode = SeekMode.parse(a.mode or cfg["seek"]["mode"])
    tier = Tier.parse(cfg["run"]["tier"])
    if not mode.allowed(tier):
        raise TelosError(f"seek mode {mode} is not allowed in tier {tier}")
    seek = holo("seek").seek
    sup = Supervisor(cfg, env=getattr(b, "env", None), attach=a.attach, run_name="seek", log=err)
    try:
        sup.open()
        r: Receipt = seek(b, a.tick, sup, mode=mode, cfg=cfg)
    finally:
        sup.close()
    for line in tv.receipt_lines(r):
        out(line)
    return 0 if r.result != "fail" else 1


def cmd_branch(ctx: Ctx, a: argparse.Namespace) -> int:
    b = open_bundle(a.bundle)
    try:
        b.link_at(a.tick)
    except KeyError:
        raise TelosError(f"{a.bundle} has no tick {a.tick}") from None
    dest = Path(a.out) if a.out else fresh_path(
        Path(a.bundle).with_name(f"{Path(a.bundle).stem}@{a.tick}.holo"))
    child = holo("create_branch").create_branch(b, a.tick, dest)
    child.close()
    out(f"branched {a.bundle} at tick {a.tick} -> {dest}")
    return 0


def cmd_edit(ctx: Ctx, a: argparse.Namespace) -> int:
    if is_bundle(a.target):
        text = open_bundle(a.target).score
        path = Path(a.out or f"{Path(a.target).stem}.score")
        if path.exists() and not a.force:
            raise TelosError(f"{path} exists; pass --force or --out")
        path.write_text(text, encoding="utf-8")
        out(f"wrote the bundle's score to {path}")
    else:
        path = Path(a.target)
        if not path.exists():
            path.write_text("#!score v1\n", encoding="utf-8")
    if not a.no_editor:
        editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
        subprocess.run([*shlex.split(editor), str(path)], check=False)
    return cmd_score_check(ctx, argparse.Namespace(files=[str(path)]))


# ---------------------------------------------------------------- inspecting

def cmd_show(ctx: Ctx, a: argparse.Namespace) -> int:
    b = open_bundle(a.bundle)
    out(f"{a.bundle}")
    for line in tv.header_lines(b.header):
        out(line)
    if a.legend:
        out(tv.LEGEND)
    out()
    for line in tv.tick_table(records(b), start=a.start, end=a.end, limit=a.limit):
        out(line)
    return 0


def _pair(a: argparse.Namespace) -> tuple[Any, Any, bool]:
    """Two bundles, and whether their roots may be compared.

    Two bundles are two launches. Section 4.3 compares roots across launches only
    when both share the determinism key and a mask registry qualifies sim_root;
    without one, only journal digests are compared.
    """
    x, y = open_bundle(a.a), open_bundle(a.b)
    same_key = x.header.determinism_key == y.header.determinism_key and bool(x.header.determinism_key)
    qualified = bool(getattr(x.env, "mask_registry", "")) and bool(getattr(y.env, "mask_registry", ""))
    return x, y, same_key and qualified and not a.no_roots


def cmd_diff(ctx: Ctx, a: argparse.Namespace) -> int:
    x, y, roots = _pair(a)
    n = 0
    for d in tv.diff_ticks(lineage(x), lineage(y), compare_roots=roots):
        if n == 0:
            out("\n".join(d.lines()))
            if a.first:
                return 1
            out()
            out("tick      field")
        if n < a.limit:
            out(f"{d.tick:<9} {d.field}")
        n += 1
    if n == 0:
        out(f"no divergence{'' if roots else ' (journal only; roots not compared)'}")
        return 0
    if n > a.limit:
        out(f"... {n - a.limit} more")
    return 1


def cmd_bisect(ctx: Ctx, a: argparse.Namespace) -> int:
    x, y, roots = _pair(a)
    ta, tb = {r.tick for r in lineage(x)}, {r.tick for r in lineage(y)}
    if not ta and not tb:
        out("both runs are empty")
        return 0
    lo, hi = min(ta | tb), max(ta | tb)
    if not roots:
        # journal digests are per tick, so a split can reconverge and binary search would miss it
        d = next(tv.diff_ticks(lineage(x), lineage(y), compare_roots=False), None)
        if d is None:
            out(f"runs agree at every tick {lo}..{hi} (journal only; roots not compared)")
            return 0
        out("\n".join(d.lines()))
        out(f"linear scan over ticks {lo}..{hi} (journal only; roots not compared)")
        return 1
    d, probes = tv.bisect_ticks(lambda t: x.record(t) if t in ta else None,
                                lambda t: y.record(t) if t in tb else None, lo, hi, compare_roots=roots)
    if d is None:
        out(f"runs agree at tick {hi} ({probes} probe(s))")
        return 0
    out("\n".join(d.lines()))
    out(f"{probes} probe(s) over ticks {lo}..{hi}")
    return 1


def cmd_verify(ctx: Ctx, a: argparse.Namespace) -> int:
    b = open_bundle(a.bundle)
    cfg = bundle_cfg(ctx, b, verify__fresh_process=True if a.fresh else None,
                     verify__compare=a.compare, run__backend=a.backend)
    if cfg["verify"]["fresh_process"] and not os.environ.get(FRESH_JUDGE):
        b.close()
        return verify_in_child(ctx, a, cfg)
    r: Receipt = tier_mod("judge").judge(b, cfg)
    for line in tv.receipt_lines(r):
        out(line)
    return 0 if r.result in ("pass", "best_effort") else 1


def verify_in_child(ctx: Ctx, a: argparse.Namespace, cfg: Mapping[str, Any]) -> int:
    """Judge from a new interpreter in the pinned environment, so nothing this process loaded or
    set can reach the replay."""
    if cfg["game"]["pinned_env"]:
        keep = ("PATH", "HOME", "USER", "DISPLAY", "XAUTHORITY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR")
        env = {k: os.environ[k] for k in keep if k in os.environ}
        env.update(LANG="C", LC_ALL="C", TZ="UTC", PYTHONHASHSEED="0")
    else:
        env = dict(os.environ)
    env["PYTHONPATH"] = str(PKG_ROOT)
    env[FRESH_JUDGE] = "1"
    argv = [sys.executable, "-m", "kptas", *(["--config", ctx.config] if ctx.config else []), "verify", a.bundle]
    argv += [*(["--compare", a.compare] if a.compare else []), *(["--backend", a.backend] if a.backend else [])]
    return subprocess.run(argv, env=env, stdin=subprocess.DEVNULL, check=False).returncode


def cmd_tier_show(ctx: Ctx, a: argparse.Namespace) -> int:
    b = open_bundle(a.bundle)
    eff = tier_mod("effective").effective(b)
    out(f"declared {b.header.tier}  effective {Tier.parse(eff)}")
    return 0


def cmd_tier_explain(ctx: Ctx, a: argparse.Namespace) -> int:
    b = open_bundle(a.bundle)
    for line in tier_mod("explain").explain(b):
        out(line)
    return 0


def cmd_env_pin(ctx: Ctx, a: argparse.Namespace) -> int:
    env = pin_env(ctx.cfg(run__backend=a.backend))
    data = env.to_payload()
    if a.out:
        Path(a.out).write_bytes(data + b"\n")
        out(f"wrote {a.out}")
    else:
        out(data.decode())
    err(f"determinism key {env.key()}")
    return 0


def env_diff(want: EnvPin, have: EnvPin) -> Iterable[str]:
    w, h = want.to_obj(), have.to_obj()
    host = set(EnvPin.HOST_FIELDS) if want.portable else set()
    for k in w:
        if w[k] != h.get(k):
            tag = " (host, outside the key)" if k in host else ""
            yield f"{k}{tag}: pinned {w[k]!r} now {h.get(k)!r}"


def cmd_env_check(ctx: Ctx, a: argparse.Namespace) -> int:
    if is_bundle(a.pin):
        b = open_bundle(a.pin)
        want, cfg = b.env, bundle_cfg(ctx, b, run__backend=a.backend)
    else:
        try:
            want = EnvPin.from_payload(Path(a.pin).read_bytes())
        except (ValueError, TypeError) as e:
            raise TelosError(f"{a.pin}: not an env pin: {e}") from None
        cfg = ctx.cfg(run__backend=a.backend)
    have = pin_env(cfg)
    diffs = list(env_diff(want, have))
    for d in diffs:
        out(d)
    same = want.key() == have.key()
    out(f"key {'matches' if same else 'differs'}: pinned {want.key()[:16]} now {have.key()[:16]}")
    if same:
        return 0
    if cfg["env"]["guard"] == "warn":
        err("env.guard is warn; continuing")
        return 0
    return 1


def cmd_caps(ctx: Ctx, a: argparse.Namespace) -> int:
    cfg = ctx.cfg(run__backend=a.backend)
    caps = Supervisor(cfg, attach=a.attach, run_name="caps", log=err).probe()
    obj = {f.name: getattr(caps, f.name) for f in dataclasses.fields(caps)}
    obj = {k: sorted(str(x) for x in v) if isinstance(v, frozenset) else
           str(v) if isinstance(v, Tier) else v for k, v in obj.items()}
    for line in tv.kv_lines(obj, indent=""):
        out(line)
    return 0


def cmd_gym_serve(ctx: Ctx, a: argparse.Namespace) -> int:
    from kptas.gym import KotorEnv, serve
    cfg = ctx.cfg(run__backend=a.backend)
    env = KotorEnv(cfg, prefix=a.prefix, attach=a.attach, log=err)
    serve(env, a.host, a.port, once=a.once,
          ready=lambda p: out(f"gym listening on {a.host}:{p}"))
    return 0


def cmd_repl(ctx: Ctx, a: argparse.Namespace) -> int:
    from kptas.telos.repl import Repl
    return Repl(ctx, backend=a.backend, attach=a.attach).loop()


# ---------------------------------------------------------------- parser

class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # type: ignore[override]
        self.print_usage(sys.stderr)
        sub = self.prog.partition(" ")[2]
        raise TelosUsage(f"{sub}: {message}" if sub else message)


class TelosUsage(TelosError):
    pass


def build_parser() -> argparse.ArgumentParser:
    p = _Parser(prog="telos", description="Record, replay and inspect KotOR input scores.")
    p.add_argument("--config", "-c", help=f"telos.toml path (default ./{CONFIG_NAME} if present)")
    p.add_argument("--version", action="version", version=f"telos {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="command", parser_class=_Parser)

    def cmd(name: str, fn: Any, help: str, parent: Any = sub) -> argparse.ArgumentParser:
        sp = parent.add_parser(name, help=help, description=help)
        sp.set_defaults(fn=fn)
        return sp

    def backend_opts(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--backend", choices=BACKEND_CHOICES)
        sp.add_argument("--attach", action="store_true",
                        help="wait for an already started game on shim.port instead of launching one")

    sc = sub.add_parser("score", help="check or format score files")
    ssub = sc.add_subparsers(dest="sub", required=True, metavar="action", parser_class=_Parser)
    s = cmd("check", cmd_score_check, "compile scores and report errors", ssub)
    s.add_argument("files", nargs="+")
    s = cmd("fmt", cmd_score_fmt, "print scores in canonical form", ssub)
    s.add_argument("files", nargs="+")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--check", action="store_true", help="exit 1 if any file would change")
    g.add_argument("--write", "-w", action="store_true", help="rewrite files in place")

    s = cmd("run", cmd_run, "play a score and record a holorecord bundle")
    s.add_argument("score")
    s.add_argument("--out", "-o", help="bundle directory (default run.out_dir/<score>.holo)")
    s.add_argument("--max-ticks", type=int)
    s.add_argument("--table", action="store_true", help="print a row per step while running")
    s.add_argument("--from", dest="start", metavar="BUNDLE@TICK",
                   help="seek to a recorded tick and record a branch that plays the rest of the score; "
                        "the score must play what the bundle recorded up to that tick")
    s.add_argument("--mode", choices=SEEK_CHOICES, metavar="MODE",
                   help="seek mode for --from: replay, restore, hybrid or semantic (default seek.mode)")
    backend_opts(s)

    s = cmd("play", cmd_play, "play a score or a bundle's score without recording")
    s.add_argument("target")
    s.add_argument("--max-ticks", type=int)
    backend_opts(s)

    s = cmd("seek", cmd_seek, "bring a live game to a recorded tick and print the receipt")
    s.add_argument("bundle")
    s.add_argument("tick", type=int)
    s.add_argument("--mode", choices=SEEK_CHOICES, metavar="MODE",
                   help="replay, restore, hybrid or semantic (default seek.mode)")
    backend_opts(s)

    s = cmd("branch", cmd_branch, "start a new bundle from a recorded tick")
    s.add_argument("bundle")
    s.add_argument("tick", type=int)
    s.add_argument("--out", "-o")

    s = cmd("edit", cmd_edit, "open a score (or a bundle's score) in $EDITOR, then check it")
    s.add_argument("target")
    s.add_argument("--out", "-o", help="where to write a bundle's score")
    s.add_argument("--force", action="store_true")
    s.add_argument("--no-editor", action="store_true")

    s = cmd("show", cmd_show, "print a bundle's header and tick table")
    s.add_argument("bundle")
    s.add_argument("--from", dest="start", type=int)
    s.add_argument("--to", dest="end", type=int)
    s.add_argument("--limit", type=int, default=200)
    s.add_argument("--legend", action="store_true")

    for name, fn, help in (("diff", cmd_diff, "list the ticks where two runs differ"),
                           ("bisect", cmd_bisect, "binary search for the first tick two runs split")):
        s = cmd(name, fn, help)
        s.add_argument("a")
        s.add_argument("b")
        s.add_argument("--no-roots", action="store_true", help="compare journals only")
        if name == "diff":
            s.add_argument("--limit", type=int, default=50)
            s.add_argument("--first", action="store_true", help="stop at the first divergence")

    s = cmd("verify", cmd_verify, "judge replay: replay the embedded score and compare")
    s.add_argument("bundle")
    s.add_argument("--fresh", action="store_true", help="in a new process in the pinned environment")
    s.add_argument("--compare", choices=("every_tick", "syncs", "end"))
    s.add_argument("--backend", choices=BACKEND_CHOICES)

    t = sub.add_parser("tier", help="effective tier of a bundle")
    tsub = t.add_subparsers(dest="sub", required=True, metavar="action", parser_class=_Parser)
    cmd("show", cmd_tier_show, "declared and effective tier", tsub).add_argument("bundle")
    cmd("explain", cmd_tier_explain, "which input limits the tier, and why", tsub).add_argument("bundle")

    e = sub.add_parser("env", help="environment pin")
    esub = e.add_subparsers(dest="sub", required=True, metavar="action", parser_class=_Parser)
    s = cmd("pin", cmd_env_pin, "hash the current environment into an ENVP pin", esub)
    s.add_argument("--out", "-o")
    s.add_argument("--backend", choices=BACKEND_CHOICES)
    s = cmd("check", cmd_env_check, "compare the current environment with a pin or a bundle", esub)
    s.add_argument("pin")
    s.add_argument("--backend", choices=BACKEND_CHOICES)

    s = cmd("caps", cmd_caps, "launch or attach a backend and print its capabilities")
    backend_opts(s)

    gy = sub.add_parser("gym", help="Gymnasium-style environment")
    gsub = gy.add_subparsers(dest="sub", required=True, metavar="action", parser_class=_Parser)
    s = cmd("serve", cmd_gym_serve, "serve reset/step over loopback NDJSON", gsub)
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=0)
    s.add_argument("--prefix", help="score played after every reset")
    s.add_argument("--once", action="store_true", help="exit after the first client")
    backend_opts(s)

    s = cmd("repl", cmd_repl, "interactive authoring and inspection")
    backend_opts(s)
    return p


def dispatch(argv: Sequence[str], ctx: Ctx | None = None) -> int:
    """Parse and run one command. Errors become messages and exit codes, never tracebacks."""
    try:
        a = build_parser().parse_args(list(argv))
    except TelosUsage as e:
        err(str(e))
        return 2
    except SystemExit as e:  # --help and --version
        return int(e.code or 0)
    if ctx is None:
        ctx = Ctx(a.config)
    elif a.config:
        ctx = Ctx(a.config)
    try:
        return int(a.fn(ctx, a) or 0)
    except RunError as e:
        err(str(e))
        return 1
    except (TelosError, ConfigError, ScoreError, FormatError, BackendError) as e:
        err(str(e))
        return 2
    except OSError as e:
        err(f"{e.filename or ''}: {e.strerror or e}".lstrip(": "))
        return 2
    except KeyboardInterrupt:
        err("interrupted")
        return 130
    except RuntimeError as e:
        hb = sys.modules.get("kptas.holo.bundle")    # loaded whenever it could have raised
        if hb is None or not isinstance(e, hb.BudgetError):
            raise
        err(str(e))
        return 1


def main(argv: Sequence[str] | None = None) -> int:
    return dispatch(sys.argv[1:] if argv is None else argv)


if __name__ == "__main__":
    sys.exit(main())
