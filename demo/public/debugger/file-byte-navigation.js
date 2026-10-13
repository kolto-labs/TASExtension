/** Parse file offsets, never process addresses. Reject partial numeric input. */
export function parseFileOffset(value) {
  const text = typeof value === 'string' ? value.trim() : '';
  if (!/^(?:[0-9]+|0x[0-9a-f]+)$/i.test(text)) return NaN;
  const offset = Number(text);
  return Number.isSafeInteger(offset) && offset >= 0 ? offset : NaN;
}

/** Prepare an adjacent window from a completed capture, without reading it. */
export function adjacentFileRange(capture, direction) {
  if (!capture || ![-1, 1].includes(direction)) return null;
  const { offset, length, fileSize } = capture;
  if (!Number.isSafeInteger(fileSize) || fileSize < 1
    || !Number.isSafeInteger(offset) || offset < 0
    || !Number.isSafeInteger(length) || length < 1 || length > 4096
    || offset > fileSize - length) return null;
  if (direction < 0) {
    if (offset === 0) return null;
    const count = Math.min(length, offset);
    return Object.freeze({ offset:offset - count, length:count });
  }
  const next = offset + length;
  if (next >= fileSize) return null;
  return Object.freeze({ offset:next, length:Math.min(length, fileSize - next) });
}
