# Recorded first-area load progress

Candidate214aa remains the recorded owner30 session25751740. No replacement, reload or hotpatch was used for these observations. Actual inputs are released through score tick14. The visible game is still loading; playable-area acceptance remains open.

## Compare actual pauses

Both captures confirmed43/43workers parked.

| Displayed field | Presentation366 | Presentation373 | Difference |
| --- | ---: | ---: | ---: |
| Range exchanges | 4669 | 5889 | 1220 |
| Bytes read | 61170718 | 77161502 | 15990784 |
| Cache hits | 25941 | 31178 | 5237 |
| Read failures | 0 | 0 | 0 |
| Short reads / retries | 14 / 14 | 15 / 15 | 1 / 1 |
| Completed SDL proxy calls | 2170588 | 2186224 | 15636 |
| glTexCoord2f | 592644 | 598168 | 5524 |
| glVertex4f | 225292 | 230920 | 5628 |
| glVertex3fv | 369736 | 369736 | 0 |

Normal execution exports are /home/brunner56/debug-execution-2026-10-13T18-14-22-384Z.json and /home/brunner56/debug-execution-2026-10-13T18-20-23-970Z.json. The changing counters establish work between these captures. They do not identify a bottleneck or prove correct pixels. Cumulative elapsed proxy values can include pause waits and exclude direct owner calls. Presentation counts are not simulation frames.

## Source findings

BoxedWine glcommon.cpp serializes GL4ES callbacks on the SDL owner because translator state is process-global. callOpenGL dispatches each non-owner callback synchronously. callOpenGLCallback restores guest context and records its callback count before invoking the callback. sdlgl.cpp checks context lifetime and translator state even on a same-handle restore. No vendor code was changed.

Per-operation elapsed samples already exist behind BOXEDWINE_GL_DISPATCH_TIMING=1, selected for a fresh localhost launch by glDispatchTiming=1. The retained session was launched without that flag. It cannot provide those samples retroactively, and no runtime mutation was attempted. Operation counts alone do not justify bypassing owner serialization, lifetime checks or marshaling.

The owned-install adapter retains bounded64KiB cached chunks and exact range validation. Its existing comments record larger synchronous range failures. These reads should not be enlarged based only on cumulative read counts. No transport rewrite, build, tests or test probes were performed.

Normal Resume after the second capture confirmed40/43workers at presentation373. Continuous recording remains active. The next observation stays on this same session.

## Inspect the staged backing file

Reassembled only the existing Wine root chunks in memory and checked SHA256a14446b27adbaccd4fac25dfb4e951da2dd4978fecfbf6e16aa718871fe4b0c5. Extracted its lib/libc.so.6 for passive local disassembly:1655568bytes, SHA256141319771edb3c2ffbd91d7617dc133bc70eafa3bf718defa60d11ba5a729d7d. This is the staged runtime asset, not a new live memory capture or verified live-file digest.

ELF executable LOAD file offset and virtual address both begin at0x22000. File offset0xcd1c4 therefore corresponds to ELF address0xcd1c4 in that segment. Objdump labels the function beginning0xcd16b as __read@@GLIBC_2.0. Address0xcd1c2 contains int0x80;0xcd1c4 contains cmp eax,0xfffff000. Earlier offset0xcd197 also contains int0x80. The actual saved thread11 snapshot had EIP0x104fd1c4/EAX0x1000 and state Paused. Other captured threads reported Kernel wait at0x104fd1c2/EAX3. Those values are consistent with the staged read wrapper, but no live byte comparison, syscall attribution or wait cause is established.

Evidence paths: artifacts/a14446-libc-saved-address-analysis.elf and artifacts/a14446-libc-read-disassembly-20261013.txt in the WASM workspace. Disassembly SHA2568337288abf07a7c236d9470a1e9083c383620566bfa42591002081204d14daa3. No asset publication, guest mutation, execution or test was performed. Actual game subsequently advanced to presentation383 with loading artwork and nearly full bar; acceptance remains pending.
