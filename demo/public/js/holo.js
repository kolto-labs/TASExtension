// Holorecord: one record per tick. Each record keeps the serialized state
// (a savestate), the input, the state hash and the running hash chain.

import { initState, step, gameString, hash64 } from './sim.js';
import { parseScore, sameIns } from './score.js';

export const MAX_TICKS = 30000;

export function snapshot(g, prevChain) {
  const gs = gameString(g);
  const h = hash64(gs);
  const ch = hash64((prevChain || 'telos') + ':' + h);
  return {
    t: g.tick, bits: g.in, src: g.cur.src, g: gs, c: { ...g.cur }, h, ch,
    ev: g.fired.slice(), area: g.area, load: g.mode === 'load' ? 1 : 0,
  };
}

export function restore(rec) {
  const g = JSON.parse(rec.g);
  g.cur = { ...rec.c };
  return g;
}

export const stateHash = (g) => hash64(gameString(g));

let nextId = 1;

export function makeBranch({ name, score, prog, cfg, parent = null, forkTick = 0, recs = null }) {
  const b = {
    id: nextId++, name, score, prog, cfg: { ...cfg },
    parent: parent ? parent.id : null, forkTick, recs: recs ? recs.slice() : [],
  };
  if (!b.recs.length) b.recs.push(snapshot(initState(b.cfg), ''));
  return b;
}

export const headOf = (b) => b.recs.length - 1;

// Step a live state one tick on branch b and return the new record.
export function advance(b, g, ctx) {
  step(g, b.prog, ctx);
  return snapshot(g, b.recs[g.tick - 1].ch);
}

// Grow the branch until tick T (or until the score ends when T is Infinity).
export function extend(b, T, ctx = {}, stopAtEnd = false) {
  let g = restore(b.recs[headOf(b)]);
  const limit = Math.min(T, MAX_TICKS);
  while (headOf(b) < limit) {
    if (stopAtEnd && (g.cur.end || g.cur.halt)) break;
    b.recs.push(advance(b, g, ctx));
  }
  return g;
}

// First tick whose score position reaches a changed instruction, or Infinity.
export function forkTickFor(b, prog) {
  if (prog.hz0 !== b.prog.hz0) return 1;
  const A = b.prog.ins, B = prog.ins;
  let d = 0;
  while (d < A.length && d < B.length && sameIns(A[d], B[d], prog.hz0)) d++;
  if (d === A.length && d === B.length) return Infinity;
  let delay = 0;
  if (A[d] && B[d] && A[d].op === 'note' && B[d].op === 'note' && A[d].bits === B[d].bits) {
    delay = Math.min(A[d].count, B[d].count);
  }
  for (let t = 1; t < b.recs.length; t++) {
    const s = b.recs[t].src;
    const idx = s < 0 ? Infinity : s;
    if (idx >= d) return s === d ? t + delay : t;
  }
  return Infinity;
}

// Apply edited score text to branch b in place: keep records before the first
// changed tick, drop the rest. Returns {errors} or {fork}.
export function applyScore(b, text) {
  const p = parseScore(text);
  if (p.errors.length) return { errors: p.errors, prog: null };
  const fork = forkTickFor(b, p.prog);
  b.score = text;
  b.prog = p.prog;
  if (fork <= headOf(b)) b.recs.length = fork;
  return { errors: [], fork, prog: p.prog };
}

export function endTick(b) {
  for (let t = 1; t < b.recs.length; t++) if (b.recs[t].c.end) return t;
  return -1;
}

// Tick at which each mark is reached on this branch (-1 if not yet).
export function markTicks(b) {
  const len = b.prog.ins.length;
  return b.prog.marks.map((m) => {
    let tick = -1;
    for (let t = 1; t < b.recs.length; t++) {
      const r = b.recs[t];
      const hit = m.at >= len ? r.c.end : (r.src < 0 || r.src >= m.at);
      if (hit) { tick = t; break; }
    }
    return { ...m, tick };
  });
}

// Replay the score from tick 0 in a fresh sim and compare every tick hash.
export function verify(b, ctx = {}) {
  const t0 = typeof performance !== 'undefined' ? performance.now() : Date.now();
  const prog = parseScore(b.score).prog;
  const g = initState(b.cfg);
  let prev = snapshot(g, '');
  let first = prev.h === b.recs[0].h ? -1 : 0;
  const head = headOf(b);
  for (let t = 1; t <= head && first < 0; t++) {
    step(g, prog, ctx);
    prev = snapshot(g, prev.ch);
    if (prev.h !== b.recs[t].h) first = t;
  }
  const t1 = typeof performance !== 'undefined' ? performance.now() : Date.now();
  return { checked: first < 0 ? head + 1 : first + 1, first, identical: first < 0, chain: prev.ch, ms: t1 - t0 };
}
