// Deterministic stand-in scene. Integer state only, no DOM, no clock reads.
// One call to step() is one tick. The whole state is a plain object, so any
// tick can be serialized, hashed and restored.

export const TILE = 20, COLS = 32, ROWS = 18, SUB = 256;
export const W = COLS * TILE, H = ROWS * TILE;
export const KEYS = ['w', 'a', 's', 'd', 'lmb', 'rmb'];
export const BIT = { w: 1, a: 2, s: 4, d: 8, lmb: 16, rmb: 32 };
export const EVENTS = ['door.open', 'load.begin', 'load.done', 'area.ready', 'npc.down', 'pc.down'];
export const SEED = 3600000; // srand(GetTickCount()) at boot, with uptime0 = 3,600,000 ms
export const DOOR_US = 300000;
export const LOAD_US = 900000;
export const FACES = ['E', 'SE', 'S', 'SW', 'W', 'NW', 'N', 'NE'];
export const BUDGETS = [8, 32, 128];

const HALF = 7 * SUB;
const REACH_PC = 30, REACH_H = 26;

export const MAPS = {
  1: [
    '                                ',
    ' ###########                    ',
    ' #.........#                    ',
    ' #.........#                    ',
    ' #.........#                    ',
    ' #.........#####################',
    ' #.........#...................#',
    ' #.........#.................LL#',
    ' #.........D.................LL#',
    ' #.........D.................LL#',
    ' #.........#.................LL#',
    ' #.........#...................#',
    ' #.........#####################',
    ' #.........#                    ',
    ' #.........#                    ',
    ' #.........#                    ',
    ' ###########                    ',
    '                                ',
  ],
  2: [
    '################################',
    '#..............................#',
    '#..............................#',
    '#.....##..........##...........#',
    '#.....##..........##...........#',
    '#..............................#',
    '#...........######.............#',
    '#...........#..................#',
    '#...........#..................#',
    '#...........#.......##.........#',
    '#...........#.......##.........#',
    '#...........######.............#',
    '#..............................#',
    '#.....##..........##...........#',
    '#.....##..........##...........#',
    '#..............................#',
    '#..............................#',
    '################################',
  ],
};

// Wander areas for the friendly NPCs, in tiles: [x0, y0, x1, y1] inclusive.
const HOME = { n1: [2, 2, 9, 15], n2: [13, 6, 26, 11] };

const px = (v) => v * SUB;
export const toPx = (u) => u / SUB;

export function tileAt(area, cx, cy) {
  const row = MAPS[area][cy];
  return row === undefined || cx < 0 || cx >= COLS ? ' ' : row[cx];
}

function solid(g, cx, cy) {
  const ch = tileAt(g.area, cx, cy);
  return ch === '#' || ch === ' ' || (ch === 'D' && g.door < DOOR_US);
}

// MSVC rand(): holdrand = holdrand * 214013 + 2531011, result is bits 16..30.
export function rand(g, label) {
  g.rng = (Math.imul(g.rng, 214013) + 2531011) >>> 0;
  g.draws.push(label);
  return (g.rng >>> 16) & 0x7fff;
}

function fire(g, ev) {
  g.fired.push(ev);
  g.ev[ev] = (g.ev[ev] || 0) + 1;
}

export function initState(cfg = {}) {
  const g = {
    v: 1, tick: 0, vt: 0, hz: 60, seg0t: 0, seg0v: 0,
    rng: SEED, draws: [], area: 1, epoch: 1, mode: 'play', load: 0, door: 0,
    in: 0, ents: [
      { id: 'pc', kind: 'pc', x: px(70), y: px(110), face: 0, hp: 30, max: 30, act: 'idle', cd: 0 },
      { id: 'n1', kind: 'droid', x: px(150), y: px(270), face: 4, hp: 12, max: 12, act: 'idle', wt: 0, dir: -1 },
      { id: 'n2', kind: 'guard', x: px(430), y: px(150), face: 4, hp: 20, max: 20, act: 'idle', wt: 0, dir: -1 },
    ],
    ps: null, ev: {}, fired: [], msg: '',
    cfg: { budget: cfg.budget || 32, host: !!cfg.host },
  };
  g.cur = { ip: 0, used: 0, base: 0, end: false, src: -1, seg: 'start', wait: false, halt: '' };
  return g;
}

// Score cursor. Lives in state.cur: restored with the state, left out of the hash.
function nextInput(g, prog) {
  const c = g.cur;
  const ins = prog.ins;
  if (g.tick === 0) { g.hz = prog.hz0; g.seg0t = 0; g.seg0v = 0; c.seg = 'start'; }
  c.wait = false;
  for (;;) {
    const it = ins[c.ip];
    if (!it) { c.end = true; c.src = -1; return 0; }
    if (it.op === 'note') {
      if (c.used < it.count) {
        c.used++;
        c.src = c.ip;
        if (c.used >= it.count) { c.ip++; c.used = 0; }
        c.end = c.ip >= ins.length;
        return it.bits;
      }
      c.ip++; c.used = 0;
      continue;
    }
    if (it.op === 'seg') {
      g.seg0t = g.tick; g.seg0v = g.vt; g.hz = it.hz || prog.hz0; c.seg = it.name;
      c.ip++; c.used = 0;
      continue;
    }
    if (it.op === 'sync') {
      const n = g.ev[it.ev] || 0;
      if (c.used === 0) c.base = n;
      if (n > c.base) { c.ip++; c.used = 0; continue; }
      c.used++;
      c.src = c.ip;
      c.wait = true;
      c.end = false;
      if (c.used > it.timeout && !c.halt) c.halt = `@sync ${it.ev} waited ${it.timeout} ticks`;
      return 0;
    }
    c.ip++; c.used = 0;
  }
}

function blocked(g, e, nx, ny) {
  const x0 = Math.floor((nx - HALF) / (TILE * SUB)), x1 = Math.floor((nx + HALF - 1) / (TILE * SUB));
  const y0 = Math.floor((ny - HALF) / (TILE * SUB)), y1 = Math.floor((ny + HALF - 1) / (TILE * SUB));
  for (let cy = y0; cy <= y1; cy++) for (let cx = x0; cx <= x1; cx++) if (solid(g, cx, cy)) return true;
  for (const o of g.ents) {
    if (o === e || o.hp <= 0) continue;
    if (Math.abs(nx - o.x) < 2 * HALF && Math.abs(ny - o.y) < 2 * HALF) return true;
  }
  return false;
}

function move(g, e, dx, dy) {
  let moved = false;
  if (dx && !blocked(g, e, e.x + dx, e.y)) { e.x += dx; moved = true; }
  if (dy && !blocked(g, e, e.x, e.y + dy)) { e.y += dy; moved = true; }
  return moved;
}

function faceOf(dx, dy) {
  const ax = Math.abs(dx), ay = Math.abs(dy);
  if (ax === 0 && ay === 0) return -1;
  const sx = ax * 2 < ay ? 0 : Math.sign(dx);
  const sy = ay * 2 < ax ? 0 : Math.sign(dy);
  const table = { '1,0': 0, '1,1': 1, '0,1': 2, '-1,1': 3, '-1,0': 4, '-1,-1': 5, '0,-1': 6, '1,-1': 7 };
  return table[sx + ',' + sy];
}

const dist2 = (a, b) => (a.x - b.x) * (a.x - b.x) + (a.y - b.y) * (a.y - b.y);
const within = (a, b, r) => dist2(a, b) <= px(r) * px(r);
const cellOf = (e) => [Math.floor(e.x / (TILE * SUB)), Math.floor(e.y / (TILE * SUB))];
const speed = (pxPerSec, dt) => Math.floor(pxPerSec * SUB * dt / 1e6);

function stepPlayer(g, pc, bits, prev, dt) {
  if (pc.hp <= 0) { pc.act = 'down'; return; }
  pc.cd = Math.max(0, pc.cd - dt);
  let act = 'idle';
  const press = (bits & BIT.lmb) && !(prev & BIT.lmb);
  if (press && g.area === 1 && g.door === 0) {
    const door = { x: px(230), y: px(180) };
    if (within(pc, door, 36)) { g.door = 1; act = 'use'; g.msg = 'pc opens the door'; }
  }
  const h = g.area === 2 ? g.ents.find((e) => e.id === 'h1') : null;
  if ((bits & BIT.lmb) && h && h.hp > 0 && within(pc, h, REACH_PC)) {
    pc.face = faceOf(h.x - pc.x, h.y - pc.y);
    if (pc.cd === 0) {
      const roll = rand(g, 'pc.d20') % 20 + 1;
      const total = roll + 5;
      const hit = roll === 20 || (roll !== 1 && total >= 13);
      if (hit) {
        let dmg = rand(g, 'pc.dmg') % 6 + 3;
        if (roll === 20) dmg *= 2;
        h.hp = Math.max(0, h.hp - dmg);
        g.msg = `pc d20 ${roll}+5=${total} vs AC 13: ${roll === 20 ? 'critical' : 'hit'}, ${dmg} damage`;
        if (h.hp === 0) { h.act = 'down'; fire(g, 'npc.down'); }
      } else {
        g.msg = `pc d20 ${roll}+5=${total} vs AC 13: miss`;
      }
      pc.cd = 600000;
    }
    act = 'attack';
  }
  let dx = ((bits & BIT.d) ? 1 : 0) - ((bits & BIT.a) ? 1 : 0);
  let dy = ((bits & BIT.s) ? 1 : 0) - ((bits & BIT.w) ? 1 : 0);
  if (dx || dy) {
    let s = speed((bits & BIT.rmb) ? 130 : 80, dt);
    if (dx && dy) s = (s * 181) >> 8;
    const moved = move(g, pc, dx * s, dy * s);
    if (act !== 'attack') pc.face = faceOf(dx, dy);
    if (moved && act === 'idle') act = (bits & BIT.rmb) ? 'run' : 'walk';
  }
  pc.act = act;
}

function stepWander(g, n, dt) {
  n.wt -= dt;
  if (n.wt <= 0) {
    const r = rand(g, n.id + '.wander') % 8;
    n.dir = r < 3 ? -1 : rand(g, n.id + '.wander') % 4;
    n.wt += 400000 + (rand(g, n.id + '.wander') % 6) * 150000;
  }
  n.act = 'idle';
  if (n.dir < 0) return;
  const [ux, uy] = [[1, 0], [0, 1], [-1, 0], [0, -1]][n.dir];
  const s = speed(35, dt);
  const [x0, y0, x1, y1] = HOME[n.id];
  const nx = n.x + ux * s, ny = n.y + uy * s;
  const lo = (v) => px(v * TILE) + HALF, hi = (v) => px((v + 1) * TILE) - HALF;
  if (nx < lo(x0) || nx > hi(x1) || ny < lo(y0) || ny > hi(y1)) { n.dir = -1; return; }
  if (move(g, n, ux * s, uy * s)) { n.act = 'walk'; n.face = faceOf(ux, uy); } else n.dir = -1;
}

// Budgeted A*. The search may expand at most `budget` nodes in the tick it
// runs. If it does not reach the goal, the NPC walks toward the most promising
// node it saw. In the real game the budget is wall time, so it varies.
export function planPath(g, from, to, budget) {
  const passable = (cx, cy) => !solid(g, cx, cy);
  const [sx, sy] = cellOf(from), [gx, gy] = cellOf(to);
  const S = sy * COLS + sx, G = gy * COLS + gx;
  const hh = (i) => {
    const dx = Math.abs((i % COLS) - gx), dy = Math.abs(Math.floor(i / COLS) - gy);
    return 10 * Math.max(dx, dy) + 4 * Math.min(dx, dy);
  };
  const open = [{ i: S, g: 0, h: hh(S), o: 0 }];
  const best = new Map([[S, 0]]);
  const came = new Map();
  const closed = new Set();
  const seen = [];
  let order = 1, n = 0, found = false, top = null;
  while (open.length && n < budget) {
    let bi = 0;
    for (let k = 1; k < open.length; k++) {
      const a = open[k], b = open[bi];
      const fa = a.g + a.h, fb = b.g + b.h;
      if (fa < fb || (fa === fb && (a.h < b.h || (a.h === b.h && a.o < b.o)))) bi = k;
    }
    const cur = open.splice(bi, 1)[0];
    if (closed.has(cur.i)) continue;
    closed.add(cur.i);
    seen.push(cur.i);
    n++;
    if (!top || cur.h < top.h || (cur.h === top.h && cur.g < top.g)) top = cur;
    if (cur.i === G) { found = true; top = cur; break; }
    const cx = cur.i % COLS, cy = Math.floor(cur.i / COLS);
    for (let dy = -1; dy <= 1; dy++) {
      for (let dx = -1; dx <= 1; dx++) {
        if (!dx && !dy) continue;
        const nx = cx + dx, ny = cy + dy;
        if (!passable(nx, ny)) continue;
        if (dx && dy && (!passable(cx + dx, cy) || !passable(cx, cy + dy))) continue;
        const ni = ny * COLS + nx;
        if (closed.has(ni)) continue;
        const ng = cur.g + (dx && dy ? 14 : 10);
        if (!best.has(ni) || ng < best.get(ni)) {
          best.set(ni, ng);
          came.set(ni, cur.i);
          open.push({ i: ni, g: ng, h: hh(ni), o: order++ });
        }
      }
    }
  }
  const path = [];
  for (let i = top ? top.i : S; i !== S && i !== undefined; i = came.get(i)) path.push(i);
  path.reverse();
  return { path, seen, n, b: budget, full: found };
}

function stepHostile(g, h, pc, dt, ctx) {
  if (h.hp <= 0) { h.act = 'down'; return; }
  h.cd = Math.max(0, h.cd - dt);
  if (h.wt > 0) { h.wt -= dt; h.act = 'idle'; return; }
  if (pc.hp <= 0) { h.act = 'idle'; return; }
  if (within(h, pc, REACH_H)) {
    h.face = faceOf(pc.x - h.x, pc.y - h.y);
    h.act = 'attack';
    if (h.cd === 0) {
      const roll = rand(g, 'h1.d20') % 20 + 1;
      const total = roll + 3;
      if (roll === 20 || (roll !== 1 && total >= 14)) {
        const dmg = rand(g, 'h1.dmg') % 4 + 2;
        pc.hp = Math.max(0, pc.hp - dmg);
        g.msg = `h1 d20 ${roll}+3=${total} vs AC 14: hit, ${dmg} damage`;
        if (pc.hp === 0) { pc.act = 'down'; fire(g, 'pc.down'); }
      } else {
        g.msg = `h1 d20 ${roll}+3=${total} vs AC 14: miss`;
      }
      h.cd = 900000;
    }
    return;
  }
  h.rt -= dt;
  if (h.rt <= 0 || h.path.length === 0) {
    let b = g.cfg.budget;
    if (g.cfg.host) b = Math.max(2, Math.floor(b * (0.25 + (ctx.jitter ? ctx.jitter() : Math.random()))));
    const r = planPath(g, h, pc, b);
    h.path = r.path;
    h.rt = 250000;
    g.ps = { seen: r.seen, n: r.n, b: r.b, full: r.full };
  }
  h.act = 'chase';
  if (!h.path.length) return;
  const next = h.path[0];
  const tx = px((next % COLS) * TILE + TILE / 2), ty = px(Math.floor(next / COLS) * TILE + TILE / 2);
  let s = speed(70, dt);
  const ddx = tx - h.x, ddy = ty - h.y;
  if (ddx && ddy) s = (s * 181) >> 8;
  const mx = Math.sign(ddx) * Math.min(Math.abs(ddx), s), my = Math.sign(ddy) * Math.min(Math.abs(ddy), s);
  const moved = move(g, h, mx, my);
  if (moved) h.face = faceOf(mx, my);
  if (h.x === tx && h.y === ty) h.path.shift();
  else if (!moved) h.rt = 0;
}

function enterArea2(g) {
  const pc = g.ents[0];
  g.area = 2; g.epoch++; g.mode = 'play'; g.load = 0;
  pc.x = px(50); pc.y = px(170); pc.face = 0; pc.act = 'idle';
  const idle = 300000 + (rand(g, 'h1.spawn') % 4) * 100000;
  g.ents = [pc, { id: 'h1', kind: 'thug', x: px(550), y: px(170), face: 4, hp: 18, max: 18, act: 'idle', cd: 0, wt: idle, rt: 0, path: [] }];
  g.ps = null;
  fire(g, 'load.done');
  fire(g, 'area.ready');
}

// Advance one tick. prog is the compiled score. ctx.jitter feeds the lab-only
// host-timing budget and is the only input that does not come from the score.
export function step(g, prog, ctx = {}) {
  const prev = g.in;
  g.fired = [];
  g.draws = [];
  const bits = nextInput(g, prog);
  const t = g.tick + 1;
  const vt = g.seg0v + Math.floor((t - g.seg0t) * 1e6 / g.hz);
  const dt = vt - g.vt;
  g.tick = t;
  g.vt = vt;
  g.in = bits;
  const pc = g.ents[0];
  if (g.mode === 'load') {
    rand(g, 'loader');
    g.load -= dt;
    pc.act = 'load';
    if (g.load <= 0) enterArea2(g);
    return g;
  }
  if (g.door > 0 && g.door < DOOR_US) {
    g.door = Math.min(DOOR_US, g.door + dt);
    if (g.door === DOOR_US) fire(g, 'door.open');
  }
  stepPlayer(g, pc, bits, prev, dt);
  for (const e of g.ents) {
    if (e.kind === 'droid' || e.kind === 'guard') stepWander(g, e, dt);
    else if (e.kind === 'thug') stepHostile(g, e, pc, dt, ctx);
  }
  if (g.area === 1 && pc.hp > 0) {
    const [cx, cy] = cellOf(pc);
    if (tileAt(1, cx, cy) === 'L') {
      g.mode = 'load';
      g.load = LOAD_US;
      pc.act = 'load';
      fire(g, 'load.begin');
    }
  }
  return g;
}

// Canonical JSON: sorted keys, so equal states give equal strings.
export function canon(v) {
  if (v === null || typeof v !== 'object') return JSON.stringify(v);
  if (Array.isArray(v)) return '[' + v.map(canon).join(',') + ']';
  const ks = Object.keys(v).sort();
  let out = '{';
  for (let i = 0; i < ks.length; i++) {
    if (v[ks[i]] === undefined) continue;
    out += (out.length > 1 ? ',' : '') + JSON.stringify(ks[i]) + ':' + canon(v[ks[i]]);
  }
  return out + '}';
}

// 64-bit non-cryptographic hash (two 32-bit lanes), as 16 hex digits.
export function hash64(str) {
  let h1 = 0xdeadbeef, h2 = 0x41c6ce57;
  for (let i = 0; i < str.length; i++) {
    const ch = str.charCodeAt(i);
    h1 = Math.imul(h1 ^ ch, 2654435761);
    h2 = Math.imul(h2 ^ ch, 1597334677);
  }
  h1 = Math.imul(h1 ^ (h1 >>> 16), 2246822507) ^ Math.imul(h2 ^ (h2 >>> 13), 3266489909);
  h2 = Math.imul(h2 ^ (h2 >>> 16), 2246822507) ^ Math.imul(h1 ^ (h1 >>> 13), 3266489909);
  return (h2 >>> 0).toString(16).padStart(8, '0') + (h1 >>> 0).toString(16).padStart(8, '0');
}

// The hashed part of a state: everything except the score cursor.
export function gameString(g) {
  const { cur, ...rest } = g;
  return canon(rest);
}

export function keysOf(bits) {
  const out = KEYS.filter((k) => bits & BIT[k]);
  return out.length ? out.join(' ') : '.';
}
