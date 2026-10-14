import { DEBUG_OPERATIONS, evaluateAdapterRequirements } from '../debugger/debugger-contract.js';

const stateLabel = value => ({ available:'Available', unsupported:'Unsupported', unavailable:'Unavailable',
  'not-qualified':'Not qualified', unknown:'Not reported' }[value] || 'Not reported');

// This directory describes runtime support. It never dispatches debugger commands.
export function createCapabilityDirectory(make, open) {
  const element = make('details', 'dw-support');
  element.open = true;
  element.append(make('summary', '', 'Inspection areas'));
  const source = make('p', 'dw-note');
  const list = make('dl', 'dw-support-list');
  const errors = make('p', 'dw-note');
  element.append(source, errors, list);
  const areas = [
    ['Memory', ['memory.read', 'memory.search', 'memory.write', 'memory.watch.list', 'memory.watch.add', 'memory.watch.remove'],
      ['memoryRead', null, null, null, null, null], 'memory', 'Open memory',
      'Paused process reads use the existing guest-memory bridge. Search and value comparison apply to explicit captures. Live memory writes and backend watchpoints are not implemented.'],
    ['Threads', ['threads.count', 'threads.list'], ['threadCounts', 'threadList'], 'threads', 'Open threads',
      'The Threads panel captures process-scoped guest thread rows when the selected adapter supports it. Capture requires the runtime pause barrier. Worker counts alone do not identify a thread or its wait cause.'],
    ['Modules', ['modules.list'], [null], 'file', 'Inspect file layout',
      'No live module-list operation is wired here. Local sections and declared libraries describe file headers only.'],
    ['Address backing file', ['memory.mapping'], ['addressMapping'], 'address-mapping', 'Open backing-file lookup',
      'Capture the backing file for an explicit process address when the runtime supports the paused metadata ABI. The result does not identify a loaded executable or provide a Windows module list.'],
    ['Registers', ['registers.read'], ['registers'], 'registers', 'Open registers',
      'The Registers panel captures process-scoped saved guest registers when the selected adapter supports it. The current KOTOR adapter supplies x86 EIP and general registers under the pause barrier; flags, floating-point and SIMD state are not included. This is a captured snapshot, not a live watch or a guaranteed next instruction.'],
    ['Disassembly / symbols', ['code.disassemble', 'symbols.list', 'symbols.resolve', 'source.resolve'], ['disassembly', null, null, null], null, null,
      'This browser has no live disassembly, symbol lookup or source-navigation bridge. Static executable sections are not live code views.'],
    ['Break / step / watch', ['execution.pause', 'execution.resume', 'execution.step', 'breakpoints.manage',
      'breakpoints.list', 'breakpoints.add', 'breakpoints.remove'], ['pause', 'resume', 'step', 'breakpoints', null, null, null], null, null,
      'Use the Debug execution panel for supported pause, resume and step operations. Step units are shown above. No breakpoint or watchpoint editor is wired here.'],
    ['Session activity', ['input.apply', 'trace.read', 'events.read', 'input.timeline'], ['input', null, null, null], 'activity', 'Open activity',
      'View session changes, transport errors and acknowledged input received by this page; this is not a complete runtime event or input timeline.'],
    ['Snapshots / restore', ['state.snapshot', 'state.restore'], ['snapshot', 'restore'], null, null,
      'This browser has no runtime snapshot or restore command. Memory captures and score history are not runtime snapshots.'],
  ];
  let last = '';
  return { element, update({ session, descriptor, resolution, error }) {
    const operations = descriptor?.debugger?.schema === 'telos.debug-capabilities/v1' ? descriptor.debugger.operations : null;
    const key = JSON.stringify([session, operations, resolution, error]);
    if (key === last) return;
    last = key;
    source.textContent = !session ? 'No selected live session. Local file inspection remains available.' :
      resolution ? `Session ${session} · capability intersection at revision ${resolution.revision}. Runtime advertisements are listed separately.` :
      operations ? `Session ${session} · runtime advertisements only. Cross-host qualification has not been supplied.` :
      `Session ${session} · waiting for a recognized capability handshake.`;
    errors.textContent = error ? `Capability reports could not be resolved: ${error}. Use the runtime handshake below for its declarations; no extra operations are enabled.` :
      resolution && Object.keys(resolution.reportErrors).length ?
        Object.entries(resolution.reportErrors).map(([layer, reason]) => `${layer}: ${reason}`).join(' ') : '';
    list.replaceChildren(...areas.map(([label, ids, legacyIds, destination, action, scope]) => {
      const row = make('div');
      const value = make('dd');
      const reasons = new Set();
      const states = ids.map((id, index) => {
        const qualified = resolution ? resolution.operations[id] || { state:'unavailable', reason:'No owner-authored reports for this operation.' } : null;
        const advertised = operations?.[legacyIds[index]];
        const item = qualified || advertised;
        if (item?.state !== 'available' && typeof item?.reason === 'string') reasons.add(item.reason);
        return `${DEBUG_OPERATIONS[id]}: ${stateLabel(item?.state)}${item?.unit ? ` (${item.unit})` : ''}${qualified ? ' · qualified registry' : ' · runtime advertisement'}`;
      });
      value.append(make('span', '', states.length ? states.join('; ') : 'No live operation wired'));
      if (reasons.size) value.append(make('p', 'dw-note', [...reasons].join(' ')));
      value.append(make('p', 'dw-note', scope));
      // Navigation only. The reader still owns operation and pause admission.
      const navigationCapability = {
        memory:['memory.read', 'memoryRead'], threads:['threads.list', 'threadList'], registers:['registers.read', 'registers'],
        'address-mapping':['memory.mapping', 'addressMapping'],
      }[destination];
      const navigationAvailable = !navigationCapability || (resolution
        ? resolution.operations[navigationCapability[0]]?.state === 'available'
        : operations?.[navigationCapability[1]]?.state === 'available');
      if (destination && navigationAvailable) {
        const button = make('button', 'btn small', action);
        button.type = 'button';
        button.addEventListener('click', () => open(destination));
        value.append(button);
      }
      row.append(make('dt', '', label), value);
      return row;
    }));
    const extensions = Object.entries(resolution?.operations || {}).filter(([id]) => !Object.hasOwn(DEBUG_OPERATIONS, id));
    if (extensions.length) {
      const row = make('div'), value = make('dd');
      row.append(make('dt', '', 'Reported extensions'), value);
      for (const [id, operation] of extensions) {
        value.append(make('p', 'dw-note', `${id}: ${stateLabel(operation.state)}${operation.unit ? ` (${operation.unit})` : ''}. ${operation.reason}`));
      }
      value.append(make('p', 'dw-note', 'Declarations only; this browser has no command bridge for these extensions.'));
      list.append(row);
    }
  } };
}

export function createAdapterDetails(make, registry, local = false) {
  const element = make('details', 'dw-support');
  element.append(make('summary', '', local ? 'Local file adapter match' : 'Selected session adapter'));
  const status = make('p', 'dw-note');
  const requirements = make('dl', 'dw-support-list');
  element.append(status, requirements);
  let last = '';
  return { element, update(target, resolution) {
    const key = JSON.stringify([target, resolution, registry.list()]);
    if (key === last) return;
    last = key;
    requirements.replaceChildren();
    if (!target) {
      status.textContent = local ? 'Choose a local executable to inspect its header identity.' :
        'The selected session has not supplied a target identity. A local file selection does not identify the running target.';
      return;
    }
    let match;
    try { match = registry.match(target); }
    catch (error) { status.textContent = `Target identity cannot be matched: ${error.message}`; return; }
    if (!match.adapter) {
      status.textContent = `${match.reason} Registered manifests: ${registry.list().length}. Load a declarative manifest in local executable inspection below.`;
      return;
    }
    // Header-only local inspection cannot establish that this is the running binary.
    const evaluation = evaluateAdapterRequirements(match, local ? null : resolution);
    status.textContent = `${match.adapter.id} ${match.adapter.version} · ${match.state.replaceAll('-', ' ')}. ${match.reason} ` +
      (local ? 'Local file only; its relationship to the selected session is unverified. Prerequisites below are declarations, not live support.' : evaluation.reason);
    for (const id of match.adapter.requires) {
      const operation = local ? null : resolution?.operations[id];
      const row = make('div'), value = make('dd', '', local ? 'Requires a bound live target' : stateLabel(operation?.state));
      value.dataset.state = operation?.state || 'unknown';
      value.append(make('p', 'dw-note', local ? 'Inspect the selected session adapter above for live prerequisites.' :
        operation?.reason || 'No resolved capability evidence. The runtime must supply matching transport, backend, target and policy reports.'));
      row.append(make('dt', '', DEBUG_OPERATIONS[id] || id), value);
      requirements.append(row);
    }
    if (!match.adapter.requires.length) status.textContent += ' No runtime prerequisites declared.';
    if (match.adapter.interprets.length) status.textContent += ` Declared interpretations: ${match.adapter.interprets.join(', ')}. Interpretation code is not loaded.`;
  } };
}
