# Keep the overlay game grid name consistent

The TAS presentation stylesheet and dock stylesheet now both name the single
main grid area `game`. The game panel uses that same area. Editor overlay
panels retain their independent absolute positioning.

## Actual retained browser evidence

On 2026-10-13, normal Game overlay activation in corrected079d/tab21 left a
blank region above the game. Read-only computed layout reported cockpit area
`"center"` with rows `372px 0px 514px`, while p-center used grid area `game`
and began at y372. The mismatched name generated implicit tracks.

The source correction changes both names in tas-presentation.css to `game`,
matching dock-workspace.css regardless of stylesheet load order. The retained
live guest was returned to the surrounding layout without reloading or
hotpatching it. A newly opened recordings-only workspace has Game overlay
disabled; it did not qualify the correction and was closed without starting
a game. Loaded-live acceptance remains pending a later compatible runtime
stage and intentionally fresh owner. No tests or test probes were run.
