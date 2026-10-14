# Retain every paused GL entry-point count

The existing SDK counter ABI remains version1. The bridge projection now uses
`telos.paused-gl-counts/v2`: every nonzero entry is retained, with at most4096
slots. Names remain bounded to127 printable ASCII characters, indices unique,
counts safe nonnegative integers and entries sorted by count then index.
Capture still requires the same complete worker pause before and after reads.
Optional timing reads remain limited to the16 count leaders. Counts are
cumulative callback/host observations, not GPU timings, scene-only counts,
instruction locations, simulation frames or Win32 input sampling.

Parent normalization and the Debug UI accept both schemas. Version1 retains
its16-entry projection and explicit truncation; version2 requires exactly the
reported nonzero count and no omitted nonzero rows. Display remains16 leaders.
Execution-view export additionally retains validated pausedGlCounts, including
all version2 entries. Its64KiB bound still refuses oversized exports without
silently truncating the capture. Invalid telemetry does not disable execution.

## Actual issue that motivated the source change

Corrected079d retained a black screen while native presentations advanced.
The actual complete pause at present372/full36workers exported only16 of85
nonzero counters. The displayed leaders included glTexCoord2f511140,
glColor4ubv374794 and glVertex3fv347698, with2143908 completed proxy calls.
Those cumulative leaders cannot establish which draw/copy/framebuffer calls
occurred in the black interval. Missing low-ranked counters are not zero.

Normal Escape21/release22 completed at presents374/375, with total2/before1/
after1 and total1/before0/after1 respectively. The displayed console reported
Module Loaded and Module Running. No visible playable-level acceptance followed.
Console evidence and source inspection showed GL context lifecycle changes,
software-window requests and a shared presentation canvas. That does not prove
a handoff defect; no speculative SDL or GL4ES vendor patch was applied.

This projection/export change is source-only for the retained079d owner. A
JS-only stage2ad56c89b4c00be7133eda118e1a1cbb6cf1e53d3f2e0cca44194b19a8a1c44f
reuses SDKf7fe. A fresh local compatibility-profile owner is booting, with
Frame Buffer=0 copied into a separate private startup INI. Original resources
and owner remain retained. No rendering improvement or profile acceptance is
claimed before actual game evidence. No tests or test probes were run.

## Actual projection and overlay acceptance

On2026-10-13, the fresh2ad compatibility-profile owner reached the original
KOTOR title and accepted New Game press/release1/2 at full36worker pause.
The normal execution-view export retained schema v2,3142slots,81nonzero
entries,truncatedfalse and sampleInterval64. This qualifies this complete
nonzero counter projection/export for this pause. It does not qualify the
black-screen cause, timings, graphics profile, game cadence or replay.
Artifact: /home/brunner56/debug-execution-2026-10-13T13-19-51-830Z.json,
SHA256b2f9a86eaeb1a6cf440c06aed64d7187d052721424d890cb5a55e736c873bc31.
After normal Resume, character-creation Loading was visible and recording
41256372-fc29-4abf-a953-c830519cd814 remained active. The same normal overlay
showed cockpit area game/one720pxrow and game panel area game/y0/height720,
qualifying the grid-name repair for this loaded1280x720workspace.
