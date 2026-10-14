# Debug runtime package

## Resolve a paused process address

`createGuestMappingAdapter()` routes `memory.mapping` with exactly `{pid,address}`
through the existing game owner and origin/session checked bridge. Availability
requires the explicit `addressMapping` flag and native metadata ABI v1. The
producer requires a complete pause and nonblocking control/mapping locks, then
copies a retained backing-file metadata snapshot without reading guest pages.
The shared normalizer preserves exact 64-bit hex values, distinguishes offset
zero from null and requires the mapping to contain the requested address.
Static file owners gain no process mapping capability. Hosted broker adapters
use their existing owner-report intersection; the original guest adapter uses
the trusted runtime/controller. This source contract has no live qualification
yet and does not supply an arbitrary uploaded-game backend.

The production browser candidate `b23307d0` includes a bounded paused-input
bookkeeping export. It copies nine native counters under the complete pause
barrier, covering the active X-event batch, generated/consumed events and prior
completion. The bridge may include this capture in an actual input-timeout
message. Missing exports and refused captures remain explicit. These counters
describe the runtime's X-event queue; they do not identify Windows game input
handling, a game frame or deterministic replay. This candidate began a separate
localhost launch, but no first image or diagnostic capture was accepted before
browser access was lost. It remains unqualified. The export does not add an `input.timeline` adapter, a
generic backend, or snapshot/restore support. Existing guests retain their
original exports and bridge code.

The broker service exposes `getTarget()` only while its bound broker is active.
This gives inspection clients the normalized target layout without inventing
execution status for a static or recorded backend. The UI may admit available
snapshot reads from declared non-live sources; live reads still require the
owner's paused boundary. This does not install a backend or qualify another game.

`telos.debug` is optional and disabled/uninstalled by default. TAS is independent.
The current workspace shell registers the debugger UI package and provides a
session owner through `debug.runtime`. It creates `createDebugRuntime()` with
the existing BoxedWine guest-memory and execution adapters. It routes process
and memory reads, pause, resume, and render-step requests through its API. The
Execution panel uses these service routes, waits for completion evidence and
cancels its pending wait when disabled or switched to another session. The
page workbench and the rest of the shell retain their existing session bridge.

`createGuestThreadAdapter()` provides `threads.list` with exactly `{pid}` through
the same session owner. It requires the runtime's explicit `threadList` flag.
The backend copies at most 4096 registered workers under the control mutex,
requires the full pause barrier, and refuses a partial list. The bridge filters
by guest PID and copies rows before freeing its buffer. Rows contain guest
thread/process IDs and `paused` or `kernel-wait` state. They do not identify the
wait cause or provide registers, instruction locations or host pointers. The
Threads panel labels location as unavailable and presents an explicit capture.
The process picker also accepts an optional guest executable basename from a
new paused-only export. It copies at most 1024 bytes under the same control
mutex/barrier and refuses truncation. Paths and command lines are not returned.
Missing exports, invalid UTF-8 and control characters leave the name unavailable.
A Wine executable name is not the Windows game's identity. This source addition
needs a coordinated build and fresh candidate qualification.
This source path needs a runtime rebuild and live qualification; it does not
establish native or arbitrary uploaded-game support.

Process rows optionally carry `presentationOwner:true|false|null`. Null/missing
means attribution is unavailable; false means the reported owner is another
listed process. At most one row can be true. The new BoxedWine WASM export reads
the X11 display PID associated with its mapped presented GL window under the
complete pause barrier and rechecks the window binding. It returns no paths,
command lines or window titles. Missing windows/displays/processes leave the
attribution unknown; a lost barrier refuses the capture. This is presentation
ownership, not a verified Windows executable or build identity.

The iframe and owner validate the bounded ID/nullable boolean fields before
the shared process picker displays them. When its selected PID is absent, the
picker can prefer the reported owner; an existing valid PID selection remains
unchanged. Old runtimes without the export report attribution unavailable.
On 2026-10-11 production candidate `871b1c7c` reached the original title and its
real Processes panel captured 16 rows under a 49/49 pause barrier. PID 10
(`wine`) was the sole reported presentation owner and was selected instead of
the first hash-table row. Its Registers panel captured 81 rows across nine
threads. This qualifies the observed association/capture path only, not Windows
executable identity, absent-window/export cases, native or another-game support.
Other adapters must supply their own qualified association;
filenames, process order and header inspection cannot invent it.

## Host integration

`createGuestRegisterAdapter()` routes `registers.read` with exactly `{pid}` to
the existing session owner. A new paused export copies a complete selected-PID
list of at most 128 registered workers while holding the control mutex and
requiring every worker parked, with no step/run permit. It refuses missing CPUs,
unknown processes and oversized/incomplete captures. Each row contains saved
x86 EIP and eight general registers. Kernel-waiting EIP is saved CPU state;
it is not a wait-cause diagnosis or a guaranteed next instruction. Flags, FPU,
SIMD, register mutation, instruction stepping and restore are not provided.

The bridge validates IDs/states and copies bounded register values before
freeing its WASM buffer. The owner validates the exact x86 record shape. The
Registers UI uses the request service and displays thread IDs with named saved
values. Its service response contract is `{pid,captured:true,architecture,
semantics:'saved-guest-registers',threads:[{id,pid,state,registers:[{name,value}]}]}`;
values are hexadecimal strings. The generic view accepts at most 128 threads,
32 named registers per thread and 128-bit values. Native or other adapters must
return their actual saved values and owner-qualified operation. The UI contract
does not supply a backend. Future runtime changes need a coordinated build and
fresh candidate qualification; existing sessions gain no export from a source
edit. On 2026-10-11 the preserved 67cfe947 candidate's real Registers panel
captured 36 rows for guest PID 76 under a complete 50/50 pause barrier: four
threads with saved EIP and eight general registers each. The process basename
was wine-preloader; it did not establish Windows game identity. This qualifies
that selected guest capture path only, not flags/FPU/SIMD, native or another-game
backends, the loading cause, a next instruction, mutation or instruction steps.

The shared UI also accepts `saved-target-registers` for a trusted target
adapter. Captured thread state is now displayed rather than discarded;
paused, kernel-wait and stopped remain distinct, and missing state is
Unavailable. These display semantics do not grant capture readiness or
establish a live execution state for a snapshot backend. Existing guest
adapters keep their original payload semantics. This source revision has not
been loaded or qualified, and no native or other-game adapter was installed.

Create one `createDebugRuntime({getSession, adapters})` for a session owner.
Registration copies bounded JSON operation declarations and binds the adapter
identity. Readiness is sampled from the live owner; changing the original
declarations or identity cannot relabel an in-flight reply or change its step
unit. Replace the service to register changed declarations or a new target.
An explicit registered unsupported, not-qualified or unavailable state is a
qualification ceiling. A readiness callback cannot promote it to available.
Operation declarations may identify process or target capture scope. Missing
scope preserves the process default; invalid explicit scopes refuse registration.
An unresolved null scope is retained only on explicitly non-available routes
and is displayed as Unavailable, never guessed as process or target scope.
Four-layer broker reports normalize and intersect scope, refusing qualification
when available owners disagree. Broker service adapters retain the resolved
scope. The shared captured-row UI admits target scope only for non-live evidence
and requires a PID-free target capture envelope. This enables a future file or
snapshot reader without inventing a process; it installs no such reader.
Missing state retains the existing qualified adapter's readiness behavior;
invalid explicit states refuse registration. The shared UI has a capabilities
panel that reads this inventory without issuing commands. These source changes
have not been loaded or qualified after browser access was lost.
`registerDebugPackage(host, {mount})` receives only that service and
package-scoped storage; disable/removal invokes the returned UI disposer.
`DEBUG_WIDGET_OPERATIONS` identifies the operations each widget needs; it does
not claim that every operation is available. Trusted host adapters may add
namespaced operation IDs; an ID alone does not create a route, and requests
without a registered adapter return `DEBUG_UNSUPPORTED`.
The request service's capability inventory also includes the shared debugger
vocabulary from `debugger-contract.js`, so absent operations appear explicitly
as unsupported instead of disappearing from inspection. This vocabulary
includes module/source views, live breakpoint and memory-watch operations,
execution traces, runtime events, and input timelines. KOTOR currently reports
several of these as unsupported or unqualified; the vocabulary does not add a
backend route or runtime support.

`createBrokerAdapters(broker)` adapts a session-bound, four-layer-qualified
command broker to this runtime's per-operation adapter interface:

```js
const adapters = createBrokerAdapters(broker);
const runtime = createDebugRuntime({ getSession: () => game.session, adapters });
```

For a single-session owner, `createBrokerDebugRuntime(broker)` composes those
two calls and returns the same request/capability service. Replace the service
when the bound session or broker changes; do not retain it across targets.

The factory also accepts `{getStatus}` from trusted host code. This synchronous
callback receives the broker's frozen `{session, revision, target}` binding and
must return those same identities with execution `state` and an explicit
`pausedCaptureReady` assertion. The service checks the live broker before and
after reading status, copies bounded display fields, and returns null for
missing, throwing, asynchronous, stale or mismatched status. Paused capture
readiness is true only when the host explicitly reports it while paused;
neither a paused label nor worker counters create that assertion. The UI still
requires its operation advertisement and request-route gates. A host may omit
BoxedWine-specific image and worker counters. This service never reads the
global BoxedWine diagnostic snapshot, installs a backend, or executes a status
probe. Core now provides a separate trusted `debug.session-owner` service for
attaching this service to the debugger UI. A compatible real backend and its
trusted target package are still required.
The current KOTOR page retains its existing guest adapters; this source
integration is not qualification of a native or uploaded-game backend.

The adapters preserve each operation's qualified state, unit, owner layers and
reason. Create a new runtime when the active broker changes. The shell uses
its existing BoxedWine adapters by default; broker selection is source wiring,
not native or arbitrary-game backend qualification.

### Selected debugger ownership

`createSelectedDebugRuntime(fallback)` separates the UI request service from
target ownership. Core exposes its `runtime` as `debug.runtime` and its `owner`
as `debug.session-owner`. A trusted target package with the latter permission
can call `attachBroker(broker, {getStatus})`; uploaded manifests cannot supply
handlers. Attachment validates an active revocable broker before replacing the
previous selection, then closes the prior broker and notifies subscribers.
The returned lease's `detach()` restores the fallback only if that lease still
owns the selection. Revocation leaves the selected service unavailable until
its owner detaches; it never silently redirects requests to the KOTOR guest.

Replies are rejected if the selection, generation or session changes while a
request is pending. UI subscribers cancel pending captures, discard captured
values and comparison baselines, and retain watch definitions and dock layout.
The KOTOR workbench is unavailable while a broker is selected. This selects
debugger service ownership only; it does not replace the game canvas or TAS
controller, install a backend, launch an uploaded executable or qualify another
game. No synthetic attachment or live backend qualification has been performed.

The command broker now admits its explicit inspection vocabulary with valid
non-live evidence when the selected target/revision, installed backend,
transport, adapter and host policy all qualify it. Mutations, runtime controls
and unknown operation IDs still require live-runtime evidence from every owner.
Source labels and inspection status remain in the handler context and response
service. This enables trusted host composition for static or retained evidence;
it does not install a reader, execute uploaded code, or qualify another game.

Broker routes now preserve the backend owner's declared evidence source instead
of labeling every route `live-runtime`. The shared source vocabulary includes
`live-runtime`, `recorded-journal`, `simulator`, `static-analysis` and
`captured-memory`. Journal and simulator evidence retain those labels; they are
not converted to captured memory or live runtime evidence. Missing/invalid
backend source declarations produce a null source and an unavailable route.
Such a route cannot become available through its readiness callback. Service
pause/resume/step still require `live-runtime` plus their completion contract,
so a journal, analysis or simulator route cannot qualify live execution through
this bridge. Otherwise-available broker pause/resume/step routes with those
sources are advertised as not-qualified by the bridge rather than enabled in
the shared UI and rejected only after a click. This is source-level provenance handling, not backend installation
or live qualification of these source categories.

`createGuestMemoryAdapter(game)` uses the existing session/origin-checked guest
bridge for process IDs and memory reads. It delegates bounds and readiness to
the owner and checks the returned byte count. `createCapturedToolsAdapter()`
adds bounded wildcard-byte searching and integer watch arithmetic over explicit
captured values, without JS evaluation or implicit live memory access.
Captured values and static binary addresses remain separate from live addresses.

`createGuestExecutionAdapter(game)` delegates to the same session owner's
`requestVmControl()` relay. Pause and resume take empty parameters. Stepping
requires `{unit:'render-present'}`; this advances to a rendered image, not a CPU
instruction or a proven game simulation tick. Readiness is checked per operation
against the owner's runtime capabilities. The owner allows at most four pending
execution requests and waits up to 15 seconds for normalized command provenance
matching the session, request ID, action, and `vm-control` source. Only a completed
command returns `acknowledged:true`. Failed, timed-out, superseded, cancelled,
stopped, and replaced-session requests reject. Cancellation or expiry ends the
wait; the command may already have been applied and is not rolled back.
No new runtime controller, snapshot or restore route is created. The source
integration has not been qualified through the running page.

## Execution completion contract

The request service and Execution panel now validate
`telos.debug-execution-completion/v1` for pause, resume and step. A trusted
adapter returns `session`, `operation`, the exact service `requestId`,
`acknowledged:true`, `outcome:'completed'`, and
`evidence:{owner, source:'live-runtime', id}`. The evidence owner must equal
the registered adapter ID. Pause/resume use `unit:null`. Step must return the
exact requested unit, which must also equal the route's advertised unit.
Static analysis and captured-memory routes cannot execute these controls.
A handler returning successfully without completion evidence is insufficient.

`createGuestExecutionAdapter()` checks the existing VM command request/action,
source and completed outcome, then wraps that receipt in this contract. Its
`guestReceipt` retains the original provenance. The UI no longer interprets
guest VM command internals and obtains step units from the selected route.
Render steps remain labeled as images; other units retain their adapter label.

The broker adapter forwards the service request ID to the broker. A bound
backend handler receives it in `context.requestId` and must return completion
evidence for its own registered backend ID. The contract is an adapter boundary,
not a new backend or proof that an uploaded game can run. Existing custom
execution adapters must implement this new result contract before using the
request service. Direct broker calls may omit request IDs, but their results
cannot qualify service execution completion without one. No live page has
loaded or qualified this source revision yet.

Requests use `telos.debug-request/v1` with `id`, `session`, `operation`, and
`params`. Replies retain the same identifiers, adapter, source, capture time,
and result. The session is checked before and after asynchronous operations.
The runtime copies request parameters as plain JSON with a 64 KiB, 10,000-node,
16-level bound; it copies replies with a 1 MiB, 150,000-node, 24-level bound.
Cycles, accessors, sparse arrays, symbols, custom prototypes and non-finite
numbers are rejected. Returned data is a frozen copy, never the adapter's
mutable object.
The copy accounts for UTF-8 values, keys, and JSON punctuation as it walks the
input, rejecting oversized strings before encoding them and oversized arrays
before enumerating their keys. A final serialized-byte check includes JSON
escaping. Numeric array properties outside the declared length are rejected.
Unsupported operations return `DEBUG_UNSUPPORTED`, rather than fabricated
registers, threads, breakpoints, maps, or traces. A session owner must still
qualify supported operations and any memory-read pause boundary.

## Backend and REA

`demo/worker/debug-api.mjs` exports an opt-in POST handler. It requires an
explicit authorizer and exact allowed Origin, caps request bytes, rejects
stale sessions, and returns structured codes. No existing route was modified.
The host should log unexpected backend exceptions with its diagnostic system.

REA was inspected directly on 2026-10-11:

- [REA README](https://github.com/morluto/rea): local CLI/MCP orchestration over
  analysis engines, rather than an embeddable browser live-debugger engine.
- [REA CLI](https://github.com/morluto/rea/blob/main/docs/cli.md): `instructions`,
  `decompile`, and `xrefs` address queries support Ghidra/IDA/Hopper providers.
- [REA license](https://github.com/morluto/rea/blob/main/LICENSE): MIT, copyright
  2026 morluto. No REA source was copied into this package.

`demo/worker/debug-rea-adapter.mjs` supplies those static-analysis operations
through an injected local companion runner. The host binds an owned binary
path and SHA-256 to a session; request parameters cannot select filesystem paths,
process commands, or CLI flags. The runner must enforce a 60-second deadline,
2 MiB output cap, cancellation, and invocation without a shell. REA must already
be installed/configured in that local companion. Cloudflare Workers cannot
launch local Ghidra/IDA/Hopper; this adapter is not a hosted execution backend.

## Remaining integration

The shell's guest-memory/execution routes still need live qualification. It also
needs to connect the broker bridge and owner-authored four-layer
qualification to its service. The new paused register capture needs build/live
qualification; instruction-level register control, module lists, memory maps,
breakpoints, stacks, tracing, mutation, snapshots, and restore need real
emulator/backend operations; registering widget metadata does not implement
them. The REA companion runner and artifact resolver also need a host connection.
No tests, builds, server changes, or deployments were performed for this slice.

The request service exposes `getSession()` for hosts that have no KOTOR game
object. It returns the current valid session identity or null. Broker runtimes
return null after their owner is revoked or the binding becomes unavailable.
This accessor reads the existing owner binding and dispatches no command.
The Debug UI can use it independently of the optional legacy `getGame()` and
`mountWorkbench()` hooks. The generic broker still needs a trusted host to
select, compose and register the service; this factory does not load games.
