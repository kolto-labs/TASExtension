# Compare native injection and browser functionality

## Authority and scope

Direct user instruction on 2026-10-14 UTC:

> subagent to make sure the native code injection stuff and the overlays in the native game is available in their entirety within the wasm and website stuff. And vice versa.

This is a source inventory and implementation receipt, not another execution
board. The canonical outstanding execution plan retains priority and milestone
authority. Full bidirectional parity remains open. No tests, probes, syntax
checks, game launches, runtime rebuilds, staging, or deployment were performed
for this slice. The parent retains browser, gameplay, recording, and build
ownership. No tabs were opened or closed.

The native workspace source is
`/home/brunner56/kptas-routine/src/kptas/telos/`. Its production C++ shim and
this checkout's C shim are distinct implementations; neither is substituted
for the other. Browser K1 currently executes x86 Windows code through Wine in
WASM. This is not native execution or support for every uploaded game.

## Compare the implementations

N denotes the named native workspace directory above. H denotes
`KPTASInterface/` in this checkout. B denotes `demo/public/` in this checkout.
Paths below identify source owners; implementation presence is not live
qualification. Where behavior differs, the row remains a parity gap even if
both hosts have a similarly named control.

| Feature family | Native evidence | WASM/static evidence | Disposition in both directions |
| --- | --- | --- | --- |
| Proxy DLL bootstrap and entry/IAT detours | H `native/shim/src/main.c`, `patch.c`; production native `src/native/shim/src/main.cpp` | B `original-game/telos-tas-bridge.js`; selected Wine guest | No equivalent browser hook-install API. A translated guest and parent bridge do not establish injected DLL parity. Exact target/bootstrap admission and qualification remain required. |
| Clock and time import interposition | H `native/shim/src/clock.c`; `main.c` installs EXE, Miles and Bink clock hooks | B bridge execution boundary and handshake | Render-present stepping is not native deterministic clock interception. No equivalence granted by a nominal Hz setting. |
| Thread ownership and cooperative gate | H `native/shim/src/threads.c`, `gate.c`, `rt.c`; `wire.c` gate-owned request handling | B bridge pause/resume/step; runtime-owned safe points | Runtime implementations differ. Worker counts are not guest thread identities; quiescence and game clock qualification remain separate. |
| Allocation, virtual memory and handle virtualization | H `native/shim/src/alloc.c`, `vmem.c`, `vhandle.c` | Wine/translator owners below the static UI | No demonstrated native virtualization contract or portable handle state in the browser. UI memory inspection cannot substitute for these hooks. |
| OpenGL state and resource reconstruction | H `native/shim/src/gl.c`; production native `src/native/shim/src/glsync.cpp`, `glframe.inc` | Translator/graphics backend; bridge displayed observations | Requested GL arguments are not accepted backend state. Full GPU reconstruction/restore equivalence absent. GL4ES vendor is not editable by this agent. |
| DirectInput keyboard/mouse and message interposition | H `native/shim/src/dinput.c`, `msg.c`; N `session.py` input scheduler | B `js/app.js`, `js/original-game.js`, bridge | Both have input paths; coordinate, key, sequence, release and consumption boundaries require independent acceptance. |
| Geometry and save/load events | H `native/shim/src/geo.c`, `wire.c` event journal | B original game bridge and recorder events | Host and guest event domains differ. Filesystem profile persistence is not a game save/load acknowledgment. |
| Tier locking and mutation refusal | H `native/shim/src/wire.c` tier.lock and verb classes; native supervisor | B `debugger/debugger-operation-broker.js`, backend/policy reports | Separate admission owners. A UI or manifest cannot elevate native hooks or browser writes. Preserve native TAS/lab restrictions. |
| Native capture, digest, restore and verify | H `native/shim/src/state.c`, `native/state/kst_capture.c`, `kst_restore.c`, `wire.c` | B bridge state advertisements | Native implementation exists but full GOG qualification remains open. Whole guest CPU/memory/GPU/handle/audio snapshot, rewind and restore are absent or unqualified in the browser. |
| Score execution, pause and step | N `session.py`, `score_control.py`; H `native/shim/src/wire.c` | B `js/app.js`, original owner, bridge | Controls share intent; native game ticks and browser render presents remain distinct units. Input dispatch does not prove deterministic replay. |
| Seek/rewind, take and branch | N `session.py`, `timeline_tools.py` | B `js/app.js`, `holo.js` | Browser sample/editor navigation and branches are not guest VM restore or recorded native takes. Highest impact native-to-browser gap. |
| Input cell/range editing, insertion/deletion, autofire | N `input_edits.py`, `timeline_tools.py` | B `js/edit.js`, `js/input-lane-pattern.js`, `packages/tas-ui/editor.js` | Browser source now has selected key/button Hold and configurable Autofire via its existing score transaction. Native period/phase arithmetic aligns on the supported subset; literal lane vocabulary/order, live authoring acceptance and runtime execution remain distinct. |
| Clipboard and mouse input | N `input_edits.py`, `input_view.py` | B `js/edit.js`, `js/score.js` | Native wheel/relative fields must not be silently downgraded. Browser unsupported fields are refused; browser editor/dispatch parity remains open. |
| Undo/redo, marks and follow playhead | N `draft_store.py`, `timeline_tools.py`, `actions.py` | B `js/app.js`, `js/edit.js`, `js/tas-presentation.js` | Authoring intent exists in both. Undoing an input draft is not guest rewind. Persistence and behavior require qualification. |
| Overlay/surround mode; visibility, opacity and game collapse | N `overlay.py`, `shared_presentation.py` | B `js/shared-presentation-profile.js`, `js/dock-workspace.js`, `js/tas-presentation.js` | Shared presentation v3 carries mode, game visibility/collapse/opacity and timeline opacity. Import does not transfer game state; live round trip remains open. |
| Timeline density, geometry and panel docking | N `overlay.py`, `panel_controls.py` | B `js/dock-workspace.js` | Native Console docking exists; browser has more individual views. Native compact/minimal rail density differs from browser whole-panel collapse. Further browser-to-native pane docking remains open. |
| Shortcut maps and input/focus ownership | N `actions.py`, `keymap.py`, `shortcut_binder.py`, `game_focus.py` | B `js/keymap-interchange.js`, `js/keyboard-controls.js` | Shared keymap v2 retains aliases. Global native dispatch and browser focus differ; imported shortcuts cannot grant execution. |
| Preferences, profiles and style reload | N `ui_preferences.py`, `ui_profiles.py` | B `js/ui-hot-reload.js`, `js/dock-workspace.js` | Host storage and geometry stay host-specific; opaque shared host payload is retained, not executed. Restart persistence acceptance remains open. |
| Memory read/search/watch | N `memory_widget.py` reads recorded take frames; H `wire.c` mem.read | B `packages/debug-ui/memory-tools.js`, `js/debugger-workbench.js` | Recorded native RAM and paused guest captures are different evidence. Browser captured scans are bounded; native record watches are not live guest watches. Writes are not inferred from reads. |
| Processes, threads, saved registers, modules and address mapping | N `debugger_widget.py` qualification display | B `packages/debug-runtime/`, `packages/debug-ui/`, original owner | Browser has bounded runtime readers and captured register navigation for its selected process. Matching native readers and shared semantics remain open; counts/labels are insufficient. |
| Disassembly, decompilation, stack, symbols, source and references | N `debugger_widget.py`; H `core/debugger_core.py` | B debug-runtime widget manifest and operation broker | Vocabulary/widgets exist, but handlers and exact target evidence govern availability. Unsupported panes cannot become live features through presentation work. |
| Breakpoints, expressions, patches and trace | Native capability owners; H debugger Core | B debugger broker and package widget providers | No complete live bidirectional implementation. Recorded/captured inspection cannot authorize mutation; generic adapters cannot install hooks. |
| Inspection export and freshness | N `debugger_widget.py` export/observe | B `js/debugger-workbench.js` | Shared telos-debugger-inspection/2 preserves nulls, source, owner, boundary and host details. Export is not a command/capability/state import. |
| Captured scan export and register scan preparation | N `memory_scan_capture.py`, `debugger_widget.py`; recorded search remains in `memory_widget.py` | B `packages/debug-ui/index.js`, `memory-tools.js` | Browser explicit prepare-only register ranges and captured scan exports now have a native read-only file consumer in source. Native live-memory search/register preparation remains distinct. Cross-host import and browser export acceptance remain open. |
| Uploaded file identity and byte inspection | Native exact executable/shim profile owners | B `debugger/static-binary-backend.js`, `static-file-owner.js`, `js/binary-identity.js` | Browser handles local static files without execution. Native equivalent generic file inspector and arbitrary-game runtime adapters remain open. |
| Core/package lifecycle | H `core/package_host.py`, `tas_package.py`; native workspace ownership | B `js/telos-core.js`, `js/extensions.js`, package manifests | Shared lifecycle vocabulary does not mean native workspace activation is wired or independently published. No native privileged hook API in generic extensions. |
| Community run intake and mechanics | N `speedrun_widget.py`, `mechanic_sequence_widget.py` | B `js/speedrun-playground.js`, `js/footage.js` | Observation/import paths are not replay-qualified inputs. Provenance, engine-clock alignment, routes and mechanics acceptance remain open. |
| Recording, footage, audio and reload | Native recorded state and video owners; N recording library | B `original-game/game-recording.js`, recorder writer and UI | Native holorecord, browser audiovisual recording and imported footage are distinct formats. Continuous full run, audible output, correction/rewind/reload clip and actual restore remain incomplete. |

## Implement the bounded contract gap

Native Core previously enumerated eleven standard operation names, including
legacy `disassembly.read`, `breakpoints.software`, `snapshots.capture` and
`snapshots.restore`. The static host uses canonical `code.disassemble`,
breakpoint operations, `state.snapshot` and `state.restore`, plus processes,
maps, captured search, symbols, binary and trace operations. Missing vocabulary
made native default reports omit many browser feature families.

`H core/debugger_core.py` now enumerates every canonical entry in B
`debugger/debugger-contract.js` and retains all four legacy entries separately.
There is no alias promotion: a report for `snapshots.restore` does not qualify
`state.restore`. Missing owner reports still produce unavailable operations,
and all four owners still must agree before the native resolver reports
available. No dispatch handler, transport, package permission or runtime
capability was added. Native and browser capability envelope schemas remain
distinct; this vocabulary alignment does not make them interchangeable.

This changes the native source report's default entry set. Consumers that
display the complete map may show more unavailable rows. The external native
application has not adopted this checkout's updated Python module. No live
capability or overlay parity is claimed from this change.

## Inspect browser captured scans in the native debugger

A follow-up source slice implements an actual browser-to-native inspection
path. N `debugger_widget.py` adds an **Imported memory scan** section with
**Import captured scan**, **Clear imported scan**, and paginated matches.
N `memory_scan_capture.py` reads the selected local
`telos.memory-scan-capture/v1` file once with a 2 MiB limit. It rejects duplicate
JSON properties and offsets, invalid schema/fields/types/integers, invalid byte
arrays, 32-bit range overflow, unsupported value types/byte order, incompatible
previous captures, and matches outside declared candidate bounds. Candidate
and byte arrays remain bounded by the original 64 KiB range.

The pane retains the original file bytes and SHA-256, export/capture times,
declared session/PID/epoch/source, both target declarations, comparison
declaration and all matched offsets. It displays current and previous typed
values plus current raw value bytes, 200 rows at a time with next/previous
navigation. Raw bytes retain floating-point bit patterns even when decoded
display values are NaN or signed zero. Failed imports preserve the previous
artifact and visible rows. Clear releases only this imported evidence.

Imported matches are exporter claims; the native reader does not independently
replay the comparison or authenticate the capture. File hashes identify bytes,
not truth or executable identity. Target metadata remains declared metadata.
The imported artifact never binds to the native owner, adds a memory watch,
issues a process read, alters a score, supplies an owner acknowledgment,
changes operation qualification, or restores state. Imports are visit-only,
separate from live native observations and the existing inspection export.

The pre-edit debugger source is retained on SSD as
`artifacts/debugger-widget-before-scan-import-20261014.py`. The native source
directory is not a Git checkout; a scoped patch and new-module copy are kept
with this design receipt in the active checkout's artifacts directory. The
actual native source is edited in place with other dirty source preserved.
No runtime adoption or actual exported-file import was qualified. Parent-owned
native UI acceptance remains required: select an actual browser export,
inspect provenance and pages, retain evidence after a failed import, and clear
the import without affecting the current game/session. No tests or probes were
run.

## Carry native lane patterns into static score authoring

B `js/input-lane-pattern.js` implements the native
`input_edits.py:autofire` arithmetic on supported browser key/button lanes:
`(scoreTick - firstIncludedTick + phase) % period < floor(period / 2)`.
The supported period is 2–20000 score ticks, with phase 0 through period minus
one. This is explicitly score authoring; score ticks are not automatically
native game ticks or render presents. Odd periods hold for floor(period/2)
ticks and release for the rest. The native helper permits a broader phase
domain; this UI admits a bounded nonnegative phase only.

B `packages/tas-ui/editor.js` adds **Hold** and **Set autofire** context actions
for the selected key/button lane and selected tick range. Autofire asks for
period/phase before editing. No mouse-coordinate lane is patterned. The pure
helper accepts a validated lane list, preserves unrelated lane bits and mouse
coordinates through `setKey`, and delegates rewriting to the existing
`editTicks` owner. The browser's established alias removal on release and
canonical bit-lane serialization remain unchanged. Native held-key tuples
retain their native order; no new portable literal ordering claim is made.

Tick zero is excluded; the upper range is clipped to displayed rows and the
existing 30000-score-tick host limit. A transaction is capped at the native
helper's 20000-frame edit bound. Existing score parsing, skipped sync rows,
explicit repeat-unroll confirmation, source-text rewrite and undo history
remain authoritative. The phase starts at the first included score tick,
including the position of skipped sync rows; sync rows themselves stay intact.
Tail rows retain the existing editor's explicit note-extension behavior.

The new actions require the active TAS package and editor owner. A pending
dialog cannot apply after package/lifecycle, branch, score or model changes.
The repeat-unroll callback rechecks the owner; on mismatch it throws before
the existing transaction can commit even the unrolled draft. No new playback,
input dispatch, runtime sampling, guest seek or rewind is introduced.

The dirty editor's exact baseline is preserved in
`artifacts/tas-editor-before-autofire-20261014.js`; only this scoped slice is
changed. Implementation is source-only. Actual context-menu/dialog behavior,
score output, undo, sync/repeat refusal and package-disable cancellation
require parent-owned normal UI acceptance. No tests, probes, syntax checks,
runtime builds, staging, reload or browser actions were performed.

## Retain the acceptance boundary

Full parity requires qualified owner implementations for every row, both
directions, using the same declared operation meaning and exact target
identity. Transport reachability, widget presence, manifests and source are
individual evidence, not acceptance. Keep one selected controller; do not
create a second native or WASM owner to exercise the comparison.

The parent should coordinate runtime primitives in existing P1/P2 work:
deterministic boundary/clock, input consumption and release, coherent state
capture/restore, then replay and recording. Presentation interchange and
native independent-pane docking can proceed without inventing these runtime
primitives. Uploaded games require separate transport/backend/target/policy
evidence; executable identity alone grants none. Workers release remains
behind local acceptance, and private game assets remain local.
