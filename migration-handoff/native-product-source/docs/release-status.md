# Check release readiness

This is a working source snapshot with a locally tested Python wheel. It is not a public release.

The public browser demo is pinned to its September 30 version after rollback.
Native holorecord import/export and other newer browser work are local-only
acceptance results. Prior deployment receipts do not describe what is public
now. Further deployments remain frozen until the work is complete and locally
tested.

The current K1 Wine fixture has passed strict restore, actual-screen comparison,
poison refusal/recovery and save/reopen checks. The desktop workspace exposes
ordinary Open, Save, score editing and paused branching, plus an authenticated
loopback command interface. A continuous AMG LAB clip has been audited.

Those results do not establish support for every executable, mod, operating
system or game state. Broader M3 integration, K2 native support, Endar DLZ,
clean Taris/swoop routes and a WR-class run remain separate milestones.

A release archive still needs a coherent source revision, the matching native
DLL, supported-build/runtime notes, dependency installation and clean-machine
acceptance. Keep DLL and environment pins intact. Do not ship a source copy from
one revision with a different native shim because they happen to launch.

The optional Qt workspace needs PySide6, and video capture needs FFmpeg. The
core command line installs as a standalone wheel; see [installation](install.md).
The installed command and bundled keymap have passed a fresh-interpreter PySim
record/replay check covering all 83 ticks. This does not qualify a native build.

Branding is bodencrouch / kolto-labs, within OpenSWKotOR. Hyperstim is optional.
A download button must point to the real new Discord invite once one is supplied
and verified. No invite, donation destination, release URL or support promise is
invented here. Preparing local artifacts does not publish a release or open an
upstream pull request.
