# Observe a pause that cannot complete

The actual 05a9b110 startup guest accepted a pause request but remained at five of six workers and zero presentations. Its process/register capture controls correctly stayed disabled. No unsafe register snapshot or wait-cause claim was substituted.

The SDK already publishes dispatch-entry EIP, latest reported block EIP/table, sample sequence, elapsed dispatch age and a kernel-wait flag. The browser-facing exports now copy at most 64 records into a fixed native scratch buffer. They allocate no memory while a pause is pending. The UI owner is the only caller of that scratch-buffer interface.

The optional bridge reader samples those records only during pausing or a completed pause, using the existing status path. It validates heap bounds, row limits and identifiers. Parent normalization and the shared Execution view retain the bounded fields and include their displayed text in the existing view export. No timer or execution command is added. Missing, disabled or invalid diagnostics do not disable controls.

These are best-effort published trace samples. Generated direct calls may execute beyond the last reported block, and the SDK may fall back to separate atomic reads when a writer is busy. The data is not a register snapshot, current instruction, mapped executable/build identity or wait cause. Dispatch age includes waits and is not CPU time. An absent kernel-wait flag does not mean running; an empty capture does not prove all workers are idle.

The published sample remains separate from paused register capture even when both are available. A completed pause can retain kernel-wait dispatch records; it does not turn those records into a current instruction snapshot.

The actual retained startup owner has not adopted these new exports or UI. Source/build/stage progress remains separate from live diagnosis. No tests or test probes were run. Existing pause admission and snapshot guards are unchanged.

Actual9aa paused input-failure view exported published dispatch rows. Reported block sequence/EIP/table were0. Source display now marks block information unavailable for sequence0, which can be initial or wrapped; it does not identify instruction0 or table0 as observed execution. Ages/wait flags remain published telemetry, not CPU timing or wait cause. The retained page is unchanged.
