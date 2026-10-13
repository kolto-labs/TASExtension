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
  const finalizedExport = document.createElement('section');
  finalizedExport.setAttribute('aria-label', 'Finalized recording download');
  const finalizedStatus = document.createElement('p'); finalizedStatus.setAttribute('role', 'status');
  finalizedStatus.textContent = 'Prepare finalized footage in the library to download it.';
  const downloadFinalized = document.createElement('button');
  downloadFinalized.textContent = 'Download finalized footage'; downloadFinalized.disabled = true;
  const viewFinalized = document.createElement('button');
  viewFinalized.textContent = 'View finalized footage'; viewFinalized.disabled = true;
  finalizedExport.append(finalizedStatus, viewFinalized, downloadFinalized);
  root.replaceChildren(note, frame, receiptExport, finalizedExport, preview);
  let selected = null, previewUrl = null;
  let receiptName = null, receiptUrl = null;
  let reportedSecond = -1;
  let finalizedName = null, finalizedUrl = null, finalizedFile = null;
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
  function clearFinalized() {
    if (finalizedUrl) URL.revokeObjectURL(finalizedUrl);
    finalizedName = finalizedUrl = finalizedFile = null; downloadFinalized.disabled = viewFinalized.disabled = true;
    finalizedStatus.textContent = 'Prepare finalized footage in the library to download it.';
  }
  function clear() { clearPreview(); clearReceipt(); clearFinalized(); }
  function download(url, name) {
    if (!url || !name) return;
    const link = document.createElement('a');
    link.href = url; link.download = name;
    document.body.append(link); link.click(); link.remove();
  }
  downloadFootage.addEventListener('click', () => download(previewUrl, `${selected}-snapshot.webm`));
  downloadReceipt.addEventListener('click', () => download(receiptUrl, `${receiptName}-receipt-snapshot.json`));
  downloadFinalized.addEventListener('click', () => download(finalizedUrl, `${finalizedName}.webm`));
  viewFinalized.addEventListener('click', () => {
    if (!finalizedFile || !finalizedName) return;
    clearPreview(); selected = finalizedName;
    previewUrl = URL.createObjectURL(finalizedFile);
    video.muted = true; video.src = previewUrl; preview.hidden = false;
    downloadFootage.disabled = false;
    status.textContent = `${selected}: ${finalizedFile.size.toLocaleString()} retained bytes opened from the finalized file. Use Play to inspect footage; audio, frame alignment and complete-run qualification remain separate.`;
  });
  const receive = event => {
    if (event.source !== frame.contentWindow || event.origin !== url.origin
      || !['telos.recording-preview/v1','telos.recording-receipt/v1','telos.recording-finalized-download/v1'].includes(event.data?.schema)) return;
    if (event.data.action === 'clear') { clear(); return; }
    const { action, name, file } = event.data;
    if (action !== 'open' || !/^recording-[0-9a-f-]{36}$/.test(name)
      || !(file instanceof File) || !file.size || !Number.isSafeInteger(file.size)) return;
    if (event.data.schema === 'telos.recording-finalized-download/v1') {
      const receipt = event.data.receipt;
      if (file.type !== 'video/webm' || file.name !== 'game.webm'
        || receipt?.schema !== 'telos.game-recording/v1' || receipt.complete !== true
        || !Number.isSafeInteger(receipt.bytes) || receipt.bytes !== file.size
        || !Number.isSafeInteger(receipt.chunks) || receipt.chunks <= 0
        || typeof receipt.endedAt !== 'string' || !Number.isFinite(Date.parse(receipt.endedAt))) return;
      if (selected && selected !== name) clearPreview();
      if (receiptName && receiptName !== name) clearReceipt();
      clearFinalized(); finalizedName = name; finalizedFile = file; finalizedUrl = URL.createObjectURL(file);
      downloadFinalized.disabled = viewFinalized.disabled = false;
      finalizedStatus.textContent = `${name}: ${file.size.toLocaleString()} retained bytes ready to download. The receipt reports finalization; playback, audio and complete-run qualification remain separate.`;
      return;
    }
    if (finalizedName && finalizedName !== name) clearFinalized();
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
  function renderPlaybackPosition(force = false) {
    if (!previewUrl || !Number.isFinite(video.currentTime)) return;
    const second = Math.floor(video.currentTime);
    if (!force && second === reportedSecond) return;
    reportedSecond = second;
    status.textContent = `${selected}: ${video.paused ? 'Paused' : 'Playing'} at ${second} seconds, ${video.videoWidth}×${video.videoHeight}; ${video.muted ? 'muted' : 'unmuted'}. This playback position does not qualify complete footage, audible content or frame alignment.`;
  }
  video.addEventListener('timeupdate', () => renderPlaybackPosition());
  for (const event of ['play','pause','volumechange','seeked']) video.addEventListener(event, () => renderPlaybackPosition(true));
  addEventListener('message', receive);
  const dispose = () => { removeEventListener('message', receive); clear(); };
  addEventListener('pagehide', dispose, { once:true });
  return dispose;
}
