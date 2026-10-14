import { DEBUG_CAPABILITY_RESOLUTION_SCHEMA, DEBUG_EVIDENCE_SOURCES, targetIdentityKey } from '../../debugger/debugger-contract.js';
import { DEBUG_WIDGET_OPERATIONS } from './manifest.js';
import { createDebugRuntime } from './runtime.js';

const STATES = new Set(['available', 'unsupported', 'not-qualified', 'unavailable']);
const EXECUTION_OPERATIONS = ['execution.pause', 'execution.resume', 'execution.step', 'state.snapshot', 'state.restore'];
const LIVE_COMPLETION_OPERATIONS = new Set(['execution.pause', 'execution.resume', 'execution.step']);

/**
 * Adapt a session-bound capability broker to the optional debug.runtime
 * adapter API. Create a fresh adapter set whenever the active broker changes.
 */
export function createBrokerAdapters(broker) {
  if (!broker || broker.capabilities?.schema !== DEBUG_CAPABILITY_RESOLUTION_SCHEMA
    || typeof broker.execute !== 'function' || typeof broker.state !== 'string'
    || typeof broker.session !== 'string' || !broker.adapter?.id) {
    throw new TypeError('A qualified debugger command broker is required.');
  }
  const reported = broker.capabilities.operations || {};
  const operations = new Set([...Object.values(DEBUG_WIDGET_OPERATIONS).flat(), ...EXECUTION_OPERATIONS, ...Object.keys(reported)]);
  return Object.freeze([...operations].map(operation => {
    const capability = reported[operation] || {
      state:'unavailable', unit:null, layers:{},
      reason:'No owner-authored capability report covers this operation.',
    };
    if (!STATES.has(capability?.state)) throw new TypeError(`Invalid broker capability for ${operation}.`);
    const backendLayer = capability.layers?.backend;
    const backendOwner = backendLayer?.provenance && backendLayer.evidenceId !== 'none' ? backendLayer.owner : null;
    const declaredSource = backendOwner ? backendLayer.provenance.source : null;
    const source = DEBUG_EVIDENCE_SOURCES.includes(declaredSource) ? declaredSource : null;
    const completionMismatch = capability.state === 'available'
      && LIVE_COMPLETION_OPERATIONS.has(operation) && source !== 'live-runtime';
    const state = source === null ? 'unavailable' : completionMismatch ? 'not-qualified' : capability.state;
    const reason = source === null ? 'No backend owner declared a supported evidence source for this operation.'
      : completionMismatch ? `This service requires live-runtime completion for ${operation}; the backend declared ${source}.`
      : capability.reason || 'No operation qualification reason was supplied.';
    const adapter = {
      id: typeof backendOwner === 'string' && backendOwner ? backendOwner : `debug-broker.${broker.adapter.id}`,
      operations: Object.freeze({
        [operation]: Object.freeze({
          source,
          state,
          unit:capability.unit || null,
          scope:Object.hasOwn(capability, 'scope') ? capability.scope : 'process',
          owner:backendOwner,
          reason,
          layers:capability.layers || {},
        }),
      }),
      state() { return broker.state === 'available' ? state : 'unavailable'; },
      async execute(requestedOperation, params, { session, signal, requestId } = {}) {
        if (requestedOperation !== operation) throw new Error('Operation does not match this broker route.');
        if (session !== broker.session) throw new Error('Request belongs to a different broker session.');
        if (broker.state !== 'available' || state !== 'available') {
          throw new Error(reason);
        }
        return broker.execute(operation, params, { signal, requestId });
      },
    };
    return Object.freeze(adapter);
  }));
}

/** Build the request/capability service for exactly one qualified broker session. */
export function createBrokerDebugRuntime(broker, { getStatus = null } = {}) {
  if (getStatus !== null && typeof getStatus !== 'function') {
    throw new TypeError('Debugger host status must be a synchronous owner callback.');
  }
  const adapters = createBrokerAdapters(broker);
  const runtime = createDebugRuntime({ getSession:() => broker.state === 'available' ? broker.session : null, adapters });
  const binding = Object.freeze({ session:broker.session, revision:broker.revision, target:broker.target });
  const targetKey = targetIdentityKey(binding.target);
  const states = new Set(['starting', 'running', 'pausing', 'paused', 'stepping', 'error', 'stopped', 'disconnected']);
  return Object.freeze({
    ...runtime,
    getTarget() { return broker.state === 'available' ? binding.target : null; },
    getStatus() {
      // Status is supplied by the trusted host; never read another guest's
      // page globals or turn a paused label into a capture-readiness grant.
      try {
        if (!getStatus || broker.state !== 'available') return null;
        const value = getStatus(binding);
        if (!value || value.session !== binding.session || value.revision !== binding.revision
          || targetIdentityKey(value.target) !== targetKey || !states.has(value.state)
          || broker.state !== 'available') return null;
        const status = {
          session:binding.session, revision:binding.revision, target:binding.target,
          state:value.state,
          pausedCaptureReady:value.state === 'paused' && value.pausedCaptureReady === true,
        };
        for (const field of ['presents', 'activeThreads', 'parkedThreads']) {
          if (Number.isSafeInteger(value[field]) && value[field] >= 0) status[field] = value[field];
        }
        if (typeof value.error === 'string' && value.error.length <= 1024) status.error = value.error;
        return Object.freeze(status);
      } catch { return null; }
    },
  });
}
