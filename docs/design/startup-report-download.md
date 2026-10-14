# Save startup evidence locally

Actual retained-session UI reported a successful clipboard copy, while the supported clipboard reader returned empty. No full report export was captured. The startup component now offers Save diagnostic report alongside Copy, using the existing bounded report formatter. The local text Blob is capped at256KiB; its name uses the frame-declared session prefix and an ISO timestamp. Temporary URL cleanup occurs once after30 seconds to allow the browser download to begin. The UI reports download requested, not completed. No network request, guest read, game command, reload or filesystem/profile mutation occurs.

Text report v2 includes immutable frame-declared ownedSession UUID and runtime-release path hash, or unavailable. Only these validated identifiers are extracted; the full URL/query is not exported. They are declarations, not loaded game binary identity. Existing phase, timing, milestone, callback/range observations and bounded diagnostic log remain. Native execution, game clock, full run, replay and audio acceptance are not granted.

Source-only. Existing document not hotpatched/reloaded and no download qualified. Exact pre-edit dirty source and isolated patch retained in WASM artifacts/game-startup-before-report-download-20261014.js and game-startup-report-download-20261014.patch. No tests/test probes/syntax checks.
