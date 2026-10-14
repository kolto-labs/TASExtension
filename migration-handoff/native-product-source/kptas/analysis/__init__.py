"""Owner-side analysis jobs: frame-window finder (gap 9) and bounded input search (gap 11).

Both run inside the live owner as sliced jobs (kptas.analysis.jobs), drive trials through the
owner's own public operations (rewind, step, mark, inspect) on scratch takes, and always replay
the original inputs back to the user's frame before they finish. No game state, recording or
score is changed except by those operations.
"""
