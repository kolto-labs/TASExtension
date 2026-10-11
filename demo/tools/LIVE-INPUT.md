# Continuous local input capture

`live-input.mjs` captures real keyboard and mouse transitions from the local BoxedWine KOTOR iframe through Chrome DevTools Protocol (CDP). It writes an append-only raw NDJSON stream as the primary artifact and, after a clean stop, emits an editable `.projection.score` sidecar in canonical Stasis score syntax. That sidecar can be checked, edited, and played by the desktop `telos` controller. Capture itself does not take screenshots, drive the TAS transport, or control the browser game's clock.

## Why the capture is armed before launch

The pinned BoxedWine input path registers capture-phase `window` listeners for `keydown`, `keyup`, `mousemove`, `mousedown`, and `mouseup` in `upstream/platform/sdl/knativeinputSDL.cpp`. It forwards these events into the guest through the `_boxedwineEmscriptenKey`, `_boxedwineEmscriptenMouseMove`, and `_boxedwineEmscriptenMouseButton` entry points, then calls `stopImmediatePropagation()`. The recorder therefore registers at `Page.addScriptToEvaluateOnNewDocument` before the runtime iframe is created and observes transitions at the same window capture phase. It refuses to attach for recording if a runtime frame already exists, rather than producing a deceptively incomplete take.

The native Stasis score path is separate: `KPHarness/KPTASInterface/native/shim/src/dinput.c` stages a canonical note's key, button, and mouse state at the native harness gate. Browser capture timestamps do not prove alignment to those gates. The generated score is consequently marked partial and floor-quantized to a nominal frame rate; the raw event stream remains authoritative. The `.score` sidecar carries held key/button state per projected frame, but frame-boundary alignment and guest input sampling still need live route qualification.

Each projection records the raw NDJSON byte length, SHA-256, input-event count, and first/last page-performance timestamps. Keep the raw file with the score; compare its digest with `sha256sum run.raw.jsonl` before treating the projection as derived from that capture. This pins which capture was projected. It does not qualify the observed inputs, timestamp alignment, or route replay.

There are two playback destinations. The desktop Stasis CLI can execute this canonical score against its native K1 shim. The browser page's score editor currently drives only its deterministic stand-in (`app.js` calls `advance()`); the separately launched `OriginalGame` iframe has no connection to that controller. Importing a score in the browser page does not control KOTOR, and its parser supports a narrower subset. Use desktop `telos` playback below for actual TAS playback.

## Use a separate debug browser

Do not add remote debugging to, or reuse, your normal browser profile. Start a dedicated local Chromium profile in another terminal. Substitute the local prototype port if it differs:

```sh
chromium --user-data-dir=/tmp/telos-cdp-profile --remote-debugging-port=9222 --no-first-run http://127.0.0.1:47653/
```

If the executable is named `google-chrome`, use that command instead. Open the local prototype in this dedicated window, connect the user's game files, and leave the game unstarted. Start the recorder from a terminal:

```sh
cd /run/media/brunner56/WorkspaceSSD/KPTAS-work-20261010/KPHarness
node demo/tools/live-input.mjs record \
  --cdp http://127.0.0.1:9222 \
  --url http://127.0.0.1:47653/ \
  --out /tmp/kotor-run.raw.jsonl --hz 60
```

Then click the existing **Start KOTOR** control in the dedicated browser. Wait for `Recorder attached before guest input handlers` in the terminal, focus the game canvas, and play the segment continuously. Press Ctrl+C once the segment is done; the raw stream is synced every second and closed durably, and the nominal-frame score projection is written beside it. The raw file contains input transitions, monotonic host receipt timestamps, the page's high-resolution event timestamp, the iframe animation-frame counter as context, focus/visibility changes, and runtime-frame lifecycle markers. It contains no screenshots or game memory.

A failed endpoint, missing page, wrong origin, existing game iframe, or an unsupported setup reports a clear error. The tool does not start/stop a game or alter an existing browser session. The selected page must be on loopback and its game iframe must be same-origin.

## Replay and limits

For a new run, load the same route/save in the dedicated browser and focus the game canvas. Then run:

```sh
cd /run/media/brunner56/WorkspaceSSD/KPTAS-work-20261010/KPHarness
node demo/tools/live-input.mjs replay \
  --cdp http://127.0.0.1:9222 \
  --url http://127.0.0.1:47653/ \
  --in /tmp/kotor-run.raw.jsonl --delay-start-ms 5000
```

Return focus to the game canvas during the five-second countdown. Playback schedules CDP key/mouse events against the original event-time deltas; it does not pause or step between events. This is continuous browser input transport, not evidence that the TAS consumed a `.score` file or that the route reproduced. Confirm focus, guest event delivery, and route outcome in a real run before relying on playback.

The player refuses pointer-locked mouse captures because the game's relative-motion semantics have not been qualified for CDP replay. Raw relative movement is retained and the projection annotates its limits; do not treat it as executable route input. Wheel projection, held-button behavior, input focus loss, and event-to-engine-frame alignment also require route-specific qualification. The score projection's quantization can be off by up to one nominal frame before considering unknown guest gate alignment. Keep and distribute the raw NDJSON with any derived score so provenance and uncertainty are not lost.

The public speedrun viewer's `kptas.browser-visible-inputs/v1` observations are a separate evidence format. They describe labels visible in a submitted video, not captured physical input events. In particular, the selected-controls export is intentionally read-only and replay-disabled, and can contain unresolved or unmapped inputs. Do not pass those observation files to this raw-capture projector or promote their nominal video frames to TAS ticks. A route author can use a bounded, manually reviewed excerpt as a lead, but must preserve the run ID, source-video relationship, selected frame bounds, unresolved-input notes, and timing qualifications in the authored score's comments; desktop route replay still needs its own qualification.

## Play the projected score with the real desktop TAS

From `/home/brunner56/kptas-routine`, point `--config` at a K1 configuration that names the intended game installation and built shim. Do not omit the config if the checkout's default `telos.toml` is not the K1 environment you intend to control:

```sh
cd /home/brunner56/kptas-routine
PYTHONPATH=src python3 -m kptas --config /path/to/k1-telos.toml score check /path/to/run.raw.projection.score
PYTHONPATH=src python3 -m kptas --config /path/to/k1-telos.toml play /path/to/run.raw.projection.score --backend shim
```

`play` consumes score notes continuously through Stasis's native input/gate path; `@segment captured hz=60 pace=realtime` requests nominal 60 Hz pacing. To save a holorecord while playing, use `run /path/to/run.raw.projection.score --backend shim --out /path/to/run.holo` instead. These commands launch and drive the configured native K1 process, not the browser iframe. Score playback starts from boot by default. The score format can record `start=save:<sha256>`, but the desktop backend does not reload a save yet. A trace captured from a loaded save is therefore not currently a faithful executable continuation: establish a boot-equivalent route or implement save-state admission/loading first.

The missing browser integration is concrete: the host page has no shared TAS-controller object for the original-game frame, while the BoxedWine runtime accepts browser events and desktop Stasis stages inputs at a native gate. A browser-backed TAS needs a versioned host-to-iframe control protocol (score load, play/pause/seek, receipts, origin/session validation) and a guest-side scheduler that consumes score state at the verified game sampling boundary. Posting `keydown` events or dispatching CDP input injects physical-style events; it does not make those events originate from the score cursor, implement TAS seek/rewind, or qualify deterministic gate alignment. That adapter belongs in runtime/controller work and requires a separately qualified runtime build; this capture tool does not fake it.

The utility requires Node.js 22+ for its built-in WebSocket client and has no npm dependencies. It has not been exercised against a live local KOTOR frame yet; do not describe the replay path as qualified until the raw capture and guest behavior are compared in a dedicated browser.
