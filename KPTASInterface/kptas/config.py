"""telos.toml loader.

Every key is optional. DEFAULTS mirrors the schema in docs/design/contract.md
key for key. load() reads a file with tomllib, rejects unknown keys and bad
types, deep-merges the user's tables over DEFAULTS (lists and scalars replace,
tables merge), resolves every "auto" that depends on the tier, and checks the
cross-key rules. The result is a plain nested dict.
"""

from __future__ import annotations

import copy
import os
import re
import sys
import tomllib
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

from kptas.types import SEEK_ALIASES, ClockMode, SeekMode, Tier

__all__ = ["DEFAULTS", "ConfigError", "load", "loads", "resolve"]


class ConfigError(ValueError):
    """A configuration value is unknown, mistyped or inconsistent."""


DEFAULTS: dict[str, Any] = {
    "run": {
        "tier": "tas",                # lab, which writes memory and engine fields, is opt-in
        "game": "k1",
        "build": "auto",
        "backend": "auto",
        "score": "",
        "out_dir": "~/.local/share/kptas/runs",
        "max_ticks": 0,
    },
    "game": {
        "dir": "",
        "exe": "swkotor.exe",
        "wine": "wine",
        "wineprefix": "",
        "dll_overrides": "auto",
        "display": "auto",
        "launch_wrapper": "none",
        "pinned_env": True,
        "relaunch": True,
    },
    "shim": {
        "dll": "auto",
        "listen": "127.0.0.1",
        "port": 0,
        "connect_timeout_s": 30,
        "session_dir": "auto",
        "ring_mb": 64,
        "module_allowlist": ["system", "wine_builtin", "mss32", "binkw32", "miles_providers",
                             "kptas_shim"],
    },
    "tick": {
        "gate": "winmain_loop_head",
        "context": "auto",
        "entry_trampoline": True,
        "forbid_mapped_gl": True,
        "max_subticks": 100000,
    },
    "clock": {
        "mode": "auto",
        "hz": 60,
        "scale": 100,
        "qpf_hz": 10_000_000,
        "uptime0_ms": 3_600_000,
        "initial_realtime": "2004-02-12T00:00:00Z",
        "hooked_modules": ["exe", "mss32", "binkw32", "miles_providers"],
        "budget_model": "frozen",
        "budget_guard": {"site_reads": 100000, "step_us": 1},
        "spin_escape": "callsite",
        "spin_count": {"after_calls": 4000, "step_us": 1},
    },
    "seek": {
        "mode": "auto",
        "scope": "session",
        "on_incompatible": "hybrid",
        "fallback_chain": ["hybrid", "replay"],
        "keyframe_every": 600,
        "materialize_every": 36000,
    },
    "replay": {
        "origins": ["boot", "save"],
        "present": "skip",
        "require_verify_level": "journal",
    },
    "verify": {
        "compare": "every_tick",
        "stop_on_desync": True,
        "fresh_process": True,
        "rehash_write_set": True,
        "full_sim_threshold_pages": 65536,
        "full_sim_every_ticks": 3600,
        "journal": True,
        "continuation_ticks": "to_next_keyframe",
        "canary_every_seeks": 20,
        "canary_ticks": 120,
        "cross_launch": "auto",
        "mask_registry": "",
        "pointer_scan_every_ticks": 3600,
        "pixel": "report",
        "on_mismatch": "fallback",
    },
    "capture": {
        "enabled": "auto",
        "tracker": "auto",
        "sim_classes": ["game_arena", "image_data", "harness_game"],
        "ambient_fingerprint": "sampled",
        "ambient_fingerprint_every_ticks": 60,
        "max_record_lag_ticks": 120,
    },
    "arena": {
        "base": "auto",
        "size_mb": 1024,
        "chunks": "auto",
        "write_watch": True,
        "redirect_heaps": True,
        "heap_impl": "rtl_create_heap_base",
        "placement": "first_fit",
        "on_exhausted": "error",
    },
    "threads": {
        "model": "auto",
        "slots": 16,
        "order": "fifo_by_vtid",
        "hang_guard_ms": 10000,
        "preload_miles_providers": True,
    },
    "virtualize": {
        "handles": True,
        "tids": True,
        "tls": True,
        "sync": "virtual",
        "timers": "virtual",
        "getprocaddress": "thunk",
        "critical_section_debuginfo": "none",
        "registry": "virtual",
        "external_names": "registry",
    },
    "messages": {
        "real_pump": "gate_drain",
        "wndproc": "interpose",
        "external": "defwindowproc_journal",
        "internal": "pass_journal",
        "synthesize": "score_only",
    },
    "input": {
        "path": "auto",
        "dinput": "virtual_device",
        "buffered_timestamps": "virtual",
        "cursor": "virtual",
        "keystate": "virtual",
        "keymap": "auto",
        "mouse": "abs",
    },
    "portable": {
        "profile": "auto",
        "profile_file": "",
        "math": "reference",
        "x87_cw": 0x027F,
        "mxcsr": 0x1F80,
        "window": "800x600",
        "refresh_hz": 60,
        "total_phys_mb": 4096,
        "avail_phys_mb": 2048,
        "disk_free_mb": 20480,
        "os_version": "6.1.7601",
        "locale": "en-US",
        "timezone": "UTC",
        "computer_name": "KPTAS",
        "user_name": "player",
        "random": "from_score",
    },
    "files": {
        "overlay_dirs": ["SAVES:", "GAMEINPROGRESS:", "CURRENTGAME:", "TEMP:"],
        "overlay_times": "virtual",
        "file_positions": "virtual",
        "store": True,
    },
    "gl": {
        "driver": "native",
        "names": "virtual",
        "retire": "defer",
        "retire_budget_mb": 256,
        "content": "generation",
        "on_content_change": "report",
        "on_context_change": "engine_refresh",
        "shadow_budget_mb": 512,
        "readback": "recorded",
        "query_returns": "journal",
    },
    "audio": {
        "miles": "shim",
        "status": "virtual",
        "callbacks": "gate_dispatch",
        "output": "native",
        "bink": "virtual_time",
    },
    "rng": {
        "draw_journal": "callsite",
        "cexorand": "observe",
    },
    "journal": {
        "semantic": True,
        "objects": "all_in_area",
        "per_server_pass": True,
        "per_subtick": True,
        "wndproc_log": True,
        "gl_returns": True,
        "audio_positions": True,
    },
    "store": {
        "codec": "zlib",
        "level": 1,
        "pages": "every_tick",
        "xor_delta": True,
        "max_delta_chain": 32,
        "fsync": "keyframe",
        "hot_ring_mb": 1024,
        "ram_budget_mb": 2048,
        "budget_gb": 18,
        "spill_paths": [],
        "on_budget_steps": ["spill", "evict_detached", "halt"],
        "detached_payload": "evictable",
    },
    "score": {
        "include_dirs": ["lib"],
        "default_timeout_ticks": 1800,
    },
    "env": {
        "guard": "refuse",
        "pin": ["exe", "text", "chitin.key", "dialog.tlk", "override", "swkotor.ini", "keymap",
                "render", "wine", "prefix", "dll_overrides", "gl_driver", "modules", "shim",
                "profile"],
        "extra_hashed": [],
    },
    "determinism": {
        "strict_key": True,
    },
    "phasor": {
        "port": 0,
    },
    "handsoff": {
        "input_tool": "xdotool",
        "capture": "x11grab",
        "capture_fps": 60,
    },
    "sim": {
        "seed": 0,
        "pages": 256,
        "simgame_exe": "auto",
    },
    "hud": {
        "enabled": False,
        "watermark": True,
    },
    "gym": {
        "obs": "fields",
        "obs_fields": [],
        "max_ticks": 0,
    },
}

#: Built-in profile id chosen by portable.profile = "auto" above hands-off.
DEFAULT_PROFILE = "baseline-1"

_S = frozenset

#: String keys with a closed set of values.
ENUMS: dict[str, frozenset[str]] = {
    "run.tier": _S({"lab", "tas", "hands-off"}),
    "run.game": _S({"k1", "k2"}),
    "run.backend": _S({"auto", "pysim", "simgame", "shim", "handsoff", "phasor"}),
    "game.launch_wrapper": _S({"none", "setarch_R"}),
    "tick.gate": _S({"winmain_loop_head"}),
    "tick.context": _S({"auto", "inline", "fiber"}),
    "clock.mode": _S({"auto", "virtual", "fixed", "wall"}),
    "clock.budget_model": _S({"frozen", "scripted"}),
    "clock.spin_escape": _S({"callsite", "count"}),
    "seek.scope": _S({"session", "rehydrate"}),
    "seek.on_incompatible": _S({"hybrid", "replay", "error"}),
    "replay.present": _S({"skip", "draw"}),
    "replay.require_verify_level": _S({"full_root", "sim_root", "journal"}),
    "verify.compare": _S({"every_tick", "syncs", "end"}),
    "verify.cross_launch": _S({"auto", "sim_root", "journal"}),
    "verify.pixel": _S({"report", "require"}),
    "verify.on_mismatch": _S({"fallback", "halt"}),
    "capture.tracker": _S({"auto", "write_watch", "full_hash"}),
    "capture.ambient_fingerprint": _S({"off", "sampled", "every_tick"}),
    "arena.heap_impl": _S({"rtl_create_heap_base", "kptas_heap"}),
    "arena.placement": _S({"first_fit"}),
    "arena.on_exhausted": _S({"error"}),
    "threads.model": _S({"auto", "slots", "join", "free"}),
    "threads.order": _S({"fifo_by_vtid"}),
    "virtualize.sync": _S({"virtual"}),
    "virtualize.timers": _S({"virtual"}),
    "virtualize.getprocaddress": _S({"thunk"}),
    "virtualize.critical_section_debuginfo": _S({"none"}),
    "virtualize.registry": _S({"virtual", "epoch_break", "error"}),
    "virtualize.external_names": _S({"registry"}),
    "messages.real_pump": _S({"gate_drain"}),
    "messages.wndproc": _S({"interpose"}),
    "messages.external": _S({"defwindowproc_journal"}),
    "messages.internal": _S({"pass_journal"}),
    "messages.synthesize": _S({"score_only"}),
    "input.path": _S({"auto", "dinput", "os"}),
    "input.dinput": _S({"virtual_device", "passthrough"}),
    "input.buffered_timestamps": _S({"virtual"}),
    "input.cursor": _S({"virtual"}),
    "input.keystate": _S({"virtual"}),
    "input.mouse": _S({"abs", "rel"}),
    "portable.math": _S({"native", "reference"}),
    "portable.random": _S({"from_score"}),
    "files.overlay_times": _S({"virtual"}),
    "files.file_positions": _S({"virtual"}),
    "gl.driver": _S({"native", "llvmpipe"}),
    "gl.names": _S({"virtual"}),
    "gl.retire": _S({"defer"}),
    "gl.content": _S({"generation", "shadow"}),
    "gl.on_content_change": _S({"report", "shadow", "engine_refresh"}),
    "gl.on_context_change": _S({"report", "engine_refresh"}),
    "gl.readback": _S({"recorded"}),
    "gl.query_returns": _S({"journal", "recorded"}),
    "audio.miles": _S({"shim", "lockstep"}),
    "audio.status": _S({"virtual"}),
    "audio.callbacks": _S({"gate_dispatch"}),
    "audio.output": _S({"native", "pipe", "null"}),
    "audio.bink": _S({"virtual_time"}),
    "rng.draw_journal": _S({"callsite", "off"}),
    "rng.cexorand": _S({"observe", "pin"}),
    "journal.objects": _S({"all_in_area", "party", "none"}),
    "store.codec": _S({"zlib", "zstd", "none"}),
    "store.pages": _S({"every_tick", "keyframes", "digests"}),
    "store.fsync": _S({"never", "keyframe", "tick"}),
    "store.detached_payload": _S({"evictable", "keep"}),
    "env.guard": _S({"refuse", "warn"}),
    "handsoff.input_tool": _S({"xdotool"}),
    "handsoff.capture": _S({"x11grab"}),
    "gym.obs": _S({"fields", "digest", "pixels"}),
}

#: String-list keys whose elements come from a closed set.
LIST_ENUMS: dict[str, frozenset[str]] = {
    "clock.hooked_modules": _S({"exe", "mss32", "binkw32", "miles_providers"}),
    "seek.fallback_chain": _S({"hybrid", "replay"}),
    "replay.origins": _S({"boot", "save"}),
    "capture.sim_classes": _S({"game_arena", "image_data", "harness_game"}),
    "store.on_budget_steps": _S({"spill", "evict_detached", "thin", "halt"}),
    "env.pin": _S({"exe", "text", "chitin.key", "dialog.tlk", "override", "swkotor.ini", "keymap",
                   "render", "wine", "prefix", "dll_overrides", "gl_driver", "modules", "shim",
                   "profile"}),
}

#: Integer ranges, inclusive. Integers not listed must be >= 0.
RANGES: dict[str, tuple[int, int]] = {
    "shim.port": (0, 65535),
    "phasor.port": (0, 65535),
    "shim.ring_mb": (1, 512),
    "tick.max_subticks": (1, 1 << 31),
    "clock.hz": (1, 1000),
    "clock.scale": (1, 10000),
    "clock.qpf_hz": (1_000_000, 1 << 40),
    "seek.keyframe_every": (1, 1 << 31),
    "seek.materialize_every": (1, 1 << 31),
    "threads.slots": (1, 64),
    "portable.x87_cw": (0, 0xFFFF),
    "portable.mxcsr": (0, 0xFFFFFFFF),
    "portable.refresh_hz": (1, 1000),
    "store.level": (-7, 22),
    "store.max_delta_chain": (1, 1 << 16),
    "sim.pages": (1, 1 << 20),
}


def _v_continuation(key: str, v: Any) -> None:
    if not (v == "to_next_keyframe" or (type(v) is int and v >= 0)):
        raise ConfigError(f"{key}: expected \"to_next_keyframe\" or an integer >= 0, got {v!r}")


def _v_capture_enabled(key: str, v: Any) -> None:
    if not (v == "auto" or type(v) is bool):
        raise ConfigError(f"{key}: expected \"auto\", true or false, got {v!r}")


def _v_arena_base(key: str, v: Any) -> None:
    if v == "auto":
        return
    if type(v) is not int or not (0x10000 <= v < 0x80000000) or v % 0x10000:
        raise ConfigError(f"{key}: expected \"auto\" or a 64 KiB-aligned address below 0x80000000, got {v!r}")


def _v_arena_chunks(key: str, v: Any) -> None:
    if v == "auto":
        return
    if not isinstance(v, list) or not v:
        raise ConfigError(f"{key}: expected \"auto\" or a non-empty list of addresses")
    for a in v:
        _v_arena_base(key, a)


def _v_window(key: str, v: Any) -> None:
    m = re.fullmatch(r"([1-9][0-9]{1,4})x([1-9][0-9]{1,4})", v) if isinstance(v, str) else None
    if not m:
        raise ConfigError(f"{key}: expected \"<width>x<height>\", got {v!r}")


def _v_realtime(key: str, v: Any) -> None:
    try:
        if not isinstance(v, str) or not v.endswith("Z"):
            raise ValueError
        datetime.fromisoformat(v)
    except ValueError:
        raise ConfigError(f"{key}: expected an ISO-8601 UTC time like \"2004-02-12T00:00:00Z\", "
                          f"got {v!r}") from None


def _v_seek_mode(key: str, v: Any) -> None:
    if v != "auto" and v not in SEEK_ALIASES and v not in {m.value for m in SeekMode}:
        choices = ", ".join(["auto", *[m.value for m in SeekMode], *sorted(SEEK_ALIASES)])
        raise ConfigError(f"{key}: unknown value {v!r}; expected one of {choices}")


SPECIAL: dict[str, Callable[[str, Any], None]] = {
    "verify.continuation_ticks": _v_continuation,
    "capture.enabled": _v_capture_enabled,
    "arena.base": _v_arena_base,
    "arena.chunks": _v_arena_chunks,
    "portable.window": _v_window,
    "clock.initial_realtime": _v_realtime,
    "seek.mode": _v_seek_mode,
}


def _typename(v: Any) -> str:
    return {bool: "boolean", int: "integer", str: "string", list: "array", dict: "table"}.get(
        type(v), type(v).__name__)


def _check_value(key: str, default: Any, v: Any) -> None:
    if key in SPECIAL:
        SPECIAL[key](key, v)
        return
    if type(default) is bool:
        ok = type(v) is bool
    elif type(default) is int:
        ok = type(v) is int
    elif type(default) is str:
        ok = type(v) is str
    elif type(default) is list:
        ok = type(v) is list and all(type(x) is str for x in v)
    else:  # pragma: no cover - DEFAULTS holds only the types above
        ok = False
    if not ok:
        want = "array of strings" if type(default) is list else _typename(default)
        raise ConfigError(f"{key}: expected {want}, got {_typename(v)} {v!r}")
    if key in ENUMS and v not in ENUMS[key]:
        raise ConfigError(f"{key}: unknown value {v!r}; expected one of {', '.join(sorted(ENUMS[key]))}")
    if key in LIST_ENUMS:
        bad = [x for x in v if x not in LIST_ENUMS[key]]
        if bad:
            raise ConfigError(f"{key}: unknown entries {bad}; allowed: {', '.join(sorted(LIST_ENUMS[key]))}")
        if len(set(v)) != len(v):
            raise ConfigError(f"{key}: duplicate entries in {v!r}")
    if type(v) is int:
        lo, hi = RANGES.get(key, (0, 1 << 63))
        if not lo <= v <= hi:
            raise ConfigError(f"{key}: {v} is outside {lo}..{hi}")


def _merge_checked(base: dict[str, Any], user: Mapping[str, Any], prefix: str) -> None:
    for k, v in user.items():
        key = f"{prefix}.{k}" if prefix else k
        if k not in base:
            where = f"[{prefix}]" if prefix else "the top level"
            raise ConfigError(f"{key}: unknown key in {where}")
        d = base[k]
        if type(d) is dict:
            if not isinstance(v, Mapping):
                raise ConfigError(f"{key}: expected a table, got {_typename(v)} {v!r}")
            _merge_checked(d, v, key)
        else:
            _check_value(key, d, v)
            base[k] = copy.deepcopy(v)


def _fail(msg: str) -> None:
    raise ConfigError(msg)


def _derive(cfg: dict[str, Any]) -> None:
    """Resolve tier-derived "auto" values, then enforce cross-key rules."""
    run, clock, seek, inp = cfg["run"], cfg["clock"], cfg["seek"], cfg["input"]
    tier = Tier.parse(run["tier"])
    hands_off = tier is Tier.HANDS_OFF

    if run["backend"] == "auto":
        run["backend"] = "handsoff" if hands_off else ("shim" if cfg["game"]["dir"] else "pysim")
    phasor = run["backend"] == "phasor"     # observes a running game: no gate, no restore
    if seek["mode"] == "auto":
        seek["mode"] = "restore" if tier is Tier.LAB and not phasor else "replay"
    seek["mode"] = SeekMode.parse(seek["mode"]).value
    if clock["mode"] == "auto":
        clock["mode"] = "wall" if hands_off or phasor else "virtual"
    if inp["path"] == "auto":
        inp["path"] = "os" if hands_off else "dinput"
    if cfg["game"]["dll_overrides"] == "auto":
        cfg["game"]["dll_overrides"] = "dinput8=n,b" if run["backend"] == "shim" else ""
    if cfg["portable"]["profile"] == "auto":
        cfg["portable"]["profile"] = "native" if hands_off else DEFAULT_PROFILE

    backend = run["backend"]
    mode = SeekMode(seek["mode"])
    cmode = ClockMode(clock["mode"])
    t = tier.label

    if hands_off:
        if backend != "handsoff":
            _fail(f"run.backend: hands-off loads no in-process code, so the backend must be "
                  f"\"handsoff\", not {backend!r}")
        if inp["path"] != "os":
            _fail("input.path: hands-off drives OS-level input only; use \"os\"")
        if re.search(r"(^|[;,\s])dinput8\s*=\s*n", cfg["game"]["dll_overrides"]):
            _fail("game.dll_overrides: hands-off must load Wine's builtin dinput8; remove the "
                  "dinput8=n override")
        if cfg["portable"]["profile"] != "native":
            _fail("portable.profile: hands-off cannot present a portable boundary; use \"native\"")
        if not cfg["hud"]["watermark"]:
            _fail("hud.watermark: the watermark is always on above lab")
    elif backend == "handsoff":
        _fail(f"run.backend: \"handsoff\" supports only tier hands-off, not {t}")
    if phasor:
        if tier is not Tier.LAB:
            _fail(f"run.backend: \"phasor\" tops out at lab, because KotorPhasor cannot gate ticks and "
                  f"sits outside the tas module allowlist; set run.tier = \"lab\" (it is {t})")
        if mode is not SeekMode.REPLAY:
            _fail(f"seek.mode: the phasor backend cannot capture or restore, so it seeks by replay only, "
                  f"not {mode.value}")
        if cmode is not ClockMode.WALL:
            _fail(f"clock.mode: {cmode.value} is not available with the phasor backend; KotorPhasor "
                  "runs on the wall clock")

    if not mode.allowed(tier):
        _fail(f"seek.mode: {mode.value} is not allowed in tier {t}"
              + ("; hands-off seeks by replay only" if hands_off else "; semantic is lab only"))
    if not cmode.allowed(tier):
        why = {Tier.HANDS_OFF: "hands-off runs on the wall clock",
               Tier.TAS: "tas requires the virtual clock"}.get(tier, "")
        _fail(f"clock.mode: {cmode.value} is not allowed in tier {t}"
              + (f"; {why}" if why else ""))
    if clock["scale"] != 100 and cmode is not ClockMode.FIXED:
        _fail("clock.scale: a scale other than 100 needs clock.mode = \"fixed\" (lab only)")
    if cmode is ClockMode.FIXED and clock["scale"] * 1_000_000 >= clock["hz"] * 2**31:
        _fail(f"clock.scale: scale*1e6/hz must stay below 2^31 (scale={clock['scale']}, "
              f"hz={clock['hz']})")

    if tier is Tier.TAS:
        if inp["path"] != "dinput":
            _fail("input.path: tas stages input through the virtual DirectInput device; use \"dinput\"")
    if tier is not Tier.LAB:
        for key, val, lab_val in (("input.dinput", inp["dinput"], "passthrough"),
                                  ("rng.cexorand", cfg["rng"]["cexorand"], "pin")):
            if val == lab_val:
                _fail(f"{key}: {lab_val!r} writes or bypasses game state and is lab only")
        if not cfg["hud"]["watermark"]:
            _fail("hud.watermark: the watermark is always on above lab")

    if mode in (SeekMode.RESTORE, SeekMode.HYBRID):
        if cfg["threads"]["model"] == "free":
            _fail(f"seek.mode: {mode.value} needs deterministic threads; threads.model \"free\" "
                  "allows replay only")
        if cfg["tick"]["context"] == "inline":
            _fail(f"seek.mode: {mode.value} needs tick.context \"fiber\" or \"auto\", not \"inline\"")
        if cfg["store"]["pages"] == "digests":
            _fail(f"seek.mode: {mode.value} needs stored pages; store.pages \"digests\" allows "
                  "replay only")

    store = cfg["store"]
    if store["codec"] == "zstd" and sys.version_info < (3, 14):
        _fail("store.codec: zstd needs Python 3.14 or newer (compression.zstd); this is "
              f"Python {sys.version_info.major}.{sys.version_info.minor}. Use \"zlib\" or \"none\".")
    if store["codec"] == "zlib" and not 0 <= store["level"] <= 9:
        _fail(f"store.level: zlib levels are 0..9, got {store['level']}")
    if not cfg["seek"]["fallback_chain"]:
        _fail("seek.fallback_chain: must name at least one level")


def resolve(user: Mapping[str, Any], source: str = "<config>") -> dict[str, Any]:
    """Merge a parsed user mapping over DEFAULTS, validate, and resolve derived values."""
    cfg = copy.deepcopy(DEFAULTS)
    try:
        _merge_checked(cfg, user, "")
        _derive(cfg)
    except ConfigError as e:
        raise ConfigError(f"{source}: {e}") from None
    return cfg


def loads(text: str, source: str = "<string>") -> dict[str, Any]:
    try:
        user = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{source}: {e}") from None
    return resolve(user, source)


def load(path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """Load a telos.toml. None gives the defaults; a missing file is an error."""
    if path is None:
        return resolve({}, "<defaults>")
    p = os.fspath(path)
    try:
        with open(p, "rb") as f:
            user = tomllib.load(f)
    except FileNotFoundError:
        raise ConfigError(f"{p}: no such file") from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{p}: {e}") from None
    return resolve(user, p)
