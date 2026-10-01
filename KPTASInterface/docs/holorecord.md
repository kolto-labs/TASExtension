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
- A page can be stored as an XOR against an earlier page, with chains at most 32 long (`store.max_delta_chain`).
- The codec is zlib level 1 by default. zstd needs Python 3.14 or newer. `none` also works.
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

In the game every tick is captured: the heap in the fixed arena, the exe's writable data and the shim's own game-side state, plus a journal of the exe's writable data digest, engine fields and rand call counts. The M1 exit score (3,823 ticks) makes a 2.4 GB bundle.

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

A restore verifies the whole sim set against T's root. Some state outside the sim set is built by what the process ran: its first tick after boot, and the load screens of an area load. A process that restores at the boot gate, or into an area it never loaded, passes the root check and then diverges on the next ticks. The canary catches that. So with `seek.restore_warmup = "loads"` (the default for the shim) the process first plays, checked like replay, through its first tick and the tick after the last load-screen tick at or before T. The receipt's verifier shows it:

```
$ telos seek m1.holo 2400 --mode restore
  result            pass
  mode              restore
  write_pages       2271
  canary            pass
  fallback          -
  verifier          shim; origin replay@286 > restore@2400; replayed 286 ticks
```

That took 50 s: 286 ticks of replay across the Load Game path, a 2,271-page restore and a 120-tick canary. Replaying to 2400 takes about 5 minutes.

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
dll_sha256 = "31e7ec71ec5afefd"   # the file must hash to this, and so must the build the game loaded
session_dir = "~/kotor/session"    # ring and restore files; a restore set can be 300 MB
```

Every launch is a cold start. The stage's writable paths (`files.stage_paths`) are copied from `game.pin`, the prefix's registry files from `game.prefix_pin`, and `shim.dll` is copied in as `dinput8.dll`. The shim's hello names the build it runs (a sha256 prefix of the DLL), and telos stops when that is not `shim.dll_sha256`. The prefix's wineserver is stopped before the launch and again when the game ends; nothing is stopped without an explicit `game.wineprefix`. The game gets only the pinned environment. Inside a container where Wine lives on the host, telos starts it through `host-spawn` (`game.host`).

```sh
telos run m1_exit.score --out m1.holo                      # every tick captured, pages and files
telos seek m1.holo 2400 --mode restore                     # boot, restore tick 2400, check it
telos run m1_exit.score --from m1.holo@1200 --mode restore  # restore 1200, record the rest as a branch
telos diff m1.holo m1@1200.holo                             # the branch against the original
telos verify m1.holo --fresh                               # judge replay from boot in a new process
```

Measured on the M1 exit score (3,823 ticks: menus, Load Game, walking) with shim build 31e7ec71:

- The run took 9 minutes and made a 2.4 GB bundle. Its journal matched the M2 driver's tick for tick.
- `seek --mode restore` took 14 s to tick 150, 47 s to 1200 and 50 s to 2400. Each passed the root check and a 120-tick canary. `seek 2450 --mode hybrid` restored keyframe 2400 and replayed 50 ticks (91 s, an earlier build).
- The branch from tick 1200 matched the original on all 2,622 ticks after it: journal, state root and files tree.
- `verify --fresh` compared 3,823 of 3,823 ticks in 5 minutes. Its last state root equals the recording's.
- Save length: the Save Teleport score with header `save_ms=1000` moved the player 16.540 m from tick 493 to 615, and 14.197 m with `save_ms=0`. Two runs at 1000 were equal on every tick's journal, state root and files tree.

All four modes run on PySim. In the game replay, restore and hybrid run; semantic stays lab only. Replay starts from boot for now, because no backend can start a run from a save file yet. A score can still load a save the way a player does, through the Load Game menu.

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
