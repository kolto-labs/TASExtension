# Trace actual runtime faults

The retained 8f605 recorded save-reload guest reported a terminal unaligned-access error. Its browser error stack names compiled WASM function13644 at0x450691. Static disassembly of that exact retained binary shows i32.atomic.rmw.cmpxchg at the reported offset. This establishes the trapping instruction; it does not establish its pointer value, source function or corruption cause.

Production linking now emits boxedwine.html.symbols. Immutable staging includes the optional boxedwine.symbols sidecar in the payload hash inventory. This metadata does not enable profiling, modify the WASM payload or execute guest instructions. Missing maps remain compatible with historical builds. Preserve the exact release and WASM hash alongside any fault report; function indices cannot be transferred between different binaries.

The current map SHA06b780e90484bc4468148d3a9fdd3b63821b46094863fde10001e50565bb0c91 matches WASM131854078f3c56a3f0a40856bcb6efec22331c89351b2adfa041949865ce1470. It does not identify the source of the older e30165b684ce5f3183015cff9e72bdac13d1450e4c4c8f147347c2de6f70aae3 fault. A map from a future fault's exact artifact is required. No cause or runtime fix is claimed.

The failed guest was retained. Its actual recording was finalized through normal controls and its receipt read through the library UI. This is preserved partial-run media, not full-run, audible-audio, replay or rewind acceptance. No tests or probes were run.

## Correlate the retained fault body

A read-only comparison of code-section bodies found no byte-identical body across the old and current binaries. The old function13644 begins at0x45064f and spans248bytes. Current function13662, named __private_cond_signal in its own matching map, also spans248bytes and begins at4528806. The bodies differ at only four byte positions:27(201→219),189(202→220),212(235→253),235(202→220). These are call operands with an18index relocation; all other bytes match. The trapping compare/exchange corresponds to the signal routine's waiter-state CAS. This is static body/callee correlation, not applying a newer map directly to older indices or observing the bad pointer at runtime.

The pinned SDK's proxying.c signals synchronous DONE/CANCELED contexts after releasing their mutex. Upstream [PR26582](https://github.com/emscripten-core/emscripten/pull/26582) fixes the context lifetime race by signalling before unlock in both paths. The actual K1 fault is consistent with that race; root-cause attribution and reload acceptance remain unproven. Similar public error messages alone do not prove our cause.

The downstream build now supports KPTAS_PROXY_CONTEXT_LIFETIME=1. A copy of the pinned4.0.14 proxying module contains only those two ordering changes and is linked ahead of SDK archives. It preserves the SDK API/ABI and avoids editing installed SDK sources/cache or GL4ES. SDK originalSHAcbae5a9736353234d3991e19ca74ce5779fe0f7eb45a35e10f7cc2b2622b8c00; downstream moduleSHA6b0c590bc641e021fc9507d00d285ca237d02a011dbfe9cb64033707c459052a. Staging includes a backport manifest and Emscripten license only when the link configuration records the override object. The original SDK source remains unchanged.

Production command: make MAKEFLAGS=-j2 PTHREAD_POOL_SIZE=4 USE_GL4ES=1 WEBGL_COUNTERS=1 KPTAS_DISPATCH_TRACE=1 KPTAS_PROXY_CONTEXT_LIFETIME=1 multiThreadedJit. Build succeeded; WASMSHA6338488a9f693990dbf72f6baf7b6af9ccac7cf33bd689691d4c70e7f928fa3c. Candidate9aaaca97b15e58f77f6db9e6d049324a4c0de2c018d2e1bad74ffc17e19f1781 launched separately; startup/image, save/reload and replay qualification remain pending. The failed older recording and other live owners remain preserved.
