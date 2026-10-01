"""The game's side of a shim run: launching under Wine from pinned inputs, and the stage's writable files.

A cold start copies game.pin over files.stage_paths, copies game.prefix_pin's
registry files into the wineprefix, and places the shim DLL named by shim.dll.
Inside a container Wine runs on the host through host-spawn, which does not
pass the environment on, so the environment goes on the command line (env -i).

StageFiles is the files half of a savestate. It is scanned at the gate, where
the game is held, and written back in place before a restore.
"""

from __future__ import annotations

import base64
import hashlib
import os
import shutil
import stat
import subprocess
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from kptas.types import BackendError, canonical_json

HOST_SPAWN = "host-spawn"
PKG_ROOT = Path(__file__).resolve().parents[2]
EMPTY_SHA = hashlib.sha256(b"").hexdigest()

Row = tuple[str, str, int, int]          # (path, sha256 hex, size, vtime); a directory's path ends in "/"


def host_prefix(cfg: Mapping[str, Any]) -> list[str]:
    """argv prefix that runs a command where Wine lives."""
    g = cfg["game"]
    if g["host"] == "local":
        return []
    if g["host"] == "host-spawn":
        return [HOST_SPAWN, "-no-pty"]
    wine = g["wine"]
    if wine and not shutil.which(wine) and shutil.which(HOST_SPAWN):
        return [HOST_SPAWN, "-no-pty"]
    return []


def win_path(path: str | os.PathLike[str]) -> str:
    """A Linux path as a Wine process sees it (Z: is /)."""
    return "Z:" + str(Path(path).resolve()).replace("/", "\\")


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def game_dir(cfg: Mapping[str, Any]) -> Path:
    return Path(os.path.expanduser(cfg["game"]["dir"]))


def shim_dll(cfg: Mapping[str, Any]) -> Path:
    dll = cfg["shim"]["dll"]
    return PKG_ROOT / "native/shim/build/dinput8.dll" if dll == "auto" else Path(os.path.expanduser(dll))


def place_dll(cfg: Mapping[str, Any]) -> str:
    """Copy shim.dll into the game directory as dinput8.dll; returns its sha256."""
    src = shim_dll(cfg)
    if not src.is_file():
        raise BackendError(f"shim.dll {src} does not exist")
    sha = file_sha256(src)
    want = cfg["shim"]["dll_sha256"].lower()
    if want and not sha.startswith(want):
        raise BackendError(f"shim.dll {src} has sha256 {sha[:16]}, not the pinned {want[:16]}")
    dest = game_dir(cfg) / "dinput8.dll"
    if not dest.is_file() or file_sha256(dest) != sha:
        shutil.copyfile(src, dest)
    return sha


def _remove(p: Path) -> None:
    if p.is_symlink():
        raise BackendError(f"refusing to touch symlinked {p}")
    if p.is_dir():
        shutil.rmtree(p)
    elif p.exists():
        p.unlink()


def reset_stage(cfg: Mapping[str, Any]) -> None:
    """Copy game.pin over the stage's writable paths; a path the pin lacks is removed."""
    pin = cfg["game"]["pin"]
    if not pin:
        return
    pin_dir, stage = Path(os.path.expanduser(pin)), game_dir(cfg)
    if not pin_dir.is_dir():
        raise BackendError(f"game.pin {pin_dir} is not a directory")
    for rel in cfg["files"]["stage_paths"]:
        dest, src = stage / rel, pin_dir / rel
        _remove(dest)
        if src.is_dir():
            shutil.copytree(src, dest, symlinks=False)
        elif src.is_file():
            shutil.copy2(src, dest)


def reset_prefix(cfg: Mapping[str, Any]) -> None:
    """Copy game.prefix_pin's files (system.reg, user.reg, userdef.reg) into the wineprefix."""
    pp, prefix = cfg["game"]["prefix_pin"], cfg["game"]["wineprefix"]
    if not pp:
        return
    if not prefix:
        raise BackendError("game.prefix_pin needs game.wineprefix")
    src, dst = Path(os.path.expanduser(pp)), Path(os.path.expanduser(prefix))
    for f in sorted(src.iterdir()):
        if f.is_file():
            shutil.copyfile(f, dst / f.name)


def stop_wineserver(cfg: Mapping[str, Any]) -> None:
    """End every process of the configured wineprefix. Nothing happens without an explicit game.wineprefix."""
    g = cfg["game"]
    if not g["wine"] or not g["wineprefix"]:
        return
    prefix = str(Path(os.path.expanduser(g["wineprefix"])).resolve())
    argv = [*host_prefix(cfg), "sh", "-c", 'WINEPREFIX="$1" exec wineserver -k', "sh", prefix]
    try:
        subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=60, check=False)
    except (OSError, subprocess.SubprocessError):
        pass


def prepare_launch(cfg: Mapping[str, Any]) -> None:
    """Cold start: stop the prefix's wineserver, then reset the stage, the prefix and the DLL."""
    stop_wineserver(cfg)
    reset_stage(cfg)
    reset_prefix(cfg)
    place_dll(cfg)


def launch_argv(cfg: Mapping[str, Any], argv: Sequence[str], cwd: Path, env: Mapping[str, str], log: Path
                ) -> list[str]:
    """argv that starts `argv` in `cwd` with exactly `env`, on the host when Wine lives there. Output goes to
    `log` and input comes from /dev/null on the game's side, so its standard handles are the same files
    however it was started."""
    host = host_prefix(cfg)
    wrap = ["sh", "-c", 'exec "$@" > "$0" 2>&1 < /dev/null', str(log)]
    envargs = ["env", "-i", *(f"{k}={v}" for k, v in sorted(env.items()))]
    if host:
        return [*host, "-cwd", str(cwd), *wrap, *envargs, *argv]
    return [*wrap, *envargs, *argv]


# ---------------------------------------------------------------- files

class StageFiles:
    """The stage's writable paths as a files tree: rows of (path, sha256, size, vtime).

    vtime stays 0: file times come from the host clock, so they are not part of the tree.
    """

    def __init__(self, stage: Path, paths: Sequence[str]):
        self.stage = stage
        self.paths = list(paths)
        self._cache: dict[str, tuple[tuple[int, int, int], str, bytes]] = {}   # rel -> (stat key, sha, bytes)
        self._last: list[Row] | None = None

    def _walk(self) -> Iterable[tuple[str, Path, os.stat_result]]:
        for rel in self.paths:
            p = self.stage / rel
            try:
                st = p.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(st.st_mode):
                raise BackendError(f"refusing to track symlinked {p}")
            if stat.S_ISDIR(st.st_mode):
                yield rel + "/", p, st
                for root, dirs, files in os.walk(p):
                    dirs.sort()
                    base = Path(root)
                    for d in dirs:
                        q = base / d
                        yield str(q.relative_to(self.stage)).replace(os.sep, "/") + "/", q, q.lstat()
                    for f in sorted(files):
                        q = base / f
                        yield str(q.relative_to(self.stage)).replace(os.sep, "/"), q, q.lstat()
            else:
                yield rel, p, st

    def read(self) -> tuple[list[Row], dict[str, bytes]]:
        """The tree now, and the bytes of every file in it by sha256."""
        rows: list[Row] = []
        blobs: dict[str, bytes] = {EMPTY_SHA: b""}
        seen = set()
        for rel, p, st in self._walk():
            if rel.endswith("/"):
                rows.append((rel, EMPTY_SHA, 0, 0))
                continue
            if not stat.S_ISREG(st.st_mode):
                raise BackendError(f"{p} is not a regular file")
            key = (st.st_size, st.st_mtime_ns, st.st_ino)
            hit = self._cache.get(rel)
            if hit is None or hit[0] != key:
                data = p.read_bytes()
                hit = self._cache[rel] = (key, hashlib.sha256(data).hexdigest(), data)
            seen.add(rel)
            rows.append((rel, hit[1], len(hit[2]), 0))
            blobs[hit[1]] = hit[2]
        for rel in [r for r in self._cache if r not in seen]:
            del self._cache[rel]
        rows.sort(key=lambda r: r[0])
        return rows, blobs

    def changed(self) -> tuple[list[Row], dict[str, bytes]] | None:
        """The tree and its bytes when it differs from the last one seen, else None."""
        rows, blobs = self.read()
        if rows == self._last:
            return None
        self._last = rows
        return rows, blobs

    def snapshot(self) -> bytes:
        """files("snap") blob: canonical JSON {"entries": [[path, sha256, size, vtime, base64]]}."""
        rows, blobs = self.read()
        return canonical_json({"entries": [[p, h, s, v, base64.b64encode(blobs[h]).decode()]
                                           for p, h, s, v in rows]})

    def write(self, rows: Sequence[Sequence[Any]], blob: Callable[[str], bytes]) -> None:
        """Make the stage hold exactly `rows`. Files are rewritten in place, so open handles see the new
        bytes; an unchanged file is left alone."""
        want = {str(r[0]): str(r[1]) for r in rows}
        cur, _ = self.read()
        for rel, _h, _s, _v in sorted(cur, key=lambda r: r[0], reverse=True):
            if rel not in want:
                p = self.stage / rel.rstrip("/")
                if p.is_symlink():
                    raise BackendError(f"refusing to touch symlinked {p}")
                if rel.endswith("/"):
                    shutil.rmtree(p, ignore_errors=True)
                else:
                    p.unlink(missing_ok=True)
        have = {r[0]: r[1] for r in cur}
        for rel in sorted(want):
            p = self.stage / rel.rstrip("/")
            if rel.endswith("/"):
                if p.exists() and not p.is_dir():
                    p.unlink()
                p.mkdir(parents=True, exist_ok=True)
            elif have.get(rel) != want[rel]:
                if p.is_dir():
                    shutil.rmtree(p)
                p.parent.mkdir(parents=True, exist_ok=True)
                data = blob(want[rel])
                with open(p, "r+b" if p.exists() else "wb") as f:
                    f.write(data)
                    f.truncate()
        got, _ = self.read()
        if [(r[0], r[1]) for r in got] != sorted(want.items()):
            raise BackendError("the stage files do not match the tree after writing it back")
        self._last = got


__all__ = ["HOST_SPAWN", "StageFiles", "file_sha256", "game_dir", "host_prefix", "launch_argv", "place_dll",
           "prepare_launch", "reset_prefix", "reset_stage", "shim_dll", "stop_wineserver", "win_path"]
