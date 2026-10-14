# Movie file formats

A field-level comparison of TAS movie formats, and what Stasis's score and holorecord already cover. Tags such as [TV-bk2] point to the source list at the end. Quoted text is copied from source files pulled with git or curl; other text paraphrases the cited page.

## What every format stores

Every accepted format answers the same questions. TASVideos' tool requirements spell out the minimum. The movie must be replayable, documented well enough to write a parser, clear about whether it starts from power-on, Save RAM or a savestate, and must give a length that converts to fractional seconds without the game or the emulator. The frame count must be checkable by parsing the input [TV-req].

| Question | bk2 (BizHawk) | ltm (libTAS) | fm2/fm3 (FCEUX) | lsmv (lsnes) | dtm (Dolphin) | m64 (Mupen64) | jrsr (JPC-rr) | wtf (Hourglass) | .tas (Celeste) | Stasis score + holorecord |
|---|---|---|---|---|---|---|---|---|---|---|
| Container | zip [TV-bk2] | tar.gz [L-fmt] | text; fm3 adds a binary tail [FX-fm2] [FX-fm3] | zip or binary chunks [TV-lsmv] | binary, 256-byte header [TV-dtm] | binary header [TV-m64] | text sections [TV-jrsr] | binary, 1024-byte header [HG-save] | text [CT-input] | text score; holorecord is a directory [S-score] [S-holo] |
| Format version | MovieVersion [TV-bk2] | libtas_major/minor/patch_version [L-fmt] | `version` = 3 [FX-fm2] | controlsversion, coreversion [TV-lsmv] | signature `DTM\x1A` [DL-movie] | signature `M64\x1A`, version [TV-m64] | magic `JRSR`, SYSTEM [TV-jrsr] | magic `0x02775466` "\02wTf" [HG-save] | `version` lines in other script formats; none in Celeste's examples examined | `#!score v1` [S-score] |
| Tool version | emuVersion, Core [TV-bk2] | libtas version [L-fmt] | emuVersion [FX-fm2] | coreversion [TV-lsmv] | git revision (20 bytes) [DL-movie] | plugin names [TV-m64] | SYSTEM [TV-jrsr] | version int [HG-save] | RequireDependency command [CT-cmd] | determinism key covers modules [S-gloss] |
| Game identity | GameName, SHA1/MD5/CRC32, BoardName [TV-bk2] | game_name, md5 of executable [L-fmt] | romFilename, romChecksum (base64 MD5) [FX-fm2] | gamename; `<slot>.sha256` per ROM [TV-lsmv] | game ID, md5 [DL-movie] | ROM internal name, CRC32, country [TV-m64] | BIOS, VGABIOS, disk image IDs [TV-jrsr] | exe CRC, exe file size, exe name (48 bytes) [HG-save] | none in file | `game`, `build` (from exe sha256) [S-score]; determinism key over game files [S-gloss] |
| Movie identity | — | — | `guid` (required) [FX-fm2] | `projectid` (required) [TV-lsmv] | uniqueID (not implemented) [DL-movie] | movie UID/timestamp [TV-m64] | PROJECTID [TV-jrsr] | — | — | bundle id = first 32 hex of H_0 [S-holo] |
| Author | Author [TV-bk2] | authors [L-fmt] | — in header list; `comment` field [FX-fm2] | `authors`, "real name\|nickname" per line [TV-lsmv] | author (32 bytes) [DL-movie] | author (222 bytes) [TV-m64] | AUTHORS, AUTHORNICKS [TV-jrsr] | — | — | not found in docs examined |
| Rerecord count | rerecordCount [TV-bk2] | rerecord_count [L-fmt] | rerecordCount [FX-fm2] | computed from `rrdata`; `rerecords` ignored on load [TV-lsmv] | numRerecords [DL-movie] | rerecord count [TV-m64] | RERECORDS [TV-jrsr] | rerecords [HG-save] | RecordCount, auto-updated [CT-cmd] | not found in docs examined |
| Length | lines starting with `\|` [TV-bk2] | frame_count; length_sec/nsec [L-fmt] | `length` optional; count lines [FX-fm2] | frame lines [TV-lsmv] | VI count, input count [DL-movie] | VI count, input sample count, VIs per second [TV-m64] | last event timestamp (ns) [TV-jrsr] | length (frames) [HG-save] | ChapterTime/FileTime auto-filled [CT-cmd] | one TICK record per tick; virtual µs from tick count [S-holo] [S-gloss] |
| Frame rate | from platform | framerate_num/den; variable_framerate flag; `\|T` per frame [L-fmt] | palFlag [FX-fm2] | from core | from game | VIs per second [TV-m64] | time-based | fps+1 [HG-save] | fixed by game | `hz` per score and per `@segment` [S-score] |
| Start state | StartsFromSavestate; CoreState; SaveRam [TV-bk2] | savestate_frame_count [L-fmt] | `savestate` [FX-fm2] | savestate.anchor (SHA-256 trailer), moviesram.* [TV-lsmv] | starts-from-savestate flag; clear-save; memory cards [DL-movie] | start type: snapshot, power-on, EEPROM [TV-m64] | INITIALTIME, BOOT device, memory size [TV-jrsr] | initial time+1 [HG-save] | `console load` lines [CT-start] | `start=boot` or `save:<sha256>`; save start not yet implemented [S-score] |
| Clock / RTC | in SyncSettings | initial_time_sec/nsec; time-track thresholds [L-fmt] [L-hdr] | — | starttime.second/subsecond [TV-lsmv] | recordingStartTime [DL-movie] | — | INITIALTIME (ms since epoch) [TV-jrsr] | initial time [HG-save] | — | clock origin in determinism key [S-gloss] |
| Sync-affecting settings | SyncSettings JSON, loaded automatically [TV-bk2] | config.ini keys incl. `[mainthread_timetrack]` [L-hdr] | NewPPU, FDS, fourscore, port types [FX-fm2] | systemid, controlsversion [TV-lsmv] | full settings block, applied when bSaveConfig [DL-movie] | four plugin names [TV-m64] | CPUDIVIDER, MEMORYSIZE [TV-jrsr] | keyboard layout, command line (160 bytes), desync timer values [HG-save] | AnalogMode, EnforceLegal [CT-cmd] | header `tier`, `render`, `keymap`, `save_ms` [S-score] |
| Input encoding | text, one line per frame [TV-bk2] | text, `\|K` `\|M` `\|C` `\|F` `\|T` sections [L-fmt] | text `\|commands\|port0\|port1\|port2\|` [FX-fm2] | text, subframe lines [TV-lsmv] | binary, 8 bytes per GC pad [TV-dtm] | binary, 4 bytes per sample [TV-m64] | timestamped events [TV-jrsr] | 8 held key IDs per frame [HG-movie] | `frames,actions` held lines [CT-input] | `ticks  keys` notes [S-score] |
| Annotations | Comments, Subtitles [TV-bk2] | annotations.txt [L-fmt] | comment, subtitle [FX-fm2] | subtitles [TV-lsmv] | — | description (256 bytes) [TV-m64] | — | — | comments, labels [CT-input] | `#` comments, `@mark` [S-score] |
| Editor data | `.tasproj` lumps: state settings, lag log, markers, client settings, verification log, branches, session [BH-io] | editor.ini: input_names, markers, nondraw_frames [L-fmt] | fm3: markers, bookmarks, greenzone, history, piano roll, selection [FX-fm3] | — | — | — | — | — | breakpoints `***` [CT-input] | holorecord journal, pages, keyframes, index [S-holo] |
| Integrity | — | — | — | SHA-256 on anchor state [TV-lsmv] | DSP ROM hashes [DL-movie] | — | PROJECTID match for states [TV-jrsr] | exe CRC and size [HG-save] | Assert command [CT-cmd] | hash-chained journal, state roots [S-holo] |

A dash means the field was not found in the sources examined, not that it is proven absent.

## Input log conventions

**Pipe-delimited mnemonic lines.** BizHawk, FCEUX and libTAS all write one text line per frame, opened by `|`, with one section per device.

- FCEUX: `|commands|port0|port1|port2|`. A gamepad is `RLDUTSBA` (Right, Left, Down, Up, sTart, Select, B, A). Commands are a bitfield: 1 soft reset, 2 hard reset, 4 FDS insert, 8 FDS select, 16 VS coin [FX-fm2].
- BizHawk: a pressed button prints its letter, an unpressed one prints `.`. Analog values are five-digit space-padded numbers followed by commas. `|` may appear anywhere and usually separates controllers. The frame count is the number of lines that start with `|` [TV-bk2].
- libTAS keyboard: `|K` then pressed Xlib KeySyms in hex, separated by `:`, for example `|Kffe1:20:ff53`. Mouse: `|M202:264:A:1....` (x, y, absolute or relative, five buttons). Controller: six axes then `ABXYbgs()[]udlr`. Flags: `|FR........` for restart and controller plug events. Frame rate: `|T60:1` [L-fmt].
- lsnes: a line that starts with tab, CR, LF, space, `.` or `|` continues the previous frame. This is how subframe polls are written, for example `F.|BYsSudlrAXLR` [TV-lsmv].

**Two lessons for KotOR.**
1. libTAS stores keys by symbol, not by scan code, and still records the fixed layout separately [L-fmt] [L-how]. Hourglass stores the Windows keyboard layout name next to the key IDs [HG-movie]. A KotOR score should keep the keymap it was written against. Stasis already has a `keymap` header and `@alias` [S-score].
2. Mouse needs position mode (absolute or relative) per frame [L-fmt]. KotOR camera and click targeting depend on it.

**Hold-based text scripts.** Celeste `.tas`, SAR `.p2tas`, HLTAS and the Stasis score all describe what is held for a run of frames. A press is a change between lines [CT-input] [P2] [HLTAS] [S-score]. SAR adds relative ticks (`+10`) and per-button hold counts (`D8`) [P2].

## Rerecord counting rules

- libTAS: count +1 on a state load only when input changed since the previous state load [L-rr].
- lsnes: count is derived from `rrdata` records, so it can't be hand-edited by changing a header number [TV-lsmv].
- Celeste: `RecordCount:` is a statistic that "automatically tracks how often you've tweaked the TAS file" [CT-start].
- TASVideos publication subtitles show the rerecord count [TV-pub]. The movie rules examined set no minimum [TV-rules].

## Verification and save-anchored movies

TASVideos wants new movies recorded from power-on or Save RAM; savestate starts are allowed only in exceptional cases. A save-anchored submission needs a verification movie that starts from power-on, is not save-anchored itself, and creates the exact Save RAM used. It need not be optimized [TV-rules]. For a PC game, the KotOR save folder plays the part of Save RAM. Stasis records `start=save:<sha256>` but cannot yet start from a save [S-score].

## Sources

- [TV-req] https://tasvideos.org/EmulatorResources/Requirements
- [TV-bk2] https://tasvideos.org/Bizhawk/BK2Format
- [BH-io] https://github.com/TASEmulators/BizHawk/blob/c2a683fc1fc4a7e3cfd593ccab9e061482c824aa/src/BizHawk.Client.Common/movie/tasproj/TasMovie.IO.cs#L20-L45
- [L-fmt] https://clementgallet.github.io/libTAS/guides/format/
- [L-how] https://clementgallet.github.io/libTAS/guides/how/
- [L-hdr] https://github.com/clementgallet/libTAS/blob/32245db8e35ab8f2389bdb781e81ab5e5b92c4db/src/program/movie/MovieFileHeader.cpp#L75-L90
- [L-rr] https://github.com/clementgallet/libTAS/blob/32245db8e35ab8f2389bdb781e81ab5e5b92c4db/src/program/SaveState.cpp#L229-L233
- [FX-fm2] https://fceux.com/web/help/fm2.html
- [FX-fm3] https://fceux.com/web/help/taseditor/FM3format.html
- [TV-lsmv] https://tasvideos.org/EmulatorResources/Lsnes/LSMV
- [TV-dtm] https://tasvideos.org/EmulatorResources/Dolphin/DTM
- [DL-movie] https://github.com/dolphin-emu/dolphin/blob/0961ec1d87a51b7ede57cc4df7629b7f92d0d9d8/Source/Core/Core/Movie.h#L60-L140
- [TV-m64] https://tasvideos.org/EmulatorResources/Mupen/M64
- [TV-jrsr] https://tasvideos.org/EmulatorResources/JPC/JRSRFormat
- [HG-save] https://github.com/TASVideos/hourglass-win32/blob/78fa7c6d2be1b5eb46b8d4c0a37e56af616e1a46/src/wintaser/wintaser.cpp#L888-L961 (header written field by field, padded to 1024 bytes, then frames)
- [HG-movie] https://github.com/TASVideos/hourglass-win32/blob/78fa7c6d2be1b5eb46b8d4c0a37e56af616e1a46/src/wintaser/wintaser.cpp#L544-L563
- [CT-input] https://github.com/EverestAPI/CelesteTAS-EverestInterop/wiki/Input-File
- [CT-cmd] https://github.com/EverestAPI/CelesteTAS-EverestInterop/wiki/Commands
- [CT-start] https://github.com/EverestAPI/CelesteTAS-EverestInterop/wiki/Getting-Started
- [P2] https://wiki.portal2.sr/TAS
- [HLTAS] https://github.com/HLTAS/hltas
- [TV-pub] https://tasvideos.org/EncodingGuide/PublicationManual
- [TV-rules] https://tasvideos.org/MovieRules
- [S-score] Stasis score format: [../score.md](../score.md)
- [S-holo] Stasis holorecord format: [../holorecord.md](../holorecord.md)
- [S-gloss] Stasis glossary: [../glossary.md](../glossary.md)
