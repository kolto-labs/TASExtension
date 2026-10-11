# R4NG3 official K1 VOD — seven-minute visual track

This merged, read-only track covers source frames 18,000–43,199 (300–719.983
seconds at 60/1 fps) from the official SRC-linked video for verified K1
submission `y4ol3jkz`. The source video SHA-256 is
`5400e414b292a3830cabec3e3b7f39c2ccb2c623f9d51d5f7b8583cd14b7e5b2`.

`observations.json` contains 25,200 frame observations compressed into 2,339
complete visual-state transitions across 42 cells and seven contiguous source
parts. The seven original one-minute slices are retained alongside this merge.
For observation index `i`, source frame is `18,000 + i`.

The per-slice extractor recorded 446 ambiguous cell/frame observations across
the seven minutes. These remain represented as unknown state; the merge may
compress repeated complete states. Cells whose captions change between
key-like labels and game actions are unbound visual slots, not inferred keys.

## Replay status

`replay_qualified` and `physical_inputs_complete` are **false**. The visualizer
does not establish that a highlighted cell is a physical key held for that
duration. The VOD frame clock is not aligned to game-start or input-polling
boundaries, semantic/action cells remain unmapped, and mouse movement is not
captured. No executable `.score` was produced; do not replay this observation
track as physical input without independent binding, timing, and in-game
qualification.
