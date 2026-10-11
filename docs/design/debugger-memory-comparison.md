# Captured memory comparison

The workbench can retain one successful read as a baseline and compare it with
a later successful read. The two captures must match session, target profile,
backend ID and release, PID, address and byte length. Comparison uses the
copied bytes already returned by the existing memory bridge. It issues no
additional runtime calls, and raw-byte and hex downloads retain the latest
capture unchanged.

The result reports the changed-byte count and the first changed guest address
with its before/after byte values. A failed or pending read has no comparison
result. The baseline remains available for a retry. Changing session or PID,
refreshing the process list, clearing the baseline or destroying the workbench
releases it. Replacing the baseline uses the latest successful capture.

Each read remains capped at 64 KiB; retaining one baseline adds at most one
such capture. Comparison runs once per successful read or explicit baseline
selection, not during periodic status polling. The status uses text and an
ARIA status region. Clearing the baseline moves focus to its replacement
action, or to the memory section summary if no capture is available.

The official [libTAS 1.4.7 description](https://clementgallet.github.io/libTAS/2025/10/06/version-147.html)
describes modified-byte highlighting in its hex viewer. That supports making
memory changes discoverable. This narrower browser implementation reports
differences between explicit bounded captures; it is not a live memory watch,
memory search, guest process-incarnation proof or Core service connection.

Source-only implementation. No tests, runtime read, reload or build performed.
