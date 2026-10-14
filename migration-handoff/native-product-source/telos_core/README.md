# TelosCORE

TelosCORE is the lightweight platform and shared overlay host. TAS is a separate
modular package installed onto it. Core owns generic panels, layouts, appearance,
input routing and extension lifecycle; TAS supplies its timeline, piano roll,
playback controls and holorecord workflows through Core extension APIs. Core's
overlay must remain usable without TAS. These are the intended ownership
boundaries; this package currently contains the components listed below, and
runtime/overlay extraction remains unfinished. Stable distribution/import names
remain `telos-core` and `telos_core`.

This is the independently installable Python package for the current Telos Core
contracts, local provider registry, resolved-profile storage, release declarations,
and local artifact byte cache. It requires Python 3.11 or newer
and has no runtime dependencies on Stasis (`kptas`), PySide, native game code, or a catalog
service.

An independent consumer can import `telos_core`, register a local provider,
inspect its declared capabilities, negotiate an API version and permission set,
subscribe to bounded events, and dispose its activation and owned resources.
API versions in this package describe this Core facade. TAS service APIs and a
future loader ABI must be negotiated separately.

```python
from telos_core import (
    ApiVersion, Capability, ExtensionId, OutcomeState, Permission,
    ProviderId, ProviderSpec, Registry,
)

observations = Capability("example.observe", Permission.OBSERVE)
registry = Registry()
registry.register(
    ProviderSpec(ProviderId("example"), ApiVersion(1, 0), frozenset({observations})),
    lambda activation: None,
)
result = registry.negotiate(
    request_id="example-1",
    extension=ExtensionId("independent.consumer"),
    provider=ProviderId("example"),
    api=ApiVersion(1, 0),
    required=frozenset({observations}),
    granted=frozenset({observations}),
)
assert result.state is OutcomeState.COMPLETED
subscription = result.value.subscribe(capacity=4)
result.value.emit("example.ready", b"ready")
assert subscription.poll()[0][0].payload == b"ready"
registry.close()
assert subscription.closed
```

Resolved profiles are available through `telos_core.profile`. They record a
supplied desired extension closure with exact SemVer releases, artifact and
configuration SHA-256 digests, grants, required dependencies and conflicts.
Validation rejects unknown fields, duplicate identities, unsatisfied exact
references, cycles and conflicts. It does not resolve package version ranges or
verify the supplied declarations against manifests or artifact bytes.

```python
from tempfile import TemporaryDirectory
from telos_core.profile import (
    ProfileStore, ReleaseRef, ResolvedExtension, ResolvedProfile,
)

# Illustrative digests only; the caller supplies verified artifact/configuration
# digests and approved grants when integrating a real package manager.
profile = ResolvedProfile("example", (
    ResolvedExtension(ReleaseRef("example.addition", "1.0.0", "a" * 64), "b" * 64),
))
with TemporaryDirectory() as directory:
    store = ProfileStore(directory)
    first = store.save(profile, expected_revision=None)
    second = store.save(ResolvedProfile("example", ()), expected_revision=first.digest)
    assert store.load().digest == second.digest
    assert store.load(first.digest).profile == profile
    # Undo appends another desired revision; it performs no runtime rollback.
    restored = store.save(first.profile, expected_revision=second.digest)
    assert [revision.digest for revision in store.history()] == [
        restored.digest, second.digest, first.digest,
    ]
```

`ProfileStore` requires a pre-existing trusted local directory on a POSIX
filesystem supporting `flock`, atomic replacement and directory `fsync`. The
caller must establish directory ownership, permissions and isolation from
untrusted writers; the store does not enforce that trust boundary or defend
against hostile symlink replacement. Windows and unusual filesystems remain
unqualified. The contracts and registry have no dependency on this POSIX store.

Successful saves flush the revision and head before returning. If a save raises,
reopen the store: the head may name the old or new complete revision. Tests cover
process interruption and injected flush failures, not physical power loss.
`load()` and `save()` validate the selected/current revision; `history()` also
traverses and validates its ancestry. Revision content hashes detect corruption,
but do not prove publisher authenticity. History is retained without a size or
count bound; there is no pruning or garbage collection.

This package does not yet implement an extension loader, package solver or
installer, artifact authenticity checks, catalog, TAS service, native adapter,
or complete standalone Telos Core platform. Desired profile revisions do not
acknowledge runtime activation. Installing this wheel does not install or
activate game integration.

`telos_core.debugger` bridges two implemented read operations into the Core
provider lifecycle: bounded session status and bounded guest-memory reads. A
runtime host supplies per-operation reports from the transport, backend,
target adapter and policy layers, all bound to one live session, target and
revision. Only those two operations, when reported `available` by all four
layers, become Core `Capability` values. Unknown operations, stale/cross-target
reports, non-live sources and KPM provider identities are refused. The host
registers the provider with its own activation factory; extensions then use
`negotiate_debugger()` to pass the existing Core API-version and explicit-grant
checks. Each read rechecks the backend's session/target/revision before and
after dispatch and passes the expected identity into the backend; returned
observations retain that identity. These checks alone do not prove an atomic
snapshot. The backend must bind each request to the exact revision and, for
memory reads, revalidate the safe pause/all-worker barrier during capture.
Memory reads cap at 64 KiB, require a selected process ID and known 32- or
64-bit pointer width, and delegate that safety gate to the backend for every
capture. The service checks that its activation remains open after each
backend call and discards the result if it closed in flight; closing does not
cancel a backend call that is already blocked.
Closing the Core registry disposes the activation-owned service. A new session
requires a new session-scoped registry.

This bridge does not itself connect to a concrete debugger backend, execute
extension package code, sandbox providers, create target adapters or prove any
operation available. The runtime host must supply the backend and evidence.
Registers, threads/modules, disassembly, breakpoints, watches, snapshots,
restore and execution controls are not dispatched by this slice. It adds no
native-hook capability and cannot re-enable KPM. The existing release manifest
and resolved-profile APIs remain the distribution-declaration and desired
profile boundaries; this bridge does not replace them or turn either one into
runtime activation.

Release declarations use `telos_core.release_manifest.ReleaseManifest`. Its strict,
bounded schema holds identity, compatibility, composition and permission claims.
Parsing a declaration does not establish publisher trust or tested compatibility.
`telos_core.artifact_cache.ArtifactSpec.from_manifest()` carries the declared size
and exact release digest into the local cache. `ArtifactCache.put_bytes()`,
`put_file()` and `put_stream()` verify those bytes before publication;
`read_verified()` returns an exact verified byte snapshot. No URL is fetched,
archive extracted, package installed or extension activated.

The cache requires a pre-existing trusted POSIX directory with cooperating
writers. It pins the directory identity, refuses unsafe leaf entries and uses
atomic publication with file and directory flushing. It does not defend against
a hostile owner or ancestor replacement. Its size bounds apply per operation;
there is no total-cache quota, garbage collection or stream callback deadline.
The manifest and cache remain separate from caller-approved desired profiles;
no automatic dependency resolution or permission admission is supplied.

A supplied desired profile can now be checked against its exact release
manifests using `telos_core.profile_admission.bind_profile(profile, manifests)`.
This pure consistency gate requires one manifest per selected extension with the
same identity, version and artifact digest. Required dependency references and
conflict declarations must match the manifests; selected dependency versions
must satisfy the declared inclusive-minimum/exclusive-maximum SemVer ranges.
Capability grants must be a subset of declared name/permission pairs.

`ProfileBinding.composition` keeps required dependencies, present optional
references and present pack members separate. These are same-profile present
references, not explicit installation intent or activation; no selection switches
are inferred. Missing optional dependencies and
pack members remain unselected; present ones must satisfy their declared ranges.
Neither category is silently promoted to a required runtime edge. The binding
handles at most 128 selected extensions and manifests and has no filesystem or
network effects. `release_manifest.version_in_range()` exposes the same bounded
SemVer comparison without duplicating the manifest parser.

A binding does not resolve a package closure, verify artifact bytes or publisher
identity, approve grants or file/network/native-code permission requests, check
platform/loader compatibility, install code or acknowledge runtime activation.
Those gates remain separate. The existing profile store continues to accept
unbound desired revisions; callers must explicitly use this consistency gate
when joining profiles with release declarations. This source addition has not
been built or exercised through tests or an installed consumer in this pass.
