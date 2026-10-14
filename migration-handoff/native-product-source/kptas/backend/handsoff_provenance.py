"""Read-only Linux process evidence for the no-shim hands-off boundary.

Evidence is scoped to a controlled local fixture. Descriptor identity does not
attest hostile private COW changes, trusted package origin, game fidelity or
exclusive input. Unsupported filesystem/namespace observations fail closed.
"""
from __future__ import annotations

from contextlib import ExitStack, nullcontext
try:
    import fcntl
except ImportError:      # native Windows: no btrfs ioctl; _subvolume fails closed
    fcntl = None
import hashlib
import mmap
import os
import re
import stat as stat_module
import struct
from pathlib import Path
from typing import Any

from kptas.types import BackendError


def validate_runtime_pin(value: Any) -> dict[str, Any]:
    """Copy an explicit caller declaration; this does not verify package origin."""
    fields = {"schema_version", "sha256", "size", "pe_machine", "package"}
    if type(value) is not dict or set(value) != fields:
        raise BackendError("hands-off requires an explicit complete runtime pin")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise BackendError("unsupported hands-off runtime pin schema")
    if type(value["sha256"]) is not str or not re.fullmatch(r"[0-9a-f]{64}", value["sha256"]):
        raise BackendError("runtime pin requires a lowercase SHA-256")
    if type(value["size"]) is not int or not 0 < value["size"] <= 1 << 31:
        raise BackendError("runtime pin size must be a positive bounded integer")
    if type(value["pe_machine"]) is not int or value["pe_machine"] not in (0x14c, 0x8664, 0xaa64):
        raise BackendError("runtime pin requires a supported PE machine")
    package = value["package"]
    if type(package) is not dict or set(package) != {"name", "version", "architecture"}:
        raise BackendError("runtime pin requires exact package identity fields")
    for field, text in package.items():
        if type(text) is not str or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+~:@-]{0,127}", text):
            raise BackendError(f"invalid runtime package {field}")
    return {**value, "package": dict(package)}


def _pe_machine(data: bytes) -> int:
    """Read architecture from a bounded PE DLL header, not from its filename."""
    if len(data) < 64 or data[:2] != b"MZ":
        raise BackendError("runtime pin requires a PE DLL header")
    offset = struct.unpack_from("<I", data, 60)[0]
    if offset < 64 or offset + 24 > len(data) or data[offset:offset + 4] != b"PE\0\0":
        raise BackendError("runtime pin requires a valid PE header offset/signature")
    machine, sections = struct.unpack_from("<HH", data, offset + 4)
    optional_size, flags = struct.unpack_from("<HH", data, offset + 20)
    if (not sections or optional_size < (96 if machine == 0x14c else 112) or not flags & 0x2000
            or offset + 24 + optional_size + 40 * sections > len(data)):
        raise BackendError("runtime pin requires a complete PE DLL header")
    magic = struct.unpack_from("<H", data, offset + 24)[0]
    if magic != {0x14c: 0x10b, 0x8664: 0x20b, 0xaa64: 0x20b}.get(machine):
        raise BackendError("runtime pin PE machine and optional header disagree")
    return machine


def _signature(st: os.stat_result) -> tuple[int, ...]:
    return (st.st_dev, st.st_ino, st.st_mode, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def _namespace_evidence(root: Path) -> dict[str, Any]:
    observer = Path("/proc/self")
    namespaces = {name: os.readlink(root / "ns" / name) for name in ("mnt", "user")}
    if namespaces != {name: os.readlink(observer / "ns" / name) for name in namespaces}:
        raise BackendError("mapped module identity requires matching mount/user namespaces")
    mountinfo = (root / "mountinfo").read_text()
    if mountinfo != (observer / "mountinfo").read_text():
        raise BackendError("mapped module identity requires matching mount tables")
    return {"namespaces": namespaces, "mountinfo": mountinfo}


def _mount_for(path: Path, mountinfo: str) -> dict[str, str]:
    choices = []
    for line in mountinfo.splitlines():
        left, right = line.split(" - ", 1)
        fields, fs = left.split(), right.split()
        # Ambiguous proc escaping is unsupported instead of guessed.
        mountpoint = fields[4]
        if "\\" in mountpoint or "\\" in fields[3]:
            raise BackendError("escaped mount path is unsupported for module identity")
        if path.is_relative_to(Path(mountpoint)):
            choices.append((len(mountpoint), {"mount_id": fields[0], "device": fields[2],
                            "root": fields[3], "mountpoint": mountpoint,
                            "filesystem": fs[0], "source": fs[1]}))
    if not choices:
        raise BackendError("module identity has no containing mount")
    return max(choices, key=lambda item: item[0])[1]


def _subvolume(fd: int) -> dict[str, Any]:
    # Linux UAPI btrfs_ioctl_get_subvol_info_args (504 bytes), _IOR(0x94,60,504).
    if fcntl is None:
        raise BackendError("module identity needs Linux ioctl (Btrfs subvolume); unavailable on this platform")
    info = bytearray(504)
    fcntl.ioctl(fd, 0x81F8943C, info, True)
    treeid = struct.unpack_from("=Q", info)[0]
    if not treeid:
        raise BackendError("module identity has no Btrfs subvolume ID")
    return {"id": treeid, "uuid": bytes(info[296:312]).hex()}


def _map_identity(fd: int, path: Path, inode: int) -> tuple[int, int, int]:
    maps = Path("/proc/self/maps")
    before = set(maps.read_text().splitlines())
    with mmap.mmap(fd, 1, access=mmap.ACCESS_READ):
        after = set(maps.read_text().splitlines())
        added = after - before
        if before - after or len(added) != 1:
            raise BackendError("ambiguous observer mmap module identity")
        row = added.pop().split(None, 5)
        if (len(row) != 6 or row[5] != str(path) or row[1] != "r--s"
                or int(row[2], 16) != 0 or int(row[4]) != inode):
            raise BackendError("observer mmap does not identify the retained module descriptor")
        major, minor = (int(x, 16) for x in row[3].split(":"))
        return major, minor, inode


def _read_module(path: Path, mapped: tuple[int, int, int], root: Path, held: ExitStack | None = None) -> tuple[bytes, dict[str, Any]]:
    if path.resolve(strict=True) != path:
        raise BackendError("mapped module identity needs a canonical nonsymlink path")
    if (root / "root" / str(path).lstrip("/")).resolve(strict=True) != path:
        raise BackendError("target root module identity differs from observer path")
    opened = os.fdopen(os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK), "rb")
    context = nullcontext(held.enter_context(opened)) if held is not None else opened
    with context as handle:
        initial = os.fstat(handle.fileno())
        if not stat_module.S_ISREG(initial.st_mode) or initial.st_size <= 0:
            raise BackendError("mapped module identity needs a nonempty regular file")
        if _signature(initial) != _signature(path.stat(follow_symlinks=False)):
            raise BackendError("mapped module changed while opening descriptor")
        ordinary = (os.major(initial.st_dev), os.minor(initial.st_dev), initial.st_ino)
        evidence: dict[str, Any] = {"identity_method": "descriptor_dev_inode",
                                  "device": list(ordinary[:2]), "inode": initial.st_ino,
                                  "descriptor_signature": list(_signature(initial))}
        namespace = None
        if mapped != ordinary:
            namespace = _namespace_evidence(root)
            mount = _mount_for(path, namespace["mountinfo"])
            if mount["filesystem"] != "btrfs" or mapped[2] != initial.st_ino:
                raise BackendError("mapped module identity no longer matches its file")
            if (root / "root" / str(path).lstrip("/")).resolve(strict=True) != path:
                raise BackendError("target root module identity differs from observer path")
            subvolume = _subvolume(handle.fileno())
            bridge = _map_identity(handle.fileno(), path, initial.st_ino)
            if bridge != mapped:
                raise BackendError("mapped module identity differs from same-descriptor Btrfs bridge")
            evidence.update(identity_method="btrfs_same_descriptor_map_bridge",
                            map_device=list(bridge[:2]), mount=mount, subvolume=subvolume,
                            namespaces=namespace["namespaces"])
        data = handle.read()
        if (_signature(initial) != _signature(os.fstat(handle.fileno()))
                or _signature(initial) != _signature(path.stat(follow_symlinks=False))
                or path.resolve(strict=True) != path):
            raise BackendError("mapped module changed during hands-off attestation")
        if namespace is not None:
            if (namespace != _namespace_evidence(root)
                    or _subvolume(handle.fileno()) != subvolume
                    or _map_identity(handle.fileno(), path, initial.st_ino) != mapped
                    or (root / "root" / str(path).lstrip("/")).resolve(strict=True) != path):
                raise BackendError("Btrfs module identity changed during attestation")
        return data, evidence


def attest(pid: int, expected_exe: str, *, proc: Path = Path("/proc"),
           runtime_pin: dict[str, Any] | None = None) -> dict[str, Any]:
    with ExitStack() as held:
        pin = validate_runtime_pin(runtime_pin) if runtime_pin is not None else None
        return _attest(pid, expected_exe, proc=proc, held=held, runtime_pin=pin)


def _attest(pid: int, expected_exe: str, *, proc: Path, held: ExitStack,
            runtime_pin: dict[str, Any] | None = None) -> dict[str, Any]:
    if type(pid) is not int or pid <= 0 or len(expected_exe) != 64:
        raise BackendError("hands-off needs a process PID and pinned executable hash")
    root = proc / str(pid)
    try:
        before = (root / "stat").read_text().rsplit(")", 1)[1].split()[19]
        process_namespace = _namespace_evidence(root)
        maps = (root / "maps").read_text()
        paths: dict[str, tuple[int, int, int, bool]] = {}
        deleted_nonmodule_mappings = []
        for line in maps.splitlines():
            row = line.split(None, 5)
            if len(row) != 6 or not row[5].startswith("/"):
                continue
            path = row[5]
            if path.endswith(" (deleted)"):
                # Wine also unlinks temporary backing mappings. Apply the same
                # module-name scope as below before refusing a deleted module;
                # do not confuse its deletion suffix with a file extension.
                original = path[:-len(" (deleted)")]
                low = Path(original).name.lower()
                if low.endswith((".dll", ".exe", ".so")) or ".so." in low:
                    raise BackendError("hands-off cannot attest a deleted mapped module")
                deleted_nonmodule_mappings.append({"path": original, "permissions": row[1]})
                continue
            major, minor = (int(x, 16) for x in row[3].split(":"))
            ident = (major, minor, int(row[4]))
            prior = paths.get(path)
            if prior and prior[:3] != ident:
                raise BackendError("mapped path has inconsistent file identity")
            paths[path] = (*ident, "x" in row[1] or bool(prior and prior[3]))
        modules = []
        executable = []
        directinput = []
        for name in sorted(paths):
            path = Path(name)
            low = path.name.lower()
            if not low.endswith((".dll", ".exe", ".so")) and ".so." not in low:
                continue
            if any(x in low for x in ("kptas", "kpharness", "kotorphasor")):
                raise BackendError(f"hands-off found a tool module: {path.name}")
            data, identity = _read_module(path, paths[name][:3], root, held)
            digest = hashlib.sha256(data).hexdigest()
            item = {"path": name, "sha256": digest, "size": len(data), **identity}
            modules.append(item)
            if low.endswith(".exe") and digest == expected_exe and paths[name][3]:
                executable.append(item)
            if low == "dinput8.dll":
                # Wine's builtin PE marker lives in its DOS stub. A native
                # dinput8 proxy without this marker cannot pass as builtin.
                if data[:2] != b"MZ" or b"Wine builtin DLL" not in data[:256] or not paths[name][3]:
                    raise BackendError("loaded dinput8 is not a Wine builtin PE")
                if runtime_pin is not None:
                    machine = _pe_machine(data)
                    if (digest != runtime_pin["sha256"] or len(data) != runtime_pin["size"]
                            or machine != runtime_pin["pe_machine"]):
                        raise BackendError("loaded dinput8 does not match the declared runtime pin")
                    item["pe_machine"] = machine
                directinput.append(item)
        if not executable or len(directinput) != 1:
            raise BackendError("loaded pinned executable and exactly one builtin dinput8 are required")
        for item in modules:
            candidate = Path(item["path"])
            if ((root / "root" / str(candidate).lstrip("/")).resolve(strict=True) != candidate
                    or candidate.resolve(strict=True) != candidate
                    or list(_signature(candidate.stat(follow_symlinks=False))) != item["descriptor_signature"]):
                raise BackendError("mapped module changed before collection completed")
        after = (root / "stat").read_text().rsplit(")", 1)[1].split()[19]
        if process_namespace != _namespace_evidence(root):
            raise BackendError("hands-off process namespace/mount identity changed")
        if maps != (root / "maps").read_text():
            raise BackendError("hands-off mapped module list changed during attestation")
        if before != after:
            raise BackendError("hands-off process changed during attestation")
        return {"pid": pid, "starttime": before, "exe_sha256": expected_exe,
                "dinput8_builtin_marker": directinput[0], "builtin_identity": "matches_declared_runtime_pin" if runtime_pin else "not_attested",
                "runtime_pin": runtime_pin, "runtime_package_origin": "not_independently_verified", "modules": modules,
                "maps_sha256": hashlib.sha256(maps.encode()).hexdigest(),
                "deleted_nonmodule_mappings": deleted_nonmodule_mappings,
                "threat_model": "controlled_local_fixture",
                "mapped_object_handle": "not_obtained",
                "private_cow_integrity": "not_attested",
                "fidelity": "unverified", "exclusive_input": "not_attested"}
    except (OSError, ValueError, IndexError) as exc:
        raise BackendError(f"cannot attest hands-off process: {exc}") from None
