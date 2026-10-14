"""Frame window finder dialog: a thin client of the owner's analysis jobs.

Not wired into the More menu yet. install_window_finder(workspace) adds the menu entry and
registers the action (category "Analysis") once it has passed a live run.
"""
from __future__ import annotations

import threading

from PySide6 import QtCore as C, QtWidgets as W


class _Bridge(C.QObject):
    done = C.Signal(str, object, object)     # what, result, error


class WindowFinderDialog(W.QDialog):
    def __init__(self, parent=None, *, port=47610, frame=None, current=None):
        super().__init__(parent)
        self.setWindowTitle('Find a frame window')
        self.port, self.job_id, self.result, self._busy = port, None, None, False
        self.bridge = _Bridge()
        self.bridge.done.connect(self._finished)
        form = W.QFormLayout()
        self.frame = W.QSpinBox(); self.frame.setRange(0, 10_000_000)
        self.until = W.QSpinBox(); self.until.setRange(0, 10_000_000)
        if frame is not None:
            self.frame.setValue(frame)
            self.until.setValue(min(frame + 30, current) if current else frame + 30)
        self.shift = W.QSpinBox(); self.shift.setRange(0, 30); self.shift.setValue(3)
        self.hold = W.QSpinBox(); self.hold.setRange(0, 15)
        self.predicate = W.QLineEdit('sem.pc.area == orig')
        self.by = W.QCheckBox('Pass if it holds at any frame up to G')
        form.addRow('Press at frame', self.frame)
        form.addRow('Check at frame G', self.until)
        form.addRow('Shift up to ± frames', self.shift)
        form.addRow('Hold up to ± frames', self.hold)
        form.addRow('Check', self.predicate)
        form.addRow('', self.by)
        self.progress = W.QProgressBar(); self.progress.setRange(0, 1); self.progress.setValue(0)
        self.text = W.QPlainTextEdit(); self.text.setReadOnly(True)
        buttons = W.QHBoxLayout()
        self.start_button = W.QPushButton('Find window'); self.start_button.clicked.connect(self.start)
        self.cancel_button = W.QPushButton('Stop'); self.cancel_button.clicked.connect(self.cancel)
        self.export_button = W.QPushButton('Save as trainer trial…'); self.export_button.clicked.connect(self.export)
        self.cancel_button.setEnabled(False); self.export_button.setEnabled(False)
        for b in (self.start_button, self.cancel_button, self.export_button):
            buttons.addWidget(b)
        layout = W.QVBoxLayout(self)
        layout.addLayout(form); layout.addWidget(self.progress); layout.addWidget(self.text); layout.addLayout(buttons)
        self.timer = C.QTimer(self); self.timer.setInterval(500); self.timer.timeout.connect(self.poll)

    def _call(self, what, op, **args):
        """Owner requests run off the GUI thread; a slow slice reads as busy, not as an error."""
        from kptas.analysis.cli import request

        def work():
            try:
                self.bridge.done.emit(what, request(self.port, op, timeout=30 if op != 'analysis_status' else 2,
                                                    **args), None)
            except Exception as exc:   # noqa: BLE001 - shown in the dialog
                self.bridge.done.emit(what, None, exc)
        self._busy = True
        threading.Thread(target=work, daemon=True).start()

    def start(self):
        spec = {'frame': self.frame.value(), 'until': self.until.value(), 'shift': self.shift.value(),
                'hold': self.hold.value(), 'predicate': self.predicate.text(),
                'mode': 'by' if self.by.isChecked() else 'at'}
        self.start_button.setEnabled(False)
        self.text.setPlainText('Starting…')
        self._call('start', 'analysis_start', kind='window', spec=spec)

    def cancel(self):
        if self.job_id:
            self._call('cancel', 'analysis_cancel', job_id=self.job_id)

    def poll(self):
        if self.job_id and not self._busy:
            self._call('status', 'analysis_status', job_id=self.job_id)

    def export(self):
        path, _ = W.QFileDialog.getSaveFileName(self, 'Save trainer trial', 'window.sequence.toml',
                                                'Trials (*.sequence.toml)')
        if not path or self.result is None:
            return
        from kptas.analysis.window import export_trial
        try:
            out = export_trial(self.result, path)
            self.text.appendPlainText(f'Saved {out["path"]}. ' + ' '.join(out['notes']))
        except Exception as exc:   # noqa: BLE001
            self.text.appendPlainText(f'Could not save the trial: {exc}')

    def _finished(self, what, result, error):
        self._busy = False
        if error is not None:
            if isinstance(error, TimeoutError) and what == 'status':
                return   # the owner is inside a slice; poll again
            self.text.appendPlainText(f'{error}')
            if what == 'start':
                self.start_button.setEnabled(True)
            return
        if what == 'start':
            self.job_id = result['job_id']
            self.cancel_button.setEnabled(True)
            self.timer.start()
        if what in ('start', 'status'):
            self.progress.setRange(0, max(1, result.get('total') or 1))
            self.progress.setValue(result.get('done') or 0)
            if result.get('status') in ('completed', 'cancelled', 'failed'):
                self.timer.stop()
                self.result = result
                self.cancel_button.setEnabled(False)
                self.start_button.setEnabled(True)
                summary = result.get('summary') or {}
                lines = [summary.get('text') or '', result.get('error') or '']
                back = result.get('returned') or {}
                lines.append('Returned to your frame.' if back.get('ok') else
                             f'Did not return to your frame: {back.get("error")} {back.get("recover", "")}')
                self.text.setPlainText('\n'.join(x for x in lines if x))
                self.export_button.setEnabled('early' in summary)
            else:
                self.text.setPlainText(f'{result.get("phase")}: {result.get("done")} of {result.get("total")} trials')


def install_window_finder(workspace):
    """More > Find a frame window…, plus the 'analysis.window_finder' action."""
    from .actions import register_action
    port = getattr(getattr(workspace.controller, 'transport', None), 'port', 47610)
    holder = {}

    def show():
        dialog = holder.get('dialog')
        if dialog is None:
            dialog = holder['dialog'] = WindowFinderDialog(workspace.root, port=port)
        dialog.show(); dialog.raise_()
    action = workspace.overlay.more_menu.addAction('Find a frame window…')
    action.triggered.connect(show)
    register_action('analysis.window_finder', 'Find a frame window', category='Analysis', callback=show,
                    description='Measure how many frames earlier or later a press still works.')
    return holder
