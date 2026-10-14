"""Bind a supplied desired profile to release declarations, without activation.

This is a pure, bounded consistency gate. It does not resolve version ranges,
verify artifact bytes or publishers, approve requested permissions, admit a
platform/loader, or install code. A binding is not a runtime admission token.
"""
from __future__ import annotations

from dataclasses import dataclass

from .contracts import Capability, Permission
from .profile import ReleaseRef, ResolvedProfile
from .release_manifest import MAX_ITEMS, ReleaseManifest, version_in_range


@dataclass(frozen=True)
class CompositionSelection:
    """Same-profile present references, not explicit install intent or activation.

    No optional/member selection switches are inferred from these references.
    Their roles remain separate from required runtime dependency edges.
    """
    release: ReleaseRef
    required: tuple[ReleaseRef, ...]
    optional: tuple[ReleaseRef, ...]
    pack_members: tuple[ReleaseRef, ...]


def _selected(declarations, entries, *, required, owner):
    result = []
    for declaration in declarations:
        identity = declaration["id"]
        entry = entries.get(identity)
        if entry is None:
            if required:
                raise ValueError(f"missing declared dependency: {owner} -> {identity}")
            continue
        if not version_in_range(entry.release.version, declaration["versions"]):
            raise ValueError(f"declared version range mismatch: {owner} -> {identity}")
        result.append(entry.release)
    return tuple(result)


def _bind(profile, manifests):
    if type(profile) is not ResolvedProfile:
        raise TypeError("profile must be ResolvedProfile")
    if (type(manifests) is not tuple or len(manifests) > MAX_ITEMS
            or any(type(manifest) is not ReleaseManifest for manifest in manifests)):
        raise TypeError("manifests must be a bounded immutable ReleaseManifest tuple")
    if len(profile.extensions) > MAX_ITEMS:
        raise ValueError("profile exceeds declaration binding limit")
    entries = {entry.release.id: entry for entry in profile.extensions}
    declarations = {manifest.release.id: manifest for manifest in manifests}
    if len(declarations) != len(manifests):
        raise ValueError("duplicate manifest identity")
    if set(declarations) != set(entries):
        raise ValueError("profile and manifest identities must match exactly")
    selections = []
    for identity in sorted(entries):
        entry, manifest = entries[identity], declarations[identity]
        if entry.release != manifest.release:
            raise ValueError(f"exact release identity mismatch: {identity}")
        claims = manifest.to_dict()
        required = _selected(claims["requires"], entries, required=True, owner=identity)
        if {ref.id: ref for ref in entry.requires} != {ref.id: ref for ref in required}:
            raise ValueError(f"profile dependency claims differ from manifest: {identity}")
        if set(entry.conflicts) != set(claims["conflicts"]):
            raise ValueError(f"profile conflict claims differ from manifest: {identity}")
        # ResolvedProfile already refuses present declared conflicts and cycles.
        declared_capabilities = {
            Capability(claim["name"], Permission(claim["permission"]))
            for claim in claims["capabilities"]
        }
        if not set(entry.grants) <= declared_capabilities:
            raise ValueError(f"grant is not declared with the same permission: {identity}")
        selections.append(CompositionSelection(
            entry.release, required,
            _selected(claims["optional_requires"], entries, required=False, owner=identity),
            _selected(claims["pack_members"], entries, required=False, owner=identity),
        ))
    return tuple(selections)


@dataclass(frozen=True)
class ProfileBinding:
    """Immutable desired closure matched to claims, not an admitted installation.

    Required runtime edges must exactly match the manifest. Optional dependencies
    and pack members do not become required edges; absent ones stay unselected.
    Present optional/member releases must satisfy their declared ranges. Granted
    capabilities must be a subset of declared name/permission pairs, but their
    presence does not prove caller approval. File/network/native-code requests,
    compatibility and artifact authenticity still require separate admission.
    """
    profile: ResolvedProfile
    manifests: tuple[ReleaseManifest, ...]

    def __post_init__(self):
        _bind(self.profile, self.manifests)
        object.__setattr__(self, "manifests", tuple(sorted(
            self.manifests, key=lambda manifest: manifest.release.id)))

    @property
    def composition(self) -> tuple[CompositionSelection, ...]:
        return _bind(self.profile, self.manifests)

    @property
    def dependency_order(self) -> tuple[str, ...]:
        return self.profile.dependency_order()


def bind_profile(profile: ResolvedProfile,
                 manifests: tuple[ReleaseManifest, ...]) -> ProfileBinding:
    """Refuse missing/contradictory declarations before presenting a desired setup."""
    return ProfileBinding(profile, manifests)
