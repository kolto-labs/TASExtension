# Third-source screening: no qualifying input overlay found

This bounded visual screen followed the official Speedrun.com verified K1 runs API (`https://www.speedrun.com/api/v1/runs?game=9dokkk1p&status=verified&orderby=verify-date&direction=desc&max=200&embed=players,category,platform`) and sampled the linked public VODs below. All four sources are distinct from Otterito `megnd53y` and every R4NG3 run. yt-dlp reported each sampled stream as 1920x1080 at 60 fps; each run page supplies the VOD link. The frames here are screening receipts, not input extractions.

| Run | Runner / category | Official run and linked VOD | Sample | Visual finding |
|---|---|---|---|---|
| `z17l43gz` | NinkoHD, Any% | https://www.speedrun.com/kotor1/runs/z17l43gz · https://youtu.be/ta87e5oq-e0 | about 00:33 | Legible split panel and timer; no keyboard/mouse display. [Frame](ninko-z17l43gz-source-t033s.png) |
| `zxx030ez` | TheLadiesLoveMe, Any% | https://www.speedrun.com/kotor1/runs/zxx030ez · https://www.youtube.com/watch?v=vB7voCSw9K8 | about 00:32 | Legible split panel and game footage; no keyboard/mouse display. [Frame](theladiesloveme-zxx030ez-source-t032s.png) |
| `z08ne7em` | ChaosDrifter, Any% | https://www.speedrun.com/kotor1/runs/z08ne7em · https://www.twitch.tv/videos/2375876305 | about 00:37 | Stable 60 fps source; sampled output is a montage with webcam and timer. No readable key/button map. [Frame](chaosdrifter-z08ne7em-source-t037s.png) |
| `zqj81gxm` | BuildNye, Any% | https://www.speedrun.com/kotor1/runs/zqj81gxm · https://www.youtube.com/watch?v=6THuVZUWhZY | about 00:37 | Clear game capture and split timer; no keyboard/mouse display. [Frame](buildnye-zqj81gxm-source-t037s.png) |

Each screenshot was decoded from a short public-VOD excerpt requested around the stated source time. The image is a frame from the acquired/re-encoded excerpt; it is not a frame-exact source archive or a full-VOD checksum. Speedrun.com API and yt-dlp metadata supported the linked-run, resolution, duration, and frame-rate checks. The visible layout supports only the findings above; it does not prove that no input overlay appears at another time in those VODs.

None of these four samples provides the required readable, stable keyboard/mouse panel. I therefore did not select one as an input source, create a replay trace, or emit `.score` data. A distinct qualifying source remains unidentified. These artifacts only narrow the search; an unsampled interval or another verified submission could still contain a suitable overlay.

SHA-256:

- `ninko-z17l43gz-source-t033s.png`: `9e4ab81625179779598324e8116ff66c6a462cf5d02a7f3fe273f8128d7a73c7`
- `theladiesloveme-zxx030ez-source-t032s.png`: `27fac0fc649def4a9fadb9e4e41a45c8180c603a620ca46cb4595bd99a669e96`
- `chaosdrifter-z08ne7em-source-t037s.png`: `d26c3b0c5b0c9ece084972df0879a9c991a64fd4d36135561783893ac077c82f`
- `buildnye-zqj81gxm-source-t037s.png`: `ff0fd380ee1f47cb5bae0f5925c4fa3443bbb64a65e06eb0a5d9fc8b89b4f702`
