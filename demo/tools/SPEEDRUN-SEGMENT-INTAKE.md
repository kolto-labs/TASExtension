# Speedrun segment intake

The local speedrun catalog contains K1 submission references and derived input-observation JSON. It does not contain the Twitch video files or the original compressed observation streams. The strongest local source relationship is the Otterito Any% No Major Glitches run `megnd53y`: the catalog says its Twitch video is directly linked in the verified submission. Its selected-controls observation export has 385,259 observation frames, 109 visible lanes, 16 unresolved bindings, 4,110 frames with active unmapped controls, and `replay_qualified: false`.

R4NG3's `y4ol3jkz` record points at a runner-hosted alternate highlight. The catalog explicitly says that its correspondence to the submission video is unverified, so the segment extractor refuses it. The separate 600-frame Otterito sample is submission-linked, but its estimated video origin is 410.017 seconds and `player_alignment_qualified` is false. It is a calibration lead, not a globally aligned input trace. No K2 speedrun submission artifact is present in this local catalog.

## Extract a bounded evidence excerpt

Use the selected-observation frame ordinals shown in the speedrun viewer. These ordinals address the observed sequence; they do not establish a Twitch player time or a game tick. The end frame is exclusive.

```sh
node demo/tools/extract-speedrun-segment.mjs demo/public/media/speedruns/otterito__submitted-20241015__acquired-20261004T072747.150864Z__run-megnd53y__observed-controls/observations.json --from-frame START_FRAME --to-frame END_FRAME --out /path/to/new-excerpt.json
```

Replace `START_FRAME` and `END_FRAME` with the chosen zero-based ordinal bounds. The destination must not already exist. The tool verifies the input's replay-disabled observation schema, requires an adjacent catalog entry with a directly linked submission video, and confirms that the input path matches that entry. The output records the exact source observation and catalog SHA-256 values, submission/video identity, inclusive/exclusive frame bounds, the prior held state at the start, all observed state changes in the range, lane definitions, unresolved notes, alignment limits, and a bounded count of active unmapped states.

The output is `kptas.speedrun-segment-evidence/v1` JSON, not `.score` syntax. It preserves the source frame/decoder metadata but does not convert visible highlights into physical inputs or nominal frames into TAS ticks. Use it as a review packet for manual route authoring; source alignment, complete physical input, initial game state, and route replay still need independent qualification.
