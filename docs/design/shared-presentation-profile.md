# Shared presentation profile

`telos-shared-presentation/v1` transfers only surrounding/overlay presentation
and the game preview panel's visibility and opacity. It contains no input,
recording, runtime, shortcut, executable or theme commands.

```json
{
  "format": "telos-shared-presentation/v1",
  "presentation": "surround",
  "panels": {
    "game": { "visible": true, "opacity": 0.9 }
  },
  "hostProfiles": {}
}
```

All shared fields are required. Unknown shared fields or versions reject the
whole import before applying it. Visibility is boolean; opacity is finite and
between 0.55 and 1 inclusive. The entire JSON file is limited to 64 KiB. Host
identifiers contain lowercase letters, digits or hyphens, start with a letter,
and have at most 48 characters.

Browser Workspace **Export shared** creates this file; Workspace **Import**
accepts it alongside the existing browser-specific layout format. Native
Layout **Manage → Export shared presentation / Import shared presentation**
uses the same JSON schema. Native `preview` maps to shared `game`, and native
`workspace` maps to shared `surround`.

`hostProfiles` contains opaque JSON for round trips. Export refreshes only the
exporting host's `browser` or `native` payload and preserves the other payloads.
Import never applies these payloads. Consequently pixel rectangles, normalized
floating positions, dock areas, split ratios, hidden editor choices, and
host-specific settings retain their current values. Native opacity below 0.55
must be adjusted before shared export; export does not silently clamp it.

The native adapter updates presentation without calling the full preferences
reload: no shortcut configuration commands, preview pixel invalidation, or
recording/session mutations. Browser import uses its mounted presentation
owner and dock owner. Both retain prior UI state for rollback if application
fails. Geometry preservation concerns panel rectangles, not identical widget
appearance or game-frame dimensions across different window systems.

Implementation is source-reviewed only. Cross-host round-trip interaction and
native/browser rendering have not been exercised in this change.
