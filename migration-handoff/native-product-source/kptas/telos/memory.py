"""Read-only RAM watch and RAM search over recorded frames (gap-analysis item 8).

Values come from the captured page store of a take (Bundle.map_at plus
content-addressed pages). Nothing here talks to the running game, and nothing
here writes memory: freezing a value is a memory write, which only lab runs
may do (docs/tiers.md). No Qt import.

Pointer paths follow LiveSplit DeepPointer: read u32 at `base`, then for every
offset but the last read u32 at (value + offset); the address is value + last
offset. With no offsets the address is `base` itself.
"""
from __future__ import annotations

import struct
import tomllib
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

try:  # Optional speed-up for search; the pure-Python path gives the same answers.
    import numpy as _np
except Exception:  # noqa: BLE001
    _np = None

PAGE = 4096
EXE_BASE = 0x00400000  # swkotor.exe image base (native/shim/src/shim.hpp, GOG 1.03)
TYPES = {'u8': ('<B', 1), 's8': ('<b', 1), 'u16': ('<H', 2), 's16': ('<h', 2),
         'u32': ('<I', 4), 's32': ('<i', 4), 'f32': ('<f', 4), 'f64': ('<d', 8),
         'ptr': ('<I', 4), 'str': (None, 16)}
SEARCH_TYPES = ('u8', 's8', 'u16', 's16', 'u32', 's32', 'f32', 'f64')
FORMATS = ('dec', 'hex')
COMPARISONS = ('exact', 'not_equal', 'greater', 'less', 'changed', 'unchanged', 'increased', 'decreased')
NEEDS_VALUE = frozenset({'exact', 'not_equal', 'greater', 'less'})
MAX_RESULTS = 1000
MAX_WATCHES = 256
LOOKBACK = 4096          # frames searched back for the nearest captured frame
WALK_LIMIT = 2048        # frames walked forward from a cached map before map_at is used instead
FREEZE_REFUSAL = ('Freeze is off. Freezing writes game memory, and only lab runs may write memory. '
                  'TAS runs stay read-only, so the recording replays the same way.')


class MemoryError_(ValueError):
    """A watch or search request that cannot be answered from the recording."""


# ---------------------------------------------------------------- watches

@dataclass(frozen=True)
class Watch:
    name: str
    base: int
    offsets: tuple[int, ...] = ()
    type: str = 'u32'
    fmt: str = 'dec'
    length: int = 16          # bytes read for type 'str'
    status: str = 'unverified'
    source: str = ''
    note: str = ''

    def __post_init__(self):
        if not isinstance(self.name, str) or not self.name.strip() or len(self.name) > 80:
            raise MemoryError_('watch name must be 1-80 characters')
        if self.type not in TYPES:
            raise MemoryError_(f'watch type must be one of {", ".join(TYPES)}')
        if self.fmt not in FORMATS:
            raise MemoryError_('watch format must be dec or hex')
        if not 0 <= self.base <= 0xFFFFFFFF or len(self.offsets) > 12:
            raise MemoryError_('watch address must be a 32-bit value with at most 12 offsets')
        if not 1 <= self.length <= 256:
            raise MemoryError_('string length must be 1-256 bytes')
        if self.status not in ('verified', 'unverified'):
            raise MemoryError_('watch status must be verified or unverified')

    @property
    def address_text(self) -> str:
        return format_address(self.base, self.offsets)


def format_address(base: int, offsets=()) -> str:
    parts = [f'0x{base:08X}'] + [('-' if o < 0 else '') + f'0x{abs(o):X}' for o in offsets]
    return ', '.join(parts)


def parse_address(text: str) -> tuple[int, tuple[int, ...]]:
    """'0x7A39FC, 0x4, 0x4' or 'swkotor.exe+0x3A39E8, 0x4C, 0x0'. Numbers are hex, 0x optional."""
    tokens = [t.strip() for t in str(text).replace('->', ',').replace(';', ',').split(',') if t.strip()]
    if not tokens:
        raise MemoryError_('enter an address, such as 0x7A39FC')
    def num(tok):
        neg = tok.startswith('-')
        tok = tok.lstrip('+-').strip()
        try:
            v = int(tok[2:] if tok.lower().startswith('0x') else tok, 16)
        except ValueError:
            raise MemoryError_(f'{tok!r} is not a hex number') from None
        return -v if neg else v
    head = tokens[0]
    low = head.lower()
    if low.startswith(('swkotor.exe+', 'exe+')):
        base = EXE_BASE + num(head.split('+', 1)[1])
    else:
        base = num(head)
    if not 0 <= base <= 0xFFFFFFFF:
        raise MemoryError_('address must fit in 32 bits')
    return base, tuple(num(t) for t in tokens[1:])


def decode(raw: bytes, typ: str):
    fmt = TYPES[typ][0]
    if fmt is None:
        text = raw.split(b'\0', 1)[0].decode('latin-1')
        return ''.join(ch if ch.isprintable() else '?' for ch in text)
    return struct.unpack(fmt, raw)[0]


def size_of(typ: str, length: int = 16) -> int:
    return length if typ == 'str' else TYPES[typ][1]


def format_value(value, typ: str, fmt: str = 'dec') -> str:
    if value is None:
        return '—'
    if typ == 'str':
        return repr(value)
    if typ == 'ptr':
        return f'0x{value:08X}'
    if typ in ('f32', 'f64'):
        if fmt == 'hex':
            bits = struct.unpack('<I' if typ == 'f32' else '<Q', struct.pack(TYPES[typ][0], value))[0]
            return f'0x{bits:0{8 if typ == "f32" else 16}X}'
        return f'{value:.9g}' if typ == 'f32' else f'{value:.17g}'
    if fmt == 'hex':
        size = TYPES[typ][1]
        return f'0x{value & ((1 << (8 * size)) - 1):0{2 * size}X}'
    return str(value)


def parse_value(text: str, typ: str):
    """A typed search value. Floats accept 0x… as raw bits; results are rounded to the type."""
    text = str(text).strip()
    if not text:
        raise MemoryError_('enter a value')
    fmt, size = TYPES[typ]
    try:
        if typ in ('f32', 'f64'):
            if text.lower().startswith('0x'):
                return struct.unpack(fmt, int(text, 16).to_bytes(size, 'little'))[0]
            return struct.unpack(fmt, struct.pack(fmt, float(text)))[0]
        value = int(text, 0)
        struct.pack(fmt, value)
        return value
    except (ValueError, struct.error, OverflowError):
        raise MemoryError_(f'{text!r} is not a valid {typ} value') from None


# ---------------------------------------------------------------- one frame of memory

class FrameImage:
    """Memory of one captured frame: a PageMap plus a page fetcher. Read-only."""

    def __init__(self, pmap, fetch, tick: int, epoch: int, requested: int | None = None):
        self.map = pmap
        self.fetch = fetch
        self.tick = tick
        self.epoch = epoch
        self.requested = tick if requested is None else requested
        self._pages: OrderedDict[int, bytes | None] = OrderedDict()

    def ref(self, vaddr: int):
        return self.map.get(vaddr & ~0xFFF)

    def page(self, vaddr: int) -> bytes | None:
        vaddr &= ~0xFFF
        if vaddr in self._pages:
            return self._pages[vaddr]
        ref = self.map.get(vaddr)
        data = None
        if ref is not None:
            try:
                data = self.fetch(ref.key)
            except KeyError:  # MissingObject / EvictedError: budget removed this page
                data = None
        self._pages[vaddr] = data
        while len(self._pages) > 256:
            self._pages.popitem(last=False)
        return data

    def read(self, addr: int, n: int) -> bytes | None:
        if addr < 0 or addr + n > 1 << 32:
            return None
        out = bytearray()
        while n > 0:
            data = self.page(addr)
            if data is None:
                return None
            off = addr & 0xFFF
            take = min(n, PAGE - off)
            out += data[off:off + take]
            addr += take
            n -= take
        return bytes(out)


@dataclass(frozen=True)
class WatchValue:
    address: int | None
    value: object = None
    error: str = ''


def resolve(image: FrameImage, watch: Watch) -> tuple[int | None, str]:
    if not watch.offsets:
        return watch.base, ''
    raw = image.read(watch.base, 4)
    if raw is None:
        return None, f'0x{watch.base:08X} is not in this frame'
    ptr = struct.unpack('<I', raw)[0]
    for step, off in enumerate(watch.offsets[:-1], 1):
        if ptr == 0:
            return None, f'null pointer at step {step}'
        raw = image.read(ptr + off, 4)
        if raw is None:
            return None, f'step {step} points outside recorded memory'
        ptr = struct.unpack('<I', raw)[0]
    if ptr == 0:
        return None, f'null pointer at step {len(watch.offsets)}'
    return ptr + watch.offsets[-1], ''


def read_watch(image: FrameImage, watch: Watch) -> WatchValue:
    addr, error = resolve(image, watch)
    if addr is None:
        return WatchValue(None, None, error)
    raw = image.read(addr, size_of(watch.type, watch.length))
    if raw is None:
        return WatchValue(addr, None, 'not in recorded memory')
    return WatchValue(addr, decode(raw, watch.type))


# ---------------------------------------------------------------- a recording, read-only

class RecordingMemory:
    """Opens one take read-only and answers memory at any recorded frame.

    Not thread-safe: Bundle and Store share file handles. Use one worker thread.
    """

    def __init__(self, path: str | Path, *, opener=None):
        self.path = str(path)
        self._opener = opener
        self.bundle = None
        self._maps: OrderedDict[int, object] = OrderedDict()   # captured tick -> PageMap (private copy)

    def open(self):
        if self.bundle is None:
            if self._opener is not None:
                self.bundle = self._opener(self.path)
            else:
                from kptas.holo.bundle import Bundle
                self.bundle = Bundle.open(self.path)   # writable=False: no locks, no index writes
        return self

    def close(self):
        if self.bundle is not None:
            try:
                self.bundle.close()
            finally:
                self.bundle = None
                self._maps.clear()

    @property
    def last_tick(self) -> int:
        return self.open().bundle.last_tick

    def _record(self, tick):
        try:
            return self.bundle.record(tick)
        except (KeyError, ValueError):
            return None

    def _captured(self, rec) -> bool:
        from kptas.types import TickFlag
        return rec is not None and bool(rec.flags & TickFlag.CAPTURED)

    def frame_tick(self, tick: int) -> tuple[int | None, str]:
        """Nearest captured frame at or before `tick` in the same process run."""
        self.open()
        rec = self._record(tick)
        if rec is None:
            return None, f'frame {tick} is not in this recording yet'
        epoch = rec.epoch
        for t in range(tick, max(-1, tick - LOOKBACK), -1):
            r = rec if t == tick else self._record(t)
            if r is None:
                break
            if r.epoch != epoch:
                return None, 'no capture in this process run before this frame'
            if self._captured(r):
                return t, ''
        return None, f'no captured frame in the last {LOOKBACK} frames'

    def _pagemap(self, tick: int):
        from kptas.holo.pages import PageMap
        hit = self._maps.get(tick)
        if hit is not None:
            self._maps.move_to_end(tick)
            return hit
        rec = self._record(tick)
        pm = None
        # Walk forward from the nearest earlier cached map in the same run; the
        # recorded state root proves the result, otherwise rebuild with map_at.
        earlier = [t for t in self._maps if t < tick and tick - t <= WALK_LIMIT]
        if earlier and rec is not None:
            start = max(earlier)
            if self._record(start) is not None and self._record(start).epoch == rec.epoch:
                pm = PageMap(self._maps[start].refs())
                ok = True
                for t in range(start + 1, tick + 1):
                    r = self._record(t)
                    if r is None or r.epoch != rec.epoch:
                        ok = False
                        break
                    if self._captured(r):
                        pm.apply(r.pages)
                if not ok or pm.root() != rec.state_root:
                    pm = None
        if pm is None:
            pm = self.bundle.map_at(tick)
        self._maps[tick] = pm
        while len(self._maps) > 6:
            self._maps.popitem(last=False)
        return pm

    def image(self, tick: int) -> FrameImage:
        used, why = self.frame_tick(tick)
        if used is None:
            raise MemoryError_(why)
        pm = self._pagemap(used)
        return FrameImage(pm, self.bundle.page, used, self._record(used).epoch, requested=tick)

    def series(self, watch: Watch, tick: int, span: int = 300, max_points: int = 400):
        """[(captured tick, value or None)] for frames tick-span..tick of the same run.

        One map is walked forward and checked against each recorded root.
        """
        from kptas.holo.pages import PageMap
        self.open()
        end, why = self.frame_tick(tick)
        if end is None:
            raise MemoryError_(why)
        epoch = self._record(end).epoch
        ticks = []
        for t in range(end, max(-1, end - span - 1), -1):
            r = self._record(t)
            if r is None or r.epoch != epoch:
                break
            if self._captured(r):
                ticks.append(t)
        ticks.reverse()
        if len(ticks) > max_points:
            stride = len(ticks) / max_points
            ticks = [ticks[int(i * stride)] for i in range(max_points)] + [ticks[-1]]
        if not ticks:
            return []
        out = []
        pm = PageMap(self._pagemap(ticks[0]).refs())
        at = ticks[0]
        for t in ticks:
            if t != at:
                for u in range(at + 1, t + 1):
                    r = self._record(u)
                    if self._captured(r):
                        pm.apply(r.pages)
                at = t
                if pm.root() != self._record(t).state_root:
                    pm = PageMap(self.bundle.map_at(t).refs())
            img = FrameImage(pm, self.bundle.page, t, epoch)
            out.append((t, read_watch(img, watch).value))
        return out


# ---------------------------------------------------------------- RAM search

REGIONS = ('all', 'exe', 'heap')


def _region_ok(ref, region: str) -> bool:
    if region == 'all':
        return True
    from kptas.types import MemClass
    return (ref.mclass == MemClass.IMAGE_DATA) if region == 'exe' else (ref.mclass == MemClass.GAME_ARENA)


def _np_dtype(typ):
    return {'u8': '<u1', 's8': '<i1', 'u16': '<u2', 's16': '<i2', 'u32': '<u4', 's32': '<i4',
            'f32': '<f4', 'f64': '<f8'}[typ]


def _bits_dtype(typ):
    return {1: '<u1', 2: '<u2', 4: '<u4', 8: '<u8'}[TYPES[typ][1]]


class RamSearch:
    """Cheat Engine / BizHawk style narrowing over recorded frames.

    `start` takes a frame; each `narrow` compares a later (or the same) frame
    against the given value or against the frame of the previous step.
    Candidates: None means every address of the start frame's pages.
    """

    def __init__(self, typ: str = 'u32', aligned: bool = True, region: str = 'all'):
        if typ not in SEARCH_TYPES:
            raise MemoryError_(f'search type must be one of {", ".join(SEARCH_TYPES)}')
        if region not in REGIONS:
            raise MemoryError_('region must be all, exe or heap')
        self.type = typ
        self.size = TYPES[typ][1]
        self.step = self.size if aligned else 1
        self.region = region
        self.previous: FrameImage | None = None
        self.candidates: dict[int, object] | None = None
        self.history: list[tuple[int, str, str, int]] = []   # (tick, comparison, value text, count)

    @property
    def started(self) -> bool:
        return self.previous is not None

    def start(self, image: FrameImage) -> int:
        self.previous = image
        self.candidates = None
        self.history = [(image.tick, 'start', '', self.count())]
        return self.history[-1][3]

    def _all_offsets(self):
        n = (PAGE - self.size) // self.step + 1
        return _np.arange(0, n * self.step, self.step, dtype=_np.uint16) if _np is not None else range(0, n * self.step, self.step)

    def _pages(self):
        if self.candidates is not None:
            return self.candidates.items()
        prev = self.previous
        return ((r.vaddr, None) for r in prev.map.refs() if _region_ok(r, self.region))

    def count(self) -> int:
        if self.previous is None:
            return 0
        if self.candidates is None:
            per = (PAGE - self.size) // self.step + 1
            return per * sum(1 for r in self.previous.map.refs() if _region_ok(r, self.region))
        return sum(len(v) for v in self.candidates.values())

    def narrow(self, image: FrameImage, comparison: str, value=None, value_text: str = '') -> int:
        if self.previous is None:
            raise MemoryError_('start a search first')
        if comparison not in COMPARISONS:
            raise MemoryError_('unknown comparison')
        if comparison in NEEDS_VALUE and value is None:
            raise MemoryError_('this comparison needs a value')
        if image.epoch != self.previous.epoch:
            raise MemoryError_('this frame is in another process run; heap addresses differ. Start a new search.')
        prev = self.previous
        out: dict[int, object] = {}
        for vaddr, offs in list(self._pages()):
            cref, pref = image.ref(vaddr), prev.ref(vaddr)
            if cref is None:
                continue
            if offs is None:
                offs = self._all_offsets()
            if comparison not in NEEDS_VALUE and pref is not None and cref.key == pref.key:
                if comparison == 'unchanged':
                    out[vaddr] = offs
                continue  # identical page: nothing changed, increased or decreased
            cur = image.page(vaddr)
            old = prev.page(vaddr) if comparison not in NEEDS_VALUE else None
            if cur is None or (comparison not in NEEDS_VALUE and old is None):
                continue
            kept = self._filter(cur, old, offs, comparison, value)
            if len(kept):
                out[vaddr] = kept
        self.candidates = out
        self.previous = image
        n = self.count()
        self.history.append((image.tick, comparison, value_text, n))
        return n

    def _filter(self, cur, old, offs, comparison, value):
        typ = self.type
        if _np is not None:
            offs = _np.asarray(offs, dtype=_np.int64)
            def view(page, dtype):
                arr = _np.ndarray(shape=(PAGE - self.size + 1,), dtype=dtype, buffer=page, strides=(1,))
                return arr[offs]
            c = view(cur, _np_dtype(typ))
            if comparison in ('exact', 'not_equal'):
                if typ in ('f32', 'f64'):   # exact means the same bits
                    want = _np.frombuffer(struct.pack(TYPES[typ][0], value), dtype=_bits_dtype(typ))[0]
                    eq = view(cur, _bits_dtype(typ)) == want
                else:
                    eq = c == value
                mask = eq if comparison == 'exact' else ~eq
            elif comparison == 'greater':
                mask = c > value
            elif comparison == 'less':
                mask = c < value
            else:
                o = view(old, _np_dtype(typ))
                if comparison in ('changed', 'unchanged'):
                    same = view(cur, _bits_dtype(typ)) == view(old, _bits_dtype(typ))
                    mask = ~same if comparison == 'changed' else same
                elif comparison == 'increased':
                    mask = c > o
                else:
                    mask = c < o
            return offs[mask].astype(_np.uint16)
        fmt, size = TYPES[typ]
        bits = {1: '<B', 2: '<H', 4: '<I', 8: '<Q'}[size]
        kept = []
        want_bits = struct.unpack(bits, struct.pack(fmt, value))[0] if (
            comparison in ('exact', 'not_equal') and typ in ('f32', 'f64')) else None
        for o in offs:
            c = struct.unpack_from(fmt, cur, o)[0]
            if comparison in ('exact', 'not_equal'):
                eq = (struct.unpack_from(bits, cur, o)[0] == want_bits) if want_bits is not None else c == value
                ok = eq if comparison == 'exact' else not eq
            elif comparison == 'greater':
                ok = c > value
            elif comparison == 'less':
                ok = c < value
            elif comparison in ('changed', 'unchanged'):
                same = struct.unpack_from(bits, cur, o)[0] == struct.unpack_from(bits, old, o)[0]
                ok = (not same) if comparison == 'changed' else same
            else:
                p = struct.unpack_from(fmt, old, o)[0]
                ok = c > p if comparison == 'increased' else c < p
            if ok:
                kept.append(o)
        return kept

    def results(self, image: FrameImage | None = None, limit: int = MAX_RESULTS):
        """First `limit` candidates as (address, value in `image`). Explicit candidates only."""
        if self.candidates is None:
            return []
        image = image or self.previous
        out = []
        for vaddr in sorted(self.candidates):
            page = image.page(vaddr)
            for o in self.candidates[vaddr]:
                o = int(o)
                value = decode(page[o:o + self.size], self.type) if page is not None else None
                out.append((vaddr + o, value))
                if len(out) >= limit:
                    return out
        return out


# ---------------------------------------------------------------- watch files (TOML)

def _toml_str(s: str) -> str:
    return '"' + s.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n') + '"'


def dump_watches(watches) -> str:
    lines = ['# Stasis memory watches. Addresses are hex; offsets follow LiveSplit pointer paths.', '']
    for w in watches:
        lines += ['[[watch]]', f'name = {_toml_str(w.name)}', f'address = {_toml_str(w.address_text)}',
                  f'type = {_toml_str(w.type)}', f'format = {_toml_str(w.fmt)}']
        if w.type == 'str':
            lines.append(f'length = {w.length}')
        lines.append(f'status = {_toml_str(w.status)}')
        if w.source:
            lines.append(f'source = {_toml_str(w.source)}')
        if w.note:
            lines.append(f'note = {_toml_str(w.note)}')
        lines.append('')
    return '\n'.join(lines)


def load_watches(text: str) -> list[Watch]:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise MemoryError_(f'not a watch file: {exc}') from None
    entries = data.get('watch', [])
    if not isinstance(entries, list):
        raise MemoryError_('a watch file holds [[watch]] tables')
    out = []
    for i, e in enumerate(entries[:MAX_WATCHES], 1):
        if not isinstance(e, dict):
            raise MemoryError_(f'watch {i} is not a table')
        try:
            base, offsets = parse_address(e.get('address', ''))
            out.append(Watch(str(e.get('name', '')), base, offsets, str(e.get('type', 'u32')),
                             str(e.get('format', 'dec')), int(e.get('length', 16)),
                             str(e.get('status', 'unverified')), str(e.get('source', '')), str(e.get('note', ''))))
        except (MemoryError_, TypeError, ValueError) as exc:
            raise MemoryError_(f'watch {i}: {exc}') from None
    return out


# ---------------------------------------------------------------- KotOR K1 seed (GOG 1.03 swkotor.exe)

ASL = ('kotor-speedruns/Autosplitter K1.asl @2060ae5 '
       '(https://github.com/kotor-speedruns/Autosplitter/blob/2060ae5ea8f38f02520111cf4e5a1e4e9f040388/K1.asl)')
SHIM = 'native/shim/src/shim.hpp ADDR_APP_MANAGER + journal.cpp semantic() offsets (GOG 1.03, disassembly)'
_APP = 0x007A39FC


def _w(name, base, offsets, typ, status, source, note='', fmt='dec', length=16):
    return Watch(name, base, tuple(offsets), typ, fmt, length, status, source, note)


KOTOR_K1_SEED = (
    _w('Area module name', 0x007A39E8, (0x4C, 0x0), 'str', 'verified', ASL,
       'ASL "area" (string10). Read "tar_m02ac" from recorded frames.'),
    _w('Area tick count', 0x007B935C, (0x54, 0x64, 0x18C), 'u32', 'verified', ASL,
       'ASL "tickcount". Recorded frames: +422 over 422 frames.'),
    _w('App manager', _APP, (), 'ptr', 'verified', ASL + '; ' + SHIM,
       'ASL base 0x3A39FC + image base 0x400000 equals the shim constant.'),
    _w('Client anr (+0x288)', _APP, (0x4, 0x4, 0x288), 'u32', 'verified', SHIM, 'Journal sem.anr.'),
    _w('Client module (+0x18)', _APP, (0x4, 0x4, 0x18), 'ptr', 'verified', SHIM, 'Journal sem.cmod is this != 0.'),
    _w('Client cpid (+0x20)', _APP, (0x4, 0x4, 0x20), 'u32', 'verified', SHIM, 'Journal sem.cpid.'),
    _w('Client idis (+0x2BC)', _APP, (0x4, 0x4, 0x2BC), 'u32', 'verified', SHIM, 'Journal sem.idis.'),
    _w('Client lsg (+0x33C)', _APP, (0x4, 0x4, 0x33C), 'u32', 'verified', SHIM, 'Journal sem.lsg.'),
    _w('Client mm (+0x280)', _APP, (0x4, 0x4, 0x280), 'u32', 'verified', SHIM, 'Journal sem.mm is this != 0.'),
    _w('Server pause (+0x10078)', _APP, (0x8, 0x4, 0x10078), 'u8', 'verified', SHIM, 'Journal sem.pause (low byte).'),
    _w('Server mtm (+0x10080)', _APP, (0x8, 0x4, 0x10080), 'u32', 'verified', SHIM, 'Journal sem.mtm.'),
    _w('Server lmod (+0x100B4)', _APP, (0x8, 0x4, 0x100B4), 'u32', 'verified', SHIM, 'Journal sem.lmod.'),
    _w('Server lfs (+0x100C4)', _APP, (0x8, 0x4, 0x100C4), 'u32', 'verified', SHIM, 'Journal sem.lfs.'),
    _w('Load bar', _APP, (0x4, 0x4, 0x278, 0xC4), 's32', 'unverified', ASL,
       'ASL "loadBar"; > 0 while loading. Path resolves; only 0 seen so far.'),
    _w('End state', 0x007BB4E4, (), 'u32', 'unverified', ASL,
       'ASL "endState"; splits when 1132924759 in STA_M45AD. Address reads; meaning not checked.'),
    _w('Message box', _APP, (0x4, 0x4, 0x40, 0x98), 'ptr', 'unverified', ASL, 'ASL "messageBoxPTR".'),
    _w('Message box callback', _APP, (0x4, 0x4, 0x40, 0x98, 0x68), 'ptr', 'unverified', ASL,
       'ASL "messageBoxCallbackPTR"; 0x5F1BD0 marks the AMG box.'),
    _w('Modal count', _APP, (0x4, 0x4, 0x40, 0x38, 0x98), 'u32', 'unverified', ASL, 'ASL "modalCount".'),
    _w('Modal stack first', _APP, (0x4, 0x4, 0x40, 0x38, 0x94, 0x0), 'ptr', 'unverified', ASL,
       'ASL "modalStackFirstPTR".'),
    _w('Modal stack second', _APP, (0x4, 0x4, 0x40, 0x38, 0x94, 0x4), 'ptr', 'unverified', ASL,
       'ASL "modalStackSecondPTR".'),
)


def watch_file() -> Path:
    """The saved watch list next to keymap.toml (normally ~/.config/kptas/memory-watches.toml)."""
    try:
        from .ui_profiles import profile_directory
        return profile_directory().parent / 'memory-watches.toml'
    except Exception:  # noqa: BLE001
        return Path.home() / '.config' / 'kptas' / 'memory-watches.toml'
