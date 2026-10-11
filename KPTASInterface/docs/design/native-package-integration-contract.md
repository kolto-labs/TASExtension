# Native Telos Core package integration

**Status:** architecture and integration contract. This document records what
the current native package code does and what the native application still
needs to wire. It does not claim that the native GUI can currently install or
remove TAS at runtime.

## Product boundary

Telos Core is the base application and owns the game session, debugger,
workspace, package manager, shared services, and extension lifecycle. TAS is
an optional package installed into Core. A Core profile with TAS absent or
disabled must still launch the game, inspect the session with Core-owned
debugger tools, and manage other packages. TAS may consume Core services; Core
must not depend on TAS modules to initialize.

TAS owns its score formats, authoring and playback workflows, recording,
timeline/branch tools, and TAS-specific panels. It must obtain process/session
access through an explicitly granted Core service. It does not own the game
process or bypass Core's session and persistence owners.

Kotor Patch Manager (KPM) is not a Telos Core runtime, package, dependency, or
required installation route. Telos Core and TAS must not require KPM or claim
that KPM activates, loads, or manages their packages. The current native shim
and supervisor are existing implementation components, not a user-facing
KPM integration. Any future loader change must preserve Core as the product's
session and extension owner and be documented as its own trusted runtime
adapter.

## Existing native implementation

- `kptas.core.package_host` validates declarative manifests, persists
  per-profile enrollment, orders dependencies, checks declared services, and
  runs explicitly registered lifecycle objects.
- `kptas.core.tas_package` registers the stable `telos.tas` identity and
  lifecycle against the Core-owned `tas.runtime` service. Its default state is
  not installed and not enabled.
- `kptas.core.cli` can list and edit package enrollment. Its lifecycle is
  enrollment-only: it does not connect to a running GUI or activate TAS.
- `kptas.telos.native_surface` projects the existing native workspace as a
  host-facing surface. It is not itself a package manager or lifecycle bridge.
- The native workspace still owns the existing TAS-centric implementation;
  there is no in-repository application bootstrap that provides `tas.runtime`,
  calls `register_tas_package`, and starts the package host.

Therefore the current `install`, `enable`, `disable`, and `remove` CLI verbs
edit profile state only. They do not install or erase executable package
files, and they do not prove live native activation. The browser lifecycle is
a separate implementation and does not prove native parity.

## Native integration sequence

1. Keep the current game session, supervisor, input transport, journal, and
   durable state under their current owners. Define a narrow Core-owned
   `tas.runtime` adapter over the existing TAS workspace rather than moving
   those authorities into a plugin.
2. At native application startup, construct the Core package host, publish
   only the `tas.runtime` service, explicitly register trusted built-ins
   (including the TAS lifecycle), then start the host. Core UI and debugger
   startup must work if TAS registration or activation fails.
3. Make the profile's installed/enabled choice drive actual lifecycle state.
   Disabling TAS must first stop playback and recording, release held inputs,
   finish or report pending edits, cancel capture, and hide or unregister TAS
   panels. Failure leaves the package inactive and cleanup retryable. Enabling
   must not replay stale pending commands.
4. Keep package state separate from package data. Removing TAS enrollment
   must retain user scores, recordings, profiles, and layouts unless the user
   separately deletes those data. Until an independently distributable,
   trusted artifact format exists, report removal as unenrollment rather than
   deleting code.
5. Only after the native lifecycle is connected should an installer or
   manager UI describe TAS as installed/enabled/active. External packages need
   a reviewed trust/signature and isolation policy; manifest metadata alone
   must never import or execute a module path.

## Contributor rules

- Put cross-extension contracts and host lifecycle in `kptas/core/`.
- Keep TAS-only behavior behind `kptas/core/tas_package.py` and the
  `tas.runtime` capability; do not import TAS models into Core startup.
- Keep game-process, hook, memory, and input privileges out of ordinary
  extension grants. A privileged native adapter requires separate identity,
  target qualification, and explicit user-facing trust.
- Keep manifest identity/API versions independent from runtime protocol,
  game-adapter, and package versions.
- Update the browser and native package contracts together when changing
  shared IDs or capability semantics, while reporting each platform's actual
  activation status separately.
- Preserve an explicit Core-only startup path in all package-host changes.

## Acceptance evidence still required

The native package separation is complete only when the application bootstrap
actually connects the package host and `tas.runtime`, and an interactive
native session demonstrates all of the following: fresh profile starts with
TAS absent; Core game/debugger remains available; installing and enabling TAS
activates its panels and commands; disabling it releases inputs and removes
its UI while retaining user data; re-enabling restores the package without
stale playback; uninstalling removes enrollment but preserves scores and
layouts; and a lifecycle failure leaves Core usable with a specific,
recoverable error. No current CLI enrollment receipt alone satisfies this
gate.
