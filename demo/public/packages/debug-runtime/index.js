import { DEBUG_MANIFEST } from './manifest.js';
export { DEBUG_MANIFEST, DEBUG_WIDGET_OPERATIONS } from './manifest.js';
export { createDebugRuntime, createGuestMemoryAdapter, DebugOperationError } from './runtime.js';
export { createCapturedToolsAdapter, searchCapturedBytes, evaluateIntegerExpression } from './captured-tools.js';

/** Registers an optional package. Widget mount/unmount belong to its UI provider. */
export function registerDebugPackage(host, { mount = () => () => {} } = {}) {
  return host.register(DEBUG_MANIFEST, {
    activate(context) {
      const runtime = context.service('debug.runtime');
      if (typeof runtime?.request !== 'function' || typeof runtime?.capabilities !== 'function') throw new TypeError('Debug runtime service is invalid.');
      return mount({ runtime, storage: context.storage });
    },
    deactivate() {},
  });
}
