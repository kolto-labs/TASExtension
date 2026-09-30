// "Recorded in the real game": plays back a capture described by
// media/swoop/manifest.json. The browser only shows the recording; it does
// not simulate the real game. Nothing here claims the toolkit made it.
//
// Manifest fields: title, source_note, video, poster, frames[] {t, src},
// inputs[] {t, keys}. Times are in seconds from the start of the recording.

const MANIFEST = 'media/swoop/manifest.json';
const MONO = 'ui-monospace, "SFMono-Regular", Menlo, Consolas, "Liberation Mono", monospace';
const LANE_ORDER = ['w', 'a', 's', 'd', 'q', 'e', 'space', 'tab', 'esc', '1', '2', '3', '4', '5', '6', '7', '8', '9', 'lmb', 'rmb'];

function sameOrigin(src, base) {
  if (typeof src !== 'string' || !src || src.length > 500) return '';
  try {
    const u = new URL(src, base);
    return u.origin === location.origin && (u.protocol === 'http:' || u.protocol === 'https:') ? u.href : '';
  } catch { return ''; }
}

const num = (v) => (typeof v === 'number' && Number.isFinite(v) && v >= 0 && v < 86400 ? v : null);

export function cleanManifest(m, base) {
  if (!m || typeof m !== 'object' || Array.isArray(m)) return null;
  const frames = (Array.isArray(m.frames) ? m.frames : []).slice(0, 5000)
    .map((f) => (f && typeof f === 'object' ? { t: num(f.t), src: sameOrigin(f.src, base) } : null))
    .filter((f) => f && f.t !== null && f.src)
    .sort((a, b) => a.t - b.t);
  const inputs = (Array.isArray(m.inputs) ? m.inputs : []).slice(0, 50000)
    .map((e) => {
      if (!e || typeof e !== 'object' || num(e.t) === null) return null;
      const raw = Array.isArray(e.keys) ? e.keys : typeof e.keys === 'string' ? e.keys.split(/[\s,+]+/) : [];
      const keys = raw.filter((k) => typeof k === 'string' && /^[A-Za-z0-9_@.,$-]{1,16}$/.test(k) && k !== '.').map((k) => k.toLowerCase());
      return { t: e.t, keys };
    })
    .filter(Boolean)
    .sort((a, b) => a.t - b.t);
  const video = sameOrigin(m.video, base);
  const poster = sameOrigin(m.poster, base);
  if (!frames.length && !video) return null;
  return {
    title: typeof m.title === 'string' ? m.title.slice(0, 200) : '',
    note: typeof m.source_note === 'string' ? m.source_note.slice(0, 1000) : '',
    video, poster, frames, inputs,
  };
}

export async function fetchManifest() {
  const url = new URL(MANIFEST, document.baseURI);
  try {
    const res = await fetch(url, { cache: 'no-cache', credentials: 'same-origin' });
    const type = res.headers.get('content-type') || '';
    if (!res.ok || !/json/i.test(type)) return null;
    return cleanManifest(await res.json(), url.href);
  } catch { return null; }
}

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
}

const fmt = (s) => `${String(Math.floor(s / 60)).padStart(2, '0')}:${(s % 60).toFixed(2).padStart(5, '0')}`;

export class Footage {
  constructor(root, { reduced }) {
    this.root = root;
    this.reduced = reduced;
    this.loaded = false;
    this.m = null;
    this.t = 0;
  }

  async open() {
    if (this.loaded) return;
    this.loaded = true;
    this.root.replaceChildren(el('p', 'status', 'Loading the capture.'));
    this.m = await fetchManifest();
    if (!this.m) {
      this.root.replaceChildren(el('p', 'footage-empty', 'Real game capture is on the way.'));
      return;
    }
    this.build();
  }

  build() {
    const m = this.m;
    const frag = document.createDocumentFragment();
    frag.append(el('h3', 'footage-title', m.title || 'Recording'));
    frag.append(el('p', 'honest', 'This is a recording of the real game. Your browser plays it back. It does not simulate it.'));
    if (m.note) frag.append(el('p', 'help', m.note));
    const view = el('div', 'footage-view');
    if (m.video) {
      const v = document.createElement('video');
      v.src = m.video;
      if (m.poster) v.poster = m.poster;
      v.controls = true;
      v.preload = 'metadata';
      v.playsInline = true;
      v.muted = true;
      v.setAttribute('aria-label', 'Recording of the real game');
      v.addEventListener('timeupdate', () => this.setTime(v.currentTime, 'video'));
      v.addEventListener('loadedmetadata', () => { this.duration(); this.render(); });
      this.video = v;
      view.append(v);
    } else {
      const img = document.createElement('img');
      img.alt = 'Frame from the recording of the real game';
      this.img = img;
      view.append(img);
    }
    frag.append(view);
    const scrub = el('div', 'footage-scrub');
    const range = document.createElement('input');
    range.type = 'range';
    range.min = '0';
    range.step = '0.01';
    range.value = '0';
    range.id = 'footage-range';
    range.setAttribute('aria-label', 'Scrub the recording');
    range.addEventListener('input', () => this.setTime(+range.value, 'range'));
    this.range = range;
    this.clock = el('span', 'mono footage-clock', '00:00.00');
    scrub.append(range, this.clock);
    frag.append(scrub);
    if (m.frames.length) {
      const strip = el('div', 'footage-strip');
      strip.setAttribute('role', 'group');
      strip.setAttribute('aria-label', 'Frames. Pick one to jump there.');
      this.thumbs = m.frames.map((f, i) => {
        const b = el('button', 'fthumb');
        b.type = 'button';
        b.setAttribute('aria-label', `Frame at ${fmt(f.t)}`);
        const im = document.createElement('img');
        im.src = f.src;
        im.alt = '';
        im.loading = 'lazy';
        const cap = el('span', 'mono', f.t.toFixed(2));
        b.append(im, cap);
        b.addEventListener('click', () => this.setTime(f.t, 'strip'));
        b.dataset.i = String(i);
        strip.append(b);
        return b;
      });
      this.strip = strip;
      frag.append(strip);
    }
    const laneHead = el('h4', 'sub', 'Recorded inputs (read only)');
    frag.append(laneHead);
    if (m.inputs.length) {
      const lane = document.createElement('canvas');
      lane.className = 'footage-lane';
      lane.setAttribute('role', 'img');
      this.lane = lane;
      this.keys = [...new Set(m.inputs.flatMap((e) => e.keys))].sort((a, b) => {
        const ia = LANE_ORDER.indexOf(a), ib = LANE_ORDER.indexOf(b);
        return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib) || a.localeCompare(b);
      }).slice(0, 24);
      lane.setAttribute('aria-label', `Recorded inputs over time for ${this.keys.join(', ')}. Read only.`);
      lane.addEventListener('pointerdown', (ev) => {
        const r = lane.getBoundingClientRect();
        this.setTime(((ev.clientX - r.left - 44) / Math.max(1, r.width - 52)) * this.dur, 'lane');
      });
      frag.append(lane);
      this.held = el('p', 'mono help');
      this.held.setAttribute('aria-live', 'off');
      frag.append(this.held);
    } else frag.append(el('p', 'help', 'The capture lists no inputs.'));
    this.root.replaceChildren(frag);
    this.duration();
    this.setTime(0, 'init');
    window.addEventListener('resize', () => this.render());
  }

  duration() {
    const m = this.m;
    let d = 0;
    if (m.frames.length) d = Math.max(d, m.frames[m.frames.length - 1].t);
    if (m.inputs.length) d = Math.max(d, m.inputs[m.inputs.length - 1].t);
    if (this.video && Number.isFinite(this.video.duration)) d = Math.max(d, this.video.duration);
    this.dur = Math.max(d, 0.01);
    this.range.max = String(this.dur);
  }

  frameAt(t) {
    const f = this.m.frames;
    let lo = 0, hi = f.length - 1, best = 0;
    while (lo <= hi) { const mid = (lo + hi) >> 1; if (f[mid].t <= t) { best = mid; lo = mid + 1; } else hi = mid - 1; }
    return best;
  }

  heldAt(t) {
    const e = this.m.inputs;
    let cur = [];
    for (const x of e) { if (x.t > t) break; cur = x.keys; }
    return cur;
  }

  setTime(t, from) {
    this.t = Math.min(Math.max(0, t || 0), this.dur);
    if (from !== 'range') this.range.value = String(this.t);
    if (this.video && from !== 'video' && Math.abs(this.video.currentTime - this.t) > 0.03) this.video.currentTime = this.t;
    this.clock.textContent = fmt(this.t);
    this.range.setAttribute('aria-valuetext', fmt(this.t));
    if (this.m.frames.length) {
      const i = this.frameAt(this.t);
      if (this.img && this.img.getAttribute('src') !== this.m.frames[i].src) this.img.src = this.m.frames[i].src;
      if (this.cur !== i) {
        if (this.thumbs[this.cur]) this.thumbs[this.cur].classList.remove('on');
        this.cur = i;
        const th = this.thumbs[i];
        th.classList.add('on');
        const s = this.strip;
        const left = th.offsetLeft - s.clientWidth / 2 + th.clientWidth / 2;
        s.scrollTo({ left, behavior: this.reduced() || from === 'video' ? 'auto' : 'smooth' });
      }
    }
    if (this.held) this.held.textContent = 'Held at ' + fmt(this.t) + ': ' + (this.heldAt(this.t).join(' ') || 'nothing');
    this.render();
  }

  render() {
    const cv = this.lane;
    if (!cv || !this.keys) return;
    const rowH = 14, w = cv.clientWidth || 600, h = this.keys.length * rowH + 16;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    if (cv.width !== Math.round(w * dpr) || cv.height !== Math.round(h * dpr)) { cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr); cv.style.height = h + 'px'; }
    const c = cv.getContext('2d');
    c.setTransform(dpr, 0, 0, dpr, 0, 0);
    c.fillStyle = '#05070a';
    c.fillRect(0, 0, w, h);
    const x0 = 44, span = w - 52;
    const X = (t) => x0 + (t / this.dur) * span;
    c.font = `10px ${MONO}`;
    c.textBaseline = 'middle';
    const ev = this.m.inputs;
    this.keys.forEach((k, r) => {
      const y = 4 + r * rowH;
      c.fillStyle = 'rgba(127, 147, 160, 0.08)';
      c.fillRect(x0, y, span, rowH - 2);
      c.fillStyle = '#7f93a0';
      c.textAlign = 'right';
      c.fillText(k, x0 - 6, y + rowH / 2);
      c.fillStyle = 'rgba(90, 216, 255, 0.75)';
      for (let i = 0; i < ev.length; i++) {
        if (!ev[i].keys.includes(k)) continue;
        const a = X(ev[i].t), b = X(i + 1 < ev.length ? ev[i + 1].t : this.dur);
        c.fillRect(a, y + 1, Math.max(1, b - a), rowH - 4);
      }
    });
    const px = Math.round(X(this.t)) + 0.5;
    c.strokeStyle = '#7dff9a';
    c.beginPath(); c.moveTo(px, 0); c.lineTo(px, h); c.stroke();
  }
}
