# Stasis

The TAS toolkit in Kolto Tank. Stasis is the product. Its project directory is `KPTASInterface` and its package is `inputinterfacetelos`. It imports as `kptas`, the command line is `telos`, and the design lives in [docs/design/contract.md](docs/design/contract.md). Where the code and the contract disagree, the contract says the code is wrong.

Stasis was formerly KPTAS / InputInterfaceTelos; Hyperstim was formerly KotorPhasor (its DLL is still kphasor.dll and scripts still load from kotorphasor/scripts); Kolto Tank was formerly KPHarness.

Python 3.11 or newer. The core uses the standard library. The in-game workspace
also needs PySide6; video capture needs FFmpeg. Native play needs the matching
shim, supported game build and pinned runtime environment.

Maintained by bodencrouch / kolto-labs, in OpenSWKotOR. Hyperstim is optional.
No Discord invite or public release download is claimed by this source snapshot.

The current K1 Wine fixture passes strict native restore and save/reopen checks.
Its [continuous AMG practice clip](docs/showcase-amg.md) uses the actual display.
This does not complete M3 integration, K2, clean speedrun routes or release
qualification. Read [release status](docs/release-status.md) before distributing
or treating a local test result as support for another setup.

## Layout

```
KPTASInterface/
  kptas/
    score/        parser, fmt, compiler, key names
    holo/         holorecord bundles: journal, page store, keyframes, index, branches, seek, verify
    tier/         environment pin, computed tier, judge replay
    backend/      pysim, shim, handsoff and phasor backends, the staging ring
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
    shim/         the K1 dinput8.dll shim
    tools/        native drivers and validation tools
  docs/           score.md, holorecord.md, tiers.md, glossary.md, design/contract.md
```

## Running it

From the repo root, `telos` is `python3 -m kptas` with `PYTHONPATH=KPTASInterface`:

```sh
PYTHONPATH=KPTASInterface python3 -m kptas --help
```

The standalone package installs a `telos` command with `python -m pip install .`.
See [installation](docs/install.md) for the optional overlay and native runtime.
A local Python wheel is available; this is not yet a qualified public release.

## Commands

| Command | What it does |
|---|---|
| `telos score check FILE...` | compile scores and report errors |
| `telos score fmt FILE... [--check \| --write]` | print scores in canonical form; `--check` exits 1 if a file would change |
| `telos run SCORE [--out DIR] [--backend B] [--max-ticks N] [--table]` | play a score and record a holorecord bundle |
| `telos play SCORE\|BUNDLE` | play without recording |
| `telos control BUNDLE --overlay` | open the native workspace at a stopped tick; see [controls](docs/controls.md) |
| `telos show BUNDLE [--from T] [--to T] [--limit N] [--legend]` | header and tick table |
| `telos diff A B [--first] [--limit N] [--no-roots]` | ticks where two runs differ |
| `telos bisect A B` | first tick where two runs split |
| `telos seek BUNDLE TICK [--mode M]` | bring a live backend to a recorded tick and print the receipt |
| `telos branch BUNDLE TICK [--out DIR]` | start a new bundle from a recorded tick |
| `telos verify BUNDLE [--fresh] [--compare every_tick\|syncs\|end]` | judge replay: replay the embedded score and compare |
| `telos frames info BUNDLE\|DIR` | count the frames `capture.frames` stored and the ticks they cover |
| `telos frames video BUNDLE\|DIR [--out FILE] [--from T] [--to T]` | encode the frames as H.264 with each frame's tick drawn on it |
| `telos tier show BUNDLE` / `telos tier explain BUNDLE` | declared and effective tier, and what limits it |
| `telos env pin [--out FILE]` / `telos env check PIN\|BUNDLE` | hash the environment into a pin, or compare against one |
| `telos caps` | launch or attach a backend and print what it can do |
| `telos edit SCORE\|BUNDLE` | open a score (or a bundle's score) in `$EDITOR`, then check it |
| `telos gym serve` | serve reset/step over loopback NDJSON |
| `telos repl` | interactive authoring and inspection |

Exit codes: 0 ok, 1 a check failed or a run halted, 2 usage, configuration or a missing module.

Every command in the table except `edit`, `gym serve`, `repl` and `frames` has been run on PySim. `frames` needs the shim: PySim draws nothing. The pages that go deeper: [score.md](docs/score.md), [holorecord.md](docs/holorecord.md), [tiers.md](docs/tiers.md).

## Config

One file, `telos.toml`, read from the current directory or from `--config`. Every key is optional. With no file at all, a run is tas tier at 60 Hz on PySim. tas is read-only, and its default seek is checkpoint plus replay. lab, which can write memory, has to be asked for:

```toml
[run]
tier = "lab"

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
| `audio.device` | virtual | virtual | real |

`auto` never picks phasor. With `run.backend = "phasor"`, `seek.mode` resolves to replay, `clock.mode` to wall and `audio.device` to real.

A score header's `tier` and `hz` win over `run.tier` and `clock.hz`.

`seek.mode` also takes `checkpoint` and `checkpoint_replay` (both mean replay), and `exact`, `exact_tick` and `tick_restore` (all mean restore).

## Backends

| Backend | What it is | Tier ceiling | Status |
|---|---|---|---|
| `pysim` | A deterministic stand-in game in pure Python. It emits the same records and page keys as the shim. | tas | works; every example in these docs ran on it |
| `shim` | The real game, driven through the `dinput8.dll` shim | tas, or lab if a module outside the allowlist is loaded | K1 fixtures have per-tick capture and strict restore evidence under Wine; broader platform qualification remains open |
| `simgame` | SimGame.exe under Wine, running the real shim core | tas | planned; not in the tree yet |
| `handsoff` | OS-level input through xdotool, frames through ffmpeg x11grab, no code in the game process | hands-off | OS input and frame capture exist; exact native restore is not provided by this backend |
| `phasor` | Optional. Attaches to Hyperstim and reads state/events; semantic mode can issue typed engine actions. It has no deterministic tick gate or restore. | lab | See [semantic controls](docs/semantic-control.md) for commands, evidence and K2 limits |

Receipts from `pysim` and `simgame` carry their own determinism key, so they never count toward publishing a game run.

`phasor` needs `run.tier = "lab"` and a port: `[phasor] port`, or `KOTORPHASOR_HARNESS_PORT` when that is 0. A tick there is 1/hz of wall time. Every note must hold nothing (`.`), and `@sync` matches the event name only. Each event frame lands in the bundle as an `event:` MARK.

## Driving a recording live

`telos control` opens a recorded bundle in the game itself: play, pause,
step a tick, rewind a tick, or branch into an edited score, all off
hotkeys over a stopped gate. See [docs/controls.md](docs/controls.md).

[docs/showcase-amg.md](docs/showcase-amg.md) walks through the first
thing it got pointed at: the Anywhere Menu Glitch. Failed once with a
plain Alt+F4, then got rewound and branched into the real thing.

## How the shim loads

`swkotor.exe` imports `dinput8.dll`. A proxy `dinput8.dll` next to the exe, with `WINEDLLOVERRIDES=dinput8=n,b` under Wine, is loaded and initialized before the exe's C runtime and `WinMain`. M0 confirmed that order on Wine 11 staging with K1 GOG 1.03.

`DllMain` runs under the loader lock, so it keeps to a short list:

1. Check the exe's `.text` hash against the K1 GOG 1.03 table.
2. Read the math mode from `KPTAS_PORTABLE` and remove it from the environment.
3. Reserve its own memory: `harness_core`, the arena and harness_game.
4. Build the virtual machine the game sees (`KPTAS_BOUNDARY`): command line, install path, environment, CPU, locale, time zone and GL identity.
5. Read the clock origin from `KPTAS_CLOCK` and the audio device from `KPTAS_AUDIO`.
6. Patch the exe's import table, so the virtual clock is live before the runtime's first time call.
7. Write a detour at the exe's entry point and the engine detours, then, under reference math, the math sites.

It starts no threads, no Winsock, and calls no `LoadLibrary`. If the hash does not match, the shim goes passive: `DirectInput8Create` is forwarded to the system `dinput8.dll` and nothing else is touched.

The entry detour runs next, outside the loader lock. It connects to Python on loopback (`KPTAS_PORT`, `KPTAS_SESSION`), answers `hello`, and holds until `start`. From then on the gate at the top of each `WinMain` pass owns input, the clock and capture.

The source is in `native/shim/src/`. `native/shim/build.sh` builds freestanding C with `zig cc` for `x86-windows-gnu`, with no C runtime. It calls `$Z cc`, so set `Z` to your zig command. There is no install step: `shim.dll` in `telos.toml` names the build, and every launch copies it into the game directory as `dinput8.dll`.

## Native pieces that are done

- **M0 probes** (`native/probes/`). A fixed 1 GB write-watched arena at `0x10000000` reports exactly the pages written. A stack switch keeps SEH working and costs 14 ns. The proxy's `DllMain` runs before the runtime and `WinMain`. Results are in [contract §12](docs/design/contract.md).
- **Reference math** (`native/portable/`). `refmath.c` gives sin, cos, tan, atan2, exp, log, pow and the rest, built only from operations SSE2 performs exactly, so the output should not depend on the CPU. `golden/check.py` is the check: an i386 build under Wine and an x86_64 Linux build must give the same bytes. So far they do, on one host. `math_sites.toml` lists the 105 x87 transcendental sites in the K1 GOG 1.03 exe: 99 in game code, 6 in the runtime. Under `portable.math = "reference"`, the default, the shim points every one of them at `refmath.c`. `tools/gen_math_patches.py` writes its table and proves each patch safe from a listing of the exe (contract §7, item 3). On one host, the M1 exit score runs 24 of the game sites and all three runtime entries, and its journal is the same under both modes on each of its first 1000 ticks. The state roots differ from tick 0: native math takes the runtime's SSE2 pow, which sets an MXCSR flag that a saved frame keeps.
