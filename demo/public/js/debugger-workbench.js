import { DEBUG_OPERATIONS, DebugTargetAdapterRegistry, debuggerQualificationView, resolveDebuggerCapabilities } from '../debugger/debugger-contract.js';
import { identifyBinaryHeader } from './binary-identity.js';
import { createCapturedMemoryInspector } from './captured-memory-inspector.js';
import { createAdapterDetails, createCapabilityDirectory } from './debugger-capability-directory.js';
import { DEBUG_REQUEST_SCHEMA, DEBUG_REPLY_SCHEMA } from '../packages/debug-runtime/runtime.js';

function boundedInputKeys(value) {
  if (!Array.isArray(value) || !value.length || value.length > 32
    || Array.from(value).some(key => typeof key !== 'string' || !/^[a-z0-9][a-z0-9-]{0,31}$/.test(key))
    || new Set(value).size !== value.length) return null;
  return [...value];
}

function boundedThreadLifecycle(value) {
  if (!value || typeof value !== 'object') return null;
  const keys = ['starting', 'running', 'kernelWait', 'paused', 'exiting', 'total'];
  if (!keys.every(key => Number.isSafeInteger(value[key]) && value[key] >= 0 && value[key] <= 4095)) return null;
  if (keys.slice(0, 5).reduce((sum, key) => sum + value[key], 0) !== value.total) return null;
  return Object.fromEntries(keys.map(key => [key, value[key]]));
}

// Observe the session owner; the existing transport remains the only controller.
export function mountDebuggerWorkbench(game, { isEnabled = () => true, debugRuntime, mountRoot = document.getElementById('cockpit') } = {}) {
  if (!(mountRoot instanceof HTMLElement) || !mountRoot.isConnected) {
    throw new Error('The debugger workspace mount point is unavailable.');
  }
  let debugRequestSequence = 0;
  async function requestDebug(operation, params = {}) {
    const requestSession = game.session;
    if (!requestSession || typeof debugRuntime?.request !== 'function') throw new Error('The debugger request service is unavailable.');
    const reply = await debugRuntime.request({ schema:DEBUG_REQUEST_SCHEMA, id:`debug-workbench-${++debugRequestSequence}`,
      session:requestSession, operation, params });
    if (reply?.schema !== DEBUG_REPLY_SCHEMA || reply.session !== requestSession
      || reply.operation !== operation || game.session !== requestSession) {
      throw new Error('The game session changed or returned an invalid debugger reply.');
    }
    return reply.data;
  }
  async function readMemory(pid, address, length) {
    const data = await requestDebug('memory.read', { pid, address, length });
    if (data?.pid !== pid || data.address !== address || data.length !== length
      || !Array.isArray(data.bytes) || data.bytes.length !== length
      || data.bytes.some(byte => !Number.isInteger(byte) || byte < 0 || byte > 255)) {
      throw new Error('The debugger returned an invalid memory response.');
    }
    return Uint8Array.from(data.bytes);
  }
  const make = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text) node.textContent = text;
    return node;
  };
  const panel = make('details', 'debugger-workbench');
  panel.dataset.telosDockWidget = 'debugger';
  panel.dataset.telosDockTitle = 'Debugger';
  panel.open = false;
  const summary = make('summary', 'dw-summary');
  const title = make('span', 'dw-title', 'Debugger workbench');
  const badge = make('span', 'dw-status', 'Disconnected');
  summary.append(title, badge);
  const body = make('div', 'dw-body');
  const provenance = make('p', 'dw-provenance', 'Live runtime session');
  const grid = make('dl', 'dw-metrics');
  const fields = new Map();
  for (const label of ['Session', 'Target', 'Executable', 'Build SHA-256', 'Backend', 'Release', 'Format', 'Architecture', 'OS / ABI', 'Pointer width', 'Byte order', 'Execution boundary', 'Input admission', 'Score keys', 'Rendered images', 'Parked workers', 'Worker lifecycle snapshot']) {
    const row = make('div');
    const value = make('dd', '', '—');
    row.append(make('dt', '', label), value);
    grid.append(row);
    fields.set(label, value);
  }
  const capabilities = make('ul', 'dw-capabilities');
  capabilities.setAttribute('aria-label', 'Advertised runtime capabilities');
  const note = make('p', 'dw-note', 'Waiting for a capability handshake from the selected session.');
  const support = make('details', 'dw-support');
  const supportSummary = make('summary', '', 'Runtime capability advertisements');
  const supportList = make('dl', 'dw-support-list');
  supportList.setAttribute('aria-label', 'Debugger operation support reported by this session');
  support.hidden = true;
  support.append(supportSummary, supportList,
    make('p', 'dw-note', 'Unsupported means the backend does not provide the operation. Unavailable means it is not ready. Not qualified means its behavior has not been established for this target.'));
  const qualificationPanel = make('details', 'dw-support');
  qualificationPanel.append(make('summary', '', 'Operation qualification'));
  const qualificationIdentity = make('p', 'dw-note');
  const qualificationList = make('dl', 'dw-support-list');
  qualificationList.setAttribute('aria-label', 'Cross-host debugger operation qualification');
  qualificationPanel.append(qualificationIdentity, qualificationList);
  const boundaryNote = make('p', 'dw-note');
  const memoryPanel = make('details', 'dw-memory');
  memoryPanel.append(make('summary', '', 'Read game memory'));
  const memoryAvailability = make('p', 'dw-note');
  const memoryForm = make('form', 'dw-memory-form');
  const memoryControls = make('fieldset');
  memoryControls.append(make('legend', '', 'Paused game process'));
  const processLabel = make('label', '', 'Process');
  const processSelect = make('select');
  processSelect.append(make('option', '', 'Refresh processes to select a PID'));
  processLabel.append(processSelect);
  const refreshProcesses = make('button', 'btn small', 'Refresh processes');
  refreshProcesses.type = 'button';
  const addressLabel = make('label', '', 'Game address (hex)');
  const addressInput = make('input');
  addressInput.type = 'text';
  addressInput.placeholder = '0x00400000';
  addressInput.spellcheck = false;
  addressInput.autocomplete = 'off';
  addressLabel.append(addressInput);
  const lengthLabel = make('label', '', 'Bytes (1–65536)');
  const lengthInput = make('input');
  lengthInput.type = 'number';
  lengthInput.min = '1';
  lengthInput.max = '65536';
  lengthInput.step = '1';
  lengthInput.value = '256';
  lengthLabel.append(lengthInput);
  const readMemoryButton = make('button', 'btn small', 'Read memory');
  readMemoryButton.type = 'submit';
  memoryControls.append(processLabel, refreshProcesses, addressLabel, lengthLabel, readMemoryButton);
  memoryForm.append(memoryControls);
  const memoryStatus = make('p', 'dw-note');
  memoryStatus.setAttribute('role', 'status');
  const memoryCapture = make('p', 'dw-note');
  const memoryDump = make('pre', 'dw-memory-dump');
  memoryDump.tabIndex = 0;
  memoryDump.setAttribute('aria-label', 'Captured game memory: address, hexadecimal bytes, printable ASCII');
  memoryDump.hidden = true;
  const valueInspector = createCapturedMemoryInspector();
  const memorySearch = createCaptureSearch(() => syncSession());
  const captureActions = make('div', 'dw-capture-actions');
  captureActions.setAttribute('role', 'group');
  captureActions.setAttribute('aria-label', 'Captured game memory actions');
  const downloadBytes = make('button', 'btn small', 'Download bytes (.bin)');
  const downloadHex = make('button', 'btn small', 'Download hex (.txt)');
  const useBaseline = make('button', 'btn small', 'Use as baseline');
  const clearBaseline = make('button', 'btn small', 'Clear baseline');
  for (const button of [downloadBytes, downloadHex, useBaseline, clearBaseline]) {
    button.type = 'button';
    button.disabled = true;
  }
  captureActions.append(downloadBytes, downloadHex, useBaseline, clearBaseline);
  const comparisonStatus = make('p', 'dw-note');
  comparisonStatus.setAttribute('role', 'status');
  const exportStatus = make('p', 'dw-note', 'Downloads become available after a successful memory read.');
  exportStatus.setAttribute('role', 'status');
  memoryPanel.append(memoryAvailability, memoryForm, memoryStatus, memoryCapture, memorySearch.element, memoryDump, valueInspector.element, captureActions, comparisonStatus, exportStatus,
    make('p', 'dw-note', 'Read a captured range from the selected process using its 32-bit game address.'));
  const handoffs = make('section', 'dw-input-watch');
  handoffs.hidden = true;
  handoffs.setAttribute('aria-label', 'Aggregate SDL handoff interval metrics');
  const handoffHead = make('div', 'dw-input-head');
  handoffHead.append(make('h3', '', 'Aggregate SDL handoffs'));
  const handoffGrid = make('dl', 'dw-metrics');
  const handoffFields = [];
  for (const label of ['Handoffs / sec', 'Caller wait µs / handoff', 'Callback + safe-point µs / handoff', 'Presentations / sec']) {
    const row = make('div');
    const value = make('dd', '', '—');
    row.append(make('dt', '', label), value);
    handoffGrid.append(row);
    handoffFields.push(value);
  }
  handoffs.append(handoffHead, handoffGrid);
  const inputWatch = make('section', 'dw-input-watch');
  inputWatch.hidden = true;
  inputWatch.setAttribute('aria-label', 'Latest acknowledged input');
  const inputHead = make('div', 'dw-input-head');
  const inputTime = make('time', 'dw-note');
  inputHead.append(make('h3', '', 'Latest acknowledged input'), inputTime);
  const inputValues = make('dl', 'dw-metrics');
  const inputFields = new Map();
  for (const label of ['Score tick', 'Render present', 'Consumed events', 'Requested keys', 'Requested buttons', 'Requested mouse', 'GL presentations during input', 'GL presentations before consumption', 'GL presentations after consumption', 'Round trip']) {
    const row = make('div');
    const value = make('dd', '', '—');
    row.append(make('dt', '', label), value);
    inputValues.append(row);
    inputFields.set(label, value);
  }
  inputWatch.append(inputHead, inputValues);
  const historyHead = make('div', 'dw-history-head');
  const clear = make('button', 'btn small', 'Clear history');
  clear.type = 'button';
  clear.disabled = true;
  const exportInspection = make('button', 'btn small', 'Export inspection');
  exportInspection.type = 'button';
  exportInspection.disabled = true;
  exportInspection.setAttribute('aria-label', 'Export captured inspection and session activity as JSON');
  historyHead.append(make('h3', '', 'Session activity'), exportInspection, clear);
  historyHead.tabIndex = -1;
  const history = make('ol', 'dw-history');
  history.setAttribute('aria-label', 'Recent session activity, newest first');
  const empty = make('p', 'dw-empty', 'Runtime changes and acknowledged inputs will appear here.');
  const retention = make('p', 'dw-note', 'Latest 40 observations in this page. Cleared when the session changes.');
  retention.setAttribute('role', 'status');
  const binary = make('details', 'dw-binary');
  binary.append(make('summary', '', 'Inspect a local executable'));
  binary.append(make('p', 'dw-provenance', 'Source: file headers on this device. This does not start a runtime or attach a debugger.'));
  const pickerLabel = make('label', 'dw-binary-picker', 'Executable file');
  const picker = make('input');
  picker.type = 'file';
  pickerLabel.append(picker);
  const binaryStatus = make('p', 'dw-note', 'Choose a file to inspect its metadata locally. Nothing is uploaded.');
  binaryStatus.setAttribute('role', 'status');
  const adapterRegistry = new DebugTargetAdapterRegistry();
  const liveAdapter = createAdapterDetails(make, adapterRegistry);
  const fileAdapter = createAdapterDetails(make, adapterRegistry, true);
  const inspectionDirectory = createCapabilityDirectory(make, destination => {
    if (['threads', 'registers', 'address-mapping'].includes(destination)) {
      const dock = window.TelosDock;
      if (typeof dock?.reveal === 'function') dock.reveal(`debug-${destination}`);
      else dock?.show(`debug-${destination}`);
      return;
    }
    const target = destination === 'memory' ? memoryPanel : destination === 'file' ? binary : historyHead;
    if (target.tagName === 'DETAILS') target.open = true;
    target.scrollIntoView({ block:'nearest' });
    (target.querySelector('summary') || target).focus({ preventScroll:true });
  });
  let inspectedTarget = null;
  const adapterLabel = make('label', 'dw-binary-picker', 'Load a target adapter manifest (JSON)');
  const adapterPicker = make('input');
  adapterPicker.type = 'file';
  adapterPicker.accept = '.json,application/json';
  adapterLabel.append(adapterPicker);
  const adapterStatus = make('p', 'dw-note', 'No target adapter registered. Local SHA-256 supports exact-build identity for files up to 128 MiB. Manifests do not load code or grant runtime operations.');
  adapterStatus.setAttribute('role', 'status');
  let lastAdapterStatus = adapterStatus.textContent;
  const setAdapterStatus = text => {
    if (text !== lastAdapterStatus) adapterStatus.textContent = lastAdapterStatus = text;
  };
  const binaryValues = make('dl', 'dw-metrics');
  binaryValues.hidden = true;
  const binaryFields = new Map();
  for (const label of ['File', 'Format', 'Architecture', 'ABI', 'Identification', 'SHA-256', 'Reason', 'Declared entry', 'Preferred image base']) {
    const row = make('div');
    const value = make('dd', '', 'Unknown');
    row.append(make('dt', '', label), value);
    binaryValues.append(row);
    binaryFields.set(label, value);
  }
  const evidence = make('ul', 'dw-binary-evidence');
  evidence.setAttribute('aria-label', 'Header evidence');
  const metadataStatus = make('p', 'dw-note');
  const dependencyList = make('ul', 'dw-binary-dependencies');
  dependencyList.setAttribute('aria-label', 'Declared dependency library names');
  dependencyList.tabIndex = 0;
  const metadataScope = make('p', 'dw-note');
  const layoutPanel = make('details', 'dw-file-layout');
  layoutPanel.append(make('summary', '', 'Inspect declared sections and load segments'));
  const layoutStatus = make('p', 'dw-note');
  const layoutScroll = make('div', 'dw-layout-scroll');
  layoutScroll.tabIndex = 0;
  layoutScroll.setAttribute('role', 'region');
  layoutScroll.setAttribute('aria-label', 'Declared executable file layout');
  const layoutTable = make('table', 'dw-layout-table');
  layoutTable.append(make('caption', '', 'File metadata only. Addresses and permissions are not live runtime mappings. Sizes are bytes.'));
  const layoutHead = make('thead');
  const layoutHeadRow = make('tr');
  for (const label of ['Name', 'Kind / address basis', 'Address', 'File offset', 'File bytes', 'Virtual / section bytes', 'Permissions / flags']) {
    const heading = make('th', '', label);
    heading.scope = 'col';
    layoutHeadRow.append(heading);
  }
  layoutHead.append(layoutHeadRow);
  const layoutRows = make('tbody');
  layoutTable.append(layoutHead, layoutRows);
  layoutScroll.append(layoutTable);
  layoutPanel.append(layoutStatus, layoutScroll,
    make('p', 'dw-note', 'PE sections and ELF load segments show R/W/X permissions. ELF sections show W/X/A flags; A means allocated and does not establish read permission. ELF addresses may need a load bias. NOBITS sections have no file bytes. PE names are the eight-byte header names. Inspection is capped at 256 sections and 256 program headers; extended ELF numbering is unavailable.'));
  layoutPanel.hidden = true;
  binary.append(pickerLabel, binaryStatus, binaryValues, evidence, metadataStatus, dependencyList, metadataScope, layoutPanel, adapterLabel, adapterStatus, fileAdapter.element,
    make('p', 'dw-note', 'Header identification is separate from runtime compatibility. Inspecting a file does not change the active game.'));
  const showAdapterMatch = () => {
    let resolution = null, error = null;
    if (session && descriptor?.capabilityReports != null) {
      try { resolution = resolveDebuggerCapabilities({session, revision:descriptor.revision, target:descriptor.target, reports:descriptor.capabilityReports}); }
      catch (cause) { error = cause instanceof Error ? cause.message : 'Invalid capability reports'; }
    }
    liveAdapter.update(descriptor?.target, resolution);
    fileAdapter.update(inspectedTarget, null);
    inspectionDirectory.update({ session, descriptor, resolution, error });
  };
  let adapterInspection = 0;
  adapterPicker.addEventListener('change', async () => {
    const request = ++adapterInspection;
    const file = adapterPicker.files?.[0];
    adapterPicker.value = '';
    if (!file) return;
    if (file.size > 256 * 1024) { setAdapterStatus('Manifest exceeds the 256 KiB limit.'); return; }
    setAdapterStatus('Reading target adapter manifest…');
    try {
      const text = await file.text();
      if (request !== adapterInspection) return;
      const manifest = JSON.parse(text);
      const registered = adapterRegistry.register(manifest);
      setAdapterStatus(`Registered declarative adapter ${registered.id} ${registered.version}; executable code was not loaded.`);
      showAdapterMatch();
    } catch (error) {
      if (request !== adapterInspection) return;
      setAdapterStatus(`Manifest rejected: ${error instanceof Error ? error.message : 'invalid manifest'}`);
    }
  });
  let inspection = 0;
  picker.addEventListener('change', async () => {
    const request = ++inspection;
    const file = picker.files?.[0];
    inspectedTarget = null;
    showAdapterMatch();
    binaryValues.hidden = true;
    layoutPanel.hidden = true;
    layoutRows.replaceChildren();
    evidence.replaceChildren();
    dependencyList.replaceChildren();
    metadataStatus.textContent = '';
    metadataScope.textContent = '';
    if (!file) { binaryStatus.textContent = 'Choose a file to inspect its metadata locally. Nothing is uploaded.'; return; }
    binaryStatus.textContent = 'Reading headers and dependency tables…';
    try {
      const value = await identifyBinaryHeader(file);
      if (request !== inspection) return;
      if (['identified', 'ambiguous'].includes(value.status)) {
        let sha256 = null;
        let hashStatus = 'Exact-build matching unavailable: Web Crypto SHA-256 is not available in this context.';
        if (file.size <= 128 * 1024 * 1024 && typeof globalThis.crypto?.subtle?.digest === 'function') {
          try {
            binaryStatus.textContent = 'Calculating SHA-256 locally… file contents stay in this browser.';
            const digest = await globalThis.crypto.subtle.digest('SHA-256', await file.arrayBuffer());
            if (request !== inspection) return;
            sha256 = [...new Uint8Array(digest)].map(byte => byte.toString(16).padStart(2, '0')).join('');
            hashStatus = 'SHA-256 calculated locally; file contents were not uploaded.';
          } catch {
            hashStatus = 'Exact-build matching unavailable: the local SHA-256 calculation failed.';
          }
        } else if (file.size > 128 * 1024 * 1024) {
          hashStatus = 'Exact-build matching unavailable: executable exceeds the 128 MiB local hashing limit.';
        }
        if (request !== inspection) return;
        inspectedTarget = { profile:null, executable:null, sha256, format:value.format,
          architecture:value.architecture || null, osAbi:value.abi || null,
          pointerBits:value.metadata?.pointerBits ?? null, byteOrder:value.byteOrder || null };
        binaryStatus.textContent = `${file.name}: ${value.status}. ${hashStatus}`;
      }
      const identified = value.status === 'identified' ? 'Identified' : value.status === 'ambiguous' ? 'Ambiguous' : 'Unknown';
      const metadata = value.metadata, entry = metadata?.entryPoint;
      const values = [file.name, value.format, value.architecture || value.architectures?.join(', '), value.abi, identified, inspectedTarget?.sha256 || 'Unavailable', value.reason?.replaceAll('-', ' '),
        entry ? `${entry.address} (${entry.kind}) · ${entry.note}` : 'Unavailable', entry?.imageBase || 'Unavailable'];
      [...binaryFields.values()].forEach((node, index) => { node.textContent = values[index] || 'Unknown'; });
      evidence.replaceChildren(...(value.evidence || []).map(text => make('li', '', text)));
      dependencyList.replaceChildren(...(metadata?.dependencies || []).map(name => make('li', '', name)));
      metadataStatus.textContent = !metadata ? 'Entry and dependency metadata are unavailable for this format.' :
        metadata.status === 'available' ? `Declared dependencies: ${metadata.dependencies.length ? metadata.dependencies.length + ' library names' : 'none in the inspected table'}.` :
        `Dependency inspection ${metadata.status}: ${metadata.reason}. Listed names, if any, are incomplete.`;
      metadataScope.textContent = metadata?.scope || '';
      const layout = value.layout;
      layoutPanel.hidden = false;
      layoutStatus.textContent = !layout ? 'Section and segment inspection is unavailable for this format.' :
        layout.status === 'available' ? `${layout.entries.length} declared sections and load segments inspected.` :
        `Layout inspection ${layout.status}: ${layout.reason}. Listed entries, if any, are incomplete.`;
      layoutRows.replaceChildren(...(layout?.entries || []).map(entry => {
        const row = make('tr');
        for (const key of ['name', 'kind', 'address', 'offset', 'fileSize', 'memorySize', 'permissions']) row.append(make('td', '', entry[key]));
        return row;
      }));
      layoutScroll.hidden = !layout?.entries.length;
      binaryValues.hidden = false;
      if (!inspectedTarget) binaryStatus.textContent = `${file.name}: ${identified.toLowerCase()}.`;
      showAdapterMatch();
    } catch {
      if (request === inspection) { binaryStatus.textContent = 'Could not read this file. Choose it again to retry.'; showAdapterMatch(); }
    }
  });
  body.append(provenance, grid, capabilities, note, liveAdapter.element, inspectionDirectory.element, support, qualificationPanel, boundaryNote, memoryPanel, handoffs, inputWatch, historyHead, empty, history, retention, binary);
  panel.append(summary, body);
  // The game owner replaces its own children while opening. Keep package UI
  // in the stable workspace even when Debug activates before the dock owner.
  mountRoot.append(panel);
  window.TelosDock?.register('debugger', 'Debugger', panel);

  let session = game.session || null;
  let descriptor = null;
  let lastState = null;
  let lastCapabilityText = '';
  let lastSupportText = '';
  let lastQualificationText = '';
  const qualification = () => debuggerQualificationView({session, revision:descriptor?.revision, target:descriptor?.target, reports:descriptor?.capabilityReports});
  let handoffSample = null;
  let memoryRequest = 0;
  let memoryBusy = false;
  let memoryReady = false;
  let capturedMemory = null;
  let baselineMemory = null;
  let activityRecords = [];
  const downloadUrls = new Map();
  const renderComparison = () => {
    useBaseline.disabled = !capturedMemory;
    clearBaseline.disabled = !baselineMemory;
    if (!baselineMemory) { comparisonStatus.textContent = ''; return; }
    const baseline = baselineMemory, capture = capturedMemory;
    const range = `PID ${baseline.pid} · 0x${baseline.address.toString(16)} · ${baseline.bytes.length} bytes`;
    if (!capture || capture === baseline) {
      comparisonStatus.textContent = `Baseline: ${range}. Read this range again to compare.`;
      return;
    }
    if (['session', 'target', 'backend', 'release'].some(key => capture.identity[key] !== baseline.identity[key]) ||
        capture.pid !== baseline.pid || capture.address !== baseline.address || capture.bytes.length !== baseline.bytes.length) {
      comparisonStatus.textContent = `Baseline: ${range}. Read the same range and session, or replace the baseline.`;
      return;
    }
    let changed = 0, first = -1;
    for (let offset = 0; offset < capture.bytes.length; offset++) {
      if (capture.bytes[offset] !== baseline.bytes[offset]) {
        changed++;
        if (first < 0) first = offset;
      }
    }
    const byte = value => value.toString(16).padStart(2, '0');
    comparisonStatus.textContent = `${changed} of ${capture.bytes.length} bytes changed since baseline` +
      (first < 0 ? '.' : ` · first at 0x${(capture.address + first).toString(16)}: ${byte(baseline.bytes[first])} → ${byte(capture.bytes[first])}.`);
  };
  const resetBaseline = () => { baselineMemory = null; renderComparison(); };
  useBaseline.addEventListener('click', () => {
    syncSession();
    if (!capturedMemory) return;
    baselineMemory = capturedMemory;
    renderComparison();
  });
  clearBaseline.addEventListener('click', () => {
    resetBaseline();
    (capturedMemory ? useBaseline : memoryPanel.querySelector('summary')).focus({ preventScroll:true });
  });
  const clearCaptureExport = () => {
    capturedMemory = null;
    valueInspector.setCapture(null);
    memorySearch.setCapture(null);
    downloadBytes.disabled = true;
    downloadHex.disabled = true;
    exportStatus.textContent = 'Downloads become available after a successful memory read.';
    renderComparison();
  };
  const exportCapture = format => {
    // Check session ownership even if the next periodic render has not run yet.
    syncSession();
    const capture = capturedMemory;
    if (!capture) return;
    let url;
    let link;
    try {
      const header = [
        'Captured guest memory',
        `Received at: ${capture.at}`,
        `Runtime session: ${JSON.stringify(capture.identity.session)}`,
        `Target profile: ${JSON.stringify(capture.identity.target)}`,
        `Backend: ${JSON.stringify(capture.identity.backend)}`,
        `Backend release: ${JSON.stringify(capture.identity.release)}`,
        `Guest PID: ${capture.pid}`,
        `Guest virtual address: 0x${capture.address.toString(16).padStart(8, '0')}`,
        `Length: ${capture.bytes.length} bytes`,
        'Capture state: paused',
        '',
      ].join('\n');
      const blob = new Blob(format === 'bin' ? [capture.bytes] : [header, '\n', capture.hex, '\n'],
        { type: format === 'bin' ? 'application/octet-stream' : 'text/plain;charset=utf-8' });
      url = URL.createObjectURL(blob);
      link = make('a');
      link.href = url;
      link.download = `guest-memory-pid-${capture.pid}-0x${capture.address.toString(16)}-${capture.at.replace(/[:.]/g, '-')}.${format}`;
      link.hidden = true;
      panel.append(link);
      link.click();
      // Keep the blob alive while the browser starts its download.
      const timer = setTimeout(() => {
        URL.revokeObjectURL(url);
        downloadUrls.delete(url);
      }, 60000);
      downloadUrls.set(url, timer);
      exportStatus.textContent = `Download requested: ${capture.bytes.length} captured bytes${format === 'txt' ? ' with capture metadata and hexadecimal addresses' : ' as a raw binary file'}.`;
    } catch {
      if (url) URL.revokeObjectURL(url);
      exportStatus.textContent = 'Could not prepare the download. The captured memory remains available; try again.';
    } finally {
      link?.remove();
    }
  };
  downloadBytes.addEventListener('click', () => exportCapture('bin'));
  downloadHex.addEventListener('click', () => exportCapture('txt'));
  let lastStatusRequest = -Infinity;
  const listeners = [];
  const count = value => Number.isSafeInteger(value) && value >= 0 ? value.toLocaleString() : '—';
  const identity = value => typeof value === 'string' && value.trim() ? value : 'Not identified';
  const operationLabels = { input:'Input', pause:'Pause', resume:'Resume', step:'Step', threadCounts:'Thread counts', threadList:'Thread list', memoryRead:'Memory read', registers:'Registers', addressMapping:'Address backing file', disassembly:'Disassembly', breakpoints:'Breakpoints', snapshot:'Snapshot', restore:'Restore' };
  const runtimeOperationAliases = Object.freeze({
  'input.apply':'input', 'execution.pause':'pause', 'execution.resume':'resume', 'execution.step':'step',
  'threads.count':'threadCounts', 'threads.list':'threadList', 'memory.read':'memoryRead',
  'registers.read':'registers', 'memory.mapping':'addressMapping', 'code.disassemble':'disassembly', 'breakpoints.manage':'breakpoints',
  'state.snapshot':'snapshot', 'state.restore':'restore',
});
  const set = (node, text) => { if (node.textContent !== text) node.textContent = text; };
  const resetHistory = () => {
    activityRecords = [];
    history.replaceChildren();
    empty.hidden = false;
    clear.disabled = true;
    retention.textContent = 'Latest 40 observations in this page. Cleared when the session changes.';
  };
  clear.addEventListener('click', resetHistory);
  const resetHandoffs = () => {
    handoffSample = null;
    handoffs.hidden = true;
    for (const field of handoffFields) set(field, '—');
  };
  const renderHandoffs = status => {
    const counters = status?.sdlDispatch;
    if (!counters || !Number.isSafeInteger(counters.calls) || counters.calls <= 0 ||
        !Number.isSafeInteger(status.presents) || status.presents < 0 ||
        ![counters.callerMicros, counters.callbackMicros].every(value => Number.isSafeInteger(value) && value >= 0)) {
      resetHandoffs();
      return;
    }
    const next = { at:status.receivedAt, presents:status.presents, calls:counters.calls, callerMicros:counters.callerMicros, callbackMicros:counters.callbackMicros };
    if (!handoffSample) { handoffSample = next; return; }
    if (next.at === handoffSample.at) return;
    const elapsed = (next.at - handoffSample.at) / 1000;
    const calls = next.calls - handoffSample.calls;
    const caller = next.callerMicros - handoffSample.callerMicros;
    const callback = next.callbackMicros - handoffSample.callbackMicros;
    const presents = next.presents - handoffSample.presents;
    if (elapsed <= 0 || calls <= 0 || caller < 0 || callback < 0 || presents < 0) { resetHandoffs(); return; }
    [calls / elapsed, caller / calls, callback / calls, presents / elapsed].forEach((value, index) => set(handoffFields[index], value.toFixed(1)));
    handoffSample = next;
    handoffs.hidden = false;
  };
  const syncSession = () => {
    const current = game.session || null;
    if (current === session) return;
    session = current;
    descriptor = null;
    lastState = null;
    memoryRequest++;
    memoryBusy = false;
    memoryReady = false;
    lastStatusRequest = -Infinity;
    processSelect.replaceChildren(make('option', '', 'Refresh processes to select a PID'));
    memoryStatus.textContent = '';
    memoryCapture.textContent = '';
    memoryDump.textContent = '';
    memoryDump.hidden = true;
    clearCaptureExport();
    resetBaseline();
    resetHandoffs();
    inputWatch.hidden = true;
    inputTime.removeAttribute('datetime');
    inputTime.textContent = '';
    for (const value of inputFields.values()) value.textContent = '—';
    resetHistory();
  };
  const record = (label, text, error = false) => {
    const received = new Date();
    activityRecords.unshift({ receivedAt:received.toISOString(), session, label, text, error });
    activityRecords.length = Math.min(activityRecords.length, 40);
    const row = make('li', error ? 'dw-error' : '');
    const heading = make('strong', '', label);
    const time = make('time', 'dw-note', received.toLocaleTimeString());
    time.dateTime = received.toISOString();
    time.title = received.toISOString();
    heading.append(make('br'), time);
    row.append(heading, make('span', '', text));
    history.prepend(row);
    while (history.children.length > 40) history.lastElementChild.remove();
    empty.hidden = true;
    clear.disabled = false;
  };
  exportInspection.addEventListener('click', () => {
    syncSession();
    if (!session || (!descriptor && !activityRecords.length && !capturedMemory)) return;
    let url, link;
    try {
      const capture = capturedMemory?.identity.session === session ? capturedMemory : null;
      const candidate = window.__telosRuntimeDiagnostics;
      const observed = candidate?.session === session ? candidate : null;
      const age = observed ? performance.now() - observed.receivedAt : NaN;
      const operations = descriptor?.debugger?.schema === 'telos.debug-capabilities/v1' ? descriptor.debugger.operations : null;
      const serviceStatus = debugRuntime?.capabilities?.();
      const serviceOperations = serviceStatus?.schema === 'telos.debug-adapter-status/v1' && serviceStatus.session === session
        ? Object.fromEntries(Object.entries(serviceStatus.operations || {}).map(([id, value]) => [id, {
          state:['available', 'unsupported', 'unavailable', 'not-qualified'].includes(value?.state) ? value.state : 'unknown',
          source:['live-runtime', 'static-analysis', 'captured-memory'].includes(value?.source) ? value.source : null,
          unit:typeof value?.unit === 'string' ? value.unit : null,
          owner:typeof value?.owner === 'string' ? value.owner : null,
          reason:typeof value?.reason === 'string' ? value.reason : null,
        }])) : null;
      const document = {
        schema:'telos-debugger-inspection/2', exportedAt:new Date().toISOString(),
        session,
        source:'Observed session events and previously captured guest memory',
        target:descriptor?.target || descriptor?.guestId || null,
        backend:descriptor?.backend?.id || null, release:descriptor?.backend?.version || null,
        executionBoundary:descriptor?.executionBoundary || null,
        observation:observed ? {
          receivedAt:Number.isFinite(age) && age >= 0 ? new Date(Date.now() - age).toISOString() : null,
          state:typeof observed.state === 'string' ? observed.state : null, ageMilliseconds:Number.isFinite(age) && age >= 0 ? age : null,
          fresh:Number.isFinite(age) && age >= 0 && age < 5000,
          position:null, unit:descriptor?.executionBoundary || null,
          unknownState:null, error:null, revision:null,
        } : null,
        capabilityQualification:qualification(),
        operationProjection:{ schema:'telos-inspection-operations/1', scope:'runtime-handshake',
          entries:Object.entries(DEBUG_OPERATIONS).map(([id]) => {
            const capability = operations?.[runtimeOperationAliases[id] || id];
            return {
              id, kind:'runtime-operation', enabled:null,
              state:['available', 'unsupported', 'unavailable', 'not-qualified'].includes(capability?.state) ? capability.state : 'unknown',
              unit:typeof capability?.unit === 'string' ? capability.unit : null,
            };
          }),
        },
        backendDetails:{ browserRuntime:{
          capabilitySchema:descriptor?.debugger?.schema || null,
          inputKeys:boundedInputKeys(descriptor?.inputKeys),
          inputAdmission:['quiescent-pause-and-consumption', 'quiescent-pause', 'pre-input-present-baseline', 'legacy-render-drain'].includes(descriptor?.inputAdmission) ? descriptor.inputAdmission : null,
          debugRequestService:serviceOperations ? { schema:'telos.debug-adapter-status/v1', operations:serviceOperations } : null,
          renderedImages:Number.isSafeInteger(observed?.presents) ? observed.presents : null,
          activeWorkers:Number.isSafeInteger(observed?.activeThreads) ? observed.activeThreads : null,
          parkedWorkers:Number.isSafeInteger(observed?.parkedThreads) ? observed.parkedThreads : null,
          threadStateCounts:boundedThreadLifecycle(observed?.threadStateCounts),
        } },
        advertisedCapabilities:Object.fromEntries(Object.keys(operationLabels).map(key => [key, typeof descriptor?.capabilities?.[key] === 'boolean' ? descriptor.capabilities[key] : null])),
        activityOrder:'newest-first', activityLimit:40,
        activity:activityRecords.filter(item => item.session === session),
        memory:capture ? {
          identity:{...capture.identity}, receivedAt:capture.at, pid:capture.pid,
          address:capture.address, length:capture.bytes.length, captureState:'paused',
          encoding:'hex', bytes:Array.from(capture.bytes, byte => byte.toString(16).padStart(2, '0')).join(''),
        } : null,
        recordedState:null,
      };
      url = URL.createObjectURL(new Blob([JSON.stringify(document, null, 2) + '\n'], {type:'application/json'}));
      link = make('a'); link.href = url; link.download = `telos-inspection-${document.exportedAt.replace(/[:.]/g, '-')}.json`;
      link.hidden = true; panel.append(link); link.click();
      const downloadedUrl = url;
      downloadUrls.set(url, setTimeout(() => { URL.revokeObjectURL(downloadedUrl); downloadUrls.delete(downloadedUrl); }, 60000));
      retention.textContent = `Inspection exported: ${document.activity.length} observations and ${capture?.bytes.length || 0} captured memory bytes.`;
    } catch {
      if (url) URL.revokeObjectURL(url);
      retention.textContent = 'Could not export the inspection. Captured observations remain available.';
    } finally { link?.remove(); }
  });
  const render = () => {
    if (!isEnabled()) return;
    syncSession();
    showAdapterMatch();
    exportInspection.disabled = !session || (!descriptor && !activityRecords.length && !capturedMemory);
    const candidate = window.__telosRuntimeDiagnostics;
    const status = session && candidate?.session === session ? candidate : null;
    const age = status ? performance.now() - status.receivedAt : NaN;
    const fresh = Number.isFinite(age) && age >= 0 && age < 5000;
    const terminal = ['error', 'stopped'].includes(status?.state) || ['error', 'stopped'].includes(descriptor?.status);
    renderHandoffs(fresh && !terminal ? status : null);
    const state = !session ? 'Disconnected' : terminal ? 'Stopped' : !fresh ? 'Waiting for status' :
      ({ running:'Running', pausing:'Pausing', paused:'Paused', stepping:'Stepping' }[status.state] || 'Waiting for status');
    set(badge, state);
    set(provenance, !session ? 'No active runtime session. Local executable inspection is available below.' :
      !descriptor ? 'Source: selected runtime. Waiting for its capability handshake.' :
      fresh && !terminal ? 'Source: selected runtime. Status received within the last 5 seconds.' :
      'Source: selected runtime. Status is not live; identity and support below reflect its last handshake.');
    badge.dataset.state = terminal ? 'stopped' : fresh ? status.state : 'waiting';
    const debug = descriptor?.debugger;
    const operations = debug?.schema === 'telos.debug-capabilities/v1' ? debug.operations : null;
    const serviceOperations = debugRuntime?.capabilities?.().operations || {};
    const qualified = qualification();
    const qualificationText = JSON.stringify(qualified);
    if (qualificationText !== lastQualificationText) {
      set(qualificationIdentity, `Session: ${qualified.session || 'Unknown'} · revision: ${qualified.revision ?? 'Unknown'} · target: ${qualified.target?.profile || 'Unknown'} · boundary: ${qualified.boundary?.unit || 'Unknown'}`);
      qualificationList.replaceChildren(...Object.entries(qualified.operations).map(([id, operation]) => {
        const row = make('div');
        const state = operation.state.replaceAll('-', ' ');
        const value = make('dd', '', state.charAt(0).toUpperCase() + state.slice(1) + (operation.unit ? ` · ${operation.unit}` : ''));
        value.dataset.state = operation.state;
        value.title = [operation.reason, ...Object.entries(operation.layers).map(([layer, item]) => `${layer}: ${item.state} · ${item.owner} · ${item.evidenceId} · ${item.provenance?.source || 'Unknown source'} · ${item.reason}`)].join('\n');
        value.tabIndex = 0;
        value.setAttribute('aria-label', `${operation.label}: ${state}. ${operation.reason}`);
        if (Object.keys(operation.layers).length || qualified.error) {
          const evidence = make('details');
          evidence.append(make('summary', '', 'Evidence'));
          evidence.append(make('p', 'dw-note', operation.reason));
          for (const [layer, item] of Object.entries(operation.layers)) {
            evidence.append(make('p', 'dw-note', `${layer}: ${item.state} · ${item.owner} · ${item.evidenceId} · ${item.provenance?.source || 'Unknown source'} · ${item.reason}`));
          }
          value.append(evidence);
        }
        row.append(make('dt', '', operation.label), value);
        return row;
      }));
      lastQualificationText = qualificationText;
    }
    const renderBoundary = descriptor?.executionBoundary === 'render-present';
    const memoryReason = !session ? 'Unavailable: no active game session.' :
      !descriptor ? 'Unavailable: waiting for the selected runtime capability handshake.' :
      descriptor.backend?.id !== 'boxedwine-wasm' ? 'Unavailable: this browser memory bridge supports BoxedWine guest processes only.' :
      operations?.memoryRead?.state !== 'available' ? `Memory read: ${operations?.memoryRead?.state || 'not reported'}. The selected runtime must advertise this operation.` :
      descriptor.capabilityReports != null && qualified.operations['memory.read']?.state !== 'available' ?
        `Memory read is blocked by session qualification: ${qualified.operations['memory.read']?.reason || 'No matching capability evidence.'}` :
      typeof debugRuntime?.request !== 'function' || serviceOperations['memory.read']?.state !== 'available' ? 'Unavailable: the shared debugger runtime has no qualified guest-memory route.' :
      terminal ? 'Unavailable: the selected runtime stopped or reported an error. Reconnect using the game controls, then pause and refresh processes.' :
      !status ? 'Unavailable: waiting for a runtime pause acknowledgement.' :
      status.state !== 'paused' ? 'Pause the game before listing processes or reading guest memory.' :
      !Number.isSafeInteger(status.activeThreads) || status.activeThreads <= 0 || status.parkedThreads !== status.activeThreads ? 'Unavailable: waiting for all guest workers to park.' : '';
    memoryReady = !memoryReason;
    // A successful reply proves a paused capture; later status aging or resuming does not invalidate it.
    if (terminal && memoryBusy) {
      memoryRequest++;
      memoryBusy = false;
      set(memoryStatus, 'Request discarded because the runtime stopped.');
    }
    memoryControls.disabled = !memoryReady || memoryBusy;
    readMemoryButton.disabled = !processSelect.value || !/^\d+$/.test(processSelect.value);
    set(memoryAvailability, memoryReason || (fresh
      ? 'Pause acknowledged. The runtime revalidates that all workers are paused when processing each request.'
      : 'Last acknowledgement: all guest workers paused. Status is stale; each request requires the runtime to revalidate the pause before capturing data.'));
    support.hidden = !operations;
    const supportText = JSON.stringify(operations || null);
    if (supportText !== lastSupportText) {
      supportList.replaceChildren(...Object.entries(operationLabels).map(([key, label]) => {
        const operation = operations?.[key];
        const row = make('div');
        const states = { available:'Available', unsupported:'Unsupported', unavailable:'Unavailable', 'not-qualified':'Not qualified' };
        const state = states[operation?.state] || 'Not reported';
        const unit = typeof operation?.unit === 'string' && operation.unit.trim() ? ` · ${operation.unit}` : '';
        const value = make('dd', '', `${state}${unit}`);
        if (typeof operation?.reason === 'string') value.append(make('p', 'dw-note', operation.reason));
        value.dataset.state = operation?.state || 'unknown';
        row.append(make('dt', '', label), value);
        return row;
      }));
      lastSupportText = supportText;
    }
    set(boundaryNote, renderBoundary ? 'A render step advances to the next presented image. It is not a CPU instruction step. Runtime snapshot and restore support is listed separately from score history.' : '');
    set(fields.get('Session'), session || 'Not connected');
    set(fields.get('Target'), identity(descriptor?.target?.profile || descriptor?.guestId));
    set(fields.get('Executable'), identity(descriptor?.target?.executable));
    set(fields.get('Build SHA-256'), identity(descriptor?.target?.sha256));
    set(fields.get('Backend'), identity(descriptor?.backend?.id));
    set(fields.get('Release'), identity(descriptor?.backend?.version));
    set(fields.get('Format'), identity(descriptor?.target?.format));
    set(fields.get('Architecture'), identity(descriptor?.target?.architecture));
    set(fields.get('OS / ABI'), identity(descriptor?.target?.osAbi));
    const pointerBits = descriptor?.target?.pointerBits, byteOrder = descriptor?.target?.byteOrder;
    set(fields.get('Pointer width'), [8,16,32,64,128].includes(pointerBits) ? `${pointerBits} bits` : 'Unavailable');
    set(fields.get('Byte order'), byteOrder === 'little' ? 'Little endian' : byteOrder === 'big' ? 'Big endian' : 'Unavailable');
    set(fields.get('Execution boundary'), renderBoundary ? 'Render presentation' : typeof descriptor?.executionBoundary === 'string' ? descriptor.executionBoundary : 'Not qualified');
    const inputKeys = boundedInputKeys(descriptor?.inputKeys);
    const admissionLabels = new Map([['quiescent-pause-and-consumption','Complete worker pause; native consumption then next GL presentation'], ['quiescent-pause','Complete worker pause'], ['pre-input-present-baseline','Pre-input render baseline'], ['legacy-render-drain','Legacy render drain']]);
    set(fields.get('Input admission'), admissionLabels.get(descriptor?.inputAdmission) || 'Unavailable');
    set(fields.get('Score keys'), inputKeys ? inputKeys.join(' ') : 'Unavailable');
    fields.get('Rendered images').parentElement.hidden = !renderBoundary;
    fields.get('Parked workers').parentElement.hidden = debug ? operations?.threadCounts?.state !== 'available' : !renderBoundary;
    set(fields.get('Rendered images'), fresh && !terminal ? count(status.presents) : '—');
    set(fields.get('Parked workers'), fresh && !terminal ? `${count(status.parkedThreads)} / ${count(status.activeThreads)}` : '—');
    const lifecycle = fresh && !terminal ? boundedThreadLifecycle(status?.threadStateCounts) : null;
    fields.get('Worker lifecycle snapshot').parentElement.hidden = !lifecycle;
    set(fields.get('Worker lifecycle snapshot'), lifecycle
      ? `Starting ${lifecycle.starting} · Running ${lifecycle.running} · Kernel wait ${lifecycle.kernelWait} · Paused ${lifecycle.paused} · Exiting ${lifecycle.exiting} · Total ${lifecycle.total}` : '—');
    fields.get('Worker lifecycle snapshot').title = 'One backend lifecycle snapshot. Counts do not identify guest threads, instruction locations or wait causes. The parked-worker counters are sampled separately.';
    const caps = descriptor?.capabilities || {};
    const labels = [];
    if (operations) {
      for (const [key, label] of Object.entries(operationLabels)) {
        const operation = operations[key];
        if (operation?.state !== 'available') continue;
        const unit = typeof operation?.unit === 'string' && operation.unit.trim() ? ` (${operation.unit})` : '';
        labels.push({ text:`${label}${unit}: Available`, state:'available' });
      }
    } else if (!debug) {
      if (caps.input === true) labels.push({ text:'Score input: Available', state:'available' });
      if (caps.pause === true) labels.push({ text:'Pause: Available', state:'available' });
      if (caps.resume === true) labels.push({ text:'Resume: Available', state:'available' });
      if (caps.renderStep === true && renderBoundary) labels.push({ text:'Render step: Available', state:'available' });
    }
    const capabilityText = JSON.stringify(labels);
    if (capabilityText !== lastCapabilityText) {
      capabilities.replaceChildren(...labels.map(({ text, state }) => {
        const item = make('li', 'dw-capability', text);
        item.dataset.state = state;
        return item;
      }));
      lastCapabilityText = capabilityText;
    }
    set(note, !descriptor ? 'Waiting for a capability handshake from the selected session.' :
      debug && !operations ? 'Capability descriptor version unavailable or unrecognized.' :
      labels.length ? 'These operations are advertised by the session. Use the game controls above to control execution.' : 'No available operations were advertised by this session.');
  };
  processSelect.addEventListener('change', () => {
    clearCaptureExport();
    resetBaseline();
    memoryDump.hidden = true;
    memoryDump.textContent = '';
    memoryCapture.textContent = '';
    memoryStatus.textContent = '';
    render();
  });
  refreshProcesses.addEventListener('click', async () => {
    render();
    if (!memoryReady || memoryBusy) return;
    const request = ++memoryRequest;
    const selectedSession = session;
    const previousPid = processSelect.value;
    memoryBusy = true;
    memoryStatus.textContent = 'Revalidating the runtime pause and listing guest processes…';
    render();
    try {
      const processes = (await requestDebug('processes.list', {}))?.processes;
      render();
      if (request !== memoryRequest || selectedSession !== session) return;
      if (!Array.isArray(processes) || processes.length > 4096 || processes.some(value => !Number.isSafeInteger(value?.pid) || value.pid <= 0 || value.pid > 0xffffffff)) throw new Error('The runtime returned an invalid process list.');
      const pids = processes.map(value => value.pid);
      processSelect.replaceChildren(...pids.map(pid => {
        const option = make('option', '', `PID ${pid}`);
        option.value = String(pid);
        return option;
      }));
      if (pids.some(pid => String(pid) === previousPid)) processSelect.value = previousPid;
      clearCaptureExport();
      resetBaseline();
      memoryDump.hidden = true;
      memoryDump.textContent = '';
      memoryCapture.textContent = '';
      memoryStatus.textContent = pids.length ? `${pids.length} guest processes. Select a PID and address to read.` : 'No guest processes were returned.';
    } catch (error) {
      if (request === memoryRequest) memoryStatus.textContent = `Could not list processes: ${error.message || 'request failed'}. Confirm the game is paused, then refresh processes again.`;
    } finally {
      if (request === memoryRequest) { memoryBusy = false; render(); }
    }
  });
  memoryForm.addEventListener('submit', async event => {
    event.preventDefault();
    render();
    if (!memoryReady || memoryBusy) return;
    const pid = Number(processSelect.value), addressText = addressInput.value.trim(), lengthText = lengthInput.value.trim();
    if (!Number.isSafeInteger(pid) || pid <= 0 || pid > 0xffffffff || ![...processSelect.options].some(option => option.value === String(pid))) {
      memoryStatus.textContent = 'Refresh processes and select a guest PID.';
      return;
    }
    if (!/^(?:0x)?[0-9a-f]{1,8}$/i.test(addressText)) {
      memoryStatus.textContent = 'Enter a 32-bit hexadecimal guest address, such as 0x00400000.';
      addressInput.focus();
      return;
    }
    const address = Number.parseInt(addressText.replace(/^0x/i, ''), 16), length = Number(lengthText);
    if (address === 0) {
      memoryStatus.textContent = 'Address zero cannot be read. Enter a nonzero guest address.';
      addressInput.focus();
      return;
    }
    if (!/^\d+$/.test(lengthText) || !Number.isSafeInteger(length) || length < 1 || length > 65536 || address + length > 0x100000000) {
      memoryStatus.textContent = 'Read 1–65536 bytes without crossing the 32-bit address boundary.';
      lengthInput.focus();
      return;
    }
    const request = ++memoryRequest;
    const selectedSession = session;
    memoryBusy = true;
    // Bind export provenance to the request, not whichever descriptor is later visible.
    const captureIdentity = Object.freeze({
      session: selectedSession,
      target: identity(descriptor?.target?.profile || descriptor?.guestId),
      backend: identity(descriptor?.backend?.id),
      release: identity(descriptor?.backend?.version),
    });
    clearCaptureExport();
    memoryDump.hidden = true;
    memoryDump.textContent = '';
    memoryCapture.textContent = '';
    memoryStatus.textContent = 'Revalidating the runtime pause and capturing guest memory…';
    render();
    try {
      const bytes = await readMemory(pid, address, length);
      render();
      if (request !== memoryRequest || selectedSession !== session) return;
      if (!(bytes instanceof Uint8Array) || bytes.length !== length) throw new Error('The runtime returned an incomplete or invalid memory read.');
      const lines = [];
      for (let offset = 0; offset < bytes.length; offset += 16) {
        const chunk = Array.from(bytes.subarray(offset, offset + 16));
        const hex = chunk.map(byte => byte.toString(16).padStart(2, '0')).join(' ').padEnd(47, ' ');
        const ascii = chunk.map(byte => byte >= 32 && byte <= 126 ? String.fromCharCode(byte) : '.').join('');
        lines.push(`${(address + offset).toString(16).padStart(8, '0')}  ${hex}  |${ascii}|`);
      }
      const capturedAt = new Date();
      capturedMemory = { identity: captureIdentity, pid, address, at: capturedAt.toISOString(), bytes: bytes.slice(), hex: lines.join('\n') };
      valueInspector.setCapture(capturedMemory);
      memorySearch.setCapture(capturedMemory);
      renderComparison();
      downloadBytes.disabled = false;
      downloadHex.disabled = false;
      exportStatus.textContent = 'Download raw bytes or hex text with capture details.';
      memoryDump.textContent = capturedMemory.hex;
      memoryDump.hidden = false;
      memoryCapture.textContent = `Received ${capturedAt.toLocaleTimeString()} · ${captureIdentity.target} · session ${captureIdentity.session} · PID ${pid} · 0x${address.toString(16)} · ${length} bytes`;
      memoryStatus.textContent = 'Memory captured.';
    } catch (error) {
      if (request === memoryRequest) memoryStatus.textContent = `Could not read guest memory: ${error.message || 'request failed'}. Confirm the pause, refresh the PID list and check that this address range is readable.`;
    } finally {
      if (request === memoryRequest) { memoryBusy = false; render(); }
    }
  });
  const listen = (name, handler) => {
    const guarded = event => { if (isEnabled()) handler(event); };
    addEventListener(name, guarded);
    listeners.push([name, guarded]);
  };
  listen('telos:runtime-session', event => {
    syncSession();
    const value = event.detail;
    if (!session || value?.schema !== 'telos-runtime-session/v1' || value.session !== session) return;
    descriptor = value;
    if (['running', 'pausing', 'paused', 'stepping', 'error', 'stopped'].includes(value.status) && value.status !== lastState) {
      record('Runtime', value.status, value.status === 'error');
      lastState = value.status;
    }
    render();
  });
  listen('telos:wasm-release', event => {
    syncSession();
    const value = event.detail;
    if (!descriptor || !session || value?.schema !== 'telos.input-release-observation/v1' || value.session !== session
        || !Number.isSafeInteger(value.sequence) || value.sequence < 1) return;
    const meanings = {
      'input-dispatch':'Release dispatched; native consumption was not reported.',
      'queued-native-events':'Release events queued; native consumption is pending.',
      'consumed-native-events':'Release events consumed; a following presentation was not qualified.',
      'no-held-input-events':'Release generated no held-input events; this is not a neutral gameplay tick.',
    };
    if (value.boundary !== null && !Object.hasOwn(meanings, value.boundary)) return;
    record('Input release', `Sequence ${value.sequence} · ${meanings[value.boundary] || 'Runtime acknowledged release; its consumption boundary was not reported.'}`);
  });
  listen('telos:wasm-ack', event => {
    syncSession();
    const value = event.detail;
    if (!descriptor || !session || value?.session !== session) return;
    if (![value.tick, value.present, value.eventsConsumed].every(item => Number.isSafeInteger(item) && item >= 0)) return;
    set(inputFields.get('Score tick'), count(value.tick));
    set(inputFields.get('Render present'), count(value.present));
    set(inputFields.get('Consumed events'), count(value.eventsConsumed));
    const requested = value.requestedInput;
    const allowedKeys = new Set(['w','a','s','d','q','e','space','tab','esc','enter','up','down','left','right','1','2','3','4','5','6','7','8','9','i']);
    const validRequested = requested?.schema === 'telos.requested-input/v1' && requested.source === 'workspace-request'
      && Array.isArray(requested.keys) && requested.keys.length <= 24
      && Array.from(requested.keys).every(key => allowedKeys.has(key)) && new Set(requested.keys).size === requested.keys.length
      && Array.isArray(requested.buttons) && requested.buttons.length <= 2
      && Array.from(requested.buttons).every(button => ['lmb','rmb'].includes(button))
      && new Set(requested.buttons).size === requested.buttons.length
      && (requested.mouse === null || [requested.mouse?.x,requested.mouse?.y].every(n => Number.isSafeInteger(n) && n >= 0 && n <= 9999));
    set(inputFields.get('Requested keys'), validRequested ? requested.keys.join(' ') || 'None' : 'Unavailable');
    set(inputFields.get('Requested buttons'), validRequested ? requested.buttons.join(' ') || 'None' : 'Unavailable');
    set(inputFields.get('Requested mouse'), validRequested ? requested.mouse ? `${requested.mouse.x},${requested.mouse.y} canvas pixels` : 'No position update' : 'Unavailable');
    set(inputFields.get('GL presentations during input'), Number.isSafeInteger(value.presentsAdvanced) ? count(value.presentsAdvanced) : '—');
    const r = value.presentationReceipt;
    const validReceipt = r?.schema === 'telos.input-consumption-present/v1' && Number.isSafeInteger(r.total)
      && r.total >= 1 && r.total <= 4096 && r.requiresConsumption === true
      && Number.isSafeInteger(r.beforeConsumption) && r.beforeConsumption >= 0 && r.afterConsumption === 1
      && r.total === r.beforeConsumption + 1;
    set(inputFields.get('GL presentations before consumption'), validReceipt ? count(r.beforeConsumption) : '—');
    set(inputFields.get('GL presentations after consumption'), validReceipt ? '1' : '—');
    set(inputFields.get('Round trip'), Number.isFinite(value.latencyMs) && value.latencyMs >= 0 ? `${value.latencyMs.toFixed(1)} ms` : '—');
    const received = new Date();
    inputTime.dateTime = received.toISOString();
    inputTime.textContent = `Received ${received.toLocaleTimeString()}`;
    inputWatch.hidden = false;
    const latency = Number.isFinite(value.latencyMs) && value.latencyMs >= 0 ? ` · ${value.latencyMs.toFixed(1)} ms` : '';
    record('Input acknowledged', `Score tick ${count(value.tick)} · present ${count(value.present)} · ${count(value.eventsConsumed)} events consumed${validRequested ? ` · requested keys ${requested.keys.join(' ') || 'None'}, buttons ${requested.buttons.join(' ') || 'None'}, mouse ${requested.mouse ? `${requested.mouse.x},${requested.mouse.y}` : 'unchanged'}` : ''}${validReceipt ? ` · GL presents ${r.total} (${r.beforeConsumption} before consumption, 1 after)` : ''}${latency}`);
  });
  listen('telos:wasm-transport-error', event => {
    syncSession();
    const value = event.detail;
    if (!descriptor || !session || value?.session !== session || typeof value.message !== 'string') return;
    record('Transport error', value.message.slice(0, 240), true);
  });
  const refresh = () => {
    if (!isEnabled() || document.hidden || panel.closest('[data-dock-id]')?.hidden) return;
    render();
    const status = window.__telosRuntimeDiagnostics;
    const now = performance.now();
    // Paused guests may emit no changes; request real snapshots before their proof expires.
    if (session && descriptor?.capabilities?.pause === true &&
        !['error', 'stopped'].includes(descriptor.status) &&
        !(status?.session === session && ['running', 'error', 'stopped'].includes(status.state)) &&
        now - lastStatusRequest >= 1000) {
      lastStatusRequest = now;
      dispatchEvent(new CustomEvent('telos:wasm-vm-control', { detail:{ action:'status' } }));
    }
  };
  const timer = setInterval(refresh, 500);
  render();
  return { destroy() {
    adapterInspection++;
    inspection++;
    memoryRequest++;
    clearInterval(timer);
    clearCaptureExport();
    resetBaseline();
    for (const [url, timeout] of downloadUrls) {
      clearTimeout(timeout);
      URL.revokeObjectURL(url);
    }
    downloadUrls.clear();
    for (const [name, handler] of listeners) removeEventListener(name, handler);
    panel.remove();
  } };
}

// Search an immutable capture locally; this never scans or changes the live process.
function createCaptureSearch(syncSession) {
  const make = (tag, text = '') => {
    const node = document.createElement(tag);
    node.textContent = text;
    return node;
  };
  const element = make('details');
  element.className = 'dw-capture-search';
  element.hidden = true;
  element.append(make('summary', 'Find in captured memory'));
  const form = make('form');
  form.className = 'dw-memory-form';
  const controls = make('fieldset');
  controls.append(make('legend', 'Exact byte search · captured range only'));
  const modeLabel = make('label', 'Search format');
  const mode = make('select');
  for (const [value, text] of [['hex', 'Hex bytes'], ['text', 'Text (UTF-8, case sensitive)']]) {
    const option = make('option', text);
    option.value = value;
    mode.append(option);
  }
  modeLabel.append(mode);
  const queryLabel = make('label', 'Find');
  const query = make('input');
  query.type = 'text';
  query.maxLength = 767;
  query.autocomplete = 'off';
  query.spellcheck = false;
  query.placeholder = '4D 5A';
  queryLabel.append(query);
  const find = make('button', 'Find all');
  find.type = 'submit';
  find.className = 'btn small';
  controls.append(modeLabel, queryLabel, find);
  form.append(controls);
  const navigation = make('div');
  navigation.className = 'dw-capture-actions';
  const previous = make('button', 'Previous match');
  const next = make('button', 'Next match');
  for (const button of [previous, next]) {
    button.type = 'button';
    button.className = 'btn small';
    button.disabled = true;
  }
  navigation.append(previous, next);
  const status = make('p', 'Search up to 256 bytes. Hex accepts complete byte pairs with optional spaces.');
  status.className = 'dw-note';
  status.setAttribute('role', 'status');
  const context = make('pre');
  context.className = 'dw-memory-dump dw-match-context';
  context.tabIndex = 0;
  context.hidden = true;
  context.setAttribute('aria-label', 'Selected match and surrounding captured bytes; matching bytes are highlighted');
  element.append(form, navigation, status, context);
  let capture = null, matches = [], selected = 0, width = 0;
  const reset = message => {
    matches = [];
    selected = 0;
    previous.disabled = next.disabled = true;
    context.hidden = true;
    context.replaceChildren();
    query.removeAttribute('aria-invalid');
    status.textContent = message;
  };
  const render = () => {
    const offset = matches[selected];
    previous.disabled = selected === 0;
    next.disabled = selected >= matches.length - 1;
    status.textContent = `Match ${selected + 1} of ${matches.length} · address 0x${(capture.address + offset).toString(16).padStart(8, '0')} · offset ${offset} · ${width} bytes · PID ${capture.pid}.`;
    const start = Math.max(0, Math.floor(offset / 16) * 16 - 16);
    const end = Math.min(capture.bytes.length, Math.ceil((offset + width) / 16) * 16 + 16);
    const fragments = [];
    for (let row = start; row < end; row += 16) {
      fragments.push(document.createTextNode(`${(capture.address + row).toString(16).padStart(8, '0')}  `));
      for (let index = row; index < Math.min(row + 16, end); index++) {
        const text = capture.bytes[index].toString(16).padStart(2, '0');
        fragments.push(index >= offset && index < offset + width ? make('mark', text) : document.createTextNode(text));
        fragments.push(document.createTextNode(' '));
      }
      fragments.push(document.createTextNode('\n'));
    }
    context.replaceChildren(...fragments);
    context.hidden = false;
  };
  form.addEventListener('submit', event => {
    event.preventDefault();
    syncSession();
    if (!capture) return;
    reset('');
    let pattern;
    if (mode.value === 'hex') {
      const hex = query.value.trim();
      if (/^(?:[0-9a-f]{2}\s*)+$/i.test(hex)) {
        pattern = Uint8Array.from(hex.match(/[0-9a-f]{2}/gi), byte => parseInt(byte, 16));
      }
    } else {
      pattern = new TextEncoder().encode(query.value);
    }
    if (!pattern?.length || pattern.length > 256) {
      query.setAttribute('aria-invalid', 'true');
      status.textContent = mode.value === 'hex'
        ? 'Enter 1–256 complete hex bytes, for example 4D 5A or 4D5A.'
        : 'Enter text encoding to 1–256 UTF-8 bytes. Spaces and letter case are matched exactly.';
      query.focus();
      return;
    }
    width = pattern.length;
    const bytes = capture.bytes;
    // Capture size is bounded by the 64 KiB read limit; preserve overlapping matches.
    for (let offset = 0; offset <= bytes.length - width; offset++) {
      let index = 0;
      while (index < width && bytes[offset + index] === pattern[index]) index++;
      if (index === width) matches.push(offset);
    }
    if (matches.length) render();
    else status.textContent = `No matches in these ${bytes.length} captured bytes. Read another range to search elsewhere.`;
  });
  const move = delta => {
    syncSession();
    if (!capture || selected + delta < 0 || selected + delta >= matches.length) return;
    selected += delta;
    render();
  };
  previous.addEventListener('click', () => move(-1));
  next.addEventListener('click', () => move(1));
  query.addEventListener('input', () => reset('Press Find all to search this capture.'));
  mode.addEventListener('change', () => {
    query.placeholder = mode.value === 'hex' ? '4D 5A' : 'Case-sensitive text';
    reset('Press Find all to search this capture.');
  });
  return { element, setCapture(value) {
    capture = value;
    element.hidden = !capture;
    reset(capture ? `Search this ${capture.bytes.length}-byte capture. Patterns are limited to 256 bytes.` : '');
  } };
}
