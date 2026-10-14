"""Shared types for InputInterfaceTelos (import package ``kptas``).

docs/design/contract.md is normative. This module implements its byte layouts
and is checked against it; when the two disagree, fix this module.
All integers are little-endian. Standard library only.
"""

from __future__ import annotations

import enum
import hashlib
import json
import struct
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, fields
from typing import TYPE_CHECKING, Any, ClassVar, Literal, Protocol, runtime_checkable

if TYPE_CHECKING:
    from kptas.score import Note

PRODUCT = "InputInterfaceTelos"
PAGE_SIZE = 4096
PAGE_KEY_LEN = 16
DIGEST_LEN = 32
NULL_KEY = bytes(PAGE_KEY_LEN)
NULL_DIGEST = bytes(DIGEST_LEN)

WIRE_PROTO = "kptas-wire/1"
WIRE_MAX_LINE = 1 << 20


class FormatError(ValueError):
    """Bytes or values do not match a kptas layout."""


class BackendError(RuntimeError):
    """A backend could not carry out a request."""


class RefusedError(BackendError):
    """The verb is outside the locked tier's allowlist."""


def page_key(data: bytes) -> bytes:
    """Page identity: BLAKE2s, 16-byte digest, over exactly one 4096-byte page."""
    if len(data) != PAGE_SIZE:
        raise FormatError(f"a page is {PAGE_SIZE} bytes, got {len(data)}")
    return hashlib.blake2s(data, digest_size=PAGE_KEY_LEN).digest()


def blob_key16(data: bytes) -> bytes:
    """BLAKE2s-128 of arbitrary bytes (JOURNAL ring records, batch digests)."""
    return hashlib.blake2s(data, digest_size=PAGE_KEY_LEN).digest()


ZERO_PAGE_KEY = page_key(bytes(PAGE_SIZE))


def sha256(*parts: bytes) -> bytes:
    h = hashlib.sha256()
    for p in parts:
        h.update(p)
    return h.digest()


def canonical_json(obj: Any) -> bytes:
    """UTF-8 JSON with sorted keys and no whitespace. Used by every JSON chunk body."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def _names(cls: type) -> list[str]:
    """Instance field names of a dataclass, in order (ClassVar pseudo-fields excluded)."""
    return [f.name for f in fields(cls)]


def _need(cond: bool, msg: str) -> None:
    if not cond:
        raise FormatError(msg)


def _u(value: int, bits: int, what: str) -> None:
    _need(type(value) is int or isinstance(value, enum.IntEnum | enum.IntFlag),
          f"{what} must be an int")
    _need(0 <= int(value) < (1 << bits), f"{what}={value} does not fit u{bits}")


# ---------------------------------------------------------------- tiers and modes

class Tier(enum.IntEnum):
    """User-facing integrity tier. Higher is stricter: lab < tas < hands-off."""
    LAB = 0
    TAS = 1
    HANDS_OFF = 2

    @property
    def label(self) -> str:
        return ("lab", "tas", "hands-off")[self.value]

    def __str__(self) -> str:
        return self.label

    @classmethod
    def parse(cls, value: str | Tier) -> Tier:
        if isinstance(value, Tier):
            return value
        for t in cls:
            if t.label == value:
                return t
        raise ValueError(f"unknown tier {value!r}; expected lab, tas or hands-off")


class SeekMode(enum.StrEnum):
    REPLAY = "replay"
    RESTORE = "restore"
    HYBRID = "hybrid"
    SEMANTIC = "semantic"

    @classmethod
    def parse(cls, value: str | SeekMode) -> SeekMode:
        if isinstance(value, SeekMode):
            return value
        v = SEEK_ALIASES.get(value, value)
        try:
            return cls(v)
        except ValueError:
            raise ValueError(f"unknown seek mode {value!r}") from None

    def allowed(self, tier: Tier) -> bool:
        if self is SeekMode.REPLAY:
            return True
        if self is SeekMode.SEMANTIC:
            return tier is Tier.LAB
        return tier in (Tier.LAB, Tier.TAS)


SEEK_ALIASES: Mapping[str, str] = {
    "checkpoint": "replay", "checkpoint_replay": "replay",
    "exact": "restore", "exact_tick": "restore", "tick_restore": "restore",
}


class ClockMode(enum.StrEnum):
    VIRTUAL = "virtual"  # clocks the game reads are virtualized; engine timers untouched
    FIXED = "fixed"      # engine SetFixedSnapshotRate on the world timers (an engine write)
    WALL = "wall"        # no time virtualization

    def allowed(self, tier: Tier) -> bool:
        if self is ClockMode.VIRTUAL:
            return tier in (Tier.LAB, Tier.TAS)
        if self is ClockMode.FIXED:
            return tier is Tier.LAB
        return tier in (Tier.LAB, Tier.HANDS_OFF)


class VerbClass(enum.IntFlag):
    """Per-tick record of what touched the game. Bits 0-15 are allowed above lab."""
    NONE = 0
    INPUT = 1 << 0          # virtual input staged in process
    OS_INPUT = 1 << 1       # OS-level input (hands-off path)
    GATE = 1 << 2           # tick released through the in-process gate
    CLOCK = 1 << 3          # virtual clock hooks active
    WAIT = 1 << 4           # ticks consumed by a @sync wait
    CAPTURE = 1 << 5        # in-process page capture or digest
    READ = 1 << 6           # memory read, watch, semantic read
    OS_EVENT = 1 << 7       # alt-f4, focus, blur, close, relaunch
    SCREEN = 1 << 8         # frame grab
    RESTORE = 1 << 9        # state written back from a recorded tick
    FILES_RESTORE = 1 << 10  # files overlay reset to a recorded tree
    WRITE = 1 << 16         # memory write of bytes not recorded from this run
    SEMANTIC = 1 << 17      # semantic field write-back
    FIXED_CLOCK = 1 << 18   # engine timer fixed step (engine write)
    FILES_WRITE = 1 << 19   # files put with bytes not recorded from this run

    def ceiling(self) -> Tier:
        """Strictest tier this set of verbs allows."""
        if self & LAB_VERBS:
            return Tier.LAB
        if self & IN_PROCESS_VERBS:
            return Tier.TAS
        return Tier.HANDS_OFF

    def publishable(self) -> bool:
        """True when a tick with these verbs can appear in a published (clean) replay."""
        return not (self & (LAB_VERBS | AUTHORING_VERBS))


LAB_VERBS = VerbClass.WRITE | VerbClass.SEMANTIC | VerbClass.FIXED_CLOCK | VerbClass.FILES_WRITE
IN_PROCESS_VERBS = (VerbClass.INPUT | VerbClass.GATE | VerbClass.CLOCK | VerbClass.CAPTURE
                    | VerbClass.READ | VerbClass.RESTORE | VerbClass.FILES_RESTORE)
AUTHORING_VERBS = VerbClass.RESTORE | VerbClass.FILES_RESTORE

_LT = frozenset({Tier.LAB, Tier.TAS})
_L = frozenset({Tier.LAB})

#: Wire verb -> tiers whose tier.lock allowlist contains it. hands-off has no wire.
VERB_ALLOW: Mapping[str, frozenset[Tier]] = {
    "hello": _LT, "tier.lock": _LT, "clock": _LT, "ring.attach": _LT, "capture": _LT,
    "start": _LT, "gate": _LT, "input": _LT, "step": _LT, "wait": _LT, "os_event": _LT,
    "input.source": _LT, "input.physical.prepare": _LT,
    "digest": _LT, "snap": _LT, "restore": _LT, "restore.admit": _LT, "verify": _LT, "mem.read": _LT,
    "mem.write": _L, "semantic.read": _LT, "semantic.apply": _L, "watch": _LT,
    "watch.rm": _LT, "files": _LT, "subscribe": _LT, "frame": _LT, "hud": _LT, "hud.stats": _LT, "input.state": _LT, "hud.poll": _LT, "hud.bindings": _LT, "bye": _LT,
    "clock.save_ms": _LT, "clock.save_ms_next": _LT, "frames": _LT, "gl.need": _LT, "gl.apply": _LT,
}

#: Verbclass bits a verb adds to the tick it runs in.
VERB_CLASS: Mapping[str, VerbClass] = {
    "input": VerbClass.INPUT,
    "input.source": VerbClass.NONE, "input.physical.prepare": VerbClass.READ,
    "step": VerbClass.INPUT | VerbClass.GATE | VerbClass.CLOCK,
    "wait": VerbClass.WAIT | VerbClass.GATE | VerbClass.CLOCK,
    "os_event": VerbClass.OS_EVENT,
    "capture": VerbClass.CAPTURE, "snap": VerbClass.CAPTURE, "digest": VerbClass.CAPTURE,
    "verify": VerbClass.CAPTURE,
    "restore": VerbClass.RESTORE, "gl.need": VerbClass.RESTORE, "gl.apply": VerbClass.RESTORE,
    "mem.read": VerbClass.READ, "semantic.read": VerbClass.READ, "watch": VerbClass.READ,
    "watch.rm": VerbClass.READ,
    "mem.write": VerbClass.WRITE, "semantic.apply": VerbClass.SEMANTIC,
    "frame": VerbClass.SCREEN,
    "clock": VerbClass.CLOCK,
    "clock.save_ms": VerbClass.INPUT, "clock.save_ms_next": VerbClass.INPUT,
    "start": VerbClass.GATE,
}


def verb_class(verb: str, args: Mapping[str, Any] | None = None, *, started: bool = True) -> VerbClass:
    """Verbclass bits for one request, including the mode-dependent cases."""
    args = args or {}
    if verb == "clock":
        mode = args.get("mode", "virtual")
        return {"virtual": VerbClass.CLOCK, "fixed": VerbClass.FIXED_CLOCK}.get(mode, VerbClass.NONE)
    if verb == "files":
        return VerbClass.FILES_WRITE if args.get("op") == "put" and started else VerbClass.NONE
    return VERB_CLASS.get(verb, VerbClass.NONE)


class MemClass(enum.IntEnum):
    """Restorable memory classes. ambient and excluded memory never appears in records."""
    GAME_ARENA = 1
    IMAGE_DATA = 2
    HARNESS_GAME = 3

    @property
    def label(self) -> str:
        return self.name.lower()


class VerifyLevel(enum.StrEnum):
    NONE = "none"          # memory stage only; external reconstruction is pending
    FULL_ROOT = "full_root"
    SIM_ROOT = "sim_root"
    JOURNAL = "journal"
    DIFF_ONLY = "diff_only"
    FRAMES = "frames"      # hands-off: frame hashes at sync points, best effort


class ProcOutcome(enum.StrEnum):
    ALIVE = "alive"
    EXITED = "exited"


class OsEvent(enum.StrEnum):
    ALTF4 = "altf4"
    ALTF4_SAVE = "altf4@save"   # Alt+F4 pressed while the next save is written
    FOCUS = "focus"
    BLUR = "blur"
    CLOSE = "close"
    RELAUNCH = "relaunch"


# ---------------------------------------------------------------- page references

class PageFlag(enum.IntFlag):
    NONE = 0
    DECOMMIT = 1   # page left the sim set (decommitted); key and prot are zero


_PAGEREF = struct.Struct("<I16sBBH")
_PAGE_REF_ALLOWED_FLAGS = int(PageFlag.DECOMMIT)


@dataclass(frozen=True, slots=True)
class PageRef:
    """One page of the sim set at one tick. 24 bytes on disk."""
    vaddr: int
    key: bytes
    mclass: MemClass = MemClass.GAME_ARENA
    prot: int = 0x04          # Win32 PAGE_* protection (low 16 bits); PAGE_READWRITE
    flags: PageFlag = PageFlag.NONE

    SIZE: ClassVar[int] = _PAGEREF.size

    def __post_init__(self) -> None:
        _u(self.vaddr, 32, "vaddr")
        _need(self.vaddr % PAGE_SIZE == 0, f"vaddr {self.vaddr:#x} is not page aligned")
        _need(isinstance(self.key, bytes) and len(self.key) == PAGE_KEY_LEN, "key must be 16 bytes")
        try:
            object.__setattr__(self, "mclass", MemClass(self.mclass))
        except ValueError:
            raise FormatError(f"unknown memory class {self.mclass!r}") from None
        _u(self.prot, 16, "prot")
        object.__setattr__(self, "flags", PageFlag(self.flags))
        # Integer tests avoid constructing two IntFlags for every decoded ref.
        # IntFlag inversion excludes unknown bits; use an integer complement
        # so the reserved-bit check also rejects them. Keep the public type.
        flags = int(self.flags)
        _need(not (flags & ~_PAGE_REF_ALLOWED_FLAGS), f"unknown page flags {flags:#x}")
        if flags & _PAGE_REF_ALLOWED_FLAGS:
            _need(self.key == NULL_KEY and self.prot == 0, "a DECOMMIT ref has a zero key and prot")

    def to_bytes(self) -> bytes:
        return _PAGEREF.pack(self.vaddr, self.key, int(self.mclass), int(self.flags), self.prot)

    @classmethod
    def from_bytes(cls, buf: bytes, offset: int = 0) -> PageRef:
        _need(len(buf) - offset >= cls.SIZE, "truncated page ref")
        vaddr, key, mclass, flags, prot = _PAGEREF.unpack_from(buf, offset)
        return cls(vaddr, key, mclass, prot, PageFlag(flags))


_LEAF_ENTRY = struct.Struct("<H16sBxH")
_ROOT_ENTRY = struct.Struct("<H32s")
EMPTY_ROOT = sha256(b"KPRT")


def leaf_hash(hi: int, entries: Sequence[PageRef]) -> bytes:
    """Hash of one 4 MiB leaf (1024 pages). entries must be ascending and share hi."""
    parts = [b"KPLF", struct.pack("<H", hi)]
    for ref in entries:
        parts.append(_LEAF_ENTRY.pack((ref.vaddr >> 12) & 0x3FF, ref.key, int(ref.mclass), ref.prot))
    return sha256(*parts)


def state_root(pages: Iterable[PageRef]) -> bytes:
    """Root of the two-level radix tree over a complete page map.

    DECOMMIT refs are absent pages. vpn = vaddr >> 12; leaf index hi = vpn >> 10.
    """
    leaves: dict[int, list[PageRef]] = {}
    seen: set[int] = set()
    for ref in pages:
        _need(ref.vaddr not in seen, f"duplicate page {ref.vaddr:#x}")
        seen.add(ref.vaddr)
        if ref.flags & PageFlag.DECOMMIT:
            continue
        leaves.setdefault(ref.vaddr >> 22, []).append(ref)
    parts = [b"KPRT"]
    for hi in sorted(leaves):
        entries = sorted(leaves[hi], key=lambda r: r.vaddr)
        parts.append(_ROOT_ENTRY.pack(hi, leaf_hash(hi, entries)))
    return sha256(*parts)


# ---------------------------------------------------------------- staging ring

RING_MAGIC = b"KPSR"
RING_VERSION = 1
RING_HEADER_SIZE = 4096
_RING = struct.Struct("<4sHHIIQQQIIII")


@dataclass(slots=True)
class RingHeader:
    """First 56 bytes of the staging-ring mapping; the rest of the 4096-byte header is zero."""
    capacity: int
    write_pos: int = 0
    read_pos: int = 0
    last_tick: int = 0
    producer_pid: int = 0
    consumer_pid: int = 0
    state: int = 0          # 0 new, 1 attached, 2 closing
    version: int = RING_VERSION
    align: int = 8
    header_size: int = RING_HEADER_SIZE

    SIZE: ClassVar[int] = _RING.size

    def to_bytes(self) -> bytes:
        _need(self.capacity % PAGE_SIZE == 0 and self.capacity > 0, "capacity must be a page multiple")
        _need(self.read_pos <= self.write_pos, "read_pos is ahead of write_pos")
        return _RING.pack(RING_MAGIC, self.version, self.align, self.header_size, self.capacity,
                          self.write_pos, self.read_pos, self.last_tick, self.producer_pid,
                          self.consumer_pid, self.state, 0)

    @classmethod
    def from_bytes(cls, buf: bytes, offset: int = 0) -> RingHeader:
        _need(len(buf) - offset >= cls.SIZE, "truncated ring header")
        (magic, version, align, header_size, capacity, wpos, rpos, last, ppid, cpid, state,
         _res) = _RING.unpack_from(buf, offset)
        _need(magic == RING_MAGIC, f"bad ring magic {magic!r}")
        _need(version == RING_VERSION, f"unsupported ring version {version}")
        return cls(capacity, wpos, rpos, last, ppid, cpid, state, version, align, header_size)


class RecordKind(enum.IntEnum):
    PAGE = 1
    TICK_END = 2
    WRAP = 3
    JOURNAL = 4


class RecordFlag(enum.IntFlag):
    NONE = 0
    DATA = 0x0001           # PAGE: 4096 data bytes follow
    ZERO = 0x0002           # PAGE: page is all zero, no data
    DECOMMIT = 0x0004       # PAGE: page not committed at this tick, no data
    SWEEP = 0x0008          # PAGE: emitted by a full sweep, not by write tracking
    RESTORE_POINT = 0x0100  # TICK_END: the gate predicate held
    FULL = 0x0200           # TICK_END: the batch lists every sim page
    RESTORE = 0x0400        # TICK_END: the batch a restore wrote, not a played tick
    UNCAPTURED = 0x0800     # TICK_END: capture cadence skipped this gate; JOURNAL only, no PAGE, no root.
                            # Pages changed here arrive with the next captured tick (delta since the last capture)


SR_MAGIC = b"SR"
_SR = struct.Struct("<2sBBHHIIQ16s")
_PAGE_FORMS = RecordFlag.DATA | RecordFlag.ZERO | RecordFlag.DECOMMIT


def _pad8(n: int) -> int:
    return (n + 7) & ~7


@dataclass(frozen=True, slots=True)
class StagingRecord:
    """One record in the staging ring (shim to Python) or a restore file (Python to shim).

    Header is 40 bytes, then data_len bytes, then zero padding to a multiple of 8.
    """
    kind: RecordKind
    tick: int
    vaddr: int = 0
    hash: bytes = NULL_KEY
    data: bytes = b""
    flags: RecordFlag = RecordFlag.NONE
    mclass: int = 0
    prot: int = 0

    HEADER_SIZE: ClassVar[int] = _SR.size

    def __post_init__(self) -> None:
        try:
            object.__setattr__(self, "kind", RecordKind(self.kind))
        except ValueError:
            raise FormatError(f"unknown record kind {self.kind!r}") from None
        object.__setattr__(self, "flags", RecordFlag(self.flags))

    @property
    def size(self) -> int:
        return self.HEADER_SIZE + _pad8(len(self.data))

    def validate(self, verify_hash: bool = True) -> None:
        _u(self.tick, 64, "tick")
        _u(self.vaddr, 32, "vaddr")
        _u(self.prot, 16, "prot")
        _u(self.mclass, 8, "mclass")
        _need(len(self.hash) == PAGE_KEY_LEN, "hash must be 16 bytes")
        _need(len(self.data) < (1 << 32), "data too long")
        k, f = self.kind, RecordFlag(self.flags)
        if k is RecordKind.PAGE:
            _need(self.vaddr % PAGE_SIZE == 0, f"PAGE vaddr {self.vaddr:#x} not aligned")
            _need(self.mclass in tuple(MemClass), f"PAGE mclass {self.mclass} unknown")
            _need(not (f & ~(_PAGE_FORMS | RecordFlag.SWEEP)), f"PAGE flags {int(f):#x} invalid")
            forms = f & _PAGE_FORMS
            _need(forms in (RecordFlag.DATA, RecordFlag.ZERO, RecordFlag.DECOMMIT),
                  "PAGE needs exactly one of DATA, ZERO, DECOMMIT")
            if forms == RecordFlag.DATA:
                _need(len(self.data) == PAGE_SIZE, "DATA page needs 4096 bytes")
                if verify_hash:
                    _need(page_key(self.data) == self.hash, f"page {self.vaddr:#x} hash mismatch")
            elif forms == RecordFlag.ZERO:
                _need(not self.data and self.hash == ZERO_PAGE_KEY, "ZERO page: no data, zero-page hash")
            else:
                _need(not self.data and self.hash == NULL_KEY and self.prot == 0,
                      "DECOMMIT page: no data, null hash, prot 0")
        elif k is RecordKind.TICK_END:
            _need(self.vaddr == 0 and self.mclass == 0 and self.prot == 0 and not self.data,
                  "TICK_END carries only tick, flags and the batch digest")
            _need(not (f & ~(RecordFlag.RESTORE_POINT | RecordFlag.FULL | RecordFlag.RESTORE
                             | RecordFlag.UNCAPTURED)),
                  f"TICK_END flags {int(f):#x} invalid")
            _need(not (f & RecordFlag.UNCAPTURED) or not (f & ~RecordFlag.UNCAPTURED),
                  "an UNCAPTURED TICK_END carries no other flag")
        elif k is RecordKind.WRAP:
            _need(self.vaddr == 0 and self.mclass == 0 and self.prot == 0 and not self.data
                  and f == RecordFlag.NONE and self.hash == NULL_KEY, "WRAP is all zero")
        elif k is RecordKind.JOURNAL:
            _need(self.vaddr == 0 and self.mclass == 0 and self.prot == 0 and f == RecordFlag.NONE,
                  "JOURNAL carries only tick, data and hash")
            if verify_hash:
                _need(blob_key16(self.data) == self.hash, "JOURNAL hash mismatch")
        else:  # pragma: no cover - RecordKind() already rejects
            raise FormatError(f"unknown record kind {k!r}")

    def to_bytes(self) -> bytes:
        self.validate(verify_hash=False)
        head = _SR.pack(SR_MAGIC, int(self.kind), self.mclass, int(self.flags), self.prot,
                        self.vaddr, len(self.data), self.tick, self.hash)
        return head + self.data + bytes(_pad8(len(self.data)) - len(self.data))

    @classmethod
    def from_bytes(cls, buf: bytes | bytearray | memoryview, offset: int = 0, *,
                   verify_hash: bool = True) -> StagingRecord:
        """Decode the record at offset. The caller advances by ``record.size``."""
        _need(len(buf) - offset >= cls.HEADER_SIZE, "truncated record header")
        magic, kind, mclass, flags, prot, vaddr, dlen, tick, h = _SR.unpack_from(buf, offset)
        _need(magic == SR_MAGIC, f"bad record magic {bytes(magic)!r} at {offset}")
        try:
            kind = RecordKind(kind)
        except ValueError:
            raise FormatError(f"unknown record kind {kind}") from None
        start = offset + cls.HEADER_SIZE
        _need(len(buf) - start >= _pad8(dlen), "truncated record data")
        data = bytes(buf[start:start + dlen])
        _need(not any(bytes(buf[start + dlen:start + _pad8(dlen)])), "nonzero record padding")
        rec = cls(kind, tick, vaddr, bytes(h), data, RecordFlag(flags), mclass, prot)
        rec.validate(verify_hash)
        return rec

    # constructors ---------------------------------------------------------

    @classmethod
    def page(cls, tick: int, vaddr: int, data: bytes, mclass: MemClass, prot: int = 0x04,
             sweep: bool = False) -> StagingRecord:
        extra = RecordFlag.SWEEP if sweep else RecordFlag.NONE
        key = page_key(data)
        if key == ZERO_PAGE_KEY:
            return cls(RecordKind.PAGE, tick, vaddr, ZERO_PAGE_KEY, b"", RecordFlag.ZERO | extra,
                       int(mclass), prot)
        return cls(RecordKind.PAGE, tick, vaddr, key, bytes(data), RecordFlag.DATA | extra,
                   int(mclass), prot)

    @classmethod
    def decommit(cls, tick: int, vaddr: int, mclass: MemClass) -> StagingRecord:
        return cls(RecordKind.PAGE, tick, vaddr, NULL_KEY, b"", RecordFlag.DECOMMIT, int(mclass), 0)

    @classmethod
    def journal(cls, tick: int, data: bytes) -> StagingRecord:
        return cls(RecordKind.JOURNAL, tick, 0, blob_key16(data), bytes(data))

    @classmethod
    def tick_end(cls, tick: int, batch: Iterable[StagingRecord], *, restore_point: bool = False,
                 full: bool = False) -> StagingRecord:
        f = (RecordFlag.RESTORE_POINT if restore_point else RecordFlag.NONE) | \
            (RecordFlag.FULL if full else RecordFlag.NONE)
        return cls(RecordKind.TICK_END, tick, 0, batch_digest(batch), b"", f)

    def page_ref(self) -> PageRef:
        _need(self.kind is RecordKind.PAGE, "only PAGE records have a page ref")
        pf = PageFlag.DECOMMIT if self.flags & RecordFlag.DECOMMIT else PageFlag.NONE
        return PageRef(self.vaddr, self.hash, MemClass(self.mclass), self.prot, pf)


_BATCH_ENTRY = struct.Struct("<BI16s")


def batch_digest(batch: Iterable[StagingRecord]) -> bytes:
    """BLAKE2s-128 over (kind u8, vaddr u32, hash) of every PAGE and JOURNAL record of a tick."""
    h = hashlib.blake2s(digest_size=PAGE_KEY_LEN)
    for r in batch:
        if r.kind in (RecordKind.PAGE, RecordKind.JOURNAL):
            h.update(_BATCH_ENTRY.pack(int(r.kind), r.vaddr, r.hash))
    return h.digest()


# ---------------------------------------------------------------- journal chunks

CHUNK_MAGIC = b"HR"
_CHUNK_HEAD = struct.Struct("<2s4sI")
CHUNK_HEAD_SIZE = _CHUNK_HEAD.size
CHUNK_TRAILER_SIZE = DIGEST_LEN
CHUNK_TYPES = (b"HOLO", b"SCOR", b"ENVP", b"TICK", b"MARK", b"EPOC", b"RCPT", b"SEAL")
CHAINED_TYPES = frozenset({b"TICK", b"MARK", b"EPOC", b"RCPT", b"SEAL"})
MAX_CHUNK_PAYLOAD = 1 << 28


def frame_chunk(fourcc: bytes, payload: bytes) -> bytes:
    """[b"HR"][4cc][len u32][payload][sha256(payload)]"""
    _need(fourcc in CHUNK_TYPES, f"unknown chunk type {fourcc!r}")
    _need(len(payload) <= MAX_CHUNK_PAYLOAD, "chunk payload too large")
    return _CHUNK_HEAD.pack(CHUNK_MAGIC, fourcc, len(payload)) + payload + sha256(payload)


def read_chunk(buf: bytes | bytearray | memoryview, offset: int = 0) -> tuple[bytes, bytes, int]:
    """Return (fourcc, payload, next_offset). Raises FormatError on a bad or torn chunk."""
    _need(len(buf) - offset >= CHUNK_HEAD_SIZE, "truncated chunk header")
    magic, fourcc, n = _CHUNK_HEAD.unpack_from(buf, offset)
    _need(magic == CHUNK_MAGIC, f"bad chunk magic {bytes(magic)!r} at {offset}")
    _need(fourcc in CHUNK_TYPES, f"unknown chunk type {bytes(fourcc)!r} at {offset}")
    _need(n <= MAX_CHUNK_PAYLOAD, "chunk payload too large")
    start = offset + CHUNK_HEAD_SIZE
    end = start + n + CHUNK_TRAILER_SIZE
    _need(len(buf) >= end, "truncated chunk")
    payload = bytes(buf[start:start + n])
    _need(sha256(payload) == bytes(buf[start + n:end]), f"chunk digest mismatch at {offset}")
    return bytes(fourcc), payload, end


def chain_genesis(holo: bytes, scor: bytes, envp: bytes) -> bytes:
    """H_0 = sha256(sha256(HOLO) || sha256(SCOR) || sha256(ENVP)) over the three payloads."""
    return sha256(sha256(holo), sha256(scor), sha256(envp))


def chain_link(prev: bytes, fourcc: bytes, body: bytes) -> bytes:
    """H_k = sha256(H_{k-1} || 4cc || body) for every chained chunk."""
    _need(len(prev) == DIGEST_LEN, "previous link must be 32 bytes")
    _need(fourcc in CHAINED_TYPES, f"{fourcc!r} is not a chained chunk type")
    return sha256(prev, fourcc, body)


def chained_payload(prev: bytes, fourcc: bytes, body: bytes) -> tuple[bytes, bytes]:
    """Return (payload, link) where payload = body || link."""
    link = chain_link(prev, fourcc, body)
    return body + link, link


def split_chained(payload: bytes) -> tuple[bytes, bytes]:
    _need(len(payload) >= DIGEST_LEN, "chained payload shorter than its link")
    return payload[:-DIGEST_LEN], payload[-DIGEST_LEN:]


# ---------------------------------------------------------------- store frames

class ObjKind(enum.IntEnum):
    PAGE_I = 1      # intact page
    PAGE_X = 2      # page XOR a base page
    LEAF = 3        # state-tree leaf (payload = the bytes leaf_hash hashes)
    ROOT = 4        # state-tree root (payload = the bytes state_root hashes)
    FILE_BLOB = 5   # file content
    FILE_TREE = 6   # canonical JSON file tree
    BATCH = 7       # a frame of pages compressed together; its members are PAGE_I and PAGE_X objects


class Codec(enum.IntEnum):
    NONE = 0
    ZLIB = 1
    ZSTD = 2


OBJ_MAGIC = b"KPOB"
_OBJ_HEAD = struct.Struct("<4sBBBBII")


CODEC_NAMES = ("auto", "zlib", "zstd", "none")


def zstd_available() -> bool:
    try:
        import compression.zstd  # type: ignore[import-not-found]  # noqa: F401  # Python >= 3.14
    except ImportError:
        return False
    return True


def resolve_codec(name: str) -> str:
    """The codec a store writes with: "auto" is zstd when this Python has compression.zstd, else zlib."""
    _need(name in CODEC_NAMES, f"unknown codec {name!r}")
    if name == "auto":
        return "zstd" if zstd_available() else "zlib"
    return name


def _compress(codec: Codec, data: bytes, level: int) -> bytes:
    if codec is Codec.NONE:
        return data
    if codec is Codec.ZLIB:
        import zlib
        return zlib.compress(data, level)
    try:
        from compression import zstd  # type: ignore[import-not-found]  # Python >= 3.14
    except ImportError:
        raise FormatError("zstd needs Python 3.14 or newer (compression.zstd)") from None
    return zstd.compress(data, level)


def _decompress(codec: Codec, data: bytes, expected: int) -> bytes:
    """Decode at most the declared raw size, plus one byte to detect overflow.

    General FILE_BLOB/FILE_TREE lengths use the format's unsigned 32-bit rlen;
    no smaller implicit cap is imposed. Output grows in bounded chunks instead
    of trusting a compressed stream's advertised allocation size.
    """
    if codec is Codec.NONE:
        _need(len(data) == expected, "decoded length mismatch")
        return data
    if codec is Codec.ZLIB:
        import zlib
        decoder, errors = zlib.decompressobj(), (zlib.error,)
    else:
        try:
            from compression import zstd  # type: ignore[import-not-found]
        except ImportError:
            raise FormatError("this object is zstd-compressed; reading it needs Python 3.14 or newer") from None
        decoder, errors = zstd.ZstdDecompressor(), (zstd.ZstdError,)
    out = bytearray()
    pending, at = b"", 0
    try:
        while not decoder.eof:
            if not pending and (codec is Codec.ZLIB or decoder.needs_input):
                _need(at < len(data), "truncated compressed object")
                pending = data[at:at + (64 << 10)]
                at += len(pending)
            chunk = decoder.decompress(pending, min(64 << 10, expected + 1 - len(out)))
            out.extend(chunk)
            _need(len(out) <= expected, "decoded object exceeds declared length")
            pending = decoder.unconsumed_tail if codec is Codec.ZLIB else b""
        _need(not decoder.unused_data and at == len(data), "trailing compressed object data")
    except errors as exc:
        raise FormatError(f"invalid compressed object: {exc}") from None
    _need(len(out) == expected, "decoded length mismatch")
    return bytes(out)


def _object_shape(kind: ObjKind, klen: int, rlen: int) -> None:
    """Check canonical shape before reading or decoding object payloads."""
    _need(kind is not ObjKind.BATCH, "a batch frame is read with read_batch")
    page = kind in (ObjKind.PAGE_I, ObjKind.PAGE_X)
    _need(klen == (PAGE_KEY_LEN if page else DIGEST_LEN), "noncanonical object key length")
    if page:
        _need(rlen == PAGE_SIZE, "page decoded length must be 4096")
    elif kind is ObjKind.LEAF:
        # 32-bit address space: a leaf has at most 1024 page entries.
        _need(6 <= rlen <= 6 + 1024 * _LEAF_ENTRY.size and (rlen - 6) % _LEAF_ENTRY.size == 0,
              "invalid LEAF decoded length")
    elif kind is ObjKind.ROOT:
        _need(4 <= rlen <= 4 + 1024 * _ROOT_ENTRY.size and (rlen - 4) % _ROOT_ENTRY.size == 0,
              "invalid ROOT decoded length")


def frame_object(kind: ObjKind, key: bytes, raw: bytes, codec: Codec = Codec.ZLIB, level: int = 1,
                 base: bytes | None = None) -> bytes:
    """Encode one store frame. For PAGE_X, raw is the XOR of the page and its base page."""
    kind, codec = ObjKind(kind), Codec(codec)
    klen = PAGE_KEY_LEN if kind in (ObjKind.PAGE_I, ObjKind.PAGE_X) else DIGEST_LEN
    _need(len(key) == klen, f"{kind.name} key must be {klen} bytes")
    _need((base is not None) == (kind is ObjKind.PAGE_X), "only PAGE_X frames carry a base key")
    if base is not None:
        _need(len(base) == PAGE_KEY_LEN, "base key must be 16 bytes")
    stored = _compress(codec, raw, level)
    head = _OBJ_HEAD.pack(OBJ_MAGIC, int(kind), int(codec), klen, 0, len(stored), len(raw))
    body = head + key + (base or b"") + stored
    return body + _U32_STORE.pack(_crc32(body))


def read_object(buf: bytes | bytearray | memoryview, offset: int = 0
                ) -> tuple[ObjKind, bytes, bytes | None, bytes, int]:
    """Return (kind, key, base key or None, raw bytes, next_offset). Checks CRC and length."""
    _need(len(buf) - offset >= _OBJ_HEAD.size, "truncated store frame header")
    magic, kind, codec, klen, res, slen, rlen = _OBJ_HEAD.unpack_from(buf, offset)
    _need(magic == OBJ_MAGIC, f"bad store frame magic {bytes(magic)!r} at {offset}")
    _need(res == 0, "reserved byte must be zero")
    try:
        kind, codec = ObjKind(kind), Codec(codec)
    except ValueError as e:
        raise FormatError(str(e)) from None
    _object_shape(kind, klen, rlen)
    blen = PAGE_KEY_LEN if kind is ObjKind.PAGE_X else 0
    end = offset + _OBJ_HEAD.size + klen + blen + slen
    _need(len(buf) >= end + 4, "truncated store frame")
    _need(_crc32(bytes(buf[offset:end])) == _U32_STORE.unpack_from(buf, end)[0],
          f"store frame CRC mismatch at {offset}")
    _need(kind is not ObjKind.BATCH, "a batch frame is read with read_batch")
    p = offset + _OBJ_HEAD.size
    key = bytes(buf[p:p + klen])
    base = bytes(buf[p + klen:p + klen + blen]) if blen else None
    raw = _decompress(codec, bytes(buf[p + klen + blen:end]), rlen)
    _need(len(raw) == rlen, "decoded length mismatch")
    return kind, key, base, raw, end + 4


def frame_batch(members: Sequence[tuple[ObjKind, bytes, bytes | None, bytes]], codec: Codec = Codec.ZLIB,
                level: int = 1) -> bytes:
    """Encode pages as one store frame: a member table, then the members' bytes as one compressed stream.

    A member is (kind, page key, base key or None, 4096 bytes); for PAGE_X the bytes are the page XOR its base.
    """
    codec = Codec(codec)
    _need(0 < len(members) <= _BATCH_MAX, f"a batch holds 1..{_BATCH_MAX} pages")
    table = [_U32_STORE.pack(len(members))]
    for kind, key, base, raw in members:
        _need(kind in (ObjKind.PAGE_I, ObjKind.PAGE_X), "a batch holds only PAGE_I and PAGE_X")
        _need(len(key) == PAGE_KEY_LEN and len(raw) == PAGE_SIZE, "a batch member is a 16-byte key and 4096 bytes")
        _need((base is not None) == (kind is ObjKind.PAGE_X), "only PAGE_X members carry a base key")
        _need(base is None or len(base) == PAGE_KEY_LEN, "base key must be 16 bytes")
        table.append(bytes((int(kind),)) + key + (base or b""))
    stored = _compress(codec, b"".join(m[3] for m in members), level)
    body = b"".join(table) + stored
    head = _OBJ_HEAD.pack(OBJ_MAGIC, int(ObjKind.BATCH), int(codec), 0, 0, len(body), len(members) * PAGE_SIZE)
    frame = head + body
    return frame + _U32_STORE.pack(_crc32(frame))


def read_batch(buf: bytes | bytearray | memoryview, offset: int = 0, decode: bool = True
               ) -> tuple[list[tuple[ObjKind, bytes, bytes | None]], bytes | None, int]:
    """Return (members as (kind, key, base or None), the members' bytes or None, next_offset).

    Checks the CRC and the member table; with decode=False the payload is not decompressed.
    """
    _need(len(buf) - offset >= _OBJ_HEAD.size, "truncated store frame header")
    magic, kind, codec, klen, res, slen, rlen = _OBJ_HEAD.unpack_from(buf, offset)
    _need(magic == OBJ_MAGIC and kind == ObjKind.BATCH, f"no batch frame at {offset}")
    _need(klen == 0 and res == 0, "a batch frame has no key and a zero reserved byte")
    try:
        codec = Codec(codec)
    except ValueError as e:
        raise FormatError(str(e)) from None
    end = offset + _OBJ_HEAD.size + slen
    _need(len(buf) >= end + 4, "truncated store frame")
    _need(_crc32(bytes(buf[offset:end])) == _U32_STORE.unpack_from(buf, end)[0],
          f"store frame CRC mismatch at {offset}")
    p = offset + _OBJ_HEAD.size
    _need(slen >= 4, "batch frame without a member count")
    n = _U32_STORE.unpack_from(buf, p)[0]
    _need(0 < n <= _BATCH_MAX and rlen == n * PAGE_SIZE, "batch member count and decoded length disagree")
    p += 4
    members: list[tuple[ObjKind, bytes, bytes | None]] = []
    for _ in range(n):
        _need(p < end, "batch member table runs past the frame")
        k = buf[p]
        _need(k in (ObjKind.PAGE_I, ObjKind.PAGE_X), f"batch member of kind {k}")
        size = 1 + PAGE_KEY_LEN * (2 if k == ObjKind.PAGE_X else 1)
        _need(p + size <= end, "batch member table runs past the frame")
        key = bytes(buf[p + 1:p + 1 + PAGE_KEY_LEN])
        base = bytes(buf[p + 1 + PAGE_KEY_LEN:p + size]) if k == ObjKind.PAGE_X else None
        members.append((ObjKind(k), key, base))
        p += size
    raw = None
    if decode:
        raw = _decompress(codec, bytes(buf[p:end]), rlen)
        _need(len(raw) == rlen, "decoded length mismatch")
    return members, raw, end + 4


def _crc32(data: bytes) -> int:
    import zlib
    return zlib.crc32(data) & 0xFFFFFFFF


_U32_STORE = struct.Struct("<I")
_BATCH_MAX = 1 << 16


# ---------------------------------------------------------------- tick records

class TickFlag(enum.IntFlag):
    NONE = 0
    CAPTURED = 0x001        # state_root and pages cover the sim set
    RESTORE_POINT = 0x002   # the gate predicate held; a legal restore target
    KEYFRAME = 0x004        # tree objects for state_root are in the page store
    MATERIALIZED = 0x008    # a materialized keyframe pack exists for this tick
    EVICTED = 0x010         # page payloads were evicted under the budget policy
    HOLD = 0x020            # the tick contained sub-ticks (UpdateScreen presents)
    EPOCH_START = 0x040     # first tick of a process epoch
    SIM_ROOT = 0x080        # sim_root is valid
    FILES = 0x100           # files_root is valid


class Reached(enum.IntEnum):
    RECORDED = 0
    REPLAY = 1
    RESTORE = 2
    HYBRID = 3
    SEMANTIC = 4


class PointReason(enum.IntEnum):
    """Why a tick is not a restore point. NONE means it is one."""
    NONE = 0
    NOT_CAPTURED = 1
    GL_MAPPED = 2
    FILE_IO = 3
    SCHEDULER = 4
    FOREIGN_MODULE = 5
    OTHER = 255


TICK_VERSION = 1
_TICK_HEAD = struct.Struct("<BBBBQIHHII32s32s32s32s32sH")
_U32 = struct.Struct("<I")


@dataclass(frozen=True, slots=True)
class TickRecord:
    """Canonical body of a TICK chunk: one tick, one complete state identity."""
    tick: int
    epoch: int = 0
    note: str = "."
    verbclass: VerbClass = VerbClass.NONE
    server_passes: int = 1
    subticks: int = 0
    flags: TickFlag = TickFlag.NONE
    reached: Reached = Reached.RECORDED
    reason: PointReason = PointReason.NOT_CAPTURED
    state_root: bytes = NULL_DIGEST
    sim_root: bytes = NULL_DIGEST
    files_root: bytes = NULL_DIGEST
    input_sha256: bytes = NULL_DIGEST
    journal_sha256: bytes = NULL_DIGEST
    pages: tuple[PageRef, ...] = ()
    journal: bytes = b""

    HEAD_SIZE: ClassVar[int] = _TICK_HEAD.size

    def __post_init__(self) -> None:
        try:
            object.__setattr__(self, "pages", tuple(self.pages))
            object.__setattr__(self, "verbclass", VerbClass(self.verbclass))
            object.__setattr__(self, "flags", TickFlag(self.flags))
            object.__setattr__(self, "reached", Reached(self.reached))
            object.__setattr__(self, "reason", PointReason(self.reason))
        except ValueError as e:
            raise FormatError(str(e)) from None

    def validate(self) -> None:
        _u(self.tick, 64, "tick")
        _u(self.epoch, 32, "epoch")
        _u(self.server_passes, 16, "server_passes")
        _u(self.subticks, 16, "subticks")
        _u(int(self.verbclass), 32, "verbclass")
        _u(int(self.flags), 32, "flags")
        for name in ("state_root", "sim_root", "files_root", "input_sha256", "journal_sha256"):
            v = getattr(self, name)
            _need(isinstance(v, bytes) and len(v) == DIGEST_LEN, f"{name} must be 32 bytes")
        note = self.note.encode("utf-8")
        _need(len(note) < (1 << 16), "note too long")
        _need("\n" not in self.note and "\r" not in self.note, "note must be one line")
        f = TickFlag(self.flags)
        captured = bool(f & TickFlag.CAPTURED)
        _need(captured or (self.state_root == NULL_DIGEST and not self.pages),
              "state_root and pages need the CAPTURED flag")
        _need(captured or self.state_root == NULL_DIGEST, "CAPTURED flag needs a state_root")
        if captured:
            _need(self.state_root != NULL_DIGEST, "CAPTURED tick has a null state_root")
        _need(bool(f & TickFlag.SIM_ROOT) == (self.sim_root != NULL_DIGEST),
              "SIM_ROOT flag and sim_root disagree")
        _need(bool(f & TickFlag.FILES) == (self.files_root != NULL_DIGEST),
              "FILES flag and files_root disagree")
        rp = bool(f & TickFlag.RESTORE_POINT)
        _need(rp == (self.reason == PointReason.NONE), "RESTORE_POINT flag and reason disagree")
        _need(not rp or captured, "a restore point must be captured")
        prev = -1
        for ref in self.pages:
            _need(isinstance(ref, PageRef), "pages must hold PageRef")
            _need(ref.vaddr > prev, "pages must be strictly ascending by vaddr")
            prev = ref.vaddr
        if self.journal:
            _need(sha256(self.journal) == self.journal_sha256, "journal bytes do not match journal_sha256")

    def encode(self) -> bytes:
        self.validate()
        note = self.note.encode("utf-8")
        parts = [
            _TICK_HEAD.pack(TICK_VERSION, int(self.reached), int(self.reason), 0, self.tick,
                            self.epoch, self.server_passes, self.subticks, int(self.flags),
                            int(self.verbclass), self.state_root, self.sim_root, self.files_root,
                            self.input_sha256, self.journal_sha256, len(note)),
            note,
            _U32.pack(len(self.pages)),
        ]
        parts.extend(ref.to_bytes() for ref in self.pages)
        parts.append(_U32.pack(len(self.journal)))
        parts.append(self.journal)
        return b"".join(parts)

    @classmethod
    def decode(cls, data: bytes) -> TickRecord:
        _need(len(data) >= cls.HEAD_SIZE, "truncated tick record")
        (ver, reached, reason, res, tick, epoch, sp, st, flags, vc, sroot, simr, froot, inp,
         jsha, nlen) = _TICK_HEAD.unpack_from(data, 0)
        _need(ver == TICK_VERSION, f"unsupported tick record version {ver}")
        _need(res == 0, "reserved byte must be zero")
        off = cls.HEAD_SIZE
        _need(len(data) >= off + nlen + 4, "truncated note")
        try:
            note = data[off:off + nlen].decode("utf-8")
        except UnicodeDecodeError as e:
            raise FormatError(f"tick record note is not UTF-8: {e}") from None
        off += nlen
        (npages,) = _U32.unpack_from(data, off)
        off += 4
        _need(len(data) >= off + npages * PageRef.SIZE + 4, "truncated page refs")
        pages = tuple(PageRef.from_bytes(data, off + i * PageRef.SIZE) for i in range(npages))
        off += npages * PageRef.SIZE
        (jlen,) = _U32.unpack_from(data, off)
        off += 4
        _need(len(data) == off + jlen, "tick record length mismatch")
        try:
            rec = cls(tick, epoch, note, VerbClass(vc), sp, st, TickFlag(flags), Reached(reached),
                      PointReason(reason), sroot, simr, froot, inp, jsha, pages, data[off:])
        except ValueError as e:
            raise FormatError(str(e)) from None
        rec.validate()
        return rec

    def chain(self, prev: bytes) -> bytes:
        return chain_link(prev, b"TICK", self.encode())

    def to_chunk(self, prev: bytes) -> tuple[bytes, bytes]:
        """Return (framed TICK chunk, link H_n)."""
        payload, link = chained_payload(prev, b"TICK", self.encode())
        return frame_chunk(b"TICK", payload), link

    @classmethod
    def from_chunk(cls, payload: bytes) -> tuple[TickRecord, bytes]:
        body, link = split_chained(payload)
        return cls.decode(body), link


# ---------------------------------------------------------------- JSON chunk bodies

def _hex(value: str, what: str, n: int = 64, empty_ok: bool = True) -> None:
    if empty_ok and value == "":
        return
    _need(len(value) == n and all(c in "0123456789abcdef" for c in value),
          f"{what} must be {n} lowercase hex digits")


@dataclass(frozen=True, slots=True)
class ParentRef:
    holo: str   # parent bundle id: hex of its H_0
    tick: int   # branch point: the last tick shared with the parent
    link: str   # hex of the parent's chain link at that tick


@dataclass(frozen=True, slots=True)
class Header:
    """HOLO chunk body. Holds no wall-clock time and no random id: identical runs match."""
    game: str = "k1"
    build: str = "auto"
    exe_sha256: str = ""
    tier: Tier = Tier.TAS
    hz: int = 60
    clock: ClockMode = ClockMode.VIRTUAL
    scale: int = 100
    seek: SeekMode = SeekMode.REPLAY
    threads: str = "join"
    start: str = "boot"            # "boot" | "save:<sha256 hex>"
    keyframe_every: int = 600
    codec: str = "auto"
    journal_spec: str = "semantic/1"
    profile: str = "native"        # PortableProfile id; "native" = no portable boundary
    math: str = "native"           # portable.math the run used; bundles from before it was recorded are native
    determinism_key: str = ""
    parent: ParentRef | None = None
    format: str = "holo/1"
    product: str = PRODUCT
    page_hash: str = "blake2s-128"
    chain_hash: str = "sha256"

    def __post_init__(self) -> None:
        object.__setattr__(self, "tier", Tier.parse(self.tier))
        object.__setattr__(self, "clock", ClockMode(self.clock))
        object.__setattr__(self, "seek", SeekMode.parse(self.seek))
        _need(self.format == "holo/1", f"unsupported bundle format {self.format!r}")
        _need(type(self.hz) is int and 1 <= self.hz <= 1000, "hz must be 1..1000")
        _need(self.start == "boot" or (self.start.startswith("save:") and len(self.start) == 69),
              "start must be 'boot' or 'save:<sha256>'")
        _hex(self.exe_sha256, "exe_sha256")
        _hex(self.determinism_key, "determinism_key")
        _need(self.codec in CODEC_NAMES, f"unknown codec {self.codec!r}")
        _need(self.math in ("native", "reference"), f"unknown math mode {self.math!r}")

    def to_obj(self) -> dict[str, Any]:
        obj = {k: getattr(self, k) for k in _names(type(self))}
        obj["tier"] = self.tier.label
        obj["clock"] = str(self.clock)
        obj["seek"] = str(self.seek)
        obj["parent"] = None if self.parent is None else {
            "holo": self.parent.holo, "tick": self.parent.tick, "link": self.parent.link}
        return obj

    def to_payload(self) -> bytes:
        return canonical_json(self.to_obj())

    @classmethod
    def from_payload(cls, payload: bytes) -> Header:
        obj = json.loads(payload)
        parent = obj.pop("parent", None)
        unknown = set(obj) - set(_names(cls))
        _need(not unknown, f"unknown header keys {sorted(unknown)}")
        return cls(**obj, parent=None if parent is None else ParentRef(**parent))


@dataclass(frozen=True)
class PortableProfile:
    """The fixed machine boundary the shim presents to the game (decision D12).

    A record made behind one profile replays on any host whose real driver and CPU
    can back it, on Wine or Windows. Every value here is pending the host-input census;
    the built-in table ships only values that census has confirmed.
    """
    id: str
    # (1) virtual GPU identity: glGetString replies and every GL query whose result enters game memory
    gl_vendor: str = ""
    gl_renderer: str = ""
    gl_version: str = ""
    gl_extensions: tuple[str, ...] = ()
    gl_queries: Mapping[str, tuple[int, ...]] = field(default_factory=dict)   # GL enum name -> values
    # (2) CPU identity: CPUID replies at patched call sites; RDTSC sites read the virtual clock
    cpuid: Mapping[str, tuple[int, int, int, int]] = field(default_factory=dict)  # "leaf[.sub]" hex
    rdtsc: Literal["virtual", "native"] = "virtual"
    # (3) math: control words pinned at boot on every game thread
    x87_cw: int = 0x027F
    mxcsr: int = 0x1F80
    math: Literal["native", "reference"] = "reference"
    # (4) host facts
    total_phys_mb: int = 4096
    avail_phys_mb: int = 2048
    disk_free_mb: int = 20480
    display_modes: tuple[str, ...] = ("640x480@60", "800x600@60", "1024x768@60")
    refresh_hz: int = 60
    system_metrics: Mapping[str, int] = field(default_factory=dict)          # SM_* name -> value
    os_version: str = "6.1.7601"
    locale: str = "en-US"
    timezone: str = "UTC"
    computer_name: str = "KPTAS"
    user_name: str = "player"
    random: Literal["from_score"] = "from_score"
    cpu_count: int = 1               # SYSTEM_INFO and NUMBER_OF_PROCESSORS
    cpu_level: int = 6
    cpu_revision: int = 0x0D06
    install_dir: str = "C:\\KotOR"  # the exe sees itself as install_dir\swkotor.exe
    # (5) window: virtual client size; mouse coordinates are in this space
    window: tuple[int, int] = (800, 600)

    def __post_init__(self) -> None:
        _need(bool(self.id), "profile id must not be empty")
        _u(self.x87_cw, 16, "x87_cw")
        _u(self.mxcsr, 32, "mxcsr")
        _need(self.math in ("native", "reference"), f"unknown math mode {self.math!r}")
        _need(self.rdtsc in ("virtual", "native"), f"unknown rdtsc mode {self.rdtsc!r}")
        _need(type(self.cpu_count) is int and 1 <= self.cpu_count <= 32, "cpu_count must be 1..32")
        _u(self.cpu_level, 16, "cpu_level")
        _u(self.cpu_revision, 16, "cpu_revision")
        d = self.install_dir
        _need(len(d) >= 3 and d[0].isalpha() and d[1:3] == ":\\" and ";" not in d,
              "install_dir must be an absolute Windows path such as C:\\KotOR")
        _need(len(self.window) == 2, "window must be (width, height)")
        w, h = self.window
        _need(type(w) is int and type(h) is int and w > 0 and h > 0, "window must be two positive ints")

    def to_obj(self) -> dict[str, Any]:
        obj = {k: getattr(self, k) for k in _names(type(self))}
        for k in ("gl_extensions", "display_modes", "window"):
            obj[k] = list(obj[k])
        obj["gl_queries"] = {k: list(v) for k, v in self.gl_queries.items()}
        obj["cpuid"] = {k: list(v) for k, v in self.cpuid.items()}
        obj["system_metrics"] = dict(self.system_metrics)
        return obj

    def to_payload(self) -> bytes:
        return canonical_json(self.to_obj())

    @classmethod
    def from_obj(cls, obj: Mapping[str, Any]) -> PortableProfile:
        unknown = set(obj) - set(_names(cls))
        _need(not unknown, f"unknown profile keys {sorted(unknown)}")
        o = dict(obj)
        for k in ("gl_extensions", "display_modes", "window"):
            if k in o:
                o[k] = tuple(o[k])
        if "gl_queries" in o:
            o["gl_queries"] = {k: tuple(v) for k, v in o["gl_queries"].items()}
        if "cpuid" in o:
            o["cpuid"] = {k: tuple(v) for k, v in o["cpuid"].items()}
        return cls(**o)

    def key(self) -> str:
        return hashlib.sha256(self.to_payload()).hexdigest()


@dataclass(frozen=True)
class EnvPin:
    """ENVP chunk body.

    Key inputs feed the determinism key. Host-fingerprint fields are recorded
    but stay out of the key behind a portable profile, so one profile gives one
    key on Wine and on Windows. With profile "native" (or none) they join the key.
    """
    # key inputs
    exe_sha256: str = ""
    text_sha256: str = ""
    files: Mapping[str, str] = field(default_factory=dict)     # logical name -> sha256 hex
    render: str = ""
    modules: tuple[str, ...] = ()        # game-side "name@sha256": exe, mss32, binkw32, providers, shim
    shim_sha256: str = ""
    clock: Mapping[str, Any] = field(default_factory=dict)
    profile: Mapping[str, Any] = field(default_factory=dict)     # PortableProfile.to_obj(), math included
    threads: str = "join"
    mask_registry: str = ""                                      # registry text, "" = none
    extra: Mapping[str, str] = field(default_factory=dict)
    # host fingerprint
    gl_renderer: str = ""        # the real driver behind the profile; the game never sees it
    wine: str = ""               # "" on Windows
    prefix_sha256: str = ""
    dll_overrides: str = ""
    launch_wrapper: str = "none"
    host_modules: tuple[str, ...] = ()   # every other loaded module, load order
    host_facts: Mapping[str, str] = field(default_factory=dict)   # CPU, time zone, user, locale, install path

    HOST_FIELDS: ClassVar[tuple[str, ...]] = ("gl_renderer", "wine", "prefix_sha256", "dll_overrides",
                                             "launch_wrapper", "host_modules", "host_facts")

    def to_obj(self) -> dict[str, Any]:
        obj = {k: getattr(self, k) for k in _names(type(self))}
        obj["files"] = dict(self.files)
        obj["modules"] = list(self.modules)
        obj["host_modules"] = list(self.host_modules)
        obj["clock"] = dict(self.clock)
        obj["profile"] = dict(self.profile)
        obj["extra"] = dict(self.extra)
        obj["host_facts"] = dict(self.host_facts)
        return obj

    def to_payload(self) -> bytes:
        return canonical_json(self.to_obj())

    @classmethod
    def from_payload(cls, payload: bytes) -> EnvPin:
        obj = json.loads(payload)
        unknown = set(obj) - set(_names(cls))
        _need(not unknown, f"unknown env keys {sorted(unknown)}")
        for k in ("modules", "host_modules"):
            if k in obj:
                obj[k] = tuple(obj[k])
        return cls(**obj)

    @property
    def portable(self) -> bool:
        return self.profile.get("id", "native") != "native"

    def key(self) -> str:
        """Determinism key: sha256 of the canonical key inputs (plus the host fingerprint when native)."""
        obj = self.to_obj()
        if self.portable:
            for k in self.HOST_FIELDS:
                del obj[k]
        return hashlib.sha256(canonical_json(obj)).hexdigest()


# ---------------------------------------------------------------- backend-facing values

@dataclass(frozen=True, slots=True)
class Caps:
    """What a backend can do. The shim reports it in its hello reply."""
    backend: str
    tier_ceiling: Tier
    clock_modes: frozenset[ClockMode] = frozenset()
    seek_modes: frozenset[SeekMode] = frozenset({SeekMode.REPLAY})
    input_paths: frozenset[str] = frozenset()        # "dinput", "os"
    thread_models: frozenset[str] = frozenset()      # "join", "slots", "free"
    tick_source: str = "none"                        # "winmain_loop_head" | "sim" | "none"
    seed_hooked: bool = False    # time hooks were live before the startup srand(GetTickCount())
    capture: bool = False
    fiber: bool = False
    ring: bool = False
    miles_shim: bool = False
    gl_names: bool = False
    hud: bool = False
    portable: frozenset[str] = frozenset()   # boundary items served: gpu cpuid rdtsc math host window
    build: str = ""
    exe_sha256: str = ""
    shim_version: str = ""
    modules: tuple[str, ...] = ()
    hooks: tuple[str, ...] = ()
    loader: str = ""                         # hello "loader": "dinput8" ("" when the shim does not say)
    integrity_entries: int | None = None     # hello integrity.entries: how many guarded entries the shim holds

    @classmethod
    def from_hello(cls, obj: Mapping[str, Any]) -> Caps:
        c = obj.get("caps", {})
        return cls(
            backend=obj.get("backend", "shim"),
            tier_ceiling=Tier.parse(c.get("tier_ceiling", "lab")),
            clock_modes=frozenset(ClockMode(m) for m in c.get("clock_modes", ())),
            seek_modes=frozenset(SeekMode.parse(m) for m in c.get("seek_modes", ("replay",))),
            input_paths=frozenset(c.get("input_paths", ())),
            thread_models=frozenset(c.get("thread_models", ())),
            tick_source=c.get("tick_source", "none"),
            seed_hooked=bool(obj.get("seed_hooked", False)),
            capture=bool(c.get("capture", False)), fiber=bool(c.get("fiber", False)),
            ring=bool(c.get("ring", False)), miles_shim=bool(c.get("miles_shim", False)),
            gl_names=bool(c.get("gl_names", False)), hud=bool(c.get("hud", False)),
            portable=frozenset(c.get("portable", ())),
            build=obj.get("build", ""), exe_sha256=obj.get("exe_sha256", ""),
            shim_version=obj.get("shim", ""), modules=tuple(obj.get("modules", ())),
            hooks=tuple(obj.get("hooks", ())),
        )


@dataclass(frozen=True, slots=True)
class EngineEvent:
    name: str            # main_menu, area.ready, load.done, save.begin, watch.hit, ...
    tick: int
    arg: str | None = None


def _digest_from_hex(v: str | None) -> bytes:
    return NULL_DIGEST if not v else bytes.fromhex(v)


def _event(e: Any, tick: int) -> EngineEvent:
    if isinstance(e, str):
        return EngineEvent(e, tick)
    arg = e.get("arg")
    if arg is None and e.get("us") is not None:
        arg = str(e["us"])            # save.begin / save.done: virtual microseconds the save took
    return EngineEvent(e["name"], int(e.get("tick", tick)), None if arg is None else str(arg))


@dataclass(frozen=True, slots=True)
class StepReport:
    tick: int                     # the last tick run
    epoch: int
    server_passes: int
    subticks: int
    verbclass: VerbClass
    state_root: bytes = NULL_DIGEST
    journal_sha256: bytes = NULL_DIGEST
    restore_point: bool = False
    reason: PointReason = PointReason.NOT_CAPTURED
    events: tuple[EngineEvent, ...] = ()
    outcome: ProcOutcome = ProcOutcome.ALIVE

    @classmethod
    def from_wire(cls, obj: Mapping[str, Any]) -> StepReport:
        """An event is an object (name, tick, arg) or a bare name, which dates it to the report's tick.
        A journal digest that is not sha256 (the shim's is BLAKE2s-128) is left out."""
        tick = obj["tick"]
        journal = obj.get("journal")
        return cls(
            tick=tick, epoch=obj["epoch"], server_passes=obj.get("server_passes", 1),
            subticks=obj.get("subticks", 0), verbclass=VerbClass(obj.get("verbclass", 0)),
            state_root=_digest_from_hex(obj.get("state_root")),
            journal_sha256=_digest_from_hex(journal if isinstance(journal, str) and len(journal) == 64 else None),
            restore_point=bool(obj.get("restore_point", False)),
            reason=PointReason(obj.get("reason", PointReason.NOT_CAPTURED)),
            events=tuple(_event(e, tick) for e in obj.get("events", ())),
            outcome=ProcOutcome(obj.get("outcome", "alive")),
        )


@dataclass(frozen=True, slots=True)
class Receipt:
    """RCPT chunk body. States only what was measured."""
    kind: Literal["seek", "verify", "judge"]
    result: Literal["pass", "fail", "best_effort"]
    from_tick: int
    to_tick: int
    mode: SeekMode
    verify_level: VerifyLevel
    write_pages: int = 0
    micros: int = 0
    state_root: str = ""
    journal_sha256: str = ""
    pixel_exact: bool | None = None
    canary: Literal["none", "pending", "pass", "fail"] = "none"
    fallback: str = ""
    first_divergence: int | None = None
    restore_points: tuple[tuple[int, int], ...] = ()   # half-open tick ranges that were restore points
    verifier: str = ""
    env_key: str = ""

    def __post_init__(self) -> None:
        if self.verify_level == VerifyLevel.NONE and self.kind != "seek":
            raise FormatError("verification level none is only valid for an incomplete seek receipt")

    def to_obj(self) -> dict[str, Any]:
        obj = {k: getattr(self, k) for k in _names(type(self))}
        obj["mode"] = str(self.mode)
        obj["verify_level"] = str(self.verify_level)
        obj["restore_points"] = [list(r) for r in self.restore_points]
        return obj

    def to_payload(self) -> bytes:
        """The chained RCPT body. micros is wall time, so it stays out (Bundle.receipt keeps it in meta.json)
        and identical runs give identical journals."""
        obj = self.to_obj()
        del obj["micros"]
        return canonical_json(obj)

    @classmethod
    def from_payload(cls, payload: bytes) -> Receipt:
        obj = json.loads(payload)
        obj["mode"] = SeekMode.parse(obj["mode"])
        obj["verify_level"] = VerifyLevel(obj["verify_level"])
        obj["restore_points"] = tuple(tuple(r) for r in obj.get("restore_points", ()))
        return cls(**obj)


@runtime_checkable
class Backend(Protocol):
    """One way to run a score. Implementations: pysim, simgame, shim, handsoff, phasor.

    Methods outside the backend's capabilities raise BackendError; methods outside
    the locked tier raise RefusedError. Every method that runs ticks returns a StepReport.
    """
    name: str

    @property
    def caps(self) -> Caps: ...

    def attach(self, cfg: Mapping[str, Any], env: EnvPin) -> Caps:
        """Launch or connect, hold before CRT startup, answer hello."""
        ...

    def lock_tier(self, tier: Tier) -> None:
        """One-way ratchet, before tick 0."""
        ...

    def clock(self, mode: ClockMode, hz: int, scale: int = 100) -> None: ...

    def start(self) -> StepReport:
        """Release the pre-CRT hold and run to the first gate (tick 0)."""
        ...

    def step(self, n: int, note: Note) -> StepReport:
        """Run n ticks holding the same canonical note."""
        ...

    def wait(self, event: str, arg: str | None, timeout_ticks: int,
             note: Note | None = None) -> StepReport:
        """Run ticks holding note (default: nothing held) until event or timeout."""
        ...

    def capture(self, kind: Literal["dirty", "full"]) -> None:
        """Choose what the next gate emits: tracked pages, or a full sweep."""
        ...

    def records(self) -> Iterator[StagingRecord]:
        """Drain staging records produced since the last call."""
        ...

    def restore(self, tick: int, root: bytes, pages: Iterable[StagingRecord]) -> Receipt:
        """Write the page set W so the live state equals root, then verify."""
        ...

    def read(self, addr: int, length: int) -> bytes: ...

    def write(self, addr: int, data: bytes) -> None:
        """lab only; verbclass WRITE."""
        ...

    def semantic(self, op: Literal["read", "apply"], fields: Mapping[str, Any]) -> Mapping[str, Any]:
        """Read engine fields, or write them back (apply is lab only)."""
        ...

    def files(self, op: Literal["snap", "put"], blob: bytes | None = None) -> bytes | None: ...

    def os_event(self, ev: OsEvent) -> ProcOutcome: ...

    def watch(self, wid: str, addr: int, length: int, cond: str) -> None: ...

    def unwatch(self, wid: str) -> None: ...

    def events(self) -> Iterator[EngineEvent]: ...

    def frame(self) -> bytes | None:
        """Current frame as PNG bytes, or None when the backend has no display."""
        ...

    def close(self) -> None: ...
