# R4NG3 official VOD visual input track

This continuous two-minute track covers source frames 18,000–25,199
(300–419.983s at the video's reported 60/1 fps) from the video linked in
verified K1 submission `y4ol3jkz`. It is separate from R4NG3's previously
catalogued alternate Twitch highlight.

`observations.json` contains 7,200 decoded source frames compressed into 768
complete-state transitions across 42 visualizer cells. The source-video SHA-256
is `5400e414b292a3830cabec3e3b7f39c2ccb2c623f9d51d5f7b8583cd14b7e5b2`.
The `parts` list documents the two exact, contiguous decode slices and each
standalone slice is kept alongside this merged track. Source frame `s` maps to
observation frame `s - 18000`.

The overlay detector measured 96 ambiguous cell/frame observations in the
first minute and none in the second. Ambiguous cells are preserved in each
complete-state event rather than silently treated as pressed or released.

## Replay status

`replay_qualified` and physical-input completeness are **false**. This records
which visualizer cells were highlighted, not guaranteed physical key-down
intervals. Video frame time is not aligned to game start, game frames, or input
polling. Several cells have semantic or unresolved bindings; mouse position is
not captured. This is review evidence, not `.score` data.
