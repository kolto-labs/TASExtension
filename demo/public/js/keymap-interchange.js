// Native keymap v1/v2 interchange. V2 aliases are explicitly host-window scoped.
// Accept its JSON-quoted strings and one-line arrays; never execute file content.
const KEYS = {
  ArrowUp:'Up', ArrowDown:'Down', ArrowLeft:'Left', ArrowRight:'Right',
  PageUp:'PgUp', PageDown:'PgDown', Insert:'Ins', Delete:'Del', Enter:'Return',
  Space:'Space', Home:'Home', End:'End', Backspace:'Backspace', Backquote:'`',
  Minus:'-', Equal:'=', BracketLeft:'[', BracketRight:']', Backslash:'\\',
  Semicolon:';', Quote:"'", Comma:',', Period:'.', Slash:'/',
};
const REVERSE = Object.fromEntries(Object.entries(KEYS).map(([code, key]) => [key, code]));
const MODIFIERS = { Control:'Ctrl', Alt:'Alt', Shift:'Shift', Meta:'Meta' };
export function nativeSequence(chord) {
  if (!chord) return '';
  const parts = chord.split('+'), code = parts.pop();
  const key = KEYS[code] || (/^Key[A-Z]$/.test(code) ? code.slice(3) : /^Digit[0-9]$/.test(code) ? code.slice(5) : /^F(?:[1-9]|1[0-9]|2[0-4])$/.test(code) ? code : null);
  if (!key || parts.some(part => !MODIFIERS[part])) throw new Error('Choose a non-keypad shortcut before exporting this keymap.');
  return [...parts.map(part => MODIFIERS[part]), key].join('+');
}
function browserSequence(sequence) {
  if (!sequence) return '';
  const parts = sequence.split('+'), key = parts.pop();
  const mods = parts.map(part => ({Ctrl:'Control', Alt:'Alt', Shift:'Shift', Meta:'Meta'})[part]);
  const code = REVERSE[key] || (/^[A-Z]$/.test(key) ? `Key${key}` : /^[0-9]$/.test(key) ? `Digit${key}` : /^F(?:[1-9]|1[0-9]|2[0-4])$/.test(key) ? key : null);
  if (!code || mods.some(part => !part) || new Set(mods).size !== mods.length) throw new Error(`Unsupported shared shortcut: ${sequence}`);
  return [...['Control', 'Alt', 'Shift', 'Meta'].filter(part => mods.includes(part)), code].join('+');
}
export function exportKeymap(bindings, ids) {
  return 'version = 2\nalias_scope = "host-window"\n\n[keys]\n' + ids.map(id => {
    const value = bindings[id] || [];
    const keys = [...new Set((Array.isArray(value) ? value : [value]).filter(Boolean).map(nativeSequence))];
    return `${JSON.stringify(id)} = ${JSON.stringify(keys.length === 1 ? keys[0] : keys)}`;
  }).join('\n') + '\n';
}
function uncomment(line) {
  let quoted = false, escaped = false;
  for (let i = 0; i < line.length; i++) {
    const char = line[i];
    if (escaped) { escaped = false; continue; }
    if (quoted && char === '\\') { escaped = true; continue; }
    if (char === '"') quoted = !quoted;
    if (!quoted && char === '#') return line.slice(0, i).trim();
  }
  return line.trim();
}
export function importKeymap(text, ids) {
  if (new TextEncoder().encode(text).length > 65536) throw new Error('Shortcut files must be 64 KiB or smaller.');
  let version = null, aliasScope = null, inKeys = false, seenKeys = false, skipped = 0;
  const bindings = {}, seen = new Set();
  for (const original of text.replace(/^\uFEFF/, '').split(/\r?\n/)) {
    const line = uncomment(original);
    if (!line) continue;
    if (line === '[keys]') {
      if (seenKeys) throw new Error('Duplicate keys section.');
      inKeys = true; seenKeys = true; continue;
    }
    if (!inKeys && /^version\s*=\s*[12]$/.test(line) && version === null) { version = Number(line.split('=')[1].trim()); continue; }
    if (!inKeys && /^alias_scope\s*=\s*"host-window"$/.test(line) && aliasScope === null) { aliasScope = 'host-window'; continue; }
    const match = inKeys && line.match(/^("(?:[^"\\]|\\.)*"|[A-Za-z_][A-Za-z0-9_.-]*)\s*=\s*(.*)$/);
    if (!match) throw new Error('Use a version 1 or 2 keymap exported by Keyboard or native Settings.');
    const id = match[1].startsWith('"') ? JSON.parse(match[1]) : match[1];
    if (seen.has(id)) throw new Error(`Duplicate action: ${id}`);
    seen.add(id);
    const value = JSON.parse(match[2]);
    if (typeof value !== 'string' && !(Array.isArray(value) && value.every(item => typeof item === 'string'))) throw new Error(`Invalid shortcut for ${id}.`);
    if (!ids.includes(id)) { skipped++; continue; }
    const sequences = [...new Set((Array.isArray(value) ? value : [value]).filter(Boolean))];
    bindings[id] = [...new Set(sequences.map(browserSequence))];
  }
  if ((version !== 1 && version !== 2) || (version === 2 && aliasScope !== 'host-window') || (version === 1 && aliasScope !== null) || !seenKeys || !Object.keys(bindings).length) throw new Error('The file needs shared actions in [keys]; version 2 requires alias_scope = \"host-window\".');
  return { bindings, skipped };
}
