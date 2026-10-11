// Canvas drawing for the stand-in scene, the HUD and the holorecord strip.

import { TILE, COLS, ROWS, W, H, MAPS, BIT, DOOR_US, LOAD_US, toPx } from './sim.js';
import { KEY_ORDER, BUTTONS } from './score.js';

export const C = {
  void: '#05070a', panel: '#0b1016', kolto: '#19e3c1', bacta: '#5ad8ff', ok: '#7dff9a',
  warn: '#ffb000', accent: '#ff4fd8', text: '#d7e3ea', dim: '#7f93a0',
  // Tints of the nine palette colours. Magenta (accent) marks divergence only.
  floor: 'rgba(127, 147, 160, 0.07)', grid: 'rgba(127, 147, 160, 0.10)', wall: 'rgba(127, 147, 160, 0.22)',
};
const MONO = 'ui-monospace, "SFMono-Regular", Menlo, Consolas, "Liberation Mono", monospace';
const SANS = 'system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif';

// Invented glyphs in the spirit of Aurebesh (not the real letterforms).
// Each glyph is a list of strokes in a 6 x 8 box.
const GLYPHS = {
  t: [[0, 1, 6, 1], [3, 1, 1, 8], [3, 1, 5, 8]],
  o: [[0, 0, 6, 0], [6, 0, 6, 8], [0, 8, 6, 8], [0, 0, 0, 3]],
  l: [[0, 0, 0, 8], [0, 8, 6, 4]],
  a: [[0, 0, 6, 8], [6, 0, 0, 8], [0, 4, 6, 4]],
  s: [[6, 0, 0, 0], [0, 0, 6, 8], [6, 8, 0, 8]],
  i: [[0, 4, 6, 4], [3, 0, 3, 8], [1, 0, 5, 0]],
  e: [[0, 0, 6, 4], [6, 4, 0, 8], [3, 0, 3, 8]],
  d: [[0, 0, 6, 0], [6, 0, 3, 8], [3, 8, 0, 0]],
};
export const GLYPH_TEXT = 'tool assisted';

function drawGlyphs(ctx, text, x, y, scale, color) {
  ctx.save();
  ctx.strokeStyle = color;
  ctx.lineWidth = 1.1;
  ctx.lineCap = 'round';
  let cx = x;
  for (const ch of text) {
    if (ch === ' ') { cx += 5 * scale; continue; }
    ctx.beginPath();
    for (const [x0, y0, x1, y1] of GLYPHS[ch] || []) {
      ctx.moveTo(cx + x0 * scale, y + y0 * scale);
      ctx.lineTo(cx + x1 * scale, y + y1 * scale);
    }
    ctx.stroke();
    cx += 9 * scale;
  }
  ctx.restore();
  return cx - x;
}

const pad = (n, w) => String(n).padStart(w, '0');
export function fmtVt(us) {
  const ms = Math.floor(us / 1000);
  return `${pad(Math.floor(ms / 60000), 2)}:${pad(Math.floor(ms / 1000) % 60, 2)}.${pad(ms % 1000, 3)}`;
}

function drawTiles(ctx, g) {
  const map = MAPS[g.area];
  for (let y = 0; y < ROWS; y++) {
    for (let x = 0; x < COLS; x++) {
      const ch = map[y][x];
      const X = x * TILE, Y = y * TILE;
      if (ch === ' ') continue;
      if (ch === '#') {
        ctx.fillStyle = C.wall;
        ctx.fillRect(X, Y, TILE, TILE);
        continue;
      }
      ctx.fillStyle = C.floor;
      ctx.fillRect(X, Y, TILE, TILE);
      ctx.strokeStyle = C.grid;
      ctx.lineWidth = 1;
      ctx.strokeRect(X + 0.5, Y + 0.5, TILE - 1, TILE - 1);
    }
  }
  // Wall edges facing floor, drawn thin in kolto.
  ctx.strokeStyle = 'rgba(25, 227, 193, 0.28)';
  ctx.lineWidth = 1;
  ctx.beginPath();
  for (let y = 0; y < ROWS; y++) {
    for (let x = 0; x < COLS; x++) {
      if (map[y][x] !== '#') continue;
      const open = (dx, dy) => { const r = map[y + dy]; const c = r && r[x + dx]; return c && c !== '#' && c !== ' '; };
      const X = x * TILE, Y = y * TILE;
      if (open(0, 1)) { ctx.moveTo(X, Y + TILE - 0.5); ctx.lineTo(X + TILE, Y + TILE - 0.5); }
      if (open(0, -1)) { ctx.moveTo(X, Y + 0.5); ctx.lineTo(X + TILE, Y + 0.5); }
      if (open(1, 0)) { ctx.moveTo(X + TILE - 0.5, Y); ctx.lineTo(X + TILE - 0.5, Y + TILE); }
      if (open(-1, 0)) { ctx.moveTo(X + 0.5, Y); ctx.lineTo(X + 0.5, Y + TILE); }
    }
  }
  ctx.stroke();
}

function drawDoorAndZone(ctx, g, reduced) {
  if (g.area !== 1) return;
  // Door: two tiles at column 11, rows 8 and 9. It slides up as it opens.
  const open = g.door / DOOR_US;
  const X = 11 * TILE, Y = 8 * TILE;
  const h = Math.round(2 * TILE * (1 - open));
  ctx.fillStyle = C.floor;
  ctx.fillRect(X, Y, TILE, 2 * TILE);
  ctx.strokeStyle = 'rgba(255, 176, 0, 0.5)';
  ctx.strokeRect(X + 0.5, Y + 0.5, TILE - 1, 2 * TILE - 1);
  if (h > 0) {
    ctx.fillStyle = C.warn;
    ctx.globalAlpha = 0.85;
    ctx.fillRect(X + 4, Y, TILE - 8, h);
    ctx.globalAlpha = 1;
  }
  // Load zone: columns 29-30, rows 7-10.
  const LX = 29 * TILE, LY = 7 * TILE, LW = 2 * TILE, LH = 4 * TILE;
  ctx.save();
  ctx.beginPath();
  ctx.rect(LX, LY, LW, LH);
  ctx.clip();
  ctx.fillStyle = 'rgba(25, 227, 193, 0.08)';
  ctx.fillRect(LX, LY, LW, LH);
  ctx.strokeStyle = 'rgba(25, 227, 193, 0.45)';
  const shift = reduced ? 0 : (g.tick % 20);
  ctx.beginPath();
  for (let k = -LH; k < LW + LH; k += 10) {
    ctx.moveTo(LX + k + shift / 2, LY);
    ctx.lineTo(LX + k + shift / 2 - LH, LY + LH);
  }
  ctx.stroke();
  ctx.restore();
  ctx.strokeStyle = C.kolto;
  ctx.strokeRect(LX + 0.5, LY + 0.5, LW - 1, LH - 1);
  ctx.fillStyle = C.kolto;
  ctx.font = `9px ${MONO}`;
  ctx.textAlign = 'center';
  ctx.fillText('LOAD', LX + LW / 2, LY - 4);
}

function drawPath(ctx, g) {
  if (g.area !== 2 || !g.ps) return;
  ctx.fillStyle = 'rgba(90, 216, 255, 0.13)';
  for (const i of g.ps.seen) ctx.fillRect((i % COLS) * TILE + 1, Math.floor(i / COLS) * TILE + 1, TILE - 2, TILE - 2);
  const h = g.ents.find((e) => e.id === 'h1');
  if (!h || h.hp <= 0 || !h.path.length) return;
  ctx.strokeStyle = C.bacta;
  ctx.setLineDash([3, 3]);
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(toPx(h.x), toPx(h.y));
  for (const i of h.path) ctx.lineTo((i % COLS) * TILE + TILE / 2, Math.floor(i / COLS) * TILE + TILE / 2);
  ctx.stroke();
  ctx.setLineDash([]);
}

const FACE_V = [[1, 0], [0.7, 0.7], [0, 1], [-0.7, 0.7], [-1, 0], [-0.7, -0.7], [0, -1], [0.7, -0.7]];

function drawEntities(ctx, g) {
  for (const e of g.ents) {
    const x = toPx(e.x), y = toPx(e.y);
    const color = e.kind === 'pc' ? C.kolto : e.kind === 'thug' ? C.warn : C.bacta;
    const down = e.hp <= 0;
    ctx.globalAlpha = down ? 0.4 : 1;
    ctx.fillStyle = C.void;
    ctx.beginPath();
    ctx.arc(x, y, 7, 0, Math.PI * 2);
    ctx.fill();
    ctx.lineWidth = 2;
    ctx.strokeStyle = color;
    ctx.stroke();
    if (e.kind === 'pc') { ctx.fillStyle = color; ctx.beginPath(); ctx.arc(x, y, 3, 0, Math.PI * 2); ctx.fill(); }
    if (e.kind === 'droid') { ctx.strokeRect(x - 3, y - 3, 6, 6); }
    const f = FACE_V[e.face] || [0, 0];
    ctx.beginPath();
    ctx.moveTo(x + f[0] * 7, y + f[1] * 7);
    ctx.lineTo(x + f[0] * 12, y + f[1] * 12);
    ctx.stroke();
    if (down) {
      ctx.beginPath();
      ctx.moveTo(x - 5, y - 5); ctx.lineTo(x + 5, y + 5);
      ctx.moveTo(x + 5, y - 5); ctx.lineTo(x - 5, y + 5);
      ctx.stroke();
    }
    // HP bar and id.
    ctx.fillStyle = 'rgba(127, 147, 160, 0.35)';
    ctx.fillRect(x - 9, y - 14, 18, 2);
    ctx.fillStyle = e.kind === 'thug' ? C.warn : C.ok;
    ctx.fillRect(x - 9, y - 14, Math.round(18 * e.hp / e.max), 2);
    ctx.globalAlpha = 1;
    ctx.fillStyle = C.dim;
    ctx.font = `9px ${MONO}`;
    ctx.textAlign = 'center';
    ctx.fillText(e.id, x, y + 18);
  }
}

function drawLoading(ctx, g) {
  ctx.fillStyle = C.void;
  ctx.fillRect(0, 0, W, H);
  const p = Math.max(0, Math.min(1, 1 - g.load / LOAD_US));
  ctx.fillStyle = C.text;
  ctx.font = `600 16px ${SANS}`;
  ctx.textAlign = 'center';
  ctx.fillText('Loading', W / 2, H / 2 - 16);
  ctx.strokeStyle = C.dim;
  ctx.strokeRect(W / 2 - 120.5, H / 2 - 4.5, 240, 8);
  ctx.fillStyle = C.kolto;
  ctx.fillRect(W / 2 - 119, H / 2 - 3, Math.round(238 * p), 6);
  ctx.fillStyle = C.dim;
  ctx.font = `11px ${MONO}`;
  ctx.fillText(`sample area 2   ${Math.round(p * 100)}%`, W / 2, H / 2 + 24);
}

function panel(ctx, x, y, w, h) {
  ctx.fillStyle = 'rgba(5, 7, 10, 0.78)';
  ctx.fillRect(x, y, w, h);
  ctx.strokeStyle = 'rgba(127, 147, 160, 0.35)';
  ctx.lineWidth = 1;
  ctx.strokeRect(x + 0.5, y + 0.5, w - 1, h - 1);
}

function key(ctx, x, y, w, label, on) {
  ctx.fillStyle = on ? C.kolto : 'rgba(5, 7, 10, 0.9)';
  ctx.fillRect(x, y, w, 15);
  ctx.strokeStyle = on ? C.kolto : 'rgba(127, 147, 160, 0.5)';
  ctx.strokeRect(x + 0.5, y + 0.5, w - 1, 14);
  ctx.fillStyle = on ? C.void : C.dim;
  ctx.font = `600 9px ${MONO}`;
  ctx.textAlign = 'center';
  ctx.fillText(label, x + w / 2, y + 11);
}

export const TIER_COLOR = { lab: C.warn, tas: C.kolto, 'hands-off': C.ok };

const KEY_COLORS = { w: C.kolto, a: C.bacta, s: C.ok, d: C.text };
const TIMELINE_ACTIONS = [...KEY_ORDER, ...BUTTONS].map((name, index) => ({
  name,
  color: KEY_COLORS[name] || `hsl(${Math.round((index * 137.508) % 360)} 82% 67%)`,
  shape: name === 'lmb' ? 'mouse-left' : name === 'rmb' ? 'mouse-right' : 'key',
}));
const ACTION_INDEX = new Map(TIMELINE_ACTIONS.map((action, index) => [action.name, index]));
const MOUSE_POSITION_SHAPE = 'mouse-position';
const timelineInputCaches = new WeakMap();

function packTimelineInput(input = {}) {
  let mask = 0;
  TIMELINE_ACTIONS.forEach((action, i) => { if (input[action.name]) mask |= (1 << i); });
  return { mask, mx: Number.isFinite(input.mx) ? input.mx : null, my: Number.isFinite(input.my) ? input.my : null };
}

function packRecordedInput(rec = {}) {
  let mask = 0;
  for (const name of ['w', 'a', 's', 'd', 'lmb', 'rmb']) {
    const lane = ACTION_INDEX.get(name);
    if (lane >= 0 && ((rec.bits || 0) & BIT[name])) mask |= (1 << lane);
  }
  return { mask, mx: null, my: null };
}

function sameTimelineInput(a, b) {
  return !!a && a.mask === b.mask && a.mx === b.mx && a.my === b.my;
}

function buildTimelineInputCache(info, head, span) {
  const inputAt = typeof info.inputAt === 'function' ? info.inputAt : (tick) => packRecordedInput(info.recs[tick]);
  const source = info.inputSource || info.recs;
  if (!source || typeof source !== 'object') return null;
  let cache = timelineInputCaches.get(source);
  const fresh = !cache || cache.prog !== info.prog;
  if (fresh) {
    cache = { prog: info.prog, states: [], tickEvents: [], buckets: new Map(), head: -1, dirtyBuckets: true };
    timelineInputCaches.set(source, cache);
  }

  let invalidate = Infinity;
  if (!fresh) {
    if (head < cache.head) invalidate = Math.max(0, head + 1);
    for (let k = Math.max(1, cache.head + 1); k <= head; k++) {
      if (Boolean(info.recs[k]?.c?.wait) !== Boolean(info.recs[k - 1]?.c?.wait)) invalidate = Math.min(invalidate, k);
    }
  }
  const current = typeof info.inputAt === 'function' ? packTimelineInput(inputAt(head)) : inputAt(head);
  if (cache.states[head] && !sameTimelineInput(cache.states[head], current)) invalidate = Math.min(invalidate, head);
  if (Number.isFinite(invalidate)) {
    cache.states.length = invalidate;
    cache.tickEvents.length = invalidate;
    cache.dirtyBuckets = true;
  }
  if (cache.states.length > span + 1) {
    cache.states.length = span + 1;
    cache.tickEvents.length = span + 1;
    cache.dirtyBuckets = true;
  }

  const oldLength = cache.states.length;
  for (let k = cache.states.length; k <= span; k++) {
    const state = k === head ? current : (typeof info.inputAt === 'function' ? packTimelineInput(inputAt(k)) : inputAt(k));
    const previous = cache.states[k - 1];
    cache.states[k] = state;
  }
  if (cache.states.length !== oldLength) cache.dirtyBuckets = true;
  if (cache.span !== span || cache.width !== info.width || cache.v0 !== info.v0) cache.dirtyBuckets = true;
  if (cache.dirtyBuckets) {
    cache.buckets.clear();
    const bucketFor = (tick) => {
      const x = Math.round(info.X(tick));
      let bucket = cache.buckets.get(x);
      if (!bucket) {
        bucket = { x, count: 0, presses: 0, releases: 0, first: null, runCount: 0, futureCount: 0 };
        cache.buckets.set(x, bucket);
      }
      return bucket;
    };
    cache.tickEvents.length = cache.states.length;
    for (let k = 0; k < cache.states.length; k++) {
      const state = cache.states[k], previous = cache.states[k - 1];
      const changed = previous ? previous.mask ^ state.mask : state.mask;
      const mouseChanged = previous ? previous.mx !== state.mx || previous.my !== state.my : state.mx !== null && state.my !== null;
      let tickEvent = null;
      const addAction = (action, pressed) => {
        const bucket = bucketFor(k);
        if (!tickEvent) tickEvent = cache.tickEvents[k] = { x: bucket.x, count: 0, presses: 0, releases: 0, first: null };
        tickEvent.count++;
        if (!tickEvent.first) tickEvent.first = { action, pressed };
        if (!bucket.first) bucket.first = tickEvent.first;
        bucket.count++;
        if (k <= head) bucket.runCount++; else bucket.futureCount++;
        if (pressed) { tickEvent.presses++; bucket.presses++; }
        else { tickEvent.releases++; bucket.releases++; }
      };
      for (let i = 0; i < TIMELINE_ACTIONS.length; i++) {
        const bit = 1 << i;
        if (!(changed & bit)) continue;
        addAction(TIMELINE_ACTIONS[i], !!(state.mask & bit));
      }
      if (mouseChanged) {
        const action = { shape: MOUSE_POSITION_SHAPE, color: C.bacta };
        const pressed = state.mx !== null && state.my !== null;
        addAction(action, pressed);
      }
    }
    cache.span = span;
    cache.width = info.width;
    cache.v0 = info.v0;
    cache.dirtyBuckets = false;
  } else if (cache.head !== head) {
    for (let k = Math.max(0, cache.head + 1); k <= head; k++) {
      const event = cache.tickEvents[k];
      if (!event) continue;
      const bucket = cache.buckets.get(event.x);
      if (!bucket) continue;
      bucket.runCount += event.count;
      bucket.futureCount -= event.count;
    }
  }
  cache.head = head;
  return cache;
}

function drawActionShape(ctx, x, y, action, pressed, alpha = 1) {
  ctx.save();
  ctx.globalAlpha = alpha;
  ctx.beginPath();
  if (action.shape === 'key') ctx.arc(x, y, 2.15, 0, Math.PI * 2);
  else if (action.shape === 'mouse-left') {
    ctx.moveTo(x, y - 2.5); ctx.lineTo(x + 2.5, y + 2); ctx.lineTo(x - 2.5, y + 2); ctx.closePath();
  } else if (action.shape === 'mouse-right') {
    ctx.moveTo(x, y - 2.5); ctx.lineTo(x + 2.5, y); ctx.lineTo(x, y + 2.5); ctx.lineTo(x - 2.5, y); ctx.closePath();
  } else {
    ctx.moveTo(x - 2.5, y); ctx.lineTo(x + 2.5, y); ctx.moveTo(x, y - 2.5); ctx.lineTo(x, y + 2.5);
  }
  if (action.shape === MOUSE_POSITION_SHAPE) {
    ctx.strokeStyle = C.bacta;
    ctx.lineWidth = 1;
    ctx.stroke();
  } else if (pressed) {
    ctx.fillStyle = action.color;
    ctx.fill();
  } else {
    ctx.strokeStyle = action.color;
    ctx.lineWidth = 1;
    ctx.stroke();
  }
  ctx.restore();
}

function drawTimelineInputEvents(ctx, buckets, y) {
  for (const bucket of buckets.values()) {
    const alpha = bucket.runCount && bucket.futureCount ? 0.72 : bucket.runCount ? 1 : 0.48;
    if (bucket.count === 1) drawActionShape(ctx, bucket.x, y + 2, bucket.first.action, bucket.first.pressed, alpha);
    else {
      const label = `${bucket.presses ? `+${bucket.presses}` : ''}${bucket.presses && bucket.releases ? ' ' : ''}${bucket.releases ? `−${bucket.releases}` : ''}`;
      ctx.save();
      ctx.globalAlpha = alpha;
      ctx.font = `7px ${MONO}`;
      ctx.fillStyle = bucket.releases && !bucket.presses ? C.panel : 'rgba(5, 7, 10, 0.92)';
      ctx.strokeStyle = bucket.presses && !bucket.releases ? C.kolto : C.text;
      ctx.lineWidth = 1;
      const badgeW = Math.max(14, ctx.measureText(label).width + 4);
      ctx.fillRect(bucket.x - badgeW / 2, y - 1, badgeW, 10);
      ctx.strokeRect(bucket.x - badgeW / 2 + 0.5, y - 0.5, badgeW - 1, 9);
      ctx.fillStyle = C.text;
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.fillText(label, bucket.x, y + 4);
      ctx.restore();
    }
  }
}

function timelineInputDescription(input, previous, tick, head) {
  const held = TIMELINE_ACTIONS.filter((action) => input?.[action.name]).map((action) => action.name);
  const pressed = previous ? TIMELINE_ACTIONS.filter((action) => input?.[action.name] && !previous[action.name]).map((action) => action.name) : [];
  const released = previous ? TIMELINE_ACTIONS.filter((action) => !input?.[action.name] && previous[action.name]).map((action) => action.name) : [];
  const parts = [`Tick ${tick}`];
  parts.push(tick <= head ? 'recorded branch' : 'authored score ahead; not recorded yet');
  parts.push(held.length ? `held ${held.join(', ')}` : 'no buttons held');
  if (pressed.length) parts.push(`pressed ${pressed.join(', ')}`);
  if (released.length) parts.push(`released ${released.join(', ')}`);
  if (input?.mx !== null && input?.my !== null) parts.push('mouse position command set; coordinates are not plotted as a movement path');
  return parts.join('; ');
}

const TIMELINE_MARKER_HELP = 'Score-input markers: keyboard actions use colored circles; left mouse is a triangle; right mouse is a diamond; a crosshair is an authored mouse-position command, not a movement path. Filled shapes are presses and outlines are releases. Full opacity marks ticks within the recorded branch; muted opacity marks authored input beyond its head. Chords and pixel-colliding ticks use a count badge; hover or focus a tick for its exact held, pressed, and released input names.';

// Returns the watermark box so the page can show its hover text.
function drawHud(ctx, g, o) {
  // Tick and virtual time.
  panel(ctx, 8, 8, 150, 44);
  ctx.textAlign = 'left';
  ctx.fillStyle = C.dim;
  ctx.font = `9px ${MONO}`;
  ctx.fillText('TICK', 16, 22);
  ctx.fillText('VT', 16, 42);
  ctx.fillStyle = C.text;
  ctx.font = `600 13px ${MONO}`;
  ctx.fillText(pad(g.tick, 6), 48, 23);
  ctx.fillText(fmtVt(g.vt), 48, 43);
  ctx.fillStyle = C.dim;
  ctx.font = `9px ${MONO}`;
  ctx.textAlign = 'right';
  ctx.fillText(`${g.hz} Hz`, 150, 22);
  // Scene label.
  ctx.textAlign = 'center';
  ctx.fillStyle = C.dim;
  ctx.fillText(`sample scene · area ${g.area}${g.mode === 'load' ? ' · loading' : ''}`, W / 2, 16);
  // Tier badge.
  const tier = o.tier || 'lab';
  const label = tier.toUpperCase();
  ctx.font = `700 10px ${MONO}`;
  const tw = ctx.measureText(label).width + 16;
  ctx.fillStyle = 'rgba(5, 7, 10, 0.78)';
  ctx.fillRect(W - 8 - tw, 8, tw, 18);
  ctx.strokeStyle = TIER_COLOR[tier];
  ctx.strokeRect(W - 8 - tw + 0.5, 8.5, tw - 1, 17);
  ctx.fillStyle = TIER_COLOR[tier];
  ctx.textAlign = 'center';
  ctx.fillText(label, W - 8 - tw / 2, 21);
  // Input display.
  panel(ctx, 8, H - 50, 124, 42);
  const b = g.in;
  key(ctx, 30, H - 45, 18, 'W', b & BIT.w);
  key(ctx, 10, H - 28, 18, 'A', b & BIT.a);
  key(ctx, 30, H - 28, 18, 'S', b & BIT.s);
  key(ctx, 50, H - 28, 18, 'D', b & BIT.d);
  key(ctx, 74, H - 45, 26, 'LMB', b & BIT.lmb);
  key(ctx, 102, H - 45, 26, 'RMB', b & BIT.rmb);
  ctx.fillStyle = g.cur && g.cur.wait ? C.warn : C.dim;
  ctx.font = `9px ${MONO}`;
  ctx.textAlign = 'left';
  ctx.fillText(g.cur && g.cur.wait ? 'SYNC WAIT' : (g.cur && g.cur.end ? 'SCORE END' : 'SCORE'), 74, H - 18);
  // Combat line.
  if (g.msg && g.mode !== 'load') {
    ctx.textAlign = 'center';
    ctx.fillStyle = C.dim;
    ctx.fillText(g.msg, W / 2, H - 10);
  }
  // Watermark.
  let box = null;
  if (o.watermark !== false) {
    const gw = 12 * 9 * 1 + 5;
    const x = W - 12 - gw, y = H - 44;
    ctx.globalAlpha = 0.9;
    ctx.fillStyle = C.kolto;
    ctx.font = `800 15px ${MONO}`;
    ctx.textAlign = 'right';
    ctx.fillText('TAS', W - 12, y + 10);
    drawGlyphs(ctx, GLYPH_TEXT, x, y + 18, 1, 'rgba(25, 227, 193, 0.85)');
    ctx.globalAlpha = 1;
    box = { x: x - 4, y: y - 6, w: gw + 8, h: 36 };
  }
  return box;
}

export function drawScene(ctx, g, o = {}) {
  ctx.fillStyle = C.void;
  ctx.fillRect(0, 0, W, H);
  if (g.mode === 'load') {
    drawLoading(ctx, g);
  } else {
    drawTiles(ctx, g);
    drawDoorAndZone(ctx, g, o.reduced);
    if (o.showPath) drawPath(ctx, g);
    drawEntities(ctx, g);
  }
  return drawHud(ctx, g, o);
}

// Holorecord strip. info: {recs, t, hover, marks, div, v0, v1, captured}
export function drawTimeline(ctx, w, h, info) {
  const { recs, t, marks, v0, v1 } = info;
  const head = recs.length - 1;
  const n = Math.max(1, v1 - v0);
  const X = (tick) => ((tick - v0) / n) * (w - 16) + 8;
  if (info.compact) {
    const y = Math.round((h - 4) / 2), trackH = 4;
    ctx.fillStyle = C.panel;
    ctx.fillRect(0, 0, w, h);
    ctx.fillStyle = 'rgba(5, 7, 10, 0.85)';
    ctx.fillRect(8, y, w - 16, trackH);
    const areaColor = (rec) => rec.load ? 'rgba(255, 176, 0, 0.76)' : rec.area === 1 ? 'rgba(90, 216, 255, 0.78)' : 'rgba(25, 227, 193, 0.78)';
    const last = Math.min(head, v1);
    let start = Math.max(0, v0);
    for (let k = start + 1; k <= last + 1; k++) {
      if (k === last + 1 || areaColor(recs[k]) !== areaColor(recs[start])) {
        const kindColor = areaColor(recs[start]);
        ctx.fillStyle = kindColor;
        ctx.fillRect(X(start), y, Math.max(1, X(k) - X(start)), trackH);
        start = k;
      }
    }
    const actionCache = buildTimelineInputCache({ ...info, X, width: w }, head, Math.max(v1, 0));
    const describeTick = Number.isSafeInteger(info.hover) && info.hover >= 0 ? info.hover : t;
    const describedInput = typeof info.inputAt === 'function' ? info.inputAt(describeTick) : null;
    const previousInput = describedInput && describeTick > 0 ? info.inputAt(describeTick - 1) : null;
    const markerHelp = describedInput
      ? `${timelineInputDescription(describedInput, previousInput, describeTick, head)}. ${TIMELINE_MARKER_HELP}`
      : `Only recorded WASD and mouse-button transitions are available for this timeline. ${TIMELINE_MARKER_HELP}`;
    if (ctx.canvas.title !== markerHelp) ctx.canvas.title = markerHelp;
    if (ctx.canvas.getAttribute('aria-description') !== markerHelp) ctx.canvas.setAttribute('aria-description', markerHelp);
    if (head < v1) {
      ctx.fillStyle = 'rgba(127, 147, 160, 0.5)';
      for (let x = X(head + 1); x < w - 8; x += 6) ctx.fillRect(x, y, 1, trackH);
    }
    if (actionCache) drawTimelineInputEvents(ctx, actionCache.buckets, y);
    for (const mark of marks) {
      if (mark.tick < v0 || mark.tick > v1 || mark.tick < 0) continue;
      const x = Math.round(X(mark.tick));
      ctx.fillStyle = C.warn;
      ctx.fillRect(x, y - 2, 2, trackH + 4);
    }
    if (info.div != null && info.div >= v0 && info.div <= v1) {
      const x = Math.round(X(info.div));
      ctx.fillStyle = C.accent;
      ctx.fillRect(x, y - 3, 2, trackH + 6);
    }
    if (info.hover >= 0) {
      const x = Math.round(X(info.hover));
      ctx.fillStyle = 'rgba(215, 227, 234, 0.6)';
      ctx.fillRect(x, y - 2, 1, trackH + 4);
    }
    if (t >= v0 && t <= v1) {
      const x = Math.round(X(t));
      ctx.fillStyle = C.ok;
      ctx.fillRect(x - 1, 1, 3, h - 2);
    }
    return { X, tickAt: (x) => Math.round(v0 + ((x - 8) / (w - 16)) * n) };
  }
  ctx.fillStyle = C.panel;
  ctx.fillRect(0, 0, w, h);
  const bandY = 4, bandH = 12, trackY = 20, trackH = 26;
  // Epoch bands: area 1, loading, area 2.
  let start = Math.max(0, v0);
  const kind = (r) => (r.load ? 'load' : 'a' + r.area);
  const colors = { a1: 'rgba(90, 216, 255, 0.28)', load: 'rgba(255, 176, 0, 0.35)', a2: 'rgba(25, 227, 193, 0.28)' };
  const names = { a1: 'area 1', load: 'load', a2: 'area 2' };
  const last = Math.min(head, v1);
  for (let k = start + 1; k <= last + 1; k++) {
    if (k === last + 1 || kind(recs[k]) !== kind(recs[start])) {
      const kk = kind(recs[start]);
      const x0 = X(start), x1 = X(k);
      ctx.fillStyle = colors[kk];
      ctx.fillRect(x0, bandY, Math.max(1, x1 - x0), bandH);
      if (x1 - x0 > 36) {
        ctx.fillStyle = C.text;
        ctx.font = `9px ${MONO}`;
        ctx.textAlign = 'left';
        ctx.fillText(names[kk], x0 + 4, bandY + 9);
      }
      start = k;
    }
  }
  // Track and greenzone.
  ctx.fillStyle = 'rgba(5, 7, 10, 0.7)';
  ctx.fillRect(8, trackY, w - 16, trackH);
  if (head >= v0) {
    const g1 = Math.min(head, v1);
    ctx.fillStyle = info.captured === false ? 'rgba(127, 147, 160, 0.14)' : 'rgba(25, 227, 193, 0.22)';
    ctx.fillRect(X(Math.max(v0, 0)), trackY, Math.max(1, X(g1 + 1) - X(Math.max(v0, 0))), trackH);
    // Mid-load ticks are not restore points: shade them amber.
    ctx.fillStyle = 'rgba(255, 176, 0, 0.25)';
    for (let k = Math.max(v0, 0); k <= g1; k++) if (recs[k].load) ctx.fillRect(X(k), trackY + trackH - 5, Math.max(1, X(k + 1) - X(k)), 5);
  }
  if (head < v1) {
    ctx.strokeStyle = 'rgba(127, 147, 160, 0.25)';
    ctx.beginPath();
    for (let x = X(Math.max(head + 1, v0)); x < w - 8; x += 6) { ctx.moveTo(x, trackY + trackH); ctx.lineTo(x + 6, trackY); }
    ctx.stroke();
  }
  // Scale.
  const stepChoices = [1, 5, 10, 25, 50, 100, 200, 500, 1000, 2000, 5000];
  const step = stepChoices.find((s) => (s / n) * (w - 16) >= 56) || 10000;
  ctx.fillStyle = C.dim;
  ctx.font = `9px ${MONO}`;
  ctx.textAlign = 'center';
  ctx.strokeStyle = 'rgba(127, 147, 160, 0.4)';
  ctx.beginPath();
  for (let k = Math.ceil(v0 / step) * step; k <= v1; k += step) {
    const x = Math.round(X(k)) + 0.5;
    ctx.moveTo(x, trackY + trackH);
    ctx.lineTo(x, trackY + trackH + 4);
    ctx.fillText(String(k), x, trackY + trackH + 14);
  }
  ctx.stroke();
  // Marks.
  for (const m of marks) {
    if (m.tick < v0 || m.tick > v1 || m.tick < 0) continue;
    const x = Math.round(X(m.tick)) + 0.5;
    ctx.strokeStyle = 'rgba(255, 176, 0, 0.7)';
    ctx.beginPath();
    ctx.moveTo(x, trackY);
    ctx.lineTo(x, trackY + trackH);
    ctx.stroke();
    ctx.fillStyle = C.warn;
    ctx.beginPath();
    ctx.moveTo(x, trackY - 1); ctx.lineTo(x + 4, trackY + 3); ctx.lineTo(x, trackY + 7); ctx.lineTo(x - 4, trackY + 3);
    ctx.fill();
    ctx.font = `9px ${MONO}`;
    const mw = ctx.measureText(m.name).width;
    const right = x + 3 + mw > w - 8;
    ctx.textAlign = right ? 'right' : 'left';
    ctx.fillText(m.name, right ? x - 3 : x + 3, trackY + 16);
  }
  // First divergence: the only use of magenta.
  if (info.div != null && info.div >= 0 && info.div >= v0 && info.div <= v1) {
    const x = Math.round(X(info.div)) + 0.5;
    ctx.strokeStyle = C.accent;
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.moveTo(x, bandY);
    ctx.lineTo(x, trackY + trackH);
    ctx.stroke();
    ctx.lineWidth = 1;
  }
  // Hover.
  if (info.hover >= 0) {
    const x = Math.round(X(info.hover)) + 0.5;
    ctx.strokeStyle = 'rgba(215, 227, 234, 0.4)';
    ctx.beginPath();
    ctx.moveTo(x, trackY);
    ctx.lineTo(x, trackY + trackH);
    ctx.stroke();
  }
  // Playhead.
  if (t >= v0 && t <= v1) {
    const x = Math.round(X(t)) + 0.5;
    ctx.strokeStyle = C.ok;
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.moveTo(x, bandY);
    ctx.lineTo(x, trackY + trackH + 2);
    ctx.stroke();
    ctx.lineWidth = 1;
    const label = String(t);
    ctx.font = `600 9px ${MONO}`;
    const lw = ctx.measureText(label).width + 8;
    const lx = Math.min(Math.max(x - lw / 2, 2), w - lw - 2);
    ctx.fillStyle = C.ok;
    ctx.fillRect(lx, trackY + trackH + 5, lw, 12);
    ctx.fillStyle = C.void;
    ctx.textAlign = 'center';
    ctx.fillText(label, lx + lw / 2, trackY + trackH + 14);
  }
  return { X, tickAt: (x) => Math.round(v0 + ((x - 8) / (w - 16)) * n) };
}

// Small picture of one state, for branch thumbnails. Returns a data: URL.
export function thumbnail(g, o, w = 320) {
  const c = document.createElement('canvas');
  c.width = w;
  c.height = Math.round(w * H / W);
  const x = c.getContext('2d');
  x.setTransform(w / W, 0, 0, w / W, 0, 0);
  drawScene(x, g, { ...o, reduced: true });
  return c.toDataURL('image/png');
}
