# Overlay and workspace preferences

The overlay and workspace are two layouts of the same widgets and session. Switching views keeps the same controller connection, input document, piano model, selected tick and unsaved mouse coordinates. Closing either view detaches the UI; it does not close the game. Layout files contain presentation settings only and are separate from semantic run plans and environment configuration.

Start the client of an existing session owner:

```sh
python -m kptas.telos.live_workspace --host 127.0.0.1 --port 47610 \
  --ui-config examples/ui/workspace.toml
```

The local controller entry accepts the same preferences:

```sh
telos control recording.holo --overlay --ui-config examples/ui/overlay.toml
```

`--layout overlay` or `--layout workspace` overrides the file's `[ui].layout`. Without a file or explicit flag, the original overlay is the default. The file does not change the game resolution, start or replace a session, alter an environment pin, or modify drafts. Local archive control retains its normal Open/restore behavior; a remote owner exposes only its supported operations. Remote archive stepping uses recorded input; applying edited input requires a supported branch operation.

Use **Ctrl+Alt+L** while the UI is focused to switch layouts and **Ctrl+Alt+R** to reload the selected preferences file. Reload reapplies an explicit command-line layout override if one was supplied. Shortcuts work in two places. While the UI has focus, Qt's `WindowShortcut` context runs them. While the game has focus, the native tool dispatcher runs the same `[ui.shortcuts]` keys, as long as the runtime has that dispatcher and the game hook can read the key. They are not OS-global key grabs. Runtimes without the native dispatcher use the fixed Ctrl+Alt+F6 to F11 and Ctrl+Shift+F12 chords instead. When the UI is focused, the controller drains but ignores host and native hotkey edges, so only the UI shortcuts dispatch actions. Auto-repeat is disabled for these actions.

A malformed file, an unknown setting, a missing/oversized file, an invalid size or colliding shortcut is refused before changing the current UI. At startup the original default remains usable and the Console explains the error. On reload the existing view, theme, model, session and drafts remain intact. Qt stylesheet parse diagnostics are checked on a hidden widget before application. This catches Qt-reported stylesheet errors, but is not a claim that Qt diagnoses every unsupported selector or styling value.

## Version 1

See `examples/ui/overlay.toml` and `examples/ui/workspace.toml` for complete starting points. The top-level `version = 1` is required; all other sections are optional. Unknown keys are errors rather than ignored settings.

| Section | Fields | Meaning |
| --- | --- | --- |
| `[ui]` | `layout`, `theme`, `game_surface` | `overlay` or `workspace`; optional relative QSS path; `owner_frame` or `native` game surface |
| `[ui.panels]` | `editor`, `inputs`, `console`, `score`, `history`, `journal`, `branches`, `preview` | Boolean visibility; omitted `editor` preserves current expansion; preview is workspace-only |
| `[ui.sizing]` | `window_width`, `window_height` | UI surface only; integers 640–3840 and 480–2160 (workspace height at least 600) |
| `[ui.sizing]` | `panel_width` | Overlay panel width, 280–1200; clamped to available surface |
| `[ui.sizing]` | `piano_height` | 75–600; must leave at least 280 pixels for other controls |
| `[ui.sizing]` | `column_width`, `row_height` | Piano cell sizes, 20–120 and 14–64; column width is a minimum and grows to fit tick labels |
| `[ui.shortcuts]` | `play_pause`, `step`, `rewind`, `apply`, `save`, `toggle_editor`, `switch_layout`, `reload_ui`, `toggle_engagement`, `toggle_visibility` | Unique single-key Qt PortableText combinations; empty string disables an action |

A key with Ctrl, Alt, Meta or a function key works anywhere in the window. A key without them, such as `Space` or `P`, works only while the timeline has focus, and never while the game has focus. Settings warns when you choose one. Canonical equivalents such as `Shift+Ctrl+P` and `Ctrl+Shift+P` collide. Shortcut assignment does not bypass operation guards or owner capabilities. In the local read-only piano inspector, the remote-only Apply shortcut is omitted.

The remote overlay defaults to an opaque, game-sized `owner_frame` surface below its panels. This displays the current owner's frame even when the native game intentionally skips screen presentation while paused. `game_surface = "native"` selects the original transparent panel mask. The local archive overlay defaults to native presentation unless configured explicitly. The workspace uses the same owner-frame reader in its Game view panel; expanding the editor places game and editor alongside each other. Its default height is 720 pixels, with startup size and position clamped to the available screen. The frame keeps its aspect ratio.

A read-only refresh retains the paired acknowledged picture and UI state until the next complete response arrives. Failed or incoherent responses clear the picture. Frame provenance distinguishes the acknowledged owner gate from the actual capture tick: older owners may not supply a capture tick, and the UI does not invent one. Showing an owner frame neither advances the game nor replaces its owner.

## Settings and custom keys

Open **More → Settings…** or press **Ctrl+,** while the UI has focus. The dialog has four tabs:

- **Keys** lists every action. Search by name, category or key. Click a key to change it. Esc cancels and Backspace removes the key. A key that another action already uses is refused. Clear that key first.
- **Display** changes the layout settings above. It also sets panel opacity and window position. Choose a theme file there, or turn on Reduce motion.
- **Playback** sets the largest Play batch in frames and seconds, and the preview refresh interval. It is available in the workspace only.
- **Shortcut list** shows every key by category. Print it or copy it as text.

Select **Apply** or **Save** to use changes. **Reload UI preferences** (Ctrl+Alt+R) also reloads the shortcuts file, so hand edits apply without a restart. Custom keys are stored in `$XDG_CONFIG_HOME/kptas/keymap.toml`:

```toml
version = 1

[keys]
"play_pause" = "Ctrl+Alt+F7"
"redo_input_edit" = ["Ctrl+Shift+Z", "Ctrl+Y"]
"open_settings" = ""
```

A value is one key, a list of keys or `""` for no key. Quote each action id. A key in this file wins over `[ui.shortcuts]` in a layout. When two actions in the same window ask for one key, the action registered first keeps it. The other has no key, and the Console names both. A broken file gives default keys and one Console warning. Ids that no loaded feature knows are kept. Settings lists them, and the Console reports them once startup has settled.

Reset key and Reset all keys write the default into the file for the ten `[ui.shortcuts]` actions. Layout files can still hold an older key, and the explicit default wins over it. Deleting `keymap.toml` by hand does not undo keys already saved into a layout.

Reduce motion and playback values are stored in `$XDG_CONFIG_HOME/kptas/settings.toml`. A playback value you never changed stays unset, so the built-in pacing and `KPTAS_PLAY_BATCH_MAX_S` still apply.

A key the game hook cannot read, such as `Ctrl+Alt+,`, still works while Stasis has focus. It is left out of the game-focus table, and the Console says so once. Qt's `Del` and `Ins` count as Delete and Insert there.

The chords for older runtimes without the native dispatcher are fixed. Custom keys do not change them:

| Chord | Action |
| --- | --- |
| Ctrl+Alt+F6 | Show the workspace |
| Ctrl+Alt+F7 | Play or pause |
| Ctrl+Alt+F8 | Advance one frame |
| Ctrl+Alt+F9 | Go back one frame |
| Ctrl+Alt+Shift+F9 | Go back to the previous bookmark |
| Ctrl+Alt+F10 | Create a branch here |
| Ctrl+Alt+F11 | Close the controller |
| Ctrl+Shift+F12 | Show or hide the TAS interface |

## Saved layouts and panel controls

The **Layout** combo lists saved profiles. **Load** applies the selected profile; **Save** updates the active profile. **Manage → Save as…**, **Rename…**, and **Delete** operate on named profiles in `$XDG_CONFIG_HOME/kptas/layouts` (normally `~/.config/kptas/layouts`). Saving writes validated TOML and an immutable QSS copy atomically. Profiles are saved only when requested; moving a panel does not silently rewrite a file.

Each panel has a draggable title, an opacity slider, a minimize button, a hide button, and a lower-right resize grip. The **Panels** menu stays outside those panels so hidden or minimized controls remain recoverable. **Reset layout** restores the active preset's panel arrangement. Loading a saved profile restores its positions, sizes, opacity, visibility and minimized state. Switching between the two base presets resets panel geometry to that preset while retaining the owner, draft and current selection.

Saved panel state uses the same external schema:

```toml
[ui.panel_states.editor]
x = 380
y = 230
width = 570
height = 480
visible = true
collapsed = false
opacity = 0.9
```

Panel identifiers are `transport`, `editor`, `inputs`, and `preview`. Opacity is 0.1 through 1.0. Coordinates and positive sizes are validated, then clamped to the current screen; content minimum sizes keep controls usable. Presentation files contain no callbacks or owner commands.

## Recording selector

The separate **Recording** combo lists recent `.holo` directories and the currently attached recording. **Browse…** adds a recording to the list. **Load** and **Save** dispatch through the existing serialized owner controller. Loading first preserves the input draft in a verified recovery file, then clears the active draft only after a successful new-recording acknowledgment. A refused load retains the draft and recovery file. The upper transport **Save** retains its input-document/score function.

Recording operations are capability-gated. An older connected owner that does not support recording load/save keeps these buttons disabled with an explanatory tooltip; browsing never creates another game session. The local archive entry uses its existing draft-safe archive replacement path.

## Theme files

Themes use [native Qt Style Sheets](https://doc.qt.io/qt-6/stylesheet.html). The application loads the theme after its built-in palette on this presentation root, not as an application-global stylesheet. You can edit these files without rebuilding Python or the DLL.

When the client starts with an explicit `--ui-config`, it watches that TOML
file and its QSS dependency. External saves are debounced for 400 ms, including
editors that replace files atomically. A QSS-only edit keeps current panel
positions, collapsed states and window dimensions; a TOML edit reapplies the
layout settings. An edit that arrives during loading or application queues
another reload instead of being silently acknowledged. Invalid edits leave the
last accepted design active and report an error; correcting the file retries
it. The automatic last-session file is excluded from this watcher.

This reload path updates presentation data, not Python modules or the game DLL.
Changes to Python code still require restarting the UI client; they do not
require replacing the game owner. Referenced image assets are not watched
independently, and Qt may cache images by path. Use a new asset filename and
update its QSS reference when replacing an image.

Stable object selectors include `#telosRoot`, `#transportPanel`, `#editorPanel`, `#inputPanel`, `#workspaceTabs`, `#pianoRoll`, `#seekSlider`, `#stepButton`, `#rewindButton`, `#scoreEditor`, `#commandEntry`, `#commandLog`, `#tasBadge` and `#workspacePreview`. The root also has `layoutPreset="overlay"` or `layoutPreset="workspace"` for preset-specific rules.

Local `url(...)` image/icon paths resolve relative to the QSS file. Existing Qt `:/` resource URLs also work. Network URLs and imported stylesheets are unsupported. `qproperty-*` assignments are refused because they can change widget behavior outside the validated presentation settings. Preferences are limited to 64 KiB, QSS to 256 KiB, and each local theme asset to 8 MiB; inputs must be regular files. These limits prevent a FIFO or device from blocking reload.

Qt applies its normal stylesheet cascade. See the [Qt stylesheet reference](https://doc.qt.io/qt-6/stylesheet-reference.html) for supported widget properties and the [QShortcut documentation](https://doc.qt.io/qt-6/qshortcut.html) for shortcut scope.
