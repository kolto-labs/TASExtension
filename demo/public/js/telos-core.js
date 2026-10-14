// Small package host for Telos Core. Packages register trusted in-tree
// lifecycle modules; catalog metadata alone can never execute code.
export const CORE_API = Object.freeze({ id: 'telos.core.extension', major: 1, minor: 0 });
const STORAGE_KEY = 'telos.core.packages.v2';
const ID = /^[a-z][a-z0-9.-]{0,79}$/;
const VERSION = /^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-[0-9A-Za-z.-]+)?$/;
const ACTIONS = new Set(['install', 'enable', 'disable', 'remove']);

function fail(code, detail = '') {
  const error = new Error(detail ? `${code}: ${detail}` : code);
  error.code = code;
  throw error;
}

function checkedManifest(value) {
  if (!value || value.schema !== 'telos.extension/v1' || !ID.test(value.id || '')
    || typeof value.name !== 'string' || !value.name.trim() || value.name.length > 100
    || !VERSION.test(value.version || '') || typeof value.description !== 'string'
    || !value.description.trim() || value.description.length > 500
    || !value.api || value.api.id !== CORE_API.id || value.api.major !== CORE_API.major
    || !Number.isSafeInteger(value.api.minor) || value.api.minor < 0 || value.api.minor > CORE_API.minor) {
    fail('CORE_MANIFEST_INVALID', 'expected a compatible telos.extension/v1 manifest');
  }
  const requires = value.requires ?? [];
  const permissions = value.permissions ?? [];
  const provides = value.provides ?? [];
  if (!Array.isArray(requires) || requires.some((id) => !ID.test(id) || id === value.id)
    || new Set(requires).size !== requires.length
    || !Array.isArray(permissions) || permissions.some((name) => typeof name !== 'string' || !/^[a-z][a-z0-9.-]{0,79}$/.test(name))
    || new Set(permissions).size !== permissions.length
    || !Array.isArray(provides) || provides.some((name) => typeof name !== 'string' || !/^[a-z][a-z0-9.-]{0,79}$/.test(name))
    || new Set(provides).size !== provides.length
    || (value.defaultState != null && (typeof value.defaultState.installed !== 'boolean' || typeof value.defaultState.enabled !== 'boolean'))) {
    fail('CORE_MANIFEST_INVALID', 'invalid dependencies, permissions, or package features');
  }
  return Object.freeze({
    ...value,
    api: Object.freeze({ ...value.api }),
    requires: Object.freeze([...requires]),
    permissions: Object.freeze([...permissions]),
    provides: Object.freeze([...provides]),
    defaultState: value.defaultState ? Object.freeze({
      installed: value.defaultState.installed,
      enabled: value.defaultState.installed && value.defaultState.enabled,
    }) : null,
    source: 'bundled',
  });
}

export class ExtensionHost extends EventTarget {
  constructor({ services = {} } = {}) {
    super();
    this.catalog = new Map();
    this.active = new Map();
    this.errors = new Map();
    this.cleanupPending = new Set();
    this.changing = new Set();
    this.services = new Map(Object.entries(services));
    this.saved = {};
    this.storage = this.#load();
  }

  #load() {
    try {
      const raw = JSON.parse(localStorage.getItem(STORAGE_KEY) || 'null');
      if (raw?.version === 2 && raw.packages && typeof raw.packages === 'object' && !Array.isArray(raw.packages)) {
        this.saved = this.#validSaved(raw.packages);
        return true;
      }
      // Preserve the earlier package state when upgrading the workspace shell.
      const old = JSON.parse(localStorage.getItem('telos.extensions.v1') || 'null');
      if (old?.version === 1 && old.packages && typeof old.packages === 'object' && !Array.isArray(old.packages)) {
        this.saved = this.#validSaved(old.packages);
        return this.#persist();
      }
    } catch { /* Storage can be disabled; the in-memory core remains usable. */ }
    return true;
  }

  #validSaved(packages) {
    return Object.fromEntries(Object.entries(packages)
      .filter(([id, item]) => ID.test(id) && item && typeof item === 'object'
        && typeof item.installed === 'boolean' && typeof item.enabled === 'boolean')
      .map(([id, item]) => [id, { installed: item.installed, enabled: item.installed && item.enabled }]));
  }

  #persist() {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify({ version: 2, packages: this.saved }));
      this.storage = true;
      return true;
    } catch {
      this.storage = false;
      return false;
    }
  }

  provide(id, service) {
    if (!ID.test(id) || this.services.has(id) || service == null) fail('CORE_SERVICE_INVALID', id);
    this.services.set(id, service);
    return () => { if (this.services.get(id) === service) this.services.delete(id); };
  }

  register(manifestValue, lifecycle, { restore = true } = {}) {
    const manifest = checkedManifest(manifestValue);
    if (this.catalog.has(manifest.id) || typeof lifecycle?.activate !== 'function'
      || typeof lifecycle?.deactivate !== 'function') fail('CORE_PACKAGE_DUPLICATE_OR_INVALID', manifest.id);
    this.catalog.set(manifest.id, { manifest, lifecycle, dispose: null });
    if (!this.saved[manifest.id] && manifest.defaultState) {
      this.saved[manifest.id] = {
        installed: manifest.defaultState.installed === true,
        enabled: manifest.defaultState.installed === true && manifest.defaultState.enabled === true,
      };
      this.#persist();
    }
    if (restore && this.saved[manifest.id]?.installed && this.saved[manifest.id]?.enabled) {
      // Restore through the same dependency transaction as an explicit enable.
      // A saved dependent must never activate with missing or disabled services.
      try { this.change(manifest.id, 'enable'); }
      catch (error) {
        this.saved[manifest.id] = { installed: true, enabled: false };
        this.#persist();
        this.errors.set(manifest.id, error?.message || String(error));
      }
    }
    return manifest;
  }

  #closure(id, visiting = new Set(), done = new Set()) {
    const item = this.catalog.get(id);
    if (!item) fail('CORE_PACKAGE_UNKNOWN', id);
    if (visiting.has(id)) fail('CORE_DEPENDENCY_CYCLE', id);
    if (done.has(id)) return [];
    visiting.add(id);
    const ordered = [];
    for (const dependency of item.manifest.requires) ordered.push(...this.#closure(dependency, visiting, done));
    visiting.delete(id);
    done.add(id);
    ordered.push(id);
    return ordered;
  }

  #context(id) {
    const manifest = this.catalog.get(id).manifest;
    const permitted = new Set(manifest.permissions);
    const host = this;
    return Object.freeze({
      core: CORE_API,
      package: Object.freeze({ id, version: manifest.version }),
      service(name) {
        if (!permitted.has(name)) fail('CORE_CAPABILITY_NOT_DECLARED', `${id} cannot use ${name}`);
        const value = host.services.get(name);
        if (value == null) fail('CORE_SERVICE_UNAVAILABLE', name);
        return value;
      },
      storage: Object.freeze({
        get(key) { return host.#readPackageValue(id, key); },
        set(key, value) { return host.#writePackageValue(id, key, value); },
        remove(key) { return host.#removePackageValue(id, key); },
      }),
    });
  }

  #storageKey(id, key) {
    if (typeof key !== 'string' || !/^[a-zA-Z0-9._-]{1,80}$/.test(key)) fail('CORE_STORAGE_KEY_INVALID');
    return `telos.package.${id}.${key}`;
  }
  #readPackageValue(id, key) {
    try { const raw = localStorage.getItem(this.#storageKey(id, key)); return raw == null ? null : JSON.parse(raw); }
    catch (error) { if (error?.code) throw error; fail('CORE_STORAGE_UNAVAILABLE'); }
  }
  #writePackageValue(id, key, value) {
    let raw;
    try { raw = JSON.stringify(value); } catch { fail('CORE_STORAGE_VALUE_INVALID'); }
    if (typeof raw !== 'string' || raw.length > 65536) fail('CORE_STORAGE_VALUE_TOO_LARGE');
    try { localStorage.setItem(this.#storageKey(id, key), raw); }
    catch { fail('CORE_STORAGE_UNAVAILABLE'); }
  }
  #removePackageValue(id, key) {
    try { localStorage.removeItem(this.#storageKey(id, key)); }
    catch { fail('CORE_STORAGE_UNAVAILABLE'); }
  }

  #activate(id) {
    const item = this.catalog.get(id);
    if (this.cleanupPending.has(id)) fail('CORE_CLEANUP_RETRY_REQUIRED', `Disable ${item.manifest.name} to finish cleanup, then enable it again.`);
    for (const dependency of item.manifest.requires) {
      if (!this.enabled(dependency)) fail('CORE_DEPENDENCY_DISABLED', `Enable ${this.catalog.get(dependency)?.manifest.name || dependency} before ${item.manifest.name}.`);
    }
    try {
      const dispose = item.lifecycle.activate(this.#context(id));
      if (dispose != null && typeof dispose !== 'function') fail('CORE_DISPOSER_INVALID', id);
      item.dispose = dispose || null;
      this.active.set(id, true);
      this.errors.delete(id);
    } catch (error) {
      let message = error?.message || String(error);
      try { item.lifecycle.deactivate(this.#context(id)); item.dispose?.(); item.dispose = null; }
      catch (cleanupError) {
        this.cleanupPending.add(id);
        message += `. CORE_CLEANUP_FAILED: ${cleanupError?.message || String(cleanupError)}`;
      }
      this.errors.set(id, message);
      const failure = new Error(message, { cause: error });
      failure.code = error?.code || 'CORE_ACTIVATION_FAILED';
      throw failure;
    }
  }

  #deactivate(id) {
    const item = this.catalog.get(id);
    try {
      item.lifecycle.deactivate(this.#context(id));
      item.dispose?.();
      item.dispose = null;
      this.active.delete(id);
      this.cleanupPending.delete(id);
      this.errors.delete(id);
    } catch (error) {
      this.saved[id] = { installed: this.saved[id]?.installed === true, enabled: false };
      const persisted = this.#persist();
      this.cleanupPending.add(id);
      const message = `CORE_CLEANUP_FAILED: ${error?.message || String(error)}${persisted ? '' : '. CORE_STORAGE_UNAVAILABLE: disabled state applies for this visit only'}`;
      this.errors.set(id, message);
      const failure = new Error(message, { cause: error });
      failure.code = 'CORE_CLEANUP_FAILED';
      throw failure;
    }
  }

  enabled(id) { return this.active.has(id) && !this.cleanupPending.has(id); }
  state(id) {
    const item = this.saved[id];
    return { installed: item?.installed === true, enabled: this.enabled(id), cleanupPending: this.cleanupPending.has(id), error: this.errors.get(id) || '' };
  }
  dependents(id, { installedOnly = true } = {}) {
    return [...this.catalog.values()].filter(({ manifest }) => manifest.requires.includes(id)
      && (!installedOnly || this.saved[manifest.id]?.installed)).map(({ manifest }) => manifest.id);
  }

  change(id, action) {
    const entry = this.catalog.get(id);
    if (!entry) fail('CORE_PACKAGE_UNKNOWN', id);
    if (!ACTIONS.has(action)) fail('CORE_ACTION_INVALID', action);
    const state = this.state(id);
    if (this.changing.has(id)) fail('CORE_CHANGE_IN_PROGRESS', id);
    if (action === 'enable' && state.cleanupPending) fail('CORE_CLEANUP_RETRY_REQUIRED', id);
    if (action === 'enable' && !state.installed) fail('CORE_PACKAGE_NOT_INSTALLED', id);
    if ((action === 'disable' || action === 'remove') && this.dependents(id).some((dep) => this.enabled(dep))) {
      fail('CORE_PACKAGE_IN_USE', `enabled dependents: ${this.dependents(id).join(', ')}`);
    }
    if (action === 'remove' && this.dependents(id).length) {
      fail('CORE_PACKAGE_IN_USE', `installed dependents: ${this.dependents(id).join(', ')}`);
    }

    this.changing.add(id);
    let affected = [id];
    try {
      if (action === 'install' && !state.installed) {
        affected = this.#closure(id);
        for (const packageId of affected) this.saved[packageId] = { installed: true, enabled: this.enabled(packageId) };
      } else if (action === 'enable' && !state.enabled) {
        affected = this.#closure(id);
        const activated = [];
        try {
          for (const packageId of affected) {
            if (!this.saved[packageId]?.installed) fail('CORE_DEPENDENCY_NOT_INSTALLED', `Install ${this.catalog.get(packageId).manifest.name} in Extensions, then enable ${entry.manifest.name} again.`);
            if (!this.enabled(packageId)) { this.#activate(packageId); activated.push(packageId); }
            this.saved[packageId] = { installed: true, enabled: true };
          }
        } catch (error) {
          for (const packageId of activated.reverse()) {
            try { this.#deactivate(packageId); } catch { /* keep cleanup state visible */ }
            this.saved[packageId] = { installed: true, enabled: false };
          }
          throw error;
        }
      } else if ((action === 'disable' || action === 'remove') && (state.enabled || state.cleanupPending)) {
        this.#deactivate(id);
        this.saved[id] = { installed: true, enabled: false };
      }
      if (action === 'disable') this.saved[id] = { installed: state.installed, enabled: false };
      // Keep an explicit uninstall tombstone. A package with a bundled
      // defaultState may be registered again after a reload; deleting its
      // entry would silently reinstall it on the next Core startup.
      if (action === 'remove') {
        this.saved[id] = { installed: false, enabled: false };
        this.errors.delete(id);
      }
      const persisted = this.#persist();
      for (const packageId of affected) this.dispatchEvent(new CustomEvent('change', { detail: { id: packageId, action, ...this.state(packageId), persisted } }));
    } catch (error) {
      this.errors.set(id, error?.message || String(error));
      this.#persist();
      throw error;
    } finally { this.changing.delete(id); }
  }
}
