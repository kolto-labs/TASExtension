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

The default tier is tas, and it is read-only: it sends inputs, runs the virtual clock, captures state, and restores only states the game was already in. Its default seek is checkpoint plus replay. lab is for work that writes memory, and you have to ask for it.

## How the tier is computed

The tier in the header is only a claim. The effective tier is the lowest of five things:

1. the declared tier (score header, then `run.tier`)
2. the backend's ceiling: tas for pysim, simgame and shim, hands-off for handsoff
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
$ telos run walk_tas.score --out t.holo
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

`walk_tas.score` is [pysim_walk.score](../examples/pysim_walk.score) with `tier=lab` changed to `tier=tas`. After `telos verify t.holo --fresh` passes, the judge row reads `last judge pass in t@judge.holo` and the last line becomes `publishable yes`.

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
4. Compare `journal_sha256` on every tick, or only at `@sync` points or at the end (`--compare syncs|end`). State roots are compared only when both runs captured, share a determinism key and have a qualified mask registry.
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
