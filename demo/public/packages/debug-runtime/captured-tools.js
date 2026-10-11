import { DebugOperationError } from './runtime.js';

/** Bounded wildcard byte search in an explicit capture; never reads arbitrary host memory. */
export function searchCapturedBytes(bytes, pattern, { baseAddress = 0, limit = 256 } = {}) {
  if (!(bytes instanceof Uint8Array) || bytes.length > 1048576 || typeof pattern !== 'string'
    || !Number.isSafeInteger(baseAddress) || baseAddress < 0 || !Number.isSafeInteger(limit) || limit < 1 || limit > 4096) {
    throw new DebugOperationError('DEBUG_SEARCH_INVALID', 'Invalid captured-memory search.');
  }
  const tokens = pattern.trim().split(/\s+/);
  if (tokens.length < 1 || tokens.length > 256 || tokens.some(token => !/^(?:[0-9a-f]{2}|\?\?)$/i.test(token))) {
    throw new DebugOperationError('DEBUG_PATTERN_INVALID', 'Use hexadecimal bytes or ?? wildcards.');
  }
  const needle = tokens.map(token => token === '??' ? null : parseInt(token, 16));
  const matches = []; let truncated = false;
  const anchor = needle.findIndex(value => value !== null);
  let comparisons = 0;
  for (let offset = 0; offset <= bytes.length - needle.length; offset++) {
    if (anchor >= 0 && bytes[offset + anchor] !== needle[anchor]) continue;
    let matchesNeedle = true;
    for (let index = 0; index < needle.length; index++) {
      if (++comparisons > 16777216) throw new DebugOperationError('DEBUG_SEARCH_BUDGET', 'Search pattern is too broad for this capture; reduce the range or add fixed bytes.');
      if (needle[index] !== null && bytes[offset + index] !== needle[index]) { matchesNeedle = false; break; }
    }
    if (matchesNeedle) {
      if (matches.length === limit) { truncated = true; break; }
      matches.push(baseAddress + offset);
    }
  }
  return { source: 'captured-memory', matches, truncated, scannedBytes: bytes.length };
}

/** Integer watch arithmetic on supplied registers/symbols; deliberately no JS eval. */
export function evaluateIntegerExpression(expression, values = {}) {
  if (typeof expression !== 'string' || expression.length === 0 || expression.length > 512) throw new DebugOperationError('DEBUG_EXPRESSION_INVALID', 'Expression must be 1–512 characters.');
  const tokens = expression.match(/0x[0-9a-f]+|\d+|[a-z_][a-z0-9_]*|<<|>>|[()+\-*/%&|^~]/gi) || [];
  if (tokens.length > 128 || tokens.join('') !== expression.replace(/\s/g, '')) throw new DebugOperationError('DEBUG_EXPRESSION_INVALID', 'Expression contains unsupported syntax.');
  let index = 0;
  const precedence = { '|': 1, '^': 2, '&': 3, '<<': 4, '>>': 4, '+': 5, '-': 5, '*': 6, '/': 6, '%': 6 };
  const bounded = value => {
    if (value < -(1n << 127n) || value >= (1n << 128n)) throw new DebugOperationError('DEBUG_EXPRESSION_RANGE', 'Expression exceeds 128-bit arithmetic.');
    return value;
  };
  function atom(depth) {
    if (depth > 32) throw new DebugOperationError('DEBUG_EXPRESSION_DEPTH', 'Expression nesting is too deep.');
    const token = tokens[index++];
    if (['+', '-', '~'].includes(token)) {
      const value = atom(depth + 1); return bounded(token === '-' ? -value : token === '~' ? ~value : value);
    }
    if (token === '(') { const value = parse(0, depth + 1); if (tokens[index++] !== ')') throw new DebugOperationError('DEBUG_EXPRESSION_INVALID', 'Missing closing parenthesis.'); return value; }
    if (/^(?:0x[0-9a-f]+|\d+)$/i.test(token || '')) return bounded(BigInt(token));
    if (typeof token === 'string' && Object.hasOwn(values, token)) {
      const value = values[token];
      if (typeof value === 'bigint') return bounded(value);
      if (typeof value === 'number' && Number.isSafeInteger(value)) return bounded(BigInt(value));
      if (typeof value === 'string' && /^(?:-?\d+|0x[0-9a-f]+)$/i.test(value) && value.length <= 80) return bounded(BigInt(value));
      throw new DebugOperationError('DEBUG_EXPRESSION_VALUE', 'Expression value is not an exact integer.', { name: token });
    }
    throw new DebugOperationError('DEBUG_EXPRESSION_NAME', 'Unknown register or symbol.', { name: token ?? null });
  }
  function parse(minimum, depth) {
    let left = atom(depth);
    while ((precedence[tokens[index]] ?? -1) >= minimum) {
      const operator = tokens[index++]; const right = parse(precedence[operator] + 1, depth + 1);
      if ((operator === '/' || operator === '%') && right === 0n) throw new DebugOperationError('DEBUG_EXPRESSION_DIVIDE_ZERO', 'Division by zero.');
      if ((operator === '<<' || operator === '>>') && (right < 0n || right > 127n)) throw new DebugOperationError('DEBUG_EXPRESSION_SHIFT', 'Shift must be 0–127.');
      left = bounded(operator === '+' ? left + right : operator === '-' ? left - right : operator === '*' ? left * right
        : operator === '/' ? left / right : operator === '%' ? left % right : operator === '&' ? left & right
        : operator === '|' ? left | right : operator === '^' ? left ^ right : operator === '<<' ? left << right : left >> right);
    }
    return left;
  }
  const value = parse(0, 0);
  if (index !== tokens.length) throw new DebugOperationError('DEBUG_EXPRESSION_INVALID', 'Unexpected expression token.');
  return { decimal: value.toString(), hexadecimal: value < 0n ? `-0x${(-value).toString(16)}` : `0x${value.toString(16)}` };
}

export function createCapturedToolsAdapter() {
  return { id: 'telos.captured-tools', operations: {
    'memory.search': { source: 'captured-memory' }, 'expressions.evaluate': { source: 'captured-memory' },
  }, execute(operation, params) {
    if (operation === 'expressions.evaluate') return evaluateIntegerExpression(params.expression, params.values);
    if (!Array.isArray(params.bytes) || params.bytes.length > 1048576 || params.bytes.some(value => !Number.isInteger(value) || value < 0 || value > 255)) throw new DebugOperationError('DEBUG_CAPTURE_INVALID', 'Capture bytes must be integers from 0 to 255.');
    return searchCapturedBytes(Uint8Array.from(params.bytes), params.pattern, params);
  } };
}
