# Use the in-game workspace

Stasis opens a holorecord at a stopped game tick. Play, pause, step,
rewind and branch all act on that game. The workspace uses the shim backend.
Hyperstim is optional and does not provide this restore path.

```sh
telos --config telos.toml control recording.holo --start-mark before_save --overlay
```

Use `--tick 420` instead of `--start-mark` to choose a numbered tick. With neither,
it opens tick 0. Opening runs strict restore checks; it does not advance a hidden
tick to obtain an image. The absolute tick stays visible. A chosen bookmark also
shows your offset from it.

## Control the game

| Action | Shortcut | Behavior |
|---|---|---|
| Show workspace | Ctrl+Alt+F6 | Expand the score, piano roll and inspection tabs |
| Play or pause | Ctrl+Alt+F7 | Toggle playback |
| Step | Ctrl+Alt+F8 | Apply exactly one tick of input, then stay paused |
| Rewind | Ctrl+Alt+F9 | Strictly restore the previous recorded tick |
| Rewind to mark | Ctrl+Alt+Shift+F9 | Restore the preceding score bookmark |
| Branch | Ctrl+Alt+F10 | Create a paused child recording at this tick |
| Exit | Ctrl+Alt+F11 | Close the controller and finish its recording |

Play uses the score runner for waits, expected process outcomes and relaunches.
A wait can advance several ticks; its score timeout still applies. Step refuses
those transitions because they are not a single input tick. A queued pause runs
after the current wait or relaunch finishes. `@assert` is currently preserved and
reported as unevaluated; it does not prove the expression.

The keyboard and mouse panel shows the game's virtual device state sampled at
the accepted tick. Saved input notes are available in the piano roll. An armed
close-on-save request is separate from held keys. During work, the workspace
holds its last accepted image and labels until it can publish the new gate.

## Save, open and branch

Open selects a holorecord folder. Load score changes the editor without applying
those inputs to the current game. Save score exports the editor text. Branch
creates a child from the current tick and the edited score, with no automatic
step. Choose Play or Step when ready. A branch needs its parent files for the
shared history; keep them together.

The Save button checkpoints an active child recording. Otherwise it saves the
score through a file picker. The save notice reports the last committed tick.
A later displayed tick does not mean that tick has already reached disk.

The optional `--branch-score edited.score --branch-out child.holo` arguments
prepare a branch without a picker. Output paths must be new. The editor and
piano roll are distinct: edit text in Score; double-click a piano-roll row to
seek to that tick.

## Restore checks

Successful workspace restore requires a pass receipt, actual restore mode,
no fallback, full-root verification and a passing continuation canary. A failed
check stops playback. A checkpoint image, if needed, is labeled as such; it is
not proof that the native framebuffer was restored. Restore remains tied to a
compatible pinned executable, shim and game environment.

The current K1 Wine fixture has passed strict loaded-game restore, actual-screen
pixel comparison, poison refusal and recovery, and save/reopen lifecycle checks.
That is fixture acceptance, not a claim that every game state, machine, mod or
K2 build restores correctly. Rewind can still take tens of seconds.

## Attach automation to the running workspace

The controller prints a loopback URL and bearer token. `--control-port 0` chooses
a free port; the service always binds to `127.0.0.1`. Use that endpoint instead
of launching another controller against the same game.

Send JSON to `POST /commands` with `Authorization: Bearer TOKEN`. The reply
contains an id. Read `GET /commands/ID` for queued, running, done or failed state.
`GET /status` returns the controller's cached status without probing the game.

```json
{"action":"seek","tick":420}
{"action":"step"}
{"action":"branch","out":"/new/child.holo","score":"/inputs/retry.score"}
{"action":"save"}
```

Other actions are `play_pause`, `rewind`, `rewind_mark`, `workspace`, `stop`,
`load_bundle` with `path`, `load_score` with `path`, and `save` with a score-export
`path`. Path-based commands avoid file pickers. Load score needs the workspace;
headless branching accepts a score path directly. HTTP threads only queue work;
the same controller thread executes both UI and API actions.

There are at most 32 pending commands. A full queue returns 429. Receipts are
kept for the latest 128 completed commands and end with the process. A client
timeout does not cancel a queued action: check its id before resubmitting.

## Record a continuous take

`--video take.mp4` records the controller view in wall-clock order, with frame
and event ledgers beside it. It has no audio capture. For proof of the real
visible workspace, also capture its X11 display as one uninterrupted take.
A whole-file transcode is fine; cuts, splices, speed changes and added input
highlights would no longer show the original continuous interaction.
