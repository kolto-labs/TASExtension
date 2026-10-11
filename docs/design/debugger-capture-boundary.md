# Debugger capture identity

The browser workbench calls the existing `OriginalGame` memory bridge. It does
not attach a second debugger, run Core provider factories, or grant backend
capabilities. The runtime remains responsible for validating the selected
guest PID, address range and paused-worker barrier at capture.

Before requesting bytes, the workbench snapshots the current owner-reported
session, target profile, backend ID and backend release. Its existing request
generation and session checks discard late results after a session change.
Successful captures retain those identity fields with the copied bytes; a
later descriptor refresh cannot rewrite their origin. Changing sessions or
selecting another PID clears the workbench's capture/export state.

Hex downloads now include this identity alongside PID, address, length and
host receipt time. Raw `.bin` downloads remain exact bytes without metadata.
Receipt time is when the page received the result, not a guest timestamp.
The identity is supplied by the existing host handshake; it is not independent
proof of backend qualification or a runtime snapshot that can be restored.
Local executable header inspection never supplies the runtime identity.

The official [Chrome debugger API](https://developer.chrome.com/docs/extensions/reference/api/debugger)
separates a debug target from its protocol session and routes commands/events
using those identities. That supports retaining identity at this host/UI
boundary; this change does not use CDP or add extension permissions. Core's
separate debugger service contract also keeps session/target identity on its
observations. Connecting that service to a concrete browser backend remains a
separate integration task, with admission and capture safety owned there.

Source-only slice: no memory request, runtime reload, test, build or downloaded
artifact was executed to qualify this change.
