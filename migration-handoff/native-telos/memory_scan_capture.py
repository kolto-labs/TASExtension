"""Read browser scan artifacts as captured evidence, without a process backend."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import math
import re
import struct

LIMIT = 2 * 1024 * 1024
MAX_BYTES = 65536
TYPES = {'u8': (1, 'B'), 'u16': (2, 'H'), 'u32': (4, 'I'),
         'i32': (4, 'i'), 'f32': (4, 'f'), 'f64': (8, 'd')}
_CAPTURE_FIELDS = {'session', 'pid', 'workspaceEpoch', 'capturedAt', 'address',
                   'byteLength', 'valueType', 'pointerBits', 'byteOrder',
                   'source', 'targetDeclaration', 'bytes'}
_FIELDS = {'schema', 'scope', 'session', 'pid', 'exportedAt', 'capture',
           'previousCapture', 'comparison', 'matchedOffsets', 'displayedRowLimit',
           'matchedOffsetsComplete', 'qualification'}


def _integer(value, low, high, label):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f'{label}: expected an integer from {low} to {high}')
    return value


def _text(value, limit, label):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f'{label}: expected bounded text')
    return value


def _timestamp(value, label):
    _text(value, 64, label)
    try:
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as exc:
        raise ValueError(f'{label}: expected an ISO timestamp') from exc
    if stamp.tzinfo is None:
        raise ValueError(f'{label}: timestamp must declare a timezone')


def _object(value, fields, label):
    if type(value) is not dict or set(value) != fields:
        raise ValueError(f'{label}: expected the versioned fields')


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON property')
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError(f'Non-finite JSON constant: {value}')


def _target(value):
    if value is None:
        return
    fields = {'profile', 'executable', 'sha256', 'format', 'architecture',
              'osAbi', 'pointerBits', 'byteOrder'}
    _object(value, fields, 'target declaration')
    for key in fields - {'sha256', 'pointerBits'}:
        if value[key] is not None:
            _text(value[key], 256, 'target ' + key)
    if value['sha256'] is not None and (not isinstance(value['sha256'], str)
            or re.fullmatch(r'[a-fA-F0-9]{64}', value['sha256']) is None):
        raise ValueError('Invalid declared executable SHA-256')
    if value['pointerBits'] is not None and (type(value['pointerBits']) is not int
            or value['pointerBits'] not in (8, 16, 32, 64, 128)):
        raise ValueError('Invalid declared pointer width')


def _capture(value):
    _object(value, _CAPTURE_FIELDS, 'capture')
    _text(value['session'], 128, 'capture session')
    _integer(value['pid'], 1, 0xffffffff, 'PID')
    _integer(value['workspaceEpoch'], 0, 9007199254740991, 'workspace epoch')
    _timestamp(value['capturedAt'], 'capture time')
    address = _integer(value['address'], 0, 0xffffffff, 'capture address')
    length = _integer(value['byteLength'], 1, MAX_BYTES, 'capture length')
    kind = value['valueType']
    if not isinstance(kind, str) or kind not in TYPES:
        raise ValueError('Unsupported captured value type')
    if length < TYPES[kind][0] or address + length > 0x100000000:
        raise ValueError('Capture overflows the declared address space or value width')
    if type(value['pointerBits']) is not int or value['pointerBits'] != 32:
        raise ValueError('Only declared 32-bit captured address spaces are supported')
    if value['byteOrder'] not in ('little', 'big'):
        raise ValueError('Unsupported captured byte order')
    if value['source'] is not None:
        _text(value['source'], 256, 'capture source')
    _target(value['targetDeclaration'])
    if type(value['bytes']) is not list or len(value['bytes']) != length:
        raise ValueError('Incomplete captured bytes')
    for item in value['bytes']:
        _integer(item, 0, 255, 'captured byte')


def _offsets(value, length, size, label):
    if type(value) is not list or len(value) > MAX_BYTES:
        raise ValueError(f'{label}: expected bounded offsets')
    seen = set()
    for offset in value:
        _integer(offset, 0, length - size, label)
        if offset in seen:
            raise ValueError(f'{label}: duplicate offset')
        seen.add(offset)
    return seen


@dataclass(frozen=True)
class ScanCapture:
    raw: bytes
    document: dict
    sha256: str

    def rows(self, start=0, count=200):
        """Decode retained bytes only; imported match claims are not recomputed."""
        capture = self.document['capture']
        previous = self.document['previousCapture']
        size, code = TYPES[capture['valueType']]
        data = bytes(capture['bytes'])
        prior = bytes(previous['bytes']) if previous is not None else None
        fmt = ('<' if capture['byteOrder'] == 'little' else '>') + code
        result = []
        for offset in self.document['matchedOffsets'][start:start + count]:
            current = struct.unpack_from(fmt, data, offset)[0]
            before = struct.unpack_from(fmt, prior, offset)[0] if prior is not None else None
            result.append((offset, capture['address'] + offset, repr(current),
                           repr(before) if prior is not None else 'Absent',
                           data[offset:offset + size].hex()))
        return result


def load_capture(path):
    """Read a selected local file once. No memory capture, backend or execution."""
    with open(path, 'rb') as stream:
        raw = stream.read(LIMIT + 1)
    if not raw or len(raw) > LIMIT:
        raise ValueError('Captured scan must be at most 2 MiB')
    try:
        doc = json.loads(raw, object_pairs_hook=_unique_object,
                         parse_constant=_reject_constant)
    except (UnicodeError, RecursionError, json.JSONDecodeError) as exc:
        raise ValueError('Captured scan is not bounded JSON') from exc
    _object(doc, _FIELDS, 'captured scan')
    if doc['schema'] != 'telos.memory-scan-capture/v1' or doc['scope'] != 'process':
        raise ValueError('Unsupported captured scan schema or scope')
    _capture(doc['capture'])
    current = doc['capture']
    if doc['session'] != current['session'] or type(doc['pid']) is not int or doc['pid'] != current['pid']:
        raise ValueError('Capture identity does not match the export envelope')
    _timestamp(doc['exportedAt'], 'export time')
    _text(doc['qualification'], 4096, 'export qualification')
    if type(doc['displayedRowLimit']) is not int or doc['displayedRowLimit'] != 200 or doc['matchedOffsetsComplete'] is not True:
        raise ValueError('Unsupported match completeness or row limit')
    prior = doc['previousCapture']
    if prior is not None:
        _capture(prior)
        for key in ('session', 'pid', 'workspaceEpoch', 'address', 'byteLength',
                    'valueType', 'pointerBits', 'byteOrder', 'source'):
            if prior[key] != current[key]:
                raise ValueError('Previous capture has a different owner, range or memory model')
    criteria = doc['comparison']
    _object(criteria, {'rule', 'value', 'stride', 'consideredOffsets', 'initialCandidateBasis'}, 'comparison')
    rule = criteria['rule']
    if rule not in ('all', 'equal', 'changed', 'same', 'increased', 'decreased'):
        raise ValueError('Unsupported comparison rule')
    if rule in ('changed', 'same', 'increased', 'decreased') and prior is None:
        raise ValueError('Comparison requires previous captured bytes')
    size = TYPES[current['valueType']][0]
    stride = _integer(criteria['stride'], 1, size, 'stride')
    if stride not in (1, size):
        raise ValueError('Unsupported scan stride')
    value = criteria['value']
    if rule == 'equal':
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ValueError('Equality requires a finite comparison value')
        integer_ranges = {'u8': (0, 255), 'u16': (0, 65535),
                          'u32': (0, 0xffffffff), 'i32': (-0x80000000, 0x7fffffff)}
        if current['valueType'] in integer_ranges:
            _integer(value, *integer_ranges[current['valueType']], 'comparison value')
    elif value is not None:
        raise ValueError('Only equality may declare a comparison value')
    matched = _offsets(doc['matchedOffsets'], current['byteLength'], size, 'matched offset')
    considered = criteria['consideredOffsets']
    basis = 'previous-matched-offsets' if prior is not None else 'explicit-range-and-stride'
    if criteria['initialCandidateBasis'] != basis:
        raise ValueError('Comparison candidate basis does not match the captures')
    if prior is None:
        if considered is not None or any(offset % stride for offset in matched):
            raise ValueError('Initial candidate offsets do not match the declared stride')
    else:
        admitted = _offsets(considered, current['byteLength'], size, 'considered offset')
        if not matched.issubset(admitted):
            raise ValueError('Matched offsets exceed the declared considered offsets')
    return ScanCapture(raw, doc, hashlib.sha256(raw).hexdigest())
