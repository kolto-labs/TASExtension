# Inspect audio route history

The live recording inspector reported a running capture context with no active guest devices during first-area loading. Its zero-valued capture clock keeps encoding moving, but it is not game PCM. The connected INI declares sound enabled; loaded configuration identity and audible playback remain unverified.

The AudioWorklet owner's existing snapshot now includes optional `telos.audio-device-lifecycle/v1` metadata scoped to the runtime document. Counters record existing events; they do not open devices, copy PCM, query the backend, alter drains or add timers.

| Counter | Existing event counted |
| --- | --- |
| openAttempts | Entry to the AudioWorklet route's open method |
| openRejected | Validation/availability rejection or synchronous open exception |
| openedDevices | Device inserted into the route's map |
| disposedDevices | Device successfully removed from that map |
| nodeReadyEvents | Worklet node constructed and connected |
| nodeFailureEvents | Node setup exception or processor-error callback |

Counts saturate at Number.MAX_SAFE_INTEGER and expose countsSaturated. They persist across device close/reopen and context shutdown within the document. They are event totals, not mutually exclusive outcome partitions, current device counts, PCM totals or simulation ticks. SDL fallback and other audio routes are outside this record.

The existing manual capture inspector normalizes the six counters and saturation flag. Missing metadata stays null for older runtimes. Explicit inspection reads the existing owner snapshot once; export retains that dated observation. This does not add an arbitrary-game debugger capability or prove encoded/audible audio.

Source is implemented in the downstream BoxedWine audio library and existing untracked recorder. Original library retained as artifacts/boxedwine-audio-worklet-before-lifecycle-20261013.js. Production service kptas-wasm-build-audio-lifecycle-20261013 completed Result=success, ExecMainStatus=0. Build flags retain the established multiThreadedJit/PTHREAD_POOL_SIZE=4/WEBGL_COUNTERS/KPTAS_DISPATCH_TRACE/KPTAS_PROXY_CONTEXT_LIFETIME configuration, with one build job. WASM hash remains62999e8e0c4fe4c3c32033be1fc8d4a941a4540a378dd8e56a4aee1446cf26a4; generated JavaScript hash56ab571c0d97d1b07d65244e309161953945c52da395ce7c4a09dd324de12c3e.

Production stage --reuse-staged-root exited0 and produced9a5c8cefc8b566a3e1afc3e6b3d70f7c71db689fdcc48b1d4dea66f540afb226. It also includes the pending bounded texture-trace retention and visible audio-save fallback. Default unchanged; no installation assets copied or uploaded. Candidate unlaunched and behavior unqualified. Both live guests retain their current source.

No tests, probes, synthetic requests, vendor edits, live hotpatch/reload, publication or deployment. Dirty/index/data preserved. Native capacity gates remain unmet; this small incremental WASM build does not waive them.

## Actual title observation

Candidate9a5c8 ran as session6dd2a90d-5088-4b2c-a541-40a53b16ae82 with recording1d23a5ea-a560-41ed-bb84-e585b2dfb662. Recording acknowledged active before the first presentation. Normal manual inspection displayed the six lifecycle fields. At the visible paused title415/full49, all six were0, countsSaturated false, active devices0, context running and capture lease owned. This establishes no observed open attempt on this AudioWorklet route through that observation. SDL fallback/other routes, audible audio and encoded audio remain unverified. Nonzero, failure, close/reopen and saturation paths remain source-only.

Visible audio link preparation was observed, but automatic export and direct Save audio observation activation each produced no download event. The subsequent parent-workspace route is source implemented/staged and remains unadopted. Original downloaded-file behavior is not fixed or qualified by this display evidence.
