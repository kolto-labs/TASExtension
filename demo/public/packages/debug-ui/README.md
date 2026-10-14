# Debug UI package

## Capture an address backing file

The Address backing file view requests `memory.mapping` with a selected PID
and an explicit 32-bit hexadecimal virtual address, including address zero.
The adapter must advertise process-scoped support and the capture boundary
must be ready. Replies are validated against the requested address and PID.
The table preserves the complete bounded path, mapping start/extent and exact
64-bit file offset. An unavailable offset remains unknown; offset zero is valid.
The export retains the request, normalized metadata, semantics and receive time
without another backend request. Capture failure retains the previous result.
Session/process changes and cancellation prevent stale completion from replacing
the current view. This source path still requires production build and actual use.
Backing metadata does not establish a Windows module list or executable identity.

## Read sparse GL proxy timings

The shared Execution view can display the SDK's optional sampled GL dispatch
metrics beside paused call counts. On a future localhost launch,
`glDispatchTiming=1` supplies `BOXEDWINE_GL_DISPATCH_TIMING:1`; the SDK samples
one in 64 proxied calls per function. Published hosts ignore this option, and
it does not alter an existing guest. The bridge reads three metric values for
at most the sixteen existing count leaders under the full pause barrier,
then rechecks that barrier. Missing or invalid timing data leaves call counts
and controls usable. Resume and new commands invalidate the capture cache.

The view reports sampled call counts and cumulative caller/callback elapsed
microseconds with the actual sampling interval. It does not extrapolate totals,
subtract elapsed spans, rank unlisted functions or claim CPU time. Waits and
in-flight dispatch accounting can affect these independent counters. Zero
samples do not establish that timing is enabled or that the function was fast.
The existing displayed-view export includes this text without a new request.
On 2026-10-13, separate actual K1 candidate072/session6a2ce8d0 reached the
original title and normal Pause confirmed35of35workers at present35. The
shared view captured interval64 samples for fifteen displayed GL functions.
For example, glTexCoord2f reported1631samples/540232caller-us/7516callback-us;
glColor4ubv1619/507537/7980; glVertex3fv1613/470751/8158. Normal displayed-view
export preserved these readings in debug-execution-2026-10-13T04-29-53-989Z.json.
This partially qualifies capture, normalization and presentation for this
paused guest. Sparse elapsed spans identify a scheduling investigation target;
they do not establish CPU cost, an exact overhead subtraction or a speedup.
Missing/invalid readers, interruption and other backends remain unqualified.
The separately retained recorded8f605 guest was not reloaded to load this path.

Paused GL call counts use the existing native counter exports when they are
compiled into the runtime. The bridge reads at a complete pause barrier,
retains up to sixteen nonzero counters ordered by count/index, and reports the
total counter/nonzero counts and truncation. Names come from bounded native
constant strings, not uploaded game bytes. The snapshot is cached while paused
and invalidated by control commands or a running status. Missing exports,
invalid counters or a changed barrier leave this optional field Unavailable.
These are raw int99 callback counts plus explicitly named host totals, not
timing samples, frame rates or a compatibility grant. No extra GL operation,
error query, deferred callback or polling loop is added. This source readout
has scoped live qualification from paused K1 guests; interruption and other
backends remain unqualified.

Execution retains optional cumulative file-range and SDL-dispatch counters
from the selected runtime's existing status report. File counters describe
range exchanges, bytes and range-read time, cache hits/residency, observed
files, failures, short reads and retries. The page accepts only finite,
nonnegative counters and exposes missing or invalid telemetry as Unavailable.
No extra guest/file requests or polling loop are added. SDL counters come from
the proxy dispatch path in `upstream/platform/sdl/sdlcallback.cpp`. They count
completed proxy calls and elapsed caller/callback spans; direct owner-thread
calls bypass these counters. Caller elapsed includes proxy wait and callback
work. The spans can overlap; they are not a CPU profile or a frame-cadence
measurement.
The native getter loads the three relaxed atomic counters separately; a running
guest can update them between reads. The report is not an atomic timing snapshot
and must not be used as an exact subtraction-based overhead measurement.
The range counters exclude Wine root/archive reads and the initial asynchronous
startup-file preload. The observed-file count covers files entering the chunk
cache; a direct configuration range read can occur while that count is zero.
Do not interpret these as all filesystem activity or total startup cost.

The retained recorded guest has not loaded these additions. A separate actual
candidate startup on 2026-10-12 displayed 2 range exchanges, 1,684 bytes,
46.9 ms range-read time and zero reported failures, with no first game image
yet. Later, at 6:15 elapsed, it displayed 417 exchanges, 4,530,836 bytes,
11,151.9 ms range-read time and no reported failures. SDL reported ten proxy
calls, 109,446 µs caller elapsed and 98,102 µs callback elapsed. This qualifies
the observed telemetry path and nonzero updates only; performance attribution
and cold/warm/repeat acceptance remain open.

Captured row views provide a separate **Export captured rows** action. The
`telos.debugger-row-capture/v1` JSON envelope contains the selected session and
target, operation, evidence source, target/process scope, process ID when
applicable, receipt time, column headings and displayed rows. It exports a
prepared immutable snapshot without another backend read. The rows match the
display's bounded strings; this is not a raw register or binary-file export.
Exports exceeding 4 MiB are disabled without silently dropping rows. Replacing
or clearing a capture releases its Blob URL, and session changes clear stale
exports. The envelope does not establish executable identity, replay or platform
compatibility. Actual K2 header, section and dependency captures were exported
through these controls in a separate localhost no-game workspace on
2026-10-12. The downloaded envelopes retained the static file session and target
scope with null process ID and unknown full-file hash. Runtime row exports,
oversize handling and cancellation remain unqualified.

Register rows now include the backend's captured thread state: Paused, Kernel
wait or Stopped. Missing state is shown as Unavailable; the UI does not infer
thread state from the session's pause label. The shared view accepts the
existing `saved-guest-registers` semantics and the backend-neutral
`saved-target-registers` semantics, retaining the session/PID/source/readiness
guards and bounded register shapes. KOTOR's adapter keeps its existing guest
semantics and explicit states. Capture captions retain the reply's architecture
and distinguish saved guest registers from saved target registers. Architecture
labels are bounded identifiers; they describe the adapter's reply and do not
verify an executable or a target's installed platform. This source change does not install a native or
other-game backend or identify a wait cause or next instruction. The retained
K1 session captured 81 saved register rows from nine threads on 2026-10-12,
distinguishing three Paused threads from six Kernel wait threads at its observed
49-worker pause boundary. That is scoped register/state-display evidence only.

`telos.debug` is a bundled, independently enabled package. Core supplies the
`debug.runtime` service (`getSession`, `getStatus`, `capabilities`, `request`).
An optional `getTarget` supplies the current owner's target layout. When it is
present, an unavailable target does not fall back to an older descriptor.
Legacy `getGame` and `mountWorkbench` hooks remain optional. The package owns its dock
registrations, polling, captured-address watches, and package storage. It does
not own the game lifecycle. Disabling it suspends rendering/status work and
makes its panels unavailable without terminating the game or deleting layouts.
The workbench mounts in the stable workspace container supplied by Core, never
inside the game owner's replaceable content. This preserves it when Debug
activates before the dock or before the game opens.
Target adapter file reads use a selection revision. A superseded read or a
completion after workbench destruction cannot register an adapter or replace
the current import status. Accepted manifests remain declarative metadata.

Implemented views:

- File headers, File sections and File dependencies: target-scoped metadata
  row contracts for the static binary reader. Headers use Field/Captured value;
  sections retain Name/Kind and address basis/Address/File offset/File bytes/
  Virtual bytes/Permissions and flags; dependencies retain Name/Inspection
  scope. They use the shared source/session/scope/readiness and row bounds.
  Core supplies a local picker through `debug.file-inspection`; actual local
  K1/K2 files have been selected and these three views captured their PE metadata. File
  headers do not verify runtime compatibility or an executable's full hash.

- Inspect file: a local binary picker with pending-selection cancellation and
  explicit return from the file debugger. It uses Core's restricted file
  inspection service; the package does not receive the generic session owner
  or executable handlers. Failed/ambiguous parsing retains the existing debugger
  selection. Disabling the package cancels pending work without changing the
  game VM or discarding completed selection. File data remains local and is
  not persisted by this flow. K1/K2 selection and metadata capture were observed
  locally; interruption, cancellation and other formats remain unqualified.

- Debugger capabilities: the selected session's operation advertisements, with
  reported state, evidence source, unit, owner and reason. It shows up to 256
  operations, uses text-only cells, updates on selection/readiness changes and
  clears when the report becomes unavailable. Reading the panel sends no
  backend command and grants no capture or execution permission. This source
  report was observed for the local K1 file inspection, with only its three
  file operations available. This is not runtime or arbitrary-game qualification.

- Execution: capability-gated pause, resume, and render-presentation step; real
  session status, target, boundary, present count, and parked-worker count.
- Processes: a paused guest process-list capture and selected PID.
- Watches: up to 32 persistent nonzero guest addresses, typed values using the target's declared byte order
  (u8/u16/u32/i32/f32/f64), manual capture, per-read failures, changed-value
  highlighting, and stale-result rejection across disable/session/PID changes.
- Existing memory and diagnostics workbench: memory capture/hex/search/baseline
  comparison, binary header and section inspection, session diagnostics.
- Threads: a paused KOTOR guest PID capture of thread IDs and kernel-wait/paused
  states through `threads.list`. Instruction locations and wait causes remain
  unavailable.
- Registers: a paused KOTOR guest PID capture of saved x86 EIP and general
  registers through `registers.read`. These are saved values, not guaranteed
  next instructions. Flags, FPU/SIMD state and instruction stepping are unavailable.
- Disassembly, stack, modules and breakpoints: registered dock views with
  capability gates. The current browser runtime has no readers for these views;
  their panels remain outside the catalog. No synthetic CPU state or armed
  breakpoints are displayed.

All captured views use the session-bound request service and its
`telos.debug-adapter-status/v1` available-operation gates. No KOTOR method names
are required for disassembly, stack, modules or breakpoint-list readers.
These four operations take `{pid}` and return `{pid, captured:true, rows}`;
rows are bounded to 4,096 arrays of scalar cells matching their table headings.
The shared view contract also covers `code.functions`, `symbols.list`,
`code.references`, `trace.read`, `events.read` and `input.timeline` with the same
selected-PID request and captured-row envelope. Their headings are respectively
Address/Name/Extent, Address/Name/Module, From/To/Kind,
Position/Unit/Address/Event, Position/Unit/Kind/Details and
Position/Unit/Input/State. Position units are supplied by the backend, never
assumed to be game frames, CPU instructions or the authoring score's ticks.
These are bounded captured read views; they do not stream traces, dispatch
inputs, resolve symbols, replay recordings or arm breakpoint/watch mutations.
They retain selected session/process/source/readiness gates and stale-result
rejection. No current guest readers implement these six operations, so their
panels remain outside the catalog. A native or uploaded-game backend must
provide and qualify its readers. Source implementation has not been loaded
or qualified in a live browser.
Threads and registers retain their existing structured capture contracts.
Other captured row views may use `scope:'target'` only with a declared non-live
evidence source. Their request has empty params, and the reply data must be
`{scope:'target',captured:true,rows}` with no PID field. This permits file or
snapshot inspection without fabricating a process. Missing scope defaults to
the existing process contract. Live reads and structured thread/register views
remain process-scoped. Unsupported scopes cannot enable a reader. Source/scope
changes reject pending captures; session/selection guards remain. The capability
panel displays scope. No target-scoped backend is installed or live-qualified
by this source change.
Thread-list replies with unavailable instruction locations may report Paused,
Kernel wait, Stopped or no captured state. A missing state is Unavailable in
both views. The view does not label an unknown state as Paused. This shared
contract permits a backend to report a stopped target without claiming KOTOR
guest scheduling semantics. Location-bearing thread replies remain unsupported.
Readers execute only while the host confirms its paused capture boundary,
and stale session/process/disable results are discarded. These are read views,
not an instruction decoder, breakpoint engine, stack unwinder, or VM snapshot
implementation. Those capabilities must be implemented and qualified in the
backend before enabling their UI.

Inspection readiness follows the service's declared evidence source. Available
live-runtime reads still require the host's confirmed paused capture boundary.
Available recorded-journal, simulator, static-analysis and captured-memory
reads use their session-bound snapshot admission; they do not require or imply
a live program pause. Missing or unknown sources cannot enable capture. Source
changes discard pending captures and memory comparison baselines. The UI names
the source alongside captured rows and watched values. Execution commands keep
their separate live-runtime completion requirements.

These routes are source implementation only. Core's selected debugger service
defaults to the existing guest adapters and permits a separately authorized
trusted owner to attach a revocable broker. Selection changes invalidate pending
captures and hide the guest workbench; no other-game or snapshot backend is
installed or live-qualified by this wiring. Numeric memory tools remain limited to a
declared 32-bit address layout and little/big byte order.

All views use the common dock workspace for dragging, resizing, hiding,
opacity, and saved placement. No extra renderer or dependency is introduced.

Additional memory tools use the session-bound `debug.runtime` request service
with `processes.list` and `memory.read` operations. The browser host adapter
delegates to its existing origin/session-checked guest-memory bridge; it does
not add a second transport or debugger controller:

- Value scanner: choose an explicit range of at most 65,536 bytes and a
  u8/u16/u32/i32/f32/f64 type using the owner's declared byte order. Start with unknown values or an exact
  value, then narrow the same candidate addresses by changed, unchanged,
  increased, decreased, or exact value. Each scan compares with the preceding
  successful capture. Range, type, and address spacing remain fixed until reset. Type-width
  spacing starts at the requested range address; every-byte scanning is optional.
  All candidates are retained; the table displays up to 200. A result can be
  added to persistent Watches without writing guest memory.
- Strings: inspect the scanner's actual captured bytes as printable ASCII or
  explicitly selected UTF-16LE/UTF-16BE ASCII characters, with minimum length and case-insensitive filtering.
  Both UTF-16 byte alignments are inspected. The table displays up to 200 matches
  and 1,024 characters per string; range-boundary strings may be incomplete.

The scanner and strings share one snapshot, so string inspection requires no
additional guest read. Snapshot provenance includes PID, address range, and
capture time. Scans require a paused target with all active workers parked, and
failed reads preserve the previous successful capture. Session/PID changes and
package deactivation invalidate pending reads. Changing the selected process
through a refreshed process list also invalidates older watch/tool captures.
No writes, value freezing, pointer-chain search, whole-process scan, instruction
stepping, or invented CPU data are provided. TAS is not enabled or imported.

Host services may supply synchronous `getStatus()` with the selected `session`,
execution `state`, and `pausedCaptureReady`. The UI requires an explicit true
capture-readiness assertion while paused, in addition to the existing operation
capability and request-route gates. The BoxedWine shell derives that assertion
from its complete worker pause barrier. Other trusted hosts must derive it from
their own qualified capture boundary; they need not invent BoxedWine worker
counters. A null, stale-session or throwing host status fails closed and never
falls back to another backend's page-global diagnostics. Hosts without this
method retain the existing guest worker-barrier path. This source change adds
no backend, operation qualification, automatic read, or uploaded-game support;
the running immutable game page has not loaded it.

Process-list, register, thread and legacy table captures now recheck the host
capture boundary before accepting a completed response. Registers, threads
and table views also require that boundary before enabling Refresh; a paused
label alone does not grant a read. A resumed or no-longer-ready target leaves
the previous captured rows intact and reports a failed capture. This is source
work only; the retained pages have not loaded or qualified this revision.

Execution, memory, process, thread and register capability gates now consume
the current session's canonical `debug.runtime` operation report. Those routes
do not require a second KOTOR descriptor alias to enable their controls.
Process-list readiness uses `processes.list` independently of `memory.read`.
Legacy direct table readers still require their descriptor and reader gates.
This removes a host composition dependency; it does not wire a new game owner
into the shell, broaden the accepted response schemas, or qualify a backend.
The retained live pages have not loaded this source revision.

Shared controls and capture tools accept the service's `getSession()` identity
without requiring a KOTOR game object. A null or throwing accessor disconnects
the UI without falling back to a different game. Legacy hosts without the
accessor retain `getGame().session`. Direct guest readers remain optional;
unsupported views stay unavailable. `mountWorkbench()` is also optional: the
Memory & diagnostics shortcut stays unavailable until a host mounts that
workbench, while independently registered capture panels use their qualified
routes. The retained KOTOR pages remain unchanged and this composition has
not been live-qualified for another game.

Capability reports must use `telos.debug-adapter-status/v1` and match the
current session. Missing, throwing or mismatched reports disable shared routes.
The paused status message describes image stepping only for `render-present`;
other units keep the adapter's Step control and do not imply a rendered image.
The current watch/scanner address schema uses exact 32-bit numeric addresses.
The selected owner must declare `target.pointerBits:32` and `target.byteOrder`
as `little` or `big`. The UI reads that target from current-session host status,
or from the legacy session descriptor when status does not contain a target.
An explicitly null status target does not fall back to another declaration.
Missing or incompatible layouts keep typed memory views unavailable, without
disabling independent process, thread, register or execution routes.
KOTOR's owner now declares its existing x86 32-bit little endian layout through
the same target getter in host status and the legacy session descriptor.
Typed capture uses the host status target before considering display events.
Watches and scans decode the captured bytes using the declared order and label
capture provenance. Scans recheck the layout at completion and clear snapshots
when a later observation differs; watches recheck byte order at completion.
These comparisons do not detect every transient change between observations.
Hosts must replace the bound session or owner when changing the selected target.
This source revision is not loaded into the retained pages. Big endian decoding
and the UTF-16BE selector are not live-qualified for another game. Wider address
spaces still require an exact address representation, compatible responses and
live qualification; no numeric truncation or inferred backend support is added.

### Retain a failed image-step notice

The BoxedWine bridge now requests Pause after an admitted image step fails.
The request closes the native run-until-present interval; later status must
confirm the worker barrier before inspection. It records a separate recovery
Pause without changing the failed step into a completed step.
A bounded `lastControlError` display field survives status polling until a new
command. The acknowledgement `error` remains specific to the current response,
so a historical display notice cannot reject another pending request.
This source revision has not been loaded into the retained guests. In the
retained original-game session, one requested image step stayed at present398;
a normal Pause recovered49/49 workers and Resume preserved the guest.
That observation does not qualify this candidate recovery implementation or
identify the cause of the stalled scene.

### Export the displayed execution view

`Export execution view` saves the current rendered metric labels, values, status
message, bound session, render time and export time as
`telos.execution-display-capture/v1`. It makes no backend request. Render time
is a UI timestamp; it is not a native observation or completion receipt.
The export contains displayed text and cannot establish executable identity,
audible playback, replay, simulation cadence or platform compatibility.
The session is rechecked before export. Owner/session replacement invalidates
the cached display and download URL; deactivation also releases the URL.
Exports over64KiB are refused without truncating the displayed data.
This supports hosts that provide the existing execution view, including optional
file-I/O, SDL proxy and paused GL counters when actually available.
It does not add a backend operation or support for an uploaded game's runtime.
The current retained guests have not loaded this source revision. A real local K2 file owner exported its execution view on2026-10-13.
The downloaded JSON retained its static-file session, PE32/x86 display,
Inspection/static-analysis labels and unavailable runtime counters.
Runtime-owner export, owner replacement and size-limit qualification remain pending.

### Interpret the proxy elapsed counters

In `sdlcallback.cpp`, the caller timer ends after
`TelosControl::leaveKernelWait()`. That operation can park a caller at the VM
pause barrier. Caller elapsed therefore includes that pause wait as well as
proxy execution and resumption. The candidate's title capture had95582completed
proxy calls/34075517caller microseconds; after Resume it showed95753calls and
365895614caller microseconds. This observation supports the source scope; it
does not measure an exact pause duration or proxy overhead. Independent
cumulative reads and overlapping thread intervals also prevent interpreting
the difference from callback elapsed as CPU cost or frame rate.

The callback timer surrounds the graphics callback as well. In
`sdlgl.cpp`, presentation calls `TelosControl::notePresent(thread)`, which can
wait at the step's pause boundary. After scored tick2 paused at present51,
callback elapsed was5681985microseconds; after Resume it was62370854.
Both elapsed counters can therefore contain deliberate barrier waits. Their
difference is not a safe estimate of rendering or proxy CPU cost.

The next bridge revision also publishes a current score-input failure through
VM status, so the shared Execution view can retain its bounded failure notice.
The recorded1237candidate stopped before class input dispatch when its10second
pre-input render gate expired, then recovered a37/37pause barrier. Its existing
transport displayed the failure; this later shared notice change remains source
only. A subsequent bridge revision uses the advertised60second render limit
for both manual image steps and the pre-input alignment helper. Parent image
requests allow70seconds for that wait and reply; Pause/Resume keep15seconds.
Longer deadlines do not prove input consumption, replay or performance.

### Bound a queued score operation

The next bridge source gives each valid score input one receive-side90second
budget, including time in its queue. The pause, pre-input image and post-input
image phases share that deadline; each image phase also keeps its60second
limit. Expired queued work is refused before input dispatch. The deadline is
rechecked after alignment before any input is sent. A scored failure requests
a recovery Pause before reporting failure and the parent's held-input release.
The parent timeout also requests Pause, whose existing active-job cancellation
path can interrupt the loop before its queued release. A sent Pause is not a
confirmed worker barrier. Input may already have reached the guest.
Checks run when JavaScript receives control; they cannot preempt a blocked
synchronous export or promise an exact wall-clock upper bound. The source
revision is not loaded into the retained guests and has not been live-qualified.

Queued VM controls now check the latest admitted control intent before
dispatch. Status requests leave that intent unchanged. A newer Pause that
cancels an active score job prevents an older queued Resume or Step from
running afterward. The superseded response rejects only its own request and
does not replace the current retained failure notice. This source path remains
unqualified in the retained guests.

The child rechecks budget/cancellation immediately before completing a scored
frame. The parent also checks acknowledgement arrival time against its own
send-side budget, so a delayed timer cannot admit a late success. Refusal
records uncertainty and requests Pause; it does not undo input already
delivered to the guest. These are source changes, not live deadline proofs.

The execution-view download was also used with the actual BoxedWine runtime
owner on2026-10-13: sessionbd9ee88b-ac7f-4356-a579-a26303018b4d,
K1/running/render presentation/live-runtime,present0 and displayed35/36workers.
The retained JSON includes the then-displayed file/SDL counters and unavailable
paused GL counts. These values differ from the subsequent AX observation,
which is expected for a snapshot captured before that observation.
It qualifies the runtime-owner display export, not startup completion or
native executable identity. Owner replacement and oversize cases remain open.
