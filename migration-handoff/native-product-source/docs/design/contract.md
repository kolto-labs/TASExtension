> **Legacy technical milestone contract.** M0–M5 numbering in this specification remains Legacy. The [canonical product plan](../plans/2026-10-02-2022-feat-kptas-complete-product-plan.md) defines the current Product M1–M7 milestones and execution priorities; this contract does not claim their completion.

# Stasis contract, version 1

This contract covers Stasis (formerly InputInterfaceTelos; project directory `KPTASInterface`), a TAS toolkit for Star Wars: KOTOR 1 and 2. It is the single source of truth, and every later module follows it. `kptas/types.py` implements its byte layouts and `kptas/config.py` implements its configuration schema. Both are checked against this document, and where they disagree, the code is wrong.

All integers are little-endian. All addresses are for K1 GOG 1.03 (`swkotor.exe`, 4,042,752 bytes, sha256 `9c10e0450a6eeca4…`) unless marked otherwise.

## 1. Decisions

| ID | Decision | Reason |
|---|---|---|
| D1 | The product is Stasis (formerly InputInterfaceTelos; the recorded `PRODUCT` stays `InputInterfaceTelos`). The project directory is `KPTASInterface`, the import package is `kptas`, the CLI is `telos` and the distribution is `inputinterfacetelos`. | One name per layer, so none collides with Hyperstim or KPM. |
| D2 | A holorecord is a directory bundle `<name>.holo/` (§8). It holds an append-only chunk journal, a content-addressed page store, materialized keyframe packs, a files overlay store and a disposable index. One TICK record identifies one complete state by page references plus a state root. | Framed chunks recover by scanning. Content addressing makes a per-tick state cheap. |
| D3 | There are three tiers: lab < tas < hands-off. hands-off is the strictest (§4). | The stricter the tier, the less the tool may touch. |
| D4 | Page identity is BLAKE2s with a 16-byte digest, computed in the shim and checkable with `hashlib.blake2s(digest_size=16)`. Tick records, chunks and the chain use sha256. The codec is `auto` by default: zstd on Python ≥ 3.14 (`compression.zstd`), zlib otherwise. zlib, zstd or none can be set. Python code is standard-library only. | Both hashes are in hashlib, and BLAKE2s is fast on 32-bit x86, where the shim runs. |
| D5 | Clock modes are `virtual` (the default for tas and lab), `fixed` (lab only) and `wall` (forced in hands-off). `hz` defaults to 60 and models a 60 Hz V-Sync machine. A tick is one WinMain pass. | The engine has no fixed step. Its game time is a scaled wall-clock delta (§2). |
| D6 | There is one TOML schema, and every key is optional (§11). | No run should need configuration to start. |
| D7 | Native code lives in `KPTASInterface/native/shim`. It is freestanding C built with `zig cc` for x86-windows-gnu and loads as a `dinput8.dll` proxy (§6). | The exe imports dinput8 (IAT 0x73d000), so the proxy initializes before the exe's CRT and WinMain. M0 confirmed this order on Wine 11 staging (wow64) with K1 GOG 1.03 (§12). |
| D8 | The wire verbs are listed in §10. There is no rng verb. | RNG state is ordinary memory (`_holdrand` at `_tiddata+0x14`). |
| D9 | Milestones M0–M5 are in §12. | Each milestone measures before it builds and has an exit test. |
| D10 | BioWare's own save and load are untouched. Save teleport, AMG and hotshots stay in-game actions. | The tool records play. It does not replace game mechanics. |
| D11 | There are two sim backends: PySim (pure Python) and SimGame.exe (built with zig, linking the real shim core, run under Wine). | Both let the Python and native code be tested headlessly. |
| D12 | Portable determinism. The shim presents a fixed virtual machine boundary, named by a profile id that goes into HOLO and the determinism key (§7). CRT math defaults to a deterministic reference implementation. Every item is pending the host-input census. | A record made on one machine must replay identically on any machine, under Wine or on Windows. |
| C1 | Python listens on loopback. The shim connects at boot, using `KPTAS_PORT` and `KPTAS_SESSION` from its environment. `KPTAS_CLOCK` carries the clock origin that `DllMain` needs before any connection exists, and `KPTAS_PORTABLE` the math mode and control words. | Python owns the process lifetime, and each connection starts a new epoch. |
| C2 | Until thread slots exist (M1), `threads.model = "join"`. When the exe resumes one of its own worker threads, the hooked `ResumeThread` returns only after that thread parks again. | Loads and queued reads then depend only on the score, before the M2 scheduler exists. |
| C3 | Every chunk after ENVP is chained, with its 4cc in the hash. | Marks, epochs, receipts and the seal become tamper-evident too. |
| C4 | A recording's journal holds no wall-clock time and no random id. The bundle id is the first 32 hex digits of H_0. Only seeks and verification append RCPT chunks, which may carry measured latency; recording never does. | Two identical recordings then produce byte-identical journals, which is the M1 exit test. |
| C5 | Store frames use CRC-32 (`zlib.crc32`), not CRC-32C. | CRC-32C is not in the standard library, and content keys verify decoded bytes anyway. |
| C6 | Each tick's semantic journal entry sits inline in its TICK record. | One TICK body then describes and verifies its tick on its own. |
| C7 | The framed TICK chunk is the commit point. The superblocks from the tick-state revision 2 layout are dropped. | A chunk with a valid digest already marks a durable end. |
| C8 | The default tier is tas, with seek `replay`. lab has to be asked for. | tas writes no memory except recorded bytes put back through restore, so it only returns to states the game was already in. The compiler leaves an omitted header tier unset, so `run.tier` decides, and the tier that counts is still the computed one (SEAL). |
| C9 | tas requires an allowlisted module set from tick 0, and every restore checks it again. | Any other in-process module can write game memory without appearing in the verbclass log. |
| C10 | CExoRand is observed in the journal. It is never pinned above lab. | Pinning it is a memory write. It does more than vary ambient sound timing: that timing decides the tick on which `CSWCSoundObject::AIUpdate` (0x66bbd0) draws the gameplay `rand` (0x66afba), and GenSeed (0x5ed080) folds uninitialised stack, QPC and `GetSystemInfo` into the seed, so the stream changes with the shim build. |
| C11 | Memory reads (`mem.read`, `watch`, `semantic.read`) are allowed in tas. | They observe without changing state. |
| C12 | Revision 2's TOML is adapted: `[sched]` and `[io]` fold into `[threads]`, `[kernel]` becomes `[files]`, `[host]` moves into `[portable]` and `[tick_store]` becomes `[store]`. The Linux helper, `[runtime]` and the hash and codec knobs are removed. | D4, D7 and D12 fix those choices. |
| C13 | Behind a portable profile, the determinism key covers only the game-side inputs. The host fingerprint (Wine version, prefix, real GL driver, DLL overrides, launch wrapper, other modules) is recorded but joins the key only with profile `native`. Under `native` it also holds the host facts the game then sees (CPU, time zone, user, locale, install path); behind a profile they are not recorded. | One profile must give one key on Wine and on Windows, or D12's cross-machine replay could never compare roots. |
| C14 | In lab and tas the game's audio device is the shim's virtual Miles device (`audio.device = "virtual"`): all 61 `_AIL_*` imports are answered from a model in harness_game, on the virtual clock. `none` keeps the sound-off game (`Disable Sound=1`) of the M1 and M2 keys; `real` (lab, and hands-off) lets the host's Miles and audio hardware answer. | With sound on, Miles answers decide sim state (sources' play lists, `CalculatePitchVarianceFrequency` draws from tick 650 of the M1 exit score, the dialog wait, the ini's EAX and voice counts). With a real device those answers depend on the host's hardware and on Miles's own threads. |
| C15 | In tas the shim takes a restore only to a root this process captured itself or to one of an admitted set the driver hands over once per process (the source bundle's recorded roots), and it refuses any other before it reads or writes anything. A restore that does not verify poisons the session: no tick runs until a restore verifies or the process is replaced. Every verified restore is journalled on the next tick. | Checking the root alone let a driver write any page set whose root it computed itself. Rolling a failed restore back would need the old bytes of every written page and of the real side (handles, the files tree put first), so the session stops instead. The journalled set lets the judge check that the admitted roots were the recording's own. |

## 2. Engine facts relied on

All of these come from static byte-level and decompile checks. The only live observations so far are the M0 results in §12.

| Fact | Address |
|---|---|
| WinMain | 0x4041f0 |
| Client MainLoop | 0x602eb0 |
| Server MainLoop, called once per pass (`inc esi; cmp esi,1; jl` at 0x404778) | 0x4ae860 → 0x4babb0 |
| Present in WinMain. It is skipped when the movie check (0x5edb40) is nonzero or ReInitializing (`[0x7a3a2c]`) is set. | 0x4048c1 |
| UpdateScreen, and its present, which serves load and save screens | 0x401c10, 0x401cc2 |
| IAT slots: DirectInput8Create 0x73d000, SwapBuffers 0x73d00c, GetTickCount 0x73d0f8, QueryPerformanceCounter 0x73d1a8, QueryPerformanceFrequency 0x73d1ac, Sleep 0x73d1e4, CreateThread 0x73d1f0 | |
| GetHighResolutionTimer: microseconds from QPC, falling back to GetTickCount × 1000 | 0x5e9260 → 0x5ec210 |
| `CWorldTimer::SetSnapshotTime`: adds (QPC delta) × scale/100 unless its fixed flag is set | 0x4adbd0 |
| SetFixedSnapshotRate, and SetFramesPerSecond. The latter's only caller (0x4c5946) is gated on `movieFrameRate` `[0x832904]`, which nothing writes. | 0x4ae170, 0x5ee4a0 |
| `lockFrameRate` is 0.0 and has no writer in normal play | 0x7a3c64 |
| `AurSetVSyncState` calls `wglSwapIntervalEXT(state != 0)`. The state defaults to 1. | 0x44ee90, 0x78d438 |
| Creature AI budget of 10,000 µs of QPC time. RunActions has 1,000 µs. | 0x4b0b70 |
| `rand` (MSVC LCG, multiplier 0x343FD, increment 0x269EC3) and `srand` | 0x6fc447, 0x6fc43a |
| `_holdrand` at `_tiddata+0x14`, reached through the TLS index global | 0x7a29b0 |
| `srand(GetTickCount())`, once, inside 0x5f8550, reached from StartServices 0x5ed860 | 0x5f8d61–0x5f8d68 |
| Grass reseed `srand(frndint(x+y+z))` inside `CAurTriangleBin::CreateArrays` 0x4a87c0 | 0x4a89c5–0x4a89f4 |
| CExoRand (ambient sound only): Rand, GenSeed and ReSeed. The object is at `[*(0x7a39e0)+0x10]`. | 0x5ecca0, 0x5ed080, 0x5ecd40 |
| Exe threads started through CreateThread: backdrop 0x401a30 (created at 0x403885), movie worker 0x405b30 (0x405e07), RIM loader 0x409b90 (0x409cd4), async reader 0x5ebe10 (0x5ebf0d, during CRT static init) | |
| Loads: LoadModuleStart 0x4c9050, InProgress 0x4c5720 and Finish 0x4c5880. The `Sleep(10)` spin is at 0x4c4400 and `UpdateScreen(1/30)` at 0x4c443c. | |
| Save, first pass: SaveGame makes the slot directory, takes the screenshot and arms a stall event. The script autosave (DoSinglePlayerAutoSave) calls it from the server MainLoop (0x4bae4c, slot 1, "AUTOSAVE"). The player's saves reach it through CServerExoApp::SaveGame (0x4ae6e0, a tail jump), called from the player's module message (0x5249b1) and the server-admin message (0x528817). | 0x4b58a0 |
| Save, second pass: StallEventSaveGame, called from the server MainLoop's stall-event branch (0x4bafb1) on the next pass, which skips SetSnapshotTime. It writes the save on the main thread and blocks. Every save that SaveGame arms runs both passes. | 0x4b3110 |
| Area-transition autosave: DoPCAutosave writes "AUTOSAVE" (slot 1) itself, on the main thread, blocking. StartNewModule (0x4bab7b) calls it before the new area loads, when AutoSaveOnEnteringModule (0x4b5050) says so: modulesave.2da `force` always, `no` never, `yes` only after 15 minutes without a save and with the client options bit at +0x8 bit 2 set. It never reaches SaveGame or StallEventSaveGame. | 0x4b8300 |
| WinMessageHandler, the game window's WndProc. WM_CLOSE calls `CClientExoApp::QueryExitProgram` (0x5eda00), which pauses the game and opens the quit box. WM_SYSKEYDOWN VK_MENU turns game input off; WM_SYSKEYDOWN VK_F4 with Alt down turns it back on, sends itself WM_CLOSE, then falls through to DefWindowProcA. WM_SYSCOMMAND returns 0 for SC_SIZE, SC_SCREENSAVE and SC_MONITORPOWER; SC_CLOSE goes to DefWindowProcA, which sends WM_CLOSE. | 0x402800 |
| The dialog voice-over wait asks the Miles stream (IsSoundPlayingInDialog); in lab and tas that is the virtual device | 0x5edbc0 |
| Sound init: `CExoSoundInternal::Initialize` returns at once when `DisableSound` is 1, and sets it when `_AIL_quick_startup` fails. The 3D provider comes from `soundprovider.2da` by the names Miles enumerates (LoadSoundProvider). | 0x5da8a0, 0x832894, 0x5d9140 |
| The exe imports opengl32, glu32, gdi32, kernel32, user32, dinput8, mss32, binkw32, ole32, version and imm32. It does not import winmm. File I/O is C stdio with no overlapped I/O. | |

**Where sources disagree.** In each case the verified facts win.

- **60 Hz.** The engine has no fixed step and no accumulator. Each pass advances by wall-clock time. The figure 60 comes from V-Sync on a 60 Hz display. `hz` models that machine; it is not an engine constant. The dormant 30 Hz movie path is not used.
- **UpdateScreen re-entry.** One check found that UpdateScreen runs the server loop only when arg2 == 1 (tested at 0x401cdb), and that no static caller passes 1: the client loop's argument comes from `xor ebx,ebx` at 0x602ed6. Another check says the call from 0x6032e5 does run the server loop. The tick definition in §5 holds either way, and O6 will settle it.
- **`fixed` mode.** Fixing the five world timers leaves client timer_2, the server time_stop_timer and every QPC budget on real time. At 60 Hz each step is 16,666 µs, so game time drifts about 40 µs per second. `fixed` therefore never claims determinism on its own.
- **Unverified addresses.** The WinMain loop-head call site (pushes at 0x404585–0x404592, GetMessageA at 0x4045a7), WinMessageHandler 0x402800, InitExtensions 0x436490/0x44f070 and the CRT SSE2 CPUID check at 0x7043c1 come from the design notes only and have not been re-verified.

## 3. Glossary

- **Tick.** One WinMain pass in which the loop-head PeekMessageA finds no message (§5).
- **Sub-tick.** An UpdateScreen present inside a tick, for example during a load. It is journalled but is never a restore target.
- **Epoch.** One game process. It starts at a launch or relaunch and ends at exit (EPOC chunk).
- **Gate.** The single point per tick where input, clock, capture and restore happen: the WinMain loop-head PeekMessageA.
- **Note.** The canonical held-input set for one or more ticks, from `kptas.score`. `.` means nothing is held.
- **Sim set.** The restorable memory classes: `game_arena` (game heaps, game VirtualAlloc regions and game thread stacks, M2+), `image_data` (the exe's `.data` and `.bss`) and `harness_game` (virtual tables, context records, the virtual clock and the score cursor). Everything else is ambient (fingerprinted only) or excluded.
- **State root.** A sha256 two-level radix tree over the complete page map (§8.6). Also called `full_root`. `sim_root` is the same tree with the mask registry applied, and is comparable across launches only once qualified.
- **Restore point.** A captured tick at which the gate predicate held.
- **Keyframe.** A tick whose state-tree objects are stored. A materialized pack is a keyframe written as one contiguous file.
- **Determinism key.** `EnvPin.key()`: sha256 of the ENVP key inputs (C13). Runs with different keys never share roots.
- **Verbclass.** A per-tick bitset recording what touched the game (§4.2).
- **Receipt.** A measured result: seek, verify or judge (§4.3).

## 4. Tiers and verbclass rules

### 4.1 Tiers

| | lab | tas | hands-off |
|---|---|---|---|
| In-process code | shim plus anything else | shim only, with an allowlisted module set | none (Wine's builtin dinput8) |
| Clock | virtual (default), fixed, wall | virtual | wall |
| Seek | restore (default), hybrid, replay, semantic | replay (default); restore and hybrid while authoring | replay |
| Input | dinput, os | dinput | os |
| Memory writes | any | recorded bytes only, via restore | none |
| Published artifact | the holorecord, marked lab | the score, which must pass a clean judge replay | the score and capture, best effort |

A tas restore (C15) is held to the game's own recorded states:
- Its target root must be one the process captured at one of its own gates, or one of the admitted set (`restore.admit`, once per process). Python admits the recorded roots of the bundle it seeks in: the state roots of every captured restore point on its ancestry, ascending, named by the sha256 of their concatenation. Any other root is refused with `refused` before the set file is opened.
- A restore that does not verify (root, page re-hash, audit or tick) leaves its pages written and poisons the session. `step`, `wait` and `digest` are then refused until a restore verifies or the process is replaced. The live map takes the re-hashed keys of what was written, so a later restore can trust it.
- A verified restore is journalled on the next played tick as an `ev` entry `{"name":"restore","root":R,"set":S,"tick":T}`, where S is `self`, the admitted set's sha256, or `lab`. That tick's verbclass carries RESTORE.

lab restores any root and is never poisoned. A lab restore that does not verify is journalled too, with the root memory holds after it.

Revision 2's `observe`, `restore` and `author` map onto these tiers as follows:

- `observe` is the replay-only behaviour. It is the tas default and the only behaviour in hands-off.
- `restore` is tas authoring: restore and hybrid seeks, logged with the RESTORE bit.
- `author` is lab.

Revision 2's `tier` key is gone. Its three values survive as per-tier limits on seek modes.

The **effective tier** is the minimum of five things:
- the declared tier (score header first, then `run.tier`; see the note in §5.5);
- the backend ceiling;
- the module-set ceiling;
- `VerbClass.ceiling()` taken over every tick;
- the last judge receipt.

A run is **publishable** when all three of these hold:
- its effective tier is tas or hands-off;
- no tick carries RESTORE or FILES_RESTORE;
- a judge receipt passes.

### 4.2 Verbclass bits (`kptas.types.VerbClass`, u32 in every TICK)

| Bit | Name | Meaning | Allowed in |
|---|---|---|---|
| 0 | INPUT | virtual input staged in process | lab, tas |
| 1 | OS_INPUT | OS-level input | all |
| 2 | GATE | tick released by the in-process gate | lab, tas |
| 3 | CLOCK | virtual clock hooks active | lab, tas |
| 4 | WAIT | ticks spent in a `@sync` wait | all |
| 5 | CAPTURE | page capture or digest | lab, tas |
| 6 | READ | memory read, watch, semantic read | lab, tas |
| 7 | OS_EVENT | alt-f4, focus, blur, close, relaunch | all |
| 8 | SCREEN | frame grab | all |
| 9 | RESTORE | state written back from a recorded tick | lab, tas (authoring) |
| 10 | FILES_RESTORE | files overlay reset to a recorded tree | lab, tas (authoring) |
| 16 | WRITE | memory write of bytes not recorded from this run (poke, RNG set, helper mutations) | lab |
| 17 | SEMANTIC | semantic field write-back | lab |
| 18 | FIXED_CLOCK | engine timer fixed step | lab |
| 19 | FILES_WRITE | files put with unrecorded bytes after tick 0 | lab |

How a tick's verbclass limits the tier:
- If any of bits 16–19 is set, the ceiling is lab.
- Otherwise, if any in-process bit (0, 2, 3, 5, 6, 9, 10) is set, the ceiling is tas.
- Otherwise the ceiling is hands-off.

No verb patches engine logic. Glitches are never fixed.

### 4.3 Ratchet and judge replay

**Ratchet.** `tier.lock` runs before `start`. From then on, the shim refuses any verb outside the locked tier's allowlist (`kptas.types.VERB_ALLOW`) with the error `refused`.

A tas lock also refuses:
- `clock` with `fixed` or `wall`;
- `files put` after `start`;
- `input.dinput = "passthrough"`.

tas recording refuses to start unless all of these hold:
- the virtual clock is live in every hooked module;
- `seed_hooked` is true (the time hooks were live before the startup `srand(GetTickCount())` at 0x5f8d61);
- the module set is allowlisted;
- the configured portable profile is served.

**Judge replay (`telos verify --fresh`).**
1. Start a new process in the pinned environment.
2. Run `tier.lock` and `clock`, then place the start save by file only.
3. Replay the embedded SCOR. When the recording ends with the game exiting (its `proc.down` falls after the last tick), the replay plays that tick too, so the fresh process exits where the recorded one did.
4. Compare with the submitted run. These are compared:
   - `journal_sha256`, on every tick (or at `@sync` points, per `verify.compare`), with restore events left out on both sides;
   - raw `state_root` and separate `files_root`, when both runs have the recognized raw-root declaration and matching environment keys; every selected `every_tick`, `syncs`, or `end` point must have CAPTURED/nonzero memory and FILES/nonzero file roots, otherwise the qualified comparison fails;
   - every EPOC's tick, reason and `files_root` on the branch ancestry, plus the exit status of an epoch the game ended itself;
   - every journalled restore: its root must be the recorded `state_root` of the tick before it, and its set must be `self` or the recorded roots of the bundle or one of its parents.
5. Append an RCPT chunk on a new branch.

A tier can only rise through a new passing receipt.

**hands-off verification** checks four things:
- the module list shows no kptas shim or proxy;
- dinput8 is Wine's builtin;
- the exe hash matches;
- input came from xdotool only.

Its receipt is `best_effort`: frame hashes at `@sync` points plus human review of the video.

## 5. Seek, tick, gate and clock

### 5.1 Seek modes

| Mode | Mechanism | Tiers | Verify level |
|---|---|---|---|
| replay | From the latest `boot` or `save` origin at or before T on T's ancestry, relaunch or reload and replay the score, skipping presents only. | all | `sim_root` if qualified, else `journal` |
| restore | At a gate, compute W, the pages that differ between the live map and map(T). Reconcile commit state, write T's files tree and W, reconcile the real side (handles, GL objects and context state through `gl.need` and `gl.apply`, Miles, cursor), then verify. There is no replay of the target's ticks, and a fresh process restores directly; `seek.restore_warmup = loads` (diagnostics) first plays the first tick and T's area load. | lab, tas | `full_root` + journal + continuation canary |
| hybrid | Restore the latest keyframe K ≤ T that is a restore point (the latest restore point when there is no such keyframe), then replay K..T. | lab, tas | `full_root` |
| semantic | Write back journalled engine fields. The branch is tainted. | lab | `diff_only` |

Rules:
- A native restore writes only within its target process epoch. For a fresh session, a dead process, or a target in another epoch (including backward seeks), the supervisor first starts the recorded target epoch at its boot gate, then performs the requested restore or hybrid operation. Default direct restore replays zero ticks and does not take a fallback merely because the epoch changed. Replay mode still starts at that epoch's recorded origin and replays its score.
- Epoch setup requires an unambiguous recorded EPOCH_START and usable origin cursor/files. Required target page and file blobs are checked before replacing the old process; corruption is an error. Native startup uses the launch pin, then installs the recorded start overlay at the held gate. Target files and state are restored afterward. Missing origin page payloads alone do not prevent a direct restore when origin metadata and target pages remain available.
- Seek setup cannot replace an attached external process or write boot/closure records into an active branch sink. A live same-epoch backend can be reused; a caller recording a branch must finish its branch transition before seeking. Restored clock/save cursor metadata is synchronized without issuing new clock or save inputs.
- On a mismatch, each level of `seek.fallback_chain` is tried once. If the last level also fails, the run halts with a first-divergent-field report giving the journal field, page, symbol and key diff. It never loops.
- hybrid never anchors at a tick where a restore already failed in the same seek; that would repeat the attempt. A hybrid anchored at T itself replays nothing, so it gets the restore's checks (journal fields and canary).
- A continuation canary checks a recorded process exit immediately after the final gate, even when T itself is that gate. A missing exit fails the restore and takes the configured fallback. A canary that reaches an epoch's final recorded tick discards that process before restoring T again. Native quit processing can destroy windows before the exit report; its memory root alone cannot prove that the process is still usable.
- Placement of the score on the recording follows the recording's epochs: `!close` only asks the game to quit, and the process ends where its epoch ends.
- Every seek appends a receipt: from, to, mode, verify level, |W|, µs, roots, `pixel_exact`, canary, and the fallback taken.

### 5.2 Tick

Tick n is one WinMain (0x4041f0) pass in which the loop-head PeekMessageA finds no message. Such a pass makes three calls:
1. the client MainLoop 0x602eb0, once;
2. the server MainLoop 0x4babb0, once;
3. the present at 0x4048c1, unless a movie or ReInitializing skips it.

A pass that finds a message only dispatches it, and is not a tick.

- The tick counter advances when the gate returns "no message".
- The shim cross-checks three counts. Each tick has exactly one top-level entry to 0x602eb0. `server_passes` counts entries to 0x4babb0 during the tick. `subticks` counts entries to UpdateScreen 0x401c10.
- UpdateScreen never advances the tick, whatever it calls.
- A tick with sub-ticks sets the HOLD flag. `tick.max_subticks` halts the run with a report.
- Tick 0 is the first gate after `start`. CRT startup and StartServices run before it, at virtual time 0.

### 5.3 Gate

The gate is the exe's PeekMessageA IAT hook. It fires when all of these hold:
- the return address is WinMain's loop-head call site (O5);
- UpdateScreen depth is 0;
- `tick > last_gated`.

Every other exe PeekMessageA returns no message. One exception: inside a save, while a score `!altf4@save` is live, UpdateScreen's pump (0x401510; peeks return to 0x401529 and 0x40157c) is served the synthesized queue, so the Alt+F4 keys and the SC_CLOSE they post reach the game mid-save as on Windows. No tick ends there and the clock does not move.

The sequence:
1. Pump the real queue with real `PeekMessageA(PM_REMOVE)` and `DispatchMessageA`. The game's WndProc is interposed. External messages are journalled and routed to `DefWindowProcA`. Messages caused by the game's own calls pass through and are journalled.
2. Evaluate the restore-point predicate (M2+). A tick fails it if a GL buffer is mapped, a file operation is in flight, a scheduler invariant is broken, or a non-allowlisted module is loaded.
3. Drain wire commands.
4. Capture into the staging ring (M2+; M1 sends only the JOURNAL record).
5. Apply any pending restore (M3+).
6. Advance the virtual clock to V(n+1).
7. Stage input for tick n+1: DirectInput buffered records with virtual `dwTimeStamp` and `dwSequence`, key state, cursor and synthesized messages.
8. Return either a queued score message, which the next GetMessageA serves identically, or "no message".

`tick.context`:
- `inline` (M1) runs the hook on the game stack. It supports replay only.
- `fiber` (M2+) switches to a harness stack and stores a context record in `harness_game`: GPRs, EFLAGS, x87 CW, MXCSR, LastError and the SEH head. Restore requires it.

### 5.4 Threads

- **`join` (M1).** Threads the exe creates through CreateThread (0x73d1f0) run normally. The exe's `ResumeThread` on one of them blocks until that thread self-suspends, waits or exits, so the RIM loader (0x409b90) and the async reader (0x5ebe10) finish their job inside the call that woke them. `Sleep` inside a tick yields real time without advancing virtual time. `threads.hang_guard_ms` halts with a report; it never changes execution.
- **`slots` (M2+).** Pre-created slot threads run under a deterministic serial scheduler. Switches happen only at virtual sync calls, and the thread set stays constant for the session.
- **`free`.** Vanilla concurrency. Replay is unverified, and restore and hybrid are refused.

### 5.5 Virtual clock

- **Advance.** Within a segment that starts at tick s with rate `hz`: V(n) = V(s) + ⌊(n − s) · 10⁶ / hz⌋ µs. This equals 10⁶/hz per tick with exact remainder carry, so 60 ticks at 60 Hz are exactly 1,000,000 µs. V is frozen within a tick. A new `@segment hz` takes effect at the next tick.
- **Reads.** Let U = `uptime0_ms` · 1000 + V.
  - QPC = ⌊U · `qpf_hz` / 10⁶⌋, and QPF = `qpf_hz`.
  - GetTickCount and timeGetTime = ⌊U / 1000⌋ mod 2³².
  - GetSystemTimeAsFileTime = FILETIME(`initial_realtime`) + 10·V. GetSystemTime and GetLocalTime derive from it, using the profile's time zone.
  - The clock is live from the proxy's `DllMain` (§6), before the CRT runs. The first reads come from the CRT's security-cookie setup: GetSystemTimeAsFileTime (return address 0x006FC599), then GetTickCount (0x0070A9B5), then QPC (0x0070A9C1), all on the main thread and all at V = 0 (M0). The startup `srand(GetTickCount())` seed is therefore `uptime0_ms` (3,600,000).
- **Hooked modules.** In the exe's IAT: QPC, QPF, GetTickCount, Sleep, the waits, and the FILETIME and SYSTEMTIME calls. The same set in the IATs of mss32, binkw32 (or `binkw32_real` behind another proxy) and the Miles providers. `GetProcAddress`-resolved time APIs go through fixed thunks. Only mss32, binkw32 and the K2 exe import `timeGetTime`.
- **Budgets.** `budget_model = "frozen"` means wall-clock budgets (the 10 ms creature AI budget at 0x4b0b70) always complete. A call site that reads the clock more than `budget_guard.site_reads` times in one tick gets a deterministic `step_us` micro-advance; each trip is journalled. `spin_escape` handles the `lockFrameRate` spin.
- **`fixed` (lab, FIXED_CLOCK).** Calls `SetFixedSnapshotRate(t, 1, hz)` on the five world timers, bracketed by pause and unpause, with the guard scale·10⁶/hz < 2³¹. The timer addresses are O9. The §2 limits apply.
- **`wall`.** No hooks.
- **`hz` precedence.** `@segment hz`, then the score header `hz=`, then `[clock] hz`, then 60. The declared tier follows the same order: score header, then `run.tier`, then tas.
- **Omitted header keys.** `kptas/score/compile.py` leaves `hz` and `tier` unset when a header omits them, so `[clock] hz` and `run.tier` apply.
- **Precision preference.** `[clock] precision = "frame"` is the default, selecting 60 Hz. `"tick"` selects 1000 Hz only when `[clock] hz` is omitted. Explicit rates and the score precedence above remain authoritative. Both use the completed WinMain-pass capture boundary; precision does not imply a render-present hook or engine-internal substeps. Recording environment pins preserve effective clock rates.

## 6. Native shim (D7)

```
KPTASInterface/native/
  shim/src/     the dinput8.dll proxy: gate, clock, input, messages, threads, handles, GL, frames,
                Miles, boundary, math sites, wire, journal; shim/build.sh builds shim/build/dinput8.dll
  state/        kst, the savestate engine: arena, capture, restore, slot contexts (linked into the shim)
  portable/     refmath and the x87 math site inventory (refmath is linked into the shim)
  simgame/      SimGame.exe; links state/ statically
  probes/       the M0 probes and the Miles stream probe
  tools/        the M1 and M2 drivers
```

**Build.** `zig cc -target x86-windows-gnu -ffreestanding -nostdlib -shared`, linked only against the import libraries for kernel32, user32 and ws2_32. There is no CRT: the shim has its own `memcpy`, `memset` and BLAKE2s.

**Load.** `dinput8.dll` sits in the game directory. Under Wine it loads only with `WINEDLLOVERRIDES=dinput8=n,b`, which is part of the environment pin. The loader binds the exe's imports and runs the proxy's `DllMain(PROCESS_ATTACH)` before the exe's entry point.

`DllMain` runs under the loader lock. In this order, it:
- reads the math mode and control words from `KPTAS_PORTABLE` (§7, item 3) and removes the variable;
- reserves memory: `harness_core`, the arena (1 GiB across `[0x22000000, 0x30000000)` and `[0x31000000, 0x63000000)` for k1_gog_103), and harness_game (4 MB at 0x0E000000). The arena allocator excludes the intervening 16 MiB, leaving room for host mappings without lowering its total capacity. One allocation must fit within one extent;
- builds the virtual machine boundary (§7) from `KPTAS_BOUNDARY`, so the CRT's first look at the environment, the command line and the machine already sees the virtual ones;
- reads the clock origin (`qpf_hz`, `uptime0_ms`, `initial_ft`, `hz`, mode) from `KPTAS_CLOCK`, which Python sets at launch, and the audio device from `KPTAS_AUDIO`. `initial_ft` is `clock.initial_realtime` as a Windows FILETIME;
- patches the exe's IAT slots, so the virtual clock is live at V = 0 before the CRT's first time call;
- writes a 5-byte detour at the exe's entry point and the engine detours, and last, under reference math, the math sites;
- wraps every exe import that nothing above hooked in a thunk for the external-call trace (`diag.c`, `xtrace.c`). With the trace off, a thunk jumps straight to the real function.

It never starts Winsock or threads and never calls LoadLibrary. A clock live at `DllMain` is required, not optional: the CRT security cookie and the startup `rand` seed both read time before WinMain.

The **entry detour** runs before CRT startup and outside the loader lock:
1. load ws2_32;
2. connect to Python;
3. map the staging ring;
4. answer `hello`;
5. hold until `start`;
6. jump to the original entry.

As a result, CRT startup (the security cookie, then the static init that creates the reader thread at 0x5ebf0d), StartServices' `srand` and WinMain all run hooked. M0 confirmed that `DllMain` runs before the CRT and WinMain. `hello` still reports `seed_hooked` per run, as a check that the hooks were installed. A `clock` request before `start` may set `hz` and `mode`, but must repeat the origin that `DllMain` installed; otherwise it fails with `state`.

**DirectInput.** `DirectInput8Create` returns a virtual `IDirectInput8`, with keyboard and mouse `IDirectInputDevice8` objects driven by the score, so Wine's dinput is never acquired. `input.dinput = "passthrough"` (lab only) forwards the call to the system dinput8, which is loaded from the system directory on the first call.

**Miles.** The exe reaches Miles only through 61 `_AIL_*` imports from MSS32.DLL. It registers no Miles callbacks or file callbacks, and calls them from the `CExoSound*` code on the main thread slot (`quick_handles` also from the movie thread slot). With `audio.device = "virtual"` (`KPTAS_AUDIO`), `audio.c` hooks all 61 or none, and answers from `SH->aud`:
- `quick_startup` succeeds. The driver, providers, listener, voices and streams are tagged values allocated in call order.
- The provider list is the seven providers, with the handles 9 to 15, that the real Miles 6.5h enumerates under Wine. `audio.provider` decides which open. The default, `software`, opens the three that open there with a device (DirectSound3D Software Emulation, Dolby Surround, Miles Fast 2D Positional Audio), so the game ends at Miles Fast 2D (row 4 of `soundprovider.2da`), as it does with a real device.
- Status and positions are computed when asked, from the virtual clock. Sample lengths come from the WAV header (PCM, IMA ADPCM). A stream, read through the game's own virtual file handle, plays for its decoded length (header, or MP3 frame count) and reports the total the real Miles reports: the data size over the byte rate (the WAV header's average byte rate, or the first MP3 frame's bit rate), rounded to nearest with ties to even. `native/probes/miles.c` measured that over all 15008 stream files. The real Miles stops a stream 22 to 123 ms (six files, one host) after its decoded length, a drain that depends on the host; the model has none.
- `decompress_ASI` and `decompress_ADPCM` return a canonical PCM WAV image (`audio.decode = "canonical"`): the size and format of the decoded wave, with zeroed samples, on the exe's CRT heap. The game reads only the header.
- `BinkOpen` gets no sound track, because the driver the game hands Bink is not a real Miles driver.

Nothing is played (`audio.output = "null"`). In `real` mode (lab), `open_stream` is still rewritten to a real file handle, or Miles could not read the game's virtual one; `audio.shadow` logs the real answers beside the model's (provider list, WAV info, decode sizes, stream lengths).

**Allocator.** `harness_core` is a private region reserved at boot. The shim never allocates from the process heap or the game heap.

**Channel.** Winsock loopback TCP carrying NDJSON (§10). Bulk bytes travel through files in the session directory.

**Staging ring.** The shim calls `CreateFileMapping` on a session-directory file (a `Z:` path under Wine) that the Python process also mmaps (§9, O11).

**Coexistence.** Hyperstim loads through a binkw32 proxy. KPM is not supported. A vulnerability was found in KPM, so Stasis does not run alongside it. The host refuses a game.dir that holds KPM's loader, and an active shim exits with code 75 when KPM is in the process. Neither one changes KPM's files.
- The shim never patches their slots. SwapBuffers (0x73d00c) belongs to Hyperstim's overlay when it is present, and the M5 HUD chains through whatever pointer that slot holds.
- Each gate checks the shim's patched bytes, and a changed slot halts the run with a report.
- Restore refuses while any module outside `shim.module_allowlist` that allocates from game-visible heaps is loaded. tas requires the allowlist from tick 0 (C9).

**Builds.** K1 GOG 1.03 comes first. Steam needs the unpacked exe (sha256 `2719ed2c…`). The SteamStub exe (sha256 `34e6d971…`) has an encrypted `.text` and is refused. K2 comes later, and its addresses are unverified.

**No Linux helper.** There is no ptrace, pidfd or `/proc/PID/mem` helper.

**SimGame.exe (D11).** A zig-built Win32 program. It links the shim core and exercises the paths the shim depends on:
- a PeekMessage WinMain loop with client and server passes;
- an MSVC-LCG `rand` whose `holdrand` lives in TLS;
- a worker created during static init that self-suspends;
- a QPC-budgeted loop;
- heap churn and stdio file I/O.

It runs headless under Wine.

## 7. Portable boundary (D12)

The profile id goes into `Header.profile`. The full values (`kptas.types.PortableProfile`) go into `ENVP.profile`, and so into the determinism key. Behind a profile, the host fingerprint stays out of the key (C13), so a Wine run and a Windows run share one key. `native` means no boundary: the real values are recorded, not presented, the host fingerprint joins the key, and a replay is valid only on a matching host.

The portable work is a separate track. It does not gate M0–M5, and each item lands once the census confirms it.

**The boundary as served (native/shim/src/bound.c).** The supervisor passes the resolved profile to the shim as `KPTAS_BOUNDARY`, `;`-separated `key=value` pairs: `os`, `lcid`, `acp`, `oemcp`, `tz`, `computer`, `user`, `cpus`, `cpu_level`, `cpu_rev`, `install`, `gl_vendor`, `gl_renderer`, `gl_version`, `gl_ext` (space-separated; `WGL_` names go to the WGL string) and `gl_q` (`enum:value`, comma-separated; an enum with no value is unsupported). Unset means the built-in baseline-1 values; `native` means no boundary. hello reports `boundary.sha256`, the sha256 of the text the shim parsed, and the Python side refuses a shim whose digest is not its own profile's. That is the "portable profile is served" check.

What the exe sees, whatever the host:
- **Install path.** `GetModuleFileNameA` gives `install_dir\swkotor.exe` for the exe, and other modules under the real game directory come back under `install_dir`. `GetCommandLineA` is that path, quoted, followed by any real arguments. `GetCurrentDirectoryA` and `GetFullPathNameA` answer in the same space. A path under `install_dir` that the game passes to a file call (CreateFileA, FindFirstFileA, GetFileAttributesA, SetFileAttributesA, DeleteFileA, CopyFileA, MoveFileA, Create/RemoveDirectoryA, GetFileVersionInfo(Size)A) reaches the real game directory. Relative paths are untouched. The handle table keeps the path as the game gave it.
- **Environment.** `GetEnvironmentStrings`, `GetEnvironmentStringsW` and `GetEnvironmentVariableA/W` serve one fixed block: ALLUSERSPROFILE, APPDATA, CommonProgramFiles, COMPUTERNAME, ComSpec, HOMEDRIVE, HOMEPATH, LOCALAPPDATA, NUMBER_OF_PROCESSORS, OS, PATH, PATHEXT, PROCESSOR_ARCHITECTURE, PROCESSOR_IDENTIFIER, PROCESSOR_LEVEL, PROCESSOR_REVISION, ProgramData, ProgramFiles, PUBLIC, SystemDrive, SystemRoot, TEMP, TMP, USERDOMAIN, USERNAME, USERPROFILE and windir, built from `computer_name`, `user_name` and the CPU values. There is no TZ and no KPTAS_* variable. The shim reads its own variables from the real environment in `DllMain`. The only reader in the exe is the CRT's tzset, for TZ.
- **Machine.** `GetSystemInfo` (and `GetNativeSystemInfo` through GetProcAddress) gives `cpu_count` processors of family `cpu_level` and revision `cpu_revision`, 4 KiB pages and 64 KiB granularity. `GetVersionExA` gives `os_version`, platform NT, no service-pack string. `GetUserDefaultLCID`, `GetACP` and `GetOEMCP` give the locale's values. GetCPInfo, GetLocaleInfoA/W, LCMapStringA/W, CompareStringA/W and GetStringTypeA map the default locale and code page to them. The time zone is UTC: `GetTimeZoneInformation` and `FileTimeToLocalFileTime`. `GetStartupInfoA` describes a GUI process started from the shell, and `GetStdHandle` gives no handles. `GetComputerName` and `GetUserName` give the profile names.
- **GL identity.** `glGetString` gives the profile's vendor, renderer, version and extensions, and `wglGetExtensionsStringARB/EXT` give its WGL names. Other `glGetString` names pass through. `glGetIntegerv` and `glGetFloatv` serve the profile's `gl_queries`: the limits the exe stores in its own memory, which are GL_STENCIL_BITS, GL_MAX_TEXTURE_MAX_ANISOTROPY_EXT and GL_MAX_GENERAL_COMBINERS_NV (unsupported in baseline-1, as on a driver without NV_register_combiners: the result is left alone and GL_INVALID_ENUM is raised). Every other query passes through. At the first extensions query the shim checks every advertised name against the real driver, and at the first use of a served limit it checks the driver can back it. It logs every shortfall (hello's `gl_missing` counts them).
- **Seed.** `CExoRandInternal::GenSeed` (0x5ed080) copies 128 bytes of uninitialised stack into the engine's random pool. A detour zeroes the dead stack below its entry, so the seed depends only on the virtual QPC and the virtual SYSTEM_INFO. This applies with profile `native` too.

Every string the game can keep a pointer to (command line, module path, environment blocks, GL strings) lives in one block in harness_game at 0x0E3F0000 (its last 64 KB, above shim_hg), filled in `DllMain`. The pointers the CRT and the renderer store are then the same in every run, and a restore writes the same bytes back. Nothing comes from the game heap, so the boundary does not move the heap layout.

The session directory reaches only Wine's own environment, not game memory. As a second guard, `shim.session_dir = "auto"` names it `session-0` for every run, seek and judge. A lock file keeps a second live process off it: that process gets `session-1` to `session-9`, which have the same length.

Where the items below stand. Item 3 (math) is served, and so are the parts of items 1, 2 and 4 that the boundary above covers: the GL identity, the machine, the locale, the time zone, the computer and user names, the install path and the environment. Item 5 (window) is served by `geo.c`. Still open, and tracked as O14:
- CPUID and RDTSC sites (item 2). None is patched. The one CPUID use found so far is `_CIpow`'s, and reference math replaces that routine.
- The boot refusal of item 1. The shim checks the driver at the game's first extensions query and first use of a served limit, and logs each shortfall. hello reports the count as `gl_missing`, but hello answers before the game's first query, so the count there is 0, and nothing refuses a run yet.
- Host addresses in game memory. The pointers GetProcAddress and wglGetProcAddress hand the game, module handles, and return addresses into the shim and its trampolines all reach game memory. They are the same from run to run on one host and one Wine build, and another Wine build would change them without changing the key.

The built-in profile table ships only values the census has confirmed.

1. **GPU identity** (needs GL virtual names from M4).
   - What the game sees: `glGetString` vendor, renderer, version and extensions, and every GL query whose result enters game memory, return profile values.
   - Boot check: the real driver must support every advertised extension. If it does not, tas refuses to start and lab warns. Profiles advertise a conservative set.
   - Why it matters: extension probing picks render paths, and render code draws from the gameplay `rand` (emitters, area updates, grass reseed).
2. **CPU identity.** CPUID call sites, taken from a static site list, are patched to return profile values. RDTSC sites, if the census finds any, read the virtual clock.
3. **Math.**
   - Every game thread starts with the x87 control word and MXCSR the profile names (defaults 0x027F and 0x1F80). They are the first two words of each kst slot frame, so the main slot runs CRT startup with them too. At each gate the shim counts the parked frames whose control words have moved off those values (MXCSR's sticky exception flags do not count). It reports the count as `fpu_drift` in `stats` and changes nothing.
   - `math = "reference"` is the default, on every machine. The shim takes over every x87 transcendental site of the exe (`native/portable/math_sites.toml`: 99 in game code, 6 in the CRT) with the refmath routines in `native/portable`, which give the same bits on every CPU:
     - a game site runs the whole instructions around its opcode (5 to 10 bytes, no branch, no inner jump target) from a cave in the shim, with a call to the refmath x87 stub in place of the opcode. The fyl2x stub is chosen per site from the constant the site loads: ln, lg, l2 or generic;
     - `_CIacos`, `_CIasin` and `_CIpow` jump to the refmath `_CI` stubs at their entry. This also drops `_CIpow`'s CPUID-chosen SSE2 path (`__pow_pentium4`);
     - the CRT's own sites become `ud2`, because nothing reaches them once those entries are hooked.
   - `native/portable/tools/gen_math_patches.py` writes the shim's table (`native/shim/src/math_sites.inc`) from a listing of the exe. It refuses when it cannot prove a window safe or a CRT site covered. The shim checks every row's bytes, and that no other patch owns them, before it writes any. On a mismatch it patches nothing, `hello` reports `math: "failed"` with the reason, and `start` is refused.
   - `fsqrt` is correctly rounded on every CPU, so it stays.
   - `math = "native"` leaves the exe's code alone, so results can differ between CPUs. It stays available as an option.
   - The math mode is part of the determinism key (through the profile) and of `Header.math`, because the two modes give different results. Python sends it to the shim in `KPTAS_PORTABLE`, and refuses a shim whose `hello` serves another mode or other control words. `DllMain` removes `KPTAS_PORTABLE` from the environment before the CRT copies it. The variable has one length in both modes (`math=ref` or `math=nat`), because Wine allocates the environment before `DllMain` runs, and a longer value moves every later Wine heap pointer the game stores (its command line pointer, for one).
4. **Host facts.** The values that vary between runs on one machine (available memory, free disk space) are pinned from the start, as part of the determinism layer; the rest wait for the census.
   - memory: `GlobalMemoryStatus(Ex)`, and `GetDiskFreeSpaceExA` (through the GetProcAddress thunk);
   - display: display modes and refresh rate, and `GetSystemMetrics`;
   - system: `GetVersionEx`, `GetSystemInfo`, locale and time zone;
   - identity: computer and user name;
   - host randomness.
5. **Window.** The client size is virtual and fixed by the profile. Client rect, window rect and cursor positions all use that space, so a score's mouse coordinates mean the same thing everywhere. The size must equal the pinned render resolution.

**Portable exit test** (after M4, pending census). A score recorded on machine A replays with identical journals on machine B under Wine, and on Windows.

## 8. Holorecord bundle (D2)

### 8.1 Layout

```
<name>.holo/
  journal                          append-only chunk stream (8.2)
  pages/pack-NNNNNN                store frames: pages, state-tree leaves and roots (8.7)
  keyframes/kf-TTTTTTTTTTTT.pack   materialized keyframe packs (8.8)
  files/pack-NNNNNN                store frames: file blobs and file trees
  index/                           disposable; rebuilt when missing or stale
  meta.json                        wall-clock and host notes, parent path; outside the chain
```

- **Bundle id.** The first 32 hex digits of H_0.
- **Branch.** A branch is a new bundle whose HOLO names `parent = {holo, tick, link}`. It stores only ticks after the branch point, and it resolves missing objects through the parent path recorded in `meta.json`.

### 8.2 Chunk framing

| Offset | Size | Field |
|---|---|---|
| 0 | 2 | magic `HR` |
| 2 | 4 | type (4cc) |
| 6 | 4 | payload length, u32 |
| 10 | n | payload |
| 10+n | 32 | sha256(payload) |

- **Order.** HOLO, SCOR, ENVP, then TICK, MARK, EPOC and RCPT in time order, then SEAL on a clean close.
- **Reading.** A reader scans from offset 0. The first bad or truncated chunk ends the valid prefix.
- **Writing.** A writer truncates the journal to the valid prefix before appending.

### 8.3 Chunk bodies

JSON bodies are canonical: UTF-8, sorted keys, no whitespace, no NaN.

| 4cc | Body | Chained |
|---|---|---|
| HOLO | `Header` JSON (fields as in `kptas.types.Header`; `start` is `boot` or `save:<sha256>`; `math` is the `portable.math` the run used, and reads as `native` in bundles written before it was recorded) | in H_0 |
| SCOR | the resolved canonical score text | in H_0 |
| ENVP | `EnvPin` JSON: key inputs (binary and data hashes, render stamp, game-side modules, shim hash, clock, profile, threads model, mask registry) and the host fingerprint (C13) | in H_0 |
| TICK | `TickRecord` (8.4) ‖ link | yes |
| MARK | `{name, tick}` ‖ link (names below) | yes |
| EPOC | `{epoch, tick, reason: altf4\|close\|relaunch\|crash, exit_status, files_root}` ‖ link | yes |
| RCPT | `Receipt` JSON, including `restore_points` as half-open tick ranges ‖ link | yes |
| SEAL | `{computed_tier, publishable, ticks, final_link}` ‖ link | yes |

**MARK names.** Three kinds share the chunk, and all three are chained:
- A score `@mark` label, at the tick the supervisor has reached. A label never contains `:`.
- `event:<name>` or `event:<name>:<arg>`, one per engine event, at the event's global tick. The supervisor writes them from each step report. Seek reads them back (`recorded_events`), so a resume inside a note still holds the events of that note's earlier ticks. It splits on the first `:` after the prefix: a name cannot contain `:`, an arg can.
- `evicted:<first>-<last>` or `evicted:unreferenced`, written by the store when a budget eviction drops payload. `Bundle.mark` refuses this prefix.

**Chain.**
- H_0 = sha256(sha256(HOLO) ‖ sha256(SCOR) ‖ sha256(ENVP)).
- For each chained chunk, H_k = sha256(H_{k−1} ‖ 4cc ‖ body), and the payload is body ‖ H_k.

### 8.4 TICK record (`TickRecord.encode`, version 1)

| Offset | Size | Field |
|---|---|---|
| 0 | 1 | version = 1 |
| 1 | 1 | reached: 0 recorded, 1 replay, 2 restore, 3 hybrid, 4 semantic |
| 2 | 1 | reason, when the tick is not a restore point: 0 none, 1 not captured, 2 GL mapped, 3 file I/O, 4 scheduler, 5 foreign module, 255 other |
| 3 | 1 | reserved, 0 |
| 4 | 8 | tick, u64 |
| 12 | 4 | epoch, u32 |
| 16 | 2 | server_passes, u16 |
| 18 | 2 | subticks, u16 |
| 20 | 4 | flags: CAPTURED 0x1, RESTORE_POINT 0x2, KEYFRAME 0x4, MATERIALIZED 0x8, EVICTED 0x10, HOLD 0x20, EPOCH_START 0x40, SIM_ROOT 0x80, FILES 0x100 |
| 24 | 4 | verbclass, u32 |
| 28 | 32 | state_root (zero unless CAPTURED) |
| 60 | 32 | sim_root (zero unless SIM_ROOT) |
| 92 | 32 | files_root (zero unless FILES) |
| 124 | 32 | input_sha256: the staged input frame |
| 156 | 32 | journal_sha256: sha256 of the semantic entry |
| 188 | 2 | note length, u16 |
| 190 | n | note, UTF-8, one line |
| … | 4 | page-ref count, u32 |
| … | 24·k | page refs, strictly ascending by vaddr |
| … | 4 | journal length, u32 (0 when only the digest is kept) |
| … | m | semantic entry, canonical JSON (`semantic/1`) |

**Page ref (24 bytes).**

| Offset | Size | Field |
|---|---|---|
| 0 | 4 | vaddr (page-aligned) |
| 4 | 16 | page key |
| 20 | 1 | memory class: 1 game_arena, 2 image_data, 3 harness_game |
| 21 | 1 | flags: DECOMMIT 0x1 (key and prot are zero) |
| 22 | 2 | Win32 page protection |

**Validity rules.**
- RESTORE_POINT is set exactly when reason = 0, and it requires CAPTURED.
- A tick without CAPTURED has no page refs and a zero state root.

**Semantic entry (`semantic/1`, M1 fields).** These fields must not contain pointers:
- tick, V, and `server_passes`/`subticks`;
- the main thread's `_holdrand`;
- `rand` draw counts per return address (the draw hook at 0x6fc447);
- the CExoRand state;
- budget-guard trips;
- the internal WndProc message log;
- the input hash.

With the virtual Miles device on, the entry starts with `aud`: `n` Miles calls and `ev` state changes in the tick, and `d`, a BLAKE2s-128 digest of the device model with the WAV image addresses left out. Entries of a `none` run keep their old bytes.

M2 and later add per-creature, event-queue and audio fields, under a new `journal_spec`.

### 8.5 Complete state per tick

- A TICK's page refs list the pages that changed since the previous tick in the same epoch. After a restore, that set is W.
- map(T) = the map at the nearest KEYFRAME tick K ≤ T, from its ROOT and LEAF objects, plus the refs of ticks K+1 through T.
- KEYFRAME ticks come every `seek.keyframe_every` ticks, at every EPOCH_START (after a full sweep), and after every restore.
- `state_root` must equal the root recomputed from map(T). This is how one state serialization equals one tick.
- The invariant holds for CAPTURED ticks, which means from M2 on. M1 ticks are identified by their journal digest only.

### 8.6 State root

- vpn = vaddr >> 12, with hi = vpn >> 10 and lo = vpn & 0x3FF.
- Each leaf covers 4 MiB and hashes the pages present in that range, in ascending order:
  leaf(hi) = sha256(`KPLF` ‖ hi u16 ‖ for each present page: lo u16, key 16 B, class u8, pad u8, prot u16).
  Each page contributes 22 bytes.
- root = sha256(`KPRT` ‖ for each non-empty leaf in ascending order: hi u16, leaf hash 32 B). Each leaf contributes 34 bytes.
- The empty root is sha256(`KPRT`). Decommitted pages are absent. Protection is included, so commit state and layout are part of the root.

### 8.7 Store frames (`frame_object` / `read_object`)

| Offset | Size | Field |
|---|---|---|
| 0 | 4 | magic `KPOB` |
| 4 | 1 | kind: 1 PAGE_I, 2 PAGE_X, 3 LEAF, 4 ROOT, 5 FILE_BLOB, 6 FILE_TREE, 7 BATCH |
| 5 | 1 | codec: 0 none, 1 zlib, 2 zstd |
| 6 | 1 | key length: 16 for pages, 32 otherwise |
| 7 | 1 | reserved, 0 |
| 8 | 4 | stored length |
| 12 | 4 | decoded length |
| 16 | 16 or 32 | key |
| … | 16 | base page key (PAGE_X only) |
| … | s | payload |
| … | 4 | CRC-32 of every preceding byte of the frame |

**Keys:**
- A page's key is its page key, taken over the decoded page.
- For LEAF and ROOT frames the payload is exactly the bytes that §8.6 hashes, so the key is sha256(payload).
- For FILE_BLOB frames the key is sha256 of the file content.
- A FILE_TREE payload is `{"entries": [[path, sha256, size, vtime_100ns], …]}`, sorted by path, and its key is sha256(payload). That key is `files_root`.

**Batch frames.** A BATCH frame holds pages, each a PAGE_I or PAGE_X object, compressed as one stream. It uses the same 16-byte head with key length 0, and stored length covering everything up to the CRC. The decoded length is the count × 4096.

| Offset | Size | Field |
|---|---|---|
| 0 | 16 | head: `KPOB`, kind 7, codec, 0, 0, stored length, decoded length |
| 16 | 4 | member count n, 1 to 65536 |
| 20 | … | member table, n entries: kind u8 (1 or 2), page key 16 B, base page key 16 B (PAGE_X only) |
| … | … | the members' bytes in table order, 4096 each (the page, or the page XOR its base), compressed as one stream |
| … | 4 | CRC-32 of every preceding byte of the frame |

- An object in a batch is addressed by the frame's offset and its member number. The table is not compressed, so an index can be rebuilt without decompressing.
- A writer puts one tick's new pages in batches of at most `store.batch_pages`, in the order of the tick's page refs. With `batch_pages = 1` it writes a frame per page.
- A reader reads frames of their own and batch frames in any mix.
- Every frame names its own codec. The HOLO `codec` is the setting a run was recorded with; `telos store repack` writes a copy with other settings and keeps the journal, so the copy's HOLO still names the original.

**Page storage:**
- A PAGE_X payload is the page XOR its base page.
- A page's base is its previous version at the same vaddr, else its page in the last materialized pack. The writer takes the XOR when it has more zero bytes than the page (in a batch), or when its frame is smaller (a frame per page).
- An XOR chain ends at an intact page (PAGE_I) stored in the bundle, or kept by an ancestor, and is at most `store.max_delta_chain` long. A base whose chain is that long already is not used.
- The zero page (key `c3f725e0c29f69cd616a0fe9b762bcb5`) is never stored.

### 8.8 Keyframes, index, commit order

- **Materialized pack.** `keyframes/kf-<tick>.pack` holds ROOT, then the LEAFs, then every non-zero page as PAGE_I, in ascending vaddr: in BATCH frames, or a frame per page when `store.batch_pages = 1`. One sequential read restores the image. It is written every `seek.materialize_every` ticks and at every EPOCH_START. Its tick writes no page or tree frames to `pages/`, since the pack holds them all.
- **Index.** `index/` has a private, versioned format. Deleting it is always safe. A reader takes the newest version it finds and also reads the older ones; a writer replaces them with the newest.
- **Commit order.** The objects a TICK references are appended first, and fsynced per `store.fsync`, before the TICK itself. The TICK chunk is the commit point. When a bundle is opened, unreferenced objects are ignored.
- **Unsealed runs.** A run without a SEAL is unsealed, and its tier is computed when it is sealed.

## 9. Staging ring (shim to Python)

**Mapping.** A 4096-byte header followed by `capacity` bytes of data, where capacity = `shim.ring_mb` MiB. Only the first 56 header bytes are used; the rest are zero.

**Header.**

| Offset | Size | Field |
|---|---|---|
| 0 | 4 | magic `KPSR` |
| 4 | 2 | version = 1 |
| 6 | 2 | record alignment = 8 |
| 8 | 4 | header size = 4096 |
| 12 | 4 | capacity (multiple of 4096) |
| 16 | 8 | write_pos: bytes written, monotonic (producer) |
| 24 | 8 | read_pos: bytes consumed, monotonic (consumer) |
| 32 | 8 | last tick closed by TICK_END (producer) |
| 40 | 4 | producer pid |
| 44 | 4 | consumer pid |
| 48 | 4 | state: 0 new, 1 attached, 2 closing |
| 52 | 4 | reserved |

**Record (`StagingRecord`).** Each record is a 40-byte header, then data, then zero padding to a multiple of 8.

| Offset | Size | Field |
|---|---|---|
| 0 | 2 | magic `SR` |
| 2 | 1 | kind: 1 PAGE, 2 TICK_END, 3 WRAP, 4 JOURNAL |
| 3 | 1 | memory class (PAGE), else 0 |
| 4 | 2 | flags |
| 6 | 2 | page protection (PAGE), else 0 |
| 8 | 4 | vaddr (PAGE), else 0 |
| 12 | 4 | data length |
| 16 | 8 | tick |
| 24 | 16 | hash |
| 40 | n | data |

**Flags.**
- PAGE records carry exactly one of these three:
  - DATA 0x1: 4096 bytes follow, and hash = page key.
  - ZERO 0x2: no data, and hash = the zero-page key.
  - DECOMMIT 0x4: no data, hash zero, prot 0.
- PAGE records may also carry SWEEP 0x8, meaning the record comes from a full sweep.
- TICK_END records may carry RESTORE_POINT 0x100 and FULL 0x200. A restore writes its own batch, the pages it wrote, closed by a TICK_END with RESTORE 0x400. It is not a played tick, and the consumer drops it.
- JOURNAL records carry the semantic entry, and hash = BLAKE2s-128(data).
- WRAP records are all zero.

**Batch.** Each tick emits its PAGE records, then exactly one JOURNAL record, then one TICK_END. The TICK_END hash is BLAKE2s-128 over (kind u8, vaddr u32, hash) for every PAGE and JOURNAL record of the batch, in order (21 bytes each).

**Rules.**
- offset = pos mod capacity. Records never straddle the end of the data area.
- If fewer than 40 bytes remain, the producer skips to the next capacity boundary. If a record does not fit, it writes a WRAP and skips.
- The producer writes the record, then stores `write_pos`. The consumer reads `write_pos`, consumes, then stores `read_pos`.
- When free space runs short, or the consumer lags by more than `capture.max_record_lag_ticks`, the gate waits. That spends wall time only; exactness is kept.
- Python writes restore sets (W) in the same record format to a session-directory file and passes its path to `restore`.

**Frame ring (`capture.frames`).** Frames travel in a second file, which Python creates and the `frames` verb names. It sits on tmpfs (`$XDG_RUNTIME_DIR/kptas`), because the shim writes one whole frame per tick. The shim never maps it. At each gate it opens the file, writes with `WriteFile` at fixed offsets, and closes it again. A handle or mapping that existed only while capture is on would move later handles or Wine allocations, and some of those reach game memory (the WGL extension-string pointer at 0x7a688c).

The file is a 4096-byte header, then `slots` slots. Each slot is a 4096-byte header page followed by `slot_bytes` of pixels.

| Offset | Size | Field |
|---|---|---|
| 0 | 4 | magic `KPFR` |
| 4 | 4 | version = 1 |
| 8 | 4 | slots |
| 12 | 4 | slot_bytes (a multiple of 4096) |
| 16 | 8 | written: frames published (producer) |
| 24 | 8 | read: frames consumed (consumer) |
| 32 | 4 | producer pid |
| 36 | 4 | state: 1 attached, 2 closed |

Slot `written mod slots` starts with this header:

| Offset | Size | Field |
|---|---|---|
| 0 | 8 | seq: the frame number |
| 8 | 8 | tick: the gate that wrote it, in the shim's numbering (the supervisor's offset makes it global) |
| 16 | 4 | width |
| 20 | 4 | height |
| 24 | 4 | format: 1 = BGRA, rows bottom-up |
| 28 | 4 | swaps: presents during the tick |
| 32 | 4 | reads: back-buffer reads during the tick |
| 36 | 4 | flags: REPEAT 0x1 (no read this tick; the frame before it again), BLANK 0x2 (black: nothing read since start or since a restore), CROPPED 0x4 |
| 40 | 4 | read_us: time in `glReadPixels` during the tick |
| 44 | 4 | skipped: presents not read (another drawable was current) |
| 48 | 4 | finish_us: time in `glFinish` before the reads, which is the frame's rendering |

**Frame rules.**
- At every SwapBuffers the shim reads the back buffer with `glReadPixels` into one buffer in `harness_core`, before the present runs or is skipped. The read runs on the harness stack. It puts back the read buffer, the pixel-store state, the pixel-pack buffer binding, the x87 environment, MXCSR and LastError. It never calls `glGetError`.
- The SwapBuffers hook makes the same calls on the game stack with capture on and off; only the work on the harness stack differs. The present (0x4048c1) and the gate's PeekMessageA are called from WinMain at the same depth, so whatever the hook's calls leave on the stack ends up in unwritten slots of the gate's frame. Those slots are live, so they are hashed. When only the capture path made the calls, page 0x3111f000 (the top of the main slot's stack) differed from tick 1.
- The read waits for llvmpipe to draw the frame, which a skipped present never does. That wait is the cost of a capture. A read into a pixel-pack buffer does not hide it: on llvmpipe, `glReadPixels` into one blocks until the frame is drawn.
- At gate n, before the journal push, the last frame read during tick n goes into the next slot. The shim writes the pixels, then the slot header, then `written`. Python reads `written`, stores the frame, then writes `read`. So a step's reply arrives after its frames are in the ring.
- A full ring makes the gate wait. That costs wall time only. After 120 s with no progress the shim turns capture off and reports it in the `frames` reply.
- The frames verb only turns capture on and off. The capture is not journalled and adds no verbclass. With capture on and off, the journal and every state root are the same.

**Render skipping** (hello `caps.render_skip = 1`). `step` and `wait` take `render_skip` (`"off"` | `"present"` | `"draw"`, default `"off"`) and `preview_every` (integer 0..3600, default 1; 0 = no preview read). Absent arguments are the behaviour before render skipping; a wrong type or value is `bad_args`. The host sends `capture.render_skip` and `capture.preview_every`; they are not in ENVP, the journal or the determinism key.
- Frame n is render-due (the rule above `wire_render_due` in wire.cpp) when any of these hold: `preview_every` > 0 and n % `preview_every` == 0; gate n or gate n+1 is on the capture grid; it is the last frame of a step, or the one before a captured last frame; a wait runs whose end is captured; the start gate; no command runs. The decision reads only the tick and the command's arguments, so a replay with the same arguments decides the same way. The host keeps `capture_every` and `capture_phase` fixed across commands.
- The real SwapBuffers always runs on the harness stack, inside an FP scope, with LastError kept, so the game stack is the same whether it runs or not. The game ignores its result (0x4048c1, 0x401cc2).
- `"present"`: a frame that is not render-due does not present. The HUD follows presents. A SwapBuffers that chains into anything but gdi32 or opengl32 is never skipped.
- `"draw"`: such a frame also skips `glDrawElements`, `glDrawArrays` and `glClear` whose arguments cannot raise `GL_INVALID_ENUM` or `GL_INVALID_VALUE`, outside list compilation, UpdateScreen, saves and other slots. Immediate mode and `glCallLists` still run. Opt-in: its exactness is shown only by an A/B run.
- The preview read (frames verb, view only) runs on preview ticks and on the frame a held gate shows; other ticks repeat the last frame (REPEAT).
- Guards fail closed. These are faults:
  - a `glReadPixels` on a draw-mode frame that is not render-due; the shim makes no driver call for it;
  - a front-buffer `glReadPixels` after a render-skipped present; the shim makes no driver call for it;
  - a `glCopyTex*` on a draw-skipped frame whose target a rendered draw binds, or that is not rewritten on a rendered frame or deleted before a captured gate;
  - a `glCopyTex*` into no named texture on a draw-skipped frame.
- A fault refuses the command at that gate, before its capture and tick push, with `state` and "render_skip stopped: <reason>. Set capture.render_skip to "present" or "off". A fresh owner is required." Every later execution verb gets the same reply. Tick pushes carry `render` counters.
- The baseline for comparing runs is the same build with `render_skip = "off"`. With `replay.present = "skip"` nothing presents, so `"present"` changes nothing there.

## 10. Wire protocol (D8)

**Envelope.** NDJSON over loopback TCP, UTF-8, one object per line, at most 1 MiB per line. The protocol is `kptas-wire/1`.

| Message | Shape |
|---|---|
| Request | `{"id":N,"verb":V,"args":{…}}` |
| Success reply | `{"id":N,"ok":true,"result":{…}}` |
| Error reply | `{"id":N,"ok":false,"error":{"code":C,"message":M}}` |
| Push (no id) | `{"push":P,…}` |

Error codes: `refused` (outside the tier allowlist, a tas restore root that is not admitted, or a tick asked for while the session is poisoned), `bad_args`, `unsupported`, `state` (wrong phase), `busy`, `internal`.

**Session order.** `hello`, `tier.lock`, `clock`, `ring.attach`, `capture`, `frames` (when `capture.frames` is on), `start`, then `step`, `wait` and the others.

**StepReport** is `{tick, epoch, server_passes, subticks, verbclass, state_root, journal, restore_point, reason, events[], outcome}`. The roots are hex strings, and `state_root` is null when nothing was captured. An event is `{name, tick}` plus `us`, `kind` and `slot` for `save.begin` and `save.done` (virtual µs the save advanced; `auto` for the autosave, `manual` for a save the player or the server admin asked for; the save slot, 0 for the quicksave) or `code` for `proc.down`; a bare name dates the event to the report's tick. A step the process did not survive answers with `outcome: exited`.

| Verb | Args → result | Tiers | Adds |
|---|---|---|---|
| `hello` | `{proto}` → `{proto, shim, shim_sha256, build, game, exe_sha256, text_sha256, epoch, modules[], hooks[], load_order[], seed_hooked, boundary{}, caps{}}`. `build` is the first 16 hex digits of the loaded DLL's sha256. With `shim.dll_sha256` set, Python refuses a build that does not match it, a build that is not 16 or more hex digits (older builds sent the game name there), and, for a 64-digit pin, a `shim_sha256` that differs; `game` names the address table (`k1_gog_103`). `boundary` is `{mode, sha256, gl_missing}` (§7). `caps` says what the shim virtualizes, plus `save_ms`, `restore_admit`, `frames` and `os_events[]` when it has those verbs, and `audio` (`{device, …}`) for the Miles device it serves. It also names the math it serves: `math` (`reference`, `native`, or `failed` with `math_error`), `math_sites`, `x87_cw`, `mxcsr`, and `portable[]` (`math` under reference). | lab, tas | — |
| `tier.lock` | `{tier}` → `{tier, allow[]}`; one-way, before `start` | lab, tas | — |
| `clock` | `{mode, hz, scale, qpf_hz, uptime0_ms, initial_realtime, hooked_modules[], budget_guard{}, spin{}}` → `{mode, us_per_tick, hooked[]}` | virtual: lab, tas; fixed, wall: lab | CLOCK; FIXED_CLOCK when fixed; none when wall |
| `ring.attach` | `{path, mb}` → `{capacity, write_pos}` | lab, tas | — |
| `capture` | `{enabled, tracker, classes[], sweep}` → `{classes:[{name, ranges[]}]}` | lab, tas | CAPTURE |
| `frames` | `{enabled, path, slots, slot_bytes}` → `{enabled, written, frames, repeat, blank, swaps, reads, skipped, finish_us_mean, …, error}`. It attaches to the frame ring (§9) or, with `enabled: false`, stops; any phase. `caps.frames` says the shim has it. | lab, tas | — |
| `start` | `{}` → StepReport for tick 0 | lab, tas | GATE |
| `gate` | `{on}` → `{held, tick}` | lab, tas | — |
| `input` | `{note, keys[], buttons[], mouse{abs\|rel}, wheel, msgs[]}` → `{for_tick}` | lab, tas | INPUT |
| `step` | `{n, first, note, keys[], buttons[], mouse{abs\|rel}, wheel, msgs[]}` → StepReport. `note` is the canonical note text the journal records; `first` is true on the note's first tick only (press edges, wheel, the absolute move); `msgs` carries the note's os event on its first tick. | lab, tas | INPUT, GATE, CLOCK |
| `wait` | `{event, arg, timeout, note, …input}` → StepReport + `{waited}`; only events after the wait began count | lab, tas | WAIT, GATE, CLOCK |
| `os_event` | `{ev: close\|altf4\|altf4@save}` → `{for_tick, outcome}`; at the start of the next tick, `close` sends WM_CLOSE to the game window and `altf4` sends the keyboard messages of a real Alt+F4; `altf4@save` holds those messages for the first message pump of the next save (UpdateScreen 0x401510) | lab, tas | OS_EVENT |
| `clock.save_ms` | `{ms}` → `{for_tick, ms}`: virtual length of every in-game save; an input, journalled | lab, tas | INPUT |
| `clock.save_ms_next` | `{ms}` → `{for_tick, ms}`: the next save only | lab, tas | INPUT |
| `digest` | `{regions[]}` → `{region: hex}` | lab, tas | CAPTURE |
| `snap` | `{kind: digest\|full\|semantic}` → `{tick, state_root, path?}` | lab, tas | CAPTURE |
| `restore` | `{tick, root, path}` → `{tick, pages, root, verified, poisoned, by, us}`. In tas a root neither captured by this process nor admitted is `refused` before anything is read or written; one that does not verify sets `poisoned` (C15). `{tick, root, check: true}` → `{admitted, by}` writes nothing. | lab, tas | RESTORE on the next tick |
| `restore.admit` | `{path, n, sha256}` → `{n, sha256}`: the file holds n strictly ascending 32-byte roots; once per process (`state` after that) | lab, tas | — |
| `gl.need` | `{}` → `{need[]}`: after a restore, the sha256 keys of the GL descriptors (textures, ARB programs, context state) that harness_game holds for T and the driver does not | lab, tas | RESTORE |
| `gl.apply` | `{path}` → `{rebuilt, deleted, missing, verify_bad, ok}`: rebuild from a set of descriptor groups, read each object back and check its key | lab, tas | RESTORE |
| `verify` | `{level, expect_root}` → `{root, ok}` | lab, tas | CAPTURE |
| `mem.read` | `{addr, len ≤ 65536}` → `{data_b64}` | lab, tas | READ |
| `mem.write` | `{addr, data_b64}` → `{}` | lab | WRITE |
| `semantic.read` | `{fields[]}` → `{values{}}` | lab, tas | READ |
| `semantic.apply` | `{fields{}}` → `{}` | lab | SEMANTIC |
| `watch` / `watch.rm` | `{id, addr, len, cond}` / `{id}` → `{}` | lab, tas | READ |
| `files` | `{op: snap\|put, path}` → `{sha256}`. `put` after `start` is lab only. | lab, tas | FILES_WRITE on put after start |
| `subscribe` | `{topics[]}` → `{}` | lab, tas | — |
| `frame` | `{path}` → `{w, h, sha256}` (M5) | lab, tas | SCREEN |
| `hud` | `{on, text}` → `{}`; the watermark cannot be turned off above lab (M5) | lab, tas | — |
| `bye` | `{exit}` → `{}` | lab, tas | — |
| `log` | `{geo, trace, site, from, to, xmode, xdump}` → `{}`; diagnostics only, it changes no game state and nothing journalled. `trace` bits: 1 large allocations and frees, 2 `rand` at `site` (0 = every site), 4 heap allocations, 8 calls to unhooked exe imports and extension functions (`xmode` 1 skips result-less GL calls, 2 skips Set/GetLastError, 4 only counts calls per tick); `from`..`to` bound the ticks; `xdump` writes the call records or counts to a file. Each traced line carries a stack-scan backtrace of exe return addresses. The trace path itself leaves other bytes in dead stack, which code that reads uninitialized stack can see, so a traced run may diverge where an untraced one does not. | lab, tas | — |

**Pushes.**
- `tick`: one per tick, when subscribed.
- `event`: `{name, tick, arg}`, for `main_menu`, `area.ready`, `load.done`, `save.begin`, `save.done`, `dialog.open`, `dialog.close`, `gui.open <panel>` and `proc.down`.
- `watch.hit`: this also holds the gate.
- `log` and `desync`.
- `exit`: `{tick, code, last_gate}`, once, when the game exits during tick `tick`. The step or wait that was running answers first, with `outcome: exited` and a `proc.down` event carrying `code`. That tick has no gate and no record; `last_gate` is the last tick that has.

hands-off has no wire. Its backend drives xdotool and x11grab.

## 11. Backend protocol and configuration

`kptas.types.Backend` defines these methods:
- lifecycle: `caps`, `attach`, `lock_tier`, `clock`, `start`, `close`;
- running ticks: `step`, `wait`;
- capture and state: `capture`, `records`, `restore`, `read`, `write`, `semantic`, `files`;
- other: `os_event`, `watch`, `unwatch`, `events`, `frame`.

A backend raises `BackendError` for a missing capability and `RefusedError` for a verb outside the locked tier.

| Backend | Ceiling | What it is |
|---|---|---|
| `pysim` | tas | Pure Python. Emits the same StagingRecords and page keys as the shim. |
| `simgame` | tas | SimGame.exe under Wine, running the real shim core. |
| `shim` | tas, or lab when the module set is not allowlisted | The game. |
| `handsoff` | hands-off | xdotool plus x11grab. No in-process code. |
| `phasor` | lab | Optional. Attaches to a game already running with Hyperstim, over its loopback NDJSON port. It watches status and events only: no input, no tick gate, no virtual clock, no capture or restore. |

Receipts from `pysim` and `simgame` carry a different determinism key, so they never count toward publishing a game run.

**Loading.** `kptas.config.load(path | None)` works in five steps:
1. parse with `tomllib`;
2. reject unknown keys and wrong types;
3. deep-merge over `DEFAULTS` (tables merge; lists and scalars replace);
4. resolve the tier-dependent `auto` values;
5. enforce the cross-key rules.

The tier-dependent `auto` values:

| Key | lab | tas | hands-off |
|---|---|---|---|
| `seek.mode` | restore | replay | replay |
| `clock.mode` | virtual | virtual | wall |
| `input.path` | dinput | dinput | os |
| `portable.profile` | `baseline-1` | `baseline-1` | native |
| `run.backend` | shim if `game.dir` is set, else pysim | shim if `game.dir` is set, else pysim | handsoff |
| `game.dll_overrides` | `dinput8=n,b;winepulse.drv=d;winealsa.drv=d;wineoss.drv=d` when the backend is shim, else empty | same as lab | empty |
| `audio.device` | virtual | virtual | real |
| `seek.restore_warmup` | none | none | none |

`auto` never picks `phasor`. With backend `phasor`, `seek.mode` resolves to replay, `clock.mode` to wall and `audio.device` to real.

Every default is shown below.

```toml
[run]
tier = "tas"                  # "lab" | "tas" | "hands-off"; a score header tier overrides
game = "k1"                   # "k1" | "k2" (k2 addresses unverified)
build = "auto"                # from the exe sha256
backend = "auto"              # "auto" | "pysim" | "simgame" | "shim" | "handsoff" | "phasor"
score = ""                    # "" = REPL authoring
out_dir = "~/.local/share/kptas/runs"   # bundles; local disk, never /tmp
max_ticks = 0                 # 0 = unlimited

[game]
dir = ""                      # directory holding swkotor.exe
exe = "swkotor.exe"
wine = "wine"                 # "" = native Windows
wineprefix = ""
dll_overrides = "auto"        # WINEDLLOVERRIDES; pinned. auto for the shim: dinput8=n,b and the host audio drivers off
display = "auto"
launch_wrapper = "none"       # "none" | "setarch_R"; a determinism-key input
pinned_env = true             # env -i: HOME, USER, LANG=C, PATH=/usr/bin:/bin, DISPLAY, WINE* and KPTAS_*
env = []                      # extra "NAME=value" entries; part of the determinism key
relaunch = true               # honour @relaunch
host = "auto"                 # "auto" | "local" | "host-spawn": auto uses host-spawn when wine is only on the host
pin = ""                      # directory copied over files.stage_paths before every launch (cold start)
prefix_pin = ""               # registry files copied into the wineprefix before every launch

[shim]
dll = "auto"                  # native/shim/build/dinput8.dll; copied into game.dir before every launch
dll_sha256 = ""               # when set, the dll's sha256 must start with it
listen = "127.0.0.1"          # Python listens; the shim connects
port = 0                      # 0 = ephemeral, passed as KPTAS_PORT
connect_timeout_s = 30
session_dir = "auto"          # $XDG_RUNTIME_DIR/kptas/session-0 for every run: ring and blob files
ring_mb = 64                  # staging ring; lives in the game's 2 GB address space
module_allowlist = ["system", "wine_builtin", "mss32", "binkw32", "miles_providers", "kptas_shim"]

[tick]
gate = "winmain_loop_head"
context = "auto"              # "auto" | "inline" (replay only) | "fiber" (needed for restore)
entry_trampoline = true       # CRT startup and WinMain on an arena stack (M2+)
forbid_mapped_gl = true       # a mapped GL buffer makes the tick not a restore point
max_subticks = 100000

[clock]
precision = "frame"            # default; "tick" selects 1000 Hz only if hz is omitted
mode = "auto"                 # "auto" | "virtual" | "fixed" (lab) | "wall"
hz = 60                       # virtual frame rate: a 60 Hz V-Sync machine, not an engine constant
scale = 100                   # engine timer scale; fixed mode only
qpf_hz = 10_000_000
uptime0_ms = 3_600_000        # virtual GetTickCount at tick 0; the startup srand seed
initial_realtime = "2004-02-12T00:00:00Z"
hooked_modules = ["exe", "mss32", "binkw32", "miles_providers"]
budget_model = "frozen"       # "frozen" | "scripted"
budget_guard = { site_reads = 100000, step_us = 1 }
spin_escape = "callsite"      # "callsite" | "count"
spin_count = { after_calls = 4000, step_us = 1 }

[seek]
mode = "auto"                 # "auto" | "replay" | "restore" | "hybrid" | "semantic"
                              # aliases: checkpoint, checkpoint_replay -> replay; exact, exact_tick, tick_restore -> restore
scope = "session"             # "session" | "rehydrate"
on_incompatible = "hybrid"    # "hybrid" | "replay" | "error"
fallback_chain = ["hybrid", "replay"]
keyframe_every = 600
materialize_every = 36000
restore_warmup = "none"       # "none" | "loads" | "auto" (= none). A restore needs no warm-up.
                              # loads (diagnostics): before a restore to K the process plays, checked like replay,
                              # through its first tick and the tick after the last load-screen (HOLD) tick <= K,
                              # unless it already played them, to tell a gap in the restored state from other faults.
hot_tier_mb = 512             # verified page, file, GL and tree bytes kept in RAM across bundle reopens
                              # (a rewind reopens its chain). Entries are keyed by content and checked
                              # against that key before they enter. 0 turns it off.
hot_maps = 32                 # resolved page maps kept per session

[replay]
origins = ["boot", "save"]
present = "skip"              # "skip" | "draw"; draws run unless capture.render_skip = "draw"
require_verify_level = "journal"   # "full_root" | "sim_root" | "journal"

[verify]
compare = "every_tick"        # judge replay: "every_tick" | "syncs" | "end"
stop_on_desync = true
fresh_process = true
rehash_write_set = true
full_sim_threshold_pages = 65536
full_sim_every_ticks = 3600
journal = true
continuation_ticks = "to_next_keyframe"   # or an integer
canary_every_seeks = 20
canary_ticks = 120
cross_launch = "auto"         # "auto" | "sim_root" | "journal"
mask_registry = ""            # absent qualification; or supported raw-root JSON/file
pointer_scan_every_ticks = 3600
pixel = "report"              # "report" | "require"
on_mismatch = "fallback"      # "fallback" | "halt"

[capture]
enabled = "auto"              # "auto" | true | false
tracker = "auto"              # "auto" | "write_watch" | "full_hash"
sim_classes = ["game_arena", "image_data", "harness_game"]
ambient_fingerprint = "sampled"   # "off" | "sampled" | "every_tick"
ambient_fingerprint_every_ticks = 60
max_record_lag_ticks = 120
frames = "off"                # "off" | "every_tick": the back buffer of each tick, read inside the game (§9)
frames_sink = "mkv"           # "mkv" (lossless FFV1, one file per epoch) | "png" (one file per tick)
frames_slots = 8              # frame ring slots; the gate waits when all are full
render_skip = "present"       # "off" | "present" | "draw": skip work on frames nobody sees (§9, render skipping)
preview_every = 4             # preview readback and HUD every N ticks; 0 = none; frames = "every_tick" forces 1

[arena]
base = "auto"                 # per-build table (k1_gog_103: 0x10000000, from M0) or a 64 KiB-aligned address
size_mb = 1024
chunks = "auto"               # or a list of addresses
write_watch = true
redirect_heaps = true
heap_impl = "rtl_create_heap_base"   # commit the header page first (M0) | "kptas_heap"
placement = "first_fit"
on_exhausted = "error"

[threads]
model = "auto"                # "auto" (slots if available, else join) | "slots" | "join" | "free"
slots = 16
order = "fifo_by_vtid"
hang_guard_ms = 10000
preload_miles_providers = true

[virtualize]
handles = true
tids = true
tls = true
sync = "virtual"
timers = "virtual"
getprocaddress = "thunk"
critical_section_debuginfo = "none"
registry = "virtual"          # "virtual" | "epoch_break" | "error"
external_names = "registry"

[messages]
real_pump = "gate_drain"
wndproc = "interpose"
external = "defwindowproc_journal"
internal = "pass_journal"
synthesize = "score_only"

[input]
path = "auto"                 # "auto" | "dinput" | "os"
dinput = "virtual_device"     # "virtual_device" | "passthrough" (lab)
buffered_timestamps = "virtual"
cursor = "virtual"
keystate = "virtual"
keymap = "auto"               # from swkotor.ini; pinned as $aliases
mouse = "abs"                 # "abs" | "rel"

[portable]                    # D12; every value pending census
profile = "auto"              # "auto" | "native" | a profile id
profile_file = ""             # "" = built-in table for the id
math = "reference"            # "reference" (deterministic CRT math) | "native"; in the key
x87_cw = 0x027F
mxcsr = 0x1F80
window = "800x600"            # virtual client size; must match the render resolution
refresh_hz = 60
total_phys_mb = 4096
avail_phys_mb = 2048
disk_free_mb = 20480
os_version = "6.1.7601"
locale = "en-US"
timezone = "UTC"
computer_name = "KPTAS"
user_name = "player"
random = "from_score"         # BCryptGenRandom, RtlGenRandom, CryptGenRandom
cpu_count = 1                 # SYSTEM_INFO and NUMBER_OF_PROCESSORS
cpu_level = 6
cpu_revision = 0x0D06
install_dir = 'C:\KotOR'      # the exe sees itself as install_dir\swkotor.exe

[files]
overlay_dirs = ["SAVES:", "GAMEINPROGRESS:", "CURRENTGAME:", "TEMP:"]
overlay_times = "virtual"
file_positions = "virtual"
store = true                  # shim: record the stage's writable files at every gate (TICK files_root)
stage_paths = ["Saves", "Temp", "currentgame", "gameinprogress", "logs", "Tempclient", "LocalVault",
               "DMVault", "ServerVault", "swkotor.ini"]

[gl]
driver = "native"             # real driver behind the profile: "native" | "llvmpipe"
names = "virtual"
retire = "defer"
retire_budget_mb = 256
content = "generation"        # "generation" | "shadow"
on_content_change = "report"  # "report" | "shadow" | "engine_refresh"
on_context_change = "engine_refresh"   # "report" | "engine_refresh"
shadow_budget_mb = 512
readback = "recorded"
query_returns = "journal"     # "journal" | "recorded"

[audio]
device = "auto"               # "virtual" (lab, tas) | "none" (Disable Sound=1, the M1/M2 key) | "real" (lab, hands-off)
provider = "software"         # "software" | "hardware" | "eax": which pinned 3D providers open
decode = "canonical"          # decoded waves in sim: right size and format, zeroed samples
output = "null"               # where the virtual device's sound goes; not in the key
shadow = false                # lab with device "real": log real Miles answers beside the model's
miles = "shim"                # "shim" | "lockstep"
status = "virtual"
callbacks = "gate_dispatch"
bink = "virtual_time"

[rng]
draw_journal = "callsite"     # "callsite" | "off"
cexorand = "observe"          # "observe" | "pin" (lab: a memory write)

[journal]
semantic = true
objects = "all_in_area"       # "all_in_area" | "party" | "none"
per_server_pass = false        # derived: false for clock.precision "frame", true for "tick"; an explicit value wins
per_subtick = false            # derived the same way
wndproc_log = true
gl_returns = true
audio_positions = true

[store]
codec = "auto"                # "auto" (zstd on Python >= 3.14, else zlib) | "zlib" | "zstd" | "none"
level = 3
pages = "every_tick"          # "every_tick" | "keyframes" | "digests" (replay only)
xor_delta = true
max_delta_chain = 32
batch_pages = 64              # pages per batch frame; 1 writes a frame per page
fsync = "keyframe"            # "never" | "keyframe" | "tick"
hot_ring_mb = 1024            # decoded-page cache in the Python process
ram_budget_mb = 2048
budget_gb = 18
spill_paths = []
on_budget_steps = ["spill", "evict_detached", "halt"]   # "thin" only if listed
detached_payload = "evictable"   # "evictable" | "keep"

[score]
include_dirs = ["lib"]
default_timeout_ticks = 1800

[env]
guard = "refuse"              # "refuse" | "warn"
pin = ["exe", "text", "chitin.key", "dialog.tlk", "override", "swkotor.ini", "keymap",
       "render", "wine", "prefix", "dll_overrides", "gl_driver", "modules", "shim", "profile", "host"]
extra_hashed = []

[determinism]
strict_key = true

[phasor]
port = 0                      # Hyperstim harness port; 0 = KOTORPHASOR_HARNESS_PORT

[handsoff]
input_tool = "xdotool"
capture = "x11grab"
capture_fps = 60

[sim]
seed = 0
pages = 256                   # PySim sim-set size
simgame_exe = "auto"

[hud]
enabled = false
watermark = true              # always on above lab

[gym]
obs = "fields"                # "fields" | "digest" | "pixels"
obs_fields = []
max_ticks = 0
```

**Cross-key rules.**
- **hands-off** requires all of: backend `handsoff`, seek `replay`, clock `wall`, input `os`, no `dinput8=n` override, profile `native`, and the watermark on.
- **Backend `handsoff`** is allowed only in tier hands-off.
- **Backend `phasor`** is allowed only in tier lab, with seek `replay` and clock `wall`.
- **tas** requires the `virtual` clock and `dinput` input. `semantic` seeks, `fixed` clocks, `passthrough` input, `cexorand = "pin"` and `watermark = false` are all lab only.
- **Clock scale.** `scale` ≠ 100 requires `fixed`. `fixed` requires scale·10⁶/hz < 2³¹.
- **restore and hybrid** refuse `threads.model = "free"`, `tick.context = "inline"` and `store.pages = "digests"`.
- **zstd** on Python < 3.14 is an error. zlib levels are 0–9, also when `auto` means zlib.
- **`capture.frames`** other than `off` needs backend `shim`.

## 12. Milestones (D9)

Receipts record which ticks were restore points. Nothing is claimed before it is measured, and every exit test runs first on PySim, then SimGame.exe, then the game.

- **M0: probes.** Each probe writes a receipt with the observed values under Wine 11.0.
  - **(a) Arena and write-watch.** Reserve a fixed base with `MEM_RESERVE|MEM_WRITE_WATCH` from `DllMain`. `GetWriteWatch(RESET)` must return exactly the written pages, including kernel-side `ReadFile` writes and pages written after a decommit and recommit. Also test `RtlCreateHeap` with a caller-supplied base.
  - **(b) Own context switch with SEH.** Switch stacks while updating the TEB NtTib fields. SEH and C++ exceptions must be raised and caught on an arena stack. `__chkstk` must work, and the x87 CW and MXCSR must be preserved.
  - **(c) dinput8 load order.** Record the ordering of: proxy `DllMain`, the entry detour, the first CreateThread (0x5ebf0d), the GetTickCount call feeding `srand` at 0x5f8d61, and WinMain. Also confirm that without the override, Wine's builtin dinput8 loads.
  - **Exit.** All three receipts exist for SimGame.exe and for K1 GOG 1.03.
  - **Results, K1 GOG 1.03 on Wine 11 staging (wow64).**
    - **(c) Load order: confirmed (D7).** The proxy's `DllMain` runs before the CRT and WinMain. The first time calls after it are:
      1. GetSystemTimeAsFileTime, for the CRT security cookie (return address 0x006FC599);
      2. GetTickCount (0x0070A9B5);
      3. QueryPerformanceCounter (0x0070A9C1).

      All three are on the main thread. The virtual clock is therefore installed in `DllMain` (§6), so the cookie and the `rand` seed are virtual too.
    - **(a) Arena: works.**
      - A fixed-base reservation of 1024 MB at 0x10000000 with `MEM_RESERVE|MEM_WRITE_WATCH` succeeds.
      - The exe is not large-address-aware, so user space is 2 GB. The only free range of 64 MB or more is 1944.9 MB at 0x0193A000, and the arena sits inside it.
      - `GetWriteWatch(RESET)` is exact across decommit and recommit.
      - Reset costs 0.80 ms, 1.27 ms and 3.90 ms for 1k, 10k and 100k pages. Watched first-touch adds about 1.3 µs per page.
    - **(a) Heap: works, with one rule.** `RtlCreateHeap` with a committed HeapBase inside the arena works, and `HeapAlloc` lands in the arena. A reserved-only HeapBase faults, so the heap's header page must be committed before the call.
    - **(b) Context switch: works.** The switch saves esi, edi, ebx, ebp, esp, the x87 CW and MXCSR, and updates TEB `fs:[0]`, `fs:[4]`, `fs:[8]` and `fs:[0xE0C]`. Three cases pass on the switched stack: an SEH frame resuming after an access violation, `RaiseException`, and a 256 KiB stack probe. A switch costs 14.3 ns.
    - **Not yet run.** Kernel-side `ReadFile` writes into watched pages; C++ exceptions on a switched stack; the builtin-dinput8 check without the override; SimGame.exe receipts; Windows parity.
- **M1: exact replay.** Shim loads early, gate at the WinMain loop head, tick counter, virtual clock, virtual DirectInput, message interposition, journal.
  - **Exit.** The same score, run twice in fresh processes from `boot` on one machine, gives byte-identical journals. The score covers the main menu, loading a pinned save and 3,600 ticks of play that include a module transition.
  - **Conditions.** `threads.model = "join"` (C2) makes the load reproducible. The run-varying host facts are pinned (§7 item 4). Movies are disabled in the pinned `swkotor.ini`, and the score avoids voiced dialog, because the voice-over wait (0x5edbc0) follows real Miles until M4.
  - The receipt lists no restore points, since nothing is captured yet.
- **M2: arena, slots, virtual handles, capture of every tick.** This adds:
  - the game arena, heap redirection and the placement allocator;
  - thread slots and the serial scheduler;
  - virtual handles, sync, timers, TLS and TIDs;
  - the files overlay;
  - ring capture every tick, the page store and state trees.
  - **Exit.**
    - The M1 exit still holds with every tick CAPTURED.
    - Every tick's map(T) resolves and matches its `state_root`.
    - A full sweep at every keyframe equals the incremental map.
    - Capture cost per tick is measured and recorded.
    - The state_root equality between the two launches is measured and reported, but not required. Measured on build 57cefdac: equal on every tick, also with another install path, session directory, user, time zone and CPU count.
- **M3: restore, verification, canaries.** This adds the fiber gate with context records, commit and real-side reconciliation, the verify levels, continuation canaries and the fallback chain.
  - **Exit.**
    - restore(T)+k equals the record (full_root and journal on every tick, k ≤ 600) for at least 100 sampled restore points in one session, including one across a load.
    - An injected fault ends in a halt with a first-divergent-field report.
    - Receipts list the restore-point ranges.
- **M4: Miles shim and GL virtual names.** This adds the Miles shim, Bink virtual time and GL name virtualization.
  - **Exit.**
    - The M1 exit holds on a score with voiced dialog and one movie.
    - A restore across voice-over works.
    - The render hash after a no-op restore matches, and `pixel_exact` is reported.
- **M5: HUD and OS input.** This adds the GL overlay HUD with the TAS watermark, chained through the SwapBuffers slot, and the hands-off backend.
  - **Exit.**
    - Every captured frame shows the tier and tick, taken from runtime state.
    - A hands-off run plays a score with xdotool and x11grab while the builtin dinput8 is verified, and writes a `best_effort` receipt with frame hashes at `@sync` points.
- **Portable track (D12, not gating).** Each §7 item lands once the census confirms it. Exit: the portable exit test in §7.

## 13. Open items

| ID | Open item | Probe that settles it |
|---|---|---|
| O1 | Settled by M0(c): `DllMain` runs before the CRT and WinMain. Still open: whether Wine's builtin dinput8 loads without the override (the hands-off check). | Launch once without `WINEDLLOVERRIDES`; list the loaded dinput8 |
| O2 | Settled by M0(a): the fixed base and write-watch reset, including across decommit. Still open: kernel-side `ReadFile` writes into watched pages. | `ReadFile` into a watched page, then `GetWriteWatch` |
| O3 | Settled by M0(a): `RtlCreateHeap` works with a committed header page. `kptas_heap` stays as a fallback only. | — |
| O4 | Settled by M0(b): the context switch, SEH, `RaiseException` and stack probing. Still open: C++ exceptions on a switched stack, and Windows parity. | Throw and catch across the switched stack, on Wine and on Windows |
| O5 | The loop-head PeekMessageA call site | Log the return addresses of every exe PeekMessageA call for 1,000 ticks |
| O6 | Whether UpdateScreen re-enters the server loop during loads | Count entries to 0x4babb0 and 0x401c10 per tick across ten module loads |
| O7 | Whether the RIM loader and async reader park after each job under `join`, without a lock held across `ResumeThread` | Trace Suspend, Resume and Wait of exe threads across ten loads; zero hang-guard trips |
| O8 | Settled on one host: the GenSeed detour zeroes the stack residue (§7), and the journals' CExoRand fields are equal across cold launches of the M1 exit score, also with another install path, user, time zone and CPU count. Still open: another Wine build. | Compare `[*(0x7a39e0)+0x10]` in two M1 exit journals on a second Wine build |
| O9 | Addresses of the five world timers, for `fixed` | Static read of SetFramesPerSecond 0x5ee4a0's callees |
| O10 | Hook sites for sync events. `save.*` = 0x4b3110 and 0x4b8300 entry and exit, with the caller and slot noted at SaveGame 0x4b58a0 entry; `load.done` = 0x4c5880 exit; the others are unknown. | Static probe for main-menu, area-ready, dialog and GUI-panel sites |
| O11 | Whether a Wine `CreateFileMapping` over a `Z:` file stays coherent with a Linux mmap | SimGame.exe ring round trip, 10⁶ records |
| O12 | Direct KUSER_SHARED_DATA (0x7ffe0000) reads, and GetProcAddress-resolved time or host APIs in the exe, mss32 and binkw32 | Static census before the M1 exit |
| O13 | Whether GOG Miles and Bink match the Steam DLLs that the thread census used | Hash compare; repeat the CreateThread scan if they differ |
| O14 | Portable items still open: CPUID and RDTSC sites, the tas boot refusal when the driver cannot back the GL identity, and the host addresses (GetProcAddress and wglGetProcAddress results, module handles, shim return addresses) in game memory. Item 3 (math) and the boundary of §7 are served. | The host-input census; a second Wine build for the addresses |
| O15 | How often the frozen-budget guard trips | Journal trip counts across the M1 exit run |
| O16 | IAT-slot conflicts with Hyperstim loaded (lab) | M1 run with `kphasor.dll` present; gate patch check |
| O17 | K1 Steam unpacked call sites, and all of K2 | Repeat O5–O12 per build after GOG passes |
| O18 | Settled on one host without a mask registry: the full state root is equal on every tick across cold launches (six launches of the M1 exit score on build 57cefdac). Still open: whether another Wine build or GL driver needs mask entries, and comparing roots across launches in the judge. | M2 two-launch page diff on a second Wine build and driver |
| O19 | Whether the CRT cookie routine (around 0x006FC599) also mixes the process and thread ids. If it does, the cookie in `.data` depends on the launch until virtual ids exist (M2) or a mask entry covers it. | Static read of the cookie routine |

### Raw-root comparison declaration (version 1)

`verify.mask_registry` may contain a JSON string or point to a UTF-8 JSON file. The first supported declaration is exactly `{"format":"kptas-root-qualification/1","root_kind":"full_root","page_map":"kptas-page-map/1","masks":[]}`. The parsed object is canonicalized before ENVP/key construction. Empty configuration means absent qualification. Duplicate or unknown fields, other models/versions, nonempty masks, malformed JSON and arbitrary legacy text are errors. No mask transform or qualified `sim_root` is implemented by this declaration.

This is comparison intent, not evidence of deterministic execution. A strict fresh judge requires the same qualified environment key and both CAPTURED/nonzero state_root and FILES/nonzero files_root at every selected comparison point. It compares raw memory roots and the separate files roots; absent coverage or inequality fails. EPOC and journal comparisons and restore-admission ancestry remain required. A qualified key mismatch refuses rather than degrading to journal-only comparison, including under a warning environment guard.

FULL_ROOT receipts are derived from actual comparisons. Their verifier states the selected scope (`every_tick`, `syncs`, or `end`) and actual root/files comparison counts. An end-only receipt is not every-tick proof. Without qualification, the journal policy remains JOURNAL. Pair diff/bisect use the same validation and coverage rules unless the caller explicitly requests `--no-roots`. Historical ENVP and receipts are immutable evidence and are never retroactively upgraded; a newly qualified reference must be recorded and judged.
