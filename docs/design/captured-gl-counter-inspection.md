# Inspect any captured GL counter

Execution now includes a selector for every entry in an admitted paused GL capture. Changing the selection describes its exact captured count and optional sampled caller/callback elapsed values. It reads the existing status snapshot and makes no backend request or GL query. The selection index is retained in the normal execution export alongside the captured entries.

The selector is disabled without entries in a complete paused capture. Existing session and capability ownership remains authoritative. The helper refuses invalid indices, counts, names and timing values. Missing timing and explicitly zero samples have separate messages. Older captures can lack timing beyond their count leaders; this does not imply zero elapsed time. Runtime-wide cumulative samples do not identify a process, establish CPU/GPU time or estimate all calls.

Implemented in source on2026-10-13 to expose entries omitted from the compact16leader display. Retained livef6aca32 was not reloaded or hotpatched. New timing-read candidatef4a9 remains unlaunched. Actual selector use and export qualification are pending; no tests or test probes were run. Other runtime/platform support remains unqualified.
