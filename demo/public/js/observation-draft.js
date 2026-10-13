export const OBSERVATION_DRAFT_SCHEMA = 'kptas.reference-input-draft/v1';

// Draft positions are authoring hypotheses. No game controller consumes them.
export function normalizeDraftPlacement(value, frameCount) {
  if (!value || value.unit !== 'draft-tick' || value.mapping !== 'one-selected-observation-per-draft-tick'
    || value.qualified !== false || !Number.isSafeInteger(value.start_tick) || value.start_tick < 0
    || !Number.isSafeInteger(frameCount) || frameCount < 1
    || !Number.isSafeInteger(value.start_tick + frameCount - 1)
    || typeof value.uncertainty !== 'string' || !value.uncertainty.trim()
    || value.uncertainty.length > 512 || /[\x00-\x1f\x7f]/.test(value.uncertainty)) {
    throw new TypeError('Choose a draft start tick and an uncertainty note of up to 512 characters.');
  }
  return Object.freeze({ unit:value.unit, mapping:value.mapping, qualified:false,
    start_tick:value.start_tick, uncertainty:value.uncertainty });
}

export function unwrapObservationDraft(packet) {
  if (packet?.schema !== OBSERVATION_DRAFT_SCHEMA) return { evidence:packet, placement:null };
  if (packet.read_only !== true || packet.executable_score !== false || packet.replay_qualified !== false
    || packet.physical_inputs_complete !== false || !packet.evidence
    || packet.evidence.schema === OBSERVATION_DRAFT_SCHEMA) {
    throw new TypeError('The observation draft cannot grant execution or replay qualification.');
  }
  return { evidence:packet.evidence, placement:packet.authoring };
}

export function createObservationDraft(evidence, placement, frameCount) {
  return { schema:OBSERVATION_DRAFT_SCHEMA, read_only:true, executable_score:false,
    replay_qualified:false, physical_inputs_complete:false,
    createdAt:new Date().toISOString(), authoring:normalizeDraftPlacement(placement, frameCount),
    provenanceScope:'Embedded observation packet and its reported provenance. Source claims are not independently authenticated; authoring positions do not qualify game-frame or input-sampling alignment.',
    evidence };
}
