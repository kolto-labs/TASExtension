"""Append-only chunk journal (contract 8.2, 8.3): framing scan, torn-tail recovery, chain audit."""

from __future__ import annotations

import json
import os
import struct
from dataclasses import dataclass, field
from typing import Any, BinaryIO, Iterator

from kptas.types import (CHAINED_TYPES, CHUNK_HEAD_SIZE, CHUNK_MAGIC, CHUNK_TRAILER_SIZE, CHUNK_TYPES,
                         MAX_CHUNK_PAYLOAD, NULL_DIGEST, EnvPin, FormatError, Header, Receipt, Tier,
                         TickRecord, canonical_json, chain_genesis, chain_link, frame_chunk, read_chunk,
                         split_chained)

GENESIS = (b"HOLO", b"SCOR", b"ENVP")
EPOCH_REASONS = ("altf4", "close", "relaunch", "crash")
_HEAD = struct.Struct("<2s4sI")

try:
    import fcntl
except ImportError:  # not POSIX; the single-writer rule is then advisory only
    fcntl = None  # type: ignore[assignment]


def _need(cond: bool, msg: str) -> None:
    if not cond:
        raise FormatError(msg)


def mark_body(name: str, tick: int) -> bytes:
    _need(isinstance(name, str) and name != "", "mark name must be a non-empty string")
    _need(type(tick) is int and tick >= 0, "mark tick must be an int >= 0")
    return canonical_json({"name": name, "tick": tick})


def epoch_body(epoch: int, tick: int, reason: str, exit_status: int | None, files_root: bytes) -> bytes:
    _need(reason in EPOCH_REASONS, f"epoch reason must be one of {EPOCH_REASONS}")
    _need(type(epoch) is int and epoch >= 0 and type(tick) is int and tick >= 0, "bad epoch or tick")
    _need(exit_status is None or type(exit_status) is int, "exit_status must be an int or None")
    return canonical_json({"epoch": epoch, "tick": tick, "reason": reason, "exit_status": exit_status,
                           "files_root": files_root.hex() if files_root != NULL_DIGEST else ""})


def seal_body(computed_tier: str | Tier, publishable: bool, ticks: int, final_link: bytes) -> bytes:
    return canonical_json({"computed_tier": Tier.parse(computed_tier).label, "publishable": bool(publishable),
                           "ticks": ticks, "final_link": final_link.hex()})


class Scanner:
    """Iterate valid chunks from `start`. Iteration stops at the first bad or torn chunk.

    After iteration, `valid_len` is the end of the valid prefix and `error` says why it ended.
    """

    def __init__(self, f: BinaryIO, start: int = 0) -> None:
        self.f = f
        self.valid_len = start
        self.error: str | None = None
        f.seek(0, os.SEEK_END)
        self.size = f.tell()

    def __iter__(self) -> Iterator[tuple[int, bytes, bytes, int]]:
        f, pos = self.f, self.valid_len
        while pos < self.size:
            f.seek(pos)
            head = f.read(CHUNK_HEAD_SIZE)
            if len(head) < CHUNK_HEAD_SIZE:
                self.error = f"truncated chunk header at {pos}"
                return
            magic, fourcc, n = _HEAD.unpack(head)
            if magic != CHUNK_MAGIC or fourcc not in CHUNK_TYPES or n > MAX_CHUNK_PAYLOAD:
                self.error = f"bad chunk header at {pos}"
                return
            if pos + CHUNK_HEAD_SIZE + n + CHUNK_TRAILER_SIZE > self.size:
                self.error = f"torn chunk at {pos}"
                return
            try:
                fourcc, payload, rel = read_chunk(head + f.read(n + CHUNK_TRAILER_SIZE))
            except FormatError as e:
                self.error = f"{e} at {pos}"
                return
            end = pos + rel
            self.valid_len = end
            yield pos, fourcc, payload, end
            pos = end


@dataclass
class ChainState:
    """Order and chain rules of 8.2 and 8.3, fed one valid chunk at a time."""
    genesis: list[bytes] = field(default_factory=list)
    header: Header | None = None
    score: str | None = None
    env: EnvPin | None = None
    h0: bytes | None = None
    link: bytes = NULL_DIGEST
    tick_floor: int = -1          # branch point; own ticks must be later
    ticks: int = 0
    last_tick: int = -1
    last_epoch: int = 0
    seal: dict[str, Any] | None = None
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def bundle_id(self) -> str:
        _need(self.h0 is not None, "journal has no complete HOLO, SCOR, ENVP prefix")
        return self.h0.hex()[:32]  # type: ignore[union-attr]

    def feed(self, fourcc: bytes, payload: bytes) -> tuple[bytes, bytes, Any]:
        """Check one chunk; return (body, link, decoded body). Raises FormatError on any violation."""
        n = len(self.genesis)
        name = fourcc.decode()
        if n < 3:
            _need(fourcc == GENESIS[n], f"chunk {n} must be {GENESIS[n].decode()}, got {name}")
            if n == 0:
                self.header = decoded = Header.from_payload(payload)
                if self.header.parent is not None:
                    self.tick_floor = self.header.parent.tick
            elif n == 1:
                self.score = decoded = payload.decode("utf-8")
            else:
                self.env = decoded = EnvPin.from_payload(payload)
                dk = self.header.determinism_key  # type: ignore[union-attr]
                _need(dk == "" or dk == self.env.key(), "HOLO determinism_key does not match ENVP")
            self.genesis.append(payload)
            if n == 2:
                self.h0 = self.link = chain_genesis(*self.genesis)
            self.counts[name] = 1
            return payload, (self.link if n == 2 else NULL_DIGEST), decoded
        _need(fourcc in CHAINED_TYPES, f"{name} may not follow ENVP")
        _need(self.seal is None or fourcc == b"RCPT", f"{name} after SEAL; only RCPT may follow it")
        body, link = split_chained(payload)
        _need(chain_link(self.link, fourcc, body) == link, f"chain link mismatch in {name} chunk")
        decoded: Any
        if fourcc == b"TICK":
            decoded = TickRecord.decode(body)
            _need(decoded.tick > max(self.last_tick, self.tick_floor),
                  f"tick {decoded.tick} does not follow tick {max(self.last_tick, self.tick_floor)}")
            _need(decoded.epoch >= self.last_epoch, f"epoch {decoded.epoch} goes backwards")
            self.last_tick, self.last_epoch = decoded.tick, decoded.epoch
            self.ticks += 1
        elif fourcc == b"RCPT":
            decoded = Receipt.from_payload(body)
        else:
            decoded = json.loads(body)
            _need(canonical_json(decoded) == body, f"{name} body is not canonical JSON")
            if fourcc == b"MARK":
                mark_body(decoded.get("name"), decoded.get("tick"))
                _need(set(decoded) == {"name", "tick"}, "MARK body keys are name and tick")
            elif fourcc == b"EPOC":
                _need(set(decoded) == {"epoch", "tick", "reason", "exit_status", "files_root"},
                      "EPOC body keys are epoch, tick, reason, exit_status, files_root")
                root = bytes.fromhex(decoded["files_root"]) if decoded["files_root"] else NULL_DIGEST
                _need(epoch_body(decoded["epoch"], decoded["tick"], decoded["reason"],
                                 decoded["exit_status"], root) == body, "EPOC body is malformed")
            else:
                _need(set(decoded) == {"computed_tier", "publishable", "ticks", "final_link"},
                      "SEAL body keys are computed_tier, publishable, ticks, final_link")
                _need(decoded["final_link"] == self.link.hex(), "SEAL final_link is not the link before SEAL")
                _need(decoded["ticks"] == self.ticks, f"SEAL counts {decoded['ticks']} ticks, journal has {self.ticks}")
                Tier.parse(decoded["computed_tier"])
                self.seal = decoded
        self.link = link
        self.counts[name] = self.counts.get(name, 0) + 1
        return body, link, decoded


@dataclass
class AuditReport:
    ok: bool
    error: str | None
    error_offset: int | None
    valid_len: int
    file_len: int
    bundle_id: str
    counts: dict[str, int]
    ticks: int
    last_tick: int
    sealed: bool
    last_link: str

    @property
    def torn_bytes(self) -> int:
        return self.file_len - self.valid_len


def audit_journal(path: str | os.PathLike[str]) -> AuditReport:
    """Scan a journal read-only and check framing, order and every chain link.

    A torn tail is reported (torn_bytes) but is not a failure; a valid frame that
    breaks the chain or the order rules is.
    """
    st = ChainState()
    err: str | None = None
    at: int | None = None
    with open(path, "rb") as f:
        sc = Scanner(f)
        for off, fourcc, payload, _end in sc:
            try:
                st.feed(fourcc, payload)
            except (FormatError, ValueError, KeyError, TypeError) as e:
                err, at = str(e) or type(e).__name__, off
                break
        valid = sc.valid_len if err is None else at
        if err is None and st.h0 is None:
            err, at = "journal has no complete HOLO, SCOR, ENVP prefix", sc.valid_len
    return AuditReport(ok=err is None, error=err, error_offset=at, valid_len=valid or 0, file_len=sc.size,
                       bundle_id=st.h0.hex()[:32] if st.h0 else "", counts=dict(st.counts), ticks=st.ticks,
                       last_tick=st.last_tick, sealed=st.seal is not None, last_link=st.link.hex())


class Journal:
    """One open journal file. A writable journal holds an exclusive lock."""

    def __init__(self, path: str | os.PathLike[str], writable: bool, create: bool = False) -> None:
        self.path = os.fspath(path)
        self.writable = writable
        # A failed write or durability barrier may have published an unknown
        # prefix. Refuse further appends until this writer is closed and the
        # journal is explicitly reopened/recovered.
        self.write_failure: BaseException | None = None
        mode = "x+b" if create else ("r+b" if writable else "rb")
        self.f = open(self.path, mode)
        if writable and fcntl is not None:
            try:
                fcntl.flock(self.f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                self.f.close()
                raise FormatError(f"{self.path} is open for writing elsewhere") from None

    def size(self) -> int:
        return os.fstat(self.f.fileno()).st_size

    def scan(self, start: int = 0) -> Scanner:
        return Scanner(self.f, start)

    def read(self, offset: int) -> tuple[bytes, bytes]:
        self.f.seek(offset)
        head = self.f.read(CHUNK_HEAD_SIZE)
        _need(len(head) == CHUNK_HEAD_SIZE, f"no chunk at {offset}")
        n = _HEAD.unpack(head)[2]
        _need(n <= MAX_CHUNK_PAYLOAD, f"bad chunk length at {offset}")
        fourcc, payload, _ = read_chunk(head + self.f.read(n + CHUNK_TRAILER_SIZE))
        return fourcc, payload

    def truncate(self, length: int) -> None:
        _need(self.writable, "journal is open read-only")
        _need(self.write_failure is None, "journal writer failed; close and recover before truncating")
        try:
            self.f.truncate(length)
            self.f.flush()
            os.fsync(self.f.fileno())
        except BaseException as exc:
            self.write_failure = exc
            raise

    def append_raw(self, fourcc: bytes, payload: bytes) -> tuple[int, int]:
        """Append one framed chunk at the end; return (offset, end)."""
        _need(self.writable, "journal is open read-only")
        _need(self.write_failure is None, "journal writer failed; close and recover before appending")
        frame = frame_chunk(fourcc, payload)
        try:
            self.f.seek(0, os.SEEK_END)
            off = self.f.tell()
            if self.f.write(frame) != len(frame):
                raise OSError("short journal write")
            self.f.flush()
        except BaseException as exc:
            # A failed write may have published a prefix. Never append beyond
            # it or pretend retrying the same record is safe.
            self.write_failure = exc
            raise
        return off, off + len(frame)

    def fsync(self) -> None:
        _need(self.writable, "journal is open read-only")
        _need(self.write_failure is None, "journal writer failed; close and recover before syncing")
        try:
            self.f.flush()
            os.fsync(self.f.fileno())
        except BaseException as exc:
            self.write_failure = exc
            raise

    def close(self) -> None:
        if not self.f.closed:
            try:
                self.f.close()
            except BaseException as exc:
                if self.write_failure is None:
                    self.write_failure = exc
                raise
