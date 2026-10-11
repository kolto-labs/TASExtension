#!/usr/bin/env python3
"""Create a provenance-rich visual input track from an SRC-linked K1 VOD.

This extracts on-screen highlight states only. It does not claim that a
highlight equals a physical key hold or that video-frame boundaries equal the
game's input sampling boundary, and it never emits .score syntax.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

import cv2
import numpy as np

BASE_ARTIFACT = "r4ng3__submitted-20230725__acquired-20261011T003106Z__run-y4ol3jkz__official-vod"
ACQUIRED_AT_UTC = "2026-10-11T00:31:06Z"
RUN_URL = "https://www.speedrun.com/kotor1/runs/y4ol3jkz"
VIDEO_URL = "https://www.youtube.com/watch?v=zdoG5XK6wl0"

# Rectangles were manually calibrated against the lower-left visualizer at
# 1920x1080. Coordinates are x0,y0,x1,y1, with a 3px inset for sampling.
# Explicit physical labels can be mapped to a score token; game-action names
# remain semantic labels because they do not identify a physical binding.
CELLS = [
    ("esc", "Esc", "key", "esc", 7,870,39,902),
    ("f1", "F1", "key", "f1", 72,870,104,902),
    ("f2", "F2", "key", "f2", 106,870,138,902),
    ("f3", "F3", "key", "f3", 139,870,171,902),
    ("f4", "F4", "key", "f4", 172,870,204,902),
    ("grave", "unlabeled key cell", "unresolved_label", None, 7,911,39,943),
    *((str(n), str(n), "key", str(n), 39+(n-1)*33,911,70+(n-1)*33,943) for n in range(1,8)),
    ("tab", "Tab", "key", "tab", 7,944,55,976),
    ("q", "Q", "key", "q", 57,944,88,976),
    ("w", "W", "key", "w", 90,944,120,976),
    ("e", "E", "key", "e", 122,944,153,976),
    ("tabadj_1", "variable action label slot (caption changes)", "variable_label_slot", None, 155,944,186,976),
    ("tabadj_2", "variable action label slot (caption changes)", "variable_label_slot", None, 188,944,218,976),
    ("solo", "SoloMode", "semantic_label", None, 7,982,64,1015),
    ("action_row_1_slot_1", "variable action label slot (caption changes)", "variable_label_slot", None, 66,982,97,1015),
    ("action_row_1_slot_2", "variable action label slot (caption changes)", "variable_label_slot", None, 99,982,129,1015),
    ("action_row_1_slot_3", "variable action label slot (caption changes)", "variable_label_slot", None, 131,982,162,1015),
    ("action_row_1_slot_4", "variable action label slot (caption changes)", "variable_label_slot", None, 164,982,195,1015),
    ("action_row_1_slot_5", "variable action label slot (caption changes)", "variable_label_slot", None, 196,982,227,1015),
    ("enter", "Enter", "key", "enter", 230,982,302,1015),
    ("action_row_2_slot_1", "variable action label slot (caption changes)", "variable_label_slot", None, 7,1017,80,1049),
    ("action_row_2_slot_2", "variable action label slot (caption changes)", "variable_label_slot", None, 81,1017,112,1049),
    ("action_row_2_slot_3", "variable action label slot (caption changes)", "variable_label_slot", None, 114,1017,145,1049),
    ("action_row_2_slot_4", "variable action label slot (caption changes)", "variable_label_slot", None, 147,1017,178,1049),
    ("action_row_2_slot_5", "variable action label slot (caption changes)", "variable_label_slot", None, 180,1017,211,1049),
    ("action_row_2_slot_6", "variable action label slot (caption changes)", "variable_label_slot", None, 212,1017,243,1049),
    ("ctrl", "Ctrl", "unresolved_modifier", None, 7,1049,48,1078),
    ("win", "Win", "unresolved_modifier", None, 50,1049,96,1078),
    ("alt", "Alt", "unresolved_modifier", None, 98,1049,138,1078),
    ("space", "Space", "unresolved_label", None, 140,1049,333,1078),
    ("lmb", "LMB", "mouse_button", "lmb", 410,944,442,976),
    ("rmb", "RMB", "mouse_button", "rmb", 446,944,478,976),
    ("left", "Left", "key", "left", 365,1027,398,1059),
    ("down", "Down", "key", "down", 400,1027,433,1059),
    ("right", "Right", "key", "right", 435,1027,468,1059),
    ("up", "Up", "key", "up", 400,1008,433,1040),
]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video", type=Path, help="locally acquired source video")
    parser.add_argument("--out", type=Path, required=True, help="new observations.json destination")
    parser.add_argument("--start-frame", type=int, default=0, help="zero-based source frame to begin at")
    parser.add_argument("--frame-count", type=int, help="maximum number of source frames to decode; default is full video")
    parser.add_argument("--active-threshold", type=float, default=205.0)
    parser.add_argument("--inactive-threshold", type=float, default=145.0)
    args = parser.parse_args()
    if args.inactive_threshold >= args.active_threshold:
        parser.error("inactive threshold must be lower than active threshold")
    if args.start_frame < 0 or (args.frame_count is not None and args.frame_count < 1):
        parser.error("start-frame must be nonnegative and frame-count must be positive")
    return args


def main() -> int:
    args = parse_args()
    # Limit thread fanout for the small overlay crop. A full-frame OpenCV
    # thread pool costs more than these small reductions.
    cv2.setNumThreads(2)
    video = args.video.resolve(strict=True)
    output = args.out.resolve()
    if output.exists() or output == video:
        raise SystemExit(f"refusing to overwrite source or existing output: {output}")
    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        raise SystemExit(f"cannot decode source video: {video}")
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = cap.get(cv2.CAP_PROP_FPS)
    reported_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    if (width, height) != (1920, 1080) or abs(fps - 60.0) > 0.001:
        raise SystemExit(f"expected calibrated 1920x1080 @ 60fps, got {width}x{height} @ {fps}")
    if any(x0 < 0 or y0 < 0 or x1 > width or y1 > height for *_, x0,y0,x1,y1 in CELLS):
        raise SystemExit("one or more calibrated cells fall outside the video frame")
    cap.set(cv2.CAP_PROP_POS_FRAMES, args.start_frame)
    source_start = int(round(cap.get(cv2.CAP_PROP_POS_FRAMES)))
    if source_start != args.start_frame:
        raise SystemExit(f"decoder could not seek exactly to frame {args.start_frame}; landed at {source_start}")
    if reported_count and source_start >= reported_count:
        raise SystemExit(f"start-frame {source_start} is past end of source ({reported_count} frames)")
    if (args.frame_count is not None and reported_count
            and source_start + args.frame_count > reported_count):
        raise SystemExit(
            f"requested source span [{source_start}, {source_start + args.frame_count}) "
            f"exceeds decoder-reported source length {reported_count}"
        )
    limit = args.frame_count if args.frame_count is not None else max(0, reported_count - source_start)
    artifact = f"{BASE_ARTIFACT}__frames-{source_start}-{source_start + limit}"

    lanes = [
        {"id": ident, "label": label, "kind": kind, "score_token": token,
         "visual_roi": [x0, y0, x1, y1]}
        for ident, label, kind, token, x0, y0, x1, y1 in CELLS
    ]
    events: list[dict] = []
    prior: tuple[tuple[str, ...], tuple[str, ...]] | None = None
    frame = 0
    undecidable_observations = 0
    active_frame_counts = {ident: 0 for ident, *_ in CELLS}
    lane_ids = [cell[0] for cell in CELLS]
    xs0 = np.asarray([cell[4] + 5 for cell in CELLS], dtype=np.intp)
    ys0 = np.asarray([cell[5] + 5 - 865 for cell in CELLS], dtype=np.intp)
    xs1 = np.asarray([cell[6] - 5 for cell in CELLS], dtype=np.intp)
    ys1 = np.asarray([cell[7] - 5 - 865 for cell in CELLS], dtype=np.intp)
    areas = (xs1 - xs0) * (ys1 - ys0)
    # Use only an inset from each key's fill. White highlight is distinct from
    # the steady gray unpressed keycap in sampled official frames.
    while True:
        ok, bgr = cap.read()
        if not ok or (args.frame_count is not None and frame >= args.frame_count):
            break
        # The overlay occupies <5% of the full image. Convert only its bounding
        # box; converting the full 1080p frame made the batch needlessly slow.
        overlay = cv2.cvtColor(bgr[865:1080, 0:480], cv2.COLOR_BGR2GRAY)
        integral = cv2.integral(overlay, sdepth=cv2.CV_32S)
        totals = (integral[ys1, xs1] - integral[ys0, xs1]
                  - integral[ys1, xs0] + integral[ys0, xs0])
        means = totals / areas
        active_mask = means >= args.active_threshold
        uncertain_mask = (means >= args.inactive_threshold) & ~active_mask
        active = [lane_ids[i] for i in np.flatnonzero(active_mask)]
        uncertain = [lane_ids[i] for i in np.flatnonzero(uncertain_mask)]
        for i in np.flatnonzero(active_mask):
            active_frame_counts[lane_ids[i]] += 1
        if uncertain:
            undecidable_observations += len(uncertain)
        state = (tuple(active), tuple(uncertain))
        if state != prior:
            events.append({"frame": frame, "held": list(active), "unknown": list(uncertain)})
            prior = state
        frame += 1
        if frame % 30000 == 0:
            print(f"decoded {frame} frames; state transitions {len(events)}", file=sys.stderr)
    cap.release()
    if frame == 0:
        raise SystemExit("source decoded zero frames in the requested range")
    if args.frame_count is not None and frame != args.frame_count:
        raise SystemExit(
            f"incomplete requested span: decoded {frame} of {args.frame_count} frames "
            f"starting at source frame {source_start}; refusing partial artifact"
        )
    if args.frame_count is None and reported_count and source_start + frame != reported_count:
        print(f"warning: decoder frames end at {source_start + frame}, container reports {reported_count}", file=sys.stderr)

    observation = {
        "schema": "kptas.browser-visible-inputs/v1",
        "artifact": artifact,
        "read_only": True,
        "projection_execution_disabled": True,
        "replay_qualified": False,
        "timeline_mode": "selected-observations",
        "frame_count": frame,
        "source_frame_origin": source_start,
        "nominal_source_fps": 60,
        "source_primary_correspondence_verified": True,
        "physical_inputs_complete": False,
        "source": {
            "run_id": "y4ol3jkz",
            "runner": "R4NG3",
            "submission_date": "2023-07-25",
            "acquired_at_utc": ACQUIRED_AT_UTC,
            "submission_url": RUN_URL,
            "source_video_url": VIDEO_URL,
            "source_relationship": "official video linked by the verified submission record",
            "video_sha256": sha256(video),
            "video_bytes": video.stat().st_size,
            "video_file_name": video.name,
            "video_codec": "H.264",
            "dimensions": [width, height],
            "frame_rate": "60/1",
            "container_duration_seconds": 1447.933333,
            "decoder_frame_count": reported_count,
            "observed_source_frame_range": [source_start, source_start + frame],
            "capture_method": "OpenCV sequential decode from exact sought source frame; source frame index retained",
        },
        "parts": [{
            "source_part": 1,
            "artifact": artifact,
            "first_source_frame": source_start,
            "last_source_frame": source_start + frame - 1,
            "selected_frame_count": frame,
            "observation_path": "observations.json",
            "source_video_url": VIDEO_URL,
        }],
        "state_semantics": {
            "held": "visualizer cells classified as white-highlighted in this video frame; this is not asserted to be a physical key hold",
            "unknown": "cell mean luminance fell in the undecidable interval between inactive and active thresholds",
            "event_compression": "complete state emitted at frame 0 and whenever held/unknown sets change",
        },
        "playback_alignment": {
            "coordinate": "zero-based decoded source-video frame index",
            "approximate_seconds": "source_frame / 60",
            "source_video_frame_alignment": "sequential frame index; CFR 60/1 reported by ffprobe",
            "player_alignment_qualified": False,
            "game_frame_alignment_qualified": False,
            "game_input_sampling_boundary_qualified": False,
            "physical_hold_semantics_qualified": False,
        },
        "visual_extraction": {
            "method": "mean grayscale luminance over manually calibrated visualizer cell interior",
            "roi_coordinate_system": "1920x1080 absolute pixel coordinates, x0/y0 inclusive, x1/y1 exclusive",
            "sampling_inset_pixels": 5,
            "active_threshold_inclusive": args.active_threshold,
            "inactive_threshold_exclusive": args.inactive_threshold,
            "ambiguous_range": [args.inactive_threshold, args.active_threshold],
            "roi_calibration_reference_source_frame": 18000,
            "calibration_reference_time_seconds": 300,
            "calibration_note": "manual inspection confirms the lower-left keyboard/mouse panel and W highlight; cell captions change with game state, so variable action cells are represented by stable visual-slot IDs rather than guessed keys",
            "manual_caption_samples": [
                {"source_frame":18000,"caption_note":"action cells show key-like captions including A/S/D/F/G and Z/X/C/V/B"},
                {"source_frame":21600,"caption_note":"same cells instead show action captions including Items/QS/Equip/Cncl/Opts/Lvl/Round/Swing/Walk"}
            ],
            "undecidable_cell_frame_observations": undecidable_observations,
            "active_frames_by_lane": active_frame_counts,
        },
        "lanes": lanes,
        "events": events,
        "unknown": [
            "Overlay state may represent highlight/tap display rather than sustained physical input.",
            "Frame index follows the official video only; run start, game start, and pauses/loading transitions are not globally aligned.",
            "Game input polling, simulation frame boundary, and hold/release duration are unqualified.",
            "Some visual cells are semantic/ambiguous and intentionally have no score token.",
            "Mouse movement position is not extracted; only labeled LMB/RMB and arrow-like key cells are sampled.",
        ],
        "binding_summary": {
            "tokenized_cells_not_replay_qualified": [lane[0] for lane in CELLS if lane[3] is not None],
            "unmapped_or_semantic_cells": [lane[0] for lane in CELLS if lane[3] is None],
            "replay_disabled_reason": "visual highlight semantics, physical hold duration, game-frame alignment, and initial game state are not qualified",
        },
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=output.name + ".", suffix=".partial", dir=output.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(observation, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, output)
    except BaseException:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise
    print(json.dumps({
        "output": str(output), "video_sha256": observation["source"]["video_sha256"],
        "source_frames": [source_start, source_start + frame],
        "decoded_frames": frame, "source_fps": fps, "transition_events": len(events),
        "lanes": len(lanes), "tokenized_cells_not_replay_qualified": len(observation["binding_summary"]["tokenized_cells_not_replay_qualified"]),
        "uncertain_cell_frame_observations": undecidable_observations,
        "replay_qualified": False,
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
