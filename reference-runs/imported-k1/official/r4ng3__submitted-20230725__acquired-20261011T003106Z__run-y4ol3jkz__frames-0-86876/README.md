# R4NG3 official K1 VOD — full-video visual observations

This merged, read-only track covers every decodable source frame, 0–86,875
(0–1,447.916 seconds at 60/1 fps), in the official SRC-linked 1920x1080 video
for verified K1 Any% Unrestricted submission `y4ol3jkz`. The VOD is 24m07.933s.
Source video SHA-256:
`5400e414b292a3830cabec3e3b7f39c2ccb2c623f9d51d5f7b8583cd14b7e5b2`.

`observations.json` contains 86,876 per-frame visual observations compressed
into 6,614 complete-state transitions across 42 overlay cells and 25 contiguous
provenance parts. The original 24 one-minute slices and final 476-frame slice
are retained beside the merged artifact. Observation index `i` maps to source
video frame `i`. The slices record 1,342 ambiguous cell/frame observations as
unknown state. Captions that change from physical-looking key names to game
actions remain unbound visual slots; no key is guessed from those labels.

This is full-VOD visual evidence, **not a full-run physical input recording**.
It does not establish that every displayed highlight is a physical key-down,
nor the key hold duration, game/input-poll alignment, continuity of game state
across edits or loading transitions, or mouse movement. `replay_qualified` and
`physical_inputs_complete` are false; no executable `.score` was emitted.
Independent input binding, timing alignment, and in-game qualification are
still needed before using this as replay input.
