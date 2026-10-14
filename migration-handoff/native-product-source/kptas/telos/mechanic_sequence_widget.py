"""Optional accessible guide; consumes owner receipts without issuing commands."""
from PySide6 import QtCore as C, QtWidgets as W

from .mechanic_sequences import SequenceError, SequenceTracker, load_profile


class MechanicSequenceDialog(W.QDialog):
    profile_loaded = C.Signal()
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Sequence file guide')
        self.setModal(False)
        self.setSizeGripEnabled(True)
        self.resize(360, 230)
        self.tracker = None
        self.latest = None
        self._rendered = None
        layout = W.QVBoxLayout(self)
        controls = W.QHBoxLayout()
        self.open_button = W.QPushButton('Open sequence…')
        self.start_button = W.QPushButton('Start here')
        self.start_button.setToolTip('Start from the last committed input boundary. This does not change the game.')
        self.start_button.setEnabled(False)
        self.expand = W.QToolButton()
        self.expand.setText('All steps ▸')
        self.expand.setCheckable(True)
        self.expand.setAccessibleName('Show all mechanic sequence steps')
        for button in (self.open_button,self.start_button,self.expand):
            controls.addWidget(button)
        layout.addLayout(controls)
        self.title = W.QLabel('Open a mechanic sequence')
        self.title.setWordWrap(True)
        self.status = W.QLabel('Waiting for committed input evidence')
        self.status.setWordWrap(True)
        self.status.setAccessibleName('Mechanic sequence status')
        self.rows = W.QListWidget()
        self.rows.setAccessibleName('Ordered mechanic steps')
        self.rows.setSelectionMode(W.QAbstractItemView.NoSelection)
        layout.addWidget(self.title)
        layout.addWidget(self.status)
        layout.addWidget(self.rows)
        self.open_button.clicked.connect(self.open_profile)
        self.start_button.clicked.connect(self.start_here)
        self.expand.toggled.connect(self.refresh)

    def open_profile(self):
        path, _ = W.QFileDialog.getOpenFileName(self, 'Open mechanic sequence', '', 'Mechanic sequences (*.sequence.toml)')
        if not path:
            return
        try:
            # No game-outcome adapters are registered in the first slice.
            # Unsupported predicates are refused, never treated as manual wins.
            profile = load_profile(path)
            self.tracker = SequenceTracker(profile)
            self.profile_loaded.emit()
            self.refresh()
        except (OSError, SequenceError) as exc:
            self.status.setText(str(exc))

    def accept_receipt(self, receipt):
        self.latest = receipt
        if self.tracker is not None:
            changed = self.tracker.consume(receipt)
            if changed:
                # Static checkmarks respect reduced motion and avoid repeated
                # sounds on restore. Qt exposes labels and checked state.
                self.rows.setAccessibleDescription(f'Step {len(self.tracker.hits)} completed')
        self.refresh()

    def start_here(self):
        if self.tracker is None or self.latest is None:
            return
        receipt = self.latest
        try:
            self.tracker.start(session=receipt.session,take=receipt.take,boundary=receipt.boundary,
                               precision=receipt.precision,held=receipt.held,revision=receipt.revision)
            self.refresh()
        except SequenceError as exc:
            self.status.setText(str(exc))

    def boundary_changed(self, snapshot, *, failed=False):
        if self.tracker is None or not hasattr(self.tracker, 'boundary'):
            return
        tracker = self.tracker
        token = snapshot.owner_token if snapshot is not None else None
        if failed or snapshot is None or snapshot.error or snapshot.unknown_state or not token:
            tracker.suspend('Owner acknowledgement unavailable; progress retained')
            self.latest = None
        elif snapshot.clock_precision != tracker.profile.precision:
            tracker.suspend('Owner precision changed or is unavailable; start a new guide')
            self.latest = None
        elif snapshot.take_path != tracker.take or token['session_id'] != tracker.session:
            tracker.suspend('Recording lineage changed; start a new guide')
            self.latest = None
        elif snapshot.tick < tracker.boundary:
            receipt = snapshot.receipt or {}
            valid = (receipt.get('result') == 'pass' and receipt.get('mode') == 'restore'
                     and receipt.get('verify_level') == 'full_root' and receipt.get('canary') == 'pass'
                     and not receipt.get('fallback') and receipt.get('to_tick') == snapshot.tick)
            tracker.restore(boundary=snapshot.tick,revision=token['revision'],validated_full_restore=valid)
            self.latest = None
        elif snapshot.tick > tracker.boundary:
            tracker.suspend('Per-boundary input evidence missing; progress retained')
            self.latest = None
        self.refresh()

    def refresh(self, *_):
        tracker = self.tracker
        identity = (tracker.profile.sha256, tuple(tracker.hits), tracker.status, tracker.active, self.expand.isChecked(), self.latest is not None) if tracker is not None else None
        if identity is not None and identity == self._rendered:
            return
        self._rendered = identity
        self.expand.setText('All steps ▾' if self.expand.isChecked() else 'All steps ▸')
        self.start_button.setEnabled(tracker is not None and self.latest is not None)
        if tracker is None:
            return
        self.title.setText(f'{tracker.profile.title} · {len(tracker.hits)}/{len(tracker.profile.steps)}')
        self.status.setText(tracker.status)
        self.rows.clear()
        active = len(tracker.hits)
        if self.expand.isChecked():
            indices = range(len(tracker.profile.steps))
        else:
            indices = range(max(0,active-1), min(len(tracker.profile.steps),active+3))
        for index in indices:
            step = tracker.profile.steps[index]
            done = index < active
            item = W.QListWidgetItem(('✓ ' if done else '› ' if index == active else '  ') + step.label)
            item.setFlags(C.Qt.ItemIsEnabled)
            item.setData(C.Qt.AccessibleTextRole, f'{step.label}: {"completed" if done else "current" if index == active else "pending"}')
            font = item.font(); font.setStrikeOut(done); item.setFont(font)
            self.rows.addItem(item)
