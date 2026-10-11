// Interpret an existing paused capture. This view never reads or writes runtime memory.
export function createCapturedMemoryInspector() {
  const make = (tag, text = '') => {
    const node = document.createElement(tag);
    node.textContent = text;
    return node;
  };
  const element = make('section');
  element.className = 'dw-value-inspector';
  element.hidden = true;
  element.setAttribute('aria-label', 'Captured memory value inspector');
  element.append(make('h3', 'Interpret captured bytes'));
  const source = make('p');
  source.className = 'dw-note';
  const form = make('form');
  form.className = 'dw-memory-form';
  const controls = make('fieldset');
  controls.append(make('legend', 'Location within this capture'));
  const offsetLabel = make('label', 'Byte offset (decimal or 0x hex)');
  const offsetInput = make('input');
  offsetInput.type = 'text';
  offsetInput.value = '0';
  offsetInput.autocomplete = 'off';
  offsetInput.spellcheck = false;
  offsetLabel.append(offsetInput);
  const endianLabel = make('label', 'Byte order');
  const endianSelect = make('select');
  for (const [value, text] of [['little', 'Little endian'], ['big', 'Big endian']]) {
    const option = make('option', text);
    option.value = value;
    endianSelect.append(option);
  }
  endianLabel.append(endianSelect);
  const inspect = make('button', 'Inspect offset');
  inspect.type = 'submit';
  inspect.className = 'btn small';
  controls.append(offsetLabel, endianLabel, inspect);
  form.append(controls);
  const navigation = make('div');
  navigation.className = 'dw-capture-actions';
  const previous = make('button', 'Previous byte');
  const next = make('button', 'Next byte');
  for (const button of [previous, next]) {
    button.type = 'button';
    button.className = 'btn small';
  }
  navigation.append(previous, next);
  const status = make('p');
  status.className = 'dw-note';
  status.setAttribute('role', 'status');
  const values = make('dl');
  values.className = 'dw-metrics';
  const fields = [
    ['Unsigned 8-bit', 1, 'getUint8'], ['Signed 8-bit', 1, 'getInt8'],
    ['Unsigned 16-bit', 2, 'getUint16'], ['Signed 16-bit', 2, 'getInt16'],
    ['Unsigned 32-bit', 4, 'getUint32'], ['Signed 32-bit', 4, 'getInt32'],
    ['Unsigned 64-bit', 8, 'getBigUint64'], ['Signed 64-bit', 8, 'getBigInt64'],
    ['Float 32-bit', 4, 'getFloat32'], ['Float 64-bit', 8, 'getFloat64'],
  ].map(([label, width, method]) => {
    const row = make('div');
    const value = make('dd');
    row.append(make('dt', label), value);
    values.append(row);
    return { width, method, value };
  });
  const scope = make('p', 'Values interpret the same starting byte. They do not identify game fields or follow pointers. Read memory again to refresh the capture.');
  scope.className = 'dw-note';
  element.append(source, form, navigation, status, values, scope);

  let capture = null;
  let offset = 0;
  const render = () => {
    element.hidden = !capture;
    if (!capture) {
      source.textContent = '';
      status.textContent = '';
      for (const field of fields) field.value.textContent = '';
      return;
    }
    const { bytes, address, pid, at } = capture;
    const remaining = bytes.length - offset;
    const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
    const little = endianSelect.value === 'little';
    source.textContent = `Captured ${at} · PID ${pid} · ${bytes.length} bytes. Default byte order is little endian; select the order appropriate for the target.`;
    status.textContent = `Address 0x${(address + offset).toString(16).padStart(8, '0')} · offset ${offset} · ${remaining} bytes remaining · ${little ? 'little' : 'big'} endian.`;
    previous.disabled = offset === 0;
    next.disabled = remaining <= 1;
    for (const { width, method, value } of fields) {
      if (remaining < width) {
        value.textContent = `Needs ${width} bytes; ${remaining} remain`;
      } else {
        const decoded = view[method](offset, little);
        value.textContent = Object.is(decoded, -0) ? '-0' : String(decoded);
      }
    }
  };
  form.addEventListener('submit', event => {
    event.preventDefault();
    if (!capture) return;
    const text = offsetInput.value.trim();
    const requested = /^(?:0x[0-9a-f]+|\d+)$/i.test(text) ? Number(text) : NaN;
    if (!Number.isSafeInteger(requested) || requested < 0 || requested >= capture.bytes.length) {
      offsetInput.setAttribute('aria-invalid', 'true');
      status.textContent = `Enter an offset from 0 to ${capture.bytes.length - 1}, in decimal or with a 0x prefix. Values below still show offset ${offset}.`;
      offsetInput.focus();
      return;
    }
    offsetInput.removeAttribute('aria-invalid');
    offset = requested;
    render();
  });
  const move = delta => {
    if (!capture || offset + delta < 0 || offset + delta >= capture.bytes.length) return;
    offset += delta;
    offsetInput.value = String(offset);
    offsetInput.removeAttribute('aria-invalid');
    render();
  };
  previous.addEventListener('click', () => move(-1));
  next.addEventListener('click', () => move(1));
  endianSelect.addEventListener('change', () => {
    offsetInput.value = String(offset);
    offsetInput.removeAttribute('aria-invalid');
    render();
  });
  return { element, setCapture(value) {
    capture = value;
    offset = 0;
    offsetInput.value = '0';
    offsetInput.removeAttribute('aria-invalid');
    render();
  } };
}
