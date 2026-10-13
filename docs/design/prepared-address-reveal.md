# Reveal a prepared address form

Actual use in the paused first-area session632bc210 showed that Prepare mapping for thread11 eip filled the address form, but the Address backing file panel stayed collapsed. Manually expanding it exposed the prepared address and separate Capture action.

TelosDock now has an additive reveal(id) action. It rejects unavailable/unregistered panels, expands the requested panel, and uses the existing visibility/persistence path. Panel placement, geometry and other panels are retained. Ordinary show(id) behavior stays the same.

Saved-register navigation uses reveal when available, with show as a fallback for older dock hosts. Existing session, PID, epoch, capability, supported-reader and pause-boundary admission guards stay in place. The action prepares a form and focuses it; it does not capture memory or alter the retained mapping table/export.

Source implemented2026-10-13; coupled dirty dock-workspace.js/debug-ui/index.js preserved. Active guest unchanged; fix unadopted and unqualified. No tests, test probes, synthetic requests or runtime rebuild. Broader modular debugger and uploaded-game qualification remain open.
