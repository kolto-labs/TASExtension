import { WINE_ROOT } from '../tools/wine-root-contract.mjs';

const base = `/original-game/wine-roots/${WINE_ROOT.sha256}/`;
function headers() {
  return new Headers({
    'Content-Type':'application/zip', 'Accept-Ranges':'bytes',
    'ETag':`"${WINE_ROOT.sha256}"`, 'Cache-Control':'public, max-age=31536000, immutable',
    'Cross-Origin-Resource-Policy':'same-origin', 'Cross-Origin-Embedder-Policy':'credentialless',
    'X-Content-Type-Options':'nosniff',
  });
}
export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    // Local snapshots are deliberately excluded from public uploads.
    if (url.pathname.startsWith('/original-game/wine-roots/') && url.pathname.endsWith('/.local-root.zip')) return new Response('Not found', { status:404 });
    if (url.pathname !== base + 'wine-root.zip') {
      if (url.pathname.startsWith('/original-game/wine-roots/') && url.pathname.endsWith('/wine-root.zip')) return new Response('Not found', { status:404 });
      return env.ASSETS.fetch(request);
    }
    if (!['GET','HEAD'].includes(request.method)) return new Response('Method not allowed', { status:405, headers:{Allow:'GET, HEAD'} });
    const responseHeaders = headers();
    // HEAD has no producer/body. Do not create an unwritten fixed stream:
    // outgoing Response bodies are pumped even when HTTP suppresses bytes.
    // Payload-derived Content-Length may be omitted for HEAD. Range applies
    // to GET only; the facade still identifies the full archive with its ETag.
    if (request.method === 'HEAD') return new Response(null, { headers:responseHeaders });
    let start = 0, end = WINE_ROOT.bytes - 1, partial = false;
    const range = request.headers.get('Range');
    const ifRange = request.headers.get('If-Range');
    if (range && (!ifRange || ifRange === responseHeaders.get('ETag'))) {
      const match = /^bytes=(\d*)-(\d*)$/.exec(range);
      if (match && (match[1] || match[2])) {
        if (!match[1]) { const count = Number(match[2]); start = Math.max(0, WINE_ROOT.bytes - count); if (count === 0) start = WINE_ROOT.bytes; }
        else { start = Number(match[1]); if (match[2]) end = Math.min(end, Number(match[2])); }
      } else start = WINE_ROOT.bytes;
      if (!Number.isSafeInteger(start) || !Number.isSafeInteger(end) || start > end || start >= WINE_ROOT.bytes) {
        responseHeaders.set('Content-Range', `bytes */${WINE_ROOT.bytes}`);
        return new Response(null, { status:416, headers:responseHeaders });
      }
      partial = true;
      responseHeaders.set('Content-Range', `bytes ${start}-${end}/${WINE_ROOT.bytes}`);
    }
    responseHeaders.set('Content-Length', String(end - start + 1));
    const manifestResponse = await env.ASSETS.fetch(new Request(new URL(base + 'chunks.json', url), { method:'GET' }));
    if (!manifestResponse.ok) return new Response('Wine root package unavailable', { status:503 });
    const manifest = await manifestResponse.json();
    const chunks = manifest.chunks;
    if (manifest.sha256 !== WINE_ROOT.sha256 || manifest.bytes !== WINE_ROOT.bytes || manifest.chunkBytes !== WINE_ROOT.chunkBytes || !Array.isArray(chunks) || chunks.length !== Math.ceil(WINE_ROOT.bytes / WINE_ROOT.chunkBytes) || chunks.some((chunk, i) => chunk.name !== `${i}-${chunk.sha256}.bin` || !/^[0-9a-f]{64}$/.test(chunk.sha256) || chunk.bytes !== Math.min(WINE_ROOT.chunkBytes, WINE_ROOT.bytes - i * WINE_ROOT.chunkBytes))) return new Response('Wine root package invalid', { status:503 });
    let position = start;
    let cancelled = false;
    // One bounded chunk per pull provides backpressure. The archive is never
    // assembled in Worker memory; the unchanged BoxedWine loader consumes it.
    const stream = new ReadableStream({
      async pull(controller) {
        try {
          if (cancelled) return;
          const index = Math.floor(position / WINE_ROOT.chunkBytes);
          const chunk = chunks[index];
          const response = await env.ASSETS.fetch(new Request(new URL(base + chunk.name, url), { method:'GET' }));
          if (response.status !== 200) throw new Error('Wine root chunk unavailable');
          const bytes = new Uint8Array(await response.arrayBuffer());
          if (cancelled) return;
          if (bytes.length !== chunk.bytes) throw new Error('Wine root chunk length mismatch');
          const offset = position % WINE_ROOT.chunkBytes;
          const count = Math.min(bytes.length - offset, end - position + 1);
          controller.enqueue(bytes.subarray(offset, offset + count));
          position += count;
          if (position > end) controller.close();
        } catch (error) { if (!cancelled) controller.error(error); }
      },
      cancel() { cancelled = true; },
    });
    const fixed = new FixedLengthStream(end - start + 1);
    // A chunk failure rejects the consumer stream through pipeTo's abort;
    // consume its rejection here so it cannot become an unhandled exception.
    stream.pipeTo(fixed.writable).catch(() => {});
    return new Response(fixed.readable, { status:partial ? 206 : 200, headers:responseHeaders });
  },
};
