# Roadmap

> **Historical snapshot, not the active execution plan.** This page still contains stale 2026-10-04 milestone and completion claims. The single current KPTAS queue, priority order, direct-user source crosswalk and live acceptance state are maintained in [`canonical-outstanding-execution-plan.md`](/home/brunner56/kptas-routine/planning-inputs/execution/canonical-outstanding-execution-plan.md). Do not use the status or estimates below to decide what is currently accepted or next. Preserve this document as historical context; do not duplicate the active queue here.

KPTASInterface (InputInterfaceTelos) is a TAS toolkit for KotOR I and II. The goal: every tick of the game is a state you can save, load, rewind, branch and replay, and a score played twice gives the exact same game, on any machine.

Status legend: **done** (built and checked), **next** (being built now), **planned** (designed, not started), **research** (still needs an answer).

Estimates are working days of focused work. They are guesses made from what the design says is left, not promises.

## Where things stand

| Area | Status | Proof |
|---|---|---|
| Engine research: what one tick is, which clocks the game reads, where time and input come in | done | Each fact and its address in [contract §2](KPTASInterface/docs/design/contract.md#2-engine-facts-relied-on), from static byte and decompile checks; the few addresses not yet re-verified are listed there |
| Design contract: score format, holorecord bundle, byte formats, wire verbs, tiers, all four seek modes, TOML | done | `KPTASInterface/docs/design/contract.md` |
| Score language: parser, `fmt`, compiler, keymap from the game's own `keymap.2da` | done | Example scores compile, `fmt` is idempotent |
| M0 probes under Wine | done | A `dinput8.dll` proxy runs before the game's startup code. A fixed 1 GB write-watched arena reports exactly the written pages. An own stack switch keeps SEH working (14 ns per switch). |
| M1 replay in the real game (KotOR I, GOG 1.03, under Wine) | next | Two fresh boots of one score wrote byte-identical per-tick journals for 3,823 ticks: menus, mouse clicks, Load Game, walking. One machine, and a newer shim build is being re-verified. The game journal holds a digest of the exe's writable data, engine fields and rand call counts, not the full heap (M2). |
| Portable boundary census (GPU identity, CPU math, host facts, resolution) | research | 105 x87 math sites inventoried from the Ghidra listing. The other machine-dependent inputs found so far are in [contract §7](KPTASInterface/docs/design/contract.md#7-portable-boundary-d12); hidden ones are the M2 risk. |
| Python core: holorecord bundle, seek planner (all four modes), verify, tiers, backends, `telos` CLI | done | On PySim, seeks to ticks 20, 41 and 70 reach the recorded state root in every mode; `verify --fresh` compares every tick; two recordings of the same score give byte-identical journals |
| Reference math | done | Within 1 ulp of correctly rounded; on one host, i386 under Wine and native x86_64 give byte-identical golden output |
| Browser prototype | done | https://kptas-demo.bocloud.workers.dev |
| Browser game presentation | next | The ordinary player UI describes the game and controls, not its implementation stack. Keep technical provenance and diagnostics available in their appropriate places, and describe WebGL only where it is the active graphics path. |

## Milestones

### M1: exact replay (first identical pair in; re-verify and showcase next)

The shim loads as `dinput8.dll`, stops the game at the top of each WinMain pass, and feeds it a virtual clock and virtual DirectInput. Loads are made deterministic by letting the loader threads finish their job at fixed points.

Done when: the same score played twice gives an identical per-tick journal.

That bar has been met once, by the 3,823-tick pair in the table above. Left before M1 is called done: the same check on the newer shim build, then a public showcase run.

### Python core (done)

- holorecord bundle: journal, page store, keyframe packs, index, hash chain
- seek planner for replay, restore, hybrid and semantic, with a finite fallback chain
- branch, diff and bisect to the first tick where two runs split
- tier enforcement, environment pin, verify receipts
- backends: PySim (pure Python), the shim client, KotorPhasor, hands-off
- the `telos` CLI and REPL
- a Gymnasium-style `step()` API

### Portable profile (reference math done; hooking it into the shim is planned, ~1 day)

One fixed virtual machine for every player:
- the same GPU identity
- pinned CPU feature bits
- reference math for sin, cos and friends
- pinned RAM, disk, locale and display values
- a fixed window size

### M2: every tick captured (planned, ~3-4 days)

- The game's heap and allocations move into the fixed arena.
- Game threads run in slots with a serial scheduler.
- Handles, thread ids, TLS, sync objects and timers become virtual.
- Every tick's dirty pages are hashed in the shim and stored by the Python side.

Experiments first:
- two cold boots
- address-space map
- pointer census
- host-input census
- thread and time census

### M3: true per-tick restore (planned, ~2-3 days)

Jump straight to any recorded tick and keep playing. Then check it: a restored tick plus N more ticks must match the original run tick for tick. Also covered here: restoring into a fresh process, and runs that start from a save.

### M4: audio and GL (planned, ~2-3 days)

- A Miles shim so voice-over waits run on virtual time.
- Bink on virtual time.
- GL names made virtual so a restored tick draws the same picture.

### M5: HUD and real input (planned, ~1-2 days)

- An in-game overlay: tick counter, input display, tier, and a TAS watermark (with an Aurebesh easter egg).
- An OS-level input path for the hands-off tier.

### After that

- KotOR II support (address tables for the Aspyr builds). ~2-4 days.
- Native Windows parity. Same shim, second platform run. ~1-2 days.
- Tests, written last from the checks run most often during the build. ~1-2 days.
- First showcase runs: the swoop race and the fighter encounter, since both play out the same every time. All Quests Unrestricted after that.

## Odds

- **Replay (M1):** the first identical pair is in. What's left is repeating it on the newer shim build.
- **True restore (M2-M3):** the hard part. The biggest unknowns are:
  - hidden time or host inputs;
  - game code keeping pointers into Wine's memory;
  - whether the serial scheduler changes load behaviour.

  Each has a named experiment that retires it before anything is claimed.
- **Full toolkit with HUD, KotOR II and Windows:** about two to three more weeks of work.

## Ground rules

Same rules as the [README](README.md#rules). One more for this file: nothing is called done until it has been measured.
