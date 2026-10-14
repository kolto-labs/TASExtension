# Gap analysis: what Stasis should borrow

A ranked list of features that established TAS tools have and Stasis does not yet have, or has only in part. Each item says why it matters, how others do it, and a suggested approach for Stasis's Python/Qt host and native shim. These are recommendations, not plan entries. The canonical plan stays the source of truth for owners and milestones [S-plan].

Tags point to the source list at the end. Quoted text is copied from source pulled with git or curl.

## How items are ranked

The strategy says to resist a change that "displaces the current gameplay objective without removing its bottleneck" [S-strat]. The measured bottlenecks are restore time and authoring latency: rewind "can still take tens of seconds" [S-ctl], and branch ancestry causes "growing 10–16 second authoring latency" [S-plan]. Items that cut those come first. Evidence and standards come next, then editing comfort, then practice features. Lua stays last, as the strategy ranks it [S-strat].

## Already in Stasis (not gaps)

Checked against the Stasis docs so they are not listed as missing: greenzone ticks and restore [S-gloss], branches as child bundles [S-gloss], `@mark`, `@segment`, `@sync`, `@include`, `@repeat`, per-segment `hz` [S-score], `telos diff` and `bisect` [S-gloss], determinism key, virtual clock and reference math [S-gloss], draft Undo/Redo bounded to 256 transactions, Follow, frame back and advance, exact seek [S-plan], remappable shortcuts in `keymap.toml` [S-ui], the loopback HTTP control API [S-ctl], and a designed but unbuilt mechanic-sequence checklist [S-plan]. Range copy/paste with grouped undo is already the next planned UI step [S-plan].

## Ranked gaps

### 1. Tiered state retention near the edit point

- **Why it matters.** It is the direct fix for the current bottleneck. A TAS editor is used by stepping back a few ticks, changing one input and replaying. If each step back costs tens of seconds, iteration stops.
- **How others do it.** BizHawk Zwinder keeps a Current buffer (256 MB, 500-frame target) "used near the last edited frame", a Recent buffer where those states "decay", a Gaps buffer to "re-greenzone" old areas, and Ancient states every 5000 frames [BH-zw]. The newer Paged manager takes one memory limit, groups states into new, mid and old with a frames-between value each, accepts states out of order after loading a branch, and can keep marker states whatever the spacing [BH-paged]. FCEUX keeps a state for every emulated frame [FX-gloss]. libTAS forks to save in the background and stores only soft-dirty pages [L-how].
- **Suggested for Stasis.** Stasis already captures every tick into a content-addressed page store, with keyframes every 600 ticks [S-holo]. The missing piece is a hot tier. In the host, keep fully materialized page maps for a window of ticks around the playhead and the last edit, in RAM or a tmpfs ring, evicting by distance like Zwinder. In the shim, restore from that ring by mapping or copying pages directly, before falling back to keyframe-plus-deltas. Add keyframes at every `@mark` and at the last edited tick ("markers keep states"). Report hot hits and misses in the seek receipt so the gain is measured, not assumed.
- **Measured (2026-10-04, lane 2 receipts, `capture.every = 30`, `canary_ticks = 8`).** A rewind to a restore point took 1.7 to 2.9 s on a quiet disk. `seek` took 1.2 to 2.2 s of that. Seal, reopen, branch and persist took about 0.5 s. When the disk was busy (runs b11), those four steps grew to 10 to 20 s. The native restore, including both full audits, took 60 to 235 ms (shim log, about 9,000 pages), and wrote 59 to 1,059 pages. No `seek-phase` profile was recorded, so this split is an estimate from the lap phases, the shim log and per-tick host phases: most of the seek is likely the continuation canary, which replays 8 ticks with a capture on every tick, and two restore round trips (files, pages, GL). A rewind to an uncaptured tick also replays K..T into the branch. In kpm-b4r that took 0.85 to 2.1 s. Capturing every tick makes play about 5 times slower (30 ticks: 6.5 to 11 s against 1.3 to 2.4 s), so a dense capture window is not a free win. With the default `canary_ticks = 120`, the canary alone would cost tens of seconds.
- **Added, pending live measurement.** `seek.hot_tier_mb` (default 512, 0 turns it off) adds a RAM tier of verified object bytes that survives the bundle reopen of each rewind. `seek.hot_maps` (default 32) sets how many resolved page maps a session keeps. A fix stores the session map cache as bare refs, because telos reads the same dict. The old `(root, refs)` values could make a rewind that follows another rewind, with no step between, fall back to writing every page. This was found by reading the code and has not yet been seen in a log. `KPTAS_SEEK_PROFILE=1` logs `seek-hot` hits and misses.
- **Deferred.** Use the canary's replay as the re-record span for hybrid rewinds (in telos). Keep the parent bundle open across rewinds rather than reopening it. Add an opt-in dense-capture window after an edit. Native snapshot slots would not help, because the native apply is already under 0.25 s.

### 2. Seek feedback: progress, cancel, turbo, return to pre-edit tick

- **Why it matters.** While restore is slow, the user needs to see progress, cancel a mistaken seek, and get back to where they were after editing the past.
- **How others do it.** FCEUX shows a progress bar "to estimate the time needed for Playback seeking", has Turbo seek, and an "Auto-restore last position" option [FX-tb]. A green arrow marks the pre-edit position [FX-pr]. BizHawk binds Cancel Seek, "Seek To Green Arrow" (R), "Toggle Auto-Restore" (Alt+R) and "Toggle Turbo Seek" (Shift+S) [BH-bind]. libTAS freezes scrolling until the seek target is reached and can return to the current frame after a rewind edit ("Rewind seeks to current frame") [L-rew].
- **Suggested for Stasis.** Have the controller publish seek phase and progress (restore, verify, replay n of m ticks) on `GET /status` and in the Qt status line. Add a `cancel_seek` command that stops at the next safe gate. Store the pre-edit tick when a past edit becomes a branch, show it as a marker, and add one action to replay the child up to it.

### 3. Lag and non-draw counters, with row colors

- **Why it matters.** "Lag" has three meanings across tools (see [conventions.md](conventions.md)). For KotOR, the ticks where the game ignores input (loads, fades, modal stalls) are where frames are lost and where save-buffer tricks live.
- **How others do it.** The lag counter goes up "whenever a game fails to poll for input" [TV-feat]. BizHawk keeps a lag log in the project and colors lag rows red, with stated and invalidated variants [BH-io] [BH-pal]. libTAS marks non-draw frames red [L-how]. gz counts lag from the VI counter [GZ-hud].
- **Suggested for Stasis.** The holorecord frame index already records presents per tick and flags ticks with none as `repeat` [S-holo], so non-draw ticks can be derived now. Polling lag needs one new journal field: a per-tick count of the game's input reads that the shim can see (device-state reads and key-state queries). Then derive two flags per tick: no input read (polling lag) and no present (non-draw). Show both as piano-roll row tints and two HUD counters, each named for what it counts. Option for patterns and autofire to skip lag ticks, like BizHawk's "Account for lag frames" [BH-set].

### 4. A rerecord count that can be trusted

- **Why it matters.** TASVideos publication subtitles show it [TV-pub], and every major format stores it [TV-bk2] [L-fmt] [TV-lsmv]. No rerecord count was found in the Stasis docs.
- **How others do it.** libTAS counts a state load only "If the movie was modified since last state load" [L-rr]. lsnes computes the count from `rrdata` records and ignores the stored number on load [TV-lsmv]. Celeste counts file edits [CT-start].
- **Suggested for Stasis.** Count a restore that follows an applied input change, using libTAS's rule. Derive it from journal entries (restore records already exist [S-holo]) across the branch ancestry, like lsnes, rather than trusting a header integer. Show it in the HUD and in exported metadata.

### 5. Start from a save, with a verification run

- **Why it matters.** KotOR categories run for hours, and practicing The Routine starts from prepared saves [S-plan]. TASVideos wants save-anchored movies to come with a verification movie that starts from power-on and creates the exact save used [TV-rules]. Stasis records `start=save:<sha256>`, but "no backend can reload a save yet" [S-score].
- **How others do it.** lsnes embeds the anchor state with a SHA-256 trailer and `moviesram.*` files [TV-lsmv]. Dolphin stores memory-card and clear-save flags [DL-movie]. BizHawk marks `StartsFromSavestate` and can start a project from SaveRam [TV-bk2] [BH-menu].
- **Suggested for Stasis.** Stage the save files whose tree hash equals the header sha into the pin's `Saves` before launch, then load them with real inputs from the main menu inside the score. That keeps the start faithful to what a player does. Add a `verifies=<bundle id>` link from a boot-start score whose final files tree contains that save. The judge then checks the two together.

### 6. Branch thumbnails, notes and a tree

- **Why it matters.** With many children of one parent, users choose by sight and memory, not by bundle id.
- **How others do it.** BizHawk branches store the core and OSD frame buffers, a timestamp, markers, change log, UUID and "UserText"; the box has "Add with Text" and "Update with Text" [BH-branch] [BH-bbox]. FCEUX draws a Branches Tree [FX-tb].
- **Suggested for Stasis.** At branch creation, save a small PNG of the accepted gate image and a free-text note in the child bundle. Show the ancestry as a tree with thumbnails, delta ticks to each mark (item 13), and determinism key match.

### 7. Marker notes and movie annotations

- **Why it matters.** A TAS is reviewed by others. Notes explain why a frame matters; annotations record settings and versions a judge needs.
- **How others do it.** FCEUX markers carry a one-line note and can be searched for similar notes [FX-gloss] [FX-tb]. BizHawk has "Set Marker with Text" and Comments and Subtitles in `.bk2` [BH-menu] [TV-bk2]. libTAS has `annotations.txt` for versions, libraries and options [L-fmt] [L-opt]. TASVideos requires changed libTAS settings to be documented in annotations or the submission text [TV-libtas].
- **Suggested for Stasis.** Allow `@mark NAME "note"` and an `author=` header key. Add a bundle `annotations.md` that `telos` fills with the determinism key fields, DLL hash and env pin, followed by free text.

### 8. Memory watch and search, read from the recording

- **Why it matters.** DLZ needs a party member's x to equal a trigger corner "to the bit" [S-gloss]. Getting that by input alone needs live numeric feedback.
- **How others do it.** RAM search and watch are standard [TV-feat]. libTAS adds pointer scan and hex view [L-ui]. Hourglass searches committed read-write pages [HG-ramsearch]. The KotOR autosplitter already holds pointer paths for area name, load bar, end state and a tick count [KS-asl].
- **Suggested for Stasis.** Read watches from the captured page store at any tick, live or recorded, without touching the process. Seed a watch file from the autosplitter's pointer paths. Search compares values across chosen ticks in the store. Mark watches as read-only evidence, separate from lab writes [S-tiers].

### 9. Frame-window finder ("early or late by N ticks")

- **Why it matters.** Runners learn a trick faster when told the true window and how far off they were. No primary source was found for a tool that measures this; it is a Stasis opportunity, not a copy.
- **How others do it.** The closest are SF6's per-frame meter and held-frame input history [SF6-eh] [SF6-infil], TM-CE's action-state OSDs [TMCE], and LiveSplit segment deltas [LS-comp]. Brute-force tools run the search side of the same idea [TV-basicbot] [BXTRS].
- **Suggested for Stasis.** Given a note and a success predicate (a watch value or an `@sync` event), restore the tick before the note and run child branches with the note shifted −k..+k ticks. Report the window and where the attempt fell, for example "late by 2 ticks; window 3 ticks". This depends on item 1 for speed and item 10 for the predicate. Results feed the planned checklist [S-plan].

### 10. Evaluate `@assert`

- **Why it matters.** `@assert` is parsed but "not evaluated yet" [S-score]. Without it, automated searches and regression replays cannot stop on a wrong outcome.
- **How others do it.** Celeste's `Assert` aborts the TAS when its condition is false [CT-cmd]. SAR's `check` tool validates position and angle [P2].
- **Suggested for Stasis.** Evaluate expressions over item 8's watches at the gate. A failure stops the run with a receipt naming tick, expression and values.

### 11. Brute-force input search

- **Why it matters.** Small windows such as DLZ alignment or save-buffer timing are cheaper to search than to hand-tune.
- **How others do it.** BasicBot tries random inputs over a frame range to maximize or minimize RAM values with tie-breakers; its page calls it "a dumb bot" that "does not learn" [TV-basicbot]. bxt-rs ships a "Brute-force optimizer for TASes" [BXTRS].
- **Suggested for Stasis.** A host command that takes a tick range, allowed keys and an objective over watches, and drives headless branches through the HTTP API. Keep the best score as a branch with its search log in annotations. It is lab work until the winning score is replayed under tas [S-tiers].

### 12. Resync helper for merging an improvement

- **Why it matters.** Celeste's improvement workflow merges new movement into old at a resync point such as a transition or a stop [CT-start]. Finding that point by hand is slow.
- **How others do it.** Celeste users comment out old inputs and compare times [CT-start]. BizHawk's read-only timeline check reports the frame where a state diverges from the movie [TV-bhrr].
- **Suggested for Stasis.** Content-addressed state roots make this cheap. For a child that diverged at tick t, search for ticks where the child's root equals the parent's root at some later tick (a rejoin). The parent's tail can then be appended with a tick offset and verified by root. When no exact rejoin exists, fall back to `diff` on chosen watches. No counterpart was found in the sources examined.

### 13. Segment comparison between branches

- **Why it matters.** It answers "did this edit save time, and where".
- **How others do it.** LiveSplit shows Previous Segment, Possible Time Save, Sum of Best and a delta against a comparison [LS-comp]. Celeste fills `ChapterTime:` after playback and improvements are posted as frame deltas [CT-start] [CT-cmd].
- **Suggested for Stasis.** For each `@mark` reached by both a branch and its reference, show the tick delta. Also show time in µs from the virtual clock and the real-time-without-loads figure, using the speedrun.com Any% start and end rules [SRC-api] and the autosplitter's load-bar test [KS-asl].

### 14. Input patterns, column lock and insert mode

- **Why it matters.** KotOR inputs are long holds with rhythmic taps (save buffering, menu sequences). Painting each tick is slow.
- **How others do it.** Autofire and autohold [TV-basic]. TAStudio pattern painting with custom per-movie patterns [BH-set]. Dolphin turbo press and release durations [DL-tas]. libTAS "Lock input column" and "Paste Insert" [L-ui]. FCEUX Superimpose [FX-tb].
- **Suggested for Stasis.** Add pattern fill for a selected range (press n, release m, optional lag skip), a lock on the mouse and camera track, and an Insert mode that shifts later notes. All of these write drafts, which apply only through a branch, as today [S-plan].

### 15. Visible undo history and hot changes

- **Why it matters.** Draft undo exists [S-plan], but a list shows what each step did and lets the user jump back several steps at once.
- **How others do it.** FCEUX's History log lists each operation and undoes to a clicked entry [FX-tb]. Recent edits glow from bright orange and cool by one level per later edit, and undone rows flash purple [FX-pr]. BizHawk has "Show Undo History" and "Max Undo Count" [BH-menu] [BH-set].
- **Suggested for Stasis.** Show the 256-entry draft history as a labeled list. Tint recently edited cells by age.

### 16. Time-query trace

- **Why it matters.** Spin-waits are the classic PC TAS time leak [L-how]. A KotOR thread that polls the clock between gates would never see time move under a virtual clock.
- **How others do it.** libTAS has a time-trace window and busy-loop detection that hashes time-query call stacks [L-ui] [L-busy]. Hourglass caps queries per API before forcing time forward [HG-time].
- **Suggested for Stasis.** In the shim, count clock calls per tick by return address and thread, and journal a summary under a lab diagnostic flag. Flag any thread that calls the clock many times within one tick.

### 17. Audio in captures, and encode conventions

- **Why it matters.** TASVideos requires audio/video dumping or external recording [TV-req], and publication subtitles carry game, author, time and rerecords [TV-pub]. Stasis `--video` has no audio [S-ctl].
- **How others do it.** libTAS encodes through FFmpeg from its own mixer [L-gh] [L-how].
- **Suggested for Stasis.** If the virtual Miles model can produce samples, write a WAV beside the video ledger keyed by tick. Add an optional subtitle track with author, rerecords and time.

### 18. Slow-motion fractions

- **Why it matters.** Watching a trick at reduced speed is how people check it before frame-stepping.
- **How others do it.** TASVideos lists slow motion at 50, 25, 12, 6 and 3 percent [TV-feat]. CelesteTAS has a slow-forward key [CT-ctl].
- **Suggested for Stasis.** Add `pace=0.5` style values next to `pace=gate` and `pace=realtime` [S-score], and a UI speed control. Virtual time per tick does not change, so sync is unaffected.

### 19. Scripting callbacks

- **Why it matters.** Community additions want per-tick hooks. The strategy ranks this lowest [S-strat].
- **How others do it.** libTAS calls onInput, onFrame, then onPaint, and drawing is only allowed in onPaint [L-lua]. BizHawk adds input-poll, load-state and memory bus events [TV-bhlua].
- **Suggested for Stasis.** When the time comes, mirror those three phases as events on the existing HTTP API, then wrap them in Lua.

## Top 15 in one table

| # | Gap | Main source |
|---|---|---|
| 1 | Tiered hot state retention near edits and marks | BizHawk Zwinder and Paged [BH-zw] [BH-paged] |
| 2 | Seek progress, cancel, turbo, return to pre-edit tick | FCEUX, BizHawk, libTAS [FX-tb] [BH-bind] [L-rew] |
| 3 | Lag and non-draw counters with row tints | [TV-feat] [BH-pal] [L-how] |
| 4 | Rerecord count derived from the journal | libTAS rule, lsnes rrdata [L-rr] [TV-lsmv] |
| 5 | Save-anchored start with verification run | TASVideos rules [TV-rules] |
| 6 | Branch thumbnails, notes and tree | [BH-branch] [FX-tb] |
| 7 | Marker notes and annotations | [FX-gloss] [L-fmt] |
| 8 | Memory watch and search from the page store | [L-ui] [KS-asl] |
| 9 | Frame-window finder | Stasis-specific; [SF6-infil] [TV-basicbot] |
| 10 | Evaluated `@assert` | Celeste Assert [CT-cmd] |
| 11 | Brute-force input search | BasicBot, bxt-rs [TV-basicbot] [BXTRS] |
| 12 | Resync helper by state-root rejoin | Celeste workflow [CT-start] |
| 13 | Segment deltas between branches | LiveSplit, Celeste [LS-comp] [CT-cmd] |
| 14 | Patterns, column lock, insert mode | [BH-set] [L-ui] [DL-tas] |
| 15 | Visible undo history and hot changes | FCEUX [FX-tb] [FX-pr] |

## Sources

- [BH-zw] https://github.com/TASEmulators/BizHawk/blob/c2a683fc1fc4a7e3cfd593ccab9e061482c824aa/src/BizHawk.Client.Common/movie/tasproj/ZwinderStateManagerSettings.cs#L39-L109
- [BH-paged] https://github.com/TASEmulators/BizHawk/blob/c2a683fc1fc4a7e3cfd593ccab9e061482c824aa/src/BizHawk.Client.Common/tools/TAStudio/PagedStateManager.cs#L17-L86
- [BH-bind] https://github.com/TASEmulators/BizHawk/blob/c2a683fc1fc4a7e3cfd593ccab9e061482c824aa/src/BizHawk.Client.Common/config/Binding.cs#L136-L153
- [BH-io] https://github.com/TASEmulators/BizHawk/blob/c2a683fc1fc4a7e3cfd593ccab9e061482c824aa/src/BizHawk.Client.Common/movie/tasproj/TasMovie.IO.cs#L20-L45
- [BH-pal] https://github.com/TASEmulators/BizHawk/blob/c2a683fc1fc4a7e3cfd593ccab9e061482c824aa/src/BizHawk.Client.EmuHawk/tools/TAStudio/TAStudioPalette.cs#L13-L24
- [BH-set] https://github.com/TASEmulators/BizHawk/blob/c2a683fc1fc4a7e3cfd593ccab9e061482c824aa/src/BizHawk.Client.EmuHawk/tools/TAStudio/TAStudioSettingsForm.Designer.cs#L368-L927
- [BH-menu] https://github.com/TASEmulators/BizHawk/blob/c2a683fc1fc4a7e3cfd593ccab9e061482c824aa/src/BizHawk.Client.EmuHawk/tools/TAStudio/TAStudio.Designer.cs
- [BH-branch] https://github.com/TASEmulators/BizHawk/blob/c2a683fc1fc4a7e3cfd593ccab9e061482c824aa/src/BizHawk.Client.Common/movie/tasproj/TasBranch.cs#L12-L39
- [BH-bbox] https://github.com/TASEmulators/BizHawk/blob/c2a683fc1fc4a7e3cfd593ccab9e061482c824aa/src/BizHawk.Client.EmuHawk/tools/TAStudio/BookmarksBranchesBox.Designer.cs
- [FX-tb] https://fceux.com/web/help/taseditor/Toolbox.html
- [FX-pr] https://fceux.com/web/help/taseditor/PianoRoll.html
- [FX-gloss] https://fceux.com/web/help/taseditor/Glossary.html
- [L-how] https://clementgallet.github.io/libTAS/guides/how/
- [L-fmt] https://clementgallet.github.io/libTAS/guides/format/
- [L-opt] https://clementgallet.github.io/libTAS/guides/options/
- [L-lua] https://clementgallet.github.io/libTAS/guides/lua/
- [L-gh] https://github.com/clementgallet/libTAS
- [L-rr] https://github.com/clementgallet/libTAS/blob/32245db8e35ab8f2389bdb781e81ab5e5b92c4db/src/program/SaveState.cpp#L229-L233
- [L-rew] https://github.com/clementgallet/libTAS/blob/32245db8e35ab8f2389bdb781e81ab5e5b92c4db/src/program/ui/InputEditorModel.cpp#L1286-L1330
- [L-ui] https://github.com/clementgallet/libTAS/tree/32245db8e35ab8f2389bdb781e81ab5e5b92c4db/src/program/ui
- [L-busy] https://github.com/clementgallet/libTAS/blob/32245db8e35ab8f2389bdb781e81ab5e5b92c4db/src/library/BusyLoopDetection.cpp
- [HG-time] https://github.com/TASVideos/hourglass-win32/blob/78fa7c6d2be1b5eb46b8d4c0a37e56af616e1a46/src/wintasee/dettime.cpp#L286-L305
- [HG-ramsearch] https://github.com/TASVideos/hourglass-win32/blob/78fa7c6d2be1b5eb46b8d4c0a37e56af616e1a46/src/wintaser/ramsearch.cpp#L197-L206
- [DL-movie] https://github.com/dolphin-emu/dolphin/blob/0961ec1d87a51b7ede57cc4df7629b7f92d0d9d8/Source/Core/Core/Movie.h#L86-L140
- [DL-tas] https://github.com/dolphin-emu/dolphin/blob/0961ec1d87a51b7ede57cc4df7629b7f92d0d9d8/Source/Core/DolphinQt/TAS/TASInputWindow.cpp#L57-L63
- [GZ-hud] https://github.com/glankk/gz/blob/1155b00b260d8c205a81f8d6b560077a4abbe784/USAGE.md#L44-L58
- [CT-cmd] https://github.com/EverestAPI/CelesteTAS-EverestInterop/wiki/Commands
- [CT-ctl] https://github.com/EverestAPI/CelesteTAS-EverestInterop/wiki/Controls
- [CT-start] https://github.com/EverestAPI/CelesteTAS-EverestInterop/wiki/Getting-Started
- [P2] https://wiki.portal2.sr/TAS
- [BXTRS] https://github.com/YaLTeR/bxt-rs/blob/6ab51edead6e3f5eaf221acfe5ca24794157f224/README.md
- [KS-asl] https://github.com/kotor-speedruns/Autosplitter/blob/2060ae5ea8f38f02520111cf4e5a1e4e9f040388/K1.asl
- [SRC-api] https://www.speedrun.com/api/v1/games?name=Knights%20of%20the%20Old%20Republic&embed=categories,variables
- [SF6-eh] https://www.eventhubs.com/news/2022/sep/16/sf6-training-visual-frame-data
- [SF6-infil] https://words.infil.net/w03-sf6beta-p6.html
- [TMCE] https://github.com/AlexanderHarrison/TrainingMode-CommunityEdition
- [LS-comp] https://livesplit.org/components/
- [TV-feat] https://tasvideos.org/EmulatorResources/Features
- [TV-basic] https://tasvideos.org/TASingGuide/BasicTools
- [TV-bk2] https://tasvideos.org/Bizhawk/BK2Format
- [TV-lsmv] https://tasvideos.org/EmulatorResources/Lsnes/LSMV
- [TV-rules] https://tasvideos.org/MovieRules
- [TV-req] https://tasvideos.org/EmulatorResources/Requirements
- [TV-pub] https://tasvideos.org/EncodingGuide/PublicationManual
- [TV-libtas] https://tasvideos.org/EmulatorResources/LibTAS
- [TV-basicbot] https://tasvideos.org/Bizhawk/BasicBot
- [TV-bhrr] https://tasvideos.org/Bizhawk/Rerecording
- [TV-bhlua] https://tasvideos.org/Bizhawk/LuaFunctions
- [S-strat] [../../STRATEGY.md](../../STRATEGY.md)
- [S-plan] [../plans/2026-10-02-2022-feat-kptas-complete-product-plan.md](../plans/2026-10-02-2022-feat-kptas-complete-product-plan.md)
- [S-ctl] [../controls.md](../controls.md)
- [S-ui] [../ui-preferences.md](../ui-preferences.md)
- [S-gloss] [../glossary.md](../glossary.md)
- [S-score] [../score.md](../score.md)
- [S-holo] [../holorecord.md](../holorecord.md)
- [S-tiers] [../tiers.md](../tiers.md)
