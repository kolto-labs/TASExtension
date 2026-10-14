import { DEBUG_MANIFEST as DEBUG_RUNTIME_MANIFEST } from '../debug-runtime/manifest.js';
import { DEBUG_REQUEST_SCHEMA, DEBUG_REPLY_SCHEMA } from '../debug-runtime/runtime.js';
import { validateExecutionCompletion } from '../debug-runtime/execution-completion.js';
import { createMemoryTools } from './memory-tools.js';
import { parseFileOffset, adjacentFileRange } from '../../debugger/file-byte-navigation.js';
import { declaredFileRange } from '../../debugger/declared-file-range.js';
import { normalizeMappedFileCapture } from '../../debugger/mapped-file-capture.js';
import { savedRegisterAddress } from '../../debugger/register-address.js';
import { describeCapturedGlCounter } from '../../debugger/gl-counter-description.js';
import { normalizeHostGlQueryOutputs, describeHostGlQueryOutputs } from '../../debugger/host-gl-query-outputs.js';
import { normalizeGuestGlErrorReplies, describeGuestGlErrorReplies } from '../../debugger/guest-gl-error-replies.js';

export const DEBUG_MANIFEST = Object.freeze({
  ...DEBUG_RUNTIME_MANIFEST,
  permissions:Object.freeze([...DEBUG_RUNTIME_MANIFEST.permissions, 'debug.file-inspection']),
  provides: Object.freeze(['debug.execution', 'debug.memory', 'debug.watches', 'debug.processes', 'debug.scan', 'debug.strings', 'debug.workspace']),
  description: 'Inspect the running game, control execution, capture memory, and follow watched addresses in dockable debugger tools.',
});

const TOOL_VIEWS = [
  ['registers', 'Registers', 'registers', 'registers.read', ['Thread', 'Captured state', 'Register', 'Saved value']],
  ['disassembly', 'Disassembly', 'disassembly', 'code.disassemble', ['Address', 'Bytes', 'Instruction']],
  ['stack', 'Stack', 'stack', 'stack.read', ['Address', 'Value', 'Symbol']],
  ['threads', 'Threads', 'threadList', 'threads.list', ['Thread', 'State', 'Location']],
  ['modules', 'Modules', 'modules', 'modules.list', ['Module', 'Base', 'Size']],
  ['address-mapping', 'Address backing file', 'addressMapping', 'memory.mapping', ['Field', 'Captured value']],
  ['breakpoints', 'Breakpoints', 'breakpoints', 'breakpoints.list', ['Address', 'Kind', 'State']],
  ['functions', 'Functions', 'functions', 'code.functions', ['Address', 'Name', 'Extent']],
  ['symbols', 'Symbols', 'symbols', 'symbols.list', ['Address', 'Name', 'Module']],
  ['references', 'Code references', 'references', 'code.references', ['From', 'To', 'Kind']],
  ['trace', 'Execution trace', 'trace', 'trace.read', ['Position', 'Unit', 'Address', 'Event']],
  ['events', 'Runtime events', 'events', 'events.read', ['Position', 'Unit', 'Kind', 'Details']],
  ['input-timeline', 'Input timeline', 'inputTimeline', 'input.timeline', ['Position', 'Unit', 'Input', 'State']],
  ['file-headers', 'File headers', 'fileHeaders', 'binary.identity', ['Field', 'Captured value']],
  ['file-sections', 'File sections', 'fileSections', 'binary.sections', ['Name', 'Kind / address basis', 'Address', 'File offset', 'File bytes', 'Virtual bytes', 'Permissions / flags']],
  ['file-dependencies', 'File dependencies', 'fileDependencies', 'binary.dependencies', ['Name', 'Inspection scope']],
  ['file-bytes', 'File bytes', 'fileBytes', 'binary.bytes', ['File offset', 'Hex bytes', 'Text']],
];
// Shared service routes are the capability authority for hosted backends.
const CAPABILITY_ROUTES = Object.freeze({
  pause:'execution.pause', resume:'execution.resume', step:'execution.step',
  memoryRead:'memory.read', processList:'processes.list',
  threadList:'threads.list', registers:'registers.read', addressMapping:'memory.mapping',
  disassembly:'code.disassemble', stack:'stack.read', modules:'modules.list', breakpoints:'breakpoints.list',
  functions:'code.functions', symbols:'symbols.list', references:'code.references',
  trace:'trace.read', events:'events.read', inputTimeline:'input.timeline',
  fileHeaders:'binary.identity', fileSections:'binary.sections', fileDependencies:'binary.dependencies',
  fileBytes:'binary.bytes',
});
const SNAPSHOT_SOURCES = new Set(['recorded-journal', 'simulator', 'static-analysis', 'captured-memory']);
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
      let fileInspection = null;
      try { fileInspection = context.service('debug.file-inspection'); } catch { /* Optional on other hosts. */ }
      workspace ||= createDebugWorkspace(context.service('debug.runtime'), context.storage, fileInspection);
      workspace.setEnabled(true);
    },
    deactivate() { workspace?.setEnabled(false); },
  };
}

function createDebugWorkspace(runtime, storage, fileInspection = null) {
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
  let requestSequence = 0;
  let controlAbort = null, controlMessage = '', controlEpoch = 0;
  let executionDisplay = null, executionCaptureUrl = null;
  function clearExecutionDownload() {
    if (executionCaptureUrl) URL.revokeObjectURL(executionCaptureUrl);
    executionCaptureUrl = null;
  }
  let watches = [];
  function getGame() {
    try { return runtime.getGame?.() || null; } catch { return null; }
  }
  function getSession() {
    try {
      const value = typeof runtime.getSession === 'function' ? runtime.getSession() : getGame()?.session;
      return typeof value === 'string' && value.length > 0 && value.length <= 128 ? value : null;
    } catch { return null; }
  }
  function capabilities() {
    try {
      const current = getSession(), value = runtime.capabilities?.();
      return current && value?.schema === 'telos.debug-adapter-status/v1' && value.session === current ? value : null;
    } catch { return null; }
  }
  function memoryModel() {
    try {
      const value = state();
      const target = typeof runtime.getTarget === 'function' ? runtime.getTarget()
        : value && Object.hasOwn(value, 'target') ? value.target : descriptor?.target;
      // Current address forms and replies use exact 32-bit numeric addresses.
      // A backend advertisement alone cannot qualify a different data layout.
      return target?.pointerBits === 32 && ['little', 'big'].includes(target.byteOrder)
        ? { pointerBits:32, byteOrder:target.byteOrder, targetDeclaration:target } : null;
    } catch { return null; }
  }
  async function requestDebug(operation, params = {}, options = {}) {
    const requestSession = getSession();
    if (!requestSession || typeof runtime.request !== 'function') throw new Error('The debugger request service is unavailable.');
    const id = `debug-ui-${++requestSequence}`;
    const reply = await runtime.request({ schema:DEBUG_REQUEST_SCHEMA, id,
      session:requestSession, operation, params }, options);
    if (reply?.schema !== DEBUG_REPLY_SCHEMA || reply.id !== id || reply.session !== requestSession
      || reply.operation !== operation || getSession() !== requestSession) {
      throw new Error('The game session changed or returned an invalid debugger reply.');
    }
    if (['execution.pause','execution.resume','execution.step'].includes(operation)) {
      if (reply.source !== 'live-runtime') throw new Error('Execution completion requires a live runtime reply.');
      validateExecutionCompletion(reply.data, { operation, session:requestSession, owner:reply.adapter, unit:params.unit, requestId:id });
    }
    return reply.data;
  }
  async function readMemory(pidValue, address, length) {
    const data = await requestDebug('memory.read', { pid:pidValue, address, length });
    if (data?.pid !== pidValue || data.address !== address || data.length !== length
      || !Array.isArray(data.bytes) || data.bytes.length !== length
      || data.bytes.some(byte => !Number.isInteger(byte) || byte < 0 || byte > 255)) {
      throw new Error('The debugger returned an invalid memory response.');
    }
    return Uint8Array.from(data.bytes);
  }
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
  const control = (text, action) => button(text, async () => {
    syncSession(); render();
    const operation = action === 'frame' ? 'step' : action;
    const route = `execution.${operation}`;
    if (!available(operation) || !session || controlAbort
      || capabilities()?.operations?.[route]?.state !== 'available') return;
    const ticket = controlEpoch, requestSession = session, controller = new AbortController();
    const stepUnit = capabilities()?.operations?.[route]?.unit;
    if (operation === 'step' && (typeof stepUnit !== 'string' || !stepUnit.length || stepUnit.length > 256 || stepUnit.trim() !== stepUnit)) return;
    controlAbort = controller;
    controlMessage = `${text} requested. Waiting for the game to confirm completion.`;
    render();
    try {
      const result = await requestDebug(route, operation === 'step' ? { unit:stepUnit } : {}, { signal:controller.signal });
      if (controller.signal.aborted || ticket !== controlEpoch || !enabled || session !== requestSession) return;
      controlMessage = operation === 'step' ? (result.unit === 'render-present' ? 'Advanced to a rendered image.' : `Step confirmed · ${result.unit}.`) : `${text} confirmed by the game runtime.`;
    } catch (error) {
      if (!controller.signal.aborted && ticket === controlEpoch && enabled && session === requestSession) controlMessage = `${text} was not confirmed: ${error.message || error}`;
    } finally {
      if (controlAbort === controller) controlAbort = null;
      if (ticket === controlEpoch && enabled) render();
    }
  });
  const pause = control('Pause', 'pause'), resume = control('Resume', 'resume'), step = control('Step image', 'frame');
  step.title = 'Advance to the next rendered image; this is separate from stepping a CPU instruction.';
  controls.append(pause, resume, step);
  const execution = statusLine('Connect a game to inspect execution.');
  const metrics = node('dl', '', 'debug-tool-metrics');
  const fields = new Map();
  for (const label of ['Target', 'State', 'Boundary', 'Presented images', 'Parked workers', 'Inspection sources', 'File range I/O', 'SDL proxy dispatch totals', 'Paused GL call counts', 'Sampled GL proxy elapsed', 'Guest-requested GL error replies', 'Host shader query outputs', 'Published dispatch samples']) {
    const row = node('div'), value = node('dd', '—'); row.append(node('dt', label), value); fields.set(label, value); metrics.append(row);
  }
  const glCounterLabel = node('label', 'Captured GL counter '), glCounterSelect = node('select');
  glCounterSelect.setAttribute('aria-label', 'Captured GL counter');
  glCounterLabel.append(glCounterSelect);
  const glCounterInfo = statusLine('No captured GL counter selected.');
  let glCounterOptionsKey = '';
  glCounterSelect.addEventListener('change', () => render());
  const tools = node('div', '', 'debug-tool-actions'), toolButtons = new Map();
  const exportExecution = button('Export execution view', () => {
    syncSession();
    const captured = executionDisplay;
    if (!enabled || !captured || captured.session !== session || getSession() !== session) return;
    const exportedAt = new Date().toISOString();
    const snapshot = new Blob([JSON.stringify({ schema:'telos.execution-display-capture/v1',
      ...captured, exportedAt,
      qualification:'Displayed status and validated paused counter snapshot. No backend request. This export does not prove execution completion, guest identity, audible playback, replay or platform support.'
    }, null, 2)], { type:'application/json' });
    if (snapshot.size > 64 * 1024) {
      setText(execution, 'Execution view exceeds the 64 KiB export limit.'); return;
    }
    clearExecutionDownload();
    executionCaptureUrl = URL.createObjectURL(snapshot);
    const link = node('a'); link.href = executionCaptureUrl;
    link.download = `debug-execution-${exportedAt.replace(/[:.]/g, '-')}.json`;
    sessionPanel.append(link); link.click(); link.remove();
  });
  tools.append(exportExecution);
  for (const [id, label] of [['debug-file-inspection', 'Inspect file'], ['debug-capabilities', 'Debugger capabilities'], ['debugger', 'Memory & diagnostics'], ['debug-watches', 'Watches'], ['debug-processes', 'Processes'], ['debug-threads', 'Threads'], ['debug-registers', 'Registers'], ['debug-address-mapping', 'Address backing file'], ['debug-scan', 'Value scanner'], ['debug-strings', 'Strings']]) {
    const open = button(label, () => { if (enabled) window.TelosDock?.show(id); });
    toolButtons.set(id, open); tools.append(open);
  }
  sessionPanel.append(controls, execution, metrics, glCounterLabel, glCounterInfo, tools);

  const filePanel = panel('file-inspection', 'Inspect file');
  const fileReady = () => ['open', 'close', 'cancelPending', 'getSelection'].every(method => typeof fileInspection?.[method] === 'function');
  const fileInput = node('input'); fileInput.type = 'file'; fileInput.setAttribute('aria-label', 'Select a local binary for static inspection');
  const fileMessage = statusLine('Choose a local binary. Inspection does not launch it or upload its bytes.');
  let fileBusy = false, fileRequest = 0;
  const returnToGame = button('Return to game debugger', () => {
    if (!enabled || !fileReady()) return;
    fileRequest++; fileBusy = false; fileInspection.close();
    fileInput.value = ''; setText(fileMessage, 'File inspection closed.'); render();
  });
  fileInput.addEventListener('change', async () => {
    if (!enabled || !fileReady() || !fileInput.files?.length) return;
    const ticket = ++fileRequest, file = fileInput.files[0];
    fileBusy = true; setText(fileMessage, 'Reading file metadata…'); render();
    try {
      await fileInspection.open(file);
      if (ticket !== fileRequest || !enabled) return;
      setText(fileMessage, 'File debugger selected. Open File headers, File sections, File dependencies or File bytes from Panels, then capture a view. Runtime compatibility remains unqualified.');
    } catch (error) {
      if (ticket === fileRequest && enabled) setText(fileMessage, `File inspection failed: ${error.message || error}`);
    } finally { if (ticket === fileRequest) { fileBusy = false; render(); } }
  });
  filePanel.append(fileInput, returnToGame, fileMessage);

  const capabilityPanel = panel('capabilities', 'Debugger capabilities');
  const capabilityMessage = statusLine('Connect a target to read its capability report.');
  const capabilityTable = table(['Operation', 'Reported state', 'Evidence source', 'Scope', 'Unit', 'Owner', 'Reason'], 'Selected debugger capability report');
  capabilityPanel.append(capabilityMessage,
    node('p', 'Available operations still require the target\'s capture boundary. This report does not verify a game, executable or platform.', 'muted'),
    capabilityTable.element);
  let capabilityRenderKey = null;
  function renderCapabilities() {
    // Inspect advertisements only. Opening this view sends no backend request.
    const report = capabilities(), states = {
      available:'Available', unsupported:'Unsupported', 'not-qualified':'Not qualified', unavailable:'Unavailable',
    };
    const text = value => typeof value === 'string' ? value.slice(0, 1024) : '';
    const entries = Object.entries(report?.operations || {}).filter(([operation]) =>
      operation.length <= 256 && /^[a-z][a-z0-9]*(?:\.[a-z][a-z0-9-]*){1,7}$/.test(operation)).sort(([a], [b]) => a.localeCompare(b));
    const rows = entries.slice(0, 256).map(([operation, value]) => [operation,
      Object.hasOwn(states, value?.state) ? states[value.state] : 'Unavailable',
      value?.source === 'live-runtime' || SNAPSHOT_SOURCES.has(value?.source) ? value.source : 'Unavailable',
      value?.scope === 'target' ? 'Target' : !Object.hasOwn(value || {}, 'scope') || value.scope === 'process' ? 'Process' : 'Unavailable',
      text(value?.unit) || 'Unavailable', text(value?.owner) || 'Unavailable', text(value?.reason) || 'No reason supplied']);
    const key = JSON.stringify([session, Boolean(report), rows, entries.length]);
    if (key === capabilityRenderKey) return;
    capabilityRenderKey = key;
    capabilityTable.body.replaceChildren(...rows.map(values => {
      const row = node('tr'); row.append(...values.map(value => node('td', value))); return row;
    }));
    setText(capabilityMessage, !report ? 'Connect a target to read its capability report.'
      : `Selected session ${session} · ${rows.length} operations reported.${entries.length > rows.length ? ' Only the first 256 operations are shown.' : ''}`);
  }

  const processPanel = panel('processes', 'Processes');
  const processRefresh = button('Refresh processes', async () => {
    syncSession(); if (!processReady() || processBusy) return;
    const ticket = epoch, source = capabilities()?.operations?.['processes.list']?.source;
    processBusy = true; processMessage.textContent = 'Reading the process list…'; render();
    try {
      const result = (await requestDebug('processes.list', {}))?.processes;
      if (ticket !== epoch || !enabled || session !== getSession()) return;
      if (!processReady() || capabilities()?.operations?.['processes.list']?.source !== source) throw new Error('The inspection boundary or evidence source changed before the process list completed.');
      if (!Array.isArray(result) || result.length > 4096 || result.some(item => !Number.isSafeInteger(item?.pid) || item.pid <= 0 || item.pid > 0xffffffff
        || (item.name != null && (typeof item.name !== 'string' || item.name.length > 1024 || /[\x00-\x1f\x7f/\\]/.test(item.name)))
        || (item.presentationOwner != null && typeof item.presentationOwner !== 'boolean'))
        || result.filter(item => item.presentationOwner === true).length > 1
        || new Set(result.map(item => item.pid)).size !== result.length) throw new Error('Invalid process-list response.');
      processes = result;
      if (!processes.some(item => item.pid === pid)) {
        pid = processes.find(item => item.presentationOwner === true)?.pid || processes[0]?.pid || null;
        epoch++; watchBusy = false; processBusy = false;
        for (const watch of watches) { watch.value = null; watch.changed = false; watch.error = ''; }
        for (const view of views) view.clear();
        memoryTools.clear(); renderWatches(); render();
      }
      renderProcesses(); processMessage.textContent = `${processes.length} processes captured. Select the process for watches.`;
    } catch (error) { if (ticket === epoch) processMessage.textContent = `Process read failed: ${error.message || error}`; }
    finally { if (ticket === epoch) { processBusy = false; render(); } }
  });
  const processMessage = statusLine('Pause the game to list its processes.');
  const processTable = table(['Use', 'Process ID', 'Executable name', 'Presentation owner'], 'Captured processes');
  processPanel.append(processRefresh, processMessage,
    node('p', 'Presentation ownership is a runtime association. It does not verify an executable or build.', 'muted'),
    processTable.element);

  const watchPanel = panel('watches', 'Watches');
  const watchForm = node('form', '', 'debug-watch-form');
  const nameInput = node('input'); nameInput.maxLength = 64; nameInput.placeholder = 'Name'; nameInput.setAttribute('aria-label', 'Watch name');
  const addressInput = node('input'); addressInput.required = true; addressInput.placeholder = '0x00400000'; addressInput.spellcheck = false; addressInput.setAttribute('aria-label', 'Guest address in hexadecimal');
  const typeInput = node('select'); typeInput.setAttribute('aria-label', 'Value type, target byte order');
  for (const type of Object.keys(TYPES)) { const option = node('option', type); option.value = type; typeInput.append(option); }
  typeInput.value = 'u32';
  const add = node('button', 'Add watch', 'btn small'); add.type = 'submit';
  watchForm.append(nameInput, addressInput, typeInput, add);
  const watchRefresh = button('Capture values', async () => {
    syncSession(); if (!memoryReady() || !pid || !watches.length || watchBusy) return;
    const ticket = epoch, capturePid = pid, captureSession = session, capturedWatches = [...watches], captureModel = memoryModel();
    const captureSource = capabilities()?.operations?.['memory.read']?.source;
    if (!captureModel) return;
    watchBusy = true; watchMessage.textContent = 'Capturing watched values…'; render();
    const results = await Promise.allSettled(capturedWatches.map(watch => Promise.resolve().then(() => readMemory(capturePid, watch.address, TYPES[watch.type][0]))));
    if (ticket !== epoch || !enabled || capturePid !== pid || captureSession !== getSession()) return;
    if (!memoryReady() || memoryModel()?.byteOrder !== captureModel?.byteOrder || capabilities()?.operations?.['memory.read']?.source !== captureSource) { watchBusy = false; watchMessage.textContent = 'The inspection boundary, evidence source or byte order changed. Capture values again.'; render(); return; }
    let errors = 0;
    results.forEach((result, index) => {
      const watch = capturedWatches[index]; if (!watches.includes(watch)) return;
      if (result.status === 'rejected') { watch.error = result.reason?.message || 'Read failed'; errors++; return; }
      const bytes = result.value, [size, method] = TYPES[watch.type];
      if (!(bytes instanceof Uint8Array) || bytes.length !== size) { watch.error = 'Incomplete memory response'; errors++; return; }
      const value = String(new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength)[method](0, captureModel.byteOrder === 'little'));
      watch.changed = watch.value !== null && watch.value !== value; watch.value = value; watch.error = '';
    });
    watchBusy = false; renderWatches(); render();
    watchMessage.textContent = `${new Date().toLocaleTimeString()} · PID ${capturePid} · ${captureSource} · ${captureModel.byteOrder} endian · ${results.length - errors} values captured${errors ? `; ${errors} reads failed` : ''}. Values are snapshots, updated on capture.`;
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
    context: () => {
      const model = memoryModel();
      return { enabled, epoch, session:getSession() || null, pid, game:getGame(), readMemory,
        pointerBits:model?.pointerBits ?? null, byteOrder:model?.byteOrder ?? null,
        targetDeclaration:model?.targetDeclaration ?? null,
        source:capabilities()?.operations?.['memory.read']?.source ?? null,
        supported:Boolean(model) && available('memoryRead') && typeof runtime.request === 'function'
          && capabilities()?.operations?.['memory.read']?.state === 'available', ready:memoryReady() };
    },
    addWatch(address, type) {
      if (!enabled || !address || watches.length >= 32 || watches.some(watch => watch.address === address && watch.type === type)) return false;
      watches.push({ name: formatAddress(address), address, type, value: null, changed: false });
      saveWatches(); renderWatches(); render(); return true;
    },
  });

  // View registrations define the front-end contract without fabricating CPU
  // state. A target adapter must advertise the operation AND supply its reader.
  const views = TOOL_VIEWS.map(([id, title, capability, operation, headings]) => {
    const body = panel(id, title), readiness = statusLine('Connect a debugger-capable target to inspect this view.');
    const message = statusLine('No capture for the selected target or process.');
    const data = table(headings, `${title} from the selected debugger target`);
    let busy = false, request = 0, captureDownload = null;
    let fileOffset = null, fileLength = null, fileCapture = null, previousBytes = null, nextBytes = null;
    let mappingAddress = null;
    let registerAddressActions = [];
    let sectionByteActions = [];
    if (id === 'address-mapping') {
      const label = node('label', 'Virtual address (hex) ');
      mappingAddress = node('input'); mappingAddress.type = 'text'; mappingAddress.value = '0x00400000'; mappingAddress.spellcheck = false;
      mappingAddress.setAttribute('aria-label', 'Backing-file virtual address');
      label.append(mappingAddress); body.append(label,
        node('p', 'Capture one address in the selected process. Backing-file metadata does not identify a loaded executable or list Windows modules.', 'muted'));
    }
    if (id === 'file-bytes') {
      const offsetLabel = node('label', 'File offset '), lengthLabel = node('label', 'Byte count ');
      fileOffset = node('input'); fileOffset.type = 'text'; fileOffset.value = '0'; fileOffset.spellcheck = false;
      fileOffset.setAttribute('aria-label', 'File byte offset');
      fileLength = node('input'); fileLength.type = 'number'; fileLength.min = '1'; fileLength.max = '4096'; fileLength.step = '1'; fileLength.value = '128';
      fileLength.setAttribute('aria-label', 'File byte count');
      offsetLabel.append(fileOffset); lengthLabel.append(fileLength); body.append(offsetLabel, lengthLabel,
        node('p', 'Read 1–4096 bytes from the selected file. Enter a decimal offset or 0x hexadecimal offset. Offsets are not process addresses.', 'muted'));
      const prepareRange = direction => {
        syncSession();
        if (!enabled || busy || fileCapture?.session !== getSession()) return;
        const range = adjacentFileRange(fileCapture, direction);
        if (!range) return;
        fileOffset.value = `0x${range.offset.toString(16)}`; fileLength.value = String(range.length);
        message.textContent = `Prepared ${range.length} bytes at ${fileOffset.value}. Capture file bytes to read this range. The table and export still contain the previous capture.`;
        fileOffset.focus();
      };
      previousBytes = button('Prepare previous file bytes', () => prepareRange(-1));
      nextBytes = button('Prepare next file bytes', () => prepareRange(1));
      previousBytes.disabled = true; nextBytes.disabled = true;
      body.append(previousBytes, nextBytes);
    }
    const clearDownload = () => {
      if (captureDownload) URL.revokeObjectURL(captureDownload.url);
      captureDownload = null;
    };
    const exportCapture = button(`Export ${title.toLowerCase()}`, () => {
      syncSession();
      if (!enabled || busy || !captureDownload || captureDownload.session !== getSession()) return;
      const link = node('a'); link.href = captureDownload.url;
      link.download = captureDownload.filename;
      body.append(link); link.click(); link.remove();
    });
    const targetScoped = () => !['registers', 'threads', 'address-mapping'].includes(id)
      && capabilities()?.operations?.[operation]?.scope === 'target'
      && SNAPSHOT_SOURCES.has(capabilities()?.operations?.[operation]?.source);
    const supportedReader = () => {
      const report = capabilities()?.operations?.[operation];
      return typeof runtime.request === 'function' && report?.state === 'available'
        && (report.scope === 'target' ? targetScoped() : !Object.hasOwn(report, 'scope') || report.scope === 'process');
    };
    const refresh = button(`Capture ${title.toLowerCase()}`, async () => {
      syncSession(); if (!available(capability) || !supportedReader() || busy || (!pid && !targetScoped()) || !captureReady(operation)) return;
      const ticket = epoch, capturePid = pid, read = ++request; busy = true; update();
      const captureSource = capabilities()?.operations?.[operation]?.source;
      const captureTargetScope = targetScoped();
      try {
        const captureParams = id === 'file-bytes'
          ? { offset:parseFileOffset(fileOffset.value),
            length:fileLength.value.trim() ? Number(fileLength.value) : NaN }
          : captureTargetScope ? {} : { pid:capturePid };
        if (id === 'address-mapping') {
          const text = mappingAddress.value.trim();
          if (!/^(?:0x)?[0-9a-f]{1,8}$/i.test(text)) throw new Error('Enter a 32-bit hexadecimal virtual address.');
          captureParams.address = parseInt(text.replace(/^0x/i, ''), 16);
        }
        if (id === 'file-bytes' && (!captureTargetScope
          || !Number.isSafeInteger(captureParams.offset) || captureParams.offset < 0
          || !Number.isSafeInteger(captureParams.length) || captureParams.length < 1 || captureParams.length > 4096)) {
          throw new Error('Choose a file offset and byte count from 1 to 4096.');
        }
        let rows, registerCaptureLabel = '', capturedFileSize = null, mappingCapture = null, sectionFileSize = null;
        if (id === 'registers') {
          const value = await requestDebug('registers.read', { pid:capturePid });
          if (value?.pid !== capturePid || value.captured !== true || !['saved-guest-registers','saved-target-registers'].includes(value.semantics)
            || typeof value.architecture !== 'string' || !/^[a-zA-Z0-9][a-zA-Z0-9._+-]{0,31}$/.test(value.architecture)
            || !Array.isArray(value.threads) || value.threads.length > 128
            || value.threads.some(thread => !Number.isSafeInteger(thread?.id) || thread.id < 1 || thread.id > 0xffffffff
              || thread.pid !== capturePid || (thread.state != null && !['kernel-wait','paused','stopped'].includes(thread.state))
              || !Array.isArray(thread.registers) || thread.registers.length > 32
              || thread.registers.some(register => typeof register?.name !== 'string' || !/^[a-zA-Z][a-zA-Z0-9._-]{0,31}$/.test(register.name)
                || typeof register.value !== 'string' || !/^0x[0-9a-f]{1,32}$/i.test(register.value))
              || new Set(thread.registers.map(register => register.name)).size !== thread.registers.length)
            || new Set(value.threads.map(thread => thread.id)).size !== value.threads.length) throw new Error('The adapter returned an invalid saved register capture.');
          registerCaptureLabel = ` ${value.architecture} · ${value.semantics === 'saved-guest-registers' ? 'Saved guest registers' : 'Saved target registers'}. Saved values do not identify a wait cause or a next instruction.`;
          rows = value.threads.flatMap(thread => thread.registers.map(register => [thread.id,
            ({ 'kernel-wait':'Kernel wait', paused:'Paused', stopped:'Stopped' })[thread.state] || 'Unavailable',
            register.name, register.value]));
        } else if (id === 'address-mapping') {
          mappingCapture = normalizeMappedFileCapture(await requestDebug(operation, captureParams), captureParams);
          rows = [['Requested address', formatAddress(mappingCapture.address)],
            ['Backing path', mappingCapture.path], ['Mapped start', formatAddress(mappingCapture.start)],
            ['Mapped extent (bytes)', mappingCapture.length], ['File offset', mappingCapture.fileOffset ?? 'Unavailable']];
          registerCaptureLabel = ' Backing-file metadata only. Offset zero is valid; unavailable offsets remain unknown.';
        } else if (id === 'threads') {
          const value = await requestDebug('threads.list', { pid:capturePid });
          if (value?.pid !== capturePid || value.captured !== true || value.locationAvailable !== false
            || !Array.isArray(value.threads) || value.threads.length > 4096
            || value.threads.some(thread => !Number.isSafeInteger(thread?.id) || thread.id < 1 || thread.id > 0xffffffff
              || thread.pid !== capturePid || (thread.state != null && !['kernel-wait','paused','stopped'].includes(thread.state)))
            || new Set(value.threads.map(thread => thread.id)).size !== value.threads.length) throw new Error('The adapter returned an invalid thread capture.');
          rows = value.threads.map(thread => [thread.id,
            ({ 'kernel-wait':'Kernel wait', paused:'Paused', stopped:'Stopped' })[thread.state] || 'Unavailable', 'Unavailable']);
        } else {
          const value = await requestDebug(operation, captureParams);
          if (!value || value.captured !== true || (captureTargetScope
            ? value.scope !== 'target' || Object.hasOwn(value, 'pid')
            : value.pid !== capturePid || (Object.hasOwn(value, 'scope') && value.scope !== 'process'))) throw new Error('The adapter returned an invalid scoped capture.');
          rows = value.rows;
          if (id === 'file-sections' && captureTargetScope && captureSource === 'static-analysis'
            && value.semantics === 'declared-file-ranges' && value.headerStatus === 'identified') {
            if (!Number.isSafeInteger(value.fileSize) || value.fileSize < 1) throw new Error('The section capture returned an invalid selected-file extent.');
            sectionFileSize = value.fileSize;
          }
          if (id === 'file-bytes' && (value.semantics !== 'selected-file-bytes'
            || value.offset !== captureParams.offset || value.length !== captureParams.length)) {
            throw new Error('The file byte capture does not match the requested range.');
          }
          if (id === 'file-bytes' && Object.hasOwn(value, 'fileSize')) {
            if (!Number.isSafeInteger(value.fileSize) || value.fileSize < 1
              || captureParams.offset > value.fileSize - captureParams.length) {
              throw new Error('The file byte capture returned an invalid file extent.');
            }
            capturedFileSize = value.fileSize;
          }
        }
        if (ticket !== epoch || read !== request || !enabled || capturePid !== pid || session !== getSession()) return;
        if (!captureReady(operation) || capabilities()?.operations?.[operation]?.source !== captureSource
          || targetScoped() !== captureTargetScope) throw new Error('The inspection boundary, evidence source or capture scope changed before this capture completed.');
        if (!Array.isArray(rows) || rows.length > 4096 || rows.some(row => !Array.isArray(row) || row.length !== headings.length || row.some(value => !['string', 'number', 'boolean'].includes(typeof value) || (typeof value === 'number' && !Number.isFinite(value))))) throw new Error('The adapter returned an invalid row list.');
        const displayedRows = rows.map(values => values.map(value => String(value).slice(0, id === 'address-mapping' ? 4096 : 2048)));
        const receivedAt = new Date();
        clearDownload();
        const envelope = { schema:'telos.debugger-row-capture/v1', session,
          target:runtime.getTarget?.() || null, operation, source:captureSource,
          scope:captureTargetScope ? 'target' : 'process', pid:captureTargetScope ? null : capturePid,
          receivedAt:receivedAt.toISOString(), headings:[...headings], rows:displayedRows,
          qualification:'Previously captured displayed rows. This export makes no backend request and does not establish execution, replay, executable identity or platform compatibility.',
          semantics:registerCaptureLabel.trim() || null };
        if (id === 'file-bytes') {
          envelope.parameters = captureParams; envelope.semantics = 'selected-file-bytes';
          envelope.fileSize = capturedFileSize;
          fileCapture = { ...captureParams, fileSize:capturedFileSize, session };
        }
        if (sectionFileSize !== null) {
          envelope.fileSize = sectionFileSize;
          envelope.semantics = 'declared-file-ranges';
          envelope.headerStatus = 'identified';
        }
        if (mappingCapture) {
          envelope.parameters = captureParams; envelope.semantics = mappingCapture.semantics;
          envelope.mapping = mappingCapture;
        }
        const snapshot = new Blob([JSON.stringify(envelope, null, 2)], { type:'application/json' });
        // Export is bounded independently of the accepted/displayed row list.
        // Oversize captures stay visible, but are never silently truncated in an export.
        if (snapshot.size <= 4 * 1024 * 1024) captureDownload = {
          session, url:URL.createObjectURL(snapshot),
          filename:`debug-${id}-${receivedAt.toISOString().replace(/[:.]/g, '-')}.json` };
        registerAddressActions = [];
        sectionByteActions = [];
        const capturedSession = session;
        data.body.replaceChildren(...displayedRows.map((values, rowIndex) => {
          const row = node('tr'), cells = values.map(value => node('td', value));
          const fileRange = sectionFileSize === null ? null : declaredFileRange(rows[rowIndex], sectionFileSize);
          if (id === 'file-sections' && fileRange) {
            const prepare = button(`Prepare file bytes for ${values[0]}`, () => {
              syncSession();
              if (ticket !== epoch || !enabled || capturedSession !== session || capturedSession !== getSession()
                || fileInspection?.getSelection?.()?.session !== capturedSession) return;
              views.find(view => view.id === 'debug-file-bytes')?.prepareFileRange(fileRange,
                `Declared ${values[0]} file span captured at ${receivedAt.toLocaleTimeString()}`);
            });
            sectionByteActions.push({ button:prepare, range:fileRange, epoch:ticket, session:capturedSession });
            cells[3].append(' ', prepare);
          }
          if (id === 'registers') {
            const address = savedRegisterAddress(values[3], memoryModel()?.pointerBits);
            if (address !== null) {
              const prepare = button(`Prepare mapping for thread ${values[0]} ${values[2]}`, () => {
                syncSession();
                if (ticket !== epoch || capturePid !== pid || !enabled || session !== getSession()) return;
                views.find(view => view.id === 'debug-address-mapping')?.prepareAddress(address,
                  `Saved ${values[2]} from thread ${values[0]} at ${receivedAt.toLocaleTimeString()}`);
              });
              registerAddressActions.push({ button:prepare, epoch:ticket, pid:capturePid, session,
                capability:'addressMapping', operation:'memory.mapping' });
              cells[3].append(' ', prepare);
              if (address > 0) {
                const prepareScan = button(`Prepare scan for thread ${values[0]} ${values[2]}`, () => {
                  syncSession();
                  if (ticket !== epoch || capturePid !== pid || !enabled || session !== getSession()) return;
                  memoryTools.prepareRange(address, `Saved ${values[2]} from thread ${values[0]} at ${receivedAt.toLocaleTimeString()}`);
                });
                registerAddressActions.push({ button:prepareScan, epoch:ticket, pid:capturePid, session, address,
                  capability:'memoryRead', operation:'memory.read' });
                cells[3].append(' ', prepareScan);
              }
            }
          }
          row.append(...cells); return row;
        }));
        message.textContent = `${rows.length} rows captured · ${captureTargetScope ? 'Selected target' : `PID ${capturePid}`} · ${receivedAt.toLocaleTimeString()} · ${captureSource || 'Source unavailable'}. This is a snapshot.${registerCaptureLabel}${captureDownload ? '' : ' Capture exceeds the 4 MiB row-export limit.'}`;
      } catch (error) { if (ticket === epoch) message.textContent = `Read failed: ${error.message || error}`; }
      finally { if (read === request) { busy = false; update(); } }
    });
    const update = () => {
      const connected = Boolean(session);
      const supported = connected && available(capability) && supportedReader();
      refresh.disabled = !enabled || !supported || (!pid && !targetScoped()) || busy || !captureReady(operation);
      exportCapture.disabled = !enabled || busy || !captureDownload || captureDownload.session !== session;
      if (fileOffset) { fileOffset.disabled = !enabled || busy; fileLength.disabled = !enabled || busy; }
      if (mappingAddress) mappingAddress.disabled = !enabled || busy;
      for (const action of registerAddressActions) action.button.disabled = !enabled || busy
        || action.epoch !== epoch || action.pid !== pid || action.session !== session
        || !memoryModel() || !available(action.capability) || !captureReady(action.operation)
        || capabilities()?.operations?.[action.operation]?.state !== 'available'
        || (action.operation === 'memory.read' && !memoryTools.canPrepareRange(action.address));
      for (const action of sectionByteActions) action.button.disabled = !enabled || busy
        || action.epoch !== epoch || action.session !== session || action.session !== getSession()
        || fileInspection?.getSelection?.()?.session !== action.session
        || !views.find(view => view.id === 'debug-file-bytes')?.canPrepareFileRange(action.range);
      if (previousBytes) {
        const canPrepare = enabled && !busy && fileCapture?.session === session;
        previousBytes.disabled = !canPrepare || !adjacentFileRange(fileCapture, -1);
        nextBytes.disabled = !canPrepare || !adjacentFileRange(fileCapture, 1);
      }
      if (!supported) setText(readiness, !connected ? 'Connect a game to inspect this view.' : `${title} are unavailable for the connected target.`);
      else if (targetScoped()) setText(readiness, busy ? `Capturing ${title.toLowerCase()} for the selected target.`
        : `Inspect ${capabilities().operations[operation].source} data for the selected target.`);
      else if (!pid) setText(readiness, 'Select a process in Processes.');
      else if (busy) setText(readiness, `Capturing ${title.toLowerCase()} for PID ${pid}.`);
      else if (SNAPSHOT_SOURCES.has(capabilities()?.operations?.[operation]?.source)) setText(readiness, `Inspect ${capabilities().operations[operation].source} data for PID ${pid}.`);
      else if (state()?.state !== 'paused') setText(readiness, `Pause the game to capture ${title.toLowerCase()} for PID ${pid}.`);
      else if (!captureReady(operation)) setText(readiness, 'Wait for the target to confirm its capture boundary.');
      else setText(readiness, `Ready to capture ${title.toLowerCase()} for PID ${pid}.`);
    };
    body.append(refresh, exportCapture, readiness, message, data.element);
    return { id:`debug-${id}`, supportedReader, update,
      prepareAddress(address, provenance) {
        if (!mappingAddress || !enabled || busy || !pid || !available(capability)
          || !supportedReader() || !captureReady(operation)
          || !memoryModel() || !Number.isSafeInteger(address) || address < 0 || address > 0xffffffff) return;
        mappingAddress.value = formatAddress(address);
        setText(message, `${provenance}. Prepared ${formatAddress(address)} for PID ${pid}. Capture address backing file to inspect it. This saved value is not a verified pointer. The table and export retain the previous capture.`);
        if (typeof window.TelosDock?.reveal === 'function') window.TelosDock.reveal('debug-address-mapping');
        else window.TelosDock?.show('debug-address-mapping');
        mappingAddress.focus();
      },
      canPrepareFileRange(range) {
        return Boolean(fileOffset && enabled && !busy && session === getSession()
          && fileInspection?.getSelection?.()?.session === session && available(capability)
          && supportedReader() && targetScoped() && captureReady(operation)
          && capabilities()?.operations?.[operation]?.source === 'static-analysis'
          && Number.isSafeInteger(range?.fileSize) && range.fileSize > 0
          && Number.isSafeInteger(range.offset) && range.offset >= 0
          && Number.isSafeInteger(range.length) && range.length >= 1 && range.length <= 4096
          && Number.isSafeInteger(range.declaredLength) && range.declaredLength >= range.length
          && range.offset <= range.fileSize - range.declaredLength
          && (!fileCapture?.fileSize || fileCapture.fileSize === range.fileSize));
      },
      prepareFileRange(range, provenance) {
        syncSession();
        if (!this.canPrepareFileRange(range)) return;
        fileOffset.value = `0x${range.offset.toString(16)}`; fileLength.value = String(range.length);
        message.textContent = `${provenance}. Prepared ${range.length} of ${range.declaredLength} declared file bytes at ${fileOffset.value}. Capture file bytes to read this range. Prior captured rows and export are retained. File offsets do not establish process mappings.`;
        if (typeof window.TelosDock?.reveal === 'function') window.TelosDock.reveal('debug-file-bytes');
        else window.TelosDock?.show('debug-file-bytes');
        fileOffset.focus();
      },
      cancel() { request++; busy = false; }, clear() { request++; busy = false; fileCapture = null; registerAddressActions = []; sectionByteActions = []; clearDownload(); data.body.replaceChildren(); setText(message, 'No capture for the selected target or process.'); } };
  });

  function state() {
    // A host-installed backend owns capture readiness. Its failure must not
    // fall through to another backend's page-global diagnostic snapshot.
    let value;
    try {
      value = typeof runtime.getStatus === 'function' ? runtime.getStatus() : window.__telosRuntimeDiagnostics;
    } catch { return null; }
    return value?.session === session ? value : null;
  }
  function available(operation) {
    if (!enabled || !session) return false;
    const route = CAPABILITY_ROUTES[operation];
    if (!route) return descriptor?.debugger?.operations?.[operation]?.state === 'available';
    try {
      const value = capabilities();
      return value?.session === session && value.operations?.[route]?.state === 'available';
    } catch { return false; }
  }
  function pausedCaptureReady() {
    const value = state();
    return getSession() === session && value?.state === 'paused'
      && (typeof runtime.getStatus === 'function'
        ? value.pausedCaptureReady === true
        : Number.isSafeInteger(value.activeThreads) && value.activeThreads > 0
          && value.activeThreads === value.parkedThreads);
  }
  function memoryReady() {
    return Boolean(memoryModel()) && captureReady('memory.read') && available('memoryRead') && typeof runtime.request === 'function'
      && capabilities()?.operations?.['memory.read']?.state === 'available';
  }
  function processReady() {
    return captureReady('processes.list') && available('processList') && typeof runtime.request === 'function';
  }
  function captureReady(operation) {
    if (!enabled || !session || getSession() !== session) return false;
    const value = capabilities()?.operations?.[operation];
    if (value?.state !== 'available') return false;
    // Snapshot admission is owned by the session-bound service. It does not
    // imply a live process exists or that an execution pause occurred.
    if (SNAPSHOT_SOURCES.has(value.source)) return true;
    return value.source === 'live-runtime' && pausedCaptureReady();
  }
  function syncSession(force = false) {
    const current = getSession() || null;
    if (!force && current === session) return;
    controlEpoch++; controlAbort?.abort(); controlAbort = null; controlMessage = '';
    session = current; descriptor = null; epoch++; pid = null; processes = []; processBusy = false; watchBusy = false;
    executionDisplay = null; clearExecutionDownload();
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
      const cell = node('td'); cell.append(select); row.append(cell, node('td', String(process.pid)), node('td', process.name || 'Unavailable'),
        node('td', process.presentationOwner == null ? 'Unavailable' : process.presentationOwner ? 'Reported owner' : 'No')); return row;
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
    fileInput.disabled = !fileReady();
    returnToGame.disabled = !fileReady() || (!fileBusy && !fileInspection.getSelection());
    renderCapabilities();
    const routeReady = operation => typeof runtime.request === 'function' && capabilities()?.operations?.[`execution.${operation}`]?.state === 'available';
    pause.disabled = !!controlAbort || !available('pause') || !routeReady('pause') || ['paused', 'pausing', 'error', 'stopped'].includes(value?.state);
    resume.disabled = !!controlAbort || !available('resume') || !routeReady('resume') || value?.state !== 'paused';
    step.disabled = !!controlAbort || !available('step') || !routeReady('step') || value?.state !== 'paused';
    const stepUnit = capabilities()?.operations?.['execution.step']?.unit;
    const inspectionSources = [...new Set(Object.entries(capabilities()?.operations || {})
      .filter(([operation, value]) => !operation.startsWith('execution.') && value.state === 'available')
      .map(([, value]) => value.source).filter(source => source === 'live-runtime' || SNAPSHOT_SOURCES.has(source)))];
    setText(fields.get('Inspection sources'), inspectionSources.join(', ') || 'Unavailable');
    step.disabled ||= typeof stepUnit !== 'string' || !stepUnit.length || stepUnit.length > 256 || stepUnit.trim() !== stepUnit;
    setText(step, stepUnit === 'render-present' ? 'Step image' : 'Step');
    step.title = stepUnit === 'render-present' ? 'Advance to the next rendered image; this is separate from stepping a CPU instruction.'
      : typeof stepUnit === 'string' && stepUnit ? `Advance one ${stepUnit}.` : 'The adapter must declare its step unit.';
    let selectedTarget = null;
    try { selectedTarget = typeof runtime.getTarget === 'function' ? runtime.getTarget() : value?.target || descriptor?.target; } catch { /* Unavailable owner target. */ }
    setText(fields.get('Target'), selectedTarget?.profile || (selectedTarget && descriptor?.guestId)
      || [selectedTarget?.format, selectedTarget?.architecture].filter(value => typeof value === 'string' && value.length <= 256).join(' · ') || '—');
    setText(fields.get('State'), value?.state || descriptor?.status || (session && inspectionSources.length ? 'Inspection' : 'Disconnected'));
    setText(fields.get('Boundary'), descriptor?.executionBoundary === 'render-present' ? 'Render presentation' : descriptor?.executionBoundary || '—');
    setText(fields.get('Presented images'), Number.isSafeInteger(value?.presents) ? String(value.presents) : '—');
    setText(fields.get('Parked workers'), Number.isSafeInteger(value?.parkedThreads) && Number.isSafeInteger(value?.activeThreads) ? `${value.parkedThreads} / ${value.activeThreads}` : '—');
    const io = value?.assetIo;
    const ioCounters = ['requests','bytes','cacheHits','failures','shortReads','readRetries','residentBytes','cacheEntries','filesRead'];
    const validIo = io?.schema === 'telos.owned-file-io/v1'
      && ioCounters.every(key => Number.isSafeInteger(io[key]) && io[key] >= 0)
      && Number.isFinite(io.milliseconds) && io.milliseconds >= 0 && io.milliseconds <= Number.MAX_SAFE_INTEGER;
    setText(fields.get('File range I/O'), validIo
      ? `${io.requests} range exchanges · ${io.bytes} bytes read · ${io.milliseconds.toFixed(1)} ms range-read time · ${io.cacheHits} cache hits · ${io.residentBytes} cached bytes / ${io.cacheEntries} entries · ${io.filesRead} chunk-cached files observed · ${io.failures} failures · ${io.shortReads} short reads / ${io.readRetries} retries (cumulative)`
      : 'Unavailable');
    const dispatch = value?.sdlDispatch;
    const validDispatch = ['calls','callerMicros','callbackMicros'].every(key => Number.isSafeInteger(dispatch?.[key]) && dispatch[key] >= 0);
    setText(fields.get('SDL proxy dispatch totals'), validDispatch
      ? `${dispatch.calls} completed proxy calls · ${dispatch.callerMicros} µs caller elapsed · ${dispatch.callbackMicros} µs callback elapsed (cumulative; direct owner calls excluded; caller/callback elapsed can include pause-barrier waits)`
      : 'Unavailable');
    const gl = value?.glCounts;
    const guestErrors = pausedCaptureReady() ? normalizeGuestGlErrorReplies(value?.guestGlErrorReplies) : null;
    setText(fields.get('Guest-requested GL error replies'), describeGuestGlErrorReplies(guestErrors));
    const hostQueryOutputs = pausedCaptureReady() ? normalizeHostGlQueryOutputs(value?.hostGlQueryOutputs) : null;
    setText(fields.get('Host shader query outputs'), describeHostGlQueryOutputs(hostQueryOutputs));
    const validGl = value?.state === 'paused'
      && Number.isSafeInteger(value.activeThreads) && value.activeThreads > 0 && value.activeThreads <= 4096
      && value.parkedThreads === value.activeThreads && ['telos.paused-gl-counts/v1', 'telos.paused-gl-counts/v2'].includes(gl?.schema)
      && Number.isSafeInteger(gl.slots) && gl.slots > 0 && gl.slots <= 4096
      && Number.isSafeInteger(gl.nonzero) && gl.nonzero >= 0 && gl.nonzero <= gl.slots
      && Array.isArray(gl.entries) && gl.entries.length === (gl.schema === 'telos.paused-gl-counts/v2' ? gl.nonzero : Math.min(gl.nonzero, 16))
      && gl.truncated === (gl.nonzero > gl.entries.length)
      && gl.entries.every((entry, index, entries) => Number.isSafeInteger(entry?.index) && entry.index >= 0 && entry.index < gl.slots
        && Number.isSafeInteger(entry.count) && entry.count > 0
        && typeof entry.name === 'string' && /^[\x20-\x7e]{1,127}$/.test(entry.name)
        && (!index || entries[index - 1].count > entry.count
          || (entries[index - 1].count === entry.count && entries[index - 1].index < entry.index)))
      && new Set(gl.entries.map(entry => entry.index)).size === gl.entries.length;
    const displayedGlEntries = validGl ? gl.entries.slice(0, 16) : [];
    setText(fields.get('Paused GL call counts'), validGl
      ? `${displayedGlEntries.map(entry => `${entry.name}: ${entry.count}`).join(' · ') || 'No nonzero counters reported'} (${displayedGlEntries.length} displayed of ${gl.nonzero} nonzero counters; cumulative callback/host counts, not timings)`
      : 'Unavailable');
    const timingAvailable = validGl && Number.isSafeInteger(gl.sampleInterval)
      && gl.sampleInterval > 0 && gl.sampleInterval <= 4096;
    const selectableGlEntries = validGl ? gl.entries : [];
    const optionsKey = JSON.stringify(selectableGlEntries.map(entry => [entry.index, entry.name]));
    if (optionsKey !== glCounterOptionsKey) {
      const selected = glCounterSelect.value;
      glCounterSelect.replaceChildren(...selectableGlEntries.map(entry => new Option(entry.name, String(entry.index))));
      const preferred = selectableGlEntries.find(entry => String(entry.index) === selected)
        || selectableGlEntries.find(entry => entry.timing?.samples > 0) || selectableGlEntries[0];
      glCounterSelect.value = preferred ? String(preferred.index) : '';
      glCounterOptionsKey = optionsKey;
    }
    glCounterSelect.disabled = !selectableGlEntries.length;
    const selectedGlCounter = selectableGlEntries.find(entry => String(entry.index) === glCounterSelect.value);
    setText(glCounterInfo, validGl && !selectableGlEntries.length ? 'No nonzero GL counters in this capture.'
      : describeCapturedGlCounter(selectedGlCounter, timingAvailable ? gl.sampleInterval : null));
    const sampled = timingAvailable ? displayedGlEntries.filter(entry =>
      ['samples','callerMicros','callbackMicros'].every(key => Number.isSafeInteger(entry.timing?.[key]) && entry.timing[key] >= 0)
      && entry.timing.samples > 0 && entry.timing.samples <= entry.count) : [];
    setText(fields.get('Sampled GL proxy elapsed'), timingAvailable
      ? sampled.length ? `${sampled.map(entry => `${entry.name}: ${entry.timing.samples} samples / ${entry.timing.callerMicros} µs caller / ${entry.timing.callbackMicros} µs callback`).join(' · ')} (cumulative sparse samples, interval ${gl.sampleInterval}; count leaders only; elapsed can include waits; not CPU time or total-call estimates)`
        : 'No timing samples captured for the displayed count leaders. Timing may be disabled.'
      : 'Unavailable');
    const progress = value?.dispatchProgress;
    const progressEntries = ['pausing','paused'].includes(value?.state) && progress?.schema === 'telos.published-dispatch-progress/v1'
      && progress.semantics === 'best-effort-published-trace' && Array.isArray(progress.entries) && progress.entries.length <= 64
      && Array.from(progress.entries).every(entry => entry && ['tid','pid','entryEip','reportedEip','tableIndex','sequence','ageMs'].every(key => Number.isSafeInteger(entry[key]) && entry[key] >= 0 && entry[key] <= 0xffffffff)
        && entry.tid > 0 && typeof entry.kernelWait === 'boolean')
      && new Set(progress.entries.map(entry => entry.tid)).size === progress.entries.length ? progress.entries : null;
    setText(fields.get('Published dispatch samples'), progressEntries
      ? progressEntries.length ? progressEntries.map(entry => `PID ${entry.pid} · thread ${entry.tid} · ${entry.sequence ? `reported EIP 0x${entry.reportedEip.toString(16).padStart(8,'0')} · table ${entry.tableIndex === 0xffffffff ? 'Unavailable' : entry.tableIndex}` : 'Reported block unavailable (sequence 0)'} · sequence ${entry.sequence} · dispatch age ${entry.ageMs} ms · ${entry.kernelWait ? 'Kernel wait reported' : 'No kernel-wait flag'}`).join(' / ') + ' · Published trace samples; not registers, current instructions, wait causes or CPU timing.'
        : 'No published dispatch samples. This does not prove that every worker is idle.'
      : 'Unavailable');
    setText(execution, value?.error || value?.lastControlError || controlMessage || (!session ? 'Connect a game to inspect execution.'
      : inspectionSources.length && inspectionSources.every(source => SNAPSHOT_SOURCES.has(source)) ? 'Inspection uses snapshot evidence. This does not establish live execution.'
      : value?.state === 'paused' ? (stepUnit === 'render-present' ? 'Paused · inspect memory or advance one image.' : 'Paused. Capture supported views or use Step.') : 'Game execution follows the connected session.'));
    // Retain the validated paused counts alongside the compact displayed text.
    // The render timestamp is not a native observation or completion receipt.
    executionDisplay = enabled && session ? Object.freeze({ session,
      renderedAt:new Date().toISOString(),
      selectedGlCounterIndex:selectedGlCounter?.index ?? null,
      guestGlErrorReplies:guestErrors,
      hostGlQueryOutputs:hostQueryOutputs,
      metrics:Object.freeze([...fields].map(([label, field]) => Object.freeze([label, field.textContent]))),
      pausedGlCounts:validGl ? Object.freeze({ schema:gl.schema, slots:gl.slots, nonzero:gl.nonzero,
        truncated:gl.truncated, sampleInterval:timingAvailable ? gl.sampleInterval : null,
        timingReadScope:timingAvailable && gl.schema === 'telos.paused-gl-counts/v2' && !gl.truncated
          && gl.timingReadScope === 'all-nonzero-entries' ? gl.timingReadScope : null,
        entries:Object.freeze(gl.entries.map(entry => Object.freeze({ index:entry.index, name:entry.name, count:entry.count,
          timing:timingAvailable && entry.timing
            && ['samples','callerMicros','callbackMicros'].every(key => Number.isSafeInteger(entry.timing[key]) && entry.timing[key] >= 0)
            && entry.timing.samples <= entry.count
            ? Object.freeze({ samples:entry.timing.samples, callerMicros:entry.timing.callerMicros, callbackMicros:entry.timing.callbackMicros }) : null }))) }) : null,
      message:execution.textContent }) : null;
    exportExecution.disabled = !executionDisplay;
    processRefresh.disabled = !processReady() || processBusy;
    watchRefresh.disabled = !memoryReady() || !pid || !watches.length || watchBusy;
    const model = memoryModel();
    typeInput.setAttribute('aria-label', model ? `Value type, ${model.byteOrder} endian` : 'Value type, byte order unavailable');
    add.disabled = watches.length >= 32;
    for (const view of views) view.update();
    memoryTools.update();
    syncDockAvailability();
  }
  function syncDockAvailability() {
    const dock = window.TelosDock; if (!dock) return;
    if (availabilityDock !== dock) { availabilityDock = dock; dockAvailability.clear(); }
    const requirements = new Map(TOOL_VIEWS.map(([id, , capability, operation]) => [`debug-${id}`, { capability, operation }]));
    requirements.set('debug-threads', { capability:'threadList', operation:'threads.list' });
    requirements.set('debug-registers', { capability:'registers', operation:'registers.read' });
    for (const id of memoryTools.ids) requirements.set(id, { capability: 'memoryRead', operation: 'memory.read' });
    requirements.set('debug-processes', { capability: 'processList', operation: 'processes.list' });
    requirements.set('debug-watches', { capability: 'memoryRead', operation: 'memory.read' });
    const changes = new Map([[true, []], [false, []]]);
    for (const id of ['debugger', ...panels.keys()]) {
      const requirement = requirements.get(id);
      const serviceReady = requirement?.operation
        ? typeof runtime.request === 'function' && capabilities()?.operations?.[requirement.operation]?.state === 'available'
          && (views.find(view => view.id === id)?.supportedReader() ?? true)
          && (requirement.operation !== 'memory.read' || Boolean(memoryModel()))
        : typeof getGame()?.[requirement?.method] === 'function';
      const supported = enabled && (id === 'debug-file-inspection' ? fileReady()
        : id === 'debugger' ? Boolean(workbench) && (runtime.workbenchAvailable?.() ?? true)
        : !requirement || (Boolean(session) && available(requirement.capability) && serviceReady));
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
    // Guest events cannot relabel a selected broker, even if a trusted owner
    // deliberately reused the guest's session string.
    const guest = getGame();
    if (guest && guest.session === session && value?.schema === 'telos-runtime-session/v1'
      && value.session === session) { descriptor = value; render(); }
  });
  register(); renderWatches();
  runtime.subscribe?.(() => {
    // A new target may reuse a session string; selection still invalidates
    // captures, pending controls and the old descriptor.
    syncSession(true);
    if (enabled && !workbench && typeof runtime.mountWorkbench === 'function') {
      workbench = runtime.mountWorkbench({ isEnabled:() => enabled });
    }
    render();
    syncDockAvailability();
  });
  return { setEnabled(value) {
    enabled = value; document.body.dataset.debugExtension = value ? 'enabled' : 'disabled';
    if (enabled) {
      if (typeof runtime.mountWorkbench === 'function') workbench ||= runtime.mountWorkbench({ isEnabled: () => enabled });
      getGame()?.publishRuntimeSession?.();
      timer ||= setInterval(() => { if (!document.hidden && [...panels.values()].some(panel => !panel.hidden)) render(); }, 500);
      render();
    } else { executionDisplay = null; clearExecutionDownload(); exportExecution.disabled = true; epoch++; controlEpoch++; controlAbort?.abort(); controlAbort = null; controlMessage = ''; fileRequest++; fileBusy = false; fileInspection?.cancelPending?.(); processBusy = false; watchBusy = false; for (const view of views) view.cancel(); memoryTools.cancel(); clearInterval(timer); timer = null; }
    register();
  } };
}
