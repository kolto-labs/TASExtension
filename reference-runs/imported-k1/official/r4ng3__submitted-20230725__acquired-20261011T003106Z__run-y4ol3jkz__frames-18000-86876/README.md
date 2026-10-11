# R4NG3 official K1 VOD — complete sampled span

This merged, read-only visual track covers source frames 18,000–86,875
(300.000–1,447.916 seconds at 60/1 fps) from the official SRC-linked video
for verified K1 submission `y4ol3jkz`. This is 19m07.933s of the 24m07.933s
source. The initial five minutes (frames 0–17,999) were not extracted, so this
artifact is **not a full-run input recording**. Source video SHA-256:
`5400e414b292a3830cabec3e3b7f39c2ccb2c623f9d51d5f7b8583cd14b7e5b2`.

`observations.json` contains 68,876 frame observations compressed into 5,170
complete visual-state transitions across 42 cells and 20 contiguous source
parts. Each original one-minute (and final 476-frame) slice is retained next
to the merge. Observation index `i` maps exactly to source frame `18,000 + i`.
The slices contain 1,165 ambiguous cell/frame observations, preserved as
unknown state. Caption-changing cells remain unbound visual slots, not guessed
physical keys.

## Replay status

`replay_qualified` and `physical_inputs_complete` are **false**. Visible
highlights do not establish physical key-down events or hold duration. The VOD
frame clock is not aligned to game start or input polling, some cells are
unresolved or semantic, and mouse movement is not captured. No executable
`.score` was produced. Independent binding, timing alignment, and in-game
replay qualification are required before this evidence can drive the game.
