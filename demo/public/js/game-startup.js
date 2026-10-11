// Presentation only. Readiness comes from the existing, session-checked bridge;
// iframe load and files-ready must never be treated as a rendered game frame.
const PHASES = {
  starting: ['Opening KOTOR', 'Connecting to the game session.'],
  'files-ready': ['Starting KOTOR', 'Game files are connected. Waiting for the first game image.'],
  paused: ['KOTOR is paused', 'Resume the game below to continue loading.'],
  error: ['KOTOR could not continue', 'Review the recovery steps below before starting another session.'],
  stopped: ['KOTOR has stopped', 'You can close the game below and start another session.'],
};

// Recovery hints describe the next supported action, not automatic recovery.
function describeFailure(message) {
  const code = message.match(/\b([A-Z][A-Z0-9_]{3,}):/)?.[1] || 'STARTUP_ERROR';
  if (/OWNED_PARENT_(TIMEOUT|SEND_FAILED|UNAVAILABLE)/.test(code)) return {
    code, recovery: 'Retry after reconnecting',
    action: 'Keep this page open. Close the game session, reconnect the game folder if needed, then start the game again.',
  };
  if (/^OWNED_(?:PARENT_)?(SNAPSHOT|MANIFEST)/.test(code) || /selected installation changed/i.test(message)) return {
    code, recovery: 'Reconnect files before retrying',
    action: 'Close the game session and connect the same installation folder again. Check that its files have not changed or moved, then start again.',
  };
  if (code === 'OWNED_LOCAL_MANIFEST_HTTP') return {
    code, recovery: 'File service needs attention',
    action: 'Restore the local game-file service, then start a new session. The reported HTTP status and request path are included below.',
  };
  return { code, recovery: 'Session stopped', action: 'Copy the diagnostic report below. Close this game session before trying again; repeating the same error requires investigating its reported cause.' };
}

export class GameStartup {
  constructor(host, frame) {
    if (!document.querySelector('link[data-game-startup-style]')) {
      const style = document.createElement('link');
      style.rel = 'stylesheet'; style.href = new URL('./game-startup.css', import.meta.url).href;
      style.dataset.gameStartupStyle = ''; document.head.append(style);
    }
    this.frame = frame; this.phase = 'starting'; this.presented = false;
    this.startedAt = new Date().toISOString(); this.entries = [];
    this.startedClock = performance.now(); this.lastStatusClock = null;
    this.phaseClock = this.startedClock; this.finishedClock = null;
    this.lastObservedStatus = null; this.dispatchProgressClock = null;
    this.evidence = new Map([['Session attached', { at: this.startedAt, elapsed: 0 }]]);
    // The covered game must not receive keyboard focus while startup controls
    // are visible. Restore the iframe's own attributes when revealing it.
    this.frameWasInert = frame.inert;
    this.frameAriaHidden = frame.getAttribute('aria-hidden');
    frame.inert = true; frame.setAttribute('aria-hidden', 'true');
    this.element = document.createElement('section'); this.element.className = 'game-startup';
    // The stylesheet is fetched lazily. Cover the iframe immediately so the
    // runtime's own startup page cannot flash through before it arrives.
    this.element.style.cssText = 'position:absolute;inset:0;z-index:2;background-color:var(--void,#05070a)';
    this.element.setAttribute('aria-label', 'KOTOR startup');
    const content = document.createElement('div'); content.className = 'game-startup-content';
    const eyebrow = document.createElement('p'); eyebrow.className = 'game-startup-eyebrow'; eyebrow.textContent = 'KOTOR · PLAYER';
    const emblem = document.createElement('div'); emblem.className = 'game-startup-emblem'; emblem.setAttribute('aria-hidden', 'true'); emblem.textContent = 'K';
    this.live = document.createElement('div'); this.live.setAttribute('role', 'status'); this.live.setAttribute('aria-live', 'polite'); this.live.setAttribute('aria-atomic', 'true');
    this.title = document.createElement('h2'); this.copy = document.createElement('p');
    this.live.append(this.title, this.copy);
    this.timing = document.createElement('p'); this.timing.className = 'game-startup-timing';
    // Elapsed time is deliberately outside live regions to avoid an announcement every second.
    this.timing.setAttribute('aria-live', 'off');
    this.progressSummary = document.createElement('p'); this.progressSummary.className = 'game-startup-timing';
    this.progressSummary.setAttribute('aria-live', 'off');
    this.steps = document.createElement('ol'); this.steps.className = 'game-startup-steps'; this.steps.setAttribute('aria-label', 'Startup milestones');
    for (const label of ['Session opened', 'Game files connected', 'Game image']) {
      const step = document.createElement('li'); step.textContent = label; this.steps.append(step);
    }
    const details = this.details = document.createElement('details'); details.className = 'game-startup-diagnostics';
    const summary = document.createElement('summary'); summary.textContent = 'Startup diagnostics';
    this.runtimeStatus = document.createElement('div'); this.runtimeStatus.className = 'game-startup-runtime-status';
    this.runtimeStatus.setAttribute('role', 'status');
    this.runtimeStatus.setAttribute('aria-live', 'polite');
    this.runtimeStatus.setAttribute('aria-atomic', 'true');
    this.runtimeStatus.textContent = 'Waiting for session status.';
    this.runtimeMetrics = document.createElement('dl'); this.runtimeMetrics.className = 'game-startup-evidence';
    this.runtimeMetrics.setAttribute('aria-label', 'Runtime measurements');
    this.runtimeMetrics.setAttribute('aria-live', 'off');
    this.metricValues = new Map();
    for (const label of ['Control state', 'Render presents', 'SDL dispatch calls', 'SDL caller time', 'SDL callback time', 'Workers at safe point']) {
      const term = document.createElement('dt'); term.textContent = label;
      const value = document.createElement('dd'); value.textContent = 'Not reported';
      this.metricValues.set(label, value); this.runtimeMetrics.append(term, value);
    }
    this.diagnosis = document.createElement('p'); this.diagnosis.className = 'game-startup-diagnosis';
    this.diagnosis.hidden = true; this.diagnosis.setAttribute('role', 'status');
    this.evidenceList = document.createElement('dl'); this.evidenceList.className = 'game-startup-evidence';
    this.evidenceList.setAttribute('aria-label', 'Observed startup progress');
    this.failure = document.createElement('dl'); this.failure.className = 'game-startup-failure'; this.failure.hidden = true;
    this.log = document.createElement('pre'); this.log.tabIndex = 0; this.log.setAttribute('aria-label', 'Startup diagnostic log');
    this.log.textContent = 'Game frame attached. Waiting for startup messages.';
    const copy = document.createElement('button'); copy.type = 'button'; copy.textContent = 'Copy diagnostic report';
    this.copyStatus = document.createElement('span'); this.copyStatus.setAttribute('role', 'status');
    copy.addEventListener('click', async () => {
      const report = this.report();
      try {
        if (!navigator.clipboard?.writeText) throw new Error('Clipboard unavailable');
        await navigator.clipboard.writeText(report);
        this.copyStatus.textContent = 'Report copied.';
      } catch {
        this.manualReport.value = report; this.manualReport.hidden = false;
        this.manualReport.focus(); this.manualReport.select();
        this.copyStatus.textContent = 'Clipboard unavailable. Copy the selected report below.';
      }
    });
    this.manualReport = document.createElement('textarea'); this.manualReport.hidden = true; this.manualReport.readOnly = true;
    this.manualReport.setAttribute('aria-label', 'Diagnostic report to copy');
    const actions = document.createElement('div'); actions.className = 'game-startup-diagnostic-actions'; actions.append(copy, this.copyStatus);
    details.append(summary, this.failure, this.runtimeStatus, this.runtimeMetrics, this.evidenceList, actions, this.manualReport, this.log);
    content.append(eyebrow, emblem, this.live, this.timing, this.progressSummary, this.diagnosis, this.steps, details);
    this.element.append(content); host.append(this.element);
    this.update('starting');
    this.renderEvidence();
    this.refreshTiming();
  }
  refreshTiming() {
    clearTimeout(this.timingTimer);
    const now = this.finishedClock ?? performance.now();
    const elapsed = Math.max(0, Math.floor((now - this.startedClock) / 1000));
    const duration = `${Math.floor(elapsed / 60)}:${String(elapsed % 60).padStart(2, '0')}`;
    const age = this.lastStatusClock === null ? null : Math.max(0, Math.floor((now - this.lastStatusClock) / 1000));
    const status = age === null ? 'Awaiting session status' : age < 5 ? 'Status received just now' : `Last status ${age}s ago`;
    this.timing.textContent = `${duration} elapsed · ${status}`;
    this.timing.dataset.stale = String(this.finishedClock === null && (age ?? elapsed) >= 30);
    this.timing.title = `Current phase: ${this.phase}. ${Math.max(0, Math.floor((now - this.phaseClock) / 1000))} seconds in this phase.`;
    this.renderProgressSummary();
    this.refreshDiagnosis(now);
    // Removed sessions and completed startup screens must not retain a polling loop.
    if (this.finishedClock === null && this.element.isConnected && !this.element.hidden) {
      this.timingTimer = setTimeout(() => this.refreshTiming(), 1000);
    }
  }
  renderProgressSummary() {
    const phase = {
      starting: 'Opening session', 'files-ready': 'Waiting for image',
      paused: 'Paused', error: 'Stopped by error', stopped: 'Stopped', presented: 'Image received',
    }[this.phase] || this.phase;
    const last = [...this.evidence].at(-1);
    this.progressSummary.textContent = `Phase: ${phase} · Last milestone: ${last?.[0] || 'None'}`;
  }
  refreshDiagnosis(now = performance.now()) {
    const status = this.lastObservedStatus;
    let diagnosis = '', kind = '';
    if (this.finishedClock === null && !this.presented) {
      const elapsed = now - this.startedClock;
      const statusAge = this.lastStatusClock === null ? null : now - this.lastStatusClock;
      if (statusAge === null && elapsed >= 30000) {
        kind = 'status-missing';
        diagnosis = 'No session status received. Open startup diagnostics and copy the report. Check the connection to the game session before restarting; loading may still be active.';
      } else if (statusAge !== null && statusAge >= 30000) {
        kind = 'status-stale';
        diagnosis = 'Session status has stopped arriving. Open diagnostics for the last reported state and copy the report before closing the session.';
      } else if (status?.state === 'paused') {
        kind = 'paused';
        diagnosis = 'The session is paused. Use Resume in the game controls to continue loading.';
      } else if (status?.state === 'pausing' && now - this.runtimeStateClock >= 30000) {
        kind = 'pause-pending';
        diagnosis = 'Pause has not been confirmed. Open diagnostics to inspect the worker counts; avoid sending another step or restore while this request is pending.';
      } else if (status && elapsed >= 30000 && status.presents === 0) {
        const dispatchAge = this.dispatchProgressClock === null ? null : now - this.dispatchProgressClock;
        if (status.sdlDispatch && dispatchAge !== null && dispatchAge >= 30000) {
          kind = 'dispatch-stalled';
          diagnosis = 'The session responds, but its event dispatch has not advanced for at least 30 seconds and no image has arrived. Copy the diagnostic report before restarting.';
        } else {
          kind = 'no-image';
          diagnosis = 'The session responds; the first image is still pending. Keep this session open and inspect the render and event counters in diagnostics.';
        }
      }
    }
    // Announce transitions only. Thresholds identify missing observations,
    // never an inferred crash or permission to discard the running session.
    if (kind !== this.diagnosis.dataset.kind || diagnosis !== this.diagnosis.textContent) {
      this.diagnosis.textContent = diagnosis;
      this.diagnosis.dataset.kind = kind;
    }
    this.diagnosis.hidden = !diagnosis;
  }
  finishTiming() {
    this.finishedClock ??= performance.now();
    this.refreshTiming();
  }
  reveal(focusGame = false) {
    this.finishTiming();
    const ownedFocus = this.element.contains(document.activeElement);
    this.element.hidden = true;
    this.frame.inert = this.frameWasInert;
    if (this.frameAriaHidden === null) this.frame.removeAttribute('aria-hidden');
    else this.frame.setAttribute('aria-hidden', this.frameAriaHidden);
    if (focusGame || ownedFocus) this.frame.focus();
  }
  markEvidence(label) {
    if (this.evidence.has(label)) return;
    this.evidence.set(label, { at: new Date().toISOString(), elapsed: performance.now() - this.startedClock });
    this.renderEvidence();
    this.renderProgressSummary();
  }
  renderEvidence() {
    this.evidenceList.replaceChildren();
    for (const label of ['Session attached', 'Files connected', 'Runtime status received', 'First image presented']) {
      const evidence = this.evidence.get(label);
      const term = document.createElement('dt'); term.textContent = label;
      const value = document.createElement('dd');
      value.textContent = evidence ? `+${(evidence.elapsed / 1000).toFixed(1)} s` : 'Not observed';
      if (evidence) value.title = evidence.at;
      this.evidenceList.append(term, value);
    }
  }
  report() {
    return ['Startup report v1', `Session started: ${this.startedAt}`, `Current phase: ${this.phase}`,
      `Last milestone: ${[...this.evidence.keys()].at(-1) || 'none'}`,
      `Observation: ${this.diagnosis.dataset.kind || 'waiting'}`,
      `Elapsed seconds: ${(((this.finishedClock ?? performance.now()) - this.startedClock) / 1000).toFixed(1)}`,
      `Seconds since status: ${this.lastStatusClock === null ? 'none received' : ((performance.now() - this.lastStatusClock) / 1000).toFixed(1)}`,
      `Game image observed: ${this.presented}`, `Files connected: ${Boolean(this.filesReady)}`,
      `Phase duration seconds: ${(((this.finishedClock ?? performance.now()) - this.phaseClock) / 1000).toFixed(1)}`,
      ...[...this.evidence].map(([label, evidence]) => `${label}: ${evidence.at} (+${(evidence.elapsed / 1000).toFixed(1)} s)`),
      this.runtimeStatus.textContent,
      ...[...this.metricValues].map(([label, value]) => `${label}: ${value.textContent}`),
      this.diagnosis.hidden ? '' : this.diagnosis.textContent, this.failureSummary || '', '', ...this.entries].join('\n');
  }
  record(state, message) {
    // Preserve raw diagnostic text within a bounded per-session buffer.
    if (typeof message !== 'string' || !message) return;
    const text = message.length > 8192 ? `${message.slice(0, 8192)}\n[Message truncated at 8192 characters]` : message;
    const line = `${state}: ${text}`;
    if (line === this.lastLine) return;
    this.lastLine = line;
    this.entries.push(`${new Date().toISOString()} ${line}`);
    while (this.entries.length > 64 || this.entries.join('\n').length > 32768) this.entries.shift();
    this.log.textContent = this.entries.join('\n');
  }
  showFailure(message) {
    const failure = describeFailure(typeof message === 'string' ? message : '');
    const cause = typeof message === 'string' && message.trim() ? message.slice(0, 8192) : 'No error details were supplied by the session.';
    const phase = this.phase;
    const lastMilestone = [...this.evidence.keys()].at(-1) || 'None';
    this.failureSummary = `Code: ${failure.code}\nDuring: ${phase}\nLast milestone: ${lastMilestone}\nCause: ${cause}\nRecovery: ${failure.recovery}\nNext step: ${failure.action}`;
    this.failure.replaceChildren();
    for (const [label, value] of [['Code', failure.code], ['During', phase], ['Last milestone', lastMilestone], ['Cause', cause], ['Recovery', failure.recovery], ['Next step', failure.action]]) {
      const term = document.createElement('dt'); term.textContent = label;
      const description = document.createElement('dd'); description.textContent = value;
      this.failure.append(term, description);
    }
    this.failure.hidden = false; this.details.open = true;
  }
  update(phase, message) {
    this.record(phase, message);
    // Status delivery can race a terminal startup message. A later polling
    // reply must not replace the error with an apparently healthy startup.
    if (['error', 'stopped'].includes(this.phase)) return;
    if (phase === 'files-ready') {
      this.filesReady = true;
      this.markEvidence('Files connected');
    }
    if (!PHASES[phase] || (this.presented && !['error', 'stopped'].includes(phase))) return;
    if (phase === 'error') this.showFailure(message);
    if (['error', 'stopped'].includes(phase)) {
      this.element.hidden = false;
      this.frame.inert = true; this.frame.setAttribute('aria-hidden', 'true');
    }
    // A delayed starting notification cannot undo the files-ready milestone.
    if (phase === 'starting' && this.filesReady) phase = 'files-ready';
    if (this.phase === phase && this.element.dataset.phase === phase) return;
    this.phase = phase; this.phaseClock = performance.now();
    this.renderProgressSummary();
    if (['error', 'stopped'].includes(phase)) {
      this.finishedClock = performance.now(); this.finishTiming();
    }
    this.element.dataset.phase = phase;
    [this.title.textContent, this.copy.textContent] = PHASES[phase];
    const waiting = !['error', 'stopped'].includes(phase);
    this.steps.setAttribute('aria-busy', String(waiting));
    for (const [index, step] of [...this.steps.children].entries()) {
      const complete = index === 0 || (index === 1 && this.filesReady) || (index === 2 && this.presented);
      step.dataset.state = complete ? 'complete' : 'waiting';
      if (waiting && index === (this.filesReady ? 2 : 1)) step.setAttribute('aria-current', 'step');
      else step.removeAttribute('aria-current');
    }
  }
  observe(status) {
    // Caller already normalized and checked the message's source and session.
    if (['error', 'stopped'].includes(this.phase)) return;
    this.lastStatusClock = performance.now();
    this.markEvidence('Runtime status received');
    const previous = this.lastObservedStatus;
    if (!previous || status.state !== previous.state) this.runtimeStateClock = this.lastStatusClock;
    if (!previous || status.state !== previous.state || status.error !== previous.error) {
      this.record('runtime', `state=${status.state}; presents=${status.presents ?? 'unreported'}; workers=${status.parkedThreads ?? '?'}/${status.activeThreads ?? '?'}${status.error ? `; error=${status.error}` : ''}`);
    }
    const dispatch = status.sdlDispatch;
    // These counters measure the SDL call path, not guest instruction execution.
    if (dispatch && (!previous?.sdlDispatch || dispatch.calls !== previous.sdlDispatch.calls)) {
      this.dispatchProgressClock = this.lastStatusClock;
    }
    this.lastObservedStatus = status;
    const setMetric = (label, value) => { this.metricValues.get(label).textContent = value; };
    setMetric('Control state', status.state);
    setMetric('Render presents', status.presents === null ? 'Not reported' : String(status.presents));
    setMetric('SDL dispatch calls', dispatch ? String(dispatch.calls) : 'Not reported');
    setMetric('SDL caller time', dispatch ? `${(dispatch.callerMicros / 1000).toFixed(1)} ms` : 'Not reported');
    setMetric('SDL callback time', dispatch ? `${(dispatch.callbackMicros / 1000).toFixed(1)} ms` : 'Not reported');
    setMetric('Workers at safe point', status.parkedThreads !== null && status.activeThreads !== null
      ? `${status.parkedThreads} / ${status.activeThreads}` : 'Not reported');
    const description = `Control state: ${status.state}. Dispatch and render observations are listed separately below.${status.error ? ` Error: ${status.error}` : ''}`;
    if (this.runtimeStatus.textContent !== description) this.runtimeStatus.textContent = description;
    this.refreshDiagnosis();
    if (status.presents !== null && status.presents > 0) {
      this.presented = true;
      this.phase = 'presented'; this.phaseClock = performance.now();
      this.element.dataset.phase = 'presented';
      this.markEvidence('First image presented');
      this.steps.setAttribute('aria-busy', 'false');
      for (const [index, step] of [...this.steps.children].entries()) {
        step.dataset.state = index === 1 && !this.filesReady ? 'waiting' : 'complete';
        step.removeAttribute('aria-current');
      }
      this.reveal();
    } else if (!this.presented) {
      this.update(status.state === 'paused' ? 'paused' : this.filesReady ? 'files-ready' : 'starting');
    }
  }
}
