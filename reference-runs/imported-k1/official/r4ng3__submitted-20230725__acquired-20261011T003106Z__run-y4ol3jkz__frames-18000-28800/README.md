# R4NG3 official VOD visual input track

This continuous three-minute track covers source frames 18,000–28,799
(300–479.983s at the video's reported 60/1 fps) from the video linked in
verified K1 submission `y4ol3jkz`. It is distinct from the previously
catalogued alternate Twitch highlight.

`observations.json` contains 10,800 observed source frames compressed into
1,099 complete-state transitions across 42 visualizer cells. It preserves the
official source-video SHA-256, exact source frame origin/span, three contiguous
source parts, ROI coordinates, threshold values, and all unresolved lane/frame
states. Each one-minute source slice remains available alongside the merged
track. Source frame `s` maps to observation frame `s - 18000`.

The detector recorded 205 ambiguous cell/frame observations across this span;
they remain explicit in state events. They were not converted to presses or
releases.

Manual frame comparison found that several keyboard-shaped cells change their
captions during the run: at source frame 18,000 they show key-like labels, while
at 21,600 the same cells show game actions such as QS, Equip, Opts, and Round.
The track therefore represents those cells as stable visual slots with no
score token; an earlier physical-key guess was removed. Only 23 cells retain a
potentially expressible token, and none of them are replay-qualified.

## Replay status

`replay_qualified` and physical-input completeness are **false**. This is what
the video visualizer highlights, not guaranteed physical key-down intervals.
The trace is not aligned to game start, game frames, or input polling. Some
cells are semantic or unresolved, and mouse position is not captured. Do not
import this as `.score` until those gaps are qualified against an independent
input or in-game replay receipt.
