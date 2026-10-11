# Demo publication

Install the pinned build dependency with `npm ci`, then publish with
`npm run deploy`. The script builds `dist/` and runs the pinned Wrangler 4.125.0
CLI to upload it.

For a local Worker preview, run `npm run preview`; it builds `dist/` and starts
the same pinned Wrangler CLI. Wrangler serves the generated `dist/` tree.
`public/` remains the readable source tree used by `serve-local.py` and the
runtime preparation tools. The published app contains a minified bundle at
`dist/js/app.js`; original modules under `public/js/` and source maps are left
out of `dist/`.

Minification and bundling reduce the published files and remove the original
module layout, but browser-delivered code remains available to visitors and can
be inspected or reformatted. This is not a way to make client code secret.

For UI work against an already running `tools/serve-local.py`, open the local
source page with `?uiHotReload=1`. First activation after upgrading the local
server requires one coordinated server restart to load its revision endpoint.
After that, ordinary CSS and presentation edits need no build, server restart,
game restart, or page reload. The opt-in watcher requests consolidated CSS and
JavaScript content revisions
from the local server every two seconds while the tab is visible. The server
caches source hashes until file metadata changes. CSS swaps after loading;
`tas-presentation.js` changes remount the presentation through its
snapshot/restore/destroy lifecycle. Changes to `ui-shell.js` produce a
manual-reload notice and take effect on the next manual page load. The dock
workspace script is watched too, but changes are reported as pending: its
listeners, observers, and dynamic panel registrations do not yet have a safe
teardown lifecycle for in-place replacement. The game iframe stays mounted.

The watcher does not replace app, editor, debugger, or runtime owners. Console
messages and `window.telosUiHotReload.status()` report replacement failures;
`list()` reports which modules support replacement. Changes to the watcher
itself produce a manual-reload notice and preserve the active session. Pages
without the query opt-in behave as before. The production shell compiles out
the developer loader, and the watcher source is excluded from `dist/`.

After a successful paused memory read, Advanced diagnostics → Read game memory
also provides a captured-byte value inspector. Enter a byte offset in decimal
or `0x` hexadecimal, or move one byte with Previous/Next, and choose little or
big endian. The view displays signed and unsigned 8/16/32/64-bit integers and
32/64-bit floats at that offset. Integer values retain all 64 bits; values that
extend past the capture show how many bytes are needed. These are local
interpretations of the timestamped capture, not live watches or identified game
fields. A new read resets the offset, and changing the session/process or
clearing its capture clears the inspector. This adds no runtime operation.
