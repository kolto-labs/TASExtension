# Stop IPC trace bookkeeping after its budget

The downstream BoxedWine socket trace has a128-event diagnostic budget.
Previously its atomic event count continued incrementing and its waiting-socket
set still took a global mutex after logging had stopped. Normal IPC could
therefore continue paying for disabled diagnostic bookkeeping.

The event counter now saturates at128 using relaxed compare/exchange. The
last accepted event clears the waiting-socket set under its diagnostic mutex.
Wait registration checks the budget before and inside that mutex. Later
registration, lookup and removal return immediately when the budget is spent.
IPC buffers, condition waits, wakeups, error handling and guest data are
unchanged. The compile-time trace option and128-event scope remain available.

This is a source change in downstream source/kernel/kunixsocket.cpp. It is
not a GL4ES vendor contribution or a demonstrated cause of slow rendering.
Runtime performance, trace ordering under contention and complete game
acceptance remain unqualified until actual use of a separately staged build.
The current recorded guest keeps its immutable2ad runtime.

The pre-edit source is retained at
/home/brunner56/kptas-browser-game-wasm/artifacts/kunixsocket-before-trace-budget-20261013T1430.cpp,
SHA-256 b1b020aa9008e8753de59b343d14e446215ac7add6ea3b8def321ae2e94f452c.
The scoped edited source SHA-256 is
0398b7f04a5a09ef201418462a2448e8f36e23a59325b70121c1e3381a936842.
CRLF and all source outside the four helper functions were preserved.

Production build uses Emscripten4.0.14, multiThreadedJit, pool4,
WEBGL_COUNTERS=1, KPTAS_DISPATCH_TRACE=1 and
KPTAS_PROXY_CONTEXT_LIFETIME=1, with one low-priority job. The initial exec
attempt omitted the lifetime option and ended143; it was not accepted or
staged. The corrected finite build runs as a transient user oneshot service,
without a timer, schedule or boot enablement. Prior SDK outputs and build
configuration are preserved separately. No tests, probes or syntax checks
were run. Completion and staging receipts must be recorded separately.

## Production build and staging on2026-10-13

Corrected finite build service kptas-wasm-build-ipc-budget-20261013.service
finished active/exited, MainPID0, Resultsuccess and ExecMainStatus0.
Compiled WASM SHA-256 is
f6cb0733772007995add07ecd3f9cc6f6b64e82c9acd19de1718c37beb67828e.
One production staging batch also included the recording script/writer
dimension observations. Candidate release
5860d62fe925cd79c5b24a3bf6238878cd96bbd6bc83d65f1e2e8effa2dada33
staged in1142.8ms, qualified:false, with the same a14446 Wine root.
No installation assets were copied or uploaded by staging.

Default53711e901 remains unchanged and unqualified. Existing recorded2ad
owner28 remains live on its earlier SDK; the new candidate is unlaunched.
Compiler/staging success does not establish live trace behavior, graphics,
performance, platform support, save/reload, replay or full-game acceptance.
Current counter/recording evidence remains tied to its original runtime.
