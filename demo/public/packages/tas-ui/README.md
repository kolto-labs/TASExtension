# Optional TAS UI pack

Core registers only the `telos.tas` manifest and a deferred package entry point.
The manifest defaults to uninstalled/disabled and requires `telos.debug`.
Install records the package choice; Enable dynamically imports this directory.
No TAS editor, parser, simulation, bindings, render loop, HTML workspace, or
presentation stylesheet is downloaded or initialized for a disabled package.

`workspace.html` owns the score editor, transport/timeline, piano roll, branches,
sample-state tools, score console, keyboard settings, import/export controls,
and related dialogs. `workspace.js` mounts that markup before the editor starts.
The Core dock has only the game as a default panel. TAS registers its own panels,
all closed by default; open the required tools through Extensions or Panels.
Existing explicit visibility and saved layout choices are restored on enable.

Disable releases authored inputs, pauses TAS playback, flushes pending edits,
cancels modal edits, timers and animation frames, releases pointer capture,
and suspends editor subscriptions. It unregisters all TAS panels, disconnects
their dock observers and global listeners, and detaches package markup/styles.
It retains documents, branches, and dock placement in memory. Re-enable restores
the same nodes and session state; it never overwrites work with an example.
Uninstall has the same cleanup while retaining user documents and saved layouts.
Already-downloaded modules remain in the browser cache, but inactive code is not
scheduled. A cancelled load cannot mount after disable.

Earlier builds silently enabled TAS. The existing explicit-choice v1 migration
disables that earlier state once without downloading the editor or deleting data.
When an old workspace has the exact inherited all-open geometry, the pack closes
those inherited panels. Named saved layouts and explicit layout choices remain
intact. Reset layout returns the game-only workspace even with TAS enabled.

A load failure disables the package and reports a retryable error in Extensions.
The game owner and its iframe ancestry remain untouched. Presentation hot reload
continues to preserve its own state through package activation and suspension.

Interactive acceptance used an isolated Playwright CLI browser with local-game
auto-connect unavailable: zero TAS markup/module requests before enable; editor
text and explicit panel visibility survive disable/re-enable; Reset shows only
the game; Uninstall removes all TAS roots and leaves no cleanup error. No guest
was launched, and no filesystem tests or runtime builds were run.

Upgrade acceptance also confirmed that a legacy inherited all-open layout becomes
game-only, while the same geometry saved as a named TAS layout remains visible.
