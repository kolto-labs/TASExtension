export function describeCapturedGlCounter(entry, sampleInterval) {
  if (!entry || !Number.isSafeInteger(entry.index) || entry.index < 0 || entry.index >= 4096
    || !Number.isSafeInteger(entry.count) || entry.count < 1
    || typeof entry.name !== 'string' || !/^[\x20-\x7e]{1,127}$/.test(entry.name)) return 'No captured GL counter selected.';
  const prefix = `${entry.name} · counter ${entry.index} · observed count ${entry.count}.`;
  const timing = entry.timing;
  if (!Number.isSafeInteger(sampleInterval) || sampleInterval < 1 || sampleInterval > 4096
    || !timing || !['samples', 'callerMicros', 'callbackMicros'].every(key => Number.isSafeInteger(timing[key]) && timing[key] >= 0)
    || timing.samples > entry.count) return `${prefix} Sampled timing is unavailable in this capture.`;
  if (!timing.samples) return `${prefix} Zero timing samples reported in this capture.`;
  return `${prefix} ${timing.samples} samples · ${timing.callerMicros} µs caller elapsed · ${timing.callbackMicros} µs callback elapsed · sampling interval ${sampleInterval}. Runtime-wide cumulative observations; elapsed can include waits. Not CPU/GPU time or estimates of all calls.`;
}
