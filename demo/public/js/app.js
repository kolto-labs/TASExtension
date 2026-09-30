// Page wiring: transport, score editor, timeline, inspector.

import { W, H, FACES, DOOR_US, LOAD_US, toPx, keysOf } from './sim.js';
import { parseScore, EXAMPLE } from './score.js';
import { makeBranch, headOf, extend, advance, restore, applyScore, markTicks, MAX_TICKS } from './holo.js';
import { drawScene, drawTimeline, fmtVt } from './render.js';

const $ = (id) => document.getElementById(id);
const reducedMq = matchMedia('(prefers-reduced-motion: reduce)');

const el = {
  view: $('view'), tl: $('timeline'), score: $('score'), backdrop: $('backdrop'), gutter: $('gutter'),
  errors: $('score-errors'), scoreStatus: $('score-status'), stageStatus: $('stage-status'),
  play: $('b-play'), playLabel: $('play-label'), iPlay: $('i-play'), iPause: $('i-pause'),
  speed: $('speed'), zoom: $('zoom'), readout: $('readout'), budget: $('budget'), showPath: $('show-path'),
  tip: $('tip'), ents: $('i-ents'),
};
const ctx = el.view.getContext('2d');
const tctx = el.tl.getContext('2d');

const app = {
  b: null, g: null, t: 0, playing: false, speed: 1, acc: 0, last: 0,
  tier: 'lab', watermark: true, showPath: true,
  wmBox: null, hoverTick: -1, tlGeom: null, marks: [], lineTicks: new Map(),
  lines: [], errLines: new Map(), activeLine: -1, editTimer: 0, dirty: true,
};
const simCtx = {};

// ---------- status lines ----------
function say(node, text, tone = '') {
  node.textContent = text;
  node.className = node.className.replace(/\b(ok|warn)\b/g, '').trim() + (tone ? ' ' + tone : '');
}

// ---------- branch bookkeeping ----------
function rebuildIndexes() {
  const b = app.b;
  app.marks = markTicks(b);
  app.lineTicks = new Map();
  for (let t = 1; t < b.recs.length; t++) noteLineTick(b.recs[t]);
}
function noteLineTick(rec) {
  if (rec.src < 0) return;
  const it = app.b.prog.ins[rec.src];
  if (it && !app.lineTicks.has(it.line)) app.lineTicks.set(it.line, rec.t);
}

function newRun(text, cfg) {
  const p = parseScore(text);
  app.b = makeBranch({ name: 'main', score: text, prog: p.prog, cfg });
  extend(app.b, Infinity, simCtx, true);
  rebuildIndexes();
}

// ---------- moving the playhead ----------
function seek(T) {
  const b = app.b;
  T = Math.max(0, Math.min(MAX_TICKS, T | 0));
  if (T > headOf(b)) {
    app.g = extend(b, T, simCtx);
    rebuildIndexes();
    T = headOf(b);
  } else {
    app.g = restore(b.recs[T]);
  }
  app.t = T;
  app.dirty = true;
}

// One tick forward from the live state. Inside the stored range the new
// state is checked against the stored hash.
function stepForward() {
  const b = app.b;
  if (app.t >= MAX_TICKS) { pause(); return; }
  const wasEnd = app.g.cur.end;
  const next = app.t + 1;
  const rec = advance(b, app.g, simCtx);
  if (next <= headOf(b)) {
    if (b.recs[next].h !== rec.h) {
      b.recs.length = next;
      b.recs.push(rec);
      rebuildIndexes();
      say(el.stageStatus, `Tick ${next} did not match the stored state. The stored ticks after it were dropped.`, 'warn');
    }
  } else {
    b.recs.push(rec);
    noteLineTick(rec);
    if (rec.ev.length || rec.c.end) app.marks = markTicks(b);
  }
  app.t = next;
  app.dirty = true;
  if (app.playing && rec.c.end && !wasEnd) {
    pause();
    say(el.stageStatus, `End of score at tick ${next}. Play again to run on with no input.`);
  }
  if (app.playing && rec.c.halt && !b.recs[next - 1].c.halt) {
    pause();
    say(el.stageStatus, `Run halted at tick ${next}: ${rec.c.halt}.`, 'warn');
  }
}

function play() {
  if (app.playing) return;
  app.playing = true;
  app.last = performance.now();
  app.acc = 0;
  el.play.setAttribute('aria-pressed', 'true');
  el.playLabel.textContent = 'Pause';
  el.iPlay.hidden = true;
  el.iPause.hidden = false;
  requestAnimationFrame(loop);
}
function pause() {
  app.playing = false;
  el.play.setAttribute('aria-pressed', 'false');
  el.playLabel.textContent = 'Play';
  el.iPlay.hidden = false;
  el.iPause.hidden = true;
  app.dirty = true;
}
const togglePlay = () => (app.playing ? pause() : play());

function loop(now) {
  if (!app.playing) return;
  const dt = Math.min(100, now - app.last);
  app.last = now;
  app.acc += (dt / 1000) * app.g.hz * app.speed;
  let n = Math.min(Math.floor(app.acc), 48);
  app.acc -= Math.floor(app.acc);
  while (n-- > 0 && app.playing) stepForward();
  draw();
  if (app.playing) requestAnimationFrame(loop);
}

function goEnd() {
  const b = app.b;
  let end = -1;
  for (let t = 1; t < b.recs.length; t++) if (b.recs[t].c.end) { end = t; break; }
  if (end < 0) {
    extend(b, Infinity, simCtx, true);
    rebuildIndexes();
    for (let t = 1; t < b.recs.length; t++) if (b.recs[t].c.end) { end = t; break; }
    if (end < 0) end = headOf(b);
  }
  seek(end);
}

// ---------- score editor ----------
function renderEditor() {
  const text = el.score.value;
  const lines = text.split('\n');
  app.lines = lines;
  const bd = document.createDocumentFragment();
  const gt = document.createDocumentFragment();
  lines.forEach((line, i) => {
    const ln = i + 1;
    const d = document.createElement('div');
    d.textContent = line.length ? line : ' ';
    const g = document.createElement('div');
    const n = document.createElement('span');
    n.className = 'n';
    n.textContent = String(ln);
    const t = document.createElement('span');
    t.className = 't';
    const tick = app.lineTicks.get(ln);
    t.textContent = tick != null ? String(tick) : '';
    g.append(n, t);
    const err = app.errLines.get(ln);
    if (err) {
      d.className = 'err';
      g.className = 'err';
      const m = document.createElement('span');
      m.className = 'emsg';
      m.textContent = '   <- ' + err;
      d.append(m);
    }
    bd.append(d);
    gt.append(g);
  });
  el.backdrop.replaceChildren(bd);
  el.gutter.replaceChildren(gt);
  app.activeLine = -1;
  syncScroll();
}

function syncScroll() {
  el.backdrop.scrollTop = el.score.scrollTop;
  el.backdrop.scrollLeft = el.score.scrollLeft;
  el.gutter.scrollTop = el.score.scrollTop;
}

function currentLine() {
  const r = app.b.recs[app.t];
  if (!r || r.src < 0) return { line: -1, wait: false };
  const it = app.b.prog.ins[r.src];
  return { line: it ? it.line : -1, wait: !!r.c.wait };
}

function updateActiveLine() {
  const { line, wait } = currentLine();
  if (line === app.activeLine && !app.dirtyLines) return;
  const rows = el.backdrop.children, gut = el.gutter.children;
  if (app.activeLine > 0 && rows[app.activeLine - 1]) {
    rows[app.activeLine - 1].classList.remove('active', 'wait');
    gut[app.activeLine - 1].classList.remove('active');
  }
  app.activeLine = line;
  if (line > 0 && rows[line - 1]) {
    rows[line - 1].classList.add(wait ? 'wait' : 'active');
    gut[line - 1].classList.add('active');
    if (document.activeElement !== el.score) {
      const lh = 18;
      const top = (line - 1) * lh, view = el.score.clientHeight;
      if (top < el.score.scrollTop + lh || top > el.score.scrollTop + view - 3 * lh) {
        el.score.scrollTop = Math.max(0, top - Math.floor(view / 3));
        syncScroll();
      }
    }
  }
}

function showErrors(errors) {
  app.errLines = new Map();
  for (const e of errors) if (!app.errLines.has(e.line)) app.errLines.set(e.line, e.msg);
  const frag = document.createDocumentFragment();
  for (const e of errors.slice(0, 6)) {
    const li = document.createElement('li');
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.textContent = `Line ${e.line}: ${e.msg}`;
    btn.addEventListener('click', () => focusLine(e.line));
    li.append(btn);
    frag.append(li);
  }
  if (errors.length > 6) {
    const li = document.createElement('li');
    li.textContent = `${errors.length - 6} more.`;
    frag.append(li);
  }
  el.errors.replaceChildren(frag);
}

function focusLine(line) {
  const idx = app.lines.slice(0, line - 1).reduce((a, l) => a + l.length + 1, 0);
  el.score.focus();
  el.score.setSelectionRange(idx, idx + (app.lines[line - 1] || '').length);
  el.score.scrollTop = Math.max(0, (line - 4) * 18);
  syncScroll();
}

function applyEdit() {
  const text = el.score.value;
  const oldHead = headOf(app.b);
  const res = applyScore(app.b, text);
  if (res.errors.length) {
    showErrors(res.errors);
    renderEditor();
    say(el.scoreStatus, `${res.errors.length === 1 ? '1 error' : res.errors.length + ' errors'}. Playing the last good version.`, 'warn');
    return;
  }
  showErrors([]);
  if (res.fork === Infinity || res.fork > oldHead) {
    rebuildIndexes();
    say(el.scoreStatus, res.fork === Infinity ? 'Parsed. No input changed.' : 'Parsed. The change is past the recorded ticks.', 'ok');
  } else if (res.fork <= app.t) {
    app.g = extend(app.b, app.t, simCtx);
    app.t = headOf(app.b);
    rebuildIndexes();
    say(el.scoreStatus, `Changed from tick ${res.fork}. Replayed ${app.t - res.fork + 1} ticks to the playhead.`, 'ok');
  } else {
    rebuildIndexes();
    say(el.scoreStatus, `Changed from tick ${res.fork}. Later ticks were dropped. Play on to record them.`, 'ok');
  }
  renderEditor();
  app.dirty = true;
}

// ---------- inspector ----------
function text(id, v) { const n = $(id); if (n.textContent !== v) n.textContent = v; }

function renderInspector() {
  const g = app.g, r = app.b.recs[app.t];
  text('i-tick', `${g.tick} of ${headOf(app.b)} stored`);
  text('i-vt', `${fmtVt(g.vt)}  (${g.vt.toLocaleString('en-US')} µs)`);
  text('i-hz', `${g.hz} Hz, segment ${g.cur.seg}`);
  text('i-area', `${g.area}, ${g.mode === 'load' ? 'loading' : 'playing'}, epoch ${g.epoch}`);
  text('i-in', g.tick ? keysOf(g.in) + (g.cur.wait ? '  (sync wait)' : '') : 'none (boot state)');
  const { line } = currentLine();
  text('i-line', line > 0 ? `line ${line}` : g.cur.end ? 'past the end' : 'none');
  text('i-rng', '0x' + (g.rng >>> 0).toString(16).padStart(8, '0'));
  text('i-draws', g.draws.length ? `${g.draws.length}: ${g.draws.join(', ')}` : '0');
  text('i-ev', g.fired.length ? g.fired.join(', ') : 'none');
  text('i-door', g.area !== 1 ? 'not in this area' : g.door === 0 ? 'closed' : g.door >= DOOR_US ? 'open' : `opening ${Math.round((g.door / DOOR_US) * 100)}%`);
  text('i-path', g.ps ? `${g.ps.n} of ${g.ps.b} nodes, ${g.ps.full ? 'reached you' : 'stopped short'}` : 'no search yet');
  text('i-msg', g.msg || 'none');
  text('i-hash', r ? r.h : '');
  text('i-chain', r ? r.ch : '');
  const rows = g.ents.map((e) => [e.id, toPx(e.x).toFixed(1), toPx(e.y).toFixed(1), FACES[e.face] || '-', `${e.hp}/${e.max}`, e.act]);
  const key = JSON.stringify(rows);
  if (el.ents.dataset.key !== key) {
    el.ents.dataset.key = key;
    const frag = document.createDocumentFragment();
    for (const row of rows) {
      const tr = document.createElement('tr');
      row.forEach((v, i) => {
        const td = document.createElement('td');
        td.textContent = v;
        if (i === 1 || i === 2) td.className = 'num';
        tr.append(td);
      });
      frag.append(tr);
    }
    el.ents.replaceChildren(frag);
  }
}

// ---------- drawing ----------
function fitCanvas(canvas, c, w, h) {
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  const cw = canvas.clientWidth;
  const scale = cw / w;
  const bw = Math.round(cw * dpr), bh = Math.round(h * scale * dpr);
  if (canvas.width !== bw || canvas.height !== bh) { canvas.width = bw; canvas.height = bh; }
  c.setTransform(dpr * scale, 0, 0, dpr * scale, 0, 0);
}

function viewRange() {
  const head = headOf(app.b);
  const z = +el.zoom.value;
  const span = Math.max(head, 60);
  if (!z || z >= span) return [0, span];
  let v0 = Math.max(0, app.t - Math.floor(z / 2));
  if (v0 + z > span) v0 = Math.max(0, span - z);
  return [v0, v0 + z];
}

function draw() {
  fitCanvas(el.view, ctx, W, H);
  app.wmBox = drawScene(ctx, app.g, { tier: app.tier, watermark: app.watermark, showPath: app.showPath, reduced: reducedMq.matches });
  const cw = el.tl.clientWidth;
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  if (el.tl.width !== Math.round(cw * dpr)) { el.tl.width = Math.round(cw * dpr); el.tl.height = Math.round(64 * dpr); }
  tctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  const [v0, v1] = viewRange();
  app.tlGeom = drawTimeline(tctx, cw, 64, { recs: app.b.recs, t: app.t, marks: app.marks, v0, v1, hover: app.hoverTick, split: null });
  el.tl.setAttribute('aria-valuemax', String(headOf(app.b)));
  el.tl.setAttribute('aria-valuenow', String(app.t));
  el.tl.setAttribute('aria-valuetext', `tick ${app.t} of ${headOf(app.b)}`);
  el.readout.textContent = `tick ${app.t}  ·  ${fmtVt(app.g.vt)}`;
  if (!app.playing || app.t % 4 === 0) renderInspector();
  updateActiveLine();
  app.dirty = false;
}

function idle() {
  if (app.dirty && !app.playing) draw();
  requestAnimationFrame(idle);
}

// ---------- events ----------
function isTyping(t) {
  return t && (t.tagName === 'TEXTAREA' || t.tagName === 'SELECT' || (t.tagName === 'INPUT' && t.type !== 'checkbox'));
}

function onKey(e) {
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  if (isTyping(e.target)) return;
  const onButton = e.target && (e.target.tagName === 'BUTTON' || e.target.tagName === 'INPUT');
  if (e.key === ' ' || e.code === 'Space') {
    if (onButton) return;
    e.preventDefault();
    togglePlay();
  } else if (e.key === '.') {
    e.preventDefault();
    pause();
    stepForward();
  } else if (e.key === ',') {
    e.preventDefault();
    pause();
    seek(app.t - 1);
  } else if (e.key === 'Home' && e.target !== el.tl) {
    e.preventDefault(); pause(); seek(0);
  } else if (e.key === 'End' && e.target !== el.tl) {
    e.preventDefault(); pause(); goEnd();
  }
}

function tlTickAt(ev) {
  const rect = el.tl.getBoundingClientRect();
  return app.tlGeom ? app.tlGeom.tickAt(ev.clientX - rect.left) : 0;
}

function bind() {
  document.addEventListener('keydown', onKey);
  el.play.addEventListener('click', togglePlay);
  $('b-step').addEventListener('click', () => { pause(); stepForward(); });
  $('b-back').addEventListener('click', () => { pause(); seek(app.t - 1); });
  $('b-start').addEventListener('click', () => { pause(); seek(0); });
  $('b-end').addEventListener('click', () => { pause(); goEnd(); });
  el.speed.addEventListener('change', () => { app.speed = +el.speed.value; });
  el.zoom.addEventListener('change', () => { app.dirty = true; });
  el.showPath.addEventListener('change', () => { app.showPath = el.showPath.checked; app.dirty = true; });
  el.budget.addEventListener('change', () => {
    pause();
    const t = app.t;
    newRun(app.b.score, { ...app.b.cfg, budget: +el.budget.value });
    seek(Math.min(t, headOf(app.b)));
    renderEditor();
    say(el.stageStatus, `Path budget set to ${el.budget.value} nodes. The run was replayed from tick 0.`, 'ok');
  });

  el.score.addEventListener('input', () => {
    clearTimeout(app.editTimer);
    app.editTimer = setTimeout(applyEdit, 350);
  });
  el.score.addEventListener('scroll', syncScroll);

  // Timeline: click or drag to load any tick; arrows step.
  let dragging = false;
  el.tl.addEventListener('pointerdown', (ev) => {
    dragging = true;
    el.tl.setPointerCapture(ev.pointerId);
    pause();
    seek(tlTickAt(ev));
  });
  el.tl.addEventListener('pointermove', (ev) => {
    const t = tlTickAt(ev);
    if (dragging) { if (t !== app.t) seek(t); } else if (t !== app.hoverTick) { app.hoverTick = t; app.dirty = true; }
  });
  el.tl.addEventListener('pointerup', () => { dragging = false; });
  el.tl.addEventListener('pointerleave', () => { app.hoverTick = -1; app.dirty = true; });
  el.tl.addEventListener('keydown', (ev) => {
    const step = { ArrowLeft: -1, ArrowRight: 1, ArrowDown: -1, ArrowUp: 1, PageDown: -60, PageUp: 60 }[ev.key];
    if (step) { ev.preventDefault(); pause(); seek(app.t + step); }
    else if (ev.key === 'Home') { ev.preventDefault(); pause(); seek(0); }
    else if (ev.key === 'End') { ev.preventDefault(); pause(); goEnd(); }
  });

  // Watermark hover text.
  el.view.addEventListener('pointermove', (ev) => {
    const b = app.wmBox;
    const rect = el.view.getBoundingClientRect();
    const x = ((ev.clientX - rect.left) / rect.width) * W, y = ((ev.clientY - rect.top) / rect.height) * H;
    if (b && x >= b.x && x <= b.x + b.w && y >= b.y && y <= b.y + b.h) {
      el.tip.textContent = 'tool assisted';
      el.tip.hidden = false;
      el.tip.style.left = Math.min(ev.clientX + 12, window.innerWidth - 140) + 'px';
      el.tip.style.top = ev.clientY - 34 + 'px';
    } else el.tip.hidden = true;
  });
  el.view.addEventListener('pointerleave', () => { el.tip.hidden = true; });
  window.addEventListener('resize', () => { app.dirty = true; });
  reducedMq.addEventListener('change', () => { app.dirty = true; });
}

function init() {
  el.score.value = EXAMPLE;
  newRun(EXAMPLE, { budget: 32 });
  app.g = restore(app.b.recs[0]);
  app.t = 0;
  renderEditor();
  bind();
  say(el.scoreStatus, `Parsed. Recorded ${headOf(app.b)} ticks.`, 'ok');
  say(el.stageStatus, 'Every tick is a savestate. Click the strip to load any tick.');
  draw();
  requestAnimationFrame(idle);
}

init();
