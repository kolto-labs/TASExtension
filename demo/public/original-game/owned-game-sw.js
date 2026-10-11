// User-authorized, disk-backed assets only. This URL namespace never uses fetch.
const PREFIX = '/original-game/owned-game-assets/';
const DB = 'telos-owned-installations-v1';
const UUID = /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/;
const installations = new Map();
const revoked = new Set();
function database() {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(DB, 1);
    request.onupgradeneeded = () => request.result.createObjectStore('sessions');
    request.onsuccess = () => resolve(request.result); request.onerror = () => reject(request.error);
  });
}
async function stored(session, value, remove = false) {
  const db = await database();
  try {
    return await new Promise((resolve, reject) => {
      const tx = db.transaction('sessions', value !== undefined || remove ? 'readwrite' : 'readonly');
      const store = tx.objectStore('sessions');
      const request = remove ? store.delete(session) : value !== undefined ? store.put(value, session) : store.get(session);
      let result; request.onsuccess = () => { result = request.result; };
      tx.oncomplete = () => resolve(result); tx.onerror = () => reject(tx.error); tx.onabort = () => reject(tx.error);
    });
  } finally { db.close(); }
}
addEventListener('install', event => event.waitUntil(skipWaiting()));
addEventListener('activate', event => event.waitUntil(clients.claim()));
addEventListener('message', event => event.waitUntil((async () => {
  try {
    const { type, session, files } = event.data || {};
    if (!event.source || !UUID.test(session)) throw new Error('Invalid local asset session.');
    if (type === 'release') { revoked.add(session); installations.delete(session); await stored(session, undefined, true); }
    else if (type === 'authorize') {
      if (!Array.isArray(files) || files.length < 3 || files.length > 100000) throw new Error('Invalid installation.');
      const names = new Set();
      for (let index = 0; index < files.length; index++) {
        const entry = files[index];
        const label = typeof entry?.name === 'string' ? entry.name.slice(0, 180) : `entry ${index}`;
        if (typeof entry?.name !== 'string' || entry.name.length > 1024) throw new Error(`Invalid installation asset ${label}: missing or oversized path.`);
        if (!Number.isSafeInteger(entry.bytes) || entry.bytes < 0) throw new Error(`Invalid installation asset ${label}: invalid file size (${String(entry.bytes)}).`);
        if (entry.name.includes('\\')) throw new Error(`Invalid installation asset ${label}: path contains a backslash.`);
        if (entry.name.split('/').some(p => !p || p === '.' || p === '..' || /[\x00-\x1f]/.test(p))) throw new Error(`Invalid installation asset ${label}: unsafe path component.`);
        const name = entry.name.toLowerCase(); if (names.has(name)) throw new Error('Conflicting installation paths.'); names.add(name);
      }
      // The source page retains File snapshots and delivers them directly to
      // the runtime iframe on demand. Persist only metadata here; passing all
      // blobs through a SharedWorker duplicates work and can stall startup.
      const descriptors = files.map(({ name, bytes, modified }) => ({ name, bytes, modified }));
      revoked.delete(session); await stored(session, { files: descriptors });
      if (!revoked.has(session)) installations.set(session, descriptors);
    } else throw new Error('Unsupported local asset operation.');
    event.ports[0]?.postMessage({ ok: true });
  } catch (error) { event.ports[0]?.postMessage({ ok: false, error: error.message }); }
})()));
async function installation(session) {
  if (revoked.has(session)) return null;
  if (installations.has(session)) return installations.get(session);
  const record = await stored(session);
  if (!record || revoked.has(session)) return null;
  // Only the manifest needs to survive worker restarts. Reads are fulfilled
  // from File snapshots that the parent page sends directly to the iframe.
  installations.set(session, record.files);
  return record.files;
}
addEventListener('fetch', event => {
  const url = new URL(event.request.url);
  if (url.origin !== location.origin || !url.pathname.startsWith(PREFIX)) return;
  event.respondWith((async () => {
    const headers = { 'Cache-Control': 'no-store', 'Cross-Origin-Resource-Policy': 'same-origin' };
    const match = /^\/original-game\/owned-game-assets\/([^/]+)\/(manifest|\d+)$/.exec(url.pathname);
    if (!match || !UUID.test(match[1]) || !['GET', 'HEAD'].includes(event.request.method)) return new Response('Invalid local asset request.', { status: 400, headers });
    const session = match[1];
    const files = await installation(session);
    if (!files) return new Response('Select your game folder again.', { status: 410, headers });
    if (match[2] === 'manifest') {
      headers['Content-Type'] = 'application/json';
      const manifest = { schema: 'telos.owned-game-assets/v1', files: files.map(({ name, file, bytes }, index) => ({ name, bytes: file?.size ?? bytes, url: `${PREFIX}${session}/${index}` })), writable: ['swkotor.ini', 'saves', 'logs', 'currentgame', 'gameinprogress', 'temp', 'tempclient', 'localvault', 'dmvault', 'servervault'] };
      return new Response(event.request.method === 'HEAD' ? null : JSON.stringify(manifest), { headers });
    }
    const entry = files[Number(match[2])];
    // This endpoint is retained for an installation backed by a file handle.
    // File-input sessions are read through the iframe snapshot bridge instead.
    if (entry?.handle && !entry.file) {
      entry.file = await entry.handle.getFile();
      if (entry.file.size !== entry.bytes || entry.file.lastModified !== entry.modified) throw new Error('Installation asset changed; select the folder again.');
    }
    const range = /^bytes=(\d+)-(\d+)$/.exec(event.request.headers.get('Range') || '');
    if (!entry || !range) return new Response('A bounded asset range is required.', { status: 416, headers });
    if (!entry.file) return new Response('Runtime snapshots are served directly from the connected page.', { status: 410, headers });
    if (revoked.has(session)) return new Response('Installation access released.', { status: 410, headers });
    const start = Number(range[1]), end = Number(range[2]);
    if (!Number.isSafeInteger(start) || !Number.isSafeInteger(end) || start < 0 || end < start || end >= entry.file.size || end - start + 1 > 1024 * 1024) return new Response('Invalid asset range.', { status: 416, headers });
    headers['Content-Range'] = `bytes ${start}-${end}/${entry.file.size}`; headers['Content-Length'] = String(end - start + 1); headers['Content-Type'] = 'application/octet-stream';
    return new Response(event.request.method === 'HEAD' ? null : entry.file.slice(start, end + 1), { status: 206, headers });
  })().catch(() => new Response('Local asset access failed; select the folder again.', { status: 409 })));
});
