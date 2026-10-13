# Open reference drafts from editor Import

The editor Import action recognizes kptas.reference-input-draft/v1 and hands the selected local file to the existing observation viewer. The viewer validates the wrapper, evidence and authoring placement through its existing readers. Accepted files open the captured-footage panel with provenance, unknown inputs, source timing, draft position and uncertainty.

This path does not parse observations as score commands, replace score text or branches, move the authored playhead, select another game target, pause or restart a guest, or invoke replay. It opens the reference panel while the existing controller remains selected. Raw observation packets still use the viewer's own import control; the editor handoff accepts the explicit draft wrapper.

The local draft limit is64MiB; score/holorecord imports keep their20MiB limit. Editor import revisions and package lifecycle generations reject superseded work. The host repeats admission checks across viewer loading. Footage returns its in-flight load promise and binds the reference request to its view generation. The observation reader checks admission after its file read and returns actual acceptance instead of an implicit success.

Source implementation on2026-10-13. Live adoption and actual-use acceptance are pending. No tests or test probes were run. Executable input reconstruction, alignment, replay, K2 corpus qualification and the remaining authoring requirements remain open.

## Actual import from the authoring workspace

Reloaded only the no-game workspace31 to adopt this source. Its current score textbox contained854characters and its authored playhead was0/1165. Normal editor Import selected the existing full R4NG3 reference-draft file through the local picker. The captured-footage panel opened with86876observations/start240/uncertainty and R4NG3 provenance. The selected target remained Saved recordings; no source player opened and the mismatched catalog player action stayed disabled.

Passive reading of the actual score textbox before/after import found the same854characters. The authored playhead stayed0/1165. Selecting observation31673 displayed W/LMB/RMB, draft31913, source part9 and unavailable source time at state-change31672. Screenshot /tmp/kptas-editor-reference-draft-import-20261013.jpg.

The accepted editor→host→owned viewer handoff is qualified for this real artifact in this no-game workspace. No live-game target adoption, supersession race, disabled lifecycle, malformed wrapper, limit case or other artifact is qualified by this observation. Actual recorded guest32 continued independently at presentation55/resumed36of37workers, recording18570834bytes/65chunks. Existing guest30 remained intact. No tests or test probes were run.
