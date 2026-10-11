// These tools consume the existing paused guest-memory reader. They never scan
// outside the explicit range and never write to or resume the guest.
const TYPES = {
  u8: [1, 'getUint8', 0, 255], u16: [2, 'getUint16', 0, 65535],
  u32: [4, 'getUint32', 0, 0xffffffff], i32: [4, 'getInt32', -0x80000000, 0x7fffffff],
  f32: [4, 'getFloat32'], f64: [8, 'getFloat64'],
};
const addressText = value => `0x${value.toString(16).padStart(8, '0')}`;
const node = (tag, text = '', className = '') => {
  const element = document.createElement(tag); element.textContent = text;
  if (className) element.className = className;
  return element;
};
const button = (label, run) => {
  const element = node('button', label, 'btn small'); element.type = 'button';
  element.addEventListener('click', run); return element;
};
const field = (label, element) => { const wrapper = node('label', label); wrapper.append(element); return wrapper; };
const select = entries => {
  const element = node('select');
  for (const [value, label] of entries) { const option = node('option', label); option.value = value; element.append(option); }
  return element;
};

export function createMemoryTools({ panel, table, context, addWatch }) {
  const scanPanel = panel('scan', 'Value scanner');
  const stringsPanel = panel('strings', 'Strings');
  const form = node('form', '', 'debug-memory-form');
  const address = node('input'); address.value = '0x00400000'; address.spellcheck = false;
  const length = node('input'); length.type = 'number'; length.min = '1'; length.max = '65536'; length.step = '1'; length.value = '4096';
  const type = select(Object.keys(TYPES).map(value => [value, value])); type.value = 'u32';
  const condition = select([['all', 'All values / unknown initial'], ['equal', 'Equal to'], ['changed', 'Changed'], ['same', 'Unchanged'], ['increased', 'Increased'], ['decreased', 'Decreased']]);
  const expected = node('input'); expected.placeholder = 'Decimal or 0x integer'; expected.spellcheck = false;
  const alignment = select([['aligned', 'Type width from start'], ['all', 'Every byte']]);
  const capture = node('button', 'First scan', 'btn small'); capture.type = 'submit';
  const reset = button('Reset scan', () => { clear(); update(); });
  const actions = node('div', '', 'debug-tool-actions'); actions.append(capture, reset);
  form.append(field('Start address (hex)', address), field('Bytes (max 65,536)', length), field('Value type (little endian)', type),
    field('Compare', condition), field('Value', expected), field('Address spacing', alignment), actions);
  const message = node('p', 'Pause the game and select a process. Scans read only the explicit range.', 'debug-tool-status'); message.setAttribute('role', 'status');
  const provenance = node('p', '', 'debug-tool-status');
  const results = table(['Address', 'Previous', 'Captured value', ''], 'Values matching the latest memory scan');
  const scroll = node('div', '', 'debug-memory-results'); scroll.tabIndex = 0; scroll.setAttribute('aria-label', 'Value scan results'); scroll.append(results.element);
  scanPanel.append(form, message, provenance, scroll);

  const stringMessage = node('p', 'Run a value scan to capture a memory range, then inspect its strings here.', 'debug-tool-status'); stringMessage.setAttribute('role', 'status');
  const stringForm = node('div', '', 'debug-memory-form');
  const encoding = select([['ascii', 'ASCII'], ['utf16', 'UTF-16LE (ASCII characters)']]);
  const minimum = node('input'); minimum.type = 'number'; minimum.min = '4'; minimum.max = '256'; minimum.value = '4';
  const filter = node('input'); filter.type = 'search'; filter.placeholder = 'Filter captured strings'; filter.maxLength = 256;
  const inspect = button('Inspect captured range', renderStrings);
  stringForm.append(field('Encoding', encoding), field('Minimum characters', minimum), field('Contains (case insensitive)', filter), inspect);
  const stringResults = table(['Address', 'Characters', 'Text'], 'Strings in the captured guest memory range');
  const stringScroll = node('div', '', 'debug-memory-results'); stringScroll.tabIndex = 0; stringScroll.setAttribute('aria-label', 'Captured string results'); stringScroll.append(stringResults.element);
  stringsPanel.append(stringForm, stringMessage, stringScroll);

  let busy = false, request = 0, snapshot = null, candidates = [], previous = null;
  let stringsDirty = false, lastAvailability = null;
  function isCurrent(owner) {
    const current = context();
    return current.enabled && owner.epoch === current.epoch && owner.game === current.game && owner.session === current.session && owner.pid === current.pid;
  }
  function clear() {
    request++; busy = false; snapshot = null; previous = null; candidates = [];
    results.body.replaceChildren(); stringResults.body.replaceChildren(); provenance.textContent = '';
    message.textContent = 'Scan reset. Pause the game and select a process to capture a range.';
    stringMessage.textContent = 'Run a value scan to capture a memory range, then inspect its strings here.';
    condition.value = 'all'; stringsDirty = false; lastAvailability = null;
  }
  function readValue(bytes, offset, valueType) {
    return new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength)[TYPES[valueType][1]](offset, true);
  }
  function targetValue() {
    const text = expected.value.trim(), config = TYPES[type.value];
    if (!text || !/^[+-]?(?:0x[0-9a-f]+|(?:\d+\.?\d*|\.\d+)(?:e[+-]?\d+)?)$/i.test(text)) throw new Error('Enter a finite numeric comparison value.');
    const value = Number(text);
    if (!Number.isFinite(value) || (config.length === 4 && (!Number.isInteger(value) || value < config[2] || value > config[3]))) throw new Error(`The comparison value is outside the ${type.value} range.`);
    const typed = type.value === 'f32' ? Math.fround(value) : value;
    if (!Number.isFinite(typed)) throw new Error('The comparison value exceeds the f32 range.');
    return typed;
  }
  function renderResults() {
    const data = snapshot;
    results.body.replaceChildren(...candidates.slice(0, 200).map(offset => {
      const row = node('tr'), absolute = data.address + offset;
      row.append(node('td', addressText(absolute)), node('td', previous ? String(readValue(previous.bytes, offset, data.type)) : '—'), node('td', String(readValue(data.bytes, offset, data.type))));
      const action = node('td');
      const watch = button('Watch', () => {
        if (!isCurrent(data.owner)) { clear(); update(); return; }
        message.textContent = addWatch(absolute, data.type) ? `Added ${addressText(absolute)} to Watches.` : 'Could not add watch: the address is already watched, zero, or the 32-watch limit is reached.';
      });
      watch.disabled = absolute === 0; watch.setAttribute('aria-label', `Watch ${addressText(absolute)} as ${data.type}`);
      action.append(watch); row.append(action); return row;
    }));
  }
  form.addEventListener('submit', async event => {
    event.preventDefault(); const owner = context();
    if (!owner.ready || !owner.pid || busy) return;
    let start, count, comparison;
    try {
      const text = address.value.trim();
      start = /^(?:0x)?[0-9a-f]{1,8}$/i.test(text) ? parseInt(text.replace(/^0x/i, ''), 16) : NaN;
      count = Number(length.value);
      if (!Number.isSafeInteger(start) || !Number.isSafeInteger(count) || count < TYPES[type.value][0] || count > 65536 || start + count > 0x100000000) throw new Error('Use a 32-bit hex address and a byte count from the selected value size to 65,536, within the address space.');
      if (condition.value === 'equal') comparison = targetValue();
      if (!snapshot && !['all', 'equal'].includes(condition.value)) throw new Error('First scan must use All values or Equal to.');
    } catch (error) { message.textContent = error.message; return; }
    const ticket = ++request, old = snapshot, valueType = type.value, rule = condition.value, size = TYPES[valueType][0];
    const stride = alignment.value === 'aligned' ? size : 1;
    busy = true; message.textContent = 'Capturing the paused range…'; update();
    try {
      const bytes = await owner.game.readGuestMemory(owner.pid, start, count);
      if (ticket !== request || !isCurrent(owner)) return;
      if (!context().ready) throw new Error('The target resumed before capture completed. Pause and scan again.');
      if (!(bytes instanceof Uint8Array) || bytes.length !== count) throw new Error('The target returned an incomplete memory range.');
      const offsets = old ? candidates : Array.from({ length: Math.floor((count - size) / stride) + 1 }, (_, index) => index * stride);
      const matched = offsets.filter(offset => {
        const value = readValue(bytes, offset, valueType), before = old ? readValue(old.bytes, offset, valueType) : null;
        if (rule === 'equal') return value === comparison;
        if (rule === 'changed') return !Object.is(value, before);
        if (rule === 'same') return Object.is(value, before);
        if (rule === 'increased') return value > before;
        if (rule === 'decreased') return value < before;
        return true;
      });
      previous = old; snapshot = { bytes: bytes.slice(), address: start, type: valueType, owner, at: new Date().toLocaleTimeString() }; candidates = matched;
      provenance.textContent = `${snapshot.at} · PID ${owner.pid} · ${addressText(start)}–${addressText(start + count - 1)} · ${count.toLocaleString()} captured bytes. Snapshots update only on scan.`;
      message.textContent = `${matched.length.toLocaleString()} matching addresses${matched.length > 200 ? ' · showing the first 200; narrow again to reduce results' : ''}. Next scan compares against this capture. Reset to change the range or type.`;
      renderResults(); stringsDirty = true; renderStrings();
    } catch (error) { if (ticket === request && isCurrent(owner)) message.textContent = `Scan failed: ${error.message || error}${old ? ' Previous results retained.' : ' No capture was saved.'}`; }
    finally { if (ticket === request) { busy = false; update(); } }
  });
  function renderStrings() {
    if (!snapshot) return;
    if (!isCurrent(snapshot.owner)) { clear(); update(); return; }
    const min = Number(minimum.value);
    if (!Number.isSafeInteger(min) || min < 4 || min > 256) { stringMessage.textContent = 'Minimum length must be 4–256 characters.'; return; }
    const bytes = snapshot.bytes, wide = encoding.value === 'utf16', step = wide ? 2 : 1, query = filter.value.toLowerCase();
    const rows = []; let matches = 0;
    // Check both UTF-16 byte alignments: a user-selected range may begin in the
    // middle of a code unit. Display only printable ASCII, never guess encoding.
    for (let parity = 0; parity < step; parity++) {
      let start = -1, text = '';
      const finish = () => {
        if (text.length >= min && text.toLowerCase().includes(query)) {
          matches++;
          if (rows.length < 200) rows.push({ address: snapshot.address + start, length: text.length, text });
        }
        start = -1; text = '';
      };
      for (let offset = parity; offset < bytes.length; offset += step) {
        const value = bytes[offset], printable = value >= 32 && value <= 126 && (!wide || bytes[offset + 1] === 0);
        if (printable) { if (start < 0) start = offset; text += String.fromCharCode(value); }
        else finish();
      }
      finish();
    }
    rows.sort((a, b) => a.address - b.address);
    stringResults.body.replaceChildren(...rows.map(value => {
      const row = node('tr'); row.append(node('td', addressText(value.address)), node('td', String(value.length)), node('td', value.text.length > 1024 ? `${value.text.slice(0, 1024)}…` : value.text)); return row;
    }));
    stringMessage.textContent = `${matches.toLocaleString()} ${wide ? 'UTF-16LE ASCII' : 'ASCII'} strings${matches > 200 ? ' · first 200 shown' : ''} · PID ${snapshot.owner.pid} · ${snapshot.at} · ${bytes.length.toLocaleString()} captured bytes. Text may be cut at range boundaries; display limit is 1,024 characters per string.`;
    stringsDirty = false;
  }
  function update() {
    const current = context();
    if (snapshot && !isCurrent(snapshot.owner)) clear();
    capture.disabled = !current.ready || !current.pid || busy;
    capture.textContent = snapshot ? 'Next scan' : 'First scan';
    reset.disabled = !snapshot && !busy;
    for (const input of [address, length, type, alignment]) input.disabled = busy || Boolean(snapshot);
    condition.disabled = busy; expected.disabled = busy || condition.value !== 'equal';
    for (const option of condition.options) option.disabled = !snapshot && !['all', 'equal'].includes(option.value);
    inspect.disabled = !snapshot || busy;
    const availability = !current.enabled ? 'Enable Debug to inspect memory.' : !current.supported ? 'Guest memory reading is unavailable for this target.' : !current.pid ? 'Pause and select a process in Processes.' : !current.ready ? 'Pause the game and wait for all workers to park before scanning.' : 'Ready. First scan reads only the specified range; values are little endian.';
    if (!busy && !snapshot && availability !== lastAvailability) message.textContent = availability;
    lastAvailability = availability;
    if (stringsDirty && !busy) renderStrings();
  }
  condition.addEventListener('change', update);
  return { update, clear, cancel() { request++; busy = false; }, ids: ['debug-scan', 'debug-strings'] };
}
