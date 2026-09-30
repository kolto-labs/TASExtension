// Roll model and roll-to-text edits. The score text stays the source of
// truth: every roll edit is a rewrite of score lines, and the roll is rebuilt
// from the compiled program and the recorded ticks.

import { parseScore, noteText, inputOf, sameInput, EMPTY } from './score.js';
import { headOf } from './holo.js';

// One row per tick. kind: boot (tick 0), note, wait (@sync), end (past the
// score). src is the instruction index, off the tick's offset in its note.
// played rows come from the recording; the rest are projected from the score.
export function buildRows(b, tail = 90) {
  const ins = b.prog.ins, recs = b.recs, head = headOf(b);
  const rows = [{ t: 0, kind: 'boot', src: -1, off: 0, played: true }];
  for (let t = 1; t <= head; t++) {
    const r = recs[t], s = r.src;
    if (s < 0 || !ins[s]) rows.push({ t, kind: 'end', src: -1, off: 0, played: true });
    else if (ins[s].op === 'sync') rows.push({ t, kind: 'wait', src: s, off: Math.max(0, r.c.used - 1), played: true });
    else rows.push({ t, kind: 'note', src: s, off: r.c.ip === s ? r.c.used - 1 : ins[s].count - 1, played: true });
  }
  let t = head + 1;
  const c = recs[head].c;
  if (!c.end) {
    let ip = c.ip, used = c.used;
    if (c.wait && ins[ip] && ins[ip].op === 'sync') { ip++; used = 0; }
    for (; ip < ins.length; ip++, used = 0) {
      const it = ins[ip];
      if (it.op === 'note') for (let k = used; k < it.count; k++) rows.push({ t: t++, kind: 'note', src: ip, off: k, played: false });
      else if (it.op === 'sync') rows.push({ t: t++, kind: 'wait', src: ip, off: 0, played: false });
    }
  }
  let end = rows.findIndex((r) => r.kind === 'end');
  if (end < 0) end = t;
  for (let k = 0; k < tail; k++) rows.push({ t: t++, kind: 'end', src: -1, off: 0, played: false });
  return { rows, end };
}

// Marks and segment chips, placed on rows.
export function rowMarks(prog, model) {
  const { rows, end } = model;
  const len = prog.ins.length;
  const first = (at) => {
    for (const r of rows) if (r.t > 0 && (r.kind === 'end' || r.src >= at)) return r.t;
    return -1;
  };
  return prog.marks.map((m) => ({ ...m, tick: m.at >= len ? Math.max(1, end - 1) : first(m.at) }));
}

export function rowSegments(prog, model) {
  const out = [];
  prog.ins.forEach((it, i) => {
    if (it.op !== 'seg') return;
    const r = model.rows.find((x) => x.t > 0 && (x.kind === 'end' || x.src > i));
    if (r) out.push({ tick: r.t, name: it.name, hz: it.hz || prog.hz0 });
  });
  return out;
}

export function rowInput(prog, row) {
  if (!row || row.kind !== 'note') return EMPTY;
  return inputOf(prog.ins[row.src]);
}

function splitLine(raw) {
  const indent = /^\s*/.exec(raw)[0];
  const h = raw.indexOf('#');
  return { indent, comment: h >= 0 ? raw.slice(h).trim() : '' };
}

function rle(vals) {
  const out = [];
  for (const v of vals) {
    const last = out[out.length - 1];
    if (last && sameInput(last.v, v) && sameAl(last.v, v)) last.n++;
    else out.push({ n: 1, v });
  }
  return out;
}
const sameAl = (a, b) => JSON.stringify(a.al || null) === JSON.stringify(b.al || null);

const norm = (v) => ({ bits: v.bits | 0, xbits: v.xbits | 0, mouse: v.mouse ? [v.mouse[0] | 0, v.mouse[1] | 0] : null, al: v.al && Object.keys(v.al).length ? v.al : null });

// Repeat blocks that hold any of these instructions.
function repeatsFor(prog, srcs) {
  const lines = new Set();
  for (const s of srcs) { const it = prog.ins[s]; if (it && it.rep) lines.add(it.rl); }
  return prog.repeats.filter((r) => lines.has(r.line));
}

// Expand the given @repeat blocks into literal lines. The flattened
// instruction list stays the same, so row src indices stay valid.
export function unrollRepeats(text, repeats) {
  const lines = text.split('\n');
  const sorted = [...repeats].sort((a, b) => b.line - a.line);
  for (const r of sorted) {
    const indent = /^\s*/.exec(lines[r.line - 1])[0];
    const expand = (from, to) => {
      const out = [];
      for (let i = from; i < to; i++) {
        const code = lines[i].split('#')[0].trim();
        if (!code) { if (lines[i].trim()) out.push(indent + lines[i].trim()); continue; }
        if (code.startsWith('@repeat')) {
          const n = +code.split(/\s+/)[1];
          let depth = 1, j = i + 1;
          for (; j < to; j++) {
            const c = lines[j].split('#')[0].trim();
            if (c.startsWith('@repeat')) depth++;
            else if (c === '@end' && --depth === 0) break;
          }
          const inner = expand(i + 1, j);
          for (let k = 0; k < n; k++) out.push(...inner);
          i = j;
          continue;
        }
        out.push(indent + lines[i].trim());
      }
      return out;
    };
    const body = expand(r.line, r.end - 1);
    const out = [];
    for (let k = 0; k < r.n; k++) out.push(...body);
    lines.splice(r.line - 1, r.end - r.line + 1, ...out);
  }
  return lines.join('\n');
}

function withNewline(lines) {
  while (lines.length && lines[lines.length - 1] === '') lines.pop();
  return lines.join('\n') + '\n';
}

// Append inputs after the end of the score. vals[k] is the input for tick
// end + k (k from 0). Merges into the last note when it matches.
function appendTail(lines, prog, vals) {
  let k0 = vals.findIndex((v) => v && (v.bits || v.xbits || v.mouse));
  if (k0 < 0) return;
  const seq = vals.map((v) => (v ? norm(v) : norm(EMPTY)));
  let last = vals.length - 1;
  while (last >= 0 && !(seq[last].bits || seq[last].xbits || seq[last].mouse)) last--;
  const pieces = rle(seq.slice(0, last + 1));
  while (lines.length && lines[lines.length - 1] === '') lines.pop();
  const lastIns = prog.ins[prog.ins.length - 1];
  const lastLine = lastIns ? lastIns.line - 1 : -1;
  const tailIsNote = lastIns && lastIns.op === 'note' && !lastIns.rep && lastLine === lastCodeLine(lines);
  if (tailIsNote && pieces.length && sameInput(inputOf(lastIns), pieces[0].v) && sameAl(inputOf(lastIns), pieces[0].v)) {
    const { indent, comment } = splitLine(lines[lastLine]);
    lines[lastLine] = indent + noteText(lastIns.count + pieces[0].n, inputOf(lastIns)) + (comment ? '  ' + comment : '');
    pieces.shift();
  }
  for (const p of pieces) lines.push(noteText(p.n, p.v));
}

function lastCodeLine(lines) {
  for (let i = lines.length - 1; i >= 0; i--) if (lines[i].split('#')[0].trim()) return i;
  return -1;
}

// Set inputs on ticks. changes: Map tick -> function(input) -> input.
// Returns {text, skipped} or {needUnroll: [repeat blocks]}.
export function editTicks(text, prog, model, changes) {
  const { rows, end } = model;
  const byIns = new Map();
  const tail = [];
  let skipped = 0;
  for (const [t, fn] of changes) {
    const row = rows[t];
    if (!row) continue;
    if (row.kind === 'note') {
      if (!byIns.has(row.src)) byIns.set(row.src, new Map());
      byIns.get(row.src).set(row.off, fn);
    } else if (row.kind === 'end') tail[t - end] = fn(EMPTY);
    else skipped++;
  }
  const reps = repeatsFor(prog, byIns.keys());
  if (reps.length) return { needUnroll: reps };
  const lines = text.split('\n');
  const items = [...byIns.entries()].sort((a, b) => prog.ins[b[0]].line - prog.ins[a[0]].line);
  let changed = false;
  for (const [src, offs] of items) {
    const it = prog.ins[src];
    const base = inputOf(it);
    const vals = [];
    for (let k = 0; k < it.count; k++) vals.push(offs.has(k) ? norm(offs.get(k)(base)) : base);
    const pieces = rle(vals);
    if (pieces.length === 1 && sameInput(pieces[0].v, base) && sameAl(pieces[0].v, base)) continue;
    changed = true;
    // Merge the ends into identical neighbour notes on the lines right next
    // to this one, so a run of single-tick edits stays one line.
    const near = (k, v) => {
      const x = prog.ins[k];
      return x && x.op === 'note' && !x.rep && !byIns.has(k) && Math.abs(x.line - it.line) === 1 &&
        sameInput(inputOf(x), v) && sameAl(inputOf(x), v) ? x : null;
    };
    const prev = pieces.length ? near(src - 1, pieces[0].v) : null;
    let prevCount = prev ? prev.count + pieces.shift().n : 0;
    if (prev && !pieces.length) {
      const nx = near(src + 1, inputOf(prev));
      if (nx && !splitLine(lines[nx.line - 1]).comment) { prevCount += nx.count; lines.splice(nx.line - 1, 1); }
    } else if (pieces.length) {
      const nx = near(src + 1, pieces[pieces.length - 1].v);
      if (nx) {
        const { indent: ni, comment: nc } = splitLine(lines[nx.line - 1]);
        lines[nx.line - 1] = ni + noteText(nx.count + pieces.pop().n, inputOf(nx)) + (nc ? '  ' + nc : '');
      }
    }
    const { indent, comment } = splitLine(lines[it.line - 1]);
    const out = pieces.map((p, i) => indent + noteText(p.n, p.v) + (i === 0 && comment ? '  ' + comment : ''));
    let carry = '';
    if (!out.length && comment) {
      // The whole line merged into the one above: keep its comment there when
      // that line has none, else on its own line.
      if (prev && !splitLine(lines[prev.line - 1]).comment) carry = comment;
      else out.push(indent + comment);
    }
    lines.splice(it.line - 1, 1, ...out);
    if (prev) {
      const { indent: pi, comment: pc } = splitLine(lines[prev.line - 1]);
      const cm = pc || carry;
      lines[prev.line - 1] = pi + noteText(prevCount, inputOf(prev)) + (cm ? '  ' + cm : '');
    }
  }
  if (tail.length) {
    const before = lines.length;
    appendTail(lines, prog, Array.from(tail));
    if (lines.length !== before || tail.some(Boolean)) changed = true;
  }
  if (!changed) return { text, skipped, same: true };
  return { text: withNewline(lines), skipped };
}

// Insert n empty ticks before tick t, shifting later input down.
export function insertTicks(text, prog, model, t, n) {
  const row = model.rows[t];
  if (!row || row.kind === 'end' || row.kind === 'boot') return { text, same: true, note: 'Past the end there is nothing to shift.' };
  const it = prog.ins[row.src];
  if (it.rep) return { needUnroll: repeatsFor(prog, [row.src]) };
  const lines = text.split('\n');
  const blank = noteText(n, EMPTY);
  const { indent, comment } = splitLine(lines[it.line - 1]);
  if (row.kind === 'wait' || row.off === 0) lines.splice(it.line - 1, 0, indent + blank);
  else {
    const v = inputOf(it);
    lines.splice(it.line - 1, 1,
      indent + noteText(row.off, v) + (comment ? '  ' + comment : ''),
      indent + blank,
      indent + noteText(it.count - row.off, v));
  }
  return { text: withNewline(lines) };
}

// Remove note ticks t0..t1, shifting later input up. @sync rows stay.
export function deleteTicks(text, prog, model, t0, t1) {
  const byIns = new Map();
  let skipped = 0;
  for (let t = t0; t <= t1; t++) {
    const row = model.rows[t];
    if (!row) continue;
    if (row.kind !== 'note') { if (row.kind === 'wait') skipped++; continue; }
    byIns.set(row.src, (byIns.get(row.src) || 0) + 1);
  }
  if (!byIns.size) return { text, same: true, skipped };
  const reps = repeatsFor(prog, byIns.keys());
  if (reps.length) return { needUnroll: reps };
  const lines = text.split('\n');
  const items = [...byIns.entries()].sort((a, b) => prog.ins[b[0]].line - prog.ins[a[0]].line);
  for (const [src, n] of items) {
    const it = prog.ins[src];
    const { indent, comment } = splitLine(lines[it.line - 1]);
    const left = it.count - n;
    if (left > 0) lines[it.line - 1] = indent + noteText(left, inputOf(it)) + (comment ? '  ' + comment : '');
    else if (comment) lines[it.line - 1] = indent + comment;
    else lines.splice(it.line - 1, 1);
  }
  return { text: withNewline(lines), skipped };
}

// Split the note at tick t into two lines without changing any input.
export function splitAt(text, prog, model, t) {
  const row = model.rows[t];
  if (!row || row.kind !== 'note') return { text, same: true, note: 'Only note ticks can be split.' };
  const it = prog.ins[row.src];
  if (row.off === 0) return { text, same: true, note: 'The tick already starts a line.' };
  if (it.rep) return { needUnroll: repeatsFor(prog, [row.src]) };
  const lines = text.split('\n');
  const { indent, comment } = splitLine(lines[it.line - 1]);
  const v = inputOf(it);
  lines.splice(it.line - 1, 1, indent + noteText(row.off, v) + (comment ? '  ' + comment : ''), indent + noteText(it.count - row.off, v));
  return { text: withNewline(lines) };
}

// Put `@mark name` so the mark is reached at tick t.
export function addMark(text, prog, model, t, name) {
  const row = model.rows[t];
  const lines = text.split('\n');
  const markLine = `@mark ${name}`;
  if (!row || row.kind === 'end') {
    while (lines.length && lines[lines.length - 1] === '') lines.pop();
    const gap = row ? t - model.end : 0;
    if (gap > 0) lines.push(noteText(gap, EMPTY));
    lines.push(markLine);
    return { text: withNewline(lines) };
  }
  if (row.kind === 'boot') {
    const at = lines[0].startsWith('#!score') ? 1 : 0;
    lines.splice(at, 0, markLine);
    return { text: withNewline(lines) };
  }
  const it = prog.ins[row.src];
  if (it.rep) return { needUnroll: repeatsFor(prog, [row.src]) };
  if (row.kind === 'wait' || row.off === 0) {
    lines.splice(it.line - 1, 0, markLine);
    return { text: withNewline(lines) };
  }
  const { indent, comment } = splitLine(lines[it.line - 1]);
  const v = inputOf(it);
  lines.splice(it.line - 1, 1, indent + noteText(row.off, v) + (comment ? '  ' + comment : ''), markLine, indent + noteText(it.count - row.off, v));
  return { text: withNewline(lines) };
}

// Ticks t0..t1 as score lines (run-length notes; @sync rows as comments).
export function ticksAsScore(prog, model, t0, t1) {
  const out = [];
  let run = null;
  const flush = () => { if (run) out.push(noteText(run.n, run.v)); run = null; };
  for (let t = Math.max(1, t0); t <= t1; t++) {
    const row = model.rows[t];
    if (!row) break;
    if (row.kind === 'wait') {
      flush();
      const it = prog.ins[row.src];
      if (row.off === 0 || t === Math.max(1, t0)) out.push(`@sync ${it.ev} timeout=${it.timeout}`);
      continue;
    }
    const v = rowInput(prog, row);
    if (run && sameInput(run.v, v) && sameAl(run.v, v)) run.n++;
    else { flush(); run = { n: 1, v }; }
  }
  flush();
  return out.join('\n') + '\n';
}

// Check that a text parses. Used before applying any rewrite.
export function parses(text) {
  return parseScore(text).errors;
}
