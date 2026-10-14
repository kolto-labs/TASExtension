export const DEBUG_EXECUTION_COMPLETION_SCHEMA = 'telos.debug-execution-completion/v1';
const ACTIONS = Object.freeze({ 'execution.pause':'pause', 'execution.resume':'resume', 'execution.step':'step' });
const boundedText = value => typeof value === 'string' && value.length > 0 && value.length <= 256 && value.trim() === value;

/** Validate an owner-authored completion; dispatch success alone is insufficient. */
export function validateExecutionCompletion(value, { operation, session, owner, unit, requestId } = {}) {
  if (!Object.hasOwn(ACTIONS, operation) || !boundedText(session) || !boundedText(owner)
    || value?.schema !== DEBUG_EXECUTION_COMPLETION_SCHEMA
    || !boundedText(requestId) || value.requestId !== requestId
    || value.operation !== operation || value.session !== session
    || value.outcome !== 'completed' || value.acknowledged !== true
    || value.evidence?.source !== 'live-runtime' || value.evidence.owner !== owner
    || !boundedText(value.evidence.id)) {
    throw new TypeError('The adapter did not provide matching execution completion evidence.');
  }
  if (operation === 'execution.step') {
    if (!boundedText(unit) || value.unit !== unit) throw new TypeError('Execution completion does not match the requested step unit.');
  } else if (value.unit !== null) throw new TypeError('Pause and resume completion must not claim a step unit.');
  return value;
}

/** Convert the existing guest receipt only after checking its VM provenance. */
export function guestExecutionCompletion(receipt, { operation, session, owner, requestId } = {}) {
  const action = Object.hasOwn(ACTIONS, operation) ? ACTIONS[operation] : null;
  if (!action || receipt?.acknowledged !== true || receipt.action !== action
    || receipt.unit !== 'render-present' || receipt.command?.source !== 'vm-control'
    || receipt.command.action !== action || receipt.command.outcome !== 'completed'
    || !Number.isSafeInteger(receipt.request) || receipt.request < 1
    || receipt.command.request !== receipt.request) {
    throw new TypeError('The guest did not provide matching VM command completion evidence.');
  }
  return validateExecutionCompletion({
    schema:DEBUG_EXECUTION_COMPLETION_SCHEMA, session, operation, requestId,
    acknowledged:true, outcome:'completed', unit:action === 'step' ? 'render-present' : null,
    evidence:{ owner, source:'live-runtime', id:`vm-control-${receipt.request}` },
    guestReceipt:receipt,
  }, { operation, session, owner, unit:'render-present', requestId });
}
