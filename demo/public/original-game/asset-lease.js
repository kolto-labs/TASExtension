// File snapshots live on a separate worker, never copied into persistent storage.
// The service worker can recover them while a guest blocks the browser UI in read().
const leases = new Map();
const channel = new BroadcastChannel('telos-owned-file-leases-v1');
channel.onmessage = event => {
  const request = event.data;
  if (request?.type === 'restore-lease' && leases.has(request.session)) channel.postMessage({ type: 'lease-restored', request: request.request, files: leases.get(request.session) });
};
onconnect = event => {
  const port = event.ports[0];
  port.onmessage = event => {
    const { type, session, files, request } = event.data || {};
    if (type === 'authorize' && Array.isArray(files)) { leases.set(session, files); port.postMessage({ request, ok: true }); }
    else if (type === 'release') { leases.delete(session); port.postMessage({ request, ok: true }); }
  };
  port.start();
};
