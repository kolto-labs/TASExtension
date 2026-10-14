# Inspect missing menu labels

Read-only inspection of the connected K1 resources found mainmenu8x6 in data/gui.bif, resource index28,11354bytes, GUI V3.2. All five buttons use dialogfont16x16 and alignment18. Their text colors are similar cyan vectors. The source labels are present in both the SSD core dialog.tlk and the connected startup overlay dialog.tlk:

| Button | String reference | Text |
| --- | --- | --- |
| Movies |1583| Movies |
| Options |1584| Options |
| Load |1585| Load Game |
| New |1586| New Game |
| Exit |42172| Quit |

The core TLK has49389entries/5425667bytes/SHA256b95c40c863a09ae77f37ec32c9b9ad6643f4ca4f54ee8a79bea923676d2a188b. Overlay TLK has49265entries/5390721bytes/SHA2561ad520bd736491f5a387590089fc0c91c11ab8cd16cc49828d34dd2aa2bbbf78. gui.bif SHA2560e66237816ede106f15c13df5baa0abafc104af3f66aaf4512dc59e73ef54f4a. The overlay also has executable/Bink/INI/receipt files; no GUI resource replacement was found there. No assets copied or modified. This rules out missing labels in these source assets, not incorrect runtime loading or a rendering cause.

Format layouts were read from the primary PyKotor readers: https://raw.githubusercontent.com/OpenKotOR/PyKotor/master/Libraries/PyKotor/src/pykotor/resource/formats/gff/io_gff.py and https://raw.githubusercontent.com/OpenKotOR/PyKotor/master/Libraries/PyKotor/src/pykotor/resource/formats/tlk/io_tlk.py. Only selected menu metadata was output; no game resource bytes published.

Source inspection found no obvious scalar/color forwarding error. Existing GL4ES scissor code already flushes pending begin/end drawing on changed rectangles; default small-draw batching is disabled. No vendor or renderer behavior change was made from that inspection.

A downstream requested-raster trace now observes existing AlphaFunc, Scissor, BlendFunc, DepthMask, DepthFunc, StencilFunc and relevant Enable/Disable calls. It is enabled only by exact LIBGL_KPTAS_TEXTRACE1 in the known GL4ES/WebGL-counter build. At most1024records contain the known guest context, operation and raw stack argument bits. No new rendering call, GL query, pixel read or vendor edit. Arguments are requests, not backend state or accepted behavior. They may include library initialization and remain unattributed. The normal callback still executes exactly once under existing ownership/context guards.

Source backup glcommon-before-raster-state-trace-20261014.cpp retained. Actual use and rendering diagnosis remain pending. No tests or probes; all live guests and dirty/index/data retained. This is a P1 implementation choice, not another user directive.

Production service kptas-wasm-build-raster-state-20261014 completed inactive/dead, MainPID0, Resultsuccess, ExecMainStatus0. Established multiThreadedJit flags retained with one job. New WASM SHA256999d04ad68ea5f02697ee4285800e16db949230ac5063c103164d46d6c46aea6; glcommon.cpp SHA256f321f4bf2454f8b4097326acea1d26880142e9701cffe7b30be87833855b7c1d. Production stage --reuse-staged-root exited0, release6e873aa96a4e4ca288fb867040e9c3ef75898c44d6cb046b477677892e66c188. Default unchanged/no installation assets copied or uploaded. Candidate unlaunched and trace use unqualified. Current4d0 guest re-observed325/full48/paused; recording controls still report Recording game. All retained tabs marked.

## Actual candidate adoption

Displayed log captured in bounded DOM chunks,426956characters,1024requested-raster records (cap reached): /home/brunner56/kptas-browser-game-wasm/artifacts/d9403c82-displayed-requested-raster-title-20261014.txt SHA256f5245e69ca32c8e250350083f4d7b41c0532b952e70dce6349047a421e33eb6b. AlphaFunc requests:131 GL_GREATER/ref0 and5 GL_GREATER/ref0.4. DepthFunc:5 GL_LEQUAL. DepthMask:119true/37false. StencilFunc:5 GL_ALWAYS/ref127/fullmask and4 GL_ALWAYS/ref1/mask255. No Scissor call appears within this bounded capture. Source-alpha/one-minus-source-alpha blend requests appear. These are requested arguments, not backend state, driver acceptance or attribution to a particular label. Initialization and title drawing are mixed; later calls are outside the cap. Missing title labels persist. No rendering cause or fix established.

The candidate reached a full49-worker pause at presentation80, with recording active from before the first presentation. Missing menu labels remain visible. The previous unlaunched status above is historical. No backend-state or rendering-cause qualification follows from these requested calls.
