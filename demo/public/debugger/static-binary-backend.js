import { identifyBinaryHeader } from '../js/binary-identity.js';
import { normalizeTargetIdentity, targetIdentityKey } from './debugger-contract.js';
import { captureStaticBytes } from './static-byte-reader.js';

const OPERATIONS = ['binary.identity', 'binary.sections', 'binary.dependencies', 'binary.bytes'];
const text = value => typeof value === 'string' ? value.slice(0, 2048) : 'Unavailable';
const freezeRows = rows => Object.freeze(rows.map(row => Object.freeze(row)));
const MAX_HASH_BYTES = 128 * 1024 * 1024;
// Only an identified, width-consistent header supplies a declared address
// width. Fat Mach-O and WASM stay unknown; this is not a live VM pointer size.
const HEADER_POINTER_BITS = Object.freeze({ pe32:32, 'pe32+':64, elf32:32, elf64:64, macho32:32, macho64:64 });

async function captureFileHash(file, signal) {
  const check = () => {
    if (signal?.aborted) throw signal.reason || new DOMException('Aborted', 'AbortError');
  };
  check();
  if (file.size > MAX_HASH_BYTES) return { sha256:null, reason:'File exceeds the 128 MiB local hashing limit.' };
  if (typeof globalThis.crypto?.subtle?.digest !== 'function') {
    return { sha256:null, reason:'Web Crypto SHA-256 is unavailable in this context.' };
  }
  try {
    const bytes = await file.arrayBuffer();
    check();
    const digest = await globalThis.crypto.subtle.digest('SHA-256', bytes);
    check();
    return { sha256:Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, '0')).join(''),
      reason:'SHA-256 of the complete selected file, calculated locally.' };
  } catch (error) {
    check();
    return { sha256:null, reason:'The local SHA-256 calculation failed.' };
  }
}

/** Trusted host factory. The uploaded Blob supplies bytes, never handlers. */
export async function createStaticBinaryBackend(file, { session, revision, signal } = {}) {
  if (!(file instanceof Blob) || !Number.isSafeInteger(file.size) || file.size < 4) {
    throw new TypeError('Static binary inspection requires a Blob with a complete header.');
  }
  if (typeof session !== 'string' || !session.length || session.length > 128
    || !Number.isSafeInteger(revision) || revision < 0) {
    throw new TypeError('Static binary inspection requires a bound session and revision.');
  }
  if (signal?.aborted) throw signal.reason || new DOMException('Aborted', 'AbortError');
  // Blob bytes are immutable. The existing parser reads bounded metadata
  // slices rather than allocating or executing the complete executable.
  const identity = await identifyBinaryHeader(file);
  if (signal?.aborted) throw signal.reason || new DOMException('Aborted', 'AbortError');
  // Whole-file identity remains separate from the bounded header captures.
  // It identifies selected bytes, not the executable loaded by a game VM.
  const hash = ['identified', 'ambiguous'].includes(identity.status)
    ? await captureFileHash(file, signal)
    : { sha256:null, reason:'The file header was not recognized.' };
  if (signal?.aborted) throw signal.reason || new DOMException('Aborted', 'AbortError');
  const filename = typeof file.name === 'string'
    ? file.name.split(/[\\/]/).pop().replace(/[\x00-\x1f\x7f]/g, '').slice(0, 256).trim() || null : null;
  const target = normalizeTargetIdentity({ executable:filename,
    format:identity.format, architecture:identity.architecture, osAbi:identity.abi,
    byteOrder:identity.byteOrder, pointerBits:identity.status === 'identified'
      ? HEADER_POINTER_BITS[identity.format] ?? null : null,
    sha256:hash.sha256, profile:null });
  const targetKey = targetIdentityKey(target);
  const adapterManifest = target.format
    ? Object.freeze({ schema:'telos.debug-target-adapter/v1', id:'telos.static-binary', version:'1.0.0', mode:'file-inspection',
      api:Object.freeze({ id:'telos.game-adapter', major:1, minor:0 }),
      targets:Object.freeze([Object.freeze({ format:target.format })]),
      requires:Object.freeze(['binary.identity']), interprets:Object.freeze(['File headers and metadata; no runtime compatibility']) }) : null;
  const rows = Object.freeze({
    'binary.identity':freezeRows([
      ['Inspection', 'File metadata and local identity'], ['Filename', filename || 'Unavailable'],
      ['File bytes', String(file.size)], ['Header status', text(identity.status)],
      ['Format', text(identity.format)], ['Architecture', text(identity.architecture)],
      ['Declared architectures', Array.isArray(identity.architectures) && identity.architectures.length
        ? identity.architectures.slice(0, 16).map(text).join(', ') : 'Unavailable'],
      ['OS ABI', text(identity.abi)], ['Byte order', text(identity.byteOrder)],
      ['Pointer width', target.pointerBits === null ? 'Unavailable' : `${target.pointerBits} bits`],
      ['Reason', text(identity.reason)],
      ['Section capture', text(identity.layout?.status)],
      ['Section scope', text(identity.layout?.scope)],
      ['Section reason', text(identity.layout?.reason)],
      ['Dependency capture', text(identity.metadata?.status)],
      ['Dependency scope', text(identity.metadata?.scope)],
      ['Dependency reason', text(identity.metadata?.reason)],
      ['Executable hash', hash.sha256 || 'Unavailable'], ['Hash scope', hash.reason],
      ['Runtime compatibility', 'Not qualified'],
    ]),
    'binary.sections':freezeRows((identity.layout?.entries || []).slice(0, 512).map(entry =>
      [text(entry.name), text(entry.kind), text(entry.address), text(entry.offset),
        text(entry.fileSize), text(entry.memorySize), text(entry.permissions)])),
    'binary.dependencies':freezeRows((identity.metadata?.dependencies || []).slice(0, 256).map(name =>
      [text(name), text(identity.metadata?.scope)])),
  });
  const available = Object.freeze({
    'binary.identity':true,
    'binary.sections':identity.layout?.status === 'available',
    'binary.dependencies':identity.metadata?.status === 'available',
    'binary.bytes':true,
  });
  let active = true;
  const declarations = Object.freeze(Object.fromEntries(OPERATIONS.map(operation => [operation,
    Object.freeze({ source:'static-analysis', scope:'target',
      state:available[operation] ? 'available' : 'not-qualified',
      reason:available[operation] ? operation === 'binary.bytes'
        ? 'Bounded immutable file byte capture; process memory and execution are not established.'
        : 'Captured file metadata; runtime compatibility is not established.'
        : 'The header parser did not capture this complete metadata table.' })])));
  const handlers = Object.freeze(Object.fromEntries(OPERATIONS.map(operation => [operation,
    async ({ payload, context, signal:requestSignal } = {}) => {
      if (!active) throw new Error('Static binary inspection was closed.');
      if (requestSignal?.aborted) throw requestSignal.reason || new DOMException('Aborted', 'AbortError');
      if (!context || context.session !== session || context.revision !== revision
        || targetIdentityKey(context.target) !== targetKey || context.operation !== operation
        || context.inspection !== true || context.evidenceSource !== 'static-analysis'
        || context.capability?.scope !== 'target' || context.capability?.state !== 'available') {
        throw new Error('Static binary request does not match its bound inspection target.');
      }
      if (operation === 'binary.bytes') return captureStaticBytes(file, payload, () => {
        if (!active) throw new Error('Static binary inspection was closed.');
        if (signal?.aborted) throw signal.reason || new DOMException('Aborted', 'AbortError');
        if (requestSignal?.aborted) throw requestSignal.reason || new DOMException('Aborted', 'AbortError');
      });
      if (!payload || typeof payload !== 'object' || Array.isArray(payload) || Reflect.ownKeys(payload).length) {
        throw new TypeError('Static binary inspection takes no process or execution parameters.');
      }
      if (!available[operation]) throw new Error(declarations[operation].reason);
      return Object.freeze({ scope:'target', captured:true, rows:rows[operation],
        ...(operation === 'binary.sections' ? {
          semantics:'declared-file-ranges', fileSize:file.size, headerStatus:identity.status,
        } : {}) });
    }])));
  return Object.freeze({
    target, session, revision, adapterManifest, declarations, handlers,
    manifest:Object.freeze({ schema:'telos.debug-backend/v1', id:'telos.static-binary',
      version:'1.0.0', api:Object.freeze({ id:'telos.debug-backend', major:1, minor:0 }),
      adapters:Object.freeze(['telos.static-binary']), operations:Object.freeze([...OPERATIONS]) }),
    close() { active = false; },
  });
}
