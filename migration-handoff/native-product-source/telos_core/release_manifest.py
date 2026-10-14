"""Strict release declaration schema v1, before artifact/cache verification.

All object fields are required (nullable fields remain present); unknown fields
and schema versions refuse. Ranges are inclusive minimum/exclusive maximum
SemVer bounds, not npm expressions. Required/optional dependencies and pack
members are distinct declarations. Permissions are requests, authors/source are
claims, and digests/compatibility are declarations, never verified evidence.
This module neither resolves a closure nor admits, downloads or installs code.
"""
from __future__ import annotations

from dataclasses import dataclass
from copy import deepcopy
import json
import ipaddress
import re
from urllib.parse import urlsplit

from .contracts import Capability, Permission
from .profile import ReleaseRef

MAX_MANIFEST_BYTES = 64 * 1024
MAX_ARTIFACT_BYTES = 2**40
MAX_ITEMS = 128
_TOKEN = re.compile(r"[a-z0-9][a-z0-9_.-]{0,127}\Z")
_COMMIT = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_FIELDS = "schema_version release kind display_name benefit authors license source compatibility requires optional_requires pack_members conflicts capabilities permissions artifact"


def _fields(value, names):
    if type(value) is not dict or set(value) != set(names.split()):
        raise ValueError(f"expected exact fields: {names}")


def _text(value, label, maximum=256):
    if type(value) is not str or not value or len(value) > maximum or any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in value):
        raise ValueError(f"invalid {label}")
    return value


def _token(value):
    if type(value) is not str or not _TOKEN.fullmatch(value):
        raise ValueError("invalid identifier")
    return value


def _choice(value, choices):
    if type(value) is not str or value not in choices:
        raise ValueError("unsupported declaration value")


def _array(value):
    if type(value) is not list or len(value) > MAX_ITEMS:
        raise ValueError("expected bounded array")
    return value


def _unique(values):
    if len(set(values)) != len(values):
        raise ValueError("duplicate declaration")


def _url(value, *, origin=False):
    _text(value, "HTTPS URL", 2048)
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise ValueError("invalid HTTPS URL") from error
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.fragment or "\\" in value
            or any(c.isspace() for c in value) or (port is not None and port < 1)):
        raise ValueError("invalid HTTPS URL")
    try:
        ipaddress.ip_address(parsed.hostname)
    except ValueError:
        labels = parsed.hostname.split(".")
        if (len(parsed.hostname) > 253 or any(not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label) for label in labels)):
            raise ValueError("invalid HTTPS hostname")
    if parsed.netloc.endswith(":"):
        raise ValueError("invalid HTTPS port")
    if origin and (parsed.path not in ("", "/") or parsed.query):
        raise ValueError("network scope must be an HTTPS origin")


def _path(value):
    _text(value, "extension relative path", 256)
    if (value.startswith("/") or "\\" in value or ":" in value or "%" in value
            or any(part in ("", ".", "..") for part in value.split("/"))):
        raise ValueError("invalid extension relative path")


def _semver_key(value):
    _text(value, "exact SemVer", 128)
    ReleaseRef("schema.version", value, "0" * 64)
    main, _, pre = value.split("+", 1)[0].partition("-")
    identifiers = tuple((0, int(p)) if p.isdigit() else (1, p) for p in pre.split(".")) if pre else ()
    return tuple(int(p) for p in main.split(".")), not bool(pre), identifiers


def _range(value):
    _fields(value, "minimum maximum_exclusive")
    lower = _semver_key(value["minimum"])
    if value["maximum_exclusive"] is not None and lower >= _semver_key(value["maximum_exclusive"]):
        raise ValueError("empty or reversed version range")


def version_in_range(version: str, bounds: dict) -> bool:
    """Check inclusive-minimum/exclusive-maximum SemVer declaration bounds.

    Build metadata does not affect precedence. This checks a declaration only;
    it neither chooses a release nor establishes compatibility or trust.
    """
    _range(bounds)
    key = _semver_key(version)
    return (key >= _semver_key(bounds["minimum"])
            and (bounds["maximum_exclusive"] is None
                 or key < _semver_key(bounds["maximum_exclusive"])))


def _validate(value, known_privileged):
    _fields(value, _FIELDS)
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise ValueError("unsupported manifest schema version")
    release = ReleaseRef.from_dict(value["release"])
    if len(release.id) > 128 or len(release.version) > 128:
        raise ValueError("release identity exceeds size limit")
    _choice(value["kind"], ("plugin", "pack", "preset", "compatibility_layer", "workflow"))
    _text(value["display_name"], "display name", 128)
    _text(value["benefit"], "benefit", 512)
    _text(value["license"], "declared license", 128)
    authors = _array(value["authors"])
    if not authors:
        raise ValueError("at least one declared author is required")
    for author in authors:
        _fields(author, "id name url")
        _token(author["id"])
        _text(author["name"], "author name", 128)
        if author["url"] is not None:
            _url(author["url"])
    _unique([a["id"] for a in authors])
    source = value["source"]
    _fields(source, "repository commit")
    _url(source["repository"])
    if type(source["commit"]) is not str or not _COMMIT.fullmatch(source["commit"]):
        raise ValueError("source commit must be an exact lowercase Git object identity")
    compatibility = value["compatibility"]
    _fields(compatibility, "host_api loader_abi games builds operating_systems architectures execution_tier entrypoint restart_required")
    _range(compatibility["host_api"])
    if compatibility["loader_abi"] is not None:
        _range(compatibility["loader_abi"])
    for field, choices in (("games", ("k1", "k2")), ("operating_systems", ("windows", "linux", "macos")), ("architectures", ("x86", "x86_64", "arm64"))):
        items = _array(compatibility[field])
        if not items:
            raise ValueError(f"explicit {field} constraints required")
        for item in items:
            _choice(item, choices)
        _unique(items)
    for build in _array(compatibility["builds"]):
        _text(build, "build constraint", 128)
    _unique(compatibility["builds"])
    _choice(compatibility["execution_tier"], ("data", "lua", "broker", "native"))
    if type(compatibility["restart_required"]) is not bool:
        raise ValueError("restart_required must be boolean")
    if compatibility["execution_tier"] == "data":
        if compatibility["entrypoint"] is not None:
            raise ValueError("data declarations cannot have executable entrypoints")
    else:
        _path(compatibility["entrypoint"])
    if compatibility["execution_tier"] == "native" and (compatibility["loader_abi"] is None or not compatibility["restart_required"]):
        raise ValueError("native declarations require loader ABI and restart")
    all_dependencies = []
    for field in ("requires", "optional_requires", "pack_members"):
        items = _array(value[field])
        for dependency in items:
            _fields(dependency, "id versions")
            ReleaseRef(dependency["id"], "1.0.0", "0" * 64)
            if len(dependency["id"]) > 128 or dependency["id"] == release.id:
                raise ValueError("invalid dependency identity")
            _range(dependency["versions"])
        _unique([d["id"] for d in items])
        all_dependencies.extend(d["id"] for d in items)
    _unique(all_dependencies)
    if value["pack_members"] and value["kind"] != "pack":
        raise ValueError("pack members require pack kind")
    conflicts = _array(value["conflicts"])
    for conflict in conflicts:
        ReleaseRef(conflict, "1.0.0", "0" * 64)
        if len(conflict) > 128 or conflict == release.id or conflict in all_dependencies:
            raise ValueError("self or contradictory conflict")
    _unique(conflicts)
    for capability in _array(value["capabilities"]):
        _fields(capability, "name permission")
        _token(capability["name"])
        _choice(capability["permission"], ("observe", "mutate"))
        declared = Capability(capability["name"], Permission(capability["permission"]))
        if declared.permission is Permission.MUTATE and declared.name not in known_privileged:
            raise ValueError("unknown privileged capability")
    _unique([c["name"] for c in value["capabilities"]])
    permissions = value["permissions"]
    _fields(permissions, "files network native_code")
    if type(permissions["native_code"]) is not bool or permissions["native_code"] != (compatibility["execution_tier"] == "native"):
        raise ValueError("native-code declaration must match execution tier")
    for scope in _array(permissions["files"]):
        _fields(scope, "path access")
        _path(scope["path"])
        _choice(scope["access"], ("read", "write"))
    _unique([s["path"] for s in permissions["files"]])
    for origin in _array(permissions["network"]):
        _url(origin, origin=True)
    _unique(permissions["network"])
    artifact = value["artifact"]
    _fields(artifact, "url size_bytes")
    _url(artifact["url"])
    if type(artifact["size_bytes"]) is not int or not 1 <= artifact["size_bytes"] <= MAX_ARTIFACT_BYTES:
        raise ValueError("invalid artifact size")
    # Canonicalize collections whose order carries no schema meaning.
    for field, key in (("authors", "id"), ("requires", "id"), ("optional_requires", "id"), ("pack_members", "id"), ("capabilities", "name")):
        value[field].sort(key=lambda item: item[key])
    value["conflicts"].sort()
    for field in ("games", "builds", "operating_systems", "architectures"):
        compatibility[field].sort()
    permissions["files"].sort(key=lambda item: item["path"])
    permissions["network"].sort()


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def _decode(data):
    if type(data) is not bytes or len(data) > MAX_MANIFEST_BYTES:
        raise ValueError("manifest must be bounded UTF-8 bytes")
    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=_pairs,
                          parse_constant=lambda value: (_ for _ in ()).throw(ValueError("non-finite JSON")))
    except (UnicodeError, RecursionError) as error:
        raise ValueError("invalid manifest JSON") from error


@dataclass(frozen=True)
class ReleaseManifest:
    """Immutable validated declaration with deterministic JSON serialization.

    known_privileged_capabilities is a local validation policy, excluded from
    serialized publisher metadata. Empty policy refuses every mutate request.
    Validation conveys no capability grant, signature or compatibility proof.
    """
    data: bytes
    known_privileged_capabilities: frozenset[str] = frozenset()

    def __post_init__(self):
        if type(self.known_privileged_capabilities) is not frozenset:
            raise TypeError("capability policy must be an immutable frozenset")
        for name in self.known_privileged_capabilities:
            _token(name)
        value = _decode(self.data)
        _validate(value, self.known_privileged_capabilities)
        canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
        if len(canonical) > MAX_MANIFEST_BYTES:
            raise ValueError("canonical manifest exceeds size limit")
        object.__setattr__(self, "data", canonical)

    @classmethod
    def from_dict(cls, value, *, known_privileged_capabilities=frozenset()):
        try:
            data = json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode("ascii")
        except (TypeError, ValueError, RecursionError) as error:
            raise ValueError("invalid JSON declaration") from error
        if len(data) > MAX_MANIFEST_BYTES:
            raise ValueError("manifest exceeds size limit")
        # Check Python types before JSON can coerce tuples or string subclasses.
        _validate(deepcopy(value), known_privileged_capabilities)
        return cls(data, known_privileged_capabilities)

    @classmethod
    def from_bytes(cls, data, *, known_privileged_capabilities=frozenset()):
        return cls(data, known_privileged_capabilities)

    def to_dict(self):
        return _decode(self.data)

    def to_bytes(self):
        return self.data

    @property
    def release(self) -> ReleaseRef:
        return ReleaseRef.from_dict(self.to_dict()["release"])
