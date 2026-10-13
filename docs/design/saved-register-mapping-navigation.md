# Prepare a mapping lookup from a saved register

The register table offers a preparation action beside values that fit the active 32-bit address model. It copies the exact value into the address backing file form and opens that panel. The user must choose Capture address backing file to request metadata. Preparing the form performs no backend request.

The action retains the capture's process, thread, register and displayed capture time. Session, selection epoch and process checks refuse navigation after a target change. The mapping view also requires its own advertised reader and capture boundary. A saved register value is not evidence that the value is a pointer. It is not the current instruction or a wait cause.

Each displayed preparation button retains its capture epoch, process and session. Its disabled state checks those values too, so a retained table cannot present an enabled navigation action after disable/reactivation invalidates its capture epoch. The click handler repeats the checks.

The helper accepts bounded hexadecimal register strings and compares their exact integer value against the 32-bit operation limit before conversion. Zero remains valid. Wider nonzero values and other pointer widths are unavailable in this form; they are never truncated. Register row exports retain the captured data without navigation buttons. Preparing a lookup leaves the previous mapping table and export intact and labels them accordingly.

## Evidence

Implemented in source on 2026-10-13 after actual recorded candidate214aa load diagnosis required manually copying thread11's saved EIP0x104fd1c4 into the mapping form. That earlier manual lookup returned /lib/libc.so.6 with file offset0x00000000000cd1c4. It does not qualify the new navigation action.

No tests or test probes were run. The new source has not been adopted by the retained live game; no reload or hotpatch was performed. Live adoption and actual-use qualification remain pending. This adds navigation to the existing capability layer without granting uploaded files execution, memory access, module identity, replay or platform support.
