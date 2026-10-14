/* User-started local recording storage. No network requests or game control. */
let media = null, receipt = null, offset = 0, chunks = 0, metadata = null;
let failed = false;

function writeAll(handle, bytes, at) {
  let written = 0;
  while (written < bytes.byteLength) {
    const count = handle.write(bytes.subarray(written), {at:at + written});
    if (!Number.isSafeInteger(count) || count <= 0 || count > bytes.byteLength - written) {
      throw new Error('Browser storage did not finish the recording write.');
    }
    written += count;
  }
}

function saveReceipt(complete, reason = null) {
  const bytes = new TextEncoder().encode(JSON.stringify({...metadata,
    complete, reason, bytes:offset, chunks, updatedAt:new Date().toISOString()}));
  writeAll(receipt, bytes, 0);
  receipt.truncate(bytes.byteLength);
  receipt.flush();
}

self.onmessage = async event => {
  const request = event.data;
  if (!request || !Number.isSafeInteger(request.id) || request.id < 1) return;
  try {
    if (request.operation === 'open') {
      if (media || !/^recording-[0-9a-f-]{36}$/.test(request.name || '')) throw new Error('Invalid recording storage request.');
      const root = await navigator.storage.getDirectory();
      const recordings = await root.getDirectoryHandle('kptas-recordings', {create:true});
      // The random directory is created once. Existing data is never replaced.
      try {
        await recordings.getDirectoryHandle(request.name);
        throw new Error('This recording directory already exists.');
      } catch (error) { if (error.name !== 'NotFoundError') throw error; }
      const directory = await recordings.getDirectoryHandle(request.name, {create:true});
      media = await (await directory.getFileHandle('game.webm', {create:true})).createSyncAccessHandle();
      receipt = await (await directory.getFileHandle('receipt.json', {create:true})).createSyncAccessHandle();
      // The storage owner binds the receipt to the directory it created.
      // Runtime/context declarations remain separate and are not qualified here.
      metadata = {...request.metadata, recording:request.name};
      saveReceipt(false, 'Recording has not been finalized.');
    } else if (request.operation === 'chunk') {
      if (!media || failed || !(request.bytes instanceof ArrayBuffer) ||
          !request.bytes.byteLength || request.bytes.byteLength > 32 * 1024 * 1024) {
        throw new Error('Invalid recording chunk.');
      }
      const bytes = new Uint8Array(request.bytes);
      let written = 0;
      while (written < bytes.byteLength) {
        const count = media.write(bytes.subarray(written), {at:offset});
        if (!Number.isSafeInteger(count) || count <= 0 || count > bytes.byteLength - written) {
          throw new Error('Browser storage did not finish the recording write.');
        }
        offset += count;
        written += count;
      }
      media.flush();
      chunks++;
      saveReceipt(false, 'Recording has not been finalized.');
    } else if (request.operation === 'close') {
      if (!media || !receipt) throw new Error('Recording storage is not open.');
      metadata = {...metadata, endedAt:request.endedAt,
        captureDiagnostics:request.captureDiagnostics || null,
        captureVideoDiagnostics:request.captureVideoDiagnostics || null,
        durationMs:Number.isSafeInteger(request.durationMs) && request.durationMs >= 0 ? request.durationMs : null};
      media.flush();
      saveReceipt(request.complete === true && !failed && chunks > 0,
        failed ? 'A storage write failed.' : request.reason || null);
      media.close(); receipt.close(); media = receipt = null;
    } else throw new Error('Unsupported recording storage operation.');
    self.postMessage({id:request.id, ok:true, bytes:offset, chunks});
  } catch (error) {
    failed = true;
    try { if (media) media.flush(); if (receipt) saveReceipt(false, 'A storage write failed.'); } catch (_) {}
    self.postMessage({id:request.id, ok:false, error:String(error.message || error).slice(0,240)});
  }
};
