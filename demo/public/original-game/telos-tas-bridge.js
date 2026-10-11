/* Session-scoped input bridge from the prototype TAS scheduler to BoxedWine. */
(() => {
  const session = new URLSearchParams(location.search).get('ownedSession');
  const parentOrigin = window.__telosParentOrigin;
  if (parent === window || !parentOrigin || !/^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/.test(session || '')) return;

  // This multi-threaded Emscripten build compiles out SDL's DOM keyboard
  // listeners. Use BoxedWine's exported input entry point when present; retain
  // bubbling DOM events only for runtime variants that install those listeners.
  // Token names remain at the transport boundary; SDL/X11 mappings and held
  // state now live in the compiled runtime. Keep this order aligned with the
  // stable TelosControl::InputKey bit assignments.
  const keyTokens = ['w','a','s','d','q','e','space','tab','esc','enter','up','down','left','right','1','2','3','4','5','6','7','8','9'];
  // Compatibility mapping for already-published runtime releases that predate
  // telos_input_apply. Newer runtimes use the compiled bit-mask table above.
  const legacyKeyMap = Object.freeze({
    w:[26,119], a:[4,97], s:[22,115], d:[7,100], q:[20,113], e:[8,101],
    space:[44,32], tab:[43,9], esc:[41,27], enter:[40,13],
    up:[82,0x40000000|82], down:[81,0x40000000|81], left:[80,0x40000000|80], right:[79,0x40000000|79],
    '1':[30,49], '2':[31,50], '3':[32,51], '4':[33,52], '5':[34,53],
    '6':[35,54], '7':[36,55], '8':[37,56], '9':[38,57],
  });
  let lastSequence = 0;
  let inputWork = Promise.resolve();
  let focused = false;
  const legacyHeldKeys = new Set();
  const legacyHeldButtons = new Set();
  let legacyMousePoint = null;
  const capabilitiesFor = m => Object.freeze({
    // The host page currently accepts the v8 transport contract. The route
    // detail is additive, so either runtime backend remains interoperable.
    version:'telos-wasm-tas-bridge/v8',
    input:'boxedwine-native-key-export;boxedwine-native-mouse-exports',
    inputBackend:inputBackend(m),
    mouse:'absolute-canvas-pixel-native-events;pointer-lock-relative-unavailable',
    acknowledgement:'native-input-dispatch-and-one-render',
    frameAlignment:'bounded-gl-present-drain-per-score-step',
    vm:{ pause:true, step:true, frameStep:true, checkpoint:false, restore:false, seek:false },
  });

  const vmSchema = 'telos-wasm-vm-control/v1';
  const memorySchema = 'telos-wasm-guest-memory/v1';
  const vmUnit = 'render-present';
  let vmReady = false;
  let vmMemoryAvailable = false;
  let vmLastRequest = 0;
  let vmCommandSequence = 0;
  let vmLastObservedState = null;
  let vmLastCommand = null;
  const vmCommandHistory = [];
  const vmTransitionHistory = [];
  const vmStartedAt = performance.now();
  const vmStateNames = ['running','pausing','paused','stepping'];
  function vmElapsedMs() { return Math.max(0, Math.round(performance.now() - vmStartedAt)); }
  function keepRecent(list, item) {
    list.push(item);
    if (list.length > 8) list.shift();
  }
  function recordVmCommand(source, action, { request = null, sequence = null } = {}) {
    const command = { id:++vmCommandSequence, atMs:vmElapsedMs(), source, action,
      phase:'dispatched', outcome:'pending', admittedAtMs:null, completedAtMs:null,
      request:Number.isSafeInteger(request) ? request : null,
      sequence:Number.isSafeInteger(sequence) ? sequence : null };
    vmLastCommand = command;
    keepRecent(vmCommandHistory, command);
    return command;
  }
  function markVmCommand(command, phase) {
    if (!command) return;
    const now = vmElapsedMs();
    command.phase = phase;
    if (phase === 'admitted' && command.admittedAtMs === null) command.admittedAtMs = now;
    if (phase === 'completed') {
      if (command.admittedAtMs === null) command.admittedAtMs = now;
      command.completedAtMs = now;
      command.outcome = 'completed';
    } else if (['failed','timed-out','superseded'].includes(phase)) {
      command.outcome = phase;
      command.completedAtMs = now;
    }
  }
  function supersedePriorVmControlIntent(command) {
    if (!command || !['pause','resume'].includes(command.action)) return;
    for (const prior of vmCommandHistory) {
      if (prior.id !== command.id && ['pause','resume'].includes(prior.action) && prior.outcome === 'pending') {
        markVmCommand(prior, 'superseded');
      }
    }
  }
  function observeVmState(state) {
    const m = window.Module;
    const fullyPaused = state === 2 && m && typeof m._telos_vm_active_threads === 'function'
      && typeof m._telos_vm_parked_threads === 'function'
      && m._telos_vm_active_threads() > 0
      && m._telos_vm_parked_threads() === m._telos_vm_active_threads();
    if (fullyPaused) {
      for (let index = vmCommandHistory.length - 1; index >= 0; index--) {
        const command = vmCommandHistory[index];
        if (command.action === 'pause' && command.outcome === 'pending') {
          // Admission is evidenced only by the VM-wide worker barrier.
          markVmCommand(command, 'admitted');
          markVmCommand(command, 'completed');
          break;
        }
      }
    } else if (state === 0) {
      for (let index = vmCommandHistory.length - 1; index >= 0; index--) {
        const command = vmCommandHistory[index];
        if (command.action === 'resume' && command.phase === 'admitted' && command.outcome === 'pending') { markVmCommand(command, 'completed'); break; }
      }
    }
    if (vmLastObservedState !== null && state !== vmLastObservedState) {
      keepRecent(vmTransitionHistory, {
        atMs:vmElapsedMs(), from:vmStateNames[vmLastObservedState] || 'unknown',
        to:vmStateNames[state] || 'unknown',
        lastCommand:vmLastCommand ? { ...vmLastCommand, ageMs:Math.max(0, vmElapsedMs() - vmLastCommand.atMs) } : null,
      });
    }
    vmLastObservedState = state;
  }
  function copyCommand(command) {
    return command ? { ...command } : null;
  }
  function vmCommandProvenance() {
    return { schema:'telos.vm-command-provenance/v1', observedAtMs:vmElapsedMs(),
      lastCommand:copyCommand(vmLastCommand),
      recentCommands:vmCommandHistory.map(copyCommand),
      recentTransitions:vmTransitionHistory.map(transition => ({ ...transition,
        lastCommand:copyCommand(transition.lastCommand) })) };
  }
  let vmReadyTimer = 0;
  let tasReady = false;
  let memoryLastRequest = 0;
  function memoryExports() {
    const m = window.Module;
    return m && typeof m._telos_vm_process_count === 'function'
      && typeof m._telos_vm_process_id_at === 'function' && typeof m._telos_memory_read === 'function'
      && typeof m._telos_vm_status === 'function' && typeof m._telos_vm_active_threads === 'function'
      && typeof m._telos_vm_parked_threads === 'function' && typeof m._malloc === 'function'
      && typeof m._free === 'function' && m.HEAPU8 instanceof Uint8Array ? m : null;
  }
  function memoryStatus(code) {
    return ({ 1:'The game must be fully paused before reading guest memory.',
      2:'The guest process is unavailable.', 3:'The guest memory range or output buffer is invalid.',
      4:'The guest memory range is unreadable.', 5:'Guest memory reads are limited to 64 KiB.' })[code] || 'The runtime rejected the guest memory request.';
  }
  function memoryError(code) {
    const error = new Error(memoryStatus(code));
    error.statusCode = code;
    return error;
  }
  function memoryReply(request, operation, result = {}, error = null) {
    const message = { type:'telos-wasm-guest-memory-reply', schema:memorySchema, session, request, operation,
      ok:!error, ...(error ? { status:error.statusCode ?? null, error:error.message } : result) };
    try {
      parent.postMessage(message, parentOrigin, result.buffer ? [result.buffer] : []);
      return true;
    } catch {
      if (!error && result.buffer) {
        try {
          parent.postMessage({ type:'telos-wasm-guest-memory-reply', schema:memorySchema, session, request, operation,
            ok:false, status:null, error:'The runtime could not transfer the guest memory reply.' }, parentOrigin);
        } catch { /* A disconnected parent will time out its request. */ }
      }
      return false;
    }
  }
  function memoryPaused(m) {
    return m._telos_vm_status() === 2 && m._telos_vm_active_threads() > 0
      && m._telos_vm_parked_threads() === m._telos_vm_active_threads();
  }
  function handleMemoryRequest(message) {
    const { request, operation } = message;
    if (!Number.isSafeInteger(request) || request <= memoryLastRequest || !['processes','read'].includes(operation)) return;
    memoryLastRequest = request;
    inputWork = inputWork.then(() => {
      const m = memoryExports();
      if (!m) throw new Error('Guest memory reading is unavailable in this runtime.');
      if (!memoryPaused(m)) throw memoryError(1);
      if (operation === 'processes') {
        const count = m._telos_vm_process_count();
        if (!Number.isSafeInteger(count) || count < 0 || count > 4096) throw new Error('The runtime returned an invalid process count.');
        const processes = [];
        for (let index = 0; index < count; index++) {
          const pid = m._telos_vm_process_id_at(index);
          if (!Number.isSafeInteger(pid) || pid <= 0 || pid > 0xffffffff) throw new Error('The runtime returned an invalid process ID.');
          processes.push({ pid });
        }
        memoryReply(request, operation, { processes });
        return;
      }
      const { pid, address, length } = message;
      if (!Number.isSafeInteger(pid) || pid <= 0 || pid > 0xffffffff
        || !Number.isSafeInteger(address) || address < 0 || address > 0xffffffff
        || !Number.isSafeInteger(length) || length < 1 || length > 65536
        || address + length > 0x100000000) throw memoryError(3);
      const pointer = m._malloc(length);
      if (!Number.isSafeInteger(pointer) || pointer <= 0 || pointer + length > m.HEAPU8.length) {
        if (pointer) m._free(pointer);
        throw new Error('The runtime could not allocate a bounded guest memory buffer.');
      }
      try {
        if (!memoryPaused(m)) throw memoryError(1);
        const status = m._telos_memory_read(pid, address, length, pointer);
        if (status !== 0) throw memoryError(status);
        // Copy before freeing; no view into WASM memory may escape this frame.
        const bytes = m.HEAPU8.slice(pointer, pointer + length);
        memoryReply(request, operation, { buffer:bytes.buffer }, null);
      } finally { m._free(pointer); }
    }).catch(error => {
      const bounded = new Error(String(error?.message || error).slice(0, 240));
      bounded.statusCode = error?.statusCode;
      memoryReply(request, operation, {}, bounded);
    });
  }
  function inputBackend(m) {
    if (typeof m?._telos_input_apply === 'function' && typeof m?._telos_input_release_all === 'function') return 'batch';
    if (typeof m?._telos_input_apply === 'undefined' && typeof m?._telos_input_release_all === 'undefined' &&
        typeof m?._boxedwineEmscriptenKey === 'function' && typeof m?._boxedwineEmscriptenMouseMove === 'function' &&
        typeof m?._boxedwineEmscriptenMouseButton === 'function') return 'legacy';
    return null;
  }
  function vmExports() {
    const m = window.Module;
    if (!m || typeof m._telos_vm_pause !== 'function' || typeof m._telos_vm_resume !== 'function' ||
        typeof m._telos_vm_step_present !== 'function' || typeof m._telos_vm_status !== 'function' ||
        typeof m._telos_vm_active_threads !== 'function' || typeof m._telos_vm_parked_threads !== 'function' ||
        typeof m._telos_input_begin !== 'function' || typeof m._telos_input_end !== 'function' ||
        typeof m._telos_input_last_sequence !== 'function' || typeof m._telos_input_last_event_count !== 'function' ||
        typeof m._telos_input_last_present_low !== 'function' || typeof m._telos_vm_present_count_low !== 'function' ||
        !inputBackend(m)) return null;
    return m;
  }
  function vmStatus(ackRequest = null, error = null) {
    const m = vmExports();
    const state = m ? m._telos_vm_status() : 0;
    observeVmState(state);
    const names = ['running','pausing','paused','stepping'];
    const assetIo = m && typeof m.getOwnedInstallIoStats === 'function' ? m.getOwnedInstallIoStats() : null;
    let sdlDispatch = null;
    try {
      const metric = m?._boxedwine_sdl_dispatch_metric;
      if (typeof metric === 'function') {
        const [calls, callerMicros, callbackMicros] = [0, 1, 2].map(index => metric(index));
        const safeCounter = value => typeof value === 'bigint'
          ? value >= 0n && value <= BigInt(Number.MAX_SAFE_INTEGER) ? Number(value) : null
          : Number.isSafeInteger(value) && value >= 0 ? value : null;
        const snapshot = { calls:safeCounter(calls), callerMicros:safeCounter(callerMicros), callbackMicros:safeCounter(callbackMicros) };
        if (Object.values(snapshot).every(Number.isSafeInteger)) sdlDispatch = snapshot;
      }
    } catch { /* Optional counters must never interfere with controls or status. */ }
    parent.postMessage({
      type:'telos-wasm-vm-status', schema:vmSchema, session, ackRequest,
      available:!!m, state:names[state] || 'unknown', unit:vmUnit,
      activeThreads:m && typeof m._telos_vm_active_threads === 'function' ? m._telos_vm_active_threads() : null,
      parkedThreads:m && typeof m._telos_vm_parked_threads === 'function' ? m._telos_vm_parked_threads() : null,
      presents:m && typeof m._telos_vm_present_count_low === 'function' ? m._telos_vm_present_count_low() : null,
      sdlDispatch,
      assetIo,
      commandProvenance:vmCommandProvenance(),
      presentMarker:'monotonic BoxedWine GL render-present boundary',
      clockBehavior:'host-derived guest clocks continue while paused',
      error,
    }, parentOrigin);
  }
  async function requestPresentStep(m, sequence, deadline, provenance = {}) {
    const command = recordVmCommand(provenance.source || 'unknown', 'step', provenance);
    while (performance.now() < deadline) {
      if (sequence !== null && lastSequence !== sequence) {
        markVmCommand(command, 'superseded');
        return null;
      }
      let result;
      try { result = m._telos_vm_step_present(); }
      catch (error) { markVmCommand(command, 'failed'); throw error; }
      if (result === 0) {
        markVmCommand(command, 'admitted');
        return command;
      }
      // Pause requests are asynchronous across guest workers. A worker can
      // leave its safe point between the last barrier poll and this call, so
      // retry until the same bounded deadline instead of failing on that race.
      if (result === 1 || result === 3) {
        await delay(2);
        continue;
      }
      if (result === 2) { markVmCommand(command, 'failed'); throw new Error('A rendered-presentation step is already in progress.'); }
      markVmCommand(command, 'failed');
      throw new Error('The runtime rejected the rendered-presentation step.');
    }
    if (sequence !== null && lastSequence !== sequence) {
      markVmCommand(command, 'superseded');
      return null;
    }
    markVmCommand(command, 'timed-out');
    throw new Error('No runnable guest worker became available to render the next presentation.');
  }
  async function advanceOnePresent(m, provenance = {}) {
    const start = m._telos_vm_present_count_low();
    const sequence = lastSequence;
    const deadline = performance.now() + 10000;
    // The button promises a single render from either state. Park the guest
    // first; _telos_vm_step_present only admits a step after the VM-wide
    // barrier has acknowledged every live worker.
    if (m._telos_vm_status() !== 2) {
      const pauseCommand = recordVmCommand(provenance.source || 'vm-control', 'pause', provenance);
      try { m._telos_vm_pause(); }
      catch (error) { markVmCommand(pauseCommand, 'failed'); throw error; }
      supersedePriorVmControlIntent(pauseCommand);
      if (!(await waitForPause(m, sequence, pauseCommand))) {
        if (sequence !== null && lastSequence !== sequence) return;
        throw new Error('The game workers did not reach the pause boundary before the render deadline.');
      }
    }
    const stepCommand = await requestPresentStep(m, sequence, deadline, provenance);
    if (!stepCommand) return;
    try {
      while (performance.now() < deadline) {
        if (sequence !== null && lastSequence !== sequence) {
          markVmCommand(stepCommand, 'superseded');
          return;
        }
        const delta = (m._telos_vm_present_count_low() - start) >>> 0;
        const active = m._telos_vm_active_threads();
        const parked = m._telos_vm_parked_threads();
        if (delta > 1 && delta < 0x80000000) {
          markVmCommand(stepCommand, 'failed');
          throw new Error('More than one GL presentation completed during the step.');
        }
        if (delta === 1 && m._telos_vm_status() === 2 && active > 0 && parked === active) {
          markVmCommand(stepCommand, 'completed');
          return;
        }
        await delay(2);
      }
      if (sequence !== null && lastSequence !== sequence) {
        markVmCommand(stepCommand, 'superseded');
        return;
      }
      markVmCommand(stepCommand, 'timed-out');
      throw new Error('The guest did not reach one GL presentation and return to its pause barrier.');
    } catch (error) {
      if (stepCommand.outcome === 'pending') markVmCommand(stepCommand, 'failed');
      throw error;
    }
  }
  function announceVmReady() {
    const m = vmExports();
    if (m) {
      const memoryAvailable = !!memoryExports();
      if (vmReady && memoryAvailable === vmMemoryAvailable) return;
      vmReady = true;
      vmMemoryAvailable = memoryAvailable;
      clearInterval(vmReadyTimer);
      parent.postMessage({ type:'telos-wasm-vm-ready', schema:vmSchema, session, available:true,
        unit:vmUnit, capabilities:{pause:true,resume:true,step:true,frameStep:true,checkpoint:false,restore:false,seek:false,presentMarker:true,memoryRead:memoryAvailable,inputBackend:inputBackend(m)} }, parentOrigin);
      vmStatus();
    }
  }
  function announceTasReady() {
    if (tasReady) return true;
    const m = vmExports();
    // Readiness reports that a real frame was presented and the bridge can
    // accept controls. It must not engage the TAS or pause normal game startup;
    // applyInputAtPresent establishes the pause boundary for the first score
    // input, while explicit user activation owns pause-on-engage behavior.
    if (!m || m._telos_vm_present_count_low() === 0) return false;
    tasReady = true;
    parent.postMessage({ type:'telos-wasm-tas-ready', session, capabilities:capabilitiesFor(m) }, parentOrigin);
    return true;
  }
  vmReadyTimer = setInterval(announceVmReady, 100);
  const tasReadyTimer = setInterval(() => {
    if (announceTasReady()) clearInterval(tasReadyTimer);
  }, 100);
  setTimeout(() => {
    if (vmReady) return;
    parent.postMessage({ type:'telos-wasm-vm-ready', schema:vmSchema, session, available:false,
      unit:vmUnit, capabilities:{pause:false,resume:false,step:false,frameStep:false,checkpoint:false,restore:false,seek:false,presentMarker:false,memoryRead:false} }, parentOrigin);
    // Some builds expose Module only after asynchronous runtime startup. Keep
    // looking at a low rate so a late-ready build can enable controls without
    // reloading the iframe; the negative announcement is informational only.
    clearInterval(vmReadyTimer);
    vmReadyTimer = setInterval(announceVmReady, 1000);
  }, 20000);

  function reportInputTiming(sequence, diagnostics) {
    if (diagnostics.totalMs >= 100 || sequence % 30 === 0) {
      console.info('Telos TAS timing', JSON.stringify({
          sequence, ...diagnostics,
        settleMs:Math.max(0, diagnostics.totalMs - diagnostics.pauseMs - diagnostics.dispatchMs - diagnostics.stepWaitMs),
      }));
    }
  }
  function activeCanvas() {
    const gl = document.getElementById('boxedwine-webgl-canvas-0');
    if (gl && gl.width && gl.height && getComputedStyle(gl).display !== 'none' && getComputedStyle(gl).visibility !== 'hidden') return gl;
    return document.getElementById('canvas');
  }
  const delay = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));
  async function waitForPause(m, sequence, command = null) {
    const deadline = performance.now() + 10000;
    try {
      while (lastSequence === sequence && performance.now() < deadline) {
        const active = m._telos_vm_active_threads();
        const parked = m._telos_vm_parked_threads();
        if (m._telos_vm_status() === 2 && active > 0 && parked === active) {
          if (command) {
            // Admission is evidenced only by the VM-wide worker barrier.
            markVmCommand(command, 'admitted');
            markVmCommand(command, 'completed');
          }
          return true;
        }
        await delay(2);
      }
    } catch (error) {
      if (command?.outcome === 'pending') markVmCommand(command, 'failed');
      throw error;
    }
    if (command) markVmCommand(command, lastSequence === sequence ? 'timed-out' : 'superseded');
    return false;
  }
  function dispatchInput(message, m) {
    if (inputBackend(m) === 'legacy') return dispatchLegacyInput(message, m);
    let keysMask = 0;
    for (const token of message.keys) keysMask |= 1 << keyTokens.indexOf(token);
    let buttonsMask = 0;
    for (const button of message.buttons) buttonsMask |= button === 'lmb' ? 1 : 2;
    if (message.mouse) {
      const canvas = activeCanvas(), rect = canvas?.getBoundingClientRect();
      if (!canvas || !rect?.width || !rect?.height || !canvas.width || !canvas.height) throw new Error('The game canvas is not ready to receive mouse input.');
    }
    const countPointer = m._malloc(4);
    if (!Number.isSafeInteger(countPointer) || countPointer <= 0 || countPointer + 4 > m.HEAPU8.length) {
      if (countPointer) m._free(countPointer);
      throw new Error('The runtime could not allocate the bounded input result.');
    }
    try {
      const status = m._telos_input_apply(message.sequence >>> 0, keysMask, buttonsMask,
        message.mouse ? 1 : 0, message.mouse?.x ?? 0, message.mouse?.y ?? 0, countPointer);
      const eventsDispatched = m.HEAPU32[countPointer >>> 2];
      if (status !== 0 && status !== 13 && status !== 14) {
        throw new Error(({1:'Invalid input sequence.',2:'Another input batch is open.',3:'The previous input batch is still queued.',
          4:'The runtime rejected a repeated or stale input sequence.',10:'The runtime rejected invalid input data.',
          11:'The game runtime has no active input target.',12:'The runtime could not close the input batch.',
          15:'The runtime generated no guest event for the requested key or button transition.'})[status] || 'The runtime rejected the input batch.');
      }
      focused = true;
      return { eventsDispatched, requiresConsumption:status === 0 || status === 13 };
    } finally { m._free(countPointer); }
  }
  function legacyKey(m, token, down) {
    const mapping = legacyKeyMap[token];
    if (!mapping) throw new Error('The runtime rejected an unknown keyboard token.');
    m._boxedwineEmscriptenKey(mapping[0], mapping[1], down ? 1 : 0);
  }
  function legacyMouseButton(m, button, down, point) {
    const nativeButton = button === 'lmb' ? 0 : 1;
    m._boxedwineEmscriptenMouseButton(down ? 1 : 0, nativeButton, point?.x ?? 0, point?.y ?? 0);
  }
  function dispatchLegacyInput(message, m) {
    const sequence = message.sequence >>> 0;
    const beginStatus = m._telos_input_begin(sequence);
    if (beginStatus !== 0) {
      throw new Error(({1:'Invalid input sequence.',2:'Another input batch is open.',3:'The previous input batch is still queued.',
        4:'The runtime rejected a repeated or stale input sequence.'})[beginStatus] || 'The runtime rejected the input batch.');
    }

    let eventsDispatched = 0;
    let eventTransitions = 0;
    const wantedKeys = new Set(message.keys);
    const wantedButtons = new Set(message.buttons);
    try {
      for (const token of legacyHeldKeys) {
        if (!wantedKeys.has(token)) {
          legacyKey(m, token, false);
          legacyHeldKeys.delete(token);
          eventsDispatched++;
          eventTransitions++;
        }
      }
      for (const token of wantedKeys) {
        if (!legacyHeldKeys.has(token)) {
          legacyKey(m, token, true);
          legacyHeldKeys.add(token);
          eventsDispatched++;
          eventTransitions++;
        }
      }

      if (message.mouse && (!legacyMousePoint || message.mouse.x !== legacyMousePoint.x || message.mouse.y !== legacyMousePoint.y)) {
        const canvas = activeCanvas(), rect = canvas?.getBoundingClientRect();
        if (!canvas || !rect?.width || !rect?.height || !canvas.width || !canvas.height) throw new Error('The game canvas is not ready to receive mouse input.');
        m._boxedwineEmscriptenMouseMove(message.mouse.x, message.mouse.y);
        legacyMousePoint = { x:message.mouse.x, y:message.mouse.y };
        eventsDispatched++;
      }

      const point = legacyMousePoint;
      for (const button of legacyHeldButtons) {
        if (!wantedButtons.has(button)) {
          legacyMouseButton(m, button, false, point);
          legacyHeldButtons.delete(button);
          eventsDispatched++;
          eventTransitions++;
        }
      }
      for (const button of wantedButtons) {
        if (!legacyHeldButtons.has(button)) {
          legacyMouseButton(m, button, true, point);
          legacyHeldButtons.add(button);
          eventsDispatched++;
          eventTransitions++;
        }
      }
      focused = true;
    } catch (error) {
      // The runtime's exports are void, so a partial dispatch cannot be
      // rolled back or represented as success. Release every tracked input.
      releaseAll();
      m._telos_input_end(sequence);
      throw error;
    }

    const endStatus = m._telos_input_end(sequence);
    if (endStatus === 1) throw new Error('The runtime could not close the input batch.');
    if (endStatus === 2 && eventTransitions === 0) {
      throw new Error('The runtime reported pending guest events without a keyboard or button transition.');
    }
    if (endStatus === 3 && eventTransitions > 0) {
      throw new Error('The runtime generated no guest event for the requested key or button transition.');
    }
    if (![0,2,3].includes(endStatus)) throw new Error('The runtime rejected the input batch.');
    return { eventsDispatched, requiresConsumption:endStatus === 0 || endStatus === 2 };
  }
  async function applyInputAtPresent(message) {
    const m = vmExports();
    if (!m) throw new Error('The game runtime is not ready for TAS input.');
    const startedAt = performance.now();
    let pauseMs = 0, dispatchMs = 0, stepWaitMs = 0, polls = 0;
    let stepCommand = null;

    // Each score tick owns one complete runtime presentation. Park the guest
    // first so a browser timer or busy guest cannot consume this input on an
    // earlier, unrecorded render.
    const pauseCommand = recordVmCommand('score-input', 'pause', { sequence:message.sequence });
    try { m._telos_vm_pause(); }
    catch (error) { markVmCommand(pauseCommand, 'failed'); throw error; }
    supersedePriorVmControlIntent(pauseCommand);
    const pauseStartedAt = performance.now();
    if (!(await waitForPause(m, message.sequence, pauseCommand))) {
      if (lastSequence === message.sequence) throw new Error('The game workers did not reach the pause boundary before the input deadline.');
      return;
    }
    pauseMs = performance.now() - pauseStartedAt;
    if (lastSequence !== message.sequence) return;

    try {
      const startPresent = m._telos_vm_present_count_low();
      const dispatchStartedAt = performance.now();
      const { eventsDispatched, requiresConsumption } = dispatchInput(message, m);
      dispatchMs = performance.now() - dispatchStartedAt;
      const deadline = performance.now() + 10000;
      while (lastSequence === message.sequence && performance.now() < deadline) {
      const stepStartedAt = performance.now();
      stepCommand = await requestPresentStep(m, message.sequence, deadline, { source:'score-input', sequence:message.sequence });
      if (!stepCommand) return;
      while (lastSequence === message.sequence && performance.now() < deadline) {
        polls++;
        const present = m._telos_vm_present_count_low();
        const presentDelta = (present - startPresent) >>> 0;
        const paused = m._telos_vm_status() === 2
          && m._telos_vm_active_threads() > 0
          && m._telos_vm_parked_threads() === m._telos_vm_active_threads();
        if (presentDelta > 1 && presentDelta < 0x80000000) {
          markVmCommand(stepCommand, 'failed');
          throw new Error('The runtime rendered more than one presentation during a score-frame step. Playback stopped to prevent frame drift.');
        }
        const inputConsumed = m._telos_input_last_sequence() === (message.sequence >>> 0);
        if (presentDelta === 1 && paused && (!requiresConsumption || inputConsumed)) {
          markVmCommand(stepCommand, 'completed');
          stepWaitMs += performance.now() - stepStartedAt;
          const diagnostics = { totalMs:performance.now()-startedAt, pauseMs, dispatchMs, stepWaitMs, polls };
          reportInputTiming(message.sequence, diagnostics);
          const inputPresentOffset = inputConsumed
            ? (m._telos_input_last_present_low() - startPresent) >>> 0
            : null;
          parent.postMessage({
            type:'telos-wasm-tas-ack', session, sequence:message.sequence, tick:message.tick,
            state:'input-applied', eventsDispatched, eventsConsumed:inputConsumed ? m._telos_input_last_event_count() : 0, present,
            presentsAdvanced:1, inputPresentOffset,
            inputBoundary:inputConsumed ? 'guest-consumed-events' : eventsDispatched ? 'native-mouse-position' : 'unchanged-held-state', frameBoundary:'bounded-boxedwine-gl-present',
            diagnostics,
          }, parentOrigin);
          return;
        }
        await delay(2);
      }
    }
      if (stepCommand?.outcome === 'pending') markVmCommand(stepCommand, lastSequence === message.sequence ? 'timed-out' : 'superseded');
      if (lastSequence === message.sequence) throw new Error('The game did not complete the rendered input step before the deadline. Playback stopped at the current tick.');
    } catch (error) {
      if (stepCommand?.outcome === 'pending') markVmCommand(stepCommand, 'failed');
      throw error;
    }
  }
  function releaseAll() {
    const m = window.Module;
    const release = m?._telos_input_release_all;
    if (typeof release === 'function') {
      try {
        const status = release();
        if (status !== 0 && status !== 11) console.error('TAS input release was rejected:', status);
      } catch (error) { console.error('TAS input release failed:', error); }
      return;
    }
    if (inputBackend(m) === 'legacy') {
      for (const token of legacyHeldKeys) {
        try { legacyKey(m, token, false); } catch (error) { console.error('TAS key release failed:', error); }
      }
      legacyHeldKeys.clear();
      const point = legacyMousePoint;
      for (const button of legacyHeldButtons) {
        try { legacyMouseButton(m, button, false, point); } catch (error) { console.error('TAS mouse release failed:', error); }
      }
      legacyHeldButtons.clear();
    }
  }
  function validInput(value) {
    if (!value || !Number.isSafeInteger(value.sequence) || value.sequence <= lastSequence || (value.sequence >>> 0) === 0 || !Number.isSafeInteger(value.tick) || value.tick < 1) return false;
    if (!Array.isArray(value.keys) || value.keys.length > keyTokens.length || value.keys.some(key => !keyTokens.includes(key)) || new Set(value.keys).size !== value.keys.length) return false;
    if (!Array.isArray(value.buttons) || value.buttons.length > 2 || value.buttons.some(button => !['lmb','rmb'].includes(button)) || new Set(value.buttons).size !== value.buttons.length) return false;
    if (value.mouse !== null && (!value.mouse || !Number.isSafeInteger(value.mouse.x) || !Number.isSafeInteger(value.mouse.y) || value.mouse.x < 0 || value.mouse.x > 9999 || value.mouse.y < 0 || value.mouse.y > 9999)) return false;
    return true;
  }
  addEventListener('message', event => {
    if (event.source === parent && event.origin === parentOrigin && event.data?.session === session &&
        event.data?.type === 'telos-wasm-guest-memory' && event.data?.schema === memorySchema) {
      handleMemoryRequest(event.data);
      return;
    }
    if (event.source === parent && event.origin === parentOrigin && event.data?.session === session &&
        event.data?.type === 'telos-wasm-vm-control' && event.data?.schema === vmSchema) {
      const message = event.data;
      const request = message.request;
      if (!Number.isSafeInteger(request) || request <= vmLastRequest || !['pause','resume','step','frame','status'].includes(message.action)) return;
      if ((message.action === 'step' || message.action === 'frame') && message.unit !== vmUnit) {
        vmLastRequest = request;
        vmStatus(request, `Step boundary must be ${vmUnit}.`);
        return;
      }
      vmLastRequest = request;
      inputWork = inputWork.then(async () => {
        const m = vmExports();
        if (!m) { vmStatus(request, 'Pause and step are unavailable in this game session.'); return; }
        if (message.action === 'pause') {
          const command = recordVmCommand('vm-control', 'pause', { request });
          try { m._telos_vm_pause(); }
          catch (error) { markVmCommand(command, 'failed'); throw error; }
          supersedePriorVmControlIntent(command);
        } else if (message.action === 'resume') {
          const command = recordVmCommand('vm-control', 'resume', { request });
          try { m._telos_vm_resume(); }
          catch (error) { markVmCommand(command, 'failed'); throw error; }
          supersedePriorVmControlIntent(command);
          markVmCommand(command, 'admitted');
        } else if (message.action === 'step' || message.action === 'frame') {
          await advanceOnePresent(m, { source:'vm-control', request });
        }
        vmStatus(request);
      }).catch(error => {
        vmStatus(request, String(error?.message || error));
      });
      return;
    }
    if (event.source !== parent || event.origin !== parentOrigin || event.data?.session !== session || event.data?.type !== 'telos-wasm-tas') return;
    const message = event.data;
    if (message.action === 'release') {
      inputWork = inputWork.then(async () => {
        if (Number.isSafeInteger(message.sequence)) lastSequence = Math.max(lastSequence, message.sequence);
        const m = vmExports();
        if (m && await waitForPause(m, message.sequence)) {
          m._telos_input_begin(message.sequence || 0);
          releaseAll();
          m._telos_input_end(message.sequence || 0);
        } else {
          releaseAll();
        }
        parent.postMessage({ type:'telos-wasm-tas-ack', session, sequence:message.sequence, tick:message.tick, state:'released' }, parentOrigin);
      }).catch(error => {
        releaseAll();
        parent.postMessage({ type:'telos-wasm-tas-ack', session, sequence:message.sequence, tick:message.tick, state:'error', error:String(error?.message || error) }, parentOrigin);
      });
      return;
    }
    if (message.action !== 'input' || !validInput(message)) return;
    lastSequence = message.sequence;
    const m = vmExports();
    if (!m) {
      parent.postMessage({ type:'telos-wasm-tas-ack', session, sequence:message.sequence, tick:message.tick, state:'error', error:'The game runtime is not ready for TAS input.' }, parentOrigin);
      return;
    }
    inputWork = inputWork.then(() => applyInputAtPresent(message)).catch(error => {
      if (lastSequence === message.sequence) {
        parent.postMessage({ type:'telos-wasm-tas-ack', session, sequence:message.sequence, tick:message.tick, state:'error', error:String(error?.message || error) }, parentOrigin);
      }
    });
  });
  addEventListener('blur', () => { if (focused) { releaseAll(); focused = false; } });
  addEventListener('pagehide', releaseAll);
  announceVmReady();
  setInterval(() => {
    const available = !!vmExports();
    if (available && !vmReady) announceVmReady();
    else if (!available && vmReady) {
      vmReady = false;
      parent.postMessage({ type:'telos-wasm-vm-ready', schema:vmSchema, session, available:false,
        unit:vmUnit, capabilities:{pause:false,resume:false,step:false,frameStep:false,checkpoint:false,restore:false,seek:false,presentMarker:false,memoryRead:false} }, parentOrigin);
      vmMemoryAvailable = false;
    } else if (available) { announceVmReady(); vmStatus(); }
  }, 500);
})();
