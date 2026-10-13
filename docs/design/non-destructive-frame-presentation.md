# Copy private GL frames without replacing the guest image

Actual owner28/session a7b3f16e reached first-area Loading and then a black
image. Two normal full44worker pauses at presentations99 and107 exported
all84 nonzero GL counters under schema v2. Across that resumed interval,
glDrawElements increased731 and glVertex3fv increased5393; both visible
endpoints were black. File-range failure counts stayed0. These observations
establish arriving rendering calls, not correct pixels or a proven cause.

The downstream worker presenter called transferToImageBitmap on the private
guest WebGL canvas itself. That operation replaces the source image with a
new blank image, as documented by
[MDN](https://developer.mozilla.org/en-US/docs/Web/API/OffscreenCanvas/transferToImageBitmap).
The existing DOM/main-thread branch already copies with drawImage.

The worker branch now copies the existing source into a separate cached2D
OffscreenCanvas, then transfers that copy's bitmap using the existing cropped
frame message. It retains the single pending-frame slot and existing UI
bitmap disposal. A failed send closes an untransferred bitmap and releases
the slot. The guest source is not resized or transferred by this copy path.
Private #boxedwine-webgl-thread-* contexts also retain their drawing buffer
across worker yields; the regular #canvas context keeps its existing setting.

This changes downstream source/opengl/sdl/sdlgl.cpp, not GL4ES vendor code.
Prior source is preserved at
/home/brunner56/kptas-browser-game-wasm/artifacts/sdlgl-before-nondestructive-present-20261013.cpp,
SHA-2564a45294dab310310179c47ec5b372b6e311a1f5f9df3e15d5d7a2e8365d907de.
Edited source SHA-256 is
8d91d4e2f9825e97ccf2d7969e58d9e3f030904fec5b2b15c00c872377d6c401.
Existing source outside the scoped presentation/attribute edits is preserved.

The corrected trace-budget candidate5860 remains unlaunched. Its SDK code,
symbols and metadata are preserved before this separate compatible production
build:11files/7092486bytes at artifacts/sdk-before-frame-copy-20261013.
The new finite build uses the same pool4/counters/trace/proxy-lifetime options,
one low-priority job and no scheduled execution. Existing2ad owner28 remains
paused107 with its recording intact. Build/staging completion and actual
game acceptance must be recorded separately. The black-screen cause,
graphics, copy performance, memory behavior and replay remain unqualified.
No tests, test probes, syntax checks or synthetic requests were run.

## Actual corrected-candidate use on2026-10-13

The finite frame-copy production build completed active/exited, MainPID0,
Resultsuccess and ExecMainStatus0. SDK WASM SHA-256 is
89b886c68a56cb3f13dd170b9dce354c1f87dcf077a7988b850978b1379e270a.
Candidate6889abc33e9d4ef3873061f436568b2cd2a99e7fd03a04b851dcd36f6b093af5
staged in548.0ms with the existing Wine root and unchanged default selection.

Separate owner29/session8684c095-4ccc-4aa4-8633-3ae72381f3bb recorded before
first image, reached the original title and completed16 native-consumed
press/release inputs with full43worker pauses. Normal Resume showed class
selection, Quick Character, portrait, actual Name confirmation and Play,
then first-area Loading. The title still lacks menu labels and the portrait
still has a corrupted band. No graphics repair, copy-performance improvement,
in-level rendering, retained-buffer behavior or whole-game acceptance is
claimed from those observations.

Actual32 browser-captured timing entries contain completion ticks1–16,
including duplicates, in artifacts/6889-actual-wizard-timing-20261013.json,
SHA403931c6bf097e36ef63d3e8192430bd1ae63663677a37758afddf11045eb819.
They are producer diagnostics, not an independently authenticated input
journal. Continuous capture3a56bc76 remains active; old2ad owner28 remains
paused107 with its data and capture preserved. No new build or staging was
needed for these actual gameplay actions.

First-area Loading subsequently transitioned to black while the counter
advanced156. Normal Pause167/full43 retained an actual v2 capture with3142
slots/85nonzero entries/truncatedfalse. The guest console reports Module
Loaded/Module Running. Browser warning/error capture is empty; displayed
console retains rectangular-matrix resolution gaps, but none of those names
appears among the captured nonzero proxy counters. Direct-owner calls are
outside that counter scope, so absence is not proof the APIs were never used.
File-range failure count is0 and cumulative short-read/retry counts26/26.

The presenter change is not accepted as a black-screen repair. The active
VM/capture is preserved; no restart follows from this negative result.
Execution export debug-execution-2026-10-13T16-07-04-042Z.json has SHA-256
1db018adb5e15926469427c008279f3b0e3cd1c9242faad85bdeba4abb7b6ab7.
Displayed console artifact6889-black-transition-displayed-console-20261013.txt
has SHA-2563d1f77f61993a567f0a18a7d8dd622dfb9b0ee03b35846889d1f5fab98f592b3.
Those observations do not establish the underlying rendering cause.
