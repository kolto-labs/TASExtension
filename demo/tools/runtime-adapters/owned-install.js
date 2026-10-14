/* Original assets: on-demand, bounded reads; writes stay inside this profile. */
let ownedGameFailed = false;
document.documentElement.classList.add('game-starting');
function showGameStartup() {
  const cover = document.createElement('div'); cover.id = 'game-startup';
  cover.setAttribute('role', 'status'); cover.setAttribute('aria-live', 'polite');
  cover.textContent = 'Loading KOTOR…';
  document.body.insertBefore(cover, document.getElementById('app'));
  const ready = setInterval(() => {
    if (ownedGameFailed) {
      cover.textContent = 'KOTOR could not start. See the error message and diagnostics.';
      clearInterval(ready); return;
    }
    if (typeof window.Module?._telos_vm_present_count_low === 'function' && window.Module._telos_vm_present_count_low() > 0) {
      clearInterval(ready); cover.remove(); document.documentElement.classList.remove('game-starting');
    }
  }, 100);
  addEventListener('pagehide', () => clearInterval(ready), { once:true });
}
if (document.readyState === 'loading') addEventListener('DOMContentLoaded', showGameStartup, { once:true });
else showGameStartup();

const runtimeParams = new URLSearchParams(location.search);
const localInstallLaunch = runtimeParams.get('localInstall') === '1';
// Opt-in localhost receipt for startup investigations. This is disabled for
// ordinary launches; it records only aggregate lazy-file I/O and frame-present
// counts, without changing guest execution or persisting browser profile data.
const ioTraceRequested = ['127.0.0.1', 'localhost', '[::1]'].includes(location.hostname)
  && runtimeParams.get('ioTrace') === '1';
function resolveTelosParentOrigin() {
  const requested = runtimeParams.get('parentOrigin');
  if (!requested) return localInstallLaunch ? null : location.origin;
  let parentUrl;
  try { parentUrl = new URL(requested); } catch { return null; }
  if (requested !== parentUrl.origin || parentUrl.username || parentUrl.password) return null;
  if (parentUrl.origin === location.origin) return parentUrl.origin;
  if (!localInstallLaunch || location.protocol !== 'http:' || parentUrl.protocol !== 'http:') return null;
  const loopback = host => ['localhost', '127.0.0.1', '[::1]', '::1'].includes(host.toLowerCase());
  const port = url => url.port || '80';
  return loopback(location.hostname) && loopback(parentUrl.hostname) && port(parentUrl) === port(location)
    ? parentUrl.origin : null;
}
const telosParentOrigin = resolveTelosParentOrigin();
Object.defineProperty(window, '__telosParentOrigin', { value:telosParentOrigin, configurable:false, writable:false });

// A localhost runtime can be cross-origin from its 127.0.0.1 parent. Load the
// bridge from the runtime's own origin to satisfy its script-src 'self' policy;
// the validated parent origin is used only for cross-window messaging.
if (parent !== window && telosParentOrigin && runtimeParams.get('ownedSession')) {
  const installTasBridge = () => {
    if (document.getElementById('telos-wasm-tas-bridge')) return;
    const script = document.createElement('script');
    script.id = 'telos-wasm-tas-bridge';
    script.src = new URL('/original-game/telos-tas-bridge.js?v=13-parent-origin', location.origin).href;
    document.head.append(script);
  };
  if (document.head) installTasBridge();
  else document.addEventListener('DOMContentLoaded', installTasBridge, { once:true });
}

function notifyOwnedGameStatus(state, message) {
  // Async startup can finish after a worker/renderer failure. Keep that failure
  // visible until this runtime is closed; a later mount is not recovery.
  if (state === 'error' || state === 'stopped') ownedGameFailed = true;
  else if (ownedGameFailed) return;
  const text = String(message).slice(0, 240);
  const session = runtimeParams.get('ownedSession');
  if (parent !== window && telosParentOrigin && /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/.test(session || '')) {
    parent.postMessage({ type:'telos-game-status', session, state, message:text }, telosParentOrigin);
  }
  if (state === 'error') {
    if (typeof Module !== 'undefined') Module.setStatus?.(text);
    let notice = document.getElementById('owned-game-error');
    if (!notice) {
      notice = document.createElement('p'); notice.id = 'owned-game-error'; notice.setAttribute('role','alert');
      notice.style.cssText = 'position:absolute;top:0;left:0;right:0;z-index:1000;margin:0;padding:12px;background:#101b22;color:#dce8ee;font:14px system-ui';
      document.body.append(notice);
    }
    notice.textContent = text;
  }
}
// This script loads before the runtime shell and WASM loader. Errors during
// download, instantiation or worker startup must reach the same status surface
// as failures after mounting. Do not prevent the original error propagation.
addEventListener('error', event => {
  notifyOwnedGameStatus('error', 'The game runtime stopped: ' + (event.message || 'JavaScript error.'));
});
addEventListener('unhandledrejection', event => {
  const reason = event.reason;
  notifyOwnedGameStatus('error', 'The game runtime stopped: ' + (reason?.message || String(reason)));
});
async function fetchOwnedManifest(url) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 8000);
  notifyOwnedGameStatus('starting', 'Opening your game files…');
  try { return await fetch(url, { cache:'no-store', credentials:'same-origin', signal:controller.signal }); }
  catch (error) {
    const text = error.name === 'AbortError' ? 'The local file bridge did not respond. Keep your files selected and reopen the local server.' : 'The local game files are unavailable. Reopen the local server before continuing.';
    notifyOwnedGameStatus('error', text); throw new Error(text, {cause:error});
  } finally { clearTimeout(timeout); }
}
const OWNED_SESSION = runtimeParams.get('ownedSession');
const OWNED_SESSION_PATTERN = /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/;
function requestFromParent(requestType, responseType, payload = {}, attempts = 2) {
  if (parent === window || !telosParentOrigin || !OWNED_SESSION_PATTERN.test(OWNED_SESSION || '')) {
    return Promise.reject(new Error('OWNED_PARENT_UNAVAILABLE: this game session has no verified file owner. Reconnect the game folder and retry.'));
  }
  const run = async () => {
    let lastError;
    for (let attempt = 1; attempt <= attempts; ++attempt) {
      const request = crypto.randomUUID();
      try {
        return await new Promise((resolve, reject) => {
          let settled = false;
          const finish = (error, value) => {
            if (settled) return;
            settled = true; clearTimeout(timeout); removeEventListener('message', receive);
            if (error) reject(error); else resolve(value);
          };
          const receive = event => {
            const value = event.data;
            if (event.origin !== telosParentOrigin || event.source !== parent || value?.type !== responseType
                || value.session !== OWNED_SESSION || value.request !== request) return;
            finish(null, value);
          };
          const timeout = setTimeout(() => finish(new Error(`OWNED_PARENT_TIMEOUT: ${requestType} attempt ${attempt}/${attempts} timed out after 6000 ms; parent=${telosParentOrigin}; session=${OWNED_SESSION.slice(0,8)}. Keep the game-files page open and retry.`)), 6000);
          addEventListener('message', receive);
          try { parent.postMessage({ type:requestType, session:OWNED_SESSION, request, ...payload }, telosParentOrigin); }
          catch (error) { finish(new Error(`OWNED_PARENT_SEND_FAILED: ${error?.name || 'Error'}: ${String(error?.message || error).slice(0,120)}`)); }
        });
      } catch (error) {
        lastError = error;
        if (attempt < attempts && String(error?.message || '').startsWith('OWNED_PARENT_TIMEOUT:')) await new Promise(resolve => setTimeout(resolve, 180));
        else throw error;
      }
    }
    throw lastError || new Error('OWNED_PARENT_UNAVAILABLE: no response from the game-files page.');
  };
  return run();
}
async function receiveOwnedManifest() {
  if (localInstallLaunch) return fetchOwnedManifest('/private/install.json');
  const reply = await requestFromParent('telos-owned-manifest-request', 'telos-owned-manifest', {}, 2);
  if (typeof reply.error === 'string' && reply.error) throw new Error(reply.error.slice(0, 8192));
  const manifest = reply.manifest;
  if (!manifest || manifest.schema !== 'telos.owned-game-assets/v1' || manifest.assetMode !== 'lazy'
      || !Array.isArray(manifest.files) || manifest.files.length > 100000 || !Array.isArray(manifest.writable)) {
    throw new Error(`OWNED_MANIFEST_INVALID: parent returned a malformed session manifest; files=${Array.isArray(manifest?.files) ? manifest.files.length : 'invalid'}; session=${OWNED_SESSION.slice(0,8)}.`);
  }
  return { ok:true, status:200, json:async () => manifest };
}
async function receiveOwnedFileSnapshots(manifest) {
  if (!OWNED_SESSION || localInstallLaunch) return null; // Local host serves checked same-origin ranges.
  const reply = await requestFromParent('telos-owned-snapshots-request', 'telos-owned-snapshots', {}, 2);
  if (typeof reply.error === 'string' && reply.error) throw new Error(reply.error.slice(0, 8192));
  const files = reply.files;
  if (!Array.isArray(files) || files.length !== manifest.files.length || files.length > 100000) {
    throw new Error(`OWNED_SNAPSHOT_COUNT_MISMATCH: manifest=${manifest.files.length}; parent=${Array.isArray(files) ? files.length : 'invalid'}; session=${OWNED_SESSION.slice(0,8)}. Close KOTOR, reconnect the same folder, then retry.`);
  }
  const byName = new Map();
  for (const entry of files) {
    if (typeof entry?.name !== 'string' || !(entry.file instanceof Blob) || byName.has(entry.name)) {
      throw new Error(`OWNED_SNAPSHOT_INVALID: duplicate path or non-Blob entry; parent=${files.length}; session=${OWNED_SESSION.slice(0,8)}. Reconnect the selected folder.`);
    }
    byName.set(entry.name, entry.file);
  }
  const snapshots = new Map(), urls = new Set();
  for (let index = 0; index < manifest.files.length; ++index) {
    const descriptor = manifest.files[index];
    const file = byName.get(descriptor?.name);
    if (!file) throw new Error(`OWNED_SNAPSHOT_MISSING: manifest-index=${index}; path=${String(descriptor?.name || '<invalid>').slice(-72)}; parent=${files.length}. Reconnect the same folder.`);
    if (!Number.isSafeInteger(descriptor.bytes) || descriptor.bytes < 0 || file.size !== descriptor.bytes) {
      throw new Error(`OWNED_SNAPSHOT_SIZE_MISMATCH: manifest-index=${index}; path=${String(descriptor.name).slice(-72)}; expected=${descriptor.bytes}; received=${file.size}. Reconnect the same folder.`);
    }
    if (typeof descriptor.url !== 'string' || urls.has(descriptor.url)) {
      throw new Error(`OWNED_MANIFEST_URL_INVALID: manifest-index=${index}; path=${String(descriptor.name).slice(-72)}; duplicate or missing asset URL.`);
    }
    urls.add(descriptor.url); snapshots.set(descriptor.url, file); byName.delete(descriptor.name);
  }
  if (byName.size) throw new Error(`OWNED_SNAPSHOT_EXTRA: parent=${files.length}; unmatched=${byName.size}; first=${String(byName.keys().next().value).slice(-72)}. Reconnect the same folder.`);
  return snapshots;
}
// Install before asynchronous startup can fail. An early request receives a
// truthful failure, never a durability acknowledgment before profile restore.
let ownedProfileFlushReady = false;
let ownedProfileSyncInFlight = null;
let ownedProfileSyncTimer = null;
function describeOwnedProfileSyncError(error) {
  const name = typeof error?.name === 'string'
    ? error.name.replace(/[\x00-\x1f\x7f]/g, '').slice(0, 80) : 'Error';
  const errno = Number.isSafeInteger(error?.errno) && error.errno >= 0 && error.errno <= 4096
    ? `; errno=${error.errno}` : '';
  const message = typeof error?.message === 'string'
    ? error.message.replace(/[\x00-\x1f\x7f]/g, ' ').slice(0, 512) : '';
  return `Browser profile sync failed (${name || 'Error'}${errno})${message ? ': ' + message : '.'}`;
}
function syncOwnedProfile() {
  if (!ownedProfileFlushReady) return Promise.reject(new Error('The browser profile is not ready to save.'));
  const previous = ownedProfileSyncInFlight;
  const sync = (previous ? previous.catch(() => {}) : Promise.resolve()).then(() => new Promise((resolve, reject) => {
    try { FS.syncfs(false, error => error ? reject(error) : resolve()); }
    catch (error) { reject(error); }
  }));
  ownedProfileSyncInFlight = sync;
  sync.finally(() => { if (ownedProfileSyncInFlight === sync) ownedProfileSyncInFlight = null; }).catch(() => {});
  return sync;
}
function syncOwnedProfileInBackground() {
  if (!ownedProfileFlushReady || ownedProfileSyncInFlight) return;
  syncOwnedProfile().catch(error => console.warn(describeOwnedProfileSyncError(error)));
}
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'hidden') syncOwnedProfileInBackground();
});
addEventListener('pagehide', syncOwnedProfileInBackground);
addEventListener('message', event => {
  if (parent === window || !telosParentOrigin || event.source !== parent || event.origin !== telosParentOrigin || event.data?.type !== 'telos-flush-profile' || typeof event.data.request !== 'string') return;
  const request = event.data.request;
  const reply = error => parent.postMessage({ type:'telos-profile-flushed', request, ok:!error, error:error ? describeOwnedProfileSyncError(error) : null }, telosParentOrigin);
  if (!ownedProfileFlushReady) { reply(new Error('The browser profile is not ready to save. Keep this runtime open and retry.')); return; }
  syncOwnedProfile().then(() => reply(null), error => reply(error || new Error('The browser profile could not be saved.')));
});
async function mountOwnedInstall() {
  const previousAbort = Module.onAbort;
  Module.onAbort = reason => {
    notifyOwnedGameStatus('error', 'The game runtime stopped: ' + String(reason));
    previousAbort?.(reason);
  };
  const previousExit = Module.onExit;
  Module.onExit = code => {
    notifyOwnedGameStatus(code === 0 ? 'stopped' : 'error', code === 0 ? 'The game has closed.' : `The game runtime exited with code ${code}.`);
    previousExit?.(code);
  };
  // This hook runs after the shell restores its persistent profile. Enable
  // saving before manifest, snapshot or initial INI operations can reject.
  if (typeof FS === 'undefined' || typeof FS.syncfs !== 'function' || typeof ROOT !== 'string' || !FS.analyzePath(ROOT).exists) throw new Error('The browser profile is not ready to mount game files.');
  ownedProfileFlushReady = true;
  if (ownedProfileSyncTimer === null) ownedProfileSyncTimer = setInterval(syncOwnedProfileInBackground, 30000);
  const response = await receiveOwnedManifest();
  if (!response.ok) throw new Error(`OWNED_LOCAL_MANIFEST_HTTP: status=${response.status}; path=/private/install.json. Restart the local file service and retry.`);
  const manifest = await response.json();
  if (manifest.schema !== 'telos.owned-game-assets/v1') throw new Error('Unknown owned installation manifest.');
  const assetMode = manifest.assetMode ?? 'lazy';
  if (!['lazy', 'embedded'].includes(assetMode) || (assetMode === 'embedded' && typeof readEmbeddedOwnedAssetRange !== 'function')) throw new Error('Original asset mode is unavailable.');
  const localSnapshots = await receiveOwnedFileSnapshots(manifest);
  // The dialog table is small and repeatedly scanned during startup, so mount
  // it eagerly. Keep the large GUI texture archive on the normal lazy range
  // path below; downloading the whole pack here delays the first frame.
  const startupFiles = new Map();
  const startupNames = new Set(['dialog.tlk']);
  if (assetMode !== 'embedded' && !localSnapshots) for (const file of manifest.files) {
    if (!startupNames.has(file.name.toLowerCase()) || file.bytes > 128 * 1024 * 1024) continue;
    const end = file.bytes - 1;
    const expectedRange = `bytes 0-${end}/${file.bytes}`;
    const preload = await fetch(file.url, { cache:'no-store', credentials:'same-origin', headers:{ Range:`bytes=0-${end}` } });
    if (preload.status !== 206 || preload.headers.get('Content-Range') !== expectedRange) throw new Error(`Could not preload ${file.name}: expected ${expectedRange}.`);
    const data = new Uint8Array(await preload.arrayBuffer());
    if (data.length !== file.bytes) throw new Error(`Could not preload ${file.name}: received ${data.length} of ${file.bytes} bytes.`);
    startupFiles.set(file.url, data);
  }
  const assetRoot = '/owned-game';
  FS.mkdirTree(assetRoot);
  const cache = new Map();
  // Keep synchronous guest reads within the browser-verified 64 KiB block.
  // Larger loopback ranges can fail inside sync XHR before the game draws.
  const chunkSize = 64 * 1024;
  // User-selected Blob snapshots still use 16 KiB x-user-defined text slices
  // because some Chromium builds truncate synchronous Blob-XHR responses.
  // For loopback HTTP, consume the original range response as text when the
  // synchronous window context rejects ArrayBuffer mode; retry in slices only
  // when that first response is actually short.
  const maxSnapshotTextSlice = 16_000;
  // Retain more of KOTOR's repeatedly probed BIF ranges; the small cache was
  // evicting player.bif during scene-load scans and forcing sync HTTP rereads.
  const budget = 512 * 1024 * 1024;
  let resident = 0;
  const io = { requests: 0, bytes: 0, milliseconds: 0, cacheHits: 0, failures: 0, shortReads: 0, readRetries: 0 };
  const observedFiles = new Set();
  const failedReads = new Set();
  const profileRoot = ROOT + '/owned-game';
  FS.mkdirTree(profileRoot);
  function readFailure(file, reason) {
    // A resource failure must identify the original operation, rather than
    // surfacing only as an eventual black game window. Log once per file.
    if (!failedReads.has(file.url)) {
      failedReads.add(file.url);
      io.failures++;
      console.error(`Original asset failure: ${file.name}: ${reason}`);
      notifyOwnedGameStatus('error', `Could not read ${file.name}: ${reason}. Close the game and reconnect your local files.`);
    }
    return new FS.ErrnoError(29);
  }
  function readTextSlices(file, start, end, readSlice) {
    const parts = [];
    let slices = 0;
    for (let cursor = start; cursor <= end;) {
      const length = Math.min(maxSnapshotTextSlice, end + 1 - cursor);
      if (++slices > 256) throw readFailure(file, 'too many short synchronous range reads; refusing to stall the game');
      const part = readSlice(cursor, cursor + length);
      if (part.length <= 0 || part.length > length) throw readFailure(file, `invalid range response: ${part.length} bytes for ${length} at offset ${cursor}`);
      if (part.length < length) { io.shortReads++; io.readRetries++; }
      // A positive short response is a verified prefix; continue exactly where
      // it ended. Never shrink the global slice size and turn later reads into
      // one-byte synchronous requests.
      parts.push(part);
      cursor += part.length;
    }
    return parts.join('');
  }
  function readRange(file, start, end) {
    if (assetMode === 'embedded') {
      const embedded = readEmbeddedOwnedAssetRange(file, start, end);
      if (embedded !== null) return embedded;
      // Only writable INI initialization may precede SDK static constructors.
      if (file.name.toLowerCase() !== 'swkotor.ini') throw readFailure(file, 'embedded original asset unavailable');
    }
    // Synchronous document HTTP requests cannot rely on service-worker routing.
    // For the prototype, resolve just this disk-backed File slice through blob
    // loading. Private HTTP range requests retain their existing validation.
    if (!Number.isSafeInteger(start) || !Number.isSafeInteger(end) || start < 0 || end < start || end >= file.bytes || end - start + 1 > 1024 * 1024) throw readFailure(file, 'invalid bounded range');
    const requested = end - start + 1;
    const chunkIndex = Math.floor(start / chunkSize);
    const chunkStart = chunkIndex * chunkSize;
    const chunkEnd = Math.min(file.bytes, chunkStart + chunkSize) - 1;
    // Some guest file paths ask the synchronous VFS for a byte or a few bytes
    // at a time. Fetch the containing block once and serve those reads from the
    // same bounded LRU as normal stream and mmap access; otherwise each tiny
    // guest read becomes a blocking browser request. Let complete cache blocks
    // pass through to the transport below to avoid recursive cache misses.
    if (requested < 4096 && !(start === chunkStart && end === chunkEnd)) {
      const block = chunk(file, chunkIndex);
      return block.slice(start - chunkStart, end - chunkStart + 1);
    }
    const snapshot = localSnapshots?.get(file.url);
    if (localSnapshots && !snapshot) throw readFailure(file, 'selected file unavailable');
    const started = performance.now();
    let text, bytes, transportStage = 'preparing range read';
    try {
      if (snapshot) {
        text = readTextSlices(file, start, end, (cursor, next) => {
          transportStage = `creating snapshot slice ${cursor}..${next - 1}`;
          const objectUrl = URL.createObjectURL(snapshot.slice(cursor, next, 'application/octet-stream'));
          const request = new XMLHttpRequest();
          try {
            transportStage = `opening synchronous snapshot request ${cursor}..${next - 1}`;
            request.open('GET', objectUrl, false);
            transportStage = `setting snapshot response mode ${cursor}..${next - 1}`;
            request.overrideMimeType('text/plain; charset=x-user-defined');
            transportStage = `sending synchronous snapshot request ${cursor}..${next - 1}`;
            request.send();
            io.requests++;
            transportStage = `checking snapshot response ${cursor}..${next - 1}`;
            if (request.status !== 200) throw readFailure(file, `snapshot range rejected, HTTP ${request.status}, bytes ${cursor}..${next - 1}`);
            return request.responseText;
          } finally { URL.revokeObjectURL(objectUrl); }
        });
      } else {
        const request = new XMLHttpRequest();
        transportStage = 'opening owned-file range request';
        request.open('GET', file.url, false);
        transportStage = 'setting owned-file range header';
        request.setRequestHeader('Range', `bytes=${start}-${end}`);
        let binaryResponse = true;
        try { request.responseType = 'arraybuffer'; }
        catch (error) {
          if (error?.name !== 'InvalidAccessError') throw error;
          binaryResponse = false;
          request.overrideMimeType('text/plain; charset=x-user-defined');
        }
        transportStage = 'sending owned-file range request';
        request.send();
        io.requests++;
        transportStage = 'validating owned-file range response';
        const expectedRange = `bytes ${start}-${end}/${file.bytes}`;
        if (request.status !== 206 || request.getResponseHeader('Content-Range') !== expectedRange) throw readFailure(file, `range rejected, HTTP ${request.status}, ${expectedRange}`);
        if (binaryResponse) {
          if (!(request.response instanceof ArrayBuffer)) throw readFailure(file, 'binary range response was not an ArrayBuffer');
          bytes = new Uint8Array(request.response);
        } else {
          text = request.responseText;
          const requestedBytes = end - start + 1;
          if (text.length > requestedBytes) throw readFailure(file, `oversized range, ${text.length} bytes instead of ${requestedBytes}`);
          if (text.length < requestedBytes) {
            io.shortReads++; io.readRetries++;
            text = readTextSlices(file, start, end, (cursor, next) => {
              const fallback = new XMLHttpRequest();
              fallback.open('GET', file.url, false);
              fallback.overrideMimeType('text/plain; charset=x-user-defined');
              fallback.setRequestHeader('Range', `bytes=${cursor}-${next - 1}`);
              fallback.send();
              io.requests++;
              const fallbackRange = `bytes ${cursor}-${next - 1}/${file.bytes}`;
              if (fallback.status !== 206 || fallback.getResponseHeader('Content-Range') !== fallbackRange) throw readFailure(file, `range rejected, HTTP ${fallback.status}, ${fallbackRange}`);
              return fallback.responseText;
            });
          }
        }
      }
      if (snapshot && text.length !== end - start + 1) throw readFailure(file, `short range, ${text.length} bytes instead of ${end - start + 1}`);
      if (!bytes) {
        bytes = new Uint8Array(text.length);
        for (let index = 0; index < bytes.length; index++) bytes[index] = text.charCodeAt(index) & 255;
      }
      if (bytes.length !== end - start + 1) throw readFailure(file, `short range, ${bytes.length} bytes instead of ${end - start + 1}`);
    } catch (error) {
      if (error instanceof FS.ErrnoError) throw error;
      const cause = error instanceof Error ? `${error.name}: ${error.message}` : String(error);
      throw readFailure(file, `transport failed during ${transportStage} for bytes ${start}..${end} (${cause.slice(0, 180)})`);
    } finally {
      io.milliseconds += performance.now() - started;
    }
    io.bytes += bytes.length;
    return bytes;
  }
  function chunk(file, index) {
    const key = file.url + ':' + index;
    if (cache.has(key)) {
      io.cacheHits++;
      const bytes = cache.get(key); cache.delete(key); cache.set(key, bytes); return bytes;
    }
    const start = index * chunkSize;
    const bytes = readRange(file, start, Math.min(file.bytes, start + chunkSize) - 1);
    while (resident + bytes.length > budget && cache.size) {
      const first = cache.keys().next().value;
      resident -= cache.get(first).length; cache.delete(first);
    }
    cache.set(key, bytes); resident += bytes.length;
    if (!observedFiles.has(file.url)) {
      observedFiles.add(file.url);
      if (observedFiles.size <= 16) console.log(`Original asset read: ${file.name}, bytes ${start}..${start + bytes.length - 1}`);
    }
    return bytes;
  }
  for (let index = 0; index < manifest.files.length; ++index) {
    const file = manifest.files[index];
    const localUrl = /^\/private\/install\/\d+$/.test(file?.url || '');
    const sessionUrl = OWNED_SESSION && file?.url === `/original-game/owned-game-assets/${OWNED_SESSION}/${index}`;
    if (!file || typeof file.name !== 'string' || !Number.isSafeInteger(file.bytes) || file.bytes < 0
        || file.name.split('/').some(part => !part || part === '.' || part === '..')
        || (localInstallLaunch ? !localUrl : !sessionUrl)) {
      throw new Error(`OWNED_MANIFEST_ENTRY_INVALID: index=${index}; path=${String(file?.name || '<invalid>').slice(-72)}; bytes=${String(file?.bytes)}; transport=${localInstallLaunch ? 'local-range' : 'folder-snapshot'}; url=${String(file?.url || '<missing>').slice(0,120)}.`);
    }
    const path = assetRoot + '/' + file.name;
    FS.mkdirTree(path.slice(0, path.lastIndexOf('/')));
    if (file.name.toLowerCase() === 'swkotor.ini') {
      const writable = profileRoot + '/swkotor.ini';
      if (!FS.analyzePath(writable).exists) {
        // INI is tiny, writable and persisted; never modifies the source copy.
        if (file.bytes > 1024 * 1024) throw new Error('Unexpected game configuration size.');
        FS.writeFile(writable, file.bytes ? readRange(file, 0, file.bytes - 1) : new Uint8Array());
      }
      FS.symlink(writable, path);
      continue;
    }
    const startupData = startupFiles.get(file.url);
    if (startupData) {
      FS.createDataFile(path.slice(0, path.lastIndexOf('/')), path.slice(path.lastIndexOf('/') + 1), startupData, true, false);
      continue;
    }
    FS.createDataFile(path.slice(0, path.lastIndexOf('/')), path.slice(path.lastIndexOf('/') + 1), new Uint8Array(), true, false);
    const node = FS.lookupPath(path).node;
    const originalGetattr = node.node_ops.getattr;
    node.node_ops = { ...node.node_ops, getattr(current) { return { ...originalGetattr(current), size: file.bytes, blocks: Math.ceil(file.bytes / 4096) }; }, setattr() { throw new FS.ErrnoError(63); } };
    node.stream_ops = { ...node.stream_ops, read(stream, buffer, offset, length, position) {
      const available = Math.max(0, Math.min(length, file.bytes - position));
      for (let done = 0; done < available;) {
        const current = position + done;
        const bytes = chunk(file, Math.floor(current / chunkSize));
        const begin = current % chunkSize;
        const count = Math.min(available - done, bytes.length - begin);
        buffer.set(bytes.subarray(begin, begin + count), offset + done); done += count;
      }
      return available;
    }, write() { throw new FS.ErrnoError(63); }, mmap(stream, length, position, prot, flags) {
      // Emscripten's filesystem does not fall back to stream_ops.read for an
      // mmap. Map a private, page-aligned copy through the same lazy 64 KiB
      // cache used by ordinary reads; these source files are immutable.
      const privateMapping = (flags & 2) !== 0;
      const sharedMapping = (flags & 1) !== 0;
      // MAP_PRIVATE writes land only in the allocated Wasm copy. Never claim
      // MAP_SHARED write support: these source assets are immutable and msync
      // intentionally does not write through to user files.
      if ((prot & 2) !== 0 && (!privateMapping || sharedMapping)) throw new FS.ErrnoError(2);
      if (!Number.isSafeInteger(length) || length <= 0 || !Number.isSafeInteger(position) || position < 0 || position > file.bytes || length > file.bytes - position + 65535) throw new FS.ErrnoError(28);
      // Emscripten's munmap frees the returned address directly when
      // allocated:true. Use its own page-aligned mmap allocator so that
      // address is the allocation base, never an aligned interior pointer.
      const allocateMapping = Module.telosMmapAlloc;
      if (typeof allocateMapping !== 'function') throw new FS.ErrnoError(38);
      const ptr = allocateMapping(length);
      if (!ptr) throw new FS.ErrnoError(48);
      try {
        const mappedBytes = Math.min(length, file.bytes - position);
        for (let done = 0; done < mappedBytes;) {
          const current = position + done;
          const bytes = chunk(file, Math.floor(current / chunkSize));
          const begin = current % chunkSize;
          const count = Math.min(mappedBytes - done, bytes.length - begin);
          if (count <= 0) throw readFailure(file, 'short chunk while mapping original asset');
          Module.HEAPU8.set(bytes.subarray(begin, begin + count), ptr + done);
          done += count;
        }
        if (mappedBytes < length) Module.HEAPU8.fill(0, ptr + mappedBytes, ptr + length);
        return { ptr, allocated: true };
      } catch (error) {
        Module._free(ptr);
        throw error;
      }
    }, msync() { return 0; }, llseek(stream, offset, whence) {
      const position = whence === 1 ? stream.position + offset : whence === 2 ? file.bytes + offset : offset;
      if (position < 0) throw new FS.ErrnoError(28);
      return position;
    } };
  }
  for (const directory of manifest.writable.filter(name => name !== 'swkotor.ini')) {
    FS.mkdirTree(profileRoot + '/' + directory);
    FS.symlink(profileRoot + '/' + directory, assetRoot + '/' + directory);
  }
  // BoxedWine's native directory mount reads through this Emscripten FS.
  Config.ownedInstallDirectory = assetRoot;
  // An explicit diagnostic snapshot measures the real range path without a
  // per-read console stream or a timer competing with the guest renderer.
  Module.getOwnedInstallIoStats = () => ({ ...io, residentBytes: resident, cacheEntries: cache.size, filesRead: observedFiles.size });
  if (ioTraceRequested) {
    const startedAt = performance.now();
    const report = () => {
      const runtime = window.Module || Module;
      const stats = typeof runtime.getOwnedInstallIoStats === 'function' ? runtime.getOwnedInstallIoStats() : null;
      const presents = typeof runtime._telos_vm_present_count_low === 'function'
        ? runtime._telos_vm_present_count_low() >>> 0 : null;
      const failedFiles = [...failedReads].map(url => manifest.files.find(file => file.url === url)?.name || 'unknown').slice(0, 16);
      const readFiles = manifest.files.filter(file => observedFiles.has(file.url)).map(file => file.name).slice(0, 32);
      console.info('[Telos startup receipt]', JSON.stringify({
        schema:'telos.runtime-startup-receipt/v1', session:OWNED_SESSION || null,
        elapsedMs:Math.round(performance.now() - startedAt), presents, io:stats,
        readFiles, failedFiles,
      }));
    };
    report();
    const timer = setInterval(report, 5000);
    addEventListener('pagehide', () => clearInterval(timer), { once:true });
  }
  notifyOwnedGameStatus('files-ready', 'Game files connected. Starting the original game…');
  console.log(`Original assets mounted in place: ${manifest.files.length} files, ${budget / 1048576} MiB range cache; private writable profile.`);
}
