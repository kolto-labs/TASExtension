# Local UI hot reload

Serve `demo/public` through the existing local server and opt in with
`uiHotReload=1` in the page URL. Normal visits do not load the developer module.
Only `localhost`, `127.0.0.1` and `[::1]` are accepted. In the page console:

```js
telosUiHotReload.list()
await telosUiHotReload.reloadStyles()
await telosUiHotReload.reloadModule('presentation')
```

Styles reload as a group. Replacement links load with nonmatching media before
any current link is removed; disabled sheets stage enabled but retain their
disabled state on commit. A load error or 15-second timeout retains the old
sheets. A failed commit restores retained original links. Loaded CSS is not
validated for design correctness. Shared tokens are linked explicitly before the main stylesheet, so token edits
participate in the same replacement. Any other imported CSS dependencies follow
normal browser cache rules; the loader versions linked entry stylesheets.

The presentation module snapshots its in-memory mode preference, mounts the
new version against the same DOM, then disposes the old listeners and both
MutationObservers. Construction failures clean up new resources and restore
the prior presentation. The existing layout owner receives resize/mode events.
Focus stays on the existing element where possible; stable IDs allow recovery
after supported widgets replace DOM. Text selection is restored when supported.
An active iframe is never focused or replaced by this loader.

The editor, game owner and debugger remain mounted. Captured bytes, comparison
baseline, local-file selection, editor text and the guest session are therefore
preserved. Debugger/editor JavaScript replacement is listed as unsupported.
Their styles can reload. No guest reload, server restart or compiled WASM
replacement is part of this path. Shared token sources remain the existing
canonical configuration; this loader neither regenerates them nor changes
native configuration.

## Existing page without the shell

When adopting these changes in an older live page, insert a stylesheet link for
`ui-tokens.css` before its existing `style.css` link and wait for it to load
before replacing styles. Older pages loaded tokens through the removed CSS
import; their DOM does not acquire the new link automatically.

After the source files are available, a developer can enable stylesheet reload
in an already running local page without navigation:

```js
const url = new URL(location.href);
url.searchParams.set('uiHotReload', '1');
history.replaceState(history.state, '', url);
const { installUiHotReload } = await import('/js/ui-hot-reload.js');
installUiHotReload();
await telosUiHotReload.reloadStyles();
```

Do not import the new shell into an old page to adopt its anonymous presentation
listeners. That page has no disposable handle; only stylesheet reload is
available until a later normal launch loads the shell. The current game can
remain running throughout stylesheet edits.

## Widget lifecycle contract

The registry accepts `register(name, { url, module, create, handle })`.
`create(module, state)` is synchronous and returns a handle with `snapshot()`,
`restore(state)` and `destroy()`. Modules have no top-level side effects.
Snapshots must own all state needed for replacement and rollback; a busy widget
can throw from `snapshot()` to leave its current instance intact. Construction
must clean up partial listeners, observers, timers, RAFs and resources before
throwing. Destruction is idempotent, releases only that instance's resources,
and does not remove shared DOM or mutate the game/editor owner. Stable element
IDs support focus restoration. A registration can replace an unsupported entry
once this contract is implemented; the loader is not limited to presentation.

Replacements are serialized. Versioned imports reload only the registered entry
module, not every dependency. JavaScript engines retain module namespaces, so
successive developer reloads accumulate module versions. An opt-in watcher polls
`/__dev/ui-revisions` once every two seconds while the tab is visible, passing
all watched paths as repeated `path` query parameters. The local server returns
SHA-256 revisions and caches hashes until file metadata changes. Every requested
hash must be present and valid before any baseline or retry state advances;
invalid or failed responses leave the current session intact. The watcher no
longer downloads and hashes every source file on each cycle. This requires the
local server version that provides the revision endpoint; an older running
server reports polling failure until its next coordinated restart. CSS and the presentation module replace automatically. Changes
to the shell or watcher report that a later manual page load is required.
Failed replacements retain their revision for retries with exponential backoff
from two seconds up to thirty seconds, scheduled by successful visible-tab polls.
A newer edit resets its retry delay. A module disabled after lifecycle recovery
failure is not retried automatically. Editor, debugger and runtime owners remain
mounted and are not automatically replaced. See [MDN dynamic imports](https://developer.mozilla.org/en-US/docs/Web/JavaScript/Reference/Operators/import)
for import caching and [MDN link events](https://developer.mozilla.org/en-US/docs/Web/HTML/Reference/Elements/link)
for stylesheet load/error behavior.

The publisher bundles the shell and presentation separately from `app.js`.
The computed developer import is not bundled and the source copy filter excludes
its module, so this feature is for direct local source development. This patch
has source review only: no tests, builds, server restarts or runtime reloads.
