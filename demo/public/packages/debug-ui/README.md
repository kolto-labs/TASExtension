# Debug UI package

`telos.debug` is a bundled, independently enabled package. Core supplies the
`debug.runtime` service (`getGame`, `mountWorkbench`). The package owns its dock
registrations, polling, captured-address watches, and package storage. It does
not own the game lifecycle. Disabling it suspends rendering/status work and
makes its panels unavailable without terminating the game or deleting layouts.

Implemented views:

- Execution: capability-gated pause, resume, and render-presentation step; real
  session status, target, boundary, present count, and parked-worker count.
- Processes: a paused guest process-list capture and selected PID.
- Watches: up to 32 persistent nonzero guest addresses, typed little-endian
  values (u8/u16/u32/i32/f32/f64), manual capture, per-read failures, changed-value
  highlighting, and stale-result rejection across disable/session/PID changes.
- Existing memory and diagnostics workbench: memory capture/hex/search/baseline
  comparison, binary header and section inspection, session diagnostics.
- Registers, disassembly, stack, threads, modules, and breakpoints: registered
  dock views with capability gates. The current browser runtime does **not**
  provide these operations. Their panels stay out of the dock catalog until a
  real reader is available; no synthetic CPU state or armed breakpoints are displayed.

For a target to supply one of the latter views it must advertise the named
operation as `available` in `telos.debug-capabilities/v1`, and its host adapter
must implement the corresponding `getGuest*` reader in `TOOL_VIEWS`. Readers
accept a PID and return bounded arrays of scalar table rows, matching the listed
headings. They execute only while the session is paused. These are read views,
not an instruction decoder, breakpoint engine, stack unwinder, or VM snapshot
implementation. Those capabilities must be implemented and qualified in the
backend before enabling their UI.

All views use the common dock workspace for dragging, resizing, hiding,
opacity, and saved placement. No extra renderer or dependency is introduced.

Additional memory tools use `readGuestMemory(pid, address, length)` directly:

- Value scanner: choose an explicit range of at most 65,536 bytes and a
  little-endian u8/u16/u32/i32/f32/f64 type. Start with unknown values or an exact
  value, then narrow the same candidate addresses by changed, unchanged,
  increased, decreased, or exact value. Each scan compares with the preceding
  successful capture. Range, type, and address spacing remain fixed until reset. Type-width
  spacing starts at the requested range address; every-byte scanning is optional.
  All candidates are retained; the table displays up to 200. A result can be
  added to persistent Watches without writing guest memory.
- Strings: inspect the scanner's actual captured bytes as printable ASCII or
  UTF-16LE ASCII characters, with minimum length and case-insensitive filtering.
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
