// telos rules for the stand-in: tick flags, verbclass, tiers, the four seek
// modes, verify, diff, bisect and holorecord export/import. No DOM here, so
// node can run all of it.

import { initState, step, gameString, BUDGETS } from './sim.js';
import { parseScore } from './score.js';
import { makeBranch, headOf, extend, restore, stateHash, journalOf, MAX_TICKS } from './holo.js';

const now = () => (typeof performance !== 'undefined' ? performance.now() : Date.now());

export const TIERS = ['lab', 'tas', 'hands-off'];
export const MODES = ['replay', 'restore', 'hybrid', 'semantic'];
export const DEFAULT_MODE = { lab: 'restore', tas: 'replay', 'hands-off': 'replay' };

export function modeAllowed(mode, tier) {
  if (mode === 'replay') return true;
  if (mode === 'semantic') return tier === 'lab';
  return tier === 'lab' || tier === 'tas';
}
export function modeReason(mode, tier) {
  if (modeAllowed(mode, tier)) return '';
  if (mode === 'semantic') return 'Semantic seeks write engine fields, so they need lab tier.';
  return 'Hands-off tier allows replay only. Nothing writes game memory.';
}

// TickFlag and VerbClass, as in contract.md sections 8.4 and 4.2.
export const F = {
  CAPTURED: 0x1, RESTORE_POINT: 0x2, KEYFRAME: 0x4, MATERIALIZED: 0x8, EVICTED: 0x10,
  HOLD: 0x20, EPOCH_START: 0x40, SIM_ROOT: 0x80, FILES: 0x100,
};
export const VC = {
  INPUT: 1 << 0, OS_INPUT: 1 << 1, GATE: 1 << 2, CLOCK: 1 << 3, WAIT: 1 << 4, CAPTURE: 1 << 5,
  READ: 1 << 6, OS_EVENT: 1 << 7, SCREEN: 1 << 8, RESTORE: 1 << 9, FILES_RESTORE: 1 << 10,
  WRITE: 1 << 16, SEMANTIC: 1 << 17, FIXED_CLOCK: 1 << 18, FILES_WRITE: 1 << 19,
};
const VC_SHORT = [
  [VC.INPUT, 'I'], [VC.OS_INPUT, 'O'], [VC.GATE, 'G'], [VC.CLOCK, 'C'], [VC.WAIT, 'W'],
  [VC.CAPTURE, 'P'], [VC.READ, 'R'], [VC.OS_EVENT, 'E'], [VC.SCREEN, 'S'], [VC.RESTORE, 'T'],
  [VC.FILES_RESTORE, 'F'], [VC.WRITE, 'w'], [VC.SEMANTIC, 's'], [VC.FIXED_CLOCK, 'x'], [VC.FILES_WRITE, 'f'],
];
const F_SHORT = [
  [F.CAPTURED, 'c'], [F.RESTORE_POINT, 'r'], [F.KEYFRAME, 'k'], [F.MATERIALIZED, 'm'], [F.EVICTED, 'v'],
  [F.HOLD, 'h'], [F.EPOCH_START, 'e'], [F.SIM_ROOT, 's'], [F.FILES, 'f'],
];
export const LEGEND = 'verbs: I input O os-input G gate C clock W wait P capture R read E os-event S screen ' +
  'T restore F files-restore | lab: w write s semantic x fixed-clock f files-write\n' +
  'flags: c captured r restore-point k keyframe m materialized v evicted h hold e epoch-start s sim-root f files';
export const verbsText = (vc) => VC_SHORT.filter(([b]) => vc & b).map(([, c]) => c).join('') || '-';
export const flagsText = (f) => F_SHORT.filter(([b]) => f & b).map(([, c]) => c).join('') || '-';
export const REACHED = ['recorded', 'replay', 'restore', 'hybrid', 'semantic'];
export const REASONS = { 0: 'none', 1: 'not captured', 2: 'GL mapped', 3: 'file I/O', 4: 'scheduler', 5: 'foreign module', 255: 'other' };

export const KEYFRAME_EVERY = 60;
export const captured = (b) => b.tier !== 'hands-off';
export const isEpochStart = (r) => r.t === 0 || r.ev.includes('load.done');

// Why a tick is not a restore point (0 when it is one). Mid-load ticks stand in
// for the loader's file I/O; hands-off captures nothing.
export function reasonOf(b, r) {
  if (!captured(b)) return 1;
  return r.load ? 3 : 0;
}

export function flagsOf(b, t) {
  const r = b.recs[t];
  if (!r) return 0;
  let f = 0;
  if (captured(b)) {
    f |= F.CAPTURED;
    if (!r.load) f |= F.RESTORE_POINT;
    if (isEpochStart(r) || t % KEYFRAME_EVERY === 0 || r.kf) f |= F.KEYFRAME;
    if (t % (KEYFRAME_EVERY * 10) === 0) f |= F.MATERIALIZED;
  }
  if (r.load) f |= F.HOLD;
  if (isEpochStart(r)) f |= F.EPOCH_START;
  return f;
}

export function verbsOf(b, t) {
  const r = b.recs[t];
  if (!r) return 0;
  let v = captured(b) ? VC.INPUT | VC.GATE | VC.CLOCK | VC.CAPTURE : VC.OS_INPUT | VC.SCREEN;
  if (r.c.wait) v |= VC.WAIT;
  return v | (r.xv || 0);
}

// Half-open tick ranges that were restore points.
export function restoreRanges(b) {
  const out = [];
  let a = -1;
  for (let t = 0; t <= headOf(b); t++) {
    const rp = flagsOf(b, t) & F.RESTORE_POINT;
    if (rp && a < 0) a = t;
    if (!rp && a >= 0) { out.push([a, t]); a = -1; }
  }
  if (a >= 0) out.push([a, headOf(b) + 1]);
  return out;
}

export function epochStartOf(b, T) {
  const ep = b.recs[T].ep;
  for (let t = T; t >= 0; t--) if (isEpochStart(b.recs[t]) && b.recs[t].ep === ep) return t;
  return 0;
}

export function latestRestorePoint(b, T) {
  const ep = b.recs[T].ep;
  for (let t = T; t >= 0; t--) {
    if (b.recs[t].ep !== ep) break;
    if (flagsOf(b, t) & F.RESTORE_POINT) return t;
  }
  return -1;
}

// Pages that differ: the canonical state string cut into 64-byte pages.
export const PAGE = 64;
export function writePages(a, b) {
  const n = Math.ceil(Math.max(a.length, b.length) / PAGE);
  let w = 0;
  for (let i = 0; i < n; i++) if (a.substr(i * PAGE, PAGE) !== b.substr(i * PAGE, PAGE)) w++;
  return w;
}

// Leaf paths that differ between two plain objects.
export function diffFields(a, b, path = '', out = []) {
  if (out.length > 64) return out;
  if (a === b) return out;
  const oa = a && typeof a === 'object', ob = b && typeof b === 'object';
  if (!oa || !ob || Array.isArray(a) !== Array.isArray(b)) { out.push(path || '(root)'); return out; }
  const keys = new Set([...Object.keys(a), ...Object.keys(b)]);
  for (const k of [...keys].sort()) {
    const p = Array.isArray(a) ? `${path}[${k}]` : path ? `${path}.${k}` : k;
    if (!(k in a) || !(k in b)) out.push(p);
    else diffFields(a[k], b[k], p, out);
  }
  return out;
}

// Fields a semantic write-back sets: the journalled engine fields plus the
// score cursor. Everything else keeps its live value.
const SEM_TOP = ['tick', 'vt', 'hz', 'seg0t', 'seg0v', 'rng', 'in', 'area', 'epoch', 'mode', 'door', 'load', 'msg'];
const SEM_ENT = ['x', 'y', 'hp', 'face', 'act'];

function receipt(kind, mode, from, to) {
  return {
    kind, result: 'pass', from_tick: from, to_tick: to, mode, verify_level: '', write_pages: 0,
    micros: 0, state_root: '', journal_sha256: '', pixel_exact: null, canary: 'none', fallback: '',
    first_divergence: null, restore_points: [], replayed: 0, fields: [], note: '',
  };
}

function replayFrom(b, g, from, T, ctx) {
  for (let t = from + 1; t <= T; t++) step(g, b.prog, ctx);
  return T - from;
}

// Seek branch b from the live state to tick T (T <= head) in one mode.
// Returns {g, receipt}. Marks the records it wrote to (RESTORE, SEMANTIC).
export function seek(b, live, T, mode, { tier = b.tier, ctx = {} } = {}) {
  const t0 = now();
  const rc = receipt('seek', mode, live.tick, T);
  rc.restore_points = restoreRanges(b);
  if (!modeAllowed(mode, tier)) {
    rc.result = 'fail';
    rc.note = modeReason(mode, tier);
    return { g: live, receipt: rc };
  }
  if (T < 0 || T > headOf(b)) {
    rc.result = 'fail';
    rc.note = `tick ${T} is not recorded; the head is ${headOf(b)}`;
    return { g: live, receipt: rc };
  }
  const target = b.recs[T];
  const liveStr = gameString(live);
  let g;
  if (mode === 'replay') {
    g = initState(b.cfg);
    rc.replayed = replayFrom(b, g, 0, T, ctx);
    rc.verify_level = 'journal';
    rc.note = `replayed ${T} ticks from boot`;
  } else if (mode === 'semantic') {
    g = JSON.parse(liveStr);
    const want = JSON.parse(target.g);
    for (const k of SEM_TOP) g[k] = want[k];
    for (const e of g.ents) {
      const w = want.ents.find((x) => x.id === e.id);
      if (w) for (const k of SEM_ENT) e[k] = w[k];
    }
    g.cur = { ...target.c };
    rc.write_pages = writePages(liveStr, gameString(g));
    rc.verify_level = 'diff_only';
    const { cur, ...plain } = g;
    rc.fields = diffFields(JSON.parse(JSON.stringify(plain)), want);
    target.xv = (target.xv || 0) | VC.SEMANTIC;
    target.r = 4;
    b.tainted = true;
    rc.note = rc.fields.length ? `${rc.fields.length} fields still differ; the branch is tainted` : 'no field differs; the branch is still tainted';
  } else {
    const ep = target.ep;
    const cross = live.epoch !== ep;
    let K;
    if (cross) {
      K = epochStartOf(b, T);
      rc.fallback = 'epoch start';
      rc.note = `a restore never crosses an epoch; files reset and replay from tick ${K}`;
    } else if (mode === 'restore' && (flagsOf(b, T) & F.RESTORE_POINT)) {
      K = T;
    } else {
      K = latestRestorePoint(b, T);
      if (K < 0) K = epochStartOf(b, T);
      if (mode === 'restore') {
        rc.fallback = 'hybrid';
        rc.note = `tick ${T} is not a restore point (${REASONS[reasonOf(b, target)]}); restored ${K} and replayed`;
      }
    }
    const src = b.recs[K];
    rc.write_pages = writePages(liveStr, src.g);
    g = restore(src);
    src.xv = (src.xv || 0) | VC.RESTORE | (cross ? VC.FILES_RESTORE : 0);
    src.kf = 1;
    src.r = mode === 'restore' && K === T ? 2 : 3;
    rc.replayed = replayFrom(b, g, K, T, ctx);
    rc.verify_level = 'full_root';
    if (!rc.note) rc.note = K === T ? 'wrote the recorded pages, no replay' : `restored ${K}, replayed ${T - K} ticks`;
    if (mode === 'restore' && K === T) {
      if (T < headOf(b)) {
        const probe = restore(src);
        step(probe, b.prog, ctx);
        rc.canary = stateHash(probe) === b.recs[T + 1].h ? 'pass' : 'fail';
      } else rc.canary = 'pending';
    }
  }
  rc.state_root = stateHash(g);
  rc.journal_sha256 = journalOf(g);
  if (mode === 'semantic') {
    if (rc.fields.length) { rc.result = 'fail'; rc.first_divergence = T; }
  } else if (rc.state_root !== target.h || rc.journal_sha256 !== target.j || rc.canary === 'fail') {
    rc.result = 'fail';
    rc.first_divergence = T;
    rc.note = `tick ${T} does not match the recording`;
  }
  rc.micros = Math.max(1, Math.round((now() - t0) * 1000));
  return { g, receipt: rc };
}

// Replay the score and compare with the recording. compare: every_tick, syncs
// or end. fresh parses the score text again instead of using the branch's
// compiled copy. from > 0 restores that tick first.
export function verifyRun(b, { fresh = false, compare = 'every_tick', from = 0, tier = b.tier, ctx = {} } = {}) {
  const t0 = now();
  const head = headOf(b);
  const rc = receipt(fresh ? 'judge' : 'verify', 'replay', from, head);
  rc.verify_level = compare === 'every_tick' ? 'journal' : compare;
  rc.checked = 0;
  let prog = b.prog;
  if (fresh) {
    const p = parseScore(b.score);
    if (p.errors.length) { rc.result = 'fail'; rc.note = `the score has ${p.errors.length} errors`; return rc; }
    prog = p.prog;
  }
  const check = (t) => {
    if (compare === 'end') return t === head;
    if (compare === 'syncs') return t === head || (t > 0 && b.recs[t - 1].c.wait && !b.recs[t].c.wait);
    return true;
  };
  const g = from > 0 ? restore(b.recs[from]) : initState(b.cfg);
  let first = -1;
  if (from === 0 && check(0)) { rc.checked++; if (stateHash(g) !== b.recs[0].h) first = 0; }
  for (let t = from + 1; t <= head && first < 0; t++) {
    step(g, prog, ctx);
    if (!check(t)) continue;
    rc.checked++;
    if (journalOf(g) !== b.recs[t].j || stateHash(g) !== b.recs[t].h) first = t;
  }
  rc.state_root = stateHash(g);
  rc.journal_sha256 = journalOf(g);
  rc.replayed = head - from;
  if (first >= 0) {
    rc.result = 'fail';
    rc.first_divergence = first;
    rc.note = `first divergence at tick ${first}`;
  } else {
    rc.result = tier === 'hands-off' ? 'best_effort' : 'pass';
    rc.note = `identical: ${rc.checked} ticks compared`;
  }
  rc.micros = Math.max(1, Math.round((now() - t0) * 1000));
  if (fresh && from === 0) b.judge = rc;
  return rc;
}

// First differing field at one tick, like textview.tick_differs.
export function tickDiffers(A, B, t, roots) {
  const a = A.recs[t], b = B.recs[t];
  if (!a || !b) return a || b ? 'missing' : null;
  if (a.ep !== b.ep) return 'epoch';
  if (a.j !== b.j) return 'journal_sha256';
  if (roots && a.h !== b.h) return 'state_root';
  return null;
}

export function diffBranches(A, B, { first = false, limit = 50 } = {}) {
  const roots = captured(A) && captured(B);
  const hi = Math.max(headOf(A), headOf(B));
  const list = [];
  let count = 0;
  for (let t = 0; t <= hi; t++) {
    const f = tickDiffers(A, B, t, roots);
    if (!f) continue;
    count++;
    if (list.length < limit) list.push({ tick: t, field: f });
    if (first) break;
  }
  return { list, count, roots, hi };
}

// Binary search for the first differing tick. Assumes a split persists once
// it happens, as git bisect does. Journal-only runs get a linear scan.
export function bisectBranches(A, B) {
  const roots = captured(A) && captured(B);
  const lo0 = 0, hi0 = Math.max(headOf(A), headOf(B));
  if (!roots) {
    const d = diffBranches(A, B, { first: true });
    return { linear: true, lo: lo0, hi: hi0, probes: 0, tick: d.list.length ? d.list[0].tick : -1, field: d.list.length ? d.list[0].field : '' };
  }
  let probes = 0;
  const same = (t) => { probes++; return tickDiffers(A, B, t, true) === null; };
  if (same(hi0)) return { lo: lo0, hi: hi0, probes, tick: -1, field: '' };
  let lo = lo0, hi = hi0;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (same(mid)) lo = mid + 1; else hi = mid;
  }
  return { lo: lo0, hi: hi0, probes, tick: lo, field: tickDiffers(A, B, lo, true) || '?' };
}

// Tier ceiling over every tick, then the effective tier and why.
const RANK = { lab: 0, tas: 1, 'hands-off': 2 };
const LAB_BITS = VC.WRITE | VC.SEMANTIC | VC.FIXED_CLOCK | VC.FILES_WRITE;
const INPROC = VC.INPUT | VC.GATE | VC.CLOCK | VC.CAPTURE | VC.READ | VC.RESTORE | VC.FILES_RESTORE;

export function tierReport(b) {
  let ceil = 'hands-off', why = 'no in-process verb on any tick', whyTick = -1;
  const restoreTicks = [];
  for (let t = 0; t <= headOf(b); t++) {
    const v = verbsOf(b, t);
    if (v & (VC.RESTORE | VC.FILES_RESTORE)) restoreTicks.push(t);
    if (v & LAB_BITS) { if (ceil !== 'lab') { ceil = 'lab'; why = `tick ${t} carries ${verbsText(v & LAB_BITS)} (a lab-only write)`; whyTick = t; } }
    else if (v & INPROC && ceil === 'hands-off') { ceil = 'tas'; why = `in-process verbs ${verbsText(v & INPROC)} from tick ${t}`; whyTick = t; }
  }
  const declared = b.tier;
  const eff = RANK[ceil] < RANK[declared] ? ceil : declared;
  const judged = !!(b.judge && (b.judge.result === 'pass' || b.judge.result === 'best_effort'));
  const reasons = [];
  if (eff === 'lab') reasons.push('the effective tier is lab');
  if (!judged) reasons.push('no passing judge receipt yet; run verify --fresh');
  const publishable = reasons.length === 0;
  return { declared, ceiling: ceil, why, whyTick, effective: eff, restoreTicks, judged, publishable, reasons };
}

// Holorecord export: hashes per tick, never states. Import rebuilds the states
// by replaying each branch's score and checks every hash against the file.
export const HOLO_FORMAT = 'kptas-standin-holo/1';

export function exportHolo(branches, activeId, tier) {
  const byId = new Map(branches.map((b) => [b.id, b]));
  return {
    format: HOLO_FORMAT,
    note: 'Stores per-tick hashes for the deterministic sample scene.',
    tier,
    active: byId.get(activeId) ? byId.get(activeId).name : branches[0].name,
    branches: branches.map((b) => ({
      name: b.name,
      parent: b.parent && byId.get(b.parent) ? byId.get(b.parent).name : null,
      fork_tick: b.forkTick,
      tier: b.tier,
      cfg: { budget: b.cfg.budget },
      score: b.score,
      head: headOf(b),
      tainted: b.tainted,
      receipts: b.receipts.slice(-50),
      ticks: b.recs.map((r) => [r.h, r.ch, r.j, r.xv || 0, r.r || 0, r.kf || 0]),
    })),
  };
}

const HEX16 = /^[0-9a-f]{16}$/;

export function checkHolo(obj) {
  if (!obj || typeof obj !== 'object' || obj.format !== HOLO_FORMAT) return `not a ${HOLO_FORMAT} file`;
  if (!Array.isArray(obj.branches) || !obj.branches.length || obj.branches.length > 64) return 'the file has no branches';
  const names = new Set();
  for (const b of obj.branches) {
    if (!b || typeof b.name !== 'string' || !/^[A-Za-z0-9_.@-]{1,40}$/.test(b.name) || names.has(b.name)) return 'a branch name is missing, repeated or invalid';
    names.add(b.name);
    if (typeof b.score !== 'string' || b.score.length > 200000) return `branch ${b.name} has no score`;
    if (!Number.isInteger(b.head) || b.head < 0 || b.head > MAX_TICKS) return `branch ${b.name} has a bad head`;
    if (!Array.isArray(b.ticks) || b.ticks.length !== b.head + 1) return `branch ${b.name} lists ${Array.isArray(b.ticks) ? b.ticks.length : 0} ticks, not ${b.head + 1}`;
    for (const t of b.ticks) if (!Array.isArray(t) || !HEX16.test(t[0]) || !HEX16.test(t[1])) return `branch ${b.name} has a bad tick entry`;
    if (!TIERS.includes(b.tier)) return `branch ${b.name} has an unknown tier`;
  }
  if (!TIERS.includes(obj.tier)) return 'unknown tier';
  return '';
}

export function importHolo(obj) {
  const err = checkHolo(obj);
  if (err) return { error: err };
  const out = [], report = [];
  const byName = new Map();
  for (const e of obj.branches) {
    const p = parseScore(e.score);
    if (p.errors.length) { report.push({ name: e.name, ok: false, note: `score line ${p.errors[0].line}: ${p.errors[0].msg}` }); continue; }
    const budget = BUDGETS.includes(e.cfg && e.cfg.budget) ? e.cfg.budget : 32;
    const b = makeBranch({ name: e.name, score: e.score, prog: p.prog, cfg: { budget }, tier: e.tier, forkTick: e.fork_tick | 0, parent: e.parent ? byName.get(e.parent) : null });
    extend(b, e.head);
    let first = -1;
    for (let t = 0; t <= e.head; t++) {
      const r = b.recs[t];
      if (!r || r.h !== e.ticks[t][0] || r.ch !== e.ticks[t][1]) { first = t; break; }
      r.xv = e.ticks[t][3] | 0;
      r.r = e.ticks[t][4] | 0;
      r.kf = e.ticks[t][5] | 0;
    }
    b.tainted = !!e.tainted;
    b.receipts = Array.isArray(e.receipts) ? e.receipts.filter((x) => x && typeof x === 'object').slice(-50) : [];
    if (first >= 0) b.div = first;
    byName.set(e.name, b);
    out.push(b);
    report.push({ name: e.name, ok: first < 0, first, ticks: e.head + 1, chain: headOf(b) >= 0 ? b.recs[headOf(b)].ch : '' });
  }
  return { branches: out, report, active: obj.active, tier: obj.tier };
}
