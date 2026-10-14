"""More > Speedruns…: the speedrun catalog as a list you can add to, rename and prune.

Each run shows "New (start recording)" first, then its checkpoint saves, scores
and attached takes. Starting from a save is not an owner operation yet, so the
New row shows the exact steps (or the --load-game6 command for Game6) instead
of sending blind clicks. Load score here uses the window's existing Live score
path. Nothing here deletes a save or a recording.
"""
from __future__ import annotations

from pathlib import Path

from PySide6 import QtCore as C, QtGui as G, QtWidgets as W

from . import speedrun_catalog as sc

ROLE = C.Qt.UserRole + 1
NEW_LABEL = 'New (start recording)'


class SpeedrunDialog(W.QDialog):
    def __init__(self, parent=None, *, take_path=None, load_score=None, path=None):
        super().__init__(parent)
        self.setWindowTitle('Speedruns')
        self.setModal(False)
        self.setSizeGripEnabled(True)
        self.resize(720, 520)
        self.path = Path(path) if path else sc.catalog_path()
        self.take_path = take_path or (lambda: None)
        self.load_score = load_score
        self.catalog = sc.Catalog()
        self.command = ''
        layout = W.QVBoxLayout(self)
        game_row = W.QHBoxLayout()
        self.game_label = W.QLabel()
        self.game_label.setWordWrap(True)
        self.game_label.setTextInteractionFlags(C.Qt.TextSelectableByMouse)
        change = W.QPushButton('Change game folder…')
        change.clicked.connect(self.change_game)
        game_row.addWidget(self.game_label, 1)
        game_row.addWidget(change)
        layout.addLayout(game_row)
        self.tree = W.QTreeWidget()
        self.tree.setColumnCount(3)
        self.tree.setHeaderLabels(['Run', 'Save', 'Status'])
        self.tree.setAccessibleName('Speedruns')
        self.tree.setAccessibleDescription('Each run starts with New, then its checkpoint saves, scores and takes.')
        self.tree.header().setSectionResizeMode(0, W.QHeaderView.Stretch)
        self.tree.currentItemChanged.connect(lambda *_: self.selection_changed())
        self.tree.itemActivated.connect(lambda *_: self.start_recording())
        layout.addWidget(self.tree, 1)
        self.details = W.QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setMaximumHeight(150)
        self.details.setAccessibleName('Run details')
        layout.addWidget(self.details)
        edit_row, use_row = W.QHBoxLayout(), W.QHBoxLayout()
        self.buttons = {}
        for row, key, text, slot, tip in (
                (edit_row, 'add', 'Add run…', self.add_run, 'Add a run to the list.'),
                (edit_row, 'rename', 'Rename…', self.rename_run, 'Rename the selected run.'),
                (edit_row, 'remove', 'Remove', self.remove_run, 'Remove the run from the list. Files stay on disk.'),
                (edit_row, 'seed', 'Import runs', self.seed, 'Add runs from installed speedrun saves and the score library.'),
                (use_row, 'start', 'Start recording', self.start_recording, 'Show how to start a recording from this save.'),
                (use_row, 'copy', 'Copy command', self.copy_command, 'Copy the start command.'),
                (use_row, 'start_save', 'Set start save…', self.set_start_save, 'Choose the save that New starts from.'),
                (use_row, 'attach', 'Attach this take', self.attach_take, 'Add the take you are recording to this run.'),
                (use_row, 'score', 'Load score here', self.load_selected_score, 'Play this score from the current frame.')):
            button = W.QPushButton(text)
            button.setToolTip(tip)
            button.clicked.connect(slot)
            row.addWidget(button)
            self.buttons[key] = button
        edit_row.addStretch()
        use_row.addStretch()
        layout.addLayout(edit_row)
        layout.addLayout(use_row)
        self.reload()

    # ------------------------------------------------------------ list

    def reload(self, select=None):
        self.catalog, warning = sc.load_catalog(self.path)
        self.game_label.setText(f'Game folder: {self.catalog.game}' if self.catalog.game
                                else 'Game folder: not set. Choose the folder with swkotor.exe.')
        self.tree.clear()
        bold = self.font()
        bold.setBold(True)
        for run in self.catalog.runs:
            top = W.QTreeWidgetItem([run.name, run.start_save, run.status])
            top.setFont(0, bold)
            top.setData(0, ROLE, ('run', run.id, None))
            top.setToolTip(0, run.attribution or run.source)
            new = W.QTreeWidgetItem([NEW_LABEL, run.start_save, ''])
            new.setData(0, ROLE, ('new', run.id, None))
            new.setToolTip(0, 'Start a new recording from this run\'s start save.')
            top.addChild(new)
            for i, point in enumerate(run.checkpoints):
                item = W.QTreeWidgetItem([point.name, point.save, point.area])
                item.setData(0, ROLE, ('checkpoint', run.id, i))
                top.addChild(item)
            for i, score in enumerate(run.scores):
                state = 'replay-qualified' if score.replay_qualified else 'not replay-qualified'
                item = W.QTreeWidgetItem([Path(score.path).name, '', f'{score.status or "no status"} · {state}'])
                item.setData(0, ROLE, ('score', run.id, i))
                item.setToolTip(0, score.path)
                top.addChild(item)
            for i, take in enumerate(run.recordings):
                item = W.QTreeWidgetItem([Path(take).name, '', 'take'])
                item.setData(0, ROLE, ('recording', run.id, i))
                item.setToolTip(0, take)
                top.addChild(item)
            self.tree.addTopLevelItem(top)
            if select == run.id:
                self.tree.setCurrentItem(top)
        if not self.catalog.runs:
            self.details.setPlainText(warning or 'No runs yet. Choose Import runs, or Add run.')
        elif warning:
            self.details.setPlainText(warning)
        self.selection_changed()

    def selected(self):
        item = self.tree.currentItem()
        if item is None:
            return None, None, None
        kind, run_id, index = item.data(0, ROLE)
        return kind, self.catalog.get(run_id), index

    def selection_changed(self):
        kind, run, index = self.selected()
        self.command = ''
        for key in ('rename', 'remove', 'start_save', 'attach'):
            self.buttons[key].setEnabled(run is not None)
        self.buttons['start'].setEnabled(kind in ('new', 'checkpoint'))
        self.buttons['score'].setEnabled(kind == 'score' and self.load_score is not None)
        self.buttons['copy'].setEnabled(False)
        if run is None:
            return
        if kind in ('new', 'checkpoint'):
            self.show_plan(run, run.checkpoints[index] if kind == 'checkpoint' else None)
            return
        lines = [run.name]
        if kind == 'score':
            score = run.scores[index]
            lines += [score.path, f'Segment: {score.segment}', f'Variant: {score.variant}',
                      f'Status: {score.status or "none"}',
                      'Replay-qualified: yes' if score.replay_qualified else 'Replay-qualified: no',
                      'It needs the game at the exact point where this section starts.']
        elif kind == 'recording':
            lines.append(run.recordings[index])
        else:
            lines += [f'Category: {run.category or "none"}', f'Runner: {run.runner or "unknown"}',
                      f'Source: {run.source or "none"}', run.attribution, run.status, run.notes]
        self.details.setPlainText('\n'.join(line for line in lines if line))

    def show_plan(self, run, point):
        plan = sc.start_plan(self.catalog, run, point)
        if plan.problem:
            self.details.setPlainText(plan.problem)
            return
        lines = [f'{i}. {step}' for i, step in enumerate(plan.steps, 1)]
        if plan.alternative:
            lines.append(plan.alternative)
        lines.append(sc.PIN_NOTE)
        self.details.setPlainText('\n'.join(lines))
        self.command = plan.command
        self.buttons['copy'].setEnabled(bool(plan.command))

    # ------------------------------------------------------------ actions

    def _save(self, select=None):
        try:
            sc.save_catalog(self.catalog, self.path)
        except (OSError, ValueError) as exc:
            self.details.setPlainText(f'Not saved: {exc}')
            return False
        self.reload(select)
        return True

    def add_run(self):
        name, ok = W.QInputDialog.getText(self, 'Add run', 'Run name')
        if not ok:
            return
        try:
            run = sc.add_run(self.catalog, name)
        except sc.CatalogError as exc:
            self.details.setPlainText(str(exc))
            return
        self._save(run.id)

    def rename_run(self):
        _kind, run, _ = self.selected()
        if run is None:
            return
        name, ok = W.QInputDialog.getText(self, 'Rename run', 'Run name', text=run.name)
        if ok:
            try:
                sc.rename_run(self.catalog, run.id, name)
            except sc.CatalogError as exc:
                self.details.setPlainText(str(exc))
                return
            self._save(run.id)

    def remove_run(self):
        _kind, run, _ = self.selected()
        if run is None:
            return
        answer = W.QMessageBox.question(
            self, 'Remove run',
            f'Remove "{run.name}" from the list?\n\nIts saves and recordings stay on disk. '
            'To delete installed speedrun saves, use telos saves remove.',
            W.QMessageBox.Yes | W.QMessageBox.Cancel, W.QMessageBox.Cancel)
        if answer == W.QMessageBox.Yes:
            sc.remove_run(self.catalog, run.id)
            self._save()

    def seed(self):
        game = self.catalog.game or self.choose_game()
        if not game:
            return
        library = self.catalog.library
        if not library:
            library, _ = W.QFileDialog.getOpenFileName(self, 'Choose the score library (optional)', '',
                                                       'Score library (score-library.json)')
        added = sc.seed(self.catalog, game, library)
        if self._save():
            self.details.setPlainText(f'{len(added)} runs added.' if added else 'No new runs found.')

    def choose_game(self):
        folder = W.QFileDialog.getExistingDirectory(self, 'Choose the game folder (with swkotor.exe)',
                                                    self.catalog.game or str(Path.home()))
        return folder or ''

    def change_game(self):
        folder = self.choose_game()
        if folder:
            self.catalog.game = folder
            self._save()

    def set_start_save(self):
        _kind, run, _ = self.selected()
        if run is None:
            return
        from .save_packs import listing
        rows = listing(self.catalog.game) if self.catalog.game else []
        if not rows:
            self.details.setPlainText('No saves found. Set the game folder, or install the speedrun saves.')
            return
        labels = ['(none)'] + [f'{r["folder"]}  {r["name"]}' for r in rows]
        current = next((i + 1 for i, r in enumerate(rows) if r['folder'] == run.start_save), 0)
        choice, ok = W.QInputDialog.getItem(self, 'Set start save', 'Start save', labels, current, False)
        if ok:
            run.start_save = '' if choice == '(none)' else rows[labels.index(choice) - 1]['folder']
            self._save(run.id)

    def start_recording(self):
        kind, run, index = self.selected()
        if kind in ('new', 'checkpoint'):
            self.show_plan(run, run.checkpoints[index] if kind == 'checkpoint' else None)

    def copy_command(self):
        if self.command:
            G.QGuiApplication.clipboard().setText(self.command)

    def attach_take(self):
        _kind, run, _ = self.selected()
        take = self.take_path()
        if run is None:
            return
        if not take:
            self.details.setPlainText('No take is recording. Attach works while Stasis is connected to a game.')
            return
        if str(take) not in run.recordings:
            run.recordings.append(str(take))
            self._save(run.id)

    def load_selected_score(self):
        kind, run, index = self.selected()
        if kind == 'score' and self.load_score is not None:
            self.load_score(run.scores[index].path)


def install_speedrun_panel(window):
    """Add More > Speedruns… and its shortcut to a workspace window."""
    def take_path():
        snap = getattr(window, 'snapshot', None)
        return getattr(snap, 'take_path', None) if snap is not None else None
    dialog = SpeedrunDialog(window.root, take_path=take_path,
                            load_score=lambda path: window.overlay_action('score_load_here', path=path))
    def show():
        dialog.reload()
        dialog.show(); dialog.raise_(); dialog.activateWindow()
    action = window.overlay.more_menu.addAction('Speedruns…')
    action.setToolTip('Pick a speedrun, its saves and scores, and start a recording from a save')
    action.triggered.connect(show)
    from .actions import register_action
    register_action('speedruns.show', 'Show speedruns', category='Speedruns', default='Ctrl+Alt+J', callback=show,
                    description='Open the speedrun list. Works while the Stasis window has focus.')
    return dialog
