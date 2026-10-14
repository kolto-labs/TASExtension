/* Records the guest presentation and AudioWorklet mix into local OPFS storage. */
(() => {
  const panel = document.createElement('details');
  panel.id = 'game-recording';
  panel.open = true;
  panel.setAttribute('aria-label', 'Record game footage');
  const disclosure = document.createElement('summary');
  disclosure.textContent = 'Recording controls';
  const start = document.createElement('button'), stop = document.createElement('button');
  const download = document.createElement('button'), downloadReceipt = document.createElement('button');
  const recover = document.createElement('button'), saved = document.createElement('select');
  const status = document.createElement('span');
  const recoveryStatus = document.createElement('span');
  const inspectAudio = document.createElement('button'), exportAudio = document.createElement('button');
  const audioView = document.createElement('pre');
  const audioDownload = document.createElement('a');
  audioDownload.textContent = 'Save audio observation'; audioDownload.hidden = true;
  inspectAudio.textContent = 'Inspect capture audio'; exportAudio.textContent = 'Export audio observation';
  inspectAudio.disabled = exportAudio.disabled = true;
  audioView.hidden = true; audioView.setAttribute('aria-label', 'Capture audio observation');
  audioView.style.cssText = 'max-width:28rem;max-height:16rem;overflow:auto;white-space:pre-wrap';
  recoveryStatus.setAttribute('role', 'status');
  start.textContent = 'Record game'; stop.textContent = 'Stop recording';
  download.textContent = 'Download footage'; downloadReceipt.textContent = 'Download recording receipt';
  recover.textContent = 'Find saved recordings'; saved.setAttribute('aria-label', 'Saved game recordings');
  saved.hidden = true;
  status.setAttribute('role', 'status');
  status.textContent = 'Recording is off. Footage stays in this browser until downloaded.';
  stop.disabled = true; download.disabled = downloadReceipt.disabled = true;
  panel.append(disclosure, start, stop, recover, saved, download, downloadReceipt, status, recoveryStatus,
    inspectAudio, exportAudio, audioDownload, audioView);
  document.body.append(panel);
  let active = null, savedName = null, exportUrl = null;
  let capturedAudio = null;
  let audioExportUrl = null;
  let pendingWorkspaceAudioExport = null;

  function clearAudioDownload() {
    pendingWorkspaceAudioExport = null;
    audioDownload.hidden = true;
    audioDownload.removeAttribute('href');
    audioDownload.removeAttribute('download');
    if (audioExportUrl) URL.revokeObjectURL(audioExportUrl);
    audioExportUrl = null;
  }

  function declaredRuntimeContext() {
    // These identify the document's selected context, not verified game bytes
    // or a mapping between encoded video frames and simulation ticks.
    const query = new URLSearchParams(location.search);
    const session = query.get('ownedSession');
    const ownedIndex = query.get('ownedIndex');
    return {
      schema:'telos.game-recording-context/v1', source:'runtime-document-url',
      declaredSession:typeof session === 'string' && /^[a-zA-Z0-9._-]{1,128}$/.test(session) ? session : null,
      declaredOwnedIndex:typeof ownedIndex === 'string' && /^[0-9a-f]{64}$/.test(ownedIndex) ? ownedIndex : null,
      executableIdentityVerified:false,
    };
  }

  function selectRecording(name) {
    if (![...saved.options].some(option => option.value === name)) {
      const option = document.createElement('option'); option.value = name; option.textContent = name;
      saved.append(option);
    }
    saved.value = name; savedName = name; saved.hidden = false;
  }

  function storageRequest(run, operation, extra = {}, transfer = []) {
    if (run.workerFailed) return Promise.reject(new Error('Recording storage stopped responding.'));
    return new Promise((resolve, reject) => {
      const id = ++run.nextRequest;
      run.pending.set(id, {resolve, reject});
      try { run.worker.postMessage({id, operation, ...extra}, transfer); }
      catch (error) { run.pending.delete(id); reject(error); }
    });
  }

  function audioObservation(run) {
    try {
      const observed = run.audio?.snapshot?.();
      const context = run.audioContext || run.audio?.context;
      const counter = value => Number.isSafeInteger(value) && value >= 0 ? value : null;
      const devices = Array.isArray(observed?.devices) ? observed.devices : null;
      const lifecycle = observed?.deviceLifecycle;
      const deviceLifecycle = lifecycle?.schema === 'telos.audio-device-lifecycle/v1'
        && lifecycle.scope === 'runtime-document' ? {
          schema:lifecycle.schema, scope:lifecycle.scope,
          openAttempts:counter(lifecycle.openAttempts), openRejected:counter(lifecycle.openRejected),
          openedDevices:counter(lifecycle.openedDevices), disposedDevices:counter(lifecycle.disposedDevices),
          nodeReadyEvents:counter(lifecycle.nodeReadyEvents), nodeFailureEvents:counter(lifecycle.nodeFailureEvents),
          countsSaturated:typeof lifecycle.countsSaturated === 'boolean' ? lifecycle.countsSaturated : null,
          qualification:'Cumulative AudioWorklet route events only. Other audio routes, audible playback and encoded audio are unverified.'
        } : null;
      return {
        schema:'telos.recording-audio-observation/v1', observedAt:new Date().toISOString(),
        contextState:typeof context?.state === 'string' ? context.state.slice(0,24) : null,
        audioContextTimeSeconds:Number.isFinite(context?.currentTime) && context.currentTime >= 0 ? context.currentTime : null,
        documentVisibility:document.visibilityState,
        captureLeaseOwned:!!run.destination && run.audio?.captureDestination === run.destination,
        backendFailed:typeof observed?.failed === 'boolean' ? observed.failed : null,
        deviceLifecycle,
        deviceCount:devices ? devices.length : null, devicesTruncated:devices ? devices.length > 8 : null,
        devices:devices ? devices.slice(0,8).map(device => ({id:counter(device.id), ready:typeof device.ready === 'boolean' ? device.ready : null,
          writtenFrames:counter(device.writtenFrames), consumedFrames:counter(device.consumedFrames),
          renderedFrames:counter(device.renderedFrames), nonzeroSamples:counter(device.nonzeroSamples)})) : null,
        qualification:'Observed audio state and guest PCM counters do not prove audible playback, encoded audio contents or simulation alignment.'
      };
    } catch { return null; }
  }

  function videoObservation(run) {
    try {
      const dimension = value => Number.isSafeInteger(value) && value > 0 ? value : null;
      const track = run.stream?.getVideoTracks?.()[0];
      const settings = track?.getSettings?.();
      return {
        schema:'telos.recording-video-observation/v1', observedAt:new Date().toISOString(),
        canvasWidth:dimension(run.canvas?.width), canvasHeight:dimension(run.canvas?.height),
        trackWidth:dimension(settings?.width), trackHeight:dimension(settings?.height),
        trackState:typeof track?.readyState === 'string' ? track.readyState.slice(0,24) : null,
        qualification:'Canvas and capture-track dimensions at this observation only. Encoded video dimensions, resize continuity and complete playback are not verified.'
      };
    } catch { return null; }
  }

  function fail(run, message) {
    if (active === run) inspectAudio.disabled = true;
    // Preserve the first interruption. Later shutdown events must not replace
    // the cause, and encoded bytes can still be retained after audio stops.
    if (!run.failed) {
      run.reason = String(message).slice(0,240);
      run.failureAudioObservation = audioObservation(run);
      run.failureVideoObservation = videoObservation(run);
    }
    run.failed = true;
    disclosure.textContent = 'Recording interrupted';
    status.textContent = 'Recording interrupted: ' + run.reason;
    if (run.recorder && run.recorder.state !== 'inactive') {
      try { run.recorder.stop(); }
      catch (error) { status.textContent += ' Encoder shutdown failed: ' + String(error.message).slice(0,160); }
    }
  }

  function release(run) {
    if (run.audioStateChanged) run.audioContext.removeEventListener('statechange', run.audioStateChanged);
    for (const track of run.stream?.getTracks() || []) track.stop();
    if (run.destination) run.audio.endCapture(run.destination);
    run.worker?.terminate();
    if (active === run) active = null;
    start.disabled = false; stop.disabled = true;
    inspectAudio.disabled = true;
  }

  async function beginAudioCapture(run, audio) {
    // resume() may remain pending while the browser suspends a hidden frame.
    // Bound the UI wait while retaining ownership of a late capture lease.
    let deadline;
    const capture = Promise.resolve().then(() => audio.beginCapture()).then(destination => {
      if (run.startExpired) {
        audio.endCapture(destination);
        throw new Error('Recording startup expired.');
      }
      return destination;
    });
    try {
      return await Promise.race([capture, new Promise((_, reject) => {
        deadline = setTimeout(() => {
          run.startExpired = true;
          reject(new Error('Guest audio did not resume within 30 seconds. Show the game and start recording again.'));
        }, 30000);
      })]);
    } finally { clearTimeout(deadline); }
  }

  async function finish(run) {
    stop.disabled = true;
    disclosure.textContent = 'Finalizing recording';
    try {
      await run.writes;
      const result = await storageRequest(run, 'close', {
        complete:!run.failed, reason:run.reason,
        captureDiagnostics:{schema:'telos.recording-audio-diagnostics/v1',
          atInterruption:run.failureAudioObservation || null, atEnd:audioObservation(run)},
        captureVideoDiagnostics:{schema:'telos.recording-video-diagnostics/v1',
          atInterruption:run.failureVideoObservation || null, atEnd:videoObservation(run)},
        endedAt:new Date().toISOString(), durationMs:Math.round(performance.now() - run.startedAt)
      });
      selectRecording(run.name);
      download.disabled = downloadReceipt.disabled = result.bytes === 0;
      status.textContent = run.failed || result.bytes === 0 || result.chunks === 0
        ? 'Recording is incomplete: ' + (run.reason || 'No encoded footage was saved.') +
          ` ${result.bytes.toLocaleString()} bytes in ${result.chunks.toLocaleString()} chunks retained.` +
          (run.failureAudioObservation ? ` Audio state: ${run.failureAudioObservation.contextState || 'unavailable'}. Document visibility: ${run.failureAudioObservation.documentVisibility}.` : '')
        : `Recording finalized in this browser: ${result.bytes.toLocaleString()} bytes. Download footage and its receipt.`;
      disclosure.textContent = run.failed || result.bytes === 0 || result.chunks === 0
        ? 'Recording incomplete' : 'Recording saved';
    } catch (error) {
      selectRecording(run.name);
      download.disabled = downloadReceipt.disabled = false;
      status.textContent = 'Recording could not be finalized. Retained files may be incomplete: ' + String(error.message).slice(0,200);
      disclosure.textContent = 'Recording finalization failed';
    } finally { release(run); }
  }

  start.addEventListener('click', async () => {
    if (active) return;
    const run = {name:'recording-' + crypto.randomUUID(), pending:new Map(), nextRequest:0,
      writes:Promise.resolve(), queuedBytes:0, failed:false, reason:null, startedAt:performance.now()};
    active = run; start.disabled = true;
    clearAudioDownload();
    capturedAudio = null; inspectAudio.disabled = exportAudio.disabled = true; audioView.hidden = true; audioView.textContent = '';
    disclosure.textContent = 'Starting recording';
    try {
      const canvas = document.getElementById('boxedwine-webgl-canvas-0');
      run.canvas = canvas;
      const audio = window.Module?.boxedwineAudio;
      if (!canvas?.width || !canvas.height || typeof canvas.captureStream !== 'function') throw new Error('Game video capture is unavailable.');
      if (typeof MediaRecorder !== 'function') throw new Error('Video encoding is unavailable in this browser.');
      if (!navigator.storage?.getDirectory) throw new Error('Local browser file storage is unavailable.');
      if (typeof audio?.beginCapture !== 'function' || typeof audio.endCapture !== 'function') throw new Error('This runtime has no guest audio capture owner.');
      const mimeType = ['video/webm;codecs=vp9,opus','video/webm;codecs=vp8,opus']
        .find(type => MediaRecorder.isTypeSupported(type));
      if (!mimeType) throw new Error('This browser cannot encode the game video and audio together.');
      run.audio = audio; run.destination = await beginAudioCapture(run, audio); run.audioContext = audio.context;
      run.stream = canvas.captureStream(30);
      for (const track of run.destination.stream.getAudioTracks()) run.stream.addTrack(track);
      if (run.stream.getVideoTracks().length !== 1 || run.stream.getAudioTracks().length !== 1) {
        throw new Error('The recording must contain one game video track and one guest audio mix.');
      }
      run.worker = new Worker('game-recording-writer.js');
      run.worker.onmessage = event => {
        const response = event.data, pending = run.pending.get(response?.id);
        if (!pending) return;
        run.pending.delete(response.id);
        if (response.ok) pending.resolve(response);
        else pending.reject(new Error(response.error || 'Recording storage failed.'));
      };
      run.worker.onerror = () => {
        run.workerFailed = true;
        run.storageFailed = true;
        for (const pending of run.pending.values()) pending.reject(new Error('Recording storage stopped responding.'));
        run.pending.clear(); fail(run, 'Recording storage stopped responding.');
      };
      const releaseMatch = location.pathname.match(/\/runtime-releases\/([0-9a-f]{64})\//);
      await storageRequest(run, 'open', {name:run.name, metadata:{
        schema:'telos.game-recording/v1', startedAt:new Date().toISOString(),
        runtimeRelease:releaseMatch?.[1] || null, mimeType,
        runtimeContext:declaredRuntimeContext(),
        capture:'guest-presentation-canvas-and-audio-worklet-mix',
        captureAudioClock:audio.captureClock && audio.captureDestination === run.destination ? 'constant-zero' : 'not-declared',
        audibleGuestAudioVerified:false,
        width:canvas.width, height:canvas.height, captureRateLimit:30,
        dimensionsScope:'Canvas at recording startup only; encoded dimensions and resize continuity are not verified.',
        gameFrameAlignment:false, rewindOrRestoreEvidence:false,
        sourceScope:'Game image and guest audio only. Browser/debugger chrome is excluded.',
        captureDiagnosticsAtStart:audioObservation(run),
        captureVideoDiagnosticsAtStart:videoObservation(run)
      }});
      selectRecording(run.name);
      download.disabled = downloadReceipt.disabled = true;
      run.recorder = new MediaRecorder(run.stream, {mimeType, videoBitsPerSecond:6000000, audioBitsPerSecond:192000});
      if (audio.failed || audio.context.state !== 'running') throw new Error('Guest audio became unavailable before recording.');
      run.destination.stream.addEventListener('guest-audio-unavailable', () => fail(run, 'Guest audio became unavailable.'), {once:true});
      run.audioStateChanged = () => {
        if (active === run && run.audioContext.state !== 'running') fail(run, 'The guest audio context stopped running (' + run.audioContext.state + ').');
      };
      run.audioContext.addEventListener('statechange', run.audioStateChanged);
      run.recorder.ondataavailable = event => {
        if (!event.data.size || run.storageFailed) return;
        run.queuedBytes += event.data.size;
        if (run.queuedBytes > 32 * 1024 * 1024) {
          run.queuedBytes -= event.data.size;
          fail(run, 'Storage could not keep up with video encoding.'); return;
        }
        if (active === run && !run.failed) {
          status.textContent = `Encoding produced data for ${run.name}; ${(run.savedBytes || 0).toLocaleString()} bytes acknowledged as retained. Waiting for this chunk's storage flush.`;
        }
        run.writes = run.writes.then(async () => {
          // A capture interruption does not invalidate already encoded data.
          // Storage failure alone prevents subsequent writes to this writer.
          if (run.storageFailed) return;
          const bytes = await event.data.arrayBuffer();
          const retained = await storageRequest(run, 'chunk', {bytes}, [bytes]);
          run.savedBytes = retained.bytes;
          if (active === run && !run.failed && run.recorder.state === 'recording') {
            if (savedName === run.name) download.disabled = downloadReceipt.disabled = false;
            status.textContent = `Recording game video and audio to ${run.name}: ${retained.bytes.toLocaleString()} bytes in ${retained.chunks.toLocaleString()} chunks flushed to this browser. Keep this session open until Stop finishes.`;
          }
        }).catch(error => {
          run.storageFailed = true;
          fail(run, error.message);
        }).finally(() => { run.queuedBytes -= event.data.size; });
      };
      run.recorder.onerror = () => fail(run, 'The browser video encoder failed.');
      run.recorder.onstop = () => { void finish(run); };
      for (const track of run.stream.getTracks()) {
        track.addEventListener('ended', () => {
          if (active === run && run.recorder.state !== 'inactive') fail(run, 'A recording track ended.');
        });
      }
      run.recorder.start(1000);
      // Include the current game image even if the guest has not repainted
      // since capture started. This does not advance or redraw the guest.
      const videoTrack = run.stream.getVideoTracks()[0];
      if (typeof videoTrack.requestFrame === 'function') videoTrack.requestFrame();
      stop.disabled = false;
      disclosure.textContent = 'Recording game';
      inspectAudio.disabled = false;
      status.textContent = `Recording game video and audio to ${run.name}. Waiting for the first saved chunk. Keep this session open until Stop finishes.`;
    } catch (error) {
      disclosure.textContent = 'Recording did not start';
      status.textContent = 'Recording did not start: ' + String(error.message).slice(0,240);
      release(run);
    }
  });
  stop.addEventListener('click', () => {
    if (active?.recorder?.state === 'recording') {
      inspectAudio.disabled = true;
      disclosure.textContent = 'Finalizing recording';
      stop.disabled = true; status.textContent = 'Finalizing the local recording…'; active.recorder.stop();
    }
  });

  inspectAudio.addEventListener('click', () => {
    const run = active;
    if (!run || run.failed || run.recorder?.state !== 'recording') return;
    clearAudioDownload();
    const observation = audioObservation(run);
    capturedAudio = observation ? {
      schema:'telos.recording-audio-display-capture/v1', recording:run.name,
      runtimeContext:declaredRuntimeContext(), observation,
      qualification:'Existing capture-owner observations only. No audible playback, encoded audio, game progress or replay qualification.'
    } : null;
    audioView.textContent = capturedAudio ? JSON.stringify(capturedAudio, null, 2) : 'Capture audio observation is unavailable.';
    audioView.hidden = false; exportAudio.disabled = !capturedAudio;
  });
  exportAudio.addEventListener('click', () => {
    if (!capturedAudio) return;
    try {
      clearAudioDownload();
      audioExportUrl = URL.createObjectURL(new Blob([JSON.stringify(capturedAudio, null, 2)], {type:'application/json'}));
      audioDownload.href = audioExportUrl;
      audioDownload.download = capturedAudio.recording + '-audio-observation.json';
      audioDownload.hidden = false;
      audioDownload.click();
      recoveryStatus.textContent = 'Audio download prepared. Use Save audio observation if the download does not start.';
      const parentOrigin = new URLSearchParams(location.search).get('parentOrigin');
      // The owned iframe deliberately uses no-referrer. Its bootstrap declares
      // the parent origin; the parent admits only this frame and owned session.
      const receiver = parentOrigin ? new URL(parentOrigin) : null;
      if (parent !== window && receiver && receiver.origin === parentOrigin
        && /^https?:$/.test(receiver.protocol)
        && (!document.referrer || new URL(document.referrer).origin === parentOrigin)
        && capturedAudio.runtimeContext.declaredSession) {
        const request = crypto.randomUUID();
        pendingWorkspaceAudioExport = {request, session:capturedAudio.runtimeContext.declaredSession, parentOrigin};
        parent.postMessage({type:'telos-recording-audio-export', request,
          session:pendingWorkspaceAudioExport.session, recording:capturedAudio.recording,
          file:new File([JSON.stringify(capturedAudio, null, 2)], 'audio-observation.json', {type:'application/json'})}, parentOrigin);
      }
    } catch (error) { recoveryStatus.textContent = 'Audio observation export failed: ' + String(error.message).slice(0,200); }
  });

  addEventListener('message', event => {
    const pending = pendingWorkspaceAudioExport, value = event.data;
    if (!pending || event.source !== parent || event.origin !== pending.parentOrigin
      || value?.type !== 'telos-recording-audio-export-ready' || value.session !== pending.session
      || value.request !== pending.request) return;
    recoveryStatus.textContent = 'Audio observation ready in the workspace. Use Download audio observation above the game.';
  });

  async function exportFile(name) {
    // Keep the clicked selection bound across asynchronous storage reads.
    const selectedRecording = savedName;
    if (!selectedRecording) return;
    try {
      const root = await navigator.storage.getDirectory();
      const directory = await (await root.getDirectoryHandle('kptas-recordings')).getDirectoryHandle(selectedRecording);
      const file = await (await directory.getFileHandle(name)).getFile();
      if (exportUrl) URL.revokeObjectURL(exportUrl);
      exportUrl = URL.createObjectURL(file);
      const link = document.createElement('a');
      link.href = exportUrl; link.download = selectedRecording + (name === 'game.webm' ? '.webm' : '.json');
      link.click();
    } catch (error) { recoveryStatus.textContent = 'Download failed. Local files are retained: ' + String(error.message).slice(0,200); }
  }
  download.addEventListener('click', () => { void exportFile('game.webm'); });
  downloadReceipt.addEventListener('click', () => { void exportFile('receipt.json'); });
  recover.addEventListener('click', async () => {
    try {
      const root = await navigator.storage.getDirectory();
      const directory = await root.getDirectoryHandle('kptas-recordings');
      const names = [];
      for await (const [name, handle] of directory.entries()) {
        if (handle.kind === 'directory' && /^recording-[0-9a-f-]{36}$/.test(name)) names.push(name);
        if (names.length >= 1000) break;
      }
      const previousSelection = savedName;
      saved.replaceChildren();
      for (const name of names) {
        const option = document.createElement('option'); option.value = name; option.textContent = name;
        saved.append(option);
      }
      saved.hidden = names.length === 0;
      if (names.length) {
        saved.value = names.includes(previousSelection) ? previousSelection
          : names.includes(active?.name) ? active.name : names[0];
        savedName = saved.value;
        download.disabled = downloadReceipt.disabled = active?.name === savedName && !active.savedBytes;
      }
      recoveryStatus.textContent = names.length
        ? `Found ${names.length} local recording directories. Download the receipt to check completion. Recovery may include interrupted recordings.`
        : 'No local recording directories were found.';
    } catch (error) {
      recoveryStatus.textContent = error.name === 'NotFoundError' ? 'No local recordings have been saved.'
        : 'Could not list local recordings: ' + String(error.message).slice(0,200);
    }
  });
  saved.addEventListener('change', () => {
    savedName = saved.value;
    download.disabled = downloadReceipt.disabled = !savedName || (active?.name === savedName && !active.savedBytes);
  });
  window.addEventListener('beforeunload', event => {
    if (active) { event.preventDefault(); event.returnValue = ''; }
  });
})();
