import { createStaticBinaryBackend } from './static-binary-backend.js';
import { DebugTargetAdapterRegistry, DEBUG_CAPABILITY_REPORT_SCHEMA, targetIdentityKey } from './debugger-contract.js';
import { DebuggerBackendRegistry } from './debugger-backend-registry.js';
import { createDebuggerOperationBroker } from './debugger-operation-broker.js';

/** Core-owned local file selection; never controls the game lifecycle. */
export function createStaticFileInspectionOwner(owner, runtime) {
  if (typeof owner?.attachBroker !== 'function' || typeof owner.getSelectionRevision !== 'function') {
    throw new TypeError('A revision-aware trusted debugger selector is required.');
  }
  if (typeof runtime?.getSession !== 'function' || typeof runtime.getTarget !== 'function') {
    throw new TypeError('The selected debugger binding must be observable.');
  }
  const selectionKey = () => {
    const target = runtime.getTarget();
    return JSON.stringify([owner.getSelectionRevision(), runtime.getSession(), target ? targetIdentityKey(target) : null]);
  };
  let current = null, pending = null, revision = 0;
  const listeners = new Set();
  const notify = () => {
    for (const listener of [...listeners]) {
      try { listener(); } catch (error) { console.error('File inspection listener failed:', error); }
    }
  };
  const cleanup = slot => {
    if (!slot) return;
    slot.active = false;
    for (const dispose of [() => slot.lease?.detach(), () => slot.broker?.close(),
      () => slot.reader.close(), () => slot.registry.unregister(slot.reader.manifest.id)]) {
      try { dispose(); } catch (error) { console.error('File inspection disposal failed:', error); }
    }
  };
  return Object.freeze({
    getSelection() {
      return current?.broker.state === 'available'
        ? Object.freeze({ session:current.binding.session, target:current.binding.target, source:'static-analysis' }) : null;
    },
    subscribe(listener) {
      if (typeof listener !== 'function') throw new TypeError('A file inspection listener is required.');
      listeners.add(listener); return () => listeners.delete(listener);
    },
    cancelPending() { revision++; pending?.abort(); pending = null; },
    async open(file) {
      const ticket = ++revision, controller = new AbortController();
      const selectionRevision = owner.getSelectionRevision();
      pending?.abort(); pending = controller;
      let reader = null, slot = null;
      try {
        const selectedBinding = selectionKey();
        const session = `static-file-${crypto.randomUUID()}`;
        reader = await createStaticBinaryBackend(file, { session, revision:ticket, signal:controller.signal });
        if (controller.signal.aborted || ticket !== revision) throw new DOMException('File selection changed.', 'AbortError');
        if (owner.getSelectionRevision() !== selectionRevision || selectionKey() !== selectedBinding) throw new DOMException('The debugger selection changed while the file was being read.', 'AbortError');
        if (!reader.adapterManifest) throw new Error('The file format is unrecognized. The current debugger selection is retained.');
        const adapters = new DebugTargetAdapterRegistry(), registry = new DebuggerBackendRegistry();
        adapters.register(reader.adapterManifest); registry.register(reader.manifest, reader.handlers);
        const binding = Object.freeze({ session, revision:ticket, target:reader.target });
        slot = { reader, registry, binding, active:true, broker:null, lease:null };
        const transport = Object.freeze({ id:'telos.static-file-transport',
          request() { throw new Error('Static file inspection performs no remote or game transport requests.'); } });
        const policyOwner = 'telos.static-file-policy';
        const owners = { transport:transport.id, backend:reader.manifest.id,
          target:reader.adapterManifest.id, policy:policyOwner };
        // Each layer reports its actual local responsibility. Backend readiness
        // comes from captured table completeness, not a platform/run assertion.
        const reports = Object.fromEntries(Object.entries(owners).map(([layer, name]) => [layer, {
          schema:DEBUG_CAPABILITY_REPORT_SCHEMA, layer, ...binding, source:'static-analysis',
          boundary:{ kind:'file-metadata', unit:'file' },
          operations:Object.fromEntries(Object.entries(reader.declarations).map(([operation, declaration]) => [operation, {
            state:layer === 'backend' ? declaration.state : 'available', owner:name,
            scope:'target', unit:null, evidenceId:`${session}:${layer}:${operation}`,
            reason:layer === 'backend' ? declaration.reason : layer === 'target'
              ? 'Recognized file format matches the inspection adapter; unknown identity fields stay unknown and execution remains unqualified.'
              : layer === 'policy' ? 'Core permits local file metadata and bounded read-only file byte capture.'
                : 'Trusted in-process dispatch is bound to this immutable file inspection session.',
          }])),
        }]));
        slot.broker = createDebuggerOperationBroker({ ...binding, reports,
          adapterRegistry:adapters, adapterId:reader.adapterManifest.id,
          backendRegistry:registry, backendId:reader.manifest.id, transport, policyOwner,
          getCurrentBinding:() => slot.active ? binding : null });
        if (controller.signal.aborted || ticket !== revision) throw new DOMException('File selection changed.', 'AbortError');
        if (owner.getSelectionRevision() !== selectionRevision || selectionKey() !== selectedBinding) throw new DOMException('The debugger selection changed before file attachment.', 'AbortError');
        slot.lease = owner.attachBroker(slot.broker);
        if (owner.getSelectionRevision() !== selectionRevision + 1 || slot.broker.state !== 'available') {
          throw new DOMException('Another owner replaced the file inspection during attachment.', 'AbortError');
        }
        const previous = current; current = slot;
        cleanup(previous); notify();
        return Object.freeze({ session, target:reader.target, source:'static-analysis' });
      } catch (error) {
        if (slot && slot !== current) cleanup(slot);
        else if (!slot) reader?.close();
        throw error;
      } finally { if (pending === controller) pending = null; }
    },
    close() {
      revision++; pending?.abort(); pending = null;
      const previous = current; current = null;
      cleanup(previous); notify();
    },
  });
}
