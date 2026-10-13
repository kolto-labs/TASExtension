# Game overlay presentation

The Game overlay toggle changes the layout of the existing game iframe and
TAS workspace. It keeps the game session, input owner, score, transport and
recording in place. Returning to the surrounding layout restores the score
editor and other panels. The presentation toggle does not restore game state.

## Observed local use

On 2026-10-12, the retained original K1 guest switched into overlay mode at
render present 328, then returned to the surrounding editor at present 332.
The same guest session and recording remained active. Its 28-note score was
preserved. A subsequent held Enter/release segment completed at score tick 32,
present 337, with all 49 workers paused. No visible dialogue transition was
established from that segment; normal Resume continued the opening scene.
These observations do not establish full native/browser parity, game-state
restore, simulation timing or platform compatibility.

## Control-card layout source

The observed narrow overlay placed a large runtime-control card over the
scene. The compact grid rules expected direct control children, while Debug
and TAS now retain their controls inside package-owner wrappers.

The source now gives visible owner wrappers `display: contents` so their
existing buttons and status text participate in the same grid. Hidden wrappers
and disabled-package visibility rules remain in force. Nested status text spans
the grid. The overlay card is capped at 440 pixels; narrow layouts keep Close
game beside the wrapping runtime buttons, with status and file details below.
The changes add no controller, guest input handler or frame operation.

This CSS correction has not been loaded in the retained game. Its visual,
keyboard and responsive acceptance remain open. Existing captured game data
and layout selections are not evidence that these new styles were applied.
