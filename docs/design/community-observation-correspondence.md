# Read acquired community observation provenance

The read-only observation importer accepts the correspondence claim from
`source.source_primary_correspondence_verified` or the root
`source_primary_correspondence_verified` used by older acquired tracks.
The value must be a boolean. When both locations are present they must agree.
The normalized in-memory source retains that value for the viewer. The original
artifact is unchanged. This normalizes a source claim; it does not independently
authenticate the submission, physical keys or replay timing.

All existing bounds, lane references, ordered state transitions, contiguous
source-part coverage and four false playback qualifications remain required.
Read-only, projection-execution-disabled, physical-inputs-incomplete and
replay-unqualified flags remain required. No score is created.

## Actual browser use on 2026-10-13

The official R4NG3 y4ol3jkz full-VOD artifact first failed normal local import
because it carries the correspondence claim at the root. After the source
repair, only the non-game recordings/footage workspace was reloaded. The same
648947-byte artifact opened through the normal local file chooser: 86876
observations and 42 cells. Navigating to frame31673 showed W/LMB/RMB highlights
and source part9/decoded source frame31673. These are visual observations,
not physical held-input duration. Player alignment, game polling and replay
remain unqualified. Screenshot: /tmp/kptas-official-vod-import-20261013.jpg.

The acquired artifact retains its original source-video hash and 25-part
provenance. No video or private game asset was copied or published. Existing
live WASM guests and recordings were not reloaded or controlled by the importer.
No tests or test probes were run.
