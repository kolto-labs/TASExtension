# Game debugger extension contract

**Status:** proposed contract for a future compatible revision of the browser debugger session API. This document defines an extension boundary; it does not claim that arbitrary game backends or debugger plugins are implemented.

## Boundary

Debugger support is the intersection of four independently owned layers:

1. **Transport** proves that a request can reach the selected process or VM and return a bounded, session-matched response.
2. **Runtime backend** owns execution and process operations such as pause, step, thread enumeration, memory, registers and snapshots.
3. **Target adapter** identifies the executable/build and may interpret backend observations as symbols, source locations or game-specific state. It cannot supply an execution primitive the backend lacks.
4. **Policy** restricts operations for the current user, run tier, trust decision and safety state.

Effective capability is the intersection of those layers. An extension can narrow support and add interpretation; it cannot grant a capability that the transport, backend, target or policy does not provide. The static simulator remains a separate source and must never be presented as live game state.

## Session and target identity

Keep the current `telos-runtime-session/v1` wire shape readable. When this contract is adopted, introduce a new schema major rather than changing the meaning of existing fields. A negotiated debugger session descriptor should contain:

```json
{
  "schema": "telos.debug-session/v2",
  "session": "session-id",
  "revision": 7,
  "runtimeProtocol": "telos-wasm-vm-control/v1",
  "backend": { "id": "boxedwine-wasm", "version": "runtime-release-id" },
  "target": {
    "profile": "k1",
    "executable": "swkotor.exe",
    "sha256": null,
    "format": "pe32",
    "architecture": "x86",
    "osAbi": "windows-32",
    "pointerBits": 32,
    "byteOrder": "little"
  },
  "adapter": { "id": "telos.kotor", "version": "1.0.0", "api": "telos.game-adapter/v1" },
  "boundary": { "kind": "render-present", "unit": "render-present" },
  "source": "live-runtime",
  "operations": {}
}
```

Unknown values stay `null` or absent. A friendly profile name is not a build identity: target-specific symbol or semantic claims require the executable digest and an adapter match that names the required identity fields. `render-present` describes a renderer boundary and must not be relabeled as a game tick or deterministic frame.

## Operation capability values

Use namespaced, stable operation IDs such as `execution.pause`, `execution.step`, `threads.list`, `memory.read`, `registers.read`, `disassembly.read`, `breakpoints.software`, `snapshots.capture` and `snapshots.restore`. Each layer reports one of these states for an operation:

- `available`: this layer has an active implementation for this session and target.
- `unsupported`: this layer has no implementation for the operation.
- `not-qualified`: an implementation may exist, but the selected backend/target combination has not passed its qualification gate.
- `unavailable`: support may exist, but is temporarily absent (for example, no live session or a required runtime export is missing).

An operation is `available` only when every required layer explicitly reports `available`. Missing or malformed layer data fails closed. Preserve the reason, owner and evidence identity with each report; do not infer thread lists from a thread count, memory writes from memory reads, snapshots from pause/step, or restore from replay. Report data with its provenance (`live-runtime`, `recorded-journal` or `simulator`), session ID, target identity, revision and execution boundary.

The current BoxedWine/browser integration can honestly advertise input acknowledgements, pause/resume, render-present stepping, bounded worker counts, and bounded guest-memory reads only when its handshake and safety checks confirm them. It does not currently provide live register reads, disassembly, breakpoints, coherent snapshots or restore. This contract does not change those support facts.

## Telos Core package boundary

Telos Core is the proposed standalone installation and extension-management layer. It manages independently versioned high-level packages through a versioned Core API. TAS and Lua are separate, optional packages: Core must start and manage profiles without either one, TAS may be installed without Lua, and community additions may depend on Core alone or declare an explicit TAS/Lua API dependency. A package's version, Core API range, TAS API range, Lua host API range, debugger wire schema and runtime backend version are separate compatibility axes; one version number must not stand in for another.

The default extension API is declarative and high-level: named capabilities, bounded observations/events, commands admitted by their owning service, panels registered with the host, and extension-scoped storage. Extensions cannot install a proxy DLL, patch the process, add hooks, access arbitrary pointers, or take over process lifetime. TAS actions continue through the TAS provider and its existing serialized session/branch boundary. Lua uses an explicit Lua host API; this contract does not claim that an eventual Lua engine is sandboxed.

Native hooks belong to a separately identified privileged adapter. The adapter must be allowlisted for the exact target/runtime profile and undergo its own compatibility and safety qualification. It does not become the extension default because it is listed in a package manifest. Native code runs with game-process privileges and can crash or alter the game, so the Core package manager must make that boundary explicit and refuse activation without the required host policy. The generic extension SDK exposes no hook API.

The current KPHarness browser slice provides a bundled package lifecycle in `demo/public/js/telos-core.js`, a dockable manager in `demo/public/js/extensions.js`, and a separate TAS manifest in `demo/public/packages/tas/manifest.js`. TAS can be installed, enabled, disabled, and uninstalled in workspace state; dependency closure and activation ordering are handled by Core. Deactivation stops score playback and recording, releases inputs, gates TAS commands and shortcuts, and hides its panels while retaining scores and layouts. The game session and debugger remain mounted. This is an initial Core/API boundary: the manager does not download external code, and the TAS implementation and UI wiring still live in `app.js` and are bundled with the application. See `KPTASInterface/docs/design/core-package-contract.md` for the implemented browser boundary and remaining extraction work.

Remote package acquisition, external executable manifests, and third-party code activation remain out of scope for this slice; they need an explicit trust and isolation design. `game-profiles.js` registers game-install profiles; `OriginalGame` owns the browser game session and its origin/session-checked iframe bridge; `debugger-workbench.js` consumes that session's capability descriptor. Those runtime boundaries do not themselves grant extension capabilities. The backend negotiation and debugger manifest sections below remain proposed contracts.

## Debugger extension manifest

Backend and game-adapter releases use distinct identities and API versions. A package manifest is declarative metadata, not code execution authority:

```json
{
  "schema": "telos.debug-extension/v1",
  "id": "publisher.adapter-name",
  "version": "1.2.0",
  "kind": "game-adapter",
  "api": { "id": "telos.game-adapter", "major": 1, "minor": 0 },
  "targets": [{
    "format": "pe32",
    "architecture": "x86",
    "osAbi": "windows-32",
    "profile": "k1",
    "executableSha256": ["...optional exact builds..."]
  }],
  "requires": ["memory.read"],
  "interprets": ["symbols", "source-map"],
  "permissions": ["read-debug-observations"]
}
```

Reject an unknown manifest major, incompatible adapter API major, invalid identity, target mismatch or unknown privileged permission before activation. The adapter must declare exact target fields it needs; an omitted digest means a profile-level match only and cannot support exact-build claims. An Exchange/catalog record may describe compatibility but cannot authorize a runtime operation or provide executable startup instructions.

Do not expose arbitrary native pointers, hook installation, memory writes or restore through the generic adapter API. Native code executes with game-process privileges and is not sandboxed. A future privileged native adapter needs a separately reviewed ABI and qualification gate. Read/write bounds, timeouts, cancellation, per-session scoping and disposal are part of the backend contract. Mutating TAS requests must continue through the existing serialized session/branch admission boundary.

### KPM exclusion from extension-managed launches

The current KPM exclusion applies at the extension-host boundary: no package, debugger backend, target adapter, catalog record or user extension may load or re-enable KPM, request its hook chain, or override the host's refusal. Stasis must refuse launch when KPM presence is reliably established. The refusal path must not load, patch, modify, disable, replace or delete KPM files. If an installation cannot be classified reliably, report the KPM check as unknown; do not claim the exclusion gate passed. This is host policy, never a manifest permission that a publisher can grant itself.

The older Telos Core product plan described KPM as the lower-level bootstrap under Core. That conflicts with the later KPM exclusion if both describe the same launch. The current contract applies the exclusion and does not permit a KPM bootstrap; the earlier KPM-hosted Core direction remains unresolved as product history and must not be implemented silently. This contract does not qualify any KPM ABI or integration.

## Versioning and negotiation

Keep four versions independent: wire protocol, runtime backend, target adapter API, and extension release. During session initialization, the host selects compatible API majors, binds the adapter to an identified target, and obtains capability reports from transport/backend/adapter/policy. Additive minor fields may be ignored by older readers; a new required behavior or changed field meaning requires a new major. Revisions increase monotonically within one session; stale replies and observations are discarded. Session changes invalidate prior operation results.

This division follows established debugger protocol practice: negotiate capabilities before issuing operations, treat omitted capabilities as unsupported, and describe architecture and register layout as target metadata rather than game semantics ([Debug Adapter Protocol initialization](https://microsoft.github.io/debug-adapter-protocol/overview.html), [GDB target descriptions](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Target-Description-Format.html)). The extension manifest follows the declarative contribution model used by debugger hosts such as VS Code, while keeping admission and runtime capability checks owned by this host ([VS Code debugger extensions](https://code.visualstudio.com/api/extension-guides/debugger-extension)).

## Mapping to current KPHarness boundaries

The browser iframe bridge is the transport; the selected BoxedWine VM implementation is the backend; `game-profiles.js` provides coarse K1/K2 installation profiles; no general-purpose game adapter exists yet. The native `KPTASInterface` shim is a K1-specific injected TAS backend and its design contract owns early clock/hook state and strict run-tier admission. `kptas/supervisor.py` owns process lifetime, serialized step actions and durable holorecord outputs. A future Telos Core facade may broker declared services, but it must not import TAS internals or bypass the supervisor's serialization and admission boundary. It manages separately versioned TAS, Lua and other high-level packages; a privileged native adapter is a distinct opt-in host integration. The debugger wire protocol is independent from those package APIs and cannot make unsupported runtime debugging operations appear available.

The current repository's KPM bootstrap, readiness callbacks and hook-ownership ABI remain unverified. This contract names no KPM-specific ABI and forbids extension-managed KPM activation. It leaves the older KPM-hosted Telos Core proposal unresolved and does not present that proposal as a supported architecture.
