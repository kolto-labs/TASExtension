# Frame and tick precision

New configurations use simulation-frame precision by default:

```toml
[clock]
precision = "frame"
hz = 60
```

Each input, state capture and restore boundary remains one completed outer
game-loop pass. At 60 Hz that advances virtual game time by approximately
16.667 milliseconds. A frame here is a simulation step, not a guarantee that
the renderer presents an image: loading and suppressed presentation still
need simulation progress and recoverable state.

Finer virtual-time steps remain available:

```toml
[clock]
precision = "tick"
# Defaults to 1000 Hz when hz is omitted.
```

An explicit `clock.hz` always wins. Score header rates, score rate segments
and restored recording rates retain their existing precedence. Selecting
precision never converts the numeric input positions of an existing score.
The effective clock rate is already included in the recording environment
pin; recordings with different effective timing are not interchangeable.
The precision name is a rate-selection preference, not an additional native
clock or controller. Lua remains a separate extension; the shared clock is
intended to belong to TelosCORE.

The rate accepts any integer from 1 through 1000, not just multiples of 30.
60 Hz is the recommended starting point for this game and the showcase.
30 Hz is an optional coarser timing choice; other rates such as 120 or 144
are configurable but are not qualified merely by accepting the setting.
Changing the rate changes the virtual time advanced by each input position
and can change gameplay outcomes. Choose it before authoring a run; preserve
the recorded rate when loading or restoring. Capture-video FPS is separate
from this simulation clock rate.

The previous default was already 60 Hz with this safe outer-loop boundary.
Making frame precision explicit does not by itself accelerate existing
60 Hz recordings, resolve restore failures, or guarantee 60 FPS video.
GetTickCount's millisecond units do not imply 1000 captures per second.
Tick precision likewise does not claim access to engine-internal substeps.

## What precision switches, and what it does not

The gate is the same in both modes: one stop per outer game-loop pass, at the
WinMain loop head with UpdateScreen depth 0. There are no gate stops inside
UpdateScreen or a server pass, and no separate 1000 Hz code path; the rate
only enters `V(n) = V(s) + (n - s) * 1e6 / hz`. Every native hook is
installed in both modes, because each one either keeps the gate correct
(UpdateScreen depth), keeps the game live (the budget guard's in-step clock
advance), or is already part of every recorded state and journal (the
ServerMainLoop and UpdateScreen counters journalled as `sp` and `st`).

Precision does select these defaults; a value written in the file wins:

| key | frame | tick |
| --- | --- | --- |
| `clock.hz` (when omitted) | 60 | 1000 |
| `journal.per_server_pass` | false | true |
| `journal.per_subtick` | false | true |

The two journal keys describe detail inside one step. The native journal
does not read them yet, so neither value changes journal bytes, state roots
or the determinism key.

Settings named `every_tick` (`verify.compare`, `store.pages`,
`capture.frames`) mean every gate step, which is every frame in frame
precision. They keep their protocol names.

User-facing wording follows the precision through `kptas.config.unit_name`:
"frame" unless `clock.precision = "tick"`. The shim learns the precision from
the `clock` verb and uses it only for the HUD label and refusal text. The
environment (`KPTAS_CLOCK`) is unchanged, because its length moves Wine heap
pointers that recorded state contains.
