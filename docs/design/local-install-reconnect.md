# Retry the localhost installation connection

The initial local-install timeout previously left folder selection or a workspace reload as recovery paths. The existing P1 browser owner now exposes Retry local installation for a loopback host with no attached game and no accepted local manifest. It uses the same bounded manifest fetch, library/index identity checks, profile admission and serialized launch path. No separate guest or tab is created by this action.

Connection and launch activity disable retry. The current restore generation owns pending status and error display; obsolete responses cannot clear a newer attempt. An explicit failed retry retains connected files and saved folder permissions and does not invoke remembered-folder staging. An initial failure keeps its existing remembered-folder fallback. Successful validation continues the configured automatic local launch.

Source only. The retained browser has not adopted or exercised this action. No tests, probes, syntax checks, runtime rebuild, staging, deployment or profile mutation. Backup: WASM artifacts/original-game-before-local-retry-20261014.js. Coupled dirty original-game.js remains uncommitted.
