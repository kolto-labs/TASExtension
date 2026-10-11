#!/usr/bin/env node
// Extract a bounded, read-only evidence packet from a catalogued speedrun
// input-observation track. This never emits Stasis score syntax.
import fs from 'node:fs';
import path from 'node:path';
import process from 'node:process';
import { createHash } from 'node:crypto';

const SCHEMA = 'kptas.browser-visible-inputs/v1';
const OUTPUT_SCHEMA = 'kptas.speedrun-segment-evidence/v1';
const MAX_INPUT_BYTES = 64 * 1024 * 1024;

const HELP = `
Usage:
  node extract-speedrun-segment.mjs OBSERVATIONS.json --from-frame N --to-frame N --out excerpt.json [--allow-unverified-source]

Frames are zero-based selected-observation ordinals; --to-frame is exclusive.
By default the source must match a video directly linked in the verified run
submission. --allow-unverified-source permits a catalogued alternate highlight
whose submission correspondence is unverified. Output is read-only evidence,
never a score. The destination must not already exist.
`;

function parseArgs(argv) {
  if (argv.includes('--help') || argv.includes('-h')) throw new Error(HELP);
  const input = argv[0];
  const options = { from: null, to: null, out: null, allowUnverifiedSource: false };
  for (let i = 1; i < argv.length; i++) {
    const flag = argv[i];
    if (flag === '--from-frame') options.from = Number(argv[++i]);
    else if (flag === '--to-frame') options.to = Number(argv[++i]);
    else if (flag === '--out') options.out = argv[++i];
    else if (flag === '--allow-unverified-source') options.allowUnverifiedSource = true;
    else throw new Error(`Unknown option: ${flag}\n${HELP}`);
  }
  if (!input || !options.out || !Number.isSafeInteger(options.from) || !Number.isSafeInteger(options.to)) {
    throw new Error(HELP);
  }
  if (options.from < 0 || options.to <= options.from) throw new Error('Frame bounds must satisfy 0 <= from < to.');
  return { input: path.resolve(input), ...options };
}

function readJson(file, maxBytes = MAX_INPUT_BYTES) {
  const stat = fs.statSync(file);
  if (!stat.isFile() || stat.size > maxBytes) throw new Error(`Refusing non-file or oversized input: ${file}`);
  const bytes = fs.readFileSync(file);
  let value;
  try { value = JSON.parse(bytes.toString('utf8')); }
  catch { throw new Error(`Invalid JSON: ${file}`); }
  return { bytes, value };
}

function catalogRun(inputPath, artifactId, allowUnverifiedSource) {
  const catalogPath = path.resolve(path.dirname(inputPath), '..', 'catalog.json');
  if (!fs.existsSync(catalogPath)) throw new Error('No adjacent speedrun catalog; refusing to infer source identity.');
  const { bytes, value } = readJson(catalogPath, 4 * 1024 * 1024);
  if (value?.schema !== 'kptas.browser-speedruns/v1' || !Array.isArray(value.runs)) {
    throw new Error('Unsupported speedrun catalog.');
  }
  const matches = value.runs.filter(run => run.id === artifactId);
  if (matches.length !== 1) throw new Error('Observation artifact does not map to exactly one catalog run.');
  const run = matches[0];
  const linked = typeof run.source_relationship === 'string'
    && /directly linked in (the )?verified submission/i.test(run.source_relationship);
  if ((!linked && !allowUnverifiedSource) || !run.submission_url || !run.source_video_url) {
    throw new Error('Catalog does not verify this video against the submission; pass --allow-unverified-source to export review evidence with that limitation preserved.');
  }
  const expected = path.resolve(path.dirname(catalogPath), '..', '..', run.observations || '');
  if (expected !== inputPath) throw new Error('Observation path does not match the catalog entry.');
  return { bytes, run, linked };
}

function clippedParts(parts, from, to, totalFrames) {
  if (!Array.isArray(parts)) return [];
  let selectedStart = 0;
  const clipped = [];
  for (const part of parts) {
    const first = part.first_source_frame;
    const last = part.last_source_frame;
    const count = part.selected_frame_count;
    if (!Number.isSafeInteger(first) || !Number.isSafeInteger(last) || !Number.isSafeInteger(count)
        || first < 0 || last < first || count < 1) {
      throw new Error('Cannot map selected-observation ordinals: source part lacks valid frame bounds/count.');
    }
    const sourceSpan = last - first + 1;
    if (count !== sourceSpan) {
      throw new Error(`Cannot map selected-observation ordinals for source part ${part.source_part ?? '?'}: ${count} selected frames span ${sourceSpan} source-frame ids; non-1:1 sampling is unsupported.`);
    }
    const selectedEndExclusive = selectedStart + count;
    const excerptStart = Math.max(from, selectedStart);
    const excerptEndExclusive = Math.min(to, selectedEndExclusive);
    if (excerptStart < excerptEndExclusive) {
      const sourceStart = first + (excerptStart - selectedStart);
      const sourceEnd = first + (excerptEndExclusive - selectedStart) - 1;
      clipped.push({
        ...part,
        excerpt_selected_from_frame: excerptStart,
        excerpt_selected_to_frame_exclusive: excerptEndExclusive,
        excerpt_source_first_frame: sourceStart,
        excerpt_source_last_frame: sourceEnd,
        excerpt_frame_count: excerptEndExclusive - excerptStart,
      });
    }
    selectedStart = selectedEndExclusive;
  }
  if (selectedStart !== totalFrames) {
    throw new Error(`Cannot map selected-observation ordinals: source parts cover ${selectedStart} selected frames, expected ${totalFrames}.`);
  }
  return clipped;
}

function activeCounts(start, end, state, events, lanes) {
  const byId = new Map(lanes.map(lane => [lane.id, lane]));
  const totals = { unresolved_visible_frames: 0, active_unmapped_frames: 0, active_unmapped_lane_ids: new Set() };
  let cursor = start;
  const addInterval = (next, current) => {
    const frames = Math.max(0, next - cursor);
    if (current.unknown?.length) totals.unresolved_visible_frames += frames;
    for (const id of current.held || []) {
      const lane = byId.get(id);
      if (!lane || lane.score_token == null) {
        totals.active_unmapped_frames += frames;
        totals.active_unmapped_lane_ids.add(id);
      }
    }
    cursor = next;
  };
  let current = state;
  for (const event of events) {
    if (event.frame <= start || event.frame >= end) continue;
    addInterval(event.frame, current);
    current = event;
  }
  addInterval(end, current);
  return {
    unresolved_visible_frames: totals.unresolved_visible_frames,
    active_unmapped_frames: totals.active_unmapped_frames,
    active_unmapped_lane_ids: [...totals.active_unmapped_lane_ids].sort(),
  };
}

function extract({ input, from, to, out, allowUnverifiedSource }) {
  const { bytes, value } = readJson(input);
  const observation = value;
  const strictReadOnly = observation?.timeline_mode === 'selected-observations'
      && observation.read_only === true && observation.projection_execution_disabled === true;
  const legacyObservation = observation?.source_primary_correspondence_verified === false
      && observation?.physical_inputs_complete === false;
  if (observation?.schema !== SCHEMA || (!strictReadOnly && !legacyObservation)
      || observation.replay_qualified !== false) {
    throw new Error('Input is not a replay-disabled selected-observation artifact.');
  }
  if (!Number.isSafeInteger(observation.frame_count) || to > observation.frame_count
      || !Array.isArray(observation.events) || !Array.isArray(observation.lanes)
      || !observation.events.length || observation.events[0]?.frame !== 0) {
    throw new Error('Observation track has invalid frame bounds or no frame-zero state.');
  }
  let previousFrame = -1;
  for (const event of observation.events) {
    if (!Number.isSafeInteger(event.frame) || event.frame < 0 || event.frame >= observation.frame_count || event.frame < previousFrame
        || !Array.isArray(event.held) || !Array.isArray(event.unknown)) {
      throw new Error('Observation events are not ordered complete-state records.');
    }
    previousFrame = event.frame;
  }
  const { bytes: catalogBytes, run, linked } = catalogRun(input, observation.artifact, allowUnverifiedSource);
  const stateEvent = observation.events.filter(event => event.frame <= from).at(-1);
  if (!stateEvent) throw new Error('No captured input state is available at the segment start.');
  const events = observation.events.filter(event => event.frame >= from && event.frame < to);
  const counts = activeCounts(from, to, stateEvent, events, observation.lanes);
  const packet = {
    schema: OUTPUT_SCHEMA,
    read_only: true,
    executable_score: false,
    replay_qualified: false,
    source: {
      artifact_id: observation.artifact,
      observation_schema: observation.schema,
      observation_sha256: createHash('sha256').update(bytes).digest('hex'),
      observation_bytes: bytes.length,
      catalog_sha256: createHash('sha256').update(catalogBytes).digest('hex'),
      run_id: run.id,
      runner: run.runner,
      submitted: run.submitted,
      category: run.category,
      submission_url: run.submission_url,
      source_video_url: run.source_video_url,
      source_relationship: run.source_relationship,
      source_primary_correspondence_verified: observation.source_primary_correspondence_verified ?? linked,
      submission_video_match_verified: linked,
    },
    span: {
      coordinate: 'zero-based selected-observation ordinal; inclusive start, exclusive end',
      from_frame: from,
      to_frame_exclusive: to,
      frame_count: to - from,
      timeline_mode: observation.timeline_mode,
      coordinate_fps: run.nominal_fps ?? observation.nominal_source_fps ?? null,
      time_mapping: observation.playback_alignment?.approximate_seconds ?? null,
      player_alignment_qualified: false,
      physical_inputs_complete: false,
      source_parts: clippedParts(observation.parts, from, to, observation.frame_count),
    },
    state_at_start: stateEvent,
    events,
    lanes: observation.lanes,
    state_semantics: observation.state_semantics,
    unknown: observation.unknown,
    playback_alignment: observation.playback_alignment,
    binding_summary: observation.binding_summary,
    provenance_warnings: [
      ...(linked ? [] : ['This alternate video is not verified as the video attached to the official submission.']),
      'Frame ordinals are observation coordinates, not qualified player timestamps or game ticks.',
      'Visible overlay highlights do not establish physical key state, hold duration, or engine sampling boundaries.',
    ],
    segment_counts: counts,
  };
  const outputPath = path.resolve(out);
  if (outputPath === input) throw new Error('Output must be a new file separate from the source artifact.');
  fs.writeFileSync(outputPath, `${JSON.stringify(packet, null, 2)}\n`, { flag: 'wx', mode: 0o600 });
  return { outputPath, packet };
}

try {
  const result = extract(parseArgs(process.argv.slice(2)));
  process.stdout.write(`Evidence excerpt written: ${result.outputPath}\n`);
  process.stdout.write(`Frames ${result.packet.span.from_frame}..${result.packet.span.to_frame_exclusive - 1} of ${result.packet.span.frame_count}; unresolved-visible=${result.packet.segment_counts.unresolved_visible_frames}; active-unmapped=${result.packet.segment_counts.active_unmapped_frames}.\n`);
  process.stdout.write('This file is not a score and is not qualified for TAS replay.\n');
} catch (error) {
  process.stderr.write(`${error.message}\n`);
  process.exitCode = 2;
}
