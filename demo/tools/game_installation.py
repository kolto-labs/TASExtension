"""Backend-owned validation and indexing for a user-selected local KOTOR install.

The module only reads files from a path explicitly supplied to the local
server. It produces a compact same-origin manifest; game bytes remain on the
user's machine and are served lazily by the caller.
"""
from __future__ import annotations

import os
import hashlib
import re
import stat
from pathlib import Path

ASSET_DIRS = {"data", "texturepacks", "modules", "rims", "lips", "movies", "streammusic", "streamsounds", "streamwaves", "sounds", "music", "override", "miles"}
TOP_EXTS = {".exe", ".dll", ".ini", ".key", ".tlk", ".erf", ".bif"}
OMIT = {"dinput8.dll", "winmm.dll", "dsound.dll"}
WRITABLE = ["swkotor.ini", "saves", "logs", "currentgame", "gameinprogress", "temp", "tempclient", "localvault", "dmvault", "servervault"]
K1_REQUIRED_FILES = {"swkotor.exe", "chitin.key", "dialog.tlk", "binkw32.dll", "mss32.dll"}
MAX_INDEXED_FILES = 100000
LIBRARY_PROFILE_SCHEMA = "telos.k1-library-profile/v1"
BINK_PROXY = {"bytes": 104223, "sha256": "0972fbfe3f25c5d05969dd3e9db4d0c8413dcfe53ef3d8cf0f01944d0f4be8d6"}
BINK_REAL = {"bytes": 375808, "sha256": "2d0ae23a6175dc7b635c402a5e7e9542e923c0d1c376a8c5ef876ca0d5959d23"}
BINK_COMPAT_ID = "k1-bink-proxy-to-verified-real/v1"


def game_installation(root: Path, *, require_executable: bool = True) -> tuple[list[tuple[Path, str, int]], bytes]:
    """Index and validate an on-disk K1 install without copying its assets."""
    root = root.resolve(strict=True)
    files: list[tuple[Path, str, int]] = []
    identities: set[str] = set()
    stack = [root]
    while stack:
        directory = stack.pop()
        with os.scandir(directory) as entries:
            for item in entries:
                path = Path(item.path)
                relative = path.relative_to(root).as_posix()
                if item.is_symlink():
                    continue
                if item.is_dir(follow_symlinks=False):
                    if path.parent != root or item.name.casefold() in ASSET_DIRS:
                        stack.append(path)
                    continue
                if not item.is_file(follow_symlinks=False):
                    continue
                if path.parent == root and path.suffix.casefold() not in TOP_EXTS and relative.casefold() != "startup.txt":
                    continue
                if relative.casefold() in OMIT:
                    continue
                size = item.stat(follow_symlinks=False).st_size
                key = relative.casefold()
                if key in identities:
                    raise RuntimeError(f"Case-insensitive game asset collision: {relative}")
                identities.add(key)
                if len(files) >= MAX_INDEXED_FILES:
                    raise RuntimeError(f"K1 installation contains more than {MAX_INDEXED_FILES} files.")
                files.append((path, relative, size))
    files.sort(key=lambda entry: entry[1].casefold())
    by_name = {name.casefold(): (path, size) for path, name, size in files}
    required_files = K1_REQUIRED_FILES if require_executable else K1_REQUIRED_FILES - {"swkotor.exe"}
    if not required_files.issubset(by_name):
        missing = ", ".join(sorted(required_files - by_name.keys()))
        raise RuntimeError(f"K1 installation is missing required files: {missing}.")
    empty_required = sorted(name for name in required_files if by_name[name][1] == 0)
    if empty_required:
        raise RuntimeError(f"K1 installation contains empty required files: {', '.join(empty_required)}.")
    key_path, key_size = by_name["chitin.key"]
    if key_size > 16 * 1024 * 1024:
        raise RuntimeError("Unexpected chitin.key size.")
    key = key_path.read_bytes()
    if len(key) < 64 or key[:8] != b"KEY V1  ":
        raise RuntimeError("Unsupported original resource KEY index.")
    count, table = int.from_bytes(key[8:12], "little"), int.from_bytes(key[16:20], "little")
    if count > 65536 or table + count * 12 > len(key):
        raise RuntimeError("Invalid resource KEY table.")
    referenced_bifs: set[str] = set()
    for index in range(count):
        row = table + index * 12
        offset = int.from_bytes(key[row + 4:row + 8], "little")
        length = int.from_bytes(key[row + 8:row + 10], "little")
        if offset + length > len(key):
            raise RuntimeError("Invalid resource BIF filename.")
        name = key[offset:offset + length].split(b"\0", 1)[0].decode("latin-1").replace("\\", "/").casefold()
        referenced_bifs.add(name)
    # File pickers may expose a root-level BIF without the `data/` prefix
    # stored in chitin.key. Match the browser validator's basename fallback
    # and publish the canonical KEY path while retaining the local source path.
    aliases: dict[str, str] = {}
    for name in referenced_bifs:
        if name in by_name:
            if by_name[name][1] == 0:
                raise RuntimeError(f"Original KEY references an empty BIF: {name}")
            continue
        basename = name.rsplit("/", 1)[-1]
        if basename in by_name:
            if by_name[basename][1] == 0:
                raise RuntimeError(f"Original KEY references an empty BIF: {name}")
            aliases[basename] = name
        else:
            raise RuntimeError(f"Original KEY references an unavailable BIF: {name}")
    if aliases:
        files = [(path, aliases.get(name.casefold(), name), size) for path, name, size in files]
        files.sort(key=lambda entry: entry[1].casefold())
    return files, key


def make_manifest(files: list[tuple[Path, str, int]], *, immutable_library_bytes: dict[int, bytes] | None = None) -> dict:
    """Build and validate the compact object exposed by /private/install.json."""
    descriptors = [{"name": name, "bytes": size, "url": f"/private/install/{index}"}
                   for index, (_, name, size) in enumerate(files)]
    library_profile = make_library_profile(files, descriptors, immutable_library_bytes)
    manifest = {"schema": "telos.owned-game-assets/v1", "assetMode": "lazy",
                "gameProfile": "k1", "files": descriptors, "writable": WRITABLE,
                "libraryProfile": library_profile}
    manifest["indexIdentity"] = index_identity(descriptors, library_profile)
    validate_manifest(manifest)
    return manifest


def _read_indexed_library(path: Path, expected_size: int) -> bytes | None:
    """Read only a known-size regular file without following symlinks or races."""
    chunks = []
    try:
        descriptor = open_absolute_no_follow(path, nonblocking=True)
    except OSError:
        return None
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size != expected_size:
            return None
        remaining = expected_size
        while remaining:
            block = os.read(descriptor, min(64 * 1024, remaining))
            if not block:
                return None
            chunks.append(block)
            remaining -= len(block)
        if os.read(descriptor, 1):
            return None
        after = os.fstat(descriptor)
        before_identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
        after_identity = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
        if before_identity != after_identity:
            return None
        return b"".join(chunks)
    except OSError:
        return None
    finally:
        os.close(descriptor)


def make_library_profile(files: list[tuple[Path, str, int]], descriptors: list[dict],
                          immutable_library_bytes: dict[int, bytes] | None = None) -> dict:
    """Select only the byte-verified K1 Bink proxy adapter; never infer substitutes."""
    by_name = {name.casefold(): (path, size, index) for index, (path, name, size) in enumerate(files)}
    proxy = by_name.get("binkw32.dll")
    backup = by_name.get("binkw32_real.dll")
    if proxy is None:
        raise RuntimeError("K1 installation is missing the indexed binkw32.dll library.")
    proxy_path, proxy_size, _ = proxy
    proxy_bytes = _read_indexed_library(proxy_path, BINK_PROXY["bytes"]) if proxy_size == BINK_PROXY["bytes"] else None
    proxy_hash = hashlib.sha256(proxy_bytes).hexdigest() if proxy_bytes is not None else None
    if proxy_bytes is not None and immutable_library_bytes is not None:
        immutable_library_bytes[proxy[2]] = proxy_bytes
    identity_parts = ["native", proxy_hash] if proxy_hash else ["native"]
    replacements = []
    profile_id = "native"
    if proxy_size == BINK_PROXY["bytes"] and proxy_hash == BINK_PROXY["sha256"] and backup is not None:
        backup_path, backup_size, backup_index = backup
        backup_bytes = _read_indexed_library(backup_path, BINK_REAL["bytes"]) if backup_size == BINK_REAL["bytes"] else None
        backup_hash = hashlib.sha256(backup_bytes).hexdigest() if backup_bytes is not None else None
        if backup_size == BINK_REAL["bytes"] and backup_hash == BINK_REAL["sha256"]:
            if immutable_library_bytes is not None:
                immutable_library_bytes[backup_index] = backup_bytes
            profile_id = BINK_COMPAT_ID
            identity_parts = [BINK_COMPAT_ID, proxy_hash, backup_hash]
            replacements = [{
                "target": "binkw32.dll",
                "targetSha256": proxy_hash,
                "source": "binkw32_real.dll",
                "sourceSha256": backup_hash,
                "bytes": backup_size,
                "url": descriptors[backup_index]["url"],
            }]
    identity = hashlib.sha256("\0".join(identity_parts).encode("ascii")).hexdigest()
    return {"schema": LIBRARY_PROFILE_SCHEMA, "id": profile_id,
            "identity": identity, "replacements": replacements}


def index_identity(descriptors: list[dict], library_profile: dict) -> str:
    """Bind child runtime mounts to the exact indexed names, sizes, URLs and profile."""
    lines = [f"library-profile\0{library_profile['id']}\0{library_profile['identity']}"]
    lines.extend(f"{entry['name']}\0{entry['bytes']}\0{entry['url']}" for entry in descriptors)
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def validate_manifest(manifest: object) -> None:
    """Enforce the server-owned manifest contract before it crosses HTTP."""
    if not isinstance(manifest, dict) or manifest.get("schema") != "telos.owned-game-assets/v1":
        raise RuntimeError("K1 installation produced an invalid asset manifest.")
    if (manifest.get("assetMode") != "lazy" or manifest.get("gameProfile") != "k1"
            or manifest.get("writable") != WRITABLE):
        raise RuntimeError("K1 installation produced an invalid asset manifest.")
    library_profile = manifest.get("libraryProfile")
    if (not isinstance(library_profile, dict)
            or library_profile.get("schema") != LIBRARY_PROFILE_SCHEMA
            or library_profile.get("id") not in {"native", BINK_COMPAT_ID}
            or not isinstance(library_profile.get("identity"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", library_profile["identity"])):
        raise RuntimeError("K1 installation produced an invalid library profile.")
    replacements = library_profile.get("replacements")
    if not isinstance(replacements, list) or len(replacements) > 1:
        raise RuntimeError("K1 installation produced an invalid library profile.")
    if library_profile["id"] == "native" and replacements:
        raise RuntimeError("K1 installation produced an invalid native library profile.")
    if library_profile["id"] == BINK_COMPAT_ID:
        if len(replacements) != 1:
            raise RuntimeError("K1 installation produced an invalid Bink compatibility profile.")
        replacement = replacements[0]
        if (not isinstance(replacement, dict)
                or set(replacement) != {"target", "targetSha256", "source", "sourceSha256", "bytes", "url"}
                or replacement["target"] != "binkw32.dll"
                or replacement["targetSha256"] != BINK_PROXY["sha256"]
                or replacement["source"] != "binkw32_real.dll"
                or replacement["sourceSha256"] != BINK_REAL["sha256"]
                or replacement["bytes"] != BINK_REAL["bytes"]
                or not isinstance(replacement["url"], str)
                or not re.fullmatch(r"/private/install/\d+", replacement["url"])):
            raise RuntimeError("K1 installation produced an invalid Bink compatibility profile.")
    descriptors = manifest.get("files")
    if not isinstance(descriptors, list) or len(descriptors) > MAX_INDEXED_FILES:
        raise RuntimeError("K1 installation produced an invalid asset manifest.")
    identities: set[str] = set()
    for index, descriptor in enumerate(descriptors):
        if not isinstance(descriptor, dict) or set(descriptor) != {"name", "bytes", "url"}:
            raise RuntimeError("K1 installation produced an invalid asset manifest.")
        name, size, url = descriptor["name"], descriptor["bytes"], descriptor["url"]
        if (not isinstance(name, str) or not name or len(name) > 1024
                or "\\" in name or name.startswith("/")
                or any(part in {"", ".", ".."} for part in name.split("/"))
                or any(ord(char) < 32 for char in name)
                or not isinstance(size, int) or isinstance(size, bool) or size < 0 or size > 2**53 - 1
                or url != f"/private/install/{index}"
                or name.casefold() in identities):
            raise RuntimeError("K1 installation produced an invalid asset manifest.")
        identities.add(name.casefold())
    if (not isinstance(manifest.get("indexIdentity"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", manifest["indexIdentity"])
            or manifest["indexIdentity"] != index_identity(descriptors, library_profile)):
        raise RuntimeError("K1 installation produced an invalid index identity.")
    if library_profile["id"] == BINK_COMPAT_ID:
        by_name = {descriptor["name"].casefold(): descriptor for descriptor in descriptors}
        replacement = replacements[0]
        source = by_name.get("binkw32_real.dll")
        target = by_name.get("binkw32.dll")
        if (source is None or target is None
                or source["bytes"] != BINK_REAL["bytes"]
                or source["url"] != replacement["url"]
                or target["bytes"] != BINK_PROXY["bytes"]):
            raise RuntimeError("K1 installation produced an invalid Bink replacement target.")


def open_absolute_no_follow(path: Path, *, directory: bool = False, nonblocking: bool = False) -> int:
    """Open an absolute path by walking every component without symlinks."""
    nofollow, directory_flag = getattr(os, "O_NOFOLLOW", 0), getattr(os, "O_DIRECTORY", 0)
    if not path.is_absolute():
        raise OSError("Indexed game asset path is not absolute.")
    parts = path.parts[1:]
    parent_fd = os.open(path.anchor, os.O_RDONLY | directory_flag | nofollow)
    try:
        if not parts:
            if not directory:
                raise OSError("Indexed game asset path does not name a file.")
            return os.dup(parent_fd)
        for component in parts[:-1]:
            next_fd = os.open(component, os.O_RDONLY | directory_flag | nofollow, dir_fd=parent_fd)
            os.close(parent_fd)
            parent_fd = next_fd
        nonblocking_flag = getattr(os, "O_NONBLOCK", 0) if nonblocking else 0
        return os.open(parts[-1], os.O_RDONLY | nofollow | nonblocking_flag | (directory_flag if directory else 0), dir_fd=parent_fd)
    finally:
        os.close(parent_fd)


def open_indexed_file(path: Path, root: Path, expected_size: int) -> int:
    """Open an indexed asset without following a changed symlink path."""
    nofollow, directory_flag = getattr(os, "O_NOFOLLOW", 0), getattr(os, "O_DIRECTORY", 0)
    try:
        relative = path.relative_to(root)
    except ValueError:
        descriptor = open_absolute_no_follow(path)
    else:
        parent_fd = open_absolute_no_follow(root, directory=True)
        try:
            for component in relative.parts[:-1]:
                next_fd = os.open(component, os.O_RDONLY | directory_flag | nofollow, dir_fd=parent_fd)
                os.close(parent_fd)
                parent_fd = next_fd
            descriptor = os.open(relative.parts[-1], os.O_RDONLY | nofollow, dir_fd=parent_fd)
        finally:
            os.close(parent_fd)
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != expected_size:
        os.close(descriptor)
        raise OSError("Indexed game asset changed after it was validated.")
    return descriptor
