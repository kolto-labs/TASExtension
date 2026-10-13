# Inspect capture audio

The recording panel now offers Inspect capture audio and Export audio observation. Inspection reads the existing capture owner's audioObservation helper once, while its recorder is active. There are no audio operations, new timers, native queries, PCM copies or extra GL calls.

The captured schema is telos.recording-audio-display-capture/v1. It includes the recording name, declared runtime context, timestamp, AudioContext state/time, capture lease, backend failure flag and at most eight normalized device counter records. Missing metadata remains null. Counter observations do not prove audible playback, encoded audio, game progress or replay.

A capture is kept as a dated snapshot. Export uses that snapshot, including its recording identity, even after a later stop or failure; it does not inspect the current recorder again. A new recording clears the prior snapshot. Inspection is disabled during startup, stop/finalization, failure and release. Export failures are displayed. Saved-recording selection does not change the identity of an already captured observation.

Source implemented2026-10-13 in the existing untracked game-recording.js; prior code preserved. Staged candidate731927e669952421f59d58dcbc9d2e488aceb6c82a01f633a93b934e17d933c9 with --reuse-staged-root, exit0. Default unchanged; no installation assets copied or uploaded; no native rebuild. Candidate unlaunched and controls unqualified. Established ef8991 session632bc210 remains unchanged.

This is optional metadata from the existing BoxedWine capture owner. It does not establish arbitrary-game audio support or complete the modular debugger requirement. Generic capability/adapter integration and actual use remain open. No tests, test probes or synthetic requests.
