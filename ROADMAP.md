# Roadmap

KPTASInterface (InputInterfaceTelos) is a TAS toolkit for KotOR 1 and 2. The goal is simple to say and hard to do: every tick of the game is a state you can save, load, rewind, branch and replay, and a score played twice gives the exact same game, on any machine.

Status legend: **done** (built and checked), **next** (being built now), **planned** (designed, not started), **research** (still needs an answer).

Estimates are working days of focused work. They are guesses made from what the design says is left, not promises.

## Where things stand

| Area | Status | Proof |
|---|---|---|
| Engine research: what one tick is, which clocks the game reads, where time and input come in | done | Two independent checks per claim against the recovered code and the exe bytes |
| Design contract: score format, holorecord bundle, byte formats, wire verbs, tiers, all four seek modes, TOML | done | `KPTASInterface/docs/design/contract.md` |
| Score language: parser, `fmt`, compiler, keymap from the game's own `keymap.2da` | done | Example scores compile, `fmt` is idempotent |
| M0 probes under Wine | done | A `dinput8.dll` proxy runs before the game's startup code. A fixed 1 GB write-watched arena reports exactly the written pages. An own stack switch keeps SEH working (14 ns per switch). |
| Portable boundary census (GPU identity, CPU math, host facts, resolution) | next | Running |

## Milestones

### M1: exact replay (next, ~2-3 days)

The shim loads as `dinput8.dll`, stops the game at the top of each WinMain pass, and feeds it a virtual clock and virtual DirectInput. Loads are made deterministic by letting the loader threads finish their job at fixed points.

Done when: the same score played twice gives an identical per-tick journal.

### Python core (next, ~1 day, in parallel with M1)

- holorecord bundle: journal, page store, keyframe packs, index, hash chain
- seek planner for replay, restore, hybrid and semantic, with a finite fallback chain
- branch, diff and bisect to the first tick where two runs split
- tier enforcement, environment pin, verify receipts
- backends: PySim (pure Python), the shim client, KotorPhasor, hands-off
- the `telos` CLI and REPL
- a Gymnasium-style `step()` API

### Portable profile (planned, ~1 day)

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

- K2 support (address tables for the Aspyr builds). ~2-4 days.
- Native Windows parity. Same shim, second platform run. ~1-2 days.
- Tests, written last from the checks run most often during the build. ~1-2 days.
- First showcase runs: the swoop race and the fighter encounter, since both play out the same every time. All Quests Unrestricted after that.

## Honest odds

- **Replay (M1):** very likely this week. Every piece it needs was checked against the exe.
- **True restore (M2-M3):** the hard part. The biggest unknowns are:
  - hidden time or host inputs;
  - game code keeping pointers into Wine's memory;
  - whether the serial scheduler changes load behaviour.

  Each has a named experiment that retires it before anything is claimed.
- **Full toolkit with HUD, K2 and Windows:** about two to three weeks.

## Ground rules

- The game is never "fixed". Glitches stay glitches.
- BioWare's own save and load are untouched.
- Nothing is called done until it has been measured.
