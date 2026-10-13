# Local recording inspection

Open `/?view=saved-recordings` from the local workspace origin. Fresh local
workspace pages also provide a Saved recordings link that opens a separate tab.
This view starts no game VM and does not stop active capture. Existing game
pages retain their loaded UI until their normal lifecycle ends.

Find saved recordings lists the existing `kptas-recordings` namespace. View
receipt reads completion and provenance from the selected retained receipt.
Open footage snapshot reads the current saved `game.webm` file without writes,
deletion, finalization or uploads. Active-file reads may fail while the writer
owns its file; they do not force the writer to close. The receipt and footage
are separate observations and may reflect different times during capture.
Preview reads are capped at 64 MiB and copied into an in-memory File before
opening a media URL. A concurrent writer change can refuse the read; the UI
reports this without stopping the writer. Larger footage remains retained but
cannot use this bounded preview. A successful copy does not prove decoding.

The local game uses a localhost frame under a 127.0.0.1 workspace. The library
uses the same arrangement to access that browser storage partition. Directly
opening localhost may show different storage. The frame sends a selected File
to its explicitly named local workspace parent, which validates frame source,
origin, message schema and recording name before making a local preview URL.
This stays inside the browser; it is not a network upload or a game-state read.

The parent preview uses the workspace's existing blob media permission. It
starts muted and paused, with native playback controls. Changing selection
clears the prior preview; stale asynchronous selections/requests are discarded.
Replacing/closing the preview revokes its URL. A narrow media policy is also
queued for direct recordings.html use; the running server retains its startup
rules until a future normal server start. No live game/server restart is needed
for the parent preview.

The parent viewer provides Download footage snapshot and Download receipt
snapshot after the library prepares each File. Downloads run from the parent
document through a separate user action. Receipt snapshots preserve the read
bytes after bounded UTF-8/schema inspection. Footage snapshots retain the
existing 64 MiB copy limit; receipts retain the 64 KiB limit. Reading a new
selection clears both prepared downloads. A snapshot download does not stop
the writer, finalize capture or establish complete-run qualification. Receipt
and footage snapshots are separate reads and may describe different times.

An unfinalized WebM may have no finite duration while still allowing some
playback. Metadata, an advancing position and inspected frames prove only those
observations. They do not prove all recorded bytes are playable, audible audio,
game-frame alignment, saved-game restoration, a full run or a Routine clip.
Writer completion is likewise separate from these qualification requirements.

The capture source adds an explicit constant-zero audio source connected
only to the recording destination, keeping that mix connected before any guest
sound device exists and between device lifetimes. It is stopped/disconnected
with its capture destination and does not connect to speaker output. New
receipts declare `captureAudioClock:constant-zero` and
`audibleGuestAudioVerified:false`. Older runtimes declare the clock unavailable.
The recorder also distinguishes an encoder data event awaiting a storage flush
from waiting for the first encoded chunk. Production candidate a244f190 includes
these changes. Its actual title-screen recording ed3182df retained 2,763,738
bytes across 143 chunks at 2026-10-12T00:41:56.862Z, with the declared zero clock
and audibleGuestAudioVerified:false. Initial preview decoded a title frame, but
Play later failed with media code 4; playback is unqualified. After loading the bounded copy revision in the library only, a 7,619,565-byte
snapshot played through native controls at 800×600, muted, with actual time
advancing from 0.98 to 9.62 seconds. This qualifies only that inspected portion. Device lifetime, cleanup, audible guest
audio, finalization and full-run continuity remain open. Silence is not proof
of guest audio.

## Capture interruptions

A receipt with `complete:false` and `endedAt` reports an ended, incomplete
recording. It does not establish that footage is playable or that every file
handle closed. A receipt without an end time may describe ongoing or interrupted
capture. Keep the original receipt and footage in either case.

The recorder preserves the first failure reason. It drains queued and final
encoded chunks after a capture interruption while storage remains usable.
Storage failure stops subsequent writes. Pending audio startup has a 30-second
UI deadline; a capture lease acquired after that deadline is released by its
audio owner. These source behaviors still require actual capture qualification.

New receipts include `captureDiagnosticsAtStart` and `captureDiagnostics`
with observations at interruption and finalization. They report AudioContext
state/time, document visibility, capture-lease ownership, backend failure and
up to eight guest audio devices' PCM counters. AudioContext time is a browser
audio clock. Nonzero guest sample counters exclude the constant-zero clock;
they do not establish audible speaker playback or encoded audio contents.
Missing observations remain null. The running older candidates retain their
existing recorder and receipt format.
