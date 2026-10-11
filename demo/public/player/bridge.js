// This document runs on the player origin, never the installation-file origin.
const config = new URLSearchParams(location.search);
const parentOrigin = config.get('parent');
const session = config.get('session');
const video = config.get('video');
const UUID = /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/;
let player, ready = false, stopped = true, selectedQuality = false;
function send(value) { parent.postMessage({ ...value, session }, parentOrigin); }
function sourceQuality() {
  if (selectedQuality || !ready) return;
  const qualities = player.getQualities();
  if (Array.isArray(qualities) && qualities.some(q => q === 'chunked' || q?.group === 'chunked')) { player.setQuality('chunked'); selectedQuality = true; }
}
function state() {
  if (!ready) return;
  sourceQuality();
  const stats = player.getPlaybackStats();
  send({ type:'telos-player/state', ready, paused:player.isPaused(), time:player.getCurrentTime(), duration:player.getDuration(), resolution:stats.videoResolution || '', fps:Number.isFinite(stats.fps) && stats.fps >= 0 ? stats.fps : null, buffer:Number.isFinite(stats.bufferSize) ? Math.max(0,stats.bufferSize) : null, codecs:typeof stats.codecs === 'string' ? stats.codecs.slice(0,128) : '' });
}
if (!parentOrigin || new URL(parentOrigin).origin !== parentOrigin || parentOrigin === location.origin || !UUID.test(session) || !/^\d{1,18}$/.test(video)) {
  document.body.textContent = 'Invalid source-player configuration.';
} else if (!window.Twitch?.Player) {
  send({ type:'telos-player/error', error:'The source player could not load.' });
} else {
  const isolation = new MutationObserver(() => {
    const frame = document.querySelector('#player iframe');
    if (frame && !frame.hasAttribute('credentialless')) { frame.setAttribute('credentialless',''); if (frame.src) frame.src = frame.src; }
  });
  isolation.observe(document.querySelector('#player'), { childList:true, subtree:true });
  player = new Twitch.Player('player', { video:`v${video}`, parent:[location.hostname], width:'100%', height:'100%', autoplay:false, muted:false });
  player.addEventListener(Twitch.Player.READY, () => { ready = true; state(); });
  for (const event of [Twitch.Player.PLAY, Twitch.Player.PLAYING]) player.addEventListener(event, () => { stopped = false; state(); });
  for (const event of [Twitch.Player.PAUSE, Twitch.Player.ENDED, Twitch.Player.PLAYBACK_BLOCKED]) player.addEventListener(event, () => { stopped = true; state(); });
  player.addEventListener(Twitch.Player.SEEK, state);
  const poll = setInterval(state, 250);
  addEventListener('pagehide', () => { clearInterval(poll); isolation.disconnect(); }, {once:true});
  addEventListener('message', event => {
    const value = event.data;
    if (event.source !== parent || event.origin !== parentOrigin || value?.session !== session || value.type !== 'telos-player/command' || !UUID.test(value.request)) return;
    try {
      if (!ready) throw new Error('The source player is not ready.');
      if (value.action === 'play') player.play();
      else if (value.action === 'pause') player.pause();
      else if (value.action === 'seek' && Number.isFinite(value.value)) player.seek(Math.max(0,Math.min(value.value,player.getDuration())));
      else throw new Error('Unsupported source-player command.');
      send({ type:'telos-player/ack', request:value.request, ok:true }); state();
    } catch (error) { send({ type:'telos-player/ack', request:value.request, ok:false, error:error.message }); }
  });
}
