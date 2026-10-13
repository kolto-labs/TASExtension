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

Actual no-game workspace31 imported the full existing R4NG3 artifact through
the normal file picker:86,876observations/42lanes. Source observation31673
showed W/LMB/RMB and source part9. A chosen draft start240 displayed draft
tick31913 with the uncertainty note. Normal export saved741643bytes,
SHA256bfddf46262df930153e0fd85b4aff3d0bf87363ea3f5196320079bc3f63d4fde,
containing6614state changes,294with unknown states, and execution/replay false.
Normal local re-import retained start240 and the uncertainty note; selecting
31673 again displayed the same source inputs and draft position. This qualifies
the observed export/import/display path only. Other packets, invalid placement,
oversize paths, source replacement and disposal remain unqualified.

The observed export filename used the older catalog label; its embedded
artifact identity correctly names the20261011full observation assembly. Source
now derives filenames from the imported artifact ID. The no-game31viewer was
reloaded, the saved draft restored through its local picker, and normal export
used the actual20261011official artifact name. Saved corrected draft SHA256
7240569bad4529024ec7173a878150aab208a0716728312077e5a3896edf35b1.

Further actual use exposed a provenance mismatch: imported R4NG3 observations
retained the selected Otterito heading and catalog player. The importer now
sets its heading from the reported packet identity, replaces the source notes,
and enables the selected catalog player only when both reported video and
submission URLs match. Matching declarations are not source authentication or
player alignment. A request guard also refuses an unmatched catalog player.
No-player metrics do not borrow catalog resolution/rate. After reloading only
the no-game31viewer and restoring the saved draft, actual UI showed R4NG3,
start240, no source player and a disabled Open catalog player for the mismatched
Otterito entry. The matching-player/archived-source paths remain unqualified.
Source now invalidates late catalog-read success/error when a local observation
import changes the observation revision, retaining the existing video controller.
That race/cancellation path and later placeholder/tooltip refinements are
source-only. This handoff
does not complete community replay, K2 corpus acquisition or executable editor
integration. No tests or test probes were run.
