# Glossary

## Tool terms

**Tick.** The TAS boundary at the outer `WinMain` message-pump gate when no Windows message is waiting. It does not guarantee one rendered image: a later `SwapBuffers` call may repeat an image or present a blank one. Frame precision uses the 60 Hz default virtual-clock rate; tick precision uses 1000 Hz by default. Changing `hz` changes virtual-time resolution, not how often the game reaches the gate. Actual cadence and input alignment remain profile-specific and unqualified until measured.

**Score.** A plain-text file of notes and directives that says what to press on every tick. Same score, same game. See [score.md](score.md).

**Note.** One score line: a tick count, then every input held for those ticks. `20  w lmb rmb` holds W and both mouse buttons for 20 ticks. `.` means nothing is held.

**Sync point.** A score line, `@sync EVENT`, that waits for an engine event such as `area.ready` or `save.done` instead of counting ticks. A judge replay can compare runs at sync points only.

**Holorecord.** The recording of a run: a directory `<name>.holo/` with a hash-chained journal, a page store, keyframes and an index. One TICK record per tick. See [holorecord.md](holorecord.md).

**Keyframe.** A tick whose whole memory map is stored, not just the pages that changed. Seeking starts from the nearest one.

**Greenzone.** The ticks that have a stored state, so going back to them is a restore, not a replay. The name comes from older TAS editors. `telos show` marks them `c`.

**Branch.** A new bundle that continues another from one of its ticks. It stores only what comes after that tick and reads the rest from its parent.

**Desync.** Two runs of one score that stop matching. `telos diff` and `telos bisect` name the first tick and field where they split, and a judge replay fails on it.

**Tier.** How much the tool touched the game: lab, tas or hands-off. tas, the read-only one, is the default. The tier is computed from the run, not taken from the header. See [tiers.md](tiers.md).

**Virtual clock.** The time the game is shown, computed from the tick count instead of read from the machine. At 60 Hz each tick is 1/60 s with the remainder carried, so 60 ticks are exactly 1,000,000 µs. QueryPerformanceCounter, GetTickCount and the system time all read from it. PySim runs on it today; in the game it is part of M1.

**Reference math.** Software sin, cos, tan, atan2, exp, log and pow built only from operations SSE2 performs exactly, so the output shouldn't depend on the CPU. Checked so far: an i386 build under Wine and an x86_64 build give the same bytes, on one host. The game's own x87 math can differ between CPUs. The KotOR I exe has 105 x87 math sites, and under `portable.math = "reference"`, the default, the shim points every one of them at the reference versions. On one host the two agree within 1 ulp for game-sized arguments (angles within ±64 rad). sin, cos and tan are the exception near their zeros and poles, and at large arguments (around 1e18) every bit differs, because x87 reduces with a 66-bit pi. The M1 exit score runs 24 of the 99 game sites and all three CRT entries, some of them over a million times. Over its first 1000 ticks, the switch changed no journal field.

**Epoch.** One game process, from launch to exit. Alt-F4 ends one, and a relaunch starts the next.

**Restore point.** A captured tick where the whole state can be put back. A tick in the middle of a load, for example, is not one.

**State root.** A sha256 tree over every stored page at a tick. Two ticks with the same root have the same stored memory.

**Determinism key.** A hash of everything a run depends on: game files, modules, clock origin, portable profile. Runs with different keys are never compared by root.

**Receipt.** A measured result, written into the bundle: a seek, a verify or a judge replay, with what was checked and whether it passed.

## Speedrun terms

These come from the KotOR speedrunning community, whose guides are at <https://kotor-speedruns.github.io/kotor1/>. The names, and years of routing and glitch research behind them, are theirs. The short versions below are paraphrased; the linked guides are the source.

The tool does these the way a player does, with inputs. It never changes the game to make them easier.

**AMG, Anywhere Menu Glitch.** Alt-F4 during a save or load screen, then unpause. The quit dialog stays up while you keep limited control, including during cutscenes. Most Unrestricted glitches start from it. In a score it is `!altf4@save`, on or before the note that saves. [Guide](https://kotor-speedruns.github.io/kotor1/Major%20Glitches/Anywhere%20Menu%20Glitch)

**DLZ, Displaced Load Zone.** Setting off a trigger, usually a load transition, from a distance. A party member must stand on the negative-y side of the trigger with an x coordinate exactly equal to a corner of the trigger area, to the bit. Position only changes once per frame, so the right value is easy to step over. [Guide](https://kotor-speedruns.github.io/kotor1/Major%20Glitches/Displaced%20Load%20Zone)

**Save teleport.** Hold both mouse buttons to run, then quick save. You move forward by a distance that depends on how long the save takes. Chained, it is fast movement, and it can hop over triggers. [Guide](https://kotor-speedruns.github.io/kotor1/Techniques/Save%20Teleporting)

**Save buffer.** Triggers do not fire during the short fade-in after a quick save. Quick save repeatedly while running into a trigger, then load or use an instant item or power, and the trigger is cancelled. [Guide](https://kotor-speedruns.github.io/kotor1/Techniques/Save%20Buffering)

**Hotshot.** An application of AMG that loads a save into a different module than the one it was made in, warping you to a module you have already visited. The main character comes back with the stats and gear they had when last there. [Guide](https://kotor-speedruns.github.io/kotor1/Major%20Glitches/Hotshot)

**Wired targeting.** Right after a load, the old target's object id is briefly still live. Default action then goes to whatever object in the new module has that id, even a non-hostile one far away. [Guide](https://kotor-speedruns.github.io/kotor1/Techniques/Wired%20Targeting)
