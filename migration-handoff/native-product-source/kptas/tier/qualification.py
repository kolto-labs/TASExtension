"""Versioned declarations of raw, unmasked cross-launch root comparison intent."""

import json
from kptas.types import FormatError, NULL_DIGEST, TickFlag, canonical_json

RAW = {
    "format": "kptas-root-qualification/1",
    "root_kind": "full_root",
    "page_map": "kptas-page-map/1",
    "masks": [],
}


def canonical_qualification(text: str) -> str:
    if text == "":
        return ""

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise FormatError(f"duplicate root qualification field: {key}")
            result[key] = value
        return result

    try:
        value = json.loads(text, object_pairs_hook=unique)
    except (ValueError, TypeError) as exc:
        raise FormatError(f"invalid root qualification JSON: {exc}") from None
    if value != RAW:
        raise FormatError(
            "unsupported root qualification: expected kptas-root-qualification/1, "
            "full_root, kptas-page-map/1, empty masks, and no other fields"
        )
    return canonical_json(RAW).decode()


def comparison_qualified(left, right) -> bool:
    """Refuse incompatible declared comparisons; never downgrade them silently."""
    a = canonical_qualification(left.mask_registry)
    b = canonical_qualification(right.mask_registry)
    if not a and not b:
        return False
    if not a or not b or left.key() != right.key():
        raise FormatError(
            "qualified raw-root comparison requires matching qualified environment keys"
        )
    return True


def coverage_field(rec) -> str | None:
    if not rec.flags & TickFlag.CAPTURED or rec.state_root == NULL_DIGEST:
        return "state_root"
    if not rec.flags & TickFlag.FILES or rec.files_root == NULL_DIGEST:
        return "files_root"
    return None


def qualified_field(left, right) -> str | None:
    """Memory and files are distinct checks; missing selected coverage fails."""
    absent = coverage_field(left) or coverage_field(right)
    if absent:
        return absent
    root_diff = left.state_root != right.state_root
    file_diff = left.files_root != right.files_root
    if root_diff:
        return "state_root"
    if file_diff:
        return "files_root"
    return None
