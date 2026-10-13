# Show source timing beside draft positions

The reference draft position line now shows reported state-change PTS and its exact rational time base beside the authored draft tick and uncertainty. It names the state-change observation and reported source part. It does not extrapolate timing for subsequent observations or convert a nominal video rate into timestamps. Invalid or absent timing is shown as unavailable.

The helper accepts safe integer PTS and bounded positive integer rational time bases. It preserves the rational representation. It does not authenticate source claims, connect an executable score, move the game playhead, resolve unknown controls or qualify replay. Existing source-origin details, packet provenance and exports remain intact.

Implemented in source on 2026-10-13. Passive inspection of the existing full R4NG3 draft found no event PTS, so that artifact must display unavailable timing. Live adoption and actual displayed use are pending. No tests or test probes were run. Direct editor import/handoff and broader K1/K2 replay qualification remain open.
