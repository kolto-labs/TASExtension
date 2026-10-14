# Scripted RPG tests and engine state from TOML

`input.path = "semantic"` selects typed engine requests instead of keyboard or mouse input. Use it for scripted RPG tests and structured, nonvisual inspection of actors and their actions. The current provider is the optional `phasor` backend: it attaches to a running game with Hyperstim's loopback harness enabled and a captured NWScript VM. The standalone shim remains the default; selecting semantic actor commands with it fails clearly because it has no engine-action bridge yet.

The existing tier system classifies this mode as `lab`; that is an explicit permission boundary for engine commands, not a requirement to use a TAS workflow. It uses the engine's action queue, but issuing commands directly is different from reproducing vanilla player input. It has no deterministic frame gate, state capture or restore guarantee. Schedule times are wall-clock intervals at the configured Hz, not verified engine frames.

From KPTASInterface, with a compatible game already running and the harness port matching the example:

```sh
python -m kptas -c examples/semantic_move.toml run examples/semantic_wait.score --out semantic-move.holo
```

The example observes the player, requests a run toward `end_door05`, and observes distance afterward. Choose a tag that exists in the loaded area. It does not launch the game, load an area, dismiss a pause or unpause timers. A missing VM, invalid object or failed engine call stops the request rather than falling back to mouse input or position writes. If a transport or observation failure leaves the outcome uncertain, further requests on that backend are blocked; create a fresh session and inspect live state before deciding what to do. An uncertain request is never retried automatically. Every score note must be empty (`.`); keyboard and mouse notes are rejected.

Each `[[engine.commands]]` table has an integer `at` interval, an `action`, and optional `actor`. Actor `"pc"` means GetFirstPC; another value selects the first matching object tag. Action actors must be creatures. Commands must be ordered by `at`; commands sharing an interval execute in their listed order, once each. The bounded command schema accepts plain object tags, not Lua snippets.

| Action | Fields | Effect |
| --- | --- | --- |
| `observe` | Optional `target` tag | Read position and current action; with a target, read distance. |
| `move_to_object` | `target`; optional `run`, `range`, `clear` | Queue ActionMoveToObject. Defaults: run true, range 1.5, clear false. |
| `move_to` | `x`, `y`, `z`; optional `run`, `clear` | Construct an engine Location and queue ActionMoveToLocation. Defaults: run true, clear false. |
| `attack` | `target`; optional `clear` | Queue ActionAttack with no combat feat. Clear defaults false. |
| `clear_actions` | None | Ask the engine to clear the actor's action queue. |

For example, an attack request is:

```toml
[[engine.commands]]
at = 300
action = "attack"
actor = "pc"
target = "enemy_tag"
clear = true
```

Requests use `nwscript.with_caller` to scope the actor identity and the existing typed NWScript bridge to marshal arguments. Movement does not use SetPosition, coordinate-memory writes or the multiplayer teleport workaround. The engine decides pathfinding, collision, movement and combat outcomes. Avoid repeatedly scheduling the same attack: reissuing it can restart the action/round.

Before enabling commands, the adapter checks the harness's reported game identity against `run.game`. A missing identity or a K1/K2 mismatch refuses attachment with writes disabled. Request receipts retain the reported game and provider build. This check prevents commands from going to the wrong selected title; it does not establish that a particular action works in that title.

`engine.request` events retain the request and the actual returned engine observation. `request_accepted=true` means the call was accepted; `completed=false` deliberately makes no arrival or combat-success claim. Verify movement using a later observation's position and distance, plus the current action. An empty queue alone can mean arrival or an interrupted/failed route. Verify combat through subsequent live state, target health and game events. Pauses and obstacles can prevent either request from completing.

Python clients can call `backend.semantic("read", [])` for the live harness state, or pass a list of top-level field names. Unknown fields are refused. `backend.engine_action(...)` accepts the same typed command schema after attach, lab-tier lock and start. Arbitrary semantic field writes are unsupported.

The returned dictionaries provide a text/data representation of live RPG state without reading the screen. Tests can assert positions, distances, action IDs and other live fields rather than compare images.

## Inspect a recorded request

The ordinary `show` command displays the tick table. To print the retained engine observations as JSON Lines, without connecting to a game:

```sh
PYTHONPATH=. python examples/semantic_observations.py semantic-move.holo
```

Each row keeps the request, actor ID, observed position, current action and any target distance. `bundle_tick` is the recording's global scheduling interval; `tick` is the provider's local interval. Neither is a verified engine frame. Use later observations to test arrival. Do not treat `request_accepted` as a completed movement.

## Control an NPC

`examples/semantic_npc.toml` shows the same interface with an NPC actor. It uses Trask and an Endar door as replaceable tags. Both objects must exist in the loaded area. The recipe does not load a save or advance the story, and has not itself been live-tested.

```sh
python -m kptas -c examples/semantic_npc.toml run examples/semantic_wait.score --out semantic-npc.holo
PYTHONPATH=. python examples/semantic_observations.py semantic-npc.holo
```

A separate K1 live test used `move_to` for the player and an existing NPC. Independent state reads confirmed that each reached a destination one game unit away. Those tests used lab saves and the public `run` command. They establish those movement cases, not combat, dialogue, quest completion, K2 compatibility or deterministic restore. The tested software-rendering setup used llvmpipe and Xvfb; it was not a separate boot on a machine with no GPU installed.


The command and observation path does not call DirectInput, screenshot tools or OpenGL. The interface does not interpret screenshots or audio and does not synthesize physical input. Normal internal game rendering, audio, simulation and other systems remain intact; removing those systems is outside this interface's scope. Source and transport-double tests establish routing and refusal behavior; real movement/arrival, combat and K1/K2 compatibility require separate game tests.

## Unattended deployment

The semantic interface needs no physical mouse or keyboard and creates no fake input devices. Its requests go to the engine action queue; observations come from engine state. A physical GPU is also not an interface requirement: a compatible software OpenGL implementation such as Mesa llvmpipe can render on the CPU, with an offscreen X display such as Xvfb when the platform needs a display server. Xvfb supplies a display surface here, not simulated keyboard/mouse actions.

Software rendering still implements OpenGL and preserves the game's normal internal systems. It is different from removing GL from the process. Hardware-free deployment therefore depends on a compatible software renderer, offscreen window-system setup and the chosen game/Wine build. Verify the actual renderer in the launched process; do not infer CPU rendering from an installed package or configuration setting. This adapter neither injects input devices nor uses screenshots/audio to decide actions.

## K2 selection and current limits

`run.game = "k2"` defaults to `game.exe = "swkotor2.exe"` and includes `swkotor2.ini` in the default writable stage paths. Explicit executable names and custom stage lists are preserved. The existing environment-pin key named `swkotor.ini` hashes the selected title's INI; it is a logical key retained for archive compatibility.

K2 with the standalone `shim` backend is currently refused during configuration, including when `backend = "auto"` would select it. Its native entrypoint and hook signatures admit K1 only. Selecting another executable does not port those hooks, and a connection timeout is not a useful compatibility result.

The optional semantic provider can attach only when its reported `K2` identity matches the requested title. Its host tests cover that routing and wrong-title refusal with a transport double. **K2 live engine-action compatibility remains unverified**, as do K2 deterministic capture and restore. K1 movement evidence and software-renderer evidence do not close those requirements.


## Check the host adapter

The focused tests validate provider selection, command validation, scheduling, uncertain-result refusal and observation export using transport doubles. They do not launch a game:

```sh
python -m unittest tests.test_semantic_control tests.test_game_selection -v
```


## Keep session failures contained

A successful attachment checks the selected game before enabling semantic use. Engine writes remain disabled until lab tier is locked. Closing the backend ends that session: subsequent actions, reads and starts refuse rather than reconnect to a possibly different game. Create a new backend for a deliberate new connection.

A failed engine request or failed state observation revokes write permission. Later actions and scheduled intervals refuse before advancing their counter. A broken event connection ends observation with `proc.down`; it cannot silently reconnect. Invalid field lists are rejected before requesting state. None of these host checks needs a window, keyboard, mouse or graphics library.

This is an adapter for an already-running game. It does not supply a headless game launcher, remove the engine's graphics/audio dependencies, or prove operation on a computer without GPU/input hardware. The standalone shim still has no semantic action bridge. A compatible game/provider process and separately verified runtime setup are required. Host transport tests establish command validation and failure handling only.
