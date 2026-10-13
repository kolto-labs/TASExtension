const U32_LIMIT = 0x100000000n;
const HEX64 = /^0x[0-9a-f]{16}$/;

/** Backing-file metadata only; no executable identity or module-table claim. */
export function normalizeMappedFileCapture(value, { pid, address }) {
  if (!value || value.pid !== pid || value.address !== address
    || !Number.isSafeInteger(pid) || pid < 1 || pid > 0xffffffff
    || !Number.isSafeInteger(address) || address < 0 || address > 0xffffffff
    || value.captured !== true || value.scope !== 'process'
    || value.semantics !== 'mapped-file-address'
    || !Number.isSafeInteger(value.start) || value.start < 0 || value.start > 0xffffffff
    || typeof value.length !== 'string' || !HEX64.test(value.length)
    || (value.fileOffset !== null && (typeof value.fileOffset !== 'string' || !HEX64.test(value.fileOffset)))
    || typeof value.path !== 'string' || !value.path.length || value.path.length > 4096
    || /[\x00-\x1f\x7f]/.test(value.path)
    || new TextEncoder().encode(value.path).byteLength > 4096) {
    throw new TypeError('Invalid paused backing-file capture.');
  }
  const start = BigInt(value.start), length = BigInt(value.length), requested = BigInt(address);
  if (length < 1n || start + length > U32_LIMIT || requested < start || requested >= start + length) {
    throw new RangeError('The backing-file capture does not contain the requested address.');
  }
  return Object.freeze({ pid, address, scope:'process', captured:true,
    semantics:'mapped-file-address', start:value.start, length:value.length,
    fileOffset:value.fileOffset, path:value.path });
}
