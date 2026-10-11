import { mountDockWorkspace } from './dock-workspace.js';

mountDockWorkspace();
document.body.classList.add('wide');
document.body.classList.remove('compact');
let presentationModule = null, presentationHandle = null, presentationLoad = null;
let enabled = false, generation = 0, dev = null, registered = false;

// This small handle stays in the shell so development hot replacement can
// preserve the same owner, even while the optional TAS package is suspended.
function createPresentationHandle(module, saved = null) {
  let state = saved, mounted = enabled ? module.mountTasPresentation(saved) : null;
  return {
    snapshot() { return mounted?.snapshot() || state; },
    restore(value) { state = value; if (mounted && value) mounted.restore(value); },
    destroy() { state = mounted?.snapshot() || state; mounted?.destroy(); mounted = null; },
    suspend() { this.destroy(); },
    resume() { if (!mounted && enabled) mounted = module.mountTasPresentation(state); },
  };
}
function registerPresentationReload() {
  if (!dev || !presentationModule || !presentationHandle || registered) return;
  dev.register('presentation', {
    url: new URL('./tas-presentation.js', import.meta.url).href,
    module: presentationModule,
    create(module, state) {
      presentationModule = module;
      presentationHandle = createPresentationHandle(module, state);
      return presentationHandle;
    },
    handle: presentationHandle,
  });
  registered = true;
}
async function setTasPresentationActive(value) {
  enabled = value;
  const ticket = ++generation;
  if (!enabled) {
    presentationHandle?.suspend();
    document.body.dataset.tasPresentation = 'browser';
    const button = document.getElementById('b-tas-presentation');
    if (button) { button.disabled = true; button.setAttribute('aria-pressed', 'false'); }
    window.dispatchEvent(new Event('resize'));
    return;
  }
  try {
    presentationLoad ||= import('./tas-presentation.js');
    const module = await presentationLoad;
    if (!enabled || ticket !== generation) return;
    presentationModule ||= module;
    if (!presentationHandle) presentationHandle = createPresentationHandle(presentationModule);
    else presentationHandle.resume();
    registerPresentationReload();
  } catch (error) {
    presentationLoad = null;
    console.error('TAS presentation could not load:', error);
  }
}
window.addEventListener('telos:tas-package-state', event => {
  if (event.detail.phase === 'ready') void setTasPresentationActive(true);
  else if (['disabled', 'error'].includes(event.detail.phase)) void setTasPresentationActive(false);
});
if (document.body.dataset.tasExtension === 'enabled') void setTasPresentationActive(true);

const local = ['localhost', '127.0.0.1', '[::1]'].includes(location.hostname);
const developmentUi = typeof __TELOS_PRODUCTION__ === 'undefined' || !__TELOS_PRODUCTION__;
if (developmentUi && local && new URLSearchParams(location.search).get('uiHotReload') === '1') {
  import(new URL('./ui-hot-reload.js', import.meta.url).href).then(({ installUiHotReload }) => {
    dev = installUiHotReload();
    registerPresentationReload();
    dev.unsupported('dock-layout', 'Dock state, live geometry, and active focus must be preserved before replacing the layout owner.', {
      url: new URL('./dock-workspace.js', import.meta.url).href,
    });
    dev.unsupported('debugger', 'Preserve capture, baseline, pending reads and local-file state before opting in.');
    dev.unsupported('editor', 'The active editor and runtime owner remain mounted.');
  }).catch(error => console.error('UI hot reload could not start:', error));
}
