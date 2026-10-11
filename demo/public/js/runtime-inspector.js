// Read the session owner's accepted status; never establish a second runtime controller.
export function mountRuntimeInspector(game) {
  const panel = document.createElement('details');
  panel.className = 'runtime-inspector';
  panel.dataset.telosDockWidget = 'runtime-inspector';
  panel.dataset.telosDockTitle = 'Session inspector';
  const summary = document.createElement('summary');
  summary.textContent = 'Session inspector';
  const grid = document.createElement('dl');
  const fields = new Map();
  for (const label of ['Execution', 'Rendered images', 'Parked workers', 'Last input', 'Input latency', 'Input events consumed']) {
    const row = document.createElement('div');
    const name = document.createElement('dt'); name.textContent = label;
    const value = document.createElement('dd'); value.textContent = '—';
    row.append(name, value); grid.append(row); fields.set(label, value);
  }
  panel.append(summary, grid);
  game.root.append(panel);
  window.TelosDock?.register('runtime-inspector', 'Session inspector', panel);
  const storageKey = 'telos.runtime-inspector.expanded';
  try { panel.open = localStorage.getItem(storageKey) === 'true'; } catch { /* Storage is optional. */ }
  let timer = 0, lastAck = null, ackSession = null;
  let active = document.querySelector('#tab-original')?.getAttribute('aria-selected') === 'true';
  const set = (label, value) => {
    const node = fields.get(label);
    if (node.textContent !== value) node.textContent = value;
  };
  const count = value => Number.isSafeInteger(value) && value >= 0 ? value.toLocaleString() : '—';
  const render = () => {
    const candidate = window.__telosRuntimeDiagnostics;
    const status = game.session && candidate?.session === game.session ? candidate : null;
    const terminal = status && ['error', 'stopped'].includes(status.state);
    const fresh = status && performance.now() - status.receivedAt < 5000;
    const ack = game.session && ackSession === game.session ? lastAck : null;
    set('Execution', !game.session ? 'Disconnected' : terminal ? 'Stopped' : !fresh ? 'Waiting for status' : status.state);
    set('Rendered images', fresh ? count(status.presents) : '—');
    set('Parked workers', fresh ? `${count(status.parkedThreads)} / ${count(status.activeThreads)}` : '—');
    set('Last input', ack ? `Score ${count(ack.tick)} · present ${count(ack.present)}` : '—');
    set('Input latency', ack && Number.isFinite(ack.latencyMs) ? `${ack.latencyMs.toFixed(1)} ms` : '—');
    set('Input events consumed', ack ? count(ack.eventsConsumed) : '—');
  };
  const schedule = () => {
    clearInterval(timer);
    timer = 0;
    if (panel.open && active && !document.hidden) {
      render();
      timer = setInterval(render, 500);
    }
  };
  panel.addEventListener('toggle', () => {
    try { localStorage.setItem(storageKey, String(panel.open)); } catch { /* Keep the current layout. */ }
    schedule();
  });
  document.addEventListener('visibilitychange', schedule);
  addEventListener('telos:wasm-target', event => {
    active = event.detail?.active === true;
    schedule();
  });
  addEventListener('telos:wasm-ack', event => {
    lastAck = event.detail;
    ackSession = game.session;
  });
  schedule();
}
