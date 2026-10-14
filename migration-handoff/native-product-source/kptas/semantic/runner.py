"""Adapt semantic plans to the existing provider, supervisor and journal recorder."""
from __future__ import annotations

import copy
from pathlib import Path
from collections.abc import Mapping
from typing import Any

from kptas.config import ConfigError, resolve
from kptas.score import compile_score
from kptas.engine_control import validate_schedule
from .plan import Plan


def prepare(plan: Plan, environment: Mapping[str, Any], *, source: str = '<environment>'):
    """Return existing runtime configuration and an internal interval clock.

    The generated empty notes pace the existing supervisor. Authors supply no
    physical-input score. Command observations remain provider intervals, with
    the existing NOT_CAPTURED coverage and restore limitations.
    """
    if not isinstance(environment, Mapping):
        raise ConfigError(f'{source}: environment must be a table')
    user = copy.deepcopy(dict(environment))
    for table in ('engine', 'run', 'clock', 'input'):
        if table in user and not isinstance(user[table], dict):
            raise ConfigError(f'{source}: {table} must be a table')
    commands = user.get('engine', {}).get('commands', [])
    try:
        validate_schedule(commands)
    except ValueError as exc:
        raise ConfigError(f'{source}: engine.commands: {exc}') from None
    if commands:
        raise ConfigError('semantic plan and environment both supply commands; choose one source')
    if user.get('run', {}).get('score') or user.get('run', {}).get('max_ticks', 0):
        raise ConfigError('semantic plan owns duration; remove run.score and run.max_ticks')
    user.setdefault('run', {}).setdefault('tier', 'lab')
    user['run'].setdefault('backend', 'phasor')
    user.setdefault('input', {}).setdefault('path', 'semantic')
    user.setdefault('clock', {})['hz'] = plan.hz
    user.setdefault('engine', {})['commands'] = [dict(c) for c in plan.commands]
    cfg = resolve(user, source)
    if cfg['input']['path'] != 'semantic':
        raise ConfigError('semantic run requires input.path = "semantic"')
    score = compile_score(f"#!score v1 game={cfg['run']['game']} tier=lab hz={plan.hz}\n{plan.duration} .\n")
    return cfg, score


def run(plan: Plan, environment: Mapping[str, Any], destination: Path, *, source='<environment>', log=None):
    from kptas.telos.cli import build_header, pin_env
    from kptas.holo import Bundle
    from kptas.supervisor import BundleSink, Supervisor

    cfg, clock = prepare(plan, environment, source=source)
    env = pin_env(cfg)
    header = build_header(cfg, clock, env)
    destination.parent.mkdir(parents=True, exist_ok=True)
    sink = BundleSink(Bundle.create(destination, header, clock.text(), env, config=cfg), header.tier)
    sup = Supervisor(cfg, env=env, attach=True, sink=sink, run_name=destination.stem, log=log)
    sealed = False
    try:
        result = sup.run_score(clock)
        sealed = True
    finally:
        try:
            sup.close()
        except BaseException:
            sealed = False
            raise
        finally:
            sink.close(seal=sealed, backend_ceiling=sup.caps.tier_ceiling if sup.caps else None)
    return result
