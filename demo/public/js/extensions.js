import { CORE_API, ExtensionHost } from './telos-core.js';
import { TAS_MANIFEST } from '../packages/tas/manifest.js';

export { CORE_API };
export const TAS_ID = 'telos.tas';
export const extensions = new ExtensionHost();

// Static panel ownership metadata must be available before the optional pack
// downloads. Include legacy IDs so saved layouts cannot expose dormant tools.
const TAS_PANEL_IDS = Object.freeze(['score', 'right', 'branches', 'transport', 'timeline',
  'piano-roll', 'score-history', 'sample-state', 'sample-hex', 'sample-scene', 'console']);

// Only catalog metadata is loaded by Core. The optional pack owns its UI,
// editor, render loop, and input lifecycle; none is downloaded before enable.
export function registerOptionalTas(host) {
  const choiceKey = 'telos.tas.explicit-choice.v1';
  let admitted = false, wanted = false, ready = false, generation = 0, pack = null, loading = null;
  try { admitted = localStorage.getItem(choiceKey) === '1'; } catch { /* session-only state */ }
  const syncVisibility = () => {
    const available = admitted && wanted && ready && extensions.enabled(TAS_ID);
    document.body.dataset.tasExtension = available ? 'enabled' : 'disabled';
    // Availability does not rewrite saved visibility or user geometry.
    window.TelosDock?.setAvailable?.(TAS_PANEL_IDS, available);
  };
  window.addEventListener('telos:dock-ready', syncVisibility);
  window.addEventListener('telos:tas-package-state', event => {
    if (!['loading', 'ready', 'disabled', 'error'].includes(event.detail?.phase)) return;
    ready = event.detail.phase === 'ready' && wanted && extensions.enabled(TAS_ID);
    syncVisibility();
  });
  extensions.addEventListener('change', event => {
    if (event.detail.id === TAS_ID) syncVisibility();
  });
  syncVisibility();
  const load = async (ticket) => {
    try {
      loading ||= import('../packages/tas-ui/index.js').finally(() => { loading = null; });
      const module = await loading;
      if (!wanted || ticket !== generation || !extensions.enabled(TAS_ID)) return;
      pack ||= module.createTasUi(host);
      pack.activate();
    } catch (error) {
      if (wanted && ticket === generation) {
        ready = false; syncVisibility(); host.onError(error);
      }
    }
  };
  extensions.register(TAS_MANIFEST, {
    activate() {
      if (!admitted) return;
      wanted = true; ready = false;
      syncVisibility();
      void load(++generation);
    },
    deactivate() {
      wanted = false; ready = false; generation++;
      syncVisibility();
      try { pack?.deactivate(); } finally { syncVisibility(); }
    },
  }, { restore: admitted });
  // Earlier shells auto-enabled the editor. Keep their data and layouts but
  // require the package's existing explicit-choice migration before loading.
  if (!admitted) {
    if (extensions.state(TAS_ID).installed) extensions.change(TAS_ID, 'disable');
    try { localStorage.setItem(choiceKey, '1'); } catch { /* session-only state */ }
  }
  admitted = true;
  syncVisibility();
}

const stateLabel = (state) => state.cleanupPending ? 'Cleanup required'
  : state.enabled ? 'Enabled' : state.installed ? 'Installed · disabled' : 'Available';

export function mountExtensionManager() {
  const existing = document.getElementById('extension-manager');
  if (existing) return existing;
  const link = document.createElement('link');
  link.rel = 'stylesheet';
  link.href = new URL('./extensions.css', import.meta.url).href;
  document.head.append(link);

  const panel = document.createElement('section');
  panel.id = 'extension-manager';
  panel.className = 'extension-manager';
  panel.hidden = true;
  panel.dataset.telosDockWidget = 'extensions';
  panel.dataset.telosDockTitle = 'Extensions';

  const title = document.createElement('h2');
  title.textContent = 'Extensions';
  const intro = document.createElement('p');
  intro.textContent = 'Telos Core provides the game session, workspace, and package lifecycle. Debug and TAS add tools to your workspace.';

  const core = document.createElement('section');
  core.className = 'extension-core';
  const coreTitle = document.createElement('h3');
  coreTitle.textContent = 'Telos Core';
  const coreText = document.createElement('p');
  coreText.textContent = `Core extension API ${CORE_API.major}.${CORE_API.minor} · running`;
  const coreDescription = document.createElement('p');
  coreDescription.className = 'extension-note';
  coreDescription.textContent = 'Debug adds execution and memory tools. The optional TAS pack adds score editing, input recording, and run branches. Enable only the packages you use.';
  const coreActions = document.createElement('div');
  coreActions.className = 'extension-actions';
  const openDebugger = document.createElement('button');
  openDebugger.type = 'button'; openDebugger.className = 'btn';
  openDebugger.textContent = 'Open debugger';
  const exportReport = document.createElement('button');
  exportReport.type = 'button'; exportReport.className = 'btn';
  exportReport.textContent = 'Export extension report';
  exportReport.setAttribute('aria-label', 'Download extension versions, dependencies, and lifecycle diagnostics as JSON');
  coreActions.append(openDebugger, exportReport);
  core.append(coreTitle, coreText, coreDescription, coreActions);

  const note = document.createElement('p');
  note.className = 'extension-note';
  note.textContent = 'Install adds a package to this workspace; Enable starts it. Disable and Uninstall leave game files, session data, scores, and layouts intact. Packages shown as bundled are delivered with this build; this manager does not fetch remote code.';

  const filters = document.createElement('div');
  filters.className = 'extension-filters';
  const searchLabel = document.createElement('label');
  searchLabel.textContent = 'Find extensions';
  const search = document.createElement('input');
  search.type = 'search';
  search.placeholder = 'Name, capability, or dependency';
  search.setAttribute('aria-controls', 'extension-catalog');
  searchLabel.append(search);
  const filterLabel = document.createElement('label');
  filterLabel.textContent = 'Show';
  const filter = document.createElement('select');
  for (const [value, label] of [['all', 'All extensions'], ['installed', 'Installed'],
    ['enabled', 'Enabled'], ['available', 'Available to install'], ['attention', 'Needs attention']]) {
    const option = document.createElement('option');
    option.value = value; option.textContent = label; filter.append(option);
  }
  filter.setAttribute('aria-controls', 'extension-catalog');
  filterLabel.append(filter);
  filters.append(searchLabel, filterLabel);
  const count = document.createElement('p');
  count.className = 'extension-note';
  count.setAttribute('role', 'status');
  const empty = document.createElement('div');
  empty.className = 'extension-empty';
  const emptyText = document.createElement('p');
  const reset = document.createElement('button');
  reset.type = 'button'; reset.className = 'btn'; reset.textContent = 'Clear filters';
  empty.append(emptyText, reset);
  const list = document.createElement('div');
  list.id = 'extension-catalog';
  list.className = 'extension-list';
  const status = document.createElement('p');
  status.className = 'extension-status';
  status.setAttribute('role', 'status');
  status.setAttribute('aria-live', 'polite');
  panel.append(title, intro, core, note, filters, count, list, empty, status);
  document.getElementById('cockpit').append(panel);

  // These destinations are existing host panels, not manifest-granted runtime
  // capabilities. Recheck the mounted panel when invoked during shell startup.
  const openPanel = (id, label) => {
    const destination = document.querySelector(`[data-dock-id="${id}"]`);
    if (!window.TelosDock || !destination) {
      status.textContent = `${label} is not mounted in this workspace yet. Try again after the workspace finishes loading.`;
      return;
    }
    window.TelosDock.show(id);
    if (destination.hidden) {
      status.textContent = `${label} is currently unavailable in this workspace.`;
      return;
    }
    if (destination.dataset.collapsed === 'true') destination.querySelector(`[data-dock-collapse="${id}"]`)?.click();
    const details = destination.matches('details') ? destination : destination.querySelector(':scope > details');
    if (details) details.open = true;
    destination.scrollIntoView({ block: 'nearest' });
    status.textContent = `${label} opened.`;
  };
  openDebugger.addEventListener('click', () => {
    if (extensions.enabled('telos.debug')) openPanel('debug-session', 'Execution');
    else status.textContent = 'Enable Debug to open its tools.';
  });

  exportReport.addEventListener('click', () => {
    let url;
    let anchor;
    try {
      const report = {
        schema: 'telos.extension-report/v1',
        exportedAt: new Date().toISOString(),
        host: 'browser',
        coreApi: { ...CORE_API },
        persistenceReported: extensions.storage,
        packages: [...extensions.catalog.values()].map(({ manifest }) => ({
          id: manifest.id, name: manifest.name, version: manifest.version,
          api: { ...manifest.api }, source: manifest.source,
          requires: [...manifest.requires], provides: [...manifest.provides],
          permissions: [...manifest.permissions],
          ...extensions.state(manifest.id),
        })).sort((a, b) => a.id.localeCompare(b.id)),
      };
      url = URL.createObjectURL(new Blob([JSON.stringify(report, null, 2) + '\n'], { type: 'application/json' }));
      anchor = document.createElement('a');
      anchor.href = url;
      anchor.download = `telos-extensions-${report.exportedAt.replace(/[:.]/g, '-')}.json`;
      document.body.append(anchor); anchor.click();
      const downloadUrl = url;
      setTimeout(() => URL.revokeObjectURL(downloadUrl), 60000);
      url = null;
      status.textContent = `Extension report prepared: ${report.packages.length} packages. Includes lifecycle errors; review before sharing.`;
    } catch (error) {
      status.textContent = `Could not export the extension report: ${error?.message || String(error)}. Try again.`;
    } finally {
      anchor?.remove();
      if (url) URL.revokeObjectURL(url);
    }
  });

  const expandedPackages = new Set();
  const packageName = (id) => extensions.catalog.get(id)?.manifest.name || id;
  const render = () => {
    const focused = document.activeElement;
    const focusedPackage = focused?.closest('[data-extension-id]')?.dataset.extensionId;
    const focusedAction = focused?.dataset.extensionAction;
    const focusedControl = focused?.dataset.extensionFocus;
    list.replaceChildren();
    openDebugger.disabled = !extensions.enabled('telos.debug');
    const terms = search.value.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean);
    let shown = 0;
    for (const { manifest } of extensions.catalog.values()) {
      const state = extensions.state(manifest.id);
      const searchable = [manifest.id, manifest.name, manifest.description,
        ...manifest.provides, ...manifest.requires, ...manifest.requires.map(packageName)].join(' ').toLocaleLowerCase();
      const matchesState = filter.value === 'all'
        || (filter.value === 'installed' && state.installed)
        || (filter.value === 'enabled' && state.enabled)
        || (filter.value === 'available' && !state.installed)
        || (filter.value === 'attention' && (state.error || state.cleanupPending));
      if (!matchesState || !terms.every(term => searchable.includes(term))) continue;
      shown += 1;
      const card = document.createElement('article');
      card.dataset.extensionId = manifest.id;
      card.tabIndex = -1;
      const heading = document.createElement('div');
      heading.className = 'extension-heading';
      const name = document.createElement('h3');
      name.textContent = manifest.name;
      const badge = document.createElement('span');
      badge.className = 'extension-state';
      badge.dataset.state = state.error ? 'error' : state.enabled ? 'enabled' : state.installed ? 'disabled' : 'available';
      badge.textContent = state.cleanupPending ? stateLabel(state) : state.error ? 'Needs attention' : stateLabel(state);
      heading.append(name, badge);

      const description = document.createElement('p');
      description.textContent = manifest.description;
      const guidance = document.createElement('p');
      guidance.className = 'extension-note';
      guidance.textContent = state.cleanupPending
        ? 'Retry cleanup before enabling this package again. Some shutdown work did not finish.'
        : state.enabled ? 'Enabled in this workspace.'
        : state.installed ? 'Installed in this workspace. Enable it to use its tools.'
        : 'Bundled with this build. Install it in this workspace, then enable it to use its tools.';
      const tools = document.createElement('div');
      tools.className = 'extension-tools';
      if (manifest.id === 'telos.debug' && state.enabled) {
        const shortcuts = document.createElement('div');
        shortcuts.className = 'extension-actions';
        for (const [id, label] of [['debug-session', 'Execution'], ['debugger', 'Memory & diagnostics'], ['debug-watches', 'Watches'], ['debug-processes', 'Processes']]) {
          const shortcut = document.createElement('button');
          shortcut.type = 'button'; shortcut.className = 'btn'; shortcut.textContent = `Open ${label}`;
          shortcut.dataset.extensionFocus = `panel:${id}`;
          shortcut.addEventListener('click', () => { if (extensions.enabled('telos.debug')) openPanel(id, label); });
          shortcuts.append(shortcut);
        }
        tools.append(shortcuts);
      }
      if (manifest.id === TAS_ID) {
        const scope = document.createElement('p');
        scope.className = 'extension-note';
        scope.textContent = 'Score editor, Transport / Timeline, Branches and input recording belong to this optional pack. They remain hidden until TAS is enabled and ready. Live actions depend on the connected game session.';
        tools.append(scope);
        if (state.enabled) {
          const shortcuts = document.createElement('div');
          shortcuts.className = 'extension-actions';
          for (const [id, label] of [['score', 'score editor'], ['transport', 'transport / timeline'], ['right', 'branches'], ['piano-roll', 'piano roll']]) {
            const shortcut = document.createElement('button');
            shortcut.type = 'button'; shortcut.className = 'btn';
            shortcut.textContent = `Open ${label}`;
            shortcut.dataset.extensionFocus = `panel:${id}`;
            shortcut.addEventListener('click', () => {
              if (extensions.enabled(TAS_ID)) openPanel(id, label[0].toUpperCase() + label.slice(1));
            });
            shortcuts.append(shortcut);
          }
          tools.append(shortcuts);
        }
      }
      const details = document.createElement('details');
      details.className = 'extension-details';
      details.open = expandedPackages.has(manifest.id);
      const summary = document.createElement('summary');
      summary.textContent = 'Package details and access';
      summary.dataset.extensionFocus = 'details';
      details.addEventListener('toggle', () => {
        if (details.open) expandedPackages.add(manifest.id);
        else expandedPackages.delete(manifest.id);
      });
      const metadata = document.createElement('dl');
      metadata.className = 'extension-metadata';
      const rows = [
        ['Package', manifest.id],
        ['Version', manifest.version],
        ['Core API', `${manifest.api.major}.${manifest.api.minor}`],
        ['Delivery', manifest.source === 'bundled' ? 'Bundled with this build' : 'Unavailable'],
      ];
      if (manifest.requires.length) rows.push(['Dependencies', manifest.requires.join(', ')]);
      if (manifest.provides.length) rows.push(['Adds', manifest.provides.join(', ')]);
      if (manifest.permissions.length) rows.push(['Core access', manifest.permissions.join(', ')]);
      for (const [label, value] of rows) {
        const term = document.createElement('dt'); term.textContent = label;
        const detail = document.createElement('dd'); detail.textContent = value;
        metadata.append(term, detail);
      }

      details.append(summary, metadata);
      // Catalog relationships include uninstalled packages; lifecycle dependents
      // below deliberately remain limited to those that block removal.
      const catalogDependents = extensions.dependents(manifest.id, { installedOnly: false });
      for (const [label, ids] of [['Requires', manifest.requires], ['Used by', catalogDependents]]) {
        if (!ids.length) continue;
        const related = document.createElement('div');
        related.className = 'extension-related';
        const caption = document.createElement('span'); caption.textContent = label;
        related.append(caption);
        for (const id of ids) {
          const jump = document.createElement('button');
          jump.type = 'button'; jump.className = 'btn'; jump.textContent = packageName(id);
          jump.dataset.extensionFocus = `related:${label}:${id}`;
          jump.disabled = !extensions.catalog.has(id);
          jump.setAttribute('aria-label', `View ${packageName(id)} package`);
          jump.addEventListener('click', () => {
            search.value = ''; filter.value = 'all'; expandedPackages.add(id); render();
            const destination = [...list.children].find(node => node.dataset.extensionId === id);
            destination?.focus({ preventScroll: true });
            destination?.scrollIntoView({ block: 'nearest' });
          });
          related.append(jump);
        }
        details.append(related);
      }
      const dependents = extensions.dependents(manifest.id);
      const activeDependents = dependents.filter((id) => extensions.enabled(id));
      const dependencyNote = document.createElement('p');
      dependencyNote.id = `extension-dependencies-${manifest.id}`;
      dependencyNote.className = 'extension-note';
      dependencyNote.hidden = !dependents.length;
      dependencyNote.textContent = activeDependents.length
        ? `Disable ${activeDependents.map(packageName).join(', ')} before disabling ${manifest.name}. Uninstall dependent packages before removing it.`
        : dependents.length ? `Required by ${dependents.map(packageName).join(', ')}. Uninstall those packages before removing ${manifest.name}.` : '';
      const failure = document.createElement('p');
      failure.className = 'extension-error';
      failure.hidden = !state.error;
      failure.textContent = state.error;
      const actions = document.createElement('div');
      actions.className = 'extension-actions';
      const choices = state.installed
        ? [[state.enabled || state.cleanupPending ? 'disable' : 'enable', state.cleanupPending ? 'Retry cleanup' : state.enabled ? 'Disable' : 'Enable'], ['remove', 'Uninstall']]
        : [['install', 'Install']];
      for (const [action, label] of choices) {
        const button = document.createElement('button');
        button.type = 'button'; button.className = 'btn'; button.textContent = label;
        button.setAttribute('aria-label', `${label} ${manifest.name}`);
        button.dataset.extensionAction = action;
        button.dataset.extensionFocus = `action:${action}`;
        const blocked = action === 'remove' ? dependents.length > 0
          : action === 'disable' && activeDependents.length > 0;
        button.disabled = blocked;
        if (blocked) button.setAttribute('aria-describedby', dependencyNote.id);
        button.addEventListener('click', () => {
          try { extensions.change(manifest.id, action); }
          catch (error) {
            render();
            status.textContent = `${manifest.name}: ${error?.message || String(error)}`;
          }
        });
        actions.append(button);
      }
      card.append(heading, description, guidance, tools, actions, dependencyNote, details, failure);
      list.append(card);
    }
    count.textContent = `${shown} of ${extensions.catalog.size} extensions`;
    empty.hidden = shown > 0;
    emptyText.textContent = extensions.catalog.size ? 'No extensions match these filters.' : 'No extensions are registered.';
    reset.hidden = !search.value && filter.value === 'all';
    // A dependency operation can rebuild unrelated cards too. Preserve each
    // control's identity, including details and navigation, across that rebuild.
    if (focusedPackage) {
      const card = [...list.children].find((node) => node.dataset.extensionId === focusedPackage);
      const preferred = focusedControl && [...(card?.querySelectorAll('[data-extension-focus]') || [])]
        .find(node => node.dataset.extensionFocus === focusedControl && !node.matches(':disabled'));
      // Install becomes Enable, and Enable becomes Disable. Stay in the
      // lifecycle actions instead of jumping to a newly visible tool shortcut.
      const replacement = focusedAction && card?.querySelector('[data-extension-action]:not(:disabled)');
      (preferred || replacement || card || filter).focus({ preventScroll: true });
    }
  };

  search.addEventListener('input', render);
  search.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && search.value) {
      event.preventDefault(); event.stopPropagation(); search.value = ''; render();
    }
  });
  filter.addEventListener('change', render);
  reset.addEventListener('click', () => {
    search.value = ''; filter.value = 'all'; render(); search.focus();
  });

  extensions.addEventListener('change', (event) => {
    render();
    const manifest = extensions.catalog.get(event.detail.id)?.manifest;
    if (!manifest) return;
    const messages = {
      install: `${manifest.name} installed. Enable it when you want to use it.`,
      enable: `${manifest.name} enabled.`,
      disable: `${manifest.name} disabled; workspace data is retained.`,
      remove: `${manifest.name} uninstalled; workspace data is retained.`,
    };
    status.textContent = messages[event.detail.action] || `${manifest.name} updated.`;
    if (!event.detail.persisted) status.textContent += ' The browser could not save this choice; it applies for this visit.';
  });
  const register = () => window.TelosDock?.register('extensions', 'Extensions', panel, { visible: false });
  window.addEventListener('telos:dock-ready', register);
  register();
  const button = document.createElement('button');
  button.className = 'btn'; button.type = 'button'; button.textContent = 'Extensions';
  button.addEventListener('click', () => { if (window.TelosDock) window.TelosDock.show('extensions'); else panel.hidden = !panel.hidden; });
  document.querySelector('header.top').append(button);
  render();
  return panel;
}
