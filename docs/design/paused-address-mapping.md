# Resolve a paused address to its backing file

The existing BoxedWine exports copy_module_name and module_file_offset are
diagnostic helpers. They do not implement a safe debugger lookup contract:
the name can be silently truncated, zero can mean either a valid file offset
or failure, and the native entry points do not require a complete pause.
Returning their values as verified module identity would overstate the data.

KProcess::getModuleName and getModuleEip inspect mapped-file records. They
describe backing files, not the Wine Windows loader's complete module table.
The production getMappedFileForRange method snapshots a selected mapping and
retains its file/lease owners, protecting consumers from later map surgery.
It acquires the mapped-file mutex; a public paused operation must not block
indefinitely on a lock held by a parked kernel waiter.

## Required producer and capability boundary

Implement one versioned native capture for a selected PID/address. It must
require the complete runtime pause, obtain a nonblocking mapping snapshot,
and distinguish no mapping, busy metadata, missing file offset and insufficient
name capacity. Return an exact name length and explicit offset availability;
offset zero is valid. Preserve64-bit file offsets without JavaScript integer
rounding. Snapshot metadata only; no page fault, file read or target mutation.
Keep legacy diagnostic exports unchanged.

The backend-neutral operation reports process-scoped live-runtime evidence.
Selected hosted backends use the existing session/revision/target broker and
its transport, backend, target and policy support intersection. The original
guest adapter uses the existing trusted runtime/controller and origin/session
checked bridge; it does not establish four independent support reports. A matching
ABI version and complete request/reply validation are required before showing
availability. Static file inspection grants no process mapping capability.
Other game adapters may implement the same operation with their own address
width and capture boundary; availability is never inferred from file format.

The view needs a selected process and explicit virtual address. Display the
requested address, mapped range, backing path, offset availability and capture
time. Export the exact request and reported semantics. A backing path/offset
does not prove executable hash, loaded Windows build, current instruction,
disassembly, symbol identity, replay or platform compatibility.

## Current disposition

Source implementation now includes the nonblocking native metadata snapshot,
versioned paused producer, bounded bridge transport, parent/runtime validation,
operation vocabulary and a process-scoped debugger view with exact exports.
The view validates the reply again and retains the complete bounded path;
unavailable file offsets remain null and valid zero offsets remain exact hex.
Availability requires ABI v1 and both native exports. The one-job production
build completed successfully and staged candidate214aa1fc with SDK WASM
SHA2566b8321c7243c2653dc8e274ca090b979bdf9bff3c9c1eaec07252eb0bf0f022b.
Separate guest30/session25751740-3189-41fc-9310-551286814c9e adopted the view
and capability handshake. Its normal toolbar opens the view, which requires a
process selection before capture. During actual startup diagnosis, normal Pause
confirmed all38workers at present0. Processes captured14rows; selected PID10
wine had one saved thread11 in kernel wait, with EIP0x104fd197. The actual
lookup returned /lib/libc.so.6, start0x10452000, extent0x0000000000100000,
file offset0x00000000000cd197. Normal export retained exact request/metadata
in debug-address-mapping-2026-10-13T16-52-12-433Z.json,
SHA256f937a376a8491b6e4879a81a81f948478308df20e2bf502cb0f564fd66cfd7f0.
This qualifies the producer/transport/validation/view/export path for this
one successful pause capture. Zero/unknown offsets, busy metadata, oversized
paths, no mapping, interruption and other backends remain unqualified. This
does not identify the Windows game executable or establish a wait cause.
The guest resumed after capture; its startup recording remains active. These
observations do not establish original-game acceptance. No
synthetic requests, tests or probes were run. Candidate6889 remains in its
established browser session; the default runtime is unchanged.
