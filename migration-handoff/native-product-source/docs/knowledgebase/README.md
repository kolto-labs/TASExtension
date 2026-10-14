# TAS knowledge base for Stasis

What other TAS tools do, how they do it, and what Stasis should take from them. Written 2026-10-04 from tool documentation and pinned source code. Every claim carries a source tag that resolves to a URL in the file's own source list.

## Reading rules

- A quotation is copied from a source file pulled with git or curl. Other sentences paraphrase the cited page.
- "Not found in sources examined" means the reading did not find it. It does not prove the tool lacks it.
- GitHub links are pinned to the commit read on 2026-10-04: libTAS `32245db`, BizHawk `c2a683f`, Hourglass `78fa7c6`, Dolphin `0961ec1`, gz `1155b00`, KotOR autosplitter `2060ae5`, bxt-rs `6ab51ed`.
- Several TASVideos pages were unreachable or missing during research: the TAStudio manual page, an lsnes overview and a JPC-rr overview under other names, and the FCEUX "TAS Editor in depth" page. They are not cited. The FCEUX TAS Editor manual pages and BizHawk source were used instead.

## Documents

**[systems.md](systems.md)** describes how each tool is built: libTAS, Hourglass, BizHawk TAStudio, FCEUX TAS Editor, lsnes, Dolphin, JPC-rr, Mupen64, PSXjin, Mednafen, CelesteTAS, the Source-engine mod tools (SPT, Bunnymod XT, bxt-rs, SAR) and KotOR community tooling. A summary table covers target, control method, frame boundary, state capture, editor, file format and TASVideos status. The detail sections cover libTAS's hooks, timer, savestates and Wine hooks, Hourglass's per-API time limits and thread modes, BizHawk's tiered greenzone and branch contents, Celeste's text-script workflow, and the KotOR speedrun.com rules and LiveSplit autosplitter.

**[movie-formats.md](movie-formats.md)** compares bk2, ltm, fm2/fm3, lsmv, dtm, m64, jrsr, wtf and Celeste `.tas` field by field against the Stasis score and holorecord. The rows are container, version, game and movie identity, author, rerecords, length, frame rate, start state, clock, sync settings, input encoding, annotations, editor data and integrity. It also explains pipe-delimited input logs, three ways of counting rerecords, and TASVideos' rule for save-anchored movies and verification movies.

**[conventions.md](conventions.md)** gives the default hotkeys of BizHawk, libTAS, FCEUX, CelesteTAS and Stasis side by side, including the F-key savestate habit that most tools share. It defines read-only and read-write behavior and the frame, lag and rerecord counters. It gives three incompatible definitions of lag and shows that Stasis can derive non-draw ticks from data it already records, while polling lag needs a per-tick input-read count in the shim journal. It also covers greenzone invalidation, auto-restore, branches and bookmarks, undo, multitrack, autofire, splicing, memory tools, Lua and speed control. It links the earlier UI conventions survey instead of repeating it.

**[ux-patterns.md](ux-patterns.md)** collects interaction patterns. Piano roll: drag-paint, wheel scrubbing, the two-cursor model, hot-change colors, row tints by state and lag. It covers markers and notes, branch thumbnails, recording modes, HUD counters and practice features. The practice sources are the SF6 frame meter and input history, the gz practice ROM's macro and savestate model, Melee Training Mode, LiveSplit deltas and Celeste's auto-filled times. It says plainly that no primary source was found for "early or late by N frames" feedback, and sketches how Stasis could measure the real window.

**[pc-tas-determinism.md](pc-tas-determinism.md)** compares how libTAS, Hourglass, JPC-rr and Stasis handle each source of variation on PC. The table covers frame boundary, game clock, spin-waits, sleeps, long save stalls, threads, entropy, CPU math, audio, GPU, input, focus, disk writes, savestate memory, restore checks and sync settings. It draws eight lessons, lists the Stasis choices not found elsewhere, and maps Stasis against TASVideos' minimum tool requirements.

**[gap-analysis.md](gap-analysis.md)** ranks 19 features that other tools have and Stasis lacks or has only in part. The order follows Stasis's measured bottlenecks first: restore taking tens of seconds and 10–16 s authoring latency. Each item gives why it matters, how others do it, and a suggested approach for the Python/Qt host and native shim. It first lists features that are already in Stasis, so they are not counted as gaps.

## Related Stasis docs

- [Earlier TAS UI conventions research (2026-10-03)](../../../planning-inputs/execution/tas-ui-conventions-current/research.md)
- [Glossary](../glossary.md), [score format](../score.md), [holorecord format](../holorecord.md), [controls](../controls.md), [tiers](../tiers.md)
- [Canonical product plan](../plans/2026-10-02-2022-feat-kptas-complete-product-plan.md)

## Open items

- "Spirit" in the research brief was not identified. The docs read it as the Source-engine mod tool family (SPT, SAR, BXT).
- No KotOR TAS or KotOR-specific TAS tool besides Stasis was found.
- Default read-only hotkeys for Dolphin and full TAStudio user docs were not confirmed from primary pages.
