// A prepared metadata file is downloaded only by an explicit workspace click.
export function mountAudioObservationDownload() {
  const root = document.createElement('section'); root.hidden = true;
  root.setAttribute('aria-label', 'Audio observation download');
  const status = document.createElement('p'); status.setAttribute('role', 'status');
  const button = document.createElement('button'); button.textContent = 'Download audio observation';
  button.disabled = true; root.append(status, button);
  let session = null, ticket = 0, url = null, name = null, disposed = false;
  function clear() {
    ++ticket; if (url) URL.revokeObjectURL(url);
    url = name = null; button.disabled = true; root.hidden = true; status.textContent = '';
  }
  button.addEventListener('click', () => {
    if (!url || !name || disposed) return;
    const link = document.createElement('a'); link.href = url; link.download = name;
    document.body.append(link); link.click(); link.remove();
  });
  return {
    root,
    setSession(value) { if (value !== session) { clear(); session = value; } },
    async prepare(file, ownerSession, recording) {
      if (disposed || ownerSession !== session || !(file instanceof File)
        || file.name !== 'audio-observation.json' || file.type !== 'application/json'
        || !Number.isSafeInteger(file.size) || file.size < 1 || file.size > 16384
        || typeof recording !== 'string' || !/^recording-[0-9a-f-]{36}$/.test(recording)) return false;
      const request = ++ticket;
      try {
        const bytes = await file.arrayBuffer();
        const data = JSON.parse(new TextDecoder('utf-8', {fatal:true}).decode(bytes));
        if (bytes.byteLength !== file.size || data?.schema !== 'telos.recording-audio-display-capture/v1'
          || data.recording !== recording || data.runtimeContext?.schema !== 'telos.game-recording-context/v1'
          || data.runtimeContext.source !== 'runtime-document-url'
          || data.runtimeContext.declaredSession !== ownerSession
          || data.runtimeContext.executableIdentityVerified !== false
          || data.observation?.schema !== 'telos.recording-audio-observation/v1'
          || typeof data.observation.observedAt !== 'string' || data.observation.observedAt.length > 64
          || !Number.isFinite(Date.parse(data.observation.observedAt))) return false;
        if (disposed || request !== ticket || ownerSession !== session) return false;
        const prepared = URL.createObjectURL(new Blob([bytes], {type:'application/json'}));
        if (url) URL.revokeObjectURL(url);
        url = prepared; name = `${recording}-audio-observation.json`;
        button.disabled = false; root.hidden = false;
        status.textContent = `${recording}: audio observation from ${data.observation.observedAt} is ready to download. It does not prove audible playback or encoded audio.`;
        return true;
      } catch {
        if (!disposed && request === ticket && ownerSession === session) {
          root.hidden = false;
          status.textContent = 'The audio observation could not be prepared. Any previous download remains available.';
        }
        return false;
      }
    },
    dispose() { disposed = true; clear(); session = null; },
  };
}
