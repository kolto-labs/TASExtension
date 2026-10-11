# Speedrun segment intake

This SSD-local copy adds an explicit, opt-in path for catalogued videos whose
relationship to an official submission has not been verified. It still emits
only review evidence: it never emits score syntax, never qualifies input timing,
and never promotes visible highlights into physical key presses.

## R4NG3 official linked video

R4NG3's verified K1 Any% Unrestricted submission `y4ol3jkz` has an official SRC-linked YouTube video, separate from the previously catalogued alternate Twitch highlight. The source is 1920x1080 H.264 at 60/1 fps, 24:07.933 long, and visibly includes a keyboard/mouse panel in the lower-left. The scratch copy finished downloading at 2026-10-11T00:31:06Z; it remains outside the SSD checkout because the SSD has under 1 GiB free. `extract-visible-keyboard.py` samples that panel into frame-indexed visual observations. Its metadata preserves the exact source-video SHA-256, decoded source-frame span, calibrated cell ROIs, thresholds, transition states, and unresolved/semantic lanes. Use `--start-frame` and `--frame-count` to process bounded slices; the extractor now refuses out-of-range or incomplete requested spans. The full-VOD observation track covers all 86,876 decoded frames (24m07.933s), stored as 25 contiguous provenance parts and compressed to 6,614 visible-state transitions, with 1,342 ambiguous cell/frame observations preserved. It is not a full-run physical input recording: the visualizer cannot establish physical key-down duration, game/input-poll alignment, or mouse movement. `replay_qualified` remains false, no `.score` was emitted, and the track has not been added to the demo catalog.

These observations are not executable: a visualizer highlight may represent a tap rather than the physical hold interval, game start and per-frame input sampling are not aligned, and some cells label semantic actions instead of physical keys. Keep `replay_qualified` false and do not convert to `.score` until those semantics have independent evidence. Keep the prior R4NG3 Twitch highlight (`1880940873`) separate from the official YouTube source (`zdoG5XK6wl0`).

Manual comparison of source frames 18,000 and 21,600 showed that keyboard-shaped cells change captions from key-like values to action labels such as Items, QS, Equip, Opts, and Round. The extractor therefore records these as stable visual slots with no guessed key binding. It no longer labels the bright QS cell as physical D. Only 23 cells have a candidate score token, and even those remain unqualified for replay.

To merge contiguous, matching slices after extraction:

```sh
python reference-runs/imported-k1/tools/merge-visible-tracks.py \
  reference-runs/imported-k1/official/RUN__frames-START-END/observations.json \
  reference-runs/imported-k1/official/RUN__frames-END-NEXT/observations.json \
  --out reference-runs/imported-k1/official/RUN__frames-START-NEXT/observations.json
```

The merger requires identical video hashes and lane/ROI definitions, sorted contiguous source frame spans, and complete-state events. It preserves each slice as a provenance part and removes only duplicate boundary states. It will not overwrite existing outputs.

The local speedrun catalog contains K1 submission references and derived input-observation JSON. It does not contain the Twitch video files or the original compressed observation streams. The strongest local source relationship is the Otterito Any% No Major Glitches run `megnd53y`: the catalog says its Twitch video is directly linked in the verified submission. Its selected-controls observation export has 385,259 observation frames, 109 visible lanes, 16 unresolved bindings, 4,110 frames with active unmapped controls, and `replay_qualified: false`.

R4NG3's `y4ol3jkz` record points at a runner-hosted alternate highlight. The catalog explicitly says that its correspondence to the submission video is unverified, so the segment extractor refuses it. The separate 600-frame Otterito sample is submission-linked, but its estimated video origin is 410.017 seconds and `player_alignment_qualified` is false. It is a calibration lead, not a globally aligned input trace. No K2 speedrun submission artifact is present in this local catalog.

## Extract a bounded evidence excerpt

Use the selected-observation frame ordinals shown in the speedrun viewer. These ordinals address the observed sequence; they do not establish a Twitch player time or a game tick. The end frame is exclusive.

```sh
node demo/tools/extract-speedrun-segment.mjs demo/public/media/speedruns/otterito__submitted-20241015__acquired-20261004T072747.150864Z__run-megnd53y__observed-controls/observations.json --from-frame START_FRAME --to-frame END_FRAME --out /path/to/new-excerpt.json
```

Replace `START_FRAME` and `END_FRAME` with the chosen zero-based ordinal bounds. The destination must not already exist. The tool verifies the input's replay-disabled observation schema, requires an adjacent catalog entry with a directly linked submission video, and confirms that the input path matches that entry. The output records the exact source observation and catalog SHA-256 values, submission/video identity, inclusive/exclusive frame bounds, the prior held state at the start, all observed state changes in the range, lane definitions, unresolved notes, alignment limits, and a bounded count of active unmapped states.

The output is `kptas.speedrun-segment-evidence/v1` JSON, not `.score` syntax. It preserves the source frame/decoder metadata but does not convert visible highlights into physical inputs or nominal frames into TAS ticks. Use it as a review packet for manual route authoring; source alignment, complete physical input, initial game state, and route replay still need independent qualification.

## Executable-score qualification: official R4NG3 full VOD

The complete official linked VOD observation artifact is
`official/r4ng3__submitted-20230725__acquired-20261011T003106Z__run-y4ol3jkz__frames-0-86876/observations.json`.
It contains 86,876 sequential decoded source frames at reported CFR 60/1,
42 visual lanes, and 6,614 compressed state transitions. Twenty-three lanes
have explicit score-token mappings (`esc`, function/number keys, `tab`, `q/w/e`,
`enter`, mouse buttons, and arrows); the other 19 are semantic or unmapped.
The artifact's lane mapping is a label hypothesis, not input-timing
qualification. Its metadata explicitly leaves player alignment, game-frame
alignment, game input-sampling boundary, and physical hold semantics false.

A useful review interval is source frames `[31672,34451)` (about 08:48.0–09:34.2
on the video clock): 77 visual state transitions, no undecidable cells in those
transitions, and only explicitly tokenized lanes observed active (`e`, `lmb`,
`q`, `rmb`, `tab`, `w`). This is a candidate for human route review, not a
defensible executable `.score`: white overlay highlights may represent taps or
visualizer state rather than physical down duration, the video frame clock is
not qualified against game polling, and the interval has no matched initial
save/game state. Emitting score syntax would silently invent both held-input
durations and the state from which to execute it. The full observation track
remains read-only, with `replay_qualified: false` and
`physical_inputs_complete: false`.

The second-pass screen in
`official/candidate-screening-second-pass-20261011/README.md` checked short
samples from two newer official R4NG3 All Quests VODs and one Hamptermiku Any%
VOD. All are reported 60 fps, but no readable input overlay was visible in the
sampled portions. This narrows the candidate set without proving the rest of
any VOD lacks an overlay. Do not promote these sources to input traces unless
a longer/full-source inspection finds labeled controls with usable timing.

## R4NG3 alternate-highlight excerpt

R4NG3's `y4ol3jkz` catalog item has 80,400 observed frames and 43 lanes. The local observation metadata says the alternate highlight is not verified as the video attached to the official submission, and its HLS parts are not frame-accurately aligned to the player. A deliberate opt-in can now export a bounded packet while preserving that distinction and clipping the part/frame coverage to the requested excerpt:

```sh
node reference-runs/imported-k1/tools/extract-speedrun-segment.mjs demo/public/media/speedruns/r4ng3__submitted-20230725__acquired-20261003T224804Z__run-y4ol3jkz/observations.json --from-frame 21000 --to-frame 21600 --out reference-runs/imported-k1/evidence/r4ng3__submitted-20230725__acquired-20261003T224804Z__run-y4ol3jkz__frames-21000-21600.json --allow-unverified-source
```

`--allow-unverified-source` is required for this alternate highlight; without it the default verified-submission gate remains in force. The resulting excerpt records the run and submission identity, video URL and warning that it is not verified as the submission video, SHA-256 digests for the observations and catalog, the exact half-open frame range, overlapping source-part descriptors, lane/key-token map, active unmapped controls, and the original alignment/unknown-input qualifications. Frame 21,000–21,599 is inside source part 1 (HLS segments 645–777); the segment list is part-level evidence, not a per-frame HLS map. The generated packet remains `replay_qualified: false` and `executable_score: false`.

## New official R4NG3 VOD screening (2026-10-10)

The official K1 R4NG3 All Quests run `z047r4ey` links a 1920x1080 AVC VOD
reported by yt-dlp at 30 fps; it is excluded from the requested 60 fps input
corpus. The 60 fps official R4NG3 runs `y9q9r0nz` and `zg0rgjvm` were sampled
again at about 25, 55, and 85 minutes. These later samples show gameplay,
webcam, splits, and game HUD only, with no keyboard/mouse visualization. The
bounded screening receipt and contact sheet are in
`official/candidate-screening-third-pass-20261010/README.md`. No input trace or
score was emitted because no visible input control state was present in the
reviewed frames. These samples do not prove the rest of either video lacks an
overlay.
