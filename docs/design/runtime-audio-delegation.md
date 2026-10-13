# Delegate runtime audio playback

The local parent uses 127.0.0.1 while its owned runtime uses localhost. These are different origins. Both runtime iframe creation paths now declare autoplay; the local-install path keeps its cross-origin-isolated declaration. The browser still controls whether an AudioContext can run. This declaration does not prove audible PCM, capture continuity or the cause of a suspension.

Observed before the change: session5a4b356c reached title18/full50, but capture1daceb5e stopped after its audio context became suspended while the document was visible. It retained zero chunks. A normal Record game retry expired after30seconds waiting for audio.resume. Those captures remain preserved.

Actual adoption2026-10-13: fresh tab4/session632bc210-2d56-49b8-a94a-5d19b49c2a29 loaded the same ef8991 runtime. Read-only DOM observation shows allow=cross-origin-isolated; autoplay and the localhost runtime URL. Normal Record game acknowledged recordingee701f86-dd72-4523-93c6-61ca66d5bf9c before the first presentation. Retained chunks and continuing audio remain pending. Existing guest3 was observed paused18/full50 and left untouched.

Reference: https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Permissions-Policy/autoplay

No tests or probes. No runtime rebuild, default selection or deployment was required for this parent source change. The coupled parent source remains dirty and preserved.

Actual bounded continuity: recordingee701f86 acknowledged18409bytes/2chunks flushed while still active during present0/workers47of49. No suspension observed through this interval. Game content, audible PCM, full capture and causality remain unqualified.

Continuing actual adoption: title reached, then four released inputs through no-saved-games dialog, paused39/full50. Capture active with422956bytes/37chunks. No suspension observed over this interval. Audible PCM and longer capture remain unqualified. Actual screenshot /tmp/kptas-audio-delegated-menu-capture-20261013.jpg.
