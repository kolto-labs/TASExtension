"""The game's side of a shim run: launching under Wine from pinned inputs, and the stage's writable files.

A cold start copies game.pin over files.stage_paths, sets the ini's Disable Sound
from audio.device, copies game.prefix_pin's registry files into the wineprefix,
and places the shim DLL named by shim.dll.
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
import sys
from time import perf_counter_ns
from contextlib import contextmanager
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from kptas.types import BackendError, canonical_json
from kptas.filetree import canonical_path, validate_rows

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
    """A Linux path as a Wine process sees it (Z: is /). On native Windows, the resolved native path."""
    if os.name == "nt":
        return str(Path(path).resolve())
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


def _checked_shim(cfg: Mapping[str, Any]) -> tuple[Path, str]:
    """The configured shim DLL and its sha256, held to shim.dll_sha256 when that is set."""
    src = shim_dll(cfg)
    if not src.is_file():
        raise BackendError(f"shim.dll {src} does not exist")
    sha = file_sha256(src)
    want = cfg["shim"]["dll_sha256"].lower()
    if want and not sha.startswith(want):
        raise BackendError(f"shim.dll {src} has sha256 {sha[:16]}, not the pinned {want[:16]}")
    return src, sha


#: Strings every Stasis shim build carries (main.cpp reads these environment variables).
SHIM_MARKERS = (b"KPTAS_PORT", b"KPTAS_SESSION")


def is_stasis_shim(path: Path) -> bool:
    """True when the file is a Stasis shim build (any version), by its marker strings."""
    try:
        data = Path(path).read_bytes()
    except OSError:
        return False
    return all(m in data for m in SHIM_MARKERS)


def place_dll(cfg: Mapping[str, Any]) -> str:
    """Copy shim.dll into the game directory as dinput8.dll; returns its sha256.

    An older Stasis shim there is replaced. Any other dinput8.dll (another mod's) is moved aside once to
    dinput8.dll.stasis-backup-<sha8>, and the move is reported on stderr; it is never deleted."""
    src, sha = _checked_shim(cfg)
    dest = game_dir(cfg) / "dinput8.dll"
    if dest.is_file():
        old = file_sha256(dest)
        if old == sha:
            return sha
        if not is_stasis_shim(dest):
            backup = dest.with_name(f"dinput8.dll.stasis-backup-{old[:8]}")
            if backup.exists():
                raise BackendError(
                    f"{dest} is not a Stasis shim, and {backup.name} already exists. "
                    "Move one of them out of the game folder, then start again.")
            os.replace(dest, backup)
            print(f"stasis: moved the game folder's own dinput8.dll (another mod) to {backup.name}. "
                  "Rename it back to dinput8.dll to use that mod without Stasis.", file=sys.stderr, flush=True)
    shutil.copyfile(src, dest)
    return sha


SHIM_LOG = "kptas_shim.log"      # the shim's slog file, appended to under KPTAS_SESSION (rt.cpp slog_init)


def shim_log_size(session: Path) -> int:
    """Bytes in the session's shim log now. The log is appended to across runs, so a launch reads from here."""
    try:
        return (Path(session) / SHIM_LOG).stat().st_size
    except OSError:
        return 0


# ---------------------------------------------------------------- KPM is not supported
#
# Stasis does not run with KPM (KotOR Patch Manager) installed: a vulnerability was found in KPM. Stasis refuses to
# launch a game directory that carries KPM's loader, and the shim refuses to run in a process that loaded it.
# The check only reads. Stasis never moves, rewrites or deletes KPM's files or any other third-party file.
#
# KPM's loader is KProxy, a small binkw32.dll whose exports forward to binkw32Hooked.dll (the game's real Bink,
# renamed) and which loads KotorPatcher.dll; KotorPatcher reads patch_config.toml and loads DLLs from patches/.

#: The shim's exit code when it refuses to run with KPM or a foreign patch at its entry (shim.hpp KPTAS_EXIT_KPM).
KPM_EXIT_CODE = 75
#: The shim log line it writes before that exit (main.cpp kpm_refuse).
KPM_LOG_MARK = "refuse: kpm present:"
KPM_REFUSAL = "Stasis does not run with KPM installed."
#: Strings only a KProxy carries: its forwarders name binkw32Hooked, and it loads KotorPatcher. A real Bink has
#: neither (the GOG binkw32.dll, 375808 bytes, has no match; KProxy, 16896 bytes, has both).
_KPROXY_MARKS = (b"binkw32Hooked", b"KotorPatcher")


def _kpm_scan(gdir: Path) -> list[tuple[str, str]]:
    """(kind, "path (why)") for each KPM loader file in gdir. Read-only; an absent gdir has none.
    Names match case-insensitively, as Wine resolves them."""
    try:
        entries = {p.name.lower(): p for p in gdir.iterdir()}
    except OSError:
        return []
    found = []
    for name, why in (("kotorpatcher.dll", "KPM's patcher"), ("binkw32hooked.dll", "the game's Bink, renamed by KPM"),
                      ("patch_config.toml", "KPM's patch list")):
        if name in entries:
            found.append((name, f"{entries[name]} ({why})"))
    patches = entries.get("patches")
    if patches is not None and patches.is_dir():
        try:
            dlls = sorted(p.name for p in patches.iterdir() if p.name.lower().endswith(".dll"))
        except OSError:
            dlls = []
        if dlls:
            names = ", ".join(dlls)
            found.append(("patches", f"{patches} (KPM patch DLLs: {names})"))
    bink = entries.get("binkw32.dll")
    if bink is not None and bink.is_file():
        try:
            data = bink.read_bytes()
        except OSError:
            data = b""
        if any(m in data for m in _KPROXY_MARKS):
            found.append(("kproxy", f"{bink} (KPM's KProxy, not the game's Bink)"))
    return found


def kpm_refusal(found: Sequence[tuple[str, str]]) -> str:
    """The launch refusal for KPM files found in game.dir (_kpm_scan rows). Never advises deleting a real Bink:
    binkw32.dll is replaced only when it is a KProxy."""
    kinds = {kind for kind, _ in found}
    steps = []
    if "kproxy" in kinds and "binkw32hooked.dll" in kinds:
        steps.append("To restore the original game files, delete binkw32.dll. "
                     "Then rename binkw32Hooked.dll to binkw32.dll. It is the game's original Bink.")
    elif "kproxy" in kinds:
        steps.append("Replace binkw32.dll with the game's original copy. "
                     "Your store's verify-files option can restore it.")
    remove = [label for kind, label in (("binkw32hooked.dll", "binkw32Hooked.dll"),
                                        ("kotorpatcher.dll", "KotorPatcher.dll"),
                                        ("patch_config.toml", "patch_config.toml"),
                                        ("patches", "the patches folder"))
              if kind in kinds and not (kind == "binkw32hooked.dll" and "kproxy" in kinds)]
    if remove:
        joined = remove[0] if len(remove) == 1 else f"{', '.join(remove[:-1])} and {remove[-1]}"
        steps.append(f"Remove {joined}.")
    steps.append("This check only reads these files.")
    listing = "; ".join(text for _, text in found)
    return f"Found KPM files: {listing}. {KPM_REFUSAL} {' '.join(steps)}"


def refuse_kpm(cfg: Mapping[str, Any]) -> None:
    """Raise BackendError when game.dir carries KPM's loader. Read-only. Run it before anything writes."""
    d = cfg.get("game", {}).get("dir", "")
    found = _kpm_scan(Path(os.path.expanduser(d))) if d else []
    if found:
        raise BackendError(kpm_refusal(found))


def kpm_exit_diagnosis(session: Path, offset: int, returncode: int | None = None) -> str:
    """The refusal when the shim refused at load, else "". The shim log line written after `offset` is the
    primary signal and names the reason; exit code KPM_EXIT_CODE is a second one (it may not survive
    host-spawn or setarch)."""
    try:
        with open(Path(session) / SHIM_LOG, "rb") as f:
            if offset > os.fstat(f.fileno()).st_size:
                offset = 0                  # the log was replaced by a shorter one
            f.seek(offset)
            text = f.read().decode("utf-8", "replace")
    except OSError:
        text = ""
    line = next((ln.strip() for ln in text.splitlines() if KPM_LOG_MARK in ln), "")
    if not line:
        if returncode != KPM_EXIT_CODE:
            return ""
        return (f"The game exited with code {KPM_EXIT_CODE}, the code the shim uses when it refuses to run "
                f"with KPM. {KPM_REFUSAL}")
    if " is loaded" in line:            # main.cpp kpm_guard: KotorPatcher.dll or binkw32Hooked.dll in the process
        why = ("The shim refused to run. KPM was loaded from outside the game folder "
               "(another folder, a Wine DLL override, or a game started elsewhere). " + KPM_REFUSAL)
    else:                               # a foreign patch at the exe entry, or the entry hook never ran
        why = ("The shim refused to run. Something other than Stasis patched swkotor.exe at start. "
               "Remove the other patch tool and relaunch.")
    return f"{why} Shim log: {line}"


def stage_loader(cfg: Mapping[str, Any]) -> str:
    """Place the shim as game.dir/dinput8.dll; returns its sha256."""
    return place_dll(cfg)


def reset_stage(cfg: Mapping[str, Any]) -> None:
    """Copy game.pin over the stage's writable paths; a path the pin lacks is removed."""
    pin = cfg["game"]["pin"]
    if not pin:
        return
    pin_dir, stage = Path(os.path.expanduser(pin)), game_dir(cfg)
    if not pin_dir.is_dir():
        raise BackendError(f"game.pin {pin_dir} is not a directory")
    rows, blobs = StageFiles(pin_dir, cfg['files']['stage_paths']).read()
    StageFiles(stage, cfg['files']['stage_paths']).write(rows, blobs.__getitem__)


def audio_ini(cfg: Mapping[str, Any], raw: bytes) -> bytes:
    """The ini bytes with [Sound Options] Disable Sound set from audio.device: "none" is the sound-off game
    (Disable Sound=1); "virtual" and "real" initialize sound. Only that line changes."""
    device = cfg.get("audio", {}).get("device", "none")
    if device not in ("none", "virtual", "real"):
        return raw
    nl = b"\r\n" if b"\r\n" in raw else b"\n"
    want = b"Disable Sound=" + (b"1" if device == "none" else b"0")
    lines = raw.split(nl)
    sect, at, head = None, None, None
    for i, ln in enumerate(lines):
        t = ln.strip()
        if t.startswith(b"[") and t.endswith(b"]"):
            sect = t[1:-1].strip().lower()
            if sect == b"sound options":
                head = i
            continue
        if sect == b"sound options" and t.split(b"=", 1)[0].strip().lower() == b"disable sound":
            at = i
            break
    if at is not None:
        if lines[at] == want:
            return raw
        lines[at] = want
    elif head is not None:
        lines.insert(head + 1, want)
    else:
        lines += [b"[Sound Options]", want]
    return nl.join(lines)


def ini_name(cfg: Mapping[str, Any]) -> str:
    return "swkotor2.ini" if cfg.get("run", {}).get("game") == "k2" else "swkotor.ini"


def display_ini(cfg: Mapping[str, Any], raw: bytes) -> bytes:
    """Apply the portable client size to the renderer's actual mode selection."""
    if cfg.get("portable", {}).get("profile", "native") == "native":
        return raw
    from kptas.tier.envpin import profile_obj
    w, h = profile_obj(cfg)["window"]
    nl = b"\r\n" if b"\r\n" in raw else b"\n"
    lines, section, found = [], None, set()
    for line in raw.split(nl):
        stripped = line.strip()
        if stripped.startswith(b"[") and stripped.endswith(b"]"):
            if section == b"graphics options":
                for key, value in ((b"Width", w), (b"Height", h)):
                    if key.lower() not in found:
                        lines.append(key + b"=" + str(value).encode())
                        found.add(key.lower())
            section = stripped[1:-1].lower()
        key = stripped.split(b"=", 1)[0].strip().lower()
        if section == b"graphics options" and key in (b"width", b"height"):
            line = key.capitalize() + b"=" + str(w if key == b"width" else h).encode()
            found.add(key)
        lines.append(line)
    if section != b"graphics options" and not found:
        lines.append(b"[Graphics Options]")
    for key, value in ((b"Width", w), (b"Height", h)):
        if key.lower() not in found:
            lines.append(key + b"=" + str(value).encode())
    return nl.join(lines)


def stage_audio(cfg: Mapping[str, Any]) -> None:
    """Set Disable Sound in the stage's ini from audio.device (audio_ini)."""
    if not cfg.get("game", {}).get("dir"):
        return
    ini = game_dir(cfg) / ini_name(cfg)
    try:
        raw = ini.read_bytes()
    except FileNotFoundError:
        raw = b""
    new = display_ini(cfg, audio_ini(cfg, raw))
    if new != raw:
        ini.write_bytes(new)


def launch_ini(cfg: Mapping[str, Any]) -> bytes | None:
    """The game ini as prepare_launch leaves it: game.pin's copy when the pin covers it (reset_stage),
    else the stage's own, with audio and portable display settings applied."""
    g = cfg["game"]
    name = ini_name(cfg)
    src = game_dir(cfg) / name
    if g.get("pin") and name in cfg.get("files", {}).get("stage_paths", ()):
        src = Path(os.path.expanduser(g["pin"])) / name
    try:
        raw = src.read_bytes()
    except FileNotFoundError:
        raw = b""
    return display_ini(cfg, audio_ini(cfg, raw))


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
    if os.name == "nt":
        return
    g = cfg["game"]
    if not g["wine"] or not g["wineprefix"]:
        return
    prefix = str(Path(os.path.expanduser(g["wineprefix"])).resolve())
    # A separately selected Wine build has its own wineserver protocol. Using
    # PATH's server can leave the selected prefix alive while resetting its files.
    wine = os.path.expanduser(g["wine"])
    server = str(Path(wine).parent / "wineserver") if "/" in wine else "wineserver"
    argv = [*host_prefix(cfg), "env", f"WINEPREFIX={prefix}", server]
    try:
        killed = subprocess.run([*argv, "-k"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE, timeout=60, check=False)
        # Wine reports rc=1 without diagnostics when this prefix has no server.
        # Accept that candidate only after -w independently proves quiescence.
        if killed.returncode != 0 and not (killed.returncode == 1 and not killed.stderr):
            raise subprocess.CalledProcessError(killed.returncode, killed.args, stderr=killed.stderr)
        subprocess.run([*argv, "-w"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.PIPE, timeout=60, check=True)
    except (OSError, subprocess.SubprocessError) as exc:
        raise BackendError(f"cannot stop and wait for configured wineserver {server}: {exc}") from exc


def prepare_launch(cfg: Mapping[str, Any]) -> None:
    """Cold start: stop the prefix's wineserver, then reset the stage, the prefix and the DLL. A game.dir with
    KPM's loader is refused first, before anything is written."""
    refuse_kpm(cfg)
    stop_wineserver(cfg)
    reset_stage(cfg)
    stage_audio(cfg)
    reset_prefix(cfg)
    stage_loader(cfg)


def launch_argv(cfg: Mapping[str, Any], argv: Sequence[str], cwd: Path, env: Mapping[str, str], log: Path
                ) -> list[str]:
    """argv that starts `argv` in `cwd` with exactly `env`, on the host when Wine lives there. Output goes to
    `log` and input comes from /dev/null on the game's side, so its standard handles are the same files
    however it was started.

    On native Windows there is no sh or env: argv comes back unwrapped, and the caller's Popen must set
    cwd, env, stdin=DEVNULL and stdout/stderr to `log` itself."""
    if os.name == "nt":
        return list(argv)
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

    def __init__(self, stage: Path, paths: Sequence[str], *, timing: bool = False):
        self.stage = Path(stage)
        self.paths = list(paths)
        for path in self.paths:
            canonical_path(path, virtual=False)
            if path.endswith('/'):
                raise BackendError('files.stage_paths roots must not end in /')
        ordered = sorted(self.paths)
        for i, path in enumerate(ordered):
            if any(path == prev or path.startswith(prev + '/') for prev in ordered[:i]):
                raise BackendError('files.stage_paths roots overlap')
        self._last: list[Row] | None = None
        self._cache = {}
        self.timing = timing
        self.last_read_timing: dict[str, int] | None = None

    @contextmanager
    def _dir(self, rel=''):
        if os.name == 'nt':
            with self._dir_nt(rel) as path:
                yield path
            return
        # Walk from / one component at a time; no ancestor or final symlink is
        # followed. All later operations are relative to these directory fds.
        fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
        try:
            parts = Path(os.path.abspath(self.stage)).parts[1:] + tuple(rel.split('/') if rel else ())
            for part in parts:
                new = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd); fd = new
            yield fd
        except OSError as exc:
            raise BackendError(f'unsafe or unavailable stage directory: {exc}') from exc
        finally:
            os.close(fd)

    @contextmanager
    def _dir_nt(self, rel=''):
        # Native Windows has no directory fds: walk the absolute path one component at a time and refuse
        # any ancestor or final symlink, junction or other reparse point. Yields the checked directory path.
        try:
            parts = Path(os.path.abspath(self.stage)).parts
            path = parts[0]
            for part in parts[1:] + tuple(rel.split('/') if rel else ()):
                path = os.path.join(path, part)
                st = os.lstat(path)
                if _nt_is_link(st):
                    raise OSError(f'symlink or reparse point: {path}')
                if not stat.S_ISDIR(st.st_mode):
                    raise NotADirectoryError(f'not a directory: {path}')
            yield path
        except OSError as exc:
            raise BackendError(f'unsafe or unavailable stage directory: {exc}') from exc

    def _regular(self, fd, name, rel):
        if os.name == 'nt':
            f = _nt_open_regular(fd, name, os.O_RDONLY)
        else:
            f = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        try:
            st = os.fstat(f)
            if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
                raise BackendError(f'refusing nonregular or hardlinked stage file: {name}')
            key = (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
            cached = self._cache.get(rel)
            if cached is None or cached[0] != key:
                with os.fdopen(f, 'rb', closefd=False) as stream:
                    data = stream.read()
                started = perf_counter_ns() if self.timing else 0
                digest = hashlib.sha256(data).hexdigest()
                if self.timing:
                    self._read_timing['hash_ns'] += perf_counter_ns() - started
                    self._read_timing['hashed_bytes'] += len(data)
                    self._read_timing['cache_misses'] += 1
                # Digest and bytes share precisely the existing fingerprint rule.
                cached = (key, data, digest)
                self._cache[rel] = cached
            elif self.timing:
                self._read_timing['cache_hits'] += 1
            return cached[1], cached[2]
        finally:
            os.close(f)

    def read(self) -> tuple[list[Row], dict[str, bytes]]:
        """Read only configured roots, refusing symlinks at every depth."""
        if os.name == 'nt':
            return self._read_nt()
        started = perf_counter_ns() if self.timing else 0
        self.last_read_timing = None
        if self.timing:
            self._read_timing = dict(hash_ns=0, hashed_bytes=0, cache_hits=0, cache_misses=0)
        rows, blobs = [], {EMPTY_SHA: b''}
        def visit(parent, name, rel):
            st = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if stat.S_ISDIR(st.st_mode):
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                try:
                    rows.append((rel + '/', EMPTY_SHA, 0, 0))
                    for item in sorted(os.listdir(child)):
                        visit(child, item, rel + '/' + item)
                finally:
                    os.close(child)
            elif stat.S_ISREG(st.st_mode):
                # The no-follow lstat above carries the same fingerprint _regular takes
                # from fstat. An unchanged single-link file reuses its cached bytes and
                # digest without another open/fstat/close per file per gate.
                cached = self._cache.get(rel)
                if (cached is not None and st.st_nlink == 1 and cached[0] ==
                        (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)):
                    if self.timing:
                        self._read_timing['cache_hits'] += 1
                    data, digest = cached[1], cached[2]
                else:
                    data, digest = self._regular(parent, name, rel)
                rows.append((rel, digest, len(data), 0)); blobs[digest] = data
            else:
                raise BackendError(f'refusing symlink or special stage entry: {rel}')
        for rel in self.paths:
            parent, _, name = rel.rpartition('/')
            try:
                with self._dir(parent) as fd:
                    try:
                        os.stat(name, dir_fd=fd, follow_symlinks=False)
                    except FileNotFoundError:
                        continue
                    visit(fd, name, rel)
            except BackendError as exc:
                if isinstance(exc.__cause__, FileNotFoundError):
                    continue
                raise
        rows.sort()
        self._cache = {r[0]: self._cache[r[0]] for r in rows if r[0] in self._cache}
        validate_rows(rows)
        if self.timing:
            self.last_read_timing = dict(self._read_timing, total_ns=perf_counter_ns() - started)
        return rows, blobs

    def _read_nt(self) -> tuple[list[Row], dict[str, bytes]]:
        """read() for native Windows: path-based, refusing symlinks and reparse points at every depth."""
        started = perf_counter_ns() if self.timing else 0
        self.last_read_timing = None
        if self.timing:
            self._read_timing = dict(hash_ns=0, hashed_bytes=0, cache_hits=0, cache_misses=0)
        rows, blobs = [], {EMPTY_SHA: b''}
        def visit(parent, name, rel):
            path = os.path.join(parent, name)
            st = os.lstat(path)
            if _nt_is_link(st):
                raise BackendError(f'refusing symlink or special stage entry: {rel}')
            if stat.S_ISDIR(st.st_mode):
                rows.append((rel + '/', EMPTY_SHA, 0, 0))
                for item in sorted(os.listdir(path)):
                    visit(path, item, rel + '/' + item)
            elif stat.S_ISREG(st.st_mode):
                cached = self._cache.get(rel)
                if (cached is not None and st.st_nlink == 1 and cached[0] ==
                        (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)):
                    if self.timing:
                        self._read_timing['cache_hits'] += 1
                    data, digest = cached[1], cached[2]
                else:
                    data, digest = self._regular(parent, name, rel)
                rows.append((rel, digest, len(data), 0)); blobs[digest] = data
            else:
                raise BackendError(f'refusing symlink or special stage entry: {rel}')
        for rel in self.paths:
            parent, _, name = rel.rpartition('/')
            try:
                with self._dir(parent) as d:
                    try:
                        os.lstat(os.path.join(d, name))
                    except FileNotFoundError:
                        continue
                    visit(d, name, rel)
            except BackendError as exc:
                if isinstance(exc.__cause__, FileNotFoundError):
                    continue
                raise
        rows.sort()
        self._cache = {r[0]: self._cache[r[0]] for r in rows if r[0] in self._cache}
        validate_rows(rows)
        if self.timing:
            self.last_read_timing = dict(self._read_timing, total_ns=perf_counter_ns() - started)
        return rows, blobs

    def preflight(self, rows, blob):
        """Validate the whole tree and freeze every payload before any stage change."""
        rows, data = validate_rows(rows, blob)
        names = {r[0] for r in rows}
        for rel, _h, _s, vtime in rows:
            canonical_path(rel, virtual=False)
            name = rel.rstrip('/')
            root = next((r for r in self.paths if name == r or name.startswith(r + '/')), None)
            if root is None:
                raise BackendError(f'file tree path is outside configured writable roots: {rel}')
            if vtime != 0:
                raise BackendError('stage files use zero virtual timestamps')
            # Stage snapshots include every directory within a tracked root.
            parent = name.rpartition('/')[0]
            while parent and (parent == root or parent.startswith(root + '/')):
                if parent + '/' not in names:
                    raise BackendError(f'file tree omits directory {parent}/')
                parent = parent.rpartition('/')[0]
        current, _ = self.read()  # validates all existing deletion/read destinations
        # Probe existing ancestry even for absent tracked roots. A regular file
        # ancestor above a nested configured root cannot be replaced implicitly.
        for root in self.paths:
            parent = root.rpartition('/')[0]
            if parent:
                with self._dir(parent):
                    pass
        return rows, data, current

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
        """Restore a validated complete overlay, preserving unchanged/open files.

        Requires the owned stage to be quiescent (held game or stopped server).
        Descriptor-relative no-follow operations additionally prevent symlink
        substitution from redirecting reads, writes or deletions outside it.
        """
        if os.name == 'nt':
            return self._write_nt(rows, blob)
        rows, data, cur = self.preflight(rows, blob)
        want = {r[0]: r[1] for r in rows}
        have = {r[0]: r[1] for r in cur}
        for rel, _h, _s, _v in sorted(cur, key=lambda r: (r[0].count('/'), r[0]), reverse=True):
            if rel in want:
                continue
            parent, _, name = rel.rstrip('/').rpartition('/')
            with self._dir(parent) as fd:
                if rel.endswith('/'):
                    os.rmdir(name, dir_fd=fd)
                else:
                    os.unlink(name, dir_fd=fd)
        for rel, digest, _size, _vtime in sorted(rows, key=lambda r: (r[0].count('/'), r[0])):
            parent, _, name = rel.rstrip('/').rpartition('/')
            with self._dir(parent) as fd:
                if rel.endswith('/'):
                    if rel not in have:
                        os.mkdir(name, dir_fd=fd)
                elif have.get(rel) != digest:
                    flags = os.O_WRONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                    if rel not in have:
                        flags |= os.O_CREAT | os.O_EXCL
                    f = os.open(name, flags, 0o666, dir_fd=fd)
                    try:
                        st = os.fstat(f)
                        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
                            raise BackendError(f'refusing nonregular or hardlinked destination: {rel}')
                        with os.fdopen(f, 'wb', closefd=False) as stream:
                            stream.write(data[digest]); stream.flush()
                        os.ftruncate(f, len(data[digest]))
                    finally:
                        os.close(f)
        got, _ = self.read()
        if got != rows:
            raise BackendError('the stage files do not match the tree after writing it back')
        self._last = got

    def _write_nt(self, rows: Sequence[Sequence[Any]], blob: Callable[[str], bytes]) -> None:
        """write() for native Windows: path-based, each parent re-checked for links and reparse points."""
        rows, data, cur = self.preflight(rows, blob)
        want = {r[0]: r[1] for r in rows}
        have = {r[0]: r[1] for r in cur}
        for rel, _h, _s, _v in sorted(cur, key=lambda r: (r[0].count('/'), r[0]), reverse=True):
            if rel in want:
                continue
            parent, _, name = rel.rstrip('/').rpartition('/')
            with self._dir(parent) as d:
                path = os.path.join(d, name)
                if _nt_is_link(os.lstat(path)):
                    raise BackendError(f'refusing symlink or reparse point in stage: {rel}')
                if rel.endswith('/'):
                    os.rmdir(path)
                else:
                    os.unlink(path)
        for rel, digest, _size, _vtime in sorted(rows, key=lambda r: (r[0].count('/'), r[0])):
            parent, _, name = rel.rstrip('/').rpartition('/')
            with self._dir(parent) as d:
                if rel.endswith('/'):
                    if rel not in have:
                        os.mkdir(os.path.join(d, name))
                elif have.get(rel) != digest:
                    flags = os.O_WRONLY
                    if rel not in have:
                        flags |= os.O_CREAT | os.O_EXCL
                    f = _nt_open_regular(d, name, flags, create=rel not in have)
                    try:
                        st = os.fstat(f)
                        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
                            raise BackendError(f'refusing nonregular or hardlinked destination: {rel}')
                        with os.fdopen(f, 'wb', closefd=False) as stream:
                            stream.write(data[digest]); stream.flush()
                        os.ftruncate(f, len(data[digest]))
                    finally:
                        os.close(f)
        got, _ = self.read()
        if got != rows:
            raise BackendError('the stage files do not match the tree after writing it back')
        self._last = got


def _nt_is_link(st: os.stat_result) -> bool:
    """A symlink, junction or other reparse point (native Windows; always just S_ISLNK elsewhere)."""
    return stat.S_ISLNK(st.st_mode) or bool(
        getattr(st, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _nt_open_regular(directory: str, name: str, flags: int, *, create: bool = False) -> int:
    """Native Windows stand-in for os.open(name, flags | O_NOFOLLOW, dir_fd=...): refuse a link or
    reparse point at `directory/name`, then open it in binary mode."""
    path = os.path.join(directory, name)
    if not create:
        st = os.lstat(path)
        if _nt_is_link(st):
            raise BackendError(f'refusing symlink or reparse point in stage: {path}')
    return os.open(path, flags | getattr(os, 'O_BINARY', 0), 0o666)


__all__ = ["HOST_SPAWN", "StageFiles", "audio_ini", "file_sha256", "game_dir", "host_prefix", "ini_name",
           "KPM_EXIT_CODE", "kpm_exit_diagnosis", "kpm_refusal", "launch_argv", "launch_ini", "place_dll",
           "prepare_launch", "refuse_kpm", "shim_log_size", "stage_loader", "reset_prefix", "reset_stage", "shim_dll",
           "stage_audio", "stop_wineserver", "win_path"]
