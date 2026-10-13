# Show source timing beside draft positions

The reference draft position line now shows reported state-change PTS and its exact rational time base beside the authored draft tick and uncertainty. It names the state-change observation and reported source part. It does not extrapolate timing for subsequent observations or convert a nominal video rate into timestamps. Invalid or absent timing is shown as unavailable.

The helper accepts safe integer PTS and bounded positive integer rational time bases. It preserves the rational representation. It does not authenticate source claims, connect an executable score, move the game playhead, resolve unknown controls or qualify replay. Existing source-origin details, packet provenance and exports remain intact.

Implemented in source on 2026-10-13. Passive inspection of the existing full R4NG3 draft found no event PTS, so that artifact must display unavailable timing. Live adoption and actual displayed use are pending. No tests or test probes were run. Direct editor import/handoff and broader K1/K2 replay qualification remain open.

## Actual viewer adoption

Reloaded only the no-game reference workspace31 and opened K1 speedruns. Existing Otterito catalog observations displayed draft tick0 beside reported state-change PTS242194410×1/90000, source part0, observation0 and uncertainty. No source player was opened.

Normal local picker restored the existing full R4NG3 reference draft. It retained86876observations/start240/uncertainty. Selecting31673 showed W/LMB/RMB and draft31913 beside unavailable source time at state-change31672. This correctly distinguishes the selected observation from the timestamp-bearing state change. Screenshot /tmp/kptas-draft-source-clock-20261013.jpg. No new export, executable score, upload, game launch or alignment grant.

Reported-PTS and absent-PTS display paths are qualified for these existing artifacts only. Other rational clocks, malformed values, discontinuities and editor integration remain unqualified. Diagnostic guest32 continued independently at presentation0/resumed31of33workers, recording86915bytes/3chunks; guest30 remained preserved. No tests or test probes were run.
