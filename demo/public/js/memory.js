// Memory panel: RAM-watch style expressions over the per-tick state, a
// sparkline of the last 64 ticks, and a hex view of the canonical state string.

const ENT_IDS = ['pc', 'n1', 'n2', 'h1'];
const SEG = /^(?:\.([A-Za-z_][A-Za-z0-9_]*)|\[(\d{1,4})\])/;

// Compile `pc.hp`, `ents[0].x`, `rng`, `input.w` into a list of steps.
export function compileWatch(src) {
  const s = String(src).trim();
  const m = /^([A-Za-z_][A-Za-z0-9_]*)/.exec(s);
  if (!m || s.length > 80) return { error: 'Start with a name such as pc, ents, rng or input.' };
  const steps = [m[1]];
  let rest = s.slice(m[0].length);
  while (rest.length) {
    const k = SEG.exec(rest);
    if (!k) return { error: `Cannot read '${rest}'. Use .name or [number].` };
    steps.push(k[1] !== undefined ? k[1] : +k[2]);
    rest = rest.slice(k[0].length);
  }
  return { steps };
}

// state: the parsed game state; input: {w: true, ..., mx, my} for the tick.
export function readWatch(w, state, input) {
  if (!w.steps) return undefined;
  let v;
  const [root, ...path] = w.steps;
  if (root === 'input') v = input;
  else if (ENT_IDS.includes(root) && !(root in state)) v = state.ents ? state.ents.find((e) => e.id === root) : undefined;
  else v = state[root];
  for (const p of path) {
    if (v === null || v === undefined) return undefined;
    v = v[p];
  }
  return v;
}

export function fmtWatch(v, expr) {
  if (v === undefined) return 'none';
  if (typeof v === 'number' && /(^|\.)rng$/.test(expr)) return '0x' + (v >>> 0).toString(16).padStart(8, '0');
  if (typeof v === 'boolean') return v ? 'held' : '.';
  if (typeof v === 'string') return v.length > 40 ? JSON.stringify(v.slice(0, 40)) + '…' : JSON.stringify(v);
  if (v && typeof v === 'object') { const j = JSON.stringify(v); return j.length > 40 ? j.slice(0, 40) + '…' : j; }
  return String(v);
}

export function sparkline(canvas, values) {
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  const w = 80, h = 18;
  if (canvas.width !== w * dpr) { canvas.width = w * dpr; canvas.height = h * dpr; }
  const c = canvas.getContext('2d');
  c.setTransform(dpr, 0, 0, dpr, 0, 0);
  c.clearRect(0, 0, w, h);
  const nums = values.map((v) => (typeof v === 'boolean' ? (v ? 1 : 0) : v)).filter((v) => typeof v === 'number' && Number.isFinite(v));
  if (nums.length < 2) return;
  let lo = Math.min(...nums), hi = Math.max(...nums);
  if (hi === lo) { hi += 1; lo -= 1; }
  c.strokeStyle = '#19e3c1';
  c.lineWidth = 1;
  c.beginPath();
  let started = false;
  values.forEach((v, i) => {
    const n = typeof v === 'boolean' ? (v ? 1 : 0) : v;
    if (typeof n !== 'number' || !Number.isFinite(n)) { started = false; return; }
    const x = (i / Math.max(1, values.length - 1)) * (w - 2) + 1;
    const y = h - 2 - ((n - lo) / (hi - lo)) * (h - 4);
    if (started) c.lineTo(x, y); else { c.moveTo(x, y); started = true; }
  });
  c.stroke();
}

// Hex rows of the state string; bytes that differ from prev are lit.
export function hexView(str, prev) {
  const frag = document.createDocumentFragment();
  const hx = (n, w) => n.toString(16).padStart(w, '0');
  for (let off = 0; off < str.length; off += 16) {
    const o = document.createElement('span');
    o.className = 'o';
    o.textContent = hx(off, 4) + '  ';
    frag.append(o);
    let ascii = '';
    for (let i = 0; i < 16; i++) {
      const k = off + i;
      if (k >= str.length) { frag.append('   '); continue; }
      const code = str.charCodeAt(k) & 0xff;
      const changed = prev != null && (k >= prev.length || prev.charCodeAt(k) !== str.charCodeAt(k));
      const s = document.createElement('span');
      s.className = changed ? 'b chg' : 'b';
      s.textContent = hx(code, 2);
      frag.append(s, i === 7 ? '  ' : ' ');
      ascii += code >= 32 && code < 127 ? String.fromCharCode(code) : '.';
    }
    frag.append(' ' + ascii + '\n');
  }
  return frag;
}
