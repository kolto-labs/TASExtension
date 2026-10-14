# Score format

A score is a text file. Each line is a note: how many ticks to hold, then every input held during them.

```
#!score v1 game=k1 build=auto hz=60 start=boot
# Short PySim run: walk, run, turn and pause, with no game install needed.
@segment walk
10  w
20  w lmb rmb
6   .
12  a
@mark turn
8   d lmb
4   .
@segment slow hz=30
10  w
5   s
1   esc
6   .
```

That is [examples/pysim_walk.score](../examples/pysim_walk.score). Check it (`telos` here is `PYTHONPATH=KPTASInterface python3 -m kptas`, run from the repo root):

```
$ telos score check KPTASInterface/examples/pysim_walk.score
ok KPTASInterface/examples/pysim_walk.score: 13 items, 82 note ticks, 1 files, sha256 ae7bf76c6d464927  [game=k1 build=auto hz=60 start=boot]
```

Tick 0 is the first gate after the game starts, before any input. The first note starts at tick 1, so `12  a` covers ticks 37 to 48.

## Header

The first line is `#!score v1`, then `key=value` pairs.

| Key | Values | If left out |
|---|---|---|
| `game` | `k1`, `k2` | `k1` |
| `build` | `auto`, or a build id | `auto` (from the exe's sha256) |
| `tier` | `lab`, `tas`, `hands-off` | `run.tier` in `telos.toml`, then `tas` |
| `hz` | a whole number | `clock.hz`, then 60 |
| `start` | `boot`, or `save:<sha256>` | `boot` |
| `render` | free text, for example `800x600/low` | unset |
| `keymap` | free text | unset |
| `save_ms` | a whole number, milliseconds | `clock.save_ms`, then a documented backend estimate (pysim and the shim: 900, an unmeasured guess for a quicksave stall; see `DEFAULT_SAVE_MS` in `kptas/backend/pysim.py`) |

Other lowercase keys are kept as written. The header's `tier` and `hz` win over `telos.toml`.

`save_ms` is how much virtual time (the clock every `timeGetTime`/`QueryPerformanceCounter`/`GetSystemTimeAsFileTime` read sees) one in-game save advances by, while zero score ticks elapse -- the engine's own MainLoop is stalled for the save, so nothing in the score counts it. In real play the first frame after a save sees a large wall-clock delta; runners use that (including the "save teleport" trick). Under a virtual clock a save is otherwise free, so `save_ms` must be set explicitly to keep that behavior. `save_ms=0` is allowed, but only as an explicit choice, never a silent default.

`start=save:...` is recorded in the bundle header, but no backend can reload a save yet, so every run starts from boot for now. `score check` accepts any value after `save:`; `run` wants the full 64-digit sha256.

## Saves

Every in-game save gets `save_ms`: the quicksave, a save from the save menu, and the autosave. The game writes saves in two places. StallEventSaveGame (0x4b3110) writes the player's saves and the autosave a script asks for: SaveGame (0x4b58a0) takes the screenshot and arms it, and the next server pass writes it. DoPCAutosave (0x4b8300) writes the autosave the game makes on entering an area whose `autosaveonenter` in modulesave.2da is `force`, whatever `AutoSave` says in swkotor.ini; StartNewModule calls it before the area loads, and it never goes through SaveGame. Either way the clock jumps once per save, when the write returns. In the journal, `save.begin` and `save.done` say who asked for the save, `"kind":"auto"` or `"kind":"manual"`, and its slot: 0 is the quicksave and 1 the autosave.

### The save teleport's ceiling

A longer save gives the player a bigger jump, up to one second's worth. The player's own movement (the keys, or both mouse buttons) is integrated on the client by CSWPlayerControlCamRelative::Control (0x679940), and the first thing it does is clamp the frame time: a frame longer than 1.0 s (the float at 0x73d764) is integrated as exactly 1.0 s (0x3f800000, stored at 0x679960). The frame after a save lasts the save length plus one tick, so at 60 Hz the clamp takes over above `save_ms` 983.33.

Measured on the Dantooine courtyard, with a quicksave 20 ticks into a run with both mouse buttons. With `save_ms=983` the player's first moving tick after the save covers 3.728 m. With 984, 1500 and 3000 it covers 3.734 m, and the next tick 3.356 m in all three. The jump needs the run held through the save: with both buttons released on the F4 note the player does not move at all.

Above the cap only the rest of the world still sees the whole save. Creature heartbeats (SpawnInHeartbeatPerception 0x4eb6e0) and other timed random draws count the full gap, so different save lengths can split their draws after the save. The first differing tick depends on the route: on the separate held-run `st_h` route, 1500 and 3000 ms first split at tick 497, the second tick after the save. The player's path can split later. On this route 984 ms splits from 3000 ms at the third moving tick, and 1250 and 1500 ms at the fourth, each a different way, by up to 2.5 m on a single tick. All of them end within 2 cm of each other at the courtyard wall, 21.7 m from where the save began. The splits come at different ticks and go different ways, which points to the world's timers rather than a second cap on the jump. How far the jump carries in open ground is not measured yet: every run so far ran into that wall.

## Notes

`COUNT INPUTS`. Notes are stateless: a note lists everything held, not what changed. `.` means nothing is held.

| Input | Meaning |
|---|---|
| `w`, `f4`, `space`, `lshift`, `num5`, `up` | a key, held every tick of the note |
| `lalt+f4` | a chord, pressed in that order |
| `lmb`, `rmb`, `mmb` | mouse buttons, held |
| `m@X,Y` | cursor held at X,Y |
| `mdX,Y` | cursor moved by X,Y every tick |
| `wh+N`, `wh-N` | wheel, first tick only |
| `!altf4`, `!altf4@save`, `!focus`, `!blur`, `!close` | an OS event, first tick only |
| `"text"` | types the text on a US layout, each character for COUNT ticks; it must be the only input |
| `$name` | an alias from `@alias` |

A count of 0 is allowed. Its one-shot inputs land on the first tick of the next note.

In the game `!close` sends WM_CLOSE to the game window at the start of its tick. K1 answers with its quit box and pauses; it exits only when the score clicks OK. PySim treats `!close` as an immediate exit.

`!altf4` sends what a real Alt+F4 sends, pressed and released at the start of its tick: WM_SYSKEYDOWN VK_MENU, WM_SYSKEYDOWN VK_F4 with Alt down (lParam bit 29), WM_SYSKEYUP VK_F4, then WM_KEYUP VK_MENU. From there the messages travel as they do on Windows. The game's WndProc (WinMessageHandler 0x402800) turns game input off on Alt, back on with F4, sends itself WM_CLOSE, then passes the key to DefWindowProc. DefWindowProc posts WM_SYSCOMMAND SC_CLOSE, which reaches the game in the same message loop pass. The WndProc hands SC_CLOSE back to DefWindowProc, and DefWindowProc sends a second WM_CLOSE. So a real Alt+F4 calls QueryExitProgram (0x5eda00) twice. The quit box opens once and the game pauses, as after `!close`: the second call finds the box already listed (CSWGuiManager::AddPanel 0x40bc70 returns early) and only plays the panel sound again. One OK click exits with code 0, on the same tick as after `!close`. PySim, as with `!close`, exits at once.

`!altf4` covers only the window messages. A person's Alt and F4 also reach DirectInput, and F4 is the quicksave key, so press them as a chord in the same note to play the real thing: `1 lalt+f4 !altf4`. The window messages arrive first, so the quit box is already open when the game reads DirectInput, and F4 does not quicksave. The journal of the chord differs from `!altf4` alone only in the input hash of that tick and the next.

`!altf4@save` presses the same Alt+F4 while the next save is being written, which is how the Anywhere Menu Glitch starts. A plain `!altf4` cannot do that: it arrives at the start of its tick, before anything in that tick saves, and a `@sync save.begin` returns at the gate after the save is already done. On Windows a key pressed during a save reaches the game through the save screen's own message pump. Both save writers, StallEventSaveGame (0x4b3110) and DoPCAutosave (0x4b8300), call UpdateScreen (0x401c10), first at 0x4b325a and 0x4b83c6. UpdateScreen always runs the pump at 0x401510: PeekMessageA, GetMessageA, TranslateMessage, DispatchMessageA. The shim normally answers that pump with "no message". `!altf4@save` arms the four key messages from its tick on. The first time a save runs that pump, the keys are queued and the pump is served them, then the WM_SYSCOMMAND SC_CLOSE that DefWindowProc posts, in the same pump loop. The quit box opens while the save screen is up. The save's length and the clock do not change. The arm waits for the next save, however many ticks away, so it can ride on the quicksave note itself: `1 f4 !altf4@save`. The journal shows `altf4` between that save's `save.begin` and `save.done`. Handsoff refuses it, and PySim exits as for `!altf4`.

Seeks and `verify` take the exit from the recording, not from the note: a run goes on after `!close` or `!altf4` until its epoch ends, and the judge plays the tick in which the game exits and compares the exit status.

An ordinary `telos seek` or `telos run --from recording.holo@T --mode restore` can restore a captured tick in a later or earlier process epoch. The supervisor starts that recorded epoch, installs its files at the held boot gate, and restores T directly. With default warmup, the receipt reports `restore` with zero replay ticks and no fallback. Hybrid restores an anchor in the target epoch and replays only from that anchor. Unavailable origin metadata, corrupt stored state, or an attached process that would need replacement is refused explicitly.

Key names are lowercase. `shift`, `ctrl`, `alt`, `escape` and `return` are accepted and rewritten as `lshift`, `lctrl`, `lalt`, `esc` and `enter`. The full table is in `kptas/score/keys.py`, with each key's Windows VK code and DirectInput scan code.

```
#!score v1 game=k1 hz=60
@include ../lib/k1-keymap.score      # from a score in examples/
@segment menu pace=gate
@sync main_menu timeout=7200
1   m@400,330 lmb                    # click at 400,330
3   md5,-2                           # cursor moves by 5,-2 each tick
1   wh-1                             # one wheel notch
0   !focus                           # rides on the next note's first tick
2   lalt+f4                          # chord, pressed in order
4   $qsave                           # alias from the keymap
2   "Revan"                          # R e v a n, 2 ticks each
```

## Sugar

Three prefixes save typing when one key changes:

- `+k` presses k and keeps it held
- `-k` releases k
- `~k` holds k for this note only

A note is either sugar or plain keys, never both. `telos score fmt` expands sugar into full notes. With `@alias run = lshift` defined:

```
4 w                      ->   4   w
8 +$run                  ->   8   w $run
2 ~e                     ->   2   e w $run
6 -w                     ->   6   $run
```

Sugar needs to know the held set, so the first note after an `@include` must be a full note.

## Directives

| Directive | What it does |
|---|---|
| `@include PATH` | reads another score. It is looked for next to the including file, then in `score.include_dirs` (default `lib`, relative to `telos.toml` or the current directory). Cycles are an error. |
| `@alias NAME = KEY` | names one key or chord. Use it as `$NAME`. Redefining a name to a different key is an error. |
| `@segment NAME [hz=N] [scale=N] [pace=P]` | starts a segment. Left-out options take the header or default value, never the previous segment's. `pace=gate` (the default) runs as fast as the backend allows; `pace=realtime` waits for the wall clock. `scale*1e6/hz` must stay below 2^31. |
| `@sync EVENT [PANEL] [timeout=N]` | waits for an engine event instead of counting ticks. The timeout is in ticks; the default is `score.default_timeout_ticks`, 1800. |
| `@expect OUTCOME [timeout=N]` | `exit`: the game must exit within the timeout. `alive`: it must still be running. `exit\|alive`: either. |
| `@relaunch [if=C]` | starts the game again. `if=exit` does it only when the game has exited; the default is `always`. |
| `@mark NAME` | names the current tick in the bundle. Names are unique. |
| `@save ms=N` | overrides `save_ms` for the single next in-game save only, then reverts to the header value. An unused override survives `@relaunch`; a restore recovers the override pending at that tick. For save-length variance and the save-teleport trick without changing every save in the score. |
| `@assert FIELD OP VALUE [tol=N] [on_fail=stop\|report]` | checks one value from the journal entry of the last frame played. A failure stops the run, or with `on_fail=report` logs and goes on. See [Asserts](#asserts). |
| `@repeat N` ... `@end` | repeats the lines between them |

`@sync` events: `main_menu`, `area.ready`, `load.done`, `save.begin`, `save.done`, `dialog.open`, `dialog.close`, `gui.open PANEL`, `proc.down`, `proc.up`.

`#` starts a comment, except inside quotes.

## Asserts

`@assert` compares one value from the journal entry of the frame just played: the gate the assert sits at. It reads only what the run already recorded, so it adds no input and asks the game nothing. A recording and its judge replay check the same bytes.

```
1   m@398,486 lmb
20  .
@assert pause == 0
@assert pc.x >= 12.5
@assert pc.x == 0x41480000                  # the float's exact bits
@assert pc.y == 3.25 tol=0.01
@assert rand.n == 4 on_fail=report          # log and go on
@assert sem.lfs == 0                        # any journal path
```

`OP` is `==`, `!=`, `<`, `<=`, `>` or `>=`. `VALUE` is a whole number, a decimal number or `0x` hex. On a float field, a `0x` value is compared with the float's raw IEEE-754 bits. `tol=N` goes with `==` and `!=` only, and treats values within N as equal.

| Field | Journal entry | Notes |
|---|---|---|
| `pc.x`, `pc.y`, `pc.z` | `sem.pc.pos` | player position, float |
| `pc.ori.x`, `pc.ori.y`, `pc.ori.z` | `sem.pc.ori` | player orientation, float |
| `pc.area` | `sem.pc.area` | the area's object id, not its name |
| `pc.id` | `sem.pc.id` | the player's object id |
| `pause` | `sem.pause` | |
| `rand.n` | `rand.n` | `rand()` draws on the main thread this frame (PySim: the sum of `rand_draws`) |
| `rand.other` | `rand.o` | draws on other threads this frame |
| `srand.n` | `srand.n` | `srand` calls this frame |
| `holdrand` | `hr` | the CRT seed after the frame |
| `clock_us` | `v` | virtual microseconds |
| `server_passes`, `subticks`, `client_passes` | `sp`, `st`, `cl` | |
| `tick` | | the run's frame number, counted across relaunches |

Any other name is a dotted path into the entry, with list items by number: `sem.lfs`, `sem.mm`, `rand.s.0.1`. The game's journal fields are written in `native/shim/src/journal.cpp`. The module name is not journalled; `sem.cmod` only says whether the client has a module.

A failure names the place, the frame and both values:

```
telos: route.score:14: @assert pc.x >= 12.5 failed at frame 340: pc.x is 11.98 (bits 0x413fae14), expected >= 12.5
```

A field the backend does not journal, or a frame with no journal entry, counts as a failure and follows `on_fail`. PySim journals no `sem`, so `pc.*` and `pause` fail there. `telos play` keeps no journal: it logs each assert as not evaluated and goes on. `telos run`, `telos verify` and the TAS owner evaluate them. The judge replay (`telos verify`) stops at the last recorded frame, so it does not check asserts after the final note. A failing `on_fail=stop` assert ends `telos run` with exit status 1 and leaves the recording unsealed.

## Canonical form

`telos score fmt` expands sugar, lines up the columns and keeps comments. Running it twice changes nothing. `--check` exits 1 if a file would change, and `--write` rewrites in place.

Compiling goes further: includes, repeats, aliases and text are all resolved into one flat list. That compiled text is what a bundle stores, and the first 16 hex digits of its sha256 are what `score check` prints. The example above compiles to:

```
#!score v1 game=k1 build=auto hz=60
@segment menu hz=60 scale=100 pace=gate
@sync main_menu timeout=7200
1   m@400,330 lmb
3   md5,-2
1   wh-1
0   !focus
2   lalt+f4
4   f4
2   lshift+r
2   e
...
```

## Errors

Every error names the file, line and column, and suggests the nearest name when there is one:

```
telos: bad.score:3:3: cannot release 'a': it is not held
telos: bad2.score:2:3: unknown key 'wq' (did you mean 'w'?)
telos: bad3.score:2:7: unknown sync event 'area.redy' (did you mean 'area.ready'?)
```

## The keymap

[lib/k1-keymap.score](../lib/k1-keymap.score) holds the KotOR I bindings as aliases, read from the `keymap.2da` in `patch.erf`:

```
@alias move_forward = w              # ActionUp: Move Forward
@alias pause = space                 # Pause: Pause Game
@alias qsave = f4                    # Quicksave: Quick Save
```

Rebinding keys in the game makes this file wrong. Check it again first.

## A score for the game

[examples/taris_amg.score](../examples/taris_amg.score) is written for KotOR I: a save teleport on Taris, then an AMG. It compiles. It has not run anywhere; M1's first game run used a different score. Its own comments say the menu positions are placeholders, and its `start=save:9f3c41d2` is short, so `run` would refuse it as written. It shows the shape a real route takes: `@sync` on engine events, `!altf4@save` on the note that starts the save, `@expect exit|alive` and `@relaunch if=exit`.
