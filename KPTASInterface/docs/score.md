# Score format

A score is a text file. Each line is a note: how many ticks to hold, then every input held during them.

```
#!score v1 game=k1 build=auto tier=lab hz=60 start=boot
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
ok KPTASInterface/examples/pysim_walk.score: 13 items, 82 note ticks, 1 files, sha256 5951ac5a9d915346  [game=k1 build=auto tier=lab hz=60 start=boot]
```

Tick 0 is the first gate after the game starts, before any input. The first note starts at tick 1, so `12  a` covers ticks 37 to 48.

## Header

The first line is `#!score v1`, then `key=value` pairs.

| Key | Values | If left out |
|---|---|---|
| `game` | `k1`, `k2` | `k1` |
| `build` | `auto`, or a build id | `auto` (from the exe's sha256) |
| `tier` | `lab`, `tas`, `hands-off` | `run.tier` in `telos.toml`, then `lab` |
| `hz` | a whole number | `clock.hz`, then 60 |
| `start` | `boot`, or `save:<sha256>` | `boot` |
| `render` | free text, for example `800x600/low` | unset |
| `keymap` | free text | unset |

Other lowercase keys are kept as written. The header's `tier` and `hz` win over `telos.toml`.

`start=save:...` is recorded in the bundle header, but no backend can reload a save yet, so every run starts from boot for now. `score check` accepts any value after `save:`; `run` wants the full 64-digit sha256.

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
| `!altf4`, `!focus`, `!blur` | an OS event, first tick only |
| `"text"` | types the text on a US layout, each character for COUNT ticks; it must be the only input |
| `$name` | an alias from `@alias` |

A count of 0 is allowed. Its one-shot inputs land on the first tick of the next note.

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
| `@assert EXPR` | parsed and kept, but not evaluated yet. The runner logs a line and moves on. |
| `@repeat N` ... `@end` | repeats the lines between them |

`@sync` events: `main_menu`, `area.ready`, `load.done`, `save.begin`, `save.done`, `dialog.open`, `dialog.close`, `gui.open PANEL`, `proc.down`, `proc.up`.

`#` starts a comment, except inside quotes.

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

[examples/taris_amg.score](../examples/taris_amg.score) is written for KotOR I: a save teleport on Taris, then an AMG. It compiles. It has not run anywhere, since exact replay in the game is still M1. Its own comments say the menu positions are placeholders, and its `start=save:9f3c41d2` is short, so `run` would refuse it as written. It shows the shape a real route takes: `@sync` on engine events, `!altf4` during `save.begin`, `@expect exit|alive` and `@relaunch if=exit`.
