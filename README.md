# KPHarness

A harness and TAS toolkit for Star Wars: Knights of the Old Republic I and II.

The TAS part is KPTASInterface, codename InputInterfaceTelos. Every tick is a savestate, scores are plain text, and the same score gives the same game on any machine. Today that holds in a stand-in game. The real game is being wired up now; status is below.

**Try it in a browser:** <https://kptas-demo.bocloud.workers.dev>. The game there is a small stand-in, not KotOR, so the tools can be tried without an install.

## Quickstart

Python 3.11 or newer, standard library only. No game needed. From the repo root:

```sh
PYTHONPATH=KPTASInterface python3 -m kptas score check KPTASInterface/examples/pysim_walk.score
PYTHONPATH=KPTASInterface python3 -m kptas run KPTASInterface/examples/pysim_walk.score --backend pysim --out a.holo
PYTHONPATH=KPTASInterface python3 -m kptas run KPTASInterface/examples/pysim_walk.score --backend pysim --out b.holo
PYTHONPATH=KPTASInterface python3 -m kptas show a.holo --from 35 --to 40
PYTHONPATH=KPTASInterface python3 -m kptas diff a.holo b.holo
```

What comes back (the `show` header is cut here):

```
ok KPTASInterface/examples/pysim_walk.score: 13 items, 82 note ticks, 1 files, sha256 5951ac5a9d915346  [game=k1 build=auto tier=lab hz=60 start=boot]
a.holo: ticks 0..82, 1 epoch(s), stopped at end
b.holo: ticks 0..82, 1 epoch(s), stopped at end
...
tick  ep  verbs  flags  reached   sp  sub  journal   root      note
35    0   IGCP   cr     recorded  1   0    32726895  187f0728  .
36    0   IGCP   cr     recorded  1   0    d1327c9b  85efdc61  .
37    0   IGCP   cr     recorded  1   0    21f8e36d  a92800c9  a
38    0   IGCP   cr     recorded  1   0    06b3c544  992ce2f2  a
39    0   IGCP   cr     recorded  1   0    6d8b0014  5a19db92  a
40    0   IGCP   cr     recorded  1   0    356272bd  4a37cbf2  a
no divergence (journal only; roots not compared)
```

Two runs, one score, no difference. `cmp a.holo/journal b.holo/journal` prints nothing: the two journals are the same bytes. Change `12  a` to `12  d` in a copy of the score, run it, and `diff` says `first divergence at tick 37: note`.

That is proven on PySim, the pure-Python stand-in. Proving it in the game is milestone M1.

`run` will not overwrite a bundle. Delete `a.holo` and `b.holo` before you run the block again.

## What works today

| Piece | Status |
|---|---|
| Score language: parser, `fmt`, compiler, K1 keymap | done |
| Design contract ([contract.md](KPTASInterface/docs/design/contract.md)) | done |
| Python core: holorecord store, backends including PySim, the `telos` CLI | done |
| Reference math for sin, cos and friends | done |
| M0 probes under Wine: early load, fixed arena, stack switch | done |
| Browser prototype | done |
| M1: exact replay in the real game | in progress |
| M2, M3: every tick captured, restore to any tick in the game | planned |
| M4: audio and GL on virtual time | planned |
| M5: HUD and hands-off input | planned |
| KotOR II | planned |

Estimates, milestones and the honest odds are in [ROADMAP.md](ROADMAP.md).

## Rules

- Glitches are never fixed. The tool never patches engine logic.
- BioWare's save and load are untouched. Save teleports, AMG and hotshots stay in-game actions.
- A run made with this tool is a TAS. It is never an RTA submission, whatever its tier.
- The tier (lab, tas or hands-off) is stamped on every file and on screen. On file it is in the score header, the bundle header and its seal. On screen it is the HUD watermark, which lands with M5 and cannot be turned off above lab.

## Docs

- [KPTASInterface/README.md](KPTASInterface/README.md): package layout, commands, config, backends, the shim
- [Score format](KPTASInterface/docs/score.md)
- [Holorecord bundles and seeking](KPTASInterface/docs/holorecord.md)
- [Tiers](KPTASInterface/docs/tiers.md)
- [Glossary](KPTASInterface/docs/glossary.md), including the speedrun terms
- [Kolto Labs](docs/kolto-labs.md)

The glitch names, and years of routing and glitch research behind them, come from the KotOR speedrunning community.

## Who made this

I'm Wizard, [bodencrouch](https://github.com/bodencrouch) on GitHub. I've been a developer since 2011, and I mod KotOR.

Emulated games got save states, rewind and frame advance a long time ago. KotOR never did. It isn't emulated: it's a Windows program that reads the wall clock and asks DirectInput for keys, so there was nothing to hang those tools on. I'm building KPHarness so it can have them too, without changing how the game plays.

## Money

KPHarness is free and open source. It is not a registered nonprofit. Donations are appreciated and never required: [GitHub Sponsors](https://github.com/sponsors/bodencrouch).

## License

MIT. See [LICENSE](LICENSE).

Not affiliated with Lucasfilm, Disney, BioWare, Obsidian or Aspyr. No game files are included; bring your own copy.
