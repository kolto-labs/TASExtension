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
  const keyTokens = ['w','a','s','d','q','e','space','tab','esc','enter','up','down','left','right','1','2','3','4','5','6','7','8','9','i'];
  function supportedKeyTokens(m) {
    // Old compiled and legacy runtimes retain the original 23-key prefix.
    // Extra keys require an explicit compiled table declaration.
    let count = 23;
    if (inputBackend(m) === 'batch' && typeof m._telos_input_key_count === 'function') {
      try { if (m._telos_input_key_count() === keyTokens.length) count = keyTokens.length; } catch {}
    }
    return keyTokens.slice(0, count);
  }
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
  let activeScoreInput = null;
  const legacyHeldKeys = new Set();
  const legacyHeldButtons = new Set();
  let legacyMousePoint = null;
  const renderDeadlineMs = 60000;
  const inputDeadlineMs = 90000;
  const consumptionPresent = m => typeof m?._telos_vm_step_input_present === 'function'
    && typeof m?._telos_vm_input_present_version === 'function' && m._telos_vm_input_present_version() === 1;
  const capabilitiesFor = m => Object.freeze({
    version:consumptionPresent(m) ? 'telos-wasm-tas-bridge/v12' : 'telos-wasm-tas-bridge/v11',
    supportedKeys:Object.freeze(supportedKeyTokens(m)),
    input:'boxedwine-native-key-export;boxedwine-native-mouse-exports',
    inputBackend:inputBackend(m),
    mouse:'absolute-canvas-pixel-native-events;pointer-lock-relative-unavailable',
    acknowledgement:consumptionPresent(m) ? 'native-event-consumption-then-next-render' : 'native-input-dispatch-and-one-render',
    frameAlignment:consumptionPresent(m) ? 'quiescent-pause-and-next-gl-present-after-native-consumption' : 'quiescent-pause-and-bounded-gl-present-per-score-step',
    inputDeadlineMs,
    renderDeadlineMs,
    vm:{ pause:true, step:true, frameStep:true, checkpoint:false, restore:false, seek:false },
  });

  const vmSchema = 'telos-wasm-vm-control/v1';
  const memorySchema = 'telos-wasm-guest-memory/v1';
  const vmUnit = 'render-present';
  let vmReady = false;
  let vmMemoryAvailable = false;
  let vmLastRequest = 0;
  let vmLastIntentRequest = 0;
  let vmCommandSequence = 0;
  let vmLastObservedState = null;
  let vmLastCommand = null;
  let pausedGlCounts = null;
  let guestGlErrorReplies = null;
  let hostGlQueryOutputs = null;
  let vmLastError = null;
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
    pausedGlCounts = null;
    guestGlErrorReplies = null;
    hostGlQueryOutputs = null;
    vmLastError = null;
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
    if (!Number.isSafeInteger(request) || request <= memoryLastRequest || !['processes','read','threads','registers','mapping'].includes(operation)) return;
    memoryLastRequest = request;
    inputWork = inputWork.then(() => {
      const m = memoryExports();
      if (!m) throw new Error('Guest memory reading is unavailable in this runtime.');
      if (!memoryPaused(m)) throw memoryError(1);
      if (operation === 'mapping') {
        if (typeof m._telos_vm_paused_mapping_abi_version !== 'function'
          || m._telos_vm_paused_mapping_abi_version() !== 1
          || typeof m._telos_vm_copy_paused_mapping !== 'function') throw new Error('Paused backing-file capture is unavailable.');
        const { pid, address } = message;
        if (!Number.isSafeInteger(pid) || pid < 1 || pid > 0xffffffff
          || !Number.isSafeInteger(address) || address < 0 || address > 0xffffffff) throw memoryError(3);
        const capacity = 4097, total = 32 + capacity, pointer = m._malloc(total);
        if (!Number.isSafeInteger(pointer) || pointer <= 0 || pointer % 4 || pointer + total > m.HEAPU8.length) {
          if (pointer) m._free(pointer);
          throw new Error('The runtime could not allocate bounded mapping metadata.');
        }
        try {
          const status = m._telos_vm_copy_paused_mapping(pid, address, pointer + 32, capacity, pointer);
          if (status !== 0) throw new Error(({ 1:'Pause all workers before capturing a backing file.',
            2:'The selected process is unavailable.', 3:'The mapping request is invalid.',
            4:'No backing file is mapped at this address.', 5:'The backing path exceeds the capture limit.',
            6:'Mapping metadata is busy; capture again while paused.', 7:'Mapping metadata allocation failed.',
            8:'This runtime does not support paused mapping capture.' })[status] || 'Mapping capture was refused.');
          if (!memoryPaused(m) || pointer + total > m.HEAPU8.length) throw memoryError(1);
          const view = new DataView(m.HEAPU8.buffer, pointer, 32);
          const length = view.getUint32(4, true), offsetAvailable = view.getUint32(20, true);
          if (view.getUint32(0, true) !== 1 || length < 1 || length >= capacity
            || offsetAvailable > 1 || m.HEAPU8[pointer + 32 + length] !== 0
            || (!offsetAvailable && (view.getUint32(24, true) || view.getUint32(28, true)))) throw new Error('Invalid mapping metadata ABI output.');
          const hex = (low, high) => `0x${view.getUint32(high, true).toString(16).padStart(8,'0')}${view.getUint32(low, true).toString(16).padStart(8,'0')}`;
          const path = new TextDecoder('utf-8', { fatal:true }).decode(m.HEAPU8.slice(pointer + 32, pointer + 32 + length));
          if (/[\x00-\x1f\x7f]/.test(path)) throw new Error('The backing path contains unsupported control bytes.');
          const start = view.getUint32(8, true), extent = hex(12, 16);
          if (BigInt(extent) < 1n || BigInt(start) + BigInt(extent) > 0x100000000n
            || address < start || BigInt(address) >= BigInt(start) + BigInt(extent)) throw new Error('Mapping metadata does not contain the requested address.');
          memoryReply(request, operation, { pid, address, scope:'process', captured:true,
            semantics:'mapped-file-address', path, start, length:extent,
            fileOffset:offsetAvailable ? hex(24, 28) : null });
        } finally { m._free(pointer); }
        return;
      }
      if (operation === 'registers') {
        if (typeof m._telos_vm_copy_paused_registers !== 'function') throw new Error('Register capture is unavailable in this runtime.');
        const { pid } = message;
        if (!Number.isSafeInteger(pid) || pid <= 0 || pid > 0xffffffff) throw memoryError(3);
        const capacity = 128, length = capacity * 12 * 4;
        const pointer = m._malloc(length);
        if (!Number.isSafeInteger(pointer) || pointer <= 0 || pointer % 4 || pointer + length > m.HEAPU8.length) {
          if (pointer) m._free(pointer);
          throw new Error('The runtime could not allocate a bounded register buffer.');
        }
        try {
          const count = m._telos_vm_copy_paused_registers(pid, pointer, capacity);
          if (!Number.isSafeInteger(count) || count < 0 || count > capacity) throw new Error('Register capture requires a complete paused process with at most 128 registered workers.');
          if (pointer + count * 48 > m.HEAPU8.length || !memoryPaused(m)) throw memoryError(1);
          const view = new DataView(m.HEAPU8.buffer, pointer, count * 48), threads = [];
          const names = ['eip','eax','ecx','edx','ebx','esp','ebp','esi','edi'];
          for (let index = 0; index < count; index++) {
            const base = index * 48, id = view.getUint32(base, true), owner = view.getUint32(base + 4, true), state = view.getUint32(base + 8, true);
            if (!id || owner !== pid || ![2,3].includes(state)) throw new Error('The runtime returned an invalid register row.');
            threads.push({ id, pid, state:state === 2 ? 'kernel-wait' : 'paused',
              registers:names.map((name, offset) => ({ name, value:`0x${view.getUint32(base + 12 + offset * 4, true).toString(16).padStart(8,'0')}` })) });
          }
          if (new Set(threads.map(thread => thread.id)).size !== threads.length) throw new Error('The runtime returned duplicate register threads.');
          memoryReply(request, operation, { pid, threads, captured:true, architecture:'x86', semantics:'saved-guest-registers' });
        } finally { m._free(pointer); }
        return;
      }
      if (operation === 'threads') {
        if (typeof m._telos_vm_copy_paused_threads !== 'function') throw new Error('Thread listing is unavailable in this runtime.');
        const { pid } = message;
        if (!Number.isSafeInteger(pid) || pid <= 0 || pid > 0xffffffff) throw memoryError(3);
        const capacity = 4096, length = capacity * 3 * 4;
        const pointer = m._malloc(length);
        if (!Number.isSafeInteger(pointer) || pointer <= 0 || pointer % 4 || pointer + length > m.HEAPU8.length) {
          if (pointer) m._free(pointer);
          throw new Error('The runtime could not allocate a bounded thread buffer.');
        }
        try {
          const count = m._telos_vm_copy_paused_threads(pointer, capacity);
          if (!Number.isSafeInteger(count) || count < 1 || count > capacity) throw new Error('Thread capture requires a complete paused barrier and at most 4096 workers.');
          const view = new DataView(m.HEAPU8.buffer, pointer, count * 12), threads = [];
          for (let index = 0; index < count; index++) {
            const id = view.getUint32(index * 12, true), owner = view.getUint32(index * 12 + 4, true), state = view.getUint32(index * 12 + 8, true);
            if (!id || !owner || ![2,3].includes(state)) throw new Error('The runtime returned an invalid paused thread row.');
            if (owner === pid) threads.push({ id, pid:owner, state:state === 2 ? 'kernel-wait' : 'paused' });
          }
          memoryReply(request, operation, { pid, threads, captured:true, locationAvailable:false });
        } finally { m._free(pointer); }
        return;
      }
      if (operation === 'processes') {
        const count = m._telos_vm_process_count();
        if (!Number.isSafeInteger(count) || count < 0 || count > 4096) throw new Error('The runtime returned an invalid process count.');
        const processes = [];
        const hasNames = typeof m._telos_vm_copy_paused_process_name === 'function';
        const capacity = 1025, pointer = hasNames ? m._malloc(capacity) : 0;
        if (hasNames && (!Number.isSafeInteger(pointer) || pointer <= 0 || pointer + capacity > m.HEAPU8.length)) {
          if (pointer) m._free(pointer);
          throw new Error('The runtime could not allocate a bounded process-name buffer.');
        }
        try { for (let index = 0; index < count; index++) {
          const pid = m._telos_vm_process_id_at(index);
          if (!Number.isSafeInteger(pid) || pid <= 0 || pid > 0xffffffff) throw new Error('The runtime returned an invalid process ID.');
          let name = null;
          if (hasNames) {
            const length = m._telos_vm_copy_paused_process_name(pid, pointer, capacity);
            if (!Number.isSafeInteger(length) || length < 0 || length >= capacity || pointer + length + 1 > m.HEAPU8.length) throw new Error('The runtime refused the paused process-name capture.');
            if (m.HEAPU8[pointer + length] !== 0) throw new Error('The runtime returned an invalid process name.');
            const bytes = m.HEAPU8.slice(pointer, pointer + length);
            // Linux executable names need not be UTF-8. Keep that field
            // unavailable rather than inventing a replacement name.
            try { name = new TextDecoder('utf-8', { fatal:true }).decode(bytes); } catch { name = null; }
            if (name !== null && /[\x00-\x1f\x7f/\\]/.test(name)) name = null;
          }
          processes.push({ pid, name });
        } } finally { if (pointer) m._free(pointer); }
        if (!memoryPaused(m) || new Set(processes.map(process => process.pid)).size !== processes.length) throw new Error('Process capture lost its pause barrier or returned duplicate IDs.');
        let presentationOwner = null;
        if (typeof m._telos_vm_paused_presentation_owner === 'function') {
          const owner = m._telos_vm_paused_presentation_owner();
          if (!Number.isSafeInteger(owner) || owner < 0 || owner >= 0xffffffff
            || (owner !== 0 && !processes.some(process => process.pid === owner))) {
            throw new Error('The runtime refused a consistent paused presentation-owner capture.');
          }
          if (owner !== 0) presentationOwner = owner;
        }
        if (!memoryPaused(m)) throw new Error('Presentation attribution lost its pause barrier.');
        memoryReply(request, operation, { processes:processes.map(process => ({ ...process,
          presentationOwner:presentationOwner === null ? null : process.pid === presentationOwner,
        })) });
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
  function captureGuestGlErrorReplies(m, state) {
    if (state !== 2) { guestGlErrorReplies = null; return null; }
    try {
      const exports = ['version','slots','code','count'].map(name => `_boxedwine_guest_gl_error_reply_${name}`);
      if (!m || exports.some(name => typeof m[name] !== 'function') || !memoryPaused(m)
        || m[exports[0]]() !== 1 || m[exports[1]]() !== 9) return null;
      if (guestGlErrorReplies) return guestGlErrorReplies;
      const active = m._telos_vm_active_threads(), entries = [];
      let queries = 0;
      for (let index = 0; index < 9; index++) {
        const rawCode = m[exports[2]](index), rawCount = m[exports[3]](index);
        const expected = index === 0 ? 0 : index < 8 ? 0x0500 + index - 1 : 0xffffffff;
        if (!Number.isInteger(rawCode) || rawCode < -0x80000000 || rawCode > 0xffffffff
          || (rawCode >>> 0) !== expected) return null;
        const count = typeof rawCount === 'bigint' && rawCount >= 0n && rawCount <= BigInt(Number.MAX_SAFE_INTEGER)
          ? Number(rawCount) : rawCount;
        if (!Number.isSafeInteger(count) || count < 0 || !Number.isSafeInteger(queries + count)) return null;
        queries += count; entries.push(Object.freeze({ code:index === 8 ? null : expected, count }));
      }
      if (!memoryPaused(m) || m._telos_vm_active_threads() !== active) return null;
      guestGlErrorReplies = Object.freeze({ schema:'telos.guest-gl-error-replies/v1', scope:'runtime',
        semantics:'guest-requested-glGetError-replies', entries:Object.freeze(entries) });
      return guestGlErrorReplies;
    } catch { return null; }
  }

  function captureHostGlQueryOutputs(m, state) {
    if (state !== 2) { hostGlQueryOutputs = null; return null; }
    try {
      const exports = ['version','slots','kind','value','count'].map(name => `_boxedwine_host_gl_query_output_${name}`);
      if (!m || exports.some(name => typeof m[name] !== 'function') || !memoryPaused(m)
        || m[exports[0]]() !== 1 || m[exports[1]]() !== 7) return null;
      if (hostGlQueryOutputs) return hostGlQueryOutputs;
      const active = m._telos_vm_active_threads(), entries = [];
      let total = 0;
      for (let index = 0; index < 7; index++) {
        const kind = m[exports[2]](index), rawValue = m[exports[3]](index), rawCount = m[exports[4]](index);
        const expectedKind = index < 3 ? 0 : index < 6 ? 1 : 2;
        const expectedValue = index < 6 && index % 3 < 2 ? index % 3 : 0xffffffff;
        if (kind !== expectedKind || !Number.isInteger(rawValue) || rawValue < -0x80000000
          || rawValue > 0xffffffff || (rawValue >>> 0) !== expectedValue) return null;
        const count = typeof rawCount === 'bigint' && rawCount >= 0n && rawCount <= BigInt(Number.MAX_SAFE_INTEGER)
          ? Number(rawCount) : rawCount;
        if (!Number.isSafeInteger(count) || count < 0 || !Number.isSafeInteger(total + count)) return null;
        total += count;
        entries.push(Object.freeze({ kind:['shader-compile','program-link','unreadable'][kind],
          value:expectedValue === 0xffffffff ? null : expectedValue, count }));
      }
      if (!memoryPaused(m) || m._telos_vm_active_threads() !== active) return null;
      hostGlQueryOutputs = Object.freeze({ schema:'telos.host-gl-query-output-values/v1', scope:'runtime',
        semantics:'existing-host-query-buffer-values', outputWriteVerified:false, objectValidityVerified:false,
        entries:Object.freeze(entries) });
      return hostGlQueryOutputs;
    } catch { return null; }
  }

  function capturePausedGlCounts(m, state) {
    if (state !== 2) { pausedGlCounts = null; return null; }
    try {
      if (!m || ['version','count','get','name'].some(name => typeof m[`_boxedwine_gl_counter_${name}`] !== 'function')) return null;
      const active = m._telos_vm_active_threads();
      if (!active || active !== m._telos_vm_parked_threads() || m._boxedwine_gl_counter_version() !== 1) return null;
      if (pausedGlCounts) return pausedGlCounts;
      const slots = m._boxedwine_gl_counter_count();
      if (!Number.isSafeInteger(slots) || slots < 1 || slots > 4096) return null;
      // These optional SDK counters sample only proxied GL calls. Read at most
      // three values for each of the existing sixteen displayed count leaders.
      // A missing export or invalid metric must not disable the count capture.
      let sampleInterval = null, timingSlots = 0;
      try {
        if (['_boxedwine_sdl_gl_dispatch_metric', '_boxedwine_sdl_gl_dispatch_metric_count',
          '_boxedwine_sdl_gl_dispatch_metric_sample_rate'].every(name => typeof m[name] === 'function')) {
          timingSlots = m._boxedwine_sdl_gl_dispatch_metric_count();
          const interval = m._boxedwine_sdl_gl_dispatch_metric_sample_rate();
          if (Number.isSafeInteger(timingSlots) && timingSlots > 0 && timingSlots <= 4096
            && Number.isSafeInteger(interval) && interval > 0 && interval <= 4096) sampleInterval = interval;
        }
      } catch { /* Timing observation is optional. */ }
      const nonzero = [];
      for (let index = 0; index < slots; index++) {
        const raw = m._boxedwine_gl_counter_get(index);
        const count = typeof raw === 'bigint'
          ? raw >= 0n && raw <= BigInt(Number.MAX_SAFE_INTEGER) ? Number(raw) : null : raw;
        if (!Number.isSafeInteger(count) || count < 0) return null;
        if (count) nonzero.push({ index, count });
      }
      nonzero.sort((a, b) => b.count - a.count || a.index - b.index);
      // Preserve every nonzero count; a top-16-only capture cannot answer
      // whether a less frequent draw/copy/framebuffer entry point was called.
      const entries = nonzero.map(entry => {
        const address = m._boxedwine_gl_counter_name(entry.index);
        if (!Number.isInteger(address)) throw new Error('Invalid GL counter name pointer.');
        const pointer = address >>> 0, heap = m.HEAPU8;
        if (!(heap instanceof Uint8Array) || !pointer || pointer >= heap.length) throw new Error('Unavailable GL counter name.');
        let name = '', terminated = false;
        for (let offset = 0; offset < 128 && pointer + offset < heap.length; offset++) {
          const byte = heap[pointer + offset];
          if (!byte) { terminated = true; break; }
          if (byte < 32 || byte > 126) throw new Error('Invalid GL counter name.');
          name += String.fromCharCode(byte);
        }
        if (!terminated || !name) throw new Error('Unbounded GL counter name.');
        let timing = null;
        try {
          if (sampleInterval !== null && entry.index < timingSlots) {
            const [samples, callerMicros, callbackMicros] = [0, 1, 2]
              .map(metric => m._boxedwine_sdl_gl_dispatch_metric(entry.index, metric));
            if ([samples, callerMicros, callbackMicros].every(value => Number.isSafeInteger(value) && value >= 0)
              && samples <= entry.count) {
              timing = Object.freeze({ samples, callerMicros, callbackMicros });
            }
          }
        } catch { /* Preserve the observed count if this optional reader fails. */ }
        return Object.freeze({ ...entry, name, timing });
      });
      if (m._telos_vm_status() !== 2 || m._telos_vm_active_threads() !== active || m._telos_vm_parked_threads() !== active) return null;
      pausedGlCounts = Object.freeze({ schema:'telos.paused-gl-counts/v2', slots,
        nonzero:nonzero.length, entries:Object.freeze(entries), truncated:nonzero.length > entries.length,
        sampleInterval, timingReadScope:sampleInterval === null ? null : 'all-nonzero-entries' });
      return pausedGlCounts;
    } catch { return null; }
  }
  function captureDispatchProgress(m, state) {
    // Optional published trace data, not registers or a quiescent snapshot.
    // The native fixed buffer avoids allocator waits during a stalled pause.
    if (state !== 1 && state !== 2) return null;
    try {
      if (!m || ['version','buffer'].some(name => typeof m[`_telos_vm_dispatch_progress_${name}`] !== 'function')
        || typeof m._telos_vm_sample_dispatch_progress !== 'function'
        || m._telos_vm_dispatch_progress_version() !== 1) return null;
      const count = m._telos_vm_sample_dispatch_progress();
      const pointer = m._telos_vm_dispatch_progress_buffer();
      const heap = m.HEAPU8;
      if (!Number.isSafeInteger(count) || count < 0 || count > 64
        || !Number.isSafeInteger(pointer) || pointer <= 0 || pointer % 4
        || !(heap instanceof Uint8Array) || pointer + count * 32 > heap.length) return null;
      const view = new DataView(heap.buffer, heap.byteOffset + pointer, count * 32);
      const entries = [];
      for (let index = 0; index < count; index++) {
        const words = Array.from({length:8}, (_, word) => view.getUint32(index * 32 + word * 4, true));
        if (!words[0] || words[7] > 1) return null;
        entries.push({tid:words[0],pid:words[1],entryEip:words[2],reportedEip:words[3],tableIndex:words[4],
          sequence:words[5],ageMs:words[6],kernelWait:words[7] === 1});
      }
      if (new Set(entries.map(entry => entry.tid)).size !== entries.length) return null;
      return {schema:'telos.published-dispatch-progress/v1',semantics:'best-effort-published-trace',entries};
    } catch { return null; }
  }
  function vmStatus(ackRequest = null, error = null, retainError = true) {
    // Status polls must preserve a bounded control failure until a new command.
    if (retainError && typeof error === 'string' && error) vmLastError = error.slice(0, 240);
    const m = vmExports();
    const state = m ? m._telos_vm_status() : 0;
    observeVmState(state);
    const names = ['running','pausing','paused','stepping'];
    let assetIo = null;
    try {
      if (m && typeof m.getOwnedInstallIoStats === 'function') assetIo = m.getOwnedInstallIoStats();
    } catch { /* Optional file telemetry cannot interrupt execution status. */ }
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
    let threadStateCounts = null;
    try {
      if (typeof m?._telos_vm_thread_state_counts === 'function') {
        const packed = m._telos_vm_thread_state_counts();
        // Valid snapshots fit below bit 60. The refusal sentinel may arrive
        // as signed -1n, depending on the i64 JS boundary. Never coerce Numbers.
        if (typeof packed === 'bigint' && packed >= 0n && packed < (1n << 60n)) {
          const values = [0,1,2,3,4].map(index => Number((packed >> BigInt(index * 12)) & 0xfffn));
          const total = values.reduce((sum, value) => sum + value, 0);
          if (total <= 4095) {
            threadStateCounts = {starting:values[0],running:values[1],kernelWait:values[2],paused:values[3],exiting:values[4],total};
          }
        }
      }
    } catch { /* Optional lifecycle metadata cannot disable VM controls. */ }
    parent.postMessage({
      type:'telos-wasm-vm-status', schema:vmSchema, session, ackRequest,
      available:!!m, state:names[state] || 'unknown', unit:vmUnit,
      activeThreads:m && typeof m._telos_vm_active_threads === 'function' ? m._telos_vm_active_threads() : null,
      parkedThreads:m && typeof m._telos_vm_parked_threads === 'function' ? m._telos_vm_parked_threads() : null,
      presents:m && typeof m._telos_vm_present_count_low === 'function' ? m._telos_vm_present_count_low() : null,
      sdlDispatch,
      threadStateCounts,
      dispatchProgress:captureDispatchProgress(m, state),
      assetIo,
      glCounts:capturePausedGlCounts(m, state),
      guestGlErrorReplies:captureGuestGlErrorReplies(m, state),
      hostGlQueryOutputs:captureHostGlQueryOutputs(m, state),
      commandProvenance:vmCommandProvenance(),
      presentMarker:'monotonic BoxedWine GL render-present boundary',
      clockBehavior:'host-derived guest clocks continue while paused',
      error,
      lastControlError:vmLastError,
    }, parentOrigin);
  }
  async function requestPresentStep(m, sequence, deadline, provenance = {}, isCancelled = null, consumptionSequence = null) {
    const command = recordVmCommand(provenance.source || 'unknown', 'step', provenance);
    while (performance.now() < deadline) {
      if ((sequence !== null && lastSequence !== sequence) || isCancelled?.()) {
        markVmCommand(command, 'superseded');
        return null;
      }
      let result;
      try { result = consumptionSequence === null ? m._telos_vm_step_present() : m._telos_vm_step_input_present(consumptionSequence >>> 0); }
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
    if ((sequence !== null && lastSequence !== sequence) || isCancelled?.()) {
      markVmCommand(command, 'superseded');
      return null;
    }
    markVmCommand(command, 'timed-out');
    throw new Error('No runnable guest worker became available to render the next presentation.');
  }
  async function advanceOnePresent(m, provenance = {}, isCancelled = null, deadlineLimit = Infinity) {
    const start = m._telos_vm_present_count_low();
    const sequence = lastSequence;
    const deadline = Math.min(performance.now() + renderDeadlineMs, deadlineLimit);
    // The button promises a single render from either state. Park the guest
    // first; _telos_vm_step_present only admits a step after the VM-wide
    // barrier has acknowledged every live worker.
    if (m._telos_vm_status() !== 2) {
      const pauseCommand = recordVmCommand(provenance.source || 'vm-control', 'pause', provenance);
      try { m._telos_vm_pause(); }
      catch (error) { markVmCommand(pauseCommand, 'failed'); throw error; }
      supersedePriorVmControlIntent(pauseCommand);
      if (!(await waitForPause(m, sequence, pauseCommand, isCancelled, deadline))) {
        if (isCancelled?.()) return;
        if (sequence !== null && lastSequence !== sequence) return;
        throw new Error('The game workers did not reach the pause boundary before the render deadline.');
      }
    }
    const stepCommand = await requestPresentStep(m, sequence, deadline, provenance, isCancelled);
    if (!stepCommand) return;
    try {
      while (performance.now() < deadline) {
        if ((sequence !== null && lastSequence !== sequence) || isCancelled?.()) {
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
      if ((sequence !== null && lastSequence !== sequence) || isCancelled?.()) {
        markVmCommand(stepCommand, 'superseded');
        return;
      }
      markVmCommand(stepCommand, 'timed-out');
      throw new Error('The guest did not reach one GL presentation and return to its pause barrier.');
    } catch (error) {
      if (stepCommand.outcome === 'pending') markVmCommand(stepCommand, 'failed');
      // Close an admitted interval after a failed step. Pause is asynchronous;
      // subsequent status must prove the worker barrier before inspection.
      const recovery = recordVmCommand(provenance.source || 'vm-control', 'pause', provenance);
      try {
        m._telos_vm_pause();
        supersedePriorVmControlIntent(recovery);
        markVmCommand(recovery, 'admitted');
      } catch {
        markVmCommand(recovery, 'failed');
      }
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
        unit:vmUnit, capabilities:{pause:true,resume:true,step:true,frameStep:true,checkpoint:false,restore:false,seek:false,presentMarker:true,memoryRead:memoryAvailable,threadList:memoryAvailable && typeof m._telos_vm_copy_paused_threads === 'function',registers:memoryAvailable && typeof m._telos_vm_copy_paused_registers === 'function',addressMapping:memoryAvailable && typeof m._telos_vm_paused_mapping_abi_version === 'function' && m._telos_vm_paused_mapping_abi_version() === 1 && typeof m._telos_vm_copy_paused_mapping === 'function',inputBackend:inputBackend(m)} }, parentOrigin);
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
        settleMs:Math.max(0, diagnostics.totalMs - diagnostics.pauseMs - diagnostics.alignmentMs - diagnostics.dispatchMs - diagnostics.stepWaitMs),
      }));
    }
  }
  function activeCanvas() {
    const gl = document.getElementById('boxedwine-webgl-canvas-0');
    if (gl && gl.width && gl.height && getComputedStyle(gl).display !== 'none' && getComputedStyle(gl).visibility !== 'hidden') return gl;
    return document.getElementById('canvas');
  }
  const delay = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));
  async function waitForPause(m, sequence, command = null, isCancelled = null, deadlineLimit = Infinity) {
    const deadline = Math.min(performance.now() + 10000, deadlineLimit);
    try {
      while (lastSequence === sequence && !isCancelled?.() && performance.now() < deadline) {
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
    if (command) markVmCommand(command, lastSequence === sequence && !isCancelled?.() ? 'timed-out' : 'superseded');
    return false;
  }
  function dispatchInput(message, m) {
    const supported = supportedKeyTokens(m);
    if (message.keys.some(key => !supported.includes(key))) throw new Error('The compiled runtime does not support a requested score key.');
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
      // The runtime exposes HEAPU8, not necessarily a HEAPU32 module property.
      // Reacquire the byte view after native input can grow the shared heap.
      const heap = m.HEAPU8;
      if (!(heap instanceof Uint8Array) || countPointer + 4 > heap.byteLength) {
        throw new Error('The runtime input result is outside its current heap.');
      }
      const eventsDispatched = new DataView(heap.buffer, heap.byteOffset + countPointer, 4).getUint32(0, true);
      if (status !== 0 && status !== 13 && status !== 14) {
        throw new Error(({1:'Invalid input sequence.',2:'Another input batch is open.',3:'The previous input batch is still queued.',
          4:'The runtime rejected a repeated or stale input sequence.',10:'The runtime rejected invalid input data.',
          11:'The game runtime has no active input target.',12:'The runtime could not close the input batch.',
          15:'The runtime generated no guest event for the requested key or button transition.'})[status] || 'The runtime rejected the input batch.');
      }
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
  function capturePausedInputBookkeeping(m) {
    if (typeof m._telos_vm_copy_paused_input_state !== 'function') return {error:'not exported by this runtime'};
    const pointer = m._malloc(36);
    if (!Number.isSafeInteger(pointer) || pointer <= 0 || pointer + 36 > m.HEAPU8.length) {
      if (Number.isSafeInteger(pointer) && pointer > 0) m._free(pointer);
      return {error:'capture buffer unavailable'};
    }
    try {
      const status = m._telos_vm_copy_paused_input_state(pointer, 9);
      if (status !== 0) return {error:`capture refused (${status})`};
      const heap = m.HEAPU8;
      if (!(heap instanceof Uint8Array) || pointer + 36 > heap.byteLength) return {error:'capture buffer changed'};
      const view = new DataView(heap.buffer, heap.byteOffset + pointer, 36);
      const words = Array.from({ length:9 }, (_, index) => view.getUint32(index * 4, true));
      if (words[1] > 64 || words[2] > words[1] || words[3] > 1 || words[8] > 64) return {error:'invalid captured counters'};
      return {words};
    } catch { return {error:'capture unavailable'}; }
    finally { m._free(pointer); }
  }
  function pausedInputBookkeeping(m) {
    const capture = capturePausedInputBookkeeping(m);
    if (capture.error) return capture.error;
    const words = capture.words;
    return `active sequence ${words[0]}, generated ${words[1]}, consumed ${words[2]}, open ${words[3]}, last started ${words[4]}, last completed ${words[5]}, completed events ${words[6]}, completed present ${words[7]}, tracked queued ${words[8]}`;
  }
  async function applyInputAtPresent(message, job) {
    const isCancelled = () => job.cancelled;
    if (job.cancelled || lastSequence !== message.sequence) return;
    const m = vmExports();
    if (!m) throw new Error('The game runtime is not ready for TAS input.');
    const startedAt = performance.now();
    if (startedAt >= job.deadline) throw new Error('The queued score input expired before dispatch. No input was sent.');
    let pauseMs = 0, alignmentMs = 0, dispatchMs = 0, stepWaitMs = 0, polls = 0;
    let stepCommand = null;
    let lastInputObservation = null;

    // Admit input only under a complete worker pause. A later native event
    // consumption and bounded presentation are separate receipts; neither
    // identifies a Win32 sampling point or a simulation frame.
    const pauseCommand = recordVmCommand('score-input', 'pause', { sequence:message.sequence });
    try { m._telos_vm_pause(); }
    catch (error) { markVmCommand(pauseCommand, 'failed'); throw error; }
    supersedePriorVmControlIntent(pauseCommand);
    const pauseStartedAt = performance.now();
    if (!(await waitForPause(m, message.sequence, pauseCommand, isCancelled, job.deadline))) {
      if (job.cancelled) return;
      if (lastSequence === message.sequence) throw new Error('The game workers did not reach the pause boundary before the input deadline.');
      return;
    }
    pauseMs = performance.now() - pauseStartedAt;
    if (lastSequence !== message.sequence) return;

    // Idle menus may not present until an input arrives. Requiring a render
    // before dispatch would prevent that input from ever reaching the menu.
    // A full pause is input admission, not an inferred engine-frame boundary.
    const captured = capturePausedInputBookkeeping(m);
    if (captured.error) throw new Error('Input admission requires paused event bookkeeping: ' + captured.error);
    if (captured.words[3] || captured.words[1] > captured.words[2] || captured.words[8]) {
      throw new Error('A previous input batch is still queued. No new input was sent. Paused X-event bookkeeping: ' + pausedInputBookkeeping(m) + '.');
    }

    // Capture one admission/dispatch origin after native bookkeeping. A native
    // allocator or host callback can finish an earlier presentation between
    // separate reads; those origins must not silently describe different spans.
    const beforeAlignment = m._telos_vm_present_count_low();
    const alignment = {schema:'telos.input-quiescent-alignment/v1', beforeInput:true,
      fromPresent:beforeAlignment, toPresent:beforeAlignment, presentsAdvanced:0};
    try {
      if (performance.now() >= job.deadline) throw new Error('The score input deadline expired before dispatch. No input was sent.');
      const startPresent = beforeAlignment;
      const dispatchStartedAt = performance.now();
      const { eventsDispatched, requiresConsumption } = dispatchInput(message, m);
      const consumptionGate = consumptionPresent(m);
      dispatchMs = performance.now() - dispatchStartedAt;
      // A consumed transition can enter a synchronous game load before the
      // next image. Bound elapsed time without accepting an extra render or
      // substituting elapsed time for guest event consumption.
      const deadline = Math.min(performance.now() + renderDeadlineMs, job.deadline);
      while (lastSequence === message.sequence && !job.cancelled && performance.now() < deadline) {
      const stepStartedAt = performance.now();
      stepCommand = await requestPresentStep(m, message.sequence, deadline, { source:'score-input', sequence:message.sequence }, isCancelled, consumptionGate && requiresConsumption ? message.sequence : null);
      if (!stepCommand) return;
      while (lastSequence === message.sequence && !job.cancelled && performance.now() < deadline) {
        polls++;
        const observedPresent = m._telos_vm_present_count_low();
        const paused = m._telos_vm_status() === 2
          && m._telos_vm_active_threads() > 0
          && m._telos_vm_parked_threads() === m._telos_vm_active_threads();
        // A render can arrive between the first count read and the pause read.
        // Use the barrier's count before comparing its consumption marker.
        const present = paused ? m._telos_vm_present_count_low() : observedPresent;
        const presentDelta = (present - startPresent) >>> 0;
        if ((!consumptionGate && presentDelta > 1 && presentDelta < 0x80000000) || (consumptionGate && presentDelta > 4096)) {
          markVmCommand(stepCommand, 'failed');
          throw new Error(consumptionGate ? 'The input exceeded the bounded presentation budget before completion.' : 'The runtime rendered more than one presentation during a score step. Playback stopped.');
        }
        const consumedSequence = m._telos_input_last_sequence();
        const inputConsumed = consumedSequence === (message.sequence >>> 0);
        const inputPresentOffset = inputConsumed ? (m._telos_input_last_present_low() - startPresent) >>> 0 : null;
        const afterConsumption = inputConsumed ? presentDelta - inputPresentOffset : null;
        if (consumptionGate && requiresConsumption && inputConsumed && paused &&
          (inputPresentOffset > presentDelta || afterConsumption > 1)) {
          markVmCommand(stepCommand, 'failed');
          throw new Error('The runtime did not stop at the first presentation after native event consumption.');
        }
        lastInputObservation = { presentDelta, paused, consumedSequence, requiresConsumption, eventsDispatched };
        if (performance.now() >= deadline || lastSequence !== message.sequence || job.cancelled) break;
        if (paused && (!requiresConsumption || inputConsumed)
          && (consumptionGate && requiresConsumption ? afterConsumption === 1 : presentDelta === 1)) {
          markVmCommand(stepCommand, 'completed');
          stepWaitMs += performance.now() - stepStartedAt;
          const diagnostics = { totalMs:performance.now()-startedAt, pauseMs, alignmentMs, dispatchMs, stepWaitMs, polls,
            completion:{schema:'telos.input-completion-diagnostic/v1',tick:message.tick,startPresent,present,
              totalPresents:presentDelta,inputPresentOffset,requiresConsumption,
              afterConsumption:requiresConsumption ? afterConsumption : null,
              requestedInput:{source:'bridge-request',keys:[...message.keys],buttons:[...message.buttons],
                mouse:message.mouse ? {x:message.mouse.x,y:message.mouse.y} : null}} };
          reportInputTiming(message.sequence, diagnostics);

          parent.postMessage({
            type:'telos-wasm-tas-ack', session, sequence:message.sequence, tick:message.tick,
            state:'input-applied', eventsDispatched, eventsConsumed:inputConsumed ? m._telos_input_last_event_count() : 0, present,
            presentsAdvanced:presentDelta, inputPresentOffset,
            presentationReceipt:consumptionGate ? {schema:'telos.input-consumption-present/v1', total:presentDelta,
              requiresConsumption, beforeConsumption:requiresConsumption ? inputPresentOffset : null,
              afterConsumption:requiresConsumption ? afterConsumption : null} : undefined,
            inputBoundary:inputConsumed ? 'guest-consumed-events' : eventsDispatched ? 'native-mouse-position' : 'unchanged-held-state', frameBoundary:consumptionGate ? 'bounded-boxedwine-gl-present-with-consumption-gate' : 'bounded-boxedwine-gl-present',
            alignment,
            diagnostics,
          }, parentOrigin);
          return;
        }
        await delay(2);
      }
    }
      if (stepCommand?.outcome === 'pending') markVmCommand(stepCommand, lastSequence === message.sequence && !job.cancelled ? 'timed-out' : 'superseded');
      if (lastSequence === message.sequence && !job.cancelled) {
        const observed = lastInputObservation;
        const detail = observed ? ` Last observation: sequence ${message.sequence}, presentations advanced ${observed.presentDelta}, paused boundary ${observed.paused}, last consumed sequence ${observed.consumedSequence}, consumption required ${observed.requiresConsumption}, dispatched transitions ${observed.eventsDispatched}.`
          : ' No input-completion observation was obtained.';
        throw new Error('The game did not complete the rendered input step before the deadline. Playback stopped at the current tick.' + detail
          + ` Paused X-event bookkeeping: ${pausedInputBookkeeping(m)}.`);
      }
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
        return status === 0;
      } catch (error) { console.error('TAS input release failed:', error); }
      return false;
    }
    if (inputBackend(m) === 'legacy') {
      let released = true;
      for (const token of legacyHeldKeys) {
        try { legacyKey(m, token, false); } catch (error) { released = false; console.error('TAS key release failed:', error); }
      }
      legacyHeldKeys.clear();
      const point = legacyMousePoint;
      for (const button of legacyHeldButtons) {
        try { legacyMouseButton(m, button, false, point); } catch (error) { released = false; console.error('TAS mouse release failed:', error); }
      }
      legacyHeldButtons.clear();
      return released;
    }
    return false;
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
        event.data?.type === 'telos-wasm-guest-view' && event.data?.schema === 'telos-wasm-guest-view/v1' &&
        event.data?.action === 'show-window') {
      const cover = document.getElementById('game-startup');
      if (cover) cover.hidden = true;
      return;
    }
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
      if (message.action !== 'status') vmLastIntentRequest = request;
      if (message.action === 'pause' && activeScoreInput && !activeScoreInput.cancelled) {
        const job = activeScoreInput;
        const m = vmExports();
        if (m) {
          const command = recordVmCommand('vm-control', 'pause', {request});
          try {
            m._telos_vm_pause();
            job.cancelled = true;
            supersedePriorVmControlIntent(command);
            parent.postMessage({type:'telos-wasm-tas-ack', session, sequence:job.sequence,
              tick:job.tick, state:'error', error:'The pending score input was cancelled by Pause. Input may already have reached the guest; no completed score frame is claimed.'}, parentOrigin);
            vmStatus(request);
          } catch (error) {
            markVmCommand(command, 'failed');
            vmStatus(request, String(error?.message || error));
          }
          return;
        }
      }
      inputWork = inputWork.then(async () => {
        // Status reads do not replace control intent. A newer Pause can cancel
        // the score job immediately while older controls remain in this queue.
        if (message.action !== 'status' && request !== vmLastIntentRequest) {
          vmStatus(request, 'This execution request was superseded before dispatch.', false);
          return;
        }
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
        let releaseBoundary = 'input-dispatch';
        if (m && await waitForPause(m, message.sequence)) {
          const sequence = message.sequence >>> 0;
          const beginStatus = m._telos_input_begin(sequence);
          if (beginStatus !== 0) throw new Error(`Input release was not admitted (status ${beginStatus}). Native consumption is unverified.`);
          let released = false;
          let endStatus;
          try { released = releaseAll(); }
          finally { endStatus = m._telos_input_end(sequence); }
          if (!released || ![0, 2, 3].includes(endStatus)) throw new Error(`Input release could not be dispatched (end status ${endStatus}).`);
          releaseBoundary = endStatus === 2 ? 'queued-native-events' : endStatus === 3 ? 'no-held-input-events' : 'consumed-native-events';
        } else {
          if (!releaseAll()) throw new Error('Input release has no confirmed input target.');
        }
        // A queued release is dispatch evidence only; it does not qualify a
        // neutral gameplay tick or consumption before the next presentation.
        parent.postMessage({ type:'telos-wasm-tas-ack', session, sequence:message.sequence, tick:message.tick, state:'released', releaseBoundary }, parentOrigin);
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
    // Queueing and every phase share this receive-side budget.
    const deadline = performance.now() + inputDeadlineMs;
    inputWork = inputWork.then(async () => {
      const job = {sequence:message.sequence, tick:message.tick, deadline, cancelled:false};
      activeScoreInput = job;
      try { await applyInputAtPresent(message, job); }
      finally { if (activeScoreInput === job) activeScoreInput = null; }
    }).catch(error => {
      if (lastSequence === message.sequence) {
        const failure = String(error?.message || error);
        // A failed post-input render may still own an admitted native step.
        // Stop that interval before the parent queues its held-input release.
        const recovery = recordVmCommand('score-input', 'pause', {sequence:message.sequence});
        try {
          m._telos_vm_pause();
          supersedePriorVmControlIntent(recovery);
          markVmCommand(recovery, 'admitted');
        } catch { markVmCommand(recovery, 'failed'); }
        parent.postMessage({ type:'telos-wasm-tas-ack', session, sequence:message.sequence, tick:message.tick, state:'error', error:failure }, parentOrigin);
        // Publish the current failure through the same session owner as VM
        // controls. Later polls retain its bounded display notice.
        vmStatus(null, failure);
      }
    });
  });
  // The owning workspace handles top-level focus/visibility loss. Moving
  // between its editor and this iframe must not mutate scored held state.
  addEventListener('blur', () => { if (parent === window) releaseAll(); });
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
