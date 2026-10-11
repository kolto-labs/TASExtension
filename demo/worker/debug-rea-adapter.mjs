import { DebugOperationError } from '../public/packages/debug-runtime/runtime.js';

const COMMANDS = Object.freeze({
  'code.disassemble': 'instructions', 'code.decompile': 'decompile', 'code.references': 'xrefs',
});

/**
 * Local analysis companion only. The host resolves its already-owned artifact;
 * requests never select paths, executable names, CLI flags, or shell commands.
 * runCommand(executable, argv, {signal, timeoutMs, maxOutputBytes}) must enforce
 * both bounds and return {exitCode, stdout}; it must invoke without a shell.
 */
export function createReaAnalysisAdapter({ resolveArtifact, runCommand, executable = 'rea', provider = 'ghidra' }) {
  if (typeof resolveArtifact !== 'function' || typeof runCommand !== 'function'
    || typeof executable !== 'string' || !executable || !['ghidra', 'ida', 'hopper'].includes(provider)) {
    throw new TypeError('REA adapter requires a target resolver, bounded command runner, and known provider.');
  }
  return Object.freeze({
    id: 'telos.rea-analysis',
    operations: Object.fromEntries(Object.keys(COMMANDS).map(operation => [operation, {
      source: 'static-analysis', unit: 'binary-address', provider,
    }])),
    async execute(operation, params, { session, signal }) {
      const command = COMMANDS[operation];
      if (!command) throw new DebugOperationError('DEBUG_UNSUPPORTED', 'REA does not supply that operation.');
      // Retain wide addresses as hex strings; never round through a JS Number.
      const address = params.address;
      if (typeof address !== 'string' || !/^0x[0-9a-f]{1,32}$/i.test(address)) {
        throw new DebugOperationError('DEBUG_ADDRESS_INVALID', 'Analysis address must be a hexadecimal string.');
      }
      const artifact = await resolveArtifact(session);
      if (!artifact || typeof artifact.path !== 'string' || !artifact.path.startsWith('/')
        || !/^[a-f0-9]{64}$/i.test(artifact.sha256 || '') || artifact.session !== session) {
        throw new DebugOperationError('DEBUG_ARTIFACT_UNAVAILABLE', 'No verified binary is bound to this session.');
      }
      const result = await runCommand(executable, [command, artifact.path, address, '--provider', provider, '--json'], {
        signal, timeoutMs: 60000, maxOutputBytes: 2097152,
      });
      if (result?.exitCode !== 0) throw new DebugOperationError('DEBUG_ANALYSIS_FAILED', 'REA analysis failed.', { provider, operation, exitCode: result?.exitCode ?? null });
      if (typeof result.stdout !== 'string' || new TextEncoder().encode(result.stdout).length > 2097152) {
        throw new DebugOperationError('DEBUG_ANALYSIS_RESULT_TOO_LARGE', 'REA returned an oversized analysis result.');
      }
      let evidence;
      try { evidence = JSON.parse(result.stdout); }
      catch { throw new DebugOperationError('DEBUG_ANALYSIS_INVALID', 'REA returned malformed JSON.'); }
      if (!evidence || typeof evidence !== 'object') throw new DebugOperationError('DEBUG_ANALYSIS_INVALID', 'REA returned no analysis evidence.');
      return { artifactSha256: artifact.sha256.toLowerCase(), address, provider,
        addressSpace: 'binary', evidence };
    },
  });
}
