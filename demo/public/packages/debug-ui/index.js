import { DEBUG_MANIFEST as DEBUG_RUNTIME_MANIFEST } from '../debug-runtime/manifest.js';
import { createMemoryTools } from './memory-tools.js';

export const DEBUG_MANIFEST = Object.freeze({
  ...DEBUG_RUNTIME_MANIFEST,
  provides: Object.freeze(['debug.execution', 'debug.memory', 'debug.watches', 'debug.processes', 'debug.scan', 'debug.strings', 'debug.workspace']),
  description: 'Inspect the running game, control execution, capture memory, and follow watched addresses in dockable debugger tools.',
});

const TOOL_VIEWS = [
  ['registers', 'Registers', 'registers', 'getGuestRegisters', ['Register', 'Value']],
  ['disassembly', 'Disassembly', 'disassembly', 'getGuestDisassembly', ['Address', 'Bytes', 'Instruction']],
  ['stack', 'Stack', 'stack', 'getGuestStack', ['Address', 'Value', 'Symbol']],
  ['threads', 'Threads', 'threadList', 'getGuestThreads', ['Thread', 'State', 'Location']],
  ['modules', 'Modules', 'modules', 'getGuestModules', ['Module', 'Base', 'Size']],
  ['breakpoints', 'Breakpoints', 'breakpoints', 'getGuestBreakpoints', ['Address', 'Kind', 'State']],
];
const TYPES = { u8: [1, 'getUint8'], u16: [2, 'getUint16'], u32: [4, 'getUint32'], i32: [4, 'getInt32'], f32: [4, 'getFloat32'], f64: [8, 'getFloat64'] };
const node = (tag, text = '', cls = '') => {
  const value = document.createElement(tag); value.textContent = text;
  if (cls) value.className = cls;
  return value;
};
const button = (text, handler) => {
  const value = node('button', text, 'btn small'); value.type = 'button';
  value.addEventListener('click', handler); return value;
};
const setText = (element, text) => { if (element.textContent !== text) element.textContent = text; };
const formatAddress = address => `0x${address.toString(16).padStart(8, '0')}`;

export function createDebugUiLifecycle() {
  let workspace;
  return {
    activate(context) {
      workspace ||= createDebugWorkspace(context.service('debug.runtime'), context.storage);
      workspace.setEnabled(true);
    },
    deactivate() { workspace?.setEnabled(false); },
  };
}

function createDebugWorkspace(runtime, storage) {
  const root = document.getElementById('cockpit');
  if (!document.querySelector('link[data-debug-tools-style]')) {
    const style = document.createElement('link'); style.rel = 'stylesheet';
    style.href = new URL('./style.css', import.meta.url).href; style.dataset.debugToolsStyle = ''; document.head.append(style);
  }
  const panels = new Map();
  const dockAvailability = new Map();
  let availabilityDock = null;
  let enabled = false, session = null, descriptor = null, epoch = 0, pid = null;
  let processes = [], processBusy = false, watchBusy = false, timer = null, workbench = null;
  let watches = [];
  try {
    const saved = storage.get('watches');
    if (Array.isArray(saved)) watches = saved.filter(value => value && typeof value.name === 'string' && value.name.length <= 64 &&
      Number.isSafeInteger(value.address) && value.address > 0 && TYPES[value.type] && value.address + TYPES[value.type][0] <= 0x100000000)
      .slice(0, 32).map(value => ({ name: value.name, address: value.address, type: value.type, value: null, changed: false }));
  } catch { /* An unavailable preference store must not block inspection. */ }
  const panel = (id, title) => {
    const section = node('section', '', 'debug-tool');
    section.dataset.telosDockWidget = `debug-${id}`;
    section.dataset.telosDockTitle = title;
    section.dataset.extensionOwner = DEBUG_MANIFEST.id;
    section.hidden = true;
    const body = node('div', '', 'debug-tool-body');
    section.append(body); root.append(section); panels.set(`debug-${id}`, section);
    return body;
  };
  const statusLine = text => { const item = node('p', text, 'debug-tool-status'); item.setAttribute('role', 'status'); return item; };
  const table = (headings, label) => {
    const element = node('table', '', 'debug-tool-table'); element.setAttribute('aria-label', label);
    const head = node('thead'), row = node('tr');
    for (const label of headings) { const cell = node('th', label); cell.scope = 'col'; row.append(cell); }
    head.append(row); const body = node('tbody'); element.append(head, body); return { element, body };
  };
  const sessionPanel = panel('session', 'Execution');
  const controls = node('div', '', 'debug-tool-actions');
  const control = (text, action) => button(text, () => {
    syncSession(); render();
    const operation = action === 'frame' ? 'step' : action;
    if (!available(operation) || !session) return;
    dispatchEvent(new CustomEvent('telos:wasm-vm-control', { detail: { action } }));
  });
  const pause = control('Pause', 'pause'), resume = control('Resume', 'resume'), step = control('Step image', 'frame');
  step.title = 'Advance to the next rendered image; this is separate from stepping a CPU instruction.';
  controls.append(pause, resume, step);
  const execution = statusLine('Connect a game to inspect execution.');
  const metrics = node('dl', '', 'debug-tool-metrics');
  const fields = new Map();
  for (const label of ['Target', 'State', 'Boundary', 'Presented images', 'Parked workers']) {
    const row = node('div'), value = node('dd', '—'); row.append(node('dt', label), value); fields.set(label, value); metrics.append(row);
  }
  const tools = node('div', '', 'debug-tool-actions'), toolButtons = new Map();
  for (const [id, label] of [['debugger', 'Memory & diagnostics'], ['debug-watches', 'Watches'], ['debug-processes', 'Processes'], ['debug-scan', 'Value scanner'], ['debug-strings', 'Strings']]) {
    const open = button(label, () => { if (enabled) window.TelosDock?.show(id); });
    toolButtons.set(id, open); tools.append(open);
  }
  sessionPanel.append(controls, execution, metrics, tools);

  const processPanel = panel('processes', 'Processes');
  const processRefresh = button('Refresh processes', async () => {
    syncSession(); if (!memoryReady() || typeof runtime.getGame()?.getGuestProcesses !== 'function' || processBusy) return;
    const ticket = epoch; processBusy = true; processMessage.textContent = 'Reading paused process list…'; render();
    try {
      const result = await runtime.getGame().getGuestProcesses();
      if (ticket !== epoch || !enabled || session !== runtime.getGame()?.session) return;
      if (!Array.isArray(result) || result.length > 4096 || result.some(item => !Number.isSafeInteger(item?.pid) || item.pid <= 0 || item.pid > 0xffffffff)) throw new Error('Invalid process-list response.');
      processes = result;
      if (!processes.some(item => item.pid === pid)) {
        pid = processes[0]?.pid || null; epoch++; watchBusy = false; processBusy = false;
        for (const watch of watches) { watch.value = null; watch.changed = false; watch.error = ''; }
        for (const view of views) view.clear();
        memoryTools.clear(); renderWatches(); render();
      }
      renderProcesses(); processMessage.textContent = `${processes.length} processes captured. Select the process for watches.`;
    } catch (error) { if (ticket === epoch) processMessage.textContent = `Process read failed: ${error.message || error}`; }
    finally { if (ticket === epoch) { processBusy = false; render(); } }
  });
  const processMessage = statusLine('Pause the game to list its processes.');
  const processTable = table(['Use', 'Process ID'], 'Captured guest processes');
  processPanel.append(processRefresh, processMessage, processTable.element);

  const watchPanel = panel('watches', 'Watches');
  const watchForm = node('form', '', 'debug-watch-form');
  const nameInput = node('input'); nameInput.maxLength = 64; nameInput.placeholder = 'Name'; nameInput.setAttribute('aria-label', 'Watch name');
  const addressInput = node('input'); addressInput.required = true; addressInput.placeholder = '0x00400000'; addressInput.spellcheck = false; addressInput.setAttribute('aria-label', 'Guest address in hexadecimal');
  const typeInput = node('select'); typeInput.setAttribute('aria-label', 'Value type, little endian');
  for (const type of Object.keys(TYPES)) { const option = node('option', type); option.value = type; typeInput.append(option); }
  typeInput.value = 'u32';
  const add = node('button', 'Add watch', 'btn small'); add.type = 'submit';
  watchForm.append(nameInput, addressInput, typeInput, add);
  const watchRefresh = button('Capture values', async () => {
    syncSession(); if (!memoryReady() || !pid || !watches.length || watchBusy) return;
    const ticket = epoch, capturePid = pid, captureSession = session, captureGame = runtime.getGame(), capturedWatches = [...watches];
    watchBusy = true; watchMessage.textContent = 'Capturing watched values…'; render();
    const results = await Promise.allSettled(capturedWatches.map(watch => Promise.resolve().then(() => captureGame.readGuestMemory(capturePid, watch.address, TYPES[watch.type][0]))));
    if (ticket !== epoch || !enabled || capturePid !== pid || captureSession !== runtime.getGame()?.session) return;
    if (!memoryReady()) { watchBusy = false; watchMessage.textContent = 'The target resumed before capture completed. Pause and capture values again.'; render(); return; }
    let errors = 0;
    results.forEach((result, index) => {
      const watch = capturedWatches[index]; if (!watches.includes(watch)) return;
      if (result.status === 'rejected') { watch.error = result.reason?.message || 'Read failed'; errors++; return; }
      const bytes = result.value, [size, method] = TYPES[watch.type];
      if (!(bytes instanceof Uint8Array) || bytes.length !== size) { watch.error = 'Incomplete memory response'; errors++; return; }
      const value = String(new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength)[method](0, true));
      watch.changed = watch.value !== null && watch.value !== value; watch.value = value; watch.error = '';
    });
    watchBusy = false; renderWatches(); render();
    watchMessage.textContent = `${new Date().toLocaleTimeString()} · PID ${capturePid} · ${results.length - errors} values captured${errors ? `; ${errors} reads failed` : ''}. Values are snapshots, updated on capture.`;
  });
  const watchMessage = statusLine('Add addresses, then pause and select a process to capture values. Definitions stay saved with this package.');
  const watchTable = table(['Name', 'Address', 'Type', 'Captured value', ''], 'Watched guest addresses');
  watchPanel.append(watchForm, watchRefresh, watchMessage, watchTable.element);
  const saveWatches = () => {
    try { storage.set('watches', watches.map(({ name, address, type }) => ({ name, address, type }))); }
    catch { watchMessage.textContent = 'Watch definitions apply to this visit; browser storage is unavailable.'; }
  };
  watchForm.addEventListener('submit', event => {
    event.preventDefault(); if (!enabled) return;
    const text = addressInput.value.trim();
    const address = /^(?:0x)?[0-9a-f]{1,8}$/i.test(text) ? parseInt(text.replace(/^0x/i, ''), 16) : 0;
    if (!address || address + TYPES[typeInput.value][0] > 0x100000000 || watches.length >= 32) {
      watchMessage.textContent = watches.length >= 32 ? 'Up to 32 watch definitions can be saved.' : 'Enter a nonzero 32-bit hexadecimal address with room for the value.';
      addressInput.setAttribute('aria-invalid', 'true'); addressInput.focus(); return;
    }
    addressInput.removeAttribute('aria-invalid');
    watches.push({ name: nameInput.value.trim() || formatAddress(address), address, type: typeInput.value, value: null, changed: false });
    saveWatches(); renderWatches(); render(); addressInput.value = ''; nameInput.value = ''; addressInput.focus();
  });

  const memoryTools = createMemoryTools({ panel, table,
    context: () => ({ enabled, epoch, session: runtime.getGame()?.session || null, pid, game: runtime.getGame(),
      supported: available('memoryRead') && typeof runtime.getGame()?.readGuestMemory === 'function', ready: memoryReady() }),
    addWatch(address, type) {
      if (!enabled || !address || watches.length >= 32 || watches.some(watch => watch.address === address && watch.type === type)) return false;
      watches.push({ name: formatAddress(address), address, type, value: null, changed: false });
      saveWatches(); renderWatches(); render(); return true;
    },
  });

  // View registrations define the front-end contract without fabricating CPU
  // state. A target adapter must advertise the operation AND supply its reader.
  const views = TOOL_VIEWS.map(([id, title, capability, method, headings]) => {
    const body = panel(id, title), message = statusLine('Connect a debugger-capable target to inspect this view.');
    const data = table(headings, `${title} from the attached process`);
    let busy = false, request = 0;
    const refresh = button('Refresh', async () => {
      syncSession(); if (!available(capability) || typeof runtime.getGame()?.[method] !== 'function' || busy || !pid || state()?.state !== 'paused') return;
      const ticket = epoch, capturePid = pid, read = ++request; busy = true; update();
      try {
        const rows = await runtime.getGame()[method](capturePid);
        if (ticket !== epoch || read !== request || !enabled || capturePid !== pid || session !== runtime.getGame()?.session) return;
        if (!Array.isArray(rows) || rows.length > 4096 || rows.some(row => !Array.isArray(row) || row.length !== headings.length || row.some(value => !['string', 'number', 'boolean'].includes(typeof value)))) throw new Error('The adapter returned an invalid row list.');
        data.body.replaceChildren(...rows.map(values => { const row = node('tr'); row.append(...values.map(value => node('td', String(value).slice(0, 2048)))); return row; }));
        message.textContent = `${rows.length} rows captured · PID ${capturePid}.`;
      } catch (error) { if (ticket === epoch) message.textContent = `Read failed: ${error.message || error}`; }
      finally { if (read === request) { busy = false; update(); } }
    });
    const update = () => {
      const connected = session && descriptor;
      const supported = connected && available(capability) && typeof runtime.getGame()?.[method] === 'function';
      refresh.disabled = !enabled || !supported || !pid || busy || state()?.state !== 'paused';
      if (!supported) setText(message, !connected ? 'Connect a game to inspect this view.' : `${title} are unavailable for the connected target.`);
      else if (!pid) setText(message, 'Select a process in Processes.');
    };
    body.append(refresh, message, data.element);
    return { update, cancel() { request++; busy = false; }, clear() { request++; busy = false; data.body.replaceChildren(); } };
  });

  function state() { const value = window.__telosRuntimeDiagnostics; return value?.session === session ? value : null; }
  function available(operation) { return enabled && descriptor?.debugger?.operations?.[operation]?.state === 'available'; }
  function memoryReady() {
    const value = state();
    return runtime.getGame()?.session === session && available('memoryRead') && typeof runtime.getGame()?.readGuestMemory === 'function' && value?.state === 'paused' && value.activeThreads > 0 && value.activeThreads === value.parkedThreads;
  }
  function syncSession() {
    const current = runtime.getGame()?.session || null;
    if (current === session) return;
    session = current; descriptor = null; epoch++; pid = null; processes = []; processBusy = false; watchBusy = false;
    for (const watch of watches) { watch.value = null; watch.changed = false; watch.error = ''; }
    for (const view of views) view.clear();
    memoryTools.clear();
    processMessage.textContent = 'Pause the game to list its processes.';
    watchMessage.textContent = 'Select a process and capture values for this session.';
    renderProcesses(); renderWatches();
  }
  function renderProcesses() {
    processTable.body.replaceChildren(...processes.map(process => {
      const row = node('tr'), select = button(process.pid === pid ? 'Selected' : 'Select', () => {
        if (pid !== process.pid) {
          epoch++; watchBusy = false; processBusy = false; pid = process.pid;
          for (const watch of watches) { watch.value = null; watch.changed = false; watch.error = ''; }
          for (const view of views) view.clear();
          memoryTools.clear();
          renderProcesses(); renderWatches(); render();
        }
      });
      select.setAttribute('aria-pressed', String(process.pid === pid));
      const cell = node('td'); cell.append(select); row.append(cell, node('td', String(process.pid))); return row;
    }));
  }
  function renderWatches() {
    watchTable.body.replaceChildren(...watches.map(watch => {
      const row = node('tr'); row.dataset.changed = String(watch.changed);
      row.append(node('td', watch.name), node('td', formatAddress(watch.address)), node('td', watch.type), node('td', watch.error || watch.value || '—'));
      const action = node('td'), remove = button('Remove', () => { watches = watches.filter(item => item !== watch); saveWatches(); renderWatches(); render(); });
      remove.setAttribute('aria-label', `Remove watch ${watch.name}`); action.append(remove); row.append(action); return row;
    }));
  }
  function render() {
    if (!enabled) return;
    syncSession(); const value = state();
    pause.disabled = !available('pause') || ['paused', 'pausing', 'error', 'stopped'].includes(value?.state);
    resume.disabled = !available('resume') || value?.state !== 'paused';
    step.disabled = !available('step') || value?.state !== 'paused';
    setText(fields.get('Target'), descriptor?.target?.profile || descriptor?.guestId || '—');
    setText(fields.get('State'), value?.state || descriptor?.status || 'Disconnected');
    setText(fields.get('Boundary'), descriptor?.executionBoundary === 'render-present' ? 'Render presentation' : descriptor?.executionBoundary || '—');
    setText(fields.get('Presented images'), Number.isSafeInteger(value?.presents) ? String(value.presents) : '—');
    setText(fields.get('Parked workers'), Number.isSafeInteger(value?.parkedThreads) ? `${value.parkedThreads} / ${value.activeThreads}` : '—');
    setText(execution, value?.error || (!session ? 'Connect a game to inspect execution.' : value?.state === 'paused' ? 'Paused · inspect memory or advance one image.' : 'Game execution follows the connected session.'));
    processRefresh.disabled = !memoryReady() || typeof runtime.getGame()?.getGuestProcesses !== 'function' || processBusy;
    watchRefresh.disabled = !memoryReady() || !pid || !watches.length || watchBusy;
    add.disabled = watches.length >= 32;
    for (const view of views) view.update();
    memoryTools.update();
    syncDockAvailability();
  }
  function syncDockAvailability() {
    const dock = window.TelosDock; if (!dock) return;
    if (availabilityDock !== dock) { availabilityDock = dock; dockAvailability.clear(); }
    const requirements = new Map(TOOL_VIEWS.map(([id, , capability, method]) => [`debug-${id}`, { capability, method }]));
    for (const id of memoryTools.ids) requirements.set(id, { capability: 'memoryRead', method: 'readGuestMemory' });
    requirements.set('debug-processes', { capability: 'memoryRead', method: 'getGuestProcesses' });
    requirements.set('debug-watches', { capability: 'memoryRead', method: 'readGuestMemory' });
    const changes = new Map([[true, []], [false, []]]);
    for (const id of ['debugger', ...panels.keys()]) {
      const requirement = requirements.get(id);
      const supported = enabled && (!requirement || (Boolean(session) && available(requirement.capability) &&
        typeof runtime.getGame()?.[requirement.method] === 'function'));
      if (toolButtons.has(id)) toolButtons.get(id).disabled = !supported;
      if (dockAvailability.get(id) === supported) continue;
      dockAvailability.set(id, supported); changes.get(supported).push(id);
    }
    // Keep unsupported adapter views out of the panel catalog as well as the
    // workspace. Saved layouts cannot turn a missing reader into a live tool.
    for (const [supported, ids] of changes) if (ids.length) dock.setAvailable(ids, supported);
  }
  const register = () => {
    const dock = window.TelosDock; if (!dock) return;
    for (const [id, section] of panels) if (!section.dataset.dockId) dock.register(id, section.dataset.telosDockTitle, section, { visible: false });
    syncDockAvailability();
  };
  addEventListener('telos:dock-ready', register);
  addEventListener('telos:runtime-session', event => {
    if (!enabled) return;
    syncSession(); const value = event.detail;
    if (value?.schema === 'telos-runtime-session/v1' && value.session === session) { descriptor = value; render(); }
  });
  register(); renderWatches();
  return { setEnabled(value) {
    enabled = value; document.body.dataset.debugExtension = value ? 'enabled' : 'disabled';
    if (enabled) {
      workbench ||= runtime.mountWorkbench({ isEnabled: () => enabled });
      runtime.getGame()?.publishRuntimeSession?.();
      timer ||= setInterval(() => { if (!document.hidden && [...panels.values()].some(panel => !panel.hidden)) render(); }, 500);
      render();
    } else { epoch++; processBusy = false; watchBusy = false; for (const view of views) view.cancel(); memoryTools.cancel(); clearInterval(timer); timer = null; }
    register();
  } };
}
