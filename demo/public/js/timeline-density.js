// Presentation preference only. An unloaded TAS owner retains the intention;
// this seam never installs a package or sends a guest/runtime command.
export const TIMELINE_DENSITY_KEY = 'telos.timeline-density';
export const TIMELINE_DENSITIES = Object.freeze(['editor', 'timeline', 'minimal']);
let intention = null;
export function preferredTimelineDensity() {
  if (TIMELINE_DENSITIES.includes(intention)) return intention;
  try {
    const saved = localStorage.getItem(TIMELINE_DENSITY_KEY);
    if (TIMELINE_DENSITIES.includes(saved)) return saved;
    return localStorage.getItem('telos.timeline.expanded') === 'true' ? 'editor' : 'timeline';
  } catch { return 'timeline'; }
}
export function rememberTimelineDensity(density, { persist = false } = {}) {
  if (!TIMELINE_DENSITIES.includes(density)) throw new Error('Unsupported timeline density.');
  if (persist) localStorage.setItem(TIMELINE_DENSITY_KEY, density);
  intention = density;
}
export function requestTimelineDensity(density, { persist = true } = {}) {
  rememberTimelineDensity(density,{persist});
  const detail = { density, applied:false, error:null };
  window.dispatchEvent(new CustomEvent('telos:timeline-density', {detail}));
  if (detail.error) throw new Error(detail.error);
  return detail.applied;
}
