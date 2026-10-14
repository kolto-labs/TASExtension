# Open a native holorecord in the local browser workspace

These instructions describe the current local implementation. The public demo
was rolled back to its September 30 version and does not establish availability
of the features below. New browser work must be completed and tested locally
before deployment; the current deployment freeze remains in force.

The browser workspace has two separate uses. Its stand-in scene lets you try
TAS controls without the game. Its native holorecord dialog reads real desktop
recordings and exports their original files. It does not run KOTOR or restore
a native process in the browser.

Choose **Load native holorecord**, then select a `.holo` folder or an uncompressed
ustar `.tar` containing one `.holo` root. The folder must contain `journal` and
`meta.json`. The browser retains each file's payload unchanged, including page
stores, frame archives, metadata and parent references. Export produces a tar
for download. Extract it with an archive tool before opening it in the desktop
TAS. Keep referenced parent bundles; they are not fetched or bundled for you.

The journal inspector checks framing, hashes, chain links and bounded tick
records. It does not validate the full page store, environment compatibility or
native restore receipts. A malformed archive is rejected without replacing the
previous selection. Native playback and restore stay disabled here.

Selection and inspection are local: the page does not upload the recording.
The selected bundle is temporary. Download the export for a portable copy;
“export requested” is not proof that a browser has committed it to disk.

Tar transport preserves payload bytes, empty directories and modification times
to seconds. Folder pickers cannot report empty directories. Neither format path
preserves Unix ownership or permissions. Limits are 8 GiB of payload, 100000
files and portable ustar path lengths. Links, traversal paths and conflicting
file/directory entries are refused.

The separate stand-in workspace uses IndexedDB autosave and its own export
format. That format contains scores and hashes; it is not a native full-memory
holorecord. Browser storage can be evicted, so keep exports for work you need.
