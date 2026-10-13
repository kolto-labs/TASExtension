# Reserve space for overlay recording controls

The recorded localhost K1 session exposed a pointer overlap on 2026-10-13.
In Game overlay, the parent presentation toggle intercepted activation of the
iframe's Recording game disclosure. Returning to the surrounding layout made
the same disclosure accessible. The guest and recording continued throughout.

The overlay stylesheet now places the presentation switch, target caption and
runtime controls in flow above the iframe. The child recorder can occupy its
own top edge. The game view takes the remaining space and keeps the existing
aspect-fit canvas. No guest resolution, input coordinates or controller logic
changes are part of this patch.

The dock owner adds ID-based grid rules and viewport limits. Overlay rules now
select that grid explicitly and place the retained game panel in its single
area. The workspace toolbar, game dock titlebar and unrelated dock panels hide
only while overlay mode is selected. Stored placement and visibility data are
not rewritten. Transport remains bounded above the game, whether its existing
owner mounted it under the game panel or under the cockpit. Splitters hide
while the overlay grid is active.

The separate 861ca780 candidate loaded the earlier correction and exposed a
placement failure during normal Overlay activation. At a 1280 by 720 viewport,
the retained game panel occupied x608/y659 with height61, and its game image
was outside the visible area. Returning to Surround restored the same guest.
The dock assigns an inline `grid-area: game` to that panel. It overrode the
overlay's `center` rule and created an implicit placement outside the intended
single area. The source now overrides that assignment only in Overlay and
resets the absolute transport's grid area to auto. Stored inline placement is
retained for Surround.

This latest correction is source-only. Existing game pages were not reloaded
or injected to adopt it. Recording disclosure access, expanded recorder
geometry, transport scrolling, floating layouts and the corrected full-image
overlay still require application qualification. No tests, test probes,
syntax checks, stylesheet injection or guest restart were used.
