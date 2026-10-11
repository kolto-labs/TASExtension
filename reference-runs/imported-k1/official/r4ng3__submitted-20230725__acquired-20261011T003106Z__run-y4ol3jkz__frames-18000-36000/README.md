# R4NG3 official VOD visual input track

This continuous five-minute track covers source frames 18,000–35,999
(300–599.983s at 60/1 fps) from the official SRC-linked video for verified K1
submission `y4ol3jkz`. The source video's SHA-256 is
`5400e414b292a3830cabec3e3b7f39c2ccb2c623f9d51d5f7b8583cd14b7e5b2`.

`observations.json` contains 18,000 source-frame observations compressed into
1,464 complete-state transitions across 42 visualizer cells and five
contiguous, provenance-linked slices. `parts` records each one-minute slice;
the individual observations remain beside this merged track. Source frame `s`
maps to observation frame `s - 18000`.

Across the span, 330 cell/frame observations fell in the detector's ambiguous
brightness range. They are preserved as unknown states. Manual comparison also
showed some keyboard-shaped cells change their captions from keys to semantic
actions during play, so those cells use stable visual-slot IDs and have no
score tokens. No labels were inferred from brightness.

## Replay status

`replay_qualified` and physical-input completeness are **false**. The track
reports visual highlights, not proven physical key-down/hold intervals. It is
not aligned to game start, game frames, or the engine's input-poll boundary.
Some cells are semantic/unresolved and mouse movement is not captured. This is
review material, not executable `.score` data.
