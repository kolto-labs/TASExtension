# Debug runtime package

`telos.debug` is optional and disabled/uninstalled by default. TAS is independent.
The package is not registered by an entry point yet: integration belongs to the
workspace-shell owner, with a `debug.runtime` service and a UI mount/disposer.

## Host integration

Create one `createDebugRuntime({getSession, adapters})` for a session owner.
Provide it with `host.provide('debug.runtime', runtime)`, then call
`registerDebugPackage(host, {mount})`. The mount receives only that service and
package-scoped storage. Disable/removal invokes the returned UI disposer.
`DEBUG_WIDGET_OPERATIONS` identifies the operations each widget needs; it does
not claim that every operation is available.

`createGuestMemoryAdapter(game)` uses the existing session/origin-checked guest
bridge for process IDs and memory reads. It delegates bounds and readiness to
the owner and checks the returned byte count. `createCapturedToolsAdapter()`
adds bounded wildcard-byte searching and integer watch arithmetic over explicit
captured values, without JS evaluation or implicit live memory access.
Captured values and static binary addresses remain separate from live addresses.

Requests use `telos.debug-request/v1` with `id`, `session`, `operation`, and
`params`. Replies retain the same identifiers, adapter, source, capture time,
and result. The session is checked before and after asynchronous operations.
Unsupported operations return `DEBUG_UNSUPPORTED`, rather than fabricated
registers, threads, breakpoints, maps, or traces. A session owner must still
qualify supported operations and any memory-read pause boundary.

## Backend and REA

`demo/worker/debug-api.mjs` exports an opt-in POST handler. It requires an
explicit authorizer and exact allowed Origin, caps request bytes, rejects
stale sessions, and returns structured codes. No existing route was modified.
The host should log unexpected backend exceptions with its diagnostic system.

REA was inspected directly on 2026-10-11:

- [REA README](https://github.com/morluto/rea): local CLI/MCP orchestration over
  analysis engines, rather than an embeddable browser live-debugger engine.
- [REA CLI](https://github.com/morluto/rea/blob/main/docs/cli.md): `instructions`,
  `decompile`, and `xrefs` address queries support Ghidra/IDA/Hopper providers.
- [REA license](https://github.com/morluto/rea/blob/main/LICENSE): MIT, copyright
  2026 morluto. No REA source was copied into this package.

`demo/worker/debug-rea-adapter.mjs` supplies those static-analysis operations
through an injected local companion runner. The host binds an owned binary
path and SHA-256 to a session; request parameters cannot select filesystem paths,
process commands, or CLI flags. The runner must enforce a 60-second deadline,
2 MiB output cap, cancellation, and invocation without a shell. REA must already
be installed/configured in that local companion. Cloudflare Workers cannot
launch local Ghidra/IDA/Hopper; this adapter is not a hosted execution backend.

## Remaining integration

The shell must register the optional package, mount widgets, and supply runtime
qualification/readiness. Live register capture, thread/module lists, memory
maps, breakpoints, stacks, tracing, mutation, snapshots, and restore need real
emulator/backend operations; registering widget metadata does not implement
them. The REA companion runner and artifact resolver also need a host connection.
No tests, builds, server changes, or deployments were performed for this slice.
