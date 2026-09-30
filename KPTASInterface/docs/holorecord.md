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

In the game, M1 records a journal per tick only: a digest of the exe's writable data, engine fields and rand call counts. Capturing pages every tick, the full heap included, is M2.

## Seeking

`telos seek BUNDLE TICK` brings a live backend to a recorded tick and checks it. There are four ways to get there.

| Mode | How | Tiers | Checked by |
|---|---|---|---|
| replay | Start from the boot, replay the score up to T | all | journal (or `sim_root` when qualified) |
| restore | Write W, the pages where the live map and map(T) differ, then check | lab, tas | full root, journal, and a continuation canary |
| hybrid | Restore the latest restore point at or before T, then replay to T | lab, tas | full root |
| semantic | Write back the journalled engine fields; the branch is marked tainted | lab | diff only |

The default is restore in lab and replay in tas and hands-off. `seek.mode` in `telos.toml` picks another one, and also takes `checkpoint` for replay and `exact` or `tick_restore` for restore.

A mode that cannot apply to T is swapped per `seek.on_incompatible` (hybrid by default). A mode whose check fails moves down `seek.fallback_chain` (hybrid, then replay), each level once. If the last one fails, the seek halts and reports the first field that differs. It never loops.

Tick 15 is inside the PySim load, so a restore there falls back:

```
$ telos seek a.holo 15 --mode restore
telos: seek 15: restore is not possible: tick 15 is not a restore point (other)
  result            pass
  mode              hybrid
  write_pages       3
  fallback          restore (tick 15 is not a restore point (other)) > hybrid
  verifier          pysim; origin restore@10; replayed 5 ticks
```

(Receipt trimmed.) A plain restore to tick 40 wrote 27 pages, checked the full root and passed its canary.

All four modes run on PySim today. In the game, replay is M1, which has its first identical pair; restore and hybrid need M2 and M3, and semantic stays lab only. Replay starts from boot for now, because no backend can start a run from a save file yet. A score can still load a save the way a player does, through the Load Game menu. The M1 pair did.

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
