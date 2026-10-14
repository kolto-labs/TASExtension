import { parseFileOffset } from './file-byte-navigation.js';
import { MAX_STATIC_BYTE_READ } from './static-byte-reader.js';

/** Declared table file span only. Column 2 is a virtual address, never used. */
export function declaredFileRange(row, fileSize) {
  if (!Array.isArray(row) || row.length !== 7
    || !Number.isSafeInteger(fileSize) || fileSize < 1
    || typeof row[3] !== 'string' || row[3].length > 32
    || typeof row[4] !== 'string' || !/^[0-9]{1,16}$/.test(row[4])) return null;
  const offset = parseFileOffset(row[3]), declaredLength = Number(row[4]);
  if (!Number.isSafeInteger(offset) || !Number.isSafeInteger(declaredLength)
    || declaredLength < 1 || offset > fileSize - declaredLength) return null;
  return Object.freeze({ offset, length:Math.min(declaredLength, MAX_STATIC_BYTE_READ),
    declaredLength, fileSize });
}
