const CODES = Object.freeze([0, 0x0500, 0x0501, 0x0502, 0x0503, 0x0504, 0x0505, 0x0506, null]);

// Optional runtime-wide telemetry; invalid data never grants a capability.
export function normalizeGuestGlErrorReplies(value) {
  if (value?.schema !== 'telos.guest-gl-error-replies/v1' || value.scope !== 'runtime'
    || value.semantics !== 'guest-requested-glGetError-replies' || !Array.isArray(value.entries)
    || value.entries.length !== CODES.length) return null;
  let queries = 0;
  for (let index = 0; index < CODES.length; index++) {
    const entry = value.entries[index];
    if (entry?.code !== CODES[index] || !Number.isSafeInteger(entry.count) || entry.count < 0) return null;
    queries += entry.count;
    if (!Number.isSafeInteger(queries)) return null;
  }
  return Object.freeze({ schema:value.schema, scope:'runtime', semantics:value.semantics,
    queries, entries:Object.freeze(value.entries.map(({ code, count }) => Object.freeze({ code, count }))) });
}

export function describeGuestGlErrorReplies(value) {
  const capture = normalizeGuestGlErrorReplies(value);
  if (!capture) return 'Unavailable';
  if (!capture.queries) return 'No guest-requested glGetError replies observed. This does not establish a clean renderer.';
  const errors = capture.entries.slice(1).filter(entry => entry.count)
    .map(entry => `${entry.code === null ? 'Other code' : `0x${entry.code.toString(16).padStart(4, '0')}`}: ${entry.count}`);
  return `${capture.queries} observed replies · ${capture.entries[0].count} returned zero${errors.length ? ` · ${errors.join(' / ')}` : ' · no nonzero replies observed'}. Runtime-wide counters, without process/context attribution; not pixel correctness or error causality.`;
}
