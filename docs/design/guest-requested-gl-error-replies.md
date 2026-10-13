# Observe error replies already requested by the guest

Actual candidate214aa portrait rendering remains corrupted. Call counts and
resolution messages do not identify the cause. Downstream
`glcommon_glGetError` already returns either the thread's stored error (clearing
it as before) or one host `pglGetError` result. Observing this returned EAX value
adds evidence without requesting another GL operation or draining another error.
GL4ES vendor sources remain unchanged.

Source now records replies only when OpenGL, Emscripten and the existing WebGL
counter flag are enabled. Nine fixed atomic U64 counters cover zero, codes
0x0500 through0x0506 and an aggregate other-code bucket. Counters saturate;
there is no allocation, timer or mutex in the observation. Original call,
return and stored-error clearing semantics remain intact. Internal host error
queries outside this guest entry point are excluded.

The new versioned getters expose fixed metadata/counters only. UINT32_MAX in
the code getter denotes Other, not a reported literal error code. The getters
are observational atomic reads and do not themselves establish a paused
snapshot. The browser consumer must validate ABI version/slot count/code layout,
preserve exact U64 values or refuse unrepresentable counts, and require/recheck
the complete pause boundary before presenting a snapshot. Report runtime-wide
aggregates; no PID/context attribution or causal timing is supplied. Zero
queries do not establish a clean renderer; error-free observed replies do not
prove correct pixels or the absence of errors between requests.

Producer source is implemented and backed up. The compatible one-job production
build completed with active/exited/MainPID0/Resultsuccess/ExecMainStatus0. SDK
WASM SHA2561a1f9630b50ab44e15171acdd852ff33d9b89d865ea4084aba90e7d19feb9226
staged candidatef6aca22d9e8838dc203bde0a7ee77d14bdd431b6d37e9c3c926c0a417a3644a4,
qualifiedfalse/unlaunched. The existing/default selection is unchanged and no
installation assets were copied or uploaded. Actual reply capture remains
unadopted and unqualified.
Bridge/parent/view/export wiring is now implemented in source: the bridge
requires ABI v1/nine slots/exact code layout and complete pause before/after
capture; U64 counts and their sum must fit exactly in JavaScript or the optional
field stays unavailable. Parent and UI normalize the fixed record again under
their pause admission. Execution shows an explicit runtime-wide summary and
exports the normalized snapshot without another request. Cached observations
are invalidated with existing control/run boundaries. The consumer joined the
completed compatible production batch. Existing candidate30 and every retained guest
remain intact. No synthetic GL queries, tests or test probes were run. This is
a P1 diagnostic implementation decision, not a new direct user requirement or
a graphics repair. Other runtime/platform support remains unqualified.

## Actual startup adoption on 2026-10-13

Opened stagedf6aca separately in tab32/sessionaa9f26db-8441-4e4b-91e0-3ba20bc46c94 with glDispatchTiming=1. This supersedes the earlier unlaunched status. Existing214aa tab30 stayed fully paused at presentation393, black after first-area Loading, with its recording active. No existing guest was reloaded or hotpatched.

Started recording27b207b9-9869-413c-b660-154883671843 before the new guest's first presentation. Normal Pause confirmed4/4workers and presentation0. Execution displayed the zero-query message and normal export retained all nine normalized zero-count buckets with queries0. Export /home/brunner56/debug-execution-2026-10-13T18-29-31-278Z.json SHA9833526b83a949c8eb2348fec8bbeef7398fb396de5ff7e5ef3f3d2155f22017. Screenshot /tmp/kptas-f6aca-zero-replies-startup-20261013.jpg.

Producer/bridge/parent/UI/export zero-query path is qualified for this actual startup pause only. Nonzero replies, rendering, process/context attribution, performance and causality remain unqualified. No guest GL query was added or triggered for this capture. Normal Resume confirmed3/4workers/presentation0; startup and recording remain active, with no scored inputs. Default selection and Workers gate are unchanged. No build, tests, test probes or private-asset publication occurred during adoption.
