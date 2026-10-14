# Prepare a memory scan from a saved register

The shared debugger Registers view can now prepare an explicit scanner range from a saved value. This extends the existing register-to-backing-file preparation without adding a backend operation. Both actions use the same controller, selected process and session.

Prepare scan is available only for an exact nonzero32-bit value, a declared32-bit target byte order, a supported memory reader, available memory.read operation, confirmed capture boundary and current session/PID/epoch. The scanner must have no active request or existing snapshot. Old register values remain labeled with their capture time; a saved numeric value is not a verified pointer.

Preparation changes only address, byte count and initial comparison choice, reveals Value scanner through the existing dock API and focuses its address field. It retains a valid chosen byte count up to65,536, or uses4,096 when invalid, clamping to remaining32-bit address space. The selected value type must fit. It never reads/writes memory, resumes/steps execution, adds a watch or resets previous scan/string results. First scan remains the explicit read; subsequent comparisons and string inspection reuse existing behavior. Zero and wider addresses do not produce a scan action.

The package has no KOTOR profile dependency for this action. Other games need a compatible declared32-bit memory model and actual register/memory readers through existing capability admission. Selected-file inspection alone grants neither reader. Wider target models remain unsupported by these address forms.

Source-only. Browser inventory remained empty; no adoption or interaction qualification. Original source backups are retained in /home/brunner56/kptas-browser-game-wasm/artifacts as debug-ui-index-before-register-scan-20261014.js and debug-ui-memory-tools-before-register-scan-20261014.js. Shared index SHA256c7fa043c419289b7bad092e581b8241a5fc9872656a538e91eb4a72d585ddd7d; memory-tools SHA2565cf62893524cec4219548326a39da6e42f734f915fb70d26600b515b116e83c5. Existing coupled dirty source/index/data retained. No tests, probes, runtime rebuild, deployment or game asset upload.
