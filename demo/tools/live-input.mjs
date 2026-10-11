#!/usr/bin/env node
// Capture/replay browser input for the local BoxedWine game frame. This tool
// does not launch or restart a browser/game; the caller supplies a loopback CDP
// endpoint. Raw input is primary; .score output is a lossy, explicitly
// approximate projection onto nominal frames.
import fs from 'node:fs';
import path from 'node:path';
import process from 'node:process';
import { createHash } from 'node:crypto';

const SCHEMA = 'telos.live-input-capture/v1';
const RUNTIME_PATH = /\/game-runtime\.html$/;
const MAX_CAPTURE_BYTES = 128 * 1024 * 1024;

const HELP = `
Usage:
  node live-input.mjs record --cdp http://127.0.0.1:9222 --url http://127.0.0.1:47653/ --out run.raw.jsonl [--hz 60]
  node live-input.mjs replay --cdp http://127.0.0.1:9222 --url http://127.0.0.1:47653/ --in run.raw.jsonl [--delay-start-ms 5000]

Record must be armed on the local prototype before its KOTOR runtime iframe is
created. Press Ctrl+C to stop; raw NDJSON is flushed and a `.projection.score`
is written beside it. Replay sends the raw events through Chrome DevTools input
to the focused game iframe, in real time. It never pauses/steps the TAS.

Requires Node.js 22+ (built-in WebSocket), a dedicated Chrome/Chromium launched
with --remote-debugging-port on loopback, and a local same-origin game iframe.
`;

function options(argv) {
  const [command, ...rest] = argv;
  if (!['record', 'replay'].includes(command)) throw new Error(HELP);
  const o = { command, hz: 60, delayStartMs: 5000 };
  for (let i = 0; i < rest.length; i++) {
    const a = rest[i];
    if (a === '--cdp') o.cdp = rest[++i];
    else if (a === '--url') o.url = rest[++i];
    else if (a === '--out') o.out = rest[++i];
    else if (a === '--in') o.input = rest[++i];
    else if (a === '--hz') o.hz = Number(rest[++i]);
    else if (a === '--delay-start-ms') o.delayStartMs = Number(rest[++i]);
    else if (a === '--help' || a === '-h') throw new Error(HELP);
    else throw new Error(`Unknown option: ${a}\n${HELP}`);
  }
  if (!o.cdp || !o.url || (command === 'record' ? !o.out : !o.input)) throw new Error(HELP);
  if (!Number.isInteger(o.hz) || o.hz < 1 || o.hz > 240) throw new Error('--hz must be an integer from 1 to 240.');
  if (!Number.isFinite(o.delayStartMs) || o.delayStartMs < 0 || o.delayStartMs > 60000) throw new Error('--delay-start-ms must be 0..60000.');
  o.cdpUrl = new URL(o.cdp);
  o.pageUrl = new URL(o.url);
  if (!['127.0.0.1', 'localhost', '[::1]'].includes(o.cdpUrl.hostname) || !['http:', 'https:'].includes(o.cdpUrl.protocol)) {
    throw new Error('Refusing a non-loopback CDP endpoint. Use a dedicated local browser.');
  }
  if (!['127.0.0.1', 'localhost', '[::1]'].includes(o.pageUrl.hostname) || !['http:', 'https:'].includes(o.pageUrl.protocol)) {
    throw new Error('This utility is limited to a local prototype/runtime.');
  }
  return o;
}

class Cdp {
  constructor(ws) {
    this.ws = ws;
    this.nextId = 1;
    this.pending = new Map();
    this.listeners = new Map();
    ws.addEventListener('message', e => {
      let m;
      try { m = JSON.parse(e.data); } catch { return; }
      if (m.id) {
        const p = this.pending.get(m.id);
        if (!p) return;
        this.pending.delete(m.id);
        if (m.error) p.reject(new Error(m.error.message || 'CDP command failed'));
        else p.resolve(m.result || {});
      } else if (m.method) {
        for (const fn of this.listeners.get(m.method) || []) fn(m.params || {});
      }
    });
  }
  static async connect(url) {
    if (typeof WebSocket !== 'function') throw new Error('Node.js 22 or newer is required for its built-in WebSocket client.');
    const ws = new WebSocket(url);
    await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('Timed out connecting to local Chrome DevTools.')), 5000);
      ws.addEventListener('open', () => { clearTimeout(timer); resolve(); }, { once: true });
      ws.addEventListener('error', () => { clearTimeout(timer); reject(new Error('Could not connect to Chrome DevTools.')); }, { once: true });
    });
    return new Cdp(ws);
  }
  on(method, fn) {
    if (!this.listeners.has(method)) this.listeners.set(method, new Set());
    this.listeners.get(method).add(fn);
  }
  call(method, params = {}) {
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.ws.send(JSON.stringify({ id, method, params }));
    });
  }
  notify(method, params = {}) {
    // CDP dispatch is intentionally asynchronous; waiting for an ack between
    // events would stretch the captured wall-time sequence.
    this.ws.send(JSON.stringify({ id: this.nextId++, method, params }));
  }
  close() { try { this.ws.close(); } catch {} }
}

async function getJson(url) {
  const response = await fetch(url, { signal: AbortSignal.timeout(4000) });
  if (!response.ok) throw new Error(`Chrome DevTools endpoint returned HTTP ${response.status}.`);
  return response.json();
}

async function attach(o) {
  let version;
  try { version = await getJson(new URL('/json/version', o.cdpUrl)); }
  catch (e) {
    throw new Error(`No Chrome DevTools endpoint at ${o.cdpUrl.origin}. Start a separate local Chrome with --remote-debugging-port; the current browser is left untouched. (${e.message})`);
  }
  const targets = await getJson(new URL('/json/list', o.cdpUrl));
  const pages = targets.filter(t => t.type === 'page' && t.webSocketDebuggerUrl && t.url);
  const target = pages.find(t => {
    try { const u = new URL(t.url); return u.origin === o.pageUrl.origin && u.pathname === o.pageUrl.pathname; }
    catch { return false; }
  });
  if (!target) throw new Error(`No open page matches ${o.pageUrl.origin}${o.pageUrl.pathname}. Open the local prototype in the dedicated CDP browser first.`);
  const wsUrl = new URL(target.webSocketDebuggerUrl);
  if (!['127.0.0.1', 'localhost', '[::1]'].includes(wsUrl.hostname)) throw new Error('Chrome returned a non-loopback page debugger URL; refusing it.');
  const cdp = await Cdp.connect(wsUrl.href);
  const tree = await cdp.call('Page.getFrameTree');
  const frames = [];
  const walk = node => { frames.push(node.frame); for (const c of node.childFrames || []) walk(c); };
  walk(tree.frameTree);
  for (const frame of frames) {
    if (RUNTIME_PATH.test(new URL(frame.url).pathname)) {
      if (new URL(frame.url).origin !== o.pageUrl.origin) { cdp.close(); throw new Error('The game iframe is not same-origin with the prototype.'); }
      if (o.command === 'record') { cdp.close(); throw new Error('A game iframe is already loaded. Close the game, re-arm capture on the prototype page, then launch KOTOR so input observation starts before BoxedWine installs its handlers.'); }
    }
  }
  return { cdp, target, browser: version.Browser, frames };
}

const INPUT_INSTALL = `(() => {
  if (!location.pathname.endsWith('/game-runtime.html') || window.__telosLiveCaptureInstalled) return;
  window.__telosLiveCaptureInstalled = true;
  const emit = value => { try { window.__telosLiveCapture(JSON.stringify(value)); } catch {} };
  const codeMap = {
    Escape:'esc', F1:'f1',F2:'f2',F3:'f3',F4:'f4',F5:'f5',F6:'f6',F7:'f7',F8:'f8',F9:'f9',F10:'f10',F11:'f11',F12:'f12',
    Backquote:'grave',Digit1:'1',Digit2:'2',Digit3:'3',Digit4:'4',Digit5:'5',Digit6:'6',Digit7:'7',Digit8:'8',Digit9:'9',Digit0:'0',
    Minus:'minus',Equal:'equals',Backspace:'backspace',Tab:'tab',KeyQ:'q',KeyW:'w',KeyE:'e',KeyR:'r',KeyT:'t',KeyY:'y',KeyU:'u',KeyI:'i',KeyO:'o',KeyP:'p',
    BracketLeft:'lbracket',BracketRight:'rbracket',Backslash:'backslash',CapsLock:'capslock',KeyA:'a',KeyS:'s',KeyD:'d',KeyF:'f',KeyG:'g',KeyH:'h',KeyJ:'j',KeyK:'k',KeyL:'l',
    Semicolon:'semicolon',Quote:'apostrophe',Enter:'enter',ShiftLeft:'lshift',ShiftRight:'rshift',KeyZ:'z',KeyX:'x',KeyC:'c',KeyV:'v',KeyB:'b',KeyN:'n',KeyM:'m',Comma:'comma',Period:'period',Slash:'slash',
    ControlLeft:'lctrl',ControlRight:'rctrl',AltLeft:'lalt',AltRight:'ralt',Space:'space',PrintScreen:'prtsc',ScrollLock:'scrolllock',Pause:'pause',
    Insert:'ins',Home:'home',PageUp:'pgup',Delete:'del',End:'end',PageDown:'pgdn',ArrowRight:'right',ArrowLeft:'left',ArrowDown:'down',ArrowUp:'up',NumLock:'numlock',
    NumpadDivide:'numdiv',NumpadMultiply:'nummul',NumpadSubtract:'numsub',NumpadAdd:'numadd',NumpadEnter:'numenter',NumpadDecimal:'numdec',
    Numpad0:'num0',Numpad1:'num1',Numpad2:'num2',Numpad3:'num3',Numpad4:'num4',Numpad5:'num5',Numpad6:'num6',Numpad7:'num7',Numpad8:'num8',Numpad9:'num9'
  };
  let raf = 0, lastRaf = performance.now();
  const frame = now => { raf++; lastRaf = now; requestAnimationFrame(frame); };
  requestAnimationFrame(frame);
  const canvas = () => document.getElementById('boxedwine-webgl-canvas-0') || document.getElementById('canvas');
  function topPoint(x, y) {
    let w = window, px = x, py = y;
    try { while (w !== w.top) { const f = w.frameElement, r = f.getBoundingClientRect(); px += r.left; py += r.top; w = w.parent; } }
    catch { return null; }
    return { x: px, y: py };
  }
  function point(ev) {
    const c = canvas(), r = c?.getBoundingClientRect();
    if (!c || !r || !r.width || !r.height || !c.width || !c.height) return null;
    const p = topPoint(ev.clientX, ev.clientY);
    if (!p) return null;
    return { x: Math.max(0,Math.min(c.width-1,Math.round((ev.clientX-r.left)*c.width/r.width))),
      y: Math.max(0,Math.min(c.height-1,Math.round((ev.clientY-r.top)*c.height/r.height))), topX:p.x, topY:p.y,
      canvasWidth:c.width,canvasHeight:c.height };
  }
  function mouseEvent(ev, kind) {
    const c=canvas(); if (!c) return;
    const locked=document.pointerLockElement===document.getElementById('canvas');
    let p=null;
    if (Number.isFinite(ev.clientX) && Number.isFinite(ev.clientY)) p=point(ev);
    if (kind==='move' && !locked && !p) return;
    if ((kind==='down'||kind==='up') && !locked && !p) return;
    emit({kind:'input',domain:'mouse',event:kind,timeMs:performance.timeOrigin+performance.now(),
      nowMs:performance.now(),raf,lastRafMs:lastRaf,locked,button:ev.button,buttons:ev.buttons,
      x:p?.x??null,y:p?.y??null,topX:p?.topX??null,topY:p?.topY??null,
      width:p?.canvasWidth??c.width,height:p?.canvasHeight??c.height,
      movementX:ev.movementX||0,movementY:ev.movementY||0,deltaX:ev.deltaX||0,deltaY:ev.deltaY||0});
  }
  // Register at window capture before BoxedWine's own window-capture handlers;
  // those handlers stop propagation after forwarding to SDL/X11.
  window.addEventListener('keydown',ev=>{
    const t=ev.target;
    if (!codeMap[ev.code] || (t&&(t.isContentEditable||/^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName||'')))) return;
    if ((ev.ctrlKey||ev.metaKey) && !/^(Control|Shift|Alt|Meta)/.test(ev.code)) return;
    emit({kind:'input',domain:'key',event:'down',timeMs:performance.timeOrigin+performance.now(),nowMs:performance.now(),raf,lastRafMs:lastRaf,
      code:ev.code,key:codeMap[ev.code],vk:ev.keyCode||0,repeat:ev.repeat,shift:ev.shiftKey,ctrl:ev.ctrlKey,alt:ev.altKey,meta:ev.metaKey});
  },true);
  window.addEventListener('keyup',ev=>{
    const t=ev.target;
    if (!codeMap[ev.code] || (t&&(t.isContentEditable||/^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName||'')))) return;
    if ((ev.ctrlKey||ev.metaKey) && !/^(Control|Shift|Alt|Meta)/.test(ev.code)) return;
    emit({kind:'input',domain:'key',event:'up',timeMs:performance.timeOrigin+performance.now(),nowMs:performance.now(),raf,lastRafMs:lastRaf,
      code:ev.code,key:codeMap[ev.code],vk:ev.keyCode||0,repeat:ev.repeat,shift:ev.shiftKey,ctrl:ev.ctrlKey,alt:ev.altKey,meta:ev.metaKey});
  },true);
  window.addEventListener('mousemove',ev=>mouseEvent(ev,'move'),true);
  window.addEventListener('mousedown',ev=>mouseEvent(ev,'down'),true);
  window.addEventListener('mouseup',ev=>mouseEvent(ev,'up'),true);
  window.addEventListener('wheel',ev=>mouseEvent(ev,'wheel'),true);
  document.addEventListener('pointerlockchange',()=>emit({kind:'state',state:'pointer-lock',locked:document.pointerLockElement===document.getElementById('canvas'),timeMs:performance.timeOrigin+performance.now(),raf}),true);
  window.addEventListener('blur',()=>emit({kind:'state',state:'blur',timeMs:performance.timeOrigin+performance.now(),raf}),true);
  window.addEventListener('focus',()=>emit({kind:'state',state:'focus',timeMs:performance.timeOrigin+performance.now(),raf}),true);
  document.addEventListener('visibilitychange',()=>emit({kind:'state',state:document.visibilityState,timeMs:performance.timeOrigin+performance.now(),raf}),true);
  emit({kind:'frame-ready',timeMs:performance.timeOrigin+performance.now(),raf,href:location.href,
    origin:location.origin,viewport:{width:innerWidth,height:innerHeight},canvas:{width:canvas()?.width||0,height:canvas()?.height||0}});
})()`;

function safeRecordWriter(file) {
  const absolute = path.resolve(file);
  fs.mkdirSync(path.dirname(absolute), { recursive: true });
  const fd = fs.openSync(absolute, 'wx', 0o600);
  let closed = false;
  const write = obj => {
    if (closed) return;
    fs.writeSync(fd, `${JSON.stringify(obj)}\n`);
  };
  return {
    path: absolute,
    write,
    close() { if (!closed) { fs.fsyncSync(fd); fs.closeSync(fd); closed = true; } },
    sync() { if (!closed) fs.fdatasyncSync(fd); },
  };
}

function frameFor(evt, baseTime, hz) {
  return Math.max(1, Math.floor(((evt.timeMs - baseTime) * hz) / 1000) + 1);
}

function projectionFromRaw(rawPath, outputPath, hz) {
  const rawBytes = fs.readFileSync(rawPath);
  const rawSha256 = createHash('sha256').update(rawBytes).digest('hex');
  const lines = rawBytes.toString('utf8').split(/\r?\n/).filter(Boolean);
  if (Buffer.byteLength(lines.join('\n')) > MAX_CAPTURE_BYTES) throw new Error('Capture too large to project (128 MiB limit).');
  const records = lines.map((s, i) => { try { return JSON.parse(s); } catch { throw new Error(`Invalid raw capture line ${i + 1}.`); } });
  if (records[0]?.schema !== SCHEMA) throw new Error('Unsupported raw capture schema.');
  const events = records.filter(x => x.kind === 'input').sort((a, b) => a.timeMs - b.timeMs || a.seq - b.seq);
  if (!events.length) throw new Error('No game inputs were captured; raw NDJSON is retained.');
  const start = events[0].timeMs;
  const frames = new Map();
  const held = new Set();
  let mouse = null, maxFrame = 1, pointerLockSeen = false;
  for (const e of events) {
    const f = frameFor(e, start, hz); maxFrame = Math.max(maxFrame, f);
    let state = frames.get(f);
    if (!state) { state = { wheel:0, mdx:0, mdy:0 }; frames.set(f, state); }
    if (e.locked) pointerLockSeen = true;
    if (e.domain === 'key') {
      if (e.event === 'down' && !e.repeat) held.add(e.key);
      if (e.event === 'up') held.delete(e.key);
    } else if (e.domain === 'mouse') {
      if (e.event === 'down' || e.event === 'up') {
        const b = ({ 0:'lmb', 1:'mmb', 2:'rmb' })[e.button];
        if (b) { if (e.event === 'down') held.add(b); else held.delete(b); }
      }
      if (e.event === 'move' && e.locked) { state.mdx += e.movementX || 0; state.mdy += e.movementY || 0; }
      else if ((e.event === 'move' || e.event === 'down' || e.event === 'up') && !e.locked && Number.isFinite(e.x) && Number.isFinite(e.y)) mouse = [Math.round(e.x), Math.round(e.y)];
      if (e.event === 'wheel' && Number.isFinite(e.deltaY) && e.deltaY !== 0) state.wheel += e.deltaY < 0 ? Math.max(1,Math.round(Math.abs(e.deltaY)/100)) : -Math.max(1,Math.round(Math.abs(e.deltaY)/100));
    }
    state.held = [...held].sort(); state.mouse = mouse && [...mouse];
  }
  const noteFor = (f) => {
    const s = frames.get(f) || {};
    const tokens = [...(s.held || [])];
    if (s.mouse) tokens.push(`m@${s.mouse[0]},${s.mouse[1]}`);
    if (s.mdx || s.mdy) tokens.push(`md${Math.round(s.mdx||0)},${Math.round(s.mdy||0)}`);
    if (s.wheel) tokens.push(`wh${s.wheel > 0 ? '+' : ''}${s.wheel}`);
    return tokens.length ? tokens.join(' ') : '.';
  };
  const emitted = [
    `#!score v1 game=k1 hz=${hz}`,
    '# Native KPTAS score projection from browser input events; importable/editable, not an exact TAS recording.',
    '# Intended for the desktop KPTAS score parser/shim. The browser demo parser only accepts a smaller input subset.',
    `# Source: ${path.basename(rawPath)} (see raw NDJSON for event order, page timestamps, rAF count, focus and pointer-lock evidence).`,
    `# Source SHA-256: ${rawSha256} (${rawBytes.length} bytes; ${events.length} input events).`,
    `# Source event window: ${events[0].timeMs}..${events.at(-1).timeMs} page performance milliseconds.`,
    '# Mapping: first captured event becomes score tick 1; browser performance timestamps are floor-quantized to nominal frames; no start-state is embedded.',
    '# Start defaults to boot. A trace from a loaded save needs an equivalent boot route or save-loading support before native replay.',
    '# Uncertainty: <=1 nominal frame quantization plus unknown alignment to the guest engine gate; this is not tick/frame-boundary proof.',
    '# Mouse in pointer lock and wheel behavior require route-specific live qualification before relying on this projection.',
    `@segment captured hz=${hz} pace=realtime`,
  ];
  // Input rows represent held-state per frame. Carry the last observed state
  // forward across frames with no DOM transition; otherwise a 10-frame key
  // hold would incorrectly appear as a one-frame tap in the projection.
  let prior = null, count = 0;
  let heldState = [], mouseState = null;
  for (let f = 1; f <= maxFrame; f++) {
    const s = frames.get(f);
    if (s) {
      heldState = s.held || heldState;
      if (s.mouse) mouseState = s.mouse;
    }
    const tokens = [...heldState];
    if (mouseState) tokens.push(`m@${mouseState[0]},${mouseState[1]}`);
    if (s && (s.mdx || s.mdy)) tokens.push(`md${Math.round(s.mdx||0)},${Math.round(s.mdy||0)}`);
    if (s?.wheel) tokens.push(`wh${s.wheel > 0 ? '+' : ''}${s.wheel}`);
    const n = tokens.length ? tokens.join(' ') : '.';
    if (n === prior) { count++; continue; }
    if (prior !== null) emitted.push(`${count} ${prior}`);
    prior = n; count = 1;
  }
  if (prior !== null) emitted.push(`${count} ${prior}`);
  const projection = outputPath || rawPath.replace(/\.jsonl$/i, '') + '.projection.score';
  fs.writeFileSync(projection, `${emitted.join('\n')}\n`, { flag:'wx', mode:0o600 });
  return { projection, frames: maxFrame, pointerLockSeen };
}

function runtimeFrameCount(frames) {
  return frames.filter(f => {
    try { return RUNTIME_PATH.test(new URL(f.url).pathname); } catch { return false; }
  }).length;
}

async function record(o) {
  // Validate the endpoint/page/frame state before reserving the output path.
  // A failed arm must not leave a fake empty capture or remove an existing file.
  const session = await attach(o);
  const { cdp, target, browser, frames } = session;
  const raw = safeRecordWriter(o.out);
  const runtime = new Set();
  let eventCount = 0, stopping = false, doneResolve;
  const done = new Promise(r => { doneResolve = r; });
  raw.write({ schema:SCHEMA, kind:'capture', createdAt:new Date().toISOString(), browser, targetUrl:target.url,
    expectedGameFrame:'/original-game/runtime-releases/<pinned>/game-runtime.html', frameRateHz:o.hz,
    timestampSemantics:'raw page performance.timeOrigin+performance.now plus host receipt monotonic nanoseconds; rAF is an observation aid, not the game update gate',
    captureMode:'event transitions; no frame screenshots or per-frame polling', scoreProjection:'partial nominal-frame quantization' });
  raw.sync();

  const frameMap = new Map();
  const registerFrame = frame => {
    if (!frame || !RUNTIME_PATH.test((() => { try { return new URL(frame.url).pathname; } catch { return ''; } })())) return;
    const u = new URL(frame.url);
    if (u.origin !== o.pageUrl.origin) return;
    if (!runtime.has(frame.id)) {
      runtime.add(frame.id);
      raw.write({ kind:'runtime-frame', frameId:frame.id, url:u.href, observedAt:new Date().toISOString(),
        method:'Page.addScriptToEvaluateOnNewDocument armed before iframe creation' });
      process.stdout.write('KOTOR runtime iframe loaded same-origin; waiting for game input. Keep this tab focused.\n');
    }
  };
  cdp.on('Page.frameNavigated', p => registerFrame(p.frame));
  cdp.on('Page.frameDetached', p => {
    if (runtime.has(p.frameId)) { raw.write({ kind:'runtime-frame-detached', frameId:p.frameId, hostMonotonicNs:process.hrtime.bigint().toString() }); runtime.delete(p.frameId); }
  });
  cdp.on('Runtime.executionContextCreated', p => {
    if (p.context?.auxData?.frameId) frameMap.set(p.context.id, p.context.auxData.frameId);
  });
  cdp.on('Runtime.bindingCalled', p => {
    if (p.name !== '__telosLiveCapture') return;
    const frameId = frameMap.get(p.executionContextId);
    if (!frameId || !runtime.has(frameId)) return;
    let data;
    try { data = JSON.parse(p.payload); } catch { return; }
    if (data.kind === 'frame-ready') {
      raw.write({ kind:'game-frame-ready', frameId, ...data, hostMonotonicNs:process.hrtime.bigint().toString() });
      process.stdout.write(`Recorder attached before guest input handlers; browser viewport ${data.viewport?.width}x${data.viewport?.height}. Input events are raw, not yet frame-gate qualified.\n`);
      return;
    }
    raw.write({ ...data, frameId, seq:++eventCount, hostMonotonicNs:process.hrtime.bigint().toString() });
    if (eventCount && eventCount % 1000 === 0) process.stdout.write(`Captured ${eventCount} input/state events.\n`);
  });

  cdp.on('Inspector.targetCrashed', () => { raw.write({ kind:'runtime-crashed', hostMonotonicNs:process.hrtime.bigint().toString() }); doneResolve(); });
  cdp.on('Page.frameNavigated', p => {
    if (runtime.has(p.frame.id) && !RUNTIME_PATH.test((() => { try { return new URL(p.frame.url).pathname; } catch { return ''; } })())) doneResolve();
  });

  await cdp.call('Runtime.enable');
  await cdp.call('Page.enable');
  await cdp.call('Runtime.addBinding', { name:'__telosLiveCapture' });
  await cdp.call('Page.addScriptToEvaluateOnNewDocument', { source:INPUT_INSTALL });
  for (const f of frames) registerFrame(f);
  process.stdout.write(`Capture armed on ${target.url}. No game iframe yet; launch KOTOR from this page.\n`);
  const syncTimer = setInterval(() => { try { raw.sync(); } catch {} }, 1000);
  const stop = async signal => {
    if (stopping) return;
    stopping = true;
    raw.write({ kind:'capture-end', signal, eventCount, hostMonotonicNs:process.hrtime.bigint().toString(), endedAt:new Date().toISOString() });
    clearInterval(syncTimer);
    try { raw.close(); } finally { cdp.close(); doneResolve(); }
  };
  process.once('SIGINT', () => { void stop('SIGINT'); });
  process.once('SIGTERM', () => { void stop('SIGTERM'); });
  await done;
  if (!stopping) await stop('runtime-ended');
  if (!eventCount) { process.stdout.write(`Raw capture saved: ${raw.path} (no guest input events observed).\n`); return; }
  const result = projectionFromRaw(raw.path, null, o.hz);
  process.stdout.write(`Raw capture: ${raw.path}\nPartial score projection: ${result.projection}\nProjected ${result.frames} nominal ${o.hz}Hz frames${result.pointerLockSeen ? '; pointer-lock movement present (review before replay)' : ''}.\n`);
}

function readRaw(input) {
  const st = fs.statSync(input);
  if (!st.isFile() || st.size > MAX_CAPTURE_BYTES) throw new Error('Raw capture must be a regular file no larger than 128 MiB.');
  const lines = fs.readFileSync(input, 'utf8').split(/\r?\n/).filter(Boolean);
  const records = lines.map((s, i) => { try { return JSON.parse(s); } catch { throw new Error(`Invalid raw capture line ${i + 1}.`); } });
  if (records[0]?.schema !== SCHEMA) throw new Error('Unsupported input-capture schema.');
  if (!records.some(r => r.kind === 'capture-end')) throw new Error('Capture has no clean end marker; preserve it but do not replay without manual review.');
  const events = records.filter(r => r.kind === 'input').sort((a, b) => a.timeMs - b.timeMs || a.seq - b.seq);
  if (!events.length) throw new Error('Raw capture contains no replayable input events.');
  if (events.some(e => e.locked && e.domain === 'mouse' && (e.movementX || e.movementY))) {
    throw new Error('Replay refused: pointer-lock relative mouse motion is recorded, but CDP-to-BoxedWine pointer-lock semantics have not been qualified. Keyboard/non-locked mouse projection remains available.');
  }
  return { records, events };
}

const codeToVk = {
  Escape:27,Enter:13,Space:32,Tab:9,Backspace:8,ArrowUp:38,ArrowDown:40,ArrowLeft:37,ArrowRight:39,
  ShiftLeft:160,ShiftRight:161,ControlLeft:162,ControlRight:163,AltLeft:164,AltRight:165,
  CapsLock:20,NumLock:144,ScrollLock:145,Pause:19,Insert:45,Delete:46,Home:36,End:35,PageUp:33,PageDown:34,
  Backquote:192,Minus:189,Equal:187,BracketLeft:219,BracketRight:221,Backslash:220,Semicolon:186,Quote:222,Comma:188,Period:190,Slash:191,
  NumpadEnter:13,NumpadAdd:107,NumpadSubtract:109,NumpadMultiply:106,NumpadDivide:111,NumpadDecimal:110,
};
function virtualKey(e) {
  if (codeToVk[e.code]) return codeToVk[e.code];
  if (/^Key[A-Z]$/.test(e.code)) return e.code.charCodeAt(3);
  if (/^Digit[0-9]$/.test(e.code)) return e.code.charCodeAt(5);
  if (/^Numpad[0-9]$/.test(e.code)) return 96 + Number(e.code.at(-1));
  const f = /^F(1[0-2]|[1-9])$/.exec(e.code); if (f) return 111 + Number(f[1]);
  return e.vk || 0;
}
function modifiers(e) { return (e.alt?1:0) | (e.ctrl?2:0) | (e.meta?4:0) | (e.shift?8:0); }
function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

async function replay(o) {
  const { events } = readRaw(path.resolve(o.input));
  const session = await attach(o);
  const { cdp, target, frames } = session;
  const runtime = frames.find(f => {
    try { return RUNTIME_PATH.test(new URL(f.url).pathname) && new URL(f.url).origin === o.pageUrl.origin; } catch { return false; }
  });
  if (!runtime) { cdp.close(); throw new Error('No active same-origin KOTOR game iframe. Launch and load the game first, then re-run playback.'); }
  const mouselocked = events.some(e => e.domain === 'mouse' && e.locked);
  if (mouselocked) { cdp.close(); throw new Error('Replay refused: capture includes pointer-lock mouse input. The game’s BoxedWine path uses locked relative motion; this tool has not qualified its CDP replay mapping.'); }
  await cdp.call('Page.enable');
  process.stdout.write(`Ready to replay ${events.length} raw inputs into ${runtime.url}. Return focus to the game tab; first input in ${o.delayStartMs} ms. Do not use this while another take is running.\n`);
  if (o.delayStartMs) await sleep(o.delayStartMs);
  const firstTime = events[0].timeMs;
  const start = performance.now();
  let sent = 0;
  for (const e of events) {
    const due = start + (e.timeMs - firstTime);
    const wait = due - performance.now();
    if (wait > 1) await sleep(wait);
    if (e.domain === 'key') {
      cdp.notify('Input.dispatchKeyEvent', { type:e.event === 'down'?'keyDown':'keyUp', key:e.key.length===1?e.key:e.code,
        code:e.code, windowsVirtualKeyCode:virtualKey(e), nativeVirtualKeyCode:virtualKey(e), modifiers:modifiers(e), autoRepeat:!!e.repeat });
    } else if (e.domain === 'mouse' && ['move','down','up','wheel'].includes(e.event)) {
      const type = e.event === 'move'?'mouseMoved':e.event === 'down'?'mousePressed':e.event === 'up'?'mouseReleased':'mouseWheel';
      const params = { type, x:e.topX??0, y:e.topY??0, modifiers:0, pointerType:'mouse' };
      if (e.event === 'down' || e.event === 'up') {
        params.button = ({0:'left',1:'middle',2:'right'})[e.button];
        params.buttons = e.event === 'down' ? (e.button===0?1:e.button===1?4:2) : 0;
        params.clickCount = 1;
      }
      if (e.event === 'wheel') { params.deltaX=e.deltaX||0; params.deltaY=e.deltaY||0; }
      cdp.notify('Input.dispatchMouseEvent', params);
    }
    if (++sent % 500 === 0) process.stdout.write(`Dispatched ${sent}/${events.length} events.\n`);
  }
  // Release every key/button that could have been left down in a truncated
  // event stream. This is a safety release, not part of the captured score.
  const down = new Map();
  for (const e of events) if (e.domain==='key') { if(e.event==='down') down.set(e.code,e); else down.delete(e.code); }
  for (const e of down.values()) cdp.notify('Input.dispatchKeyEvent',{type:'keyUp',key:e.key.length===1?e.key:e.code,code:e.code,windowsVirtualKeyCode:virtualKey(e),nativeVirtualKeyCode:virtualKey(e)});
  cdp.notify('Input.dispatchMouseEvent',{type:'mouseReleased',x:0,y:0,button:'left',buttons:0,clickCount:1});
  cdp.close();
  process.stdout.write(`Dispatched ${sent} events continuously. Replay completion is transport evidence only; check game state and route outcome.\n`);
}

async function main() {
  try {
    if (typeof fetch !== 'function' || typeof WebSocket !== 'function') throw new Error('Use Node.js 22 or newer.');
    const o = options(process.argv.slice(2));
    if (o.command === 'record') await record(o);
    else await replay(o);
  } catch (e) { process.stderr.write(`${e.message}\n`); process.exitCode = 2; }
}
await main();
