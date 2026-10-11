import { ListenerScope } from '../packages/tas-ui/listener-scope.js';
// Piano roll: one row per tick, one column per input. Drawn on a canvas with
// only the visible rows painted. The roll never edits anything itself: it
// reports paints, seeks and menu requests to the page through hooks.

import { BIT } from './sim.js';
import { heldIn } from './score.js';
import { rowInput } from './edit.js';
import { flagsOf, F, REASONS, reasonOf } from './telos.js';

export const ROW = 18;
const MONO = 'ui-monospace, "SFMono-Regular", Menlo, Consolas, "Liberation Mono", monospace';
const P = {
  void: '#05070a', panel: '#0b1016', kolto: '#19e3c1', bacta: '#5ad8ff', phosphor: '#7dff9a',
  amber: '#ffb000', magenta: '#ff4fd8', ink: '#d7e3ea', dim: '#7f93a0',
};
const tint = (hex, a) => {
  const n = parseInt(hex.slice(1), 16);
  return `rgba(${n >> 16}, ${(n >> 8) & 255}, ${n & 255}, ${a})`;
};

// Bound actions from KPTASInterface/lib/k1-keymap.score, and what the
// stand-in does with the key.
const K1 = {
  w: 'Move Forward', a: 'Pan Camera Left', s: 'Move Backwards', d: 'Pan Camera Right',
  q: 'Select Previous (Previous Menu in GUIs)', e: 'Select Next (Next Menu in GUIs)',
  space: 'Pause Game', tab: 'Change Leader', esc: 'Game Menu',
  enter: 'Enter key', up: 'Up arrow key', down: 'Down arrow key',
  left: 'Left arrow key', right: 'Right arrow key',
  1: 'Activate Left Action, dialog reply 1', 2: 'Activate Middle Action, dialog reply 2',
  3: 'Activate Right Action, dialog reply 3', 4: 'Activate Friendly Power, dialog reply 4',
  5: 'Activate Medical Item, dialog reply 5', 6: 'Activate Non-medical Item, dialog reply 6',
  7: 'Activate Mine, dialog reply 7', 8: 'Dialog reply 8', 9: 'Dialog reply 9',
  lmb: 'Left mouse button', rmb: 'Right mouse button', mx: 'Mouse x', my: 'Mouse y',
};
const STANDIN = { w: 'moves up', a: 'moves left', s: 'moves down', d: 'moves right', lmb: 'opens the door and attacks', rmb: 'runs' };

function makeCols() {
  const cols = [];
  let x = 0;
  const add = (id, w, kind, label, gap = 0) => { x += gap; cols.push({ id, x, w, kind, label: label || id }); x += w; };
  add('tick', 84, 'tick', 'tick');
  add('mk', 20, 'mark', 'mk');
  add('fl', 26, 'flags', 'fl');
  for (const k of ['w', 'a', 's', 'd', 'q', 'e']) add(k, 20, 'key', k, k === 'w' ? 4 : 0);
  for (const k of ['space', 'tab', 'esc']) add(k, 32, 'key', k === 'space' ? 'spc' : k, k === 'space' ? 6 : 0);
  for (let d = 1; d <= 9; d++) add(String(d), 16, 'key', String(d), d === 1 ? 6 : 0);
  for (const key of ['enter', 'up', 'down', 'left', 'right']) add(key, key === 'enter' ? 42 : 36, 'key', key, key === 'enter' ? 6 : 0);
  add('lmb', 30, 'key', 'lmb', 6);
  add('rmb', 30, 'key', 'rmb');
  add('mx', 52, 'mouse', 'mouse x', 6);
  add('my', 52, 'mouse', 'mouse y');
  for (const c of cols) {
    c.wired = c.id in BIT;
    c.bind = K1[c.id] || '';
    if (c.kind === 'key' || c.kind === 'mouse') {
      c.title = `${c.kind === 'mouse' ? c.label : c.id} — ${c.bind} (K1 keymap). ` +
        (c.wired ? `Sample scene: ${STANDIN[c.id]}.` : 'Painting this lane still writes the score; the sample scene has no matching action.');
    }
  }
  cols.width = x + 4;
  return cols;
}
export const COLS = makeCols();
export const colIndex = (id) => COLS.findIndex((c) => c.id === id);
export const FIRST_INPUT = colIndex('w');
const LAST_INPUT = COLS.length - 1;
const isKeyCol = (c) => c && c.kind === 'key';

export class Roll {
  constructor(el, hooks) {
    this.events = new ListenerScope();
    this.el = el;
    this.hooks = hooks;
    this.scroll = el.querySelector('.roll-scroll');
    this.canvas = el.querySelector('canvas');
    this.spacer = el.querySelector('.roll-spacer');
    this.head = el.querySelector('.roll-head');
    this.ctx = this.canvas.getContext('2d');
    this.data = null;
    this.sel = { t0: 1, t1: 1, c: FIRST_INPUT, anchor: 1 };
    this.stroke = null;
    this.hover = -1;
    this.dirty = true;
    this.buildHead();
    this.bind();
  }

  buildHead() {
    const frag = document.createDocumentFragment();
    for (const c of COLS) {
      const s = document.createElement('span');
      s.className = 'rh ' + c.kind + (c.wired ? ' wired' : '') + (c.kind === 'key' && !c.wired ? ' inert' : '');
      s.textContent = c.label;
      s.dataset.col = c.id;
      if (c.title) s.title = c.title;
      else if (c.kind === 'mark') s.title = 'Marks (@mark). Hover a diamond for its name.';
      else if (c.kind === 'flags') s.title = 'Tick flags. A filled diamond is a keyframe.';
      else s.title = 'Tick. Click a tick number to seek there.';
      s.style.left = c.x + 'px';
      s.style.width = c.w + 'px';
      frag.append(s);
    }
    this.head.replaceChildren(frag);
    this.head.style.width = COLS.width + 'px';
  }

  set(data) { this.data = data; this.clampSel(); this.dirty = true; }

  get rows() { return this.data ? this.data.model.rows : []; }

  maxTick() { return Math.max(0, this.rows.length - 1); }

  clampSel() {
    const m = this.maxTick();
    const s = this.sel;
    s.t0 = Math.min(Math.max(0, s.t0), m);
    s.t1 = Math.min(Math.max(0, s.t1), m);
    s.anchor = Math.min(Math.max(0, s.anchor), m);
  }

  selRange() { return [Math.min(this.sel.t0, this.sel.t1), Math.max(this.sel.t0, this.sel.t1)]; }

  select(t0, t1 = t0, c = this.sel.c, reveal = true) {
    this.sel = { t0, t1, c, anchor: t0 };
    this.clampSel();
    if (reveal) this.reveal(this.sel.t1);
    this.dirty = true;
    if (this.hooks.onSelect) this.hooks.onSelect();
  }

  reveal(t) {
    const top = t * ROW, sh = this.scroll.clientHeight - this.head.offsetHeight;
    if (top < this.scroll.scrollTop) this.scroll.scrollTop = top;
    else if (top + ROW > this.scroll.scrollTop + sh) this.scroll.scrollTop = top + ROW - sh;
  }

  // Keep the playhead in view while playing.
  follow(t) {
    const top = t * ROW, sh = this.scroll.clientHeight - this.head.offsetHeight;
    if (top < this.scroll.scrollTop || top + ROW > this.scroll.scrollTop + sh) this.scroll.scrollTop = Math.max(0, top - Math.floor(sh / 3));
  }

  at(ev) {
    const r = this.canvas.getBoundingClientRect();
    const x = ev.clientX - r.left, y = ev.clientY - r.top;
    const t = Math.floor((y + this.scroll.scrollTop) / ROW);
    let c = -1;
    for (let i = 0; i < COLS.length; i++) if (x >= COLS[i].x && x < COLS[i].x + COLS[i].w) { c = i; break; }
    return { t: Math.min(Math.max(0, t), this.maxTick()), c };
  }

  held(t, c) {
    const col = COLS[c];
    const row = this.rows[t];
    if (!row || !this.data) return false;
    return isKeyCol(col) && heldIn(rowInput(this.data.prog, row), col.id);
  }

  suspend() {
    this.events.suspend();
    if (this.pointerId != null && this.canvas.hasPointerCapture(this.pointerId)) this.canvas.releasePointerCapture(this.pointerId);
    this.pointerId = null; this.stroke = null; this.selecting = false; this.hover = -1;
  }
  resume() { this.events.resume(); this.dirty = true; }

  bind() {
    const cv = this.canvas;
    this.events.listen(this.scroll, 'scroll', () => { this.dirty = true; });
    this.events.listen(this.scroll, 'wheel', () => { if (this.hooks.onBrowse) this.hooks.onBrowse(); }, { passive: true });
    this.events.listen(this.scroll, 'pointerdown', (ev) => {
      // A press on the scrollport itself is scrollbar/empty-track navigation.
      // Canvas edits and selections must not disable follow mode.
      if (ev.target === this.scroll && this.hooks.onBrowse) this.hooks.onBrowse();
    });
    this.events.listen(cv, 'pointerdown', (ev) => {
      if (ev.button !== 0 || !this.data) return;
      const { t, c } = this.at(ev);
      this.el.focus({ preventScroll: true });
      const col = COLS[c];
      if (!col) return;
      if (ev.shiftKey) {
        this.sel.t1 = t;
        if (c >= FIRST_INPUT) this.sel.c = c;
        this.dirty = true;
        if (this.hooks.onSelect) this.hooks.onSelect();
        return;
      }
      if (col.kind === 'tick') { this.select(t, t, this.sel.c, false); this.hooks.onSeek(t); return; }
      if (col.kind === 'key') {
        const row = this.rows[t];
        if (row.kind === 'wait' || row.kind === 'boot') { this.select(t, t, c, false); if (this.hooks.onRefuse) this.hooks.onRefuse(row); return; }
        this.pointerId = ev.pointerId; cv.setPointerCapture(ev.pointerId);
        this.stroke = { t0: t, t1: t, c0: c, c1: c, value: !this.held(t, c) };
        this.dirty = true;
        return;
      }
      this.pointerId = ev.pointerId; cv.setPointerCapture(ev.pointerId);
      this.selecting = true;
      this.select(t, t, col.kind === 'mouse' ? c : this.sel.c, false);
    });
    this.events.listen(cv, 'pointermove', (ev) => {
      if (!this.data) return;
      const { t, c } = this.at(ev);
      this.edgeScroll(ev);
      if (this.stroke) {
        if (t !== this.stroke.t1 || (isKeyCol(COLS[c]) && c !== this.stroke.c1)) {
          this.stroke.t1 = t;
          if (isKeyCol(COLS[c])) this.stroke.c1 = c;
          this.dirty = true;
        }
      } else if (this.selecting) {
        if (t !== this.sel.t1) { this.sel.t1 = t; this.dirty = true; if (this.hooks.onSelect) this.hooks.onSelect(); }
      }
      if (t !== this.hover) { this.hover = t; this.dirty = true; }
      if (this.hooks.onHover) this.hooks.onHover(t, c, ev.clientX, ev.clientY);
    });
    const end = () => {
      if (this.stroke) {
        const s = this.stroke;
        this.stroke = null;
        const t0 = Math.min(s.t0, s.t1), t1 = Math.max(s.t0, s.t1);
        const c0 = Math.min(s.c0, s.c1), c1 = Math.max(s.c0, s.c1);
        this.sel = { t0, t1, c: c0, anchor: t0 };
        const cols = COLS.slice(c0, c1 + 1).filter(isKeyCol).map((c) => c.id);
        this.dirty = true;
        this.hooks.onPaint(t0, t1, cols, s.value);
      }
      this.selecting = false;
    };
    this.events.listen(cv, 'pointerup', end);
    this.events.listen(cv, 'pointercancel', () => { this.stroke = null; this.selecting = false; this.dirty = true; });
    this.events.listen(cv, 'pointerleave', () => { this.hover = -1; this.dirty = true; if (this.hooks.onHover) this.hooks.onHover(-1, -1, 0, 0); });
    this.events.listen(cv, 'dblclick', (ev) => {
      const { t, c } = this.at(ev);
      if (COLS[c] && COLS[c].kind === 'mouse' && this.hooks.onEditMouse) this.hooks.onEditMouse(t);
    });
    this.events.listen(cv, 'contextmenu', (ev) => {
      ev.preventDefault();
      if (!this.data) return;
      const { t, c } = this.at(ev);
      const [a, b] = this.selRange();
      if (t < a || t > b) this.select(t, t, c >= FIRST_INPUT ? c : this.sel.c, false);
      else if (c >= FIRST_INPUT) this.sel.c = c;
      this.dirty = true;
      this.hooks.onContext(ev.clientX, ev.clientY);
    });
    this.events.listen(this.head, 'contextmenu', (ev) => {
      const id = ev.target && ev.target.dataset ? ev.target.dataset.col : '';
      const c = colIndex(id);
      if (c < FIRST_INPUT) return;
      ev.preventDefault();
      this.sel.c = c;
      this.dirty = true;
      this.hooks.onContext(ev.clientX, ev.clientY);
    });
    this.events.listen(this.el, 'keydown', (ev) => this.onKey(ev));
    this.events.listen(this.el, 'focus', () => { this.dirty = true; if (this.hooks.onSelect) this.hooks.onSelect(); });
    this.events.listen(this.el, 'blur', () => { this.dirty = true; });
  }

  edgeScroll(ev) {
    if (!this.stroke && !this.selecting) return;
    const r = this.scroll.getBoundingClientRect();
    if (ev.clientY < r.top + 8) this.scroll.scrollTop -= ROW;
    else if (ev.clientY > r.bottom - 8) this.scroll.scrollTop += ROW;
  }

  onKey(ev) {
    if (!this.data) return;
    const s = this.sel, h = this.hooks;
    const move = (d, extend = false) => {
      const t = Math.min(Math.max(0, s.t1 + d), this.maxTick());
      if (extend) { s.t1 = t; this.reveal(t); this.dirty = true; if (h.onSelect) h.onSelect(); }
      else this.select(t, t, s.c);
    };
    const k = ev.key;
    if ((k === 'PageUp' || k === 'PageDown') && h.onBrowse) h.onBrowse();
    const ctrl = ev.ctrlKey || ev.metaKey;
    if (ctrl && (k === 'f' || k === 'F')) { ev.preventDefault(); h.onFind(); return; }
    if (ctrl && (k === 'b' || k === 'B')) { ev.preventDefault(); h.onBranch(); return; }
    if (ctrl || ev.altKey) return;
    if (k === 'ArrowLeft' || k === 'ArrowRight') { ev.preventDefault(); move((k === 'ArrowLeft' ? -1 : 1) * (ev.shiftKey ? 10 : 1)); }
    else if (k === 'ArrowUp' || k === 'ArrowDown') { ev.preventDefault(); move(k === 'ArrowUp' ? -1 : 1, ev.shiftKey); }
    else if (k === 'PageUp' || k === 'PageDown') { ev.preventDefault(); move((k === 'PageUp' ? -1 : 1) * Math.max(1, Math.floor((this.scroll.clientHeight - this.head.offsetHeight) / ROW) - 1)); }
    else if (k === '[' || k === ']') {
      ev.preventDefault();
      s.c = Math.min(Math.max(FIRST_INPUT, s.c + (k === '[' ? -1 : 1)), LAST_INPUT);
      this.dirty = true;
      if (h.onSelect) h.onSelect();
    } else if (k === 'Home') { ev.preventDefault(); this.select(0, 0); }
    else if (k === 'End') { ev.preventDefault(); this.select(this.data.head, this.data.head); }
    else if (k === 'Enter') { ev.preventDefault(); h.onSeek(s.t1); }
    else if (k === 'n' || k === 'N') { ev.preventDefault(); h.onMark(); }
    else if (k === 't' || k === 'T') {
      ev.preventDefault();
      const col = COLS[s.c];
      const [a, b] = this.selRange();
      if (isKeyCol(col)) h.onPaint(a, b, [col.id], !this.held(a, s.c));
      else if (col && col.kind === 'mouse' && h.onEditMouse) h.onEditMouse(s.t1);
    } else if (k === 'Delete' || k === 'Backspace') { ev.preventDefault(); h.onClear(); }
    else if (k === 'Escape') { ev.preventDefault(); this.select(s.t1, s.t1, s.c, false); }
    else if (k === 'ContextMenu' || (k === 'F10' && ev.shiftKey)) {
      ev.preventDefault();
      const r = this.canvas.getBoundingClientRect();
      const col = COLS[s.c] || COLS[FIRST_INPUT];
      h.onContext(r.left + col.x + col.w, r.top + (s.t1 + 1) * ROW - this.scroll.scrollTop);
    }
  }

  size() {
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const h0 = Math.max(ROW, this.scroll.clientHeight - this.head.offsetHeight);
    const sp0 = Math.max(0, this.rows.length * ROW - h0) + 'px';
    if (this.spacer.style.height !== sp0) this.spacer.style.height = sp0;
    const w = Math.max(COLS.width, this.scroll.clientWidth), h = Math.max(ROW, this.scroll.clientHeight - this.head.offsetHeight);
    const bw = Math.round(w * dpr), bh = Math.round(h * dpr);
    if (this.canvas.width !== bw || this.canvas.height !== bh) {
      this.canvas.width = bw;
      this.canvas.height = bh;
      this.canvas.style.width = w + 'px';
      this.canvas.style.height = h + 'px';
    }
    const sp = Math.max(0, this.rows.length * ROW - h) + 'px';
    if (this.spacer.style.height !== sp) this.spacer.style.height = sp;
    this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    return [w, h];
  }

  draw() {
    if (!this.data) return;
    const [w, h] = this.size();
    const c = this.ctx, d = this.data, b = d.b, rows = this.rows;
    const top = this.scroll.scrollTop;
    const t0 = Math.max(0, Math.floor(top / ROW)), t1 = Math.min(rows.length - 1, Math.ceil((top + h) / ROW));
    c.fillStyle = P.void;
    c.fillRect(0, 0, w, h);
    c.font = `11px ${MONO}`;
    c.textBaseline = 'middle';
    const head = d.head;
    const stroke = this.stroke;
    const [sa, sb] = this.selRange();
    const focused = document.activeElement === this.el;
    for (let t = t0; t <= t1; t++) {
      const row = rows[t];
      const y = t * ROW - top;
      const played = t <= head;
      const f = played ? flagsOf(b, t) : 0;
      // Background by greenzone state.
      if (t === b.div) c.fillStyle = tint(P.magenta, 0.42);
      else if (played && (row.kind === 'wait' || (f & F.HOLD))) c.fillStyle = tint(P.amber, 0.2);
      else if (played && (f & F.CAPTURED)) c.fillStyle = tint(P.kolto, 0.08 + 0.2 * Math.max(0, 1 - rpDistance(b, t) / 90));
      else if (played) c.fillStyle = tint(P.dim, 0.1);
      else c.fillStyle = P.void;
      c.fillRect(0, y, w, ROW);
      if (played && (f & F.HOLD) && t !== b.div) hatch(c, 0, y, w, ROW);
      if (t === this.hover) { c.fillStyle = tint(P.ink, 0.05); c.fillRect(0, y, w, ROW); }
      if (t >= sa && t <= sb) { c.fillStyle = tint(P.bacta, 0.12); c.fillRect(0, y, COLS[FIRST_INPUT].x - 2, ROW); }
      // Restore point rail.
      if (f & F.RESTORE_POINT) { c.fillStyle = P.bacta; c.fillRect(0, y, 3, ROW); }
      c.fillStyle = tint(P.dim, 0.1);
      c.fillRect(0, y + ROW - 1, w, 1);
      // Tick column: cursor arrow, number, segment chip.
      const tc = COLS[0];
      if (t === d.playhead) {
        c.fillStyle = P.phosphor;
        c.beginPath();
        c.moveTo(6, y + 4); c.lineTo(13, y + ROW / 2); c.lineTo(6, y + ROW - 4);
        c.fill();
      }
      c.fillStyle = t === d.playhead ? P.phosphor : !played && row.kind === 'end' ? tint(P.dim, 0.5) : played ? P.ink : P.dim;
      c.textAlign = 'right';
      c.fillText(String(t), tc.x + 50, y + ROW / 2 + 1);
      const seg = d.segs.get(t);
      if (seg) {
        c.font = `9px ${MONO}`;
        const label = `${seg.name} ${seg.hz}`;
        const lw = 30;
        c.fillStyle = tint(P.bacta, 0.2);
        c.fillRect(tc.x + tc.w - lw - 2, y + 3, lw, ROW - 6);
        c.fillStyle = P.bacta;
        c.textAlign = 'left';
        c.save();
        c.beginPath(); c.rect(tc.x + tc.w - lw - 2, y, lw, ROW); c.clip();
        c.fillText(label, tc.x + tc.w - lw + 1, y + ROW / 2 + 1);
        c.restore();
        c.font = `11px ${MONO}`;
      }
      if (d.marks.has(t)) diamond(c, COLS[1].x + COLS[1].w / 2, y + ROW / 2, 5, P.amber, true);
      if (f & F.KEYFRAME) diamond(c, COLS[2].x + COLS[2].w / 2, y + ROW / 2, 4, P.kolto, true);
      if (f & F.MATERIALIZED) diamond(c, COLS[2].x + COLS[2].w / 2, y + ROW / 2, 7, P.kolto, false);
      // Input cells.
      if (row.kind === 'wait') {
        c.fillStyle = P.amber;
        c.textAlign = 'left';
        const it = d.prog.ins[row.src];
        c.fillText(`@sync ${it.ev}${played ? '' : ' (not reached yet)'}`, COLS[FIRST_INPUT].x + 4, y + ROW / 2 + 1);
      } else {
        const inp = rowInput(d.prog, row);
        const inStroke = stroke && row.kind !== 'boot' && t >= Math.min(stroke.t0, stroke.t1) && t <= Math.max(stroke.t0, stroke.t1);
        for (let i = FIRST_INPUT; i < COLS.length; i++) {
          const col = COLS[i];
          const x = col.x;
          if (col.kind === 'mouse') {
            if (inp.mouse) {
              c.fillStyle = P.ink;
              c.textAlign = 'right';
              c.fillText(String(inp.mouse[col.id === 'mx' ? 0 : 1]), x + col.w - 4, y + ROW / 2 + 1);
            }
            continue;
          }
          let on = heldIn(inp, col.id);
          if (inStroke && i >= Math.min(stroke.c0, stroke.c1) && i <= Math.max(stroke.c0, stroke.c1)) on = stroke.value;
          if (on) {
            c.fillStyle = col.wired ? P.kolto : tint(P.dim, 0.75);
            c.fillRect(x + 2, y + 2, col.w - 4, ROW - 5);
            c.fillStyle = P.void;
            c.textAlign = 'center';
            c.font = `600 10px ${MONO}`;
            c.fillText(col.label, x + col.w / 2, y + ROW / 2);
            c.font = `11px ${MONO}`;
            if (inp.al && inp.al[col.id]) { c.fillStyle = P.bacta; c.fillRect(x + col.w - 6, y + 2, 4, 4); }
          } else if (row.kind !== 'boot') {
            c.fillStyle = tint(P.dim, played ? 0.35 : 0.2);
            c.fillRect(x + col.w / 2 - 1, y + ROW / 2 - 1, 2, 2);
          }
        }
      }
      // Epoch boundary rule. A restore never crosses it.
      if (played && t > 0 && (f & F.EPOCH_START)) {
        c.fillStyle = P.ink;
        c.fillRect(0, y, w, 2);
        c.font = `9px ${MONO}`;
        const label = `epoch ${b.recs[t].ep}`;
        const lw = c.measureText(label).width + 8;
        c.fillRect(w - lw - 4, y, lw, 11);
        c.fillStyle = P.void;
        c.textAlign = 'right';
        c.fillText(label, w - 8, y + 6);
        c.font = `11px ${MONO}`;
      }
      if (t === d.end && t > 0) {
        c.fillStyle = tint(P.dim, 0.6);
        for (let x = 0; x < w; x += 8) c.fillRect(x, y, 4, 1);
      }
    }
    const col = COLS[this.sel.c];
    if (col) {
      const ya = sa * ROW - top, yb = (sb + 1) * ROW - top;
      c.strokeStyle = focused ? P.bacta : tint(P.bacta, 0.55);
      c.lineWidth = focused ? 2 : 1;
      c.strokeRect(col.x + 1, ya + 1, col.w - 2, yb - ya - 2);
      c.lineWidth = 1;
    }
    this.dirty = false;
  }

  describe(t, c) {
    const d = this.data;
    if (!d) return '';
    const row = this.rows[t], col = COLS[c];
    if (!row) return '';
    const parts = [`Tick ${t}`];
    if (col && (col.kind === 'key' || col.kind === 'mouse')) {
      const inp = rowInput(d.prog, row);
      if (row.kind === 'wait') parts.push('@sync wait row, edit it in the score text');
      else if (col.kind === 'key') parts.push(`${col.id} ${heldIn(inp, col.id) ? 'held' : 'not held'}${col.wired ? '' : ', inert here'}`);
      else parts.push(`${col.label} ${inp.mouse ? inp.mouse[col.id === 'mx' ? 0 : 1] : 'not set'}`);
    }
    if (d.marks.has(t)) parts.push('mark ' + d.marks.get(t).join(', '));
    parts.push(stateText(d.b, row, t, d.head));
    return parts.join('. ') + '.';
  }
}

export function stateText(b, row, t, head) {
  if (t > head) return row.kind === 'end' ? 'Past the end of the score, not played' : 'Not played yet';
  const f = flagsOf(b, t);
  const out = [];
  if (t === b.div) out.push('first divergence');
  if (row.kind === 'wait') out.push('@sync wait');
  if (f & F.HOLD) out.push('mid-load (hold)');
  out.push(f & F.CAPTURED ? 'greenzone' : 'played, no state stored');
  if (f & F.RESTORE_POINT) out.push('restore point');
  else out.push(`not a restore point: ${REASONS[reasonOf(b, b.recs[t])]}`);
  if (f & F.KEYFRAME) out.push('keyframe');
  if (f & F.EPOCH_START) out.push(`epoch ${b.recs[t].ep} starts`);
  if (f & F.CAPTURED) { const dd = rpDistance(b, t); if (dd > 0) out.push(`${dd} ticks after the last restore point`); }
  return out.join(', ');
}

// Ticks back to the nearest restore point at or before t: the replay a hybrid
// seek would need. Capped at 999.
export function rpDistance(b, t) {
  for (let k = t, n = 0; k >= 0 && n < 999; k--, n++) if (flagsOf(b, k) & F.RESTORE_POINT) return n;
  return 999;
}

function diamond(c, x, y, r, color, fill) {
  c.beginPath();
  c.moveTo(x, y - r); c.lineTo(x + r, y); c.lineTo(x, y + r); c.lineTo(x - r, y); c.closePath();
  if (fill) { c.fillStyle = color; c.fill(); } else { c.strokeStyle = color; c.stroke(); }
}

function hatch(c, x, y, w, h) {
  c.save();
  c.beginPath(); c.rect(x, y, w, h); c.clip();
  c.strokeStyle = tint(P.amber, 0.35);
  c.beginPath();
  for (let k = -h; k < w; k += 8) { c.moveTo(x + k, y + h); c.lineTo(x + k + h, y); }
  c.stroke();
  c.restore();
}
