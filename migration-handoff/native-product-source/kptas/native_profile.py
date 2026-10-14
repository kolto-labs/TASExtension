"""Read-only executable/profile validation; byte matches do not admit native hooks.

Run ``python -m kptas.native_profile IMAGE TABLE --game k2`` to inspect an
existing KotorPhasor address table against an exact local executable.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import struct


class ProfileError(ValueError):
    """An image or address table failed structural or identity validation."""


def inspect_profile(image: bytes, table: dict, *, game: str) -> dict:
    def span(offset: int, size: int) -> bytes:
        if offset < 0 or size < 0 or offset + size > len(image):
            raise ProfileError("PE span exceeds executable bounds")
        return image[offset:offset + size]

    def unpack(fmt: str, offset: int):
        return struct.unpack(fmt, span(offset, struct.calcsize(fmt)))

    if game not in {"k1", "k2"} or str(table.get("game", "")).lower() != game:
        raise ProfileError("Requested title does not match profile")
    digest = hashlib.sha256(image).hexdigest()
    if not table.get("reference_sha256") or digest != table["reference_sha256"]:
        raise ProfileError("Executable SHA256 does not match profile reference")
    if span(0, 2) != b"MZ":
        raise ProfileError("Not a DOS/PE image")
    pe, = unpack("<I", 60)
    if span(pe, 4) != b"PE\0\0":
        raise ProfileError("Missing PE signature")
    machine, count = unpack("<HH", pe + 4)
    optional_size, = unpack("<H", pe + 20)
    optional = pe + 24
    if machine != 0x14c or optional_size < 96 or unpack("<H", optional)[0] != 0x10b:
        raise ProfileError("Profile requires a PE32 x86 executable")
    span(optional, optional_size)
    entry, = unpack("<I", optional + 16)
    base, = unpack("<I", optional + 28)
    if base != table.get("image_base"):
        raise ProfileError("Image base differs from profile")
    sections = []
    for index in range(count):
        at = optional + optional_size + index * 40
        header = span(at, 40)
        name = header[:8].rstrip(b"\0").decode("ascii", errors="strict")
        virtual_size, rva, raw_size, raw_offset = struct.unpack_from("<IIII", header, 8)
        # Avoid allocating attacker-controlled virtual padding. Hash it in chunks.
        if virtual_size > 512 * 1024 * 1024:
            raise ProfileError("Section virtual size exceeds inspection limit")
        raw = span(raw_offset, raw_size)
        h = hashlib.sha256(raw[:virtual_size])
        padding = max(0, virtual_size - raw_size)
        while padding:
            amount = min(padding, 65536)
            h.update(b"\0" * amount)
            padding -= amount
        sections.append({"name": name, "rva": rva, "virtual_size": virtual_size,
                         "raw_size": raw_size, "raw_offset": raw_offset,
                         "mapped_sha256": h.hexdigest()})
    for expected in table.get("known_text", []):
        found = [s for s in sections if s["name"] == expected["section"]]
        if len(found) != 1 or found[0]["mapped_sha256"] != expected["sha256"]:
            raise ProfileError("Mapped section hash differs from profile")
    if not table.get("known_text"):
        raise ProfileError("Profile lacks a mapped section identity")

    def address_bytes(address: int, size: int) -> bytes:
        rva = address - base
        found = [s for s in sections if s["rva"] <= rva and
                 rva + size <= s["rva"] + min(s["virtual_size"], s["raw_size"])]
        if len(found) != 1:
            raise ProfileError("Site is not in exactly one file-backed section")
        s = found[0]
        return span(s["raw_offset"] + rva - s["rva"], size)

    validated, unverified = [], []
    for name, site in table.get("sites", {}).items():
        expected = site.get("expected_bytes")
        if site.get("addr") is None or not expected:
            unverified.append(name)
            continue
        try:
            expected_bytes = bytes.fromhex(expected)
        except (ValueError, TypeError) as exc:
            raise ProfileError(f"Invalid expected bytes for {name}") from exc
        if not expected_bytes or address_bytes(site["addr"], len(expected_bytes)) != expected_bytes:
            raise ProfileError(f"Site bytes differ from profile: {name}")
        validated.append({"site": name, "address": site["addr"], "bytes": expected})
    sentinel = table.get("sentinel")
    if sentinel:
        expected = bytes.fromhex(sentinel["bytes"])
        if not expected or address_bytes(sentinel["va"], len(expected)) != expected:
            raise ProfileError("Sentinel bytes differ from profile")
    return {"game": game, "build": table.get("build"), "image_sha256": digest,
            "image_base": base, "entry_rva": entry, "sections": sections,
            "validated_sites": validated, "unverified_sites": unverified,
            "profile_identity_verified": True, "native_hooks_admitted": False,
            "qualification": "Static image identity and byte checks only; no hook semantics or live acceptance"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", type=Path)
    parser.add_argument("table", type=Path)
    parser.add_argument("--game", required=True, choices=("k1", "k2"))
    args = parser.parse_args()
    try:
        result = inspect_profile(args.image.read_bytes(), json.loads(args.table.read_text()), game=args.game)
    except (ProfileError, OSError, ValueError, KeyError, TypeError) as exc:
        parser.exit(2, f"Profile refused: {exc}\n")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
