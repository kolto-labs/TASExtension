"""Input-source wire contracts shared by native consumer adapters.

These are acknowledgements at a held game boundary, not a second input path.
Decoding does not engage a source, release a frame or qualify runtime fidelity.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

SOURCE_PROTOCOL = 'kptas-input-source/1'
PHYSICAL_PROTOCOL = 'kptas-physical-input/1'
BUTTONS = ('lmb', 'rmb', 'mmb')


def _gate(value, expected):
    if type(expected) is not int or expected < 0:
        raise ValueError('invalid expected input gate')
    if type(value) is not int or value != expected:
        raise ValueError('input acknowledgement gate mismatch')


@dataclass(frozen=True)
class SourceAcknowledgement:
    tick: int
    engaged: bool
    input_source: str

    @classmethod
    def decode(cls, value, *, tick, engaged):
        if type(engaged) is not bool:
            raise ValueError('engaged must be a boolean')
        source = 'authored' if engaged else 'physical'
        if (not isinstance(value, Mapping) or value.get('schema') != SOURCE_PROTOCOL
                or value.get('engaged') is not engaged or value.get('input_source') != source):
            raise ValueError('input source transition acknowledgement mismatch')
        _gate(value.get('tick'), tick)
        return cls(tick, engaged, source)


@dataclass(frozen=True)
class PhysicalCandidate:
    tick: int
    ticket: int
    keys: tuple[int, ...]
    buttons: tuple[str, ...]
    wheel: int
    absolute: tuple[int, int] | None
    osev: str | None

    @classmethod
    def decode(cls, value, *, tick):
        if not isinstance(value, Mapping) or value.get('schema') != PHYSICAL_PROTOCOL:
            raise ValueError('invalid physical candidate schema')
        _gate(value.get('tick'), tick)
        ticket = value.get('ticket')
        keys, buttons, wheel = value.get('keys'), value.get('buttons'), value.get('wheel')
        osev = value.get('osev')
        if (type(ticket) is not int or not 0 < ticket < 1 << 53
                or not isinstance(keys, list) or len(keys) > 255
                or any(type(key) is not int or not 0 < key < 256 for key in keys)
                or len(set(keys)) != len(keys)
                or not isinstance(buttons, list) or len(buttons) > len(BUTTONS)
                or any(type(button) is not str or button not in BUTTONS for button in buttons)
                or len(set(buttons)) != len(buttons)
                or type(wheel) is not int or not -(1 << 31) <= wheel < 1 << 31
                or osev not in (None, 'altf4')):
            raise ValueError('invalid physical candidate acknowledgement')
        absolute = None
        mouse = value.get('mouse')
        if mouse is not None:
            if (not isinstance(mouse, Mapping) or set(mouse) != {'abs'}
                    or not isinstance(mouse['abs'], list) or len(mouse['abs']) != 2
                    or any(type(coordinate) is not int or not -(1 << 31) <= coordinate < 1 << 31
                           for coordinate in mouse['abs'])):
                raise ValueError('invalid physical mouse candidate')
            absolute = tuple(mouse['abs'])
        return cls(tick, ticket, tuple(sorted(keys)),
                   tuple(button for button in BUTTONS if button in buttons), wheel, absolute, osev)
