# InputInterfaceTelos cockpit — UI spec v2

A browser prototype of the InputInterfaceTelos TAS toolkit for KotOR. This
spec sets the panel layout, the piano roll, the memory views and the command
line, at the precision level of the TAS tools it borrows from. It targets the
existing browser stand-in (`demo/public/js/sim.js`, `score.js`, `holo.js`)
and the real grammar in `KPTASInterface/kptas/telos` and
`docs/design/contract.md`.

## What the reference tools do well

**BizHawk TAStudio**, built on FCEUX's TAS Editor, is a piano roll: one row
per frame, one column per input. Frames already played sit in the
**greenzone** — an automatic savestate per frame, so scrubbing back is a
savestate load, not a replay. Frames where the game lagged still count as
greenzone but get their own colour, so a TASer can tell "played and normal"
from "played and lagged" at a glance. **Markers** are named frames, shown
with a widened frame-number glyph in their own column; they exist purely to
navigate and to organize a route. **Branches** are named save slots, each
holding its input, markers and a greenzone slice; hovering one in the
Branches list pops up the screenshot taken at that frame, so a TASer picks a
branch by sight before loading it. Right-click on a column header or a
selection opens a context menu (insert/delete frames, truncate, clear,
force or unset greenzone). A **History** log below Branches records every
edit as a line. Playback has a cursor arrow pinned to the current frame,
"seek to frame" (click a frame number to jump there), and auto-restore of
the previous playback position after a seek completes. Around the roll:
RAM Watch (named addresses, live values), RAM Search (narrow addresses by
comparison across polls), a Hex Editor, a Lua Console, an Input Display
overlay, and status readouts for frame count, lag count and rerecord count.

**libTAS** (Linux) uses the same idea with different mechanics: its input
editor table has a savestate-slot column, a frame column, then one column
per input; binary columns toggle on click, analog columns need a
double-click to type a value, and no frame before the playback head can be
edited. RAM Watch there supports pointer chains (`pointer -> offset ->
pointer -> ...`) so a watch survives a restart. RAM Search filters by memory
region and comparison, same shape as BizHawk's.

**TMInterface** (TrackMania) and **CelesteTAS Studio** replace the piano
roll with a plain-text script: one instruction per line, timestamped. Both
tools watch the script file and reload it the moment it changes on disk, so
editing and re-running never need a manual step. Celeste's Studio is a
purpose-built text editor shipped with the mod; its defining trick, well
known in that community, is a live cursor — the line that corresponds to the
frame currently playing is tracked and highlighted as playback advances, so
the text and the game stay pointed at the same instant. That live-cursor
idea, not any specific keybinding, is what this spec borrows for score-to-
roll sync.

Sources: [tasvideos.org/Bizhawk](https://tasvideos.org/Bizhawk) ·
[FCEUX TAS Editor manual, Piano Roll](https://fceux.com/web/help/taseditor/PianoRoll.html)
· [FCEUX TAS Editor manual, Glossary](https://fceux.com/web/help/taseditor/Glossary.html)
· [libTAS docs](https://clementgallet.github.io/libTAS/help/) ·
[libTAS menu options](https://tasvideos.org/EmulatorResources/LibTAS/MenuOptions)
· [TMInterface: your first script](https://donadigo.com/tminterface/first-script)
· [CelesteTAS-EverestInterop](https://github.com/EverestAPI/CelesteTAS-EverestInterop)

## Panels and default layout

Six panels: **Header**, **Score** (text), **Roll** (piano roll), **Branches**,
**Memory** (watch + hex), **Console**. The header always shows: score name,
tier badge (lab / tas / hands-off), tick, epoch, hz, seek mode, and the
watermark state (`kolto` dot = on; the watermark cannot be turned off above
lab tier).

At **1440x900** all six are visible at once, roll centered and largest:

```
+--------------------------------------------------------------------------+
| InputInterfaceTelos   tier: tas   tick 4128   ep 2   60hz   seek: replay |
+---------------+--------------------------------------------+-------------+
| SCORE         | ROLL                                       | BRANCHES    |
| 1  #!score v1 | tick  mk  fl  w a s d q e | spc tab esc |1-9| o main@0   |
| 2  @segment.. |  4123     c    . . . . . .|  .   .   .  | . |   [thumb]  |
| 3  1  w       |  4124     c    W . . . . .|  .   .   .  | . | o amg@640  |
| 4  24 w lmb   |  4125  *  cr   W . . . . .|  .   .   .  | . |   [thumb]  |
| 5  @sync sa.. |  4126     c    W . S . . .|  .   .   .  | . |            |
| 6  16 .       | >4127     c    . . . . . .|  .   .   .  | . | + branch   |
| 7  @sync sa.. |  4128     .    . . . . . .|  .   .   .  | . +-------------+
| 8  @mark aft..|  4129     .    . . . . . .|  .   .   .  | . | MEMORY      |
|               |  ...  mouseX mouseY  lmb rmb              | | watch  hex |
|               |                                            | | pc.hp  24  |
+---------------+--------------------------------------------+ | pc.x  1660 |
| CONSOLE  telos> seek 4127 --mode hybrid                     | | rng   9f3c |
| tick 4127  ep 2  verbs IGCP  flags cr  root 8a11c2..         | +-------------+
+--------------------------------------------------------------------------+
```

At **1280x720**, Score and Memory become tabs behind Branches/Roll rather
than fixed columns: a tab strip sits above the roll (`Roll | Score`) and
above Branches (`Branches | Memory`), each remembering its last tab.
Console stays pinned at the bottom, one line tall until focused, then three.
The header drops `epoch` and `seek mode` into an overflow menu (`⋯`).

## Piano roll

**Columns**, left to right, matching the K1 keymap
(`KPTASInterface/lib/k1-keymap.score`): tick, mark, flags, then input
clusters — `w a s d` move, `q e` select prev/next (menu prev/next in GUIs),
`space` pause, `tab` change leader, `esc` game menu, `1`–`9` action slots
and dialogue replies, `lmb` `rmb`, and two analog columns `mouse x` /
`mouse y`. Column headers show the bound action on hover, read from the
keymap (`w — Move Forward`). **Only `w a s d lmb rmb` are wired in this
prototype's sim** (`sim.js`'s `KEYS`/`BIT`); the rest render and paint but
stay inert until the sim is extended — hovering an inert column says so.

**Rows** are ticks. A tick's background encodes greenzone state, taken
straight from `TickFlag` in `contract.md` §8.4:

- **Unplayed** (no `CAPTURED`): flat `void`, no gradient — nothing to show.
- **Greenzone** (`CAPTURED`): tinted `kolto`, brightness inverse to seek
  cost — the tick count back to the nearest restore point `K ≤ T`. A tick
  right after a restore point is brightest; brightness fades toward `dim`
  as that distance grows, since that is exactly what a hybrid seek has to
  replay.
- **Restore point** (`RESTORE_POINT`): a solid `bacta` left-edge bar, on top
  of the greenzone tint, so restore points read as a distinct rail down the
  roll, not just the brightest greenzone ticks.
- **Keyframe / materialized** (`KEYFRAME` / `MATERIALIZED`): a small filled
  diamond in the flags column, `kolto`.
- **Wait / hold** (`WAIT` from `@sync`, `HOLD` from load subticks): `amber`
  background instead of `kolto` — this prototype's stand-in for lag frames,
  since KotOR has no fixed frame budget to miss. A diagonal hatch marks
  `HOLD` (a load) versus solid `amber` for a plain `@sync` wait.
  Non-restore-point reasons (GL mapped, file I/O, scheduler wait, foreign
  module) show in the row's tooltip, not as separate colours.
- **First divergence / desync**: `magenta`, the palette's only use of it.
- **Epoch boundary**: a full-width `ink` rule across the roll with the
  epoch number, since a restore never crosses an `EPOC` (see the AMG
  example score's `@relaunch`). Restores cannot drag a selection across it.
- **Segment band**: `@segment name hz=` changes show as a thin label chip
  in the tick column, since tick length changes at the boundary.

The **playback cursor** is a `phosphor` arrow glyph pinned to the current
tick, matching BizHawk's; it moves as ticks are stepped or replayed, and a
seek jumps it directly rather than animating through every tick in between.
**Seek to tick**: click a tick number to seek there under the active mode.
**Auto-restore**: after a seek or a verify pass, the roll keeps the
previous scroll position and selection (clamped to the new head) instead of
snapping to tick 0 or the new tick.

**Marks** (`@mark`) show as an `amber` diamond in the mark column with the
name on hover — FCEUX's yellow marker, recoloured. **Drag-painting**: click
and drag across cells in one column to toggle a run of ticks the same way;
drag across columns and rows to paint a rectangle. One drag stroke is one
text edit (see below), so one undo step. **Context menu** (right-click a
selection): insert note, delete, split at cursor, add mark, branch here,
seek here (mode submenu, disabled entries per tier), copy ticks, verify
from here.

### Score and roll stay in sync

The text score is the source of truth; the roll is a rendering of its
compiled form (`kptas.score.compile`), the same relationship `score.js`
already has to `holo.js`. Three rules:

1. **Roll edit → text edit.** Painting a cell may split a run-length note
   into up to three lines (before / changed / after), written back into
   the score text at the right line. Toggling a whole note leaves it one
   line.
2. **Text edit → roll rebuild.** On every compile, the roll diffs the new
   program against the old one (`forkTickFor` in `holo.js`) and truncates
   greenzone from the first changed tick forward — recorded ticks before
   that point are kept, not rehashed.
3. **Live cursor.** The current tick's `rec.src` — its index into the
   compiled instruction list — maps back to that instruction's source
   line, which is highlighted in the Score panel as playback runs. This is
   Celeste Studio's live-cursor trick, driven by data the sim already
   carries.

Editing a cell **inside an `@repeat` block** has no per-iteration override
in the score grammar, so the editor unrolls the block into literal notes
and asks to confirm before writing. `@sync` wait rows are shown but are not
directly editable — change the event or timeout in the text. A `$alias`
(for example `$qsave`) shows its resolved key in the roll cell and keeps
the alias name in the text.

## Memory: watch and hex

Two tabs over the same per-tick state. **Watch**: a named list of
expressions read from the tick's semantic entry or sim state (`pc.hp`,
`ents[0].x`, `rng`, `vt`), each showing its current value and, in a thin
sparkline, its last 64 ticks — modelled on RAM Watch, but keyed by JSON
path in this prototype rather than a raw address. Right-click a cell in the
roll's mouse or note columns to "watch this"; double-click a watch row to
edit its expression. **Hex**: a byte view of the canonical state string
(`gameString` in `sim.js`), for the feel of a hex editor; recently changed
bytes highlight for one tick, same as BizHawk's Hex Editor. Against a real
backend this tab shows the tick's actual page refs — vaddr, memory class
(`game_arena` / `image_data` / `harness_game`), protection — per
`contract.md` §8.4, one row per page instead of raw bytes.

## Command line

Not a Lua console: a line-oriented command line that speaks the real
`telos` grammar (`kptas/telos/repl.py`, `cli.py`), so what works here works
in the real REPL. Two kinds of line:

- **Score lines**, which run immediately and are appended to the score:
  `4 w lmb` (a note), `.` (no input), `@sync area.ready timeout=1800`,
  `@mark after_teleport`, `@segment amg hz=25`, `@repeat 3` / `@end`.
- **Commands**: `seek <tick> [--mode replay|restore|hybrid|semantic]`,
  `branch <tick> [--out path]`, `diff <a> <b> [--first]`,
  `verify [--fresh] [--compare every_tick|syncs|end]`,
  `show [--from N] [--to N] [--legend]`, `tier show`, `tier explain`,
  `status`, `save <path>`, `help`. `undo` drops the last authored line from
  the *session* buffer — a different undo from the Score panel's editor
  undo (Ctrl+Z), which only touches unsent text.
- `step [n]` is cockpit sugar for `n .` — advance n ticks holding nothing —
  since the real grammar has no bare "step" verb.

Output is the same fixed-width row `cli.py`'s `tv.report_line` prints:
tick, epoch, verb letters (`IGCP...`), restore-point flag, outcome, journal
and root prefixes. A seek mode disabled by the current tier (§4.1 —
hands-off allows replay only) appears struck through in the `--mode`
autocomplete with the reason on hover, not silently rejected after Enter.

## Hotkeys

Live key presses are never game input — KotOR input only ever comes from
score text or roll clicks. That frees the keyboard for the tool itself.

| Key | Scope | Action |
|---|---|---|
| Ctrl+S | global | Save score |
| Ctrl+Z | Score panel | Undo last text edit |
| Ctrl+B | Roll | Branch at the selected tick |
| Ctrl+G | global | Go to tick (opens a seek prompt) |
| Ctrl+` | global | Focus the console |
| ←  / → | Roll | Move selection one tick |
| Shift+← / → | Roll | Move selection 10 ticks |
| Home / End | Roll | Jump to tick 0 / the current head |
| Enter | Roll | Seek the playhead to the selection |
| N | Roll | Add a mark at the selection |
| Delete | Roll | Clear the selected cells |
| Ctrl+F | Roll | Find a mark by name |
| ↑ / ↓ | Console | Step through command history |
| Enter | Console | Run the line |
| Esc | Console, Roll | Clear the line / clear the selection |

Tab, Space, Esc, F5, F11, F12 and Ctrl+W/T/N/L stay with the browser; the
input columns that share their names are never live keyboard shortcuts.

## Palette

Only these nine, with alpha tints of each allowed for gradients:
`void #05070a`, `panel #0b1016`, `kolto #19e3c1`, `bacta #5ad8ff`,
`phosphor #7dff9a`, `amber #ffb000`, `magenta #ff4fd8`, `ink #d7e3ea`,
`dim #7f93a0`. Magenta is reserved for the first divergent tick and for a
desync report — nothing else uses it, so it always means "look here."
