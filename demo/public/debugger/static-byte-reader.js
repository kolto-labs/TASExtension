export const MAX_STATIC_BYTE_READ = 4096;

/** Capture file offsets only. This never reads a VM or executes uploaded bytes. */
export async function captureStaticBytes(file, payload, assertActive) {
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)
    || Reflect.ownKeys(payload).length !== 2
    || !Object.hasOwn(payload, 'offset') || !Object.hasOwn(payload, 'length')) {
    throw new TypeError('File byte capture requires only offset and length.');
  }
  const { offset, length } = payload;
  if (!Number.isSafeInteger(offset) || offset < 0
    || !Number.isSafeInteger(length) || length < 1 || length > MAX_STATIC_BYTE_READ
    || offset > file.size - length) {
    throw new RangeError('Choose a file range of 1–4096 bytes within the selected file.');
  }
  assertActive();
  const bytes = new Uint8Array(await file.slice(offset, offset + length).arrayBuffer());
  assertActive();
  if (bytes.length !== length) throw new Error('The selected file range was incomplete.');
  const rows = [];
  for (let index = 0; index < bytes.length; index += 16) {
    const line = bytes.subarray(index, index + 16);
    rows.push(Object.freeze([
      `0x${(offset + index).toString(16).padStart(8, '0')}`,
      Array.from(line, byte => byte.toString(16).padStart(2, '0')).join(' '),
      Array.from(line, byte => byte >= 32 && byte <= 126 ? String.fromCharCode(byte) : '.').join(''),
    ]));
  }
  return Object.freeze({ scope:'target', captured:true, offset, length,
    semantics:'selected-file-bytes', rows:Object.freeze(rows) });
}
