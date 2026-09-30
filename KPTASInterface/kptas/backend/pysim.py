"""PySim: a deterministic pure-Python stand-in game behind the Backend protocol.

Every mutable game fact lives in the flat page space (tick, virtual clock,
phase, loader progress, heap bitmap, objects, rand state, input tables), so a
page-level restore is a full restore. Python keeps only caches: the captured
page map, the dirty set, pending verbclass bits and events.

Tick order: gate t captures tick t, then holds. The next step releases it:
pending restore, V(t+1), input for t+1, then pass t+1 and gate t+1.
"""

from __future__ import annotations

import base64
import hashlib
import json
import struct
import time
import zlib
from collections import deque
from collections.abc import Iterable, Iterator, Mapping
from typing import Any, Literal

from kptas.supervisor import filetime
from kptas.types import (PAGE_SIZE, VERB_ALLOW, BackendError, Caps, ClockMode, EngineEvent, EnvPin,
                         MemClass, OsEvent, PageRef, PointReason, ProcOutcome, Receipt,
                         RecordFlag, RecordKind, RefusedError, SeekMode, StagingRecord, StepReport,
                         Tier, VerbClass, VerifyLevel, canonical_json, page_key, sha256, state_root)

from .staging import RingFull, StagingRing

IMAGE_BASE, IMAGE_PAGES = 0x007A0000, 4
HARNESS_BASE, HARNESS_PAGES = 0x0FF00000, 4
ARENA_BASE = 0x10000000
PROT_RW = 0x04

# control block (harness page 0)
CB = HARNESS_BASE
_CB = {  # name: (offset, struct format)
    "magic": (0x00, "<I"), "tick": (0x04, "<Q"), "v": (0x0C, "<Q"), "seg_start": (0x14, "<Q"),
    "seg_v": (0x1C, "<Q"), "hz": (0x24, "<I"), "pending_hz": (0x28, "<I"), "phase": (0x2C, "<I"),
    "load_progress": (0x30, "<I"), "load_total": (0x34, "<I"), "ai_cursor": (0x38, "<I"),
    "module": (0x3C, "<I"), "area_ticks": (0x40, "<I"), "flags": (0x44, "<I"), "epoch": (0x48, "<I"),
    "fade_us": (0x4C, "<I"), "saves": (0x50, "<I"), "menu_ticks": (0x54, "<I"),
}
CB_PREV_KEYS = CB + 0x100      # 32-byte bitmap of keys down on the previous tick
CB_PREV_BUTTONS = CB + 0x120
CB_CONTEXT = CB + 0x200        # fiber context record stand-in
INPUT = HARNESS_BASE + PAGE_SIZE   # staged input frame (harness page 1)
INPUT_SIZE = 256 + 4 + 4 + 4 + 4 + 4
HOLDRAND = IMAGE_BASE + 0x29C4     # _tiddata + 0x14 stand-in
CEXORAND = IMAGE_BASE + 0x0010
EMITTER = IMAGE_BASE + PAGE_SIZE   # image page 1: emitter samples and menu counter
HEAP_BITMAP = ARENA_BASE + 0x40
OBJ_TABLE = ARENA_BASE + PAGE_SIZE
OBJ_SIZE, MAX_OBJ = 32, 16
_OBJ = struct.Struct("<IIiiiiiI")   # alive, kind, x, y, vx, vy, hp, trail page

PHASE_MENU, PHASE_LOADING, PHASE_AREA = 0, 1, 2
FLAG_AREA_READY, FLAG_EXITED = 1, 2
LOAD_TICKS, LOAD_SUBTICKS = 12, 3
WORLD_W, WORLD_H = 800, 600
AI_BUDGET_US, THINK_US = 10_000, 1_500
# Estimate, not measured: an in-game quicksave (StallEventSaveGame equivalent) blocking the
# main thread on period HDD/SSD storage. Documented default per docs/score.md `save_ms`;
# score.save_ms or a one-shot `@save ms=N` overrides it. 0 is allowed only as an explicit choice.
DEFAULT_SAVE_MS = 900

# stand-in return addresses of rand() call sites, journalled per site
SITE_SPAWN, SITE_THINK, SITE_ATTACK, SITE_EMITTER = 0x004C5900, 0x004B0C10, 0x005A1234, 0x004A89F0
SITE_AI_CLOCK, SITE_FADE_CLOCK = 0x004B0B90, 0x004045C0

_MSVC_A, _MSVC_C = 0x343FD, 0x269EC3
_DIK = None


def _dik(name: str) -> int:
    global _DIK
    if _DIK is None:
        from kptas.score.keys import dik
        _DIK = dik
    return _DIK(name)


class _Note:
    held: tuple[str, ...] = ()
    mouse_abs = None
    mouse_rel = None
    buttons: tuple[str, ...] = ()
    wheel = 0
    osev = None


_EMPTY = _Note()


class PySim:
    """Pure-Python sim backend. Optional ``ring_path`` routes records through a real ring file."""

    name = "pysim"

    def __init__(self, ring_path: str | None = None, ring_capacity: int = 4 << 20,
                 open_ring: bool = False):
        self._ring_path = ring_path
        self._open_ring = open_ring
        self._ring_capacity = ring_capacity
        self._ring: StagingRing | None = None
        self._spill: deque[StagingRecord] = deque()
        self._pages: dict[int, bytearray] = {}
        self._dirty: set[int] = set()
        self._gone: set[int] = set()
        self._refs: dict[int, PageRef] = {}
        self._root = sha256(b"KPRT")
        self._tier: Tier | None = None
        self._started = False
        self._epoch = 0
        self._pending_vc = VerbClass.NONE
        self._next_full = False
        self._events: deque[EngineEvent] = deque()
        self._tick_events: list[EngineEvent] = []
        self._report_events: list[EngineEvent] = []
        self._saving = False
        self._watch_hit = False
        self._draws: dict[str, int] = {}
        self._intra = 0
        self._watches: dict[str, list[Any]] = {}
        self._files: dict[str, tuple[bytes, int]] = {}
        self._extra_input: _Note | None = None
        self._last_journal = b""
        self._last_reason = PointReason.NOT_CAPTURED
        self._last_subticks = 0
        self._caps = Caps(
            backend="pysim", tier_ceiling=Tier.TAS, clock_modes=frozenset({ClockMode.VIRTUAL}),
            seek_modes=frozenset(SeekMode), input_paths=frozenset({"dinput"}),
            thread_models=frozenset({"join"}), tick_source="sim", seed_hooked=True, capture=True,
            fiber=True, ring=True, build="pysim-1", exe_sha256=hashlib.sha256(b"pysim/1").hexdigest(),
            shim_version="pysim/1", modules=("pysim",), hooks=("gate", "clock", "input"))
        self._configure({})

    # ------------------------------------------------------------------ config and lifecycle

    def _configure(self, cfg: Mapping[str, Any]) -> None:
        sim, clock = cfg.get("sim", {}), cfg.get("clock", {})
        self.seed = int(sim.get("seed", 0))
        npages = int(sim.get("pages", 256))
        self.arena_pages = npages - IMAGE_PAGES - HARNESS_PAGES
        if self.arena_pages < 32 or self.arena_pages > 4096:
            raise BackendError(f"sim.pages={npages} leaves {self.arena_pages} arena pages; need 32..4096")
        self.hz = int(clock.get("hz", 60))
        self.qpf_hz = int(clock.get("qpf_hz", 10_000_000))
        self.uptime0_ms = int(clock.get("uptime0_ms", 3_600_000))
        self.realtime0 = filetime(str(clock.get("initial_realtime", "2004-02-12T00:00:00Z")))
        self.budget_model = str(clock.get("budget_model", "frozen"))
        guard = clock.get("budget_guard", {})
        self.site_reads = int(guard.get("site_reads", 100_000))
        self.step_us = int(guard.get("step_us", 1))
        # clock.save_ms from config; a score header `save_ms` replaces it (set_save_ms) and a
        # one-shot `@save ms=N` covers only the next save (set_next_save_ms).
        self.save_ms = int(clock.get("save_ms", DEFAULT_SAVE_MS))
        self._next_save_ms: int | None = None
        if not 1 <= self.hz <= 1000 or self.qpf_hz < 1 or self.step_us < 1:
            raise BackendError("clock.hz must be 1..1000, qpf_hz >= 1 and budget_guard.step_us >= 1")
        if self.save_ms < 0:
            raise BackendError("clock.save_ms must be >= 0")

    def set_save_ms(self, ms: int) -> None:
        """Default virtual length of every save (the score header `save_ms`)."""
        if ms < 0:
            raise BackendError("save ms must be >= 0")
        self.save_ms = ms

    def set_next_save_ms(self, ms: int) -> None:
        """One-shot override for the very next save.begin (a compiled `@save ms=N` item).

        Consumed by the caller that drives the score timeline against a save.begin
        @sync or an in-game save trigger; reverts to self.save_ms after one use.
        """
        if ms < 0:
            raise BackendError("save ms must be >= 0")
        self._next_save_ms = ms

    @property
    def caps(self) -> Caps:
        return self._caps

    def attach(self, cfg: Mapping[str, Any], env: EnvPin | None = None) -> Caps:
        """Configure and hold before CRT startup. Nothing runs until start()."""
        if self._started:
            raise BackendError("attach after start")
        self._configure(cfg)
        if self._ring_path and self._open_ring:
            self._ring = StagingRing.open(self._ring_path)   # a consumer elsewhere drains it
            self._ring.set_producer()
        elif self._ring_path:
            self._ring = StagingRing.create(self._ring_path, self._ring_capacity)
            self._ring.set_producer()
            self._ring.set_consumer()
        return self._caps

    def _allow(self, verb: str) -> None:
        allowed = VERB_ALLOW.get(verb, frozenset())
        if self._tier is None and Tier.TAS not in allowed:
            raise RefusedError(f"{verb} is lab only; lock the tier at lab first")
        if self._tier is not None and self._tier not in allowed:
            raise RefusedError(f"{verb} is refused in tier {self._tier.label}")

    def _alive(self) -> None:
        if not self._started:
            raise BackendError("not started")
        if self._get("flags") & FLAG_EXITED:
            raise BackendError("the process has exited")

    def lock_tier(self, tier: Tier) -> None:
        tier = Tier.parse(tier)
        if self._started:
            raise BackendError("tier.lock must come before start")
        if self._tier is not None:
            raise BackendError(f"tier already locked at {self._tier.label}")
        if tier > self._caps.tier_ceiling:
            raise BackendError(f"pysim cannot run tier {tier.label}; its ceiling is tas")
        self._tier = tier

    def clock(self, mode: ClockMode, hz: int, scale: int = 100) -> None:
        mode = ClockMode(mode)
        self._allow("clock")
        if mode is not ClockMode.VIRTUAL:
            if self._tier is Tier.TAS:
                raise RefusedError(f"clock {mode} is refused in tier tas")
            raise BackendError(f"pysim supports only the virtual clock, not {mode}")
        if scale != 100:
            raise BackendError("scale other than 100 needs the fixed clock")
        if not 1 <= hz <= 1000:
            raise BackendError("hz must be 1..1000")
        if self._started:
            self._set("pending_hz", hz)   # takes effect at the next tick
        else:
            self.hz = hz
        self._pending_vc |= VerbClass.CLOCK

    def start(self) -> StepReport:
        if self._started:
            raise BackendError("already started")
        self._allow("start")
        self._started = True
        self._boot(0)
        self._pending_vc |= VerbClass.GATE
        return self._report(VerbClass.NONE)

    def close(self) -> None:
        if self._ring is not None:
            if not self._open_ring:
                self._ring.set_state(2)
            self._ring.close()
            self._ring = None

    # ------------------------------------------------------------------ memory

    @staticmethod
    def mclass(vaddr: int) -> MemClass:
        if IMAGE_BASE <= vaddr < IMAGE_BASE + IMAGE_PAGES * PAGE_SIZE:
            return MemClass.IMAGE_DATA
        if HARNESS_BASE <= vaddr < HARNESS_BASE + HARNESS_PAGES * PAGE_SIZE:
            return MemClass.HARNESS_GAME
        return MemClass.GAME_ARENA

    def _in_sim(self, vaddr: int) -> bool:
        return (IMAGE_BASE <= vaddr < IMAGE_BASE + IMAGE_PAGES * PAGE_SIZE
                or HARNESS_BASE <= vaddr < HARNESS_BASE + HARNESS_PAGES * PAGE_SIZE
                or ARENA_BASE <= vaddr < ARENA_BASE + self.arena_pages * PAGE_SIZE)

    def _commit(self, vaddr: int) -> None:
        if vaddr not in self._pages:
            self._pages[vaddr] = bytearray(PAGE_SIZE)
            self._gone.discard(vaddr)
        self._dirty.add(vaddr)

    def _decommit(self, vaddr: int) -> None:
        if self._pages.pop(vaddr, None) is not None:
            self._dirty.discard(vaddr)
            self._gone.add(vaddr)

    def _page(self, addr: int) -> bytearray:
        try:
            return self._pages[addr & ~0xFFF]
        except KeyError:
            raise BackendError(f"access violation at {addr:#x}") from None

    def _ld(self, fmt: str, addr: int) -> int:
        return struct.unpack_from(fmt, self._page(addr), addr & 0xFFF)[0]

    def _st(self, fmt: str, addr: int, value: int) -> None:
        struct.pack_into(fmt, self._page(addr), addr & 0xFFF, value)
        self._dirty.add(addr & ~0xFFF)

    def _get(self, name: str) -> int:
        off, fmt = _CB[name]
        return self._ld(fmt, CB + off)

    def _set(self, name: str, value: int) -> None:
        off, fmt = _CB[name]
        self._st(fmt, CB + off, value)

    def _peek(self, addr: int, n: int) -> bytes:
        out = bytearray()
        while n > 0:
            page, off = self._page(addr), addr & 0xFFF
            k = min(n, PAGE_SIZE - off)
            out += page[off:off + k]
            addr, n = addr + k, n - k
        return bytes(out)

    def _poke(self, addr: int, data: bytes) -> None:
        i = 0
        while i < len(data):
            page, off = self._page(addr + i), (addr + i) & 0xFFF
            k = min(len(data) - i, PAGE_SIZE - off)
            page[off:off + k] = data[i:i + k]
            self._dirty.add((addr + i) & ~0xFFF)
            i += k

    # heap: first-fit page allocator whose bitmap lives in arena page 0
    def _alloc_page(self) -> int:
        for idx in range(2, self.arena_pages):
            byte = HEAP_BITMAP + idx // 8
            bits = self._ld("<B", byte)
            if not bits & (1 << (idx % 8)):
                self._st("<B", byte, bits | (1 << (idx % 8)))
                vaddr = ARENA_BASE + idx * PAGE_SIZE
                self._commit(vaddr)
                return vaddr
        raise BackendError("arena exhausted")

    def _free_page(self, vaddr: int) -> None:
        idx = (vaddr - ARENA_BASE) // PAGE_SIZE
        byte = HEAP_BITMAP + idx // 8
        self._st("<B", byte, self._ld("<B", byte) & ~(1 << (idx % 8)))
        self._decommit(vaddr)

    # ------------------------------------------------------------------ clock and rand

    def _v_at(self, n: int) -> int:
        return self._get("seg_v") + (n - self._get("seg_start")) * 1_000_000 // self._get("hz")

    def _qpc(self, site: int, reads: dict[int, int], trips: dict[str, int]) -> int:
        """One clock read at a call site; the guard micro-advances a site that spins."""
        c = reads.get(site, 0) + 1
        reads[site] = c
        if c > self.site_reads:
            self._intra += self.step_us
            k = f"{site:#010x}"
            trips[k] = trips.get(k, 0) + 1
        u = self.uptime0_ms * 1000 + self._get("v") + self._intra
        return u * self.qpf_hz // 1_000_000

    def _spin_until(self, site: int, us: int, reads: dict[int, int], trips: dict[str, int]) -> None:
        """A loop polling the clock until `us` elapse; done arithmetically, same trips as polling."""
        start = self._qpc(site, reads, trips)
        target = start + us * self.qpf_hz // 1_000_000
        while self._qpc(site, reads, trips) < target:
            if reads[site] < self.site_reads:
                reads[site] = self.site_reads   # skip the reads that cannot advance time

    def _rand(self, site: int) -> int:
        h = (self._ld("<I", HOLDRAND) * _MSVC_A + _MSVC_C) & 0xFFFFFFFF
        self._st("<I", HOLDRAND, h)
        k = f"{site:#010x}"
        self._draws[k] = self._draws.get(k, 0) + 1
        return (h >> 16) & 0x7FFF

    # ------------------------------------------------------------------ boot and passes

    def _boot(self, tick: int) -> None:
        """Process start: DllMain, CRT startup and StartServices, all at V = 0."""
        for v in list(self._pages):
            self._decommit(v)
        for i in range(IMAGE_PAGES):
            self._commit(IMAGE_BASE + i * PAGE_SIZE)
        for i in range(HARNESS_PAGES):
            self._commit(HARNESS_BASE + i * PAGE_SIZE)
        self._commit(ARENA_BASE)
        self._commit(OBJ_TABLE)
        self._st("<B", HEAP_BITMAP, 0b11)
        self._poke(IMAGE_BASE + 0x100, b"swkotor pysim .data\0")
        for name, value in (("magic", 0x4D53504B), ("tick", tick), ("v", 0), ("seg_start", tick),
                            ("seg_v", 0), ("hz", self.hz), ("epoch", self._epoch), ("fade_us", 3)):
            self._set(name, value)
        self._draws = {}
        self._intra = 0
        reads: dict[int, int] = {}
        trips: dict[str, int] = {}
        # srand(GetTickCount()) inside StartServices; V = 0 so the seed is uptime0_ms
        self._st("<I", HOLDRAND, (self.uptime0_ms * 1000 + self._get("v")) // 1000 & 0xFFFFFFFF)
        self._st("<I", CEXORAND, (0x5EED ^ self.seed) & 0xFFFFFFFF)
        self._next_full = True
        self._tick_events = [EngineEvent("main_menu", tick)]
        self._gate(tick, subticks=0, reads=reads, trips=trips, wndproc=[])

    def _stage_input(self, note: Any) -> None:
        """Gate step 7: write the input frame for the next tick into harness memory."""
        keys = bytearray(256)
        for tok in note.held:
            for k in tok.split("+"):
                keys[_dik(k)] = 0x80
        x, y = self._ld("<i", INPUT + 256), self._ld("<i", INPUT + 260)
        if note.mouse_abs is not None:
            x, y = note.mouse_abs
        if note.mouse_rel is not None:
            x, y = x + note.mouse_rel[0], y + note.mouse_rel[1]
        x, y = max(0, min(WORLD_W - 1, x)), max(0, min(WORLD_H - 1, y))
        btn = sum(1 << ("lmb", "rmb", "mmb").index(b) for b in note.buttons)
        self._poke(INPUT, bytes(keys))
        self._st("<i", INPUT + 256, x)
        self._st("<i", INPUT + 260, y)
        self._st("<I", INPUT + 264, btn)
        self._st("<i", INPUT + 268, note.wheel)
        self._st("<I", INPUT + 272, (self._ld("<I", INPUT + 272) + 1) & 0xFFFFFFFF)

    def _key_edges(self) -> tuple[bytes, set[int], int]:
        keys = self._peek(INPUT, 256)
        prev = self._peek(CB_PREV_KEYS, 32)
        now = bytearray(32)
        edges = set()
        for i, b in enumerate(keys):
            if b & 0x80:
                now[i // 8] |= 1 << (i % 8)
                if not prev[i // 8] & (1 << (i % 8)):
                    edges.add(i)
        self._poke(CB_PREV_KEYS, bytes(now))
        btn = self._ld("<I", INPUT + 264)
        bedge = btn & ~self._ld("<I", CB_PREV_BUTTONS)
        self._st("<I", CB_PREV_BUTTONS, btn)
        return keys, edges, bedge

    def _obj(self, i: int) -> list[int]:
        return list(_OBJ.unpack(self._peek(OBJ_TABLE + 0x20 + i * OBJ_SIZE, _OBJ.size)))

    def _put_obj(self, i: int, o: list[int]) -> None:
        self._poke(OBJ_TABLE + 0x20 + i * OBJ_SIZE, _OBJ.pack(*o))

    def _begin_load(self, module: int) -> None:
        for idx in range(2, self.arena_pages):
            if self._ld("<B", HEAP_BITMAP + idx // 8) & (1 << (idx % 8)):
                self._free_page(ARENA_BASE + idx * PAGE_SIZE)
        self._poke(OBJ_TABLE, bytes(0x20 + MAX_OBJ * OBJ_SIZE))
        self._set("module", module)
        self._set("phase", PHASE_LOADING)
        self._set("load_progress", 0)
        self._set("load_total", LOAD_TICKS + module % 3)
        self._set("area_ticks", 0)
        self._alloc_page()   # loader scratch, freed at LoadModuleFinish

    def _loader_step(self, tick: int) -> None:
        """The RIM loader under threads=join: one chunk per wake, finished inside ResumeThread."""
        module, prog = self._get("module"), self._get("load_progress")
        vaddr = self._alloc_page()
        seedkey = struct.pack("<IIII", self.seed, module, prog, 0x4C4F4144)
        self._poke(vaddr, hashlib.shake_128(seedkey).digest(PAGE_SIZE))
        prog += 1
        self._set("load_progress", prog)
        if prog < self._get("load_total"):
            return
        self._free_page(ARENA_BASE + 2 * PAGE_SIZE)   # scratch: the first page the load took
        n = 6 + module % 5
        self._st("<I", OBJ_TABLE, n)
        for i in range(n):
            trail = self._alloc_page()
            x = (self._rand(SITE_SPAWN) % WORLD_W) << 8
            y = (self._rand(SITE_SPAWN) % WORLD_H) << 8
            self._put_obj(i, [1, 0 if i == 0 else 1, x, y, 0, 0, 20 if i else 100, trail])
        self._set("phase", PHASE_AREA)
        self._set("flags", self._get("flags") | FLAG_AREA_READY)
        self._tick_events.append(EngineEvent("load.done", tick, str(module)))

    def _pass(self, tick: int, wndproc: list[str]) -> tuple[int, dict[int, int], dict[str, int]]:
        """Tick `tick`: client MainLoop, server MainLoop, present. Returns (subticks, reads, trips)."""
        reads: dict[int, int] = {}
        trips: dict[str, int] = {}
        self._intra = 0
        keys, edges, bedge = self._key_edges()
        phase = self._get("phase")
        subticks = 0
        if self._ld("<I", CEXORAND) and tick % 30 == 0:   # ambient sound only
            self._st("<I", CEXORAND, (self._ld("<I", CEXORAND) * 1103515245 + 12345) & 0xFFFFFFFF)
        if phase == PHASE_MENU:
            fade = self._get("fade_us")
            if fade:
                self._spin_until(SITE_FADE_CLOCK, fade, reads, trips)
                self._set("fade_us", 0)
            self._set("menu_ticks", self._get("menu_ticks") + 1)
            self._st("<I", EMITTER + 0x800, self._get("menu_ticks"))
            if _dik("enter") in edges or bedge & 1:
                self._begin_load(1)
        elif phase == PHASE_LOADING:
            subticks = LOAD_SUBTICKS
            self._loader_step(tick)
        else:
            if self._get("flags") & FLAG_AREA_READY:
                self._set("flags", self._get("flags") & ~FLAG_AREA_READY)
                self._tick_events.append(EngineEvent("area.ready", tick, str(self._get("module"))))
            self._client_area(tick, keys, edges, bedge)
            if self._get("phase") == PHASE_AREA:
                self._server_area(tick, reads, trips)
        return subticks, reads, trips

    def _client_area(self, tick: int, keys: bytes, edges: set[int], bedge: int) -> None:
        if _dik("enter") in edges:
            self._begin_load(self._get("module") + 1)
            return
        if _dik("f5") in edges:
            self._save(tick)
        p = self._obj(0)
        down = lambda k: bool(keys[_dik(k)] & 0x80)  # noqa: E731
        p[4] = (down("d") - down("a")) * 512
        p[5] = (down("s") - down("w")) * 512
        self._put_obj(0, p)
        if bedge & 1:
            mx, my = self._ld("<i", INPUT + 256), self._ld("<i", INPUT + 260)
            best, bi = None, -1
            for i in range(1, self._ld("<I", OBJ_TABLE)):
                o = self._obj(i)
                if o[0]:
                    d = abs((o[2] >> 8) - mx) + abs((o[3] >> 8) - my)
                    if best is None or d < best:
                        best, bi = d, i
            if bi > 0:
                o = self._obj(bi)
                o[6] -= 1 + self._rand(SITE_ATTACK) % 6
                if o[6] <= 0:
                    o[0] = 0
                    self._free_page(o[7])
                    o[7] = 0
                self._put_obj(bi, o)

    def _server_area(self, tick: int, reads: dict[int, int], trips: dict[str, int]) -> None:
        n = self._ld("<I", OBJ_TABLE)
        at = self._get("area_ticks")
        # creature AI under a 10 ms QPC budget; frozen time lets every creature think
        start = self._qpc(SITE_AI_CLOCK, reads, trips)
        cursor = self._get("ai_cursor")
        budget = AI_BUDGET_US * self.qpf_hz // 1_000_000
        for k in range(n - 1):
            if self._qpc(SITE_AI_CLOCK, reads, trips) - start >= budget:
                break
            i = 1 + (cursor + k) % (n - 1)
            o = self._obj(i)
            if o[0]:
                r = self._rand(SITE_THINK)
                if r % 8 == 0:
                    o[4] = (self._rand(SITE_THINK) % 7 - 3) * 64
                    o[5] = (self._rand(SITE_THINK) % 7 - 3) * 64
                    self._put_obj(i, o)
            if self.budget_model == "scripted":
                self._intra += THINK_US
            cursor = (cursor + 1) % max(1, n - 1)
        self._set("ai_cursor", cursor)
        for i in range(n):
            o = self._obj(i)
            if not o[0]:
                continue
            for a, va, lim in ((2, 4, WORLD_W << 8), (3, 5, WORLD_H << 8)):
                o[a] += o[va]
                if o[a] < 0 or o[a] >= lim:
                    o[va] = -o[va]
                    o[a] = max(0, min(lim - 1, o[a]))
            self._put_obj(i, o)
            self._st("<I", o[7] + (at % 1024) * 4, (o[2] >> 8) | (o[3] >> 8) << 16)
        self._st("<I", EMITTER + (at % 256) * 4, self._rand(SITE_EMITTER))
        self._set("area_ticks", at + 1)

    def _save(self, tick: int) -> None:
        """StallEventSaveGame: blocks the main thread and writes the save through stdio.

        The save itself advances zero score ticks (the engine loop is stalled, so no
        MainLoop pass runs), but real wall time does pass. We model that the way the
        real clock() rebase does: bump the virtual-time baseline (seg_v/seg_start) by
        save_ms, so the very next timeGetTime/QPC/GetSystemTimeAsFileTime read after
        this tick sees a large delta -- the same shape as the real save-teleport frame,
        without moving the tick counter. save_ms is self._next_save_ms (a one-shot
        `@save ms=N`) if set, else self.save_ms (header save_ms, else DEFAULT_SAVE_MS).
        """
        self._tick_events.append(EngineEvent("save.begin", tick))
        n = self._get("saves") + 1
        self._set("saves", n)
        body = self._peek(OBJ_TABLE, PAGE_SIZE) + self._peek(CB, 0x100)
        self._files[f"SAVES:/{n:06d} - QuickSave/savegame.sav"] = (body, self._get("v") * 10)
        self._saving = True
        ms = self._next_save_ms if self._next_save_ms is not None else self.save_ms
        self._next_save_ms = None
        if ms:
            self._set("seg_v", self._v_at(tick) + ms * 1000)
            self._set("seg_start", tick)
            self._set("v", self._get("seg_v"))
        self._tick_events.append(EngineEvent("save.done", tick))

    # ------------------------------------------------------------------ gate and capture

    def _emit(self, recs: list[StagingRecord]) -> None:
        if self._ring is None:
            self._spill.extend(recs)
            return
        if not self._ring.fits(recs):
            if self._open_ring:
                # like the shim's gate: spend wall time until the consumer frees space
                deadline = time.monotonic() + 30
                while not self._ring.fits(recs):
                    if time.monotonic() > deadline:
                        raise BackendError("staging ring stayed full for 30 s")
                    time.sleep(0.001)
            else:
                # Python is the consumer here, so drain into the spill queue
                self._spill.extend(self._ring.read())
        try:
            self._ring.write_batch(recs)
        except RingFull:
            raise BackendError("a single tick batch is larger than the staging ring") from None

    def _gate(self, tick: int, subticks: int, reads: dict[int, int], trips: dict[str, int],
              wndproc: list[str]) -> None:
        full, self._next_full = self._next_full, False
        recs: list[StagingRecord] = []
        vaddrs = set(self._pages) | set(self._refs) if full else self._dirty | self._gone
        for v in sorted(vaddrs):
            mc = self.mclass(v)
            if v in self._pages:
                rec = StagingRecord.page(tick, v, bytes(self._pages[v]), mc, PROT_RW, sweep=full)
                self._refs[v] = PageRef(v, rec.hash, mc, PROT_RW)
                recs.append(rec)
            elif v in self._refs:
                recs.append(StagingRecord.decommit(tick, v, mc))
                del self._refs[v]
        self._dirty.clear()
        self._gone.clear()
        loading = self._get("phase") == PHASE_LOADING
        reason = PointReason.FILE_IO if (loading or self._saving) else PointReason.NONE
        entry = {
            "tick": tick, "v": self._get("v"), "server_passes": 1, "subticks": subticks,
            "holdrand": self._ld("<I", HOLDRAND), "rand_draws": dict(sorted(self._draws.items())),
            "cexorand": self._ld("<I", CEXORAND), "budget_trips": dict(sorted(trips.items())),
            "wndproc": wndproc, "input": sha256(self._peek(INPUT, INPUT_SIZE)).hex(),
        }
        jbytes = canonical_json(entry)
        recs.append(StagingRecord.journal(tick, jbytes))
        recs.append(StagingRecord.tick_end(tick, recs, restore_point=reason is PointReason.NONE,
                                           full=full))
        self._emit(recs)
        self._root = state_root(self._refs.values())
        self._last_journal = jbytes
        self._last_reason = reason
        self._last_subticks = subticks
        self._draws = {}
        self._saving = False
        for ev in self._tick_events:
            self._events.append(ev)
            self._report_events.append(ev)
        self._tick_events = []
        self._check_watches(tick)

    def _check_watches(self, tick: int) -> None:
        for wid, w in self._watches.items():
            addr, n, cond, last = w
            try:
                now = self._peek(addr, n)
            except BackendError:
                now = b""
            hit = (cond == "change" and now != last) or \
                  (cond.startswith("eq:") and now == bytes.fromhex(cond[3:]) and last != now) or \
                  (cond.startswith("ne:") and now != bytes.fromhex(cond[3:]) and last != now)
            w[3] = now
            if hit:
                ev = EngineEvent("watch.hit", tick, wid)
                self._events.append(ev)
                self._report_events.append(ev)
                self._watch_hit = True

    def _release_and_run(self, note: Any) -> tuple[int, VerbClass]:
        """Finish gate t (clock, input), run pass t+1, then gate t+1. Returns (t+1, bits)."""
        t = self._get("tick")
        wndproc: list[str] = []
        exiting = False
        extra, self._extra_input = self._extra_input, None
        if extra is not None:
            note = _merge(note, extra)
        if note.osev:
            wndproc, exiting = _os_messages(note.osev)
        if self._get("pending_hz"):
            # new segment at tick t+1, starting from the old rate's V(t+1)
            v1 = self._v_at(t + 1)
            self._set("seg_start", t + 1)
            self._set("seg_v", v1)
            self._set("hz", self._get("pending_hz"))
            self._set("pending_hz", 0)
        self._set("v", self._v_at(t + 1))
        self._stage_input(note)
        t += 1
        self._set("tick", t)
        subticks, reads, trips = self._pass(t, wndproc)
        vc = VerbClass.INPUT | VerbClass.GATE | VerbClass.CLOCK | VerbClass.CAPTURE
        if note.osev:
            vc |= VerbClass.OS_EVENT
        self._gate(t, subticks, reads, trips, wndproc)
        if exiting:
            self._set("flags", self._get("flags") | FLAG_EXITED)
            ev = EngineEvent("proc.down", t, note.osev)
            self._events.append(ev)
            self._report_events.append(ev)
        return t, vc

    def _report(self, vc: VerbClass) -> StepReport:
        vc |= self._pending_vc
        self._pending_vc = VerbClass.NONE
        rp = self._last_reason is PointReason.NONE
        evs, self._report_events = tuple(self._report_events), []
        exited = bool(self._get("flags") & FLAG_EXITED)
        return StepReport(
            tick=self._get("tick"), epoch=self._epoch, server_passes=1, subticks=self._last_subticks,
            verbclass=vc, state_root=self._root, journal_sha256=sha256(self._last_journal),
            restore_point=rp, reason=self._last_reason, events=evs,
            outcome=ProcOutcome.EXITED if exited else ProcOutcome.ALIVE)

    # ------------------------------------------------------------------ running ticks

    def step(self, n: int, note: Any = None) -> StepReport:
        self._allow("step")
        self._alive()
        if n < 1:
            raise BackendError("step needs n >= 1")
        note = note or _EMPTY
        vc = VerbClass.NONE
        self._watch_hit = False
        self._report_events = []
        for i in range(n):
            t, bits = self._release_and_run(note if i == 0 else _held_only(note))
            vc |= bits
            if self._watch_hit or self._get("flags") & FLAG_EXITED:
                break
        return self._report(vc)

    def wait(self, event: str, arg: str | None, timeout_ticks: int, note: Any = None) -> StepReport:
        self._allow("wait")
        self._alive()
        note = note or _EMPTY
        vc = VerbClass.WAIT
        self._watch_hit = False
        self._report_events = []
        for i in range(max(1, timeout_ticks)):
            t, bits = self._release_and_run(note if i == 0 else _held_only(note))
            vc |= bits | VerbClass.WAIT
            if any(e.name == event and (arg is None or e.arg == arg) for e in self._report_events):
                break
            if self._watch_hit or self._get("flags") & FLAG_EXITED:
                break
        if note is _EMPTY:
            vc &= ~VerbClass.INPUT
        return self._report(vc)

    def input(self, note: Any) -> int:
        """Stage extra input merged into the next tick. Returns the tick it applies to."""
        self._allow("input")
        self._alive()
        self._extra_input = note
        self._pending_vc |= VerbClass.INPUT
        return self._get("tick") + 1

    # ------------------------------------------------------------------ capture and restore

    def capture(self, kind: Literal["dirty", "full"]) -> None:
        self._allow("capture")
        if kind not in ("dirty", "full"):
            raise BackendError(f"capture kind must be dirty or full, not {kind!r}")
        self._next_full = kind == "full"
        self._pending_vc |= VerbClass.CAPTURE

    def records(self) -> Iterator[StagingRecord]:
        while self._spill:
            yield self._spill.popleft()
        if self._ring is not None:
            yield from self._ring.read()

    def restore(self, tick: int, root: bytes, pages: Iterable[StagingRecord]) -> Receipt:
        """Write W at the held gate, rebuild caches, verify the full root."""
        self._allow("restore")
        self._alive()
        t0 = time.perf_counter_ns()
        from_tick = self._get("tick")
        W = [r for r in pages if r.kind is RecordKind.PAGE]
        for rec in W:   # validate the whole set before touching memory
            if not self._in_sim(rec.vaddr) or rec.mclass != self.mclass(rec.vaddr):
                raise BackendError(f"restore page {rec.vaddr:#x} is outside the sim set or class")
            if rec.vaddr == CB:
                off, fmt = _CB["epoch"]
                cb = rec.data if rec.flags & RecordFlag.DATA else bytes(PAGE_SIZE)
                if rec.flags & RecordFlag.DECOMMIT or struct.unpack_from(fmt, cb, off)[0] != self._epoch:
                    raise BackendError("restore crosses a process epoch")
        n = len(W)
        for rec in W:
            if rec.flags & RecordFlag.DECOMMIT:
                self._decommit(rec.vaddr)
            elif rec.flags & RecordFlag.ZERO:
                self._pages[rec.vaddr] = bytearray(PAGE_SIZE)
                self._gone.discard(rec.vaddr)
            else:
                self._pages[rec.vaddr] = bytearray(rec.data)
                self._gone.discard(rec.vaddr)
        # Like the shim's write-watch, restore writes stay tracked: _refs remains the last
        # captured map and the next batch carries W, so the record stream folds to the root.
        self._dirty.update(r.vaddr for r in W if not r.flags & RecordFlag.DECOMMIT)
        live = state_root(PageRef(v, page_key(bytes(b)), self.mclass(v), PROT_RW)
                          for v, b in self._pages.items())
        self._root = live
        self._draws = {}
        self._extra_input = None
        for w in self._watches.values():
            try:
                w[3] = self._peek(w[0], w[1])
            except BackendError:
                w[3] = b""
        live_tick = self._get("tick")
        phase = self._get("phase")
        self._last_reason = PointReason.FILE_IO if phase == PHASE_LOADING else PointReason.NONE
        ok = live == root and live_tick == tick
        us = (time.perf_counter_ns() - t0) // 1000
        self._pending_vc |= VerbClass.RESTORE
        return Receipt(kind="seek", result="pass" if ok else "fail", from_tick=from_tick, to_tick=tick,
                       mode=SeekMode.RESTORE, verify_level=VerifyLevel.FULL_ROOT, write_pages=n,
                       micros=us, state_root=live.hex(),
                       first_divergence=None if ok else live_tick, verifier="pysim")

    def read(self, addr: int, length: int) -> bytes:
        self._allow("mem.read")
        if not 0 <= length <= 65536:
            raise BackendError("mem.read length must be 0..65536")
        self._pending_vc |= VerbClass.READ
        return self._peek(addr, length)

    def write(self, addr: int, data: bytes) -> None:
        self._allow("mem.write")
        self._alive()
        self._poke(addr, data)
        self._pending_vc |= VerbClass.WRITE

    _FIELDS = {
        "tick": (CB + 0x04, "<Q"), "v": (CB + 0x0C, "<Q"), "hz": (CB + 0x24, "<I"),
        "phase": (CB + 0x2C, "<I"), "module": (CB + 0x3C, "<I"), "load_progress": (CB + 0x30, "<I"),
        "holdrand": (HOLDRAND, "<I"), "cexorand": (CEXORAND, "<I"), "objects": (OBJ_TABLE, "<I"),
        "player.x": (OBJ_TABLE + 0x20 + 8, "<i"), "player.y": (OBJ_TABLE + 0x20 + 12, "<i"),
        "player.hp": (OBJ_TABLE + 0x20 + 24, "<i"),
    }

    def semantic(self, op: Literal["read", "apply"], fields: Any) -> Mapping[str, Any]:
        if op == "read":
            self._allow("semantic.read")
            self._pending_vc |= VerbClass.READ
            names = list(fields) or list(self._FIELDS)
            unknown = [f for f in names if f not in self._FIELDS]
            if unknown:
                raise BackendError(f"unknown semantic fields {unknown}")
            return {f: self._ld(self._FIELDS[f][1], self._FIELDS[f][0]) for f in names}
        if op == "apply":
            self._allow("semantic.apply")
            self._alive()
            for f, val in dict(fields).items():
                if f not in self._FIELDS:
                    raise BackendError(f"unknown semantic field {f!r}")
                self._st(self._FIELDS[f][1], self._FIELDS[f][0], int(val))
            self._pending_vc |= VerbClass.SEMANTIC
            return {}
        raise BackendError(f"semantic op must be read or apply, not {op!r}")

    # ------------------------------------------------------------------ files overlay

    def files(self, op: Literal["snap", "put"], blob: bytes | None = None) -> bytes | None:
        """snap returns the overlay blob; put replaces the overlay with one.

        Blob: canonical JSON {"entries": [[path, sha256, size, vtime_100ns, data_b64], ...]}.
        Dropping data_b64 gives the section 8.7 FILE_TREE payload.
        """
        self._allow("files")
        if op == "snap":
            ents = [[p, hashlib.sha256(d).hexdigest(), len(d), vt, base64.b64encode(d).decode()]
                    for p, (d, vt) in sorted(self._files.items())]
            return canonical_json({"entries": ents})
        if op == "put":
            if self._started and self._tier is not Tier.LAB:
                raise RefusedError("files put after start is lab only")
            ents = json.loads(blob or b'{"entries":[]}')["entries"]
            files = {}
            for p, h, size, vt, b64 in ents:
                d = base64.b64decode(b64)
                if hashlib.sha256(d).hexdigest() != h or len(d) != size:
                    raise BackendError(f"files blob entry {p!r} does not match its hash")
                files[p] = (d, vt)
            self._files = files
            if self._started:
                self._pending_vc |= VerbClass.FILES_WRITE
            return None
        raise BackendError(f"files op must be snap or put, not {op!r}")

    # ------------------------------------------------------------------ events, watches, os, frame

    def os_event(self, ev: OsEvent) -> ProcOutcome:
        ev = OsEvent(ev)
        if ev is OsEvent.RELAUNCH:
            if not self._started:
                raise BackendError("not started")
            self._epoch += 1
            self._pending_vc |= VerbClass.OS_EVENT
            self._boot(self._get("tick") + 1)
            return ProcOutcome.ALIVE
        self._allow("os_event")
        self._alive()
        msgs, exiting = _os_messages(str(ev))
        self._pending_vc |= VerbClass.OS_EVENT
        if exiting:
            t = self._get("tick")
            self._set("flags", self._get("flags") | FLAG_EXITED)
            self._events.append(EngineEvent("proc.down", t, str(ev)))
            return ProcOutcome.EXITED
        self._extra_input = _merge(self._extra_input or _EMPTY, _osev_note(str(ev)))
        return ProcOutcome.ALIVE

    def watch(self, wid: str, addr: int, length: int, cond: str) -> None:
        self._allow("watch")
        if not (cond == "change" or cond.startswith(("eq:", "ne:"))):
            raise BackendError(f"watch cond must be change, eq:<hex> or ne:<hex>, not {cond!r}")
        if cond != "change":
            try:
                bytes.fromhex(cond[3:])
            except ValueError:
                raise BackendError(f"watch cond {cond!r} has invalid hex") from None
        try:
            now = self._peek(addr, length) if self._started else b""
        except BackendError:
            now = b""
        self._watches[wid] = [addr, length, cond, now]
        self._pending_vc |= VerbClass.READ

    def unwatch(self, wid: str) -> None:
        self._allow("watch.rm")
        self._watches.pop(wid, None)
        self._pending_vc |= VerbClass.READ

    def events(self) -> Iterator[EngineEvent]:
        while self._events:
            yield self._events.popleft()

    def frame(self) -> bytes | None:
        """A 160x120 grayscale PNG drawn from game memory."""
        self._allow("frame")
        self._alive()
        w, h = 160, 120
        img = bytearray(w * h)
        phase = self._get("phase")
        if phase == PHASE_MENU:
            for y in range(h):
                img[y * w:(y + 1) * w] = bytes([y * 2]) * w
        elif phase == PHASE_LOADING:
            done = self._get("load_progress") * w // max(1, self._get("load_total"))
            for y in range(56, 64):
                img[y * w:y * w + done] = b"\xff" * done
        else:
            for i in range(self._ld("<I", OBJ_TABLE)):
                o = self._obj(i)
                if o[0]:
                    x, y = (o[2] >> 8) * w // WORLD_W, (o[3] >> 8) * h // WORLD_H
                    for dy in (0, 1):
                        for dx in (0, 1):
                            if x + dx < w and y + dy < h:
                                img[(y + dy) * w + x + dx] = 255 if i == 0 else 160
        self._pending_vc |= VerbClass.SCREEN
        return _png_gray(w, h, bytes(img))


# ---------------------------------------------------------------- helpers

def _held_only(note: Any) -> Any:
    """Ticks after the first keep held keys, buttons and mouse; wheel and osev fire once."""
    if not (getattr(note, "wheel", 0) or getattr(note, "osev", None)):
        return note
    n = _Note()
    n.held, n.buttons = tuple(note.held), tuple(note.buttons)
    n.mouse_abs, n.mouse_rel = note.mouse_abs, note.mouse_rel
    return n


def _merge(a: Any, b: Any) -> Any:
    n = _Note()
    n.held = tuple(dict.fromkeys((*a.held, *b.held)))
    n.buttons = tuple(dict.fromkeys((*a.buttons, *b.buttons)))
    n.mouse_abs = b.mouse_abs if b.mouse_abs is not None else a.mouse_abs
    n.mouse_rel = b.mouse_rel if b.mouse_rel is not None else a.mouse_rel
    n.wheel = a.wheel + b.wheel
    n.osev = b.osev or a.osev
    return n


def _osev_note(ev: str) -> _Note:
    n = _Note()
    n.osev = ev
    return n


def _os_messages(ev: str) -> tuple[list[str], bool]:
    """Messages the score synthesizes for an OS event, and whether the process exits."""
    return {
        "focus": (["WM_ACTIVATEAPP 1", "WM_SETFOCUS"], False),
        "blur": (["WM_KILLFOCUS", "WM_ACTIVATEAPP 0"], False),
        "altf4": (["WM_SYSKEYDOWN VK_F4", "WM_CLOSE", "WM_DESTROY"], True),
        "close": (["WM_CLOSE", "WM_DESTROY"], True),
    }.get(ev, ([f"UNKNOWN {ev}"], False))


def _png_gray(w: int, h: int, pix: bytes) -> bytes:
    def chunk(tag: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", zlib.crc32(tag + body))
    raw = b"".join(b"\0" + pix[y * w:(y + 1) * w] for y in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


__all__ = ["PySim"]
