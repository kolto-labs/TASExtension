// Score parser for the browser prototype. A subset of the real score format:
// notes (`count inputs`), comments, a header line, @sync, @mark, @segment,
// @repeat/@end, @alias with $name inputs, m@x,y positions, and mouse buttons.
// Errors are collected per line so the editor can show them.

import { BIT, EVENTS, KEYS } from './sim.js';

const LABEL = /^[A-Za-z0-9_][A-Za-z0-9_.-]*$/;
const ANAME = /^[A-Za-z_][A-Za-z0-9_]*$/;
const INT = /^[0-9]+$/;
const MOUSE = /^m@([0-9]{1,4}),([0-9]{1,4})$/;
const MAX_INS = 50000;

export const HZ_MIN = 10, HZ_MAX = 240;

// Portable score semantics, shared with native input_edits.py. This is a
// representation contract, not an advertisement of browser dispatch support.
export const MOUSE_INPUT_CONTRACT = Object.freeze({
  schema: 'telos-mouse-input/1',
  wheel_unit: 'detent', wheel_phase: 'first-frame',
  wheel_min: -17895697, wheel_max: 17895697,
  absolute_unit: 'guest-pixel', relative_unit: 'guest-pixel',
  relative_phase: 'every-frame',
});

// Reject fields the browser would otherwise discard during an edit or copy.
// Never reinterpret a wheel detent as a DOM pixel delta or a held button.
export function requireBrowserMouseInput(inp) {
  if (inp.wheel !== undefined && inp.wheel !== 0)
    throw new Error('Wheel detents require a wheel-capable runtime and input editor.');
  if (inp.mouse_rel != null || inp.mouseRel != null || inp.mouse_abs != null)
    throw new Error('Native mouse coordinates require an explicit input adapter.');
  if (inp.mouse != null && (!Array.isArray(inp.mouse) || inp.mouse.length !== 2 ||
      inp.mouse.some(v => !Number.isSafeInteger(v) || v < 0 || v > 9999)))
    throw new Error('Mouse positions must be two whole guest-pixel coordinates from 0 to 9999.');
}

// Keys the roll shows but the stand-in scene does not read. They reach the
// score and the roll, never the sim: only `bits` is passed to step().
export const XKEYS = ['q', 'e', 'space', 'tab', 'esc', '1', '2', '3', '4', '5', '6', '7', '8', '9', 'enter', 'up', 'down', 'left', 'right'];
export const BUTTONS = ['lmb', 'rmb'];
// `xbits` stores score lanes that are not part of the sample sim's four-key
// movement mask. Mouse buttons share this compact, serializable lane mask;
// they are still emitted separately as mouse buttons by the live-game bridge.
export const XBIT = Object.fromEntries([...XKEYS, ...BUTTONS].map((k, i) => [k, 1 << i]));
export const KEY_ORDER = ['w', 'a', 's', 'd', ...XKEYS];
const SYN = { escape: 'esc' };

export const isKey = (k) => k in BIT || k in XBIT;
export const heldIn = (inp, k) => (k in BIT ? (inp.bits & BIT[k]) !== 0 : (inp.xbits & XBIT[k]) !== 0);

export function setKey(inp, k, on) {
  requireBrowserMouseInput(inp);
  const out = { bits: inp.bits, xbits: inp.xbits, mouse: inp.mouse, al: inp.al };
  if (k in BIT) out.bits = on ? out.bits | BIT[k] : out.bits & ~BIT[k];
  else out.xbits = on ? out.xbits | XBIT[k] : out.xbits & ~XBIT[k];
  if (!on && out.al && out.al[k]) { out.al = { ...out.al }; delete out.al[k]; }
  return out;
}

export const EMPTY = Object.freeze({ bits: 0, xbits: 0, mouse: null, al: null });
export const inputOf = (it) => {
  requireBrowserMouseInput(it);
  return { bits: it.bits, xbits: it.xbits || 0, mouse: it.mouse || null, al: it.al || null };
};

export function sameInput(a, b) {
  requireBrowserMouseInput(a);
  requireBrowserMouseInput(b);
  if (a.bits !== b.bits || (a.xbits || 0) !== (b.xbits || 0)) return false;
  const ma = a.mouse, mb = b.mouse;
  if (!ma || !mb) return !ma && !mb;
  return ma[0] === mb[0] && ma[1] === mb[1];
}

// Tokens for one held set, in roll order: keys, mouse, buttons, then $aliases.
export function inputTokens(inp) {
  requireBrowserMouseInput(inp);
  const toks = [], als = [];
  const push = (k) => { const a = inp.al && inp.al[k]; if (a) als.push('$' + a); else toks.push(k); };
  for (const k of KEY_ORDER) if (heldIn(inp, k)) push(k);
  if (inp.mouse) toks.push(`m@${inp.mouse[0]},${inp.mouse[1]}`);
  for (const k of BUTTONS) if (heldIn(inp, k)) push(k);
  const all = toks.concat(als);
  return all.length ? all.join(' ') : '.';
}

export function noteText(count, inp) {
  const c = String(count);
  return (c.length >= 4 ? c + ' ' : c.padEnd(4)) + inputTokens(inp);
}

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
  const aliases = {};
  const repeats = [];

  for (let i = 0; i < lines.length; i++) {
    const ln = i + 1;
    const raw = lines[i];
    if (i === 0 && raw.trimStart().startsWith('#!score')) {
      const toks = raw.trim().split(/\s+/);
      const hash = toks.findIndex((t, index) => index >= 2 && t.startsWith('#'));
      const used = hash >= 0 ? toks.slice(0, hash) : toks;
      if (used[0] !== '#!score' || used[1] !== 'v1') {
        errors.push({ line: ln, msg: "the header starts with '#!score v1'" });
        break;
      }
      for (const t of used.slice(2)) {
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
    const rl = inRep ? stack[1].line : 0;

    if (INT.test(head)) {
      const count = +head;
      if (count < 1 || count > 100000) { errors.push({ line: ln, msg: 'tick count must be 1 to 100000' }); continue; }
      const keys = toks.slice(1);
      if (!keys.length) { errors.push({ line: ln, msg: "a note needs keys, or '.' for no input" }); continue; }
      let bits = 0, xbits = 0, mouse = null, al = null, bad = false;
      if (!(keys.length === 1 && keys[0] === '.')) {
        for (const tok of keys) {
          if (tok === '.') { errors.push({ line: ln, msg: "'.' means no input, so it stands alone" }); bad = true; break; }
          if (/^wh[+-]/.test(tok) || /^md/.test(tok)) {
            errors.push({ line: ln, msg: 'wheel detents and relative mouse inputs need a capable runtime; this editor cannot execute them' });
            bad = true; break;
          }
          const mm = MOUSE.exec(tok);
          if (mm) {
            if (mouse) { errors.push({ line: ln, msg: 'a note holds one mouse position' }); bad = true; break; }
            mouse = [+mm[1], +mm[2]];
            continue;
          }
          let k = tok, alias = '';
          if (tok[0] === '$') {
            alias = tok.slice(1);
            if (!(alias in aliases)) { errors.push({ line: ln, msg: `undefined alias '${tok}'. Define it first: @alias ${alias} = key` }); bad = true; break; }
            k = aliases[alias];
          } else k = SYN[tok] || tok;
          if (!isKey(k)) {
            errors.push({ line: ln, msg: `unknown input '${tok}'. The scene reads ${KEYS.join(' ')}. The score also accepts ${XKEYS.join(' ')}, ${BUTTONS.join(' ')}, and m@x,y` });
            bad = true; break;
          }
          if (k in BIT) {
            if (bits & BIT[k]) { errors.push({ line: ln, msg: `key '${k}' is listed twice` }); bad = true; break; }
            bits |= BIT[k];
          } else {
            if (xbits & XBIT[k]) { errors.push({ line: ln, msg: `key '${k}' is listed twice` }); bad = true; break; }
            xbits |= XBIT[k];
          }
          if (alias) { al = al || {}; al[k] = alias; }
        }
      }
      if (!bad) body.push({ op: 'note', count, bits, xbits, mouse, al, line: ln, rep: inRep, rl });
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
      if (ok) body.push({ op: 'sync', ev, timeout, line: ln, rep: inRep, rl });
      continue;
    }
    if (head === '@mark') {
      if (inRep) { errors.push({ line: ln, msg: 'put @mark outside @repeat' }); continue; }
      if (!toks[1] || !LABEL.test(toks[1]) || toks.length > 2) { errors.push({ line: ln, msg: '@mark takes one name' }); continue; }
      marks.push({ name: toks[1], line: ln, at: root.length });
      continue;
    }
    if (head === '@alias') {
      if (inRep) { errors.push({ line: ln, msg: 'put @alias outside @repeat' }); continue; }
      const m = /^@alias\s+([^=\s]+)\s*=\s*(\S+)$/.exec(code);
      if (!m || !ANAME.test(m[1])) { errors.push({ line: ln, msg: '@alias takes a name and a key: @alias name = key' }); continue; }
      let v = m[2];
      if (v[0] === '$') {
        if (!(v.slice(1) in aliases)) { errors.push({ line: ln, msg: `undefined alias '${v}'` }); continue; }
        v = aliases[v.slice(1)];
      } else v = SYN[v] || v;
      if (!isKey(v)) { errors.push({ line: ln, msg: `the score editor has no column for '${m[2]}'. Use one of ${KEYS.join(' ')} ${XKEYS.join(' ')} ${BUTTONS.join(' ')}` }); continue; }
      if (aliases[m[1]] && aliases[m[1]] !== v) { errors.push({ line: ln, msg: `alias $${m[1]} is already ${aliases[m[1]]} and cannot become ${v}` }); continue; }
      aliases[m[1]] = v;
      continue;
    }
    if (head === '@segment') {
      if (!toks[1] || !LABEL.test(toks[1])) { errors.push({ line: ln, msg: '@segment needs a name' }); continue; }
      let hz = 0, pace = '', ok = true;
      for (const t of toks.slice(2)) {
        if (t.startsWith('hz=')) { const n = intOpt(t, 'hz', HZ_MIN, HZ_MAX, errors, ln); if (n === null) ok = false; else hz = n; }
        // The sample's normal playback follows hz in wall time (with the
        // explicit transport speed multiplier). Preserve this canonical mode.
        else if (t === 'pace=realtime') pace = 'realtime';
        else { errors.push({ line: ln, msg: `unknown @segment option '${t}'` }); ok = false; }
      }
      if (ok) body.push({ op: 'seg', name: toks[1], hz, pace, line: ln, rep: inRep, rl });
      continue;
    }
    if (head === '@repeat') {
      const n = toks[1];
      if (!n || !INT.test(n) || +n < 1 || +n > 1000 || toks.length > 2) { errors.push({ line: ln, msg: '@repeat takes a count from 1 to 1000' }); continue; }
      const rep = { op: 'repeat', n: +n, body: [], line: ln };
      body.push(rep);
      stack.push({ body: rep.body, line: ln, n: +n });
      repeats.push({ line: ln, end: 0, n: +n, depth: stack.length - 1 });
      continue;
    }
    if (head === '@end') {
      if (stack.length === 1) errors.push({ line: ln, msg: '@end without a matching @repeat' });
      else {
        const top = stack.pop();
        const r = repeats.find((x) => x.line === top.line);
        if (r) r.end = ln;
      }
      continue;
    }
    if (head.startsWith('@')) {
      errors.push({ line: ln, msg: `unknown directive '${head}'. This score editor reads @sync @mark @segment @repeat @end @alias` });
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
  const prog = {
    hz0, ins, header, aliases, repeats: repeats.filter((r) => r.depth === 1),
    marks: marks.map((m) => ({ name: m.name, line: m.line, at: markAt[m.at] })),
  };
  return { prog, errors, lineCount: lines.length };
}

export function sameIns(a, b, hz0) {
  if (!a || !b || a.op !== b.op) return false;
  if (a.op === 'note') return a.count === b.count && sameInput(a, b);
  if (a.op === 'sync') return a.ev === b.ev && a.timeout === b.timeout;
  if (a.op === 'seg') return (a.hz || hz0) === (b.hz || hz0);
  return false;
}

export const EXAMPLE = `#!score v1 game=standin hz=60
# Deterministic sample scene. One note per line: tick count, then every
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
