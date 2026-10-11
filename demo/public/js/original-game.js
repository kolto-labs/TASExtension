import { DEFAULT_GAME_PROFILE, gameProfileById, identifyGameProfile } from './game-profiles.js';
import { GameStartup } from './game-startup.js';

// Supported game profiles share this runtime path; assets stay in the selected install.
const DIRECTORIES = new Set(['data', 'texturepacks', 'modules', 'rims', 'lips', 'movies', 'streammusic', 'streamsounds', 'streamwaves', 'sounds', 'music', 'override', 'miles']);
const OMIT = new Set(['dinput8.dll', 'winmm.dll', 'dsound.dll']);
const TOP = /\.(exe|dll|ini|key|tlk|erf|bif)$/i;
const FOLDER_DB = 'telos-local-game-folders-v1';
const FOLDER_STORE = 'handles';
const FOLDER_KEY = 'k1-installation';
const LOCAL_AUTO_INSTALL = ['127.0.0.1', 'localhost', '[::1]'].includes(location.hostname);
// Keep a loopback-only escape hatch for browser automation and Chromium
// sessions where the File System Access picker cannot be presented. The
// directory input preserves relative paths and uses the same staging flow.
const compatibleFolderPickerRequested = LOCAL_AUTO_INSTALL
  && new URLSearchParams(location.search).get('folderPicker') === 'compat';
const textureTraceRequested = LOCAL_AUTO_INSTALL
  && new URLSearchParams(location.search).get('textureTrace') === '1';
const guestTraceRequested = LOCAL_AUTO_INSTALL
  && new URLSearchParams(location.search).get('guestTrace') === '1';
const ioTraceRequested = LOCAL_AUTO_INSTALL
  && new URLSearchParams(location.search).get('ioTrace') === '1';
// BoxedWine documents this workaround for older multi-threaded games. Keep it
// an explicit loopback-only launch experiment until KOTOR acceptance confirms it.
const cpuAffinityRequested = LOCAL_AUTO_INSTALL
  && new URLSearchParams(location.search).get('cpuAffinity') === '1';
// A loopback-only storage override lets us isolate a broken persisted Wine
// prefix without deleting the user's existing IndexedDB profile. The default
// remains persistent storage; MEMORY is a disposable startup diagnostic.
function runtimeStorageMode() {
  if (!LOCAL_AUTO_INSTALL) return 'indexed_db';
  const requested = new URLSearchParams(location.search).get('runtimeStorage')?.trim().toLowerCase();
  return requested === 'memory' ? 'memory' : 'indexed_db';
}
const TAS_KEYS = new Set(['w','a','s','d','q','e','space','tab','esc','enter','up','down','left','right','1','2','3','4','5','6','7','8','9']);
const MAX_REPORTED_THREADS = 1_000_000;
function validVmCounter(value, maximum = Number.MAX_SAFE_INTEGER) {
  return Number.isSafeInteger(value) && value >= 0 && value <= maximum;
}
function normalizeVmCommandProvenance(value) {
  if (!value || value.schema !== 'telos.vm-command-provenance/v1'
      || !validVmCounter(value.observedAtMs) || !Array.isArray(value.recentCommands)
      || value.recentCommands.length > 8 || !Array.isArray(value.recentTransitions)
      || value.recentTransitions.length > 8) return null;
  const normalizeCommand = (command, transition = false) => {
    if (!command || !validVmCounter(command.id) || command.id === 0 || !validVmCounter(command.atMs)
        || !['vm-control','score-input'].includes(command.source)
        || !['pause','resume','step'].includes(command.action)
        || !['dispatched','admitted','completed','failed','timed-out','superseded'].includes(command.phase)
        || !['pending','completed','failed','timed-out','superseded'].includes(command.outcome)
        || (command.admittedAtMs !== null && !validVmCounter(command.admittedAtMs))
        || (command.completedAtMs !== null && !validVmCounter(command.completedAtMs))
        || ((command.phase === 'completed' || ['failed','timed-out','superseded'].includes(command.phase)) !== (command.completedAtMs !== null))
        || ((command.phase === 'completed' || command.phase === 'admitted') && command.admittedAtMs === null)
        || (command.phase === 'completed' ? command.outcome !== 'completed' :
          ['failed','timed-out','superseded'].includes(command.phase) ? command.outcome !== command.phase : command.outcome !== 'pending')
        || (command.request !== null && !validVmCounter(command.request))
        || (command.sequence !== null && !validVmCounter(command.sequence))
        || (transition && !validVmCounter(command.ageMs))) return null;
    return Object.freeze({ id:command.id, atMs:command.atMs, source:command.source, action:command.action,
      phase:command.phase, outcome:command.outcome, admittedAtMs:command.admittedAtMs,
      completedAtMs:command.completedAtMs, request:command.request, sequence:command.sequence,
      ...(transition ? { ageMs:command.ageMs } : {}) });
  };
  const lastCommand = value.lastCommand === null ? null : normalizeCommand(value.lastCommand);
  const recentCommands = value.recentCommands.map(command => normalizeCommand(command));
  const recentTransitions = value.recentTransitions.map(transition => {
    if (!transition || !validVmCounter(transition.atMs)
        || !['running','pausing','paused','stepping','unknown'].includes(transition.from)
        || !['running','pausing','paused','stepping','unknown'].includes(transition.to)) return null;
    const last = transition.lastCommand === null ? null : normalizeCommand(transition.lastCommand, true);
    return transition.lastCommand === null || last
      ? { atMs:transition.atMs, from:transition.from, to:transition.to, lastCommand:last }
      : null;
  });
  if ((value.lastCommand !== null && !lastCommand) || recentCommands.some(command => !command)
      || recentTransitions.some(transition => !transition)) return null;
  return Object.freeze({ schema:value.schema, observedAtMs:value.observedAtMs, lastCommand,
    recentCommands:Object.freeze(recentCommands),
    recentTransitions:Object.freeze(recentTransitions.map(transition => Object.freeze(transition))) });
}
function normalizeVmStatus(value) {
  if (value?.available !== true || value.unit !== 'render-present'
    || !['running','pausing','paused','stepping'].includes(value.state)) return null;
  const hasActiveCount = value.activeThreads !== undefined && value.activeThreads !== null;
  const hasParkedCount = value.parkedThreads !== undefined && value.parkedThreads !== null;
  if (hasActiveCount !== hasParkedCount) return null;
  const hasThreadCounts = hasActiveCount && hasParkedCount;
  const validThreadCounts = hasThreadCounts
    && validVmCounter(value.activeThreads, MAX_REPORTED_THREADS)
    && validVmCounter(value.parkedThreads, MAX_REPORTED_THREADS)
    && value.parkedThreads <= value.activeThreads;
  const validPresents = validVmCounter(value.presents, 0xffffffff);
  const rawDispatch = value.sdlDispatch;
  const validDispatch = rawDispatch && typeof rawDispatch === 'object' && !Array.isArray(rawDispatch)
    && validVmCounter(rawDispatch.calls)
    && validVmCounter(rawDispatch.callerMicros)
    && validVmCounter(rawDispatch.callbackMicros);
  if (hasThreadCounts && !validThreadCounts) return null;
  if (value.presents !== null && !validPresents) return null;
  return Object.freeze({
    state:value.state,
    unit:value.unit,
    activeThreads:validThreadCounts ? value.activeThreads : null,
    parkedThreads:validThreadCounts ? value.parkedThreads : null,
    presents:validPresents ? value.presents : null,
    sdlDispatch:validDispatch ? Object.freeze({
      calls:rawDispatch.calls,
      callerMicros:rawDispatch.callerMicros,
      callbackMicros:rawDispatch.callbackMicros,
    }) : null,
    commandProvenance:normalizeVmCommandProvenance(value.commandProvenance),
    error:typeof value.error === 'string' ? value.error.slice(0, 240) : null,
  });
}
function normalizeTasInput(detail) {
  if (!detail || !Number.isSafeInteger(detail.tick) || detail.tick < 1) return null;
  const keys = Array.isArray(detail.keys) ? [...new Set(detail.keys)] : null;
  const buttons = Array.isArray(detail.buttons) ? [...new Set(detail.buttons)] : null;
  if (!keys || keys.length > 32 || keys.some(key => !TAS_KEYS.has(key)) || !buttons || buttons.length > 2 || buttons.some(button => !['lmb','rmb'].includes(button))) return null;
  const mouse = detail.mouse === null ? null : detail.mouse;
  if (mouse !== null && (!mouse || !Number.isSafeInteger(mouse.x) || !Number.isSafeInteger(mouse.y) || mouse.x < 0 || mouse.x > 9999 || mouse.y < 0 || mouse.y > 9999)) return null;
  const hz = Number.isFinite(detail.hz) ? Math.max(10, Math.min(240, detail.hz)) : 60;
  return { tick:detail.tick, hz, keys, buttons, mouse };
}
function assetName(name) {
  const parts = name.replaceAll('\\', '/').split('/');
  if (parts.some(part => !part || part === '.' || part === '..' || /[\x00-\x1f]/.test(part))) throw new Error('Invalid installation path.');
  return parts.length > 1 ? DIRECTORIES.has(parts[0].toLowerCase()) : !OMIT.has(name.toLowerCase()) && (TOP.test(name) || ['swkotor', 'swkotor2'].includes(name.toLowerCase()) || name.toLowerCase() === 'startup.txt');
}
function canonicalSelectedPath(name) {
  const path = name.replaceAll('\\', '/');
  if (path.includes('/')) return path;
  // A multi-file picker exposes only File.name, so recover KOTOR's canonical
  // locations for these unambiguous startup resource names. Folder selection
  // retains its relative paths and continues to take precedence above.
  if (/\.(rim|mod)$/i.test(path)) return `modules/${path}`;
  if (/\.bik$/i.test(path)) return `movies/${path}`;
  if (/^swpc_tex_[^/]+\.erf$/i.test(path)) return `TexturePacks/${path}`;
  if (/\.(m3d|flt|asi)$/i.test(path)) return `miles/${path}`;
  return path;
}
async function inspect(files, { requireExecutable = true } = {}) {
  if (files.length > 100000) throw new Error('Too many installation files.');
  const names = new Map();
  for (const entry of files) {
    if (!assetName(entry.name)) continue;
    const key = entry.name.toLowerCase();
    if (names.has(key)) throw new Error(`Conflicting installation paths: ${entry.name}`);
    names.set(key, entry);
  }
  const identified = await identifyGameProfile([...names.values()]);
  const profile = identified.profile || DEFAULT_GAME_PROFILE;
  const importedLibraries = identified.importedLibraries || [];
  const required = [
    ...(requireExecutable ? [profile.executable] : []),
    ...profile.requiredFiles.map(file => file.name),
    ...importedLibraries,
  ];
  const missing = required.filter(name => !names.has(name));
  if (!names.has('chitin.key')) return { files: [...names.values()].sort((a, b) => a.name.localeCompare(b.name, 'en')), missing, keyError: null, missingBifs: [], ready: false, profile, profileError: identified.error, importedLibraries };
  const keyFile = names.get('chitin.key').file;
  if (keyFile.size > 16 * 1024 * 1024) return { files: [...names.values()], missing, keyError: 'Unexpected chitin.key size.', missingBifs: [], ready: false, profile, profileError: identified.error, importedLibraries };
  const bytes = await keyFile.arrayBuffer();
  const data = new DataView(bytes);
  const decode = new TextDecoder('windows-1252');
  if (bytes.byteLength < 64 || decode.decode(bytes.slice(0, 8)) !== 'KEY V1  ') return { files: [...names.values()], missing, keyError: 'Unsupported resource KEY format.', missingBifs: [], ready: false, profile, profileError: identified.error, importedLibraries };
  const count = data.getUint32(8, true), table = data.getUint32(16, true);
  if (count > 65536 || table + count * 12 > bytes.byteLength) return { files: [...names.values()], missing, keyError: 'Invalid resource KEY table.', missingBifs: [], ready: false, profile, profileError: identified.error, importedLibraries };
  const missingBifs = [];
  for (let i = 0; i < count; i++) {
    const row = table + i * 12, start = data.getUint32(row + 4, true), length = data.getUint16(row + 8, true);
    if (start + length > bytes.byteLength) return { files: [...names.values()], missing, keyError: 'Invalid BIF resource filename.', missingBifs: [], ready: false, profile, profileError: identified.error, importedLibraries };
    const name = decode.decode(bytes.slice(start, start + length)).split('\0')[0].replaceAll('\\', '/').toLowerCase();
    if (!names.has(name)) {
      const basename = name.split('/').pop();
      if (names.has(basename)) { const entry = names.get(basename); names.delete(basename); entry.name = name; names.set(name, entry); }
      else missingBifs.push(name);
    }
  }
  const ready = missing.length === 0 && missingBifs.length === 0 && !identified.error;
  return { files: [...names.values()].sort((a, b) => a.name.localeCompare(b.name, 'en')), missing, keyError: null, missingBifs, ready, profile, profileError: identified.error, importedLibraries };
}
function openFolderDb() {
  return new Promise((resolve, reject) => {
    if (!globalThis.indexedDB) return reject(new Error('This browser cannot save a folder choice. The connected files remain available until this tab closes.'));
    const request = indexedDB.open(FOLDER_DB, 1);
    request.onupgradeneeded = () => { if (!request.result.objectStoreNames.contains(FOLDER_STORE)) request.result.createObjectStore(FOLDER_STORE); };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error || new Error('Could not open saved folder access.'));
  });
}
async function folderHandle(action, handle) {
  const db = await openFolderDb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(FOLDER_STORE, 'readwrite');
    const store = tx.objectStore(FOLDER_STORE);
    let request;
    if (action === 'get') request = store.get(FOLDER_KEY);
    else if (action === 'delete') request = store.delete(FOLDER_KEY);
    else request = store.put(handle, FOLDER_KEY);
    let result;
    request.onsuccess = () => { result = request.result; };
    request.onerror = () => reject(request.error || new Error('Could not update saved folder access.'));
    tx.oncomplete = () => { db.close(); resolve(result); };
    tx.onerror = () => { db.close(); reject(tx.error || new Error('Could not update saved folder access.')); };
    tx.onabort = () => { db.close(); reject(tx.error || new Error('Could not update saved folder access.')); };
  });
}
async function directoryFiles(directory) {
  const entries = [];
  async function visit(handle, prefix = '') {
    for await (const [name, child] of handle.entries()) {
      const path = prefix ? `${prefix}/${name}` : name;
      if (child.kind === 'directory') {
        if (prefix || DIRECTORIES.has(name.toLowerCase())) await visit(child, path);
      } else if (assetName(path)) entries.push({ name: path, handle: child });
      if (entries.length > 100000) throw new Error('Too many installation files.');
    }
  }
  await visit(directory);
  // File snapshots retain disk-backed data; only chitin.key is read here.
  // A bounded pool avoids thousands of serial batches and thousands of
  // temporary sliced batch arrays while limiting outstanding file snapshots.
  let next = 0, failure = null;
  const workers = Array.from({ length: Math.min(32, entries.length) }, async () => {
    while (!failure) {
      const index = next++;
      if (index >= entries.length) return;
      const entry = entries[index];
      try { entry.file = await entry.handle.getFile(); }
      catch (error) { failure = error; }
    }
  });
  await Promise.all(workers);
  if (failure) throw failure;
  return entries;
}
function message(worker, value) {
  return new Promise((resolve, reject) => {
    const channel = new MessageChannel();
    const cleanup = () => { clearTimeout(timeout); channel.port1.close(); channel.port2.close(); };
    const timeout = setTimeout(() => { cleanup(); reject(new Error('Local file access did not respond.')); }, 30000);
    channel.port1.onmessage = event => { cleanup(); event.data?.ok ? resolve(event.data) : reject(new Error(event.data?.error || 'Local file access failed.')); };
    try { worker.postMessage(value, [channel.port2]); }
    catch (error) { cleanup(); reject(error); }
  });
}
function waitForOwnedWorker(registration, scriptUrl) {
  const isCurrent = worker => worker?.scriptURL === scriptUrl && worker.state === 'activated';
  if (isCurrent(registration.active)) return Promise.resolve(registration.active);
  return new Promise((resolve, reject) => {
    let settled = false;
    const timeout = setTimeout(() => finish(new Error('The updated game-file service worker did not activate. Refresh the page and reconnect the game files.')), 15000);
    const poll = setInterval(inspect, 100);
    const watched = new WeakSet();
    const finish = (error, worker) => {
      if (settled) return;
      settled = true; clearTimeout(timeout); clearInterval(poll);
      registration.removeEventListener('updatefound', inspect);
      if (error) reject(error); else resolve(worker);
    };
    const watch = worker => {
      if (!worker || watched.has(worker)) return;
      watched.add(worker);
      const changed = () => {
        if (isCurrent(worker) && isCurrent(registration.active)) finish(null, registration.active);
        else if (worker.state === 'redundant') inspect();
      };
      worker.addEventListener('statechange', changed);
      changed();
    };
    function inspect() {
      if (isCurrent(registration.active)) return finish(null, registration.active);
      const candidate = registration.installing || registration.waiting;
      if (candidate) watch(candidate);
    }
    registration.addEventListener('updatefound', inspect);
    inspect();
  });
}
async function selectedRuntime() {
  // Only loopback may select a newly staged, unqualified runtime for actual
  // qualification. A published host requires the separately approved release.
  const local = ['127.0.0.1', 'localhost', '[::1]'].includes(location.hostname);
  const publicTransport = new URLSearchParams(location.search).get('originalRootTransport') === 'public';
  const controller = new AbortController();
  const descriptorPath = `/original-game/runtime-${local && !publicTransport ? 'local' : 'qualified'}.json`;
  // Loopback may be served from a cold local checkout. Give that small
  // descriptor a finite but realistic deadline; retain the shorter
  // public-host deadline and the existing release qualification policy.
  const timeoutMs = local ? 30000 : 8000;
  let descriptorPhase = 'response headers';
  let timedOutPhase = null;
  const timeout = setTimeout(() => {
    timedOutPhase = descriptorPhase;
    controller.abort();
  }, timeoutMs);
  try {
    const response = await fetch(descriptorPath, { cache:'no-store', credentials:'same-origin', signal:controller.signal });
    if (!response.ok) throw new Error('The KOTOR player is unavailable here. Refresh the page and try again.');
    descriptorPhase = 'response body';
    let runtime;
    try { runtime = await response.json(); }
    catch (error) {
      if (error instanceof SyntaxError) throw new Error(`KOTOR_RUNTIME_DESCRIPTOR_INVALID_JSON: ${descriptorPath} returned invalid JSON; inspect the served descriptor and its response body.`);
      throw error;
    }
    if (runtime.schema !== 'telos.original-runtime/v1' || !/^[0-9a-f]{64}$/.test(runtime.release || '') || runtime.entry !== `/original-game/runtime-releases/${runtime.release}/game-runtime.html` || typeof runtime.qualified !== 'boolean') throw new Error('KOTOR player setup failed. Refresh the page and try again.');
    if ((!local || publicTransport) && !runtime.qualified) throw new Error('KOTOR is not available at this address yet.');
    const root = runtime.root;
    const reviewedRoots = { '2d07dd116b32abfff40f8cb2fad79544f90c88b33f59aff23ea8c0e15aec2277':99556813, 'a14446b27adbaccd4fac25dfb4e951da2dd4978fecfbf6e16aa718871fe4b0c5':99450694, '472c4d9475dde72b2057c72f39e43aba3947561e6ff9e7ee0e29f97acf2f3270':99450694 };
    if (!root || reviewedRoots[root.sha256] !== root.bytes || root.base !== `/original-game/wine-roots/${root.sha256}/` || root.localFile !== '.local-root.zip' || root.publicFile !== 'wine-root.zip') throw new Error('KOTOR support files do not match this version. Refresh the page and try again.');
    // A loopback Workers preview can qualify the accepted release's public
    // transport inside this prototype before deployment.
    return { ...runtime, rootFile:local && !publicTransport ? root.localFile : root.publicFile };
  } catch (error) {
    if (error.name === 'AbortError') {
      const phase = timedOutPhase || descriptorPhase;
      throw new Error(`KOTOR_RUNTIME_DESCRIPTOR_TIMEOUT: timed out after ${timeoutMs / 1000}s waiting for ${phase} from ${descriptorPath}; check the local server and storage I/O, then retry.`);
    }
    throw error;
  } finally { clearTimeout(timeout); }
}
export class OriginalGame {
  constructor(root) {
    this.root = root; this.session = null; this.generation = 0; this.closed = false; this.localManifest = null;
    this.lifecycle = Promise.resolve(); this.folderPersistence = Promise.resolve(); this.candidate = null;
    this.activeSession = null; this.pendingRevocations = new Map(); this.disposing = null;
    this.runtimeProfileReady = false;
    this.runtimeGuestId = null; this.runtimeRelease = null; this.runtimeVmCapabilities = null; this.runtimeTasAvailable = false; this.runtimeGuestMemoryAvailable = false; this.runtimeVmStatus = 'idle'; this.runtimeVmStatusRequest = 0;
    this.guestMemorySequence = 0; this.guestMemoryRequests = new Map();
    this.allowStopWithoutSaving = false; this.tasTargetActive = false; this.tasBridgeReady = false;
    this.tasSequence = 0; this.tasLastAck = 0; this.tasPending = null; this.tasWaitingInput = null; this.tasStatus = null;
    this.tasVmReady = false; this.tasVmSequence = 0; this.tasVmStatus = null; this.tasVmButtons = [];
    this.runtimeStoppedSession = null;
    this.receiveTasTarget = event => {
      this.tasTargetActive = event.detail?.active === true;
      if (!this.tasTargetActive) this.releaseTasInputs();
    };
    this.receiveTasInput = event => {
      if (!this.tasTargetActive || !this.session || this.runtimeStoppedSession === this.session) return;
      const input = normalizeTasInput(event.detail);
      if (!input) return;
      if (!this.tasBridgeReady) {
        if (this.tasWaitingInput) {
          this.failTasTransport(`Score tick ${input.tick} arrived while tick ${this.tasWaitingInput.tick} was waiting for the first KOTOR frame.`);
          return;
        }
        this.tasWaitingInput = input;
        dispatchEvent(new CustomEvent('telos:wasm-pending', { detail:{ pending:true, tick:input.tick } }));
        return;
      }
      if (this.tasPending) {
        this.failTasTransport(`Score tick ${input.tick} arrived before runtime acknowledgement for tick ${this.tasPending.tick}. Playback stopped to avoid skipping input.`);
        return;
      }
      const frame = this.host?.querySelector('iframe');
      if (!frame?.contentWindow) {
        this.failTasTransport(`The game frame is unavailable at score tick ${input.tick}.`);
        return;
      }
      const sequence = ++this.tasSequence;
      const sentAt = performance.now();
      const pending = { sequence, tick:input.tick, hz:input.hz, sentAt, timer:0 };
      this.tasPending = pending;
      dispatchEvent(new CustomEvent('telos:wasm-pending', { detail:{ pending:true, tick:input.tick } }));
      pending.timer = setTimeout(() => {
        if (this.tasPending !== pending) return;
        this.failTasTransport(`Game frame ${pending.tick} did not finish before the input deadline. Playback stopped without skipping ahead.`);
      }, 10000);
      try { frame.contentWindow.postMessage({ type:'telos-wasm-tas', session:this.session, sequence, action:'input', ...input }, this.runtimeOrigin(frame)); }
      catch (error) { this.failTasTransport(`Could not send score tick ${input.tick} to the runtime: ${error.message || error}`); }
    };
    this.receiveTasRelease = () => this.releaseTasInputs();
    this.receiveTasVmControl = event => {
      if (!this.session) return;
      const action = event.detail?.action;
      // Pausing remains allowed during a target switch so a hidden runtime
      // cannot keep advancing. Resuming is limited to the active game target.
      if (action === 'pause' || action === 'status' || (action === 'resume' && this.tasTargetActive) || (action === 'frame' && this.tasTargetActive)) this.sendVmControl(action);
    };
    this.onPageBlur = () => this.releaseTasInputs();
    this.onVisibilityChange = () => { if (document.visibilityState !== 'visible') this.releaseTasInputs(); };
    addEventListener('telos:wasm-target', this.receiveTasTarget);
    addEventListener('telos:wasm-input', this.receiveTasInput);
    addEventListener('telos:wasm-release', this.receiveTasRelease);
    addEventListener('telos:wasm-vm-control', this.receiveTasVmControl);
    addEventListener('blur', this.onPageBlur);
    document.addEventListener('visibilitychange', this.onVisibilityChange);
    this.tasTargetActive = document.querySelector('#tab-original')?.getAttribute('aria-selected') === 'true';
  }
  sendTas(message) {
    const frame = this.host?.querySelector('iframe');
    if (!frame?.contentWindow || !this.session || !this.tasBridgeReady) return false;
    const sequence = ++this.tasSequence;
    try { frame.contentWindow.postMessage({ type:'telos-wasm-tas', session:this.session, sequence, ...message }, this.runtimeOrigin(frame)); return true; }
    catch { return false; }
  }
  sendVmControl(action) {
    const frame = this.host?.querySelector('iframe');
    if (!this.tasVmReady || !frame?.contentWindow || !this.session || !['pause','resume','step','frame','status'].includes(action)) return false;
    const request = ++this.tasVmSequence;
    const message = { type:'telos-wasm-vm-control', schema:'telos-wasm-vm-control/v1', session:this.session, request, action };
    if (action === 'step' || action === 'frame') message.unit = 'render-present';
    try { frame.contentWindow.postMessage(message, this.runtimeOrigin(frame)); return true; }
    catch { return false; }
  }
  rejectGuestMemoryRequests(message = 'The game session changed before the guest memory request completed.') {
    for (const pending of this.guestMemoryRequests.values()) { clearTimeout(pending.timer); pending.reject(new Error(message)); }
    this.guestMemoryRequests.clear();
  }
  requestGuestMemory(operation, params = {}) {
    const frame = this.host?.querySelector('iframe'), session = this.session;
    if (!session || !this.runtimeGuestMemoryAvailable || !frame?.contentWindow) return Promise.reject(new Error('Guest memory reading is unavailable in this game session.'));
    const request = ++this.guestMemorySequence;
    return new Promise((resolve, reject) => {
      const pending = { session, operation, length:params.length, resolve, reject, timer:0 };
      pending.timer = setTimeout(() => {
        if (this.guestMemoryRequests.get(request) !== pending) return;
        this.guestMemoryRequests.delete(request);
        reject(new Error('The guest memory request did not respond.'));
      }, 10000);
      this.guestMemoryRequests.set(request, pending);
      try { frame.contentWindow.postMessage({ type:'telos-wasm-guest-memory', schema:'telos-wasm-guest-memory/v1', session, request, operation, ...params }, this.runtimeOrigin(frame)); }
      catch (error) { clearTimeout(pending.timer); this.guestMemoryRequests.delete(request); reject(error); }
    });
  }
  getGuestProcesses() { return this.requestGuestMemory('processes').then(value => value.processes); }
  readGuestMemory(pid, address, length) {
    if (!Number.isSafeInteger(pid) || pid <= 0 || pid > 0xffffffff || !Number.isSafeInteger(address) || address < 0 || address > 0xffffffff
      || !Number.isSafeInteger(length) || length < 1 || length > 65536 || address + length > 0x100000000) {
      return Promise.reject(new Error('Guest memory reads require a valid PID, 32-bit address, and length from 1 to 65536 bytes.'));
    }
    return this.requestGuestMemory('read', { pid, address, length }).then(value => new Uint8Array(value.buffer).slice());
  }
  publishRuntimeSession() {
    if (!this.session) return;
    const vm = this.runtimeVmCapabilities || {};
    const runtimeReady = vm.pause === true && vm.resume === true;
    const state = (available, qualified = true) => !qualified ? 'not-qualified' : available ? 'available' : runtimeReady ? 'unsupported' : 'unavailable';
    const operation = (available, unit = null, qualified = true) => Object.freeze({ state:state(available, qualified), ...(unit ? { unit } : {}) });
    const profile = gameProfileById(this.runtimeGuestId);
    const descriptor = Object.freeze({
      schema:'telos-runtime-session/v1', session:this.session,
      runtimeProtocol:'telos-wasm-vm-control/v1', guestId:this.runtimeGuestId,
      backend:Object.freeze({ id:'boxedwine-wasm', version:this.runtimeRelease || 'unavailable' }),
      target:profile ? Object.freeze({ profile:profile.id, format:'pe32', architecture:'x86', osAbi:'windows-32' }) : null,
      debugger:Object.freeze({
        schema:'telos.debug-capabilities/v1',
        operations:Object.freeze({
          input:operation(this.runtimeTasAvailable, 'render-present'),
          pause:operation(vm.pause === true),
          resume:operation(vm.resume === true),
          step:operation(vm.step === true && vm.frameStep === true, vm.unit || null),
          threadCounts:operation(runtimeReady),
          threadList:operation(false), memoryRead:operation(this.runtimeGuestMemoryAvailable), registers:operation(false),
          disassembly:operation(false), breakpoints:operation(false), snapshot:operation(false), restore:operation(false),
        }),
      }),
      executionBoundary:vm.unit === 'render-present' ? vm.unit : null,
      status:this.runtimeVmStatus,
      capabilities:Object.freeze({
        pause:vm.pause === true, resume:vm.resume === true,
        renderStep:vm.step === true && vm.frameStep === true, input:this.runtimeTasAvailable,
        memory:this.runtimeGuestMemoryAvailable, registers:false, disassembly:false, snapshot:false, rewind:false,
      }),
    });
    dispatchEvent(new CustomEvent('telos:runtime-session', { detail:descriptor }));
  }
  runtimeOrigin(frame) {
    try { return new URL(frame?.src, location.href).origin; }
    catch { return location.origin; }
  }
  clearTasPending() {
    if (!this.tasPending) return null;
    const pending = this.tasPending;
    clearTimeout(pending.timer);
    this.tasPending = null;
    dispatchEvent(new CustomEvent('telos:wasm-pending', { detail:{ pending:false, tick:pending.tick } }));
    return pending;
  }
  failTasTransport(message) {
    const pending = this.clearTasPending();
    const waitingInput = this.tasWaitingInput;
    this.tasWaitingInput = null;
    if (waitingInput) dispatchEvent(new CustomEvent('telos:wasm-pending', { detail:{ pending:false, tick:waitingInput.tick } }));
    this.releaseTasInputs();
    // A rejected frame does not unload the iframe bridge. Keep the validated
    // session ready so the user can seek back and retry after the VM parks.
    dispatchEvent(new CustomEvent('telos:wasm-transport-error', { detail:{ session:this.session, message, tick:pending?.tick ?? null } }));
    if (this.tasStatus) this.tasStatus.textContent = message;
  }
  releaseTasInputs() {
    if (!this.tasBridgeReady || !this.session) return;
    this.sendTas({ action:'release', tick:0 });
  }
  resetTasControls() {
    const starting = !!this.session;
    this.tasBridgeReady = false; this.tasVmReady = false;
    this.tasSequence = 0; this.tasLastAck = 0; this.tasVmSequence = 0; this.runtimeVmStatusRequest = 0;
    for (const button of this.tasVmButtons) button.disabled = true;
    if (this.tasStatus) this.tasStatus.textContent = starting
      ? 'Connecting TAS controls…'
      : 'No KOTOR session connected. Sample scene and editor state are separate from live KOTOR.';
    if (this.tasVmStatus) {
      this.tasVmStatus.textContent = starting ? 'Game controls are starting.' : 'Game controls are unavailable until KOTOR starts.';
      this.tasVmStatus.title = '';
      this.tasVmStatus.hidden = !starting;
    }
  }
  stopRuntime(state, message) {
    this.startup?.update(state, message);
    const displayMessage = state === 'error'
      ? 'KOTOR stopped after an error. Open Diagnostics for details.' : 'KOTOR has stopped.';
    this.runtimeStoppedSession = this.session;
    this.rejectGuestMemoryRequests('Guest memory reading stopped with this runtime session.');
    this.runtimeGuestMemoryAvailable = false;
    this.tasBridgeReady = false;
    this.tasVmReady = false;
    this.runtimeVmCapabilities = null; this.runtimeVmStatus = state; this.runtimeTasAvailable = false; this.publishRuntimeSession();
    for (const button of this.tasVmButtons) button.disabled = true;
    this.failTasTransport(displayMessage);
    window.__telosRuntimeDiagnostics = { session:this.session, state, error:state === 'error' ? message : null, receivedAt:performance.now() };
    this.status.textContent = displayMessage;
    this.status.className = state === 'error' ? 'status bad' : 'status';
    this.status.setAttribute('role', state === 'error' ? 'alert' : 'status');
    if (this.tasVmStatus) { this.tasVmStatus.hidden = false; this.tasVmStatus.textContent = state === 'error' ? 'Stopped after an error' : 'Stopped'; this.tasVmStatus.title = message; }
  }
  installTasBridge(frame, session) {
    const current = () => !this.closed && this.session === session
      && this.runtimeStoppedSession !== session && frame === this.host?.querySelector('iframe');
    frame.addEventListener('load', () => {
      if (!current()) return;
      // Same-origin runtimes can receive the bridge from the page. The local
      // localhost iframe installs it from owned-install.js because its parent
      // may be 127.0.0.1 and must not inspect a cross-origin document.
      if (this.runtimeOrigin(frame) !== location.origin) return;
      try {
        const doc = frame.contentDocument;
        if (!doc || doc.location.origin !== location.origin) throw new Error('Runtime frame is not same-origin.');
        if (doc.getElementById('telos-wasm-tas-bridge')) return;
        const script = doc.createElement('script');
        script.id = 'telos-wasm-tas-bridge';
        script.src = '/original-game/telos-tas-bridge.js?v=15-compiled-input-batch';
        script.onerror = () => {
          if (!current()) return;
          this.tasBridgeReady = false;
          if (this.tasStatus) this.tasStatus.textContent = 'TAS controls could not connect. You can still play with the keyboard and mouse.';
        };
        doc.head.append(script);
      } catch {
        this.tasBridgeReady = false;
        if (this.tasStatus) this.tasStatus.textContent = 'TAS controls could not connect. You can still play with the keyboard and mouse.';
      }
    }, { once:true });
  }
  async open() {
    this.localInstallPending = LOCAL_AUTO_INSTALL;
    const controls = document.createElement('div'); controls.className = 'original-game-controls';
    const intro = document.createElement('div'); intro.className = 'original-game-intro';
    const heading = document.createElement('h2'); heading.textContent = 'Play KOTOR';
    const description = document.createElement('p'); description.textContent = ['127.0.0.1', 'localhost', '[::1]'].includes(location.hostname)
      ? 'Connect your KOTOR installation folder, or use an installation already connected on this computer. Files load only when needed.'
      : 'Connect a KOTOR installation to play it here. Game files stay on your device and load only when needed.';
    intro.append(heading, description);
    const actions = document.createElement('div'); actions.className = 'original-game-actions';
    const choose = document.createElement('button'); choose.type = 'button'; choose.className = 'game-action connect'; choose.textContent = 'Connect game folder';
    const start = document.createElement('button'); start.type = 'button'; start.className = 'game-action launch'; start.textContent = 'Start KOTOR'; start.disabled = true;
    choose.hidden = LOCAL_AUTO_INSTALL && !compatibleFolderPickerRequested;
    start.hidden = LOCAL_AUTO_INSTALL && !compatibleFolderPickerRequested;
    actions.append(choose, start);
    const loose = document.createElement('input'); loose.type = 'file'; loose.multiple = true; loose.hidden = true;
    const browse = document.createElement('button'); browse.type = 'button'; browse.className = 'btn small'; browse.textContent = 'Choose individual files';
    const browseFolder = document.createElement('button'); browseFolder.type = 'button'; browseFolder.className = 'btn small'; browseFolder.textContent = 'Choose a folder (compatible picker)';
    browseFolder.title = 'Select the full KOTOR installation folder using the browser file picker';
    const chooseAnother = document.createElement('button'); chooseAnother.type = 'button'; chooseAnother.className = 'btn small'; chooseAnother.textContent = 'Choose a different folder';
    const forget = document.createElement('button'); forget.type = 'button'; forget.className = 'btn small'; forget.textContent = 'Forget saved folder';
    const fallback = document.createElement('input'); fallback.type = 'file'; fallback.multiple = true; fallback.setAttribute('webkitdirectory', ''); fallback.hidden = true;
    let fallbackPickerGeneration = null;
    const compatibleInput = document.createElement('input'); compatibleInput.type = 'file'; compatibleInput.accept = '.exe,application/vnd.microsoft.portable-executable'; compatibleInput.hidden = true;
    compatibleInput.setAttribute('aria-label', 'Choose a compatible KOTOR executable');
    const compatibleButton = document.createElement('button'); compatibleButton.type = 'button'; compatibleButton.className = 'btn small'; compatibleButton.textContent = 'Use a compatible executable';
    const clearCompatible = document.createElement('button'); clearCompatible.type = 'button'; clearCompatible.className = 'btn small'; clearCompatible.textContent = 'Use folder executable'; clearCompatible.hidden = true;
    const originLabel = document.createElement('label'); originLabel.className = 'game-executable-origin'; originLabel.textContent = 'Source (your selection)';
    const originSelect = document.createElement('select'); originSelect.setAttribute('aria-label', 'Compatible executable source, selected by you');
    for (const [value, label] of [['unspecified', 'Choose source…'], ['gog', 'GOG / DRM-free'], ['steam', 'Steam'], ['other', 'Other']]) {
      const option = document.createElement('option'); option.value = value; option.textContent = label; originSelect.append(option);
    }
    originLabel.append(originSelect); originLabel.hidden = true;
    this.status = document.createElement('p'); this.status.className = 'status';
    this.status.setAttribute('role', 'status'); this.status.setAttribute('aria-live', 'polite');
    this.status.textContent = LOCAL_AUTO_INSTALL ? 'Looking for the configured KOTOR installation…' : 'Choose your installed KOTOR folder. Game files stay on your device and are read from disk only when needed.';
    this.tasStatus = document.createElement('p'); this.tasStatus.className = 'game-native-note';
    this.tasStatus.setAttribute('role', 'status'); this.tasStatus.setAttribute('aria-live', 'polite');
    const vmControls = document.createElement('div'); vmControls.className = 'game-vm-controls';
    const pauseVm = document.createElement('button'); pauseVm.type = 'button'; pauseVm.className = 'btn small'; pauseVm.textContent = 'Pause game'; pauseVm.disabled = true;
    const resumeVm = document.createElement('button'); resumeVm.type = 'button'; resumeVm.className = 'btn small'; resumeVm.textContent = 'Resume game'; resumeVm.disabled = true;
    const stepVm = document.createElement('button'); stepVm.type = 'button'; stepVm.className = 'btn small'; stepVm.textContent = 'Next render'; stepVm.title = 'Advance to the next rendered image, then pause.'; stepVm.disabled = true;
    this.tasVmButtons = [pauseVm, resumeVm, stepVm];
    this.tasVmStatus = document.createElement('p'); this.tasVmStatus.className = 'game-native-note';
    this.tasVmStatus.setAttribute('role', 'status'); this.tasVmStatus.setAttribute('aria-live', 'polite');
    this.resetTasControls();
    pauseVm.addEventListener('click', () => this.sendVmControl('pause'));
    resumeVm.addEventListener('click', () => this.sendVmControl('resume'));
    stepVm.addEventListener('click', () => this.sendVmControl('step'));
    vmControls.append(pauseVm, resumeVm, stepVm);
    this.checks = document.createElement('ul'); this.checks.className = 'game-file-checks'; this.checks.setAttribute('aria-label', 'Required game files');
    const alternatives = document.createElement('details'); alternatives.className = 'game-file-alternatives';
    alternatives.hidden = LOCAL_AUTO_INSTALL && !compatibleFolderPickerRequested;
    alternatives.open = compatibleFolderPickerRequested;
    const alternativeSummary = document.createElement('summary'); alternativeSummary.textContent = 'Other ways to connect files';
    const alternativeCopy = document.createElement('p'); alternativeCopy.textContent = 'Select the full installation with the folder picker, add individual files, or drop files here. The checklist checks every BIF referenced by chitin.key before enabling Start.';
    const alternativeActions = document.createElement('div'); alternativeActions.className = 'game-file-alternative-actions'; alternativeActions.append(browseFolder, browse, chooseAnother, forget, compatibleButton, clearCompatible, originLabel);
    const executableHelp = document.createElement('p'); executableHelp.className = 'game-native-note'; executableHelp.textContent = 'Optional: choose swkotor.exe from another copy you own. Use one that matches this install. Only the executable changes; resource data, including dialog.tlk, stays from the connected folder. Choose its source below; the filename alone cannot identify the edition.';
    this.compatibleStatus = document.createElement('p'); this.compatibleStatus.className = 'game-native-note'; this.compatibleStatus.setAttribute('role', 'status'); this.compatibleStatus.hidden = true;
    this.compatibleNotice = document.createElement('p'); this.compatibleNotice.className = 'game-native-note steam-launch-note'; this.compatibleNotice.textContent = 'This browser launch has no Steam client session. Choose a DRM-free executable from a copy you own.'; this.compatibleNotice.hidden = true;
    alternatives.append(alternativeSummary, alternativeCopy, executableHelp, alternativeActions, compatibleInput, this.compatibleStatus, this.compatibleNotice);
    this.host = document.createElement('div'); this.host.className = 'original-game-view';
    this.fileAlternatives = alternatives; this.connectFolderButton = choose;
    const setup = document.createElement('details'); setup.className = 'game-setup'; setup.open = true;
    const setupSummary = document.createElement('summary'); setupSummary.textContent = 'Game files';
    setup.append(setupSummary, intro, this.checks, alternatives);
    controls.append(actions, this.status, vmControls, setup, this.tasStatus, this.tasVmStatus, fallback, loose); this.root.replaceChildren(controls, this.host);
    this.setupDetails = setup;
    this.pendingFiles = new Map();
    this.assessment = null;
    this.compatibleExecutable = null;
    this.compatibleExecutableSource = 'unspecified';
    this.restoreGeneration = 0;
    this.releaseButton = null;
    this.receiveStatus = event => {
      const value = event.data;
      const frame = this.host?.querySelector('iframe');
      if (this.closed || !this.session || !frame || event.source !== frame.contentWindow || event.origin !== this.runtimeOrigin(frame) || value?.session !== this.session) return;
      // A worker failure is terminal for this session; delayed ready/running
      // messages cannot revive its controls or overwrite the failure.
      if (this.runtimeStoppedSession === this.session) return;
      if (value.type === 'telos-game-status' && ['error','stopped'].includes(value.state) && typeof value.message === 'string') {
        // Keep enough bounded detail for the startup diagnostics panel to show
        // a useful error code, path, and recovery context. The primary UI still
        // presents a concise status; GameStartup owns the larger log bound.
        this.stopRuntime(value.state, value.message.slice(0,8192));
        return;
      }
      if (value.type === 'telos-owned-snapshots-request') {
        if (typeof value.request !== 'string' || !/^[0-9a-f-]{36}$/.test(value.request)) return;
        if (!Array.isArray(this.files) || this.files.length < 3 || this.files.length > 100000) {
          frame.contentWindow.postMessage({type:'telos-owned-snapshots', session:this.session, request:value.request,
            error:`OWNED_PARENT_SNAPSHOT_STATE_INVALID: selected file snapshot set is ${Array.isArray(this.files) ? `size ${this.files.length}` : 'unavailable'}; expected 3..100000 entries. Reconnect the game folder and start a fresh session.`}, this.runtimeOrigin(frame));
          return;
        }
        // Structured cloning File snapshots does not read their contents here.
        // Only this current authorized iframe receives the disk-backed objects.
        frame.contentWindow.postMessage({ type:'telos-owned-snapshots', session:this.session, request:value.request,
          files:this.files.map(({name, file}) => ({name, file})) }, this.runtimeOrigin(frame));
        return;
      }
      if (value.type === 'telos-owned-manifest-request') {
        if (typeof value.request !== 'string' || !/^[0-9a-f-]{36}$/.test(value.request)) return;
        if (!Array.isArray(this.files) || this.files.length < 3 || this.files.length > 100000) {
          frame.contentWindow.postMessage({type:'telos-owned-manifest', session:this.session, request:value.request,
            error:`OWNED_PARENT_MANIFEST_STATE_INVALID: selected file set is ${Array.isArray(this.files) ? `size ${this.files.length}` : 'unavailable'}; expected 3..100000 entries. Reconnect the game folder and start a fresh session.`}, this.runtimeOrigin(frame));
          return;
        }
        // The runtime asks this owning page for metadata and then separately
        // receives the same File snapshots. This avoids accidentally reading a
        // different installation from a host-level /private/install.json and
        // remains functional when the metadata service worker restarts.
        const manifest = {
          schema:'telos.owned-game-assets/v1', assetMode:'lazy',
          files:this.files.map(({name, file}, index) => ({name, bytes:file.size, url:`/original-game/owned-game-assets/${this.session}/${index}`})),
          writable:['swkotor.ini','saves','logs','currentgame','gameinprogress','temp','tempclient','localvault','dmvault','servervault']
        };
        if (manifest.files.some(file => typeof file.name !== 'string' || !Number.isSafeInteger(file.bytes) || file.bytes < 0)) return;
        frame.contentWindow.postMessage({type:'telos-owned-manifest', session:this.session, request:value.request, manifest}, this.runtimeOrigin(frame));
        return;
      }
      if (value.type === 'telos-wasm-tas-ready') {
        const caps = value.capabilities;
        const validCaps = caps?.version === 'telos-wasm-tas-bridge/v8'
          && caps.input === 'boxedwine-native-key-export;boxedwine-native-mouse-exports'
          && caps.mouse === 'absolute-canvas-pixel-native-events;pointer-lock-relative-unavailable'
          && caps.acknowledgement === 'native-input-dispatch-and-one-render'
          && caps.frameAlignment === 'bounded-gl-present-drain-per-score-step';
        if (!validCaps) {
          this.failTasTransport('TAS controls could not connect to this game session.');
          return;
        }
        this.tasBridgeReady = true; this.tasSequence = 0; this.tasLastAck = 0;
        this.runtimeTasAvailable = true; this.publishRuntimeSession();
        if (this.tasStatus) this.tasStatus.textContent = 'TAS connected · keyboard and mouse ready.';
        if (this.tasWaitingInput) {
          const input = this.tasWaitingInput;
          this.tasWaitingInput = null;
          this.receiveTasInput({ detail:input });
        }
        return;
      }
      if (value.type === 'telos-wasm-guest-memory-reply' && value.schema === 'telos-wasm-guest-memory/v1') {
        if (!Number.isSafeInteger(value.request)) return;
        const pending = this.guestMemoryRequests.get(value.request);
        if (!pending || pending.session !== this.session || pending.operation !== value.operation) return;
        clearTimeout(pending.timer); this.guestMemoryRequests.delete(value.request);
        if (value.ok !== true) {
          const error = new Error(typeof value.error === 'string' ? value.error.slice(0, 240) : 'The runtime rejected the guest memory request.');
          error.statusCode = Number.isInteger(value.status) && value.status >= 1 && value.status <= 5 ? value.status : null;
          pending.reject(error);
        }
        else if (value.operation === 'processes' && Array.isArray(value.processes)
          && value.processes.length <= 4096 && value.processes.every(item => Number.isSafeInteger(item?.pid) && item.pid > 0 && item.pid <= 0xffffffff)) {
          pending.resolve({ processes:value.processes.map(({ pid }) => ({ pid })) });
        } else if (value.operation === 'read' && value.buffer instanceof ArrayBuffer && value.buffer.byteLength === pending.length) {
          pending.resolve({ buffer:value.buffer });
        } else pending.reject(new Error('The runtime returned a malformed guest memory reply.'));
        return;
      }
      if (value.type === 'telos-wasm-vm-ready' && value.schema === 'telos-wasm-vm-control/v1') {
        const caps = value.capabilities;
        if (!caps || typeof value.available !== 'boolean' || value.unit !== 'render-present'
          || ['pause','resume','step','frameStep','checkpoint','restore','seek','presentMarker'].some(key => typeof caps[key] !== 'boolean')
          || (caps.memoryRead !== undefined && typeof caps.memoryRead !== 'boolean')) return;
        if (this.runtimeVmCapabilities?.pause === true && value.available !== true) return;
        const memoryWasAvailable = this.runtimeGuestMemoryAvailable;
        this.runtimeVmCapabilities = { ...caps, memoryRead:caps.memoryRead === true, unit:value.unit };
        this.runtimeGuestMemoryAvailable = value.available === true && caps.memoryRead === true;
        if (memoryWasAvailable && !this.runtimeGuestMemoryAvailable) this.rejectGuestMemoryRequests('Guest memory reading became unavailable in this runtime.');
        this.tasVmReady = value.available === true && caps.pause === true && caps.resume === true
          && caps.step === true && caps.frameStep === true;
        this.publishRuntimeSession();
        for (const button of this.tasVmButtons || []) button.disabled = !this.tasVmReady;
        if (this.tasVmStatus) {
          this.tasVmStatus.hidden = !this.tasVmReady;
          if (this.tasVmReady) this.tasVmStatus.textContent = 'Game controls ready.';
        }
        return;
      }
      if (value.type === 'telos-wasm-vm-status' && value.schema === 'telos-wasm-vm-control/v1') {
        const status = normalizeVmStatus(value);
        if (!status) return;
        if (value.ackRequest !== null && (!Number.isSafeInteger(value.ackRequest) || value.ackRequest <= this.runtimeVmStatusRequest || value.ackRequest > this.tasVmSequence)) return;
        if (value.ackRequest !== null) this.runtimeVmStatusRequest = value.ackRequest;
        window.__telosRuntimeDiagnostics = { ...status, session:this.session, receivedAt:performance.now() };
        this.startup?.observe(status);
        this.runtimeVmStatus = status.state; this.publishRuntimeSession();
        const controlLabel = {
          running:'Control: resumed',
          pausing:'Control: pausing',
          paused:'Control: paused',
          stepping:'Control: stepping'
        }[status.state];
        const renderLabel = status.presents === null ? 'Render count not reported'
          : status.presents === 0 ? 'No game image yet' : 'Game image observed';
        const runtimeLabel = `${controlLabel} · ${renderLabel}`;
        if (this.runtimeProfileReady && this.status && this.startup?.presented) {
          const message = runtimeLabel;
          if (this.status.textContent !== message) this.status.textContent = message;
          this.status.className = 'status ok';
          this.status.setAttribute('role', 'status');
        }
        if (this.tasVmStatus) {
          this.tasVmStatus.hidden = false;
          const threads = status.activeThreads !== null
            ? ` · ${status.parkedThreads}/${status.activeThreads} workers at a safe point` : '';
          const presents = status.presents !== null ? ` · present ${status.presents}` : '';
          this.tasVmStatus.title = `${runtimeLabel} · boundary: ${status.unit}${threads}${presents}${status.error ? ` · ${status.error}` : ''}`;
          const compactStatus = `${runtimeLabel}${status.error ? ` · ${status.error}` : ''}`;
          if (this.tasVmStatus.textContent !== compactStatus) this.tasVmStatus.textContent = compactStatus;
        }
        return;
      }
      if (value.type === 'telos-wasm-tas-ack') {
        if (!['input-applied','released','error'].includes(value.state) || !Number.isSafeInteger(value.sequence) || value.sequence <= this.tasLastAck) return;
        if (value.state === 'error') { this.failTasTransport(value.error || 'The guest rejected TAS input.'); return; }
        if (value.state === 'released') { this.tasLastAck = value.sequence; return; }
        const pending = this.tasPending;
        if (!pending || value.sequence !== pending.sequence || value.tick !== pending.tick) return;
        const latencyMs = Math.max(0, performance.now() - pending.sentAt);
        if (!Number.isSafeInteger(value.present) || !Number.isSafeInteger(value.eventsDispatched) || value.eventsDispatched < 0 || !Number.isSafeInteger(value.eventsConsumed) || value.eventsConsumed < 0 || !Number.isSafeInteger(value.presentsAdvanced) || value.presentsAdvanced !== 1 || !(value.inputPresentOffset === null || (Number.isSafeInteger(value.inputPresentOffset) && value.inputPresentOffset >= 0 && value.inputPresentOffset <= 1))) { this.failTasTransport('TAS controls could not confirm the input. Playback stopped.'); return; }
        if (!['guest-consumed-events','native-mouse-position','unchanged-held-state'].includes(value.inputBoundary) || value.frameBoundary !== 'bounded-boxedwine-gl-present' || (value.inputBoundary === 'guest-consumed-events' && (value.eventsDispatched < 1 || value.eventsConsumed < 1 || !Number.isSafeInteger(value.inputPresentOffset))) || (value.inputBoundary === 'native-mouse-position' && (value.eventsDispatched < 1 || value.eventsConsumed !== 0 || value.inputPresentOffset !== null)) || (value.inputBoundary === 'unchanged-held-state' && (value.eventsDispatched !== 0 || value.inputPresentOffset !== null))) { this.failTasTransport('TAS controls could not confirm the input. Playback stopped.'); return; }
        // The bridge only supplies a consumption count and present offset when its
        // guest-side sequence matches this acknowledgement's sequence.
        this.tasLastAck = value.sequence;
        this.clearTasPending();
        if (this.tasStatus) this.tasStatus.textContent = value.eventsDispatched
          ? `Tick ${value.tick} · sent ${value.eventsDispatched} input changes · present ${value.present} · ${latencyMs.toFixed(1)} ms.`
          : `Tick ${value.tick} · inputs held · present ${value.present} · ${latencyMs.toFixed(1)} ms.`;
        dispatchEvent(new CustomEvent('telos:wasm-ack', { detail:{ session:this.session, tick:value.tick, sequence:value.sequence, latencyMs, present:value.present, eventsConsumed:value.eventsConsumed, presentsAdvanced:value.presentsAdvanced, inputPresentOffset:value.inputPresentOffset, delayed:latencyMs > 1000 / pending.hz } }));
        return;
      }
      if (value.type !== 'telos-game-status') return;
      if (!['starting','files-ready','error'].includes(value.state) || typeof value.message !== 'string') return;
      if (value.state === 'files-ready') this.runtimeProfileReady = true;
      this.startup?.update(value.state, value.message);
      this.status.textContent = value.state === 'files-ready'
        ? 'Game files connected. Waiting for the first KOTOR frame…' : 'Starting KOTOR…';
      this.status.setAttribute('role', value.state === 'error' ? 'alert' : 'status');
    };
    addEventListener('message', this.receiveStatus);
    const stage = async (entries, { replace = false, handle = null, remembered = false, generation = null } = {}) => {
      const current = () => !this.closed && (generation === null || generation === this.restoreGeneration);
      if (!current()) return;
      if (replace) {
        this.localManifest = null; this.localGameProfile = null;
        this.assessment = null;
        this.renderFileState();
        this.pendingFiles.clear(); this.supplementalFiles = false;
        if (!handle) {
          this.rememberedHandle = null; this.savedFolder = false;
          try { await this.persistFolderHandle('delete', null, generation); }
          catch { this.persistenceNote = 'The previous folder choice could not be cleared.'; }
          if (!current()) return;
          if (!this.persistenceNote?.includes('could not be cleared')) this.persistenceNote = '';
        }
      } else if (entries.length) this.supplementalFiles = true;
      const stagedEntries = replace ? entries : [...this.pendingFiles.values(), ...entries];
      this.pendingFiles.clear();
      for (const entry of stagedEntries) {
        const name = canonicalSelectedPath(entry.name);
        this.pendingFiles.set(name.toLowerCase(), name === entry.name ? entry : { ...entry, name });
      }
      if (handle) { this.rememberedHandle = handle; this.savedFolder = remembered; this.persistenceNote = ''; }
      const scan = ++this.scanGeneration;
      this.status.textContent = `Checking ${this.pendingFiles.size.toLocaleString()} installation files…`;
      try {
        const result = await inspect([...this.pendingFiles.values()], { requireExecutable: !this.compatibleExecutable });
        if (!current() || scan !== this.scanGeneration) return;
        this.assessment = result;
        this.renderFileState();
        if (handle && !remembered) {
          try { await this.persistFolderHandle('put', handle, generation); if (!current() || scan !== this.scanGeneration) return; this.savedFolder = true; this.status.textContent = 'Folder connected. Its handle is saved in this browser; game files are read only when needed.'; }
          catch { if (!current() || scan !== this.scanGeneration) return; this.savedFolder = false; this.status.textContent = 'Folder connected for this tab. Reconnect it after reloading.'; }
          this.renderFileState();
        }
      } catch (error) {
        if (!current() || scan !== this.scanGeneration) return;
        this.assessment = { files: [], missing: [], keyError: null, scanError: error.message, missingBifs: [], ready: false };
        this.renderFileState();
      }
    };
    this.stageFiles = stage;
    this.scanGeneration = 0;
    browse.addEventListener('click', () => loose.click());
    browseFolder.addEventListener('click', () => pickFolder());
    compatibleButton.addEventListener('click', () => compatibleInput.click());
    clearCompatible.addEventListener('click', async () => {
      const scan = ++this.scanGeneration;
      this.compatibleExecutable = null;
      this.compatibleExecutableSource = 'unspecified';
      originSelect.value = 'unspecified';
      compatibleInput.value = '';
      this.compatibleStatus.hidden = true;
      this.compatibleNotice.hidden = true;
      clearCompatible.hidden = true;
      originLabel.hidden = true;
      try { this.assessment = await inspect([...this.pendingFiles.values()]); }
      catch (error) { this.assessment = { files: [], missing: [], keyError: null, scanError: error.message, missingBifs: [], ready: false }; }
      if (this.closed || scan !== this.scanGeneration) return;
      this.renderFileState();
    });
    originSelect.addEventListener('change', () => {
      this.compatibleExecutableSource = originSelect.value;
      if (this.compatibleExecutable) this.compatibleStatus.textContent = `Using your selected swkotor.exe (${(this.compatibleExecutable.file.size / 1024 / 1024).toFixed(1)} MB) for this session.`;
      this.compatibleNotice.hidden = this.compatibleExecutableSource !== 'steam';
      this.renderFileState();
    });
    compatibleInput.addEventListener('change', async () => {
      const file = compatibleInput.files?.[0];
      if (!file) return;
      if (file.name.toLowerCase() !== 'swkotor.exe' || file.size === 0) {
        this.status.textContent = 'Choose a non-empty file named swkotor.exe.';
        this.status.className = 'status bad'; this.status.setAttribute('role', 'alert');
        compatibleInput.value = ''; return;
      }
      this.compatibleExecutable = { name: 'swkotor.exe', file };
      this.compatibleExecutableSource = originSelect.value;
      this.compatibleStatus.textContent = this.compatibleExecutableSource === 'unspecified'
        ? `Selected swkotor.exe (${(file.size / 1024 / 1024).toFixed(1)} MB). Choose its source below to continue.`
        : `Using your selected swkotor.exe (${(file.size / 1024 / 1024).toFixed(1)} MB) for this session.`;
      this.compatibleStatus.hidden = false;
      this.compatibleNotice.hidden = this.compatibleExecutableSource !== 'steam';
      clearCompatible.hidden = false;
      originLabel.hidden = false;
      const scan = ++this.scanGeneration;
      let result;
      try { result = await inspect([...this.pendingFiles.values()], { requireExecutable: false }); }
      catch (error) { result = { files: [], missing: [], keyError: null, scanError: error.message, missingBifs: [], ready: false }; }
      if (this.closed || scan !== this.scanGeneration) return;
      this.assessment = result;
      this.renderFileState();
    });
    // The individual-file picker supplements the connected folder (not
    // replace it), so a missing runtime DLL can be added without rescanning
    // or selecting the entire installation again.
    loose.addEventListener('change', async () => { const generation = ++this.restoreGeneration; await stage([...loose.files].map(file => ({ file, name: file.name })), { generation }); loose.value = ''; });
    let launchPending = false;
    const launchConnectedGame = async () => {
      if (this.closed || this.session || launchPending || !this.assessment?.ready
          || (this.compatibleExecutable && this.compatibleExecutableSource === 'unspecified')) return;
      launchPending = true;
      start.disabled = true;
      const restoreGeneration = this.restoreGeneration;
      const pending = this.launch(this.assessment.files);
      const generation = this.generation;
      const current = () => !this.closed && generation === this.generation && restoreGeneration === this.restoreGeneration;
      try { if (await pending && current()) this.status.textContent = 'Launching KOTOR…'; }
      catch (error) { if (current()) { this.renderFileState(); this.status.textContent = error.message; this.status.className = 'status bad'; this.status.setAttribute('role', 'alert'); } }
      finally {
        launchPending = false;
        if (!this.closed) start.disabled = !this.assessment?.ready || !!this.session
          || !!(this.compatibleExecutable && this.compatibleExecutableSource === 'unspecified');
      }
    };
    start.addEventListener('click', launchConnectedGame);
    this.root.addEventListener('dragover', event => { event.preventDefault(); });
    this.root.addEventListener('drop', async event => {
      event.preventDefault();
      if (this.session) { this.status.textContent = 'Close the game before changing its connected files.'; return; }
      const generation = ++this.restoreGeneration;
      // Acquire handles while the drop's permission store is still readable.
      const dropped = [...event.dataTransfer.items].map(item => ({
        handle: item.getAsFileSystemHandle ? item.getAsFileSystemHandle() : Promise.resolve(null),
        file: item.getAsFile(),
      }));
      try {
        const entries = []; let directoryHandle = null;
        for (const item of dropped) {
          const handle = await item.handle;
          if (handle?.kind === 'directory') { directoryHandle = handle; entries.push(...await directoryFiles(handle)); }
          else { const file = handle ? await handle.getFile() : item.file; if (file) entries.push({ file, name:file.name, handle }); }
        }
        if (!this.closed && generation === this.restoreGeneration) await stage(entries, { replace: !!directoryHandle, handle: directoryHandle, generation });
      } catch (error) { if (!this.closed && generation === this.restoreGeneration) this.status.textContent = error.message; }
    });
    const pickFolder = async () => {
      const generation = ++this.restoreGeneration;
      const current = () => !this.closed && generation === this.restoreGeneration;
      try {
        if (!isSecureContext || !navigator.serviceWorker || !crossOriginIsolated) throw new Error('Open this page from its secure local address to connect a game folder.');
        if (!window.showDirectoryPicker || compatibleFolderPickerRequested) {
          if (!('webkitdirectory' in fallback)) throw new Error('This browser cannot select a whole folder here. Choose individual game files or drop them into this panel instead.');
          fallbackPickerGeneration = generation;
          this.status.textContent = 'Choose the KOTOR installation folder. If the picker is canceled, your current files will stay connected.';
          this.status.className = 'status'; this.status.setAttribute('role', 'status');
          fallback.click(); return;
        }
        this.status.textContent = 'Select the KOTOR installation folder, not just swkotor.exe.';
        this.status.className = 'status';
        this.status.setAttribute('role', 'status');
        const directory = await window.showDirectoryPicker({ mode: 'read', id: 'telos-k1-game' });
        if (!current()) return;
        this.status.textContent = 'Reading installation filenames…';
        const entries = await directoryFiles(directory);
        if (current()) await stage(entries, { replace: true, handle: directory, generation });
      } catch (error) {
        if (!current()) return;
        this.renderFileState();
        if (error.name === 'AbortError') {
          this.status.textContent = 'Folder selection canceled. Your current connected files were kept.';
          this.status.className = 'status'; this.status.setAttribute('role', 'status');
        } else {
          this.status.textContent = error.message;
          this.status.className = 'status bad'; this.status.setAttribute('role', 'alert');
        }
      }
    };
    const reconnect = async () => {
      const generation = ++this.restoreGeneration;
      const current = () => !this.closed && generation === this.restoreGeneration;
      try {
        if (!isSecureContext || !navigator.serviceWorker || !crossOriginIsolated) throw new Error('Open this page from its secure local address to reconnect a game folder.');
        let handle = this.rememberedHandle;
        if (!handle) { await pickFolder(); return; }
        // Call requestPermission directly from this click handler so the browser's
        // transient user activation is still present when a saved handle needs it.
        const permission = handle.requestPermission ? await handle.requestPermission({ mode: 'read' }) : await handle.queryPermission({ mode: 'read' });
        if (!current()) return;
        if (permission !== 'granted') { this.status.textContent = 'Folder is remembered, but access needs your permission. Choose Connect again or select a folder under Other ways.'; return; }
        this.status.textContent = 'Reconnecting to the saved KOTOR folder…';
        const entries = await directoryFiles(handle);
        if (current()) await stage(entries, { replace: true, handle, remembered: true, generation });
      } catch (error) {
        if (!current()) return;
        this.renderFileState();
        this.status.textContent = error.name === 'AbortError'
          ? 'Folder access was canceled. Your current connected files were kept.'
          : `${error.message} Choose a folder again if it moved.`;
        this.status.className = error.name === 'AbortError' ? 'status' : 'status bad';
        this.status.setAttribute('role', error.name === 'AbortError' ? 'status' : 'alert');
      }
    };
    choose.addEventListener('click', compatibleFolderPickerRequested ? pickFolder : reconnect);
    chooseAnother.addEventListener('click', pickFolder);
    fallback.addEventListener('cancel', () => {
      const generation = fallbackPickerGeneration;
      fallbackPickerGeneration = null;
      if (generation === null || this.closed || generation !== this.restoreGeneration) return;
      this.renderFileState();
      this.status.textContent = 'No new folder was selected. Your current connected files were kept.';
      this.status.className = 'status'; this.status.setAttribute('role', 'status');
    });
    fallback.addEventListener('change', async () => {
      const generation = fallbackPickerGeneration ?? ++this.restoreGeneration;
      fallbackPickerGeneration = null;
      if (this.closed || generation !== this.restoreGeneration) { fallback.value = ''; return; }
      try {
        const files = [...fallback.files].map(file => {
          const path = file.webkitRelativePath || file.name;
          return { file, name: path.includes('/') ? path.split('/').slice(1).join('/') : path };
        });
        await stage(files, { replace: true, generation });
        this.persistenceNote = 'Reconnect after reloading.';
        this.renderFileState();
      } catch (error) { if (!this.closed && generation === this.restoreGeneration) { this.status.textContent = error.message; this.status.className = 'status bad'; this.status.setAttribute('role', 'alert'); } }
      finally { fallback.value = ''; }
    });
    forget.addEventListener('click', async () => {
      const generation = ++this.restoreGeneration;
      try {
        await this.persistFolderHandle('delete', null, generation); if (this.closed || generation !== this.restoreGeneration) return;
        this.rememberedHandle = null; this.savedFolder = false;
        this.renderFileState();
        this.status.textContent = 'Saved folder access removed. Files already connected remain available in this tab.';
        this.status.className = 'status ok';
      } catch (error) { this.status.textContent = error.message; }
    });
    const release = document.createElement('button'); release.type = 'button'; release.className = 'game-action close'; release.textContent = 'Close game'; release.hidden = true;
    const stopWithoutSaving = document.createElement('button'); stopWithoutSaving.type = 'button'; stopWithoutSaving.className = 'game-action close'; stopWithoutSaving.textContent = 'Stop without saving'; stopWithoutSaving.title = 'Disconnect KOTOR without saving changes made since the last browser profile sync'; stopWithoutSaving.hidden = true;
    release.addEventListener('click', async () => {
      try {
        this.status.textContent = 'Saving the KOTOR profile…';
        if (await this.closeGame()) {
          release.disabled = true;
          this.allowStopWithoutSaving = false;
          stopWithoutSaving.hidden = true;
          this.status.textContent = 'KOTOR closed. Game files disconnected and profile saved.';
        }
      } catch (error) { this.status.textContent = error.message; this.allowStopWithoutSaving = true; stopWithoutSaving.hidden = false; }
    });
    stopWithoutSaving.addEventListener('click', async () => {
      stopWithoutSaving.disabled = true;
      try {
        if (await this.closeGame({ saveProfile: false })) {
          this.allowStopWithoutSaving = false;
          this.status.textContent = 'KOTOR stopped and game files disconnected without waiting for a final save. Recent changes may be missing from the browser profile.';
          stopWithoutSaving.hidden = true;
          release.disabled = true;
        }
      } catch (error) { this.status.textContent = error.message; }
      finally { stopWithoutSaving.disabled = false; }
    });
    actions.append(release, stopWithoutSaving); this.releaseButton = release; this.stopWithoutSavingButton = stopWithoutSaving;
    this.renderFileState = () => {
      const automaticInstall = !compatibleFolderPickerRequested && (this.localInstallPending || !!this.localManifest);
      choose.hidden = automaticInstall;
      alternatives.hidden = automaticInstall;
      choose.textContent = compatibleFolderPickerRequested ? 'Choose game folder (compatible)' : this.savedFolder ? 'Reconnect saved folder' : 'Connect game folder';
      start.textContent = this.localManifest ? 'Start connected KOTOR' : 'Start KOTOR';
      choose.disabled = (!compatibleFolderPickerRequested && this.localInstallPending) || !!this.session;
      browse.disabled = choose.disabled; browseFolder.disabled = choose.disabled; chooseAnother.disabled = choose.disabled;
      if (!this.checks) return;
      const result = this.assessment;
      const profile = result?.profile || DEFAULT_GAME_PROFILE;
      const labelFiles = [
        { name:profile.executable, label:'Game executable' },
        ...profile.requiredFiles,
        ...(profile.id === 'k2' ? (result?.importedLibraries || []).map(name => ({ name, label:'Executable dependency' })) : []),
      ];
      const labelsByName = new Map();
      for (const file of labelFiles) if (!labelsByName.has(file.name)) labelsByName.set(file.name, file);
      const labels = [...labelsByName.values()];
      const rows = labels.map(({ name, label }) => {
        const selected = name === profile.executable && profile.id === 'k1' && !!this.compatibleExecutable;
        const ready = selected || !!this.localManifest?.files?.some(entry => entry.name.toLowerCase() === name) || !!result?.files?.some(entry => entry.name.toLowerCase() === name);
        const detail = selected ? `Selected ${this.compatibleExecutableSource === 'unspecified' ? 'compatible' : this.compatibleExecutableSource.toUpperCase()} executable` : name;
        return { label, detail, ready, state: ready ? 'ready' : 'missing' };
      });
      let archiveDetail = result?.keyError ? result.keyError : result?.missingBifs?.length ? `Missing ${result.missingBifs.length}: ${result.missingBifs.slice(0, 3).join(', ')}${result.missingBifs.length > 3 ? '…' : ''}` : this.localManifest ? 'All archives listed by chitin.key are present' : result?.files?.some(entry => entry.name.toLowerCase() === 'chitin.key') ? 'All archives listed by chitin.key are present' : 'Checked against chitin.key after it is added';
      rows.push({ label: 'Game resource archives', detail: archiveDetail, ready: !!this.localManifest || (!!result && !result.keyError && result.missingBifs.length === 0 && !result.missing.includes('chitin.key')), state: result?.keyError || result?.missingBifs?.length ? 'missing' : this.localManifest || result?.files?.some(entry => entry.name.toLowerCase() === 'chitin.key') ? 'ready' : 'waiting' });
      this.checks.replaceChildren(...rows.map(row => {
        const item = document.createElement('li'); item.className = `game-file-check ${row.state}`;
        const name = document.createElement('span'); name.className = 'game-file-name'; name.textContent = row.label;
        const detail = document.createElement('span'); detail.className = 'game-file-detail'; detail.textContent = row.detail;
        const state = document.createElement('span'); state.className = 'game-file-state'; state.textContent = row.state === 'ready' ? 'Ready' : row.state === 'waiting' ? 'Waiting' : 'Missing';
        item.append(name, detail, state); return item;
      }));
      const pendingCount = this.localManifest?.files?.length || this.pendingFiles?.size || 0;
      if (result?.ready) {
        const saved = this.localManifest ? ' Files are read directly from this computer as needed.' : this.savedFolder ? ' Folder handle saved in this browser.' : ' Files stay available until this tab closes.';
        const supplemental = this.supplementalFiles ? ' Reconnect individually added files after reloading.' : '';
        const needsSource = this.compatibleExecutable && this.compatibleExecutableSource === 'unspecified';
        this.status.textContent = `${pendingCount.toLocaleString()} game files connected. ${needsSource ? 'Choose the executable source to continue.' : 'Ready to launch.'}${saved}${supplemental}${this.persistenceNote ? ` ${this.persistenceNote}` : ''}`;
        this.status.className = 'status ok';
      } else if (result) {
        if (result.scanError) {
          this.status.textContent = `Could not check these files: ${result.scanError}`;
          this.status.className = 'status bad';
          start.disabled = true;
          release.hidden = !this.session; release.disabled = !this.session; stopWithoutSaving.hidden = !this.session || !this.allowStopWithoutSaving; start.hidden = automaticInstall || !!this.session;
          return;
        }
        const missing = [...result.missing, ...(result.keyError ? [result.keyError] : []), ...result.missingBifs];
        this.status.textContent = result.profileError
          ? `${result.profileError} ${missing.length ? `Still needed: ${missing.slice(0, 4).join(', ')}.` : ''}`
          : result.keyError && !result.missing.length ? `${result.keyError} Choose the game folder again or replace chitin.key.` : `${pendingCount.toLocaleString()} files found. Still needed: ${missing.slice(0, 4).join(', ')}${missing.length > 4 ? ` and ${missing.length - 4} more` : ''}.`;
        this.status.className = 'status warn';
      } else {
        this.status.textContent = this.localInstallPending ? 'Looking for a connected KOTOR installation…' : this.rememberedHandle ? 'A game folder is saved. Connect to check its current files.' : 'Choose your installed game folder, or drop the folder/files here.';
        this.status.className = 'status';
      }
      start.disabled = launchPending || !result?.ready || !!this.session || !!(this.compatibleExecutable && this.compatibleExecutableSource === 'unspecified');
      forget.disabled = !!this.session;
      compatibleButton.hidden = result?.profile?.id === 'k2';
      compatibleButton.disabled = !!this.session || result?.profile?.id === 'k2'; clearCompatible.disabled = !!this.session; originSelect.disabled = !!this.session;
      release.hidden = !this.session;
      release.disabled = !this.session;
      stopWithoutSaving.hidden = !this.session || !this.allowStopWithoutSaving;
      start.hidden = automaticInstall || !!this.session;
    };
    this.renderFileState();
    const localInstallGeneration = this.restoreGeneration;
    this.connectLocalInstall(localInstallGeneration).then(async connected => {
      if (this.closed) return;
      this.localInstallPending = false;
      if (localInstallGeneration !== this.restoreGeneration) return;
      this.renderFileState();
      if (connected && this.assessment?.ready) {
        // Compatibility mode changes the fallback picker, not the configured
        // installation's automatic launch after Play opens this panel.
        return launchConnectedGame();
      }
      await this.restoreRememberedFolder();
    }).catch(async error => {
      if (this.closed) return;
      this.localInstallPending = false;
      if (localInstallGeneration !== this.restoreGeneration) return;
      this.renderFileState();
      // Only surface our validation messages. Browser JSON errors can include
      // response contents, so report their type without leaking paths or data.
      const detail = /^The local KOTOR [A-Za-z .]+(?:\(HTTP \d{3}\))?\.$/.test(error?.message || '')
        ? error.message : `${['TypeError', 'SyntaxError', 'SecurityError', 'OperationError', 'NotSupportedError'].includes(error?.name) ? error.name : 'Error'} while reading or validating the local installation index.`;
      const report = () => {
        this.status.textContent = `LOCAL_INSTALL_CONNECTION_FAILED: ${detail} Choose your KOTOR installation folder to connect the game files.`;
        this.status.className = 'status bad'; this.status.setAttribute('role', 'alert');
      };
      report();
      const restore = this.restoreRememberedFolder();
      const fallbackGeneration = this.restoreGeneration;
      await restore;
      if (!this.closed && fallbackGeneration === this.restoreGeneration && !this.assessment?.ready) report();
    });
    this.onClose = () => { this.dispose().catch(() => {}); };
    addEventListener('pagehide', this.onClose, { once: true });
  }
  async restoreRememberedFolder() {
    const generation = ++this.restoreGeneration;
    try {
      const handle = await folderHandle('get');
      if (!handle || handle.kind !== 'directory' || this.closed || generation !== this.restoreGeneration) return;
      this.rememberedHandle = handle;
      this.savedFolder = true;
      this.renderFileState();
      const permission = await handle.queryPermission({ mode: 'read' });
      if (permission === 'granted' && !this.closed && generation === this.restoreGeneration) {
        this.status.textContent = 'Reconnecting to your saved KOTOR folder…';
        const entries = await directoryFiles(handle);
        if (!this.closed && generation === this.restoreGeneration) await this.stageFiles(entries, { replace: true, handle, remembered: true, generation });
      }
      if (!this.closed && generation === this.restoreGeneration) this.renderFileState();
    } catch {
      // An inaccessible/stale handle is retained until the user chooses a replacement.
      if (!this.closed && generation === this.restoreGeneration) { this.renderFileState(); this.status.textContent = 'Could not reopen the saved folder. Choose it again to refresh access.'; this.status.className = 'status bad'; this.status.setAttribute('role', 'alert'); }
    }
  }
  async connectLocalInstall(generation = this.restoreGeneration) {
    const current = () => !this.closed && generation === this.restoreGeneration;
    if (!LOCAL_AUTO_INSTALL || !current()) return false;
    // A local server can accept this request and then stall (for example
    // while its installation index is blocked on storage). Bound the wait so
    // the UI can return to its folder-picker fallback instead of hiding it
    // indefinitely. The generation guard below and in the caller ensures a
    // timeout from an obsolete session cannot overwrite newer folder state.
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 15000);
    let response, manifest;
    try {
      response = await fetch('/private/install.json', {
        cache:'no-store', credentials:'same-origin', signal:controller.signal,
      });
      if (response.status === 404 || !current()) return false;
      if (!response.ok) throw new Error(`The local KOTOR installation service could not be reached (HTTP ${response.status}).`);
      manifest = await response.json();
    } catch (error) {
      if (!current()) return false;
      if (controller.signal.aborted) throw new Error('The local KOTOR installation service timed out.');
      throw error;
    } finally {
      clearTimeout(timeout);
    }
    if (!current()) return false;
    if (manifest.schema !== 'telos.owned-game-assets/v1' || manifest.assetMode !== 'lazy' || !Array.isArray(manifest.files) || manifest.files.length > 100000 || !Array.isArray(manifest.writable)) throw new Error('The local KOTOR installation index is invalid.');
    const names = new Set();
    for (const file of manifest.files) {
      if (!file || typeof file.name !== 'string' || !Number.isSafeInteger(file.bytes) || file.bytes < 0 || typeof file.url !== 'string' || !/^\/private\/install\/\d+$/.test(file.url) || names.has(file.name.toLowerCase())) throw new Error('The local KOTOR installation index contains an invalid file.');
      names.add(file.name.toLowerCase());
    }
    const libraryProfile = manifest.libraryProfile;
    if (!libraryProfile || libraryProfile.schema !== 'telos.k1-library-profile/v1'
        || !['native', 'k1-bink-proxy-to-verified-real/v1'].includes(libraryProfile.id)
        || !/^[0-9a-f]{64}$/.test(libraryProfile.identity || '')
        || !Array.isArray(libraryProfile.replacements)) throw new Error('The local KOTOR library profile is invalid.');
    if (libraryProfile.id === 'native' && libraryProfile.replacements.length !== 0) throw new Error('The local KOTOR library profile is invalid.');
    if (libraryProfile.id === 'k1-bink-proxy-to-verified-real/v1') {
      const replacement = libraryProfile.replacements[0];
      const source = manifest.files.find(file => file.name.toLowerCase() === 'binkw32_real.dll');
      const target = manifest.files.find(file => file.name.toLowerCase() === 'binkw32.dll');
      if (libraryProfile.replacements.length !== 1 || !source || !target
          || replacement?.target !== 'binkw32.dll' || replacement.targetSha256 !== '0972fbfe3f25c5d05969dd3e9db4d0c8413dcfe53ef3d8cf0f01944d0f4be8d6'
          || replacement.source !== 'binkw32_real.dll' || replacement.sourceSha256 !== '2d0ae23a6175dc7b635c402a5e7e9542e923c0d1c376a8c5ef876ca0d5959d23'
          || target.bytes !== 104223 || source.bytes !== 375808 || replacement.bytes !== source.bytes
          || replacement.url !== source.url) throw new Error('The local KOTOR Bink compatibility mapping is invalid.');
    }
    const indexIdentityLines = [`library-profile\0${libraryProfile.id}\0${libraryProfile.identity}`,
      ...manifest.files.map(file => `${file.name}\0${file.bytes}\0${file.url}`)];
    const indexIdentityDigest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(indexIdentityLines.join('\n')));
    const indexIdentity = [...new Uint8Array(indexIdentityDigest)].map(value => value.toString(16).padStart(2, '0')).join('');
    if (manifest.indexIdentity !== indexIdentity) throw new Error('The local KOTOR installation index identity is invalid.');
    const localProfile = gameProfileById(manifest.gameProfile || 'k1');
    if (!localProfile) throw new Error('The local KOTOR installation index identifies an unsupported game profile.');
    const executable = localProfile.executable;
    if (!localProfile.requiredFiles.every(file => names.has(file.name)) || !names.has(executable)) throw new Error('The local KOTOR installation is missing files required by its game profile.');
    if (!current()) return false;
    this.localManifest = manifest;
    this.localGameProfile = localProfile;
    this.pendingFiles.clear();
    this.assessment = { ready:true, files:manifest.files, missing:[], missingBifs:[], keyError:null };
    this.renderFileState();
    return true;
  }
  async launchLocalOwned(manifest, generation, restoreGeneration) {
    const current = () => !this.closed && generation === this.generation && restoreGeneration === this.restoreGeneration;
    await this.retryRevocations();
    if (!current()) return false;
    const runtime = await selectedRuntime();
    if (!current()) return false;
    const gameProfile = this.localGameProfile || DEFAULT_GAME_PROFILE;
    const anchors = new Set([gameProfile.executable,'chitin.key','dialog.tlk']);
    const signature = [`library-profile\0${manifest.libraryProfile.id}\0${manifest.libraryProfile.identity}`,
      ...manifest.files.filter(file => anchors.has(file.name.toLowerCase())).map(file => `${file.name.toLowerCase()}\0${file.bytes}`)].join('\n');
    const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(signature));
    if (!current()) return false;
    const profile = [...new Uint8Array(digest)].map(n => n.toString(16).padStart(2, '0')).join('');
    const previous = this.session;
    if (previous) { await this.flushProfile(); if (!current()) return false; await this.releaseSession(previous); }
    if (!current()) return false;
    this.runtimeProfileReady = false;
    this.rejectGuestMemoryRequests(); this.guestMemorySequence = 0; this.runtimeGuestMemoryAvailable = false;
    const session = crypto.randomUUID();
    this.session = session; this.files = null; this.activeSession = null;
    this.runtimeGuestId = gameProfile.id; this.runtimeRelease = runtime.release; this.runtimeVmCapabilities = null; this.runtimeTasAvailable = false; this.runtimeVmStatus = 'starting'; this.runtimeVmStatusRequest = 0;
    this.publishRuntimeSession();
    const query = new URLSearchParams({ rootBase:runtime.root.base, root:runtime.rootFile, appBase:'/original-game/', app:`${gameProfile.archiveNamespace}-${profile}.zip`, p:gameProfile.executable, auto:'true', desktop:'false', resolution:'800x600', sound:'true', storage:runtimeStorageMode(), wasmModuleBroker:'0', ownedSession:session, localInstall:'1', parentOrigin:location.origin, libraryProfile:`${manifest.libraryProfile.id}:${manifest.libraryProfile.identity}`, ownedIndex:manifest.indexIdentity });
    const runtimeUrl = new URL(runtime.entry, location.href);
    if (LOCAL_AUTO_INSTALL && location.hostname === '127.0.0.1') runtimeUrl.hostname = 'localhost';
    // GL4ES defaults limited-NPOT hardware to CLAMP. Preserve desktop GL's
    // default REPEAT so GL4ES can expand repeating NPOT textures before upload.
    runtimeUrl.search = query.toString() + `&em-env=%22LIBGL_DEFAULTWRAP:0;${textureTraceRequested ? 'LIBGL_KPTAS_TEXTRACE:1;' : ''}%22${guestTraceRequested ? '&guestTrace=1' : ''}${ioTraceRequested ? '&ioTrace=1' : ''}${cpuAffinityRequested ? '&cpuAffinity=1' : ''}`;
    const frame = document.createElement('iframe'); frame.title = 'KOTOR'; frame.allow = 'cross-origin-isolated'; frame.src = runtimeUrl.href; frame.className = 'original-game-frame';
    this.resetTasControls();
    this.installTasBridge(frame, session);
    this.host.replaceChildren(frame);
    this.startup = new GameStartup(this.host, frame);
    this.root.classList.add('game-running'); this.setupDetails.open = false;
    this.status.textContent = `Starting KOTOR from ${manifest.files.length.toLocaleString()} files on this computer…`;
    this.renderFileState();
    return true;
  }
  serializeLifecycle(action) {
    const result = this.lifecycle.then(action);
    this.lifecycle = result.catch(() => {});
    return result;
  }
  persistFolderHandle(action, handle, generation = null) {
    const write = this.folderPersistence.catch(() => {}).then(async () => {
      if (this.closed || (generation !== null && generation !== this.restoreGeneration)) return false;
      await folderHandle(action, handle);
      return true;
    });
    this.folderPersistence = write.catch(() => {});
    return write;
  }
  launch(input) {
    if (this.closed) return Promise.resolve(false);
    const generation = ++this.generation;
    const restoreGeneration = this.restoreGeneration;
    return this.serializeLifecycle(() => this.launchOwned(input, generation, restoreGeneration));
  }
  async launchOwned(input, generation, restoreGeneration) {
    const current = () => !this.closed && generation === this.generation && restoreGeneration === this.restoreGeneration;
    if (!current()) return false;
    if (this.localManifest) return this.launchLocalOwned(this.localManifest, generation, restoreGeneration);
    if (this.compatibleExecutable && input.some(entry => entry.name.toLowerCase() === 'swkotor2.exe')) {
      throw new Error('The alternate executable option is for KOTOR 1. Clear it before launching the separate KOTOR 2 installation.');
    }
    await this.retryRevocations();
    if (!current()) return false;
    // Keep the alternate executable independent from the connected resource
    // folder, then expose it to the runtime at the game's canonical path.
    // Validate the composed list once: inspect checks every BIF in chitin.key,
    // so a second resource-only pass would repeat a full large-folder scan.
    const launchInput = this.compatibleExecutable
      ? [...input.filter(entry => entry.name.toLowerCase() !== 'swkotor.exe'), this.compatibleExecutable]
      : input;
    const inspected = await inspect(launchInput);
    if (!inspected.ready) throw new Error(inspected.profileError || 'Required game files are missing or invalid. Review the checklist above.');
    const files = inspected.files;
    const gameProfile = inspected.profile;
    const runtime = await selectedRuntime();
    if (!current()) return false;
    // Optional resource additions do not create a new save profile. This is a
    // local installation identifier, not proof of ownership or exact bytes.
    const anchors = new Set([gameProfile.executable, 'chitin.key', 'dialog.tlk']);
    const signature = files.filter(({ name }) => anchors.has(name.toLowerCase())).map(({ name, file }) => `${name.toLowerCase()}\0${file.size}\0${file.lastModified}`).join('\n');
    const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(signature));
    const profile = [...new Uint8Array(digest)].map(n => n.toString(16).padStart(2, '0')).join('');
    // Pin a protocol version in the script URL. A prior page can leave an
    // older active worker behind even after the new script has installed;
    // checking only registration.active would then authorize metadata using
    // an incompatible worker (the old worker expected File objects).
    const workerScript = new URL('/original-game/owned-game-sw.js?v=2-metadata-snapshots', location.href).href;
    const registration = await navigator.serviceWorker.register(workerScript, { scope: '/original-game/', updateViaCache: 'none' });
    await registration.update();
    const worker = await waitForOwnedWorker(registration, workerScript);
    if (!current()) return false;
      this.rejectGuestMemoryRequests(); this.guestMemorySequence = 0; this.runtimeGuestMemoryAvailable = false;
      const session = crypto.randomUUID();
    const candidate = { worker, session, leasePort: null, workerReleased: false, leaseReleased: true, release: null };
    this.candidate = candidate;
    let installed = false;
    try {
      // The runtime receives the File snapshots directly from this page after
      // reading the manifest. The Service Worker only needs the compact index;
      // cloning thousands of File objects through a SharedWorker first caused
      // nondeterministic startup stalls and lease timeouts.
      const descriptors = files.map(({ name, file }) => ({ name, bytes: file.size, modified: file.lastModified }));
      await message(worker, { type: 'authorize', session, files: descriptors });
      if (!current()) return false;
      const previous = this.session;
      if (previous) {
        await this.flushProfile();
        if (!current()) return false;
        await this.releaseSession(previous);
        if (!current()) return false;
      }
      this.runtimeProfileReady = false;
      this.worker = worker;
      this.session = session;
      this.runtimeGuestId = gameProfile.id; this.runtimeRelease = runtime.release; this.runtimeVmCapabilities = null; this.runtimeTasAvailable = false; this.runtimeVmStatus = 'starting'; this.runtimeVmStatusRequest = 0;
      this.publishRuntimeSession();
      this.activeSession = candidate;
      this.files = files;
      this.renderFileState();
      const query = new URLSearchParams({ rootBase: runtime.root.base, root: runtime.rootFile, appBase: '/original-game/', app: `${gameProfile.archiveNamespace}-${profile}.zip`, p: gameProfile.executable, auto: 'true', desktop: 'false', resolution: '800x600', sound: 'true', storage: runtimeStorageMode(), wasmModuleBroker: '0', ownedSession: session, parentOrigin: location.origin });
      const frame = document.createElement('iframe'); frame.title = 'KOTOR'; frame.src = `${runtime.entry}?${query}${textureTraceRequested ? '&em-env=%22LIBGL_DEFAULTWRAP:0;LIBGL_KPTAS_TEXTRACE:1;%22' : ''}`;
      frame.className = 'original-game-frame';
      this.resetTasControls();
      this.installTasBridge(frame, session);
      this.host.replaceChildren(frame);
      this.startup = new GameStartup(this.host, frame);
      this.root.classList.add('game-running'); this.setupDetails.open = false;
        this.status.textContent = `Launching KOTOR with ${files.length.toLocaleString()} installation files…`;
      installed = true;
      return true;
    } finally {
      try {
        if (!installed) {
          // Queue both revocations before awaiting. Keep failed cleanup owned
          // independently of the next launch candidate so it remains retryable.
          await this.releaseCandidate(candidate);
        }
      } finally {
        if (this.candidate === candidate) this.candidate = null;
      }
    }
  }
  releaseCandidate(candidate) {
    this.pendingRevocations.set(candidate.session, candidate);
    if (candidate.release) return candidate.release;
    const cleanup = [];
    if (!candidate.workerReleased) cleanup.push(message(candidate.worker, { type: 'release', session: candidate.session }).then(() => { candidate.workerReleased = true; }));
    if (!candidate.leaseReleased) cleanup.push(this.leaseMessage({ type: 'release', session: candidate.session }, candidate.leasePort).then(() => { candidate.leaseReleased = true; }));
    candidate.release = Promise.allSettled(cleanup).then(released => {
      if (released.some(result => result.status === 'rejected')) {
        throw new Error('Could not disconnect KOTOR from the selected files. Choose Close game again to retry.');
      }
      if (this.pendingRevocations.get(candidate.session) === candidate) this.pendingRevocations.delete(candidate.session);
    }).finally(() => { candidate.release = null; });
    return candidate.release;
  }
  async retryRevocations() {
    const released = await Promise.allSettled([...this.pendingRevocations.values()].map(candidate => this.releaseCandidate(candidate)));
    if (released.some(result => result.status === 'rejected')) throw new Error('Could not disconnect KOTOR from the selected files. Choose Close game again to retry.');
  }
  closeGame({ saveProfile = true } = {}) {
    if (this.closed) return Promise.resolve(false);
    const generation = ++this.generation;
    return this.serializeLifecycle(async () => {
      if (this.closed || generation !== this.generation) return false;
      const session = this.session;
      if (saveProfile) await this.flushProfile();
      if (this.closed || generation !== this.generation) return false;
      await this.releaseSession(session);
      return !this.closed && generation === this.generation;
    });
  }
  async releaseSession(expected = this.session) {
    if (expected !== this.session) return;
    this.clearTasPending();
    this.releaseTasInputs();
    const waitingInput = this.tasWaitingInput;
    this.tasWaitingInput = null;
    if (waitingInput) dispatchEvent(new CustomEvent('telos:wasm-pending', { detail:{ pending:false, tick:waitingInput.tick } }));
    this.tasBridgeReady = false;
    this.runtimeProfileReady = false;
    this.host?.replaceChildren();
    this.startup = null;
    this.root.classList.remove('game-running'); this.setupDetails.open = true;
    if (this.activeSession) this.pendingRevocations.set(this.activeSession.session, this.activeSession);
    dispatchEvent(new CustomEvent('telos:runtime-session', { detail:null }));
    this.rejectGuestMemoryRequests(); this.runtimeGuestMemoryAvailable = false;
    this.session = null;
    this.runtimeGuestId = null; this.runtimeRelease = null; this.runtimeVmCapabilities = null; this.runtimeTasAvailable = false; this.runtimeVmStatus = 'idle';
    this.resetTasControls();
    window.__telosRuntimeDiagnostics = null;
    this.activeSession = null;
    this.files = null;
    if (!this.closed) this.renderFileState?.();
    await this.retryRevocations();
  }
  flushProfile() {
    const frame = this.host?.querySelector('iframe');
    if (!frame || !this.runtimeProfileReady) return Promise.resolve();
    return new Promise((resolve, reject) => {
      const request = crypto.randomUUID();
      const finish = event => {
        if (event.source !== frame.contentWindow || event.origin !== this.runtimeOrigin(frame) || event.data?.type !== 'telos-profile-flushed' || event.data.request !== request) return;
        clearTimeout(timeout); removeEventListener('message', finish);
        event.data.ok ? resolve() : reject(new Error('The profile could not be saved. Keep the game open and retry.'));
      };
      const timeout = setTimeout(() => { removeEventListener('message', finish); reject(new Error('The game did not acknowledge the profile save. It remains open.')); }, 10000);
      addEventListener('message', finish);
      frame.contentWindow.postMessage({ type:'telos-flush-profile', request }, this.runtimeOrigin(frame));
    });
  }
  leaseMessage(value, port = this.lease?.port) {
    if (!port) return Promise.reject(new Error('Local file lease is not available.'));
    return new Promise((resolve, reject) => {
      const request = crypto.randomUUID();
      const cleanup = () => { clearTimeout(timeout); port.removeEventListener('message', finish); };
      const finish = event => { if (event.data?.request !== request) return; cleanup(); event.data.ok ? resolve() : reject(new Error(event.data.error || 'Local file lease failed.')); };
      const timeout = setTimeout(() => { cleanup(); reject(new Error('Local file lease did not respond.')); }, 10000);
      port.addEventListener('message', finish);
      try { port.postMessage({ ...value, request }); }
      catch (error) { cleanup(); reject(error); }
    });
  }
  dispose() {
    if (this.disposing) return this.disposing;
    this.closed = true; ++this.generation;
    this.clearTasPending();
    if (this.onClose) removeEventListener('pagehide', this.onClose);
    if (this.receiveStatus) removeEventListener('message', this.receiveStatus);
    removeEventListener('telos:wasm-target', this.receiveTasTarget);
    removeEventListener('telos:wasm-input', this.receiveTasInput);
    removeEventListener('telos:wasm-release', this.receiveTasRelease);
    removeEventListener('telos:wasm-vm-control', this.receiveTasVmControl);
    removeEventListener('blur', this.onPageBlur);
    document.removeEventListener('visibilitychange', this.onVisibilityChange);
    // pagehide must dispatch revocation now, not after an in-flight flush or
    // authorization timeout. Generation checks stop its cancelled continuation.
    if (this.candidate) this.pendingRevocations.set(this.candidate.session, this.candidate);
    this.pendingFiles?.clear();
    const port = this.lease?.port;
    this.disposing = this.releaseSession().then(() => {
      // Failed releases retain their original port for a later dispose retry.
      // Concurrent dispose calls share this promise and cannot close it early.
      port?.close();
      if (this.lease?.port === port) this.lease = null;
    }).finally(() => { this.disposing = null; });
    return this.disposing;
  }
}
