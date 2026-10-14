# TAS conventions

Shared habits across TAS tools: hotkeys, modes, counters, lag, greenzone, branches, undo, splicing, memory tools and scripting. Tags point to the source list at the end. Quoted text is copied from source pulled with git or curl.

For the earlier ten-tool UI survey (rails, cursors, focus, accessibility), see [the 2026-10-03 conventions research](../../../planning-inputs/execution/tas-ui-conventions-current/research.md). This file does not repeat it.

## Default hotkeys

| Action | BizHawk [BH-bind] | libTAS [L-key] | FCEUX TAS Editor [FX-ctl] | CelesteTAS [CT-ctl] | Stasis [S-ctl] |
|---|---|---|---|---|---|
| Pause / play | Pause | Pause | Pause; middle click | `]` pause/resume; Right Ctrl start/stop | Ctrl+Alt+F7 |
| Frame advance | F | V | `\` | `[` | Ctrl+Alt+F8 |
| Frame back / rewind | Shift+R (general Rewind) | right button + wheel up seeks back one frame [L-wheel] | Backspace | Left Ctrl+`[` | Ctrl+Alt+F9 |
| Fast-forward | Tab (hold); Shift+Tab turbo | Tab | Turbo seek checkbox [FX-tb] | Right Shift (hold) | planned, M2 [S-plan] |
| Slow-motion | not bound by default | not found | not found | `\` slow forward | `@segment ... pace=realtime`, per-segment `hz` [S-score] |
| Save state slot n | Shift+F1..F10 | Shift+F1..F10 | Shift+F1..F10 sets a bookmark | Right Alt+Minus (one state) | branch: Ctrl+Alt+F10 |
| Load state slot n | F1..F10 | F1..F10 | F1..F10 loads that bookmark's branch; 1..0 jump to its frame | — | rewind to mark: Ctrl+Alt+Shift+F9 |
| Load branch n | TAStudio branch box | Ctrl+F1..F10 | F1..F10 | — | open child `.holo` |
| Toggle read-only | Q | unbound ("Toggle Read/Write") | Q toggles Recording | — | tier is chosen per run [S-tiers] |
| Undo / redo | Ctrl+Z / Ctrl+Y (TAStudio) | editor Undo/Redo | Ctrl+Z / Ctrl+Y | text editor | Ctrl+Z; Ctrl+Shift+Z or Ctrl+Y for drafts [S-plan] |
| Set / delete marker | M / Ctrl+M | editor menu | double-click frame number | `#label`, `***` breakpoint | `@mark` in score [S-score] |
| Seek prev / next marker | Shift+PageUp / Shift+PageDown | — (has "Autopause on markers" during playback [L-ui]) | `<<` `>>` buttons | breakpoint fast-forward | rewind to mark |
| Seek to frame | Ctrl+G | seek to row | click row | — | `--tick`, `seek` API [S-ctl] |
| Add / delete branch | Alt+Insert / Alt+Delete | — | Shift+F1..F10 | — | Ctrl+Alt+F10 |
| Return to pre-edit frame | R ("Seek To Green Arrow") | "Rewind seeks to current frame" option: after a past edit, replay back to where you were [L-rew] | "Auto-restore last position" checkbox [FX-tb] | — | — |

BizHawk lists these actions with no default key: Autofire, Autohold, Clear Autohold, Frame Counter, Lag Counter, Input Display, Toggle Skip Lag Frame and Record A/V. TAStudio has defaults for Toggle Follow Cursor (Shift+F), Toggle Auto-Restore (Alt+R) and Toggle Turbo Seek (Shift+S) [BH-bind]. BizHawk's Shift+R "Rewind" is the general rewind action, not a TAStudio single-frame step.

**The near-standard:** F-keys are state slots, Shift+F saves, plain F loads. BizHawk and libTAS share this exactly [BH-bind] [L-key]. Frame advance is a single unmodified key: F in BizHawk, V in libTAS, `\` in the FCEUX TAS Editor. Stasis uses Ctrl+Alt chords because the game owns the keyboard. Remapping already exists in `keymap.toml`, and shortcuts need Ctrl, Alt, Meta or a function key [S-ui]. So an optional preset matching the BizHawk/libTAS layout (Shift+F saves, F loads) is possible.

## Read-only and read-write

- **Read-only (playback):** user input does not reach the movie, and state loads can't change it. **Read+write (recording):** input is written to the movie, and loading a state truncates the movie to that state's input; everything after must be recorded again [TV-basic].
- BizHawk stores the whole movie in each savestate. In read-only mode a state load runs a timeline check and reports the frame where state and movie diverge [TV-bhrr].
- libTAS: seeking back from the editor first switches to playback, then loads the nearest state, then fast-forwards to the target [L-rew].
- FCEUX TAS Editor: the Recorder's "Recording" checkbox is read-write; Q toggles it [FX-tb] [FX-ctl].
- Dolphin also has a read-only mode for `.dtm` recording; the default key was not confirmed from a primary source.

Stasis has no single read-only toggle. Its tier says what a run may do, and editing goes through drafts and branches. Recording never writes into the parent: a branch is a new bundle [S-ctl] [S-gloss].

## Counters

- **Frame counter:** movie length [TV-basic].
- **Lag counter:** in the polling sense, increments "whenever a game fails to poll for input" [TV-feat].
- **Rerecord counter:** how many times recorded input was replaced from a savestate [TV-gloss]. libTAS counts a load only if input changed since the last load [L-rr]. Celeste counts edits to the file [CT-start].

### Lag: three definitions

Pick one and label it, because tools disagree.

1. **Polling lag.** The game did not read input during the frame [TV-feat] [FX-gloss]. BizHawk keeps a per-frame lag log in the project [BH-io] and colors lag rows red [BH-pal].
2. **Time lag.** gz counts lag "by subtracting the number of game frames passed from the game's vertical interrupt counter" [GZ].
3. **Non-draw frames.** libTAS emits one when time crosses a frame boundary before a render happens. They are red in its editor and can vary between systems [L-how].

For Stasis: a tick is a main-loop pass that found no message waiting [S-gloss]. The holorecord frame index stores "presents and reads in the tick", and a tick with no present is flagged `repeat` [S-holo]. In that index "reads" sits beside the `glReadPixels` back-buffer capture, so it should not be taken as input polls. Non-draw ticks can therefore be derived now from the present count. Polling lag needs a per-tick count of the game's input reads in the shim journal; no such field was found in the docs examined. Name both kinds and say which one a counter shows.

## Greenzone and invalidation

- FCEUX: the Greenzone automatically collects savestates for all emulated frames, used to rewind or jump [FX-gloss].
- Editing input at frame F invalidates every stored state after F. BizHawk's edit path records undo and then calls `InvalidateAfter(frame)` [BH-edit].
- BizHawk keeps "Ancient" states that "never decay but are invalidated" [BH-zw]. Marker states can be kept regardless of spacing ("Markers keep states by default") [BH-set] [BH-paged].
- Its palette uses separate colors for greenzone rows, rows with a stored state, and invalidated rows. Lag rows have the same three variants [BH-pal].
- libTAS has no automatic greenzone. It seeks from the user's savestate slots [L-rew].
- Stasis: greenzone ticks are those with a stored state, so going back is a restore, not a replay; `telos show` marks them `c` [S-gloss].

## Playback cursor, lost position, auto-restore

- FCEUX separates the playback cursor (light-blue arrow) from the selection (dark-blue rows). A green arrow marks where playback was before an edit moved it [FX-pr].
- "Auto-restore last position" replays edited segments automatically after an input change [FX-tb]. BizHawk binds "Seek To Green Arrow" (R) and "Toggle Auto-Restore" (Alt+R) [BH-bind].
- Follow cursor auto-scrolls to playback [FX-tb]. Stasis has Follow, disabled by selecting or scrolling [S-plan].

## Branches and bookmarks

- FCEUX: 10 bookmark slots. "Jump" moves the cursor; "load branch" replaces the whole movie with the bookmarked copy. A branch tree shows which branches are compatible [FX-tb] [FX-gloss].
- BizHawk: unlimited branches with screenshot, timestamp, markers, change log and user text [BH-branch].
- Stasis: a branch is a new bundle that continues from a parent tick and reads earlier history from the parent [S-gloss].

## Undo

- FCEUX History log: every operation on input, markers and bookmarks, with the current item highlighted; click an item to go back to it [FX-tb]. Undo also flashes the changed rows purple [FX-pr].
- BizHawk: Undo/Redo, Show Undo History, Selection Undo/Redo, and a "Max Undo Count" setting [BH-menu] [BH-set].
- libTAS: an input change log window with undo/redo [L-ui].
- Stasis: draft Undo/Redo, bounded to 256 transactions, process-local; it does not rewind gameplay [S-plan].

## Recording modes, multitrack, autofire and autohold

- **Multitrack:** record player 1, reload, switch to player 2's track; player 1's input replays while you enter player 2's [TV-basic]. FCEUX: All/1P/2P radio buttons [FX-tb].
- **Superimpose:** FCEUX's three-state checkbox ORs new recording into existing input [FX-tb].
- **Column lock:** libTAS can lock an input column so recording leaves it alone [L-ui].
- **Autofire / autohold:** BasicTools defines autofire as pressing every other frame and autohold as holding every frame, and notes frame advance usually makes them unnecessary [TV-basic]. TAStudio paints with patterns, with an "Account for lag frames" option so patterns skip lag [BH-set]. Dolphin sets turbo press and release lengths in frames [DL-tas]. Celeste has AutoInput and StunPause for periodic inputs [CT-cmd].

For KotOR, "multitrack" maps to separate keyboard, mouse-button and mouse-motion tracks. Locking the camera track while redoing movement is the useful case.

## Splicing and merging

- FCEUX Splicer: Copy, Paste, Cut, Clear, Delete, Insert and Clone, with selection size and clipboard shown [FX-tb].
- BizHawk adds Paste Insert, Clone # Times, Insert # of Frames, Truncate Movie, "Save Selection to Macro" and "Place Macro at Selection" [BH-menu].
- Celeste `Read` pulls inputs from another file starting at a label [CT-cmd]. Its improvement workflow merges new movement into the old at a resync point (a transition, a landing, a wall stop) [CT-start].
- Stasis has `@include` and `@repeat` [S-score]. Range copy/paste with grouped undo is the next planned UI work [S-plan].

## Memory tools and scripting

- RAM search narrows candidate addresses by comparing values between frames; RAM watch shows chosen addresses live. Both are standard [TV-feat]. libTAS adds pointer scan and hex view [L-ui]. Hourglass searches committed read-write pages [HG-ramsearch].
- Lua: libTAS callbacks run onInput → frame → onFrame → onPaint [L-lua]. BizHawk adds input-poll, load-state and bus read/write/exec events [TV-bhlua].
- Stasis's strategy puts Lua at the lowest priority [S-strat].

## Speed control and input display

- TASVideos' feature list includes slow motion at 50, 25, 12, 6 and 3 percent, fast-forward, rewind and an information display such as a frame counter [TV-feat].
- Input display shows the keys pressed during playback. It helps check that a button registered [TV-basic]. libTAS OSD can show frame count, inputs, messages and RAM watches [L-opt]. gz has an input display on by default and a lag counter off by default [GZ].
- Publication encodes carry subtitles with game, author, time and rerecord count [TV-pub]. The page examined says nothing about input display in published encodes.

## Sources

- [BH-bind] https://github.com/TASEmulators/BizHawk/blob/c2a683fc1fc4a7e3cfd593ccab9e061482c824aa/src/BizHawk.Client.Common/config/Binding.cs#L29-L153
- [BH-edit] https://github.com/TASEmulators/BizHawk/blob/c2a683fc1fc4a7e3cfd593ccab9e061482c824aa/src/BizHawk.Client.Common/movie/tasproj/TasMovie.Editing.cs#L25-L60
- [BH-zw] https://github.com/TASEmulators/BizHawk/blob/c2a683fc1fc4a7e3cfd593ccab9e061482c824aa/src/BizHawk.Client.Common/movie/tasproj/ZwinderStateManagerSettings.cs#L105-L113
- [BH-paged] https://github.com/TASEmulators/BizHawk/blob/c2a683fc1fc4a7e3cfd593ccab9e061482c824aa/src/BizHawk.Client.Common/tools/TAStudio/PagedStateManager.cs#L54-L86
- [BH-branch] https://github.com/TASEmulators/BizHawk/blob/c2a683fc1fc4a7e3cfd593ccab9e061482c824aa/src/BizHawk.Client.Common/movie/tasproj/TasBranch.cs#L12-L39
- [BH-io] https://github.com/TASEmulators/BizHawk/blob/c2a683fc1fc4a7e3cfd593ccab9e061482c824aa/src/BizHawk.Client.Common/movie/tasproj/TasMovie.IO.cs#L20-L45
- [BH-pal] https://github.com/TASEmulators/BizHawk/blob/c2a683fc1fc4a7e3cfd593ccab9e061482c824aa/src/BizHawk.Client.EmuHawk/tools/TAStudio/TAStudioPalette.cs#L13-L24
- [BH-set] https://github.com/TASEmulators/BizHawk/blob/c2a683fc1fc4a7e3cfd593ccab9e061482c824aa/src/BizHawk.Client.EmuHawk/tools/TAStudio/TAStudioSettingsForm.Designer.cs#L368-L927
- [BH-menu] https://github.com/TASEmulators/BizHawk/blob/c2a683fc1fc4a7e3cfd593ccab9e061482c824aa/src/BizHawk.Client.EmuHawk/tools/TAStudio/TAStudio.Designer.cs
- [L-key] https://github.com/clementgallet/libTAS/blob/32245db8e35ab8f2389bdb781e81ab5e5b92c4db/src/program/KeyMapping.cpp#L51-L87
- [L-wheel] https://github.com/clementgallet/libTAS/blob/32245db8e35ab8f2389bdb781e81ab5e5b92c4db/src/program/ui/InputEditorView.cpp#L430-L447
- [L-rew] https://github.com/clementgallet/libTAS/blob/32245db8e35ab8f2389bdb781e81ab5e5b92c4db/src/program/ui/InputEditorModel.cpp#L1286-L1330
- [L-rr] https://github.com/clementgallet/libTAS/blob/32245db8e35ab8f2389bdb781e81ab5e5b92c4db/src/program/SaveState.cpp#L229-L233
- [L-ui] https://github.com/clementgallet/libTAS/tree/32245db8e35ab8f2389bdb781e81ab5e5b92c4db/src/program/ui
- [L-how] https://clementgallet.github.io/libTAS/guides/how/
- [L-opt] https://clementgallet.github.io/libTAS/guides/options/
- [L-lua] https://clementgallet.github.io/libTAS/guides/lua/
- [FX-ctl] https://fceux.com/web/help/taseditor/Controls.html
- [FX-tb] https://fceux.com/web/help/taseditor/Toolbox.html
- [FX-pr] https://fceux.com/web/help/taseditor/PianoRoll.html
- [FX-gloss] https://fceux.com/web/help/taseditor/Glossary.html
- [CT-ctl] https://github.com/EverestAPI/CelesteTAS-EverestInterop/wiki/Controls
- [CT-cmd] https://github.com/EverestAPI/CelesteTAS-EverestInterop/wiki/Commands
- [CT-start] https://github.com/EverestAPI/CelesteTAS-EverestInterop/wiki/Getting-Started
- [DL-tas] https://github.com/dolphin-emu/dolphin/blob/0961ec1d87a51b7ede57cc4df7629b7f92d0d9d8/Source/Core/DolphinQt/TAS/TASInputWindow.cpp#L57-L63
- [HG-ramsearch] https://github.com/TASVideos/hourglass-win32/blob/78fa7c6d2be1b5eb46b8d4c0a37e56af616e1a46/src/wintaser/ramsearch.cpp#L197-L206
- [GZ] https://github.com/glankk/gz/blob/1155b00b260d8c205a81f8d6b560077a4abbe784/USAGE.md#L44-L58
- [TV-basic] https://tasvideos.org/TASingGuide/BasicTools
- [TV-feat] https://tasvideos.org/EmulatorResources/Features
- [TV-gloss] https://tasvideos.org/Glossary
- [TV-bhrr] https://tasvideos.org/Bizhawk/Rerecording
- [TV-bhlua] https://tasvideos.org/Bizhawk/LuaFunctions
- [TV-pub] https://tasvideos.org/EncodingGuide/PublicationManual
- [S-ctl] Stasis controls: [../controls.md](../controls.md)
- [S-score] Stasis score: [../score.md](../score.md)
- [S-gloss] Stasis glossary: [../glossary.md](../glossary.md)
- [S-holo] Stasis holorecord: [../holorecord.md](../holorecord.md)
- [S-tiers] Stasis tiers: [../tiers.md](../tiers.md)
- [S-ui] Stasis UI preferences and keymap: [../ui-preferences.md](../ui-preferences.md)
- [S-plan] Stasis canonical plan: [../plans/2026-10-02-2022-feat-kptas-complete-product-plan.md](../plans/2026-10-02-2022-feat-kptas-complete-product-plan.md)
- [S-strat] Stasis strategy: [../../STRATEGY.md](../../STRATEGY.md)
