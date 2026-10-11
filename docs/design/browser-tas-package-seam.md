# Browser TAS package seam

## Current boundary

`demo/public/packages/tas/manifest.js` owns the package identity and capability declaration. `index.js` owns activation and cleanup policy. Telos Core persists installed/enabled state, checks dependencies, and invokes that lifecycle. `demo/public/js/app.js` still owns the actual TAS implementation: score state, input routing, render scheduling, editor event handlers, and dock view construction. The package reaches those functions through the single `tas.runtime` service registered by `app.js`.

This means TAS can currently be enabled and disabled, but its implementation is not yet independently packaged. In particular, changing only the package entry cannot move behavior out of `app.js`: the service methods close over private app state and are the only route to it. Changing the manifest default to disabled also would not finish optionality: existing TAS DOM and controls are assembled by the app independently of package activation.

## Smallest real extraction

Extract one vertical slice at a time into a module under `demo/public/packages/tas/`. The first slice should be score authoring, since it has a clean data boundary and can be moved without changing game startup:

1. Define a versioned `tas.score-document` contract in the package: parse, serialize, validate, and expose immutable score snapshots. Keep game session, input injection, and rendering owned by Core adapters.
2. Move score-document parsing and serialization into the package module, then have the existing app call that module through its package import. Preserve current score syntax and error locations.
3. Narrow `tas.runtime` into explicit Core capabilities for session input, clock, workspace persistence, and dock registration. Keep the package responsible for composing those capabilities into TAS behavior.
4. Move dock widget construction and event wiring into the package after the score contract is stable. Only then make the package absent by default and ensure Core-only pages do not build TAS controls.

Each slice must have exactly one owner for mutable score/session state. Do not duplicate the existing state in a package facade or add a second renderer. Preserve the present app behavior until the corresponding slice has moved end-to-end; the package lifecycle must remain able to release held game inputs and stop recording on disable.

## Acceptance boundary

The package is independently optional only when a fresh workspace starts with Core and no TAS widgets, installing/enabling TAS mounts its widgets and commands, disabling it removes those surfaces and releases input without deleting scores or layouts, and uninstalling leaves Core session and debugger functions available. A module that merely wraps `tas.runtime` does not meet that bar.
