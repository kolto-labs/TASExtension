# Observe existing backend texture arguments

The existing vendor upload trace logs glTexImage2D arguments before later conversion. It cannot establish the internal format that reaches the backend. Actual candidate9a5c8 retained200011characters and671trace records without omission, while title labels remained missing except Quit. No texture or shader cause was established.

The downstream observed-procedure resolver now optionally wraps the existing backend glTexImage2D pointer when LIBGL_KPTAS_TEXTRACE is exactly1. It forwards the original nine arguments exactly once, then records scalar arguments and whether the data pointer was non-null. No pixels, addresses or uploaded bytes are read. No texture binding/state lookup, GL error drain, readback or extra upload/query is added. GL4ES vendor source is unchanged.

KPTAS_GL_HOST_TEX_IMAGE includes sequence, target, level, internal format, dimensions, border, input format/type and data presence. First128calls and every256th call below8192are logged, at most159records. The sequence saturates at UINT64_MAX. This is runtime-wide, unattributed backend-call input metadata; capability initialization can also upload. A returned void call does not prove driver acceptance or GPU completion. Logs explicitly retain backend_error_sampled=0 and upload_acceptance=unverified.

The existing shader-query observation ABI remains unchanged. Other procedure pointers and ordinary sessions retain their existing route. All calls still execute on the SDL owner under existing GL4ES context/lifecycle guards. Timing in an opt-in traced session includes diagnostic overhead and cannot be used as an unchanged performance baseline.

Original downstream source retained as artifacts/sdlgl-before-host-texture-arguments-20261013.cpp. Production service kptas-wasm-build-host-texture-arguments-20261013 completed Result success/ExecMainStatus0. Same established multiThreadedJit flags retained with one job. New WASM SHA2567fa06fe3d18b2c05825de09bfc66ac36223b375c0715a9e2fe55fd355e348f8d; source SHA2567069f4bcf8ce5bf6bdea02a2bfabd5a21f41612472ebdd8c4e421aa4ece9b80e.

Production stage --reuse-staged-root exited0 with565e34aa87288dd83d88012059d61d72a9492c6907f8743ce3e4e0f4879f082b, batching the pending parent audio-download route. Default unchanged; no installation assets copied/uploaded. Candidate unlaunched and both new paths unqualified. Existing live guests retain their immutable releases. No tests, probes, synthetic requests, vendor edits, deployment or publication. Coupled dirty source/index/data preserved. This observation is an implementation choice within P1, not a new user requirement.

## Actual backend argument capture

Candidate565e34/sessionab943f36 adopted the logger during normal title startup. Paused96/full49. Bounded DOM reads captured248810displayed characters and129host upload records. Observed78RGB/RGB/UNSIGNED_BYTE and51RGBA/RGBA/UNSIGNED_BYTE records. These sample results narrow the format question only; no texture binding, pixel correctness, driver acceptance or rendering cause inferred. Capability initialization may be included.

The initial one-shot browser return was truncated at200000characters and added[Truncated]. It remains preserved as a partial artifact. Full displayed logs were then collected in32768-character chunks, with declared/captured length248810 and no transport marker. No new runtime or GPU query was used.
