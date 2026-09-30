"""Environment pin (contract 3, 7, 8.3 ENVP, C13): hash what a run depends on into an EnvPin.

Key inputs are the game-side facts: binaries, data files, modules, clock origin,
portable profile, thread model and mask registry. The host fingerprint (Wine,
prefix, real GL driver, overrides, launch wrapper) is recorded but joins the
determinism key only under profile "native". Nothing time-varying is hashed, so
the same configuration always gives the same key.
"""

from __future__ import annotations

import hashlib
import json
import os
import struct
import subprocess
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from kptas.config import ConfigError
from kptas.types import EnvPin, FormatError, PortableProfile

PKG_ROOT = Path(__file__).resolve().parent.parent.parent       # KPTASInterface
PYSIM_EXE = hashlib.sha256(b"pysim/1").hexdigest()             # what PySim's hello reports
BUILTIN_PROFILES: dict[str, dict[str, Any]] = {"baseline-1": {}}   # census-confirmed overrides only
_PROFILE_KEYS = ("math", "x87_cw", "mxcsr", "refresh_hz", "total_phys_mb", "avail_phys_mb", "disk_free_mb",
                 "os_version", "locale", "timezone", "computer_name", "user_name", "random")
_CLOCK_KEYS = ("mode", "hz", "scale", "qpf_hz", "uptime0_ms", "initial_realtime", "hooked_modules",
               "budget_model", "budget_guard", "spin_escape", "spin_count")
_MODULE_GLOBS = ("mss32.dll", "binkw32.dll", "binkw32_real.dll", "miles/*.asi", "miles/*.m3d",
                 "miles/*.flt", "miles/*.mix", "*.asi", "dinput8.dll")


def file_sha256(path: Path) -> str:
    """sha256 hex of a file, or "" when it is missing or unreadable."""
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for block in iter(lambda: f.read(1 << 20), b""):
                h.update(block)
    except OSError:
        return ""
    return h.hexdigest()


def tree_sha256(root: Path) -> str:
    """sha256 over sorted (relative path, content sha256) of every file under root; "" when absent."""
    if not root.is_dir():
        return ""
    rows = sorted([str(p.relative_to(root)).replace(os.sep, "/"), file_sha256(p)]
                  for p in root.rglob("*") if p.is_file())
    return hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode()).hexdigest()


def text_sha256(exe: Path) -> str:
    """sha256 of the PE .text section's raw bytes; "" when the file is missing or not PE."""
    try:
        data = exe.read_bytes()
        pe = struct.unpack_from("<I", data, 0x3C)[0]
        if data[pe:pe + 4] != b"PE\0\0":
            return ""
        nsec, optsize = struct.unpack_from("<H", data, pe + 6)[0], struct.unpack_from("<H", data, pe + 20)[0]
        sec = pe + 24 + optsize
        for i in range(nsec):
            name, _vs, _va, size, ptr = struct.unpack_from("<8sIIII", data, sec + 40 * i)
            if name.rstrip(b"\0") == b".text":
                return hashlib.sha256(data[ptr:ptr + size]).hexdigest()
    except (OSError, struct.error):
        pass
    return ""


def ini_section_sha256(ini: Path, *sections: str) -> str:
    """sha256 of the named INI sections' non-blank lines (keymap, render stamp); "" when none."""
    try:
        lines = ini.read_text(encoding="latin-1").splitlines()
    except OSError:
        return ""
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


def _wine_version(wine: str) -> str:
    try:
        r = subprocess.run([wine, "--version"], capture_output=True, text=True, timeout=15)
        return r.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


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
        ini = gdir / ("swkotor2.ini" if cfg["run"]["game"] == "k2" else "swkotor.ini")
        for name, fn in (("chitin.key", lambda: file_sha256(gdir / "chitin.key")),
                         ("dialog.tlk", lambda: file_sha256(gdir / "dialog.tlk")),
                         ("override", lambda: tree_sha256(gdir / "override")),
                         ("swkotor.ini", lambda: file_sha256(ini)),
                         ("keymap", lambda: ini_section_sha256(ini, "Keymapping"))):
            if name in pin and g["dir"]:
                files[name] = fn()
        render = ini_section_sha256(ini, "Graphics Options") if "render" in pin and g["dir"] else ""
        if "modules" in pin and gdir.is_dir():
            seen = sorted({p for pat in _MODULE_GLOBS for p in gdir.glob(pat) if p.is_file()})
            modules = [f"{p.relative_to(gdir).as_posix().lower()}@{file_sha256(p)}" for p in seen
                       if backend != "handsoff" or p.name.lower() != "dinput8.dll"]
        if "shim" in pin and backend != "handsoff":
            dll = cfg["shim"]["dll"]
            shim = file_sha256(PKG_ROOT / "native/shim/build/dinput8.dll" if dll == "auto"
                               else Path(os.path.expanduser(dll)))
    for p in cfg["env"]["extra_hashed"]:
        extra[p] = file_sha256(Path(os.path.expanduser(p))) or "missing"
    mask = cfg["verify"]["mask_registry"]
    if mask and Path(os.path.expanduser(mask)).is_file():
        mask = Path(os.path.expanduser(mask)).read_text(encoding="utf-8")
    threads = cfg["threads"]["model"]
    proc = backend in ("simgame", "shim", "handsoff")
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
        launch_wrapper=g["launch_wrapper"])


__all__ = ["PYSIM_EXE", "file_sha256", "pin_env", "profile_obj", "text_sha256", "tree_sha256"]
