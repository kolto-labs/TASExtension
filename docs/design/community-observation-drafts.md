# Preserve observations in authoring drafts

The reference viewer can export a `kptas.reference-input-draft/v1` wrapper
and import it through its existing local observation importer. It embeds the
original accepted observation packet instead of turning visible highlights
into executable input. Unknown states, unresolved bindings, source-part and
state-change PTS records, discontinuities and reported provenance remain in
that packet. Reported source identities are not authenticated by the wrapper.

The author chooses a nonnegative draft start tick and an uncertainty note.
The view shows this authored position beside the selected source observation;
source PTS remains in the existing provenance view. One selected observation
per draft tick is an explicit unqualified authoring hypothesis. It does not
identify a simulation frame, physical hold, input-sampling boundary or player
alignment. The uncertainty note remains visible beside the position and is
included in export. Missing or invalid placement is refused; safe-integer
bounds cover the complete observation span. Source replacement resets placement.

The wrapper requires read-only data, executable_score false, replay_qualified
false and physical_inputs_complete false. Nested draft wrappers are refused.
Its embedded evidence must still pass the existing track/excerpt validator.
No game controller or score importer consumes this schema. Importing it updates
the independent reference view only. The local import/export limit is64MiB;
oversize export is refused without truncation. Download URLs are retained until
replacement or disposal; no polling or scheduled work is introduced.

This implements a portable draft handoff in source. It does not complete
community replay, K2 corpus acquisition or editor/runtime acceptance. Actual
export and re-import remain unqualified. No tests or test probes were run.
