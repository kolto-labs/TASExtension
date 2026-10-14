# TAS systems and their designs

How established TAS tools are built, and what each one teaches a KotOR tool. Bracketed tags such as [L-how] point to the source list at the end. A sentence in quotation marks is copied from a source file pulled with git or curl. Everything else paraphrases the cited page.

Research date: 2026-10-04. GitHub links are pinned to the commit read that day.

## Summary table

| System | Target | How it controls the game | Frame boundary | State capture | Input editor | Movie file | TASVideos status |
|---|---|---|---|---|---|---|---|
| libTAS | Native Linux PC games. Partial Wine hooks exist. | LD_PRELOAD hooks, plus code patching with trampolines when symbols are not reachable [L-how] | Present/swap calls: SDL, GLX, Vulkan, X11 software blits [L-how] | Whole process: `/proc/self/pagemap` walk, threads stopped and recreated, optional incremental (soft-dirty), compressed, forked [L-how] | Qt grid with painting, analog edit, markers, undo change log, autohold/autofire columns [L-ui] | `.ltm` (tar.gz of config.ini, inputs, annotations.txt, editor.ini) [L-fmt] | Accepted for PC [TV-pc] [TV-libtas] |
| Hourglass | 32-bit Windows games, best on XP, no mouse [TV-hg] | DLL injected into the game; hooks time, wait, thread, sound, D3D, DirectDraw, GDI, SDL, input, file and registry APIs [HG-hooks] | Present-type hooks in d3d8/d3d9/ddraw/gdi/sdl modules call `FrameBoundary` [HG-hooks] | Committed read-write memory regions of the game process [HG-save-mem] | None found in sources examined; state slots and splice commands only (see prior research) | `.wtf` binary: keyboard state per frame [HG-movie] | Accepted, but unmaintained [TV-pc] [TV-hg] |
| BizHawk / TAStudio | Many consoles (emulated) | Emulator core | Emulated frame | Core savestates, managed by a tiered state manager [BH-zw] [BH-paged] | Piano roll with greenzone, lag, markers, branches with screenshots, patterns, undo history [BH-set] [BH-branch] | `.bk2` zip; `.tasproj` adds editor data [TV-bk2] [BH-io] | Accepted |
| FCEUX TAS Editor | NES | Emulator | Emulated frame | Greenzone of savestates for every emulated frame [FX-gloss] | The model TAStudio copied: piano roll, greenzone, 10 bookmarks with branch tree, markers with notes, history log, multitrack recorder [FX-tb] | `.fm2` text; `.fm3` adds editor modules [FX-fm2] [FX-fm3] | Accepted |
| lsnes | SNES, GB | Emulator | Frame, with subframes for multiple polls [TV-lsmv] | Core savestate; anchor savestate in movie [TV-lsmv] | Lua, memory map, debug functions [TV-lsnes] | `.lsmv` zip or binary chunks [TV-lsmv] | Accepted |
| Dolphin | GameCube, Wii | Emulator | VI | Emulator savestate | TAS Input windows with turbo press/release durations [DL-tas] | `.dtm` 256-byte header that stores sync settings [DL-movie] [TV-dtm] | Accepted |
| JPC-rr | DOS PC (x86 emulated in Java) | Emulator | Time-based; events carry nanosecond timestamps [TV-jrsr] | Emulator savestate with PROJECTID check [TV-jrsr] | None found in sources examined | `.jrsr` text sections [TV-jrsr] | Accepted (DOS) [TV-jpc] |
| Mupen64-rr | N64 | Emulator plus plugins | VI | Emulator snapshot | None found in sources examined | `.m64` binary header plus 4-byte samples [TV-m64] | Listed [TV-emu] |
| PSXjin | PSX | Emulator (PCSX overhaul) | Frame | Savestate | RAM search/watch, Lua [TV-psxjin] | `.pjm` | Obsolete; BizHawk replaces it [TV-psxjin] |
| Mednafen-rr | Several consoles | Emulator | Frame | Savestate | None found | `.mc2` [TV-mednafen] | Listed [TV-emu] |
| CelesteTAS + Studio | Celeste (PC, C# mod) | Game mod (Everest) | Game update | Mod savestates on breakpoints [CT-ctl] [CT-start] | Text editor (Studio) with live playback and auto-filled times [CT-start] | `.tas` text [CT-input] | Not a TASVideos tool. Specialized tools are not accepted [TV-pc] |
| SourcePauseTool (SPT) | Source engine games | Server plugin DLL | Engine tick | None found in sources examined | `.srctas` script, autostrafe, HUD [SPT] | `.srctas` | Specialized tool |
| Bunnymod XT / bxt-rs | Half-Life and mods | Injected DLL | Engine frame | None found in sources examined | bxt-rs has an interactive TAS editor and a brute-force optimizer [BXTRS] | `.hltas` [HLTAS] | Specialized tool |
| SAR (Portal 2) | Portal 2 | Plugin | Tick | None found in sources examined | `.p2tas` scripts with tools (strafe, setang, autoaim) and a generated `_raw` script for verification [P2] | `.p2tas` | Specialized tool |

"Spirit" from the brief was not identified. This document reads it as the family of Source-engine mod tools (SPT, SAR, BXT). Treat that as an open item, not a finding.

## libTAS

The closest analogue to Stasis: a PC game, run natively, controlled by hooks instead of emulation.

- **Hooking.** libtas.so is preloaded so its symbols win. It hooks `dlopen`/`dlsym` by parsing libc. When that fails it patches the function's first instructions with a jump and a trampoline [L-how].
- **Frame boundary.** A frame ends at a render call: `SDL_GL_SwapWindow`, `SDL_RenderPresent`, `glXSwapBuffers`, `vkQueuePresentKHR`, or an X11 shared-memory blit. Timer advance, audio mixing, input and state work all happen there [L-how].
- **Time.** A deterministic timer gains exactly one frame length per boundary. Sleeps can advance it never, on the main thread only (default) or always. Spin-waits are handled by "time tracking": count time queries and advance after a threshold [L-how]. Per-function thresholds are saved in the movie's `[mainthread_timetrack]` group, so a movie carries the settings that decide its sync [L-hdr]. Newer source adds busy-loop detection that hashes the call stack of time queries [L-busy].
- **Non-draw frames.** If time crosses a boundary before a render, libTAS emits a non-draw frame, shown red in the editor. These can differ across systems with different libc or GPU drivers [L-how].
- **Input.** The game has no access to real devices. libTAS hooks SDL1/2, Xlib, XCB, evdev/jsdev and udev, keeps its own event queues, and filters focus and enter/leave events. It uses a fixed US-like layout [L-how].
- **Savestates.** Twelve preparation steps: locks, stop audio, sync the X connection, suspend threads with a signal, save file offsets and empty pipes, then save memory from `/proc/self/pagemap`. Zero pages become metadata. Loading remaps the layout and recreates threads with a TID translation table. Options: memfd storage, per-page LZ4, incremental saving through soft-dirty bits, fork-based saving [L-how].
- **Rendering for savestates.** Mesa llvmpipe software rendering "is usually required" for savestates [L-opt].
- **Audio.** Every audio API (SDL, OpenAL, ALSA, PulseAudio, FMOD, cubeb) is converted into one internal mixer that runs on the deterministic time base [L-how].
- **Entropy.** Fake `/proc` content, constant `getpid`, `/dev/urandom` replaced by a seeded pipe, controlled GL extension strings [L-how].
- **Wine.** The current source has Wine-specific hooks: kernel32 `GetTickCount`, `GetTickCount64`, `QueryPerformanceFrequency`, `QueryPerformanceCounter` [L-wine-k32], and user32 `GetAsyncKeyState`, `GetCursorPos`, `ScreenToClient` [L-wine-u32]. These sources do not show that KotOR runs under libTAS. The TASVideos PC guide still says native Linux, not Wine or Proton, is the good case [TV-pc].
- **Editor and tools.** Input editor actions in source include insert, duplicate, paste-insert, truncate, lock column, auto-hold, auto-fire, markers, "Autopause on markers", "Rewind seeks to current frame" and an undo/redo change log. The UI tree also has RAM search, RAM watch, pointer scan, hex view, a Lua console and a time-trace window [L-ui].
- **Rewind model.** There is no automatic greenzone. A seek backwards loads the nearest of the user's savestate slots before the target, switches to playback, and fast-forwards [L-rew].
- **Rerecords.** The count goes up on a state load only "If the movie was modified since last state load" [L-rr].
- **Lua.** Callbacks `onStartup`, `onInput`, `onFrame`, `onPaint`. Drawing works only in `onPaint`, which is skipped on non-draw and fast-forward-skipped frames [L-lua].

## Hourglass

The Windows predecessor. It ran games natively and is no longer developed [TV-hg].

- **Injection.** `wintaser` (the UI) launches the game and injects `wintasee`. Hook modules cover d3d8, d3d9, ddraw, gdi, OpenGL, SDL, DirectSound, input, messages, MIDI, modules, registry, sync, threads, time, timers, waits and windows [HG-hooks].
- **Time.** A deterministic timer starts at 6000 ticks [HG-time]. Each time API has a query limit before time is forced forward: 10 by default, 150 for `GetTickCount`, 5 for a "crawl hack" type [HG-time]. This is the same idea as libTAS time tracking.
- **Threads.** The UI offers three thread modes: "Allow", "Wrap" and "Disable" [HG-rc]. Some modes switch on a virtual DirectSound, so audio does not depend on a real sound thread [HG-ipc].
- **Settings passed to the game.** Frame rate, key limit, force software, window activate flags, thread, timer, message-sync and wait-sync modes, AVI mode, fast-forward, initial time, time scale, and whether installed DLLs may load [HG-ipc].
- **Savestates and RAM search.** Both walk the game's committed read-write pages with `VirtualQueryEx` [HG-save-mem] [HG-ramsearch].
- **Movie.** The source calls it "not the most thought-out movie format": 8 held key IDs per frame, rerecord count, keyboard layout name, desync-detection timer values, version, exe name [HG-movie]. See [movie-formats.md](movie-formats.md).

## BizHawk TAStudio

A piano-roll editor in active development that follows the FCEUX TAS Editor design. Its menus include a "TAS Editor Manual Online..." item [BH-menu].

- **Greenzone storage, Zwinder.** Four tiers. Current (256 MB, 500-frame target) is "the primary buffer used near the last edited frame". Recent (128 MB, 2000) is where current states "decay". Gaps (64 MB, 125) "re-greenzone" old areas being replayed. Ancient states are kept every 5000 frames, "never decay but are invalidated" [BH-zw].
- **Greenzone storage, Paged.** A newer manager accepts states out of order, so that "Users who load a branch, emulate forward a little, then rewind can still use the full buffer space". It uses one memory limit and three groups (new, mid, old) with a frames-between setting each. It can keep states for markers regardless of spacing, and thins states when saving a project [BH-paged].
- **Invalidation.** Every input edit records an undo item and then calls `InvalidateAfter(frame)` [BH-edit].
- **Branches.** A branch stores frame, input log, core frame buffer, OSD frame buffer, change log, timestamp, markers, UUID and user text [BH-branch]. The branch box has Add, Add with Text, Load, Update, Jump To, Remove and Undo [BH-bbox].
- **Project file.** `.tasproj` adds lumps for state-manager settings, lag log, markers, client settings, verification log, branches and session [BH-io].
- **Settings.** Autosave frequency, "Account for lag frames" for patterns, "Use pattern for painting", "Bind markers to input", "Markers keep states by default", "Max Undo Count", "Load branch by double clicking", "Automatically pause at end of movie." [BH-set].
- **Tools around it.** Lua with `event.onframestart`, `onframeend`, `oninputpoll`, `onloadstate` and bus read/write/exec hooks, plus a `tastudio` namespace [TV-bhlua]. BasicBot does random input search over a frame range against a RAM objective with tie-breakers. Its page calls it "a dumb bot" that "does not learn" [TV-basicbot].
- **Rerecording.** A BizHawk savestate stores the whole movie. In read-only mode, loading a state runs a timeline check and reports the frame where the state diverges from the movie. A per-movie GUID guards against loading a state from another movie [TV-bhrr].

## FCEUX TAS Editor

The origin of most piano-roll conventions.

- The Toolbox has Playback, Recorder, Splicer, Lua, Bookmarks/Branches, History and Selection sections [FX-tb].
- The Greenzone collects a savestate for every emulated frame and is used to jump and rewind [FX-gloss].
- There are 10 bookmark slots. Each bookmark keeps a branch, a full copy of the movie when the bookmark was made. A branch tree shows how versions relate [FX-gloss] [FX-tb].
- Markers are yellow rows. Each one carries a one-line note [FX-gloss].
- The Recorder has multitrack radio buttons (All, 1P, 2P...), a three-state Superimpose box and "Use pattern" [FX-tb].
- The History log lists every operation on input, markers and bookmarks; clicking an entry undoes to it [FX-tb].
- FM3 saves markers, bookmarks, greenzone, history, piano-roll settings and selection after the FM2 data [FX-fm3].

## lsnes

- Input lines starting with a tab, CR, LF, space, `.` or `|` belong to the previous frame. This is how one frame holds several subframe polls [TV-lsmv].
- The rerecord count is computed from `rrdata`, rerecord-tracking records; the plain `rerecords` member is ignored on load [TV-lsmv].
- `projectid` identifies the movie and is mandatory [TV-lsmv].

## Dolphin

- The movie header can store and reload the settings that affect sync: idle skipping, dual core, DSP HLE, fast disc, CPU core, EFB/XFB options, memory cards, clear-save, sync GPU, netplay, language, FMA, widescreen, country, the git revision and DSP ROM hashes. `bSaveConfig` "Loads the settings below on startup if true" [DL-movie].
- The header also has frame count, input count, lag count and rerecord count [DL-movie] [TV-dtm].
- TAS Input windows have "Duration of Turbo Button Press (frames)" and "Duration of Turbo Button Release (frames)" [DL-tas].

## JPC-rr

A DOS PC emulator built for rerecording. Its input is time-stamped, not frame-stamped: each event line is a nanosecond timestamp, a class and parameters, in order [TV-jrsr]. The initialization section pins BIOS and disk image IDs, initial time, CPU divider, memory size and boot device [TV-jrsr]. Input is time-based like Stasis's virtual clock, but its frame index is a timestamp, not a tick count.

## Mupen64, PSXjin, Mednafen

- Mupen64 `.m64` has a fixed binary header. It records start type (snapshot, power-on, EEPROM), ROM name and CRC, and the names of all four plugins, which affect sync [TV-m64].
- PSXjin is obsolete; TASVideos tells people to use BizHawk [TV-psxjin].
- Mednafen-rr has the `.mc2` format [TV-mednafen].

## CelesteTAS and Celeste Studio

The best reference for a text-script TAS. Stasis's score format is in the same family.

- A line is `frames,actions`. Lines say what is **held**, not what is pressed; a press needs a change between lines [CT-input].
- Special lines: `#` with a space is a comment, `#label` with no space is a label, `***` is a breakpoint [CT-input].
- Commands include Read (another file from a label), Repeat/EndRepeat, Set, Invoke, Console, Assert (aborts the TAS if false), AutoInput, StunPause, RequireDependency and EnforceLegal. RecordCount, FileTime, ChapterTime and RealTime are filled in automatically after playback [CT-cmd].
- Workflow: put `***` at the section you change. Restarting fast-forwards there. Comment out new inputs (Ctrl+K) to compare with the old ones, and paste the current time with Ctrl+T. Merge new movement into old at a resync point such as a transition or a wall stop [CT-start].
- Improvements go to a tracker bot with a title like `-46f D5A (4:46.620(16860) -> 4:45.838(16814))` [CT-start].
- Hotkeys: see [conventions.md](conventions.md).

## Source-engine mod tools (SPT, BXT, SAR)

- SPT is a plugin for several Source engine branches with pause control, frame advance, vectorial strafing, HUD readouts and the `.srctas` script format [SPT].
- HLTAS is a Rust library for reading and writing `.hltas` scripts, used by Bunnymod XT [HLTAS]. BXT runs by injection into the running process rather than modifying files [BXT].
- bxt-rs adds an interactive TAS editor and a "Brute-force optimizer for TASes" [BXTRS].
- SAR `.p2tas` scripts use framebulks `tick>movement|angles|buttons|commands|tools`, with absolute or `+N` relative ticks. Tools run in a fixed order. After a tool-assisted playback SAR writes a `_raw` script with plain inputs for verification. `rngmanip` loads saved RNG values [P2].

These tools show a pattern that suits KotOR: high-level helpers (autostrafe, setang, autoaim) that compile down to raw inputs, with the raw form kept as the evidence.

## KotOR community tooling

- speedrun.com lists Any%, All Star Maps, All Quests, Pazaak 100% and ILs for KotOR. Rulesets are Unrestricted, NMG and Glitchless. Default timing is real time without loads, video is required and emulators are not allowed. Any% timing starts on "Play" after character creation and ends on the black screen after the final hit on Malak [SRC-api].
- The community guide site has route guides for each category, major glitches (AMG, coordinate warps, door clipping, Hotshot, The Routine, spawn warps and others) and techniques (save buffering, save teleporting, force skips, item duplication) [KS-site].
- The Routine is a menu sequence that quick-saves from the main menu and deletes most save data. The guide gives an abridged key sequence and no frame windows [KS-routine].
- The official LiveSplit autosplitter (Lane, XerO, Glasnonck) reads the area name, a tick count, an end-state value and a load-bar value from `swkotor.exe`. It starts when the area is `END_M01AA` with tick count between 0 and 30, splits on area changes, and pauses the timer while `loadBar > 0` [KS-asl] [LS-reg].
- No KotOR TAS, and no KotOR-specific TAS tool other than Stasis, was found in the sources examined.

For Stasis, the autosplitter's addresses are a free source of watch values and sync events (area, load bar, end state). The speedrun.com timing rule defines where a "real-time without loads" figure starts and stops.

## Sources

- [L-how] libTAS, Tech details: https://clementgallet.github.io/libTAS/guides/how/
- [L-fmt] libTAS, Moviefile format: https://clementgallet.github.io/libTAS/guides/format/
- [L-opt] libTAS, options: https://clementgallet.github.io/libTAS/guides/options/
- [L-lua] libTAS, Lua: https://clementgallet.github.io/libTAS/guides/lua/
- [L-hdr] https://github.com/clementgallet/libTAS/blob/32245db8e35ab8f2389bdb781e81ab5e5b92c4db/src/program/movie/MovieFileHeader.cpp#L75-L90
- [L-busy] https://github.com/clementgallet/libTAS/blob/32245db8e35ab8f2389bdb781e81ab5e5b92c4db/src/library/BusyLoopDetection.cpp
- [L-wine-k32] https://github.com/clementgallet/libTAS/blob/32245db8e35ab8f2389bdb781e81ab5e5b92c4db/src/library/wine/kernel32.cpp#L85-L119
- [L-wine-u32] https://github.com/clementgallet/libTAS/blob/32245db8e35ab8f2389bdb781e81ab5e5b92c4db/src/library/wine/user32.cpp#L58-L100
- [L-ui] https://github.com/clementgallet/libTAS/tree/32245db8e35ab8f2389bdb781e81ab5e5b92c4db/src/program/ui (InputEditorWindow.cpp, InputEditorView.cpp, RamSearchWindow.cpp, PointerScanWindow.cpp, TimeTraceWindow.cpp, InputChangeLogWindow.cpp)
- [L-rew] https://github.com/clementgallet/libTAS/blob/32245db8e35ab8f2389bdb781e81ab5e5b92c4db/src/program/ui/InputEditorModel.cpp#L1286-L1330
- [L-rr] https://github.com/clementgallet/libTAS/blob/32245db8e35ab8f2389bdb781e81ab5e5b92c4db/src/program/SaveState.cpp#L229-L233
- [HG-hooks] https://github.com/TASVideos/hourglass-win32/tree/78fa7c6d2be1b5eb46b8d4c0a37e56af616e1a46/src/wintasee/hooks
- [HG-time] https://github.com/TASVideos/hourglass-win32/blob/78fa7c6d2be1b5eb46b8d4c0a37e56af616e1a46/src/wintasee/dettime.cpp#L286-L305
- [HG-rc] https://github.com/TASVideos/hourglass-win32/blob/78fa7c6d2be1b5eb46b8d4c0a37e56af616e1a46/src/wintaser/wintaser.rc#L150-L152
- [HG-ipc] https://github.com/TASVideos/hourglass-win32/blob/78fa7c6d2be1b5eb46b8d4c0a37e56af616e1a46/src/shared/ipc.h#L11-L38 and wintaser.cpp#L2329 (virtual DirectSound for thread modes 0, 4, 5)
- [HG-save-mem] https://github.com/TASVideos/hourglass-win32/blob/78fa7c6d2be1b5eb46b8d4c0a37e56af616e1a46/src/wintaser/wintaser.cpp#L1690-L1705
- [HG-ramsearch] https://github.com/TASVideos/hourglass-win32/blob/78fa7c6d2be1b5eb46b8d4c0a37e56af616e1a46/src/wintaser/ramsearch.cpp#L197-L206
- [HG-movie] https://github.com/TASVideos/hourglass-win32/blob/78fa7c6d2be1b5eb46b8d4c0a37e56af616e1a46/src/wintaser/wintaser.cpp#L544-L563
- [BH-zw] https://github.com/TASEmulators/BizHawk/blob/c2a683fc1fc4a7e3cfd593ccab9e061482c824aa/src/BizHawk.Client.Common/movie/tasproj/ZwinderStateManagerSettings.cs#L39-L109
- [BH-paged] https://github.com/TASEmulators/BizHawk/blob/c2a683fc1fc4a7e3cfd593ccab9e061482c824aa/src/BizHawk.Client.Common/tools/TAStudio/PagedStateManager.cs#L17-L86
- [BH-edit] https://github.com/TASEmulators/BizHawk/blob/c2a683fc1fc4a7e3cfd593ccab9e061482c824aa/src/BizHawk.Client.Common/movie/tasproj/TasMovie.Editing.cs#L25-L60
- [BH-branch] https://github.com/TASEmulators/BizHawk/blob/c2a683fc1fc4a7e3cfd593ccab9e061482c824aa/src/BizHawk.Client.Common/movie/tasproj/TasBranch.cs#L12-L39
- [BH-bbox] https://github.com/TASEmulators/BizHawk/blob/c2a683fc1fc4a7e3cfd593ccab9e061482c824aa/src/BizHawk.Client.EmuHawk/tools/TAStudio/BookmarksBranchesBox.Designer.cs
- [BH-io] https://github.com/TASEmulators/BizHawk/blob/c2a683fc1fc4a7e3cfd593ccab9e061482c824aa/src/BizHawk.Client.Common/movie/tasproj/TasMovie.IO.cs#L20-L45
- [BH-set] https://github.com/TASEmulators/BizHawk/blob/c2a683fc1fc4a7e3cfd593ccab9e061482c824aa/src/BizHawk.Client.EmuHawk/tools/TAStudio/TAStudioSettingsForm.Designer.cs#L368-L927
- [BH-menu] https://github.com/TASEmulators/BizHawk/blob/c2a683fc1fc4a7e3cfd593ccab9e061482c824aa/src/BizHawk.Client.EmuHawk/tools/TAStudio/TAStudio.Designer.cs
- [TV-bhlua] https://tasvideos.org/Bizhawk/LuaFunctions
- [TV-basicbot] https://tasvideos.org/Bizhawk/BasicBot
- [TV-bhrr] https://tasvideos.org/Bizhawk/Rerecording
- [TV-bk2] https://tasvideos.org/Bizhawk/BK2Format
- [FX-gloss] https://fceux.com/web/help/taseditor/Glossary.html
- [FX-tb] https://fceux.com/web/help/taseditor/Toolbox.html
- [FX-fm2] https://fceux.com/web/help/fm2.html
- [FX-fm3] https://fceux.com/web/help/taseditor/FM3format.html
- [TV-lsnes] https://tasvideos.org/EmulatorResources/Lsnes
- [TV-lsmv] https://tasvideos.org/EmulatorResources/Lsnes/LSMV
- [DL-movie] https://github.com/dolphin-emu/dolphin/blob/0961ec1d87a51b7ede57cc4df7629b7f92d0d9d8/Source/Core/Core/Movie.h#L86-L140
- [DL-tas] https://github.com/dolphin-emu/dolphin/blob/0961ec1d87a51b7ede57cc4df7629b7f92d0d9d8/Source/Core/DolphinQt/TAS/TASInputWindow.cpp#L57-L63
- [TV-dtm] https://tasvideos.org/EmulatorResources/Dolphin/DTM
- [TV-jpc] https://tasvideos.org/EmulatorResources/JPC
- [TV-jrsr] https://tasvideos.org/EmulatorResources/JPC/JRSRFormat
- [TV-m64] https://tasvideos.org/EmulatorResources/Mupen/M64
- [TV-psxjin] https://tasvideos.org/EmulatorResources/PSXjin
- [TV-mednafen] https://tasvideos.org/EmulatorResources/Mednafen
- [TV-emu] https://tasvideos.org/EmulatorResources
- [TV-hg] https://tasvideos.org/EmulatorResources/Hourglass
- [TV-libtas] https://tasvideos.org/EmulatorResources/LibTAS
- [TV-pc] https://tasvideos.org/HomePages/Randomno/HowToTASPCGames
- [CT-input] https://github.com/EverestAPI/CelesteTAS-EverestInterop/wiki/Input-File
- [CT-cmd] https://github.com/EverestAPI/CelesteTAS-EverestInterop/wiki/Commands
- [CT-ctl] https://github.com/EverestAPI/CelesteTAS-EverestInterop/wiki/Controls
- [CT-start] https://github.com/EverestAPI/CelesteTAS-EverestInterop/wiki/Getting-Started
- [SPT] https://github.com/YaLTeR/SourcePauseTool
- [HLTAS] https://github.com/HLTAS/hltas
- [BXT] https://github.com/YaLTeR/BunnymodXT
- [BXTRS] https://github.com/YaLTeR/bxt-rs/blob/6ab51edead6e3f5eaf221acfe5ca24794157f224/README.md
- [P2] https://wiki.portal2.sr/TAS
- [SRC-api] https://www.speedrun.com/api/v1/games?name=Knights%20of%20the%20Old%20Republic&embed=categories,variables (board: https://www.speedrun.com/kotor1)
- [KS-site] https://kotor-speedruns.github.io/kotor1/
- [KS-routine] https://kotor-speedruns.github.io/kotor1/Major%20Glitches/The%20Routine
- [KS-asl] https://github.com/kotor-speedruns/Autosplitter/blob/2060ae5ea8f38f02520111cf4e5a1e4e9f040388/K1.asl
- [LS-reg] https://github.com/LiveSplit/LiveSplit.AutoSplitters/blob/381b50b6d6c8f098c8bc32d96e96905a6c70c33d/LiveSplit.AutoSplitters.xml
