import { DEBUG_REPLY_SCHEMA, DebugOperationError } from '../public/packages/debug-runtime/runtime.js';

/** Opt-in handler for a host router. Authorization and target ownership stay with the host. */
export function createDebugApiHandler({ runtime, authorize, allowedOrigin, maximumBodyBytes = 1048576 }) {
  if (typeof runtime?.request !== 'function' || typeof authorize !== 'function'
    || typeof allowedOrigin !== 'string' || new URL(allowedOrigin).origin !== allowedOrigin
    || !Number.isSafeInteger(maximumBodyBytes) || maximumBodyBytes < 1024 || maximumBodyBytes > 4194304) {
    throw new TypeError('Debug API requires a runtime, authorizer, exact origin, and bounded request size.');
  }
  const reply = (body, status) => Response.json(body, { status, headers: {
    'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
  } });
  return async request => {
    let id = null;
    try {
      if (request.method !== 'POST') return reply({ code: 'DEBUG_METHOD_REQUIRED' }, 405);
      if (request.headers.get('Origin') !== allowedOrigin) return reply({ code: 'DEBUG_ORIGIN_REJECTED' }, 403);
      if (!/^application\/json(?:\s*;|$)/i.test(request.headers.get('Content-Type') || '')) return reply({ code: 'DEBUG_JSON_REQUIRED' }, 415);
      if (!await authorize(request)) return reply({ code: 'DEBUG_AUTH_REQUIRED' }, 401);
      if (Number(request.headers.get('Content-Length')) > maximumBodyBytes) return reply({ code: 'DEBUG_BODY_TOO_LARGE' }, 413);
      const reader = request.body?.getReader();
      if (!reader) throw new DebugOperationError('DEBUG_REQUEST_INVALID', 'Debugger request has no body.');
      const chunks = []; let bytes = 0;
      try {
        for (;;) {
          const { value, done } = await reader.read(); if (done) break;
          bytes += value.byteLength;
          if (bytes > maximumBodyBytes) { await reader.cancel(); return reply({ code: 'DEBUG_BODY_TOO_LARGE' }, 413); }
          chunks.push(value);
        }
      } finally { reader.releaseLock(); }
      const body = new Uint8Array(bytes); let offset = 0;
      for (const chunk of chunks) { body.set(chunk, offset); offset += chunk.byteLength; }
      let envelope;
      try { envelope = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(body)); }
      catch { throw new DebugOperationError('DEBUG_REQUEST_INVALID', 'Debugger body must be UTF-8 JSON.'); }
      id = typeof envelope?.id === 'string' && envelope.id.length <= 128 ? envelope.id : null;
      return reply(await runtime.request(envelope, { signal: request.signal }), 200);
    } catch (error) {
      const known = error instanceof DebugOperationError;
      const code = known ? error.code : 'DEBUG_BACKEND_FAILED';
      const status = code === 'DEBUG_UNSUPPORTED' ? 501 : code === 'DEBUG_SESSION_CHANGED' ? 409
        : code === 'DEBUG_NOT_READY' ? 503 : code === 'DEBUG_CANCELLED' ? 408 : known ? 400 : 502;
      return reply({ schema: DEBUG_REPLY_SCHEMA, id, error: {
        code, message: known ? error.message : 'Debugger backend failed; inspect the server diagnostic receipt.',
        ...(known ? { details: error.details } : {}),
      } }, status);
    }
  };
}
