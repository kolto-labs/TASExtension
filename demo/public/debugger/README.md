# Debugger session contract

## Static binary reader source

`static-binary-backend.js` supplies a trusted-host factory for an immutable
local Blob. It uses the existing bounded header parser and returns a backend
manifest, handlers, target-scoped static-analysis declarations and a close
lease. It provides `binary.identity`, `binary.sections`, `binary.dependencies`
and `binary.bytes`. Partial or unavailable tables remain not-qualified.
File addresses and flags are declared metadata, not live mappings. Dependency
scope remains the parser's PE import or ELF DT_NEEDED scope; it does not prove
that libraries exist, load or satisfy all runtime dependencies.

The target retains the header parser's declared pointer width when available.
For recognized files up to 128 MiB, the factory calculates full-file SHA-256
locally through Web Crypto and includes it in the bound target and identity
rows. Missing Web Crypto, larger files or a failed hash leave the digest null
with an explicit reason; metadata inspection still works. Cancellation is
checked before and after the file read and digest, and the owner checks selection
revision before attachment. A digest identifies the selected file bytes, not
the executable loaded by a game VM. It grants no game profile or runtime support.
The factory returns a `file-inspection` adapter for a recognized format;
architecture and ABI may remain unknown. Unrecognized format returns no adapter
manifest. This mode reports matched-inspection and cannot qualify runtime
controls, mutations or process-scoped reads: every available owner must declare
target scope and static-analysis for an explicitly recognized read operation.
Runtime mode remains the default and retains format/architecture/ABI identity
requirements. Host registration, all four actual
owner reports, transport binding and selected-service attachment are still
required. The factory creates no permission reports or file/URL handler loader.
Metadata requests require the bound session/revision/target, available target
scope, static-analysis evidence, cancellation and empty parameters. Byte
requests instead require exactly `offset` and `length`, safe integer file
offsets and a complete range of 1–4096 bytes. Only that Blob slice is allocated;
the owner and cancellation are checked before and after the read. Closing the
factory refuses further reads; the host must also unregister/revoke its broker
and advance the revision when selecting a different file. Uploaded bytes never
provide executable handlers.

`static-file-owner.js` now provides the Core-owned `debug.file-inspection`
service. The Debug package's local picker requests a new immutable file binding;
Core prepares registries, local dispatch, an identified target adapter and
read-only metadata and bounded-byte policy before attaching the broker. Each layer's
report is bound to that file session/revision. Backend readiness reflects
metadata table completeness; this does not qualify runtime compatibility.
Failed or superseded parsing preserves the prior selection. Explicit close
detaches only this service's lease; another owner's newer selection is retained.
Selection revision, selected session and normalized target identity are captured
before parsing and checked before attachment. A game lifecycle change or another
owner's selection cancels the stale file result. Attachment also checks for a
newer selection made synchronously by a selection listener. This is source
implementation; these interruption paths have not been live-qualified.
The previous game VM, captures and TAS controller are not controlled by this
service. Disabling Debug cancels pending parsing but retains a completed file
selection. No file bytes are uploaded or persisted by this service.

On 2026-10-12, the retained no-game localhost workspace selected the actual
K1 startup executable through the normal file chooser and captured 18 header
rows, four PE sections and eleven import-library names through the three
Refresh controls. Selecting the directly requested K2 executable cleared the
prior displayed captures before new reads; it then captured 18 header rows,
four PE sections and fifteen import-library names. Both files were reported
as PE32/x86/Windows/little-endian, with runtime compatibility not qualified and
full-file hashes not calculated. This is observed local file selection and
metadata capture, not native execution, replay or other-game compatibility.
Cancellation, interrupted reads, non-PE formats and execution backends remain
unqualified. The live K1 game VM and recording continued independently.

On 2026-10-13, the inspection-only workspace selected the same actual K2 file
through its normal local chooser. Refresh captured 20 identity rows, including
declared width 32 bits and complete-file SHA-256
`6a522e71631dcee93467bd2010f3b23d9145326e1e2e89305f13ab104dbbffef`.
The digest matched the selected file on disk. Normal Export captured rows saved
`debug-file-headers-2026-10-13T03-56-24-480Z.json`.
This qualifies the inspected PE32 file's identity capture only. Hash failure,
size-limit and cancellation behavior remain source-only; no other platform or
runtime support follows. The recorded K1 guest was not reloaded.

PE and ELF layout source now supplies an explicit address-scope description:
PE relative virtual addresses and ELF declared virtual addresses are not
captured runtime mappings. This change has not been loaded in those retained
file selections.

The existing header parser now reads bounded thin Mach-O segment/section and
dylib-name tables in either byte order, with explicit 32/64-bit command sizes,
alignment/range checks, 256-command/section bounds and 512 layout entries.
Zero-fill sections have no file bytes. Invalid tables stay partial/unavailable;
partial captures cannot qualify their backend readers. Fat binaries retain
header-only slice classification, without combining slice layouts. Dependency
names do not resolve rpaths, symbols, filesets or libraries loaded at runtime.
Source layout facts follow Apple's [Mach-O loader definitions](https://raw.githubusercontent.com/apple-oss-distributions/xnu/main/EXTERNAL_HEADERS/mach-o/loader.h).
No real Mach-O file or platform runtime has qualified this source extension.

WebAssembly metadata now captures up to256 section file spans and bounded custom
names. Unsigned lengths are limited to five-byte/u32 encodings, spans stay
inside the file, and custom names require valid UTF-8 within the inspection
limit. Payloads are skipped. Addresses, memory sizes, host architecture, ABI
and pointer width remain unavailable; section contents/order/imports and module
validity are not checked. Unknown section IDs are retained as labels, without
claiming their semantics. These facts follow the official
[binary section format](https://webassembly.github.io/spec/core/binary/modules.html#sections).
On 2026-10-13, the normal local file picker selected the actual 5,279,145-byte
BoxedWine WASM artifact. File headers and 12 section file spans were captured,
as were the first 128 bytes in eight hex rows through File bytes. This qualifies
those selected-file views for this artifact. It does not qualify module validity,
execution, host architecture, ABI, pointer width or runtime compatibility.

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
authenticate report producers. The literal evidence ID `none` is reserved for
resolver-generated missing-layer placeholders and cannot be claimed by a
report producer. The
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

The shared vocabulary covers execution and input, processes/threads/modules,
memory maps/read/search/write/watches, registers and stack, disassembly and
static analysis, symbols/source lookup, breakpoints, expressions, traces and
events, and snapshots/restore. Stable IDs include `memory.read`,
`registers.read`, `execution.step`, `breakpoints.add`, `symbols.resolve`,
`input.timeline`, `state.snapshot`, and `state.restore`. A vocabulary entry is
only a place to report a capability; missing evidence remains unknown or
unavailable and never creates a command route. This module only validates and
intersects assertions; it does not dispatch operations. Runtime owners remain
responsible for enforcing bounds, target/session/revision matching, pause
barriers, cancellation, and mutation admission when they implement a command.

## Host command broker

`debugger-backend-registry.js` catalogs trusted host-installed operation
handlers by backend ID, API version, supported adapter IDs and operation IDs.
Registration accepts in-process host code only; it has no file or URL loader.
Unregistering a backend revokes its current brokers, aborts their in-flight
signals and requires new brokers if that backend is later registered again.
`debugger-operation-broker.js` adds the shared admission boundary for these
backends. It binds a broker instance to one matched declarative adapter,
normalized target identity, session revision, four live-runtime reports,
registered backend, and transport. Each call is admitted only when all four
owners qualify the operation and the reports name the bound transport,
backend, adapter, and host policy. Recorded-journal and simulator evidence
cannot authorize mutations or live runtime controls. An explicit inspection
allowlist can retain recorded-journal, simulator, static-analysis or
captured-memory evidence when all four owners qualify that operation and match
the selected target/revision. It includes process/thread/module/register/stack
inspection, memory reads/maps/search/watch lists, code and symbol/source views,
breakpoint lists, bounded expressions, strings, traces, events and input
timelines. It does not include writes, input application, execution/state
controls or breakpoint/watch mutations. Those and unknown custom operations
require live-runtime evidence from every owner; an otherwise-available route
without it becomes not-qualified. This is admission metadata for trusted
handlers, not a sandbox that proves a handler is read-only. The broker accepts stable operation IDs and
plain JSON requests bounded to 64 KiB, 10,000 nodes and depth 16. It copies
handler results into frozen plain JSON bounded to 1 MiB, 150,000 nodes and depth
24; non-JSON values and oversized results are rejected. Backend handlers
translate requests to transport commands.
Array lengths are checked against the remaining node budget before key
enumeration or copying. Property counts are bounded before traversal, and the
byte budget includes JSON delimiters, separators and null values. The final
serialized-byte check also accounts for escaping.
Each dispatch rechecks the selected session, revision and target through a host
binding callback. The first observed binding mismatch permanently revokes that
broker and aborts outstanding signals; returning to the earlier binding does
not revive it. Failure to read the live binding also revokes it. Cancellation
is rechecked after request validation and before the handler is called. Hosts
must still close the broker when capability evidence or policy changes. Backend
removal also revokes the broker and aborts outstanding signals. After an asynchronous handler returns,
the broker checks the live binding and backend again and discards the result if
the session changed, the broker closed, or the backend was removed. A handler
that ignores cancellation therefore cannot publish a stale result through the
broker, though the transport owner must still enforce cancellation and mutation
safety itself.

```js
import { createDebuggerOperationBroker } from './debugger-operation-broker.js';
import { DebuggerBackendRegistry } from './debugger-backend-registry.js';

const backends = new DebuggerBackendRegistry();
backends.register({
  schema: 'telos.debug-backend/v1',
  id: 'backend.gdb-remote', version: '1.0.0',
  api: { id: 'telos.debug-backend', major: 1, minor: 0 },
  adapters: ['publisher.example-game'], operations: ['memory.read'],
}, {
  'memory.read': ({ transport, payload, context, signal }) =>
    transport.request({ operation: context.operation, payload, context, signal }),
});

const broker = createDebuggerOperationBroker({
  session, revision, target, reports, adapterRegistry, adapterId: 'publisher.example-game',
  backendRegistry: backends, backendId: 'backend.gdb-remote',
  getCurrentBinding: () => ({ session: activeSession.id, revision: activeSession.revision, target: activeSession.target }),
  transport: { id: 'host.loopback', request: sendDebuggerRequest },
  policyOwner: 'host.debug-policy',
});
const bytes = await broker.execute('memory.read', { address: '0x1000', length: 64 });
broker.close();
backends.unregister('backend.gdb-remote');
```

The broker is not a transport, debugger backend, policy authority, adapter
loader, or proof that a report producer is authentic. Hosts must bind those
trusted implementations and independently enforce command-specific address,
pause, mutation, and cancellation safety. Adapter manifests remain
data-only. No current KOTOR, KOTOR II, BoxedWine, or native backend is connected
to this broker, and it does not change any runtime capability or support claim.

Evidence source is retained per report as `live-runtime`, `recorded-journal`,
`simulator`, `static-analysis` or `captured-memory`; consumers must not relabel
one as another. These source labels do not install or qualify a backend. In particular,
simulator observations do not establish live-game memory, and recorded inputs
or replay do not establish snapshot/restore capability.

## Current implementation boundary

This module is wired into the static browser workbench as a local, declarative
manifest importer. The user can inspect executable metadata, calculate
SHA-256 locally for files up to 128 MiB, load a bounded JSON adapter manifest,
and see whether the declared target identity matches and whether the selected
runtime reports its prerequisites. File contents stay in the browser; the
digest is identity metadata, not proof of safety, execution compatibility, or
debugger support. Registration does not load code, launch a game, or grant an
operation. Header identity without a calculated digest cannot establish an
exact executable build. The registry is session-local and currently
has no built-in K1/K2 or other game adapters. This does not change the current
K1/K2 profile registry or add a native backend. The current BoxedWine
operations remain limited to what that selected runtime handshake actually
reports. Live register access, disassembly, breakpoints, memory writes,
coherent snapshots, restore, and runtime support for arbitrary uploaded games
remain unsupported or unqualified until their owning backends and target
adapters implement and qualify them.

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
The browser's `runtime-handshake` projection uses the shared canonical IDs in
`debugger-contract.js` (`execution.pause`, `memory.read`, and so on). Known
legacy handshake keys are mapped to those IDs; operations with no matching
runtime advertisement remain `unknown`. The separate `advertisedCapabilities`
object retains the browser owner's host-specific keys for lossless inspection.

Activity entries require ISO `receivedAt`, string `session`, `label`, `text`,
and boolean `error`. Native entries additionally carry position, durable
position, unit, revision and take. Native `backendDetails.nativeOwner` retains
`durablePosition`, `take`, and the original action projection (schema plus
action ID, label, enabled, reason, checked and shortcut). Browser
`backendDetails.browserRuntime` retains `capabilitySchema`, `renderedImages`,
`activeWorkers`, and `parkedWorkers`; absent counters are null. Its optional
`debugRequestService` records the session-matched `telos.debug-adapter-status/v1`
operation states and their adapter owner/source/unit/reason. This is the static
request router's report, not the four-layer `capabilityQualification` and not
proof that a command was executed or that a native backend is equivalent.

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
