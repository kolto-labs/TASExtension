"""Staging ring (contract section 9) over a file mapping, plus plain record files.

The producer owns write_pos, last_tick and producer_pid; the consumer owns
read_pos, consumer_pid and state. Each side stores only its own fields.
"""

from __future__ import annotations

import mmap
import os
import struct
import dataclasses
from collections.abc import Iterable, Iterator
from pathlib import Path

from kptas.types import (PAGE_SIZE, RING_HEADER_SIZE, BackendError, FormatError, RecordFlag, RecordKind,
                         RingHeader, StagingRecord, batch_digest)

_Q = struct.Struct("<Q")
_I = struct.Struct("<I")
_OFF_WRITE, _OFF_READ, _OFF_LAST = 16, 24, 32
_OFF_PPID, _OFF_CPID, _OFF_STATE = 40, 44, 48
_OFF_BEAT = 64                     # consumer heartbeat (streamed steps); zero otherwise
_HEAD = StagingRecord.HEADER_SIZE
# a record may need a skip of nearly its own size, so the largest record is capacity // 2
MIN_CAPACITY = 3 * PAGE_SIZE


def _verified_record(buf, offset):
    """Private full-validation decoder; no digest is skipped before publication."""
    from kptas._verified_page import _verified_page
    # Decode and check all structure first. The fresh content check immediately
    # below is the same admission boundary, before any consumer-pointer update.
    rec = StagingRecord.from_bytes(buf, offset, verify_hash=False)
    if rec.kind is RecordKind.PAGE and rec.flags & RecordFlag.DATA:
        try:
            data = _verified_page(rec.vaddr, rec.hash, rec.data)
        except ValueError as exc:
            raise FormatError(str(exc)) from None
        return dataclasses.replace(rec, data=data)
    rec.validate(verify_hash=True)
    return rec

STATE_NEW, STATE_ATTACHED, STATE_CLOSING = 0, 1, 2


class RingFull(BackendError):
    """The consumer has not freed enough space for the next record."""


class StagingRing:
    """One mapping of a staging-ring file. Use ``create`` or ``open``."""

    def __init__(self, path: str | os.PathLike[str], fd: int, mm: mmap.mmap, capacity: int):
        self.path = Path(path)
        self._fd = fd
        self._mm = mm
        self.capacity = capacity

    @classmethod
    def create(cls, path: str | os.PathLike[str], capacity: int) -> StagingRing:
        """Create (or truncate) the file, write a fresh header, map it."""
        if capacity < MIN_CAPACITY or capacity % PAGE_SIZE:
            raise BackendError(f"ring capacity {capacity} must be a page multiple >= {MIN_CAPACITY}")
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.ftruncate(fd, RING_HEADER_SIZE + capacity)
            mm = mmap.mmap(fd, RING_HEADER_SIZE + capacity)
        except BaseException:
            os.close(fd)
            raise
        mm[:RingHeader.SIZE] = RingHeader(capacity).to_bytes()
        return cls(path, fd, mm, capacity)

    @classmethod
    def open(cls, path: str | os.PathLike[str]) -> StagingRing:
        fd = os.open(path, os.O_RDWR)
        try:
            size = os.fstat(fd).st_size
            if size < RING_HEADER_SIZE + MIN_CAPACITY:
                raise BackendError(f"{path}: {size} bytes is too small for a staging ring")
            mm = mmap.mmap(fd, size)
        except BaseException:
            os.close(fd)
            raise
        head = RingHeader.from_bytes(mm)
        if head.header_size != RING_HEADER_SIZE or head.align != 8:
            mm.close()
            os.close(fd)
            raise FormatError("ring header size or alignment is not the contract's")
        if head.capacity < MIN_CAPACITY or head.capacity % PAGE_SIZE:
            mm.close()
            os.close(fd)
            raise FormatError(f"ring capacity {head.capacity} must be a page multiple >= {MIN_CAPACITY}")
        if RING_HEADER_SIZE + head.capacity > size:
            mm.close()
            os.close(fd)
            raise FormatError("ring capacity exceeds the file")
        return cls(path, fd, mm, head.capacity)

    # header -------------------------------------------------------------

    @property
    def header(self) -> RingHeader:
        return RingHeader.from_bytes(self._mm)

    def _get(self, st: struct.Struct, off: int) -> int:
        return st.unpack_from(self._mm, off)[0]

    def _put(self, st: struct.Struct, off: int, v: int) -> None:
        st.pack_into(self._mm, off, v)

    @property
    def write_pos(self) -> int:
        return self._get(_Q, _OFF_WRITE)

    @property
    def read_pos(self) -> int:
        return self._get(_Q, _OFF_READ)

    @property
    def lag(self) -> int:
        """Bytes written but not yet consumed."""
        return self.write_pos - self.read_pos

    def set_producer(self, pid: int | None = None) -> None:
        self._put(_I, _OFF_PPID, os.getpid() if pid is None else pid)

    def set_consumer(self, pid: int | None = None, state: int = STATE_ATTACHED) -> None:
        self._put(_I, _OFF_CPID, os.getpid() if pid is None else pid)
        self._put(_I, _OFF_STATE, state)

    def set_state(self, state: int) -> None:
        self._put(_I, _OFF_STATE, state)

    def beat(self) -> None:
        """Bump the consumer heartbeat (u32 at header offset 64, past the RingHeader fields). The
        shim's ring wait keeps a streamed step's game waiting while it moves (wire.cpp ring_patience)."""
        if self._mm is None:
            raise BackendError("ring closed")
        self._put(_I, _OFF_BEAT, (self._get(_I, _OFF_BEAT) + 1) & 0xFFFFFFFF)

    # producer -----------------------------------------------------------

    def _plan(self, pos: int, size: int) -> tuple[int, bool]:
        """(bytes skipped before the record, whether a WRAP record fills the skip)."""
        rem = self.capacity - pos % self.capacity
        if rem < _HEAD:
            return rem, False
        if size > rem:
            return rem, True
        return 0, False

    def free(self) -> int:
        return self.capacity - self.lag

    def fits(self, records: Iterable[StagingRecord]) -> bool:
        """True when every record would fit without the consumer reading anything."""
        pos, used = self.write_pos, self.lag
        for rec in records:
            skip, _ = self._plan(pos, rec.size)
            used += skip + rec.size
            pos += skip + rec.size
            if used > self.capacity:
                return False
        return True

    def write(self, rec: StagingRecord) -> None:
        """Append one record, then publish write_pos. Raises RingFull when there is no room."""
        data = rec.to_bytes()
        if 2 * len(data) > self.capacity:
            raise BackendError(f"record of {len(data)} bytes exceeds half the ring capacity")
        pos = self.write_pos
        skip, wrap = self._plan(pos, len(data))
        if self.lag + skip + len(data) > self.capacity:
            raise RingFull(f"ring full: {self.lag} of {self.capacity} bytes unread")
        if wrap:
            off = RING_HEADER_SIZE + pos % self.capacity
            self._mm[off:off + _HEAD] = StagingRecord(RecordKind.WRAP, 0).to_bytes()
        pos += skip
        off = RING_HEADER_SIZE + pos % self.capacity
        self._mm[off:off + len(data)] = data
        if rec.kind is RecordKind.TICK_END:
            self._put(_Q, _OFF_LAST, rec.tick)
        self._put(_Q, _OFF_WRITE, pos + len(data))

    def write_batch(self, records: Iterable[StagingRecord]) -> None:
        for rec in records:
            self.write(rec)

    # consumer -----------------------------------------------------------

    def read(self, verify_hash: bool = True, limit: int | None = None, *,
             _verified_pages: bool = False) -> Iterator[StagingRecord]:
        """Yield records up to the write_pos seen at each step; store read_pos after each."""
        if type(_verified_pages) is not bool:
            raise TypeError('verified page representation must be a boolean')
        if _verified_pages and not verify_hash:
            raise ValueError('verified page representation requires full hash verification')
        n = 0
        while limit is None or n < limit:
            rpos, wpos = self.read_pos, self.write_pos
            if rpos >= wpos:
                return
            off = rpos % self.capacity
            rem = self.capacity - off
            if rem < _HEAD:
                self._put(_Q, _OFF_READ, rpos + rem)
                continue
            rec = (_verified_record(self._mm, RING_HEADER_SIZE + off) if _verified_pages else
                   StagingRecord.from_bytes(self._mm, RING_HEADER_SIZE + off, verify_hash=verify_hash))
            if rec.kind is RecordKind.WRAP:
                self._put(_Q, _OFF_READ, rpos + rem)
                continue
            if rec.size > rem:
                raise FormatError(f"record at ring offset {off} straddles the end")
            self._put(_Q, _OFF_READ, rpos + rec.size)
            n += 1
            yield rec

    def close(self) -> None:
        if self._mm is not None:
            self._mm.close()
            os.close(self._fd)
            self._mm = None  # type: ignore[assignment]

    def __enter__(self) -> StagingRing:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


# ---------------------------------------------------------------- record files and batches

def write_records(path: str | os.PathLike[str], records: Iterable[StagingRecord]) -> int:
    """Write records back to back (restore sets, Python to shim). Returns the record count."""
    n = 0
    with open(path, "wb") as f:
        for rec in records:
            f.write(rec.to_bytes())
            n += 1
        f.flush()
        os.fsync(f.fileno())
    return n


def read_records(path: str | os.PathLike[str], verify_hash: bool = True) -> list[StagingRecord]:
    buf = Path(path).read_bytes()
    out, off = [], 0
    while off < len(buf):
        rec = StagingRecord.from_bytes(buf, off, verify_hash=verify_hash)
        out.append(rec)
        off += rec.size
    return out


Batch = tuple[int, list[StagingRecord], "StagingRecord | None", StagingRecord]


class BatchAssembler:
    """Group a streamed record sequence into ticks, checking the section 9 batch rules.

    ``feed`` returns (tick, PAGE records, JOURNAL or None, TICK_END) when a
    TICK_END closes a batch, else None. A batch cut by a ring drain carries over.
    """

    def __init__(self) -> None:
        self._pages: list[StagingRecord] = []
        self._journal: StagingRecord | None = None
        self._tick: int | None = None

    @property
    def pending(self) -> bool:
        return self._tick is not None

    def feed(self, rec: StagingRecord) -> Batch | None:
        if rec.kind is RecordKind.WRAP:
            return None
        if self._tick is None:
            self._tick = rec.tick
        tick = self._tick
        if rec.tick != tick:
            raise FormatError(f"record for tick {rec.tick} inside the batch of tick {tick}")
        if rec.kind is RecordKind.PAGE:
            if self._journal is not None:
                raise FormatError(f"PAGE after JOURNAL in tick {tick}")
            self._pages.append(rec)
            return None
        if rec.kind is RecordKind.JOURNAL:
            if self._journal is not None:
                raise FormatError(f"two JOURNAL records in tick {tick}")
            self._journal = rec
            return None
        if self._journal is None:
            raise FormatError(f"tick {tick} batch has no JOURNAL record")
        if rec.flags & RecordFlag.UNCAPTURED and self._pages:
            raise FormatError(f"tick {tick}: an UNCAPTURED batch has PAGE records")
        batch = [*self._pages, self._journal]
        if batch_digest(batch) != rec.hash:
            raise FormatError(f"TICK_END digest mismatch at tick {tick}")
        out = (tick, self._pages, self._journal, rec)
        self._pages, self._journal, self._tick = [], None, None
        return out


def batches(records: Iterable[StagingRecord]) -> Iterator[Batch]:
    """All complete batches of a finished stream; a trailing partial batch is an error."""
    asm = BatchAssembler()
    for rec in records:
        b = asm.feed(rec)
        if b is not None:
            yield b
    if asm.pending:
        raise FormatError("stream ends inside a tick batch")
