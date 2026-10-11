# Second-pass official K1 VOD screening

This is a bounded source-screening pass over newer official verified K1
submissions. It records where a short public-VOD sample was inspected and what
was visibly present. The montage files are screening receipts, not source
captures, input extractions, or proof that an overlay is absent elsewhere.

| Run | Runner / category | Submission / verification | Official linked video | Source metadata | Sample and finding |
|---|---|---|---|---|---|
| [`y9q9r0nz`](https://www.speedrun.com/kotor1/runs/y9q9r0nz) | R4NG3, All Quests | 2026-08-06 / 2026-08-08 | [YouTube](https://youtu.be/ODXA_zYR91Y) | yt-dlp: 1920x1080, AV1, 60 fps, 1:46:08.97; SRC run time 1:45:12 | Four frames sampled near source time 00:05:00. Game, webcam, timer/splits visible; no readable keyboard/mouse input panel. [Montage](r4ng3-y9q9r0nz-source-300s-samples.jpg) |
| [`zg0rgjvm`](https://www.speedrun.com/kotor1/runs/zg0rgjvm) | R4NG3, All Quests | 2026-08-08 / 2026-08-09 | [YouTube](https://youtu.be/1eslOMutFs8) | yt-dlp: 1920x1080, AV1, 60 fps, 1:48:48.98; SRC run time 1:44:21 | Four frames sampled near source time 00:05:00. Game, webcam, timer/splits visible; no readable keyboard/mouse input panel. [Montage](r4ng3-zg0rgjvm-source-300s-samples.jpg) |
| [`z14l1l9z`](https://www.speedrun.com/kotor1/runs/z14l1l9z) | Hamptermiku, Any% | 2026-09-02 / 2026-09-06 | [YouTube](https://youtu.be/70JjtyIGwsI) | yt-dlp: 960x720, 60 fps, 2:10:16; SRC run time 2:05:57 | Four frames sampled near source time 00:05:00. Game, webcam, timer/splits visible; no readable keyboard/mouse input panel. [Montage](hamptermiku-z14l1l9z-source-300s-samples.jpg) |

The query used the public Speedrun.com verified K1 runs endpoint, ordered by
verification date (`game=9dokkk1p`, verified status, maximum 200, with player
and category embeds). yt-dlp reported the source dimensions, codecs, rates,
durations, and upload metadata. Samples were short excerpts requested around
five minutes, then represented as four-frame contact sheets. No full VOD was
downloaded or frame-indexed in this pass. A negative finding applies only to
the sampled time; an overlay could appear elsewhere or in another source.

All three sources meet the reported 60 fps screening condition, but none of
these sampled excerpts exposes a usable input overlay. They are therefore not
input sources and no score or replay artifact is emitted from them. The R4NG3
All Quests VOD/run-time duration gaps are also a cue to align from observed
source timestamps rather than assume the submitted timer is the video clock.

SHA-256 of screening montages:

- `r4ng3-y9q9r0nz-source-300s-samples.jpg`: `f6aa648342319fec3f15c781082d6edab66c6300401285c88c19337fac902f98`
- `r4ng3-zg0rgjvm-source-300s-samples.jpg`: `d95969a857a37750a08dd84ddd5070bdec0df119d1aa147aa5719696f01ca86b`
- `hamptermiku-z14l1l9z-source-300s-samples.jpg`: `ef62f57a154844153015a8ed00e9c916608e49ba96c8d430ab77ac5ed6a329d1`

## Follow-up: R4NG3's August 2026 All Quests record

The verified R4NG3 submission [`z047r4ey`](https://www.speedrun.com/kotor1/runs/z047r4ey)
links to [the 1:22:20 All Quests (Unrestricted) VOD](https://youtu.be/Z3GzaDD5pHM).
SRC records the run as submitted 2026-08-08 with a 1:39:23 real-time result.
`yt-dlp --skip-download --dump-single-json` reports the uploaded VOD as
1920x1080 AVC at 30 fps, 1:44:55 long; its selected 1080p stream is estimated
at 1.32 GB. This does **not** meet the requested 60 fps source criterion, so
no video was downloaded or decoded and no keyboard/mouse visibility is claimed.
It is excluded from input extraction. The gap between the VOD duration and the
SRC real-time result is 5:32; neither clock should be substituted for decoded
frame timestamps. Reconsider only if a distinct 60 fps source for this same
official run becomes available.
