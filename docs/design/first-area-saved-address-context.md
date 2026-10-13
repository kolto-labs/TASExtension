# Interpret saved first-area addresses

Actual debugger use on ef8991/session632bc210 paused252/full50 captured11processes. PID10 wine was the reported presentation owner. Registers captured9threads; executable/build identity is not verified by that association.

Thread11 savedEIP0x201e3240 mapped to /lib/libEGL.so.1, mappedstart0x201e3000, extent0x1000, fileoffset0x3240. Thread106 savedEIP0x20027332 mapped to /opt/wine/lib/wine/i386-unix/dsound.dll.so, mappedstart0x20011000, extent0x22000, fileoffset0x27332. Prepare then separate Capture operations used the actual saved-register navigation. No wait-cause inference follows from a Paused/Kernelwait flag.

The staged root archive was read and hashed: a14446b27adbaccd4fac25dfb4e951da2dd4978fecfbf6e16aa718871fe4b0c5,99450694bytes. Only two ELF entries were retained locally for analysis:
- artifacts/a14446-egl-saved-address-analysis.elf,31540bytes,SHA25677bd700810be70af752a56c1d5745f1d73fe2aa7ca8ca8f98692223e137f93ca
- artifacts/a14446-dsound-saved-address-analysis.elf,358940bytes,SHA256abc9bb7786ffee86af6e36260932a676bed3fe3efd607cb2d09e883060a14e71

Both executableLOADsegments have matching file offsets/virtual addresses for these offsets. Staged EGL offset0x3240 is the eglSwapBuffers symbol and begins int0x99. Its following inline callback-id bytes are data for the emulator dispatcher; generic objdump instruction decoding of those bytes is not executable-instruction evidence. The DirectSound saved offset lies in a floating-point-processing loop with an indirect call; no named mixer function or audio-stall cause is established.

This compares metadata with staged artifacts. Live mapped bytes, loaded-file identity, object validity and causal failure remain unverified. Actual register/mapping exports in user home retain their observation timestamps:
- debug-registers-2026-10-13T22-23-53-056Z.json
- debug-address-mapping-2026-10-13T22-24-45-971Z.json
- debug-address-mapping-2026-10-13T22-25-18-111Z.json

No binary execution, vendor modification, AgentDecompile launch, tests, probes, private game asset copying or publication.
