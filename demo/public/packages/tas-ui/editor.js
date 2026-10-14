// Page wiring: transport, score editor, piano roll, branches, history,
// memory, console, and tiers. Core owns the game and recorded-footage views.

import { extensions, TAS_ID } from '../../js/extensions.js';
import { W, H, initState, BIT } from '../../js/sim.js';
import { parseScore, EXAMPLE, setKey, heldIn, inputTokens, KEY_ORDER, XBIT, BUTTONS } from '../../js/score.js';
import {
  makeBranch, headOf, extend, advance, restore, applyScore, snapshot, MAX_TICKS,
} from '../../js/holo.js';
import {
  TIERS, MODES, DEFAULT_MODE, modeAllowed, modeReason, seek, verifyRun, diffBranches, bisectBranches,
  tierReport, exportHolo, importHolo, flagsOf, verbsOf, verbsText, flagsText, F, LEGEND, REACHED, HOLO_FORMAT,
} from '../../js/telos.js';
import { buildRows, rowMarks, rowSegments, rowInput, editTicks, unrollRepeats, insertTicks, deleteTicks, splitAt, addMark, ticksAsScore, clipboardInputs, pasteTicks } from '../../js/edit.js';
import { applyLanePattern } from '../../js/input-lane-pattern.js';
import { preferredTimelineDensity, rememberTimelineDensity } from '../../js/timeline-density.js';
import { Roll, COLS, FIRST_INPUT, stateText } from '../../js/roll.js';
import { drawScene, drawTimeline, fmtVt, thumbnail, TIER_COLOR } from '../../js/render.js';
import { compileWatch, readWatch, fmtWatch, sparkline, hexView } from '../../js/memory.js';
import { KeyboardControls, loadKeyboardSettings } from '../../js/keyboard-controls.js';
import { compileBreakpoint, loadBreakpoints, saveBreakpointEnabled, saveBreakpointExpression } from '../../js/breakpoints.js';

import { ListenerScope } from './listener-scope.js';
import { OBSERVATION_DRAFT_SCHEMA } from '../../js/observation-draft.js';

export function createTasEditor(host) {
const events = new ListenerScope();
let initialized = false;
let importRevision = 0;
let dismissDialog = null;
let cancelTimelineDrag = () => {};
const $ = (id) => document.getElementById(id);
const TAS_DOCK_IDS = ['score', 'right', 'transport', 'piano-roll', 'score-history', 'sample-state', 'sample-hex', 'sample-scene', 'console'];
const FOLLOW_PLAYHEAD_KEY = 'telos.piano-roll.follow-playhead';
const WATCHES_KEY = 'telos.memory.watches';
const TIMELINE_EXPANDED_KEY = 'telos.timeline.expanded';
const DEFAULT_WATCHES = ['tick', 'vt', 'rng', 'pc.hp', 'pc.x', 'pc.y', 'h1.hp', 'door', 'ps.n', 'msg'];
function savedTimelineExpanded() {
  try { return localStorage.getItem(TIMELINE_EXPANDED_KEY) === 'true'; }
  catch { return false; }
}
function savedFollowPlayhead() {
  try { return localStorage.getItem(FOLLOW_PLAYHEAD_KEY) !== 'false'; }
  catch { return true; }
}
function savedWatches() {
  try {
    const values = JSON.parse(localStorage.getItem(WATCHES_KEY) || 'null');
    if (Array.isArray(values)) {
      const valid = [...new Set(values.filter((x) => typeof x === 'string').map((x) => x.trim()).filter((x) => x && x.length <= 80))].slice(0, 32);
      return valid.map((expr) => ({ expr, w: compileWatch(expr) }));
    }
  } catch { /* Use the starter watch list when storage is unavailable or invalid. */ }
  return DEFAULT_WATCHES.map((expr) => ({ expr, w: compileWatch(expr) }));
}
function persistWatches() {
  try { localStorage.setItem(WATCHES_KEY, JSON.stringify(app.watches.map((w) => w.expr))); }
  catch { /* Storage can be disabled or full. */ }
}
const reducedMq = matchMedia('(prefers-reduced-motion: reduce)');
const wideMq = matchMedia('(min-width: 1400px) and (min-height: 800px)');

const el = {
  view: $('view'), tl: $('timeline'), score: $('score'), backdrop: $('backdrop'), gutter: $('gutter'),
  errors: $('score-errors'), scoreStatus: $('score-status'), stageStatus: $('stage-status'), seekReport: $('seek-report'),
  play: $('b-play'), playLabel: $('play-label'), iPlay: $('i-play'), iPause: $('i-pause'), rec: $('b-rec'), pad: $('pad'),
  speed: $('speed'), readout: $('readout'), budget: $('budget'), showPath: $('show-path'),
  tip: $('tip'), tier: $('tier'), mode: $('seek-mode'), wm: $('wm'), wmWrap: $('wm-wrap'),
  hTick: $('h-tick'), hEp: $('h-ep'), hHz: $('h-hz'), hName: $('h-name'), hTier: $('h-tier-wrap'),
  branches: $('branches'), history: $('history'), watchBody: $('watch-body'), hex: $('hex'), memNote: $('mem-note'),
  out: $('con-out'), cli: $('cli'), sugg: $('cli-sugg'), menu: $('ctx-menu'), rollLive: $('roll-live'),
  dlg: $('dlg'), dlgMsg: $('dlg-msg'), dlgIn: $('dlg-in'), dlgList: $('dlg-list'), dlgOk: $('dlg-ok'), dlgCancel: $('dlg-cancel'),
  thumbPop: $('thumb-pop'), thumbImg: $('thumb-img'), thumbCap: $('thumb-cap'),
};
const ctx = el.view.getContext('2d');
const tctx = el.tl.getContext('2d');
const simCtx = {};
let tasLifecycleGeneration = 0;

const app = {
  branches: [], b: null, g: null, t: 0,
  tier: 'lab', mode: 'restore', watermark: true, showPath: true,
  playing: false, speed: 1, acc: 0, last: 0,
  wasmInputPending: false, wasmPendingTick: null, wasmReleaseAfterAck: null,
  followPlayhead: savedFollowPlayhead(),
  model: null, marks: [], markMap: new Map(), segs: new Map(), lineTicks: new Map(),
  lines: [], errLines: new Map(), activeLine: -1, selLines: '', editTimer: 0,
  rec: { on: false, bits: 0 },
  watches: savedWatches(), ...loadBreakpoints(),
  memAt: -1, memTime: 0, hoverTick: -1, tlGeom: null, wmBox: null,
  consoleLines: [], held: [], cmdHist: [], cmdPos: 0, name: 'example.score',
  dirty: { view: true, roll: true, rows: true, editor: true, branches: true, history: true, memory: true },
  ...loadKeyboardSettings(), keyCapture: '',
  timelineExpanded: savedTimelineExpanded(),
};
const dirty = (...keys) => { for (const k of keys) app.dirty[k] = true; };
const parsed = new WeakMap();
const stateOf = (rec) => { let s = parsed.get(rec); if (!s) { s = JSON.parse(rec.g); parsed.set(rec, s); } return s; };
const keyboardControls = new KeyboardControls(app, { pause });

// ---------- messages ----------
function say(node, text, tone = '') {
  node.textContent = text;
  node.className = node.className.replace(/\b(ok|warn|bad)\b/g, '').trim() + (tone ? ' ' + tone : '');
}
function emitWasmInput(rec, tick) {
  if (!extensions.enabled(TAS_ID)) return;
  if (centerTarget !== 'original' || !rec || !Number.isSafeInteger(tick) || tick < 1) return;
  const instruction = app.b?.prog?.ins?.[rec.c?.src];
  const input = instruction?.op === 'note' ? instruction : null;
  const keys = input ? [...KEY_ORDER].filter(key => heldIn(input, key)) : [];
  const buttons = input ? BUTTONS.filter(button => heldIn(input, button)) : [];
  const mouse = input?.mouse ? { x:input.mouse[0], y:input.mouse[1] } : null;
  dispatchEvent(new CustomEvent('telos:wasm-input', { detail:{ tick, hz:app.g?.hz || 60, keys, buttons, mouse } }));
}
function releaseWasmInput(reason) {
  dispatchEvent(new CustomEvent('telos:wasm-release', { detail:{ reason } }));
}
function releaseAfterLiveFrame(reason) {
  // A host timeout is not a guest-frame boundary: a paused WASM render can
  // take hundreds of milliseconds. Defer release until this score frame's
  // acknowledgement so a stepped key/button remains held for its full frame.
  if (app.wasmInputPending) app.wasmReleaseAfterAck = reason;
  else {
    releaseWasmInput(reason);
    if (centerTarget === 'original') dispatchEvent(new CustomEvent('telos:wasm-vm-control', { detail:{ action:'pause' } }));
  }
}
function print(text, cls = '') {
  for (const line of String(text).split('\n')) {
    const d = document.createElement('div');
    if (cls) d.className = cls;
    d.textContent = line.length ? line : ' ';
    el.out.append(d);
  }
  while (el.out.childElementCount > 600) el.out.firstElementChild.remove();
  el.out.scrollTop = el.out.scrollHeight;
}
function kvLines(obj) {
  const keys = Object.keys(obj);
  const w = Math.max(...keys.map((k) => k.length));
  return keys.map((k) => {
    let v = obj[k];
    if (Array.isArray(v)) v = v.map((x) => (Array.isArray(x) ? `[${x.join(',')})` : String(x))).join(', ') || '-';
    else if (v === null || v === undefined || v === '') v = '-';
    return `  ${k.padEnd(w)}  ${v}`;
  });
}
function receiptLines(r) {
  const o = {
    kind: r.kind, result: r.result, from_tick: r.from_tick, to_tick: r.to_tick, mode: r.mode, verify_level: r.verify_level,
    write_pages: r.write_pages, micros: r.micros, state_root: r.state_root, journal_sha256: r.journal_sha256,
    pixel_exact: r.pixel_exact, canary: r.canary, fallback: r.fallback, first_divergence: r.first_divergence,
    restore_points: r.restore_points, replayed: r.replayed,
  };
  if (r.checked != null) o.checked = r.checked;
  if (r.fields && r.fields.length) o.fields = r.fields.slice(0, 12).join(', ') + (r.fields.length > 12 ? ` (+${r.fields.length - 12})` : '');
  o.note = r.note;
  return kvLines(o);
}
const toneOf = (r) => (r.result === 'pass' ? 'ok' : r.result === 'best_effort' ? 'warn' : r.first_divergence != null ? 'bad' : 'warn');

// ---------- branches ----------
function newHist(label, text) { return { list: [{ label, before: null, after: text, at: Date.now() }], pos: 0 }; }

function newRun(text, cfg, name = 'main') {
  const p = parseScore(text);
  const b = makeBranch({ name, score: text, prog: p.prog, cfg, tier: app.tier });
  extend(b, Infinity, simCtx, true);
  b.hist = newHist(`Open ${app.name}`, text);
  b.t = 0;
  return b;
}

function uniqueName(base) {
  let n = base, k = 2;
  while (app.branches.some((b) => b.name === n)) n = `${base}-${k++}`;
  return n;
}
const findBranch = (name) => app.branches.find((b) => b.name === name);

function thumbOf(b, t) {
  try { return thumbnail(restore(b.recs[Math.min(t, headOf(b))]), { tier: app.tier, watermark: app.tier !== 'lab' || app.watermark, showPath: app.showPath }, 320); }
  catch { return ''; }
}

function activate(b, t = b.t || 0) {
  if (!prepareScoreNavigation('switch branches')) return false;
  pause();
  if (app.b && app.b !== b) { app.b.t = app.t; app.b.thumb = thumbOf(app.b, app.t); }
  app.b = b;
  t = Math.min(Math.max(0, t), headOf(b));
  app.g = restore(b.recs[t]);
  app.t = t;
  setEditorText(b.score);
  showErrors([]);
  rebuild();
  roll.select(Math.max(1, Math.min(t, roll.maxTick())), undefined, undefined, true);
  dirty('branches', 'history', 'memory', 'view', 'editor');
  return true;
}

function branchAt(T, name) {
  if (!prepareScoreNavigation('create a branch')) return null;
  const b = app.b;
  T = Math.max(0, Math.min(MAX_TICKS, T | 0));
  if (T > headOf(b)) { extend(b, T, simCtx); rebuild(); }
  T = Math.min(T, headOf(b));
  const child = makeBranch({
    name: uniqueName(name || `${b.name.split('@')[0]}@${T}`), score: b.score, prog: b.prog, cfg: b.cfg,
    parent: b, forkTick: T, recs: b.recs.slice(0, T + 1).map((r) => ({ ...r })), tier: b.tier,
  });
  child.hist = newHist(`Branch from ${b.name} at tick ${T}`, b.score);
  child.t = T;
  child.thumb = thumbOf(child, T);
  app.branches.push(child);
  activate(child, T);
  print(`branched ${b.name} at tick ${T} -> ${child.name}`, 'ok');
  say(el.stageStatus, `Branch ${child.name} made at tick ${T} and loaded. Edit it, then compare with bisect ${b.name} ${child.name}.`, 'ok');
  return child;
}

// ---------- history ----------
function histPush(b, label, before, after, coalesce = '') {
  const h = b.hist;
  h.list.length = h.pos + 1;
  const last = h.list[h.pos];
  if (coalesce && last && last.co === coalesce && (coalesce.startsWith('rec') || Date.now() - last.at < 4000)) {
    last.after = after;
    last.at = Date.now();
    last.label = label;
  } else {
    h.list.push({ label, before, after, at: Date.now(), co: coalesce });
    h.pos = h.list.length - 1;
  }
  if (h.list.length > 300) { h.list.splice(1, h.list.length - 300); h.pos = h.list.length - 1; }
  dirty('history');
}
function undo() {
  flushEdit();
  const h = app.b.hist;
  if (h.pos <= 0) { say(el.scoreStatus, 'Nothing to undo.'); return; }
  const e = h.list[h.pos];
  if (applyText(e.before)) { h.pos--; say(el.scoreStatus, `Undid: ${e.label}.`, 'ok'); dirty('history'); }
}
function redo() {
  flushEdit();
  const h = app.b.hist;
  if (h.pos >= h.list.length - 1) { say(el.scoreStatus, 'Nothing to redo.'); return; }
  const e = h.list[h.pos + 1];
  if (applyText(e.after)) { h.pos++; say(el.scoreStatus, `Redid: ${e.label}.`, 'ok'); dirty('history'); }
}
function jumpHistory(i) {
  const h = app.b.hist;
  if (i === h.pos) return;
  if (applyText(h.list[i].after)) { h.pos = i; dirty('history'); say(el.scoreStatus, `Back to: ${h.list[i].label}.`, 'ok'); }
}

// ---------- score text <-> branch ----------
function setEditorText(text) {
  if (el.score.value === text) return;
  const st = el.score.scrollTop, sl = el.score.scrollLeft;
  el.score.value = text;
  el.score.scrollTop = st;
  el.score.scrollLeft = sl;
}

// Apply text to the active branch. Returns false on parse errors.
function applyText(text, { fromEditor = false, quiet = false } = {}) {
  const b = app.b;
  const oldHead = headOf(b);
  const res = applyScore(b, text);
  if (res.errors.length) {
    showErrors(res.errors);
    if (!fromEditor) setEditorText(b.score);
    dirty('editor');
    say(el.scoreStatus, `${res.errors.length === 1 ? '1 error' : res.errors.length + ' errors'}. Playing the last good version.`, 'warn');
    return false;
  }
  showErrors([]);
  if (app.scoreNavigationRefused) {
    app.scoreNavigationRefused = false;
    say(el.stageStatus, '');
  }
  if (!fromEditor) setEditorText(text);
  if (res.fork === Infinity || res.fork > oldHead) {
    if (!quiet) say(el.scoreStatus, res.fork === Infinity ? 'Parsed. No input changed.' : 'Parsed. The change is past the recorded ticks.', 'ok');
  } else if (res.fork <= app.t) {
    app.g = extend(b, app.t, simCtx);
    app.t = headOf(b);
    if (!quiet) say(el.scoreStatus, `Changed from tick ${res.fork}. Recomputed ${app.t - res.fork + 1} sample authoring records to the playhead.`, 'ok');
  } else if (!quiet) say(el.scoreStatus, `Changed from tick ${res.fork}. Later ticks were dropped. Play on to record them.`, 'ok');
  rebuild();
  dirty('editor', 'view', 'memory', 'branches');
  return true;
}

function setScore(text, label, opts = {}) {
  const b = app.b, before = b.score;
  if (text === before) return true;
  if (!applyText(text, opts)) return false;
  histPush(b, label, before, text, opts.coalesce);
  return true;
}

function flushEdit() {
  if (app.editTimer) { clearTimeout(app.editTimer); app.editTimer = 0; editorCommit(); }
}
function prepareScoreNavigation(action) {
  if (!events.active) return false;
  if (!app.b) return true;
  flushEdit();
  if (el.score.value === app.b.score) return true;
  app.scoreNavigationRefused = true;
  say(el.stageStatus, `Cannot ${action}: fix the score errors first. Your draft remains in the editor.`, 'warn');
  return false;
}
function editorCommit() {
  app.editTimer = 0;
  const text = el.score.value;
  if (text === app.b.score) return;
  const before = app.b.score;
  if (applyText(text, { fromEditor: true })) histPush(app.b, 'Edit score text', before, text, 'type');
}

// ---------- roll model ----------
function rebuild() {
  const b = app.b;
  app.model = buildRows(b);
  app.marks = rowMarks(b.prog, app.model);
  app.markMap = new Map();
  for (const m of app.marks) {
    if (m.tick < 0) continue;
    if (!app.markMap.has(m.tick)) app.markMap.set(m.tick, []);
    app.markMap.get(m.tick).push(m.name);
  }
  app.segs = new Map(rowSegments(b.prog, app.model).map((s) => [s.tick, s]));
  app.lineTicks = new Map();
  for (const r of app.model.rows) {
    if (r.t === 0 || r.src < 0) continue;
    const it = b.prog.ins[r.src];
    if (it && !app.lineTicks.has(it.line)) app.lineTicks.set(it.line, r.t);
  }
  roll.set({ b, model: app.model, prog: b.prog, marks: app.markMap, segs: app.segs, playhead: app.t, head: headOf(b), end: app.model.end });
  // History/score changes can alter the selected cell without moving it.
  // Refresh its accessible description along with the rebuilt input model.
  announce();
  app.dirty.rows = false;
  dirty('roll', 'editor');
}

// Run a roll-to-text rewrite. fn(text, prog, model) returns a result from edit.js.
async function rollEdit(label, fn, opts = {}) {
  if (!prepareScoreNavigation('edit the piano roll')) return false;
  const b = app.b;
  let res = fn(b.score, b.prog, app.model);
  if (res.needUnroll) {
    const r = res.needUnroll[0];
    const ok = await confirmBox(`This tick is inside @repeat ${r.n} (line ${r.line}). The score has no per-iteration edit, so the block must be unrolled into plain notes first. Unroll it and apply the edit?`, 'Unroll and edit');
    if (!ok) { say(el.scoreStatus, 'Edit cancelled. The @repeat block is unchanged.'); return false; }
    const text = unrollRepeats(b.score, res.needUnroll);
    const p = parseScore(text);
    if (p.errors.length) { say(el.scoreStatus, 'Could not unroll the block.', 'warn'); return false; }
    res = fn(text, p.prog, app.model);
    if (res.needUnroll) return false;
    if (res.same) res = { text };
  }
  if (res.same || !res.text) {
    if (res.skipped) say(el.scoreStatus, '@sync rows are not editable. Change the event or timeout in the score text.', 'warn');
    else if (res.note) say(el.scoreStatus, res.note);
    return false;
  }
  const done = setScore(res.text, label, opts);
  if (done && res.skipped && !opts.quiet) say(el.scoreStatus, `${label}. ${res.skipped} @sync rows were left as they are.`, 'ok');
  else if (done && !opts.quiet) say(el.scoreStatus, `${label}.`, 'ok');
  return done;
}

const range = (a, b) => (a === b ? `tick ${a}` : `ticks ${a}-${b}`);

function paint(t0, t1, cols, value) {
  const changes = new Map();
  for (let t = t0; t <= t1; t++) changes.set(t, (inp) => cols.reduce((v, k) => setKey(v, k, value), inp));
  const label = `${value ? 'Hold' : 'Clear'} ${cols.join(' ')} on ${range(t0, t1)}`;
  return rollEdit(label, (text, prog, model) => editTicks(text, prog, model, changes));
}

function clearSelection(all = false) {
  const [a, b] = roll.selRange();
  const col = COLS[roll.sel.c];
  const changes = new Map();
  for (let t = a; t <= b; t++) {
    changes.set(t, (inp) => {
      if (all) return { bits: 0, xbits: 0, mouse: null, al: null };
      if (col.kind === 'mouse') return { ...inp, mouse: null };
      return setKey(inp, col.id, false);
    });
  }
  return rollEdit(`Clear ${all ? 'all inputs' : col.label} on ${range(a, b)}`, (text, prog, model) => editTicks(text, prog, model, changes));
}

async function patternSelection(low, high, lane, mode) {
  if (!events.active || !extensions.enabled(TAS_ID) || !app.b
    || !prepareScoreNavigation('edit input lanes')) return;
  if (app.dirty.rows) rebuild();
  const branch = app.b, before = branch.score, model = app.model;
  const generation = tasLifecycleGeneration;
  let period = 2, phase = 0;
  if (mode === 'autofire') {
    const answer = await ask(
      `Set autofire for ${lane} on ${range(low, high)}. Enter period and phase in score ticks. The lane is held for floor(period/2) ticks, then released. Phase starts at the first included score tick. This edits the score; it does not run or rewind the game.`,
      '2 0', [], 'Set autofire');
    if (answer === null) return;
    const match = /^([0-9]{1,5})\s+([0-9]{1,5})$/.exec(answer);
    if (!match) {
      say(el.scoreStatus, 'Enter two whole numbers: period and phase, such as 2 0.', 'warn');
      return;
    }
    period = Number(match[1]); phase = Number(match[2]);
  }
  if (!events.active || !extensions.enabled(TAS_ID) || generation !== tasLifecycleGeneration) return;
  if (app.b !== branch || branch.score !== before || app.model !== model) {
    say(el.scoreStatus, 'The score changed while configuring the lane edit. Choose the range again.', 'warn');
    return;
  }
  const label = mode === 'hold'
    ? `Hold ${lane} on ${range(low, high)}`
    : `Autofire ${lane} on ${range(low, high)} · period ${period}, phase ${phase} score ticks`;
  try {
    await rollEdit(label, (text, prog, rows) => {
      if (!events.active || !extensions.enabled(TAS_ID) || generation !== tasLifecycleGeneration
        || app.b !== branch || branch.score !== before) {
        // Throw before the authoring owner can interpret `same` as approval
        // to commit a repeat block that was just unrolled in its dialog.
        throw new Error('The input lane edit was cancelled because its owner changed.');
      }
      return applyLanePattern(text, prog, rows, { low, high, lanes:[lane], mode, period, phase });
    });
  } catch (error) {
    say(el.scoreStatus, error.message || 'The lane pattern could not be applied.', 'warn');
  }
}

async function editMouse(t) {
  const row = app.model.rows[t];
  if (!row || row.kind === 'wait') { say(el.scoreStatus, '@sync rows are not editable. Change the event or timeout in the score text.', 'warn'); return; }
  const inp = rowInput(app.b.prog, row);
  const [a, b] = roll.selRange();
  const t0 = t >= a && t <= b ? a : t, t1 = t >= a && t <= b ? b : t;
  const v = await ask(`Set the mouse position for ${range(t0, t1)} as x,y. Leave it empty to clear it. The sample scene records the edit in the score.`, inp.mouse ? inp.mouse.join(',') : '', [], 'Set position');
  if (v === null) return;
  let mouse = null;
  if (v.trim()) {
    const m = /^\s*(\d{1,4})\s*,\s*(\d{1,4})\s*$/.exec(v);
    if (!m) { say(el.scoreStatus, 'Type the position as two whole numbers, for example 412,188.', 'warn'); return; }
    mouse = [+m[1], +m[2]];
  }
  const changes = new Map();
  for (let k = t0; k <= t1; k++) changes.set(k, (inp2) => ({ ...inp2, mouse }));
  rollEdit(`${mouse ? 'Set' : 'Clear'} mouse on ${range(t0, t1)}`, (text, prog, model) => editTicks(text, prog, model, changes));
}

async function markAt(t) {
  const name = await ask(`Name the mark at tick ${t}. Letters, digits, _ . and -.`, `mark_${t}`, [], 'Add mark');
  if (!name) return;
  if (!/^[A-Za-z0-9_][A-Za-z0-9_.-]*$/.test(name)) { say(el.scoreStatus, 'A mark name uses letters, digits, _ . and -.', 'warn'); return; }
  rollEdit(`Add mark ${name} at tick ${t}`, (text, prog, model) => addMark(text, prog, model, t, name));
}

async function findMark() {
  if (!app.marks.length) { say(el.scoreStatus, 'The score has no marks yet. Press N in the roll to add one.'); return; }
  const name = await ask('Find a mark by name.', '', app.marks.map((m) => m.name), 'Find');
  if (name === null) return;
  const m = app.marks.find((x) => x.name === name) || app.marks.find((x) => x.name.includes(name));
  if (!m || m.tick < 0) { say(el.scoreStatus, `No mark named ${name}.`, 'warn'); return; }
  roll.select(m.tick, m.tick);
  $('roll').focus();
  say(el.scoreStatus, `Mark ${m.name} is at tick ${m.tick}.`, 'ok');
}

function navigateMark(direction) {
  const candidates = app.marks.filter((mark) => mark.tick >= 0);
  const mark = direction < 0
    ? [...candidates].reverse().find((item) => item.tick < app.t)
    : candidates.find((item) => item.tick > app.t);
  if (!mark) {
    say(el.stageStatus, direction < 0 ? 'No earlier score mark.' : 'No later score mark.');
    return;
  }
  seekTo(mark.tick, app.mode, { log: false });
  roll.select(mark.tick, mark.tick);
  say(el.stageStatus, `Mark ${mark.name}, tick ${mark.tick}.`, 'ok');
}

function seekEnteredTick() {
  const input = $('tick-jump');
  const tick = intArg(input.value);
  const head = headOf(app.b);
  if (tick === null || tick > head) {
    say(el.stageStatus, `Enter a recorded tick from 0 to ${head}.`, 'warn');
    input.focus();
    return;
  }
  const receipt = seekTo(tick, app.mode, { log: false });
  if (receipt) roll.select(tick, tick);
}

// ---------- seeking ----------
function seekTo(T, mode = app.mode, { log = true, quiet = false } = {}) {
  if (!extensions.enabled(TAS_ID)) return;
  const b = app.b;
  pause();
  releaseWasmInput('timeline-seek');
  T = Math.max(0, Math.min(MAX_TICKS, T | 0));
  if (!modeAllowed(mode, app.tier)) {
    const why = modeReason(mode, app.tier);
    say(el.seekReport, `seek ${mode} refused: ${why}`, 'warn');
    if (log) print(`seek mode ${mode} is not allowed in tier ${app.tier}. ${why}`, 'warn');
    return null;
  }
  if (T > headOf(b)) {
    extend(b, T, simCtx);
    T = Math.min(T, headOf(b));
    rebuild();
  }
  const { g, receipt } = seek(b, app.g, T, mode, { tier: app.tier, ctx: simCtx });
  app.g = g;
  app.t = T;
  b.receipts.push(receipt);
  if (b.receipts.length > 200) b.receipts.shift();
  if (receipt.first_divergence != null) b.div = receipt.first_divergence;
  const ms = (receipt.micros / 1000).toFixed(receipt.micros < 10000 ? 1 : 0);
  const summary = `seek ${mode} ${receipt.from_tick} -> ${T}: ${receipt.result}. ${receipt.write_pages} pages written, ${receipt.replayed} ticks replayed, ${ms} ms` +
    (receipt.fallback ? `, fallback ${receipt.fallback}` : '') + (receipt.canary !== 'none' ? `, canary ${receipt.canary}` : '') + '.';
  if (!quiet) say(el.seekReport, summary, receipt.result === 'pass' ? 'ok' : receipt.first_divergence != null ? 'bad' : 'warn');
  if (log) { print(`seek ${T} --mode ${mode}`, 'in'); print(receiptLines(receipt).join('\n'), toneOf(receipt)); }
  roll.data.playhead = app.t;
  dirty('view', 'roll', 'memory', 'editor', 'branches');
  return receipt;
}

// One tick forward from the live state. Inside the stored range the new
// state is checked against the stored hash.
function stepForward() {
  if (!extensions.enabled(TAS_ID)) return;
  if (centerTarget === 'original' && app.wasmInputPending) {
    say(el.stageStatus, `Waiting for the runtime to acknowledge score tick ${app.wasmPendingTick}.`, 'warn');
    return;
  }
  if (!prepareScoreNavigation('step forward')) return;
  const b = app.b;
  if (app.t >= MAX_TICKS) { pause(); return; }
  const watchedFrom = app.playing && app.pauseWatchChange && app.watchBreak && !app.watchBreak.w.error && app.watchBreak.w.steps[0] !== 'input' ? b.recs[app.t] : null;
  if (app.rec.on) recordTick(app.t + 1);
  const wasEnd = app.g.cur.end;
  const next = app.t + 1;
  const rec = advance(b, app.g, simCtx);
  if (next <= headOf(b)) {
    if (b.recs[next].h !== rec.h) {
      b.recs.length = next;
      b.recs.push(rec);
      b.div = next;
      dirty('rows', 'branches');
      say(el.stageStatus, `Tick ${next} did not match the stored state. It is marked as the first divergence. The stored ticks after it were dropped.`, 'bad');
    }
  } else {
    b.recs.push(rec);
    dirty('rows', 'branches');
  }
  app.t = next;
  emitWasmInput(rec, next);
  dirty('view', 'roll', 'memory');
  if (app.playing && rec.c.end && !wasEnd) {
    pause({ afterLiveFrame:true });
    say(el.stageStatus, `End of score at tick ${next}. Play again to run on with no input.`);
  }
  if (app.playing && rec.c.halt && !b.recs[next - 1].c.halt) {
    pause({ afterLiveFrame:true });
    say(el.stageStatus, `Run halted at tick ${next}: ${rec.c.halt}.`, 'warn');
  }
  if (app.playing && watchedFrom) {
    const changed = changedStateWatch(watchedFrom, b.recs[next], app.watchBreak);
    if (changed) {
      pause({ afterLiveFrame:true });
      say(el.stageStatus, `Paused at tick ${next}: ${changed} changed.`, 'warn');
    }
  }
}

function changedStateWatch(from, to, watch) {
  if (!from || !to || !watch) return '';
  const a = readWatch(watch.w, stateOf(from), {}), b = readWatch(watch.w, stateOf(to), {});
  const same = Object.is(a, b) || (a && b && typeof a === 'object' && typeof b === 'object' && JSON.stringify(a) === JSON.stringify(b));
  return same ? '' : watch.expr;
}

// Record mode: write the pad keys into the score at tick t before it runs.
function recordTick(t) {
  // A pending @sync decides when the next note starts, so nothing is written
  // while the run waits.
  if (app.g.cur.wait) return;
  if (app.dirty.rows) rebuild();
  const row = app.model.rows[t];
  if (!row || row.kind === 'wait' || row.kind === 'boot') return;
  const inp = rowInput(app.b.prog, row);
  if (inp.bits === app.rec.bits) return;
  const bits = app.rec.bits;
  const changes = new Map([[t, (v) => {
    let out = { ...v, bits };
    if (out.al) { out.al = { ...out.al }; for (const k of Object.keys(out.al)) if (k in BIT) delete out.al[k]; }
    return out;
  }]]);
  const res = editTicks(app.b.score, app.b.prog, app.model, changes);
  if (res.needUnroll) { toggleRecord(false); say(el.stageStatus, 'Recording stopped: this tick is inside @repeat. Unroll the block in the roll first.', 'warn'); return; }
  if (res.same || !res.text) return;
  const start = app.rec.from;
  setScore(res.text, `Record ${range(start, t)}`, { quiet: true, coalesce: 'rec' + start });
  app.rec.to = t;
}

function toggleRecord(on = !app.rec.on) {
  if (on && !extensions.enabled(TAS_ID)) return;
  app.rec.on = on;
  app.rec.from = app.t + 1;
  el.rec.setAttribute('aria-pressed', String(on));
  el.rec.textContent = on ? 'Stop recording' : 'Record';
  for (const k of el.pad.querySelectorAll('.padk')) k.disabled = !on;
  say(el.stageStatus, on ? 'Recording. Each new tick takes the pad keys. Play or step to write them.' : 'Recording stopped.', on ? 'warn' : '');
  dirty('view');
}

function play() {
  if (!extensions.enabled(TAS_ID)) return;
  if (app.playing) return;
  if (!prepareScoreNavigation('play')) return;
  if (centerTarget === 'original') dispatchEvent(new CustomEvent('telos:wasm-vm-control', { detail:{ action:'resume' } }));
  app.playing = true;
  app.last = performance.now();
  app.acc = 0;
  el.play.setAttribute('aria-pressed', 'true');
  el.playLabel.textContent = 'Pause';
  el.iPlay.hidden = true;
  el.iPause.hidden = false;
  dirty('view');
}
function pause({ afterLiveFrame = false } = {}) {
  if (!app.playing) return;
  if (centerTarget === 'original' && !afterLiveFrame) dispatchEvent(new CustomEvent('telos:wasm-vm-control', { detail:{ action:'pause' } }));
  app.playing = false;
  if (afterLiveFrame && centerTarget === 'original') releaseAfterLiveFrame('playback-ended');
  else releaseWasmInput('timeline-pause');
  el.play.setAttribute('aria-pressed', 'false');
  el.playLabel.textContent = 'Play';
  el.iPlay.hidden = false;
  el.iPause.hidden = true;
  dirty('view', 'memory', 'roll');
}
const togglePlay = () => (app.playing ? pause() : play());

function goEnd() {
  const b = app.b;
  let end = b.recs.findIndex((r, t) => t > 0 && r.c.end);
  if (end < 0) {
    extend(b, Infinity, simCtx, true);
    rebuild();
    end = b.recs.findIndex((r, t) => t > 0 && r.c.end);
    if (end < 0) end = headOf(b);
  }
  return seekTo(end, app.mode, { log: false });
}

// ---------- tiers ----------
function setTier(tier) {
  if (!TIERS.includes(tier)) return;
  pause();
  app.tier = tier;
  el.tier.value = tier;
  const t = app.t;
  for (const b of app.branches) {
    const head = headOf(b);
    b.tier = tier;
    b.recs = [snapshot(initState(b.cfg), '')];
    extend(b, head, simCtx);
    b.receipts = [];
    b.judge = null;
    b.tainted = false;
    b.div = -1;
  }
  app.g = restore(app.b.recs[Math.min(t, headOf(app.b))]);
  app.t = Math.min(t, headOf(app.b));
  app.mode = DEFAULT_MODE[tier];
  if (tier !== 'lab') app.watermark = true;
  syncTierUi();
  rebuild();
  for (const b of app.branches) b.thumb = thumbOf(b, b === app.b ? app.t : b.t || b.forkTick);
  dirty('view', 'branches', 'memory', 'roll');
  say(el.stageStatus, `Tier set to ${tier}. Every branch was re-recorded from tick 0 under it. Seek mode is now ${app.mode}.`, 'ok');
  print(`tier ${tier}: seek modes ${MODES.filter((m) => modeAllowed(m, tier)).join(', ')}; watermark ${tier === 'lab' ? 'optional' : 'forced on'}`, 'ok');
}

function syncTierUi() {
  const tier = app.tier;
  el.tier.value = tier;
  if (tier !== 'lab') app.watermark = true;
  el.hTier.className = 'chip tier-' + tier;
  for (const o of el.mode.options) {
    const ok = modeAllowed(o.value, tier);
    o.disabled = !ok;
    o.textContent = ok ? o.value : `${o.value} (not in ${tier})`;
    o.title = ok ? '' : modeReason(o.value, tier);
  }
  if (!modeAllowed(app.mode, tier)) app.mode = DEFAULT_MODE[tier];
  el.mode.value = app.mode;
  const locked = tier !== 'lab';
  el.wm.disabled = locked;
  el.wm.checked = app.watermark;
  el.wmWrap.classList.toggle('locked', locked);
  el.wmWrap.title = locked ? `The watermark stays on in ${tier} tier. Only lab can hide it.` : 'Show or hide the TAS watermark. Only lab tier can hide it.';
  el.memNote.hidden = tier !== 'hands-off';
  el.memNote.textContent = tier === 'hands-off' ? 'Hands-off runs read no game memory, so watch and hex show nothing. Switch to lab or tas to read values.' : '';
  const watchBreak = $('pause-watch-change');
  if (watchBreak) {
    const expression = $('watch-break-in');
    if (expression && document.activeElement !== expression) expression.value = app.watchBreakExpr;
    const invalid = app.watchBreak && app.watchBreak.w.error;
    if (expression) expression.setAttribute('aria-invalid', String(!!invalid));
    if (invalid) app.pauseWatchChange = false;
    watchBreak.checked = app.pauseWatchChange;
    watchBreak.disabled = tier === 'hands-off' || !app.watchBreakExpr || invalid;
    watchBreak.title = tier === 'hands-off' ? 'Watch values are unavailable in hands-off tier.' : invalid ? app.watchBreak.w.error : !app.watchBreakExpr ? 'Enter a state expression first.' : 'Pause playback on the first tick where this state value changes.';
  }
}

// ---------- console ----------
const HELP = `score lines   4 w lmb | . | @sync area.ready timeout=1800 | @mark name | @segment s hz=30 | @repeat 3 ... @end
seek N [--mode replay|restore|hybrid|semantic]
branch N [--out NAME]
diff A B [--first]          bisect A B        (A and B are branch names)
verify [--fresh] [--compare every_tick|syncs|end]
show [--from N] [--to N] [--legend] [--limit N]
tier show | tier explain
status | score | save PATH | undo | help
cockpit only: step [n] (same as 'n .'), tier set lab|tas|hands-off, record on|off,
              checkout NAME, export score|holo, import`;

function flag(args, name) { const i = args.indexOf(name); if (i < 0) return false; args.splice(i, 1); return true; }
function opt(args, name) {
  const i = args.findIndex((a) => a === name || a.startsWith(name + '='));
  if (i < 0) return undefined;
  if (args[i].includes('=')) { const v = args[i].split('=')[1]; args.splice(i, 1); return v; }
  const v = args[i + 1];
  args.splice(i, 2);
  return v;
}
const intArg = (v) => (v !== undefined && /^\d+$/.test(v) ? +v : null);

const REPORT_W = [8, 3, 6, 2, 6, 3, 4, 8, 8];
const reportLine = (cells) => cells.slice(0, -1).map((c, i) => String(c).padEnd(REPORT_W[i])).join('  ') + '  ' + cells[cells.length - 1];
const REPORT_HEAD = ['tick', 'ep', 'verbs', 'rp', 'proc', 'sp', 'sub', 'journal', 'root', 'note'];
function reportRow(b, t) {
  const r = b.recs[t];
  const f = flagsOf(b, t);
  const proc = r.c.halt ? 'halt' : r.c.wait ? 'wait' : r.c.end ? 'end' : 'ok';
  return reportLine([t, r.ep, verbsText(verbsOf(b, t)), f & F.RESTORE_POINT ? 'r' : '-', proc, 1, 1, r.j.slice(0, 8), r.h.slice(0, 8), r.ev.join(',') || '.']);
}

function printRun(b, a, z) {
  if (z < a) return;
  print(reportLine(REPORT_HEAD), 'in');
  const rows = [];
  for (let t = a; t <= z; t++) rows.push(t);
  const show = rows.length > 8 ? [...rows.slice(0, 3), -1, ...rows.slice(-3)] : rows;
  for (const t of show) print(t < 0 ? `  ... ${rows.length - 6} more rows` : reportRow(b, t), t >= 0 && b.recs[t] && b.recs[t].ev.length ? 'rp' : '');
}

function scoreEnd(b) {
  const e = b.recs.findIndex((r, t) => t > 0 && r.c.end);
  return e;
}

function runScoreLine(line) {
  const b = app.b;
  const head = line.split(/\s+/)[0];
  if (app.held.length || head === '@repeat') {
    app.held.push(line);
    const depth = app.held.reduce((d, l) => d + (l.startsWith('@repeat') ? 1 : l.trim() === '@end' ? -1 : 0), 0);
    if (depth > 0) { print('held until @end', 'in'); return; }
    line = app.held.join('\n');
    app.held = [];
  }
  const base = b.score.endsWith('\n') ? b.score : b.score + '\n';
  const text = base + line + '\n';
  const p = parseScore(text);
  if (p.errors.length) { for (const e of p.errors.slice(0, 3)) print(`line ${e.line}: ${e.msg}`, 'warn'); return; }
  extend(b, Infinity, simCtx, true);
  const e0 = scoreEnd(b);
  const from = e0 > 0 ? e0 : headOf(b);
  if (!setScore(text, `Console: ${line.split('\n')[0]}${line.includes('\n') ? ' ...' : ''}`, { quiet: true })) return;
  app.consoleLines.push(line);
  extend(b, Infinity, simCtx, true);
  const e1 = scoreEnd(b);
  const to = e1 > 0 ? e1 : headOf(b);
  const firstNew = from + 1;
  if (to >= firstNew) {
    if (centerTarget === 'original') {
      print('Appended to the score. The rows below describe the sample authoring model; live KOTOR input was not sent. Use Transport to deliver the authored inputs and inspect runtime acknowledgements.', 'warn');
    }
    printRun(b, firstNew, to);
    app.g = restore(b.recs[to]);
    app.t = to;
    rebuild();
    dirty('view', 'memory');
  } else if (head === '@mark') {
    const m = app.marks.find((x) => x.name === line.split(/\s+/)[1]);
    print(`mark ${m ? m.name : ''} at tick ${m ? m.tick : '?'}`, 'ok');
  } else print('no ticks to run', 'in');
}

async function runCommand(line) {
  const args = line.split(/\s+/).filter(Boolean);
  const cmd = args.shift();
  const b = app.b;
  switch (cmd) {
    case 'help': print(HELP); return;
    case 'status': print(`tick ${app.t} epoch ${app.g.epoch} backend standin tier ${app.tier} branch ${b.name} head ${headOf(b)} alive`); return;
    case 'score': print(b.score.replace(/\n$/, '')); return;
    case 'save': {
      const path = args[0];
      if (!path) { print('save needs a path', 'warn'); return; }
      const name = path.split(/[\\/]/).pop() || 'score.score';
      download(name.endsWith('.score') ? name : name + '.score', b.score, 'text/plain');
      print(`wrote ${name} (your browser saves it as a download)`, 'ok');
      return;
    }
    case 'undo': {
      const last = app.consoleLines[app.consoleLines.length - 1];
      if (!last) { print('nothing to drop'); return; }
      const tail = last + '\n';
      if (!b.score.endsWith(tail)) { print('the last console line is no longer at the end of the score; nothing dropped', 'warn'); app.consoleLines.pop(); return; }
      if (setScore(b.score.slice(0, -tail.length), `Console undo: ${last.split('\n')[0]}`, { quiet: true })) { app.consoleLines.pop(); print(`dropped: ${last.split('\n')[0]}${last.includes('\n') ? ' ...' : ''}`, 'ok'); }
      return;
    }
    case 'step': {
      const n = args.length ? intArg(args[0]) : 1;
      if (!n || n > 100000) { print('step takes a tick count from 1 to 100000', 'warn'); return; }
      runScoreLine(`${n} .`);
      return;
    }
    case 'seek': {
      const mode = opt(args, '--mode') || app.mode;
      const T = intArg(args[0]);
      if (T === null) { print('seek needs a tick number', 'warn'); return; }
      if (!MODES.includes(mode)) { print(`unknown seek mode '${mode}'`, 'warn'); return; }
      seekTo(T, mode);
      return;
    }
    case 'branch': {
      const out = opt(args, '--out');
      const T = intArg(args[0]);
      if (T === null) { print('branch needs a tick number', 'warn'); return; }
      if (out !== undefined && !/^[A-Za-z0-9_.@-]{1,40}$/.test(out)) { print('--out takes a name of letters, digits, _ . @ and -', 'warn'); return; }
      branchAt(T, out);
      return;
    }
    case 'checkout': {
      const t = findBranch(args[0] || '');
      if (!t) { print(`no branch named ${args[0] || ''}. Branches: ${app.branches.map((x) => x.name).join(', ')}`, 'warn'); return; }
      if (activate(t)) print(`loaded ${t.name} at tick ${app.t}`, 'ok');
      return;
    }
    case 'diff': case 'bisect': {
      const first = flag(args, '--first');
      const limit = intArg(opt(args, '--limit')) || 50;
      flag(args, '--no-roots');
      const A = findBranch(args[0] || ''), B = findBranch(args[1] || '');
      if (!A || !B) { print(`${cmd} needs two branch names. Branches: ${app.branches.map((x) => x.name).join(', ')}`, 'warn'); return; }
      for (const x of [A, B]) extend(x, Infinity, simCtx, true);
      rebuild();
      if (cmd === 'diff') {
        const d = diffBranches(A, B, { first, limit });
        if (!d.count) { print(`no divergence${d.roots ? '' : ' (journal only; roots not compared)'}`, 'ok'); return; }
        const f0 = d.list[0];
        divLines(A, B, f0.tick, f0.field);
        if (!first) {
          print('\ntick      field');
          for (const x of d.list) print(`${String(x.tick).padEnd(9)} ${x.field}`);
          if (d.count > d.list.length) print(`... ${d.count - d.list.length} more`);
        }
        markDiv(A, B, f0.tick);
      } else {
        const r = bisectBranches(A, B);
        if (r.tick < 0) { print(r.linear ? `runs agree at every tick ${r.lo}..${r.hi} (journal only; roots not compared)` : `runs agree at tick ${r.hi} (${r.probes} probe(s))`, 'ok'); return; }
        divLines(A, B, r.tick, r.field);
        print(r.linear ? `linear scan over ticks ${r.lo}..${r.hi} (journal only; roots not compared)` : `${r.probes} probe(s) over ticks ${r.lo}..${r.hi}`);
        markDiv(A, B, r.tick);
      }
      return;
    }
    case 'verify': {
      const fresh = flag(args, '--fresh');
      const compare = opt(args, '--compare') || 'every_tick';
      if (!['every_tick', 'syncs', 'end'].includes(compare)) { print('--compare takes every_tick, syncs or end', 'warn'); return; }
      doVerify({ fresh, compare });
      return;
    }
    case 'show': {
      const legend = flag(args, '--legend');
      const from = intArg(opt(args, '--from')), to = intArg(opt(args, '--to'));
      const limit = intArg(opt(args, '--limit')) || 200;
      showTable(from, to, limit, legend);
      return;
    }
    case 'tier': {
      const sub = args[0];
      const r = tierReport(b);
      if (sub === 'show') print(`declared ${r.declared}  effective ${r.effective}`);
      else if (sub === 'explain') {
        print(kvLines({
          declared: `${r.declared} (set in the header)`,
          ceiling: `${r.ceiling}: ${r.why}`,
          restores: r.restoreTicks.length ? `${r.restoreTicks.length} ticks carry RESTORE (first ${r.restoreTicks[0]})` : 'none',
          judge: r.judged ? `${b.judge.result}: ${b.judge.note}` : 'none yet; run verify --fresh',
          effective: r.effective,
          publishable: r.publishable ? 'yes: publish the score, which passed a fresh replay' : `no: ${r.reasons.join('; ')}`,
        }).join('\n'));
      } else if (sub === 'set') {
        if (!TIERS.includes(args[1])) { print('tier set takes lab, tas or hands-off', 'warn'); return; }
        setTier(args[1]);
      } else print('tier takes show, explain or set', 'warn');
      return;
    }
    case 'record': {
      if (args[0] !== 'on' && args[0] !== 'off') { print('record takes on or off', 'warn'); return; }
      toggleRecord(args[0] === 'on');
      print(`record ${args[0]}`, 'ok');
      return;
    }
    case 'export': {
      if (args[0] === 'score') exportScore();
      else if (args[0] === 'holo') exportHoloFile();
      else print('export takes score or holo', 'warn');
      return;
    }
    case 'import': $('f-import').click(); return;
    default:
      print(`unknown command '${cmd}'. Type help for the list.`, 'warn');
  }
}

function divLines(A, B, t, field) {
  print(`first divergence at tick ${t}: ${field}`, 'bad');
  for (const [side, x] of [['a', A], ['b', B]]) {
    const r = x.recs[t];
    if (!r) { print(`  ${side}: (no tick)`); continue; }
    const v = field === 'journal_sha256' ? r.j : field === 'state_root' ? r.h : field === 'epoch' ? r.ep : '';
    print(`  ${side}: ${x.name} epoch ${r.ep} note ${r.ev.join(',') || '.'} ${field}=${v}`);
  }
}
function markDiv(A, B, t) {
  A.div = t;
  B.div = t;
  if (app.b === A || app.b === B) { roll.select(t, t); dirty('roll'); }
  say(el.stageStatus, `First divergence between ${A.name} and ${B.name} at tick ${t}. It is lit in the roll.`, 'bad');
  dirty('view');
}

function doVerify({ fresh = false, compare = 'every_tick', from = 0 } = {}) {
  const b = app.b;
  const r = verifyRun(b, { fresh, compare, from, tier: app.tier, ctx: simCtx });
  b.receipts.push(r);
  print(`verify${fresh ? ' --fresh' : ''}${compare !== 'every_tick' ? ' --compare ' + compare : ''}${from ? ' from ' + from : ''}`, 'in');
  print(receiptLines(r).join('\n'), toneOf(r));
  if (r.first_divergence != null) { b.div = r.first_divergence; say(el.stageStatus, `Verify found a divergence at tick ${r.first_divergence}.`, 'bad'); }
  else say(el.stageStatus, `Verify: ${r.note}.`, 'ok');
  dirty('roll', 'view', 'branches');
  return r;
}

function showTable(from, to, limit, legend) {
  const b = app.b;
  print(`${b.name} (${app.name})`);
  print(kvLines({ game: 'standin', tier: b.tier, hz: b.prog.hz0, head: headOf(b), fork_tick: b.forkTick, budget: b.cfg.budget }).join('\n'));
  if (legend) print(LEGEND);
  const cols = ['tick', 'ep', 'verbs', 'flags', 'reached', 'sp', 'sub', 'journal', 'root', 'note'];
  const rows = [];
  for (let t = from || 0; t <= Math.min(headOf(b), to == null ? Infinity : to) && rows.length < limit; t++) {
    const r = b.recs[t];
    rows.push([String(t), String(r.ep), verbsText(verbsOf(b, t)), flagsText(flagsOf(b, t)), REACHED[r.r || 0], '1', '1', r.j.slice(0, 8), r.h.slice(0, 8), r.ev.join(',') || '.']);
  }
  const w = cols.map((c, i) => Math.max(c.length, ...rows.map((r) => r[i].length)));
  const fmt = (cells) => cells.map((c, i) => (i === cells.length - 1 ? c : c.padEnd(w[i]))).join('  ');
  print('');
  print(fmt(cols), 'in');
  print(rows.map(fmt).join('\n'));
}

async function runLine(raw) {
  if (!extensions.enabled(TAS_ID)) { print('Enable TAS in Extensions to use the score console.', 'warn'); return; }
  const line = raw.trim();
  if (!line || line.startsWith('#')) return;
  print('telos> ' + line, 'in');
  app.cmdHist.push(line);
  if (app.cmdHist.length > 100) app.cmdHist.shift();
  app.cmdPos = app.cmdHist.length;
  const word = line.split(/\s+/)[0];
  try {
    if (/^\d/.test(word) || word === '.' || word.startsWith('@') || app.held.length) runScoreLine(line);
    else await runCommand(line);
  } catch (e) {
    print(`error: ${e && e.message ? e.message : e}`, 'warn');
  }
}

function updateSuggestions() {
  const v = el.cli.value;
  const m = /(^|\s)--mode(?:\s+|=)?(\S*)$/.exec(v);
  if (!m || !/^seek\b/.test(v.trim())) { el.sugg.hidden = true; return; }
  const frag = document.createDocumentFragment();
  for (const mode of MODES) {
    if (m[2] && !mode.startsWith(m[2])) continue;
    const ok = modeAllowed(mode, app.tier);
    const bt = document.createElement('button');
    bt.type = 'button';
    bt.textContent = mode;
    if (!ok) {
      bt.setAttribute('aria-disabled', 'true');
      bt.title = modeReason(mode, app.tier);
      bt.setAttribute('aria-label', `${mode}, not allowed in ${app.tier}: ${modeReason(mode, app.tier)}`);
    }
    events.listen(bt, 'click', () => {
      if (!ok) return;
      el.cli.value = v.replace(/(--mode)(?:\s+|=)?\S*$/, `$1 ${mode}`);
      el.sugg.hidden = true;
      el.cli.focus();
    });
    frag.append(bt);
    if (!ok) { const why = document.createElement('div'); why.className = 'why'; why.textContent = modeReason(mode, app.tier); frag.append(why); }
  }
  el.sugg.replaceChildren(frag);
  el.sugg.hidden = !el.sugg.childElementCount;
}

// ---------- files ----------
function download(name, text, type) {
  const url = URL.createObjectURL(new Blob([text], { type }));
  const a = document.createElement('a');
  a.href = url;
  a.download = name;
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 2000);
}
const baseName = () => app.name.replace(/\.(score|txt)$/i, '');
function exportScore() {
  flushEdit();
  const name = `${baseName()}${app.b.name === 'main' ? '' : '-' + app.b.name.replace(/[^A-Za-z0-9_.-]/g, '_')}.score`;
  // A score file is source text, so saving must preserve even an invalid draft.
  download(name, el.score.value, 'text/plain');
  print(`exported ${name}`, 'ok');
}
function exportHoloFile() {
  if (!prepareScoreNavigation('export a holorecord')) return;
  app.b.t = app.t;
  const obj = exportHolo(app.branches, app.b.id, app.tier);
  const name = `${baseName()}.holo.json`;
  download(name, JSON.stringify(obj), 'application/json');
  print(`exported ${name}: ${obj.branches.length} branches, hashes for ${obj.branches.reduce((a, b) => a + b.ticks.length, 0)} ticks, no states`, 'ok');
}
async function importFile(file) {
  if (!file || !extensions.enabled(TAS_ID)) return;
  if (file.size > 64 * 1024 * 1024) { print('The file exceeds the 64 MiB local import limit.', 'warn'); return; }
  const generation = tasLifecycleGeneration;
  const revision = ++importRevision;
  const isCurrent = () => generation === tasLifecycleGeneration && revision === importRevision && extensions.enabled(TAS_ID);
  let text;
  try { text = await file.text(); }
  catch (error) {
    if (isCurrent()) print(`Import failed: ${error.message || error}`, 'warn');
    return;
  }
  // A disable/re-enable cycle also invalidates work started by the old lifecycle.
  if (!isCurrent()) return;
  let obj = null;
  if (/^\s*\{/.test(text)) { try { obj = JSON.parse(text); } catch { obj = null; } }
  if (obj?.schema === OBSERVATION_DRAFT_SCHEMA) {
    if (typeof host.openReferenceDraft !== 'function') { print('Reference draft viewing is unavailable in this host.', 'warn'); return; }
    try {
      const opened = await host.openReferenceDraft(file, isCurrent);
      if (isCurrent()) print(opened ? `Opened ${file.name} as reference observations. Timing and replay remain unqualified.` : 'Reference draft could not be opened.', opened ? 'ok' : 'warn');
    } catch (error) { if (isCurrent()) print(`Reference import failed: ${error.message || error}`, 'warn'); }
    return;
  }
  if (file.size > 20 * 1024 * 1024) { print('Scores and holorecords are limited to 20 MiB.', 'warn'); return; }
  if (obj && obj.format === HOLO_FORMAT) {
    pause();
    print(`import ${file.name}: rebuilding every branch by replay and checking each hash`, 'in');
    const res = importHolo(obj);
    if (res.error) { print(`import failed: ${res.error}`, 'warn'); return; }
    for (const r of res.report) print(r.ok ? `  ${r.name}: identical, ${r.ticks} ticks, chain ${r.chain}` : `  ${r.name}: ${r.note || 'first mismatch at tick ' + r.first}`, r.ok ? 'ok' : 'bad');
    if (!res.branches.length) return;
    app.tier = res.tier;
    for (const b of res.branches) { b.hist = newHist(`Import ${file.name}`, b.score); b.t = Math.min(b.forkTick, headOf(b)); }
    app.branches = res.branches;
    app.name = file.name.replace(/\.holo\.json$|\.json$/i, '') + '.score';
    el.hName.textContent = app.name;
    syncTierUi();
    for (const b of app.branches) b.thumb = thumbOf(b, b.forkTick);
    const act = res.branches.find((b) => b.name === res.active) || res.branches[0];
    app.b = null;
    activate(act, 0);
    say(el.stageStatus, `Imported ${res.branches.length} branches. ${res.report.every((r) => r.ok) ? 'Every tick hash matched on replay.' : 'Some ticks did not match on replay. They are lit in the roll.'}`, res.report.every((r) => r.ok) ? 'ok' : 'bad');
    return;
  }
  if (obj) { print(`import failed: not a ${HOLO_FORMAT} file`, 'warn'); return; }
  const p = parseScore(text);
  if (p.errors.length) {
    print(`import ${file.name}: ${p.errors.length} errors, first on line ${p.errors[0].line}: ${p.errors[0].msg}`, 'warn');
    return;
  }
  if (setScore(text.replace(/\r\n?/g, '\n'), `Import ${file.name}`)) {
    app.name = file.name;
    el.hName.textContent = app.name;
    print(`imported ${file.name} into ${app.b.name}`, 'ok');
  }
}

// ---------- dialogs and menu ----------
function ask(message, value = '', list = [], ok = 'OK') { return dialog({ message, value, list, input: true, ok }); }
function confirmBox(message, ok) { return dialog({ message, input: false, ok }).then((v) => v !== null); }
function dialog({ message, value = '', list = [], input = true, ok = 'OK' }) {
  return new Promise((resolve) => {
    const back = document.activeElement;
    el.dlgMsg.textContent = message;
    el.dlgIn.hidden = !input;
    el.dlgIn.value = value;
    el.dlgList.replaceChildren(...list.map((x) => { const o = document.createElement('option'); o.value = x; return o; }));
    el.dlgOk.textContent = ok;
    let done = false;
    const finish = (v, refocus = true) => {
      if (done) return;
      done = true;
      dismissDialog = null;
      el.dlgOk.onclick = el.dlgCancel.onclick = el.dlgIn.onkeydown = el.dlg.oncancel = null;
      if (el.dlg.open) el.dlg.close();
      if (refocus && back && back.focus) back.focus();
      resolve(v);
    };
    dismissDialog = () => finish(null, false);
    el.dlgOk.onclick = () => finish(input ? el.dlgIn.value.trim() : true);
    el.dlgCancel.onclick = () => finish(null);
    el.dlgIn.onkeydown = (e) => { if (e.key === 'Enter') { e.preventDefault(); el.dlgOk.click(); } };
    el.dlg.oncancel = (e) => { e.preventDefault(); finish(null); };
    el.dlg.showModal();
    (input ? el.dlgIn : el.dlgOk).focus();
    if (input) el.dlgIn.select();
  });
}

function closeMenu(refocus = true) {
  if (el.menu.hidden) return;
  el.menu.hidden = true;
  if (refocus) $('roll').focus({ preventScroll: true });
}

function openMenu(x, y) {
  const [a, b] = roll.selRange();
  const n = b - a + 1;
  const col = COLS[roll.sel.c];
  const items = [];
  const add = (label, fn, dis = '', cls = '') => items.push({ label, fn, dis, cls });
  add(`Insert ${n === 1 ? '1 empty tick' : n + ' empty ticks'} here`, () => rollEdit(`Insert ${n} ticks at ${a}`, (t, p, m) => insertTicks(t, p, m, a, n)));
  add(`Delete ${range(a, b)}`, () => rollEdit(`Delete ${range(a, b)}`, (t, p, m) => deleteTicks(t, p, m, a, b)));
  add(`Split the note at tick ${a}`, () => rollEdit(`Split at tick ${a}`, (t, p, m) => splitAt(t, p, m, a)));
  if (col.kind === 'key') {
    const unavailable = events.active && extensions.enabled(TAS_ID) ? '' : 'Enable the TAS package to edit inputs';
    add(`Hold ${col.label} on ${range(a, b)}`, () => patternSelection(a, b, col.id, 'hold'), unavailable);
    add(`Set autofire for ${col.label} on ${range(a, b)}…`, () => patternSelection(a, b, col.id, 'autofire'), unavailable);
  }
  add(`Clear ${col.label} on ${range(a, b)}`, () => clearSelection(false));
  add(`Clear all inputs on ${range(a, b)}`, () => clearSelection(true));
  add(`Add a mark at tick ${a}`, () => markAt(a));
  add(`Branch here (tick ${a})`, () => branchAt(a));
  items.push({ sep: true });
  items.push({ head: `Seek to tick ${a}` });
  for (const mode of MODES) add(mode, () => seekTo(a, mode), modeAllowed(mode, app.tier) ? '' : modeReason(mode, app.tier), 'sub');
  items.push({ sep: true });
  add(`Copy ${range(a, b)} as score lines`, () => copyTicks(a, b));
  add(`Paste inputs at tick ${a}`, () => pasteClipboard(a));
  add(`Paste and insert at tick ${a}`, () => pasteClipboard(a, true));
  add(`Verify from tick ${a}`, () => { if (a > headOf(app.b)) { say(el.stageStatus, 'That tick is not played yet.', 'warn'); return; } doVerify({ from: a }); });
  if (col.kind === 'key' || col.kind === 'mouse') add(`Watch ${col.label}`, () => addWatch(col.kind === 'mouse' ? `input.${col.id}` : `input.${col.id}`));
  const frag = document.createDocumentFragment();
  for (const it of items) {
    if (it.sep) { frag.append(document.createElement('hr')); continue; }
    if (it.head) { const h = document.createElement('div'); h.className = 'why'; h.textContent = it.head; frag.append(h); continue; }
    const bt = document.createElement('button');
    bt.type = 'button';
    bt.setAttribute('role', 'menuitem');
    bt.textContent = it.label;
    if (it.cls) bt.className = it.cls;
    if (it.dis) {
      bt.setAttribute('aria-disabled', 'true');
      bt.title = it.dis;
      bt.setAttribute('aria-label', `${it.label}, not allowed: ${it.dis}`);
    }
    events.listen(bt, 'click', () => { if (it.dis) return; closeMenu(); it.fn(); });
    frag.append(bt);
    if (it.dis) { const w = document.createElement('div'); w.className = 'why'; w.textContent = it.dis; frag.append(w); }
  }
  el.menu.replaceChildren(frag);
  el.menu.hidden = false;
  const r = el.menu.getBoundingClientRect();
  el.menu.style.left = Math.max(4, Math.min(x, innerWidth - r.width - 4)) + 'px';
  el.menu.style.top = Math.max(4, Math.min(y, innerHeight - r.height - 4)) + 'px';
  const first = el.menu.querySelector('button:not([aria-disabled])');
  if (first) first.focus();
}

async function copyTicks(a, b) {
  try {
    const text = ticksAsScore(app.b.prog, app.model, a, b);
    try { await navigator.clipboard.writeText(text); say(el.scoreStatus, `Copied ${range(a, b)} as score lines.`, 'ok'); }
    catch { print(text.trimEnd()); say(el.scoreStatus, 'Clipboard access is unavailable. The score lines are in the console.', 'warn'); }
  } catch (error) { say(el.scoreStatus, error.message, 'warn'); }
}

async function pasteInputText(text, at, insert = false) {
  try {
    const values = clipboardInputs(text);
    await rollEdit(`Paste ${values.length} input frames at ${at}${insert ? ' (insert)' : ''}`,
      (score, prog, model) => pasteTicks(score, prog, model, at, values, insert));
  } catch (error) { say(el.scoreStatus, error.message, 'warn'); }
}

async function pasteClipboard(at, insert = false) {
  const branch = app.b, score = branch.score, draft = el.score.value, generation = tasLifecycleGeneration;
  try {
    const text = await navigator.clipboard.readText();
    if (generation !== tasLifecycleGeneration || !events.active) return;
    if (app.b !== branch || app.b.score !== score || el.score.value !== draft) {
      say(el.scoreStatus, 'The score changed while the clipboard was opening. Paste again at the intended frame.', 'warn');
      return;
    }
    await pasteInputText(text, at, insert);
  } catch {
    say(el.scoreStatus, 'Focus the piano roll and press Ctrl+V (Command+V on Mac) to paste score inputs.', 'warn');
  }
}

// ---------- memory ----------
function addWatch(expr) {
  expr = expr.trim();
  if (app.watches.some((watch) => watch.expr === expr)) { say(el.scoreStatus, `Already watching ${expr}.`, 'warn'); return; }
  if (app.watches.length >= 32) { say(el.scoreStatus, 'The watch list is limited to 32 expressions.', 'warn'); return; }
  const w = compileWatch(expr);
  if (w.error) { say(el.scoreStatus, w.error, 'warn'); return; }
  app.watches.push({ expr, w });
  persistWatches();
  dirty('memory');
  showTab('bm', 'memory');
  showTab('mem', 'watch');
}

function inputAt(t) {
  const row = app.model && app.model.rows[t];
  const inp = rowInput(app.b.prog, row);
  const o = {};
  for (const k of [...KEY_ORDER, ...BUTTONS]) o[k] = heldIn(inp, k);
  o.mx = inp.mouse ? inp.mouse[0] : null;
  o.my = inp.mouse ? inp.mouse[1] : null;
  return o;
}

function recordInputStatus() {
  const tick = app.t + 1;
  if (!app.rec.on) return 'off';
  const keys = inputTokens({ bits: app.rec.bits, xbits: 0, mouse: null, al: null });
  if (app.g.cur.wait) return `waiting · @${tick} ${keys}`;
  const row = app.model && app.model.rows[tick];
  if (!row) return `no row · @${tick} ${keys}`;
  if (row.kind === 'wait' || row.kind === 'boot') return `waiting · @${tick} ${keys}`;
  const instruction = row.src >= 0 && app.b.prog.ins[row.src];
  if (row.kind === 'note' && instruction && instruction.rep) {
    const existing = rowInput(app.b.prog, row);
    return `${existing.bits === app.rec.bits ? 'repeat unchanged' : 'repeat edit blocked'} · @${tick} ${keys}`;
  }
  return `@${tick} ${keys}`;
}

function recordWriteBlocked() {
  if (!app.rec.on || app.g.cur.wait) return app.rec.on && app.g.cur.wait;
  const row = app.model && app.model.rows[app.t + 1];
  if (!row || row.kind === 'wait' || row.kind === 'boot') return true;
  if (row.kind !== 'note' || row.src < 0 || !app.b.prog.ins[row.src]?.rep) return false;
  return rowInput(app.b.prog, row).bits !== app.rec.bits;
}

function renderMemory() {
  app.dirty.memory = false;
  const b = app.b, t = app.t;
  const blocked = app.tier === 'hands-off';
  const rec = b.recs[t];
  const rows = el.watchBody;
  const want = app.watches.length;
  while (rows.childElementCount > want) rows.lastElementChild.remove();
  app.watches.forEach((w, i) => {
    let tr = rows.children[i];
    if (!tr) {
      tr = document.createElement('tr');
      const e = document.createElement('td'); e.className = 'expr';
      const v = document.createElement('td'); v.className = 'val';
      const s = document.createElement('td'); const cv = document.createElement('canvas'); cv.setAttribute('aria-hidden', 'true'); s.append(cv);
      const x = document.createElement('td'); const xb = document.createElement('button'); xb.type = 'button'; xb.className = 'x'; xb.textContent = '×'; x.append(xb);
      tr.append(e, v, s, x);
      rows.append(tr);
      e.tabIndex = 0;
      events.listen(e, 'dblclick', () => editWatch(tr));
      events.listen(e, 'keydown', (ev) => { if (ev.key === 'Enter' || ev.key === 'F2') { ev.preventDefault(); editWatch(tr); } });
      events.listen(xb, 'click', () => { const k = [...rows.children].indexOf(tr); app.watches.splice(k, 1); persistWatches(); tr.remove(); dirty('memory'); });
    }
    const [e, v, s, x] = tr.children;
    if (!e.querySelector('input') && e.textContent !== w.expr) e.textContent = w.expr;
    e.title = w.w.error ? w.w.error : 'Double-click or press Enter to edit';
    x.firstChild.setAttribute('aria-label', `Remove watch ${w.expr}`);
    let val = '';
    if (w.w.error) val = 'bad path';
    else if (blocked) val = 'not read';
    else if (!rec) val = 'not played';
    else val = fmtWatch(readWatch(w.w, stateOf(rec), inputAt(t)), w.expr);
    if (v.textContent !== val) v.textContent = val;
    const series = [];
    if (!blocked && !w.w.error && rec) for (let k = Math.max(0, t - 63); k <= t; k++) series.push(readWatch(w.w, stateOf(b.recs[k]), inputAt(k)));
    sparkline(s.firstChild, series);
  });
  if (!$('hex-pane').hidden) {
    if (blocked || !rec) el.hex.textContent = blocked ? 'Hands-off tier reads no memory.' : 'Not played yet.';
    else el.hex.replaceChildren(hexView(rec.g, t > 0 ? b.recs[t - 1].g : null));
  }
  app.memAt = t;
}

function editWatch(tr) {
  const i = [...el.watchBody.children].indexOf(tr);
  const cell = tr.firstElementChild;
  const inp = document.createElement('input');
  inp.value = app.watches[i].expr;
  inp.className = 'mono';
  inp.setAttribute('aria-label', 'Edit watch expression');
  cell.replaceChildren(inp);
  inp.focus();
  inp.select();
  const done = (keep) => {
    if (!inp.isConnected) return;
    const v = inp.value.trim();
    if (keep && v) {
      const compiled = compileWatch(v);
      if (app.watches.some((watch, index) => index !== i && watch.expr === v)) say(el.scoreStatus, `Already watching ${v}.`, 'warn');
      else if (!compiled.error) { app.watches[i] = { expr: v, w: compiled }; persistWatches(); }
      else say(el.scoreStatus, compiled.error, 'warn');
    }
    cell.textContent = app.watches[i].expr;
    cell.focus();
    dirty('memory');
  };
  events.listen(inp, 'keydown', (ev) => { if (ev.key === 'Enter') { ev.preventDefault(); done(true); } else if (ev.key === 'Escape') { ev.preventDefault(); done(false); } });
  events.listen(inp, 'blur', () => done(true));
}

// ---------- panels ----------
function renderBranches() {
  app.dirty.branches = false;
  const frag = document.createDocumentFragment();
  for (const b of app.branches) {
    const li = document.createElement('li');
    li.className = 'branch' + (b === app.b ? ' active' : '');
    const img = document.createElement('img');
    img.alt = '';
    if (b.thumb) img.src = b.thumb;
    const name = document.createElement('span');
    name.className = 'bname';
    name.textContent = b.name + (b === app.b ? ' (loaded)' : '');
    const meta = document.createElement('span');
    meta.className = 'bmeta';
    const par = b.parent ? app.branches.find((x) => x.id === b.parent) : null;
    meta.textContent = `${par ? `from ${par.name} at ${b.forkTick}` : 'root'} · head ${headOf(b)} · ${b.tier}`;
    if (b.tainted) { const s = document.createElement('span'); s.className = 'taint'; s.textContent = ' · tainted'; meta.append(s); }
    const act = document.createElement('div');
    act.className = 'bact';
    if (b !== app.b) {
      const load = document.createElement('button');
      load.type = 'button';
      load.className = 'btn small';
      load.textContent = 'Load';
      load.setAttribute('aria-label', `Load branch ${b.name}`);
      events.listen(load, 'click', () => { if (activate(b)) print(`loaded ${b.name} at tick ${app.t}`, 'ok'); });
      act.append(load);
      const cmp = document.createElement('button');
      cmp.type = 'button';
      cmp.className = 'btn small';
      cmp.textContent = 'Bisect with loaded';
      cmp.setAttribute('aria-label', `Bisect ${b.name} against ${app.b.name}`);
      events.listen(cmp, 'click', () => runLine(`bisect ${b.name} ${app.b.name}`));
      act.append(cmp);
    }
    li.append(img, name, meta, act);
    li.tabIndex = -1;
    const show = () => showThumb(b, li);
    events.listen(li, 'mouseenter', show);
    events.listen(li, 'focusin', show);
    events.listen(li, 'mouseleave', hideThumb);
    events.listen(li, 'focusout', hideThumb);
    frag.append(li);
  }
  el.branches.replaceChildren(frag);
}
function showThumb(b, li) {
  if (!b.thumb) return;
  el.thumbImg.src = b.thumb;
  el.thumbCap.textContent = `${b.name}, tick ${b === app.b ? app.t : b.t || b.forkTick}`;
  el.thumbPop.hidden = false;
  const r = li.getBoundingClientRect();
  const pw = 330, ph = 206;
  el.thumbPop.style.left = Math.max(4, r.left - pw - 8) + 'px';
  el.thumbPop.style.top = Math.max(4, Math.min(r.top, innerHeight - ph - 4)) + 'px';
}
function hideThumb() { el.thumbPop.hidden = true; }

function renderHistory() {
  app.dirty.history = false;
  const h = app.b.hist;
  const frag = document.createDocumentFragment();
  h.list.forEach((e, i) => {
    const li = document.createElement('li');
    li.className = i === h.pos ? 'cur' : i > h.pos ? 'undone' : '';
    const bt = document.createElement('button');
    bt.type = 'button';
    bt.textContent = `${i}  ${e.label}`;
    if (i === h.pos) bt.setAttribute('aria-current', 'step');
    events.listen(bt, 'click', () => jumpHistory(i));
    li.append(bt);
    frag.append(li);
  });
  el.history.replaceChildren(frag);
  const cur = el.history.querySelector('.cur');
  if (cur) cur.scrollIntoView({ block: 'nearest' });
  $('b-undo').disabled = h.pos <= 0;
  $('b-redo').disabled = h.pos >= h.list.length - 1;
}

// ---------- score editor ----------
function renderEditor() {
  app.dirty.editor = false;
  const lines = el.score.value.split('\n');
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
  app.selLines = '';
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
  const rows = el.backdrop.children, gut = el.gutter.children;
  if (line !== app.activeLine) {
    if (app.activeLine > 0 && rows[app.activeLine - 1]) {
      rows[app.activeLine - 1].classList.remove('active', 'wait');
      gut[app.activeLine - 1].classList.remove('active');
    }
    app.activeLine = line;
    if (line > 0 && rows[line - 1]) {
      rows[line - 1].classList.add(wait ? 'wait' : 'active');
      gut[line - 1].classList.add('active');
      if (document.activeElement !== el.score) {
        const lh = 18, top = (line - 1) * lh, view = el.score.clientHeight;
        if (top < el.score.scrollTop + lh || top > el.score.scrollTop + view - 3 * lh) {
          el.score.scrollTop = Math.max(0, top - Math.floor(view / 3));
          syncScroll();
        }
      }
    }
  }
  // Lines behind the roll selection.
  const [a, b] = roll.selRange();
  const key = `${a}:${b}:${app.b.id}`;
  if (key !== app.selLines && app.model) {
    for (const r of el.backdrop.querySelectorAll('.sel')) r.classList.remove('sel');
    const set = new Set();
    for (let t = a; t <= Math.min(b, a + 400); t++) {
      const row = app.model.rows[t];
      if (row && row.src >= 0) set.add(app.b.prog.ins[row.src].line);
    }
    for (const ln of set) if (rows[ln - 1]) rows[ln - 1].classList.add('sel');
    app.selLines = key;
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
    events.listen(btn, 'click', () => focusLine(e.line));
    li.append(btn);
    frag.append(li);
  }
  if (errors.length > 6) { const li = document.createElement('li'); li.textContent = `${errors.length - 6} more.`; frag.append(li); }
  el.errors.replaceChildren(frag);
}
function focusLine(line) {
  const idx = app.lines.slice(0, line - 1).reduce((a, l) => a + l.length + 1, 0);
  el.score.focus();
  el.score.setSelectionRange(idx, idx + (app.lines[line - 1] || '').length);
  el.score.scrollTop = Math.max(0, (line - 4) * 18);
  syncScroll();
}

// ---------- drawing ----------
function fitCanvas(canvas, c, w, h) {
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  const cw = canvas.clientWidth || w;
  const scale = cw / w;
  const bw = Math.round(cw * dpr), bh = Math.round(h * scale * dpr);
  if (canvas.width !== bw || canvas.height !== bh) { canvas.width = bw; canvas.height = bh; }
  c.setTransform(dpr * scale, 0, 0, dpr * scale, 0, 0);
}

function drawView() {
  app.dirty.view = false;
  const b = app.b;
  fitCanvas(el.view, ctx, W, H);
  app.wmBox = drawScene(ctx, app.g, { tier: app.tier, watermark: app.tier !== 'lab' || app.watermark, showPath: app.showPath, reduced: reducedMq.matches });
  const cw = el.tl.clientWidth || 600;
  const timelineHeight = app.timelineExpanded ? 56 : 24;
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  if (el.tl.width !== Math.round(cw * dpr) || el.tl.height !== Math.round(timelineHeight * dpr)) {
    el.tl.width = Math.round(cw * dpr);
    el.tl.height = Math.round(timelineHeight * dpr);
  }
  tctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  const head = headOf(b);
  const span = Math.max(head, app.model ? app.model.end : head, 60);
  app.tlGeom = drawTimeline(tctx, cw, timelineHeight, { recs: b.recs, t: app.t, marks: app.marks, v0: 0, v1: span, hover: app.hoverTick, div: b.div, captured: b.tier !== 'hands-off', compact: !app.timelineExpanded, inputAt, inputSource: b, prog: b.prog });
  el.tl.setAttribute('aria-valuemax', String(head));
  el.tl.setAttribute('aria-valuenow', String(app.t));
  $('timeline-position').textContent = `tick ${app.t} / ${head}`;
  $('minimal-score-position').textContent = `score tick ${app.t} / ${head}`;
  const input = inputAt(app.t);
  const previousInput = app.t > 0 ? inputAt(app.t - 1) : null;
  const held = [...KEY_ORDER, ...BUTTONS].filter((key) => input[key]);
  const pressed = previousInput ? [...KEY_ORDER, ...BUTTONS].filter((key) => input[key] && !previousInput[key]) : [];
  const released = previousInput ? [...KEY_ORDER, ...BUTTONS].filter((key) => !input[key] && previousInput[key]) : [];
  const inputState = [held.length ? `held ${held.join(', ')}` : 'no buttons held'];
  if (pressed.length) inputState.push(`pressed ${pressed.join(', ')}`);
  if (released.length) inputState.push(`released ${released.join(', ')}`);
  if (input.mx !== null && input.my !== null) inputState.push('mouse position command set');
  el.tl.setAttribute('aria-valuetext', `tick ${app.t} of ${head}; ${app.t <= head ? 'recorded branch' : 'authored score ahead, not recorded yet'}; ${inputState.join('; ')}`);
  if (input.mx !== null && input.my !== null) held.push(`mouse ${input.mx},${input.my}`);
  const runState = app.playing ? `Playing ${app.speed}×` : 'Paused';
  const areaState = app.g.mode === 'load' ? `${app.g.area} loading` : app.g.area;
  $('metric-branch').textContent = app.b.name;
  $('metric-playback').textContent = runState;
  $('metric-tick').textContent = `${app.t} / ${head}`;
  $('metric-time').textContent = fmtVt(app.g.vt);
  $('metric-rate').textContent = `${app.g.hz} Hz`;
  $('metric-rate').title = 'Virtual simulation clock rate; this is not a measured render frame rate.';
  $('metric-area').textContent = areaState;
  $('metric-input').textContent = held.length ? held.join(' + ') : 'idle';
  $('metric-next-input').textContent = `@${app.t + 1} ${inputTokens(rowInput(app.b.prog, app.model && app.model.rows[app.t + 1]))}`;
  $('metric-next-input').title = 'Input authored in the score for the next tick. It has not been applied yet.';
  $('metric-record-input').textContent = recordInputStatus();
  $('metric-record-input').classList.toggle('is-recording', app.rec.on);
  $('metric-record-input').classList.toggle('is-paused-write', recordWriteBlocked());
  $('metric-record-input').title = app.rec.on
    ? 'Recorder pad state targeted at the next tick; a wait or repeat boundary can prevent writing it.'
    : 'Recording is off. Turn on Record to author pad input on the next tick.';
  $('metric-rng').textContent = Number.isFinite(app.g.rng) ? `0x${(app.g.rng >>> 0).toString(16).padStart(8, '0')}` : '—';
  const tickJump = $('tick-jump');
  if (document.activeElement !== tickJump) tickJump.value = String(app.t);
  tickJump.max = String(head);
  el.hTick.textContent = `score tick ${app.t}`;
  el.hEp.textContent = `model ep ${app.g.epoch}`;
  el.hHz.textContent = `score ${app.g.hz} Hz`;
  el.hHz.title = 'Score authoring rate. The runtime reports render presentation cadence separately.';
}

let tasFrameRequest = null;
function startTasRendering() {
  if (tasFrameRequest === null) tasFrameRequest = requestAnimationFrame(frame);
}
function stopTasRendering() {
  if (tasFrameRequest !== null) cancelAnimationFrame(tasFrameRequest);
  tasFrameRequest = null;
}
function frame(now) {
  tasFrameRequest = null;
  if (!extensions.enabled(TAS_ID)) return;
  if (app.playing) {
    const dt = Math.min(100, now - app.last);
    app.last = now;
    if (centerTarget === 'original' && app.wasmInputPending) {
      // Each live score tick must be acknowledged before the next is emitted.
      // Drop elapsed wall time while back-pressured so recovery never compresses
      // several old inputs into one paint or silently runs the score ahead.
      app.acc = 0;
    } else app.acc += (dt / 1000) * app.g.hz * app.speed;
    // The stand-in can catch up many ticks in one paint. Live WASM keyboard
    // events cannot: a down/up burst inside one browser task may never reach a
    // game frame. Keep the real-game target to one input transition per RAF;
    // retain backlog rather than compressing several score frames together.
    const maxStepsThisPaint = centerTarget === 'original' ? 1 : 64;
    let n = centerTarget === 'original' && app.wasmInputPending ? 0 : Math.min(Math.floor(app.acc), maxStepsThisPaint);
    app.acc -= n;
    while (n-- > 0 && app.playing) stepForward();
    if (app.dirty.rows) rebuild();
    roll.data.playhead = app.t;
    if (app.followPlayhead) roll.follow(app.t);
  }
  if (app.dirty.rows) rebuild();
  if (roll.data) roll.data.playhead = app.t;
  if (app.dirty.view) drawView();
  if (app.dirty.roll || roll.dirty) { roll.draw(); app.dirty.roll = false; }
  if (app.dirty.editor) renderEditor();
  if (app.b) updateActiveLine();
  if (app.dirty.branches) renderBranches();
  if (app.dirty.history) renderHistory();
  if (app.dirty.memory && (!app.playing || now - app.memTime > 120)) { app.memTime = now; renderMemory(); }
  events.prune();
  if (extensions.enabled(TAS_ID)) startTasRendering();
}

// ---------- layout ----------
function setFollowPlayhead(on) {
  const next = Boolean(on);
  const changed = app.followPlayhead !== next;
  app.followPlayhead = next;
  const button = $('b-follow-playhead');
  button.setAttribute('aria-pressed', String(app.followPlayhead));
  button.title = app.followPlayhead
    ? 'Keep the playing tick in view; scrolling the roll turns this off'
    : 'Resume following the playing tick';
  if (changed) {
    try { localStorage.setItem(FOLLOW_PLAYHEAD_KEY, String(app.followPlayhead)); } catch { /* Storage may be disabled. */ }
    if (app.followPlayhead && roll && app.t != null) roll.follow(app.t);
  }
}

function setTimelineExpanded(expanded, { persist = false, density = null, reveal = false } = {}) {
  const nextDensity = density ?? (expanded ? 'editor' : 'timeline');
  if (!['editor', 'timeline', 'minimal'].includes(nextDensity)) throw new Error('Unsupported timeline density.');
  const previousFocus = document.activeElement;
  const workspace = $('live-tas-workspace');
  const ownedFocus = workspace?.contains(previousFocus);
  rememberTimelineDensity(nextDensity);
  document.body.dataset.tasTimelineDensity = nextDensity;
  $('timeline-density').value = nextDensity;
  $('timeline-minimal-hud').hidden = nextDensity !== 'minimal';
  app.timelineExpanded = nextDensity === 'editor';
  const panel = $('timeline-panel');
  panel.classList.toggle('expanded', app.timelineExpanded);
  applyTimelineDetailVisibility();
  const button = $('b-toggle-timeline');
  button.textContent = app.timelineExpanded ? 'Collapse' : 'Expand';
  button.setAttribute('aria-expanded', String(app.timelineExpanded));
  if (!app.timelineExpanded && isDockPanel($('p-roll'))) window.TelosDock?.hide('piano-roll');
  // Revealing a focused editor during layout changes is temporary, not a saved choice.
  if ((persist || reveal) && app.timelineExpanded && isDockPanel($('p-roll'))) window.TelosDock?.show('piano-roll');
  if (persist) {
    try { localStorage.setItem(TIMELINE_EXPANDED_KEY, String(app.timelineExpanded)); } catch { /* Layout remains usable for this visit. */ }
    try { rememberTimelineDensity(nextDensity,{persist:true}); } catch { /* Keep this visit's intended density. */ }
  }
  if (ownedFocus && !previousFocus.getClientRects().length) {
    (nextDensity === 'minimal' ? $('b-density-timeline') : $('timeline-density')).focus({preventScroll:true});
  }
  dirty('view', 'roll', 'editor');
}

function usesWideWorkspace() {
  return document.body.dataset.dockWorkspace === 'true' || (wideMq.matches && document.body.dataset.tasPresentation !== 'overlay');
}

// Dock panels own their visibility; native tabs only control their original panes.
function isDockPanel(node) {
  return document.body.dataset.dockWorkspace === 'true' && Boolean(node?.dataset.dockId);
}

function registerDockViews() {
  const dock = window.TelosDock;
  if (!dock?.register) return;
  for (const [id, title, paneId] of [
    ['piano-roll', 'Piano roll', 'p-roll'],
    ['sample-state', 'Sample state / memory', 'p-memory'],
  ]) {
    const pane = $(paneId);
    if (!isDockPanel(pane)) dock.register(id, title, pane, { visible: false });
  }
  registerDockHistory(dock);
  registerDockHex(dock);
  registerDockCenterViews(dock);
  dock.setAvailable?.(TAS_DOCK_IDS, extensions.enabled(TAS_ID));
  applyLayout();
}

function registerDockHistory(dock) {
  if ($('p-history')) return;
  const branches = $('p-branches');
  const head = branches.querySelector('.hist-head');
  const list = $('history');
  if (!head || list.parentElement !== branches) return;
  // Move the existing controls and list together: their listeners, active
  // branch history, and renderer references remain attached to the same nodes.
  const panel = document.createElement('section');
  panel.id = 'p-history';
  panel.className = 'panel';
  panel.tabIndex = -1;
  panel.setAttribute('aria-labelledby', 'hist-h');
  branches.insertBefore(panel, head);
  panel.append(head, list);
  if (!dock.register('score-history', 'Score history', panel, { visible: false })) {
    branches.insertBefore(head, panel);
    branches.insertBefore(list, panel);
    panel.remove();
    return;
  }
  const open = document.createElement('button');
  open.type = 'button';
  open.className = 'btn small';
  open.textContent = 'History';
  open.setAttribute('aria-controls', panel.id);
  events.listen(open, 'click', () => {
    dock.show('score-history');
    panel.focus({ preventScroll: true });
  });
  branches.querySelector('.panel-head').append(open);
}

function registerDockHex(dock) {
  const pane = $('hex-pane');
  if (isDockPanel(pane) || !dock.register('sample-hex', 'Sample state hex', pane, { visible: false })) return;
  pane.classList.add('panel');
  pane.setAttribute('role', 'region');
  pane.removeAttribute('aria-labelledby');
  pane.setAttribute('aria-label', 'Serialized sample state');
  // Watches remain in their existing state panel. Dock navigation opens either
  // view without closing the other, so values and bytes can be compared.
  $('watch-pane').hidden = false;
  $('watch-pane').setAttribute('role', 'region');
  $('watch-pane').removeAttribute('aria-labelledby');
  $('watch-pane').setAttribute('aria-label', 'Sample state watches');
  const navigation = $('tab-watch').parentElement;
  navigation.setAttribute('role', 'group');
  navigation.setAttribute('aria-label', 'Open sample state views');
  for (const id of ['tab-watch', 'tab-hex']) {
    const button = $(id);
    button.removeAttribute('role');
    button.removeAttribute('aria-selected');
    button.tabIndex = 0;
    button.classList.add('btn', 'small');
    button.dataset.dockViewShortcut = 'true';
  }
  $('tab-watch').setAttribute('aria-controls', 'p-memory');
  // A paused playhead may have changed while Hex was hidden. The dock's Panels
  // menu can reveal it without going through showTab, so invalidate that view.
  events.observe(pane, () => {
    if (!pane.hidden) dirty('memory');
  }, { attributes: true, attributeFilter: ['hidden'] });
  dirty('memory');
}

function registerDockCenterViews(dock) {
  for (const [which, id, title, targetLabel] of [
    ['scene', 'sample-scene', 'How it works / sample scene', 'Use sample scene target'],
  ]) {
    const pane = $(`${which}-pane`);
    if (isDockPanel(pane) || !dock.register(id, title, pane, { visible: false })) continue;
    pane.classList.add('panel');
    pane.setAttribute('role', 'region');
    pane.removeAttribute('aria-labelledby');
    pane.setAttribute('aria-label', title);
    const controls = document.createElement('div');
    controls.className = 'row';
    const selectTarget = document.createElement('button');
    selectTarget.type = 'button';
    selectTarget.className = 'btn small';
    selectTarget.textContent = targetLabel;
    selectTarget.dataset.centerTarget = which;
    selectTarget.setAttribute('aria-pressed', String(centerTarget === which));
    events.listen(selectTarget, 'click', () => showTab('center', which));
    controls.append(selectTarget);
    if (which === 'scene') {
      const hideSceneButton = $('b-hide-scene');
      controls.append(hideSceneButton);
      hideSceneButton.hidden = false;
    }
    pane.querySelector('.dock-titlebar').after(controls);
    // Opening a view from Panels is passive: it does not redirect authored
    // input or pause the selected target.
    const reveal = () => {
      if (pane.hidden) return;
      dirty('view');
    };
    events.observe(pane, reveal, { attributes: true, attributeFilter: ['hidden'] });
    reveal();
  }
  if (!isDockPanel($('scene-pane'))) return;
  const navigation = $('tab-original').parentElement;
  navigation.setAttribute('role', 'group');
  navigation.setAttribute('aria-label', 'Choose playback target and open its view');
  for (const which of ['original', 'scene', 'real']) {
    const button = $(`tab-${which}`);
    button.removeAttribute('role');
    button.removeAttribute('aria-selected');
    button.setAttribute('aria-pressed', String(centerTarget === which));
    button.tabIndex = 0;
    button.classList.add('btn', 'small');
    button.dataset.dockViewShortcut = 'true';
  }
  // The game panel itself stays in place, including the live iframe ancestry.
  $('original-pane').setAttribute('role', 'region');
  $('original-pane').removeAttribute('aria-labelledby');
  $('original-pane').setAttribute('aria-label', 'KOTOR game');
}

function applyTimelineDetailVisibility() {
  const expanded = app.timelineExpanded;
  const compact = !usesWideWorkspace();
  const scoreSelected = app.rsTab === 'score';
  $('rs-tabs').hidden = !expanded || isDockPanel($('p-roll'));
  if (!isDockPanel($('p-roll'))) $('p-roll').hidden = !expanded || (compact && scoreSelected);
  // On compact layouts the score editor shares the expandable live workspace
  // so the collapsed view remains a single seek line. Wide layouts retain the
  // editor in its dedicated column.
  if (compact) $('p-score').hidden = !expanded || !scoreSelected;
}

function mountLiveTasWorkspace() {
  const workspace = $('live-tas-workspace');
  if (workspace.dataset.mounted === 'true') return;
  const transport = document.querySelector('#stage-row .transport');
  if (!transport) return;
  workspace.append(transport, $('timeline-panel'), $('rs-tabs'), $('p-roll'));
  workspace.dataset.mounted = 'true';
}

function showTab(group, which) {
  const set = (tabId, paneId, on) => {
    const tab = $(tabId), pane = $(paneId);
    if (tab.dataset.dockViewShortcut) {
      tab.setAttribute('aria-pressed', String(on));
      tab.tabIndex = 0;
    } else {
      tab.setAttribute('aria-selected', String(on));
      tab.tabIndex = on ? 0 : -1;
    }
    if (pane && !isDockPanel(pane)) pane.hidden = !on;
  };
  if (group === 'center') {
    host.selectTarget(which);
  } else if (group === 'rs') {
    app.rsTab = which;
    if (document.body.classList.contains('compact')) {
      set('tab-roll', 'p-roll', which === 'roll');
      set('tab-score', 'p-score', which === 'score');
    }
    dirty('roll', 'editor');
  } else if (group === 'bm') {
    app.bmTab = which;
    if (document.body.classList.contains('compact')) {
      set('tab-branches', 'p-branches', which === 'branches');
      set('tab-memory', 'p-memory', which === 'memory');
    }
    dirty('memory');
  } else if (group === 'mem') {
    if (isDockPanel($('hex-pane'))) {
      window.TelosDock.show(which === 'hex' ? 'sample-hex' : 'sample-state');
      dirty('memory');
      return;
    }
    set('tab-watch', 'watch-pane', which === 'watch');
    set('tab-hex', 'hex-pane', which === 'hex');
    dirty('memory');
  }
}

function applyLayout() {
  const focused = document.activeElement;
  const focusedPanel = focused?.closest('#p-score, #p-roll, #p-branches, #p-memory');
  mountLiveTasWorkspace();
  if (document.body.dataset.dockWorkspace === 'true') {
    // Legacy compact startup may have nested the score inside the transport
    // before the dock owner mounted. Restore the same editor node as a grid
    // child; its input listeners, selection and dock state remain intact.
    const score = $('p-score');
    const cockpit = $('cockpit');
    if (score.parentElement !== cockpit) cockpit.append(score);
    // All other registered ancestry, especially the live game iframe, stays put.
    if (!isDockPanel($('p-roll'))) $('p-roll').hidden = !app.timelineExpanded;
    if (!isDockPanel($('p-branches'))) $('p-branches').hidden = false;
    if (!isDockPanel($('p-memory'))) $('p-memory').hidden = false;
    document.body.classList.add('wide');
    document.body.classList.remove('compact');
    $('hstat').insertBefore($('h-ep'), $('h-hz'));
    $('hstat').insertBefore($('h-seek-wrap'), $('wm-wrap'));
    $('ovf-menu').hidden = true;
    $('ovf-btn').setAttribute('aria-expanded', 'false');
    applyTimelineDetailVisibility();
    if (focusedPanel && focused.getClientRects().length) focused.focus({ preventScroll: true });
    dirty('view', 'roll', 'editor');
    return;
  }
  const wide = usesWideWorkspace();
  document.body.classList.toggle('wide', wide);
  document.body.classList.toggle('compact', !wide);
  const score = $('p-score');
  // When columns become tabs, keep the panel containing focus selected.
  // Revealing an active edit is temporary; only explicit density choices save.
  if (focusedPanel && !wide) {
    if (focusedPanel.id === 'p-score' || focusedPanel.id === 'p-roll') {
      app.rsTab = focusedPanel.id === 'p-score' ? 'score' : 'roll';
      if (!app.timelineExpanded) setTimelineExpanded(true);
    } else {
      app.bmTab = focusedPanel.id === 'p-memory' ? 'memory' : 'branches';
    }
  }
  if (wide) {
    if (score.parentElement !== $('cockpit')) $('cockpit').prepend(score);
    score.hidden = false;
    $('p-roll').hidden = false;
    $('p-branches').hidden = false;
    $('p-memory').hidden = false;
    $('hstat').insertBefore($('h-ep'), $('h-hz'));
    $('hstat').insertBefore($('h-seek-wrap'), $('wm-wrap'));
    $('ovf-menu').hidden = true;
    $('ovf-btn').setAttribute('aria-expanded', 'false');
  } else {
    if (score.parentElement !== $('live-tas-workspace')) $('live-tas-workspace').append(score);
    $('ovf-menu').append($('h-ep'), $('h-seek-wrap'));
    showTab('rs', app.rsTab || 'roll');
    showTab('bm', app.bmTab || 'branches');
  }
  applyTimelineDetailVisibility();
  if (focusedPanel && focused.getClientRects().length) focused.focus({ preventScroll: true });
  dirty('view', 'roll', 'editor');
}

// ---------- events ----------
function isTyping(t) {
  if (!t) return false;
  if (t.isContentEditable || t.closest?.('[contenteditable="true"]')) return true;
  if (t.tagName === 'TEXTAREA' || t.tagName === 'SELECT') return true;
  return t.tagName === 'INPUT' && !['button', 'checkbox', 'color', 'file', 'image', 'radio', 'range', 'reset', 'submit'].includes(t.type);
}

// Hotkeys drive the tool, never the game: no key here reaches the sim as input.
function onKey(e) {
  if (!extensions.enabled(TAS_ID)) return;
  // Focused dock splitters, rails and editors own keys they have handled.
  // In particular, resizing a splitter with Home/End must never seek the game.
  if (e.defaultPrevented || e.isComposing || e.keyCode === 229) return;
  const ctrl = e.ctrlKey || e.metaKey;
  const k = e.key;
  if (el.dlg.open) return;
  if (e.repeat) return;
  if (!el.menu.hidden && k === 'Escape') { e.preventDefault(); closeMenu(); return; }
  if (!app.engaged) return;
  if (ctrl && (k === 's' || k === 'S') && !e.shiftKey) { e.preventDefault(); exportScore(); return; }
  if (ctrl && k === '`') { e.preventDefault(); dispatchEvent(new Event('telos:reveal-console')); el.cli.focus(); return; }
  if (ctrl && (k === 'g' || k === 'G')) { e.preventDefault(); dispatchEvent(new Event('telos:reveal-console')); el.cli.value = 'seek '; el.cli.focus(); updateSuggestions(); return; }
  // Numeric/watch/mouse editors own their text undo. The score editor uses
  // the document history; other fields must not undo unrelated score inputs.
  if (ctrl && ['z', 'Z', 'y', 'Y'].includes(k) && isTyping(e.target) && e.target !== el.score) return;
  if (ctrl && (k === 'z' || k === 'Z') && e.target !== el.cli && e.target.id !== 'watch-in') { e.preventDefault(); if (e.shiftKey) redo(); else undo(); return; }
  if (ctrl && (k === 'y' || k === 'Y') && e.target !== el.cli) { e.preventDefault(); redo(); return; }
  if (ctrl || e.altKey || e.shiftKey || isTyping(e.target)) return;
  const inRoll = e.target && e.target.id === 'roll';
  if (k === 'k' || k === 'K') { e.preventDefault(); togglePlay(); }
  else if (k === '.') { e.preventDefault(); pause(); stepForward(); }
  else if (k === ',') { e.preventDefault(); pause(); seekTo(app.t - 1, app.mode, { log: false }); }
  else if (k === 'Home' && !inRoll && e.target !== el.tl) { e.preventDefault(); seekTo(0, app.mode, { log: false }); }
  else if (k === 'End' && !inRoll && e.target !== el.tl) { e.preventDefault(); goEnd(); }
}

function tlTickAt(ev, recordedOnly = true) {
  const rect = el.tl.getBoundingClientRect();
  const tick = app.tlGeom ? app.tlGeom.tickAt(ev.clientX - rect.left) : 0;
  return recordedOnly ? Math.min(tick, headOf(app.b)) : tick;
}

let roll;
let liveTimer = 0;
let centerTarget = host.getTarget();
events.listen(window, 'telos:workspace-target', event => {
  centerTarget = event.detail.target;
  dirty('view', 'roll');
});
events.listen(window, 'telos:wasm-pending', event => {
  if (!extensions.enabled(TAS_ID)) return;
  app.wasmInputPending = event.detail?.pending === true;
  app.wasmPendingTick = Number.isSafeInteger(event.detail?.tick) ? event.detail.tick : null;
});
events.listen(window, 'telos:wasm-ack', event => {
  if (!extensions.enabled(TAS_ID)) return;
  const detail = event.detail;
  if (detail?.delayed) say(el.stageStatus, `Runtime event dispatch reached score tick ${detail.tick} after ${detail.latencyMs.toFixed(1)} ms; playback waited before advancing.`, 'warn');
  if (app.wasmReleaseAfterAck) {
    const reason = app.wasmReleaseAfterAck;
    app.wasmReleaseAfterAck = null;
    releaseWasmInput(reason);
    if (centerTarget === 'original') dispatchEvent(new CustomEvent('telos:wasm-vm-control', { detail:{ action:'pause' } }));
  }
});
events.listen(window, 'telos:wasm-transport-error', event => {
  app.wasmInputPending = false;
  app.wasmPendingTick = null;
  app.wasmReleaseAfterAck = null;
  if (app.playing) pause();
  else releaseWasmInput('transport-error');
  say(el.stageStatus, event.detail?.message || 'Runtime input transport failed. Playback stopped.', 'bad');
});

function bind() {
  // Resolve custom bindings and key capture before tabs, menus or the rail.
  // Keep the existing owner responsible for typing/modal guards and repeats.
  events.listen(document, 'keydown', (event) => {
    if (extensions.enabled(TAS_ID) && keyboardControls.handle(event, isTyping(event.target), el.dlg.open)) event.stopPropagation();
  }, { capture: true });
  events.listen(document, 'keydown', onKey);
  keyboardControls.bind();
  events.listen($('roll'), 'copy', (event) => {
    if (document.activeElement !== $('roll') || el.dlg.open || !event.clipboardData) return;
    event.preventDefault(); event.stopPropagation();
    try {
      const [a, b] = roll.selRange();
      event.clipboardData.setData('text/plain', ticksAsScore(app.b.prog, app.model, a, b));
      say(el.scoreStatus, `Copied ${range(a, b)} as score lines.`, 'ok');
    } catch (error) { say(el.scoreStatus, error.message, 'warn'); }
  });
  events.listen($('roll'), 'paste', (event) => {
    if (document.activeElement !== $('roll') || el.dlg.open || !event.clipboardData) return;
    event.preventDefault(); event.stopPropagation();
    pasteInputText(event.clipboardData.getData('text/plain'), roll.selRange()[0]);
  });
  setTimelineExpanded(app.timelineExpanded,{density:preferredTimelineDensity()});
  const followButton = $('b-follow-playhead');
  setFollowPlayhead(app.followPlayhead);
  events.listen(followButton, 'click', () => setFollowPlayhead(!app.followPlayhead));
  events.listen(el.play, 'click', togglePlay);
  events.listen($('b-step'), 'click', () => { pause(); stepForward(); });
  events.listen($('b-back'), 'click', () => seekTo(app.t - 1, app.mode, { log: false }));
  events.listen($('b-start'), 'click', () => seekTo(0, app.mode, { log: false }));
  events.listen($('b-end'), 'click', () => goEnd());
  events.listen(el.speed, 'change', () => { app.speed = +el.speed.value; dirty('view'); });
  events.listen(el.showPath, 'change', () => { app.showPath = el.showPath.checked; dirty('view'); });
  events.listen(el.budget, 'change', () => {
    pause();
    const t = app.t;
    for (const b of app.branches) {
      const head = headOf(b);
      b.cfg = { ...b.cfg, budget: +el.budget.value };
      b.recs = [snapshot(initState(b.cfg), '')];
      extend(b, head, simCtx);
      b.div = -1;
    }
    app.g = restore(app.b.recs[Math.min(t, headOf(app.b))]);
    app.t = Math.min(t, headOf(app.b));
    rebuild();
    dirty('view', 'memory', 'branches');
    say(el.stageStatus, `Path budget set to ${el.budget.value} nodes. Every branch was replayed from tick 0.`, 'ok');
  });
  events.listen(el.tier, 'change', () => setTier(el.tier.value));
  events.listen(el.mode, 'change', () => { app.mode = el.mode.value; say(el.seekReport, `Seek mode set to ${app.mode}.`); dirty('view'); });
  events.listen(el.wm, 'change', () => {
    if (app.tier !== 'lab') { el.wm.checked = true; return; }
    app.watermark = el.wm.checked;
    dirty('view');
  });
  events.listen($('ovf-btn'), 'click', () => {
    const m = $('ovf-menu');
    m.hidden = !m.hidden;
    $('ovf-btn').setAttribute('aria-expanded', String(!m.hidden));
  });
  events.listen(el.rec, 'click', () => toggleRecord());
  events.listen($('b-hide-scene'), 'click', () => {
    const hide = !$('stage-row').hidden;
    $('stage-row').hidden = hide;
    $('b-hide-scene').setAttribute('aria-pressed', String(hide));
    $('b-hide-scene').textContent = hide ? 'Show sample scene' : 'Hide sample scene';
    dirty('view', 'roll');
  });
  for (const k of el.pad.querySelectorAll('.padk')) {
    events.listen(k, 'click', () => {
      const bit = BIT[k.dataset.k];
      app.rec.bits ^= bit;
      k.setAttribute('aria-pressed', String(!!(app.rec.bits & bit)));
      dirty('view');
    });
  }
  events.listen($('b-export-score'), 'click', exportScore);
  events.listen($('b-export-holo'), 'click', exportHoloFile);
  events.listen($('f-import'), 'change', (e) => { const f = e.target.files[0]; e.target.value = ''; importFile(f); });
  events.listen($('b-undo'), 'click', undo);
  events.listen($('b-redo'), 'click', redo);
  events.listen($('b-find'), 'click', findMark);
  events.listen($('b-prev-mark'), 'click', () => navigateMark(-1));
  events.listen($('b-next-mark'), 'click', () => navigateMark(1));
  events.listen($('b-toggle-timeline'), 'click', () => setTimelineExpanded(!app.timelineExpanded, { persist: true }));
  events.listen($('timeline-density'), 'change', () => {
    const density = $('timeline-density').value;
    setTimelineExpanded(density === 'editor',{persist:true,density});
  });
  events.listen($('b-density-timeline'), 'click', () => setTimelineExpanded(false,{persist:true,density:'timeline'}));
  events.listen(window, 'telos:timeline-density', event => {
    const request = event.detail;
    if (!events.active || !extensions.enabled(TAS_ID) || !request) return;
    try {
      setTimelineExpanded(request.density === 'editor',{density:request.density,reveal:true});
      request.applied = true;
    } catch (error) { request.error = error.message; }
  });
  events.listen(document.querySelector('.skip[href="#roll"]'), 'click', () => {
    if (isDockPanel($('p-roll'))) window.TelosDock?.show('piano-roll');
    else { showTab('rs', 'roll'); setTimelineExpanded(true); }
  });
  events.listen($('b-seek-tick'), 'click', seekEnteredTick);
  events.listen($('tick-jump'), 'keydown', (event) => {
    if (event.key === 'Enter') { event.preventDefault(); seekEnteredTick(); }
  });
  const branchSel = () => branchAt(roll.selRange()[0]);
  events.listen($('b-branch'), 'click', branchSel);
  events.listen($('b-branch2'), 'click', branchSel);
  events.listen($('b-watch'), 'click', () => { const v = $('watch-in').value.trim(); if (v) { addWatch(v); $('watch-in').value = ''; } });
  events.listen($('watch-in'), 'keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); $('b-watch').click(); } });
  events.listen($('pause-watch-change'), 'change', (event) => {
    app.pauseWatchChange = event.target.checked;
    saveBreakpointEnabled(app.pauseWatchChange);
    say(el.stageStatus, app.pauseWatchChange ? `Playback will pause when ${app.watchBreakExpr} changes.` : 'Pause-on-change is off.');
  });
  events.listen($('watch-break-in'), 'change', (event) => {
    const expr = event.target.value.trim();
    app.watchBreakExpr = expr;
    app.watchBreak = expr ? compileBreakpoint(expr) : null;
    saveBreakpointExpression(expr);
    if (!expr || (app.watchBreak && app.watchBreak.w.error)) {
      event.target.setAttribute('aria-invalid', String(!!(app.watchBreak && app.watchBreak.w.error)));
      app.pauseWatchChange = false;
      $('pause-watch-change').checked = false;
      saveBreakpointEnabled(false);
      if (app.watchBreak?.w.error) say(el.stageStatus, app.watchBreak.w.error, 'warn');
      else say(el.stageStatus, 'Break expression cleared.');
    } else {
      event.target.setAttribute('aria-invalid', 'false');
      say(el.stageStatus, `Break expression set to ${expr}.`);
    }
    syncTierUi();
  });

  for (const [tab, group, which] of [['tab-roll', 'rs', 'roll'], ['tab-score', 'rs', 'score'],
    ['tab-branches', 'bm', 'branches'], ['tab-memory', 'bm', 'memory'], ['tab-watch', 'mem', 'watch'], ['tab-hex', 'mem', 'hex']]) {
    events.listen($(tab), 'click', () => showTab(group, which));
    events.listen($(tab), 'keydown', (e) => {
      if (e.ctrlKey || e.metaKey || e.altKey || e.shiftKey) return;
      if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(e.key)) return;
      // Tab navigation owns these keys; Home/End must not reach global seek.
      e.preventDefault();
      e.stopPropagation();
      const list = [...$(tab).parentElement.querySelectorAll('[role="tab"], [data-dock-view-shortcut]')];
      const index = e.key === 'Home' ? 0 : e.key === 'End' ? list.length - 1
        : (list.indexOf($(tab)) + (e.key === 'ArrowRight' ? 1 : list.length - 1)) % list.length;
      const next = list[index];
      if (next !== $(tab)) {
        next.focus();
        next.click();
      }
    });
  }

  events.listen(el.score, 'input', () => {
    clearTimeout(app.editTimer);
    app.editTimer = setTimeout(editorCommit, 350);
  });
  events.listen(el.score, 'scroll', syncScroll);

  events.listen(el.cli, 'keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); const v = el.cli.value; el.cli.value = ''; el.sugg.hidden = true; runLine(v); }
    else if (e.key === 'Escape') { e.preventDefault(); el.cli.value = ''; el.sugg.hidden = true; }
    else if (e.key === 'ArrowDown' && !el.sugg.hidden) { e.preventDefault(); const f = el.sugg.querySelector('button'); if (f) f.focus(); }
    else if (e.key === 'ArrowUp' || e.key === 'ArrowDown') {
      e.preventDefault();
      app.cmdPos = Math.max(0, Math.min(app.cmdHist.length, app.cmdPos + (e.key === 'ArrowUp' ? -1 : 1)));
      el.cli.value = app.cmdHist[app.cmdPos] || '';
      updateSuggestions();
    }
  });
  events.listen(el.cli, 'input', updateSuggestions);
  events.listen(el.sugg, 'keydown', (e) => {
    const list = [...el.sugg.querySelectorAll('button')];
    const i = list.indexOf(document.activeElement);
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') { e.preventDefault(); const n = list[i + (e.key === 'ArrowDown' ? 1 : -1)]; if (n) n.focus(); else el.cli.focus(); }
    else if (e.key === 'Escape') { e.preventDefault(); el.sugg.hidden = true; el.cli.focus(); }
  });

  events.listen(el.menu, 'keydown', (e) => {
    const list = [...el.menu.querySelectorAll('button')];
    const i = list.indexOf(document.activeElement);
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      const n = list[(i + (e.key === 'ArrowDown' ? 1 : list.length - 1)) % list.length];
      if (n) n.focus();
    } else if (e.key === 'Tab') { e.preventDefault(); closeMenu(); }
  });
  events.listen(document, 'pointerdown', (e) => { if (!el.menu.hidden && !el.menu.contains(e.target)) closeMenu(false); });

  // Timeline: click or drag to seek under the active mode; arrows step.
  let dragging = false, timelinePointer = null;
  cancelTimelineDrag = () => {
    dragging = false;
    if (timelinePointer != null && el.tl.hasPointerCapture(timelinePointer)) el.tl.releasePointerCapture(timelinePointer);
    timelinePointer = null;
  };
  events.listen(el.tl, 'pointerdown', (ev) => {
    dragging = true; timelinePointer = ev.pointerId;
    el.tl.setPointerCapture(ev.pointerId);
    seekTo(tlTickAt(ev), app.mode, { log: false });
  });
  events.listen(el.tl, 'pointermove', (ev) => {
    if (dragging) { const t = tlTickAt(ev); if (t !== app.t) seekTo(t, app.mode, { log: false }); }
    else { const t = tlTickAt(ev, false); if (t !== app.hoverTick) { app.hoverTick = t; dirty('view'); } }
  });
  events.listen(el.tl, 'pointerup', cancelTimelineDrag);
  events.listen(el.tl, 'pointercancel', cancelTimelineDrag);
  events.listen(el.tl, 'pointerleave', () => { app.hoverTick = -1; dirty('view'); });
  events.listen(el.tl, 'keydown', (ev) => {
    if (el.dlg.open) return;
    if ((ev.key === 'Enter' || ev.key === ' ') && !ev.ctrlKey && !ev.metaKey && !ev.altKey && !ev.shiftKey) {
      ev.preventDefault();
      ev.stopPropagation();
      if (!ev.repeat) setTimelineExpanded(true, { persist: true });
      return;
    }
    const step = { ArrowLeft: -1, ArrowRight: 1, ArrowDown: -1, ArrowUp: 1, PageDown: -60, PageUp: 60 }[ev.key];
    if (step) { ev.preventDefault(); if (step === 1) { pause(); stepForward(); } else seekTo(app.t + step, app.mode, { log: false }); }
    else if (ev.key === 'Home') { ev.preventDefault(); seekTo(0, app.mode, { log: false }); }
    else if (ev.key === 'End') { ev.preventDefault(); goEnd(); }
  });

  // Watermark hover text.
  events.listen(el.view, 'pointermove', (ev) => {
    const b = app.wmBox;
    const rect = el.view.getBoundingClientRect();
    const x = ((ev.clientX - rect.left) / rect.width) * W, y = ((ev.clientY - rect.top) / rect.height) * H;
    if (b && x >= b.x && x <= b.x + b.w && y >= b.y && y <= b.y + b.h) tip('tool assisted', ev.clientX, ev.clientY);
    else el.tip.hidden = true;
  });
  events.listen(el.view, 'pointerleave', () => { el.tip.hidden = true; });
  events.listen(window, 'resize', () => { dirty('view', 'roll'); });
  events.listen(window, 'telos:presentation-change', applyLayout);
  events.listen(window, 'telos:dock-ready', registerDockViews);
  events.listen(wideMq, 'change', applyLayout);
  events.listen(reducedMq, 'change', () => dirty('view'));
}

function tip(text, x, y) {
  el.tip.textContent = text;
  el.tip.hidden = false;
  const w = el.tip.offsetWidth;
  el.tip.style.left = Math.max(4, Math.min(x + 14, innerWidth - w - 8)) + 'px';
  el.tip.style.top = Math.max(4, y - 40) + 'px';
}

function announce() {
  clearTimeout(liveTimer);
  liveTimer = setTimeout(() => { el.rollLive.textContent = roll.describe(roll.sel.t1, roll.sel.c); }, 150);
}

function init() {
  roll = new Roll($('roll'), {
    onPaint: (t0, t1, cols, value) => paint(t0, t1, cols, value),
    onSeek: (t) => seekTo(t, app.mode),
    onSelect: () => { announce(); app.selLines = ''; },
    onBrowse: () => setFollowPlayhead(false),
    onContext: (x, y) => openMenu(x, y),
    onHover: (t, c, x, y) => {
      if (t < 0) { el.tip.hidden = true; return; }
      const row = app.model.rows[t];
      const col = COLS[c];
      let text = `${roll.describe(t, c)}`;
      if (col && col.title && c >= FIRST_INPUT) text = col.title + ' ' + text;
      else if (col && col.kind === 'mark' && app.markMap.has(t)) text = `@mark ${app.markMap.get(t).join(', ')}. ` + text;
      else if (row && row.kind !== 'end' && row.src >= 0) text += ` Score line ${app.b.prog.ins[row.src].line}.`;
      tip(text, x, y);
    },
    onEditMouse: (t) => editMouse(t),
    onMark: () => markAt(roll.selRange()[0]),
    onClear: () => clearSelection(false),
    onFind: () => findMark(),
    onBranch: () => branchAt(roll.selRange()[0]),
    onRefuse: (row) => say(el.scoreStatus, row.kind === 'wait' ? '@sync rows are not editable. Change the event or timeout in the score text.' : 'Tick 0 is the boot state. It holds no input.', 'warn'),
  });
  el.score.value = EXAMPLE;
  const main = newRun(EXAMPLE, { budget: 32 });
  app.branches = [main];
  main.thumb = thumbOf(main, 0);
  applyLayout();
  bind();
  registerDockViews();
  syncTierUi();
  el.tier.value = app.tier;
  activate(main, 0);
  centerTarget = host.getTarget();
  roll.select(1, 1, FIRST_INPUT, false);
  say(el.scoreStatus, `Parsed. Recorded ${headOf(main)} ticks.`, 'ok');
  say(el.stageStatus, 'Click the strip or a tick number to move the sample authoring playhead.');
  print('telos console. Type help for the commands. Score lines such as 4 w lmb run at the end of the score.', 'in');
  initialized = true;
}

return {
  start() {
    events.resume();
    if (!initialized) init();
    roll.resume(); keyboardControls.resume();
    centerTarget = host.getTarget();
    registerDockViews();
    dirty('view', 'roll', 'editor', 'branches', 'history', 'memory');
    startTasRendering();
  },
  stop() {
    tasLifecycleGeneration++;
    clearTimeout(app.editTimer); app.editTimer = 0;
    clearTimeout(liveTimer); liveTimer = 0;
    dismissDialog?.(); cancelTimelineDrag();
    stopTasRendering(); events.suspend(); roll?.suspend(); keyboardControls.suspend();
    el.tip.hidden = true; hideThumb();
  },
  refreshKeyboard() { keyboardControls.render(); },
  invalidate() { dirty('view', 'roll', 'editor', 'branches', 'history', 'memory'); },
  invalidatePendingWork() { tasLifecycleGeneration++; dismissDialog?.(); },
  flushEdits() { if (initialized) flushEdit(); },
  pause() { pause(); },
  stopRecording() { toggleRecord(false); },
  clearRecording() {
    app.rec.bits = 0;
    for (const key of el.pad.querySelectorAll('.padk')) key.setAttribute('aria-pressed', 'false');
    app.held = []; app.acc = 0; app.wasmReleaseAfterAck = null;
    app.wasmInputPending = false; app.wasmPendingTick = null;
  },
  releaseInputs(reason) { releaseWasmInput(reason); },
  cancelCapture() { keyboardControls.cancelCapture(); },
  resetWorkspace() { document.body.classList.remove('tas-ui-hidden'); closeMenu(); host.selectTarget('original'); },
};
}
