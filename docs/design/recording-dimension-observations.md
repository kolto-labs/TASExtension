# Recording dimension observations

Startup recordings may begin before the guest changes its canvas size. An
existing receipt reported a300×150 startup canvas; a separate retained
recording decoded an800×600 image through the actual player. Neither proves
the encoded dimensions or resize continuity of another recording.

New recording receipts retain the legacy width/height fields and explicitly
scope them to the canvas at recording startup. Additional bounded observations
record canvas size, capture-track settings and track state at startup,
interruption and finalization. These are local observations, not container
inspection, simulation timing, complete-run proof or replay qualification.
No frame copying, resizing, new capture stream or encoder behavior is added.

The currently recorded game owner is preserved and does not adopt this source
change. Production candidate5860d62fe925cd79c5b24a3bf6238878cd96bbd6bc83d65f1e2e8effa2dada33
stages the recording script and writer together, alongside the compiled IPC
trace-budget change. It remains unlaunched and unqualified. Actual receipt
and encoded-video acceptance remain pending. No tests, test probes, syntax
checks or synthetic requests were run.
