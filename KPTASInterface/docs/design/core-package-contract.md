# Telos Core package boundary

## Purpose

Telos Core is the host for game sessions, the debugger, dock workspace, and optional packages. TAS is a package hosted by Core. Core owns package identity, API compatibility, dependency ordering, lifecycle admission, scoped storage, and persistence. TAS owns its score language, recording/playback, timeline, branches, and TAS-specific panels. A Core-only workspace must not require TAS code to be active.

## Browser contract in this checkout

`demo/public/js/telos-core.js` is the small browser package host. It does not import KPTAS modules, fetch code from a catalog, or execute URLs from a manifest. Only a module deliberately registered by the application can become a package. `demo/public/js/extensions.js` is the manager view. `demo/public/packages/tas/manifest.js` and `index.js` contain the TAS package identity and lifecycle policy independently of the manager. The production build emits the package entry as a separate code-split module.

Manifests use `telos.extension/v1`, a separately negotiated Core extension API version, stable package IDs, package versions, dependency IDs, declared Core permissions, and feature IDs provided by a package. Installation resolves the registered dependency closure and records local workspace state. Enabling activates dependencies first and unwinds newly activated packages if activation fails. Disable/uninstall run cleanup and refuse removal while another enabled or installed package depends on the target. Cleanup attempts every release action and the host keeps a package disabled if any action reports failure. State and package-owned key/value storage are namespaced. Missing browser storage degrades to visit-only state and is surfaced by the manager.

The host currently manages first-party package modules delivered with the application release. Install/remove change workspace enrollment; they do not fetch or erase executable files. The production build keeps the TAS entry in a separate code-split module, and the package owns activation/deactivation ordering through a declared `tas.runtime` Core service. The TAS editor, score model, playback, and much of its UI wiring still live in `demo/public/js/app.js`. Thus TAS now has a real manifest, package entry, Core lifecycle, and separately emitted module, but is not yet an independently downloadable/removable artifact. Extracting the remaining TAS runtime/UI behind package-owned modules and adding a trusted package source/signature policy are still required for full independent installation.

## Security and capability rules

- Catalog records and manifests describe packages; neither grants code execution.
- Unknown schema/API majors, malformed IDs, duplicate registrations, missing dependencies, dependency cycles, and undeclared service access are refused before activation.
- The host does not expose process pointers, patch/hook installation, arbitrary filesystem access, network access, or runtime ownership to ordinary packages.
- Services are explicit host registrations. A package may request only the service IDs declared in its manifest; an unavailable service fails with a stable Core error.
- Package state is isolated by package ID. Uninstalling enrollment retains user scores, game files, and workspace layouts.
- A separately packaged or remotely installed executable extension requires a signed/trusted source policy and a reviewed isolation model. This browser slice intentionally does not load external JavaScript.

## Native and browser parity

Both hosts should implement the same manifest semantics, package IDs, independent API versions, dependency lifecycle, capability names, and result/error codes. Their renderers remain platform-specific: native packages register native dock widgets; browser packages register browser widgets. The game session and debugger belong to Core in each host; TAS consumes those Core services through a package adapter and does not own the game process or iframe session.

Parity is a target contract, not a claim of current parity. The native `KPTASInterface` executable still uses its existing TAS-centric structure; this browser package API has not yet been ported into it. The first native step should be a Core-owned registry facade that leaves supervisor, persistence, and process authority in their current owners while a separate TAS adapter registers against the facade.
