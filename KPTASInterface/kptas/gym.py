"""A Gymnasium-shaped environment over a telos backend, without importing gymnasium.

reset() starts a fresh epoch (optionally playing a score prefix) and returns
(obs, info). step(action) holds one note for its count of ticks and returns
(obs, reward, terminated, truncated, info). serve() exposes one env over
loopback NDJSON so an out-of-process learner can drive it.
"""

from __future__ import annotations

import base64
import json
import random
import sys
from collections.abc import Callable, Mapping
from typing import Any

from kptas.score import Note, compile_score
from kptas.score.keys import BUTTONS
from kptas.supervisor import (BackendFactory, Supervisor, TelosError, listen_socket, note_text,
                              parse_note)
from kptas.types import EnvPin, ProcOutcome, StepReport

RewardFn = Callable[[Any, StepReport], float]


class NoteSpace:
    """Actions: a Note, canonical note text ('w lmb', '4 w'), or a dict of Note fields."""

    def __init__(self, keys: tuple[str, ...] = ("w", "a", "s", "d", "space", "esc", "e"),
                 buttons: tuple[str, ...] = BUTTONS, max_count: int = 1):
        self.keys, self.buttons, self.max_count = keys, buttons, max_count
        self._rng = random.Random(0)

    def seed(self, seed: int | None = None) -> None:
        self._rng = random.Random(seed)

    def sample(self) -> Note:
        held = tuple(sorted(k for k in self.keys if self._rng.random() < 0.2))
        btn = tuple(b for b in self.buttons if self._rng.random() < 0.1)
        return Note(self._rng.randint(1, self.max_count), held=held, buttons=btn)

    def contains(self, x: Any) -> bool:
        try:
            to_note(x)
            return True
        except (TelosError, TypeError, ValueError):
            return False

    def __repr__(self) -> str:
        return f"NoteSpace(keys={len(self.keys)}, buttons={len(self.buttons)})"


class ObsSpace:
    """fields -> {name: value}; digest -> {tick, epoch, state_root, journal}; pixels -> PNG bytes."""

    def __init__(self, kind: str, fields: tuple[str, ...] = ()):
        self.kind, self.fields = kind, fields

    def contains(self, x: Any) -> bool:
        if self.kind == "pixels":
            return x is None or isinstance(x, bytes)
        if not isinstance(x, dict):
            return False
        want = set(self.fields) if self.kind == "fields" else {"tick", "epoch", "state_root", "journal"}
        return want <= set(x)

    def __repr__(self) -> str:
        return f"ObsSpace({self.kind!r}, fields={list(self.fields)})"


def to_note(action: Any) -> Note:
    if isinstance(action, Note):
        return action
    if isinstance(action, str):
        parts = action.split(None, 1)
        if parts and parts[0].isdigit():
            return parse_note(parts[1] if len(parts) > 1 else ".", int(parts[0]))
        return parse_note(action)
    if isinstance(action, Mapping):
        d = dict(action)
        for k in ("held", "buttons"):
            if k in d:
                d[k] = tuple(d[k])
        for k in ("mouse_abs", "mouse_rel"):
            if d.get(k) is not None:
                d[k] = tuple(d[k])
        d.setdefault("count", 1)
        return parse_note(note_text(Note(**d)), d["count"])   # canonicalizes key names
    raise TypeError(f"an action is a Note, note text or a mapping, not {type(action).__name__}")


class KotorEnv:
    metadata = {"render_modes": ["png"]}

    def __init__(self, cfg: Mapping[str, Any] | None = None, *, backend: str | None = None,
                 factory: BackendFactory | None = None, env_pin: EnvPin | None = None,
                 reward_fn: RewardFn | None = None, prefix: str | None = None,
                 attach: bool = False, log: Callable[[str], None] | None = None):
        if cfg is None:
            from kptas.config import load
            cfg = load(None)
        self.cfg = cfg
        g = cfg["gym"]
        self.obs_kind, self.obs_fields = g["obs"], tuple(g["obs_fields"])
        self.max_ticks = g["max_ticks"]
        self.action_space = NoteSpace()
        self.observation_space = ObsSpace(self.obs_kind, self.obs_fields)
        self.reward_fn = reward_fn
        self.prefix = prefix
        self.render_mode = "png" if self.obs_kind == "pixels" else None
        self.sup = Supervisor(cfg, env=env_pin, backend=backend, factory=factory, attach=attach,
                              run_name="gym", log=log)
        self._start_tick = 0
        self._last: StepReport | None = None

    def reset(self, *, seed: int | None = None, options: Mapping[str, Any] | None = None
              ) -> tuple[Any, dict[str, Any]]:
        """Fresh epoch. options: prefix (score text or path played before the first step)."""
        if seed is not None:
            self.action_space.seed(seed)
        self.sup.close("relaunch")
        rep = self.sup.open()
        prefix = (options or {}).get("prefix", self.prefix)
        if prefix:
            last = [rep]
            dirs = self.cfg["score"]["include_dirs"]
            self.sup.run_score(compile_score(prefix, include_dirs=dirs),
                               on_report=lambda r, _n: last.__setitem__(0, r))
            rep = last[0]
        self._start_tick, self._last = rep.tick, rep
        return self._obs(rep), self._info(rep)

    def step(self, action: Any) -> tuple[Any, float, bool, bool, dict[str, Any]]:
        if self.sup.backend is None:
            raise TelosError("call reset() before step()")
        note = to_note(action)
        n = note.count
        if self.max_ticks:
            n = max(0, min(n, self._start_tick + self.max_ticks - self.sup.tick))
        rep = self.sup.step(n, note) if n else self._last
        self._last = rep
        obs = self._obs(rep)
        reward = float(self.reward_fn(obs, rep)) if self.reward_fn else 0.0
        terminated = rep.outcome is ProcOutcome.EXITED
        truncated = bool(self.max_ticks) and rep.tick - self._start_tick >= self.max_ticks
        return obs, reward, terminated, truncated, self._info(rep)

    def render(self) -> bytes | None:
        return self.sup.backend.frame() if self.sup.backend is not None else None

    def close(self) -> None:
        self.sup.close()

    def _obs(self, rep: StepReport) -> Any:
        if self.obs_kind == "digest":
            return {"tick": rep.tick, "epoch": rep.epoch, "state_root": rep.state_root.hex(),
                    "journal": rep.journal_sha256.hex()}
        alive = rep.outcome is ProcOutcome.ALIVE
        if self.obs_kind == "pixels":
            return self.sup.backend.frame() if alive else None
        if not alive:
            return {}
        return dict(self.sup.backend.semantic("read", {f: None for f in self.obs_fields}))

    @staticmethod
    def _info(rep: StepReport) -> dict[str, Any]:
        return {"tick": rep.tick, "epoch": rep.epoch, "verbclass": int(rep.verbclass),
                "restore_point": rep.restore_point, "outcome": rep.outcome.value,
                "events": [[e.name, e.tick, e.arg] for e in rep.events]}


def _jsonable(x: Any) -> Any:
    if isinstance(x, bytes):
        return {"b64": base64.b64encode(x).decode("ascii")}
    if isinstance(x, dict):
        return {str(k): _jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_jsonable(v) for v in x]
    return x


def handle(env: KotorEnv, req: Mapping[str, Any]) -> dict[str, Any]:
    op = req.get("op")
    if op == "reset":
        obs, info = env.reset(seed=req.get("seed"), options=req.get("options"))
        return {"obs": obs, "info": info}
    if op == "step":
        obs, r, term, trunc, info = env.step(req.get("action", "."))
        return {"obs": obs, "reward": r, "terminated": term, "truncated": trunc, "info": info}
    if op == "spaces":
        return {"action": repr(env.action_space), "observation": repr(env.observation_space)}
    if op == "render":
        return {"png": env.render()}
    if op == "close":
        env.close()
        return {}
    raise TelosError(f"unknown op {op!r}; expected reset, step, spaces, render or close")


def serve(env: KotorEnv, host: str = "127.0.0.1", port: int = 0, *, once: bool = False,
          ready: Callable[[int], None] | None = None) -> None:
    """Serve one env over NDJSON: {"id","op",...} -> {"id","ok","result"|"error"}.

    One client at a time, because one env owns one game process. A port in use is
    an error; this never picks another port.
    """
    srv = listen_socket(host, port)
    if ready:
        ready(srv.getsockname()[1])
    try:
        while True:
            conn, _ = srv.accept()
            with conn, conn.makefile("rb") as rf, conn.makefile("wb") as wf:
                for line in rf:
                    if not line.strip():
                        continue
                    rid, req = None, None
                    try:
                        req = json.loads(line)
                        rid = req.get("id")
                        out = {"id": rid, "ok": True, "result": _jsonable(handle(env, req))}
                    except Exception as e:
                        out = {"id": rid, "ok": False,
                               "error": {"code": type(e).__name__, "message": str(e)}}
                    wf.write(json.dumps(out, separators=(",", ":")).encode() + b"\n")
                    wf.flush()
                    if isinstance(req, dict) and req.get("op") == "close":
                        break
            if once:
                break
    finally:
        srv.close()
        try:
            env.close()
        except Exception as e:
            print(f"gym: close: {e}", file=sys.stderr)


__all__ = ["KotorEnv", "NoteSpace", "ObsSpace", "handle", "serve", "to_note"]
