# Name the core connection

Direct user request: the visible “connecting TAS controls” text incorrectly describes core loading/connection; TAS should be an optional extension installed into the core. The user delegated choosing the core name.

Use Telos Core for the package/session host, matching the existing Extensions panel and extension API. Keep Stasis as the workspace name. The core is already running when the game bridge starts, so the status is “Connecting the game to Telos Core…” rather than claiming the core itself is still loading. Related bridge connection failures use Telos Core. Actual score-input boundary details retain their operation meaning. No identifiers, APIs, extension ownership, package installation/defaults or runtime behavior changed.

Source served by the existing localhost application. The retained live browser observation timed out; this turn did not reload the tab or inject new text into the running document. Live adoption remains unconfirmed. No tests or test probes. Pre-edit source retained in WASM artifacts/original-game-before-core-connection-copy-20261014.js; coupled dirty source preserved.

## Keep core status independent of extensions

Source review found connection status inside the telos.tas presentation wrapper. The parent game owner now maintains a separate coreConnectionStatus outside both Debug and TAS wrappers. Initial/connecting, validated input handshake, bridge failure and runtime stop update it independently of package visibility. The successful message says Game input connected to Telos Core; it does not claim full VM/debugger/replay readiness. Optional TAS retains score-input waiting and boundary details. No new capability, dispatch, package dependency, runtime timer or controller was introduced.

Live adoption remains unconfirmed because current browser discovery returned no accessible browsers. No duplicate tab, reload or runtime restart. Backup: WASM artifacts/original-game-before-core-status-ownership-20261014.js.

## Report independent readiness

Validated input and VM-control handshakes now both update core status. The display distinguishes input only, execution only, both, and a validated runtime reporting unavailable execution controls. This uses existing admitted capability replies; no timeout guesses, extra requests or capability grants. It does not claim simulation/replay or graphics/audio acceptance.

Actual server PID44787 remains Ssl and loopback listeners47655/47656 remain present. Service-manager property lookup ended with Transport endpoint is not connected; service health is not inferred from process evidence. Browser inventory rediscovered only retainedtab1/9625a. Rebinding and handoff were attempted before bounded actual-log observation timed out/reset; no log contents or current game state received, no restart/reload.
