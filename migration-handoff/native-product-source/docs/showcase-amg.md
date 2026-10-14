# Watch the AMG practice fixture

The current showcase is a continuous 15.05-second recording of the actual X11
display: 903 frames at 60 FPS. It starts at the saved `before_save` bookmark,
recording tick 866. The display also shows the offset from that bookmark.
Preparation happened before recording. The take itself has no cuts, splices,
speed changes, interpolated frames or added graphics.

It uses a cheat-marked Dantooine autosave in LAB mode. This is a practice fixture,
not the Endar Spire DLZ, a clean Taris route, swoop demonstration or WR run.
The result opens the map behind the quit dialog.

The retry score arms Alt+F4 for the save's message pump:

```text
@mark before_save
1 f4 !altf4@save
6 .
1 m
2 .
```

In this recording, tick 867 submits the quicksave and close-on-save request.
The journal places the save and dispatched close event at 868. The request is
not a claim that Alt is held continuously. The map input occurs at tick 874,
and the short score ends at 876. Those numbers describe this fixture; they are
not universal timings for the glitch.

The posting MP4 is a whole-file transcode of the lossless FFV1 screen capture.
A separate controller recording contains its own frame and event ledgers.
Their start times and frame counts differ, so the controller ledger is not a
byte hash of the screen video. Both recordings were audited across every frame.
The final screen/posting pair has 903 frames; the supporting controller take has
1152. The audited posting artifact is `amg-continuous-screen-v16.mp4`.

The v16 fixture also passed separate native checks for strict restore, unchanged
memory root during output inspection, exact actual-screen RGB after restore,
poison refusal and recovery, and native recording/save/reopen lifecycle. Those
checks belong to this pinned fixture. Broader platform coverage, M3 integration,
K2 and the remaining release milestones are still separate work.
