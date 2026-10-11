# TAS layout contract

This is the shared presentation contract for the existing browser and native
hosts. It defines layout behavior, not a new controller or a transport API.
The browser mode toggle below is implemented in source. Native mappings refer
to the existing layout/density owner; this document does not install native UI.

| Concern | Shared behavior | Browser mapping | Native mapping |
| --- | --- | --- | --- |
| Layout | Change the arrangement of the existing controls without replacing the game session or changing playback. | `browser` surrounds the game; `overlay` uses the same iframe and TAS dock. | Existing `workspace` and `overlay` preferences. |
| Default | Use the host default only when no explicit preference exists. Restore saved choices. | Browser layout by default; `telos.tas-presentation` restores an explicit overlay choice. | Existing native layout preference and host default. |
| Density | Keep transport and seek available when editing is collapsed. Expansion reveals the existing editor. | `telos.timeline.expanded` defaults collapsed; only explicit Expand/Collapse persists. | Existing native density preference and editor action. |
| Visibility | Hide/show is independent of layout and engagement; keep a reachable recovery control. | Existing workspace visibility key/button; recovery stays in parent UI. | Existing visibility action and native recovery control. |
| Engagement | Layout and visibility do not change input ownership or request pause. | Existing keyboard controls own engagement. | Existing interaction controls and acknowledged owner state. |
| Availability | Reuse the owner's effective capability, enabled state, and refusal reason. | Existing runtime controls and session capabilities. | Existing widgets/QActions and owner admission. |

Layout controls must distinguish a command from a state toggle. An ordinary
command can say “Switch layout” or name the destination. A toggle keeps one
name and reports whether that named mode is active. Browser “Game overlay”
therefore retains its label in both modes; `aria-pressed=true` means overlay.
Its title describes the return to browser layout, and its pressed style uses
the existing active/border tokens. See the
[W3C button pattern](https://www.w3.org/WAI/ARIA/apg/patterns/button/).

Changing layout retains editor contents, selection, and session identity.
Keep focus on a surviving control. If layout removes the focused control from
view, move focus to a visible recovery control in the same host, never into the
game as a side effect. Moving an active Score edit into the compact browser
dock can reveal it for the current visit without changing saved density.
Console shortcuts first restore the browser layout, then focus the same console.
When browser columns become compact tabs, the focused Roll, Score, Branches,
or Sample state panel becomes the selected tab. An old hidden tab selection
must not hide an active editor during a viewport resize. Any automatic editor
reveal remains local to the visit and does not overwrite saved density.

On the focused browser seek rail, unmodified Enter/Space opens the existing
editor, retains rail focus, and saves the explicit density choice. Collapse
remains an explicit control. Key capture and configured engagement/visibility
bindings run through the existing keyboard owner first; matched binding repeats
are consumed without retriggering or falling through into a seek. Unbound rail
navigation retains repeat. This matches native rail expansion and the canonical
research's keyboard-access requirement;
the gesture changes layout only, not playback or the seek position.

The browser resolves its existing configurable bindings once in document
capture, before focused tabs, menus, rail or editor handlers. A configured
Arrow/Home/End binding therefore has the same meaning in either layout. The
keyboard owner retains its typing/modal guards; unhandled keys continue to
the focused widget. Key-capture events cannot navigate tabs or seek before
the chosen binding is saved. This follows the browser's documented
[capture ordering](https://developer.mozilla.org/en-US/docs/Web/API/EventTarget/addEventListener#capture),
without introducing another keymap or forwarding parent keys into the game.

Editor scrolling and selection do not imply seeking. Incidental wheel input
must not execute a seek; any wheel-to-seek behavior belongs to an explicitly
focused rail and remains owner-admitted. Native's corresponding focus guard is
a separate source slice. This contract does not assert one shared keymap.
The local `tas-ui-conventions-current/research.md` identifies this distinction;
the official [FCEUX controls manual](https://fceux.com/web/help/taseditor/Controls.html)
separates plain-wheel browsing from right-button-plus-wheel playback movement,
and selection from the playback cursor. That supports requiring deliberate
seek intent, not importing FCEUX's exact gestures. The concrete browser change
in this slice is the stable layout-toggle meaning described above; the native
focused-wheel change is reviewed separately.

Committed game position, authored score position, selection, and a requested
seek destination remain distinct. Layout does not relabel browser render
presents as native ticks or make unavailable restore operations available.

Current limits: this is source-level behavior and naming alignment. Native and
browser still use their own widgets, typography, geometry, and owner APIs.
The browser toggle changes the existing layout owner through
`telos:presentation-change`; it does not consume native Python objects. Runtime
visual parity and live game behavior have not been qualified by this slice.
