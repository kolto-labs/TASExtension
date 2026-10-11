/* Backend-neutral debugger identity and capability negotiation.
 * This module describes support; it deliberately contains no execution hooks.
 */

export const DEBUG_CAPABILITY_RESOLUTION_SCHEMA = 'telos.debug-capability-resolution/v1';
export const DEBUG_CAPABILITY_REPORT_SCHEMA = 'telos.debug-capability-report/v1';
export const DEBUG_TARGET_ADAPTER_SCHEMA = 'telos.debug-target-adapter/v1';

export const DEBUG_CAPABILITY_LAYERS = Object.freeze([
  'transport', 'backend', 'target', 'policy',
]);
export const DEBUG_CAPABILITY_STATES = Object.freeze([
  'available', 'unsupported', 'not-qualified', 'unavailable',
]);
export const DEBUG_EVIDENCE_SOURCES = Object.freeze([
  'live-runtime', 'recorded-journal', 'simulator',
]);

// Stable cross-host vocabulary. Legacy backend names remain backend details.
export const DEBUG_OPERATIONS = Object.freeze({
  'input.apply':'Input', 'execution.pause':'Pause', 'execution.resume':'Resume',
  'execution.step':'Step', 'execution.seek':'Seek', 'execution.rewind':'Rewind',
  'threads.count':'Thread counts', 'threads.list':'Thread list',
  'memory.read':'Memory read', 'registers.read':'Registers',
  'code.disassemble':'Disassembly', 'breakpoints.manage':'Breakpoints',
  'state.snapshot':'Snapshot', 'state.restore':'Restore',
});

/** Display only: absent qualification is unknown, never an execution grant. */
export function debuggerQualificationView({session, revision, target, reports} = {}) {
  let resolution = null;
  let error = null;
  if (reports != null) {
    try { resolution = resolveDebuggerCapabilities({session, revision, target, reports}); }
    catch (cause) { error = cause instanceof Error ? cause.message : 'Invalid capability reports.'; }
  }
  return freeze({
    schema:'telos.debug-qualification-view/v1', session:session ?? null,
    revision:Number.isSafeInteger(revision) && revision >= 0 ? revision : null,
    target:resolution?.target ?? null, boundary:resolution?.boundary ?? null,
    source:'capability-intersection', reportErrors:resolution?.reportErrors ?? {}, error,
    operations:Object.fromEntries(Object.entries(DEBUG_OPERATIONS).map(([id, label]) => [id, {
      label, ...(resolution?.operations[id] ?? {
        state:error ? 'unavailable' : 'unknown', unit:null,
        reason:error || 'No owner-authored qualification evidence.', layers:{},
      }),
    }])),
  });
}

const ID = /^[a-z0-9][a-z0-9.-]{0,127}$/;
const OPERATION_ID = /^[a-z][a-z0-9]*(?:\.[a-z][a-z0-9-]*){1,7}$/;
const SHA256 = /^[a-f0-9]{64}$/i;
const TARGET_FIELDS = Object.freeze([
  'profile', 'executable', 'sha256', 'format', 'architecture', 'osAbi',
  'pointerBits', 'byteOrder',
]);
const TARGET_STRING_FIELDS = Object.freeze([
  'profile', 'executable', 'format', 'architecture', 'osAbi', 'byteOrder',
]);
const MAX_REPORTS = 4;
const MAX_OPERATIONS = 256;
const MAX_ADAPTERS = 512;

function record(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function boundedText(value, label, max = 256) {
  if (typeof value !== 'string' || !value.trim() || value.length > max) {
    throw new TypeError(`${label} must be a non-empty string of at most ${max} characters.`);
  }
  return value.trim();
}

function integer(value, label, maximum = Number.MAX_SAFE_INTEGER) {
  if (!Number.isSafeInteger(value) || value < 0 || value > maximum) {
    throw new TypeError(`${label} must be a bounded non-negative integer.`);
  }
  return value;
}

function freeze(value) {
  if (Array.isArray(value)) return Object.freeze(value.map(freeze));
  if (!record(value)) return value;
  return Object.freeze(Object.fromEntries(Object.entries(value).map(([key, item]) => [key, freeze(item)])));
}

/** Normalize only declared target identity fields; never infer a profile from a filename. */
export function normalizeTargetIdentity(value) {
  if (!record(value)) throw new TypeError('Target identity must be an object.');
  const target = {};
  for (const field of TARGET_STRING_FIELDS) {
    const item = value[field] ?? null;
    if (item !== null && (typeof item !== 'string' || !item.trim() || item.length > 256)) {
      throw new TypeError(`Target ${field} must be null or a bounded non-empty string.`);
    }
    target[field] = item === null ? null : item.trim();
  }
  const digest = value.sha256 ?? null;
  if (digest !== null && (typeof digest !== 'string' || !SHA256.test(digest))) {
    throw new TypeError('Target sha256 must be null or a 64-character hexadecimal digest.');
  }
  target.sha256 = digest === null ? null : digest.toLowerCase();
  const pointerBits = value.pointerBits ?? null;
  if (pointerBits !== null && ![8, 16, 32, 64, 128].includes(pointerBits)) {
    throw new TypeError('Target pointerBits must be null or a supported pointer width.');
  }
  target.pointerBits = pointerBits;
  return freeze(target);
}

export function targetIdentityKey(value) {
  const target = normalizeTargetIdentity(value);
  return JSON.stringify(TARGET_FIELDS.map(field => target[field]));
}

function normalizeBoundary(value) {
  if (value === null || value === undefined) return freeze({ kind: null, unit: null });
  if (!record(value)) throw new TypeError('Execution boundary must be an object or null.');
  const kind = value.kind ?? null;
  const unit = value.unit ?? null;
  for (const [name, item] of [['kind', kind], ['unit', unit]]) {
    if (item !== null && (typeof item !== 'string' || !item.trim() || item.length > 80)) {
      throw new TypeError(`Execution boundary ${name} must be null or a bounded string.`);
    }
  }
  return freeze({ kind:kind?.trim() ?? null, unit:unit?.trim() ?? null });
}

function normalizeOperationReport(operation, reportLayer, operationId) {
  if (!record(operation) || !DEBUG_CAPABILITY_STATES.includes(operation.state)) {
    throw new TypeError(`Invalid capability state reported by ${reportLayer}.`);
  }
  const owner = boundedText(operation.owner, 'Capability owner', 128);
  const reason = boundedText(operation.reason, 'Capability reason', 512);
  const evidenceId = boundedText(operation.evidenceId, 'Capability evidenceId', 256);
  const unit = operation.unit == null ? null : boundedText(operation.unit, 'Capability unit', 80);
  if (operationId === 'execution.step' && operation.state === 'available' && unit === null) {
    throw new TypeError('Available execution.step must name its actual step unit.');
  }
  return freeze({ state:operation.state, owner, reason, evidenceId, unit });
}

/** Validate one owner-authored, session-scoped capability assertion. */
export function normalizeCapabilityReport(value) {
  if (!record(value) || value.schema !== DEBUG_CAPABILITY_REPORT_SCHEMA
    || !DEBUG_CAPABILITY_LAYERS.includes(value.layer)
    || !DEBUG_EVIDENCE_SOURCES.includes(value.source)
    || !record(value.operations)) {
    throw new TypeError('Invalid debugger capability report envelope.');
  }
  const session = boundedText(value.session, 'Capability report session', 128);
  const revision = integer(value.revision, 'Capability report revision');
  const target = normalizeTargetIdentity(value.target);
  const boundary = normalizeBoundary(value.boundary);
  const entries = Object.entries(value.operations);
  if (entries.length > MAX_OPERATIONS || entries.some(([operationId]) => !OPERATION_ID.test(operationId))) {
    throw new TypeError('Capability report contains an invalid operation ID or too many operations.');
  }
  const operations = Object.fromEntries(entries.map(([id, operation]) => [id, normalizeOperationReport(operation, value.layer, id)]));
  return freeze({ schema:DEBUG_CAPABILITY_REPORT_SCHEMA, layer:value.layer, session, revision, target, boundary, source:value.source, operations });
}

function unavailable(layer, reason) {
  return freeze({ state:'unavailable', owner:layer, reason, evidenceId:'none', unit:null });
}

/**
 * Intersect the four independent owners. Missing, malformed or identity-mismatched
 * reports fail closed. An operation is available only when all four owners agree.
 */
export function resolveDebuggerCapabilities({ session, revision, target, reports } = {}) {
  const normalizedTarget = normalizeTargetIdentity(target);
  const expectedSession = boundedText(session, 'Debugger session', 128);
  const expectedRevision = integer(revision, 'Debugger revision');
  if (!record(reports) || Object.keys(reports).length > MAX_REPORTS) {
    throw new TypeError('Debugger capability reports must be a bounded layer map.');
  }
  const validReports = {};
  const reportErrors = {};
  for (const layer of DEBUG_CAPABILITY_LAYERS) {
    if (reports[layer] == null) {
      reportErrors[layer] = 'No capability report was supplied.';
      continue;
    }
    try {
      const report = normalizeCapabilityReport(reports[layer]);
      if (report.layer !== layer) throw new TypeError('Report layer does not match its registry slot.');
      if (report.session !== expectedSession || report.revision !== expectedRevision
        || targetIdentityKey(report.target) !== targetIdentityKey(normalizedTarget)) {
        throw new TypeError('Capability report belongs to a different session, target, or revision.');
      }
      validReports[layer] = report;
    } catch (error) {
      reportErrors[layer] = error instanceof Error ? error.message : 'Malformed capability report.';
    }
  }

  const ids = new Set(Object.values(validReports).flatMap(report => Object.keys(report.operations)));
  const operations = {};
  for (const id of ids) {
    const layers = Object.fromEntries(DEBUG_CAPABILITY_LAYERS.map(layer => {
      const report = validReports[layer];
      const assertion = report?.operations[id] || unavailable(layer, reportErrors[layer] || 'Operation was not reported by this layer.');
      return [layer, freeze({
        ...assertion,
        provenance:report ? {
          source:report.source,
          session:report.session,
          revision:report.revision,
          target:report.target,
          boundary:report.boundary,
        } : null,
      })];
    }));
    const states = Object.values(layers).map(item => item.state);
    const allAvailable = states.every(state => state === 'available');
    const state = allAvailable ? 'available' :
      states.includes('unsupported') ? 'unsupported' :
      states.includes('not-qualified') ? 'not-qualified' : 'unavailable';
    const units = [...new Set(Object.values(layers).filter(item => item.state === 'available' && item.unit).map(item => item.unit))];
    const unitConflict = units.length > 1;
    operations[id] = freeze({
      state:unitConflict && allAvailable ? 'not-qualified' : state,
      reason:unitConflict ? 'Capability owners disagree on the operation boundary unit.' :
        allAvailable ? 'All required owners report this operation available.' :
        Object.values(layers).find(item => item.state === state)?.reason || 'One or more required owners did not qualify this operation.',
      unit:unitConflict ? null : units[0] || null,
      layers,
    });
  }
  return freeze({
    schema:DEBUG_CAPABILITY_RESOLUTION_SCHEMA,
    session:expectedSession,
    revision:expectedRevision,
    target:normalizedTarget,
    boundary:validReports.backend?.boundary || normalizeBoundary(null),
    source:'capability-intersection',
    operations,
    reportErrors,
  });
}

function normalizeAdapterManifest(value) {
  if (!record(value) || value.schema !== DEBUG_TARGET_ADAPTER_SCHEMA || !ID.test(value.id)
    || typeof value.version !== 'string' || !/^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$/.test(value.version)
    || !record(value.api) || value.api.id !== 'telos.game-adapter' || value.api.major !== 1
    || !Number.isSafeInteger(value.api.minor) || value.api.minor < 0
    || !Array.isArray(value.targets) || value.targets.length < 1 || value.targets.length > 128
    || !Array.isArray(value.requires) || value.requires.length > 128
    || !Array.isArray(value.interprets) || value.interprets.length > 128) {
    throw new TypeError('Invalid debugger target-adapter manifest.');
  }
  const targets = value.targets.map((constraint) => {
    if (!record(constraint)) throw new TypeError('Target adapter constraints must be objects.');
    const match = {};
    for (const field of TARGET_FIELDS) {
      const candidate = constraint[field];
      if (candidate === undefined || candidate === null) continue;
      if (field === 'sha256') {
        if (!Array.isArray(candidate) || candidate.length < 1 || candidate.length > 64
          || candidate.some(digest => typeof digest !== 'string' || !SHA256.test(digest))) {
          throw new TypeError('Adapter build digests must be a bounded list of SHA-256 values.');
        }
        match.sha256 = Object.freeze([...new Set(candidate.map(item => item.toLowerCase()))].sort());
      } else if (field === 'pointerBits') {
        if (![8, 16, 32, 64, 128].includes(candidate)) throw new TypeError('Invalid adapter pointer width.');
        match.pointerBits = candidate;
      } else {
        match[field] = boundedText(candidate, `Adapter target ${field}`, 256);
      }
    }
    if (!match.format || !match.architecture || !match.osAbi) {
      throw new TypeError('Each target adapter match must declare format, architecture, and OS ABI.');
    }
    return freeze(match);
  });
  const requires = [...new Set(value.requires.map(id => {
    if (typeof id !== 'string' || !OPERATION_ID.test(id)) throw new TypeError('Invalid required capability ID.');
    return id;
  }))];
  const interprets = [...new Set(value.interprets.map(name => boundedText(name, 'Adapter interpretation', 80)))];
  return freeze({
    schema:DEBUG_TARGET_ADAPTER_SCHEMA,
    id:value.id,
    version:value.version,
    api:{ id:'telos.game-adapter', major:1, minor:value.api.minor },
    targets,
    requires,
    interprets,
  });
}

function matchConstraint(constraint, target) {
  let specificity = 0;
  let missingIdentity = false;
  for (const field of TARGET_FIELDS) {
    const expected = constraint[field];
    if (expected === undefined) continue;
    const actual = target[field];
    if (field === 'sha256') {
      if (actual === null) { missingIdentity = true; continue; }
      if (!expected.includes(actual)) return { state:'unmatched', specificity };
      specificity += 8;
    } else {
      if (actual === null) { missingIdentity = true; continue; }
      if (actual !== expected) return { state:'unmatched', specificity };
      specificity += 1;
    }
  }
  return { state:missingIdentity ? 'insufficient-identity' : 'matched', specificity };
}

/** Data-only registry: matching an adapter never executes it or grants operations. */
export class DebugTargetAdapterRegistry {
  #manifests = new Map();

  register(manifest) {
    const normalized = normalizeAdapterManifest(manifest);
    if (this.#manifests.has(normalized.id)) throw new Error(`Target adapter is already registered: ${normalized.id}`);
    if (this.#manifests.size >= MAX_ADAPTERS) throw new RangeError('Target adapter registry capacity reached.');
    this.#manifests.set(normalized.id, normalized);
    return normalized;
  }

  get(id) { return this.#manifests.get(id) || null; }

  list() { return Object.freeze([...this.#manifests.values()]); }

  match(targetValue) {
    const target = normalizeTargetIdentity(targetValue);
    const matches = [];
    let insufficientIdentity = false;
    for (const manifest of this.#manifests.values()) {
      for (const constraint of manifest.targets) {
        const result = matchConstraint(constraint, target);
        if (result.state === 'matched') matches.push({ manifest, specificity:result.specificity });
        if (result.state === 'insufficient-identity') insufficientIdentity = true;
      }
    }
    if (!matches.length) return freeze({
      state:insufficientIdentity ? 'insufficient-identity' : 'unmatched',
      adapter:null,
      reason:insufficientIdentity ? 'More executable identity is required to match an adapter.' : 'No registered adapter matches this target.',
    });
    const best = Math.max(...matches.map(item => item.specificity));
    const finalists = matches.filter(item => item.specificity === best);
    if (finalists.length !== 1) return freeze({ state:'ambiguous', adapter:null, reason:'Multiple equally specific adapters match this target.' });
    const adapter = finalists[0].manifest;
    const exactBuild = adapter.targets.some(constraint => Array.isArray(constraint.sha256)
      && matchConstraint(constraint, target).state === 'matched');
    return freeze({
      state:exactBuild ? 'matched-exact-build' : 'matched-profile',
      adapter,
      exactBuild,
      reason:exactBuild ? 'Executable digest matches an explicit adapter target.' :
        'Profile fields match; exact executable build identity is not established.',
    });
  }
}

/** Check a matched adapter's prerequisites without granting them or activating code. */
export function evaluateAdapterRequirements(targetMatch, effectiveCapabilities) {
  if (!record(targetMatch) || !['matched-profile', 'matched-exact-build'].includes(targetMatch.state)
    || !targetMatch.adapter) {
    return freeze({ state:'unavailable', missing:Object.freeze([]), reason:'No target adapter matches the uploaded binary identity.' });
  }
  const manifest = normalizeAdapterManifest(targetMatch.adapter);
  if (!record(effectiveCapabilities) || effectiveCapabilities.schema !== DEBUG_CAPABILITY_RESOLUTION_SCHEMA
    || !record(effectiveCapabilities.operations)) {
    return freeze({ state:'unavailable', missing:Object.freeze([...manifest.requires]), reason:'No resolved debugger session is available.' });
  }
  const missing = manifest.requires.filter(id => effectiveCapabilities.operations[id]?.state !== 'available');
  return freeze({
    state:missing.length ? 'requirements-unmet' : 'requirements-satisfied',
    missing,
    reason:missing.length ? 'The adapter requires runtime capabilities that are not all available.' :
      'All declared runtime prerequisites are available; this does not activate adapter code.',
  });
}
