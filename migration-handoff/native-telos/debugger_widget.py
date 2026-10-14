"""Optional snapshot inspector. Never polls the owner or infers engine state."""
from collections import deque
from datetime import datetime, timezone
import json
from itertools import islice
from time import monotonic

from PySide6 import QtCore as C, QtWidgets as W


# Mirrors DEBUG_OPERATIONS in the backend-neutral browser contract. An owner
# action being enabled is not a four-owner qualification report.
DEBUG_OPERATIONS = {
    'input.apply': 'Input', 'execution.pause': 'Pause', 'execution.resume': 'Resume',
    'execution.step': 'Step', 'execution.seek': 'Seek', 'execution.rewind': 'Rewind',
    'threads.count': 'Thread counts', 'threads.list': 'Thread list',
    'memory.read': 'Memory read', 'registers.read': 'Registers',
    'code.disassemble': 'Disassembly', 'breakpoints.manage': 'Breakpoints',
    'state.snapshot': 'Snapshot', 'state.restore': 'Restore',
}


def qualification_view(snapshot):
    """Native snapshots currently supply no session-bound capability reports."""
    token = snapshot.owner_token or {} if snapshot is not None else {}
    return {
        'schema': 'telos.debug-qualification-view/v1',
        'session': token.get('session_id'), 'revision': token.get('revision'),
        'target': None, 'boundary': None, 'source': 'capability-intersection',
        'reportErrors': {}, 'error': None,
        'operations': {key: {'label': label, 'state': 'unknown', 'unit': None,
                             'reason': 'No owner-authored qualification evidence.', 'layers': {}}
                       for key, label in DEBUG_OPERATIONS.items()},
    }


def recorded_state_projection(snapshot, observation):
    """Bounded display of an inspection already validated by WorkspaceController."""
    inspect = observation.get('inspect') if isinstance(observation, dict) else None
    if not isinstance(inspect, dict) or inspect.get('source') != 'recorded_journal':
        return None
    token = snapshot.owner_token or {}
    boundary = (token.get('session_id'), snapshot.take_path, snapshot.tick, token.get('revision'))
    rows = []
    for group in ('pc_float32', 'semantics', 'journal'):
        values = inspect.get(group)
        if not isinstance(values, dict):
            continue
        for key, value in islice(values.items(), 32):
            if type(value) in (str, int, float, bool) or value is None:
                rows.append(f'{group}.{str(key)[:64]} = {str(value)[:160]}')
            elif group == 'pc_float32' and isinstance(value, list):
                for index, scalar in enumerate(value[:4]):
                    if isinstance(scalar, dict):
                        rows.append(f'player.{str(key)[:64]}[{index}] = {str(scalar.get("float"))[:64]} · {str(scalar.get("bits"))[:32]}')
            elif isinstance(value, dict):
                for child, scalar in islice(value.items(), 8):
                    if type(scalar) in (str, int, float, bool) or scalar is None:
                        rows.append(f'{group}.{str(key)[:64]}.{str(child)[:64]} = {str(scalar)[:160]}')
            if len(rows) >= 64:
                break
        if len(rows) >= 64:
            break
    # Capture cadence: an uncaptured tick has the null digest, not a state hash.
    root = 'not captured' if inspect.get('captured') is False else inspect.get('state_root')
    return boundary, root, inspect.get('journal_sha256'), tuple(rows[:64])


class DebuggerDialog(W.QDialog):
    inspection_demand = C.Signal(bool)
    def __init__(self, parent, command):
        super().__init__(parent)
        self.setWindowTitle('Debugger')
        self.setModal(False)
        self.setSizeGripEnabled(True)
        self.settings = C.QSettings('KPTAS', 'TelosDebugger')
        self.resize(440, 320)
        geometry = self.settings.value('geometry')
        if isinstance(geometry, C.QByteArray):
            self.restoreGeometry(geometry)
        saved_pins = self.settings.value('pinned_fields', [])
        self.pins = []
        if isinstance(saved_pins, list):
            for field in saved_pins[:16]:
                if isinstance(field, str) and 0 < len(field) <= 160 and field not in self.pins:
                    self.pins.append(field)
        self._field_choices = None
        self.latest = None
        self.inspection = None
        self.history = deque(maxlen=128)
        self.activity_records = deque(maxlen=128)
        self.observed_at = None
        self.observed_monotonic = None
        self._history_session = None
        self._boundary = None
        self._rendered = None
        layout = W.QVBoxLayout(self)
        transport = W.QHBoxLayout()
        self.commands = []
        for label, action in (('Play / Pause / Stop', 'play_pause'), ('Frame back', 'rewind'), ('Frame advance', 'step')):
            button = W.QPushButton(label)
            button.setAccessibleName(label)
            button.clicked.connect(lambda checked=False, action=action: self.activate(action))
            self.commands.append(button)
            transport.addWidget(button)
        layout.addLayout(transport)
        seek_row = W.QHBoxLayout()
        self.seek_label = W.QLabel('&Seek position')
        self.target = W.QLineEdit('0')
        self.target.setMaxLength(19)
        self.target.setAccessibleName('Debugger exact seek position')
        self.seek_label.setBuddy(self.target)
        self.seek = W.QPushButton('Go')
        self.command = command
        self.seek.clicked.connect(self.seek_position)
        self.target.returnPressed.connect(self.seek_position)
        seek_row.addWidget(self.seek_label)
        seek_row.addWidget(self.target)
        seek_row.addWidget(self.seek)
        layout.addLayout(seek_row)
        self.status = W.QLabel('Waiting for the game to confirm')
        self.status.setWordWrap(True)
        self.status.setAccessibleName('Debugger evidence status')
        layout.addWidget(self.status)
        self.sections = []
        self.watches = self.section(layout, 'Watches', default=True)
        self.watch_controls = {}
        for key, label in (('input', 'Committed input'), ('owner', 'Owner and durability'), ('run', 'Run throughput')):
            check = W.QCheckBox(label)
            check.setChecked(self.settings.value('watch/' + key, True, type=bool))
            check.toggled.connect(lambda checked, key=key: self.preference('watch/' + key, checked))
            self.watch_controls[key] = check
            self.watches.layout().addWidget(check)
        self.values = W.QPlainTextEdit()
        self.values.setReadOnly(True)
        self.values.setMaximumBlockCount(40)
        self.values.setAccessibleName('Acknowledged watch values')
        self.watches.layout().addWidget(self.values)
        self.pinned = W.QListWidget()
        self.pinned.setAccessibleName('Pinned recorded fields and current values')
        self.pinned.setMaximumHeight(120)
        self.remove_pin = W.QPushButton('Remove selected watch')
        self.remove_pin.setAccessibleName('Remove selected pinned recorded field')
        self.remove_pin.clicked.connect(self.unpin_field)
        self.pinned.currentRowChanged.connect(lambda row: self.remove_pin.setEnabled(row >= 0))
        self.watches.layout().addWidget(self.pinned)
        self.watches.layout().addWidget(self.remove_pin)
        history = self.section(layout, 'Acknowledgement history')
        history_actions = W.QHBoxLayout()
        self.export_inspection = W.QPushButton('Export inspection…')
        self.export_inspection.setAccessibleName('Export captured inspection and session activity as JSON')
        self.export_inspection.setEnabled(False)
        self.export_inspection.clicked.connect(self.export_observation)
        self.clear_history = W.QPushButton('Clear history')
        self.clear_history.setEnabled(False)
        self.clear_history.clicked.connect(self.clear_observations)
        history_actions.addWidget(self.export_inspection)
        history_actions.addWidget(self.clear_history)
        history.layout().addLayout(history_actions)
        self.rows = W.QPlainTextEdit()
        self.rows.setReadOnly(True)
        self.rows.setMaximumBlockCount(128)
        self.rows.setAccessibleName('Last 128 observed owner boundaries')
        history.layout().addWidget(self.rows)
        recorded = self.section(layout, 'Recorded state')
        self.recorded_section = self.sections[-1][0]
        self.recorded_values = W.QPlainTextEdit()
        self.recorded_values.setReadOnly(True)
        self.recorded_values.setMaximumBlockCount(70)
        self.recorded_values.setAccessibleName('Recorded state at the inspected position')
        recorded.layout().addWidget(self.recorded_values)
        pin_row = W.QHBoxLayout()
        self.fields = W.QComboBox()
        self.fields.setAccessibleName('Available recorded fields to watch')
        self.pin_button = W.QPushButton('Pin watch')
        self.pin_button.setToolTip('Keep up to 16 recorded fields in Watches across sessions')
        self.pin_button.clicked.connect(self.pin_field)
        pin_row.addWidget(self.fields, 1)
        pin_row.addWidget(self.pin_button)
        recorded.layout().addLayout(pin_row)
        qualification = self.section(layout, 'Operation qualification')
        self.qualification = W.QPlainTextEdit()
        self.qualification.setReadOnly(True)
        self.qualification.setMaximumBlockCount(40)
        self.qualification.setAccessibleName('Cross-host debugger operation qualification')
        qualification.layout().addWidget(self.qualification)
        captured = self.section(layout, 'Imported memory scan')
        scan_actions = W.QHBoxLayout()
        scan_import = W.QPushButton('Import captured scan…')
        scan_import.setToolTip('Inspect a browser scan file without reading native game memory')
        scan_import.clicked.connect(self.import_memory_scan)
        self.scan_clear = W.QPushButton('Clear imported scan')
        self.scan_clear.setEnabled(False)
        self.scan_clear.clicked.connect(self.clear_memory_scan)
        scan_actions.addWidget(scan_import)
        scan_actions.addWidget(self.scan_clear)
        captured.layout().addLayout(scan_actions)
        self.scan_artifact = None
        self.scan_page = 0
        self.scan_status = W.QPlainTextEdit()
        self.scan_status.setReadOnly(True)
        self.scan_status.setMaximumBlockCount(32)
        self.scan_status.setAccessibleName('Imported captured memory provenance')
        self.scan_status.setPlainText('Import a captured scan to inspect saved bytes. Native memory is not read.')
        captured.layout().addWidget(self.scan_status)
        self.scan_rows = W.QTableWidget(0, 5)
        self.scan_rows.setHorizontalHeaderLabels(('Offset', 'Address', 'Captured value', 'Previous value', 'Raw bytes'))
        self.scan_rows.setEditTriggers(W.QAbstractItemView.NoEditTriggers)
        self.scan_rows.setAccessibleName('Imported captured scan matches')
        captured.layout().addWidget(self.scan_rows)
        scan_pages = W.QHBoxLayout()
        self.scan_previous = W.QPushButton('Previous matches')
        self.scan_next = W.QPushButton('Next matches')
        self.scan_previous.setEnabled(False)
        self.scan_next.setEnabled(False)
        self.scan_previous.clicked.connect(lambda: self.move_scan_page(-1))
        self.scan_next.clicked.connect(lambda: self.move_scan_page(1))
        self.scan_range = W.QLabel('No imported matches')
        scan_pages.addWidget(self.scan_previous)
        scan_pages.addWidget(self.scan_range)
        scan_pages.addWidget(self.scan_next)
        captured.layout().addLayout(scan_pages)
        diagnostics = self.section(layout, 'Capabilities and diagnostics')
        self.diagnostics = W.QPlainTextEdit()
        self.diagnostics.setReadOnly(True)
        self.diagnostics.setMaximumBlockCount(40)
        self.diagnostics.setAccessibleName('Backend capabilities and debugger limitations')
        diagnostics.layout().addWidget(self.diagnostics)
        close = W.QDialogButtonBox(W.QDialogButtonBox.Close)
        close.rejected.connect(self.close)
        layout.addWidget(close)

    def import_memory_scan(self):
        from .memory_scan_capture import load_capture
        path, _ = W.QFileDialog.getOpenFileName(self, 'Import captured memory scan', '', 'Captured scans (*.json)')
        if not path:
            return
        try:
            artifact = load_capture(path)
        except (OSError, ValueError, OverflowError) as exc:
            # Preserve the previous artifact and rows when a new import fails.
            W.QMessageBox.warning(self, 'Could not import scan', str(exc))
            return
        self.scan_artifact = artifact
        self.scan_page = 0
        self.render_memory_scan()

    def clear_memory_scan(self):
        self.scan_artifact = None
        self.scan_page = 0
        self.scan_rows.setRowCount(0)
        self.scan_status.setPlainText('Import a captured scan to inspect saved bytes. Native memory is not read.')
        self.scan_range.setText('No imported matches')
        for button in (self.scan_clear, self.scan_previous, self.scan_next):
            button.setEnabled(False)

    def move_scan_page(self, direction):
        if self.scan_artifact is None:
            return
        count = len(self.scan_artifact.document['matchedOffsets'])
        last = max(0, (count - 1) // 200)
        self.scan_page = min(last, max(0, self.scan_page + direction))
        self.render_memory_scan()

    def render_memory_scan(self):
        if self.scan_artifact is None:
            return
        artifact = self.scan_artifact
        doc, capture = artifact.document, artifact.document['capture']
        previous = doc['previousCapture']
        # These are imported declarations. Never bind them to the native owner,
        # create a watch, refresh a process, or publish native inspection evidence.
        self.scan_status.setPlainText('\n'.join((
            'Imported captured bytes · Native memory is not read',
            'File SHA-256: ' + artifact.sha256,
            f"Declared session: {doc['session']} · PID: {doc['pid']} · epoch: {capture['workspaceEpoch']}",
            f"Captured: {capture['capturedAt']} · Exported: {doc['exportedAt']}",
            'Previous capture: ' + (previous['capturedAt'] if previous is not None else 'Absent'),
            f"Declared source: {capture['source']} · {capture['pointerBits']}-bit · {capture['byteOrder']} endian",
            f"Range: 0x{capture['address']:08x} · {capture['byteLength']} bytes · {capture['valueType']}",
            'Target declaration: ' + json.dumps(capture['targetDeclaration'], ensure_ascii=False),
            'Previous target declaration: ' + json.dumps(previous['targetDeclaration'] if previous is not None else None, ensure_ascii=False),
            'Comparison declaration: ' + json.dumps({key: value for key, value in doc['comparison'].items() if key != 'consideredOffsets'}, ensure_ascii=False),
            'Match claims are imported; comparison results are not independently qualified.',
            'This file does not restore state or grant debugger operations.',
            'Exporter qualification: ' + doc['qualification'],
        )))
        start = self.scan_page * 200
        rows = artifact.rows(start, 200)
        self.scan_rows.setRowCount(len(rows))
        for row, (offset, address, value, before, raw) in enumerate(rows):
            for column, text in enumerate((f'0x{offset:x}', f'0x{address:08x}', value, before, raw)):
                self.scan_rows.setItem(row, column, W.QTableWidgetItem(text))
        count = len(doc['matchedOffsets'])
        self.scan_range.setText(f'{start + 1}–{start + len(rows)} of {count}' if rows else '0 matches')
        self.scan_clear.setEnabled(True)
        self.scan_previous.setEnabled(start > 0)
        self.scan_next.setEnabled(start + 200 < count)

    def section(self, layout, title, default=False):
        toggle = W.QToolButton()
        toggle.setText(title)
        toggle.setCheckable(True)
        toggle.setChecked(self.settings.value('section/' + title, default, type=bool))
        toggle.setToolButtonStyle(C.Qt.ToolButtonTextBesideIcon)
        body = W.QWidget()
        body.setLayout(W.QVBoxLayout())
        body.layout().setContentsMargins(4, 4, 4, 4)
        def changed(checked):
            body.setVisible(checked)
            toggle.setArrowType(C.Qt.DownArrow if checked else C.Qt.RightArrow)
            self.preference('section/' + title, checked)
        toggle.toggled.connect(changed)
        body.setVisible(toggle.isChecked())
        toggle.setArrowType(C.Qt.DownArrow if toggle.isChecked() else C.Qt.RightArrow)
        layout.addWidget(toggle)
        layout.addWidget(body)
        self.sections.append((toggle, body))
        return body

    def preference(self, key, value):
        self.settings.setValue(key, value)
        self._rendered = None
        self.render()
        self.update_inspection_demand()

    def update_inspection_demand(self):
        self.inspection_demand.emit(self.isVisible() and hasattr(self, 'recorded_section') and
                                    (self.recorded_section.isChecked() or (bool(self.pins) and self.watches.isVisible())))

    def pin_field(self):
        field = self.fields.currentData()
        if isinstance(field, str) and field not in self.pins and len(self.pins) < 16:
            self.pins.append(field)
            self.preference('pinned_fields', list(self.pins))

    def unpin_field(self):
        row = self.pinned.currentRow()
        if 0 <= row < len(self.pins):
            self.pins.pop(row)
            self.preference('pinned_fields', list(self.pins))

    def render_pins(self, snapshot, error):
        token = snapshot.owner_token or {} if snapshot is not None else {}
        boundary = None if snapshot is None else (token.get('session_id'), snapshot.take_path, snapshot.tick, token.get('revision'))
        current = (snapshot is not None and not snapshot.unknown_state and not snapshot.error and not error
                   and self.inspection is not None and self.inspection[0] == boundary)
        fields = {}
        if current:
            for row in self.inspection[3]:
                field, separator, value = row.partition(' = ')
                if separator and field not in fields:
                    fields[field] = value
        if self.recorded_values.isVisible():
            choices = tuple(fields)
            if choices != self._field_choices:
                selected = self.fields.currentData()
                self.fields.clear()
                for field in choices:
                    self.fields.addItem(field, field)
                index = self.fields.findData(selected)
                if index >= 0:
                    self.fields.setCurrentIndex(index)
                self._field_choices = choices
            self.fields.setEnabled(bool(choices))
            self.pin_button.setEnabled(bool(choices) and len(self.pins) < 16)
        if self.watches.isVisible():
            selected = self.pinned.currentRow()
            self.pinned.clear()
            for field in self.pins:
                value = fields.get(field, 'Not recorded at this position') if current else 'Awaiting inspection at the current position'
                item = W.QListWidgetItem(f'{field} = {value}')
                item.setToolTip(item.text())
                self.pinned.addItem(item)
            if 0 <= selected < len(self.pins):
                self.pinned.setCurrentRow(selected)
            self.pinned.setVisible(bool(self.pins))
            self.remove_pin.setVisible(bool(self.pins))
            self.remove_pin.setEnabled(self.pinned.currentRow() >= 0)

    def accept_inspection(self, evidence):
        self.inspection = evidence
        self._rendered = None
        self.render()

    def observe(self, snapshot, *, surface):
        # Only admitted command snapshots enter history; preview packets never do.
        # Controls consume the same projection as the original overlay. Do not
        # reconstruct capability rules from ready/playing/stop flags here.
        self.latest = (snapshot, surface)
        session = (snapshot.owner_token or {}).get('session_id') if snapshot is not None else None
        if session != self._history_session:
            self.history.clear()
            self.activity_records.clear()
            self._boundary = None
            self._history_session = session
            self.observed_at = None
            self.observed_monotonic = None
        self.export_inspection.setEnabled(bool(session))
        # Timestamp every admitted observation, even when the paused position
        # is unchanged. History deduplication is not observation freshness.
        if snapshot is not None:
            self.observed_at = datetime.now(timezone.utc).isoformat()
            self.observed_monotonic = monotonic()
        if snapshot is not None and not snapshot.unknown_state and not snapshot.error:
            token = snapshot.owner_token or {}
            boundary = (token.get('session_id'), snapshot.take_path, snapshot.tick, token.get('revision'))
            if boundary != self._boundary:
                self._boundary = boundary
                unit = 'frame' if snapshot.clock_precision == 'frame' else 'tick' if snapshot.clock_precision == 'tick' else 'position'
                self.history.append(f'{unit} {snapshot.tick} · durable {snapshot.durable_tick} · revision {token.get("revision", "unavailable")} · {snapshot.take_path}'[:256])
                self.activity_records.append({
                    'receivedAt': self.observed_at, 'session': session,
                    'label': 'Owner acknowledgement', 'text': self.history[-1], 'error': False,
                    'position': snapshot.tick, 'durablePosition': snapshot.durable_tick,
                    'unit': unit, 'revision': token.get('revision'), 'take': snapshot.take_path,
                })
        self.clear_history.setEnabled(bool(self.activity_records))
        if self.isVisible():
            self.render()

    def clear_observations(self):
        self.history.clear()
        self.activity_records.clear()
        self.clear_history.setEnabled(False)
        self._rendered = None
        self.render()

    def export_observation(self):
        """Export already observed evidence; never poll, seek, or capture the owner."""
        if self.latest is None:
            return
        snapshot, surface = self.latest
        token = snapshot.owner_token or {} if snapshot is not None else {}
        session = token.get('session_id')
        if not session:
            return
        now = datetime.now(timezone.utc)
        boundary = (session, snapshot.take_path, snapshot.tick, token.get('revision'))
        certain = not (snapshot.unknown_state or snapshot.error or surface.error)
        inspection = self.inspection if certain and self.inspection and self.inspection[0] == boundary else None
        age = max(0, (monotonic() - self.observed_monotonic) * 1000) if self.observed_monotonic is not None else None
        unit = snapshot.clock_precision or 'position'
        document = {
            'schema': 'telos-debugger-inspection/2', 'exportedAt': now.isoformat(),
            'session': session, 'source': 'Observed native owner acknowledgements and recorded journal inspection',
            'target': None, 'backend': 'native-owner', 'release': None,
            'executionBoundary': unit,
            'observation': {'receivedAt': self.observed_at, 'state': surface.status,
                            'ageMilliseconds': age, 'fresh': age is not None and age < 5000,
                            'position': snapshot.tick, 'unit': unit,
                            'unknownState': bool(snapshot.unknown_state), 'error': surface.error or snapshot.error or None,
                            'revision': token.get('revision')},
            'advertisedCapabilities': {name: True for name in sorted(snapshot.capabilities)},
            'capabilityQualification': qualification_view(snapshot),
            'operationProjection': {'schema': 'telos-inspection-operations/1', 'scope': 'native-workspace',
                                    'entries': [{'id': action.id, 'kind': 'owner-action',
                                                 'state': 'unknown', 'enabled': action.enabled, 'unit': None}
                                                for action in surface.actions]},
            'backendDetails': {'nativeOwner': {
                'durablePosition': snapshot.durable_tick, 'take': snapshot.take_path,
                'actionProjection': {'schema': surface.schema,
                    'actions': [{'id': action.id, 'label': action.label, 'enabled': action.enabled,
                                 'disabledReason': action.disabled_reason, 'checked': action.checked,
                                 'shortcut': action.shortcut} for action in surface.actions]},
            }},
            'activityOrder': 'newest-first', 'activityLimit': 128,
            'activity': [entry for entry in reversed(self.activity_records) if entry['session'] == session],
            'memory': None,
            'recordedState': ({'position': snapshot.tick, 'take': snapshot.take_path,
                               'stateRoot': inspection[1], 'journalHash': inspection[2],
                               'fields': list(inspection[3])} if inspection else None),
        }
        # Freeze the evidence before opening the dialog: later owner updates do
        # not change the identity of what the user chose to export.
        try:
            payload = (json.dumps(document, indent=2, ensure_ascii=False, allow_nan=False) + '\n').encode('utf-8')
        except (TypeError, ValueError) as error:
            self.status.setText('Could not serialize inspection: ' + str(error))
            return
        filename, _ = W.QFileDialog.getSaveFileName(
            self, 'Export inspection', 'telos-inspection-' + now.strftime('%Y%m%dT%H%M%SZ') + '.json',
            'JSON files (*.json)')
        if not filename:
            return
        output = C.QSaveFile(filename)
        if not output.open(C.QIODevice.OpenModeFlag.WriteOnly):
            self.status.setText('Could not export inspection: ' + output.errorString())
            return
        if output.write(payload) != len(payload):
            reason = output.errorString()
            output.cancelWriting()
            self.status.setText('Could not export inspection: ' + reason)
            return
        if not output.commit():
            self.status.setText('Could not export inspection: ' + output.errorString())
            return
        self.status.setText(f'Inspection exported with {len(document["activity"])} observed acknowledgements.')

    def render(self):
        if not self.isVisible() or self.latest is None:
            return
        snapshot, surface = self.latest
        error = surface.error
        token = snapshot.owner_token or {} if snapshot is not None else {}
        note = snapshot.last_inputs if snapshot is not None else None
        run = snapshot.run if snapshot is not None else None
        # Compare only displayed fields, never authored rows, score text or receipts.
        evidence = None if snapshot is None else (
            snapshot.tick, snapshot.take_path, snapshot.durable_tick, snapshot.clock_precision,
            snapshot.mode, snapshot.unknown_state, snapshot.error, token.get('session_id'),
            token.get('revision'), snapshot.physical_input_error,
            tuple(sorted(snapshot.capabilities)) if self.diagnostics.isVisible() else (),
            None if note is None else (note.held, note.buttons, note.mouse_abs, note.mouse_rel, note.wheel),
            None if run is None else (run.run_id, run.status, run.executed_frames, run.total_frames, run.elapsed_seconds))
        signature = (evidence, surface,
                     tuple(check.isChecked() for check in self.watch_controls.values()),
                     tuple(toggle.isChecked() for toggle, _ in self.sections), len(self.history), self._boundary, self.inspection, tuple(self.pins))
        if signature == self._rendered:
            return
        self._rendered = signature
        actions = {action.id: action for action in surface.actions}
        for button, action_id in zip(self.commands, ('play_pause', 'rewind', 'step')):
            action = actions[action_id]
            button.setEnabled(action.enabled)
            button.setText(action.label)
            button.setAccessibleName(action.label)
            button.setToolTip(action.disabled_reason or action.label)
        unit = 'Frame' if snapshot is not None and snapshot.clock_precision == 'frame' else 'Tick' if snapshot is not None and snapshot.clock_precision == 'tick' else 'Position'
        self.seek_label.setText('&Seek ' + unit.lower())
        self.target.setAccessibleName('Debugger exact seek ' + unit.lower())
        self.target.setToolTip(f'Enter a whole {unit.lower()} from {surface.seek_min} to {surface.seek_max}')
        self.seek.setEnabled(actions['seek'].enabled)
        self.seek.setAccessibleName(actions['seek'].label)
        self.seek.setToolTip(actions['seek'].disabled_reason or actions['seek'].label)
        self.render_pins(snapshot, error)
        if snapshot is None:
            self.status.setText(error or 'Waiting for the game to confirm')
            self.values.setPlainText('Watch values unavailable')
            self.qualification.setPlainText('Session: Unknown · target: Unknown · boundary: Unknown\n' + '\n'.join(f'{label}: Unknown' for label in DEBUG_OPERATIONS.values()))
            return
        uncertain = error or snapshot.error or ('owner outcome unknown' if snapshot.unknown_state else '')
        self.status.setText(('Snapshot values may be stale · ' + str(uncertain)) if uncertain else
                            f'{surface.status} · {unit} {surface.committed_tick} · {snapshot.mode}')
        token = snapshot.owner_token or {}
        values = []
        if self.watch_controls['owner'].isChecked():
            values.extend((f'Take: {snapshot.take_path}', f'{unit}: {snapshot.tick} · saved through: {snapshot.durable_tick}',
                           f'Session: {token.get("session_id", "unavailable")} · revision: {token.get("revision", "unavailable")}',
                           f'Clock precision: {snapshot.clock_precision or "unavailable"}'))
        if self.watch_controls['input'].isChecked():
            note = snapshot.last_inputs
            values.append('Committed input: unavailable' if note is None else
                          f'Committed input: held={note.held}; buttons={note.buttons}; mouse={note.mouse_abs}/{note.mouse_rel}; wheel={note.wheel}')
        if self.watch_controls['run'].isChecked():
            run = snapshot.run
            if run is None:
                values.append('Run metrics: no owner run receipt')
            else:
                values.append(f'Run {run.run_id}: {run.status} · {run.executed_frames}/{run.total_frames} frames · {run.elapsed_seconds:.3f}s')
                average = f'{run.executed_frames / run.elapsed_seconds:.2f} frames/s' if run.elapsed_seconds > 0 else 'unavailable'
                values.append('Run average throughput: ' + average + ' (not presentation FPS)')
        self.values.setPlainText('\n'.join(values)[:8192])
        if self.rows.isVisible():
            self.rows.setPlainText('\n'.join(self.history)[:32768])
        if self.recorded_values.isVisible():
            boundary = (token.get('session_id'), snapshot.take_path, snapshot.tick, token.get('revision'))
            if snapshot.unknown_state or snapshot.error or error:
                text = 'Recorded state unavailable while the current position is uncertain.'
            elif self.inspection is None or self.inspection[0] != boundary:
                text = ('Advance one position with this section open to inspect its recorded state.'
                        if snapshot.mode == 'live' and 'atomic_step_observation' in snapshot.capabilities else
                        'This backend does not provide recorded-state inspection for this playback mode.')
            else:
                _, root, journal, rows = self.inspection
                text = f'Inspected {unit.lower()} {snapshot.tick}\nState hash: {root}\nJournal hash: {journal}\n' + ('\n'.join(rows) or 'No recorded scalar values are available.')
            self.recorded_values.setPlainText(text[:16384])
        if self.qualification.isVisible():
            qualification = qualification_view(snapshot)
            lines = [f'Session: {qualification["session"] or "Unknown"} · revision: {qualification["revision"] if qualification["revision"] is not None else "Unknown"}',
                     'Qualified target: Unknown · qualified boundary: Unknown',
                     'Source: capability intersection · no native owner-authored reports', '']
            lines.extend(f'{value["label"]}: Unknown' for value in qualification['operations'].values())
            lines.extend(('', 'Observed owner actions (current dispatch availability):'))
            lines.extend(f'{action.label}: {"Enabled" if action.enabled else "Unavailable"}' +
                         (f' · {action.disabled_reason}' if action.disabled_reason else '') for action in surface.actions)
            self.qualification.setPlainText('\n'.join(lines))
        if self.diagnostics.isVisible():
            self.diagnostics.setPlainText(('Owner capabilities: ' + ', '.join(sorted(snapshot.capabilities)) +
            '\n\nHistory contains observed positions, not every executed frame.'
            '\nRegisters, call stacks, memory watches and breakpoints are not available from this recording backend.'
            '\n' + (snapshot.physical_input_error or ''))[:8192])

    def activate(self, action, **values):
        # The projected view can age before a click. NativeSurface rechecks the
        # original controls and returns local dispatch/refusal, never an ACK.
        result = self.command(action, **values)
        if not result.dispatched:
            self.status.setText(result.reason or 'This action is unavailable')

    def seek_position(self):
        if self.latest is None or not self.seek.isEnabled():
            return
        text = self.target.text().strip()
        surface = self.latest[1]
        if not text or not text.isascii() or not text.isdecimal():
            self.status.setText('Enter a whole position using digits 0–9')
            self.target.setFocus()
            return
        position = int(text)
        if not surface.seek_min <= position <= surface.seek_max:
            self.status.setText(f'Position must be between {surface.seek_min} and {surface.seek_max}')
            self.target.setFocus()
            return
        self.activate('seek', tick=position)

    def showEvent(self, event):
        super().showEvent(event)
        self._rendered = None
        self.render()
        self.update_inspection_demand()

    def hideEvent(self, event):
        self.inspection_demand.emit(False)
        super().hideEvent(event)

    def closeEvent(self, event):
        self.settings.setValue('geometry', self.saveGeometry())
        super().closeEvent(event)
