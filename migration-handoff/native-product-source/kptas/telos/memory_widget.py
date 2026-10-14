"""Memory panel: RAM watch and RAM search read from the take's recorded frames.

Read-only. Values come from the captured page store on disk, never from the
running game. All bundle work runs on one worker thread (Bundle is not
thread-safe); only the newest requested frame is kept.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import math
import time

from PySide6 import QtCore as C, QtGui as G, QtWidgets as W

from . import memory as M


class _Bridge(C.QObject):
    done = C.Signal(object)


class _Graph(W.QWidget):
    """Value of one watch over recent captured frames."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.points = []
        self.current = None
        self.message = 'Select a number watch to see it over frames.'
        self.setMinimumHeight(90)
        self.setAccessibleName('Watch value over frames')

    def show_points(self, points, current, message=''):
        self.points = [(t, v) for t, v in points if isinstance(v, (int, float)) and not isinstance(v, bool)
                       and math.isfinite(v)]
        self.current, self.message = current, message
        self.update()

    def paintEvent(self, _event):
        p = G.QPainter(self)
        p.fillRect(self.rect(), self.palette().base())
        r = self.rect().adjusted(6, 6, -6, -16)
        if len(self.points) < 2:
            p.drawText(self.rect(), C.Qt.AlignCenter, self.message or 'Not enough captured frames to draw.')
            return
        t0, t1 = self.points[0][0], self.points[-1][0]
        vs = [v for _t, v in self.points]
        lo, hi = min(vs), max(vs)
        span_t, span_v = max(1, t1 - t0), (hi - lo) or 1.0
        def at(t, v):
            return C.QPointF(r.left() + (t - t0) / span_t * r.width(), r.bottom() - (v - lo) / span_v * r.height())
        p.setRenderHint(G.QPainter.Antialiasing)
        p.setPen(G.QPen(G.QColor('#65D6AD'), 1.5))
        p.drawPolyline(G.QPolygonF([at(t, v) for t, v in self.points]))
        p.setPen(self.palette().text().color())
        p.drawText(self.rect().adjusted(6, 0, -6, 0), C.Qt.AlignLeft | C.Qt.AlignBottom, f'{t0}')
        p.drawText(self.rect().adjusted(6, 0, -6, 0), C.Qt.AlignRight | C.Qt.AlignBottom, f'{t1}')
        p.drawText(self.rect().adjusted(6, 0, -6, 0), C.Qt.AlignHCenter | C.Qt.AlignBottom, f'{lo:.6g} … {hi:.6g}')


class WatchDialog(W.QDialog):
    def __init__(self, parent, watch: M.Watch | None = None):
        super().__init__(parent)
        self.setWindowTitle('Edit watch' if watch else 'Add watch')
        form = W.QFormLayout(self)
        self.name = W.QLineEdit(watch.name if watch else '')
        self.address = W.QLineEdit(watch.address_text if watch else '')
        self.address.setPlaceholderText('0x7A39FC, 0x4, 0x4')
        self.address.setToolTip('Hex address, then pointer offsets. Each offset but the last is followed as a pointer.')
        self.type = W.QComboBox(); self.type.addItems(list(M.TYPES))
        self.fmt = W.QComboBox(); self.fmt.addItems(list(M.FORMATS))
        self.length = W.QSpinBox(); self.length.setRange(1, 256); self.length.setValue(watch.length if watch else 16)
        if watch:
            self.type.setCurrentText(watch.type); self.fmt.setCurrentText(watch.fmt)
        form.addRow('&Name', self.name)
        form.addRow('&Address', self.address)
        form.addRow('&Type', self.type)
        form.addRow('&Format', self.fmt)
        form.addRow('Text &length', self.length)
        self.error = W.QLabel(''); self.error.setWordWrap(True)
        form.addRow(self.error)
        buttons = W.QDialogButtonBox(W.QDialogButtonBox.Ok | W.QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._accept); buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        self._old = watch
        self.result_watch = None

    def _accept(self):
        try:
            base, offsets = M.parse_address(self.address.text())
            old = self._old
            self.result_watch = M.Watch(self.name.text().strip(), base, offsets, self.type.currentText(),
                                        self.fmt.currentText(), self.length.value(),
                                        old.status if old and (base, offsets) == (old.base, old.offsets) else 'unverified',
                                        old.source if old else 'added by hand', old.note if old else '')
        except M.MemoryError_ as exc:
            self.error.setText(str(exc))
            return
        self.accept()


class MemoryPanel(W.QDialog):
    """More > Memory. `context()` returns (take path or None, frame)."""

    def __init__(self, parent, context):
        super().__init__(parent)
        self.setWindowTitle('Memory')
        self.setModal(False)
        self.setSizeGripEnabled(True)
        self.resize(620, 520)
        self.context = context
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='kptas-memory')
        self._bridge = _Bridge(); self._bridge.done.connect(self._finished)
        self._busy = False
        self._queued = None      # newest job waiting: (kind, args)
        self._readers = {}       # path -> RecordingMemory, worker thread only
        self.search = None
        self.watches = self._load_saved()
        self._last_frame = None
        self._debounce = C.QTimer(self); self._debounce.setSingleShot(True); self._debounce.setInterval(60)
        self._debounce.timeout.connect(self.refresh)

        layout = W.QVBoxLayout(self)
        self.where = W.QLabel('Open a recording to read memory.')
        self.where.setWordWrap(True)
        layout.addWidget(self.where)
        tabs = W.QTabWidget(); layout.addWidget(tabs)
        self.tabs = tabs

        # Watches
        page = W.QWidget(); v = W.QVBoxLayout(page)
        self.table = W.QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(['Name', 'Address', 'Type', 'Value', 'Checked'])
        self.table.setSelectionBehavior(W.QAbstractItemView.SelectRows)
        self.table.setEditTriggers(W.QAbstractItemView.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setAccessibleName('Memory watch list')
        self.table.itemSelectionChanged.connect(self._selection_changed)
        self.table.doubleClicked.connect(lambda *_: self.edit_watch())
        v.addWidget(self.table, 3)
        row = W.QHBoxLayout()
        for label, slot in (('Add watch', self.add_watch), ('Edit', self.edit_watch), ('Remove', self.remove_watch),
                            ('Load KotOR list', self.load_seed), ('Import…', self.import_watches),
                            ('Export…', self.export_watches)):
            b = W.QPushButton(label); b.clicked.connect(slot); row.addWidget(b)
        v.addLayout(row)
        freeze_row = W.QHBoxLayout()
        self.freeze = W.QPushButton('Freeze')
        self.freeze.setEnabled(False)
        self.freeze.setToolTip(M.FREEZE_REFUSAL)
        freeze_row.addWidget(self.freeze)
        why = W.QLabel(M.FREEZE_REFUSAL); why.setWordWrap(True)
        freeze_row.addWidget(why, 1)
        v.addLayout(freeze_row)
        self.graph = _Graph()
        v.addWidget(self.graph, 1)
        tabs.addTab(page, '&Watch')

        # Search
        page = W.QWidget(); v = W.QVBoxLayout(page)
        opts = W.QHBoxLayout()
        self.s_type = W.QComboBox(); self.s_type.addItems(list(M.SEARCH_TYPES)); self.s_type.setCurrentText('u32')
        self.s_region = W.QComboBox(); self.s_region.addItems(list(M.REGIONS))
        self.s_region.setToolTip('all: every recorded page. exe: swkotor.exe data. heap: game memory.')
        self.s_aligned = W.QCheckBox('Aligned'); self.s_aligned.setChecked(True)
        for label, w in (('Type', self.s_type), ('Region', self.s_region)):
            opts.addWidget(W.QLabel(label)); opts.addWidget(w)
        opts.addWidget(self.s_aligned); opts.addStretch(1)
        v.addLayout(opts)
        cmp_row = W.QHBoxLayout()
        self.s_cmp = W.QComboBox(); self.s_cmp.addItems(list(M.COMPARISONS))
        self.s_cmp.setToolTip('changed, unchanged, increased and decreased compare with the frame of the last step. '
                              'Float exact means the same bits.')
        self.s_value = W.QLineEdit(); self.s_value.setPlaceholderText('Value (floats also take 0x bits)')
        cmp_row.addWidget(self.s_cmp); cmp_row.addWidget(self.s_value, 1)
        v.addLayout(cmp_row)
        btns = W.QHBoxLayout()
        self.s_start = W.QPushButton('Start search'); self.s_start.clicked.connect(self.start_search)
        self.s_narrow = W.QPushButton('Narrow at this frame'); self.s_narrow.clicked.connect(self.narrow_search)
        self.s_reset = W.QPushButton('Reset'); self.s_reset.clicked.connect(self.reset_search)
        self.s_add = W.QPushButton('Watch selected'); self.s_add.clicked.connect(self.watch_result)
        for b in (self.s_start, self.s_narrow, self.s_reset, self.s_add):
            btns.addWidget(b)
        v.addLayout(btns)
        self.s_status = W.QLabel('Start a search at a frame, then move the playhead and narrow.')
        self.s_status.setWordWrap(True)
        v.addWidget(self.s_status)
        self.s_table = W.QTableWidget(0, 2)
        self.s_table.setHorizontalHeaderLabels(['Address', 'Value'])
        self.s_table.setSelectionBehavior(W.QAbstractItemView.SelectRows)
        self.s_table.setEditTriggers(W.QAbstractItemView.NoEditTriggers)
        self.s_table.verticalHeader().setVisible(False)
        self.s_table.horizontalHeader().setStretchLastSection(True)
        v.addWidget(self.s_table, 1)
        tabs.addTab(page, '&Search')
        self._fill_table([None] * len(self.watches))

    # -- persistence
    def _load_saved(self):
        path = M.watch_file()
        try:
            if path.is_file():
                return M.load_watches(path.read_text(encoding='utf-8'))
        except (OSError, M.MemoryError_):
            pass
        return list(M.KOTOR_K1_SEED)

    def _save(self):
        path = M.watch_file()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix('.tmp')
            tmp.write_text(M.dump_watches(self.watches), encoding='utf-8')
            tmp.replace(path)
        except OSError as exc:
            self.where.setText(f'Could not save the watch list: {exc}')

    # -- frame tracking
    def frame_changed(self, *_):
        if self.isVisible():
            self._debounce.start()

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh()

    def closeEvent(self, event):
        super().closeEvent(event)

    def refresh(self):
        path, tick = self.context()
        if not path or tick is None:
            self.where.setText('Open a recording to read memory.')
            return
        sel = self._selected()
        self._submit('watch', (path, int(tick), tuple(self.watches), sel))

    # -- worker
    def _submit(self, kind, args):
        if self._busy:
            # Keep only the newest frame read; search steps are never dropped.
            if kind != 'watch' or self._queued is None or self._queued[0] == 'watch':
                self._queued = (kind, args)
            return
        self._busy = True
        self._pool.submit(self._run, kind, args)

    def _reader(self, path):
        r = self._readers.get(path)
        if r is None:
            if len(self._readers) >= 3:
                keep = self.search.reader_path if self.search is not None else None
                for p in [p for p in self._readers if p != keep][:1]:
                    self._readers.pop(p).close()
            r = self._readers[path] = M.RecordingMemory(path)
        return r

    def _image(self, path, tick):
        r = self._reader(path)
        try:
            if r.bundle is None:
                r.open(); r.opened_at = time.monotonic()
            if tick > r.last_tick and time.monotonic() - r.opened_at > 5.0:
                # A live take grows. Reopen for a newer frame at most every 5 s;
                # meanwhile show the newest frame already on disk.
                r.close(); r.open(); r.opened_at = time.monotonic()
            return r.image(min(tick, r.last_tick)), r
        except M.MemoryError_:
            raise
        except Exception as exc:  # noqa: BLE001 - a take mid-write or missing: report, retry later
            r.close()
            raise M.MemoryError_(f'cannot read this recording yet: {exc}') from None

    def _run(self, kind, args):
        try:
            if kind == 'watch':
                path, tick, watches, sel = args
                img, r = self._image(path, tick)
                values = [M.read_watch(img, w) for w in watches]
                series = None
                if sel is not None and sel < len(watches) and watches[sel].type not in ('str', 'ptr'):
                    series = r.series(watches[sel], img.tick)
                out = ('watch', path, tick, img.tick, values, sel, series)
            elif kind == 'start':
                path, tick, typ, aligned, region = args
                img, _r = self._image(path, tick)
                s = M.RamSearch(typ, aligned, region)
                s.reader_path = path
                n = s.start(img)
                out = ('search', s, n, s.results(img), img.tick)
            elif kind == 'narrow':
                path, tick, s, cmp, value, text = args
                img, _r = self._image(s.reader_path, tick) if path == s.reader_path else self._image(path, tick)
                n = s.narrow(img, cmp, value, text)
                out = ('search', s, n, s.results(img), img.tick)
            else:
                out = ('error', 'unknown request')
        except M.MemoryError_ as exc:
            out = ('error', str(exc), kind)
        except Exception as exc:  # noqa: BLE001
            out = ('error', f'{type(exc).__name__}: {exc}', kind)
        self._bridge.done.emit(out)

    def _finished(self, out):
        self._busy = False
        queued, self._queued = self._queued, None
        try:
            if out[0] == 'watch':
                _k, path, tick, used, values, sel, series = out
                note = '' if used == tick else f' · values from captured frame {used}'
                self.where.setText(f'Frame {tick}{note} · read from the recording, not the game')
                self._fill_table(values)
                if sel is not None and series is not None:
                    self.graph.show_points(series, used)
                elif sel is not None:
                    self.graph.show_points([], used, 'Text and pointer watches have no graph.')
            elif out[0] == 'search':
                _k, s, n, results, used = out
                self.search = s
                steps = ' → '.join(f'{c} {t}'.strip() + f' @{tk}: {cnt}' for tk, c, t, cnt in s.history[-4:])
                shown = '' if n <= len(results) else f' Showing the first {len(results)}.'
                self.s_status.setText(f'{n} candidates.{shown} {steps}')
                self.s_table.setRowCount(len(results))
                for i, (addr, value) in enumerate(results):
                    self.s_table.setItem(i, 0, W.QTableWidgetItem(f'0x{addr:08X}'))
                    self.s_table.setItem(i, 1, W.QTableWidgetItem(M.format_value(value, s.type)))
            else:
                msg = out[1]
                if len(out) > 2 and out[2] in ('start', 'narrow'):
                    self.s_status.setText(msg)
                else:
                    self.where.setText(msg)
        finally:
            if queued is not None:
                self._submit(*queued)

    # -- watch list
    def _fill_table(self, values):
        self.table.setRowCount(len(self.watches))
        for i, w in enumerate(self.watches):
            val = values[i] if i < len(values) else None
            if val is None:
                text = ''
            elif val.error:
                text = val.error
            else:
                text = M.format_value(val.value, w.type, w.fmt)
            cells = (w.name, w.address_text, w.type, text, 'yes' if w.status == 'verified' else 'no')
            for c, t in enumerate(cells):
                item = W.QTableWidgetItem(t)
                if c == 0:
                    item.setToolTip('\n'.join(x for x in (w.note, w.source) if x))
                if c == 3 and val is not None and val.address is not None:
                    item.setToolTip(f'Address 0x{val.address:08X}')
                self.table.setItem(i, c, item)
        self.table.resizeColumnsToContents()

    def _selected(self):
        rows = {i.row() for i in self.table.selectedIndexes()}
        return min(rows) if rows else None

    def _selection_changed(self):
        self.refresh()

    def add_watch(self, watch=None):
        dlg = WatchDialog(self, watch if isinstance(watch, M.Watch) else None)
        if isinstance(watch, M.Watch):
            dlg.setWindowTitle('Add watch')
            dlg._old = None
        if dlg.exec() and dlg.result_watch is not None and len(self.watches) < M.MAX_WATCHES:
            self.watches.append(dlg.result_watch)
            self._changed()

    def edit_watch(self):
        i = self._selected()
        if i is None:
            return
        dlg = WatchDialog(self, self.watches[i])
        if dlg.exec() and dlg.result_watch is not None:
            self.watches[i] = dlg.result_watch
            self._changed()

    def remove_watch(self):
        i = self._selected()
        if i is not None:
            del self.watches[i]
            self._changed()

    def load_seed(self):
        names = {w.name for w in self.watches}
        self.watches.extend(w for w in M.KOTOR_K1_SEED if w.name not in names)
        self._changed()

    def import_watches(self):
        path, _ = W.QFileDialog.getOpenFileName(self, 'Import watches', '', 'Watch lists (*.toml)')
        if not path:
            return
        try:
            with open(path, encoding='utf-8') as f:
                loaded = M.load_watches(f.read())
        except (OSError, M.MemoryError_) as exc:
            self.where.setText(f'Could not import: {exc}')
            return
        self.watches = (self.watches + loaded)[:M.MAX_WATCHES]
        self._changed()

    def export_watches(self):
        path, _ = W.QFileDialog.getSaveFileName(self, 'Export watches', 'memory-watches.toml', 'Watch lists (*.toml)')
        if not path:
            return
        try:
            with open(path, 'w', encoding='utf-8') as f:
                f.write(M.dump_watches(self.watches))
        except OSError as exc:
            self.where.setText(f'Could not export: {exc}')

    def _changed(self):
        self._save()
        self._fill_table([])
        self.refresh()

    # -- search
    def start_search(self):
        path, tick = self.context()
        if not path or tick is None:
            self.s_status.setText('Open a recording first.')
            return
        self.s_status.setText(f'Reading frame {tick}…')
        self._submit('start', (path, int(tick), self.s_type.currentText(), self.s_aligned.isChecked(),
                               self.s_region.currentText()))

    def narrow_search(self):
        path, tick = self.context()
        if self.search is None:
            self.s_status.setText('Start a search first.')
            return
        if not path or tick is None:
            return
        cmp = self.s_cmp.currentText()
        value, text = None, ''
        if cmp in M.NEEDS_VALUE:
            try:
                text = self.s_value.text()
                value = M.parse_value(text, self.search.type)
            except M.MemoryError_ as exc:
                self.s_status.setText(str(exc))
                return
        self.s_status.setText(f'Narrowing at frame {tick}…')
        self._submit('narrow', (path, int(tick), self.search, cmp, value, text))

    def reset_search(self):
        self.search = None
        self.s_table.setRowCount(0)
        self.s_status.setText('Start a search at a frame, then move the playhead and narrow.')

    def watch_result(self):
        rows = sorted({i.row() for i in self.s_table.selectedIndexes()})
        if not rows or self.search is None:
            return
        for r in rows[:16]:
            addr = int(self.s_table.item(r, 0).text(), 16)
            self.watches.append(M.Watch(f'Found 0x{addr:08X}', addr, (), self.search.type, 'dec', 16,
                                        'unverified', 'RAM search', 'Heap addresses hold for this process run only.'))
        self._changed()
        self.tabs.setCurrentIndex(0)


def install_memory_panel(window):
    """Add More > Memory… and the Memory actions to a workspace window."""
    def context():
        snap = getattr(window, 'snapshot', None)
        if snap is None or not getattr(snap, 'take_path', None):
            return None, None
        try:
            tick = window.seek.value()
        except Exception:  # noqa: BLE001
            tick = snap.tick
        return snap.take_path, tick
    panel = MemoryPanel(window.root, context)
    window.seek.valueChanged.connect(panel.frame_changed)
    def show():
        panel.show(); panel.raise_(); panel.activateWindow()
    action = window.overlay.more_menu.addAction('Memory…')
    action.setToolTip('Watch and search game memory in the recorded frames')
    action.triggered.connect(show)
    def search_tab_then(fn):
        def run():
            show(); panel.tabs.setCurrentIndex(1); fn()
        return run
    from .actions import register_action
    for action_id, label, key, callback, description in (
            ('memory.show', 'Show memory watch and search', 'Ctrl+Alt+M', show,
             'Open the Memory panel. Values are read from the recorded frames.'),
            ('memory.add_watch', 'Add a memory watch', 'Ctrl+Alt+W', lambda: (show(), panel.add_watch()),
             'Add an address or pointer path to the watch list.'),
            ('memory.search_start', 'Start a memory search here', '', search_tab_then(panel.start_search),
             'Start a new memory search at the current frame.'),
            ('memory.search_narrow', 'Narrow the memory search here', 'Ctrl+Alt+N', search_tab_then(panel.narrow_search),
             'Apply the chosen comparison at the current frame.')):
        register_action(action_id, label, category='Memory', default=key, callback=callback,
                        description=description + ' Works while the Stasis window has focus.')
    return panel
