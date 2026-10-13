# Capture uploaded file bytes

`binary.bytes` reads an immutable selected Blob through the existing debugger
broker. All four owners must report target-scoped static-analysis support.
The inspection vocabulary classifies this operation as a read. It grants no
process memory access, mutation, execution or platform compatibility.

The request contains exactly `offset` and `length`. Both are safe integers;
offset is nonnegative, length is 1–4096, and the entire range must fit the file.
Only that slice is allocated. The file owner and cancellation are checked
before and after reading; broker and UI session guards still refuse obsolete
results. Output contains file offsets, hex bytes and printable ASCII with dots
for other bytes. The UI uses text cells, never executable uploaded content.

File bytes has its own panel, offset/count controls, capture action and export.
Its row export records the selected file identity, operation, source, target
scope, exact request range and selected-file-bytes semantics. Those are file
offsets, not process addresses or loaded-module identity. Earlier captured
rows retain their range when input controls change. No byte editing is added.

## Actual local use on 2026-10-13

The normal file chooser selected the actual 5279145-byte boxedwine.wasm in a
recordings-only workspace; no guest launched there. Its full selected-file
SHA-256 was f7fe5961d57dd228ff1696b3293a7fc9710e4df52d2d90c73b8e6cd34e0ff511.
Normal capture read offset0/length128 and displayed eight rows, beginning
00 61 73 6d 01 00 00 00. The same selection captured20 file-header rows and12
section spans. Architecture, ABI and pointer width remained unavailable;
little endian describes file metadata, not a host CPU. Execution, process
inspection and runtime compatibility remained unavailable or unqualified.

Exports retained under /home/brunner56:

- debug-file-bytes-2026-10-13T11-48-27-657Z.json
- debug-file-headers-2026-10-13T11-52-08-182Z.json
- debug-file-sections-2026-10-13T11-49-09-214Z.json

A metadata export initially timed out because the header capture had not been
activated; its export control was disabled. Reattached to the same retained
workspace after the browser-control kernel reset. Fresh accessibility indices
captured headers and downloaded both metadata exports. No workspace or guest
was reloaded. Screenshot: /tmp/kptas-uploaded-file-byte-inspection-20261013.jpg.

Only this actual128-byte read and these metadata captures are live-qualified.
Other range sizes, cancellation, replacement and error paths remain source
implementation without runtime acceptance. No tests or test probes were run.
The self-contained byte reader and this document can be committed separately;
its integration remains alongside earlier coupled debugger work.
