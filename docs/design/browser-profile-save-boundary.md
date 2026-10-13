# Save the browser profile

The game’s Save Game action writes a game save into the guest’s writable profile. Saving that profile to browser storage is a separate operation. Neither operation is a whole-VM snapshot or rewind.

The parent now exposes Save browser profile without closing the game. The action uses the existing child-owned `FS.syncfs(false)` path and the same serialized lifecycle queue as launch and close. It does not capture process memory, rewind the score, restart the guest, or finalize recordings.

An active session without a ready profile or owning frame must fail the request. The prior parent path returned success without requesting a flush in that state. The new path binds the request to the current session, frame window, origin and request ID. Only a boolean successful reply from that owner completes the operation. A changed owner or failed send rejects the request, and the existing timeout removes the listener.

The button is hidden without a session and disabled before readiness or during a request. The completion text distinguishes profile storage from saving gameplay. A successful sync does not prove an atomic game save, cross-session reload, executable identity, deterministic replay or support for other storage backends.

The separate 05a9b110 guest adopted this UI without replacing earlier guests. During its ordinary startup the button was disabled before readiness, then enabled after files connected. Its normal save request received the owner acknowledgement and displayed Browser profile saved while the guest stayed open. This partially qualifies the normal UI/request/receipt path; it does not establish a durable gameplay save or later restoration. Failed, changed-session, timeout and early request paths remain unqualified. Earlier retained pages were not reloaded. No tests or test probes were run.

## Candidate profile boundaries

Actual recordedf6aca guest32 received the normal profile-save acknowledgement during Wine startup on2026-10-13. The guest remained resumed at presentation0 with33of36workers and recording27b207b9active. The message distinguishes profile flush from KOTOR's Save Game. It does not establish a gameplay save, atomic live-file snapshot or reload acceptance.

Current configureBrowserStorage keys ROOT and the D: drive from Config.appZipFile. The candidate archive name includes its release suffix, so distinct candidate archives select distinct writable profiles even when the declared library profile matches. The earlier shell comment claimed independence from build; it is now corrected in source. No storage identity or migration behavior changed. This preserves isolation between retained concurrent candidates. Profile migration or reuse needs an explicit, owner-bound design and evidence; it cannot be inferred from a save acknowledgement.
