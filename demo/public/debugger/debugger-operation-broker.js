import {
  DEBUG_CAPABILITY_LAYERS,
  evaluateAdapterRequirements,
  resolveDebuggerCapabilities,
  targetIdentityKey,
} from './debugger-contract.js';

const MAX_ACTIVE_COMMANDS = 4;
const JSON_LIMITS = Object.freeze({
  request:Object.freeze({ bytes:64 * 1024, nodes:10000, depth:16 }),
  response:Object.freeze({ bytes:1024 * 1024, nodes:150000, depth:24 }),
});
const OPERATION_ID = /^[a-z][a-z0-9]*(?:\.[a-z][a-z0-9-]*){1,7}$/;
// Explicit inspection vocabulary. Unknown/custom operations still require
// live evidence from every owner; a name or uploaded manifest grants no route.
const INSPECTION_OPERATIONS = new Set([
  'processes.list', 'threads.count', 'threads.list', 'modules.list',
  'memory.map', 'memory.mapping', 'memory.read', 'memory.search', 'memory.watch.list',
  'registers.read', 'stack.read', 'code.disassemble', 'code.decompile',
  'code.references', 'code.functions', 'symbols.list', 'symbols.resolve',
  'source.resolve', 'breakpoints.list', 'expressions.evaluate', 'binary.strings',
  'trace.read', 'events.read', 'input.timeline',
  'binary.identity', 'binary.sections', 'binary.dependencies', 'binary.bytes',
]);

function jsonData(value, limits, kind) {
  let nodes = 0;
  let textUnits = 0;
  let estimatedBytes = 0;
  const encoder = new TextEncoder();
  const ancestors = new WeakSet();
  const budget = bytes => {
    estimatedBytes += bytes;
    if (estimatedBytes > limits.bytes) throw new RangeError(`Debugger command ${kind} exceeds ${limits.bytes} bytes.`);
  };
  const textBytes = text => {
    if (text.length + textUnits > limits.bytes) throw new RangeError(`Debugger command ${kind} exceeds ${limits.bytes} bytes.`);
    textUnits += text.length;
    return encoder.encode(text).byteLength;
  };
  const copy = (item, depth) => {
    if (++nodes > limits.nodes || depth > limits.depth) throw new RangeError(`Debugger command ${kind} exceeds structural limits.`);
    if (item === null) { budget(4); return item; }
    if (typeof item === 'string') { budget(textBytes(item) + 2); return item; }
    if (typeof item === 'boolean') { budget(item ? 4 : 5); return item; }
    if (typeof item === 'number' && Number.isFinite(item)) { budget(String(item).length); return item; }
    if (typeof item !== 'object') throw new TypeError(`Debugger command ${kind} must contain JSON values only.`);
    if (ancestors.has(item)) throw new TypeError(`Debugger command ${kind} cannot contain cycles.`);
    const prototype = Object.getPrototypeOf(item);
    if (!Array.isArray(item) && prototype !== Object.prototype && prototype !== null) {
      throw new TypeError(`Debugger command ${kind} must use plain objects and arrays.`);
    }
    ancestors.add(item);
    try {
      budget(2);
      if (Array.isArray(item) && item.length > limits.nodes - nodes) {
        throw new RangeError(`Debugger command ${kind} exceeds structural limits.`);
      }
      const keys = Reflect.ownKeys(item);
      if (keys.length > limits.nodes - nodes + (Array.isArray(item) ? 1 : 0)) {
        throw new RangeError(`Debugger command ${kind} exceeds structural limits.`);
      }
      if (keys.some(key => typeof key !== 'string')) throw new TypeError(`Debugger command ${kind} cannot contain symbol keys.`);
      if (Array.isArray(item)) {
        if (keys.some(key => {
          if (key === 'length') return false;
          if (!/^(0|[1-9]\d*)$/.test(key)) return true;
          const index = Number(key);
          return !Number.isSafeInteger(index) || index >= item.length || String(index) !== key;
        })) {
          throw new TypeError(`Debugger command ${kind} arrays cannot contain custom properties.`);
        }
        const result = [];
        for (let index = 0; index < item.length; index++) {
          const descriptor = Object.getOwnPropertyDescriptor(item, String(index));
          if (!descriptor || !descriptor.enumerable || !Object.hasOwn(descriptor, 'value')) {
            throw new TypeError(`Debugger command ${kind} arrays must contain plain data values without holes.`);
          }
          if (index) budget(1);
          result.push(copy(descriptor.value, depth + 1));
        }
        return result;
      }
      const result = Object.create(null);
      let propertyCount = 0;
      for (const key of keys) {
        const descriptor = Object.getOwnPropertyDescriptor(item, key);
        if (!descriptor?.enumerable || !Object.hasOwn(descriptor, 'value')) {
          throw new TypeError(`Debugger command ${kind} objects must contain enumerable data properties only.`);
        }
        budget(textBytes(key) + 3 + (propertyCount++ ? 1 : 0));
        result[key] = copy(descriptor.value, depth + 1);
      }
      return result;
    } finally {
      ancestors.delete(item);
    }
  };
  const normalized = copy(value, 0);
  const text = JSON.stringify(normalized);
  if (text === undefined || encoder.encode(text).byteLength > limits.bytes) {
    throw new RangeError(`Debugger command ${kind} exceeds ${limits.bytes} bytes.`);
  }
  return deepFreeze(JSON.parse(text));
}

function deepFreeze(value) {
  if (!value || typeof value !== 'object' || Object.isFrozen(value)) return value;
  for (const child of Object.values(value)) deepFreeze(child);
  return Object.freeze(value);
}

function requireId(value, label) {
  if (typeof value !== 'string' || !/^[a-z0-9][a-z0-9.-]{0,127}$/.test(value)) {
    throw new TypeError(`${label} must be a bounded stable identifier.`);
  }
  return value;
}

/**
 * Bind one live target, declarative adapter, backend, and transport for a
 * single session revision. Adapter manifests select interpretation only;
 * trusted host code supplies operation handlers and the transport.
 */
export function createDebuggerOperationBroker({
  session, revision, target, reports, adapterRegistry, adapterId, backendRegistry, backendId, transport, policyOwner, getCurrentBinding,
}) {
  if (!adapterRegistry || typeof adapterRegistry.match !== 'function') throw new TypeError('A validated target-adapter registry is required.');
  const selectedAdapterId = requireId(adapterId, 'Target adapter ID');
  const adapterMatch = adapterRegistry.match(target);
  if (!adapterMatch?.adapter || !['matched-profile', 'matched-exact-build', 'matched-inspection'].includes(adapterMatch.state)) {
    throw new Error('A matched declarative target adapter is required.');
  }
  if (adapterMatch.adapter.id !== selectedAdapterId) throw new Error('The requested target adapter does not match this binary.');
  if (!transport || typeof transport.request !== 'function') throw new TypeError('A trusted debugger transport is required.');
  if (!backendRegistry || typeof backendRegistry.resolve !== 'function'
    || typeof backendRegistry.isActive !== 'function' || typeof backendRegistry.watch !== 'function') {
    throw new TypeError('A revocable trusted host backend registry is required.');
  }
  const backend = backendRegistry.resolve(requireId(backendId, 'Backend ID'), selectedAdapterId);
  if (!backend.operations || typeof backend.operations !== 'object') throw new TypeError('The registered debugger backend has no operation handlers.');
  if (adapterMatch.adapter.requires.some(id => typeof backend.operations[id] !== 'function')) {
    throw new Error('The registered backend does not implement every operation required by this target adapter.');
  }
  if (typeof getCurrentBinding !== 'function') throw new TypeError('A live session-binding provider is required.');
  const owners = {
    transport:requireId(transport.id, 'Transport owner'),
    backend:requireId(backend.id, 'Backend owner'),
    target:requireId(adapterMatch.adapter.id, 'Target adapter owner'),
    policy:requireId(policyOwner, 'Policy owner'),
  };
  const targetKey = targetIdentityKey(target);
  const resolution = resolveDebuggerCapabilities({ session, revision, target, reports });
  const effective = Object.freeze({ ...resolution,
    operations:Object.freeze(Object.fromEntries(Object.entries(resolution.operations).map(([id, operation]) => {
      const requiresLive = !INSPECTION_OPERATIONS.has(id);
      const liveOwners = DEBUG_CAPABILITY_LAYERS.every(layer => operation.layers[layer]?.provenance?.source === 'live-runtime');
      if (adapterMatch.adapter.mode === 'file-inspection' && operation.state === 'available'
        && (!INSPECTION_OPERATIONS.has(id) || operation.scope !== 'target'
          || !DEBUG_CAPABILITY_LAYERS.every(layer => operation.layers[layer]?.provenance?.source === 'static-analysis'))) {
        return [id, Object.freeze({ ...operation, state:'not-qualified',
          reason:'File inspection adapters permit only target-scoped static-analysis reads from every owner.' })];
      }
      return [id, operation.state === 'available' && requiresLive && !liveOwners
        ? Object.freeze({ ...operation, state:'not-qualified',
          reason:'Mutation, runtime control and unclassified operations require live-runtime evidence from every owner.' })
        : operation];
    }))),
  });
  for (const layer of DEBUG_CAPABILITY_LAYERS) {
    const report = reports?.[layer];
    if (!report || resolution.reportErrors[layer]) throw new Error(`${layer} must provide valid owner-authored evidence for commands.`);
    if (report.session !== session || report.revision !== revision || targetIdentityKey(report.target) !== targetKey) {
      throw new Error(`${layer} evidence is not bound to this target revision.`);
    }
  }
  const requirement = evaluateAdapterRequirements(adapterMatch, effective);
  if (requirement.state !== 'requirements-satisfied') throw new Error(requirement.reason);
  for (const [id, operation] of Object.entries(effective.operations)) {
    if (operation.state !== 'available') continue;
    if (typeof backend.operations[id] !== 'function') {
      throw new Error(`Operation ${id} is reported available but the bound backend has no registered handler.`);
    }
    for (const layer of DEBUG_CAPABILITY_LAYERS) {
      if (operation.layers[layer].owner !== owners[layer]) {
        throw new Error(`${layer} operation owner does not match the bound implementation.`);
      }
    }
  }

  let disposed = false;
  const pending = new Set();
  let unwatchBackend = () => {};
  const handlers = Object.create(null);
  for (const [id, handler] of Object.entries(backend.operations)) {
    if (!OPERATION_ID.test(id) || typeof handler !== 'function') throw new TypeError('Backend operations must map stable IDs to functions.');
    handlers[id] = handler;
  }
  const revokeBackend = reason => {
    if (!disposed) {
      disposed = true;
      for (const controller of pending) controller.abort(reason);
      pending.clear();
    }
    unwatchBackend();
  };
  unwatchBackend = backendRegistry.watch(backend.id, revokeBackend);
  const assertBinding = () => {
    if (disposed) {
      throw new Error('Debugger command broker is no longer active.');
    }
    let active, matchesBinding = false;
    try {
      active = backendRegistry.isActive(backend.id, backend);
      if (active) {
        const current = getCurrentBinding();
        matchesBinding = !!current && current.session === session && current.revision === revision
          && targetIdentityKey(current.target) === targetKey;
      }
    } catch {
      revokeBackend(new Error('Debugger session binding could not be verified.'));
      throw new Error('Debugger command refused because its live binding could not be verified.');
    }
    if (!active) {
      revokeBackend(new Error('Debugger backend was uninstalled.'));
      throw new Error('Debugger command broker is no longer active.');
    }
    if (!matchesBinding) {
      revokeBackend(new Error('Debugger session binding changed.'));
      throw new Error('Debugger command refused because the selected session, revision, or target changed.');
    }
  };

  return Object.freeze({
    session,
    revision,
    target:effective.target,
    adapter:adapterMatch.adapter,
    capabilities:effective,
    get state() {
      if (disposed || !backendRegistry.isActive(backend.id, backend)) {
        revokeBackend(new Error('Debugger backend is no longer active.'));
        return 'unavailable';
      }
      try {
        const current = getCurrentBinding();
        if (current?.session === session && current.revision === revision
          && targetIdentityKey(current.target) === targetKey) return 'available';
        revokeBackend(new Error('Debugger session binding changed.'));
        return 'unavailable';
      } catch {
        revokeBackend(new Error('Debugger session binding could not be verified.'));
        return 'unavailable';
      }
    },
    async execute(operation, payload, { signal, requestId = null } = {}) {
      assertBinding();
      if (requestId !== null && (typeof requestId !== 'string' || !/^[a-zA-Z0-9._-]{1,128}$/.test(requestId))) throw new TypeError('Invalid debugger request ID.');
      if (signal?.aborted) throw signal.reason || new DOMException('Aborted', 'AbortError');
      if (typeof operation !== 'string' || !OPERATION_ID.test(operation)) throw new TypeError('Invalid debugger operation ID.');
      const capability = effective.operations[operation];
      if (capability?.state !== 'available') throw new Error(capability?.reason || 'Operation is not qualified for this session.');
      const handler = handlers[operation];
      if (!handler) throw new Error('No bound backend handler implements this operation.');
      if (pending.size >= MAX_ACTIVE_COMMANDS) throw new Error('Debugger command concurrency limit reached.');
      const input = jsonData(payload ?? {}, JSON_LIMITS.request, 'request');
      const controller = new AbortController();
      const abort = () => controller.abort(signal?.reason || new DOMException('Aborted', 'AbortError'));
      signal?.addEventListener('abort', abort, { once:true });
      pending.add(controller);
      if (signal?.aborted) abort();
      const context = Object.freeze({
        session, revision, target:effective.target, adapter:adapterMatch.adapter,
        operation, unit:capability.unit, capability, requestId,
        inspection:INSPECTION_OPERATIONS.has(operation),
        evidenceSource:capability.layers.backend.provenance.source,
      });
      try {
        if (controller.signal.aborted) throw controller.signal.reason || new DOMException('Aborted', 'AbortError');
        assertBinding();
        const result = await handler({ transport, payload:input, context, signal:controller.signal });
        if (controller.signal.aborted) throw controller.signal.reason || new DOMException('Aborted', 'AbortError');
        assertBinding();
        return jsonData(result, JSON_LIMITS.response, 'response');
      } finally {
        pending.delete(controller);
        signal?.removeEventListener('abort', abort);
      }
    },
    close(reason = new DOMException('Debugger session closed.', 'AbortError')) {
      revokeBackend(reason);
    },
  });
}
