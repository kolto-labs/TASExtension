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
