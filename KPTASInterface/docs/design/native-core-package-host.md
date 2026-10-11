# Native Telos Core package-host foundation

**Status:** the Core lifecycle API and native TAS adapter are implemented as
source modules. The workspace still must provide the live runtime service and
construct/start the host; TAS is not yet a separately published distribution.

## Purpose and boundary

`kptas.core.package_host` provides the first native package-host foundation for
Telos Core. Core owns package manifest validation, API compatibility, package
enrollment state, dependency ordering, explicit service grants, package-scoped
key/value state, activation, cleanup, and stable error codes. TAS is intended
to be an optional package installed into Core. Core itself must remain usable
without TAS or Lua.

The host accepts Python lifecycle objects only through explicit
`ExtensionHost.register(manifest, lifecycle)` calls made by trusted application
code. A manifest cannot name a module or trigger imports. Native package code
is not sandboxed: after registration it runs inside the application's process
with that process's privileges. This module organizes ownership and provides
a capability-request boundary; it does not make untrusted Python safe.

The host deliberately does not own Qt, the game process, launch/supervisor,
score, journal, input, or restore behavior. Its caller must keep those owners
authoritative and expose narrow, documented services. Native hooks and other
process-privileged adapters are outside the ordinary high-level package API.

## API contract

The manifest uses `telos.extension/v1`, with independent package SemVer and
Core API compatibility (`telos.core.extension`, major 1, minor 0). The
validated fields are stable ID, name, version, description, API, dependencies,
declared permissions, provided feature IDs, and optional default installed /
enabled state. Unknown schema/API majors, malformed identifiers, duplicate
declarations, self-dependency and incompatible API ranges fail with
`HostError.code` values such as `CORE_MANIFEST_INVALID`.

The browser and native hosts share this TAS stable identity and feature
vocabulary; their default enrollment policy and host-specific lifecycle
adapters are separate. Native Core starts with TAS disabled:

```json
{
  "schema": "telos.extension/v1",
  "id": "telos.tas",
  "name": "TAS",
  "version": "1.0.0",
  "api": {"id": "telos.core.extension", "major": 1, "minor": 0},
  "defaultState": {"installed": false, "enabled": false},
  "requires": [],
  "permissions": ["tas.runtime"],
  "provides": ["tas.authoring", "tas.playback", "tas.input-recording", "tas.branch-workflow"],
  "description": "Author inputs, record scores, edit the piano roll, and manage run branches."
}
```

New Core profiles therefore start without TAS. An upgrade policy may retain an
existing user's explicit package choice, but must not silently enable TAS for
a new Core profile.

Register all trusted built-in package implementations and Core services before
calling `ExtensionHost.start()`. Startup activates persisted enabled packages
in dependency order and disables a package whose required dependency is
missing, uninstalled, disabled, or fails activation. One package failure does
not prevent unrelated registered packages from starting. Lifecycle methods
are `activate(context) -> optional disposer` and
`deactivate(context) -> result`; cleanup attempts both deactivation and the
disposer and exposes retryable cleanup failures.

`kptas.core.tas_package.TasPackageLifecycle` consumes the Core-provided
`tas.runtime` service. Activation enables TAS actions and panels, refreshes
keyboard presentation, invalidates the view, and starts TAS rendering.
Deactivation invalidates pending work, flushes edits, pauses the run, stops
recording, releases held inputs, cancels key capture, resets the workspace,
disables its actions and panels, and stops TAS rendering. It attempts every
cleanup operation and reports `TAS_CLEANUP_FAILED` if any operation fails.
The adapter validates the complete runtime contract before acting.
`register_tas_package(host)` explicitly registers this trusted implementation;
the Core app must provide `tas.runtime` before registration and call it before
`host.start()`.

The `context.service(id)` method succeeds only for service IDs declared in the
manifest and provided by Core. `context.storage` namespaces JSON values by
package, limits keys and individual values, and never deletes package data on
uninstall. `change(id, action)` supports `install`, `enable`, `disable`, and
`remove`; install resolves and records the dependency closure, enable activates
it in dependency order, and disable/remove refuse while installed dependents
still require the package. A `ChangeReceipt.persisted == false` means the
state change applies for this host session only. `retry_cleanup(id)` is
required before re-enabling a package whose cleanup did not complete.

`JsonPackageStateStore(path)` stores only Core-owned enrollment and
package-scoped JSON data, with same-directory atomic replacement and bounded
file size. The Core caller chooses the per-user/profile path. The default
`MemoryPackageStateStore` is explicitly visit-only and must not be presented
as durable installation state.

## Implementation receipt

Implemented in this source-only slice:

- `KPTASInterface/kptas/core/package_host.py`: validated manifests and API
  versioning, explicit trusted registration, service permission checks,
  dependency closure/cycle detection, startup and action lifecycle, activation
  rollback, dependent-use refusal, cleanup retry state, stable errors,
  package-scoped JSON storage, visit-only memory state, and an atomic JSON
  state-store adapter.
- `KPTASInterface/kptas/core/__init__.py`: public native Core package-host
  symbols.
- `KPTASInterface/kptas/core/tas_package.py`: canonical native TAS manifest,
  `tas.runtime` service contract, lifecycle adapter, and explicit registration
  helper. The CLI uses this shared manifest while retaining its enrollment-only
  lifecycle.

Still unwired or unqualified:

- This checkout contains the reusable `NativeSurface` projection but not the
  native `WorkspaceWindow` implementation. No in-repository launcher provides
  `tas.runtime`, calls `register_tas_package`, or starts the host. The adapter
  is ready for the owning workspace to wire, but the current native UI still
  cannot install or disable TAS live.
- Existing native TAS UI, score, playback, session, and process-owner code has
  not been extracted into a separate package distribution. The browser TAS
  entry remains its own implementation; there is no native/browser parity
  claim from this API skeleton.
- No Core profile path is selected, no existing preference state is migrated,
  and no package manager UI, trusted third-party catalog, signatures,
  isolation, or independently installable package artifact is included.
- No C++ migration, WASM edits, build, tests, runtime interaction, or native
  game acceptance is part of this receipt.

The next native integration is to implement the Core-owned workspace bridge
for the contract above and wire its startup after trusted service registration.
That bridge must expose the existing TAS UI/actions through the workspace owner
while leaving supervisor, durability, process and input authority where they
are. A later distribution split is required before “uninstall” can mean
removing TAS code; this host's `remove` action only removes workspace
enrollment.
