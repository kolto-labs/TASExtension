// Core workspace: game ownership, package registration and target navigation.
// Score parsing, simulation, timeline and editor code load only with the TAS pack.
import { extensions, TAS_ID, mountExtensionManager, registerOptionalTas } from './extensions.js';
import { OriginalGame } from './original-game.js?v=9-compiled-input-batch';
import { mountDebuggerWorkbench } from './debugger-workbench.js';
import { DEBUG_MANIFEST, createDebugUiLifecycle } from '../packages/debug-ui/index.js';
import { createDebugRuntime, createGuestMemoryAdapter, createGuestExecutionAdapter, createGuestThreadAdapter, createGuestRegisterAdapter, createGuestMappingAdapter } from '../packages/debug-runtime/runtime.js';
import { mountRecordingLibrary } from './recordings-view.js';
import { createSelectedDebugRuntime } from '../packages/debug-runtime/selected-runtime.js';
import { createStaticFileInspectionOwner } from '../debugger/static-file-owner.js';

const $ = id => document.getElementById(id);
const reduced = matchMedia('(prefers-reduced-motion: reduce)');
const recordingLibraryView = ['127.0.0.1', 'localhost', '[::1]'].includes(location.hostname)
  && new URLSearchParams(location.search).get('view') === 'saved-recordings';
let target = 'original', session = null, footage = null, footageLoading = null;
const game = new OriginalGame($('original-game'));
const debugRuntime = createDebugRuntime({ getSession:() => game.session, adapters:[createGuestMemoryAdapter(game), createGuestExecutionAdapter(game), createGuestThreadAdapter(game), createGuestRegisterAdapter(game), createGuestMappingAdapter(game)] });

function updateTargetScope() {
  const value = session?.session === game.session ? session : null;
  const capabilities = value?.capabilities || {};
  let text = target === 'scene' ? 'Target: Sample scene · Score playback and sample state'
    : target === 'real' ? 'Target: Recorded footage · Video playback'
    : recordingLibraryView ? 'Target: Saved recordings · Local receipt and footage inspection' : '';
  if (!text) {
    const actions = [];
    if (extensions.enabled(TAS_ID) && capabilities.input === true) actions.push('Score → live game input');
    if (extensions.enabled('telos.debug') && capabilities.pause === true && capabilities.resume === true) actions.push('Pause / resume');
    if (extensions.enabled('telos.debug') && capabilities.renderStep === true && value.executionBoundary === 'render-present') actions.push('Render step');
    text = `Target: ${value?.guestId || 'KOTOR'} · ${actions.join(' · ') || value?.status || 'Connecting'}`;
  }
  if ($('target-scope').textContent !== text) $('target-scope').textContent = text;
}

async function openFootage() {
  if (footage) return footage.open();
  if (footageLoading) return footageLoading;
  footageLoading = import('./footage.js').then(({ Footage }) => {
    footage = new Footage($('footage'), { reduced: () => reduced.matches });
    return footage.open();
  }).catch(error => {
    console.error('Recorded footage could not load:', error);
    $('footage').textContent = 'Recorded footage could not load. Open Proof of Concept again to retry.';
  }).finally(() => { footageLoading = null; });
  return footageLoading;
}

function selectTarget(which) {
  if (!['original', 'scene', 'real'].includes(which) || (which === 'scene' && document.body.dataset.tasExtension !== 'enabled')) return;
  const previous = target; target = which;
  dispatchEvent(new CustomEvent('telos:wasm-target', { detail: { active: which === 'original' } }));
  if (previous === 'original' && which !== 'original' && extensions.enabled(TAS_ID)) {
    dispatchEvent(new CustomEvent('telos:wasm-release', { detail: { reason: 'target-changed' } }));
    dispatchEvent(new CustomEvent('telos:wasm-vm-control', { detail: { action: 'pause' } }));
  }
  for (const name of ['original', 'scene', 'real']) {
    const tab = $(`tab-${name}`), pane = $(`${name}-pane`), on = name === which;
    if (!tab || !pane) continue;
    if (tab.dataset.dockViewShortcut) { tab.setAttribute('aria-pressed', String(on)); tab.tabIndex = 0; }
    else { tab.setAttribute('aria-selected', String(on)); tab.tabIndex = on ? 0 : -1; }
    if (!pane.dataset.dockId) pane.hidden = !on;
    else if (on) window.TelosDock?.show(pane.dataset.dockId);
  }
  for (const control of document.querySelectorAll('[data-center-target]')) control.setAttribute('aria-pressed', String(control.dataset.centerTarget === which));
  const hideScene = $('b-hide-scene');
  if (hideScene) hideScene.hidden = !$('scene-pane')?.dataset.dockId && which !== 'scene';
  if (which === 'real') void openFootage();
  dispatchEvent(new CustomEvent('telos:workspace-target', { detail: { target } }));
  updateTargetScope();
}

function registerCoreViews() {
  const dock = window.TelosDock; if (!dock) return;
  const pane = $('real-pane');
  if (!pane.dataset.dockId && dock.register('captured-footage', 'Proof of Concept / captured footage', pane, { visible: false })) {
    pane.classList.add('panel'); pane.setAttribute('role', 'region'); pane.removeAttribute('aria-labelledby');
    pane.setAttribute('aria-label', 'Proof of Concept / captured footage');
    const controls = document.createElement('div'); controls.className = 'row';
    const button = document.createElement('button'); button.type = 'button'; button.className = 'btn small';
    button.textContent = 'Use recorded footage target'; button.dataset.centerTarget = 'real'; button.setAttribute('aria-pressed', String(target === 'real'));
    button.addEventListener('click', () => selectTarget('real')); controls.append(button); pane.querySelector('.dock-titlebar').after(controls);
    new MutationObserver(() => { if (!pane.hidden) void openFootage(); }).observe(pane, { attributes: true, attributeFilter: ['hidden'] });
    if (!pane.hidden) void openFootage();
  }
  const navigation = $('tab-original').parentElement;
  navigation.setAttribute('role', 'group'); navigation.setAttribute('aria-label', 'Choose playback target and open its view');
  for (const name of ['original', 'scene', 'real']) {
    const button = $(`tab-${name}`); if (!button) continue; button.removeAttribute('role'); button.removeAttribute('aria-selected');
    button.setAttribute('aria-pressed', String(target === name)); button.tabIndex = 0;
    button.classList.add('btn', 'small'); button.dataset.dockViewShortcut = 'true';
  }
  $('original-pane').setAttribute('role', 'region'); $('original-pane').removeAttribute('aria-labelledby'); $('original-pane').setAttribute('aria-label', 'KOTOR game');
}
document.addEventListener('click', event => {
  const tab = event.target.closest('[id^="tab-"]');
  if (tab && ['original', 'scene', 'real'].includes(tab.id.slice(4))) selectTarget(tab.id.slice(4));
});
document.addEventListener('keydown', event => {
    const tab = event.target.closest('[id^="tab-"]');
    const name = tab?.id.slice(4);
    if (!['original', 'scene', 'real'].includes(name)) return;
    if (event.ctrlKey || event.metaKey || event.altKey || event.shiftKey || !['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault(); event.stopPropagation();
    const items = ['original', 'scene', 'real'].filter(item => item !== 'scene' || document.body.dataset.tasExtension === 'enabled');
    const index = event.key === 'Home' ? 0 : event.key === 'End' ? items.length - 1 : (items.indexOf(name) + (event.key === 'ArrowRight' ? 1 : items.length - 1)) % items.length;
    const next = $(`tab-${items[index]}`); next?.focus(); next?.click();
});
addEventListener('telos:runtime-session', event => {
  if (event.detail?.schema !== 'telos-runtime-session/v1' || event.detail.session !== game.session) return;
  session = event.detail; updateTargetScope();
});
addEventListener('telos:dock-ready', registerCoreViews);
registerCoreViews();

const selectedDebugger = createSelectedDebugRuntime({
  getSession() { return debugRuntime.getSession(); },
  getTarget() { return game.session ? game.getRuntimeTarget() : null; },
  getGame() { return game; },
  getStatus() {
    const value = window.__telosRuntimeDiagnostics;
    if (!game.session || value?.session !== game.session) return null;
    return Object.freeze({ ...value,
      target:game.getRuntimeTarget(),
      pausedCaptureReady:value.state === 'paused' && Number.isSafeInteger(value.activeThreads)
        && value.activeThreads > 0 && Number.isSafeInteger(value.parkedThreads)
        && value.activeThreads === value.parkedThreads,
    });
  },
  capabilities() { return debugRuntime.capabilities(); },
  request(request, options) { return debugRuntime.request(request, options); },
  mountWorkbench(options) { return mountDebuggerWorkbench(game, { ...options, debugRuntime, mountRoot: $('cockpit') }); },
});
extensions.provide('debug.runtime', selectedDebugger.runtime);
extensions.provide('debug.session-owner', selectedDebugger.owner);
extensions.provide('debug.file-inspection', createStaticFileInspectionOwner(selectedDebugger.owner, selectedDebugger.runtime));
const tasHost = {
  getGame: () => game, getTarget: () => target, selectTarget, openFootage,
  async openReferenceDraft(file, isCurrent) {
    if (!isCurrent()) return false;
    await openFootage();
    if (!isCurrent() || !footage) return false;
    const opened = await footage.openObservationDraft(file, isCurrent);
    if (!opened || !isCurrent()) return false;
    window.TelosDock?.show('captured-footage');
    return true;
  },
  isEnabled: () => extensions.enabled(TAS_ID),
  onError(error) {
    console.error('TAS package load failed:', error);
    try { extensions.change(TAS_ID, 'disable'); } catch (cleanupError) { console.error('TAS package cleanup failed:', cleanupError); }
    const status = document.querySelector('.extension-status');
    if (status) status.textContent = `TAS could not load: ${error?.message || String(error)}. Enable the package to retry.`;
  },
};
try { extensions.register(DEBUG_MANIFEST, createDebugUiLifecycle()); }
catch (error) { console.error('Debug package registration failed:', error); }
try { registerOptionalTas(tasHost); }
catch (error) { console.error('TAS package registration failed:', error); }
mountExtensionManager();
function syncPackagePresentation() {
  game.setPackagePresentation({ debug: extensions.enabled('telos.debug'), tas: document.body.dataset.tasExtension === 'enabled' });
  updateTargetScope();
}
extensions.addEventListener('change', syncPackagePresentation);
syncPackagePresentation();
addEventListener('telos:tas-package-state', event => {
  syncPackagePresentation();
  const status = document.querySelector('.extension-status');
  if (status && event.detail.phase === 'loading') status.textContent = 'Loading TAS tools…';
  if (status && event.detail.phase === 'ready') status.textContent = 'TAS tools ready. Open a panel from Extensions or Panels.';
});
selectTarget('original');
if (['127.0.0.1', 'localhost', '[::1]'].includes(location.hostname)) {
  const link = document.createElement('a'); link.textContent = 'Saved recordings';
  link.href = '/?view=saved-recordings'; link.target = '_blank'; link.rel = 'noopener';
  link.className = 'btn small';
  $('tab-original').parentElement.append(link);
}
if (recordingLibraryView) {
  mountRecordingLibrary($('original-game'));
  updateTargetScope();
} else game.open();
