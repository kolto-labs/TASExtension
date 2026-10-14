---
name: KPTASInterface / InputInterfaceTelos
last_updated: 2026-10-02
---
# KPTASInterface / InputInterfaceTelos Strategy

## Purpose

KotOR runners need to reproduce and scrutinize mechanics one input and tick at a time without spending their time reconstructing lost attempts. A convincing TAS must survive careful comparison with ordinary game behavior and preserve the work needed to explain every result.

## Positioning

Build Telos Core as a useful standalone platform in KPHarness, bootstrapped through KPM. TAS, Lua and community additions install into Telos Core. Ship the first-party TAS addition first as an independently usable distribution with Core bundled and KotorPhasor optional. Full-state holorecords, genuine restoration and faithful inputs establish its value through real gameplay.

Telos Exchange is the working name for the community hub: discover what an addition makes possible, inspect its source and compatibility, install a reversible setup, and keep it useful together with other additions. Favor small composable tools, shared stable APIs and maintained combinations over a download list or compulsory dependencies.

## Users

**Primary:** KotOR runners and mechanics researchers authoring, testing and sharing reproducible runs.

**Secondary:** RPG testers and mod authors using explicit semantic engine actions or community additions without confusing those experiments with ordinary player-input TAS evidence.

**Contributors:** independent authors publishing and maintaining small additions, packs, presets, compatibility layers and workflows; community members exploring, discussing and sharing useful setups.

## Boundaries

- Preserve base-game mechanics, glitches and RNG behavior; distinguish supported virtualization from changes to gameplay.
- Keep exact restore distinct from checkpoint replay, and TAS distinct from semantic experiments and hands-off playback.
- Keep the public browser demo on its restored version until new functionality is complete and tested locally; the organization website remains deferred. Local Telos Exchange design is authorized; the existing Kolto Labs site is verified, while its new hub, navigation and public release remain delivery work.
- Keep Telos Core and installed additions usable offline without an account. Profiles, version pins, export and reversible disable/remove preserve user choice and old recordings.
- Make trust, permissions, tested compatibility, conflicts and maintenance visible. Community activity and recommendations must use real evidence; avoid fabricated popularity, artificial scarcity, deceptive urgency and destructive lock-in.
- The geometric-reasoning demo was canceled. KPM/platform/Lua integration remains the lowest-priority product track.

_Resist a change when:_ it makes a recording look successful without making the general tool work, weakens evidence to hide a failure, or displaces the current gameplay objective without removing its bottleneck.

## Key metrics

- **Repeatability:** fresh-run and restored-continuation state, file, input and journal agreement, with graphics/media evidence measured separately in qualification receipts.
- **Work preservation:** acknowledged tick, score, draft, ancestry and bookmark recovery across interrupted saves, process crashes and relaunches, measured in recovery fixtures.
- **Practical throughput:** wall time from authoring an input to a verified result and usable recording, with before/after bottleneck measurements across all active lanes.
- **Demonstrated outcomes:** continuous audited DLZ, Routine, swoop, Taris and full-run artifacts, rather than source changes or test counts alone.
- **Usability:** users can identify the current position, applied input, save state and next action, and complete pause/edit/branch/restore without losing work.
- **Ecosystem usefulness:** independent authors build against documented Core/TAS APIs; users find a useful compatible addition, reproduce a shared setup, and update or undo it without losing their work. Record real adoption and maintenance evidence; sparse activity stays visibly sparse.

## Tracks

### Faithful and durable execution

Make the retained state and continuation trustworthy across the supported runtime.

_Why it serves the approach:_ every run and mechanics claim depends on reproducibility and recoverable work.

### Authoring and presentation

Make text, in-game and browser workflows intuitive, reactive and visually light.

_Why it serves the approach:_ the real tool must explain what happened and support another attempt without a clip-specific facade.

### Runs and mechanics

Build the clean Endar DLZ demonstration and complete the broader run portfolio while improving the TAS.

_Why it serves the approach:_ real mechanics and full runs expose limitations synthetic fixtures cannot.

### Compatibility and community

Deliver independent installation, K2 and semantic testing with clear support boundaries. Telos Core provides stable services and reversible setup management; Telos Exchange helps users discover, inspect, install, discuss, combine and maintain additions, collections and shared setups.

_Why it serves the approach:_ a community standard grows through immediately useful tools that work together, visible maintenance and safe upgrades. Core reliability and The Routine remain the immediate priorities; ecosystem implementation follows without displacing them.

## Brand

**One-liner:** Make a KotOR run one input at a time. Keep every attempt.

**Key message:** bodencrouch / kolto-labs; OpenSWKotOR is the organization. Telos Core names the platform; Telos Exchange is the working hub name. Cards should show distinctive identities and concrete value immediately. The proposed Kolto Labs navigation uses a Telos Exchange button with browsing and contributor submission as separate actions; integrate locally with the verified Kolto Labs Worker site, retaining the completion/testing gate before publishing. Documentation should sound like the user, show actual capabilities, and lead to the real new Discord download destination.
