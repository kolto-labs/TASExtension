// A state-change timestamp is not the time of every observation it covers.
// Keep the reported rational clock exact; never infer seconds from nominal FPS.
export function describeObservationSourceClock(event, sourceFrameBase = 0) {
  if (!event || !Number.isSafeInteger(event.frame) || !Number.isSafeInteger(sourceFrameBase)
    || !Number.isSafeInteger(event.frame + sourceFrameBase)) return 'Source time unavailable';
  const source = event.source;
  if (!Number.isSafeInteger(source?.decoder_pts) || typeof source.decoder_time_base !== 'string'
    || !/^[1-9][0-9]{0,15}\/[1-9][0-9]{0,15}$/.test(source.decoder_time_base)) {
    return `Source time unavailable at observation state change ${event.frame + sourceFrameBase}`;
  }
  const part = Number.isSafeInteger(source.source_part) && source.source_part >= 0
    ? ` · source part ${source.source_part}` : '';
  return `State-change source time: PTS ${source.decoder_pts} × ${source.decoder_time_base}${part} · observation ${event.frame + sourceFrameBase}. Time between state changes is not inferred.`;
}
