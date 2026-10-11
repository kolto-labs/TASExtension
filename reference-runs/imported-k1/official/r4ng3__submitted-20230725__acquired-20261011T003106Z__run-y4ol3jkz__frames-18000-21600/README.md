# R4NG3 official VOD visual input excerpt

This is a one-minute visual observation track from source-video frames
18,000–21,599 (300–359.983s at the source's reported 60/1 fps). The source is
the video linked by verified K1 submission `y4ol3jkz`, not the previously
catalogued Twitch alternate highlight.

`observations.json` contains 3,600 decoded source frames compressed into 238
complete-state transitions across 42 overlay cells. The artifact stores the
source video SHA-256, source URL, frame range, ROI coordinates, extraction
thresholds, lane labels, and unresolved cells. The W highlight visible in the
calibration frame at 300s is present at observation frame 0. This is a source
consistency check, not a replay test.

## Replay status

`replay_qualified` and physical-input completeness are **false**. The track
records what the on-screen visualizer highlighted, not guaranteed physical
key-down intervals. It is not aligned to game start, game frames, or the
engine's input polling. Semantic cells and some modifier/space labels do not
have confirmed bindings. Mouse position is not captured. It is suitable for
review and later alignment, not `.score` execution.

## Reproduce the extraction

The source MP4 remains in scratch storage and is not committed. Re-run from a
copy of the exact source and compare the recorded SHA-256 before trusting the
result:

```sh
python reference-runs/imported-k1/tools/extract-visible-keyboard.py SOURCE.mp4 \
  --start-frame 18000 --frame-count 3600 \
  --out reference-runs/imported-k1/official/r4ng3__submitted-20230725__acquired-20261011T003106Z__run-y4ol3jkz__frames-18000-21600/observations.json
```

The extractor refuses to overwrite an existing output. Its bounded mode seeks
to an exact source frame and fails closed if the decoder does not land there.
