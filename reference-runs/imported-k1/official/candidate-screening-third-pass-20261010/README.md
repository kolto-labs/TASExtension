# Official R4NG3 VOD candidate screen — third pass

Screening captured locally on 2026-10-10. It was aimed at finding additional
official-source K1 input displays, not proving whole-video absence.

## Candidates and decision

| SRC run | Official source | Video metadata | Sampled source times | Visible input display? |
| --- | --- | --- | --- | --- |
| [`y9q9r0nz`](https://www.speedrun.com/kotor1/runs/y9q9r0nz), All Quests | [ODXA_zYR91Y](https://youtu.be/ODXA_zYR91Y) | 1920x1080, reported 60 fps; sampled 1280x720 AVC stream is 60/1 | ~25:02, ~55:02, ~85:02 | No separate keyboard/mouse panel in the inspected frames |
| [`zg0rgjvm`](https://www.speedrun.com/kotor1/runs/zg0rgjvm), All Quests | [1eslOMutFs8](https://youtu.be/1eslOMutFs8) | 1920x1080, reported 60 fps; sampled 1280x720 AVC stream is 60/1 | ~25:02, ~55:02, ~85:02 | No separate keyboard/mouse panel in the inspected frames |
| [`z047r4ey`](https://www.speedrun.com/kotor1/runs/z047r4ey), All Quests | [Z3GzaDD5pHM](https://youtu.be/Z3GzaDD5pHM) | 1920x1080 AVC, 30 fps, 1:44:55; 1080p estimate ~1.32 GB | Metadata only | No video sample. Excluded because it fails the requested 60 fps criterion. |

The first two VODs were already sampled around 05:00 in the prior screening
pass. Six additional short excerpts were requested with yt-dlp at
`[1500,1508)`, `[3300,3305)`, and `[5100,5105)` seconds for each source. Each
selected stream reports 60/1 fps; the contact sheet samples the decoded local
clip at +2 seconds, corresponding approximately to source times 1502, 3302,
and 5102 seconds. These are screening timestamps only, not a per-frame input
trace or qualified game-frame mapping. Full clip files remained in temporary
NVMe scratch and are not part of this receipt.

At those six points the first VOD shows the game, webcam, and split/timer
overlay; the second shows the game, webcam, split/timer overlay, and native
game HUD. Neither inspected frame shows a separate keyboard/mouse visualizer.
The contact sheet is [`r4ng3-two-official-60fps-vods.jpg`](r4ng3-two-official-60fps-vods.jpg),
SHA-256 `30ff1c8e7a8b721571e94825d2f12d504cf1b49a7261b4eaa1402f83f83cd013`.

No input observations or scores were created from these new samples. The
reviewed frames do not expose input state, and sparse sampling does not prove
that the rest of either VOD lacks a control display. The third run was not
downloaded: its reported 30 fps disqualifies it, and its estimated full-source
size exceeds the remaining SSD capacity at the time of review. The existing
R4NG3 official 60 fps input-observation track for `y4ol3jkz` remains the best
qualified R4NG3 source in this corpus; it is still visual evidence only and
remains disabled for replay pending independent physical-input and game-time
alignment evidence.
