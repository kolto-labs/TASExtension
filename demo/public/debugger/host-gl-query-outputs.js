const LAYOUT = Object.freeze([
  ['shader-compile', 0], ['shader-compile', 1], ['shader-compile', null],
  ['program-link', 0], ['program-link', 1], ['program-link', null], ['unreadable', null],
]);

export function normalizeHostGlQueryOutputs(value) {
  if (value?.schema !== 'telos.host-gl-query-output-values/v1' || value.scope !== 'runtime'
    || value.semantics !== 'existing-host-query-buffer-values' || value.outputWriteVerified !== false
    || value.objectValidityVerified !== false || !Array.isArray(value.entries) || value.entries.length !== LAYOUT.length) return null;
  let observations = 0;
  for (let index = 0; index < LAYOUT.length; index++) {
    const entry = value.entries[index];
    if (entry?.kind !== LAYOUT[index][0] || entry.value !== LAYOUT[index][1]
      || !Number.isSafeInteger(entry.count) || entry.count < 0) return null;
    observations += entry.count;
    if (!Number.isSafeInteger(observations)) return null;
  }
  return Object.freeze({ schema:value.schema, scope:'runtime', semantics:value.semantics,
    outputWriteVerified:false, objectValidityVerified:false, observations,
    entries:Object.freeze(value.entries.map(({ kind, value, count }) => Object.freeze({ kind, value, count }))) });
}

export function describeHostGlQueryOutputs(value) {
  const capture = normalizeHostGlQueryOutputs(value);
  if (!capture) return 'Unavailable';
  if (!capture.observations) return 'No existing shader/link status query outputs captured. This does not establish shader success.';
  const describe = (label, start) => `${label} buffer values: 0=${capture.entries[start].count}, 1=${capture.entries[start + 1].count}, other=${capture.entries[start + 2].count}`;
  return `${describe('Compile-status', 0)} · ${describe('Link-status', 3)} · unreadable outputs=${capture.entries[6].count}. Output writes and object validity are unverified. No process/context attribution or error causality.`;
}
