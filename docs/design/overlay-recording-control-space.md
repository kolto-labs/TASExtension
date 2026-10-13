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

This is source progress. The retained game pages still have their earlier
stylesheet; they were not reloaded to load this change. Recording disclosure
access, expanded recorder geometry, transport scrolling, floating layouts and
return to surround require actual application qualification. No tests, test
probes, syntax checks, stylesheet injection or guest restart were used.
