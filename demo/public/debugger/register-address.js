// Preparing a lookup does not establish that a saved value is a pointer.
// The current mapping operation accepts exact 32-bit numeric addresses only.
export function savedRegisterAddress(value, pointerBits) {
  if (pointerBits !== 32 || typeof value !== 'string' || !/^0x[0-9a-f]{1,32}$/i.test(value)) return null;
  const address = BigInt(value);
  return address <= 0xffffffffn ? Number(address) : null;
}
