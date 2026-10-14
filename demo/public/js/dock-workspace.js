import { SHARED_PRESENTATION_FORMAT, PREVIOUS_SHARED_PRESENTATION_FORMAT, GAME_ONLY_SHARED_PRESENTATION_FORMAT, LEGACY_SHARED_PRESENTATION_FORMAT, SHARED_PROFILE_STORAGE, validateSharedProfile, retainedHostProfiles } from './shared-presentation-profile.js';
import { TIMELINE_DENSITY_KEY, preferredTimelineDensity, requestTimelineDensity } from './timeline-density.js';

const STORAGE_KEY = 'telos.dock-workspace.v1';
const PRESETS_KEY = 'telos.dock-layout-presets.v1';
const FLOATING_DEFAULT = { x: 0.6, y: 0.12, width: 0.36, height: 0.56 };
const AREAS = ['score', 'game', 'right', 'transport', 'console'];
const AREA_LABELS = {
  score: 'Left', game: 'Center', right: 'Right', transport: 'Lower center', console: 'Bottom',
};
const DEFAULTS = {
  version: 1,
  splits: { left: 0.23, right: 0.24, transport: 0.14, bottom: 0.18 },
  panels: {
    game: { area: 'game', visible: true, collapsed: false, expanded: false, mobileHeight: null, opacity: 1, floating: null },
  },
};

const clamp = (value, low, high) => Math.max(low, Math.min(high, value));
const clone = (value) => JSON.parse(JSON.stringify(value));

function validFloating(value) {
  if (!value || typeof value !== 'object') return null;
  const keys = ['x', 'y', 'width', 'height'];
  if (!keys.every((key) => Number.isFinite(value[key]))) return null;
  return {
    x: clamp(value.x, 0, 0.98), y: clamp(value.y, 0, 0.98),
    width: clamp(value.width, 0.18, 1), height: clamp(value.height, 0.16, 1),
  };
}

function decodeState(value) {
  if (!value || value.version !== 1 || !value.panels || !value.splits) return null;
  const state = clone(DEFAULTS);
  for (const id of Object.keys(DEFAULTS.panels)) {
    const input = value.panels[id];
    if (!input || !AREAS.includes(input.area) && input.area !== 'float') return null;
    state.panels[id] = {
      area: input.area,
      returnArea: AREAS.includes(input.returnArea) ? input.returnArea : DEFAULTS.panels[id].area,
      visible: input.visible !== false, visibilityExplicit:input.visibilityExplicit === true,
      collapsed: input.collapsed === true,
      expanded: input.expanded === true,
      mobileHeight: Number.isFinite(input.mobileHeight) ? clamp(input.mobileHeight, 0.18, 0.8) : null,
      opacity: Number.isFinite(input.opacity) ? clamp(input.opacity, 0.55, 1) : 1,
      floating: validFloating(input.floating),
    };
  }
  for (const [id, input] of Object.entries(value.panels)) {
    if (Object.hasOwn(DEFAULTS.panels, id)) continue;
    if (!/^[a-z][a-z0-9-]{0,47}$/.test(id) || !input || (!AREAS.includes(input.area) && input.area !== 'float')) return null;
    state.panels[id] = {
      area:input.area, returnArea:AREAS.includes(input.returnArea) ? input.returnArea : 'right',
      visible:input.visible === true, visibilityExplicit:input.visibilityExplicit === true, collapsed:input.collapsed === true,
      expanded:input.expanded === true,
      mobileHeight:Number.isFinite(input.mobileHeight) ? clamp(input.mobileHeight, 0.18, 0.8) : null,
      opacity:Number.isFinite(input.opacity) ? clamp(input.opacity, 0.55, 1) : 1,
      floating:validFloating(input.floating),
    };
  }
  // Dormant packages retain geometry without claiming an active dock slot.
  state.layoutIntent = value.layoutIntent === 'explicit' ? 'explicit' : value.layoutIntent === 'migrated-default' ? 'migrated-default' : null;
  for (const [key, bounds] of Object.entries({ left: [0.16, 0.42], right: [0.16, 0.42], transport: [0.08, 0.34], bottom: [0.1, 0.48] })) {
    const splitValue = value.splits[key];
    if (!Number.isFinite(splitValue)) return null;
    state.splits[key] = clamp(splitValue, ...bounds);
  }
  return state;
}

function readSavedState() {
  try { return decodeState(JSON.parse(localStorage.getItem(STORAGE_KEY) || 'null')) || clone(DEFAULTS); }
  catch { return clone(DEFAULTS); }
}

function readPresets() {
  try {
    const saved = JSON.parse(localStorage.getItem(PRESETS_KEY) || 'null');
    if (saved?.version !== 1 || !saved.layouts || typeof saved.layouts !== 'object') return {};
    return Object.fromEntries(Object.entries(saved.layouts).slice(0, 20).flatMap(([name, layout]) => {
      const clean = name.trim().slice(0, 32);
      const state = decodeState(layout);
      return clean && state ? [[clean, state]] : [];
    }));
  } catch { return {}; }
}

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

function addDockStyles() {
  if (document.querySelector('link[data-telos-dock-styles]')) return;
  const link = element('link');
  link.rel = 'stylesheet';
  link.href = new URL('./dock-workspace.css', import.meta.url).href;
  link.dataset.telosDockStyles = 'true';
  document.head.append(link);
}

function panelTitle(id) {
  return ({
    score: 'Score editor', game: 'Game and captures', right: 'Branches',
    transport: 'Transport and timeline', console: 'Console',
    debugger: 'Debugger', 'runtime-inspector': 'Session inspector',
  })[id] || id;
}

export function mountDockWorkspace() {
  if (window.TelosDock?.version === 1) return window.TelosDock;
  const cockpit = document.getElementById('cockpit');
  if (!cockpit) return null;

  addDockStyles();
  const state = readSavedState();
  const presets = readPresets();
  const panels = new Map();
  // Availability is session capability state, not a saved layout preference.
  // The shell can mount before the package host, or survive its startup failure.
  // Saved visibility must not expose optional tools before their runtime is ready.
  const unavailablePanels = new Set();
  const sizeObservers = new Map();
  const panelEnvironment = new WeakMap();
  const panelEnvironmentCleanup = new Map();
  const toggleBound = new WeakSet();
  const floatingOrder = [];
  const panelDefaults = new Map(Object.entries(DEFAULTS.panels).map(([id, value]) => [id, clone(value)]));
  const toolbar = element('nav', 'dock-toolbar');
  toolbar.setAttribute('aria-label', 'Workspace layout');
  const savedSelect = element('select', 'dock-layout-select');
  savedSelect.setAttribute('aria-label', 'Saved workspace layouts');
  const currentOption = new Option('Current layout', 'current');
  savedSelect.add(currentOption);
  for (const name of Object.keys(presets).sort((a, b) => a.localeCompare(b))) savedSelect.add(new Option(name, name));
  const nameInput = element('input', 'dock-layout-name');
  nameInput.type = 'text'; nameInput.maxLength = 32; nameInput.placeholder = 'Layout name';
  nameInput.setAttribute('aria-label', 'Name for saved layout');
  const saveLayout = element('button', 'btn small', 'Save layout'); saveLayout.type = 'button';
  const exportLayout = element('button', 'btn small', 'Export'); exportLayout.type = 'button';
  const exportShared = element('button', 'btn small', 'Export shared'); exportShared.type = 'button';
  exportShared.setAttribute('aria-label', 'Export presentation profile for browser and native app');
  const importLayout = element('button', 'btn small', 'Import'); importLayout.type = 'button';
  const importFile = element('input'); importFile.type = 'file'; importFile.accept = 'application/json,.json'; importFile.hidden = true;
  importFile.setAttribute('aria-label', 'Import a Telos layout profile');
  const reset = element('button', 'btn small dock-reset', 'Reset layout'); reset.type = 'button';
  const status = element('output', 'dock-status'); status.setAttribute('role', 'status'); status.setAttribute('aria-live', 'polite');
  const panelList = element('details', 'dock-panel-list');
  const panelListSummary = element('summary', 'btn small', 'Panels');
  const panelListBody = element('div', 'dock-panel-list-body');
  panelList.append(panelListSummary, panelListBody);
  toolbar.append(panelList, savedSelect, nameInput, saveLayout, exportLayout, exportShared, importLayout, importFile, reset, status);
  const top = document.querySelector('header.top');
  if (top) top.after(toolbar); else cockpit.before(toolbar);

  // Core owns only the game. Optional packages register their own panels.
  document.body.dataset.dockWorkspace = 'true';
  for (const id of Object.keys(DEFAULTS.panels)) {
    const node = document.getElementById('p-center');
    if (!node) continue;
    panels.set(id, node);
    node.classList.add('dock-panel');
    node.dataset.dockId = id;
    addPanelChrome(id, node);
  }


  const splitters = addSplitters();
  const saveTimer = { value: 0 };
  let renderRefreshFrame = 0;
  let activeLayout = 'current';

  function announce(message) { status.textContent = message; }

  function raiseFloatingPanel(id) {
    if (state.panels[id]?.area !== 'float') return;
    for (const key of panels.keys()) {
      if (state.panels[key]?.area === 'float' && !floatingOrder.includes(key)) floatingOrder.push(key);
    }
    const previous = floatingOrder.indexOf(id);
    if (previous >= 0) floatingOrder.splice(previous, 1);
    floatingOrder.push(id);
    // Reassign compact ranks instead of growing z-index with every interaction.
    const active = floatingOrder.filter((key) => state.panels[key]?.area === 'float');
    floatingOrder.splice(0, floatingOrder.length, ...active);
    active.forEach((key, index) => panels.get(key)?.style.setProperty('--dock-stack', String(80 + index)));
  }

  function persist() {
    clearTimeout(saveTimer.value);
    saveTimer.value = setTimeout(() => {
      try { localStorage.setItem(STORAGE_KEY, JSON.stringify(state)); }
      catch { announce('Layout is active, but browser storage is unavailable.'); }
    }, 120);
  }

  function updatePanelList() {
    panelListBody.replaceChildren();
    for (const [id, node] of panels) {
      if (unavailablePanels.has(id)) continue;
      const title = node.querySelector('.dock-title')?.textContent || panelTitle(id);
      const label = element('label', 'dock-panel-check');
      const checkbox = element('input'); checkbox.type = 'checkbox'; checkbox.checked = state.panels[id].visible;
      checkbox.dataset.panelId = id;
      checkbox.setAttribute('aria-label', `Show ${title}`);
      checkbox.addEventListener('change', () => setVisible(id, checkbox.checked));
      label.append(checkbox, element('span', '', title));
      panelListBody.append(label);
    }
  }

  function clampFloat(id) {
    const node = panels.get(id), placement = state.panels[id];
    if (!node || placement.area !== 'float') return;
    const maxWidth = Math.max(1, innerWidth - 16), maxHeight = Math.max(1, innerHeight - 16);
    const float = placement.floating || FLOATING_DEFAULT;
    const width = clamp(float.width * innerWidth, Math.min(260, maxWidth), maxWidth);
    const height = clamp(float.height * innerHeight, Math.min(180, maxHeight), maxHeight);
    const left = clamp(float.x * innerWidth, 8, innerWidth - width - 8);
    const topPx = clamp(float.y * innerHeight, 8, innerHeight - height - 8);
    placement.floating = { x: left / innerWidth, y: topPx / innerHeight, width: width / innerWidth, height: height / innerHeight };
    node.style.left = `${left}px`; node.style.top = `${topPx}px`;
    node.style.width = `${width}px`; node.style.height = placement.collapsed ? '34px' : `${height}px`;
  }

  function areaOccupant(area) {
    return [...panels.keys()].filter(id => !unavailablePanels.has(id)).map(id => state.panels[id]).find(panel => panel.visible && panel.area === area);
  }

  function syncSizeControls(id, node) {
    const placement = state.panels[id];
    const mobileDock = placement.area !== 'float' && innerWidth <= 760;
    const title = node.querySelector('.dock-title')?.textContent || panelTitle(id);
    for (const input of node.querySelectorAll(`[data-dock-size="${id}"]`)) {
      const dimension = input.dataset.dimension;
      const mobileHeight = mobileDock && dimension === 'height';
      const viewportSize = dimension === 'width' ? innerWidth : innerHeight;
      input.disabled = (placement.area !== 'float' && !mobileHeight) || placement.collapsed
        || document.body.dataset.tasPresentation === 'overlay';
      const max = mobileHeight ? Math.max(1, Math.floor(innerHeight * 0.8)) : Math.max(1, viewportSize - 16);
      const min = mobileHeight
        ? Math.min(max, Math.max(Math.ceil(innerHeight * 0.18), parseFloat(getComputedStyle(node).minHeight) || 0))
        : Math.min(dimension === 'width' ? 260 : 180, max);
      const pixels = mobileHeight
        ? (placement.mobileHeight == null ? node.getBoundingClientRect().height : placement.mobileHeight * innerHeight)
        : (placement.floating?.[dimension] || (dimension === 'width' ? 0.42 : 0.45)) * viewportSize;
      input.min = String(min); input.max = String(max);
      input.value = String(Math.round(clamp(pixels, min, max)));
      input.setAttribute('aria-valuetext', `${input.value} pixels`);
      const label = mobileHeight ? 'Panel height' : `Floating ${dimension}`;
      input.previousElementSibling.textContent = label;
      input.setAttribute('aria-label', `${title} ${label.toLowerCase()}`);
    }
  }

  function updateTracks() {
    // Effective geometry is derived; saved expanded ratios never change on hide/collapse.
    for (const [key, area] of Object.entries({ left:'score', right:'right', transport:'transport', bottom:'console' })) {
      const occupant = areaOccupant(area);
      const size = !occupant ? '0px' : occupant.collapsed ? '34px' : `${state.splits[key] * 100}%`;
      cockpit.style.setProperty(`--dock-${key}`, size);
      cockpit.dataset[`dock${key[0].toUpperCase()}${key.slice(1)}`] = !occupant ? 'empty' : occupant.collapsed ? 'collapsed' : 'open';
    }
    const upper = ['score', 'game', 'right'].map(areaOccupant).filter(Boolean);
    cockpit.style.setProperty('--dock-main-track', upper.some(panel => !panel.collapsed) ? '1fr' : upper.length ? '34px' : '0px');
    const center = areaOccupant('game') || areaOccupant('transport');
    cockpit.style.setProperty('--dock-center', center ? '1fr' : '0px');
    // When the center is vacant, let an occupied side consume the spare width.
    cockpit.style.setProperty('--dock-left-track', !center && areaOccupant('score') && !areaOccupant('score').collapsed ? '1fr' : 'var(--dock-left)');
    cockpit.style.setProperty('--dock-right-track', !center && areaOccupant('right') && !areaOccupant('right').collapsed ? '1fr' : 'var(--dock-right)');
  }

  function apply() {
    updateTracks();
    for (const [id, node] of panels) {
      const placement = state.panels[id];
      node.hidden = unavailablePanels.has(id) || !placement.visible;
      node.dataset.collapsed = String(placement.collapsed);
      node.dataset.floating = String(placement.area === 'float');
      node.dataset.dockArea = placement.area;
      node.style.setProperty('--dock-opacity', String(placement.opacity));
      node.style.gridArea = placement.area === 'float' ? 'auto' : placement.area;
      const collapse = node.querySelector(`[data-dock-collapse="${id}"]`);
      if (collapse) {
        collapse.textContent = placement.collapsed ? '+' : '−';
        collapse.setAttribute('aria-label', `${placement.collapsed ? 'Expand' : 'Collapse'} ${panelTitle(id)}`);
        collapse.setAttribute('aria-expanded', String(!placement.collapsed));
      }
      const areaSelect = node.querySelector(`[data-dock-area="${id}"]`);
      if (areaSelect) areaSelect.value = placement.area === 'float' ? 'float' : placement.area;
      if (placement.area !== 'float' && innerWidth <= 760 && Number.isFinite(placement.mobileHeight)) node.style.height = `${Math.round(placement.mobileHeight * innerHeight)}px`;
      else if (placement.area !== 'float') node.style.height = '';
      const opacity = node.querySelector(`[data-dock-opacity="${id}"]`);
      if (opacity) opacity.value = String(placement.opacity);
      if (placement.area === 'float') clampFloat(id);
      else { node.style.left = ''; node.style.top = ''; node.style.width = ''; }
      syncSizeControls(id, node);
      const details = node.matches('details') ? node : node.querySelector(':scope > details');
      if (details && !Object.hasOwn(DEFAULTS.panels, id)) details.open = placement.expanded === true;
    }
    // Preserve focus in the Panels checklist while applying a checkbox change.
    for (const checkbox of panelListBody.querySelectorAll('input[data-panel-id]')) checkbox.checked = state.panels[checkbox.dataset.panelId]?.visible === true;
    refreshSavedLayouts();
    for (const splitter of splitters) splitter.update();
  }

  function refreshSavedLayouts() {
    const selected = activeLayout;
    savedSelect.replaceChildren(new Option('Current layout', 'current'));
    for (const name of Object.keys(presets).sort((a, b) => a.localeCompare(b))) savedSelect.add(new Option(name, name));
    savedSelect.value = Object.hasOwn(presets, selected) ? selected : 'current';
  }

  function storePresets() {
    try { localStorage.setItem(PRESETS_KEY, JSON.stringify({ version: 1, layouts: presets })); }
    catch { announce('Could not save layout presets in browser storage.'); }
  }

  function dockPanel(id, area) {
    if (!panels.has(id) || !AREAS.includes(area)) return;
    const moving = state.panels[id];
    if (moving.area === area) return;
    const origin = moving.area === 'float' ? moving.returnArea : moving.area;
    const occupyingId = [...panels.keys()].find((other) => other !== id && state.panels[other].area === area);
    if (occupyingId) {
      const fallback = AREAS.includes(origin) && origin !== area
        && ![...panels.keys()].some((other) => other !== id && other !== occupyingId && state.panels[other].area === origin);
      if (fallback) state.panels[occupyingId].area = origin;
      else {
        const node = panels.get(occupyingId), rect = node.getBoundingClientRect();
        const placement = state.panels[occupyingId];
        placement.returnArea = placement.area;
        placement.area = 'float';
        placement.floating = {
          x:clamp(rect.left / innerWidth, 0, 0.98), y:clamp(rect.top / innerHeight, 0, 0.98),
          width:clamp(rect.width / innerWidth, 0.18, 1), height:clamp(rect.height / innerHeight, 0.16, 1),
        };
      }
    }
    panels.get(id)?.classList.remove('dock-maximized');
    moving.area = area;
    moving.returnArea = area;
    // Retain the last floating geometry for the next undock.
    activeLayout = 'current';
    apply(); persist();
    announce(`${panelTitle(id)} moved to ${AREA_LABELS[area]}.`);
  }

  function floatPanel(id, rect = null) {
    if (!panels.has(id)) return;
    const moving = state.panels[id];
    let bounds = rect;
    if (!bounds && moving.floating) {
      const saved = moving.floating;
      bounds = { left:saved.x * innerWidth, top:saved.y * innerHeight, width:saved.width * innerWidth, height:saved.height * innerHeight };
    }
    if (!bounds) {
      // First undock starts beneath persistent controls, even when the header
      // wraps. Explicit pointer coordinates and remembered geometry take priority.
      const controlsBottom = Math.max(toolbar.getBoundingClientRect().bottom, top?.getBoundingClientRect().bottom || 0);
      const topPx = clamp(Math.max(controlsBottom + 12, FLOATING_DEFAULT.y * innerHeight), 8, Math.max(8, innerHeight - 188));
      bounds = {
        left:FLOATING_DEFAULT.x * innerWidth,
        top:topPx,
        width:FLOATING_DEFAULT.width * innerWidth,
        height:Math.min(FLOATING_DEFAULT.height * innerHeight, innerHeight - topPx - 8),
      };
    }
    if (moving.area !== 'float') moving.returnArea = moving.area;
    moving.area = 'float';
    moving.floating = {
      x: clamp(bounds.left / innerWidth, 0, 0.98), y: clamp(bounds.top / innerHeight, 0, 0.98),
      width: clamp(bounds.width / innerWidth, 0.18, 1), height: clamp(bounds.height / innerHeight, 0.16, 1),
    };
    activeLayout = 'current'; apply(); persist();
    raiseFloatingPanel(id);
    announce(`${panelTitle(id)} is floating. Drag its title bar back into a dock region to dock it.`);
  }

  function setVisible(id, visible, { focus = true } = {}) {
    if (!panels.has(id) || (visible && unavailablePanels.has(id))) return;
    state.panels[id].visibilityExplicit = true;
    state.panels[id].visible = Boolean(visible);
    if (!visible && panels.get(id).contains(document.activeElement)) {
      panelListSummary.focus();
    }
    activeLayout = 'current'; apply(); persist();
    if (visible && focus) panels.get(id).querySelector('.dock-grip')?.focus();
    activeLayout = 'current';
  }

  function setCollapsed(id, collapsed) {
    if (!panels.has(id)) return;
    const placement = state.panels[id];
    if (collapsed && !placement.collapsed && placement.area === 'float') {
      const rect = panels.get(id).getBoundingClientRect();
      if (rect.width && rect.height) placement.floating = { x:rect.left / innerWidth, y:rect.top / innerHeight, width:rect.width / innerWidth, height:rect.height / innerHeight };
    }
    placement.collapsed = Boolean(collapsed);
    activeLayout = 'current'; apply(); persist();
    if (collapsed) panels.get(id).querySelector('.dock-grip')?.focus();
  }

  function reconcileDockAreas() {
    const occupied = new Set();
    for (const id of panels.keys()) {
      const placement = state.panels[id];
      if (placement.area === 'float') continue;
      if (occupied.has(placement.area)) {
        placement.returnArea = placement.area;
        placement.area = 'float';
      } else occupied.add(placement.area);
    }
  }

  function loadLayout(layout, name) {
    const decoded = decodeState(layout);
    if (!decoded) { announce('That layout is invalid; the current workspace was kept.'); return false; }
    for (const [id, placement] of Object.entries(decoded.panels)) state.panels[id] = clone(placement);
    reconcileDockAreas();
    Object.assign(state.splits, decoded.splits);
    state.layoutIntent = 'explicit';
    activeLayout = name;
    apply(); persist();
    announce(`${name === 'current' ? 'Current workspace' : `Layout “${name}”`} restored.`);
    return true;
  }

  function addPanelChrome(id, node, customTitle = null) {
    if (panelEnvironment.has(node)) {
      panelEnvironmentCleanup.set(id, panelEnvironment.get(node)());
      return;
    }
    node.addEventListener('pointerdown', () => raiseFloatingPanel(id), true);
    node.addEventListener('focusin', () => raiseFloatingPanel(id));
    const accessibleTitle = customTitle || panelTitle(id);
    const head = element('header', 'dock-titlebar');
    const grip = element('button', 'dock-grip', '⠿'); grip.type = 'button';
    grip.setAttribute('aria-haspopup', 'true');
    grip.setAttribute('aria-expanded', 'false');
    grip.setAttribute('aria-label', `Move ${accessibleTitle}. Drag to dock or float; activate for move options.`);
    grip.title = 'Drag to dock or float. Enter opens move options. Alt + arrow keys move floating panels; Shift increases the step.';
    const title = element('span', 'dock-title', accessibleTitle);
    const actions = element('div', 'dock-actions');
    const collapse = element('button', 'btn icon small', '−'); collapse.type = 'button'; collapse.dataset.dockCollapse = id;
    collapse.addEventListener('click', () => setCollapsed(id, !state.panels[id].collapsed));
    const settings = element('details', 'dock-settings');
    const summary = element('summary', 'btn icon small'); summary.textContent = '⋯'; summary.setAttribute('aria-label', `${accessibleTitle} settings`);
    const settingsBody = element('div', 'dock-settings-body');
    settingsBody.id = `dock-settings-${id}`;
    grip.setAttribute('aria-controls', settingsBody.id);
    summary.setAttribute('aria-controls', settingsBody.id);
    // The top layer escapes scroll containers without moving the panel or game iframe.
    const hasPopover = typeof settingsBody.showPopover === 'function';
    if (hasPopover) settingsBody.setAttribute('popover', 'auto');
    const positionSettings = () => {
      if (!settings.open) return;
      const anchor = summary.getBoundingClientRect();
      const bounds = settingsBody.getBoundingClientRect();
      settingsBody.style.left = `${Math.max(8, Math.min(anchor.right - bounds.width, innerWidth - bounds.width - 8))}px`;
      settingsBody.style.top = `${Math.max(8, Math.min(anchor.bottom + 4, innerHeight - bounds.height - 8))}px`;
    };
    const closeSettings = (restoreFocus = false) => {
      settings.open = false;
      if (hasPopover && settingsBody.matches(':popover-open')) settingsBody.hidePopover();
      if (restoreFocus) grip.focus();
    };
    settingsBody.addEventListener('keydown', (event) => {
      if (event.key !== 'Escape') return;
      event.preventDefault(); event.stopPropagation(); closeSettings(true);
    });
    settingsBody.addEventListener('toggle', (event) => {
      if (event.target === settingsBody && event.newState === 'closed') settings.open = false;
    });
    const attachEnvironment = () => {
      const controller = new AbortController();
      const signal = controller.signal;
      document.addEventListener('pointerdown', (event) => {
        if (settings.open && !settings.contains(event.target) && event.target !== grip) closeSettings();
      }, { signal });
      window.addEventListener('resize', positionSettings, { signal });
      document.addEventListener('scroll', positionSettings, { capture: true, signal });
      return () => { closeSettings(); controller.abort(); };
    };
    panelEnvironment.set(node, attachEnvironment);
    panelEnvironmentCleanup.set(id, attachEnvironment());
    const areaLabel = element('label', 'dock-setting'); areaLabel.append(element('span', '', 'Dock to'));
    const areaSelect = element('select'); areaSelect.dataset.dockArea = id;
    for (const area of AREAS) areaSelect.add(new Option(AREA_LABELS[area], area));
    areaSelect.add(new Option('Floating window', 'float'));
    areaSelect.addEventListener('change', () => {
      if (areaSelect.value === 'float') floatPanel(id);
      else dockPanel(id, areaSelect.value);
      closeSettings(true);
    });
    areaLabel.append(areaSelect);
    const opacityLabel = element('label', 'dock-setting'); opacityLabel.append(element('span', '', 'Backdrop opacity'));
    const opacityInput = element('input'); opacityInput.type = 'range'; opacityInput.min = '0.55'; opacityInput.max = '1'; opacityInput.step = '0.05'; opacityInput.value = '1'; opacityInput.dataset.dockOpacity = id;
    opacityInput.setAttribute('aria-label', `${panelTitle(id)} backdrop opacity`);
    opacityInput.addEventListener('input', () => { state.panels[id].opacity = Number(opacityInput.value); node.style.setProperty('--dock-opacity', opacityInput.value); activeLayout = 'current'; persist(); });
    opacityLabel.append(opacityInput);
    const floatWidth = element('label', 'dock-setting'); floatWidth.append(element('span', '', 'Floating width'));
    const widthInput = element('input'); widthInput.type = 'range'; widthInput.min = '260'; widthInput.max = String(Math.max(260, innerWidth - 16)); widthInput.step = '20'; widthInput.dataset.dockSize = id; widthInput.dataset.dimension = 'width';
    widthInput.setAttribute('aria-label', `${panelTitle(id)} floating width`);
    widthInput.addEventListener('input', () => {
      const placement = state.panels[id]; placement.floating ||= { x:0.2, y:0.15, width:0.42, height:0.45 };
      placement.floating.width = Number(widthInput.value) / innerWidth; activeLayout = 'current'; apply(); persist();
    });
    floatWidth.append(widthInput);
    const floatHeight = element('label', 'dock-setting'); floatHeight.append(element('span', '', 'Floating height'));
    const heightInput = element('input'); heightInput.type = 'range'; heightInput.min = '180'; heightInput.max = String(Math.max(180, innerHeight - 16)); heightInput.step = '20'; heightInput.dataset.dockSize = id; heightInput.dataset.dimension = 'height';
    heightInput.setAttribute('aria-label', `${panelTitle(id)} floating height`);
    heightInput.addEventListener('input', () => {
      const placement = state.panels[id];
      if (placement.area === 'float') {
        placement.floating ||= { x:0.2, y:0.15, width:0.42, height:0.45 };
        placement.floating.height = Number(heightInput.value) / innerHeight;
      } else if (innerWidth <= 760) {
        placement.mobileHeight = clamp(Number(heightInput.value) / innerHeight, 0.18, 0.8);
      } else return;
      activeLayout = 'current'; apply(); persist();
    });
    floatHeight.append(heightInput);
    const hide = element('button', 'btn small dock-hide', 'Hide panel'); hide.type = 'button';
    hide.addEventListener('click', () => { settings.open = false; setVisible(id, false); });
    settingsBody.append(areaLabel, opacityLabel, floatWidth, floatHeight, hide);
    settings.append(summary, settingsBody);
    actions.append(collapse, settings);
    head.append(grip, title, actions);
    node.insertBefore(head, node.firstChild);
    grip.addEventListener('click', () => {
      if (grip.dataset.dragged === 'true') { delete grip.dataset.dragged; return; }
      settings.open = true;
      grip.setAttribute('aria-expanded', 'true');
      // Focus after the toggle event exposes the top-layer controls.
    });
    settings.addEventListener('toggle', () => {
      grip.setAttribute('aria-expanded', String(settings.open));
      if (settings.open) {
        for (const other of cockpit.querySelectorAll('.dock-settings[open]')) if (other !== settings) other.open = false;
        if (hasPopover && !settingsBody.matches(':popover-open')) settingsBody.showPopover();
        positionSettings();
        if (document.activeElement === grip) areaSelect.focus();
      } else if (hasPopover && settingsBody.matches(':popover-open')) settingsBody.hidePopover();
    });
    grip.addEventListener('keydown', (event) => {
      const offsets = { ArrowLeft:[-1, 0], ArrowRight:[1, 0], ArrowUp:[0, -1], ArrowDown:[0, 1] };
      if (!event.altKey || !offsets[event.key] || state.panels[id].area !== 'float') return;
      event.preventDefault();
      const step = event.shiftKey ? 40 : 10;
      const placement = state.panels[id];
      placement.floating.x += offsets[event.key][0] * step / innerWidth;
      placement.floating.y += offsets[event.key][1] * step / innerHeight;
      clampFloat(id); activeLayout = 'current'; persist();
    });
    installPanelDrag(id, node, grip, head);
  }

  function installPanelDrag(id, node, grip, head) {
    let drag = null;
    head.addEventListener('pointerdown', (event) => {
      if (event.button !== 0 || (event.target.closest('button, select, input, summary, details, a') && event.target !== grip)) return;
      drag = { pointerId:event.pointerId, x:event.clientX, y:event.clientY, moved:false, rect:node.getBoundingClientRect(), capture:event.target === grip ? grip : head };
      drag.capture.setPointerCapture(event.pointerId);
    });
    head.addEventListener('pointermove', (event) => {
      if (!drag || drag.pointerId !== event.pointerId) return;
      if (!drag.moved && Math.hypot(event.clientX - drag.x, event.clientY - drag.y) < 7) return;
      if (!drag.moved) {
        drag.moved = true; grip.dataset.dragged = 'true';
        document.body.classList.add('dock-dragging');
        node.classList.add('dock-moving');
        drag.pointerEvents = node.style.pointerEvents;
        // The moving floating surface must not mask the dock target under it.
        node.style.pointerEvents = 'none';
      }
      if (state.panels[id].area === 'float') {
        node.style.left = `${clamp(drag.rect.left + event.clientX - drag.x, 8, innerWidth - drag.rect.width - 8)}px`;
        node.style.top = `${clamp(drag.rect.top + event.clientY - drag.y, 8, innerHeight - drag.rect.height - 8)}px`;
      }
      const target = document.elementFromPoint(event.clientX, event.clientY)?.closest('[data-dock-id]');
      document.querySelectorAll('.dock-drop-target').forEach((candidate) => candidate.classList.remove('dock-drop-target'));
      if (target && target !== node && cockpit.contains(target)) target.classList.add('dock-drop-target');
    });
    const clearDrag = (cancelled = false) => {
      if (!drag) return;
      const current = drag;
      drag = null;
      if (current.moved) {
        node.style.pointerEvents = current.pointerEvents || '';
        if (cancelled && state.panels[id].area === 'float') {
          node.style.left = `${current.rect.left}px`;
          node.style.top = `${current.rect.top}px`;
        }
      }
      node.classList.remove('dock-moving');
      document.body.classList.remove('dock-dragging');
      document.querySelectorAll('.dock-drop-target').forEach((candidate) => candidate.classList.remove('dock-drop-target'));
      if (current.capture.hasPointerCapture(current.pointerId)) current.capture.releasePointerCapture(current.pointerId);
      // Suppress the click generated by this pointerup, but allow the next click.
      setTimeout(() => { delete grip.dataset.dragged; }, 0);
    };
    head.addEventListener('pointerup', (event) => {
      if (!drag || drag.pointerId !== event.pointerId) return;
      const current = drag;
      // Resolve the destination while the moving panel is excluded from hit testing.
      const target = current.moved && document.elementFromPoint(event.clientX, event.clientY)?.closest('[data-dock-id]');
      clearDrag();
      if (!current.moved) return;
      if (target && target !== node && cockpit.contains(target)) {
        const destination = state.panels[target.dataset.dockId];
        dockPanel(id, destination.area === 'float' ? destination.returnArea : destination.area);
      } else {
        // Empty workspace is a valid undock destination. Preserve the grab offset
        // instead of snapping the panel center to the pointer when it is released.
        floatPanel(id, {
          left:current.rect.left + event.clientX - current.x,
          top:current.rect.top + event.clientY - current.y,
          width:current.rect.width,
          height:state.panels[id].collapsed ? (state.panels[id].floating?.height || FLOATING_DEFAULT.height) * innerHeight : current.rect.height,
        });
      }
    });
    head.addEventListener('pointercancel', () => clearDrag(true));
    head.addEventListener('lostpointercapture', () => clearDrag(true));
    head.addEventListener('keydown', (event) => {
      if (event.key !== 'Escape' || !drag) return;
      event.preventDefault(); event.stopPropagation(); clearDrag(true);
    });
  }

  function addSplitters() {
    const specs = [
      { key:'left', orientation:'vertical', label:'Resize left dock region', start:0.16, end:0.42 },
      { key:'right', orientation:'vertical', label:'Resize right dock region', start:0.16, end:0.42 },
      { key:'transport', orientation:'horizontal', label:'Resize transport dock region', start:0.08, end:0.34 },
      { key:'bottom', orientation:'horizontal', label:'Resize bottom dock region', start:0.1, end:0.48 },
    ];
    return specs.map((spec) => {
      const splitter = element('div', `dock-splitter dock-splitter-${spec.orientation}`);
      splitter.dataset.dockSplit = spec.key;
      splitter.tabIndex = 0; splitter.setAttribute('role', 'separator');
      splitter.setAttribute('aria-orientation', spec.orientation); splitter.setAttribute('aria-label', spec.label);
      splitter.setAttribute('aria-valuemin', String(Math.round(spec.start * 100))); splitter.setAttribute('aria-valuemax', String(Math.round(spec.end * 100)));
      cockpit.append(splitter);
      let pointer = null;
      splitter.addEventListener('pointerdown', (event) => {
        if (event.button !== 0) return;
        pointer = { id:event.pointerId, rect:cockpit.getBoundingClientRect() };
        splitter.setPointerCapture(event.pointerId);
        document.body.classList.add('dock-resizing');
      });
      splitter.addEventListener('pointermove', (event) => {
        if (!pointer || pointer.id !== event.pointerId) return;
        const size = spec.orientation === 'vertical' ? pointer.rect.width : pointer.rect.height;
        let position = spec.orientation === 'vertical' ? event.clientX - pointer.rect.left : pointer.rect.bottom - event.clientY;
        if (spec.key === 'right') position = pointer.rect.right - event.clientX;
        if (spec.key === 'transport') {
          const bottom = areaOccupant('console');
          position -= !bottom ? 0 : bottom.collapsed ? 34 : state.splits.bottom * size;
        }
        const ratio = position / size;
        state.splits[spec.key] = clamp(ratio, spec.start, spec.end);
        updateTracks();
        for (const handle of splitters) handle.update();
        splitter.setAttribute('aria-valuenow', String(Math.round(state.splits[spec.key] * 100)));
      });
      const finish = () => { if (!pointer) return; pointer = null; document.body.classList.remove('dock-resizing'); persist(); activeLayout = 'current'; };
      splitter.addEventListener('pointerup', finish); splitter.addEventListener('pointercancel', finish);
      splitter.addEventListener('lostpointercapture', finish);
      splitter.addEventListener('keydown', (event) => {
        const direction = spec.orientation === 'vertical'
          ? (event.key === 'ArrowRight' ? 1 : event.key === 'ArrowLeft' ? -1 : 0)
          : (event.key === 'ArrowUp' ? 1 : event.key === 'ArrowDown' ? -1 : 0);
        if (!direction && event.key !== 'Home' && event.key !== 'End') return;
        event.preventDefault();
        const scale = event.shiftKey ? 0.025 : 0.01;
        state.splits[spec.key] = event.key === 'Home' ? spec.start : event.key === 'End' ? spec.end
          : clamp(state.splits[spec.key] + direction * scale * (spec.key === 'right' ? -1 : 1), spec.start, spec.end);
        activeLayout = 'current'; apply(); persist();
      });
      splitter.update = () => {
        const area = ({ left:'score', right:'right', transport:'transport', bottom:'console' })[spec.key];
        const occupant = areaOccupant(area);
        splitter.hidden = innerWidth <= 760 || !occupant || occupant.collapsed
          || (spec.orientation === 'vertical' && !areaOccupant('game') && !areaOccupant('transport'));
        splitter.setAttribute('aria-valuenow', String(Math.round(state.splits[spec.key] * 100)));
        if (spec.key === 'transport' && !splitter.hidden) {
          const id = [...panels.keys()].find((key) => state.panels[key].visible && state.panels[key].area === 'transport');
          const bounds = panels.get(id)?.getBoundingClientRect();
          const workspace = cockpit.getBoundingClientRect();
          if (bounds) {
            const left = bounds.left - workspace.left - cockpit.clientLeft;
            splitter.style.left = `${left}px`;
            splitter.style.right = `${Math.max(0, cockpit.clientWidth - left - bounds.width)}px`;
          }
        }
      };
      splitter.update();
      return splitter;
    });
  }

  function resetLayout() {
    for (const id of Object.keys(state.panels)) Object.assign(state.panels[id], clone(panelDefaults.get(id) || { area:'float', returnArea:'right', visible:false, collapsed:false, opacity:1, floating:null }));
    for (const placement of Object.values(state.panels)) placement.visibilityExplicit = false;
    state.layoutIntent = 'migrated-default';
    reconcileDockAreas();
    Object.assign(state.splits, clone(DEFAULTS.splits));
    activeLayout = 'current'; savedSelect.value = 'current'; apply(); persist();
    announce('Default workspace layout restored.');
  }

  saveLayout.addEventListener('click', () => {
    const name = nameInput.value.trim().slice(0, 32);
    if (!name) { nameInput.focus(); announce('Enter a name for this layout.'); return; }
    state.layoutIntent = 'explicit';
    presets[name] = clone(state);
    const names = Object.keys(presets);
    while (names.length > 20) delete presets[names.shift()];
    storePresets(); refreshSavedLayouts(); savedSelect.value = name; activeLayout = name;
    announce(`Layout “${name}” saved.`);
  });
  savedSelect.addEventListener('change', () => {
    if (savedSelect.value === 'current') { activeLayout = 'current'; return; }
    loadLayout(presets[savedSelect.value], savedSelect.value);
  });
  reset.addEventListener('click', resetLayout);
  const presentationRequest = (action, overlay) => {
    const detail = { action, overlay, applied:false };
    window.dispatchEvent(new CustomEvent('telos:shared-presentation', {detail}));
    if (!detail.applied || detail.error) throw new Error(detail.error || 'Presentation controls are not ready.');
    return detail.overlay;
  };
  exportShared.addEventListener('click', () => {
    let url;
    try {
      const profile = validateSharedProfile({ format:SHARED_PRESENTATION_FORMAT,
        presentation:presentationRequest('read') ? 'overlay' : 'surround',
        panels:{game:{visible:state.panels.game.visible, opacity:state.panels.game.opacity, collapsed:state.panels.game.collapsed},timeline:{opacity:state.panels.transport?.opacity ?? 1,density:preferredTimelineDensity()}},
        hostProfiles:{...retainedHostProfiles(),browser:clone(state)},
      });
      url = URL.createObjectURL(new Blob([JSON.stringify(profile,null,2)], {type:'application/json'}));
      const link = element('a'); link.href = url; link.download = 'telos-presentation.json';
      link.hidden = true; toolbar.append(link); link.click(); link.remove();
      const downloadedUrl = url; setTimeout(() => URL.revokeObjectURL(downloadedUrl),1000);
      announce('Shared presentation exported.');
    } catch (error) { if (url) URL.revokeObjectURL(url); announce(`Profile export failed: ${error.message}`); }
  });
  exportLayout.addEventListener('click', () => {
    const blob = new Blob([JSON.stringify({ format:'telos-layout/v1', state }, null, 2)], { type:'application/json' });
    const url = URL.createObjectURL(blob);
    const link = element('a'); link.href = url; link.download = 'telos-layout.json'; link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    announce('Layout profile exported.');
  });
  importLayout.addEventListener('click', () => importFile.click());
  let sharedImportRevision = 0;
  importFile.addEventListener('change', async () => {
    const ticket = ++sharedImportRevision;
    const file = importFile.files?.[0]; importFile.value = '';
    if (!file) return;
    if (file.size > 65536) { announce('Layout profile exceeds the 64 KiB limit.'); return; }
    try {
      const source = await file.text();
      if (ticket !== sharedImportRevision) return;
      const document = JSON.parse(source);
      if ([SHARED_PRESENTATION_FORMAT, PREVIOUS_SHARED_PRESENTATION_FORMAT, GAME_ONLY_SHARED_PRESENTATION_FORMAT, LEGACY_SHARED_PRESENTATION_FORMAT].includes(document?.format)) {
        const profile = validateSharedProfile(document);
        const previousState = clone(state);
        const previousOverlay = presentationRequest('read');
        const previousStored = localStorage.getItem(SHARED_PROFILE_STORAGE);
        const previousDensity = preferredTimelineDensity();
        const previousDensityStored = localStorage.getItem(TIMELINE_DENSITY_KEY);
        const candidate = clone(state);
        Object.assign(candidate.panels.game,profile.panels.game);
        if (profile.panels.timeline && candidate.panels.transport) candidate.panels.transport.opacity = profile.panels.timeline.opacity;
        if (!decodeState(candidate)) throw new Error('The current dock geometry cannot accept this profile.');
        try {
          presentationRequest('apply',profile.presentation==='overlay');
          if (!loadLayout(candidate,'current')) throw new Error('Layout owner refused the profile.');
          if (profile.format === SHARED_PRESENTATION_FORMAT) requestTimelineDensity(profile.panels.timeline.density);
          localStorage.setItem(SHARED_PROFILE_STORAGE,JSON.stringify(profile));
        } catch (error) {
          const failures = [];
          const rollback = fn => { try { fn(); } catch (cause) { failures.push(cause.message); } };
          rollback(() => requestTimelineDensity(previousDensity,{persist:false}));
          // Restore dock visibility last: density may reveal/hide its editor,
          // while the original dock state is the final geometry authority.
          rollback(() => { if (!loadLayout(previousState,'current')) throw new Error('Dock rollback was refused.'); });
          rollback(() => presentationRequest('apply',previousOverlay));
          rollback(() => {
            if (previousDensityStored === null) localStorage.removeItem(TIMELINE_DENSITY_KEY);
            else localStorage.setItem(TIMELINE_DENSITY_KEY,previousDensityStored);
          });
          rollback(() => {
            if (previousStored === null) localStorage.removeItem(SHARED_PROFILE_STORAGE);
            else localStorage.setItem(SHARED_PROFILE_STORAGE,previousStored);
          });
          if (failures.length) throw new Error(`${error.message} Rollback could not restore every preference: ${failures.join('; ')}`);
          throw error;
        }
        announce('Shared presentation imported; panel geometry retained.');
        return;
      }
      if (typeof document?.format === 'string' && document.format.startsWith('telos-shared-presentation/')) throw new Error('Unsupported shared presentation profile version.');
      const candidate = decodeState(document?.format === 'telos-layout/v1' ? document.state : document);
      if (!candidate) throw new Error('The profile schema or panel geometry is invalid.');
      loadLayout(candidate, 'current');
      announce('Layout profile imported.');
    } catch (error) { announce(`Layout import failed: ${error.message}`); }
  });

  window.addEventListener('resize', () => {
    for (const [id, placement] of Object.entries(state.panels)) {
      if (placement.area === 'float') clampFloat(id);
      else if (panels.has(id)) panels.get(id).style.height = innerWidth <= 760 && Number.isFinite(placement.mobileHeight) ? `${Math.round(placement.mobileHeight * innerHeight)}px` : '';
    }
    for (const splitter of splitters) splitter.update();
  });
  function observeFloatingSize(id, node) {
    let lastWidth = -1, lastHeight = -1;
    const observer = new ResizeObserver(() => {
      const rect = node.getBoundingClientRect();
      if (node.hidden) return;
      if (id === 'transport') cockpit.style.setProperty('--dock-overlay-transport-height', `${Math.round(rect.height)}px`);
      if (Math.abs(rect.width - lastWidth) > 1 || Math.abs(rect.height - lastHeight) > 1) {
        lastWidth = rect.width; lastHeight = rect.height;
        if (!renderRefreshFrame) renderRefreshFrame = requestAnimationFrame(() => {
          renderRefreshFrame = 0;
          window.dispatchEvent(new Event('resize'));
        });
      }
      // Overlay positions are temporary; preserve surrounding workspace geometry.
      if (document.body.dataset.tasPresentation === 'overlay') return;
      const placement = state.panels[id];
      // A collapsed titlebar must never replace the expanded resize memory.
      if (placement?.collapsed) return;
      if (placement?.area !== 'float') {
        if (innerWidth <= 760 && placement) {
          placement.mobileHeight = clamp(rect.height / innerHeight, 0.18, 0.8);
          syncSizeControls(id, node);
          persist();
        }
        return;
      }
      const width = clamp(rect.width, Math.min(260, innerWidth - 16), Math.max(1, innerWidth - 16));
      const height = clamp(rect.height, Math.min(180, innerHeight - 16), Math.max(1, innerHeight - 16));
      const left = clamp(rect.left, 8, innerWidth - width - 8);
      const topPx = clamp(rect.top, 8, innerHeight - height - 8);
      state.panels[id].floating = {
        x:left / innerWidth, y:topPx / innerHeight,
        width:width / innerWidth, height:height / innerHeight,
      };
      // Keep the accessible size controls synchronized after using the
      // browser's native resize grip.
      syncSizeControls(id, node);
      persist();
    });
    sizeObservers.get(id)?.disconnect();
    sizeObservers.set(id, observer);
    observer.observe(node);
  }
  for (const [id, node] of panels) {
    node.addEventListener('toggle', () => persist());
    observeFloatingSize(id, node);
  }

  updatePanelList(); apply();
  window.TelosDock = Object.freeze({
    version:1,
    register(id, title, node, { visible = false, area = 'float' } = {}) {
      if (!(node instanceof HTMLElement) || panels.has(id) || !/^[a-z][a-z0-9-]{0,47}$/.test(id)) return false;
      let safeArea = AREAS.includes(area) ? area : 'float';
      if (safeArea !== 'float' && [...panels.keys()].some((key) => state.panels[key].area === safeArea)) safeArea = 'float';
      panelDefaults.set(id, { area:safeArea, returnArea:'right', visible:Boolean(visible), visibilityExplicit:false, collapsed:false, expanded:false, mobileHeight:null, opacity:1, floating:null });
      const hadSavedPlacement = Object.hasOwn(state.panels, id);
      state.panels[id] ||= clone(panelDefaults.get(id));
      // <details> only renders its first <summary> when closed. Put it inside
      // a neutral dock shell so the dock titlebar remains available whether
      // the inspector itself is open or collapsed.
      let panelNode = node;
      if (node instanceof HTMLDetailsElement) {
        panelNode = element('section', 'dock-widget-shell');
        node.removeAttribute('data-telos-dock-widget');
        panelNode.dataset.telosDockWidget = id;
        panelNode.dataset.telosDockTitle = title;
        panelDefaults.get(id).expanded = node.open;
        if (hadSavedPlacement) node.open = state.panels[id].expanded === true;
        else state.panels[id].expanded = node.open;
        node.parentElement?.insertBefore(panelNode, node);
        panelNode.append(node);
      }
      panels.set(id, panelNode); panelNode.classList.add('dock-panel'); panelNode.dataset.dockId = id;
      reconcileDockAreas();
      addPanelChrome(id, panelNode, title);
      cockpit.append(panelNode);
      if (!toggleBound.has(node)) {
      toggleBound.add(node); node.addEventListener('toggle', (event) => {
        if (!(node instanceof HTMLDetailsElement) || event.target !== node) return;
        state.panels[id].expanded = node.open;
        persist();
      });
      }
      observeFloatingSize(id, panelNode);
      updatePanelList(); apply(); persist();
      return true;
    },
    unregister(id) {
      const node = panels.get(id);
      if (!node || id === 'game') return null;
      if (node.contains(document.activeElement)) panelListSummary.focus();
      const saved = { id, node, title:node.querySelector('.dock-title')?.textContent || id,
        defaults:clone(panelDefaults.get(id)) };
      panelEnvironmentCleanup.get(id)?.(); panelEnvironmentCleanup.delete(id);
      sizeObservers.get(id)?.disconnect(); sizeObservers.delete(id);
      panels.delete(id); unavailablePanels.delete(id);
      const order = floatingOrder.indexOf(id); if (order !== -1) floatingOrder.splice(order, 1);
      node.remove();
      updatePanelList(); apply();
      // Retain user geometry and saved presets for a later package re-enable.
      return saved;
    },
    migrateLegacyDefaults(ids) {
      if (state.layoutIntent) return;
      const legacyAreas = ['score', 'game', 'right', 'transport', 'console'];
      const stock = legacyAreas.every(id => {
        const p = state.panels[id];
        return p && p.area === id && p.visible && !p.collapsed && !p.floating && p.opacity === 1;
      }) && Object.entries(DEFAULTS.splits).every(([key, value]) => state.splits[key] === value);
      const explicitlySaved = Object.values(presets).some(preset =>
        legacyAreas.every(id => JSON.stringify(preset.panels[id]) === JSON.stringify(state.panels[id]))
        && JSON.stringify(preset.splits) === JSON.stringify(state.splits));
      if (!stock || explicitlySaved) return;
      for (const id of ids) if (state.panels[id] && !state.panels[id].visibilityExplicit) state.panels[id].visible = false;
      state.layoutIntent = 'migrated-default';
      apply(); persist();
    },
    setAvailable(ids, available) {
      for (const id of ids) {
        if (available) unavailablePanels.delete(id);
        else unavailablePanels.add(id);
      }
      updatePanelList(); apply();
      // Do not persist: enabling an extension restores its prior layout.
    },
    show(id) { setVisible(id, true); },
    reveal(id) {
      if (!panels.has(id) || unavailablePanels.has(id)) return;
      state.panels[id].collapsed = false;
      setVisible(id, true);
    },
    hide(id) { setVisible(id, false); },
    snapshot() { return { state:clone(state), presets:clone(presets), activeLayout }; },
    restore(snapshot) {
      const decoded = decodeState(snapshot?.state);
      if (!decoded) return false;
      for (const [id, placement] of Object.entries(decoded.panels)) state.panels[id] = clone(placement);
      reconcileDockAreas();
      activeLayout = Object.hasOwn(presets, snapshot.activeLayout) ? snapshot.activeLayout : 'current';
      Object.assign(state.splits, decoded.splits); apply(); persist(); return true;
    },
  });
  const registerDeclaredPanels = () => {
    for (const node of document.querySelectorAll('[data-telos-dock-widget]')) {
      const id = node.dataset.telosDockWidget;
      if (id && !panels.has(id)) window.TelosDock.register(id, node.dataset.telosDockTitle || id, node, { visible:node.dataset.telosDockVisible === 'true', area:node.dataset.telosDockArea || 'float' });
    }
  };
  registerDeclaredPanels();
  const declaredPanelsObserver = new MutationObserver(registerDeclaredPanels);
  declaredPanelsObserver.observe(document.body, { childList:true, subtree:true });
  window.dispatchEvent(new CustomEvent('telos:dock-ready'));
  return window.TelosDock;
}
