# Navigate captured file bytes

The File bytes panel accepts complete decimal offsets and hexadecimal offsets
prefixed with `0x`. The broker still receives nonnegative safe integers and a
bounded byte count. Invalid or partial strings do not become a different range.

The immutable file reader reports its full file size with the captured range.
The UI retains this extent only with a completed capture in the selected
session. Previous and next controls prepare an adjacent range; they do not
request a read. At the file edges, the proposed range uses the remaining bytes.
The existing table and export retain the last captured range until Capture is
explicitly used. Exports include the reported file size and exact read range.

An older backend without file-size metadata can still display and export its
byte capture, but adjacent navigation remains unavailable. Session changes
clear the captured navigation extent. These are selected file offsets, not
process addresses, disassembly, runtime execution or platform qualification.

## Actual local use on 2026-10-13

Reloaded only the recordings-only file workspace25 and selected the actual
5279145-byte boxedwine.wasm through its normal chooser. Captured the header at
`0x0`, then Prepare next file bytes populated `0x80` with length128 while the
table still showed the prior header. Explicit Capture subsequently displayed
eight rows at offsets128–255 and enabled previous navigation. Normal export
saved debug-file-bytes-2026-10-13T13-59-00-154Z.json, SHA-256
e4aad89bcf6331093cfb03a08dc855fb852842f269ea8febbc7c1766cd89c9ed.
It records session static-file-742a7627-2d4e-45e5-a068-f4c3319b8263,
file size5279145, exact range and selected-file SHA-256 f7fe5961d57dd228ff1696b3293a7fc9710e4df52d2d90c73b8e6cd34e0ff511.

Only these actual captures and forward preparation are live-qualified. Edge
clamping, backward preparation and rejection/cancellation remain source-only.
The game owner28 and its startup recording continued without reload.
No tests, test probes, syntax checks or synthetic requests were run.
