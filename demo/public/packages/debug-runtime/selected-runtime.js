import { createBrokerDebugRuntime } from './broker-adapter.js';
import { DebugOperationError } from './runtime.js';

// Core supplies the fallback. Trusted target packages receive the owner
// service separately; uploaded manifests never supply executable handlers.
export function createSelectedDebugRuntime(fallback) {
  for (const method of ['getSession', 'getTarget', 'getStatus', 'capabilities', 'request']) {
    if (typeof fallback?.[method] !== 'function') throw new TypeError(`Debugger host requires ${method}.`);
  }
  let selected = null, generation = 0;
  const listeners = new Set();
  const current = () => selected?.runtime || fallback;
  const notify = () => {
    for (const listener of [...listeners]) {
      try { listener(); } catch (error) { console.error('Debugger selection listener failed:', error); }
    }
  };
  const close = binding => {
    try { binding?.broker.close(new DOMException('Debugger selection changed.', 'AbortError')); }
    catch (error) {
      // Selection/reply invalidation is already committed. A failing trusted
      // disposer must not strand the new owner's attachment without a lease.
      console.error('Debugger broker disposal failed:', error);
    }
  };
  const runtime = Object.freeze({
    getSession:() => current().getSession(),
    getTarget:() => current().getTarget(),
    getStatus:() => current().getStatus(),
    capabilities:() => current().capabilities(),
    getGame:() => selected ? null : fallback.getGame?.() || null,
    workbenchAvailable:() => selected === null,
    mountWorkbench(options = {}) {
      if (selected || typeof fallback.mountWorkbench !== 'function') return null;
      return fallback.mountWorkbench({ ...options,
        isEnabled:() => selected === null && (options.isEnabled?.() ?? true) });
    },
    subscribe(listener) {
      if (typeof listener !== 'function') throw new TypeError('A debugger selection listener is required.');
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    async request(request, options) {
      const owner = selected, revision = generation, service = current();
      const session = service.getSession();
      if (!session || session !== request?.session) {
        throw new DebugOperationError('DEBUG_SESSION_CHANGED', 'The debugger selection is unavailable or changed.');
      }
      const reply = await service.request(request, options);
      if (owner !== selected || revision !== generation || service.getSession() !== session) {
        throw new DebugOperationError('DEBUG_SESSION_CHANGED', 'The debugger selection changed while the request was running.');
      }
      return reply;
    },
  });
  const owner = Object.freeze({
    getSelectionRevision() { return generation; },
    attachBroker(broker, options = {}) {
      if (typeof broker?.close !== 'function' || broker.state !== 'available') {
        throw new TypeError('An active revocable debugger broker is required.');
      }
      // Validate the new service before touching the prior binding.
      const next = { broker, runtime:createBrokerDebugRuntime(broker, options) };
      if (broker.state !== 'available') throw new Error('The debugger broker changed during attachment.');
      if (selected?.broker === broker) throw new Error('This debugger broker is already attached.');
      const previous = selected;
      selected = next; generation++;
      try { close(previous); } finally { notify(); }
      return Object.freeze({
        detach() {
          // A superseded package cannot detach a later owner's selection.
          if (selected !== next) return false;
          selected = null; generation++;
          try { close(next); } finally { notify(); }
          return true;
        },
      });
    },
  });
  return Object.freeze({ runtime, owner });
}
