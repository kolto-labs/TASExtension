# Prepare file bytes from captured sections

Status: source implementation, awaiting normal UI acceptance. No tests, probes,
syntax checks, builds, staging, browser actions or runtime operations were run.

## Use file ranges, not virtual addresses

The uploaded-file debugger already owns local header/section capture and an
explicit 1–4096-byte file reader. Captured **File sections** rows now offer
**Prepare file bytes** when an identified static file has a nonzero file-backed
span within the selected file's extent. The button prepares the existing File
bytes offset/count inputs and reveals that panel. It does not read any bytes.
The user must invoke **Capture file bytes** separately. Existing byte rows and
their export remain intact during preparation.

The selected-file backend adds three metadata fields to `binary.sections`:
`semantics: declared-file-ranges`, `fileSize` from its immutable Blob, and
`headerStatus` from the existing bounded header parser. The row shape, capture
scope and capability declarations remain unchanged. Older adapters without
these fields retain display/export behavior and receive no navigation action.

`declared-file-range.js` uses the row's **File offset** and **File bytes**
columns exclusively. It never uses PE RVA/image base, ELF virtual address,
Mach-O virtual address, memory size or permissions to calculate file offsets.
It rejects partial numeric input, unsafe integers, zero-length/NOBITS-style
spans and ranges outside the selected file. A declared span larger than 4096
bytes prepares only its first 4096 bytes; the status names both prepared and
declared lengths. File offset zero remains valid.

## Retain the selected owner

Navigation is offered only for `static-analysis`, target-scoped section
captures with the additive declared-file-range metadata and an `identified`
header status. Ambiguous captures receive no preparation action. Existing
epoch/session checks, the current selected-file lease, the File bytes
capability/reader and capture-readiness checks must still agree at activation.
Preparation is refused while the byte reader is busy or an existing byte
capture declares a different file extent. A session/target change clears the
actions through the existing view owner. No process mapping, memory read,
execution, snapshot, replay or platform capability is created.

Capture export retains the file extent and declaration semantics alongside
the previously captured displayed rows. These remain parser declarations,
not a loaded executable identity or authenticated runtime map. The broker and
selected file owner retain responsibility for any subsequent explicit read.

## Preserve source and qualify normally

Owned implementation:

- `demo/public/debugger/declared-file-range.js`: bounded file-span preparation.
- Scoped `static-binary-backend.js`: additive selected-file section metadata.
- Scoped `packages/debug-ui/index.js`: prepared-range navigation and gates.

Both coupled dirty source baselines are preserved in
`artifacts/before-section-byte-navigation-20261014-*`. Scoped patches separate
this work from preceding scanner, register, GL and selected-file changes.
No coupled dirty file is committed wholesale.

Parent-owned acceptance remains open: capture sections from an actual selected
file, prepare a file-backed span, observe retained prior byte rows/export,
perform the explicit read, and observe normal stale/zero/ambiguous refusal
boundaries. Other generic/native debugger and full bidirectional runtime
milestones remain incomplete.
