# Preserve timing beyond the count leaders

The actualf6aca loading export at presentation103 contains82nonzero GL count entries, but timing is attached only to the16largest count entries. Source inspection found the rank<16 condition in the bridge. Less frequent operations can therefore lose their previously observed sparse samples from later captures. The compact UI's displayed timing list alone cannot identify the largest sampled costs across the runtime.

The bridge now attempts the existing three timing metadata getters for each nonzero entry with a supported metric slot. It remains bounded by the existing4096slot limit and requires a complete pause before and after capture. There are no added GL operations or error queries. Optional failed reads remain null without invalidating the captured count. Paused capture caching is unchanged.

The additive timingReadScope field names all-nonzero-entries only when the timing ABI is available. This describes attempted read coverage, not complete samples or qualification. The parent and execution exporter preserve that recognized scope only for complete v2count captures. Older captures have null scope. The UI still displays16count leaders; the export retains timing for every accepted entry.

Source implementation on2026-10-13; no build, staging or live adoption yet. The retainedf6aca guest continues without reload or hotpatch. Actual loading export /home/brunner56/debug-execution-2026-10-13T19-29-13-890Z.json is the earlier limited capture and does not qualify the new path. No tests or test probes were run. Sparse cumulative caller/callback elapsed values can include waits and are not CPU time, all-call estimates, GPU time or a proven bottleneck.
