# Debugger session contract

`debugger-contract.js` is a data-only contract for uploaded-game debugger
targets. It is independent of the page workbench, the KOTOR profiles, the
BoxedWine bridge, and the native injected backend. A host may pass parsed
binary identity into `DebugTargetAdapterRegistry`; this does not load code,
probe a process, identify a game from its filename, or enable a debugger
operation.

## Target adapter registration

An adapter manifest describes the binaries it can interpret and the operation
identifiers it requires from a separate runtime. Matching checks only explicit
target identity fields. A profile-level match is not an exact-build match;
exact-build matching requires an explicit SHA-256 allowlist.

```js
import { DebugTargetAdapterRegistry, evaluateAdapterRequirements } from './debugger-contract.js';

const adapters = new DebugTargetAdapterRegistry();
adapters.register({
  schema: 'telos.debug-target-adapter/v1',
  id: 'publisher.example-game',
  version: '1.0.0',
  api: { id: 'telos.game-adapter', major: 1, minor: 0 },
  targets: [{ format: 'elf', architecture: 'x86_64', osAbi: 'linux' }],
  requires: ['memory.read'],
  interprets: ['symbols'],
});

const match = adapters.match(parsedBinaryIdentity);
// `matched-profile` can describe interpretation compatibility only. It does
// not mean that memory.read or any other runtime operation is available.
```

The manifest is declarative metadata. It cannot contain callbacks, commands,
permissions that grant execution, or native pointers. The current registry is
intentionally empty; KOTOR/KOTOR II or another game's adapter must be added
only after its binary matching rules are reviewed.

## Capability reports

The transport, backend, target adapter, and host policy report independently
under the same session ID, revision, and normalized target identity. Every
operation assertion includes its state, owner, reason, and evidence ID. These
are owner-authored evidence metadata; the contract does not cryptographically
authenticate report producers. The
resolver returns `available` only when all four reports say `available`; a
missing or invalid layer produces `unavailable`. An explicit unsupported or
unqualified layer is preserved. Differing operation units fail qualification.

```js
import { resolveDebuggerCapabilities } from './debugger-contract.js';

const effective = resolveDebuggerCapabilities({
  session: 'session-id',
  revision: 7,
  target: parsedBinaryIdentity,
  reports: {
    transport: transportReport,
    backend: backendReport,
    target: targetReport,
    policy: policyReport,
  },
});
// `effective.schema` is telos.debug-capability-resolution/v1, not the full
// negotiated telos.debug-session/v2 envelope described by the design spec.
const prerequisites = evaluateAdapterRequirements(match, effective);
```

Operation identifiers use namespaced stable IDs such as `memory.read`,
`registers.read`, `execution.step`, `breakpoints.software`,
`snapshots.capture`, and `snapshots.restore`. This module only validates and
intersects assertions; it does not dispatch operations. Runtime owners remain
responsible for enforcing bounds, target/session/revision matching, pause
barriers, cancellation, and mutation admission when they implement a command.

Evidence source is retained per report as `live-runtime`, `recorded-journal`,
or `simulator`; consumers must not relabel one as another. In particular,
simulator observations do not establish live-game memory, and recorded inputs
or replay do not establish snapshot/restore capability.

## Current implementation boundary

This module establishes reusable identity matching and capability negotiation
for future uploaded-game adapters. It is not yet wired into the KOTOR page or
native debugger, and it does not change the current K1/K2 profile registry.
The current BoxedWine operations remain limited to what that selected runtime
handshake actually reports. Live register access, disassembly, breakpoints,
memory writes, coherent snapshots, restore, and runtime support for arbitrary
uploaded games remain unsupported or unqualified until their owning backends
and target adapters implement and qualify them.

## Saved inspection envelope

Both native and browser inspectors export `telos-debugger-inspection/2`.
Version 1 used incompatible `actionProjection` and `operationProjection`
shapes; it must not be interpreted as version 2. These files are observations,
not a command stream, executable profile, state restore, or capability grant.
The data-only capability resolver above is a separate contract.

All top-level fields below are required. `null` means evidence is absent; it
does not mean zero, false, unsupported, or equivalent to another host.

| Field | JSON type and meaning |
| --- | --- |
| `schema` | Literal `telos-debugger-inspection/2` |
| `exportedAt` | ISO 8601 UTC string; export time |
| `session` | String; the owner/session whose observations are included |
| `source` | String describing the evidence source |
| `target` | String, object, or null; the host's target profile/identity, preserved without inventing a normalized binary identity |
| `backend`, `release` | String or null; observed runtime identity, if supplied |
| `executionBoundary` | String, object, or null; owner's boundary descriptor, not an inferred game frame |
| `observation` | Observation object below, or null when no current-session status was received |
| `advertisedCapabilities` | Object mapping host capability IDs to booleans or null; declarations only, not qualified effective permissions |
| `operationProjection` | Object with literal schema `telos-inspection-operations/1`, string `scope`, and `entries` array |
| `backendDetails` | Object containing `nativeOwner` or `browserRuntime`; backend-specific evidence |
| `activityOrder` | Literal `newest-first` |
| `activityLimit` | Positive integer; current hosts retain 128 native / 40 browser entries |
| `activity` | Array of current-session observations, no longer than `activityLimit` |
| `memory` | Captured-memory object below or null |
| `recordedState` | Recorded-journal object below or null |

An observation has all these keys: `receivedAt` (ISO string or null), `state`
(host-defined string or null), `ageMilliseconds` (nonnegative finite number or
null), `fresh` (boolean), `position` (integer or null), `unit` (boundary string,
object, or null), `revision` (owner revision or null), `unknownState` (boolean
or null), and `error` (host error string or null). Fresh means receipt age is
less than 5000 ms, not that the guest state has been independently qualified.
Native receipt age uses a monotonic clock and includes unchanged-position
acknowledgements. Browser receipt wall time is reconstructed from its monotonic
age at export. Browser `position` remains null: a presented-image count is not
the native game position. Consumers must preserve large integer positions and
revisions exactly and never compare positions across differing owners/units.

Each operation entry requires `id` (string), `kind` (`owner-action` or
`runtime-operation`), `state` (`available`, `unsupported`, `unavailable`,
`not-qualified`, or `unknown`), `enabled` (boolean or null), and `unit`
(string or null). IDs are scoped by `kind` and projection `scope`. Native
`enabled` is current action admission from its UI owner; its `state` is
`unknown` because that admission is not a runtime qualification report.
Browser `state` comes from its runtime handshake and `enabled` is null because
the export does not reconstruct current UI action admission. Neither field
may be substituted for the other or used to enable operations on import.

Activity entries require ISO `receivedAt`, string `session`, `label`, `text`,
and boolean `error`. Native entries additionally carry position, durable
position, unit, revision and take. Native `backendDetails.nativeOwner` retains
`durablePosition`, `take`, and the original action projection (schema plus
action ID, label, enabled, reason, checked and shortcut). Browser
`backendDetails.browserRuntime` retains `capabilitySchema`, `renderedImages`,
`activeWorkers`, and `parkedWorkers`; absent counters are null.

Browser memory contains capture `identity`, ISO `receivedAt`, integer `pid`,
`address`, `length`, literal `captureState: paused`, literal `encoding: hex`,
and hexadecimal `bytes`. It is a prior capture tied to its own identity, not
necessarily the latest observation. Native recorded state contains `position`,
`take`, `stateRoot`, `journalHash`, and an array of display `fields`; it is
included only when its session/take/position/revision matches the current
acknowledged boundary and state is certain. Native exports null memory;
browser exports null recordedState. Export never triggers a new capture.

## Portable presentation state

Both hosts emit `telos-shared-presentation/v3`: `presentation` is `overlay`
or `surround`, `panels.game` requires boolean `visible`, boolean `collapsed`,
and finite `opacity` in 0.55–1. `panels.timeline` requires finite `opacity`
in the same range; it maps to the native transport panel and browser transport
dock without changing either host's density, visibility or collapse state.
`hostProfiles` holds opaque host-named
JSON data. The native game preview and browser game dock apply those values
through their existing panel owners; expanded geometry is retained while
minimized. Opaque host data is retained for re-export, never executed/applied.
Both hosts also accept `/v2` (game only) and `/v1` (game without
`collapsed`). Older imports retain the receiving timeline opacity; `/v1` also
retains the receiving game's minimized state. New exports always use `/v3`. The existing browser storage key is retained so saved `/v1`
host data remains recoverable. Profiles stay capped at 64 KiB.

Transport panel collapse is still host-specific: the native density selector
also owns compact/minimal rail visibility, while browser dock collapse hides
a whole transport widget. Mapping these without that density distinction would
misrepresent the presentation. Full geometry, shortcut maps, input ownership,
game pixels and runtime execution remain outside this shared profile.
