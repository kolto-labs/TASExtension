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

Producer source is implemented and backed up, but unbuilt and unadopted.
Bridge/parent/view/export wiring remains outstanding and should join one
compatible production build. Existing candidate30 and every retained guest
remain intact. No synthetic GL queries, tests or test probes were run. This is
a P1 diagnostic implementation decision, not a new direct user requirement or
a graphics repair. Other runtime/platform support remains unqualified.
