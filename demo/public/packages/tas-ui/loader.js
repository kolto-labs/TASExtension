import { createTasWorkspace } from './workspace.js';

export const TAS_DOCK_IDS = Object.freeze(['score', 'right', 'transport', 'piano-roll', 'score-history', 'sample-state', 'sample-hex', 'sample-scene', 'console']);

// Synchronous extension lifecycle adapter around the optional module download.
// A cancelled/disabled load can finish downloading but cannot mount an editor.
export function createLazyTasRuntime(host) {
  let editor = null, loading = null, wanted = false, generation = 0, ready = false;
  const workspace = createTasWorkspace(TAS_DOCK_IDS);
  const visibility = () => {
    document.body.dataset.tasExtension = wanted && ready ? 'enabled' : 'disabled';
    window.TelosDock?.setAvailable?.(TAS_DOCK_IDS, wanted && ready);
  };
  const announce = (phase, error = '') => dispatchEvent(new CustomEvent('telos:tas-package-state', { detail: { phase, error } }));
  const activate = async () => {
    const ticket = generation;
    announce('loading');
    try {
      loading ||= import('./editor.js').finally(() => { loading = null; });
      const [module] = await Promise.all([loading, workspace.prepare()]);
      if (!wanted || ticket !== generation || !host.isEnabled()) return;
      workspace.mount();
      editor ||= module.createTasEditor(host);
      editor.start();
      editor.refreshKeyboard();
      ready = true; visibility(); announce('ready');
    } catch (error) {
      if (!wanted || ticket !== generation) return;
      ready = false; visibility();
      try { editor?.stop(); } catch (cleanupError) { console.error('TAS load cleanup failed:', cleanupError); }
      announce('error', error?.message || String(error));
      workspace.unmount();
      host.onError(error);
    }
  };
  window.addEventListener('telos:dock-ready', visibility);
  visibility();
  return {
    setEnabled(value) { wanted = value; if (!value) ready = false; visibility(); },
    setPanelsAvailable() { visibility(); },
    start() {
      wanted = true;
      void activate();
    },
    stop() {
      wanted = false; ready = false; generation++;
      try { editor?.stop(); } finally {
        visibility(); announce('disabled'); workspace.unmount();
      }
    },
    invalidatePendingWork() { generation++; editor?.invalidatePendingWork(); },
    refreshKeyboard() { if (ready) editor?.refreshKeyboard(); },
    invalidate() { editor?.invalidate(); },
    flushEdits() { editor?.flushEdits(); },
    pause() { editor?.pause(); },
    stopRecording() { editor?.stopRecording(); },
    clearRecording() { editor?.clearRecording(); },
    releaseInputs(reason) { editor?.releaseInputs(reason); },
    cancelCapture() { editor?.cancelCapture(); },
    resetWorkspace() { editor?.resetWorkspace(); },
  };
}
