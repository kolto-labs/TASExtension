# InputInterfaceTelos contract, version 1

This contract covers InputInterfaceTelos (codename; project directory `KPTASInterface`), a TAS toolkit for Star Wars: KOTOR 1 and 2. It is the single source of truth, and every later module follows it. `kptas/types.py` implements its byte layouts and `kptas/config.py` implements its configuration schema. Both are checked against this document, and where they disagree, the code is wrong.

All integers are little-endian. All addresses are for K1 GOG 1.03 (`swkotor.exe`, 4,042,752 bytes, sha256 `9c10e0450a6eeca4…`) unless marked otherwise.

## 1. Decisions

| ID | Decision | Reason |
|---|---|---|
| D1 | The product is InputInterfaceTelos. The project directory is `KPTASInterface`, the import package is `kptas`, the CLI is `telos` and the distribution is `inputinterfacetelos`. | One name per layer, so none collides with KotorPhasor or KPM. |
| D2 | A holorecord is a directory bundle `<name>.holo/` (§8). It holds an append-only chunk journal, a content-addressed page store, materialized keyframe packs, a files overlay store and a disposable index. One TICK record identifies one complete state by page references plus a state root. | Framed chunks recover by scanning. Content addressing makes a per-tick state cheap. |
| D3 | There are three tiers: lab < tas < hands-off. hands-off is the strictest (§4). | The stricter the tier, the less the tool may touch. |
| D4 | Page identity is BLAKE2s with a 16-byte digest, computed in the shim and checkable with `hashlib.blake2s(digest_size=16)`. Tick records, chunks and the chain use sha256. The codec is zlib level 1 by default, zstd only on Python ≥ 3.14 (`compression.zstd`), or none. Python code is standard-library only. | Both hashes are in hashlib, and BLAKE2s is fast on 32-bit x86, where the shim runs. |
| D5 | Clock modes are `virtual` (the default for tas and lab), `fixed` (lab only) and `wall` (forced in hands-off). `hz` defaults to 60 and models a 60 Hz V-Sync machine. A tick is one WinMain pass. | The engine has no fixed step. Its game time is a scaled wall-clock delta (§2). |
| D6 | There is one TOML schema, and every key is optional (§11). | No run should need configuration to start. |
| D7 | Native code lives in `KPTASInterface/native/shim`. It is freestanding C built with `zig cc` for x86-windows-gnu and loads as a `dinput8.dll` proxy (§6). | The exe imports dinput8 (IAT 0x73d000), so the proxy initializes before the exe's CRT and WinMain. M0 confirmed this order on Wine 11 staging (wow64) with K1 GOG 1.03 (§12). |
| D8 | The wire verbs are listed in §10. There is no rng verb. | RNG state is ordinary memory (`_holdrand` at `_tiddata+0x14`). |
| D9 | Milestones M0–M5 are in §12. | Each milestone measures before it builds and has an exit test. |
| D10 | BioWare's own save and load are untouched. Save teleport, AMG and hotshots stay in-game actions. | The tool records play. It does not replace game mechanics. |
| D11 | There are two sim backends: PySim (pure Python) and SimGame.exe (built with zig, linking the real shim core, run under Wine). | Both let the Python and native code be tested headlessly. |
| D12 | Portable determinism. The shim presents a fixed virtual machine boundary, named by a profile id that goes into HOLO and the determinism key (§7). CRT math defaults to a deterministic reference implementation. Every item is pending the host-input census. | A record made on one machine must replay identically on any machine, under Wine or on Windows. |
| C1 | Python listens on loopback. The shim connects at boot, using `KPTAS_PORT` and `KPTAS_SESSION` from its environment. `KPTAS_CLOCK` carries the clock origin that `DllMain` needs before any connection exists. | Python owns the process lifetime, and each connection starts a new epoch. |
| C2 | Until thread slots exist (M1), `threads.model = "join"`. When the exe resumes one of its own worker threads, the hooked `ResumeThread` returns only after that thread parks again. | Loads and queued reads then depend only on the score, before the M2 scheduler exists. |
| C3 | Every chunk after ENVP is chained, with its 4cc in the hash. | Marks, epochs, receipts and the seal become tamper-evident too. |
| C4 | A recording's journal holds no wall-clock time and no random id. The bundle id is the first 32 hex digits of H_0. Only seeks and verification append RCPT chunks, which may carry measured latency; recording never does. | Two identical recordings then produce byte-identical journals, which is the M1 exit test. |
| C5 | Store frames use CRC-32 (`zlib.crc32`), not CRC-32C. | CRC-32C is not in the standard library, and content keys verify decoded bytes anyway. |
| C6 | Each tick's semantic journal entry sits inline in its TICK record. | One TICK body then describes and verifies its tick on its own. |
| C7 | The framed TICK chunk is the commit point. The superblocks from the tick-state revision 2 layout are dropped. | A chunk with a valid digest already marks a durable end. |
| C8 | The default tier is tas, with seek `replay`. lab has to be asked for. | tas writes no memory except recorded bytes put back through restore, so it only returns to states the game was already in. The compiler leaves an omitted header tier unset, so `run.tier` decides, and the tier that counts is still the computed one (SEAL). |
| C9 | tas requires an allowlisted module set from tick 0, and every restore checks it again. | Any other in-process module can write game memory without appearing in the verbclass log. |
| C10 | CExoRand is observed in the journal. It is never pinned above lab. | It only varies ambient sound timing, and pinning it is a memory write. |
| C11 | Memory reads (`mem.read`, `watch`, `semantic.read`) are allowed in tas. | They observe without changing state. |
| C12 | Revision 2's TOML is adapted: `[sched]` and `[io]` fold into `[threads]`, `[kernel]` becomes `[files]`, `[host]` moves into `[portable]` and `[tick_store]` becomes `[store]`. The Linux helper, `[runtime]` and the hash and codec knobs are removed. | D4, D7 and D12 fix those choices. |
| C13 | Behind a portable profile, the determinism key covers only the game-side inputs. The host fingerprint (Wine version, prefix, real GL driver, DLL overrides, launch wrapper, other modules) is recorded but joins the key only with profile `native`. | One profile must give one key on Wine and on Windows, or D12's cross-machine replay could never compare roots. |

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
| Save: StallEventSaveGame, called from the server MainLoop (0x4bafb1). It runs on the main thread and blocks. | 0x4b3110 |
| The dialog voice-over wait asks the real Miles stream (IsSoundPlayingInDialog) | 0x5edbc0 |
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
3. Replay the embedded SCOR.
4. Compare with the submitted run. Two values are compared:
   - `journal_sha256`, on every tick (or at `@sync` points, per `verify.compare`);
   - `state_root`, only when both runs captured, share the determinism key, and the mask registry qualifies `sim_root` for cross-launch use.
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
| restore | At a gate, compute W, the pages that differ between the live map and map(T). Reconcile commit state, write T's files tree and W, reconcile the real side (handles, GL, Miles, cursor), then verify. There is no replay of the target's ticks; with `seek.restore_warmup = loads` a process that has not run the first tick and T's area load plays those first. | lab, tas | `full_root` + journal + continuation canary |
| hybrid | Restore the latest keyframe K ≤ T that is a restore point (the latest restore point when there is no such keyframe), then replay K..T. | lab, tas | `full_root` |
| semantic | Write back journalled engine fields. The branch is tainted. | lab | `diff_only` |

Rules:
- A restore never crosses an EPOC. Reaching a tick in another epoch resets the files overlay to that epoch's starting `files_root` (FILES_RESTORE) and replays from the epoch's start.
- On a mismatch, each level of `seek.fallback_chain` is tried once. If the last level also fails, the run halts with a first-divergent-field report giving the journal field, page, symbol and key diff. It never loops.
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

## 6. Native shim (D7)

```
KPTASInterface/native/
  shim/core/    gate, clock, input, ring, wire, alloc, blake2s, iat, hooks
  shim/proxy/   dinput8 exports; builds build/dinput8.dll
  simgame/      SimGame.exe; links shim/core statically
```

**Build.** `zig cc -target x86-windows-gnu -ffreestanding -nostdlib -shared`, linked only against the import libraries for kernel32, user32 and ws2_32. There is no CRT: the shim has its own `memcpy`, `memset` and BLAKE2s.

**Load.** `dinput8.dll` sits in the game directory. Under Wine it loads only with `WINEDLLOVERRIDES=dinput8=n,b`, which is part of the environment pin. The loader binds the exe's imports and runs the proxy's `DllMain(PROCESS_ATTACH)` before the exe's entry point.

`DllMain` runs under the loader lock. It does four things:
- reads the clock origin (`qpf_hz`, `uptime0_ms`, `initial_ft`, `hz`, mode) from `KPTAS_CLOCK`, which Python sets at launch. `initial_ft` is `clock.initial_realtime` as a Windows FILETIME;
- reserves memory: `harness_core`, and from M2 the arena (1024 MB at 0x10000000 for k1_gog_103);
- patches the exe's IAT slots, so the virtual clock is live at V = 0 before the CRT's first time call;
- writes a 5-byte detour at the exe's entry point.

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

**Input observation receipt (wire-only v1).** For each subscribed `tick` push, the shim emits `input_observation` with the gate's tick number, staged buffered-record count and overflow count, then per-device `GetDeviceState` poll counts/nonzero-poll counts and `GetDeviceData` poll, exposed, dequeued and peeked counts. `records_dequeued` proves only that the virtual device queue removed those records during the game process's DirectInput call; it does not prove the game's input layer or gameplay acted on them. `state_nonzero_polls` proves the successful state call returned at least one nonzero byte. Empty polls remain distinguishable from calls that returned input. This telemetry is outside `semantic/1`, so it does not alter existing journal digests or replay compatibility; it is currently a live wire receipt and is not persisted into the holorecord. Durable per-tick evidence requires a versioned journal extension and corresponding reader/writer support before it can qualify engagement.

**Engagement qualification.** No `engagement_qualified` producer exists in this tracked shim. DirectInput call counters alone cannot qualify engagement: the runtime must bind the intended score/input interval to each gate, observe the specific key/button state or buffered transition returned to the game's input API, and report the matching gate/tick/session with dropped, peeked and empty-poll cases kept distinct. The current receipt establishes API delivery/consumption boundaries, not successful game-level action; do not turn it into an engagement ACK.

**Allocator.** `harness_core` is a private region reserved at boot. The shim never allocates from the process heap or the game heap.

**Channel.** Winsock loopback TCP carrying NDJSON (§10). Bulk bytes travel through files in the session directory.

**Staging ring.** The shim calls `CreateFileMapping` on a session-directory file (a `Z:` path under Wine) that the Python process also mmaps (§9, O11).

**Optional KotorPhasor integration.** KotorPhasor is a separate optional component, not a prerequisite. If a qualified run uses it, the shim never patches its slots; SwapBuffers (0x73d00c) remains KotorPhasor's overlay hook when present, and the M5 HUD chains through the pointer it owns. Each gate checks shim-patched bytes, and a changed slot halts the run with a report. Restore refuses while a module outside `shim.module_allowlist` that allocates from game-visible heaps is loaded; TAS tier requires the allowlist from tick 0 (C9).

**KPM exclusion.** Kotor Patch Manager is not a supported loader or co-loader. Telos/Stasis must not load it, chain its hooks, advertise compatibility, or silently continue when a conflicting KPM installation is reliably identified. Refuse the Stasis session before activation, preserve all KPM files, and report unknown when detection is inconclusive. This current product constraint supersedes historical coexistence language; it does not change the optional KotorPhasor boundary above.

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

Every item below is **pending the host-input census that is now running**. The built-in profile table ships only values the census has confirmed.

1. **GPU identity** (needs GL virtual names from M4).
   - What the game sees: `glGetString` vendor, renderer, version and extensions, and every GL query whose result enters game memory, return profile values.
   - Boot check: the real driver must support every advertised extension. If it does not, tas refuses to start and lab warns. Profiles advertise a conservative set.
   - Why it matters: extension probing picks render paths, and render code draws from the gameplay `rand` (emitters, area updates, grass reseed).
2. **CPU identity.** CPUID call sites, taken from a static site list, are patched to return profile values. RDTSC sites, if the census finds any, read the virtual clock.
3. **Math.**
   - The x87 control word (default 0x027F) and MXCSR (default 0x1F80) are set at boot and at every game-thread start.
   - `math = "reference"` is the default, on every machine. It replaces the CRT transcendental routines the census identifies (sin, cos, tan, atan2, exp, log, pow, and sqrt if the census finds an inexact path) with a deterministic software implementation, so results do not depend on the CPU.
   - `math = "native"` keeps the CRT routines, which can differ between CPUs. It stays available as an option.
   - The math mode is part of the determinism key, because the two modes can give different results on some CPUs.
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
| HOLO | `Header` JSON (fields as in `kptas.types.Header`; `start` is `boot` or `save:<sha256>`) | in H_0 |
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
| 124 | 32 | input_sha256: SHA-256 of the staged input frame, or all zeroes when the backend cannot provide that digest |
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

The native shim retains its original 16-byte BLAKE2s input hash as `in`; adding fields to `semantic/1` would change historical journal bytes and digests. The full SHA-256 is carried separately on each subscribed wire `tick` push as `input_sha256`, keyed by its `epoch` and `tick`, and copied only to the matching TICK v1 record. Multi-tick reports preserve each per-gate receipt independently. A missing receipt (including older shim versions) remains all zeroes; the shorter `in` digest and authored score note are not substitutes. Restore batches do not contribute input digests. The original journal bytes remain available for provenance and replay comparison.

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
| 4 | 1 | kind: 1 PAGE_I, 2 PAGE_X, 3 LEAF, 4 ROOT, 5 FILE_BLOB, 6 FILE_TREE |
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

**Page storage:**
- A PAGE_X payload is the page XOR its base page.
- XOR chains are anchored at materialized packs and are at most `store.max_delta_chain` long.
- The zero page (key `c3f725e0c29f69cd616a0fe9b762bcb5`) is never stored.

### 8.8 Keyframes, index, commit order

- **Materialized pack.** `keyframes/kf-<tick>.pack` holds ROOT, then the LEAFs, then a PAGE_I frame for every non-zero page, in ascending vaddr. One sequential read restores the image. It is written every `seek.materialize_every` ticks and at every EPOCH_START.
- **Index.** `index/` has a private, versioned format. Deleting it is always safe.
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

## 10. Wire protocol (D8)

**Envelope.** NDJSON over loopback TCP, UTF-8, one object per line, at most 1 MiB per line. The protocol is `kptas-wire/1`.

| Message | Shape |
|---|---|
| Request | `{"id":N,"verb":V,"args":{…}}` |
| Success reply | `{"id":N,"ok":true,"result":{…}}` |
| Error reply | `{"id":N,"ok":false,"error":{"code":C,"message":M}}` |
| Push (no id) | `{"push":P,…}` |

Error codes: `refused` (outside the tier allowlist), `bad_args`, `unsupported`, `state` (wrong phase), `busy`, `internal`.

**Session order.** `hello`, `tier.lock`, `clock`, `ring.attach`, `capture`, `start`, then `step`, `wait` and the others.

**StepReport** is `{tick, epoch, server_passes, subticks, verbclass, state_root, journal, restore_point, reason, events[], outcome}`. The roots are hex strings, and `state_root` is null when nothing was captured. An event is `{name, tick}` plus `us` for `save.begin` and `save.done` (virtual µs the save advanced) or `code` for `proc.down`; a bare name dates the event to the report's tick. A step the process did not survive answers with `outcome: exited`.

| Verb | Args → result | Tiers | Adds |
|---|---|---|---|
| `hello` | `{proto}` → `{proto, shim, shim_sha256, build, game, exe_sha256, text_sha256, epoch, modules[], hooks[], load_order[], seed_hooked, caps{}}`. `build` is the first 16 hex digits of the loaded DLL's sha256, and Python refuses a build that does not match `shim.dll_sha256`; `game` names the address table (`k1_gog_103`). `caps` says what the shim virtualizes, plus `save_ms` and `os_events[]` when it has those verbs. | lab, tas | — |
| `tier.lock` | `{tier}` → `{tier, allow[]}`; one-way, before `start` | lab, tas | — |
| `clock` | `{mode, hz, scale, qpf_hz, uptime0_ms, initial_realtime, hooked_modules[], budget_guard{}, spin{}}` → `{mode, us_per_tick, hooked[]}` | virtual: lab, tas; fixed, wall: lab | CLOCK; FIXED_CLOCK when fixed; none when wall |
| `ring.attach` | `{path, mb}` → `{capacity, write_pos}` | lab, tas | — |
| `capture` | `{enabled, tracker, classes[], sweep}` → `{classes:[{name, ranges[]}]}` | lab, tas | CAPTURE |
| `start` | `{}` → StepReport for tick 0 | lab, tas | GATE |
| `gate` | `{on}` → `{held, tick}` | lab, tas | — |
| `input` | `{note, keys[], buttons[], mouse{abs\|rel}, wheel, msgs[]}` → `{for_tick}` | lab, tas | INPUT |
| `step` | `{n, first, note, keys[], buttons[], mouse{abs\|rel}, wheel, msgs[]}` → StepReport. `note` is the canonical note text the journal records; `first` is true on the note's first tick only (press edges, wheel, the absolute move); `msgs` carries the note's os event on its first tick. | lab, tas | INPUT, GATE, CLOCK |
| `wait` | `{event, arg, timeout, note, …input}` → StepReport + `{waited}`; only events after the wait began count | lab, tas | WAIT, GATE, CLOCK |
| `os_event` | `{ev: close}` → `{for_tick, outcome}`; WM_CLOSE reaches the game window at the start of the next tick | lab, tas | OS_EVENT |
| `clock.save_ms` | `{ms}` → `{for_tick, ms}`: virtual length of every in-game save; an input, journalled | lab, tas | INPUT |
| `clock.save_ms_next` | `{ms}` → `{for_tick, ms}`: the next save only | lab, tas | INPUT |
| `digest` | `{regions[]}` → `{region: hex}` | lab, tas | CAPTURE |
| `snap` | `{kind: digest\|full\|semantic}` → `{tick, state_root, path?}` | lab, tas | CAPTURE |
| `restore` | `{tick, root, path}` → `{tick, pages, root, verified, us}` | lab, tas | RESTORE |
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
| `log` | `{geo}` → `{}`; diagnostics only, it changes no game state and nothing journalled | lab, tas | — |

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
| `phasor` | lab | Optional. Attaches to a game already running with KotorPhasor, over its loopback NDJSON port. It watches status and events only: no input, no tick gate, no virtual clock, no capture or restore. |

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
| `game.dll_overrides` | `dinput8=n,b` when the backend is shim, else empty | same as lab | empty |

`auto` never picks `phasor`. With backend `phasor`, `seek.mode` resolves to replay and `clock.mode` to wall.

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
session_dir = "auto"          # $XDG_RUNTIME_DIR/kptas/<run>: ring and blob files
ring_mb = 64                  # staging ring; lives in the game's 2 GB address space
module_allowlist = ["system", "wine_builtin", "mss32", "binkw32", "miles_providers", "kptas_shim"]

[tick]
gate = "winmain_loop_head"
context = "auto"              # "auto" | "inline" (replay only) | "fiber" (needed for restore)
entry_trampoline = true       # CRT startup and WinMain on an arena stack (M2+)
forbid_mapped_gl = true       # a mapped GL buffer makes the tick not a restore point
max_subticks = 100000

[clock]
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
restore_warmup = "auto"       # "auto" | "loads" | "none"; auto = loads for the shim, none otherwise.
                              # loads: before a restore to K the process plays, checked like replay, through its
                              # first tick and the tick after the last load-screen (HOLD) tick <= K, unless it
                              # already played them. The game keeps state outside the sim set that those ticks build.

[replay]
origins = ["boot", "save"]
present = "skip"              # "skip" | "draw"; draws always run
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
mask_registry = ""            # "" = none; sim_root stays unqualified
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
miles = "shim"                # "shim" | "lockstep"
status = "virtual"
callbacks = "gate_dispatch"
output = "native"             # "native" | "pipe" | "null"; output only, never mute code paths
bink = "virtual_time"

[rng]
draw_journal = "callsite"     # "callsite" | "off"
cexorand = "observe"          # "observe" | "pin" (lab: a memory write)

[journal]
semantic = true
objects = "all_in_area"       # "all_in_area" | "party" | "none"
per_server_pass = true
per_subtick = true
wndproc_log = true
gl_returns = true
audio_positions = true

[store]
codec = "zlib"                # "zlib" | "zstd" (Python >= 3.14) | "none"
level = 1
pages = "every_tick"          # "every_tick" | "keyframes" | "digests" (replay only)
xor_delta = true
max_delta_chain = 32
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
       "render", "wine", "prefix", "dll_overrides", "gl_driver", "modules", "shim", "profile"]
extra_hashed = []

[determinism]
strict_key = true

[phasor]
port = 0                      # KotorPhasor harness port; 0 = KOTORPHASOR_HARNESS_PORT

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
- **zstd** on Python < 3.14 is an error. zlib levels are 0–9.

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
    - The state_root equality between the two launches is measured and reported, but not required.
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
| O8 | Whether CExoRand is reproducible across launches (GenSeed reads uninitialised stack) | Compare `[*(0x7a39e0)+0x10]` in the two M1 exit journals |
| O9 | Addresses of the five world timers, for `fixed` | Static read of SetFramesPerSecond 0x5ee4a0's callees |
| O10 | Hook sites for sync events. `save.*` = 0x4b3110 entry and exit; `load.done` = 0x4c5880 exit; the others are unknown. | Static probe for main-menu, area-ready, dialog and GUI-panel sites |
| O11 | Whether a Wine `CreateFileMapping` over a `Z:` file stays coherent with a Linux mmap | SimGame.exe ring round trip, 10⁶ records |
| O12 | Direct KUSER_SHARED_DATA (0x7ffe0000) reads, and GetProcAddress-resolved time or host APIs in the exe, mss32 and binkw32 | Static census before the M1 exit |
| O13 | Whether GOG Miles and Bink match the Steam DLLs that the thread census used | Hash compare; repeat the CreateThread scan if they differ |
| O14 | Portable items 1–5: CPUID and RDTSC sites, GL queries, host facts, CRT math paths, profile values | The host-input census now running |
| O15 | How often the frozen-budget guard trips | Journal trip counts across the M1 exit run |
| O16 | IAT-slot conflicts with KotorPhasor loaded (lab) | M1 run with `kphasor.dll` present; gate patch check |
| O17 | K1 Steam unpacked call sites, and all of K2 | Repeat O5–O12 per build after GOG passes |
| O18 | `sim_root` across launches (mask registry) | M2 two-launch page diff |
| O19 | Whether the CRT cookie routine (around 0x006FC599) also mixes the process and thread ids. If it does, the cookie in `.data` depends on the launch until virtual ids exist (M2) or a mask entry covers it. | Static read of the cookie routine |
