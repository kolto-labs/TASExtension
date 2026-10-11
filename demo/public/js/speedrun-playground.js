// Video browsing is independent of the stand-in simulator and its holorecord.
// Overlay observations are partial evidence, never executable game inputs.

const CATALOG = 'media/speedruns/catalog.json';
const FPS = 60;

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
    const exportMarks = button('Save marks', () => this.exportMarks());
    this.exportButton = exportMarks;
    fileRow.append(this.streamButton, upload, exportMarks);
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
    this.transport?.dispose();
    this.transport = null;
    this.run = this.catalog[index];
    this.data = null;
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
      const data = await readJSON(this.run.observations, this.abort.signal);
      if (this.disposed || revision !== this.revision) return;
      if (data.schema !== 'kptas.browser-visible-inputs/v1' || !Array.isArray(data.lanes) || !Array.isArray(data.events)) throw new Error('Unsupported observation data.');
      if (data.timeline_mode === 'selected-observations' && (data.read_only !== true || data.projection_execution_disabled !== true || data.replay_qualified !== false || !Number.isSafeInteger(data.frame_count) || data.frame_count < 1 || data.events[0]?.frame !== 0)) throw new Error('This input-observation file is not supported.');
      this.data = data;
      const independent = data.timeline_mode === 'selected-observations';
      if (independent) {
        this.description.textContent = 'Browse the observed inputs below. Open the video separately when you want to compare the footage.';
        this.observationCell.replaceChildren(...data.lanes.map(lane => new Option(lane.label, lane.id)));
        this.inputDetails.open = true;
      }
      this.status.textContent = `${data.frame_count.toLocaleString()} observed frames. ${independent ? 'Observations use a separate timeline from the video.' : 'Align the observation timeline with the video.'}`;
      this.update();
    } catch (error) { if (!this.disposed && revision === this.revision) this.status.textContent = error.message; }
  }

  async openStream() {
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
    this.metrics.textContent = transport ? transport.stats() : `${this.run?.source_dimensions.join('×') || '—'} • 60 fps source`;
    const independent = this.data?.timeline_mode === 'selected-observations';
    this.roll.setAttribute('aria-label', independent ? 'Visible keyboard and mouse observations near the selected frame' : 'Visible keyboard and mouse highlights near the estimated video frame');
    this.observationControls.hidden = this.observationInspection.hidden = !independent;
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
            this.bindingInfo.textContent = `${lane.label}: ${visible}. ${lane.binding?.status === 'unresolved' ? lane.binding.detail : `Candidate score input: ${lane.score_token}. Verify its mapping and timing against the footage.`}`;
          }
          const origin = event.source || {};
          this.observationOrigin.textContent = `Source span starts at part ${origin.source_part ?? '—'}, local frame ${origin.source_local_frame ?? '—'}; native PTS ${origin.decoder_pts ?? '—'} (${origin.decoder_time_base || 'time base unavailable'}).${origin.recovery_artifact ? ` Recovery: ${origin.recovery_artifact}, frame ${origin.recovery_local_frame ?? '—'}.` : ''} Source assembly: ${this.data.provenance?.source_assembly || '—'}.`;
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
    this.abort.abort();
    this.transport?.dispose();
    this.resize?.disconnect();
  }
}
