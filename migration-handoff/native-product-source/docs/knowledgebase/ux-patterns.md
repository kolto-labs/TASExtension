# UX patterns worth copying

Interaction patterns from TAS editors, practice tools and fighting-game training modes, with a note on how each fits Stasis. Tags point to the source list at the end. Quoted text is copied from source pulled with git or curl.

## Piano roll ergonomics

| Pattern | Where | Detail |
|---|---|---|
| Drag-paint | FCEUX [FX-pr] | Click an empty cell to set a button, an occupied one to clear it. Holding the button and dragging vertically "leaves a trail". |
| Range by Shift, set by Ctrl | FCEUX [FX-pr] | Shift-click applies the click to every frame between it and the selection. Ctrl builds a non-contiguous selection. |
| Scrub with the wheel | FCEUX [FX-pr], libTAS [L-wheel] | Hold the right mouse button and roll the wheel to move playback. In libTAS, wheel down pushes one frame advance and wheel up seeks back one frame, only while paused. |
| Two cursors | FCEUX [FX-gloss] | Playback cursor (light-blue arrow) and selection (dark-blue rows) are separate. Stasis keeps a separate playhead and selection [S-plan]. |
| Green arrow | FCEUX [FX-pr] | Marks where playback was before an edit moved it. BizHawk "Seek To Green Arrow" is R [BH-bind]. |
| Hot changes | FCEUX [FX-pr] | The newest edits are bright orange and older ones "cool down by one level". |
| Undo flash | FCEUX [FX-pr] | Rows changed by Undo show purple briefly. |
| Row colors by state | BizHawk [BH-pal] | Separate colors for greenzone, greenzone-with-state, invalidated, lag, lag-with-state, invalidated lag, marker and current frame. Analog cells being edited turn gray. |
| State and marker markers | BizHawk [BH-set] | Options: "Denote states with icons", "Denote states with background color", "Denote markers with icons", "Denote markers with color on frame column". |
| Hide lag | BizHawk [BH-set] | "Lag frames to hide" collapses runs of lag rows. |
| Rotate | BizHawk [BH-set] | "Rotate input roll" turns the grid horizontal. |
| Copy with frame numbers | BizHawk [BH-set] | "Include frame numbers when copying input." |
| Scroll method | BizHawk [BH-set] | Scroll to view, top, bottom or center; "When following cursor, scroll at every frame". |
| Analog math | libTAS [L-ui] | "Multiply values by a factor" over a selection. |
| Column lock | libTAS [L-ui] | "Lock input column" keeps recording from touching it. |
| Marker hover | libTAS [L-hover] | Hovering a marked frame shows the marker text after 300 ms. |

## Markers and notes

- FCEUX: markers are yellow rows with a one-line note. Selection buttons jump between markers and search for "Similar" or "More" notes [FX-gloss] [FX-tb].
- BizHawk: "Set Marker with Text", "Select between Markers", "Remove Markers". "Bind markers to input" moves markers when frames are inserted or removed [BH-menu] [BH-set].
- libTAS: "Move markers on frame addition or removal", "Autopause on markers" [L-ui].
- Celeste: `#label` lines name rooms, and Studio inserts the current room name with Ctrl+R. `***` breakpoints fast-forward to that line on restart [CT-start] [CT-input].
- Stasis has `@mark NAME` (unique) and `@segment` [S-score]. Marker notes as free text are a gap; see [gap-analysis.md](gap-analysis.md).

## Branches with thumbnails

- A BizHawk branch keeps the core frame buffer and the OSD frame buffer, a timestamp, markers, its change log, a UUID and "UserText" [BH-branch]. The branch box offers Add, "Add with Text", Load, Update, "Update with Text", "Jump To", Remove and Undo [BH-bbox]. "Load branch by double clicking" is a setting [BH-set].
- FCEUX shows a Branches Tree of how movie versions relate [FX-tb].
- In Stasis, each branch is a child `.holo`. The holorecord frame index has a pixel digest per frame [S-holo]. A thumbnail per branch tick is a small addition.

## Recording modes

- **Insert and overwrite.** BizHawk separates Paste from "Paste Insert" and has "Insert # of Frames" and "Clone # Times" [BH-menu]. libTAS has Insert, "Insert # frames", Duplicate and "Paste Insert" [L-ui].
- **Superimpose.** FCEUX's three-state "Superimpose" blends newly recorded input with existing input [FX-tb].
- **Multitrack.** FCEUX records All or one player [FX-tb]. TASVideos describes the reload-and-record-the-other-track loop [TV-basic].
- **Patterns.** BizHawk "Use pattern for painting", "Set custom patterns (per movie)", "Auto-Fire columns only", "Account for lag frames" [BH-set]. Dolphin turbo press/release lengths [DL-tas].
- **Toggle-click.** Clicking a set cell clears it [FX-pr]; that is an XOR on one cell. No tool examined offers XOR over a range as a named mode.

For KotOR: the useful modes are overwrite (default), insert (push the rest of the score later) and a lock for the mouse/camera track. Stasis drafts already keep edits from executing until applied [S-plan].

## HUD: counters and input display

- BizHawk exposes Frame Counter, Lag Counter and Input Display as toggles [BH-bind].
- libTAS OSD shows frame count, inputs, notifications and RAM watches [L-opt].
- gz shows an input display (on by default), a lag counter (off by default) and a real-time timer (off by default) [GZ-hud].
- Stasis ties its actual-input HUD to executed inputs, not drafts [S-plan]. A lag counter and a rerecord counter are not found in its docs.

## Practice and trainer features

### Fighting-game training modes

- **Frame meter (Street Fighter 6).** A bar at the bottom of the screen where each pip is one frame. Colors show startup, active and recovery for a move or a whole combo, plus the opponent's recovery and traits such as invincibility [SF6-eh]. Frame advantage is computed live, so it accounts for meaty timing and spacing. The bar shows gaps in pressure. After 60 frames it wraps and keeps a ghost of the earlier frames [SF6-infil].
- **Input history with durations (SF6).** Input reading shows how many frames each direction is held, so a player can see button hold and release timing [SF6-eh].
- **Dummy recording with weights (SF6).** Several reversal options, each with a frequency, for example one at 75% and a 1-frame-delayed one at 25% [SF6-infil].
- **Respawn and savestate shortcuts (SF6).** Quick restarts and custom respawn positions without menus [SF6-infil].
- **Combo trials.** No primary source was fetched for SF6 or MK1 trial screens, and MK1 training specifics were only found in weak secondary sources. Stasis already has a design for a mechanic-sequence checklist: rows cross off only on acknowledged executed hits, restore and branch undo later progress, and unknown state pauses matching. Its runtime is pending [S-plan].

### Speedrun practice tools

- **gz (Ocarina of Time practice ROM).** "record macro" and "play macro", a macro frame counter, "trim macro" to drop inputs after the current frame, savestate slots that remember the macro frame so loading a state rewinds the macro, and "quick record movie" [GZ-macro]. While recording, room loads are forced synchronous so "load delay variations can not cause macros to desync" [GZ-notes]. RNG reseeds are stored in the macro and reused only if the RNG is still in sync and the reseed lands on the same macro frame; otherwise gz warns and reseeds normally [GZ-notes].
- **Melee Training Mode, Community Edition.** Input and info displays for both players, R as frame advance, recording slots that can be taken over at any point or played back as a counter action, custom action-state OSDs, a game speed option, and a HUD that updates every frame in the ledgedash event [TMCE]. The original UnclePunch mod adds event scenarios with automatic savestates and onscreen data not normally visible [UP].
- **LiveSplit.** Splits with delta against a comparison, Previous Segment, Possible Time Save ("the difference between the best segment time and the segment time of the personal best run"), Sum of Best, Detailed Timer, Run Prediction, Pace Placement and an attempt count in the title [LS-comp].
- **CelesteTAS.** `ChapterTime:` and `FileTime:` lines are filled in automatically after playback, so an edit shows its saving in frames right in the file. Old inputs are commented out (Ctrl+K) to compare versions. Improvements are posted with a frame delta and before/after times to a tracker bot [CT-start] [CT-cmd].

### "Early or late by N frames"

No primary source was found for a tool that tells the player "early by N frames" or "late by N frames" with a measured window. The closest sourced patterns are SF6's per-frame meter and held-frame input history [SF6-eh] [SF6-infil], TM-CE's action-state OSDs [TMCE], and LiveSplit's segment deltas [LS-comp].

Stasis can do better than pass/fail because it can replay. After a practice attempt, it can restore the start of the window and test the same input shifted by −k..+k ticks on child branches. The result is the true window, not a guess (see gap-analysis item 9).

## Text-script patterns

- Hold semantics, labels, comments and breakpoints: Celeste [CT-input].
- `Assert` aborts the TAS when a condition fails [CT-cmd]. Stasis keeps `@assert` but does not evaluate it yet [S-score].
- Tool layers that compile to raw input: SAR runs strafe, setang, autoaim and autojump tools, then writes a `_raw` script used for verification [P2]. This matches Stasis's split between tas evidence and lab or semantic experiments [S-tiers].
- Relative ticks `+N` and per-button hold counts: SAR [P2].

## Sources

- [FX-pr] https://fceux.com/web/help/taseditor/PianoRoll.html
- [FX-gloss] https://fceux.com/web/help/taseditor/Glossary.html
- [FX-tb] https://fceux.com/web/help/taseditor/Toolbox.html
- [BH-bind] https://github.com/TASEmulators/BizHawk/blob/c2a683fc1fc4a7e3cfd593ccab9e061482c824aa/src/BizHawk.Client.Common/config/Binding.cs#L29-L153
- [BH-pal] https://github.com/TASEmulators/BizHawk/blob/c2a683fc1fc4a7e3cfd593ccab9e061482c824aa/src/BizHawk.Client.EmuHawk/tools/TAStudio/TAStudioPalette.cs#L13-L24
- [BH-set] https://github.com/TASEmulators/BizHawk/blob/c2a683fc1fc4a7e3cfd593ccab9e061482c824aa/src/BizHawk.Client.EmuHawk/tools/TAStudio/TAStudioSettingsForm.Designer.cs#L368-L927
- [BH-menu] https://github.com/TASEmulators/BizHawk/blob/c2a683fc1fc4a7e3cfd593ccab9e061482c824aa/src/BizHawk.Client.EmuHawk/tools/TAStudio/TAStudio.Designer.cs
- [BH-branch] https://github.com/TASEmulators/BizHawk/blob/c2a683fc1fc4a7e3cfd593ccab9e061482c824aa/src/BizHawk.Client.Common/movie/tasproj/TasBranch.cs#L12-L39
- [BH-bbox] https://github.com/TASEmulators/BizHawk/blob/c2a683fc1fc4a7e3cfd593ccab9e061482c824aa/src/BizHawk.Client.EmuHawk/tools/TAStudio/BookmarksBranchesBox.Designer.cs
- [L-wheel] https://github.com/clementgallet/libTAS/blob/32245db8e35ab8f2389bdb781e81ab5e5b92c4db/src/program/ui/InputEditorView.cpp#L430-L447
- [L-hover] https://github.com/clementgallet/libTAS/blob/32245db8e35ab8f2389bdb781e81ab5e5b92c4db/src/program/ui/InputEditorView.cpp#L449-L460
- [L-ui] https://github.com/clementgallet/libTAS/tree/32245db8e35ab8f2389bdb781e81ab5e5b92c4db/src/program/ui
- [L-opt] https://clementgallet.github.io/libTAS/guides/options/
- [DL-tas] https://github.com/dolphin-emu/dolphin/blob/0961ec1d87a51b7ede57cc4df7629b7f92d0d9d8/Source/Core/DolphinQt/TAS/TASInputWindow.cpp#L57-L63
- [GZ-hud] https://github.com/glankk/gz/blob/1155b00b260d8c205a81f8d6b560077a4abbe784/USAGE.md#L44-L58
- [GZ-macro] https://github.com/glankk/gz/blob/1155b00b260d8c205a81f8d6b560077a4abbe784/USAGE.md#L301-L353
- [GZ-notes] https://github.com/glankk/gz/blob/1155b00b260d8c205a81f8d6b560077a4abbe784/USAGE.md#L637-L697
- [TMCE] https://github.com/AlexanderHarrison/TrainingMode-CommunityEdition
- [UP] https://github.com/UnclePunch/Training-Mode
- [SF6-eh] https://www.eventhubs.com/news/2022/sep/16/sf6-training-visual-frame-data
- [SF6-infil] https://words.infil.net/w03-sf6beta-p6.html
- [LS-comp] https://livesplit.org/components/
- [CT-input] https://github.com/EverestAPI/CelesteTAS-EverestInterop/wiki/Input-File
- [CT-cmd] https://github.com/EverestAPI/CelesteTAS-EverestInterop/wiki/Commands
- [CT-start] https://github.com/EverestAPI/CelesteTAS-EverestInterop/wiki/Getting-Started
- [P2] https://wiki.portal2.sr/TAS
- [TV-basic] https://tasvideos.org/TASingGuide/BasicTools
- [S-plan] Stasis canonical plan: [../plans/2026-10-02-2022-feat-kptas-complete-product-plan.md](../plans/2026-10-02-2022-feat-kptas-complete-product-plan.md)
- [S-score] [../score.md](../score.md)
- [S-holo] [../holorecord.md](../holorecord.md)
- [S-tiers] [../tiers.md](../tiers.md)
