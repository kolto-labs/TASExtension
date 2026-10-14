# Tiers

Every run has a tier: lab, tas or hands-off. The stricter the tier, the less the tool may touch. hands-off is the strictest.

A run made with this tool is a TAS whatever its tier. hands-off is still a tool playing the game, and it is never an RTA submission.

Commands here are written as `telos`. From the repo root that is `PYTHONPATH=KPTASInterface python3 -m kptas`.

## What each tier allows

| | lab | tas | hands-off |
|---|---|---|---|
| Code in the game process | the shim plus anything else | the shim only, with an allowlisted module set | none; Wine's own dinput8 |
| Clock | virtual (default), fixed, wall | virtual | wall |
| Seek | restore (default), hybrid, replay, semantic | replay (default); restore and hybrid while authoring | replay |
| Input | DirectInput or OS | DirectInput | OS |
| Memory writes | any | only recorded bytes, through restore | none |
| What gets published | the holorecord, marked lab | the score, after a clean judge replay | the score and a capture, best effort |

The default tier is tas, and it is read-only: it sends inputs, runs the virtual clock, captures state, and restores only states the game was already in. The shim holds it to that. It refuses a restore to any root it did not capture itself and was not handed from the bundle being sought, and a restore that fails to verify stops the session until one verifies. Its default seek is checkpoint plus replay. lab is for work that writes memory, and you have to ask for it.

## How the tier is computed

The tier in the header is only a claim. The effective tier is the lowest of five things:

1. the declared tier (score header, then `run.tier`)
2. the backend's ceiling: tas for pysim, simgame and shim, hands-off for handsoff, lab for phasor
3. the module set: lab if anything outside `shim.module_allowlist` is loaded in the game
4. the verbclass of every tick
5. the last judge receipt: a failed judge caps the run at lab

Every tick records a verbclass, a bitset of what touched the game on that tick. It sets a ceiling for that tick:

- Any lab-only bit (memory write, semantic write-back, fixed clock, file write after tick 0) makes it lab.
- Otherwise, any in-process bit (input, gate, clock, capture, read, restore, files restore) makes it tas.
- Otherwise it stays hands-off.

It only ratchets down. A tick can lower the tier, and only a new passing judge receipt can lift a cap. The `SEAL` chunk records the computed tier when the run closes.

## Ask the bundle

```
$ telos run KPTASInterface/examples/pysim_walk.score --out t.holo
t.holo: ticks 0..82, 1 epoch(s), stopped at end
$ telos tier explain t.holo
input       ceiling    detail
declared    tas        HOLO header (score header first, then run.tier)
backend     tas        pysim
modules     -          all allowlisted
verbclass   tas        83 ticks; tas from tick 0 (GATE|CLOCK)
judge       -          no judge receipt for this key; run telos verify --fresh
seal        tas        SEAL computed_tier over 83 ticks
effective   tas        set by declared, backend, verbclass, seal
check                  header tas is at or below the computed tas
publishable no         no passing judge receipt
```

[pysim_walk.score](../examples/pysim_walk.score) has no `tier` in its header, so the declared tier is `run.tier`, and that defaults to tas. After `telos verify t.holo --fresh` passes, the judge row reads `last judge pass in t@judge.holo` and the last line becomes `publishable yes`.

That "yes" is PySim's. PySim and SimGame receipts carry their own determinism key, so they never count toward publishing a game run.

## Publishable

A run is publishable when all three hold:

- its effective tier is tas or hands-off
- no tick carries RESTORE or FILES_RESTORE
- a judge receipt passes

Restore is allowed in tas while you author a route. A run that used it is not the one you publish. You publish the score, and the judge replays it from the start.

## Locking the tier

The config enforces the tier's rules before anything launches:

```
telos: telos.toml: clock.mode: fixed is not allowed in tier tas; tas requires the virtual clock
```

In the game, the shim adds its own lock. This is M1 work, in progress. The tier is locked before the game starts, and from then on the shim refuses any verb outside that tier's list with the error `refused`. A tas recording will also refuse to start unless the virtual clock is live in every hooked module, the time hooks were live before the game's startup `srand(GetTickCount())`, the module set is allowlisted, and the portable profile is served.

## Judge replay

`telos verify BUNDLE --fresh`:

1. Start a new process in the pinned environment.
2. Lock the tier and set the clock, then place the start save by file only.
3. Replay the score stored in the bundle.
4. Compare `journal_sha256` on every tick, or only at `@sync` points or at the end (`--compare syncs|end`). Raw state roots and separate files roots are compared when both runs declare the supported raw-root qualification and share a determinism key. Every selected tick must have captured, nonnull memory and files roots; missing coverage fails.
5. Write the receipt on a new branch, `<name>@judge.holo`.

## hands-off

hands-off has no shim and no wire. Input goes through xdotool and frames through ffmpeg x11grab. Its check is:

- the module list shows no shim or proxy
- dinput8 is Wine's own
- the exe hash matches
- input came from xdotool only

That can only ever be `best_effort`: frame hashes at `@sync` points, plus a person watching the video. In the game, hands-off is M5, planned.

## Where the tier shows

- in the score header, `tier=`
- in the bundle's `HOLO` header, which `telos show` prints
- in the `SEAL` chunk, as the computed tier
- on screen, as the HUD watermark. That is M5, planned. Above lab it cannot be turned off.

## The environment pin

`telos env pin` hashes what a run depends on (binaries, data files, modules, clock origin, portable profile, threading mode) into a determinism key. `telos env check PIN|BUNDLE` compares the current machine with it:

```
$ telos env check a.holo
key matches: pinned 0f463b08fea9da37 now 0f463b08fea9da37
```

Runs with different keys never share roots. Behind a portable profile, host details such as the Wine version, the prefix and the real GL driver are recorded but stay out of the key, so a Wine run and a Windows run can share one.

For the game, the pin describes the stage as a launch leaves it: the configured shim as `dinput8.dll`, and the pinned `swkotor.ini` with its `Disable Sound` line set from `audio.device`. The stage itself may still hold the last run's DLL, or an ini the game rewrote when it exited. The audio device and `game.env` join the key too.

### Raw-root qualification

The default `verify.mask_registry = ""` leaves cross-launch roots unqualified: a fresh judge can pass at JOURNAL level. To request strict unmasked comparison, set it to a JSON file containing:

```json
{"format":"kptas-root-qualification/1","root_kind":"full_root","page_map":"kptas-page-map/1","masks":[]}
```

The same JSON can be supplied inline as a TOML string. Valid declarations are canonicalized into the environment key. Unknown fields or versions, duplicate fields, arbitrary prose, nonempty masks and `sim_root` are rejected. This declaration requests a comparison; it does not establish determinism.

Record a new reference with the declaration, then run `telos verify reference.holo --fresh`. FULL_ROOT requires actual raw memory-root and files-root comparisons at every selected point. The receipt reports `compare every_tick|syncs|end` and separate actual counts for roots and files; an end-only pass certifies only the selected end point. Files remain a separate state tree, not part of the memory root. Qualified environment mismatches refuse even with `env.guard = "warn"`.

`diff` and `bisect` enforce the same declaration and coverage rules. `--no-roots` explicitly selects journal comparison. Existing recordings and receipts retain their original qualification and evidence level; adding a declaration to a later config does not upgrade them. PySim publishes immutable file trees at gates and restores the complete overlay, including deletions, before page restoration.

### Restoring stage files

The host validates the complete files tree and every referenced payload before changing a writable stage path. Paths must be canonical relative paths inside `files.stage_paths`; snapshots include directory rows (including empty directories). Duplicate paths, case aliases, file/directory conflicts, missing tracked parent directories, nonzero stage timestamps, invalid hashes/sizes, symlinks and hardlinked files refuse before mutation. Virtual backend paths such as `SAVES:/...` remain virtual and cannot name host stage destinations. Configured roots must not overlap or end in `/`; ancestors of a nested configured root must already exist.

Stage reads and restore operations use directory descriptors and no-follow opens at every component. They never follow a discovered directory symlink or truncate a symlink/hardlink target. Restore still requires an exclusively controlled, quiescent stage: the game is held at its gate or its server is stopped, and other processes must not rename stage directories or mutate its files concurrently. Descriptor traversal blocks symlink redirection, but this is not a transactional filesystem against arbitrary concurrent directory moves or I/O failures. Malformed trees and payloads are fully rejected before writes/deletions; valid restores preserve unchanged files and rewrite changed files in place so existing game handles retain their file identity. Origin bootstrap, direct files-put and pin reset use the same validation boundary.

### Native restore completion

Native restore has two phases. `restore` first verifies captured memory and handles; its response is `memory_verified:true`, `verified:false`, `external_pending:true`, `poisoned:true`. The host returns an intermediate seek receipt with `verify_level="none"`. It does not certify complete relevant state. Step, wait and digest remain blocked until GL reconstruction and the final native root audit commit.

`gl.apply` commits only with explicit `ok:true`, `verified:true`, `external_pending:false`, `poisoned:false`, complete nonnegative counters and zero missing/verification failures. A missing, failed or incomplete apply leaves the session blocked; recovery requires a verified restore/reconstruction or relaunch. The ordinary public seek publishes FULL_ROOT only after completion, and a failed restore/hybrid seek has level NONE. PySim's complete in-process restore retains its existing FULL_ROOT behavior. NONE cannot be used by judge/verify receipts or promote a run's verification status. Raw clients are subject to the native transaction gate; the host guard is additional protection.
