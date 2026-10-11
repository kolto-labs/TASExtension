// Trusted, explicitly enabled local-development UI replacement only.
export function installUiHotReload() {
  if (!['localhost', '127.0.0.1', '[::1]'].includes(location.hostname) ||
      new URLSearchParams(location.search).get('uiHotReload') !== '1') {
    throw new Error('UI hot reload requires a local page with uiHotReload=1.');
  }
  if (window.telosUiHotReload) return window.telosUiHotReload;
  const entries = new Map();
  let busy = false, version = 0;
  let status = { level:'info', message:'Starting local UI watcher.' };
  const report = (level, message, paths = []) => {
    status = { level, message, paths, time:Date.now() };
    console[level === 'error' ? 'error' : level === 'warn' ? 'warn' : 'info'](`[UI hot reload] ${message}`, paths);
    window.dispatchEvent(new CustomEvent('telos:ui-hot-reload', { detail:status }));
  };
  const versioned = value => {
    const url = new URL(value, location.href);
    if (url.origin !== location.origin) throw new Error('UI resources must use this page origin.');
    url.searchParams.set('uiVersion', `${Date.now()}-${++version}`);
    return url.href;
  };
  const validHandle = handle => handle &&
    ['snapshot', 'restore', 'destroy'].every(name => typeof handle[name] === 'function');
  const rememberFocus = () => {
    const node = document.activeElement;
    if (!node || node.tagName === 'IFRAME' || node === document.body) return () => {};
    const id = node.id;
    const start = node.selectionStart, end = node.selectionEnd, direction = node.selectionDirection;
    return () => {
      const target = node.isConnected ? node : id ? document.getElementById(id) : null;
      if (!target || !target.getClientRects().length || target.disabled) return;
      target.focus({ preventScroll:true });
      if (typeof start === 'number' && typeof target.setSelectionRange === 'function') {
        try { target.setSelectionRange(start, end, direction); }
        catch { /* A replacement may use a different input type. */ }
      }
    };
  };
  const exclusive = async action => {
    if (busy) throw new Error('A UI replacement is already in progress.');
    busy = true;
    try { return await action(); }
    finally { busy = false; }
  };
  const stageStyle = link => {
    const next = link.cloneNode(false);
    next.removeAttribute('id');
    next.href = versioned(link.href);
    next.media = 'not all';
    next.disabled = false;
    let timer, rejectLoad, committed = false;
    const ready = new Promise((resolve, reject) => {
      rejectLoad = reject;
      next.onload = resolve;
      next.onerror = () => reject(new Error(`Could not load ${new URL(link.href).pathname}`));
      timer = setTimeout(() => reject(new Error('UI stylesheet load timed out.')), 15000);
    });
    // Cancellation can precede collection into Promise.all if another stage fails.
    ready.catch(() => {});
    const cleanup = () => { clearTimeout(timer); next.onload = next.onerror = null; };
    link.after(next);
    return {
      ready,
      commit() {
        if (!link.isConnected || !next.isConnected) throw new Error('Stylesheet changed during replacement.');
        cleanup();
        next.media = link.media;
        if (link.id) next.id = link.id;
        next.disabled = link.disabled;
        link.replaceWith(next);
        committed = true;
      },
      cancel() {
        cleanup();
        if (committed) next.replaceWith(link);
        else next.remove();
        rejectLoad(new Error('UI stylesheet replacement cancelled.'));
      },
    };
  };
  const api = Object.freeze({
    register(name, entry) {
      if (entries.get(name)?.create) throw new Error(`UI module already registered: ${name}`);
      if (!entry || typeof entry.create !== 'function' || !validHandle(entry.handle)) {
        throw new Error('UI modules require create and snapshot/restore/destroy lifecycle methods.');
      }
      const url = new URL(entry.url, location.href);
      if (url.origin !== location.origin || !url.pathname.endsWith('.js')) throw new Error('Invalid local UI module URL.');
      entries.set(name, { ...entry, url:url.href });
    },
    unsupported(name, reason, { url: sourceUrl } = {}) {
      if (entries.has(name)) return;
      const entry = { reason };
      if (sourceUrl != null) {
        const url = new URL(sourceUrl, location.href);
        if (url.origin !== location.origin || !url.pathname.endsWith('.js')) throw new Error('Invalid local UI module URL.');
        // Watch for pending edits without granting this module a reload lifecycle.
        entry.url = url.href;
      }
      entries.set(name, entry);
    },
    status: () => ({ ...status, paths:[...(status.paths || [])] }),
    list: () => [...entries].map(([name, entry]) => ({ name, reloadable:!!entry.create, reason:entry.reason || null })),
    reloadModule: name => exclusive(async () => {
      const entry = entries.get(name);
      if (!entry?.create) throw new Error(entry?.reason || `Unknown UI module: ${name}`);
      const module = await import(versioned(entry.url));
      // Take the latest state and focus after asynchronous loading has finished.
      const state = entry.handle.snapshot();
      const restoreFocus = rememberFocus();
      let candidate, disposingOld = false;
      try {
        candidate = entry.create(module, state);
        if (!validHandle(candidate)) throw new Error('Replacement UI module has an invalid lifecycle.');
        disposingOld = true;
        entry.handle.destroy();
      } catch (error) {
        const failures = [error];
        // Candidate cleanup must never prevent recovery of the previous UI.
        try { candidate?.destroy?.(); } catch (cleanupError) { failures.push(cleanupError); }
        try {
          if (disposingOld) {
            // Retry idempotent disposal before remounting; failed disposal must
            // not silently leave two owners listening to the same controls.
            entry.handle.destroy();
            entry.handle = entry.create(entry.module, state);
            if (!validHandle(entry.handle)) throw new Error('Recovered UI has an invalid lifecycle.');
          } else entry.handle.restore(state);
        } catch (recoveryError) { failures.push(recoveryError); }
        finally {
          if (failures.length > 1) {
            entry.create = null;
            entry.reason = 'Lifecycle recovery failed; automatic replacement is disabled to preserve the session.';
          }
          restoreFocus();
        }
        throw failures.length === 1 ? error : new AggregateError(failures, 'UI replacement and lifecycle recovery failed.');
      }
      entry.module = module;
      entry.handle = candidate;
      restoreFocus();
      return { module:name, replaced:true };
    }),
    reloadStyles: () => exclusive(async () => {
      const staged = [];
      try {
        for (const link of document.querySelectorAll('link[rel="stylesheet"]')) {
          if (new URL(link.href).origin === location.origin) staged.push(stageStyle(link));
        }
        await Promise.all(staged.map(style => style.ready));
        const restoreFocus = rememberFocus();
        staged.forEach(style => style.commit());
        window.dispatchEvent(new Event('resize'));
        restoreFocus();
        return { styles:staged.length, replaced:true };
      } catch (error) {
        staged.forEach(style => style.cancel());
        throw error;
      }
    }),
  });
  Object.defineProperty(window, 'telosUiHotReload', { value:api, configurable:true });
  // The local source server provides consolidated content revisions. UI edits
  // require no production build, server restart, or page/session reload.
  const signatures = new Map();
  const pending = new Set();
  const retries = new Map();
  let stopped = false, timer, flushTimer, flushing = false, pollingFailed = false;
  const sourcePath = value => {
    const url = new URL(value, location.href);
    return url.origin === location.origin ? url.pathname.replace(/^\//, '') : null;
  };
  const watchedPaths = () => {
    const paths = new Set(['js/ui-shell.js', 'js/ui-hot-reload.js']);
    for (const link of document.querySelectorAll('link[rel="stylesheet"]')) {
      const path = sourcePath(link.href);
      if (path) paths.add(path);
    }
    for (const entry of entries.values()) {
      if (entry.url) paths.add(sourcePath(entry.url));
    }
    return [...paths].filter(Boolean);
  };
  const flush = async () => {
    if (stopped || flushing || !pending.size) return;
    if (busy) { flushTimer = setTimeout(flush, 200); return; }
    flushing = true;
    const paths = [...pending];
    pending.clear();
    const revisions = new Map(paths.map(path => [path, signatures.get(path)]));
    const settled = (changed, failed, retryable = true) => {
      for (const path of changed) {
        // A newer edit already queued by polling owns its own retry schedule.
        if (signatures.get(path) !== revisions.get(path)) continue;
        if (!failed || !retryable) { retries.delete(path); continue; }
        const attempts = (retries.get(path)?.attempts || 0) + 1;
        retries.set(path, {
          attempts,
          nextAt: Date.now() + Math.min(30000, 2000 * 2 ** Math.min(attempts - 1, 4)),
        });
      }
    };
    const css = paths.filter(path => path.endsWith('.css'));
    const presentation = paths.filter(path => path === 'js/tas-presentation.js');
    const unsupported = paths.filter(path => !css.includes(path) && !presentation.includes(path));
    try {
      if (css.length) {
        try { await api.reloadStyles(); settled(css, false); report('info', 'Styles replaced; game session preserved.', css); }
        catch (error) { settled(css, true); report('error', `Styles were not replaced: ${error.message}`, css); }
      }
      if (presentation.length) {
        try { await api.reloadModule('presentation'); settled(presentation, false); report('info', 'Presentation replaced; game session preserved.', presentation); }
        catch (error) {
          settled(presentation, true, !!entries.get('presentation')?.create);
          report('error', `Presentation was not replaced: ${error.message}`, presentation);
        }
      }
      if (unsupported.length) report('warn',
        'Changed modules have no safe reload lifecycle. Changes are pending a manual page reload; the current game session is preserved.', unsupported);
    } finally {
      flushing = false;
      if (pending.size && !stopped) flushTimer = setTimeout(flush, 200);
    }
  };
  const poll = async () => {
    if (stopped) return;
    let failed = false;
    if (!document.hidden) {
      try {
        const paths = watchedPaths();
        const endpoint = new URL('/__dev/ui-revisions', location.href);
        for (const path of paths) endpoint.searchParams.append('path', path);
        const response = await fetch(endpoint, {
          cache:'no-store', signal:AbortSignal.timeout(5000),
        });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const revisions = await response.json();
        // Validate the entire response before advancing any baseline or retry.
        if (!revisions || typeof revisions !== 'object' || Array.isArray(revisions)
          || paths.some(path => !Object.prototype.hasOwnProperty.call(revisions, path)
            || typeof revisions[path] !== 'string' || !/^[0-9a-f]{64}$/.test(revisions[path]))) {
          throw new Error('Invalid local UI revision response.');
        }
        for (const path of paths) {
          const signature = revisions[path];
          const previous = signatures.get(path);
          signatures.set(path, signature);
          if (previous !== undefined && previous !== signature) {
            retries.delete(path);
            pending.add(path);
          } else if (!flushing && retries.get(path)?.nextAt <= Date.now()) pending.add(path);
        }
      } catch { failed = true; }
      if (failed && !pollingFailed) report('warn', 'Some UI sources could not be checked. The local watcher will retry; the game session is preserved.');
      if (!failed && pollingFailed) report('info', 'Local UI source polling recovered.');
      pollingFailed = failed;
      if (pending.size) { clearTimeout(flushTimer); flushTimer = setTimeout(flush, 200); }
    }
    if (!stopped) timer = setTimeout(poll, 2000);
  };
  window.addEventListener('pagehide', () => {
    stopped = true;
    clearTimeout(timer);
    clearTimeout(flushTimer);
  });
  window.addEventListener('pageshow', event => {
    if (event.persisted && stopped) { stopped = false; void poll(); }
  });
  report('info', 'Watching local UI sources. CSS and presentation changes preserve the game session.');
  // Registration in ui-shell completes before the initial baseline is read.
  timer = setTimeout(poll, 0);
  return api;
}
