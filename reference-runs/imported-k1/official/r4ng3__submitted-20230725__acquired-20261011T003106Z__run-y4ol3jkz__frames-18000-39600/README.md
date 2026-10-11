# R4NG3 official VOD visual input track

This continuous six-minute track covers source frames 18,000–39,599
(300–659.983s at 60/1 fps) from the official SRC-linked video for verified K1
submission `y4ol3jkz`. The source video's SHA-256 is
`5400e414b292a3830cabec3e3b7f39c2ccb2c623f9d51d5f7b8583cd14b7e5b2`.

`observations.json` contains 21,600 source-frame observations compressed into
1,953 complete-state transitions across 42 visualizer cells and six contiguous
provenance parts. Each one-minute slice remains alongside this merged track.
Source frame `s` maps to observation frame `s - 18000`.

Across the six-minute span, 432 cell/frame observations were ambiguous and
remain explicit. Cells whose captions change between key-like labels and game
actions are represented as unbound visual slots, not guessed key presses.

## Replay status

`replay_qualified` and physical-input completeness are **false**. The track
records screen highlights rather than confirmed physical holds. Game start,
game-frame boundaries, and input polling are not aligned. Some visual cells
have unresolved or semantic labels, and mouse movement is absent. Do not
convert it to executable `.score` data without independent binding, timing, and
in-game replay qualification.
