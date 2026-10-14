# Admit input before an idle menu renders

The actual 861ca780 guest returned to character class selection after loading. It stopped presenting at count80 while resumed. The next authored input failed in version10's pre-input presentation path. The paused barrier contained all43workers. The source imposed a render prerequisite before dispatch; the observation is consistent with an idle menu needing input. The underlying renderer or thread wait cause remains unproven. This is observed behavior and a source diagnosis, not a simulation-frame proof.

Bridge version11 declares `quiescent-pause-and-bounded-gl-present-per-score-step`. It admits input under the complete worker pause after refusing any open or still-queued previous native input batch. It does not request a spontaneous pre-input presentation. Its `telos.input-quiescent-alignment/v1` receipt reports zero pre-input presentations.

The existing post-dispatch checks remain: native transition consumption where required, exactly one observed GL presentation and a complete pause barrier, within the existing deadlines. An in-flight presentation or an idle held-state step can still fail those checks. The change does not invent a presentation, permit extra scored renders, bypass queued events, identify a Win32 sampling point or provide an engine-frame clock.

The parent explicitly negotiates version11 and its different admission policy. Older version8/9/10 owners keep their existing declared policies. The shared debugger displays and exports Input admission separately from key support and operation qualification. A worker pause is not an engine-frame boundary.

This correction is source-only until a new owner adopts it. The retained guests and recordings were not reloaded or patched. Their older bridge behavior remains unchanged. No tests or test probes were run. Full menu interaction, held-state stepping, native sampling, cadence and replay remain unqualified.
