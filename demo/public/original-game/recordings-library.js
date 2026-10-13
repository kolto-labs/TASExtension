// User-controlled inspection of this origin's existing recording namespace.
// No game control, writes, directory creation, uploads or automatic playback.
const byId = id => document.getElementById(id);
const picker = byId('recording'), status = byId('status');
const receiptStatus = byId('receipt-status'), receiptText = byId('receipt-text');
const previewStatus = byId('preview-status'), video = byId('video');
let revision = 0, listing = 0, receiptRequest = 0, previewRequest = 0, previewUrl = null;
const parentOrigin = (() => {
  try {
    const value = new URL(new URLSearchParams(location.search).get('parentOrigin'));
    return parent !== window && ['127.0.0.1', 'localhost', '[::1]'].includes(value.hostname)
      && value.protocol === location.protocol && value.port === location.port
      && value.href === `${value.origin}/` ? value.origin : null;
  } catch { return null; }
})();
if (parentOrigin) video.style.display = 'none';
const validName = name => /^recording-[0-9a-f-]{36}$/.test(name);
const current = (name, ticket) => ticket === revision && picker.value === name;
function clearPreview() {
  const oldUrl = previewUrl; previewUrl = null;
  video.pause(); video.removeAttribute('src'); video.load();
  if (oldUrl) URL.revokeObjectURL(oldUrl);
}
function selected() {
  revision++; clearPreview(); receiptText.textContent = '';
  if (parentOrigin) parent.postMessage({ schema:'telos.recording-preview/v1', action:'clear' }, parentOrigin);
  receiptStatus.textContent = 'View the selected receipt to inspect completion and provenance.';
  previewStatus.textContent = 'Open a saved footage snapshot. Playback is independent of the running game.';
  const enabled = validName(picker.value);
  byId('receipt').disabled = byId('preview').disabled = !enabled;
}
async function directory(name) {
  if (!validName(name)) throw new Error('Choose a recording from the list.');
  if (typeof navigator.storage?.getDirectory !== 'function') throw new Error('Browser recording storage is unavailable.');
  const root = await navigator.storage.getDirectory();
  return (await root.getDirectoryHandle('kptas-recordings')).getDirectoryHandle(name);
}
byId('refresh').addEventListener('click', async () => {
  const ticket = ++listing, previous = picker.value;
  status.textContent = 'Reading recording directories…';
  try {
    const root = await navigator.storage.getDirectory();
    const records = await root.getDirectoryHandle('kptas-recordings'), names = [];
    for await (const [name, handle] of records.entries()) {
      if (handle.kind === 'directory' && validName(name)) names.push(name);
      if (names.length >= 1000) break;
    }
    if (ticket !== listing) return;
    names.sort();
    picker.replaceChildren(...names.map(name => {
      const option = document.createElement('option'); option.value = name; option.textContent = name; return option;
    }));
    picker.disabled = names.length === 0;
    if (names.includes(previous)) picker.value = previous;
    selected();
    status.textContent = `${names.length} recording directories found${names.length === 1000 ? ' (list limit reached)' : ''}. Inspect receipts and footage separately.`;
  } catch (error) {
    if (ticket === listing) status.textContent = error.name === 'NotFoundError' ? 'No recording directory was found on this origin.' : `Could not list recordings: ${String(error.message).slice(0,240)}`;
  }
});
picker.addEventListener('change', selected);
byId('receipt').addEventListener('click', async () => {
  const name = picker.value, ticket = revision, request = ++receiptRequest;
  receiptStatus.textContent = 'Reading the retained receipt…';
  try {
    const file = await (await (await directory(name)).getFileHandle('receipt.json')).getFile();
    if (file.size > 65536) throw new Error('Receipt exceeds the 64 KiB inspection limit.');
    const bytes = await file.arrayBuffer();
    if (bytes.byteLength !== file.size) throw new Error('Receipt changed during the snapshot read. Read it again.');
    const data = JSON.parse(new TextDecoder('utf-8', {fatal:true}).decode(bytes));
    if (!data || Array.isArray(data) || data.schema !== 'telos.game-recording/v1') throw new Error('Receipt schema is unsupported.');
    if (!current(name, ticket) || request !== receiptRequest) return;
    receiptText.textContent = JSON.stringify(data, null, 2);
    if (parentOrigin) {
      parent.postMessage({schema:'telos.recording-receipt/v1', action:'open', name,
        file:new File([bytes], 'receipt.json', {type:'application/json'})}, parentOrigin);
    }
    receiptStatus.textContent = data.complete === true
      ? `${name}: the writer reported finalization. Playback, frame alignment and run qualification remain separate.`
      : data.complete === false && typeof data.endedAt === 'string' && Number.isFinite(Date.parse(data.endedAt))
      ? `${name}: the receipt reports an ended, incomplete recording. ${typeof data.reason === 'string' ? data.reason.slice(0,240) : 'No interruption reason was recorded.'} Playback and run qualification remain separate.`
      : `${name}: the writer did not report finalization. This may be ongoing or interrupted footage.`;
  } catch (error) {
    if (current(name, ticket) && request === receiptRequest) receiptStatus.textContent = `Receipt unavailable; saved files remain unchanged: ${String(error.message).slice(0,240)}`;
  }
});
byId('preview').addEventListener('click', async () => {
  const name = picker.value, ticket = revision, request = ++previewRequest;
  previewStatus.textContent = 'Reading the saved footage snapshot…';
  try {
    const file = await (await (await directory(name)).getFileHandle('game.webm')).getFile();
    if (!file.size) throw new Error('No footage bytes have been saved.');
    if (file.size > 64 * 1024 * 1024) throw new Error('Footage exceeds the 64 MiB in-memory preview limit. Retained footage is unchanged.');
    // Detach the preview from the recording file before the writer appends again.
    // A concurrent file change may refuse this read; leave the writer untouched.
    const bytes = await file.arrayBuffer();
    if (bytes.byteLength !== file.size) throw new Error('Footage changed during the snapshot read. Open another snapshot.');
    const snapshot = new File([bytes], 'game.webm', { type:'video/webm' });
    if (!current(name, ticket) || request !== previewRequest) return;
    if (parentOrigin) {
      parent.postMessage({ schema:'telos.recording-preview/v1', action:'open', name, file:snapshot }, parentOrigin);
      previewStatus.textContent = `${name}: ${file.size.toLocaleString()} retained bytes sent to the local workspace preview below this library. Use its Play control to inspect footage; no playback or finalization is implied.`;
      return;
    }
    clearPreview(); previewUrl = URL.createObjectURL(snapshot); video.muted = true; video.src = previewUrl;
    previewStatus.textContent = `${name}: ${file.size.toLocaleString()} retained bytes opened. Use Play to inspect footage; an unfinalized snapshot may be incomplete.`;
  } catch (error) {
    if (current(name, ticket) && request === previewRequest) previewStatus.textContent = `Footage unavailable; saved files remain unchanged: ${String(error.message).slice(0,240)}`;
  }
});
video.addEventListener('loadedmetadata', () => {
  if (!previewUrl) return;
  const duration = Number.isFinite(video.duration) ? `${video.duration.toFixed(2)} seconds` : 'duration unavailable';
  previewStatus.textContent = `${picker.value}: browser opened ${video.videoWidth}×${video.videoHeight} video metadata, ${duration}. Playback and audible contents are not established by metadata.`;
});
video.addEventListener('error', () => {
  if (!previewUrl) return;
  const reasons = {1:'playback aborted', 2:'media loading failed', 3:'media decoding failed', 4:'media source unavailable or unsupported'};
  const code = video.error?.code;
  previewStatus.textContent = `Footage could not open: ${reasons[code] || 'unknown media error'}${code ? ` (code ${code})` : ''}. Saved files remain unchanged. This does not establish whether the retained container is valid.`;
});
window.addEventListener('pagehide', clearPreview);
