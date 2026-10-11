#!/usr/bin/env python3
"""Merge contiguous visual-observation slices from the same source VOD."""
from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tracks", nargs="+", type=Path, help="contiguous observations.json slices")
    parser.add_argument("--out", required=True, type=Path, help="new merged observations.json")
    args = parser.parse_args()
    docs = []
    for path in args.tracks:
        doc = json.loads(path.read_text(encoding="utf-8"))
        if doc.get("schema") != "kptas.browser-visible-inputs/v1" or doc.get("replay_qualified") is not False:
            raise SystemExit(f"not a replay-disabled visual track: {path}")
        if not doc.get("events") or doc["events"][0].get("frame") != 0:
            raise SystemExit(f"missing frame-zero complete state: {path}")
        start = doc.get("source_frame_origin")
        count = doc.get("frame_count")
        if not isinstance(start, int) or not isinstance(count, int) or count < 1:
            raise SystemExit(f"invalid source frame span: {path}")
        docs.append((start, path.resolve(), doc))
    docs.sort(key=lambda item: item[0])
    first_start, first_path, first = docs[0]
    if any(doc.get("source", {}).get("video_sha256") != first.get("source", {}).get("video_sha256") for _, _, doc in docs):
        raise SystemExit("cannot merge different source-video hashes")
    if any(doc.get("lanes") != first.get("lanes") for _, _, doc in docs):
        raise SystemExit("cannot merge different lane definitions/ROIs")
    cursor = first_start
    for start, _, doc in docs:
        if start != cursor:
            raise SystemExit(f"source slices are not contiguous at frame {cursor}; next starts at {start}")
        if doc["events"][-1].get("frame", -1) >= doc["frame_count"]:
            raise SystemExit("event lies outside its observation slice")
        previous = -1
        for event in doc["events"]:
            if not isinstance(event.get("frame"), int) or event["frame"] < 0 or event["frame"] <= previous:
                raise SystemExit("events must have strictly increasing in-range frame ordinals")
            if not isinstance(event.get("held"), list) or not isinstance(event.get("unknown"), list):
                raise SystemExit("each event must provide complete held and unknown lists")
            previous = event["frame"]
        cursor += doc["frame_count"]

    merged = json.loads(json.dumps(first))
    end = cursor
    merged["artifact"] = f"{first['artifact'].split('__frames-')[0]}__frames-{first_start}-{end}"
    merged["frame_count"] = end - first_start
    merged["source_frame_origin"] = first_start
    merged["source"]["observed_source_frame_range"] = [first_start, end]
    merged["source"]["capture_method"] = "contiguous exact-seek source-video slices; original source-frame indices retained"
    merged["parts"] = []
    merged_events = []
    last_state = None
    undecidable = 0
    active_counts = {lane["id"]: 0 for lane in first["lanes"]}
    for start, path, doc in docs:
        count = doc["frame_count"]
        rel_path = os.path.relpath(path, args.out.resolve().parent)
        for part in doc.get("parts", []):
            merged["parts"].append({
                **part,
                "source_part": len(merged["parts"]) + 1,
                "observation_path": rel_path,
            })
        undecidable += doc["visual_extraction"]["undecidable_cell_frame_observations"]
        for lane_id, number in doc["visual_extraction"]["active_frames_by_lane"].items():
            active_counts[lane_id] += number
        for index, event in enumerate(doc["events"]):
            state = (event["held"], event["unknown"])
            if index == 0 and state == last_state:
                continue
            merged_events.append({"frame": start - first_start + event["frame"], "held": event["held"], "unknown": event["unknown"]})
            last_state = state
    merged["events"] = merged_events
    merged["visual_extraction"]["undecidable_cell_frame_observations"] = undecidable
    merged["visual_extraction"]["active_frames_by_lane"] = active_counts
    out = args.out.resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists() or out in {path for _, path, _ in docs}:
        raise SystemExit(f"refusing to overwrite an input/output file: {out}")
    fd, temp = tempfile.mkstemp(prefix=out.name + ".", suffix=".partial", dir=out.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(merged, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, out)
    except BaseException:
        try:
            os.unlink(temp)
        except FileNotFoundError:
            pass
        raise
    print(json.dumps({"output": str(out), "source_frames": [first_start, end], "observations": merged["frame_count"], "events": len(merged_events), "parts": len(merged["parts"]), "replay_qualified": False}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
