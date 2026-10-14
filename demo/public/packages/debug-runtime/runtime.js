import { DEBUG_WIDGET_OPERATIONS } from './manifest.js';
import { DEBUG_OPERATIONS, DEBUG_EVIDENCE_SOURCES } from '../../debugger/debugger-contract.js';
import { guestExecutionCompletion, validateExecutionCompletion } from './execution-completion.js';
import { normalizeMappedFileCapture } from '../../debugger/mapped-file-capture.js';

export const DEBUG_REQUEST_SCHEMA = 'telos.debug-request/v1';
export const DEBUG_REPLY_SCHEMA = 'telos.debug-reply/v1';
const OPERATIONS = new Set([
  ...Object.keys(DEBUG_OPERATIONS),
  ...Object.values(DEBUG_WIDGET_OPERATIONS).flat(),
]);
const OPERATION_ID = /^[a-z][a-z0-9]*(?:\.[a-z][a-z0-9-]*){1,7}$/;
const JSON_LIMITS = Object.freeze({ request:{ bytes:64 * 1024, nodes:10000, depth:16 }, response:{ bytes:1024 * 1024, nodes:150000, depth:24 } });

function boundedJson(value, limits) {
  let nodes = 0;
  let estimatedBytes = 0;
  const encoder = new TextEncoder();
  const budget = bytes => {
    estimatedBytes += bytes;
    if (estimatedBytes > limits.bytes) throw new RangeError('Debugger JSON value exceeds its byte limit.');
  };
  const stringBytes = text => {
    // Bound the string before allocating its UTF-8 representation.
    if (text.length > limits.bytes - estimatedBytes) throw new RangeError('Debugger JSON value exceeds its byte limit.');
    return encoder.encode(text).byteLength;
  };
  const ancestors = new WeakSet();
  const copy = (item, depth) => {
    if (++nodes > limits.nodes || depth > limits.depth) throw new RangeError('Debugger JSON value exceeds structural limits.');
    if (item === null) { budget(4); return item; }
    if (typeof item === 'string') { budget(stringBytes(item) + 2); return item; }
    if (typeof item === 'boolean') { budget(item ? 4 : 5); return item; }
    if (typeof item === 'number' && Number.isFinite(item)) { budget(String(item).length); return item; }
    if (typeof item !== 'object') throw new TypeError('Debugger values must contain JSON data only.');
    if (ancestors.has(item)) throw new TypeError('Debugger values cannot contain cycles.');
    const prototype = Object.getPrototypeOf(item);
    if (!Array.isArray(item) && prototype !== Object.prototype && prototype !== null) throw new TypeError('Debugger objects must be plain JSON objects.');
    ancestors.add(item);
    try {
      budget(2);
      if (Array.isArray(item) && item.length > limits.nodes - nodes) throw new RangeError('Debugger JSON value exceeds structural limits.');
      const keys = Reflect.ownKeys(item);
      if (keys.length > limits.nodes - nodes + (Array.isArray(item) ? 1 : 0)) throw new RangeError('Debugger JSON value exceeds structural limits.');
      if (keys.some(key => typeof key !== 'string')) throw new TypeError('Debugger values cannot contain symbol keys.');
      if (Array.isArray(item)) {
        if (keys.some(key => {
          if (key === 'length') return false;
          if (!/^(0|[1-9]\d*)$/.test(key)) return true;
          const index = Number(key);
          return !Number.isSafeInteger(index) || index >= item.length || String(index) !== key;
        })) throw new TypeError('Debugger arrays cannot contain custom properties.');
        const result = [];
        for (let index = 0; index < item.length; index++) {
          const descriptor = Object.getOwnPropertyDescriptor(item, String(index));
          if (!descriptor?.enumerable || !Object.hasOwn(descriptor, 'value')) throw new TypeError('Debugger arrays cannot contain holes or accessors.');
          if (index) budget(1);
          result.push(copy(descriptor.value, depth + 1));
        }
        return result;
      }
      const result = Object.create(null);
      let propertyCount = 0;
      for (const key of keys) {
        const descriptor = Object.getOwnPropertyDescriptor(item, key);
        if (!descriptor?.enumerable || !Object.hasOwn(descriptor, 'value')) throw new TypeError('Debugger objects cannot contain hidden fields or accessors.');
        budget(stringBytes(key) + 3 + (propertyCount++ ? 1 : 0));
        result[key] = copy(descriptor.value, depth + 1);
      }
      return result;
    } finally { ancestors.delete(item); }
  };
  const normalized = copy(value, 0), json = JSON.stringify(normalized);
  if (json === undefined || encoder.encode(json).byteLength > limits.bytes) throw new RangeError('Debugger JSON value exceeds its byte limit.');
  const frozen = JSON.parse(json);
  const freeze = item => {
    if (!item || typeof item !== 'object' || Object.isFrozen(item)) return item;
    for (const child of Object.values(item)) freeze(child);
    return Object.freeze(item);
  };
  return freeze(frozen);
}

export class DebugOperationError extends Error {
  constructor(code, message, details = {}) {
    super(message); this.name = 'DebugOperationError'; this.code = code; this.details = details;
  }
}

/** A session owner supplies adapters; no module discovery or arbitrary plugin execution. */
export function createDebugRuntime({ getSession, adapters = [] }) {
  if (typeof getSession !== 'function') throw new TypeError('getSession is required.');
  const routes = new Map();
  for (const adapter of adapters) {
    if (!adapter || typeof adapter.id !== 'string' || !adapter.id || adapter.id.length > 256
      || adapter.id.trim() !== adapter.id || !adapter.operations || typeof adapter.execute !== 'function') {
      throw new TypeError('A debugger adapter requires id, operation descriptors, and execute.');
    }
    // Bind provenance and declarations to this registration. Readiness remains
    // live, but mutating a host adapter cannot relabel an in-flight command.
    const adapterId = adapter.id;
    const descriptors = boundedJson(adapter.operations, JSON_LIMITS.request);
    if (!descriptors || typeof descriptors !== 'object' || Array.isArray(descriptors)) {
      throw new TypeError('Debugger operation descriptors must be a JSON object.');
    }
    for (const [operation, capability] of Object.entries(descriptors)) {
      if (!OPERATION_ID.test(operation) || routes.has(operation)) throw new TypeError(`Invalid or duplicate debugger route: ${operation}`);
      if (!capability || typeof capability !== 'object' || Array.isArray(capability)
        || (Object.hasOwn(capability, 'state') && !['available', 'unsupported', 'not-qualified', 'unavailable'].includes(capability.state))
        || (Object.hasOwn(capability, 'scope') && !['process', 'target'].includes(capability.scope)
          && !(capability.scope === null && ['unsupported', 'not-qualified', 'unavailable'].includes(capability.state)))
        || !(DEBUG_EVIDENCE_SOURCES.includes(capability.source)
          || (capability.source === null && ['unsupported', 'not-qualified', 'unavailable'].includes(capability.state)))) {
        throw new TypeError('Adapter must declare its evidence source or an unavailable route with no source.');
      }
      routes.set(operation, { adapter, adapterId, capability });
    }
  }
  const sessionKey = value => typeof value === 'string' && value.length > 0 && value.length <= 128;
  const routeAvailability = (route, operation) => {
    if (route.capability.source === null) return { state:'unavailable', reason:'No owner-authored evidence source covers this route.' };
    // Registered qualification is a ceiling. Live readiness cannot promote a
    // declaration that requires replacement/requalification of this service.
    if (route.capability.state && route.capability.state !== 'available') {
      return { state:route.capability.state, reason:null };
    }
    try {
      const state = typeof route.adapter.state === 'function' ? route.adapter.state(operation) : 'available';
      return ['available', 'unsupported', 'not-qualified', 'unavailable'].includes(state)
        ? { state, reason:null } : { state:'unavailable', reason:'Adapter reported an invalid readiness state.' };
    } catch {
      return { state:'unavailable', reason:'Adapter readiness check failed.' };
    }
  };
  return Object.freeze({
    getSession() {
      const session = getSession();
      return sessionKey(session) ? session : null;
    },
    capabilities() {
      return Object.freeze({ schema: 'telos.debug-adapter-status/v1', session: getSession(),
        operations: Object.fromEntries([...new Set([...OPERATIONS, ...routes.keys()])].map(operation => {
          const route = routes.get(operation);
          if (!route) return [operation, { state:'unsupported', owner:null, source:null, reason:'No adapter provides this operation.' }];
          const availability = routeAvailability(route, operation);
          return [operation, { ...route.capability, state:availability.state,
            ...(availability.reason ? { reason:availability.reason } : {}),
            owner: Object.hasOwn(route.capability, 'owner') ? route.capability.owner : route.adapterId }
          ];
        })),
      });
    },
    async request(request, { signal } = {}) {
      if (!request || request.schema !== DEBUG_REQUEST_SCHEMA || !sessionKey(request.session)
        || typeof request.id !== 'string' || !/^[a-zA-Z0-9._-]{1,128}$/.test(request.id)
        || typeof request.operation !== 'string' || !OPERATION_ID.test(request.operation)
        || !request.params || typeof request.params !== 'object'
        || Array.isArray(request.params)) throw new DebugOperationError('DEBUG_REQUEST_INVALID', 'Invalid debugger request envelope.');
      const session = getSession();
      if (session !== request.session) throw new DebugOperationError('DEBUG_SESSION_CHANGED', 'Debugger request belongs to a different session.');
      if (signal?.aborted) throw new DebugOperationError('DEBUG_CANCELLED', 'Debugger request was cancelled.');
      const route = routes.get(request.operation);
      if (!route) throw new DebugOperationError('DEBUG_UNSUPPORTED', 'The selected adapter does not provide this operation.', { operation: request.operation });
      const state = routeAvailability(route, request.operation).state;
      if (state === 'unsupported') throw new DebugOperationError('DEBUG_UNSUPPORTED', 'The selected adapter does not support this operation.', { operation: request.operation });
      if (state !== 'available') throw new DebugOperationError('DEBUG_NOT_READY', 'The selected debugger operation is not ready or qualified.', { operation: request.operation, state });
      let params;
      try { params = boundedJson(request.params, JSON_LIMITS.request); }
      catch (error) { throw new DebugOperationError('DEBUG_REQUEST_INVALID', error.message || 'Invalid debugger parameters.'); }
      const execution = ['execution.pause', 'execution.resume', 'execution.step'].includes(request.operation);
      if (execution) {
        if (route.capability.source !== 'live-runtime') throw new DebugOperationError('DEBUG_NOT_READY', 'Execution requires a live runtime adapter.');
        if (request.operation === 'execution.step') {
          if (Object.keys(params).length !== 1 || typeof params.unit !== 'string' || !params.unit || params.unit.length > 256 || params.unit.trim() !== params.unit
            || params.unit !== route.capability.unit) throw new DebugOperationError('DEBUG_REQUEST_INVALID', 'Stepping requires the adapter-qualified unit.');
        } else if (Object.keys(params).length) throw new DebugOperationError('DEBUG_REQUEST_INVALID', 'Pause and resume take no parameters.');
      }
      const data = await route.adapter.execute(request.operation, params, { session, signal, requestId:request.id });
      if (signal?.aborted) throw new DebugOperationError('DEBUG_CANCELLED', 'Debugger request was cancelled.');
      if (getSession() !== session) throw new DebugOperationError('DEBUG_SESSION_CHANGED', 'The game changed while debugger data was being captured.');
      let safeData;
      try { safeData = boundedJson(data, JSON_LIMITS.response); }
      catch (error) { throw new DebugOperationError('DEBUG_RESPONSE_INVALID', error.message || 'The debugger returned an invalid response.'); }
      if (execution) {
        try {
          validateExecutionCompletion(safeData, { operation:request.operation, session,
            owner:route.adapterId, unit:params.unit, requestId:request.id });
        } catch (error) { throw new DebugOperationError('DEBUG_RESPONSE_INVALID', error.message); }
      }
      return { schema: DEBUG_REPLY_SCHEMA, id: request.id, session, operation: request.operation,
        source: route.capability.source, adapter: route.adapterId, capturedAt: new Date().toISOString(), data:safeData };
    },
  });
}

/** Supported execution stays on the existing session owner's VM bridge. */
export function createGuestExecutionAdapter(game) {
  if (typeof game?.requestVmControl !== 'function') throw new TypeError('An execution session owner is required.');
  const actions = Object.freeze({ 'execution.pause':'pause', 'execution.resume':'resume', 'execution.step':'step' });
  return Object.freeze({
    id:'telos.guest-execution',
    operations:Object.freeze(Object.fromEntries(Object.keys(actions).map(operation => [operation,
      Object.freeze({ source:'live-runtime', unit:'render-present', completion:'runtime-command-provenance' })]))),
    state(operation) {
      const action = actions[operation], caps = game.runtimeVmCapabilities;
      if (!action) return 'unsupported';
      if (!caps) return 'unavailable';
      if (caps[action] !== true || (action === 'step' && caps.frameStep !== true)) return 'unsupported';
      return game.tasVmReady && game.session ? 'available' : 'unavailable';
    },
    async execute(operation, params, { session, signal, requestId } = {}) {
      const action = actions[operation];
      if (!action) throw new DebugOperationError('DEBUG_UNSUPPORTED', 'Unknown game execution operation.');
      if (session !== game.session) throw new DebugOperationError('DEBUG_SESSION_CHANGED', 'Execution request belongs to a different game session.');
      if (action === 'step') {
        if (Object.keys(params).length !== 1 || params.unit !== 'render-present') throw new DebugOperationError('DEBUG_REQUEST_INVALID', 'Game stepping requires unit render-present.');
      } else if (Object.keys(params).length) throw new DebugOperationError('DEBUG_REQUEST_INVALID', 'Pause and resume take no parameters.');
      const receipt = await game.requestVmControl(action, { signal });
      if (session !== game.session) throw new DebugOperationError('DEBUG_SESSION_CHANGED', 'The game changed before execution completion.');
      return guestExecutionCompletion(receipt, { operation, session, owner:'telos.guest-execution', requestId });
    },
  });
}

/** Uses the existing origin/session-checked guest bridge, without another controller. */
export function createGuestRegisterAdapter(game) {
  if (typeof game?.captureGuestRegisters !== 'function') throw new TypeError('A register-capture session owner is required.');
  return Object.freeze({
    id:'telos.guest-registers',
    operations:Object.freeze({ 'registers.read':Object.freeze({ source:'live-runtime', completion:'paused-capture', semantics:'saved-guest-registers' }) }),
    state:() => game.runtimeGuestMemoryAvailable && game.runtimeVmCapabilities?.registers === true ? 'available' : 'unavailable',
    async execute(operation, params, { session } = {}) {
      if (operation !== 'registers.read') throw new DebugOperationError('DEBUG_UNSUPPORTED', 'Unknown register operation.');
      if (session !== game.session) throw new DebugOperationError('DEBUG_SESSION_CHANGED', 'Register request belongs to a different game session.');
      if (Object.keys(params).length !== 1 || !Number.isSafeInteger(params.pid) || params.pid < 1 || params.pid > 0xffffffff) throw new DebugOperationError('DEBUG_REQUEST_INVALID', 'Register capture requires a valid 32-bit process ID.');
      return game.captureGuestRegisters(params.pid);
    },
  });
}

/** Uses the existing origin/session-checked guest bridge, without another controller. */
export function createGuestMappingAdapter(game) {
  if (typeof game?.captureGuestMapping !== 'function') throw new TypeError('A paused mapping session owner is required.');
  return Object.freeze({ id:'telos.guest-mapping',
    operations:Object.freeze({ 'memory.mapping':Object.freeze({ source:'live-runtime', scope:'process', completion:'paused-capture', semantics:'mapped-file-address' }) }),
    state:() => game.runtimeGuestMemoryAvailable && game.runtimeVmCapabilities?.addressMapping === true ? 'available' : 'unavailable',
    async execute(operation, params, { session } = {}) {
      if (operation !== 'memory.mapping') throw new DebugOperationError('DEBUG_UNSUPPORTED', 'Unknown mapping operation.');
      if (session !== game.session) throw new DebugOperationError('DEBUG_SESSION_CHANGED', 'Mapping request belongs to a different game session.');
      if (Object.keys(params).length !== 2 || !Number.isSafeInteger(params.pid) || params.pid < 1 || params.pid > 0xffffffff
        || !Number.isSafeInteger(params.address) || params.address < 0 || params.address > 0xffffffff) throw new DebugOperationError('DEBUG_REQUEST_INVALID', 'Mapping capture requires only a valid PID and 32-bit address.');
      const value = await game.captureGuestMapping(params.pid, params.address);
      if (session !== game.session) throw new DebugOperationError('DEBUG_SESSION_CHANGED', 'The game changed before mapping capture completed.');
      return normalizeMappedFileCapture(value, params);
    },
  });
}

export function createGuestThreadAdapter(game) {
  if (typeof game?.listGuestThreads !== 'function') throw new TypeError('A thread-list session owner is required.');
  return Object.freeze({
    id:'telos.guest-threads',
    operations:Object.freeze({ 'threads.list':Object.freeze({ source:'live-runtime', completion:'paused-capture' }) }),
    state:() => game.runtimeGuestMemoryAvailable && game.runtimeVmCapabilities?.threadList === true ? 'available' : 'unavailable',
    async execute(operation, params, { session } = {}) {
      if (operation !== 'threads.list') throw new DebugOperationError('DEBUG_UNSUPPORTED', 'Unknown thread operation.');
      if (session !== game.session) throw new DebugOperationError('DEBUG_SESSION_CHANGED', 'Thread request belongs to a different game session.');
      if (Object.keys(params).length !== 1 || !Number.isSafeInteger(params.pid) || params.pid < 1 || params.pid > 0xffffffff) throw new DebugOperationError('DEBUG_REQUEST_INVALID', 'Thread listing requires a valid 32-bit process ID.');
      return game.listGuestThreads(params.pid);
    },
  });
}

/** Uses the existing origin/session-checked guest bridge, without another controller. */
export function createGuestMemoryAdapter(game) {
  if (typeof game?.readGuestMemory !== 'function' || typeof game?.getGuestProcesses !== 'function') throw new TypeError('A guest-memory session owner is required.');
  return Object.freeze({
    id: 'telos.guest-memory', state: () => game.runtimeGuestMemoryAvailable ? 'available' : 'unavailable', operations: {
      'memory.read': { source: 'live-runtime', unit: 'guest-byte' },
      'processes.list': { source: 'live-runtime' },
    },
    async execute(operation, params) {
      if (!game.runtimeGuestMemoryAvailable) throw new DebugOperationError('DEBUG_NOT_READY', 'Guest memory is not ready in this session.');
      if (operation === 'processes.list') {
        if (Object.keys(params).length) throw new DebugOperationError('DEBUG_REQUEST_INVALID', 'Process listing takes no parameters.');
        return { processes: await game.getGuestProcesses() };
      }
      if (operation !== 'memory.read') throw new DebugOperationError('DEBUG_UNSUPPORTED', 'Unknown guest-memory operation.');
      if (Object.keys(params).some(key => !['pid', 'address', 'length'].includes(key))) {
        throw new DebugOperationError('DEBUG_REQUEST_INVALID', 'Memory reads accept only PID, address, and length.');
      }
      const { pid, address, length } = params;
      if (!Number.isSafeInteger(pid) || pid < 1 || pid > 0xffffffff
        || !Number.isSafeInteger(address) || address < 0 || address > 0xffffffff
        || !Number.isSafeInteger(length) || length < 1 || length > 65536
        || address + length > 0x100000000) {
        throw new DebugOperationError('DEBUG_REQUEST_INVALID', 'Memory reads require a valid 32-bit PID/address and 1–65,536 bytes within the address space.');
      }
      const bytes = await game.readGuestMemory(pid, address, length);
      if (!(bytes instanceof Uint8Array) || bytes.length !== length) throw new DebugOperationError('DEBUG_INCOMPLETE_READ', 'Guest returned an incomplete memory range.');
      return { pid, address, length, bytes: Array.from(bytes), byteOrder: 'little' };
    },
  });
}
