# Open reference drafts from editor Import

The editor Import action recognizes kptas.reference-input-draft/v1 and hands the selected local file to the existing observation viewer. The viewer validates the wrapper, evidence and authoring placement through its existing readers. Accepted files open the captured-footage panel with provenance, unknown inputs, source timing, draft position and uncertainty.

This path does not parse observations as score commands, replace score text or branches, move the authored playhead, select another game target, pause or restart a guest, or invoke replay. It opens the reference panel while the existing controller remains selected. Raw observation packets still use the viewer's own import control; the editor handoff accepts the explicit draft wrapper.

The local draft limit is64MiB; score/holorecord imports keep their20MiB limit. Editor import revisions and package lifecycle generations reject superseded work. The host repeats admission checks across viewer loading. Footage returns its in-flight load promise and binds the reference request to its view generation. The observation reader checks admission after its file read and returns actual acceptance instead of an implicit success.

Source implementation on2026-10-13. Live adoption and actual-use acceptance are pending. No tests or test probes were run. Executable input reconstruction, alignment, replay, K2 corpus qualification and the remaining authoring requirements remain open.
