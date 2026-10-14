# Next Telos Core integration slice (U19/U20)

Status: source inventory and integration draft, 2026-10-03. No new runtime API,
installer, consumer execution or native acceptance is delivered by this document.
All product milestones remain open. Routine remains the live critical path.

Latest ownership requirement: the platform's display name is **TelosCORE**. The
lightweight runtime and shared overlay shell belong to Core; TAS is a separately
versioned package installed onto it. Generic panel/layout/theme/transparency and
input-routing services must work with TAS absent. TAS contributes its timeline,
piano roll, authoring/transport and holorecord workflows through Core APIs. The
current overlay under `kptas.telos` must be extracted with compatibility adapters
and one execution owner retained. This draft does not establish that extraction,
separate dependency manifests or a standalone overlay have shipped.

## Existing boundaries

`telos_core` is dependency-free and independently packaged. Its public facade
exports typed identities, API versions, capabilities, outcomes, `Registry`,
`Activation` and bounded subscriptions. `Registry.negotiate()` checks supported
major/minor, advertised required capabilities and caller-supplied grants before
invoking the provider factory. `Activation.own()` and `close()` track and dispose
local resources. These are local declarations and lifecycle controls: a caller's
supplied grant is not an authenticated permission decision. There is no action
dispatch, branch revision admission, native state capture/restore or panel host.
Events belong to the activation; an independently connected game observation
stream requires an adapter rather than a second owner.

`ReleaseManifest.from_bytes()` validates release declarations;
`ArtifactSpec.from_manifest()` and `ArtifactCache.read_verified()` bind exact
artifact bytes to the declared size/digest. They establish byte identity, not
publisher trust, archive extraction safety or runtime compatibility.
`ResolvedProfile.dependency_order()` orders an already resolved exact closure.
`ProfileStore.load()/save(expected_revision=...)` publish desired profile
revisions with compare-and-swap and durable POSIX storage. Saving an older profile
again appends a desired revision; it does not undo runtime activation. There is
no dependency-range solver or admitted-runtime receipt here.

## First complete source slice: observation consumer contract

Use the existing facade for a small, offline observation consumer example and
its explicit event schema. Select new example/schema documentation files before
implementation; do not import KPTAS, Qt, native pointers or a catalog into Core.
The provider adapter must supply immutable observations containing session,
take, committed tick, revision and capture generation. A consumer keeps the
identity and reports bounded subscription overflow rather than silently claiming
complete history. Disposal closes subscriptions and owned adapter resources.
The example must label its supplied observations as illustrative until connected
to the authoritative owner; it must not display a synthetic frame as gameplay.

Source acceptance owner: API lane plus independent reviewer. Check only public
facade use, supported API negotiation, explicit overflow handling and closure.
Actual independently installed consumption and native event fidelity remain
root-owned later gates under the current no-test/no-build constraint.

## Permitted actions and exact state: dependency before example

Do not implement a permitted-action example by passing a mutation capability to
`Registry.negotiate()` alone. Add a separately reviewed admission adapter contract
first: root-approved grants, expected session/take/revision/generation, bounded
request identity and existing owner dispatch. Unknown or stale context refuses
before execution; an accepted request and its completed owner acknowledgment are
distinct outcomes. No caller gets a native handle or a second live controller.
TAS semantic restrictions remain enforced by the canonical owner.

Each stateful extension must declare captured state identity/version, exact
artifact/configuration digests and whether it affects execution. Missing required
state or an unsupported state migration must block exact restore before any
runtime mutation. Stateless observation consumers can declare no execution state;
that declaration requires review rather than inference from a provider factory.
U17 native load/unload limits and the existing authoritative state/restore
boundary must be inventoried before selecting adapter source files.

## U20 preview/apply/undo integration

Start with caller-supplied exact resolved releases and already verified offline
artifacts. A preview records current desired revision, proposed exact closure,
configuration/grants, environment constraints and exact manifest/artifact inputs.
Its digest covers every input used by apply. Apply rechecks these inputs and the
current profile revision under a profile-scoped transaction lock; a changed input
requires a new preview. Existing ProfileStore CAS is a component of that
transaction, not a lock spanning cache, installation and activation.

Keep three inspected states separate: desired profile revision, verified installed
artifacts and admitted runtime closure. Do not label ProfileStore.save() as an
installation or activation. A durable receipt needs an idempotency key, exact
preview identity, previous desired/runtime closures, per-release verification and
activation outcomes, pending restart and recovery state. Receipt/storage design
must be reviewed before wiring a mutating implementation.

Undo previews an older exact desired closure against current inputs and recording
pins, then uses the same transaction/admission path. Preserve artifacts required
by recordings or rollback. Activation failure must retain the last admitted
closure and truthful desired state; native unload limitations may require an
explicit restart rather than claiming immediate rollback.

Acceptance owners: package lane and independent reviewer for source; root for
isolated installation, interruption/recovery and native activation. Dependencies
still open: range solving, publisher metadata trust/expiry/revocation, extraction
and destination safety, admission adapter, recording-pin integration, platform
atomic publication and real installer recovery. No cryptography, network fetch,
remote native mutation or public deployment is introduced by this draft.
