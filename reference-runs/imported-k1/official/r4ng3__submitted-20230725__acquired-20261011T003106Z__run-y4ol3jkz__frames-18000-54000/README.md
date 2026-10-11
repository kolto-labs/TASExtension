# R4NG3 official K1 VOD — ten-minute visual track

This merged, read-only track covers source frames 18,000–53,999 (300–899.983
seconds at 60/1 fps) from the official SRC-linked video for verified K1
submission `y4ol3jkz`. Source video SHA-256:
`5400e414b292a3830cabec3e3b7f39c2ccb2c623f9d51d5f7b8583cd14b7e5b2`.

`observations.json` contains 36,000 frame observations compressed into 3,185
complete visual-state transitions across 42 cells and ten contiguous source
parts. The ten original one-minute slices remain alongside this merge.
Observation index `i` maps to source frame `18,000 + i`.

Per-slice extraction recorded 712 ambiguous cell/frame observations. They are
preserved as unknown state. Caption-changing overlay cells remain unbound
visual slots; no physical key is inferred from a changing semantic label.

## Replay status

`replay_qualified` and `physical_inputs_complete` are **false**. Visible
highlights do not prove physical key-down state or hold duration. The VOD clock
is not aligned to game start or input polling, some cells are unresolved or
semantic, and mouse movement is not captured. No executable `.score` was
produced. Independent binding, timing alignment, and in-game replay
qualification are still required.
