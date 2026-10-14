"""Environment pin (contract 3, 7, 8.3 ENVP, C13): hash what a run depends on into an EnvPin.

Key inputs are the game-side facts: binaries, data files, modules, clock origin,
portable profile, thread model and mask registry. The portable profile is the
machine the shim serves (KPTAS_BOUNDARY), so it stands in for the host's CPU,
time zone, user, locale and install path. The host fingerprint (Wine, prefix,
real GL driver, overrides, launch wrapper) is recorded but joins the
determinism key only under profile "native", where the game sees the host
itself; only then are those host facts recorded too. Nothing time-varying is hashed, so the same configuration
always gives the same key. A matching key is a compatibility claim to verify against
actual state roots; it is not evidence that every host dependency has been virtualized.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import struct
import subprocess
import threading
import time
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from kptas.tier.qualification import canonical_qualification
from kptas.config import ConfigError
from kptas.types import EnvPin, FormatError, PortableProfile

PKG_ROOT = Path(__file__).resolve().parent.parent.parent       # KPTASInterface
PYSIM_EXE = hashlib.sha256(b"pysim/1").hexdigest()             # what PySim's hello reports
# Census-confirmed overrides only. baseline-1's GL identity is the set of extensions the K1 exe checks
# (GLRender::InitExtensions, InitializeAntiAliasing) that Mesa llvmpipe supports, plus the WGL names
# WGL_ARB_render_texture needs; the shim's built-in boundary (native/shim/src/bound.c) holds the same values.
BUILTIN_PROFILES: dict[str, dict[str, Any]] = {"baseline-1": {
    "gl_vendor": "KPTAS", "gl_renderer": "KPTAS baseline-1", "gl_version": "1.5.0",
    "gl_extensions": (
        "GL_ARB_depth_texture", "GL_ARB_multitexture", "GL_ARB_texture_compression", "GL_ARB_vertex_buffer_object",
        "GL_ARB_vertex_program", "GL_ATI_fragment_shader", "GL_EXT_bgra", "GL_EXT_compiled_vertex_array",
        "GL_EXT_fog_coord", "GL_EXT_secondary_color", "GL_EXT_stencil_two_side", "GL_EXT_texture_compression_s3tc",
        "GL_EXT_texture_cube_map", "GL_EXT_texture_env_combine", "GL_EXT_texture_filter_anisotropic",
        "GL_SGIS_generate_mipmap", "WGL_ARB_extensions_string", "WGL_ARB_pbuffer", "WGL_ARB_pixel_format",
        "WGL_ARB_render_texture", "WGL_EXT_extensions_string", "WGL_EXT_swap_control"),
    # the GL limits the exe stores (AurGetAvailableStencilBits, InitExtensions, AurOnePassEnvMapAvailable and
    # AurPixelShadersAvailable); no values = unsupported, as on a driver without NV_register_combiners
    "gl_queries": {"GL_STENCIL_BITS": (8,), "GL_MAX_TEXTURE_MAX_ANISOTROPY_EXT": (16,),
                   "GL_MAX_GENERAL_COMBINERS_NV": ()},
}}
_PROFILE_KEYS = ("math", "x87_cw", "mxcsr", "refresh_hz", "total_phys_mb", "avail_phys_mb", "disk_free_mb",
                 "os_version", "locale", "timezone", "computer_name", "user_name", "random",
                 "cpu_count", "cpu_level", "cpu_revision", "install_dir")
#: locale -> (LCID, ANSI code page, OEM code page), the locales the shim can serve
LOCALES = {"en-US": (0x409, 1252, 437)}
#: GL enums a profile's gl_queries may name (the ones the K1 exe queries into its own memory)
GL_ENUMS = {"GL_STENCIL_BITS": 0x0D57, "GL_MAX_TEXTURE_MAX_ANISOTROPY_EXT": 0x84FF,
            "GL_MAX_GENERAL_COMBINERS_NV": 0x854D, "GL_MAX_TEXTURE_UNITS_ARB": 0x84E2,
            "GL_MAX_TEXTURE_SIZE": 0x0D33}
_CLOCK_KEYS = ("mode", "hz", "scale", "qpf_hz", "uptime0_ms", "initial_realtime", "hooked_modules",
               "budget_model", "budget_guard", "spin_escape", "spin_count")
_MODULE_GLOBS = ("mss32.dll", "binkw32.dll", "binkw32_real.dll", "miles/*.asi", "miles/*.m3d",
                 "miles/*.flt", "miles/*.mix", "*.asi", "dinput8.dll")


# Process-level digest cache. A file is rehashed whenever any of (st_dev, st_ino, st_size, st_mtime_ns,
# st_ctime_ns) differs from the stat taken on the same open descriptor before hashing; a stat that changes
# while hashing is not cached. Stamps newer than _RACY_NS are never cached either, so a coarse-timestamp
# filesystem cannot hide a same-size rewrite inside one tick. Same fingerprint as rig.StageFiles._regular.
_RACY_NS = 3_000_000_000
_DIGEST_LOCK = threading.Lock()
_DIGESTS: dict[tuple[str, str], tuple[tuple[int, ...], str]] = {}
_STAGE_DIGESTS: dict[tuple[str, str], tuple[tuple[int, ...], str]] = {}   # (stage root, rel) -> digest only


def _stat_key(st: os.stat_result) -> tuple[int, ...]:
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def _settled(st: os.stat_result) -> bool:
    return time.time_ns() - max(st.st_mtime_ns, st.st_ctime_ns) >= _RACY_NS


def _cached_digest(path: Path, kind: str, compute: Any) -> str:
    """compute(open binary stream) -> digest, reused while the file's stat fingerprint is unchanged."""
    try:
        with open(path, "rb") as f:
            st = os.fstat(f.fileno())
            if not stat.S_ISREG(st.st_mode):
                return compute(f)
            key, slot = _stat_key(st), (kind, os.path.abspath(path))
            with _DIGEST_LOCK:
                hit = _DIGESTS.get(slot)
            if hit is not None and hit[0] == key:
                return hit[1]
            digest = compute(f)
            with _DIGEST_LOCK:
                if _stat_key(os.fstat(f.fileno())) == key and _settled(st):
                    _DIGESTS[slot] = (key, digest)
                else:
                    _DIGESTS.pop(slot, None)
            return digest
    except OSError:
        return ""


def _sha256_stream(f: Any) -> str:
    h = hashlib.sha256()
    for block in iter(lambda: f.read(1 << 20), b""):
        h.update(block)
    return h.hexdigest()


def file_sha256(path: Path) -> str:
    """sha256 hex of a file, or "" when it is missing or unreadable. Cached by stat fingerprint."""
    return _cached_digest(path, "sha256", _sha256_stream)


def tree_sha256(root: Path) -> str:
    """sha256 over sorted (relative path, content sha256) of every file under root; "" when absent."""
    if not root.is_dir():
        return ""
    rows = sorted([str(p.relative_to(root)).replace(os.sep, "/"), file_sha256(p)]
                  for p in root.rglob("*") if p.is_file())
    return hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode()).hexdigest()


def _text_digest(f: Any) -> str:
    try:
        data = f.read()
        pe = struct.unpack_from("<I", data, 0x3C)[0]
        if data[pe:pe + 4] != b"PE\0\0":
            return ""
        nsec, optsize = struct.unpack_from("<H", data, pe + 6)[0], struct.unpack_from("<H", data, pe + 20)[0]
        sec = pe + 24 + optsize
        for i in range(nsec):
            name, _vs, _va, size, ptr = struct.unpack_from("<8sIIII", data, sec + 40 * i)
            if name.rstrip(b"\0") == b".text":
                return hashlib.sha256(data[ptr:ptr + size]).hexdigest()
    except struct.error:
        pass
    return ""


def text_sha256(exe: Path) -> str:
    """sha256 of the PE .text section's raw bytes; "" when the file is missing or not PE. Cached like file_sha256."""
    return _cached_digest(exe, "pe-text", _text_digest)


def ini_section_sha256(ini: Path | bytes | None, *sections: str) -> str:
    """sha256 of the named INI sections' non-blank lines (keymap, render stamp); "" when none.
    ini is a path or the file's bytes."""
    if ini is None:
        return ""
    try:
        raw = ini if isinstance(ini, bytes) else ini.read_bytes()
    except OSError:
        return ""
    lines = raw.decode("latin-1").splitlines()
    want, keep, cur = {s.lower() for s in sections}, [], None
    for ln in lines:
        s = ln.strip()
        if s.startswith("[") and s.endswith("]"):
            cur = s[1:-1].strip().lower()
        elif s and cur in want:
            keep.append(f"{cur}:{s}")
    return hashlib.sha256("\n".join(keep).encode()).hexdigest() if keep else ""


def profile_obj(cfg: Mapping[str, Any]) -> dict[str, Any]:
    """PortableProfile values from [portable], the built-in table and profile_file (D12, section 7)."""
    p = cfg["portable"]
    pid = p["profile"]
    base = {"id": pid, **{k: p[k] for k in _PROFILE_KEYS}}
    w, h = (int(x) for x in str(p["window"]).lower().split("x"))
    base["window"] = [w, h]
    if pid == "native":   # no boundary: only the determinism-layer choices apply
        return {k: base[k] for k in ("id", "math", "x87_cw", "mxcsr")}
    if p["profile_file"]:
        path = Path(os.path.expanduser(p["profile_file"]))
        try:
            raw = path.read_bytes()
            extra = tomllib.loads(raw.decode()) if path.suffix == ".toml" else json.loads(raw)
        except (OSError, ValueError) as e:
            raise ConfigError(f"portable.profile_file: {path}: {e}") from None
    elif pid in BUILTIN_PROFILES:
        extra = BUILTIN_PROFILES[pid]
    else:
        raise ConfigError(f"portable.profile: no built-in profile {pid!r}; set portable.profile_file")
    try:
        return PortableProfile.from_obj({**base, **extra, "id": pid}).to_obj()
    except (FormatError, TypeError) as e:
        raise ConfigError(f"portable profile {pid!r}: {e}") from None


def boundary_env(cfg: Mapping[str, Any]) -> str:
    """KPTAS_BOUNDARY: the portable profile as the shim serves it (native/shim/src/bound.c), or "native"."""
    p = profile_obj(cfg)
    if p["id"] == "native":
        return "native"
    if p["locale"] not in LOCALES:
        raise ConfigError(f"portable.locale: the shim serves {sorted(LOCALES)}, not {p['locale']!r}")
    if p["timezone"] != "UTC":
        raise ConfigError(f"portable.timezone: the shim serves \"UTC\" only, not {p['timezone']!r}")
    lcid, acp, oem = LOCALES[p["locale"]]
    parts = {"os": p["os_version"], "lcid": hex(lcid), "acp": acp, "oemcp": oem, "tz": p["timezone"],
             "computer": p["computer_name"], "user": p["user_name"], "cpus": p["cpu_count"],
             "cpu_level": p["cpu_level"], "cpu_rev": f"{p['cpu_revision']:#06x}", "install": p["install_dir"],
             "gl_vendor": p["gl_vendor"], "gl_renderer": p["gl_renderer"], "gl_version": p["gl_version"],
             "gl_ext": " ".join(p["gl_extensions"]),
             "window_w": p["window"][0], "window_h": p["window"][1],
             "refresh_hz": p["refresh_hz"], "modes": ",".join(p["display_modes"]),
             "total_phys_mb": p["total_phys_mb"], "avail_phys_mb": p["avail_phys_mb"],
             "disk_free_mb": p["disk_free_mb"]}
    import re
    if any(type(v) is not int or not 1 <= v <= 65535 for v in p["window"]):
        raise ConfigError("portable window: each dimension must be 1..65535")
    if type(p["refresh_hz"]) is not int or not 1 <= p["refresh_hz"] <= 1000:
        raise ConfigError("portable refresh_hz must be 1..1000")
    if not p["display_modes"] or len(p["display_modes"]) > 16 or any(
            not re.fullmatch(r"[1-9][0-9]{0,4}x[1-9][0-9]{0,4}@[1-9][0-9]{0,3}", m)
            for m in p["display_modes"]):
        raise ConfigError("portable display_modes: expected 1..16 WIDTHxHEIGHT@HZ modes")
    for name in ("total_phys_mb", "avail_phys_mb", "disk_free_mb"):
        if type(p[name]) is not int or not 0 <= p[name] <= 0xffffffff:
            raise ConfigError(f"portable {name}: expected an unsigned 32-bit MiB count")
    if p["avail_phys_mb"] > p["total_phys_mb"]:
        raise ConfigError("portable avail_phys_mb exceeds total_phys_mb")
    if not re.fullmatch(r"[0-9]{1,5}\.[0-9]{1,5}\.[0-9]{1,10}", p["os_version"]):
        raise ConfigError("portable os_version must be MAJOR.MINOR.BUILD")
    if any(int(v) > 0xffffffff for v in p["os_version"].split(".")):
        raise ConfigError("portable os_version fields must fit unsigned 32 bits")
    for prefix, capacity in (("GL_", 2048), ("WGL_", 1024)):
        if sum(len(x) + 1 for x in p["gl_extensions"] if x.startswith(prefix)) >= capacity:
            raise ConfigError(f"portable {prefix} extensions exceed the shim buffer")
    if len(p["gl_queries"]) > 16:
        raise ConfigError("portable gl_queries: the shim serves at most 16 entries")
    q = []
    for name, vals in p["gl_queries"].items():
        if (name not in GL_ENUMS or len(vals) > 4 or
                any(type(v) is not int or not 0 <= v <= 0x7fffffff for v in vals)):
            raise ConfigError(f"portable profile {p['id']!r}: gl_queries {name!r} cannot be served")
        q.append(f"{GL_ENUMS[name]:#06x}:" + "/".join(str(int(v)) for v in vals))
    q.sort()
    parts["gl_q"] = ",".join(q)
    capacities = {"computer": 32, "user": 32, "install": 128, "gl_vendor": 64,
                  "gl_renderer": 96, "gl_version": 64, "gl_ext": 2048}
    for k, v in parts.items():
        if k in capacities and len(str(v)) + (1 if k == "gl_ext" else 0) >= capacities[k]:
            raise ConfigError(f"portable profile {p['id']!r}: {k} exceeds the shim buffer")
        if ";" in str(v) or not str(v).isascii() or not str(v).isprintable():
            raise ConfigError(f"portable profile {p['id']!r}: {k} {v!r} cannot be served")
    encoded = ";".join(f"{k}={v}" for k, v in parts.items())
    if len(encoded) >= 4096:
        raise ConfigError("portable profile exceeds the shim boundary buffer")
    return encoded


def host_facts(cfg: Mapping[str, Any]) -> dict[str, str]:
    """What the game sees of this host when no boundary hides it (profile "native"): CPU, time zone, user,
    locale, install path. Behind a portable profile the shim serves profile values instead, so pin_env
    records these only under "native" and a published bundle does not carry the user name or the path."""
    g = cfg["game"]
    cpu = ""
    try:
        with open("/proc/cpuinfo", encoding="utf-8", errors="replace") as f:
            names = [ln.split(":", 1)[1].strip() for ln in f if ln.startswith("model name")]
        cpu = f"{len(names)}x {names[0]}" if names else ""
    except OSError:
        pass
    tz = os.environ.get("TZ", "")
    if not tz:   # the zone file's link target, or its content when it is a plain file
        lt = Path("/etc/localtime")
        tz = os.readlink(lt) if lt.is_symlink() else f"localtime@{file_sha256(lt)[:16]}"
    env = os.environ if not g["pinned_env"] else {"LANG": "C"}
    return {"cpu": cpu or f"{os.cpu_count()}x", "tz": tz, "user": os.environ.get("USER", ""),
            "locale": env.get("LC_ALL") or env.get("LANG", ""),
            "install_dir": str(Path(os.path.expanduser(g["dir"])).resolve()) if g["dir"] else ""}


def _wine_version(wine: str) -> str:
    try:
        r = subprocess.run([wine, "--version"], capture_output=True, text=True, timeout=15)
        return r.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def shim_file_sha256(cfg: Mapping[str, Any]) -> str:
    """sha256 of the configured shim DLL (shim.dll; "auto" is the repo build)."""
    dll = cfg["shim"]["dll"]
    return file_sha256(PKG_ROOT / "native/shim/build/dinput8.dll" if dll == "auto" else Path(os.path.expanduser(dll)))


def require_native_restore_dll(env: EnvPin, cfg: Mapping[str, Any], backend: Any = None) -> None:
    """Memory restore needs exact shim code identity, independently of env.guard.

    The loaded hello is emitted from the DLL's own file hash. This is a host
    compatibility fence, not a raw-wire schema negotiation or authentication.
    """
    from kptas.supervisor import TelosError
    if cfg.get("run", {}).get("backend") != "shim" and getattr(backend, "name", None) != "shim":
        return
    def digest(value: Any, label: str) -> str:
        if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdefABCDEF" for c in value):
            raise TelosError(f"native memory restore requires a full SHA256 {label}; env.guard cannot relax DLL compatibility")
        return value.lower()
    if not isinstance(env, EnvPin):
        raise TelosError("native memory restore requires a recorded environment DLL identity")
    identities = []
    if env.shim_sha256:
        identities.append(digest(env.shim_sha256, "recorded shim identity"))
    for module in env.modules:
        name, sep, value = module.rpartition("@")
        if not sep:
            name = module
        if name.replace("\\", "/").rsplit("/", 1)[-1].lower() == "dinput8.dll":
            identities.append(digest(value if sep else "", "recorded dinput8.dll module identity"))
    if not identities:
        raise TelosError("native memory restore requires a recorded shim DLL identity; env.guard cannot relax DLL compatibility")
    recorded = identities[0]
    if any(value != recorded for value in identities):
        raise TelosError("native memory restore: recorded shim/module DLL identities disagree")
    pinned = digest(cfg.get("shim", {}).get("dll_sha256"), "configured shim DLL pin")
    actual = digest(shim_file_sha256(cfg), "configured shim DLL file identity")
    if pinned != recorded or actual != recorded:
        raise TelosError("native memory restore: recorded and configured shim DLL identities differ; env.guard cannot relax DLL compatibility")
    if backend is not None:
        hello = getattr(backend, "hello", None)
        loaded = digest(hello.get("shim_sha256") if isinstance(hello, Mapping) else None,
                        "loaded shim DLL identity")
        if loaded != recorded:
            raise TelosError("native memory restore: loaded shim DLL differs from the recording")


def _read_stage_pin(stage_files: Any, root: Path, paths: Any) -> list:
    """StageFiles.read() rows with per-file digests kept in a process-level digest-only map.

    A throwaway subclass overrides only the file hashing step, so the traversal, symlink, hardlink and
    special-file refusals and validate_rows stay StageFiles' own; no file bytes are retained."""
    from kptas.types import BackendError
    base = os.path.abspath(root)

    class _Len:
        __slots__ = ("n",)

        def __init__(self, n: int) -> None:
            self.n = n

        def __len__(self) -> int:
            return self.n

    class _DigestOnly(stage_files):   # type: ignore[misc, valid-type]
        def _regular(self, fd, name, rel):
            if os.name == "nt":
                # Native Windows: `fd` is StageFiles' checked directory path; no dir_fd/O_NOFOLLOW there.
                from kptas.backend.rig import _nt_open_regular
                f = _nt_open_regular(fd, name, os.O_RDONLY)
            else:
                f = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            try:
                st = os.fstat(f)
                if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
                    raise BackendError(f"refusing nonregular or hardlinked stage file: {name}")
                key, slot = _stat_key(st), (base, rel)
                with _DIGEST_LOCK:
                    hit = _STAGE_DIGESTS.get(slot)
                if hit is not None and hit[0] == key:
                    return _Len(st.st_size), hit[1]
                h, n = hashlib.sha256(), 0
                with os.fdopen(f, "rb", closefd=False) as stream:
                    for block in iter(lambda: stream.read(1 << 20), b""):
                        h.update(block)
                        n += len(block)
                digest = h.hexdigest()
                with _DIGEST_LOCK:
                    if _stat_key(os.fstat(f)) == key and n == st.st_size and _settled(st):
                        _STAGE_DIGESTS[slot] = (key, digest)
                    else:
                        _STAGE_DIGESTS.pop(slot, None)
                return _Len(n), digest
            finally:
                os.close(f)

    entries, _ = _DigestOnly(root, paths).read()
    return entries


def prewarm_env_digests(cfg: Mapping[str, Any], *, background: bool = False) -> Any:
    """Fill the digest caches by running pin_env(cfg) once; later pin_env calls rehash only changed files.

    Foreground: returns {"seconds", "key", "error"}; errors are reported, not raised (the real pin_env call
    raises them again). background=True returns a daemon thread started already, its dict in .result."""
    def run() -> dict[str, Any]:
        t0 = time.perf_counter()
        try:
            out: dict[str, Any] = {"key": pin_env(cfg).key(), "error": None}
        except Exception as exc:   # noqa: BLE001 - diagnostic only
            out = {"key": None, "error": repr(exc)}
        out["seconds"] = time.perf_counter() - t0
        return out
    if not background:
        return run()
    th = threading.Thread(target=lambda: setattr(th, "result", run()), name="env-digest-prewarm", daemon=True)
    th.result = None  # type: ignore[attr-defined]
    th.start()
    return th


def pin_env(cfg: Mapping[str, Any]) -> EnvPin:
    """EnvPin for a resolved config. env.pin chooses which items are hashed; the rest stay empty."""
    pin = set(cfg["env"]["pin"])
    g, backend = cfg["game"], cfg["run"]["backend"]
    files: dict[str, str] = {}
    modules: list[str] = []
    exe_sha = text = render = shim = ""
    extra = {"backend": backend}
    if backend in ("pysim", "simgame"):
        extra.update({"sim.seed": str(cfg["sim"]["seed"]), "sim.pages": str(cfg["sim"]["pages"])})
    if backend == "pysim":
        exe_sha = PYSIM_EXE
        shim = file_sha256(PKG_ROOT / "kptas/backend/pysim.py") if "shim" in pin else ""
    else:
        if backend == "simgame":
            s = cfg["sim"]["simgame_exe"]
            exe = PKG_ROOT / "native/simgame/build/SimGame.exe" if s == "auto" else Path(os.path.expanduser(s))
            gdir = exe.parent
        else:
            gdir = Path(os.path.expanduser(g["dir"])) if g["dir"] else Path()
            exe = gdir / g["exe"]
        exe_sha = file_sha256(exe) if "exe" in pin and (g["dir"] or backend == "simgame") else ""
        text = text_sha256(exe) if "text" in pin and (g["dir"] or backend == "simgame") else ""
        ini: Path | bytes | None = gdir / ("swkotor2.ini" if cfg["run"]["game"] == "k2" else "swkotor.ini")
        placed = ""
        if backend == "shim" and g["dir"]:
            # the stage as the launch leaves it (rig.prepare_launch): the pinned ini with the audio line set,
            # and the configured shim as dinput8.dll. Read before the launch, the stage may still hold the
            # previous run's DLL and ini.
            from kptas.backend.rig import launch_ini
            ini = launch_ini(cfg)
            placed = shim_file_sha256(cfg)
        ini_sha = (hashlib.sha256(ini).hexdigest() if isinstance(ini, bytes) else
                   "" if ini is None else file_sha256(ini))
        for name, fn in (("chitin.key", lambda: file_sha256(gdir / "chitin.key")),
                         ("dialog.tlk", lambda: file_sha256(gdir / "dialog.tlk")),
                         ("override", lambda: tree_sha256(gdir / "override")),
                         ("swkotor.ini", lambda: ini_sha),
                         ("keymap", lambda: ini_section_sha256(ini, "Keymapping"))):
            if name in pin and g["dir"]:
                files[name] = fn()
        render = ini_section_sha256(ini, "Graphics Options") if "render" in pin and g["dir"] else ""
        if "modules" in pin and gdir.is_dir():
            seen = sorted({p for pat in _MODULE_GLOBS for p in gdir.glob(pat) if p.is_file()}
                          | ({gdir / "dinput8.dll"} if placed else set()))
            modules = [f"{p.relative_to(gdir).as_posix().lower()}@"
                       f"{placed if placed and p.name.lower() == 'dinput8.dll' else file_sha256(p)}" for p in seen
                       if backend != "handsoff" or p.name.lower() != "dinput8.dll"]
        if "shim" in pin and backend not in ("handsoff", "phasor"):
            shim = shim_file_sha256(cfg)
    if backend == "shim" and g.get("pin"):
        # Hash exactly the inputs reset_stage admits. Sharing its reader also
        # rejects symlinks, hardlinks and special files before claiming a key.
        from kptas.backend.rig import StageFiles
        from kptas.types import BackendError
        root = Path(os.path.expanduser(g["pin"]))
        try:
            if not root.is_dir():
                raise ConfigError(f"game.pin: {root} is not a directory")
            entries = _read_stage_pin(StageFiles, root, cfg["files"]["stage_paths"])
            for rel in cfg["files"]["stage_paths"]:
                rows = []
                for path, digest, _size, _time in entries:
                    name = path.rstrip("/")
                    if name == rel or name.startswith(rel + "/"):
                        rows.append((name[len(rel):], "dir" if path.endswith("/") else digest))
                if not rows:
                    extra[f"stage.pin:{rel}"] = "missing"
                    continue
                # Keep existing keys for ordinary pins: the original walker
                # visited children in name order, directories before descendants.
                rows.sort(key=lambda row: tuple(row[0].split("/")))
                extra[f"stage.pin:{rel}"] = hashlib.sha256(
                    json.dumps(rows, separators=(",", ":")).encode()).hexdigest()
        except (OSError, BackendError) as exc:
            raise ConfigError(f"game.pin: cannot hash launch inputs: {exc}") from exc
    audio = cfg.get("audio", {})
    if backend == "shim" and audio.get("device", "none") not in ("none", "auto"):
        # the device decides which sound paths run; output only routes what the model plays
        extra.update({"audio.device": audio["device"], "audio.provider": audio["provider"],
                      "audio.decode": audio["decode"]})
    if g.get("env"):
        extra["game.env"] = "\n".join(sorted(g["env"]))
    for p in cfg["env"]["extra_hashed"]:
        extra[p] = file_sha256(Path(os.path.expanduser(p))) or "missing"
    mask = cfg["verify"]["mask_registry"]
    if mask and not mask.lstrip().startswith("{"):
        try:
            path = Path(os.path.expanduser(mask))
            if path.is_file():
                mask = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ConfigError(f"cannot read root qualification: {exc}") from exc
    mask = canonical_qualification(mask)
    threads = cfg["threads"]["model"]
    proc = backend in ("simgame", "shim", "handsoff", "phasor")
    prefix = Path(os.path.expanduser(g["wineprefix"] or os.environ.get("WINEPREFIX", "~/.wine")))
    return EnvPin(
        exe_sha256=exe_sha, text_sha256=text, files=files, render=render, modules=tuple(modules),
        shim_sha256=shim, clock={k: cfg["clock"][k] for k in _CLOCK_KEYS},
        profile=profile_obj(cfg) if "profile" in pin else {"id": cfg["portable"]["profile"]},
        threads="join" if threads == "auto" else threads, mask_registry=mask, extra=extra,
        gl_renderer=cfg["gl"]["driver"] if "gl_driver" in pin else "",
        wine=_wine_version(g["wine"]) if proc and g["wine"] and "wine" in pin else "",
        prefix_sha256=(file_sha256(prefix / ".update-timestamp") if proc and g["wine"] and "prefix" in pin
                       else ""),
        dll_overrides=g["dll_overrides"] if "dll_overrides" in pin else "",
        launch_wrapper=g["launch_wrapper"],
        host_facts=host_facts(cfg) if proc and "host" in pin and cfg["portable"]["profile"] == "native" else {})


__all__ = ["LOCALES", "PYSIM_EXE", "boundary_env", "file_sha256", "host_facts", "pin_env", "prewarm_env_digests", "profile_obj",
           "shim_file_sha256", "text_sha256", "tree_sha256"]
