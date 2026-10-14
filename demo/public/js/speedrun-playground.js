// Video browsing is independent of the stand-in simulator and its holorecord.
// Overlay observations are partial evidence, never executable game inputs.
import { createObservationDraft, unwrapObservationDraft, normalizeDraftPlacement } from './observation-draft.js';
import { describeObservationSourceClock } from './observation-source-clock.js';

const CATALOG = 'media/speedruns/catalog.json';
const FPS = 60;
const SHA256 = /^[a-f0-9]{64}$/i;

function isRecord(value) { return value !== null && typeof value === 'object' && !Array.isArray(value); }

function normalizeSegmentEvidence(packet) {
  const fail = () => { throw new Error('Unsupported or incomplete read-only observation evidence.'); };
  if (!isRecord(packet) || packet.schema !== 'kptas.speedrun-segment-evidence/v1'
    || packet.read_only !== true || packet.executable_score !== false || packet.replay_qualified !== false
    || !isRecord(packet.source) || !isRecord(packet.span) || !isRecord(packet.state_at_start)
    || !Array.isArray(packet.lanes) || !Array.isArray(packet.events) || !Array.isArray(packet.span.source_parts)
    || !isRecord(packet.playback_alignment) || !isRecord(packet.binding_summary)
    || !Array.isArray(packet.provenance_warnings) || packet.provenance_warnings.length === 0
    || packet.provenance_warnings.length > 128 || packet.provenance_warnings.some(note => typeof note !== 'string' || note.length > 512)
    || packet.lanes.length < 1 || packet.lanes.length > 512 || packet.events.length > 200000
    || packet.span.source_parts.length < 1 || packet.span.source_parts.length > 4096) fail();
  const { source, span } = packet;
  const from = span.from_frame, to = span.to_frame_exclusive, count = span.frame_count;
  if (typeof source.run_id !== 'string' || !source.run_id || source.run_id.length > 128
    || typeof source.runner !== 'string' || !source.runner || source.runner.length > 128
    || typeof source.artifact_id !== 'string' || !source.artifact_id || source.artifact_id.length > 256
    || typeof source.observation_sha256 !== 'string' || !SHA256.test(source.observation_sha256)
    || typeof source.catalog_sha256 !== 'string' || !SHA256.test(source.catalog_sha256)
    || !Number.isSafeInteger(source.observation_bytes) || source.observation_bytes < 1
    || typeof source.submission_url !== 'string' || source.submission_url.length > 2048
    || typeof source.source_video_url !== 'string' || source.source_video_url.length > 2048
    || source.observation_schema !== 'kptas.browser-visible-inputs/v1'
    || source.submission_video_match_verified !== true && source.submission_video_match_verified !== false
    || typeof span.coordinate !== 'string' || span.coordinate !== 'zero-based selected-observation ordinal; inclusive start, exclusive end'
    || span.timeline_mode !== 'selected-observations' || span.player_alignment_qualified !== false
    || span.physical_inputs_complete !== false || !Number.isSafeInteger(from) || from < 0
    || !Number.isSafeInteger(to) || to <= from || !Number.isSafeInteger(count) || count !== to - from
    || packet.state_at_start.frame < 0 || packet.state_at_start.frame > from || !Number.isSafeInteger(packet.state_at_start.frame)) fail();

  const laneIds = new Set();
  for (const lane of packet.lanes) {
    if (!isRecord(lane) || typeof lane.id !== 'string' || !lane.id || lane.id.length > 128 || laneIds.has(lane.id)
      || typeof lane.label !== 'string' || !lane.label || lane.label.length > 256
      || lane.score_token !== null && (typeof lane.score_token !== 'string' || lane.score_token.length > 64)) fail();
    laneIds.add(lane.id);
  }
  const validateState = state => {
    if (!Array.isArray(state?.held) || !Array.isArray(state?.unknown)) fail();
    const held = new Set(state.held), unknown = new Set(state.unknown);
    if (held.size !== state.held.length || unknown.size !== state.unknown.length
      || [...held, ...unknown].some(id => !laneIds.has(id)) || [...held].some(id => unknown.has(id))) fail();
  };
  validateState(packet.state_at_start);
  let previousFrame = from - 1;
  let eventAtStart = null;
  for (const event of packet.events) {
    if (!isRecord(event) || !Number.isSafeInteger(event.frame) || event.frame < from || event.frame >= to
      || event.frame <= previousFrame) fail();
    validateState(event);
    if (event.frame === from) eventAtStart = event;
    previousFrame = event.frame;
  }
  const sameMembers = (left, right) => left.length === right.length && left.every(id => right.includes(id));
  if (eventAtStart && (!sameMembers(eventAtStart.held, packet.state_at_start.held)
    || !sameMembers(eventAtStart.unknown, packet.state_at_start.unknown))) fail();

  let nextSelected = from;
  const parts = packet.span.source_parts.map(part => {
    if (!isRecord(part) || !Number.isSafeInteger(part.source_part) || part.source_part < 0
      || part.excerpt_selected_from_frame !== nextSelected
      || !Number.isSafeInteger(part.excerpt_selected_to_frame_exclusive)
      || part.excerpt_selected_to_frame_exclusive <= part.excerpt_selected_from_frame
      || part.excerpt_selected_to_frame_exclusive > to
      || part.excerpt_frame_count !== part.excerpt_selected_to_frame_exclusive - part.excerpt_selected_from_frame
      || !Number.isSafeInteger(part.excerpt_source_first_frame) || part.excerpt_source_first_frame < 0
      || !Number.isSafeInteger(part.excerpt_source_first_frame + part.excerpt_frame_count - 1)
      || part.excerpt_source_last_frame !== part.excerpt_source_first_frame + part.excerpt_frame_count - 1) fail();
    nextSelected = part.excerpt_selected_to_frame_exclusive;
    return {
      source_part:part.source_part,
      first_source_frame:part.excerpt_source_first_frame,
      last_source_frame:part.excerpt_source_last_frame,
      selected_frame_count:part.excerpt_frame_count,
      selected_offset:part.excerpt_selected_from_frame - from,
    };
  });
  if (nextSelected !== to) fail();

  const state = packet.state_at_start;
  const events = [{ frame:0, held:[...state.held], unknown:[...state.unknown], ...(state.source ? { source:state.source } : {}) }];
  for (const event of packet.events) {
    if (event.frame <= from) continue;
    events.push({ frame:event.frame - from, held:[...event.held], unknown:[...event.unknown], ...(event.source ? { source:event.source } : {}) });
  }
  return {
    schema:'kptas.browser-visible-inputs/v1', artifact:source.artifact_id,
    local_import_kind:'excerpt',
    read_only:true, projection_execution_disabled:true, replay_qualified:false,
    physical_inputs_complete:false, timeline_mode:'selected-observations',
    frame_count:count, source_frame_origin:from, source_frame_base:from,
    nominal_source_fps:span.coordinate_fps,
    source:{ ...source, video_sha256:null }, parts,
    playback_alignment:{
      player_alignment_qualified:false, game_frame_alignment_qualified:false,
      game_input_sampling_boundary_qualified:false, physical_hold_semantics_qualified:false,
      scope:packet.playback_alignment?.scope || null,
    },
    lanes:packet.lanes, events, binding_summary:packet.binding_summary || {},
    provenance:{ source_assembly:`observation packet sha256 ${source.observation_sha256}` },
    provenance_warnings:packet.provenance_warnings,
  };
}

function normalizeVisibleObservation(packet) {
  const fail = () => { throw new Error('Unsupported or incomplete read-only observation track.'); };
  if (!isRecord(packet) || packet.schema !== 'kptas.browser-visible-inputs/v1'
    || packet.timeline_mode !== 'selected-observations' || packet.read_only !== true
    || packet.projection_execution_disabled !== true || packet.replay_qualified !== false
    || packet.physical_inputs_complete !== false || !isRecord(packet.source) || !isRecord(packet.playback_alignment)
    || !Array.isArray(packet.lanes) || !Array.isArray(packet.events) || !Array.isArray(packet.parts)
    || packet.lanes.length < 1 || packet.lanes.length > 512 || packet.events.length < 1 || packet.events.length > 200000
    || packet.parts.length < 1 || packet.parts.length > 4096) fail();
  const { source, playback_alignment:alignment } = packet;
  // Older acquired tracks store this source claim at the packet root.
  // Preserve its value; conflicting locations cannot establish provenance.
  const correspondence = source.source_primary_correspondence_verified
    ?? packet.source_primary_correspondence_verified;
  if (typeof correspondence !== 'boolean'
    || Object.hasOwn(source, 'source_primary_correspondence_verified')
      && source.source_primary_correspondence_verified !== correspondence
    || Object.hasOwn(packet, 'source_primary_correspondence_verified')
      && packet.source_primary_correspondence_verified !== correspondence) fail();
  const origin = packet.source_frame_origin, count = packet.frame_count;
  const range = source.observed_source_frame_range;
  if (typeof packet.artifact !== 'string' || !packet.artifact || packet.artifact.length > 256
    || !Number.isSafeInteger(origin) || origin < 0 || !Number.isSafeInteger(count) || count < 1
    || !Number.isSafeInteger(origin + count) || !Array.isArray(range) || range.length !== 2
    || range[0] !== origin || range[1] !== origin + count
    || typeof source.run_id !== 'string' || !source.run_id || source.run_id.length > 128
    || typeof source.runner !== 'string' || !source.runner || source.runner.length > 128
    || typeof source.video_sha256 !== 'string' || !SHA256.test(source.video_sha256)
    || !Number.isSafeInteger(source.video_bytes) || source.video_bytes < 1
    || typeof source.frame_rate !== 'string' || !source.frame_rate || source.frame_rate.length > 32
    || typeof source.source_video_url !== 'string' || source.source_video_url.length > 2048
    || typeof source.submission_url !== 'string' || source.submission_url.length > 2048
    || typeof source.source_relationship !== 'string' || !source.source_relationship
    || alignment.player_alignment_qualified !== false || alignment.game_frame_alignment_qualified !== false
    || alignment.game_input_sampling_boundary_qualified !== false || alignment.physical_hold_semantics_qualified !== false
    || packet.events[0]?.frame !== 0) fail();

  const laneIds = new Set();
  for (const lane of packet.lanes) {
    if (!isRecord(lane) || typeof lane.id !== 'string' || !lane.id || lane.id.length > 128 || laneIds.has(lane.id)
      || typeof lane.label !== 'string' || !lane.label || lane.label.length > 256
      || lane.score_token !== null && (typeof lane.score_token !== 'string' || lane.score_token.length > 64)) fail();
    laneIds.add(lane.id);
  }
  const validateState = state => {
    if (!Array.isArray(state?.held) || !Array.isArray(state?.unknown)) fail();
    const held = new Set(state.held), unknown = new Set(state.unknown);
    if (held.size !== state.held.length || unknown.size !== state.unknown.length
      || [...held, ...unknown].some(id => !laneIds.has(id)) || [...held].some(id => unknown.has(id))) fail();
  };
  let previousFrame = -1;
  for (const event of packet.events) {
    if (!isRecord(event) || !Number.isSafeInteger(event.frame) || event.frame < 0 || event.frame >= count
      || event.frame <= previousFrame) fail();
    validateState(event);
    previousFrame = event.frame;
  }
  let selected = 0, sourceFrame = origin;
  for (const part of packet.parts) {
    const partCount = part?.selected_frame_count;
    if (!isRecord(part) || !Number.isSafeInteger(part.source_part) || part.source_part < 0
      || !Number.isSafeInteger(partCount) || partCount < 1
      || part.first_source_frame !== sourceFrame
      || !Number.isSafeInteger(part.last_source_frame)
      || part.last_source_frame !== part.first_source_frame + partCount - 1) fail();
    selected += partCount;
    sourceFrame += partCount;
    if (!Number.isSafeInteger(selected) || !Number.isSafeInteger(sourceFrame)) fail();
  }
  if (selected !== count || sourceFrame !== origin + count) fail();
  return { ...packet, source:{ ...source, source_primary_correspondence_verified:correspondence },
    source_frame_base:origin, local_import_kind:'track' };
}

function normalizeArchivedObservation(packet) {
  const fail = () => { throw new Error('Unsupported or incomplete read-only archived observation track.'); };
  if (!isRecord(packet) || packet.schema !== 'kptas.browser-visible-inputs/v1'
    || packet.timeline_mode !== 'selected-observations' || packet.frame_scope !== 'selected-observations'
    || packet.read_only !== true || packet.projection_execution_disabled !== true
    || packet.replay_qualified !== false || packet.physical_inputs_complete !== false
    || packet.video_time_origin_qualified !== false || packet.global_clip_timeline_qualified !== false
    || !isRecord(packet.playback_alignment) || packet.playback_alignment.qualified !== false
    || !isRecord(packet.provenance) || !isRecord(packet.provenance.input_sha256)
    || typeof packet.artifact !== 'string' || !packet.artifact || packet.artifact.length > 256
    || typeof packet.source_video_url !== 'string' || packet.source_video_url.length > 2048
    || !Number.isSafeInteger(packet.frame_count) || packet.frame_count < 1
    || !Array.isArray(packet.lanes) || packet.lanes.length < 1 || packet.lanes.length > 512
    || !Array.isArray(packet.events) || packet.events.length < 1 || packet.events.length > 200000
    || packet.events[0]?.frame !== 0) fail();
  const provenance = packet.provenance;
  for (const key of ['player_alignment_qualified', 'game_frame_alignment_qualified', 'game_input_sampling_boundary_qualified', 'physical_hold_semantics_qualified']) {
    if (Object.hasOwn(packet.playback_alignment, key) && packet.playback_alignment[key] !== false) fail();
  }
  if (typeof provenance.source_assembly !== 'string' || provenance.source_assembly.length > 2048) fail();
  for (const key of ['source_import_manifest_sha256', 'source_assembly_manifest_sha256', 'source_spans_sha256', 'adapter_sha256']) {
    if (typeof provenance[key] !== 'string' || !SHA256.test(provenance[key])) fail();
  }
  const inputHashes = Object.entries(provenance.input_sha256);
  if (inputHashes.length < 1 || inputHashes.length > 128
    || inputHashes.some(([key, value]) => !key || key.length > 256 || typeof value !== 'string' || !SHA256.test(value))) fail();
  const laneIds = new Set();
  for (const lane of packet.lanes) {
    if (!isRecord(lane) || typeof lane.id !== 'string' || !lane.id || lane.id.length > 128 || laneIds.has(lane.id)
      || typeof lane.label !== 'string' || !lane.label || lane.label.length > 256
      || lane.score_token !== null && (typeof lane.score_token !== 'string' || lane.score_token.length > 64)) fail();
    laneIds.add(lane.id);
  }
  let previousFrame = -1;
  for (const event of packet.events) {
    if (!isRecord(event) || !Number.isSafeInteger(event.frame) || event.frame < 0 || event.frame >= packet.frame_count
      || event.frame <= previousFrame || !Array.isArray(event.held) || !Array.isArray(event.unknown)
      || !isRecord(event.source)) fail();
    const held = new Set(event.held), unknown = new Set(event.unknown);
    if (held.size !== event.held.length || unknown.size !== event.unknown.length
      || [...held, ...unknown].some(id => !laneIds.has(id)) || [...held].some(id => unknown.has(id))) fail();
    const source = event.source;
    for (const key of ['source_frame', 'source_part', 'source_local_frame', 'first_selected_frame', 'last_selected_frame']) {
      if (!Number.isSafeInteger(source[key]) || source[key] < 0) fail();
    }
    if (source.first_selected_frame !== event.frame || source.last_selected_frame < event.frame
      || source.last_selected_frame >= packet.frame_count || !Number.isSafeInteger(source.frame_count)
      || source.frame_count !== source.last_selected_frame - source.first_selected_frame + 1
      || !Number.isSafeInteger(source.decoder_pts)
      || typeof source.decoder_time_base !== 'string' || !source.decoder_time_base || source.decoder_time_base.length > 128
      || typeof source.source_clock_offset !== 'string' || source.source_clock_offset.length > 128) fail();
    previousFrame = event.frame;
  }
  // Event origins identify observed state changes; they do not qualify a mapping for intervening frames.
  return { ...packet, parts:[], source_frame_origin:0, source_frame_base:0, local_import_kind:'archived-track' };
}

function archivedProvenanceText(data) {
  const p = data.provenance;
  return `import manifest SHA-256 ${p.source_import_manifest_sha256}; assembly manifest SHA-256 ${p.source_assembly_manifest_sha256}; source-span SHA-256 ${p.source_spans_sha256}; adapter SHA-256 ${p.adapter_sha256}; ${Object.keys(p.input_sha256).length} input-artifact digest claims`;
}

function normalizeImportedEvidence(packet) {
  if (packet?.schema === 'kptas.speedrun-segment-evidence/v1') return normalizeSegmentEvidence(packet);
  if (packet?.schema === 'kptas.browser-visible-inputs/v1') {
    return isRecord(packet.source) || Array.isArray(packet.parts)
      ? normalizeVisibleObservation(packet) : normalizeArchivedObservation(packet);
  }
  throw new Error('This local file is not a supported read-only observation track or excerpt.');
}

function locateSourceFrame(data, frame) {
  let nextOffset = 0;
  for (const part of data.parts || []) {
    const offset = Number.isSafeInteger(part.selected_offset) ? part.selected_offset
      : Number.isSafeInteger(part.excerpt_selected_from_frame) && Number.isSafeInteger(data.source_frame_origin)
        ? part.excerpt_selected_from_frame - data.source_frame_origin : nextOffset;
    const count = part.selected_frame_count ?? part.excerpt_frame_count;
    const first = part.first_source_frame ?? part.excerpt_source_first_frame;
    if (Number.isSafeInteger(count) && count > 0 && Number.isSafeInteger(first)
      && frame >= offset && frame < offset + count) {
      return { source_part:part.source_part, source_frame:first + frame - offset };
    }
    nextOffset = offset + (Number.isSafeInteger(count) ? count : 0);
  }
  return null;
}

function element(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text != null) node.textContent = text;
  return node;
}

function button(label, action, title = label) {
  const node = element('button', 'btn small', label);
  node.type = 'button';
  node.title = title;
  node.addEventListener('click', action);
  return node;
}

function sameOriginURL(path) {
  const url = new URL(path, document.baseURI);
  if (url.origin !== location.origin || !['http:', 'https:'].includes(url.protocol)) throw new Error('Unsupported data address.');
  return url.href;
}

async function readJSON(path, signal) {
  const response = await fetch(sameOriginURL(path), { credentials: 'same-origin', signal });
  if (!response.ok) throw new Error('The run data could not be loaded.');
  return response.json();
}

const timeText = (seconds) => `${String(Math.floor(seconds / 60)).padStart(2, '0')}:${(seconds % 60).toFixed(3).padStart(6, '0')}`;

function eventAt(events, frame) {
  let low = 0, high = events.length - 1, found = -1;
  while (low <= high) {
    const middle = (low + high) >>> 1;
    if (events[middle].frame <= frame) { found = middle; low = middle + 1; }
    else high = middle - 1;
  }
  return found;
}

class LocalVideo {
  constructor(container, file, changed) {
    this.url = URL.createObjectURL(file);
    this.video = element('video');
    this.video.src = this.url;
    this.video.controls = true;
    this.video.playsInline = true;
    this.video.preload = 'metadata';
    this.video.setAttribute('aria-label', 'K1 speedrun video');
    this.video.addEventListener('loadedmetadata', changed);
    this.video.addEventListener('timeupdate', changed);
    this.video.addEventListener('seeked', changed);
    this.video.addEventListener('pause', changed);
    this.video.addEventListener('play', changed);
    this.video.addEventListener('error', () => changed('This browser could not decode the file.'));
    container.replaceChildren(this.video);
    if (this.video.requestVideoFrameCallback) {
      const presented = () => {
        if (this.disposed) return;
        changed();
        this.callback = this.video.requestVideoFrameCallback(presented);
      };
      this.callback = this.video.requestVideoFrameCallback(presented);
    }
    this.ready = true;
    this.canStepTime = true;
  }
  time() { return this.video.currentTime; }
  duration() { return Number.isFinite(this.video.duration) ? this.video.duration : 0; }
  paused() { return this.video.paused; }
  play() { return this.video.play(); }
  pause() { this.video.pause(); }
  seek(time) { this.video.currentTime = Math.max(0, Math.min(time, this.duration())); }
  speed(rate) { this.video.playbackRate = rate; }
  stats() { return `${this.video.videoWidth || '—'}×${this.video.videoHeight || '—'} • video file`; }
  dispose() {
    this.disposed = true;
    this.video.pause();
    if (this.callback != null) this.video.cancelVideoFrameCallback(this.callback);
    this.video.removeAttribute('src');
    this.video.load();
    URL.revokeObjectURL(this.url);
  }
}

class TwitchVideo {
  async mount(container, id, changed) {
    const configuration = await readJSON('media/player-config.json');
    if (this.disposed) return;
    if (!configuration.origin) throw new Error('The source-player origin is not configured. Use a video file.');
    const origin = new URL(configuration.origin).origin;
    if (origin === location.origin || !['http:', 'https:'].includes(new URL(origin).protocol)) throw new Error('The source player must use a separate origin.');
    this.origin = origin; this.session = crypto.randomUUID(); this.changed = changed;
    this.snapshot = { time:0, duration:0, paused:true, resolution:'', fps:null };
    this.pending = new Map(); this.container = container;
    this.frame = element('iframe', 'speedrun-twitch'); this.frame.title = 'Speedrun source player';
    this.frame.setAttribute('credentialless','');
    this.frame.setAttribute('allow','autoplay; fullscreen');
    this.frame.setAttribute('sandbox','allow-scripts allow-same-origin allow-popups allow-presentation');
    const query = new URLSearchParams({ parent:location.origin, session:this.session, video:String(id) });
    this.frame.src = `${origin}/?${query}`;
    this.receive = event => {
      const value = event.data;
      if (this.disposed || event.source !== this.frame.contentWindow || event.origin !== this.origin || value?.session !== this.session) return;
      if (value.type === 'telos-player/state') {
        if (typeof value.ready !== 'boolean' || typeof value.paused !== 'boolean' || !Number.isFinite(value.time) || !Number.isFinite(value.duration) || value.time < 0 || value.duration < 0) return;
        this.ready = value.ready;
        this.snapshot = { time:value.time, duration:value.duration, paused:value.paused, resolution:typeof value.resolution === 'string' ? value.resolution.slice(0,64) : '', fps:Number.isFinite(value.fps) && value.fps >= 0 ? value.fps : null, buffer:Number.isFinite(value.buffer) ? Math.max(0,value.buffer) : null };
      } else if (value.type === 'telos-player/ack') {
        const timeout = this.pending.get(value.request);
        if (!timeout) return;
        clearTimeout(timeout); this.pending.delete(value.request);
        if (!value.ok) this.error = typeof value.error === 'string' ? value.error.slice(0,200) : 'Source control failed.';
      } else if (value.type === 'telos-player/error') this.error = 'The source player could not load.';
      else return;
      changed();
    };
    addEventListener('message', this.receive); container.replaceChildren(this.frame);
  }
  command(action, value) {
    if (!this.ready || this.disposed) return;
    const request = crypto.randomUUID();
    const timeout = setTimeout(() => { this.pending.delete(request); if (!this.disposed) { this.error = 'The source player did not respond.'; this.changed(); } }, 10000);
    this.pending.set(request, timeout);
    this.frame.contentWindow.postMessage({ type:'telos-player/command', session:this.session, request, action, value }, this.origin);
  }
  time() { return this.snapshot?.time || 0; }
  duration() { return this.snapshot?.duration || 0; }
  paused() { return !this.ready || this.snapshot.paused; }
  play() { this.command('play'); }
  pause() { this.command('pause'); }
  seek(time) { this.command('seek', Math.max(0, Math.min(time, this.duration()))); }
  stats() { if (this.error) return this.error; if (!this.ready) return 'Connecting to Twitch'; return `${this.snapshot.resolution || '—'} • ${Number.isFinite(this.snapshot.fps) ? this.snapshot.fps.toFixed(1) + ' fps' : 'fps unavailable'} • ${!this.snapshot.paused && this.snapshot.buffer === 0 ? 'Buffering' : 'Twitch'}`; }
  dispose() { this.pause(); this.disposed = true; removeEventListener('message',this.receive); for (const timeout of this.pending?.values() || []) clearTimeout(timeout); this.pending?.clear(); this.container?.replaceChildren(); }
}

export class SpeedrunPlayground {
  constructor(root) {
    this.root = root;
    this.abort = new AbortController();
    this.revision = 0;
    this.observationRevision = 0;
    this.markers = [];
    this.sourceOffset = 0;
  }

  async open() {
    this.root.textContent = 'Loading the runs.';
    try {
      const catalog = await readJSON(CATALOG, this.abort.signal);
      if (this.disposed) return;
      this.catalog = catalog.runs;
      if (!Array.isArray(this.catalog) || !this.catalog.length) throw new Error('No run has been added yet.');
      this.build();
      await this.select(0);
    } catch (error) { if (!this.disposed) this.root.textContent = error.message; }
  }

  build() {
    const fragment = document.createDocumentFragment();
    const picker = element('label', 'speedrun-field', 'Run ');
    const select = element('select');
    this.catalog.forEach((run, index) => select.append(new Option(`${run.runner} · ${run.submitted.slice(0, 10)} · ${run.category}${run.observation_label ? ` · ${run.observation_label}` : ''}`, String(index))));
    select.addEventListener('change', () => this.select(Number(select.value)));
    picker.append(select);
    fragment.append(picker);
    this.title = element('h3', 'footage-title');
    this.description = element('p', 'help');
    fragment.append(this.title, this.description);
    this.view = element('div', 'footage-view speedrun-view');
    fragment.append(this.view);
    const transport = element('div', 'speedrun-transport');
    this.back = button('−1 s', () => this.jump(-1), 'Seek back one second');
    this.play = button('Play', () => this.togglePlay());
    this.forward = button('+1 s', () => this.jump(1), 'Seek forward one second');
    this.stepBack = button('◀ 16.7 ms', () => this.step(-1), 'Pause and seek back 1/60 second. This is a time seek, not an exact decoded-frame guarantee.');
    this.stepForward = button('16.7 ms ▶', () => this.step(1), 'Pause and seek forward 1/60 second. This is a time seek, not an exact decoded-frame guarantee.');
    this.rate = element('select');
    this.rate.setAttribute('aria-label', 'Video speed');
    for (const value of [.25, .5, 1, 2, 4]) this.rate.append(new Option(`${value}×`, String(value), value === 1, value === 1));
    this.rate.addEventListener('change', () => this.transport?.speed?.(Number(this.rate.value)));
    this.rate.title = 'Adjust playback speed for a local video file.';
    const mark = button('Add mark', () => this.addMark());
    this.addMarkButton = mark;
    transport.append(this.back, this.play, this.forward, this.stepBack, this.stepForward, this.rate, mark);
    fragment.append(transport);
    const scrub = element('div', 'footage-scrub');
    this.seekbar = element('input');
    this.seekbar.type = 'range';
    this.seekbar.min = '0';
    this.seekbar.max = '1';
    this.seekbar.step = '0.001';
    this.seekbar.value = '0';
    this.seekbar.setAttribute('aria-label', 'Seek within the speedrun video');
    this.seekbar.addEventListener('input', () => this.seek(Number(this.seekbar.value)));
    this.clock = element('output', 'mono footage-clock');
    scrub.append(this.seekbar, this.clock);
    fragment.append(scrub);
    this.metrics = element('p', 'mono help');
    this.status = element('p', 'status');
    this.status.setAttribute('role', 'status');
    fragment.append(this.metrics, this.status);
    const inputDetails = element('details', 'speedrun-inputs');
    inputDetails.append(element('summary', '', 'Show visible inputs'));
    inputDetails.append(element('p', 'help', 'Keyboard and mouse highlights transcribed from the selected video overlay.'));
    this.alignmentLabel = element('label', 'speedrun-field', 'Observation offset (seconds) ');
    this.offset = element('input', 'mono');
    this.offset.type = 'number';
    this.offset.step = '0.001';
    this.offset.min = '-86400';
    this.offset.max = '86400';
    this.offset.value = '0';
    this.offset.addEventListener('input', () => { this.sourceOffset = Number(this.offset.value) || 0; this.update(); });
    this.alignmentLabel.append(this.offset);
    this.alignmentHelp = element('p', 'help');
    inputDetails.append(this.alignmentLabel, this.alignmentHelp);
    this.coverage = element('p', 'mono help');
    this.observedStart = button('Go to estimated start', () => this.seek((this.data?.video_time_origin_seconds || 0) + this.sourceOffset));
    inputDetails.append(this.coverage, this.observedStart);
    this.observationControls = element('div');
    const observationRow = element('label', 'speedrun-field', 'Selected observation ');
    this.observationFrameInput = element('input', 'mono');
    this.observationFrameInput.type = 'number';
    this.observationFrameInput.min = '0'; this.observationFrameInput.step = '1';
    this.observationFrameInput.setAttribute('aria-label', 'Selected video observation frame');
    this.observationFrameInput.addEventListener('change', () => this.setObservationFrame(this.observationFrameInput.value));
    this.observationBack = button('Previous', () => this.setObservationFrame(this.observationFrame - 1));
    this.observationNext = button('Next', () => this.setObservationFrame(this.observationFrame + 1));
    observationRow.append(this.observationFrameInput, this.observationBack, this.observationNext);
    const observationScrub = element('div', 'footage-scrub');
    this.observationSeek = element('input');
    this.observationSeek.type = 'range'; this.observationSeek.min = '0'; this.observationSeek.step = '1';
    this.observationSeek.setAttribute('aria-label', 'Browse selected video observations independently of the player');
    this.observationSeek.addEventListener('input', () => this.setObservationFrame(this.observationSeek.value));
    observationScrub.append(this.observationSeek);
    this.observationControls.append(observationRow, observationScrub);
    inputDetails.append(this.observationControls);
    this.held = element('p', 'mono help');
    this.held.setAttribute('aria-live', 'off');
    const scroll = element('div', 'speedrun-roll-scroll');
    this.roll = element('canvas', 'speedrun-roll');
    this.roll.setAttribute('role', 'img');
    this.roll.setAttribute('aria-label', 'Visible keyboard and mouse highlights near the estimated video frame');
    scroll.append(this.roll);
    inputDetails.append(this.held, scroll);
    this.observationInspection = element('div');
    const cellRow = element('label', 'speedrun-field', 'Inspect visible cell ');
    this.observationCell = element('select');
    this.observationCell.addEventListener('change', () => this.update());
    cellRow.append(this.observationCell);
    this.bindingInfo = element('p', 'help');
    this.observationOrigin = element('p', 'mono help');
    this.observationOrigin.style.overflowWrap = 'anywhere';
    this.observationInspection.append(cellRow, this.bindingInfo, this.observationOrigin);
    inputDetails.append(this.observationInspection);
    this.draftControls = element('div');
    const draftStartLabel = element('label', 'speedrun-field', 'Draft start tick ');
    this.draftStart = element('input', 'mono'); this.draftStart.type = 'number';
    this.draftStart.min = '0'; this.draftStart.step = '1'; this.draftStart.value = '0';
    this.draftStart.setAttribute('aria-label', 'Observation draft start tick');
    draftStartLabel.append(this.draftStart);
    const uncertaintyLabel = element('label', 'speedrun-field', 'Uncertainty ');
    this.draftUncertainty = element('input'); this.draftUncertainty.maxLength = 512;
    this.draftUncertainty.value = 'Game-frame alignment, physical inputs and sampling boundary are unqualified.';
    this.draftUncertainty.setAttribute('aria-label', 'Observation draft uncertainty');
    uncertaintyLabel.append(this.draftUncertainty);
    this.draftPosition = element('p', 'mono help');
    this.exportDraftButton = button('Export observation draft', () => this.exportObservationDraft());
    this.draftControls.append(draftStartLabel, uncertaintyLabel, this.draftPosition,
      element('p', 'help', 'Draft mapping uses one selected observation per draft tick. It is an authoring hypothesis. Source PTS and unknown inputs remain in the evidence; no executable score is created.'), this.exportDraftButton);
    this.draftStart.addEventListener('input', () => this.update());
    this.draftUncertainty.addEventListener('input', () => this.update());
    inputDetails.append(this.draftControls);
    inputDetails.addEventListener('toggle', () => this.update());
    this.inputDetails = inputDetails;
    fragment.append(inputDetails);
    const fileRow = element('div', 'speedrun-files');
    this.streamButton = button('Open source player', () => this.openStream());
    const upload = element('label', 'btn small file', 'Open video file');
    this.file = element('input', 'file-in');
    this.file.type = 'file';
    this.file.accept = 'video/*,.mkv';
    this.file.addEventListener('change', () => {
      const selected = this.file.files[0];
      this.file.value = '';
      if (selected) this.openFile(selected);
    });
    upload.append(this.file);
    const observationUpload = element('label', 'btn small file', 'Open read-only observation packet');
    this.observationFile = element('input', 'file-in');
    this.observationFile.type = 'file';
    this.observationFile.accept = '.json,application/json';
    observationUpload.title = 'Read a local review packet in this browser. It is not uploaded or sent to the TAS.';
    this.observationFile.addEventListener('change', () => {
      const selected = this.observationFile.files[0];
      this.observationFile.value = '';
      if (selected) void this.openObservationEvidence(selected);
    });
    observationUpload.append(this.observationFile);
    const exportMarks = button('Save marks', () => this.exportMarks());
    this.exportButton = exportMarks;
    fileRow.append(this.streamButton, upload, observationUpload, exportMarks);
    fragment.append(fileRow);
    this.markList = element('div', 'speedrun-marks');
    this.markList.setAttribute('role', 'group');
    this.markList.setAttribute('aria-label', 'Video marks');
    fragment.append(this.markList);
    const sourceDetails = element('details', 'speedrun-source');
    sourceDetails.append(element('summary', '', 'Inspect the source'));
    this.sourceInfo = element('div');
    sourceDetails.append(this.sourceInfo);
    fragment.append(sourceDetails);
    this.root.replaceChildren(fragment);
    this.resize = new ResizeObserver(() => this.update());
    this.resize.observe(this.root);
    this.update();
  }

  async select(index) {
    const revision = ++this.revision;
    this.observationRevision++;
    const observationRevision = this.observationRevision;
    this.transport?.dispose();
    this.transport = null;
    this.run = this.catalog[index];
    this.catalogPlayerMatch = true;
    this.observationImported = false;
    this.streamButton.textContent = 'Open source player';
    this.streamButton.title = 'Open source player';
    this.data = null;
    this.observationPacket = null;
    this.draftStart.value = '0';
    this.draftUncertainty.value = 'Game-frame alignment, physical inputs and sampling boundary are unqualified.';
    this.observationFrame = 0;
    this.observationCell.replaceChildren();
    this.held.textContent = 'Loading visible observations.';
    this.lastRoll = null;
    this.roll.getContext('2d').clearRect(0, 0, this.roll.width, this.roll.height);
    this.activeSource = null;
    this.markers = [];
    this.markList.replaceChildren();
    this.sourceOffset = 0;
    this.offset.value = '0';
    this.title.textContent = `${this.run.runner} · ${this.run.category}`;
    this.description.textContent = 'Browse the run footage, inspect visible input highlights, and save points to return to.';
    this.view.replaceChildren(element('p', 'help', 'Open the source player or choose a video file.'));
    this.sourceInfo.replaceChildren();
    const link = element('a', '', 'View the submission');
    link.href = this.run.submission_url;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    this.sourceInfo.append(link);
    this.sourceInfo.append(element('p', 'help', `Submitted ${this.run.submitted}. Source: ${this.run.source_dimensions.join('×')} at ${this.run.nominal_fps} fps.`));
    this.sourceInfo.append(element('p', 'help', this.run.source_relationship));
    this.sourceInfo.append(element('p', 'help', 'Input highlights are visual observations from the selected footage.'));
    this.status.textContent = 'Loading visible-input observations.';
    this.update();
    try {
      const rawData = await readJSON(this.run.observations, this.abort.signal);
      if (this.disposed || revision !== this.revision || observationRevision !== this.observationRevision) return;
      if (rawData?.schema !== 'kptas.browser-visible-inputs/v1' || !Array.isArray(rawData.lanes) || !Array.isArray(rawData.events)) throw new Error('Unsupported observation data.');
      const data = rawData.timeline_mode === 'selected-observations' ? normalizeImportedEvidence(rawData) : rawData;
      this.data = data;
      this.observationPacket = rawData;
      const independent = data.timeline_mode === 'selected-observations';
      if (independent) {
        this.description.textContent = 'Browse the observed inputs below. Open the video separately when you want to compare the footage.';
        this.observationCell.replaceChildren(...data.lanes.map(lane => new Option(lane.label, lane.id)));
        this.inputDetails.open = true;
        const source = data.source && typeof data.source === 'object' ? data.source : {};
        const alignment = data.playback_alignment && typeof data.playback_alignment === 'object' ? data.playback_alignment : {};
        const digest = key => typeof source[key] === 'string' && SHA256.test(source[key]) ? source[key].toLowerCase() : 'unavailable';
        const ranges = (data.parts || []).flatMap(part => {
          const first = part.first_source_frame ?? part.excerpt_source_first_frame;
          const last = part.last_source_frame ?? part.excerpt_source_last_frame;
          if (!Number.isSafeInteger(first) || !Number.isSafeInteger(last) || last < first) return [];
          const end = last + 1;
          if (!Number.isSafeInteger(end)) return [];
          return [`part ${part.source_part ?? '—'} [${first}, ${end})`];
        });
        const rangeSummary = ranges.length ? `${ranges.slice(0, 4).join('; ')}${ranges.length > 4 ? `; and ${ranges.length - 4} more parts` : ''}` : 'source frame mapping unavailable';
        const ordinalStart = Number.isSafeInteger(data.source_frame_origin) ? data.source_frame_origin : 0;
        const ordinalEnd = Number.isSafeInteger(data.frame_count) && Number.isSafeInteger(ordinalStart + data.frame_count) ? ordinalStart + data.frame_count : null;
        const alignmentStatus = key => alignment[key] === true ? 'qualified' : 'not qualified';
        this.sourceInfo.append(element('p', 'help', `Source-reported observation provenance: run ${source.run_id || 'unknown'} · video SHA-256 ${digest('video_sha256')} · observation SHA-256 ${digest('observation_sha256')} · catalog SHA-256 ${digest('catalog_sha256')} · selected ordinals [${ordinalStart}, ${ordinalEnd ?? 'unknown'}) · decoded source frames ${rangeSummary} · ${source.frame_rate || `${data.nominal_source_fps || 'unknown'} fps`}.`));
        if (data.local_import_kind === 'archived-track') {
          this.sourceInfo.append(element('p', 'help', `Archived track reports ${archivedProvenanceText(data)}. These claims are not authenticated by this viewer. Source mapping is recorded at state changes; no mapping between them is asserted.`));
        }
        this.sourceInfo.append(element('p', 'help', `Qualification: replay ${data.replay_qualified === true ? 'qualified' : 'not qualified'}; physical inputs ${data.physical_inputs_complete === true ? 'complete' : 'incomplete or unknown'}; player alignment ${alignmentStatus('player_alignment_qualified')}; game-frame alignment ${alignmentStatus('game_frame_alignment_qualified')}; input-sampling boundary ${alignmentStatus('game_input_sampling_boundary_qualified')}; physical hold semantics ${alignmentStatus('physical_hold_semantics_qualified')}.`));
      }
      this.status.textContent = `${data.frame_count.toLocaleString()} observed frames. ${independent ? 'Observations use a separate timeline from the video.' : 'Align the observation timeline with the video.'}`;
      this.update();
    } catch (error) { if (!this.disposed && revision === this.revision && observationRevision === this.observationRevision) this.status.textContent = error.message; }
  }

  async openObservationEvidence(file, isCurrent = () => true) {
    if (!isCurrent()) return false;
    const request = ++this.observationRevision;
    if (file.size > 64 * 1024 * 1024) {
      this.status.textContent = 'Observation packet exceeds the 64 MiB local import limit.';
      return false;
    }
    this.status.textContent = 'Reading observation evidence locally; the file is not uploaded.';
    try {
      const packet = JSON.parse(await file.text());
      if (this.disposed || request !== this.observationRevision || !isCurrent()) return false;
      const imported = unwrapObservationDraft(packet);
      const data = normalizeImportedEvidence(imported.evidence);
      const placement = imported.placement === null ? null : normalizeDraftPlacement(imported.placement, data.frame_count);
      this.data = data;
      this.observationPacket = imported.evidence;
      this.observationImported = true;
      this.draftStart.value = String(placement?.start_tick ?? 0);
      this.draftUncertainty.value = placement?.uncertainty ?? 'Game-frame alignment, physical inputs and sampling boundary are unqualified.';
      this.observationFrame = 0;
      this.observationCell.replaceChildren(...data.lanes.map(lane => new Option(lane.label, lane.id)));
      this.inputDetails.open = true;
      const source = isRecord(data.source) ? data.source : {};
      const archived = data.local_import_kind === 'archived-track';
      const identity = archived ? `artifact ${data.artifact}` : `${source.runner} (${source.run_id})`;
      const evidenceKind = data.local_import_kind === 'excerpt' ? 'excerpt' : 'track';
      this.title.textContent = `${identity} · Read-only observations`;
      this.description.textContent = `Read-only observation ${evidenceKind} from ${identity}. Player synchronization and executable input are unqualified.`;
      this.sourceInfo.querySelector('[data-imported-evidence]')?.remove();
      const importedInfo = element('div');
      importedInfo.dataset.importedEvidence = 'true';
      const reportedLink = archived ? archivedProvenanceText(data) : data.local_import_kind === 'excerpt'
        ? `observation SHA-256 ${source.observation_sha256} and catalog SHA-256 ${source.catalog_sha256}; submission-video match ${source.submission_video_match_verified ? 'claimed verified' : 'reported unverified'}`
        : `source-video SHA-256 ${source.video_sha256}; source/submission correspondence ${source.source_primary_correspondence_verified ? 'claimed verified' : 'not verified'}`;
      importedInfo.append(element('p', 'help', `Imported file reports ${reportedLink}. These metadata claims are not independently authenticated by this viewer.`));
      if (archived) importedInfo.append(element('p', 'help', 'This wrapper embeds no run or runner identity. Source mapping is recorded at state changes; no mapping between them is asserted.'));
      if (typeof source.source_relationship === 'string') importedInfo.append(element('p', 'help', `Reported source relationship: ${source.source_relationship}`));
      const sourceVideo = archived ? data.source_video_url : source.source_video_url;
      // Matching reported URLs only chooses a catalog player; it does not
      // authenticate a source or grant player/game/input alignment.
      this.catalogPlayerMatch = !archived && typeof sourceVideo === 'string'
        && sourceVideo === this.run.source_video_url
        && typeof source.submission_url === 'string' && source.submission_url === this.run.submission_url;
      this.streamButton.textContent = 'Open catalog player';
      this.streamButton.title = 'Open catalog player';
      if (!this.transport) this.view.textContent = this.catalogPlayerMatch
        ? 'Open the catalog player or choose a local video.'
        : 'Choose a local video or select a matching catalog entry.';
      importedInfo.append(element('p', 'help', this.catalogPlayerMatch
        ? 'Reported source and submission URLs match the selected catalog player. Player alignment remains unqualified.'
        : 'The selected catalog player has no matching reported source and submission URLs. Select a matching catalog entry or open a local video.'));
      if (typeof sourceVideo === 'string') importedInfo.append(element('p', 'help', `Reported source video: ${sourceVideo}`));
      if (typeof source.submission_url === 'string') importedInfo.append(element('p', 'help', `Reported submission: ${source.submission_url}`));
      this.sourceInfo.replaceChildren(importedInfo);
      this.status.textContent = `${data.frame_count.toLocaleString()} read-only observation frames loaded${placement ? ' with draft authoring positions' : ''}. Replay is not qualified; no score was created or uploaded.`;
      this.lastRoll = null;
      this.update();
      return true;
    } catch (error) {
      if (!this.disposed && request === this.observationRevision && isCurrent()) {
        this.status.textContent = error instanceof Error ? error.message : 'Observation packet could not be read.';
      }
      return false;
    }
  }

  exportObservationDraft() {
    if (this.disposed || this.data?.timeline_mode !== 'selected-observations' || !this.observationPacket) return;
    let url;
    try {
      const placement = normalizeDraftPlacement({ unit:'draft-tick', mapping:'one-selected-observation-per-draft-tick', qualified:false,
        start_tick:this.draftStart.value.trim() ? Number(this.draftStart.value) : NaN,
        uncertainty:this.draftUncertainty.value }, this.data.frame_count);
      const draft = createObservationDraft(this.observationPacket, placement, this.data.frame_count);
      const blob = new Blob([JSON.stringify(draft, null, 2)], { type:'application/json' });
      if (blob.size > 64 * 1024 * 1024) throw new Error('The draft exceeds the 64 MiB local import limit.');
      url = URL.createObjectURL(blob);
      const link = element('a'); link.href = url;
      link.download = `${String(this.data?.artifact || this.run?.id || 'observations').replace(/[^a-zA-Z0-9._-]/g, '_').slice(0,128)}.reference-draft.json`;
      this.root.append(link); link.click(); link.remove();
      if (this.draftDownloadUrl) URL.revokeObjectURL(this.draftDownloadUrl);
      this.draftDownloadUrl = url; url = null;
      this.status.textContent = 'Observation draft download requested. Unknown inputs, source timing and provenance are retained. Replay remains unqualified.';
    } catch (error) { this.status.textContent = error.message || 'The observation draft could not be exported.'; }
    finally { if (url) URL.revokeObjectURL(url); }
  }

  async openStream() {
    if (this.catalogPlayerMatch === false) {
      this.status.textContent = 'The selected catalog player does not match the imported observation metadata.';
      return;
    }
    const revision = this.revision;
    this.transport?.dispose();
    const transport = new TwitchVideo();
    this.transport = transport;
    this.resetSource({ kind: 'twitch', video_url: this.run.source_video_url });
    this.status.textContent = 'Opening Twitch.';
    this.update();
    try {
      await transport.mount(this.view, this.run.twitch_video, () => {
        if (this.disposed || this.transport !== transport || revision !== this.revision) return;
        this.status.textContent = transport.ready ? 'Twitch supports time seeking. Use a video file for smaller time steps.' : 'Opening Twitch.';
        this.update();
      });
    } catch (error) { if (!this.disposed && this.transport === transport) this.status.textContent = error.message; }
  }

  openFile(file) {
    this.transport?.dispose();
    const revision = this.revision;
    const transport = new LocalVideo(this.view, file, (error) => {
      if (this.disposed || this.transport !== transport || revision !== this.revision) return;
      if (typeof error === 'string') this.status.textContent = error;
      this.update();
    });
    this.transport = transport;
    this.resetSource({ kind: 'local-file', name: file.name, bytes: file.size, last_modified: file.lastModified, matches_catalog_source_verified: false });
    this.status.textContent = 'Video opened locally. Compare its timing with this run before using synchronized highlights.';
    this.rate.value = '1';
    this.update();
  }

  time() { return this.transport?.time() || 0; }
  setObservationFrame(value) {
    if (this.data?.timeline_mode !== 'selected-observations') return;
    const frame = Number(value);
    if (!Number.isFinite(frame)) return;
    this.observationFrame = Math.max(0, Math.min(Math.trunc(frame), this.data.frame_count - 1));
    this.observationFrameInput.value = String(this.observationFrame);
    this.update();
  }
  seek(time) { this.transport?.seek(time); this.update(); }
  jump(seconds) { this.seek(this.time() + seconds); }
  step(direction) { if (!this.transport?.canStepTime) return; this.transport.pause(); this.seek(this.time() + direction / FPS); }
  async togglePlay() {
    const transport = this.transport;
    const revision = this.revision;
    if (!transport?.ready) return;
    try {
      if (transport.paused()) await transport.play();
      else transport.pause();
      if (!this.disposed && this.transport === transport && revision === this.revision) this.update();
    } catch {
      if (!this.disposed && this.transport === transport && revision === this.revision) this.status.textContent = 'Playback did not start. Use the video’s own Play button.';
    }
  }

  resetSource(source) {
    this.activeSource = source;
    this.markers = [];
    this.markList.replaceChildren();
    this.sourceOffset = 0;
    this.offset.value = '0';
  }

  addMark() {
    if (!this.transport?.ready) return;
    const time = this.time();
    this.markers.push({ time_seconds: time, label: timeText(time) });
    this.markList.append(button(timeText(time), () => this.seek(time), `Seek to ${timeText(time)}`));
    this.update();
  }

  exportMarks() {
    const value = { schema: 'kptas.video-marks/v1', run: this.run.id, source: this.activeSource || null, catalog_source_video: this.run.source_video_url, observation_offset_seconds: this.sourceOffset, marks: this.markers };
    const blob = new Blob([JSON.stringify(value, null, 2) + '\n'], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const link = element('a');
    link.href = url;
    link.download = `${this.run.id}.video-marks.json`;
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  update() {
    if (this.disposed || !this.seekbar) return;
    const transport = this.transport;
    const ready = !!transport?.ready;
    const time = this.time();
    const duration = transport?.duration() || 0;
    this.seekbar.disabled = !ready || !duration;
    this.seekbar.max = String(Math.max(1, duration));
    this.seekbar.value = String(time);
    this.clock.textContent = timeText(time);
    this.seekbar.setAttribute('aria-valuetext', timeText(time));
    this.play.textContent = !ready || transport.paused() ? 'Play' : 'Pause';
    for (const node of [this.back, this.play, this.forward]) node.disabled = !ready;
    this.addMarkButton.disabled = !ready;
    this.exportButton.disabled = !this.markers.length;
    this.stepBack.disabled = this.stepForward.disabled = !ready || !transport.canStepTime;
    this.rate.disabled = !ready || !transport.canStepTime;
    this.streamButton.disabled = this.catalogPlayerMatch === false;
    this.metrics.textContent = transport ? transport.stats() : this.observationImported
      ? 'No source player open. Observation time and player alignment remain unqualified.'
      : `${this.run?.source_dimensions.join('×') || '—'} • 60 fps source`;
    const independent = this.data?.timeline_mode === 'selected-observations';
    this.roll.setAttribute('aria-label', independent ? 'Visible keyboard and mouse observations near the selected frame' : 'Visible keyboard and mouse highlights near the estimated video frame');
    this.observationControls.hidden = this.observationInspection.hidden = !independent;
    this.draftControls.hidden = !independent;
    this.exportDraftButton.disabled = !independent || !this.observationPacket;
    if (independent) {
      try {
        const placement = normalizeDraftPlacement({ unit:'draft-tick', mapping:'one-selected-observation-per-draft-tick', qualified:false,
          start_tick:this.draftStart.value.trim() ? Number(this.draftStart.value) : NaN,
          uncertainty:this.draftUncertainty.value }, this.data.frame_count);
        const event = this.data.events[eventAt(this.data.events, this.observationFrame)];
        this.draftPosition.textContent = `Draft tick ${placement.start_tick + this.observationFrame} · selected observation ${this.observationFrame + (this.data.source_frame_base || 0)} · ${describeObservationSourceClock(event, this.data.source_frame_base || 0)} · uncertainty: ${placement.uncertainty}`;
      } catch (error) { this.draftPosition.textContent = error.message; this.exportDraftButton.disabled = true; }
    }
    this.observedStart.hidden = this.alignmentLabel.hidden = independent;
    this.observedStart.disabled = !ready || !this.data;
    if (independent) {
      const last = this.data.frame_count - 1;
      this.observationFrameInput.max = this.observationSeek.max = String(last);
      if (document.activeElement !== this.observationFrameInput) this.observationFrameInput.value = String(this.observationFrame);
      this.observationSeek.value = String(this.observationFrame);
      this.observationSeek.setAttribute('aria-valuetext', `Selected video observation ${this.observationFrame} of ${last}`);
      this.observationBack.disabled = this.observationFrame === 0;
      this.observationNext.disabled = this.observationFrame === last;
      this.coverage.textContent = `${this.data.frame_count.toLocaleString()} selected video observations · ${this.data.lanes.length} visible cells · ${this.data.binding_summary?.unresolved ?? '—'} unresolved bindings`;
      this.alignmentHelp.textContent = 'Select an observation frame to inspect the visible inputs. Amber marks unresolved input mappings; grey marks unknown visual state. These observations use a separate timeline from the player.';
    } else {
      this.coverage.textContent = this.data ? `Estimated video range ${timeText(Math.max(0, (this.data.video_time_origin_seconds || 0) + this.sourceOffset))}–${timeText(Math.max(0, (this.data.video_time_origin_seconds || 0) + this.sourceOffset + this.data.frame_count / FPS))}` : '';
      this.alignmentHelp.textContent = `The initial mapping starts at ${timeText(this.data?.video_time_origin_seconds || 0)} and uses 60 video frames per second. Adjust it against the video before inspecting a key.`;
    }
    if (this.data && this.inputDetails.open) {
      const frame = independent ? this.observationFrame : Math.floor((time - this.sourceOffset - (this.data.video_time_origin_seconds || 0)) * FPS);
      const index = eventAt(this.data.events, frame);
      const event = this.data.events[index];
      if (frame < 0 || frame >= this.data.frame_count || !event) this.held.textContent = 'Outside the observed frames.';
      else {
        const names = event.held.map((id) => this.data.lanes.find((lane) => lane.id === id)?.label || id);
        this.held.textContent = `${independent ? 'Selected observation' : `Estimated ${this.data.frame_scope === 'sample-local' ? 'sample' : 'source'} frame`} ${(frame + (this.data.source_frame_base || 0)).toLocaleString()}: ${names.join(' · ') || 'no visible highlight'}`;
        if (event.unknown.length) this.held.textContent += ' · some highlights unknown';
        if (independent) {
          const unresolved = this.data.lanes.filter(lane => !lane.score_token && event.held.includes(lane.id));
          if (unresolved.length) this.held.textContent += ` · ${unresolved.length} active binding${unresolved.length === 1 ? '' : 's'} unresolved`;
          const lane = this.data.lanes.find(item => item.id === this.observationCell.value);
          if (lane) {
            const visible = event.unknown.includes(lane.id) ? 'visible state unknown' : event.held.includes(lane.id) ? 'visibly highlighted' : 'visibly inactive';
            const binding = lane.binding?.status === 'unresolved' ? lane.binding.detail
              : typeof lane.score_token === 'string' ? `Candidate score input: ${lane.score_token}. Verify its mapping and timing against the footage.`
                : 'No score-input binding is established for this visual lane.';
            this.bindingInfo.textContent = `${lane.label}: ${visible}. ${binding}`;
          }
          const mapped = locateSourceFrame(this.data, frame);
          const changeOrigin = isRecord(event.source) ? event.source : {};
          const origin = mapped || changeOrigin;
          const changeFrame = event.frame + (this.data.source_frame_base || 0);
          this.observationOrigin.textContent = `Source part ${origin.source_part ?? '—'} · decoded source frame ${origin.source_frame ?? origin.source_local_frame ?? '—'}${mapped ? '' : ` at observation state change ${changeFrame}`} · state-change PTS ${changeOrigin.decoder_pts ?? '—'} (${changeOrigin.decoder_time_base || 'time base unavailable'}).${changeOrigin.recovery_artifact ? ` Recovery: ${changeOrigin.recovery_artifact}, frame ${changeOrigin.recovery_local_frame ?? '—'}.` : ''} Source assembly: ${this.data.provenance?.source_assembly || '—'}.`;
        }
      }
      this.drawRoll(frame);
    }
  }

  drawRoll(frame) {
    const data = this.data;
    const canvas = this.roll;
    const width = Math.max(400, canvas.parentElement.clientWidth);
    const rowHeight = 18, height = data.lanes.length * rowHeight + 24;
    const scale = Math.min(devicePixelRatio || 1, 2);
    if (this.lastRoll?.data === data && this.lastRoll.frame === frame && this.lastRoll.width === width && this.lastRoll.scale === scale) return;
    this.lastRoll = { data, frame, width, scale };
    if (canvas.width !== Math.round(width * scale) || canvas.height !== Math.round(height * scale)) {
      canvas.width = Math.round(width * scale);
      canvas.height = Math.round(height * scale);
      canvas.style.height = `${height}px`;
    }
    const ctx = canvas.getContext('2d');
    ctx.setTransform(scale, 0, 0, scale, 0, 0);
    ctx.fillStyle = '#05070a'; ctx.fillRect(0, 0, width, height);
    if (frame < 0 || frame >= data.frame_count) {
      ctx.fillStyle = '#7f93a0'; ctx.font = '12px ui-monospace, monospace'; ctx.textAlign = 'left';
      ctx.fillText('No input observations at this point in the video.', 12, 22);
      return;
    }
    const center = Math.max(0, Math.min(frame, data.frame_count - 1));
    const first = Math.max(0, Math.min(center - 120, Math.max(0, data.frame_count - 240)));
    const last = Math.min(data.frame_count, first + 240);
    const frameSpan = Math.max(1, last - first);
    const left = 90, span = width - left - 8;
    const x = (number) => left + (number - first) / frameSpan * span;
    ctx.font = '10px ui-monospace, monospace'; ctx.textBaseline = 'middle';
    ctx.fillStyle = '#7f93a0';
    ctx.fillText(`${data.timeline_mode === 'selected-observations' ? 'Observation' : data.frame_scope === 'sample-local' ? 'Sample' : 'Source'} ${first + (data.source_frame_base || 0)}`, left, 10);
    ctx.textAlign = 'right'; ctx.fillText(String(last + (data.source_frame_base || 0)), width - 8, 10);
    const start = Math.max(0, eventAt(data.events, first));
    data.lanes.forEach((lane, row) => {
      const y = 24 + row * rowHeight;
      ctx.textAlign = 'right'; ctx.fillStyle = lane.score_token ? '#b5c8d2' : '#f3ca73';
      ctx.fillText(lane.label.slice(0, 14), left - 7, y + rowHeight / 2);
      ctx.fillStyle = '#101920'; ctx.fillRect(left, y, span, rowHeight - 1);
      for (let index = start; index < data.events.length && data.events[index].frame < last; index++) {
        const event = data.events[index];
        const end = index + 1 < data.events.length ? data.events[index + 1].frame : data.frame_count;
        if (!event.held.includes(lane.id) && !event.unknown.includes(lane.id)) continue;
        const a = x(Math.max(first, event.frame)), b = x(Math.min(last, end));
        ctx.fillStyle = event.unknown.includes(lane.id) ? '#56616a' : data.timeline_mode === 'selected-observations' && !lane.score_token ? '#f3ca73' : ['button', 'mouse_button'].includes(lane.kind) ? (data.timeline_mode === 'selected-observations' ? '#eaa86b' : '#f3ca73') : '#5ad8ff';
        ctx.fillRect(a, y + 2, Math.max(1, b - a), rowHeight - 5);
      }
    });
    if (frame >= first && frame <= last) {
      ctx.strokeStyle = '#19e3c5'; ctx.beginPath(); ctx.moveTo(x(frame), 20); ctx.lineTo(x(frame), height); ctx.stroke();
    }
  }

  dispose() {
    this.disposed = true;
    if (this.draftDownloadUrl) URL.revokeObjectURL(this.draftDownloadUrl);
    this.draftDownloadUrl = null; this.observationPacket = null;
    this.abort.abort();
    this.transport?.dispose();
    this.resize?.disconnect();
  }
}
