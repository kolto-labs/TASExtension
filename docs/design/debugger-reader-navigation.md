# Open supported readers

Execution now has Threads and Registers navigation buttons. The inspection directory offers Open threads and Open registers when the selected session reports the corresponding operation as available. When owner-authored qualification reports exist, their resolution controls that navigation.

Opening a panel does not capture data. Existing readers still require the selected session, process, adapter operation and capture boundary. The dock availability checks remain in place. No memory writes, breakpoints, module reader or whole-VM snapshot operation is added.

Each row reader uses a named Capture action and a named Export action, such as Capture threads and Export registers. This distinguishes controls across floating panels and makes them accessible without depending on a repeated generic Refresh or Export captured rows label.

During the actual retained K1 save-load diagnosis, the existing readers captured nine PID-10 threads and 81 saved-register rows under a full 36-worker pause barrier. Six threads were in kernel wait and three were paused. Locations and wait causes were unavailable. The process named wine was the reported presentation owner; this does not identify the Windows executable or its build.

The separate 05a9b110 startup guest adopted the new UI. Open threads opened its real Threads panel, with Capture threads and Export threads disabled until a process and capture boundary were selected. That navigation and label presentation are partially qualified for this startup session. No capture was fabricated. Earlier retained pages keep their old UI. Other navigation paths, qualification changes, missing readers and other backends remain live-unqualified. No tests or test probes were run.
