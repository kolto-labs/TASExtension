// Local recording inspection and snapshot downloads. No game session or uploads.
export function mountRecordingLibrary(root) {
  const url = new URL('/original-game/recordings.html', location.href);
  if (location.hostname === '127.0.0.1') url.hostname = 'localhost';
  url.searchParams.set('parentOrigin', location.origin);
  const note = document.createElement('p');
  note.textContent = 'Saved recording inspection. This view does not start or control a game session.';
  const frame = document.createElement('iframe'); frame.title = 'Saved game recordings';
  frame.src = url.href; frame.className = 'original-game-frame';
  frame.style.width = '100%'; frame.style.height = '60vh';
  const preview = document.createElement('section'); preview.hidden = true;
  preview.setAttribute('aria-label', 'Retained footage snapshot');
  const heading = document.createElement('h2'); heading.textContent = 'Retained footage snapshot';
  const status = document.createElement('p'); status.setAttribute('role', 'status');
  const video = document.createElement('video'); video.controls = true;
  video.muted = true; video.preload = 'metadata'; video.style.width = '100%';
  video.style.maxHeight = '600px';
  const downloadFootage = document.createElement('button');
  downloadFootage.textContent = 'Download footage snapshot'; downloadFootage.disabled = true;
  const receiptExport = document.createElement('section');
  receiptExport.setAttribute('aria-label', 'Receipt snapshot download');
  const receiptStatus = document.createElement('p'); receiptStatus.setAttribute('role', 'status');
  receiptStatus.textContent = 'Read a receipt in the library to prepare its download.';
  const downloadReceipt = document.createElement('button');
  downloadReceipt.textContent = 'Download receipt snapshot'; downloadReceipt.disabled = true;
  receiptExport.append(receiptStatus, downloadReceipt);
  preview.append(heading, status, video, downloadFootage);
  root.replaceChildren(note, frame, receiptExport, preview);
  let selected = null, previewUrl = null;
  let receiptName = null, receiptUrl = null;
  let reportedSecond = -1;
  function clearPreview() {
    const old = previewUrl; previewUrl = null;
    video.pause(); video.removeAttribute('src'); video.load();
    if (old) URL.revokeObjectURL(old);
    selected = null; reportedSecond = -1; preview.hidden = true;
    downloadFootage.disabled = true;
  }
  function clearReceipt() {
    if (receiptUrl) URL.revokeObjectURL(receiptUrl);
    receiptUrl = receiptName = null; downloadReceipt.disabled = true;
    receiptStatus.textContent = 'Read a receipt in the library to prepare its download.';
  }
  function clear() { clearPreview(); clearReceipt(); }
  function download(url, name) {
    if (!url || !name) return;
    const link = document.createElement('a');
    link.href = url; link.download = name;
    document.body.append(link); link.click(); link.remove();
  }
  downloadFootage.addEventListener('click', () => download(previewUrl, `${selected}-snapshot.webm`));
  downloadReceipt.addEventListener('click', () => download(receiptUrl, `${receiptName}-receipt-snapshot.json`));
  const receive = event => {
    if (event.source !== frame.contentWindow || event.origin !== url.origin
      || !['telos.recording-preview/v1','telos.recording-receipt/v1'].includes(event.data?.schema)) return;
    if (event.data.action === 'clear') { clear(); return; }
    const { action, name, file } = event.data;
    if (action !== 'open' || !/^recording-[0-9a-f-]{36}$/.test(name)
      || !(file instanceof File) || !file.size || !Number.isSafeInteger(file.size)) return;
    if (event.data.schema === 'telos.recording-receipt/v1') {
      if (file.size > 65536 || file.type !== 'application/json') return;
      if (selected && selected !== name) clearPreview();
      clearReceipt(); receiptName = name; receiptUrl = URL.createObjectURL(file);
      downloadReceipt.disabled = false;
      receiptStatus.textContent = `${name}: ${file.size.toLocaleString()} receipt bytes copied for download. This snapshot does not finalize a recording.`;
      return;
    }
    if (file.size > 64 * 1024 * 1024 || file.type !== 'video/webm') return;
    if (receiptName && receiptName !== name) clearReceipt();
    clearPreview(); selected = name; previewUrl = URL.createObjectURL(file);
    downloadFootage.disabled = false;
    video.muted = true; video.src = previewUrl; preview.hidden = false;
    status.textContent = `${name}: ${file.size.toLocaleString()} retained bytes opened. Use Play to inspect this snapshot. Finalization, audio, frame alignment and complete-run qualification remain separate.`;
  };
  video.addEventListener('loadedmetadata', () => {
    if (!previewUrl) return;
    const duration = Number.isFinite(video.duration) ? `${video.duration.toFixed(2)} seconds` : 'duration unavailable';
    status.textContent = `${selected}: ${video.videoWidth}×${video.videoHeight}, ${duration}. Metadata alone does not establish playback or audible content.`;
  });
  video.addEventListener('error', () => {
    if (previewUrl) status.textContent = `Snapshot could not open (media code ${video.error?.code || 'unknown'}). Retained files remain unchanged; container validity is unqualified.`;
  });
  video.addEventListener('timeupdate', () => {
    if (!previewUrl || !Number.isFinite(video.currentTime)) return;
    const second = Math.floor(video.currentTime);
    if (second === reportedSecond) return;
    reportedSecond = second;
    status.textContent = `${selected}: ${video.paused ? 'Paused' : 'Playing'} at ${second} seconds, ${video.videoWidth}×${video.videoHeight}; ${video.muted ? 'muted' : 'unmuted'}. This playback position does not qualify complete footage, audible content or frame alignment.`;
  });
  addEventListener('message', receive);
  const dispose = () => { removeEventListener('message', receive); clear(); };
  addEventListener('pagehide', dispose, { once:true });
  return dispose;
}
