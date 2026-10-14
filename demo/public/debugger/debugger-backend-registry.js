const BACKEND_SCHEMA = 'telos.debug-backend/v1';
const BACKEND_API = 'telos.debug-backend';
const ID = /^[a-z0-9][a-z0-9.-]{0,127}$/;
const SEMVER = /^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$/;
const OPERATION_ID = /^[a-z][a-z0-9]*(?:\.[a-z][a-z0-9-]*){1,7}$/;
const MAX_BACKENDS = 128;
const MAX_OPERATIONS = 256;
const MAX_ADAPTERS = 128;

function normalizeManifest(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)
    || value.schema !== BACKEND_SCHEMA || typeof value.id !== 'string' || !ID.test(value.id)
    || typeof value.version !== 'string' || !SEMVER.test(value.version)
    || !value.api || typeof value.api !== 'object' || Array.isArray(value.api)
    || value.api.id !== BACKEND_API || value.api.major !== 1
    || !Number.isSafeInteger(value.api.minor) || value.api.minor < 0
    || !Array.isArray(value.adapters) || !value.adapters.length || value.adapters.length > MAX_ADAPTERS
    || !Array.isArray(value.operations) || !value.operations.length || value.operations.length > MAX_OPERATIONS) {
    throw new TypeError('Invalid debugger backend manifest.');
  }
  const adapters = [...new Set(value.adapters.map(id => {
    if (typeof id !== 'string' || !ID.test(id)) throw new TypeError('Backend adapter IDs must be stable identifiers.');
    return id;
  }))];
  const operations = [...new Set(value.operations.map(id => {
    if (typeof id !== 'string' || !OPERATION_ID.test(id)) throw new TypeError('Backend operation IDs must be namespaced identifiers.');
    return id;
  }))];
  if (adapters.length !== value.adapters.length || operations.length !== value.operations.length) {
    throw new TypeError('Backend manifest entries must not contain duplicates.');
  }
  return Object.freeze({
    schema:BACKEND_SCHEMA,
    id:value.id,
    version:value.version,
    api:Object.freeze({ id:BACKEND_API, major:1, minor:value.api.minor }),
    adapters:Object.freeze(adapters),
    operations:Object.freeze(operations),
  });
}

/**
 * Host-owned catalog of installed backend code. This registry deliberately
 * has no file or URL loader: registration receives code from the trusted host.
 */
export class DebuggerBackendRegistry {
  #entries = new Map();
  #listeners = new Map();

  register(manifest, handlers) {
    const normalized = normalizeManifest(manifest);
    if (this.#entries.has(normalized.id)) throw new Error(`Debugger backend is already registered: ${normalized.id}`);
    if (this.#entries.size >= MAX_BACKENDS) throw new RangeError('Debugger backend registry capacity reached.');
    if (!handlers || typeof handlers !== 'object' || Array.isArray(handlers)) throw new TypeError('Backend handlers must be a host-owned operation map.');
    const supplied = Reflect.ownKeys(handlers);
    if (supplied.some(id => typeof id !== 'string') || supplied.length !== normalized.operations.length
      || supplied.some(id => !normalized.operations.includes(id))) {
      throw new TypeError('Backend handler IDs must exactly match its manifest operations.');
    }
    const operations = Object.create(null);
    for (const id of normalized.operations) {
      const descriptor = Object.getOwnPropertyDescriptor(handlers, id);
      if (!descriptor?.enumerable || !Object.hasOwn(descriptor, 'value') || typeof descriptor.value !== 'function') {
        throw new TypeError(`Backend operation ${id} must be a plain host function.`);
      }
      operations[id] = descriptor.value;
    }
    const entry = Object.freeze({ id:normalized.id, manifest:normalized, operations:Object.freeze(operations) });
    this.#entries.set(normalized.id, entry);
    return normalized;
  }

  resolve(id, adapterId) {
    const entry = this.#entries.get(id);
    if (!entry) throw new Error(`Debugger backend is not installed: ${id}`);
    if (!entry.manifest.adapters.includes(adapterId)) {
      throw new Error(`Debugger backend ${id} is not registered for target adapter ${adapterId}.`);
    }
    return entry;
  }

  isActive(id, entry) { return this.#entries.get(id) === entry; }

  watch(id, listener) {
    if (!this.#entries.has(id)) throw new Error(`Debugger backend is not installed: ${id}`);
    if (typeof listener !== 'function') throw new TypeError('Backend revocation listener must be a function.');
    let listeners = this.#listeners.get(id);
    if (!listeners) this.#listeners.set(id, listeners = new Set());
    listeners.add(listener);
    return () => {
      listeners.delete(listener);
      if (!listeners.size && this.#listeners.get(id) === listeners) this.#listeners.delete(id);
    };
  }

  unregister(id, reason = new Error('Debugger backend was uninstalled.')) {
    if (!this.#entries.has(id)) return false;
    this.#entries.delete(id);
    const listeners = this.#listeners.get(id);
    this.#listeners.delete(id);
    for (const listener of listeners || []) {
      try { listener(reason); } catch { /* Revocation must reach every bound broker. */ }
    }
    return true;
  }

  list() { return Object.freeze([...this.#entries.values()].map(entry => entry.manifest)); }
}
