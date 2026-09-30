// Score parser for the browser prototype. A subset of the real score format:
// notes (`count keys`), comments, a header line, @sync, @mark, @segment,
// @repeat/@end. Errors are collected per line so the editor can show them.

import { BIT, EVENTS, KEYS } from './sim.js';

const LABEL = /^[A-Za-z0-9_][A-Za-z0-9_.-]*$/;
const INT = /^[0-9]+$/;
const MAX_INS = 50000;

export const HZ_MIN = 10, HZ_MAX = 240;

function intOpt(tok, key, min, max, errs, line) {
  const v = tok.slice(key.length + 1);
  if (!INT.test(v) || +v < min || +v > max) {
    errs.push({ line, msg: `${key} must be a whole number from ${min} to ${max}` });
    return null;
  }
  return +v;
}

export function parseScore(text) {
  const lines = text.replace(/\r\n?/g, '\n').split('\n');
  const errors = [];
  const header = {};
  let hz0 = 60;
  const root = [];
  const stack = [{ body: root, line: 0, n: 1 }];
  const marks = [];

  for (let i = 0; i < lines.length; i++) {
    const ln = i + 1;
    const raw = lines[i];
    if (i === 0 && raw.trimStart().startsWith('#!score')) {
      const toks = raw.trim().split(/\s+/).slice(1);
      const hash = toks.findIndex((t) => t.startsWith('#'));
      const used = hash >= 0 ? toks.slice(0, hash) : toks;
      if (used[0] !== 'v1') errors.push({ line: ln, msg: "the header starts with '#!score v1'" });
      for (const t of used.slice(1)) {
        const [k, v] = t.split('=');
        if (!v) { errors.push({ line: ln, msg: `header entries are key=value, got '${t}'` }); continue; }
        header[k] = v;
        if (k === 'hz') { const n = intOpt(t, 'hz', HZ_MIN, HZ_MAX, errors, ln); if (n) hz0 = n; }
      }
      continue;
    }
    const code = raw.split('#')[0].trim();
    if (!code) continue;
    const toks = code.split(/\s+/);
    const head = toks[0];
    const body = stack[stack.length - 1].body;
    const inRep = stack.length > 1;

    if (INT.test(head)) {
      const count = +head;
      if (count < 1 || count > 100000) { errors.push({ line: ln, msg: 'tick count must be 1 to 100000' }); continue; }
      const keys = toks.slice(1);
      if (!keys.length) { errors.push({ line: ln, msg: "a note needs keys, or '.' for no input" }); continue; }
      let bits = 0, bad = false;
      if (keys.length === 1 && keys[0] === '.') bits = 0;
      else {
        for (const k of keys) {
          if (k === '.') { errors.push({ line: ln, msg: "'.' means no input, so it stands alone" }); bad = true; break; }
          if (!(k in BIT)) { errors.push({ line: ln, msg: `unknown key '${k}'. This scene reads ${KEYS.join(' ')}` }); bad = true; break; }
          if (bits & BIT[k]) { errors.push({ line: ln, msg: `key '${k}' is listed twice` }); bad = true; break; }
          bits |= BIT[k];
        }
      }
      if (!bad) body.push({ op: 'note', count, bits, line: ln, rep: inRep });
      continue;
    }
    if (head === '@sync') {
      const ev = toks[1];
      if (!EVENTS.includes(ev)) { errors.push({ line: ln, msg: `@sync needs an event: ${EVENTS.join(', ')}` }); continue; }
      let timeout = 600, ok = true;
      for (const t of toks.slice(2)) {
        if (t.startsWith('timeout=')) { const n = intOpt(t, 'timeout', 1, 100000, errors, ln); if (n === null) ok = false; else timeout = n; }
        else { errors.push({ line: ln, msg: `unknown @sync option '${t}'` }); ok = false; }
      }
      if (ok) body.push({ op: 'sync', ev, timeout, line: ln, rep: inRep });
      continue;
    }
    if (head === '@mark') {
      if (inRep) { errors.push({ line: ln, msg: 'put @mark outside @repeat' }); continue; }
      if (!toks[1] || !LABEL.test(toks[1]) || toks.length > 2) { errors.push({ line: ln, msg: '@mark takes one name' }); continue; }
      marks.push({ name: toks[1], line: ln, at: root.length });
      continue;
    }
    if (head === '@segment') {
      if (!toks[1] || !LABEL.test(toks[1])) { errors.push({ line: ln, msg: '@segment needs a name' }); continue; }
      let hz = 0, ok = true;
      for (const t of toks.slice(2)) {
        if (t.startsWith('hz=')) { const n = intOpt(t, 'hz', HZ_MIN, HZ_MAX, errors, ln); if (n === null) ok = false; else hz = n; }
        else { errors.push({ line: ln, msg: `unknown @segment option '${t}'` }); ok = false; }
      }
      if (ok) body.push({ op: 'seg', name: toks[1], hz, line: ln, rep: inRep });
      continue;
    }
    if (head === '@repeat') {
      const n = toks[1];
      if (!n || !INT.test(n) || +n < 1 || +n > 1000 || toks.length > 2) { errors.push({ line: ln, msg: '@repeat takes a count from 1 to 1000' }); continue; }
      const rep = { op: 'repeat', n: +n, body: [], line: ln };
      body.push(rep);
      stack.push({ body: rep.body, line: ln, n: +n });
      continue;
    }
    if (head === '@end') {
      if (stack.length === 1) errors.push({ line: ln, msg: '@end without a matching @repeat' });
      else stack.pop();
      continue;
    }
    if (head.startsWith('@')) {
      errors.push({ line: ln, msg: `unknown directive '${head}'. This prototype reads @sync @mark @segment @repeat @end` });
      continue;
    }
    errors.push({ line: ln, msg: `expected a note (tick count first) or a directive, got '${head}'` });
  }
  if (stack.length > 1) errors.push({ line: stack[stack.length - 1].line, msg: '@repeat without a matching @end' });

  const ins = [];
  const markAt = [];
  let tooBig = false;
  const flat = (list, isRoot) => {
    for (let k = 0; k < list.length; k++) {
      if (isRoot) markAt[k] = ins.length;
      const it = list[k];
      if (it.op === 'repeat') { for (let r = 0; r < it.n && !tooBig; r++) flat(it.body, false); }
      else if (ins.length >= MAX_INS) tooBig = true;
      else ins.push(it);
    }
    if (isRoot) markAt[list.length] = ins.length;
  };
  flat(root, true);
  if (tooBig) errors.push({ line: 1, msg: `the score expands past ${MAX_INS} steps` });
  const prog = { hz0, ins, marks: marks.map((m) => ({ name: m.name, line: m.line, at: markAt[m.at] })), header };
  return { prog, errors, lineCount: lines.length };
}

export function sameIns(a, b, hz0) {
  if (!a || !b || a.op !== b.op) return false;
  if (a.op === 'note') return a.count === b.count && a.bits === b.bits;
  if (a.op === 'sync') return a.ev === b.ev && a.timeout === b.timeout;
  if (a.op === 'seg') return (a.hz || hz0) === (b.hz || hz0);
  return false;
}

export const EXAMPLE = `#!score v1 game=standin hz=60
# Stand-in scene, not KotOR. One note per line: tick count, then every
# input held for those ticks. A lone . holds nothing.
# Keys: w a s d move. rmb runs. lmb opens doors and attacks.
@mark start
46  d s rmb            # run toward the door
32  d rmb
1   lmb                # press: open the door
1   .                  # release
@sync door.open        # wait until it has swung open
@mark door_open
180 d rmb              # run the corridor into the load zone
@sync load.done        # the load takes 0.9 s of virtual time
@mark area2
@segment fight hz=120  # finer steps for the fight
104 d s rmb            # drop below the wall
228 d rmb              # the thug comes out to meet you
@mark engage
480 lmb                # hold to attack while in reach
@segment wrap          # back to the header rate
30  .
@mark done
`;
