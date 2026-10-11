import { createTasLifecycle } from '../tas/index.js';
import { createLazyTasRuntime } from './loader.js';

// This entry point is imported only after an explicit package enable.
export function createTasUi(host) {
  const runtime = createLazyTasRuntime(host);
  const lifecycle = createTasLifecycle();
  const context = { service(name) {
    if (name !== 'tas.runtime') throw new Error(`Unknown TAS service: ${name}`);
    return runtime;
  } };
  return {
    activate() { lifecycle.activate(context); },
    deactivate() { lifecycle.deactivate(context); },
  };
}
