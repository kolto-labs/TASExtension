# Holorecord bundles

`telos run` writes a directory:

```
a.holo/
  journal       append-only chunk stream: header, score, environment, then one TICK per tick
  pages/        content-addressed store: memory pages, state-tree leaves and roots
  keyframes/    materialized keyframe packs
  files/        file blobs and trees for the game's save and temp folders
  index/        disposable cache; delete it and it is rebuilt
  meta.json     wall-clock and host notes, outside the hash chain
```

One TICK record names one complete state: the pages that changed since the last tick, plus a state root over the whole map. That is how one state serialization equals one tick. Byte layouts for everything below are in [contract §8](design/contract.md).

Commands here are written as `telos`. From the repo root that is `PYTHONPATH=KPTASInterface python3 -m kptas`.

## The journal

Every chunk is framed the same way:

| Offset | Size | Field |
|---|---|---|
| 0 | 2 | magic `HR` |
| 2 | 4 | type, a 4-character code |
| 6 | 4 | payload length |
| 10 | n | payload |
| 10+n | 32 | sha256 of the payload |

The order is `HOLO` (header), `SCOR` (the compiled score), `ENVP` (the environment pin), then `TICK`, `MARK`, `EPOC` and `RCPT` as they happen, and `SEAL` on a clean close.

A reader scans from the start and stops at the first bad or cut-off chunk. A writer truncates to that point before it appends. A crash costs the torn tail, not the run.

## The chain

- H_0 = sha256(sha256(HOLO) ‖ sha256(SCOR) ‖ sha256(ENVP)). The first 32 hex digits of H_0 are the bundle id.
- Every later chunk carries H_k = sha256(H_{k-1} ‖ type ‖ body).

Marks, epochs, receipts and the seal are chained too, so editing any of them breaks every link after it.

A recording holds no wall-clock time and no random id. Only seeks and verification add receipts, and those may carry timings. Two recordings of one score should therefore produce the same journal, byte for byte. On PySim they do:

```
$ cmp a.holo/journal b.holo/journal
$
```

## Pages

- Memory is stored as 4 KiB pages, keyed by BLAKE2s-128 of the page. The zero page is never stored.
- A changed page is stored as its XOR against its previous version at the same address, when the XOR has more zero bytes than the page. A chain of XORs ends at an intact page and is at most 32 long (`store.max_delta_chain`).
- A tick's new pages are compressed together, up to 64 to a frame (`store.batch_pages`).
- The codec is `auto` by default: zstd on Python 3.14 or newer, zlib otherwise, at level 3. `zlib`, `zstd` and `none` can be set. New recordings store the resolved codec in the header. A bundle written with zstd needs Python 3.14 to read its pages; an unavailable decoder or corrupt object is an error, never a reason to fall back to replay.
- Store frames end in a CRC-32. Content keys check the decoded bytes anyway.

The **state root** is a sha256 tree over the page map. Each 4 MiB range is a leaf, and the root hashes the non-empty leaves in order. Page protection is part of it, so commit state and layout count as state.

## Keyframes

A keyframe tick stores the whole map as tree objects, not just the pages that changed. There is one every `seek.keyframe_every` ticks (600), one at each epoch start and one after each restore. Every `seek.materialize_every` ticks (36000), a keyframe is also written as one file, so a single sequential read restores the image.

To rebuild map(T), take the nearest keyframe at or before T, then apply the page refs of every tick after it. The rebuilt root must equal T's `state_root`.

## Reading a bundle

```
$ telos show a.holo --legend --to 2
...
verbs: I input O os-input G gate C clock W wait P capture R read E os-event S screen T restore F files-restore | lab: w write s semantic x fixed-clock f files-write
flags: c captured r restore-point k keyframe m materialized v evicted h hold e epoch-start s sim-root f files

tick  ep  verbs  flags  reached   sp  sub  journal   root      note
0     0   GC     crkme  recorded  1   0    d99fb145  a06132d4  .
1     0   IGCP   cr     recorded  1   0    434e6e81  b675d1a4  w
2     0   IGCP   cr     recorded  1   0    f9f7c928  0ebb9321  w
```

`ep` is the epoch (one game process). `sp` counts server-loop passes in the tick, `sub` counts load-screen presents, and `journal` and `root` are the first 8 hex digits of each digest.

PySim captures every tick. It also simulates a load, so ticks 11 to 23 of the example carry `h` (hold) and are captured but not restore points:

```
10    0   IGCP   cr     recorded  1   0    b46cf646  612a84f1  w
11    0   IGCP   c      recorded  1   0    15a5f7c0  bd9cbc05  w lmb rmb
12    0   IGCP   ch     recorded  1   3    c8653c86  c1ec3f0d  w lmb rmb
```

In the game every tick is captured: the heap in the fixed arena, the exe's writable data and the shim's own game-side state, plus a journal of the exe's writable data digest, engine fields and rand call counts. The M1 exit score (3,823 ticks) made 2,516 MB in the M3 store format. Repacked into the current one it is 533 MB; see [Size](#size).

## Size

Most of a game bundle is pages. In the M1 exit run a tick changes about 365 pages, and most change in a few bytes. Each changed page is stored as its XOR against its previous version, and a tick's pages are compressed together. An area load is different: tick 167 writes 48,897 pages of fresh data, which compresses only about 1.6x.

The M1 exit bundle, part by part, in the M3 format and repacked (zstd, level 3):

| Part | M3 format | Now |
|---|---|---|
| pages | 2,382.7 MB | 448.0 MB |
| keyframes | 17.4 MB | 15.7 MB |
| journal | 37.3 MB | 37.3 MB |
| index | 76.2 MB | 31.0 MB |
| files | 2.2 MB | 1.3 MB |
| total | 2,515.9 MB | 533.2 MB (4.7x) |

The load alone is 133 MB of the new pages. Outside it, a tick costs about 80 KB. A short run that is mostly the load gains less: the first 301 ticks shrink from 243 MB to 182 MB. The first 1,000 ticks take 662 MB in the M3 format, 244 MB with zstd and 259 MB with zlib (Python 3.12).

Writing got faster too, since there is less to compress. Committing those 1,000 ticks through `Bundle.append_tick` took 157 ms a tick in the M3 format, and 70 ms with zstd or 76 ms with zlib now.

`store.max_delta_chain = 128` makes the M1 bundle 492 MB, but a restore then reads up to 3 times longer, so the default stays 32. A run that only compares journals and roots can keep no pages at all: `store.pages = "digests"`.

`telos store repack SRC DST` writes a copy of a bundle in the current format:

```
$ telos store repack m1.holo m1-new.holo
repacked m1.holo -> m1-new.holo
  result            pass
  journal           identical (37,303,638 bytes)
  ticks             3823; 3823 state roots rebuilt from the copy
  objects           1357606 decoded and checked against their keys; 1357409 in batches, 1249028 XOR (deepest chain 32)
  ...
  total bytes         2,515.9 MB ->     533.2 MB  4.72x
```

The journal is copied byte for byte, so the bundle id and every chain link stay the same. Each object is decoded and written again with the configured codec, level, batch size and chain length (`--codec`, `--level`, `--batch-pages`, `--max-chain`). Then the copy's index is deleted and the copy is opened from its packs alone. Its objects must be the source's objects, each decoding to its key, and every captured tick's map must rebuild its state root. Every source pack is checked before copying, including duplicate objects and materialized keyframes. Corrupt or truncated packs are refused rather than reconstructed from journal refs. The source is only read. A reader of this version still reads bundles in the older format.

## Seeking

`telos seek BUNDLE TICK` brings a live backend to a recorded tick and checks it. There are four ways to get there.

| Mode | How | Tiers | Checked by |
|---|---|---|---|
| replay | Start from the boot, replay the score up to T | all | journal (or `sim_root` when qualified) |
| restore | Write T's files, then W, the pages where the live map and map(T) differ, then check | lab, tas | full root, journal, and a continuation canary |
| hybrid | Restore the latest keyframe at or before T, then replay to T | lab, tas | full root |
| semantic | Write back the journalled engine fields; the branch is marked tainted | lab | diff only |

The default is restore in lab and replay in tas and hands-off. `seek.mode` in `telos.toml` picks another one, and also takes `checkpoint` for replay and `exact` or `tick_restore` for restore.

A mode that cannot apply to T is swapped per `seek.on_incompatible` (hybrid by default). A mode whose check fails moves down `seek.fallback_chain` (hybrid, then replay), each level once. If the last one fails, the seek halts and reports the first field that differs. It never loops.

Tick 15 is inside the PySim load, so a restore there falls back:

```
$ telos seek a.holo 15 --mode restore
telos: seek 15: restore is not possible: tick 15 is not a restore point (other)
  result            pass
  mode              hybrid
  fallback          restore (tick 15 is not a restore point (other)) > hybrid
  verifier          pysim; origin restore@0; replayed 15 ticks
```

(Receipt trimmed.) Hybrid restored keyframe 0 and replayed 15 ticks. A plain restore to tick 40 checked the full root and passed its canary.

### In the game

Every tick of a shim run is captured and is a restore point, so every tick has a savestate: its page map (map(T)) and its files tree (`files_root`, the stage's `Saves`, `Temp`, `currentgame`, `gameinprogress` and the other writable paths in `files.stage_paths`). The shim cannot see file writes, so the shim backend plays a step of n ticks as n one-tick steps and scans the files at every gate. Only the first carries the note's first-tick edges, so the game sees the same input. A restore writes the files back in place, then the pages.

A restore verifies the whole sim set against T's root, and it is complete: a fresh process restores at its boot gate straight to any tick and goes on exactly as the recording did. Two kinds of state live outside the game's memory and come back with it:

- **The engine's bookkeeping.** The capture walks the arena up to the highest granule it has seen. A restore raises that span to T's pages, so pages T has above a fresh process's span are watched from the first tick on.
- **The GL driver.** The game sees it: `GLRender::MakeLookUpTexture` asks `glIsTexture` every frame and rebuilds its texture when the answer is no, a save's `Screen.tga` is a fresh render of the scene, and some context state (the clear color) is set only during an area load. So `glIsTexture` and `glIsProgramARB` answer from `harness_game`, and every texture, ARB program and the context state have a sha256 key there that the state root covers. The shim reads each changed object back at the gate (levels, formats, parameters; the context through about 1,300 queries) and writes its descriptor and data once per process to `<session>/gl/`; the bundle keeps them as file blobs. After the pages, `gl.need` lists the keys the driver lacks and `gl.apply` rebuilds those objects and the context from the bundle, then reads each one back and checks its key.

`seek.restore_warmup = "loads"` remains for diagnosing a restore that is not complete: the process first plays, checked like replay, through its first tick and the tick after the last load-screen tick at or before T. The default is `"none"`.

`seek.hot_tier_mb` (default 512) keeps verified object bytes in RAM for the life of the process. Each rewind closes and reopens its bundle chain, which used to empty every cache, so the restore read its pages, files tree and GL blobs from disk again. A byte string enters the tier only after its hash matches its content key. The store must still locate the key before the tier answers, so missing and evicted objects raise as before. Set it to 0 to turn the tier off. `seek.hot_maps` (default 32) sets how many resolved page maps a session keeps. With `KPTAS_SEEK_PROFILE=1`, each seek logs a `seek-hot` line with its hits and misses.

On the Save Teleport score (737 ticks, quicksave at tick 497), fresh processes restored at the boot gate to ticks 300, 480 and 495. Each rebuilt 393 to 396 GL objects (textures, programs, the context), all of them read back to their keys, and then matched the recording on every tick to the end: journal, state root, and the files tree, including the quicksave's `Screen.tga`. The restore to 495 is the tick before the F4 note, so the save is the first thing the restored process does.

In tas a restore can only return to a state the game was in. Before its first restore, a seek hands the process the recorded roots of the bundle it seeks in, once. The shim then refuses any target root that is not one of those and not one it captured itself, before it writes anything. A restore that does not verify stops the session: the shim refuses every tick until a restore verifies or the game is relaunched. The tick after a restore journals it, with its root and the sha256 of the admitted roots, and the judge checks both against the bundle. Journals are compared without that entry, so a branch still matches its parent tick for tick.

#### Running it

The game needs a directory holding K1 GOG 1.03 (`swkotor.exe`), a Wine prefix and a display. A `telos.toml` for it:

```toml
[run]
backend = "shim"

[game]
dir = "~/kotor/stage"              # swkotor.exe and its data
wineprefix = "~/kotor/prefix"
launch_wrapper = "setarch_R"       # no address randomisation
display = ":173"                   # a private Xvfb works
pin = "~/kotor/pin"                # Saves/, logs/ and swkotor.ini as every run starts
prefix_pin = "~/kotor/prefix-pin"  # system.reg, user.reg, userdef.reg

[shim]
dll = "~/kotor/dinput8.dll"        # a pinned build, not the one in native/shim/build
dll_sha256 = "57cefdacb95d2e2d22c6e28487e88738ad39f670dae536b4c9d7897f8b35b3cc"
                                   # the file must hash to this, and so must the build the game loaded
# session_dir = "auto"             # ring, restore and GL files: $XDG_RUNTIME_DIR/kptas/session-0 for every
                                   # run, seek and judge, so the path is the same in all of them
```

Every launch is a cold start. The stage's writable paths (`files.stage_paths`) are copied from `game.pin`, the ini's `Disable Sound` is set from `audio.device`, the prefix's registry files from `game.prefix_pin`, and `shim.dll` is copied in as `dinput8.dll`. The shim's hello names the build it runs (a sha256 prefix of the DLL) and its full sha256, and telos stops when they do not match `shim.dll_sha256` (all 64 digits when the pin has them). The environment pin is taken from the stage as the launch leaves it: the pinned DLL and the pinned ini with its sound line set. The prefix's wineserver is stopped before the launch and again when the game ends; nothing is stopped without an explicit `game.wineprefix`. The game gets only the pinned environment. Inside a container where Wine lives on the host, telos starts it through `host-spawn` (`game.host`).

Sound is on. With `audio.device = "virtual"`, the default in lab and tas, the shim answers every Miles call from a model on the virtual clock, so the host's audio hardware never reaches the game, and each journal entry starts with `aud`: the tick's Miles calls, its state changes and a digest of the device. `audio.device = "none"` runs the sound-off game that older bundles recorded, and a bundle whose env pin names no device replays that way.

```sh
telos run m1_exit.score --out m1.holo                      # every tick captured, pages and files
telos seek m1.holo 2400 --mode restore                     # boot, restore tick 2400, check it
telos run m1_exit.score --from m1.holo@1200 --mode restore  # restore 1200, record the rest as a branch
telos diff m1.holo m1@1200.holo                             # the branch against the original
telos verify m1.holo --fresh                               # judge replay from boot in a new process
```

Measured on the M1 exit score (3,823 ticks: menus, Load Game, walking) with shim build 31e7ec71:

- The run took 9 minutes and made a 2.4 GB bundle in the M3 store format. Its journal matched the M2 driver's tick for tick. Repacked, the bundle is 533 MB.
- A 1,000-tick run of the score in the current format made a 244 MB bundle. Its journal, state roots and files trees equal the M3 run's on all 1,001 ticks. `seek 800 --mode restore` into it passed its root check and canary, writing 2,109 pages.
- `seek --mode restore` took 14 s to tick 150, 47 s to 1200 and 50 s to 2400. Each passed the root check and a 120-tick canary. `seek 2450 --mode hybrid` restored keyframe 2400 and replayed 50 ticks (91 s, an earlier build).
- The branch from tick 1200 matched the original on all 2,622 ticks after it: journal, state root and files tree.
- `verify --fresh` compared 3,823 of 3,823 ticks in 5 minutes. Its last state root equals the recording's.
- Save length: the Save Teleport score with header `save_ms=1000` moved the player 16.540 m from tick 493 to 615, and 14.197 m with `save_ms=0`. Two runs at 1000 were equal on every tick's journal, state root and files tree.

With shim build c985693c, which puts the GL driver in the tick, on the same score (two other game rigs running):

- The run took 33 minutes and made a 2.7 GB bundle. Its files area, which holds the GL descriptors and images next to the stage's files, is 252 MB.
- `seek --mode restore` to ticks 150, 600, 1633, 2400 and 3001, each from a fresh process with no warm-up, passed the root check and the 120-tick canary (2 to 5 minutes each). The receipts say `origin restore@T; replayed 0 ticks`.
- Fresh processes restored at the boot gate to 600, 2400 and 3001 matched the recording on every tick through 3822 (journal and state root), and the files tree at 3822.

With shim build 57cefdac, which merges the M3 work (the fixed machine the game sees, restore admission, the GL driver in the tick, reference math, every save kind, frames, the virtual Miles device and this store format), on the same score with the default config (no `shim.session_dir`, so `session-0` for every run, seek and judge):

- The run took 9 minutes and made a 762 MB bundle: pages, keyframes and the GL descriptors and images. `verify --fresh` compared 3,823 of 3,823 ticks, and its last state root equals the recording's.
- A second cold run matched it on all of its 1,000 ticks: journal, state root and files tree. So did a third with another install path (reached through a symlink), another session directory, another user name, `TZ=JST-9` and 4 CPUs instead of 12. The game saw none of those: the shim served it `C:\KotOR`, user `player`, UTC and one CPU in every run.
- `seek --mode restore` to 150, 1633 and 3001, each from a fresh process with no warm-up, passed the root check and the canary and replayed 0 ticks. The restore to 1633 wrote 64,290 pages and rebuilt 396 GL objects, each read back to its key.
- A branch recorded from a restore at 1633 matched the recording on every tick to 3822: journal (without its restore entry), state root and files tree. `verify --fresh` of the branch passed all 3,823 ticks.
- One game session restored to 100 ticks drawn at random, one of them 150 so that its ticks cross the area load, and played 60 ticks after each against the recording: 6,512 ticks compared, journal and full state root, all equal (28 minutes). That is the M3 exit.
- In lab, a byte written after a restore stops the next tick's comparison and names what differs: a journal field for the server's pause byte, or the one changed page for a byte nothing journals.
- On the Save Teleport score, a branch restored at the tick before the quicksave note matched the recording on every tick to the end, the quicksave's `Screen.tga` included.
- Frames on and off gave the same journal, state root and files tree on all 1,001 ticks of a 1,000-tick run. So did the virtual Miles device on 1,301 ticks with the host's audio device reachable and without it. Only a lab control run with the real Miles opened that device.
- The bundle holds 799 MB by the repack's count: pages 456 MB, files 258 MB (almost all GL descriptors and images), journal 38 MB, index 32 MB, keyframes 16 MB. The same score with the GL objects made 2.7 GB in the M3 format. A repack of it is the same size, since a recording is already written that way, and a restore from the repacked copy passed.

All four modes run on PySim. In the game replay, restore and hybrid run; semantic stays lab only. Replay starts from boot for now, because no backend can start a run from a save file yet. A score can still load a save the way a player does, through the Load Game menu.

#### Frames

`capture.frames = "every_tick"` stores one frame for every tick. The shim reads the back buffer with `glReadPixels` at every present, before the present runs or is skipped, so `replay.present = "skip"` still gives frames. At gate n it writes tick n's frame into a ring file on tmpfs, and telos stores it in the bundle:

```
a.holo/frames/
  e0.mkv        lossless FFV1, one file per epoch (and per frame size)
  index.jsonl   one line per frame: tick, size, presents and reads in the tick, flags, a digest of the pixels
```

With `capture.frames_sink = "png"` each tick gets its own `e0-t000123.png` instead. A tick with several presents (a load screen) keeps the last one. A tick with none repeats the frame before it (`repeat`), and the ticks before the first present are black (`blank`).

The capture is output only. It is not journalled, and the shim makes the same calls on the game stack with it on and off. On the first 1,000 ticks of the M1 exit score, a run with frames and a run without them had the same journal, state root and files tree on every tick. So did the Save Teleport score (736 ticks), whose quicksave writes a Screen.tga taken from the framebuffer.

```sh
telos frames info a.holo                    # frames, ticks covered, repeats
telos frames video a.holo --out a.mp4       # H.264 with "tick N" drawn on every frame
telos frames video a.holo --from 150 --to 200
```

```
$ telos frames info m1.holo
m1.holo/frames: 1000 frames written, 1000 ticks
ticks 0..999, none missing
blank: 1 ticks
sizes 1024x768
files: 1 (e0.mkv)
```

The frames line up with the ticks:
- On 998 of the 1,000 ticks, the presents in the tick equal 1 plus the journal's subticks (its UpdateScreen calls). Ticks 162 and 163 each have one UpdateScreen and no main present.
- Mean brightness jumps from 44 to 51 at frame 161, the first load tick. It jumps there in the FFV1 file and in the video.
- The video's frame 161 reads "tick 161".

The FFV1 file takes 619 KB a frame, so 619 MB for the 1,000 ticks. The labelled H.264 video is 9.3 MB.

The cost is drawing the frame: with the present skipped, llvmpipe never draws it otherwise. On these runs that wait averaged 133 ms a frame, with the host's load average between 40 and 120. The copy took 0.8 ms and the ring write 3 ms. Under that load the wall times (488 s without frames, 492 s with) are noise. Reading into a pixel-pack buffer does not hide that wait, because on llvmpipe the read blocks until the frame is drawn.

`run --from` sends the branch's frames to the branch bundle. The frames of the seek that led there stay in the session directory.

## Branches

```
$ telos branch a.holo 40 --out a-40.holo
branched a.holo at tick 40 -> a-40.holo
```

A branch is a new bundle whose header names its parent and the tick they share. It stores only the ticks after that point and reads the rest from the parent. `telos verify` writes its receipt on a branch too, `<name>@judge.holo`, so the original stays as recorded.

## Comparing runs

`telos diff A B` lists the ticks where two runs differ, and `telos bisect A B` finds the first one. They compare journal digests, and compare roots only when both runs share a determinism key and a mask registry. Without those, the output says so:

```
no divergence (journal only; roots not compared)
```
