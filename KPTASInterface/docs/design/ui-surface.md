# Shared TAS presentation boundary

`kptas.telos.native_surface.NativeSurface` adapts an **existing** native
`WorkspaceWindow`. It exposes immutable view data and stable action names for
menus, keyboard help and alternate presentation renderers. It creates no owner,
transport, command queue, timer, Qt widget or input editor. Call it only on the
window's GUI thread after the host refreshes its original controls.

## Actual native host and missing integration

Source inspected on 2026-10-09 lives under
`/home/brunner56/kptas-routine/src/kptas/telos/`. `overlay.py` supplies the Qt
overlay, `workspace_window.py` supplies its command queue and piano editor,
`workspace_session.py` supplies the client and acknowledged snapshots, and
`session.py` supplies the owner. `interaction_controls.py` routes native tool
shortcut events and Qt shortcuts into the same actions. `timeline_rail.py`
retains future replay and invalidation behavior.

This is an external Qt window over the injected game, **not an injected C++
widget renderer**. Native injection supplies control/input hooks. The tracked
KPHarness `m3` tree contains neither `WorkspaceWindow` nor its complete session
dependency closure. The separate source under `/home/brunner56/kptas-routine/src`
is not a Git checkout. Its three target files matched the isolated source
baseline byte-for-byte before the reviewed integration patch was applied. That
source now attaches `NativeSurface` to the existing `OverlayClient` and
`DebuggerDialog`; it reuses the current transport/session and controls. The
tracked KPHarness copy has the adapter contract but no native UI host. Neither
copy has been built, installed, or runtime-qualified.

The source patch and its isolated baseline are preserved at
`/home/brunner56/kptas-wt/native-host-surface.patch` and
`/home/brunner56/kptas-wt/native-host-surface-20261009/` until the native source
is reconciled into durable version control.

The existing native host integration is deliberately small:

```python
from kptas.telos.native_surface import NativeSurface

# Reuse the WorkspaceWindow already constructed by run_workspace().
surface = NativeSurface(window)
# After window.present(), read surface.view() on the GUI thread.
# On explicit submit/release: surface.activate('seek', tick=destination).
# An existing host shortcut callback, after Qt resolves its key and scope:
# surface.activate('step', keyboard=True).
```

Do not construct another `WorkspaceController` or `TASSession` to use this
adapter. Do not expose `activate()` through an HTTP bridge without the existing
host's transport admission, authentication and GUI-thread marshaling. GUI-thread
use is enforced. The adapter is not a replacement shortcut binder: retain Qt's
existing key conflict resolution, widget scope and autorepeat policy.

## Common contract, host presentation

| Concern | Shared meaning | Browser | Native |
|---|---|---|---|
| Presentation | Layout changes only presentation | `browser` by default; `overlay` optional | Existing `workspace` and `overlay` preferences |
| Compact/expanded | Explicit expansion preserves transport and authoring | 24px collapsed seek rail; expanded rail and piano editor | Existing density, rail and piano model retained |
| Playhead | `committed_tick` is acknowledged; no interpolation | Existing controller boundary/capability | `WorkspaceSnapshot.tick` |
| Browse/seek | Selection and preview are separate from committed progress | Existing rail | `selected_tick` and `FutureRail.target`; submit keeps restore/replay admission |
| Input history | Recorded, queued and draft inputs retain distinct provenance | Existing roll and score model | Existing `Timeline`, `PianoModel`, `last_inputs` and recorded-input helpers |
| Layout versus engagement | Hide/layout never implies pause or input handoff | Presentation module only | Original `InteractionControls`; acknowledged `engaged` can be unknown |
| Capability | Show effective enabled state and reason; no invented operations | Existing runtime capabilities | Original widgets and QActions determine availability |
| Undo/redo | Edits draft inputs, never rewinds game state | Existing draft editor | Original undo/redo QActions |
| Follow | Persisted presentation policy; browsing can turn it off | Existing Follow setting | Original Follow QAction and preferences |
| Outcome | Dispatch is not an execution ACK | Existing controller receipt | `DispatchResult.dispatched` only; host ACK refresh updates committed state |

Action IDs are `play_pause`, `step`, `rewind`, `seek`, `apply`, `save`,
`toggle_editor`, `switch_layout`, `toggle_visibility`, `toggle_engagement`,
`follow_playhead`, `undo_input_edit`, and `redo_input_edit`. These reuse native registry names where
present. `save` means **input drafts**, not recording durability. Exact seek is
an integer target in the existing rail range, admitted through
`WorkspaceWindow.submit()` and its future-replay path. A boolean is not a tick.
Missing controls remain disabled. Read-only and unknown-owner states retain
their original recovery restrictions.

`SurfaceView` intentionally does not copy piano rows or input classifications.
Renderers must keep using the host's existing row/model data; copying them here
would create a second editor model and risk labeling drafts as executed input.
The adapter likewise does not prescribe pixel geometry or replace native
shortcut bindings with browser keys. Effective native shortcut text comes from
the current preferences or QAction. Follow uses a separate registry QShortcut;
its resolved binding is not available through its QAction, so its shortcut is
`None` (unknown), not an empty string falsely claiming it is unbound.

`owner_capabilities` contains the native owner's existing capability names,
without adding browser operations or inferring support from widget existence.
`unknown_state` distinguishes an uncertain owner from an ordinary pause;
`clock_precision` preserves the owner's metadata (including missing metadata).
The display `unit` alone does not qualify the game/input/render boundary.

## Design parity and remaining differences

A shared contract can define action meaning, status hierarchy, input provenance,
focus behavior, compact/expanded access, and visual roles. Each host still needs
its own layout, capability projection and controller adapter. Browser
`tas-presentation.js` changes only a body attribute, mode-button wording and a
resize event; it does not consume this Python contract or unify the owners.

The browser and native base palettes now use the same semantic values: void
`#05070a`, panel `#0b1016`, text `#d7e3ea`, muted `#7f93a0`, active/Kolto
`#19e3c1`, focus/Bacta `#5ad8ff`, warning `#ffb000`, success `#7dff9a`, and
divergence-only `#ff4fd8`. Native QSS resolves roles through `ui_theme.py` and
continues appending user QSS last. Browser CSS and native QSS still package
these values separately, and platform-specific geometry, fonts, dense piano
fills, and user QSS can differ; this is semantic palette alignment, not pixel
identity. Native defaults to `overlay` with saved geometry/density; browser
defaults to `browser` unless the user explicitly saved overlay mode.

Both hosts can display a stable acknowledged playhead and a separate selected
position. The native adapter's `preview_target` currently names only an active
`FutureRail` forward-replay destination; it is not a generic drag-preview or
pending-backward-seek field. Adding those requires the existing host to expose
them. Browser VM render presents, authored score positions and native committed
ticks must remain separately labeled. A common Step label cannot imply that
these boundaries, restore capabilities or input acknowledgement are equivalent.

## KPM exclusion supersedes the old bootstrap queue

The current user directive, “We should not allow people to use KPM with us”,
overrides the execution plan's stale KPM bootstrap rows. No KPM option, installer,
plugin activation, compatibility mode or bypass belongs in this UI contract.
The supported direction is Stasis's own dinput8 bootstrap → Telos Core → optional
TAS/Lua additions. KPM is distinct from optional Hyperstim/KotorPhasor; exclusion
of one is not authority to remove unrelated packages.

The separate native product source at `/home/brunner56/kptas-routine/src/`
contains config rejection in `kptas/config.py`, read-only preflight in
`kptas/backend/rig.py`, and active-shim refusal with exit code 75 in
`native/shim/src/main.cpp`. The passive shim only logs, preserving games Stasis
did not launch. These are inspected source facts, not fresh runtime
qualification. The tracked KPHarness `m3` source lacks these KPM guards, so
release must not claim enforcement until the actual guarded host is reconciled
and qualified.

The canonical outstanding execution plan now replaces the old KPM-loader path
with `kpm-exclusion` and maps R21 to that outcome. Keep historical KPM material
as superseded evidence. The separate native product plan also records this at
R21, KTD11 and U17, and its `STRATEGY.md` records it in the product constraints.

The safe boundary is refusal before Stasis starts a supported session, with a
clear reason and no “continue anyway” path. Never delete, rewrite or rename KPM
files automatically, and never terminate an unrelated user game. If detection
is uncertain, report that fact; do not relabel a generic foreign patch as proven
KPM. This presentation-only adapter must relay the owner's refusal rather than
scan files, inspect processes or invent a second launcher. Canonical plan edits
and guarded-host integration remain root-owned; this slice changes neither.

## Remaining parity work

The browser and native implementations still have different execution backends,
capabilities and time boundaries. The native source slice now connects its
existing debugger dialog to the presentation boundary; it does not establish a
cross-host protocol, framebuffer restore, input handoff qualification, or live
visual parity. The native source still needs durable version-control
reconciliation. Next, map the browser's current controller to the same
view/action meanings and qualify each host's compact/expanded/focus/seek
behavior under the root-owned session.

No build, tests, application launch, runtime rebuild or deployment was performed
for this change, as directed. Source inspection is the only review evidence.
