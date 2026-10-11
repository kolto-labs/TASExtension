import { DEBUG_WIDGET_OPERATIONS } from './manifest.js';

export const DEBUG_REQUEST_SCHEMA = 'telos.debug-request/v1';
export const DEBUG_REPLY_SCHEMA = 'telos.debug-reply/v1';
const OPERATIONS = new Set(Object.values(DEBUG_WIDGET_OPERATIONS).flat().concat([
  'execution.pause', 'execution.resume', 'execution.step', 'state.snapshot', 'state.restore',
]));

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
    if (!adapter || typeof adapter.id !== 'string' || !adapter.operations || typeof adapter.execute !== 'function') {
      throw new TypeError('A debugger adapter requires id, operation descriptors, and execute.');
    }
    for (const [operation, capability] of Object.entries(adapter.operations)) {
      if (!OPERATIONS.has(operation) || routes.has(operation)) throw new TypeError(`Invalid or duplicate debugger route: ${operation}`);
      if (!['live-runtime', 'static-analysis', 'captured-memory'].includes(capability.source)) throw new TypeError('Adapter must declare its evidence source.');
      routes.set(operation, { adapter, capability });
    }
  }
  const sessionKey = value => typeof value === 'string' && value.length > 0 && value.length <= 128;
  const routeState = route => typeof route.adapter.state === 'function' ? route.adapter.state() : 'available';
  return Object.freeze({
    capabilities() {
      return Object.freeze({ schema: 'telos.debug-adapter-status/v1', session: getSession(),
        operations: Object.fromEntries([...OPERATIONS].map(operation => {
          const route = routes.get(operation);
          return [operation, route ? { ...route.capability, state: routeState(route), owner: route.adapter.id }
            : { state: 'unsupported', owner: null, source: null, reason: 'No adapter provides this operation.' }];
        })),
      });
    },
    async request(request, { signal } = {}) {
      if (!request || request.schema !== DEBUG_REQUEST_SCHEMA || !sessionKey(request.session)
        || typeof request.id !== 'string' || !/^[a-zA-Z0-9._-]{1,128}$/.test(request.id)
        || !OPERATIONS.has(request.operation) || !request.params || typeof request.params !== 'object'
        || Array.isArray(request.params)) throw new DebugOperationError('DEBUG_REQUEST_INVALID', 'Invalid debugger request envelope.');
      const session = getSession();
      if (session !== request.session) throw new DebugOperationError('DEBUG_SESSION_CHANGED', 'Debugger request belongs to a different session.');
      if (signal?.aborted) throw new DebugOperationError('DEBUG_CANCELLED', 'Debugger request was cancelled.');
      const route = routes.get(request.operation);
      if (!route) throw new DebugOperationError('DEBUG_UNSUPPORTED', 'The selected adapter does not provide this operation.', { operation: request.operation });
      if (routeState(route) !== 'available') throw new DebugOperationError('DEBUG_NOT_READY', 'The selected debugger adapter is not ready.', { operation: request.operation });
      const data = await route.adapter.execute(request.operation, request.params, { session, signal });
      if (signal?.aborted) throw new DebugOperationError('DEBUG_CANCELLED', 'Debugger request was cancelled.');
      if (getSession() !== session) throw new DebugOperationError('DEBUG_SESSION_CHANGED', 'The game changed while debugger data was being captured.');
      return { schema: DEBUG_REPLY_SCHEMA, id: request.id, session, operation: request.operation,
        source: route.capability.source, adapter: route.adapter.id, capturedAt: new Date().toISOString(), data };
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
      if (operation === 'processes.list') return { processes: await game.getGuestProcesses() };
      if (operation !== 'memory.read') throw new DebugOperationError('DEBUG_UNSUPPORTED', 'Unknown guest-memory operation.');
      const { pid, address, length } = params;
      const bytes = await game.readGuestMemory(pid, address, length);
      if (!(bytes instanceof Uint8Array) || bytes.length !== length) throw new DebugOperationError('DEBUG_INCOMPLETE_READ', 'Guest returned an incomplete memory range.');
      return { pid, address, length, bytes: Array.from(bytes), byteOrder: 'little' };
    },
  });
}
