"""Semantic RPG schedules, independent of environment and physical input scores."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from collections.abc import Mapping
from typing import Any
import tomllib

from kptas.config import ConfigError
from kptas.engine_control import validate_schedule


@dataclass(frozen=True)
class Plan:
    duration: int
    hz: int
    commands: tuple[Mapping[str, Any], ...]

    def __post_init__(self) -> None:
        if type(self.duration) is not int or not 1 <= self.duration <= 2**31 - 1:
            raise ConfigError('plan.duration must be an integer in 1..2147483647')
        if type(self.hz) is not int or not 1 <= self.hz <= 1000:
            raise ConfigError('plan.hz must be an integer in 1..1000')
        try:
            rows = validate_schedule(list(self.commands))
        except ValueError as exc:
            raise ConfigError(str(exc)) from None
        if any(row['at'] >= self.duration for row in rows):
            raise ConfigError('command at must be less than plan.duration')
        from types import MappingProxyType
        object.__setattr__(self, 'commands', tuple(MappingProxyType(row) for row in rows))

    @classmethod
    def from_legacy(cls, environment: Mapping[str, Any], *, duration: int, hz: int) -> Plan:
        """Explicitly normalize embedded commands without rewriting the source file."""
        return cls(duration, hz, tuple(environment.get('engine', {}).get('commands', [])))


def loads(text: str) -> Plan:
    try:
        obj = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f'semantic plan: {exc}') from None
    if obj.keys() - {'plan', 'commands'}:
        raise ConfigError('semantic plan accepts only [plan] and [[commands]]')
    meta = obj.get('plan')
    if not isinstance(meta, dict) or set(meta) != {'duration', 'hz'}:
        raise ConfigError('[plan] requires exactly duration and hz')
    commands = obj.get('commands', [])
    if not isinstance(commands, list):
        raise ConfigError('commands must be an array of command tables')
    return Plan(meta['duration'], meta['hz'], tuple(commands))


def load(path: str | Path) -> Plan:
    return loads(Path(path).read_text(encoding='utf-8'))
