import { TAS_MANIFEST } from './manifest.js';

const REQUIRED_RUNTIME = [
  'setEnabled', 'setPanelsAvailable', 'refreshKeyboard', 'invalidate', 'start', 'stop',
  'invalidatePendingWork', 'flushEdits', 'pause', 'stopRecording', 'clearRecording',
  'releaseInputs', 'cancelCapture', 'resetWorkspace',
];

function runtimeFrom(context) {
  const runtime = context.service('tas.runtime');
  if (!runtime || REQUIRED_RUNTIME.some((name) => typeof runtime[name] !== 'function')) {
    const error = new Error('TAS Core runtime service does not implement the declared lifecycle contract');
    error.code = 'TAS_RUNTIME_CONTRACT_INVALID';
    throw error;
  }
  return runtime;
}

// This is the installable package entry point. It owns TAS lifecycle policy;
// the Core host supplies a narrow adapter for the current in-page runtime.
export function createTasLifecycle() {
  return {
    activate(context) {
      const runtime = runtimeFrom(context);
      runtime.setEnabled(true);
      runtime.setPanelsAvailable(true);
      runtime.refreshKeyboard();
      runtime.invalidate();
      runtime.start();
    },
    deactivate(context) {
      const runtime = runtimeFrom(context);
      const errors = [];
      const cleanup = (name, ...args) => {
        try { runtime[name](...args); } catch (error) { errors.push(error); }
      };
      // Attempt every release and UI cleanup even if one subsystem is already
      // unhealthy; the Core host will keep the package disabled and report all
      // failures rather than abandoning later cleanup steps.
      cleanup('invalidatePendingWork');
      cleanup('flushEdits');
      cleanup('pause');
      cleanup('stopRecording');
      cleanup('clearRecording');
      cleanup('releaseInputs', 'tas-extension-disabled');
      cleanup('cancelCapture');
      cleanup('resetWorkspace');
      cleanup('setEnabled', false);
      cleanup('setPanelsAvailable', false);
      cleanup('stop');
      if (errors.length) {
        const error = new AggregateError(errors, 'TAS shutdown completed with cleanup failures');
        error.code = 'TAS_CLEANUP_FAILED';
        throw error;
      }
    },
  };
}

export { TAS_MANIFEST };
