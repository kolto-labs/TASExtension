// Package-owned markup is fetched only when TAS is enabled. Detached nodes are
// retained for this session so disabling tools does not discard unsaved scores.
export function createTasWorkspace(panelIds) {
  let templates = null, preparing = null, roots = [], detached = [], docks = [], mounted = false;
  const stylesheet = document.createElement('link');
  stylesheet.rel = 'stylesheet';
  stylesheet.href = new URL('../../js/tas-presentation.css', import.meta.url).href;
  async function prepare() {
    if (templates) return;
    preparing ||= fetch(new URL('./workspace.html', import.meta.url)).then(async response => {
      if (!response.ok) throw new Error(`TAS workspace could not load (HTTP ${response.status})`);
      const parsed = document.createElement('template');
      parsed.innerHTML = await response.text();
      templates = [...parsed.content.querySelectorAll('template[data-tas-mount]')];
      if (!templates.length) { templates = null; throw new Error('TAS workspace markup is missing'); }
    }).finally(() => { preparing = null; });
    return preparing;
  }
  function mount() {
    if (mounted) return;
    if (!templates) throw new Error('TAS workspace has not loaded');
    document.head.append(stylesheet);
    if (!roots.length) {
      for (const template of templates) {
        const parent = document.querySelector(template.dataset.tasMount);
        if (!parent) throw new Error(`Missing TAS mount point: ${template.dataset.tasMount}`);
        const nodes = [...template.content.children];
        for (const node of nodes) { node.dataset.tasOwner = 'telos.tas'; roots.push(node); }
        parent.append(template.content);
      }
    } else {
      for (const { node, parent, next } of detached) {
        parent.insertBefore(node, next?.parentNode === parent ? next : null);
      }
      for (const { id, title, node, defaults } of docks) {
        window.TelosDock?.register(id, title, node, defaults);
      }
    }
    window.TelosDock?.migrateLegacyDefaults?.(panelIds);
    for (const [id, title, elementId, area] of [
      ['score', 'Score editor', 'p-score', 'score'],
      ['right', 'Branches', 'p-branches', 'right'],
      ['transport', 'Transport and timeline', 'live-tas-workspace', 'transport'],
      ['console', 'Score console', 'p-console', 'console'],
    ]) {
      const node = document.getElementById(elementId);
      if (node && !node.dataset.dockId) window.TelosDock?.register(id, title, node, { visible:false, area });
    }
    const legacyRight = document.getElementById('p-right');
    if (legacyRight) legacyRight.hidden = true;
    mounted = true;
  }
  function unmount() {
    if (!mounted && !roots.length) return;
    docks = panelIds.map(id => window.TelosDock?.unregister?.(id)).filter(Boolean);
    // Panels have already detached their descendants. Remove only the remaining
    // package roots, retaining their current parent for an exact re-enable.
    detached = roots.filter(node => node.isConnected).map(node => ({ node, parent:node.parentNode, next:node.nextSibling }));
    for (const { node } of detached) node.remove();
    stylesheet.remove(); mounted = false;
  }
  return { prepare, mount, unmount };
}
