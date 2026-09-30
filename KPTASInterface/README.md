# KPTASInterface

The TAS toolkit in KPHarness. Codename InputInterfaceTelos. The import package is `kptas`, the command line is `telos`, and the design lives in [docs/design/contract.md](docs/design/contract.md). Where the code and the contract disagree, the contract says the code is wrong.

Python 3.11 or newer. The Python side is standard library only.

## Layout

```
KPTASInterface/
  kptas/
    score/        parser, fmt, compiler, key names
    holo/         holorecord bundles: journal, page store, keyframes, index, branches, seek, verify
    tier/         environment pin, computed tier, judge replay
    backend/      pysim, shim and handsoff backends, the staging ring
    telos/        the telos command line and REPL
    config.py     telos.toml loader
    types.py      the contract's byte layouts
    supervisor.py launches a backend, drives a score, survives relaunches
    gym.py        a Gymnasium-style reset()/step() over any backend
  lib/            k1-keymap.score: KotOR I key bindings as $aliases
  examples/       pysim_walk.score (runs on PySim), taris_amg.score (written for the game)
  native/
    probes/       M0 probes: fixed arena, stack switch, dinput8 load order
    portable/     reference math and the exe's x87 math site inventory
    shim/         the dinput8.dll shim for M1 (in progress)
    tools/        the M1 driver
  docs/           score.md, holorecord.md, tiers.md, glossary.md, design/contract.md
```

## Running it

From the repo root, `telos` is `python3 -m kptas` with `PYTHONPATH=KPTASInterface`:

```sh
PYTHONPATH=KPTASInterface python3 -m kptas --help
```

`pyproject.toml` also declares a `telos` script, so the commands below are written as `telos`.

## Commands

| Command | What it does |
|---|---|
| `telos score check FILE...` | compile scores and report errors |
| `telos score fmt FILE... [--check \| --write]` | print scores in canonical form; `--check` exits 1 if a file would change |
| `telos run SCORE [--out DIR] [--backend B] [--max-ticks N] [--table]` | play a score and record a holorecord bundle |
| `telos play SCORE\|BUNDLE` | play without recording |
| `telos show BUNDLE [--from T] [--to T] [--limit N] [--legend]` | header and tick table |
| `telos diff A B [--first] [--limit N] [--no-roots]` | ticks where two runs differ |
| `telos bisect A B` | first tick where two runs split |
| `telos seek BUNDLE TICK [--mode M]` | bring a live backend to a recorded tick and print the receipt |
| `telos branch BUNDLE TICK [--out DIR]` | start a new bundle from a recorded tick |
| `telos verify BUNDLE [--fresh] [--compare every_tick\|syncs\|end]` | judge replay: replay the embedded score and compare |
| `telos tier show BUNDLE` / `telos tier explain BUNDLE` | declared and effective tier, and what limits it |
| `telos env pin [--out FILE]` / `telos env check PIN\|BUNDLE` | hash the environment into a pin, or compare against one |
| `telos caps` | launch or attach a backend and print what it can do |
| `telos edit SCORE\|BUNDLE` | open a score (or a bundle's score) in `$EDITOR`, then check it |
| `telos gym serve` | serve reset/step over loopback NDJSON |
| `telos repl` | interactive authoring and inspection |

Exit codes: 0 ok, 1 a check failed or a run halted, 2 usage, configuration or a missing module.

Every command in the table except `edit`, `gym serve` and `repl` has been run on PySim. The pages that go deeper: [score.md](docs/score.md), [holorecord.md](docs/holorecord.md), [tiers.md](docs/tiers.md).

## Config

One file, `telos.toml`, read from the current directory or from `--config`. Every key is optional. With no file at all, a run is lab tier at 60 Hz on PySim.

```toml
[run]
tier = "tas"

[game]
dir = "~/games/kotor"    # the folder holding swkotor.exe; unset means PySim
```

The full list of keys and defaults is in [contract §11](docs/design/contract.md). The loader merges your tables over those defaults, fills in the values that depend on the tier, then checks the rules between keys. It stops on anything it does not know:

```
telos: telos.toml: clock.hzz: unknown key in [clock]
telos: telos.toml: clock.hz: expected integer, got string 'fast'
telos: telos.toml: clock.mode: fixed is not allowed in tier tas; tas requires the virtual clock
```

Values left as `auto` depend on the tier:

| Key | lab | tas | hands-off |
|---|---|---|---|
| `seek.mode` | restore | replay | replay |
| `clock.mode` | virtual | virtual | wall |
| `input.path` | dinput | dinput | os |
| `portable.profile` | `baseline-1` | `baseline-1` | native |
| `run.backend` | shim if `game.dir` is set, else pysim | same as lab | handsoff |

A score header's `tier` and `hz` win over `run.tier` and `clock.hz`.

`seek.mode` also takes `checkpoint` and `checkpoint_replay` (both mean replay), and `exact`, `exact_tick` and `tick_restore` (all mean restore).

## Backends

| Backend | What it is | Tier ceiling | Status |
|---|---|---|---|
| `pysim` | A deterministic stand-in game in pure Python. It emits the same records and page keys as the shim. | tas | works; every example in these docs ran on it |
| `shim` | The real game, driven through the `dinput8.dll` shim | tas, or lab if a module outside the allowlist is loaded | M1, in progress |
| `simgame` | SimGame.exe under Wine, running the real shim core | tas | planned; not in the tree yet |
| `handsoff` | OS-level input through xdotool, frames through ffmpeg x11grab, no code in the game process | hands-off | code exists; the hands-off tier in the game is M5, planned |

Receipts from `pysim` and `simgame` carry their own determinism key, so they never count toward publishing a game run.

## How the shim loads (in progress)

`swkotor.exe` imports `dinput8.dll`. A proxy `dinput8.dll` next to the exe, with `WINEDLLOVERRIDES=dinput8=n,b` under Wine, is loaded and initialized before the exe's C runtime and `WinMain`. M0 confirmed that order on Wine 11 staging with K1 GOG 1.03.

`DllMain` runs under the loader lock, so it keeps to a short list:

1. Read the clock origin from `KPTAS_CLOCK`.
2. Check the exe's `.text` hash against the K1 GOG 1.03 table.
3. Reserve its own memory.
4. Patch the exe's import table, so the virtual clock is live before the runtime's first time call.
5. Write a detour at the exe's entry point.

It starts no threads, no Winsock, and calls no `LoadLibrary`. If the hash does not match, the shim goes passive: `DirectInput8Create` is forwarded to the system `dinput8.dll` and nothing else is touched.

The entry detour runs next, outside the loader lock. It connects to Python on loopback (`KPTAS_PORT`, `KPTAS_SESSION`), answers `hello`, and holds until `start`. From then on the gate at the top of each `WinMain` pass owns input, the clock and capture.

The source is in `native/shim/src/`. `native/shim/build.sh` builds freestanding C with `zig cc` for `x86-windows-gnu`, with no C runtime. It calls `$Z cc`, so set `Z` to your zig command. There is no install step yet, because M1 is not done.

## Native pieces that are done

- **M0 probes** (`native/probes/`). A fixed 1 GB write-watched arena at `0x10000000` reports exactly the pages written. A stack switch keeps SEH working and costs 14 ns. The proxy's `DllMain` runs before the runtime and `WinMain`. Results are in [contract §12](docs/design/contract.md).
- **Reference math** (`native/portable/`). `refmath.c` gives sin, cos, tan, atan2, exp, log, pow and the rest, built only from operations SSE2 performs exactly, so the same input bits give the same output bits on every x86 CPU. `math_sites.toml` lists the 105 x87 transcendental sites in the K1 GOG 1.03 exe: 99 in game code, 6 in the runtime. `golden/check.py` compares the output of an i386 build under Wine with an x86_64 Linux build; they must match byte for byte.
