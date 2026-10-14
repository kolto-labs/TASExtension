# PC TAS determinism: libTAS, Hourglass and Stasis

A console emulator owns every clock and device, so determinism comes free. A PC TAS tool runs the real game binary and must take each source of variation away from the operating system. This file compares how libTAS and Hourglass do that, using JPC-rr as an emulated-PC reference, then sets Stasis beside them. Tags point to the source list at the end. Quoted text is copied from source pulled with git or curl.

Where this file says "not found in sources examined", that is the honest limit of the reading, not proof that a tool lacks the feature.

## Sources of variation

| Concern | libTAS | Hourglass | JPC-rr (emulated) | Stasis |
|---|---|---|---|---|
| Where a frame ends | At present/swap calls (SDL, GLX, Vulkan, X11 blit) [L-how] | Present-type hooks in d3d8, d3d9, ddraw, gdi, sdl call `FrameBoundary` [HG-hooks] | Time-based; events have ns timestamps [TV-jrsr] | At the game's frame gate: one `WinMain` loop pass with no Windows message waiting, running client and server update once [S-gloss] |
| Game clock | Deterministic timer gains one frame length per boundary [L-how] | Deterministic timer starting at 6000 ticks [HG-time] | Emulated hardware time [TV-jrsr] | Virtual clock from the tick count; at 60 Hz each tick is 1/60 s with remainder carried, so 60 ticks are exactly 1,000,000 µs. QPC, GetTickCount and system time all read it [S-gloss] |
| Spin-waits on time | Time tracking: advance after N queries per function; thresholds stored in the movie [L-how] [L-hdr]. Busy-loop detection by call-stack hash [L-busy] | Per-API query limits before forced advance: 10 default, 150 GetTickCount, 5 "crawl hack" [HG-time] | n/a | No forced-advance mechanism: virtual time moves only per tick. A thread that spins on the clock between gates is a risk; see lesson 2 and gap-analysis item 16 |
| Sleep and waits | Sleep advances never / main thread only (default) / always; waits ignored by default [L-how] | Wait and sleep hooks [HG-hooks]; wait-sync mode setting [HG-ipc] | n/a | Not found in docs examined |
| Long stalls (saves) | Not found in sources examined | Not found in sources examined | n/a | `save_ms`: virtual time one save advances while zero ticks pass, to keep save-teleport behavior. Must be explicit [S-score] |
| Threads | Suspend by signal, save TLS, registers, stack; recreate with `clone()` and a TID map; Unity loader threads serialized [L-how] | Thread modes "Allow", "Wrap", "Disable" [HG-rc] | n/a | Threading mode is part of the determinism key [S-tiers] |
| Entropy | Fake `/proc` content, constant `getpid`, seeded `/dev/urandom`, controlled GL extensions [L-how] | Not found in sources examined | n/a | Address randomization off via `setarch -R` launch wrapper [S-holo]. A recording holds no wall-clock time or random id [S-holo] |
| CPU math | Not found in sources examined | Not found | Emulated CPU | Reference math: software sin, cos, tan, atan2, exp, log, pow from exact SSE2 ops; all 105 x87 math sites of K1 point to it by default [S-gloss] |
| Audio | One internal mixer for SDL, OpenAL, ALSA, PulseAudio, FMOD, cubeb on the deterministic clock; stopped during savestates [L-how] | Virtual DirectSound in some thread modes [HG-ipc] | Emulated | `audio.device = "virtual"`: the shim answers every Miles call from a model on the virtual clock and journals it [S-holo] |
| GPU | Software rendering (llvmpipe) "usually required" for savestates [L-opt] | `storeVideoMemoryInSavestates`, `forceSoftware` settings [HG-ipc] | Emulated VGA | GL objects the game can observe (`glIsTexture`, ARB programs, context state) answer from a harness and are keyed into the state root; restore rebuilds and reads them back [S-holo] |
| Input | No device access; own event queues for SDL, Xlib, XCB, evdev, jsdev, udev; fixed US layout [L-how]. Wine hooks for `GetAsyncKeyState`, `GetCursorPos` [L-wine-u32] | DirectInput and message hooks [HG-hooks]; keyboard layout name in movie [HG-movie] | Emulated keyboard with buffering [TV-jpc] | dinput8 shim; virtual device state sampled at the accepted tick [S-ctl] |
| Focus | Filters FocusIn/FocusOut, EnterNotify/LeaveNotify [L-how] | `windowActivateFlags` setting [HG-ipc] | n/a | Explicit `!focus` and `!blur` score events [S-score] |
| Disk writes | "Prevent Writing to Disk" redirects saves to memory [L-opt] | File and registry hooks [HG-hooks] | Emulated disks with IDs [TV-jrsr] | Files tree (Saves, Temp, currentgame and other writable paths) captured per tick and restored with pages; registry from a prefix pin [S-holo] |
| Savestate memory | `/proc/self/pagemap` walk; zero pages as metadata; memfd, LZ4, soft-dirty incremental, fork [L-how] | Committed read-write regions via `VirtualQueryEx` [HG-save-mem] | Emulator state | Every tick captured. Content-addressed page store; keyframes every 600 ticks; state root is a sha256 tree over 4 MiB leaves including page protection [S-holo] |
| Restore check | Not found in sources examined | Not found | PROJECTID match [TV-jrsr] | Full-root verification plus a continuation canary; a failed restore stops the session [S-ctl] [S-holo] |
| Settings that decide sync | Stored in the movie (`[mainthread_timetrack]`, framerate, initial time) [L-hdr] [L-fmt] | Movie stores layout, command line, desync timers [HG-save] | Init section [TV-jrsr] | Determinism key over binaries, data, modules, clock origin, portable profile, threading mode [S-tiers] |

## Lessons from the other tools

1. **Frame boundary placement decides what "a frame" is.** libTAS ends frames at present calls. When time crosses a boundary with no render, it creates a non-draw frame, and these "may be consistent within one system, but inconsistent across different systems" [L-how]. Stasis ends a tick at the main-loop gate, so a loading screen that does not present still advances ticks. That avoids libTAS's non-draw problem. The cost is that "tick" and "displayed frame" differ, so the UI should show both; see lag definitions in [conventions.md](conventions.md).
2. **Spin loops are the main time leak.** Both PC tools count time queries and force time forward after a threshold [L-how] [HG-time]. libTAS now also hashes call stacks to find busy loops [L-busy]. If any KotOR thread polls QPC or GetTickCount in a loop between gates, Stasis's clock would hold still and that thread could spin. A per-tick count of time queries by caller, like libTAS's time-trace window [L-ui], would show whether that happens.
3. **Put sync-deciding settings in the movie.** Dolphin applies a settings block from the header when `bSaveConfig` is set [DL-movie]. libTAS writes time-track thresholds into the movie [L-hdr]. Stasis's determinism key does the same job by hashing, and refuses to compare runs with different keys [S-gloss].
4. **Make async work synchronous while recording.** gz forces room loads synchronous during macro record and playback so load-time jitter can't desync [GZ-notes]. libTAS serializes Unity loader threads [L-how]. KotOR area loads are the same risk. Stasis's `@sync area.ready` waits on the event instead of counting ticks [S-score].
5. **Record outside randomness as events.** gz stores every RNG reseed in the macro and reuses it only when the RNG is still in sync and the reseed lands on the same frame; otherwise it warns [GZ-notes]. SAR's `rngmanip` loads saved RNG values [P2]. Stasis instead removes the sources (clock, ASLR, audio, GL observations) [S-gloss] [S-holo]. If an unremovable source turns up, record-and-replay-with-warning is the fallback.
6. **Savestates and the GPU.** libTAS needs software rendering for savestates [L-opt]. gz skips graphics data in states and accepts cosmetic glitches [GZ-notes]. Stasis keys the GL objects the game can observe into the state root and reads them back on restore [S-holo]. A checkpoint image shown in the UI is labeled and is not proof that the native framebuffer was restored [S-ctl].
7. **Threads are the hardest part.** libTAS needs a dozen preparation steps to stop and recreate threads [L-how]. Hourglass offered Allow, Wrap and Disable [HG-rc]. Stasis can restore a fresh process at its boot gate straight to any tick [S-holo]. How in-session restores treat the game's threads is not described in the docs examined. Its threading mode is in the determinism key [S-tiers].
8. **Restore speed.** libTAS gets fast saves through soft-dirty incremental pages and forked saving [L-how]. BizHawk keeps tiered state buffers so a seek starts near the target [BH-zw] [BH-paged]. Stasis captures every tick but restore can "still take tens of seconds" [S-ctl], and branch ancestry causes 10–16 s authoring latency [S-plan]. This is where the other tools have the most to teach; see [gap-analysis.md](gap-analysis.md) items 1 and 2.

## What Stasis does that was not found elsewhere

These were not found in any source examined. They are Stasis's distinctive choices, not claims that nobody else does them.

- A restore check against a full state root plus a continuation canary, with refusal of any root the bundle did not record [S-holo] [S-ctl].
- Reference math replacing x87 sites to remove CPU dependence [S-gloss].
- An explicit, recorded save stall (`save_ms`) to keep a timing glitch faithful under a virtual clock [S-score].
- A hash-chained journal where two recordings of one score should match byte for byte [S-holo].

## Where TASVideos stands on PC tools

- Only libTAS and Hourglass are accepted for PC games. Specialized tools and external macros are not, though specialized tools may be reconsidered [TV-pc].
- For computer games, settings that the game or its documentation supports are allowed. Changes that make a game deterministic offline are allowed [TV-rules].
- libTAS submissions must use versioned releases, document the distribution, kernel and dependencies, document changed libTAS settings, and match the game's native frame rate unless justified. Stray save data is a sync risk [TV-libtas].
- Minimum tool requirements: a replayable movie, a parseable documented format, gameplay a casual viewer can't tell apart from the original, documented deviations, a clear start state verified at runtime, a length convertible to seconds without the game, a free public download, and audio/video dumping or external capture [TV-req].

Stasis against those minimums: the score and holorecord formats are documented [S-score] [S-holo]. Length in µs follows from the tick count [S-gloss]. Start state is boot, and save starts are not implemented [S-score]. A/V capture exists as `--video` without audio, plus external X11 capture [S-ctl]. Public availability is a release decision outside this file.

## Sources

- [L-how] https://clementgallet.github.io/libTAS/guides/how/
- [L-opt] https://clementgallet.github.io/libTAS/guides/options/
- [L-fmt] https://clementgallet.github.io/libTAS/guides/format/
- [L-hdr] https://github.com/clementgallet/libTAS/blob/32245db8e35ab8f2389bdb781e81ab5e5b92c4db/src/program/movie/MovieFileHeader.cpp#L75-L90
- [L-busy] https://github.com/clementgallet/libTAS/blob/32245db8e35ab8f2389bdb781e81ab5e5b92c4db/src/library/BusyLoopDetection.cpp
- [L-ui] https://github.com/clementgallet/libTAS/tree/32245db8e35ab8f2389bdb781e81ab5e5b92c4db/src/program/ui (TimeTraceWindow.cpp)
- [L-wine-u32] https://github.com/clementgallet/libTAS/blob/32245db8e35ab8f2389bdb781e81ab5e5b92c4db/src/library/wine/user32.cpp#L58-L100
- [HG-hooks] https://github.com/TASVideos/hourglass-win32/tree/78fa7c6d2be1b5eb46b8d4c0a37e56af616e1a46/src/wintasee/hooks
- [HG-time] https://github.com/TASVideos/hourglass-win32/blob/78fa7c6d2be1b5eb46b8d4c0a37e56af616e1a46/src/wintasee/dettime.cpp#L286-L305
- [HG-rc] https://github.com/TASVideos/hourglass-win32/blob/78fa7c6d2be1b5eb46b8d4c0a37e56af616e1a46/src/wintaser/wintaser.rc#L150-L152
- [HG-ipc] https://github.com/TASVideos/hourglass-win32/blob/78fa7c6d2be1b5eb46b8d4c0a37e56af616e1a46/src/shared/ipc.h#L11-L38 and https://github.com/TASVideos/hourglass-win32/blob/78fa7c6d2be1b5eb46b8d4c0a37e56af616e1a46/src/wintaser/wintaser.cpp#L2329
- [HG-save-mem] https://github.com/TASVideos/hourglass-win32/blob/78fa7c6d2be1b5eb46b8d4c0a37e56af616e1a46/src/wintaser/wintaser.cpp#L1609-L1705
- [HG-save] https://github.com/TASVideos/hourglass-win32/blob/78fa7c6d2be1b5eb46b8d4c0a37e56af616e1a46/src/wintaser/wintaser.cpp#L888-L961
- [HG-movie] https://github.com/TASVideos/hourglass-win32/blob/78fa7c6d2be1b5eb46b8d4c0a37e56af616e1a46/src/wintaser/wintaser.cpp#L544-L563
- [BH-zw] https://github.com/TASEmulators/BizHawk/blob/c2a683fc1fc4a7e3cfd593ccab9e061482c824aa/src/BizHawk.Client.Common/movie/tasproj/ZwinderStateManagerSettings.cs#L39-L109
- [BH-paged] https://github.com/TASEmulators/BizHawk/blob/c2a683fc1fc4a7e3cfd593ccab9e061482c824aa/src/BizHawk.Client.Common/tools/TAStudio/PagedStateManager.cs#L17-L86
- [DL-movie] https://github.com/dolphin-emu/dolphin/blob/0961ec1d87a51b7ede57cc4df7629b7f92d0d9d8/Source/Core/Core/Movie.h#L86-L140
- [GZ-notes] https://github.com/glankk/gz/blob/1155b00b260d8c205a81f8d6b560077a4abbe784/USAGE.md#L595-L697
- [P2] https://wiki.portal2.sr/TAS
- [TV-jpc] https://tasvideos.org/EmulatorResources/JPC
- [TV-jrsr] https://tasvideos.org/EmulatorResources/JPC/JRSRFormat
- [TV-pc] https://tasvideos.org/HomePages/Randomno/HowToTASPCGames
- [TV-rules] https://tasvideos.org/MovieRules
- [TV-libtas] https://tasvideos.org/EmulatorResources/LibTAS
- [TV-req] https://tasvideos.org/EmulatorResources/Requirements
- [S-gloss] [../glossary.md](../glossary.md)
- [S-score] [../score.md](../score.md)
- [S-holo] [../holorecord.md](../holorecord.md)
- [S-ctl] [../controls.md](../controls.md)
- [S-tiers] [../tiers.md](../tiers.md)
- [S-plan] [../plans/2026-10-02-2022-feat-kptas-complete-product-plan.md](../plans/2026-10-02-2022-feat-kptas-complete-product-plan.md)
