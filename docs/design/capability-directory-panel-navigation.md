# Capability directory panel navigation

The debugger capability directory already gates Threads, Registers and Address backing file links on their reported operations. Its callback used Dock.show, which marks a panel visible but preserves collapsed state. A link could therefore expose only a collapsed header.

The callback now uses the existing Dock.reveal when provided, with Dock.show as an older-dock fallback. Reveal respects registered and unavailable panels, expands the destination and uses the existing visibility path. This is navigation only: it does not capture registers, enumerate threads, read a mapping or grant a backend capability. No new game-specific adapter is involved.

Source inspection only. Retained live guests were not reloaded or patched, and the collapsed-panel interaction has not been qualified in a new document. Existing dirty debugger work is preserved. No tests or probes ran.
