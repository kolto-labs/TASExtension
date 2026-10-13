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

The backend-neutral operation should report process-scoped live-runtime
evidence and use the existing session/revision/target broker and controller.
Transport, backend, target and policy must all advertise support. A matching
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

This is a design decision based on the actual source inspection. No lookup
operation is exposed or advertised yet. Source implementation and actual
debugger use remain outstanding; no synthetic requests, tests or probes were
run. Current recorded candidate6889 continues in its established browser
session while this capability is prepared for a later compatible batch.
