"""Input trainer panel ("combo trial") for the Stasis overlay.

Shows a trial as an ordered to-do list of keys, checks each step off as the
session commits matching input, and shows how early or late it landed. It
only reads committed per-frame receipts (the mechanic sequence evidence
channel). It never previews, injects or polls the game.

All logic lives in trainer.py; this module only presents it.
"""
from __future__ import annotations

import math
import os
from pathlib import Path
import re
import struct
import threading
import time
import wave

from PySide6 import QtCore as C, QtGui as G, QtWidgets as W

from kptas.score.keys import token_keys

from . import trainer as T


def _system_reduced_motion() -> bool:
    flag = os.environ.get('KPTAS_REDUCED_MOTION', '').lower()
    if flag in ('1', 'true', 'yes', 'reduce'):
        return True
    base = Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config')
    try:
        text = (base / 'kdeglobals').read_text(encoding='utf-8', errors='ignore')
    except OSError:
        return False
    match = re.search(r'^AnimationDurationFactor\s*=\s*([0-9.]+)', text, re.M)
    try:
        return match is not None and float(match.group(1)) == 0.0
    except ValueError:
        return False


def _color(settings, name, alpha=None):
    color = G.QColor(settings.colors.get(name, '#ffffff'))
    if alpha is not None:
        color.setAlphaF(max(0.0, min(1.0, alpha)))
    return color


def table_span(table, origin):
    """First and last frame of a timeline table selection, or None."""
    try:
        model = table.selectionModel()
        columns = {index.column() for index in model.selectedIndexes()} if model is not None else set()
        if not columns:
            current = table.currentIndex()
            columns = {current.column()} if current.isValid() else set()
        return (origin + min(columns), origin + max(columns)) if columns else None
    except (AttributeError, RuntimeError):
        return None


# --------------------------------------------------------------------------
# Small painted widgets

class KeyCaps(W.QWidget):
    """A chord drawn as key caps, plain text or compact symbols."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.inputs = []
        self.style_name = 'keycap'
        self.colors = None
        self.dim = False
        self.setSizePolicy(W.QSizePolicy.Policy.Preferred, W.QSizePolicy.Policy.Fixed)

    def set_chord(self, inputs, style, settings, *, dim=False):
        self.inputs = T.ordered_inputs(inputs)
        self.style_name, self.colors, self.dim = style, settings, dim
        self.setAccessibleName(T.chord_label(inputs))
        self.updateGeometry()
        self.update()

    def _parts(self):
        return [T.glyph(name, self.style_name) for name in self.inputs]

    def sizeHint(self):
        metrics = self.fontMetrics()
        parts = self._parts()
        if self.style_name == 'keycap':
            width = sum(metrics.horizontalAdvance(p) + 12 for p in parts) + max(0, len(parts) - 1) * 14
        else:
            width = metrics.horizontalAdvance(T.chord_label(self.inputs, self.style_name))
        return C.QSize(max(16, width), metrics.height() + 8)

    def paintEvent(self, event):
        if self.colors is None:
            return
        painter = G.QPainter(self)
        painter.setRenderHint(G.QPainter.RenderHint.Antialiasing)
        text_color = _color(self.colors, 'muted' if self.dim else 'text')
        metrics = self.fontMetrics()
        height = self.height()
        if self.style_name != 'keycap':
            painter.setPen(text_color)
            painter.drawText(self.rect(), int(C.Qt.AlignmentFlag.AlignVCenter | C.Qt.AlignmentFlag.AlignLeft),
                             T.chord_label(self.inputs, self.style_name))
            return
        x = 0.5
        for number, part in enumerate(self._parts()):
            if number:
                painter.setPen(_color(self.colors, 'muted'))
                painter.drawText(C.QRectF(x, 0, 14, height), int(C.Qt.AlignmentFlag.AlignCenter), '+')
                x += 14
            width = metrics.horizontalAdvance(part) + 12
            rect = C.QRectF(x, 2.5, width - 1, height - 5)
            painter.setPen(G.QPen(text_color, 1))
            painter.setBrush(_color(self.colors, 'window', 0.9))
            painter.drawRoundedRect(rect, 4, 4)
            painter.drawText(rect, int(C.Qt.AlignmentFlag.AlignCenter), part)
            x += width


class CheckMark(W.QWidget):
    """Check or cross that scales, slides and glows in."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(26, 22)
        self.kind = ''            # '', hit, perfect, miss, holding, current
        self.progress = 1.0
        self.colors = None
        self.animation = C.QVariantAnimation(self)
        self.animation.setStartValue(0.0)
        self.animation.setEndValue(1.0)
        self.animation.valueChanged.connect(self._progress)

    def _progress(self, value):
        self.progress = float(value)
        self.update()

    def show_kind(self, kind, settings, *, animate=False, duration=380):
        self.kind, self.colors = kind, settings
        self.animation.stop()
        if animate:
            self.animation.setDuration(duration)
            self.animation.setEasingCurve(C.QEasingCurve.Type.OutBack if kind != 'miss' else C.QEasingCurve.Type.OutCubic)
            self.progress = 0.0
            self.animation.start()
        else:
            self.progress = 1.0
        self.update()

    def paintEvent(self, event):
        if not self.kind or self.colors is None:
            return
        painter = G.QPainter(self)
        painter.setRenderHint(G.QPainter.RenderHint.Antialiasing)
        p = self.progress
        name = {'hit': 'hit', 'perfect': 'perfect', 'miss': 'miss', 'holding': 'current', 'current': 'current'}[self.kind]
        color = _color(self.colors, name)
        cx, cy = 13 + (1.0 - min(1.0, p)) * 12, 11
        if self.kind == 'current':
            painter.setPen(G.QPen(color, 2))
            painter.drawText(self.rect(), int(C.Qt.AlignmentFlag.AlignCenter), '›')
            return
        if self.kind == 'holding':
            painter.setPen(G.QPen(color, 2))
            painter.setBrush(C.Qt.BrushStyle.NoBrush)
            painter.drawEllipse(C.QPointF(cx, cy), 7, 7)
            return
        if p < 1.0:
            glow = _color(self.colors, name, 0.45 * (1.0 - min(1.0, p)))
            painter.setPen(C.Qt.PenStyle.NoPen)
            painter.setBrush(glow)
            painter.drawEllipse(C.QPointF(cx, cy), 7 + 7 * p, 7 + 7 * p)
        scale = max(0.0, p)
        painter.translate(cx, cy)
        painter.scale(scale, scale)
        painter.setPen(C.Qt.PenStyle.NoPen)
        painter.setBrush(color)
        painter.drawEllipse(C.QPointF(0, 0), 8, 8)
        pen = G.QPen(_color(self.colors, 'panel'), 2.2)
        pen.setCapStyle(C.Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        if self.kind == 'miss':
            painter.drawLine(C.QPointF(-3.5, -3.5), C.QPointF(3.5, 3.5))
            painter.drawLine(C.QPointF(-3.5, 3.5), C.QPointF(3.5, -3.5))
        else:
            path = G.QPainterPath(C.QPointF(-4, 0.5))
            path.lineTo(-1, 3.5)
            path.lineTo(4.5, -3)
            painter.drawPath(path)
        if self.kind == 'perfect':
            painter.setPen(G.QPen(color, 1.5))
            painter.setBrush(C.Qt.BrushStyle.NoBrush)
            painter.drawEllipse(C.QPointF(0, 0), 10.5, 10.5)


class WindowBar(W.QWidget):
    """Allowed window, target frame and where the input landed."""

    def __init__(self, parent=None, *, compact=False):
        super().__init__(parent)
        self.compact = compact
        self.setFixedHeight(10 if compact else 26)
        self.setMinimumWidth(60 if compact else 120)
        self.colors = None
        self.bounds = None        # (low, high, target or None)
        self.landed = None        # elapsed frames, or None
        self.kind = ''
        self.shown_landed = None
        self.animation = C.QVariantAnimation(self)
        self.animation.valueChanged.connect(self._slide)

    def _slide(self, value):
        self.shown_landed = float(value)
        self.update()

    def set_window(self, settings, bounds, landed=None, kind='', *, animate=False, duration=220):
        self.colors, self.bounds, self.landed, self.kind = settings, bounds, landed, kind
        self.animation.stop()
        if animate and landed is not None and bounds is not None:
            start = bounds[2] if bounds[2] is not None else bounds[0]
            self.animation.setStartValue(float(start))
            self.animation.setEndValue(float(landed))
            self.animation.setDuration(duration)
            self.animation.setEasingCurve(C.QEasingCurve.Type.OutCubic)
            self.shown_landed = float(start)
            self.animation.start()
        else:
            self.shown_landed = None if landed is None else float(landed)
        self.update()

    def paintEvent(self, event):
        if self.colors is None or self.bounds is None:
            return
        low, high, target = self.bounds
        if high is None:
            high = max(low + 10, (self.landed or 0) + 2)
        pad = max(2, (high - low + 1) // 2)
        first = min(low - pad, self.landed if self.landed is not None else low - pad)
        last = max(high + pad, self.landed if self.landed is not None else high + pad)
        first, last = max(0, first), last + 1
        span = max(1, last - first)
        painter = G.QPainter(self)
        painter.setRenderHint(G.QPainter.RenderHint.Antialiasing)
        width = self.width() - 2
        bar_top = 1 if self.compact else 4
        bar_height = self.height() - 2 if self.compact else 10

        def x_of(frame):
            return 1 + (frame - first) / span * width

        painter.setPen(C.Qt.PenStyle.NoPen)
        painter.setBrush(_color(self.colors, 'window', 0.45))
        painter.drawRoundedRect(C.QRectF(1, bar_top, width, bar_height), 3, 3)
        painter.setBrush(_color(self.colors, 'hit', 0.55))
        painter.drawRoundedRect(C.QRectF(x_of(low), bar_top, max(2.0, x_of(high + 1) - x_of(low)), bar_height), 3, 3)
        if target is not None:
            painter.setBrush(_color(self.colors, 'target'))
            painter.drawRect(C.QRectF(x_of(target), bar_top - 1, max(2.0, x_of(target + 1) - x_of(target)), bar_height + 2))
        if self.shown_landed is not None:
            name = 'miss' if self.kind == 'miss' else 'perfect' if self.kind == 'perfect' else 'marker'
            x = x_of(self.shown_landed) + max(1.0, (x_of(1) - x_of(0)) / 2)
            painter.setBrush(_color(self.colors, name))
            painter.drawPolygon(G.QPolygonF([C.QPointF(x - 4, bar_top + bar_height + 6), C.QPointF(x + 4, bar_top + bar_height + 6),
                                             C.QPointF(x, bar_top + bar_height)]) if not self.compact else
                                G.QPolygonF([C.QPointF(x - 2, bar_top), C.QPointF(x + 2, bar_top),
                                             C.QPointF(x + 2, bar_top + bar_height), C.QPointF(x - 2, bar_top + bar_height)]))
        if not self.compact:
            painter.setPen(_color(self.colors, 'muted'))
            font = painter.font()
            font.setPixelSize(9)
            painter.setFont(font)
            painter.drawText(C.QRectF(0, bar_top + bar_height + 1, 40, 12), int(C.Qt.AlignmentFlag.AlignLeft), f'+{first}')
            painter.drawText(C.QRectF(width - 40, bar_top + bar_height + 1, 40, 12), int(C.Qt.AlignmentFlag.AlignRight), f'+{last - 1}')


class HistoryStrip(W.QWidget):
    """Fighting-game input display: held keys as bars over recent frames."""

    def __init__(self, history, parent=None):
        super().__init__(parent)
        self.history = history
        self.colors = None
        self.style_name = 'keycap'
        self.setMinimumHeight(40)

    def configure(self, settings):
        self.colors = settings
        self.style_name = settings.glyph_style
        self.update_height()

    def update_height(self):
        lanes = max(1, min(8, len(self.history.lanes())))
        self.setFixedHeight(lanes * 12 + 4)
        self.update()

    def paintEvent(self, event):
        if self.colors is None:
            return
        painter = G.QPainter(self)
        painter.setRenderHint(G.QPainter.RenderHint.Antialiasing)
        lanes = self.history.lanes()[:8]
        recent = self.history.recent
        if not recent:
            painter.setPen(_color(self.colors, 'muted'))
            painter.drawText(self.rect(), int(C.Qt.AlignmentFlag.AlignCenter), 'No committed input yet')
            return
        label_width = 54
        span = max(1, recent.maxlen or len(recent))
        last = recent[-1][0]
        first = last - span + 1
        width = max(1, self.width() - label_width - 2)
        font = painter.font()
        font.setPixelSize(10)
        painter.setFont(font)
        for row, (name, runs) in enumerate(lanes):
            y = 2 + row * 12
            painter.setPen(_color(self.colors, 'muted'))
            painter.drawText(C.QRectF(0, y - 1, label_width - 4, 12),
                             int(C.Qt.AlignmentFlag.AlignRight | C.Qt.AlignmentFlag.AlignVCenter),
                             T.glyph(name, 'compact' if self.style_name == 'compact' else 'keycap'))
            painter.setPen(C.Qt.PenStyle.NoPen)
            painter.setBrush(_color(self.colors, 'window', 0.35))
            painter.drawRect(C.QRectF(label_width, y + 1, width, 8))
            painter.setBrush(_color(self.colors, 'current'))
            for start, end in runs:
                x0 = label_width + max(0, start - first) / span * width
                x1 = label_width + (end - first + 1) / span * width
                painter.drawRoundedRect(C.QRectF(x0, y + 1, max(2.0, x1 - x0), 8), 2, 2)
                if end - start + 1 >= 4 and x1 - x0 > 18:
                    painter.setPen(_color(self.colors, 'panel'))
                    painter.drawText(C.QRectF(x0, y - 1, x1 - x0, 12), int(C.Qt.AlignmentFlag.AlignCenter), str(end - start + 1))
                    painter.setPen(C.Qt.PenStyle.NoPen)


# --------------------------------------------------------------------------
# Step row

class StepRow(W.QFrame):
    def __init__(self, number, step, panel):
        super().__init__(panel.steps_host)
        self.panel = panel
        self.step = step
        self.number = number
        self.state = ''
        self.flash = 0.0
        self.flash_name = 'hit'
        self._margin = 6
        self.setObjectName('trainerStep')
        self.setAttribute(C.Qt.WidgetAttribute.WA_StyledBackground, False)
        outer = W.QVBoxLayout(self)
        outer.setContentsMargins(self._margin, 3, self._margin, 3)
        outer.setSpacing(1)
        top = W.QHBoxLayout()
        top.setSpacing(6)
        self.index_label = W.QLabel(str(number))
        self.index_label.setFixedWidth(22)
        self.index_label.setAlignment(C.Qt.AlignmentFlag.AlignRight | C.Qt.AlignmentFlag.AlignVCenter)
        self.caps = KeyCaps(self)
        self.timing = W.QLabel()
        self.timing.setAlignment(C.Qt.AlignmentFlag.AlignRight | C.Qt.AlignmentFlag.AlignVCenter)
        self.check = CheckMark(self)
        top.addWidget(self.index_label)
        top.addWidget(self.caps)
        top.addStretch(1)
        top.addWidget(self.timing)
        top.addWidget(self.check)
        outer.addLayout(top)
        self.bar = WindowBar(self, compact=True)
        self.detail = W.QLabel()
        self.detail.setWordWrap(True)
        outer.addWidget(self.bar)
        outer.addWidget(self.detail)
        self.shake = C.QVariantAnimation(self)
        self.shake.setStartValue(0.0)
        self.shake.setEndValue(1.0)
        self.shake.valueChanged.connect(self._shake)
        self.shake.finished.connect(lambda: self._shake(1.0))
        self.flasher = C.QVariantAnimation(self)
        self.flasher.setStartValue(1.0)
        self.flasher.setEndValue(0.0)
        self.flasher.valueChanged.connect(self._flash)

    def _shake(self, value):
        value = float(value)
        dx = 0 if value >= 1.0 else int(round(6 * math.sin(value * math.pi * 6) * (1.0 - value)))
        self.layout().setContentsMargins(self._margin + dx, 3, self._margin - dx, 3)

    def _flash(self, value):
        self.flash = float(value)
        self.update()

    def paintEvent(self, event):
        settings = self.panel.settings
        painter = G.QPainter(self)
        painter.setRenderHint(G.QPainter.RenderHint.Antialiasing)
        painter.setPen(C.Qt.PenStyle.NoPen)
        if self.state == 'current':
            painter.setBrush(_color(settings, 'current', 0.18))
            painter.drawRoundedRect(C.QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 5, 5)
            painter.setBrush(_color(settings, 'current'))
            painter.drawRoundedRect(C.QRectF(0, 3, 3, self.height() - 6), 1.5, 1.5)
        if self.flash > 0:
            painter.setBrush(_color(settings, self.flash_name, 0.45 * self.flash))
            painter.drawRoundedRect(C.QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 5, 5)

    def window_tuple(self):
        low, high = self.step.window(self.panel.defaults())
        return low, high, self.step.target

    def describe_window(self):
        step = self.step
        rate = self.panel.settings.frame_rate
        low, high = step.window(self.panel.defaults())
        parts = []
        if step.timed:
            width = step.window_frames(self.panel.defaults())
            parts.append(f'+{step.target} f · {width} f ({width * 1000 / rate:.0f} ms)')
        elif high is None:
            parts.append('any time' if low == 0 else f'after +{low} f')
        else:
            width = high - low + 1
            parts.append(f'+{low}–{high} f ({width * 1000 / rate:.0f} ms)')
        if step.match == 'hold':
            parts.append(f'hold {step.duration} f')
        elif step.match == 'release':
            parts.append('release')
        if step.hold_min is not None or step.hold_max is not None:
            if step.hold_max is None:
                parts.append(f'hold ≥{step.hold_min} f')
            elif step.hold_min is None or step.hold_min == step.hold_max:
                parts.append(f'hold ≤{step.hold_max} f' if step.hold_min is None else f'hold {step.hold_max} f')
            else:
                parts.append(f'hold {step.hold_min}–{step.hold_max} f')
        return ' · '.join(parts)

    def apply(self, result, *, animate=False, reduced=False):
        settings = self.panel.settings
        state = result.state
        if state == 'hit' and result.verdict == 'perfect':
            state = 'perfect'
        changed = state != self.state
        self.state = state
        self.caps.set_chord(self.step.inputs, settings.glyph_style, settings, dim=state in ('pending',))
        self.index_label.setStyleSheet(f'color:{settings.colors["muted"]};')
        tip = f'{self.step.label}. Frames after the previous step: {self.describe_window()}.'
        self.setToolTip(tip)
        self.setAccessibleName(f'Step {self.number}: {self.step.label}, {state}')
        speed = settings.animation_speed
        timing_color = settings.colors['muted']
        detail, detail_color = '', settings.colors['muted']
        if state in ('hit', 'perfect'):
            if result.verdict == 'perfect':
                text, timing_color = 'Perfect', settings.colors['perfect']
            elif result.verdict in ('early', 'late'):
                text, timing_color = f'{result.verdict.title()} {abs(result.offset)} f', settings.colors['hit']
            else:
                text, timing_color = f'+{result.elapsed} f', settings.colors['hit']
            if result.hold is not None:
                text += f' · held {result.hold} f'
        elif state == 'miss':
            text, timing_color = 'Missed', settings.colors['miss']
            detail, detail_color = result.reason, settings.colors['miss']
        elif state == 'holding':
            text, timing_color = 'Holding…', settings.colors['current']
        else:
            text = self.describe_window()
            if state == 'current':
                timing_color = settings.colors['text']
        self.timing.setText(text)
        self.timing.setStyleSheet(f'color:{timing_color};')
        self.detail.setText(detail)
        self.detail.setStyleSheet(f'color:{detail_color};')
        self.detail.setVisible(bool(detail))
        show_bar = self.step.window(self.panel.defaults())[1] is not None and state in ('current', 'hit', 'perfect', 'miss', 'holding')
        self.bar.setVisible(show_bar)
        if show_bar:
            self.bar.set_window(settings, self.window_tuple(), result.elapsed,
                                state if state != 'holding' else '', animate=animate and not reduced,
                                duration=int(220 / speed))
        kind = {'hit': 'hit', 'perfect': 'perfect', 'miss': 'miss', 'holding': 'holding', 'current': 'current'}.get(state, '')
        self.check.show_kind(kind, settings, animate=animate and changed and not reduced and state in ('hit', 'perfect', 'miss'),
                             duration=int(380 / speed))
        if animate and changed and not reduced and state in ('hit', 'perfect', 'miss'):
            self.flash_name = 'miss' if state == 'miss' else 'perfect' if state == 'perfect' else 'hit'
            self.flasher.stop()
            self.flasher.setDuration(int(500 / speed))
            self.flasher.start()
            if state == 'miss':
                self.shake.stop()
                self.shake.setDuration(int(360 / speed))
                self.shake.start()
        elif not animate:
            self.flash = 0.0
            self._shake(1.0)
        self.update()


# --------------------------------------------------------------------------
# Sounds (optional, off by default)

class Sounds:
    def __init__(self):
        self.effects = {}
        self.available = None

    def _tone_file(self, name, frequencies, duration):
        base = Path(os.environ.get('XDG_CACHE_HOME') or Path.home() / '.cache') / 'kptas' / 'trainer'
        path = base / f'{name}.wav'
        if path.exists():
            return path
        base.mkdir(parents=True, exist_ok=True)
        rate = 22050
        count = int(rate * duration)
        frames = bytearray()
        for i in range(count):
            t = i / rate
            freq = frequencies[min(len(frequencies) - 1, int(t / duration * len(frequencies)))]
            envelope = min(1.0, i / 200) * max(0.0, 1.0 - t / duration)
            frames += struct.pack('<h', int(9000 * envelope * math.sin(2 * math.pi * freq * t)))
        temporary = path.with_suffix('.tmp')
        with wave.open(str(temporary), 'wb') as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(rate)
            out.writeframes(bytes(frames))
        os.replace(temporary, path)
        return path

    def play(self, name, settings, parent):
        if not settings.sound:
            return
        if self.available is False:
            return
        try:
            from PySide6.QtMultimedia import QSoundEffect
        except ImportError:
            self.available = False
            return
        self.available = True
        if name not in self.effects:
            tones = {'hit': ((880,), .07), 'perfect': ((880, 1320), .12), 'miss': ((220, 180), .16),
                     'complete': ((660, 880, 1320), .30)}[name]
            try:
                path = self._tone_file(name, *tones)
            except OSError:
                return
            effect = QSoundEffect(parent)
            effect.setSource(C.QUrl.fromLocalFile(str(path)))
            self.effects[name] = effect
        effect = self.effects[name]
        effect.setVolume(settings.sound_volume)
        effect.play()


class LiveInputReader(C.QObject):
    """Polls the owner's committed input log on a worker thread.

    The log lives on the preview port ({"inputs_since": seq}). It holds every
    committed frame: Step, Apply, Play batches and physical play.
    """
    frames = C.Signal(object)        # list[T.LiveFrame], oldest first
    state = C.Signal(str)            # live | unsupported | offline

    def __init__(self, address, parent=None):
        super().__init__(parent)
        self.address = address       # callable -> (host, command port) or None
        self.wanted = threading.Event()
        self.stopping = threading.Event()
        self.status = ''
        self.thread = threading.Thread(target=self._run, daemon=True, name='telos-trainer-inputs')
        self.thread.start()

    def set_active(self, active):
        if active:
            self.wanted.set()
        else:
            self.wanted.clear()

    def close(self):
        self.stopping.set()
        self.wanted.set()

    def _report(self, status):
        if status != self.status:
            self.status = status
            self.state.emit(status)

    def _run(self):
        from .preview_channel import read_inputs
        stream, since, nones = None, 0, 0
        while not self.stopping.is_set():
            if not self.wanted.wait(.25) or self.stopping.is_set():
                continue
            delay = .016
            try:
                address = self.address()
                if not address or None in address:
                    raise OSError('no owner address')
                host, port = address
                packet = read_inputs(host, port + 1, since)
                if packet is None:
                    # A miss can be a race with a preview read; call it unsupported after 3.
                    nones += 1
                    if nones >= 3:
                        self._report('unsupported')
                        delay = 1.0
                    else:
                        delay = .02
                else:
                    nones = 0
                    self._report('live')
                    entries = packet['entries']
                    if packet['stream'] != stream:
                        # A new owner log: begin at its newest frame, not its backlog.
                        stream, since = packet['stream'], int(packet['seq'])
                        entries = entries[-1:]
                    frames = []
                    for seq, session, take, _epoch, tick, held, buttons in entries:
                        frames.append(T.frame_from_tokens(session, take, tick, held, buttons, identity=(stream, seq)))
                        since = max(since, int(seq))
                    if frames:
                        self.frames.emit(frames)
            except RuntimeError:
                return            # The panel is gone.
            except (OSError, ValueError, TypeError, KeyError):
                try:
                    self._report('offline')
                except RuntimeError:
                    return
                delay = .5
            if self.stopping.wait(delay):
                return


# --------------------------------------------------------------------------
# Dialogs

class SpanDialog(W.QDialog):
    def __init__(self, parent, span, *, has_timeline, has_score):
        super().__init__(parent)
        self.setWindowTitle('Make a trial from frames')
        form = W.QFormLayout(self)
        self.first = W.QSpinBox()
        self.last = W.QSpinBox()
        for box in (self.first, self.last):
            box.setRange(1, 100_000_000)
        first, last = span or (1, 60)
        self.first.setValue(first)
        self.last.setValue(last)
        self.title = W.QLineEdit(f'Frames {first}–{last}')
        self.source = W.QComboBox()
        if has_timeline:
            self.source.addItem('Timeline inputs', 'timeline')
        if has_score:
            self.source.addItem('Score text', 'score')
        form.addRow('First frame', self.first)
        form.addRow('Last frame', self.last)
        form.addRow('Name', self.title)
        form.addRow('Read inputs from', self.source)
        note = W.QLabel('Each frame with a new key press becomes one step. Keys start released.')
        note.setWordWrap(True)
        form.addRow(note)
        buttons = W.QDialogButtonBox(W.QDialogButtonBox.StandardButton.Ok | W.QDialogButtonBox.StandardButton.Cancel)
        buttons.button(W.QDialogButtonBox.StandardButton.Ok).setText('Make trial')
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)


class SettingsDialog(W.QDialog):
    def __init__(self, parent, settings, conflicts):
        super().__init__(parent)
        self.setWindowTitle('Trainer settings')
        self.settings = settings
        self.colors = dict(settings.colors)
        tabs = W.QTabWidget()
        layout = W.QVBoxLayout(self)
        layout.addWidget(tabs)
        self.widgets = {}

        def page():
            widget = W.QWidget()
            form = W.QFormLayout(widget)
            tabs.addTab(widget, '')
            return widget, form

        def choice(form, name, label, items):
            box = W.QComboBox()
            for text, value in items:
                box.addItem(text, value)
            box.setCurrentIndex(max(0, box.findData(getattr(settings, name))))
            form.addRow(label, box)
            self.widgets[name] = box

        def number(form, name, label, low, high, suffix=''):
            box = W.QSpinBox()
            box.setRange(low, high)
            box.setValue(getattr(settings, name))
            if suffix:
                box.setSuffix(suffix)
            form.addRow(label, box)
            self.widgets[name] = box

        def decimal(form, name, label, low, high, step):
            box = W.QDoubleSpinBox()
            box.setRange(low, high)
            box.setSingleStep(step)
            box.setValue(getattr(settings, name))
            form.addRow(label, box)
            self.widgets[name] = box

        def toggle(form, name, label):
            box = W.QCheckBox(label)
            box.setChecked(getattr(settings, name))
            form.addRow(box)
            self.widgets[name] = box

        widget, form = page()
        tabs.setTabText(0, 'Trial')
        choice(form, 'mode', 'Mode', (('Practice (loops)', 'practice'), ('Strict (one try)', 'strict')))
        toggle(form, 'auto_reset', 'Start over after a miss (practice)')
        toggle(form, 'loop', 'Start again after finishing (practice)')
        number(form, 'show_next', 'Upcoming steps shown (0 = all)', 0, 64)
        number(form, 'show_done', 'Finished steps kept in view', 0, 64)
        number(form, 'window_early', 'Default early window', 0, 600, ' frames')
        number(form, 'window_late', 'Default late window', 0, 600, ' frames')
        number(form, 'perfect_window', 'Perfect within', 0, 30, ' frames')
        number(form, 'late_grace', 'Wait after a window closes', 0, 600, ' frames')
        toggle(form, 'check_holds', 'Check hold lengths in trials made from frames')
        number(form, 'hold_window', 'Hold length tolerance', 0, 600, ' frames')
        number(form, 'frame_rate', 'Frame rate', 1, 1000, ' Hz')

        widget, form = page()
        tabs.setTabText(1, 'Look')
        choice(form, 'glyph_style', 'Key style', (('Key caps', 'keycap'), ('Words', 'text'), ('Symbols', 'compact')))
        choice(form, 'reduced_motion', 'Motion', (('Follow Settings', 'system'), ('Reduce motion', 'reduce'), ('Full motion', 'full')))
        decimal(form, 'animation_speed', 'Animation speed', 0.25, 4.0, 0.25)
        number(form, 'reset_delay_ms', 'Show a miss for', 0, 5000, ' ms')
        choice(form, 'panel_corner', 'Panel position', (('Top right', 'top-right'), ('Top left', 'top-left'),
               ('Bottom right', 'bottom-right'), ('Bottom left', 'bottom-left'), ('Where I drag it', 'custom')))
        number(form, 'panel_width', 'Panel width', 240, 1200, ' px')
        decimal(form, 'panel_opacity', 'Background opacity', 0.2, 1.0, 0.05)
        number(form, 'history_entries', 'Input history entries', 1, 64)
        number(form, 'history_frames', 'Input history length', 10, 1200, ' frames')
        toggle(form, 'sound', 'Play sounds')
        decimal(form, 'sound_volume', 'Volume', 0.0, 1.0, 0.1)
        grid = W.QGridLayout()
        self.color_buttons = {}
        for number_, (name, _) in enumerate(T.DEFAULT_COLORS):
            button = W.QPushButton(name.title())
            button.clicked.connect(lambda _=False, name=name: self.pick_color(name))
            self.color_buttons[name] = button
            grid.addWidget(button, number_ // 4, number_ % 4)
        form.addRow('Colors', grid)
        self.paint_color_buttons()

        widget, form = page()
        tabs.setTabText(2, 'Hotkeys')
        hint = W.QLabel('These work while the Stasis window has focus. Clear a field to turn a hotkey off. '
                        'If Settings › Shortcuts changes one of these keys, that change wins.')
        hint.setWordWrap(True)
        form.addRow(hint)
        self.hotkeys = {}
        for action, _ in T.DEFAULT_HOTKEYS:
            edit = W.QKeySequenceEdit(G.QKeySequence.fromString(settings.hotkeys.get(action, ''),
                                                                 G.QKeySequence.SequenceFormat.PortableText))
            label = T.HOTKEY_LABELS[action]
            if action in conflicts:
                label += ' (in use elsewhere)'
            form.addRow(label, edit)
            self.hotkeys[action] = edit

        self.error = W.QLabel()
        self.error.setStyleSheet('color:#ef5b5b;')
        self.error.setWordWrap(True)
        layout.addWidget(self.error)
        buttons = W.QDialogButtonBox(W.QDialogButtonBox.StandardButton.Save | W.QDialogButtonBox.StandardButton.Cancel
                                     | W.QDialogButtonBox.StandardButton.RestoreDefaults)
        buttons.accepted.connect(self.try_accept)
        buttons.rejected.connect(self.reject)
        buttons.button(W.QDialogButtonBox.StandardButton.RestoreDefaults).clicked.connect(self.restore_defaults)
        layout.addWidget(buttons)
        self.result_settings = None

    def paint_color_buttons(self):
        for name, button in self.color_buttons.items():
            color = G.QColor(self.colors[name])
            text = '#000000' if color.lightnessF() > 0.55 else '#ffffff'
            button.setStyleSheet(f'background:{self.colors[name]};color:{text};')

    def pick_color(self, name):
        color = W.QColorDialog.getColor(G.QColor(self.colors[name]), self, f'{name.title()} color')
        if color.isValid():
            self.colors[name] = color.name()
            self.paint_color_buttons()

    def restore_defaults(self):
        self.result_settings = T.TrainerSettings()
        self.accept()

    def values(self):
        data = {}
        for name, widget in self.widgets.items():
            if isinstance(widget, W.QComboBox):
                data[name] = widget.currentData()
            elif isinstance(widget, W.QCheckBox):
                data[name] = widget.isChecked()
            elif isinstance(widget, W.QDoubleSpinBox):
                data[name] = round(widget.value(), 2)
            else:
                data[name] = widget.value()
        data['colors'] = dict(self.colors)
        data['hotkeys'] = {action: edit.keySequence().toString(G.QKeySequence.SequenceFormat.PortableText)
                           for action, edit in self.hotkeys.items()}
        return data

    def try_accept(self):
        try:
            current = self.settings.to_dict()
            current.update(self.values())
            self.result_settings = T.TrainerSettings.from_dict(current)
        except T.TrainerError as exc:
            self.error.setText(str(exc))
            return
        self.accept()


# --------------------------------------------------------------------------
# Panel

class TrainerPanel(W.QFrame):
    """Floating trial panel; a direct child of the overlay root."""

    def __init__(self, overlay):
        super().__init__(overlay.root)
        self.overlay = overlay
        self.setObjectName('inputTrainer')
        self.setAccessibleName('Input trainer')
        self.settings_error = ''
        try:
            self.settings = T.load_settings()
        except (T.TrainerError, OSError) as exc:
            self.settings = T.TrainerSettings()
            self.settings_error = f'Trainer settings were not loaded: {exc}'
        self.trials: list[T.Trial] = []
        self.position = -1
        self.run: T.TrialRun | None = None
        self.rows: list[StepRow] = []
        self.history = T.InputHistory(self.settings.history_entries, self.settings.history_frames)
        self.sounds = Sounds()
        self.latest = None
        self._armed = False
        self._live_enable = None
        self._note_at = None
        self._selection = None
        self._hold_rows_until = None
        self._shortcuts = []
        self.hotkey_conflicts = set()
        self.uses_keymap = False
        self._pending_restore = None
        self._system_motion = False
        self._drag = None
        self._drag_moved = False
        self.reader = None
        self.live_state = ''
        self._demand = None
        self._live_seen = 0.0
        self._recorded_seen = 0.0
        self._last_fed = None
        self._stats_by_trial = {}
        self._build()
        self.row_timer = C.QTimer(self)
        self.row_timer.setSingleShot(True)
        self.row_timer.timeout.connect(self._release_rows)
        overlay.root.installEventFilter(self)
        self.menu_action = overlay.more_menu.addAction('Input trainer')
        self.menu_action.setCheckable(True)
        self.menu_action.toggled.connect(self.set_panel_visible)
        self.apply_settings(self.settings, save=False)
        self.setVisible(self.settings.panel_visible)
        self.menu_action.setChecked(self.settings.panel_visible)
        if self.settings_error:
            self.status.setText(self.settings_error)
        self._update_demand()

    # -- layout ---------------------------------------------------------
    def _build(self):
        layout = W.QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 8)
        layout.setSpacing(5)
        header = W.QHBoxLayout()
        self.header = header
        self.title = W.QLabel('Input trainer')
        self.title.setStyleSheet('font-weight:600;')
        self.title.setCursor(C.Qt.CursorShape.SizeAllCursor)
        self.title.setToolTip('Drag to move')
        self.counter = W.QLabel()
        self.prev_button = self._tool('‹', 'Previous trial', lambda: self.select_trial(self.position - 1))
        self.next_button = self._tool('›', 'Next trial', lambda: self.select_trial(self.position + 1))
        self.close_button = self._tool('×', 'Hide the trainer', lambda: self.set_panel_visible(False))
        header.addWidget(self.title, 1)
        header.addWidget(self.counter)
        header.addWidget(self.prev_button)
        header.addWidget(self.next_button)
        header.addWidget(self.close_button)
        layout.addLayout(header)

        files = W.QHBoxLayout()
        self.trial_box = W.QComboBox()
        self.trial_box.setAccessibleName('Loaded trials')
        self.trial_box.setSizeAdjustPolicy(W.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.trial_box.setMinimumContentsLength(10)
        self.trial_box.activated.connect(self.select_trial)
        self.more = W.QToolButton()
        self.more.setText('Trials ▾')
        self.more.setPopupMode(W.QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = W.QMenu(self.more)
        menu.addAction('Open trial files…', self.open_trials)
        menu.addAction('Make a trial from frames…', self.make_from_frames)
        self.save_action = menu.addAction('Save this trial as a file…', self.save_trial)
        self.remove_action = menu.addAction('Remove this trial from the list', self.remove_trial)
        self.clear_stats_action = menu.addAction('Clear stats for this trial', self.clear_stats)
        menu.addSeparator()
        menu.addAction('Settings…', self.open_settings)
        self.more.setMenu(menu)
        files.addWidget(self.trial_box, 1)
        files.addWidget(self.more)
        layout.addLayout(files)

        controls = W.QHBoxLayout()
        self.start_button = W.QPushButton('Start')
        self.start_button.setToolTip('Start from the last committed frame. This does not change the game.')
        self.start_button.clicked.connect(self.start_stop)
        self.reset_button = W.QPushButton('Reset')
        self.reset_button.clicked.connect(self.reset_trial)
        self.mode_box = W.QComboBox()
        self.mode_box.addItem('Practice', 'practice')
        self.mode_box.addItem('Strict', 'strict')
        self.mode_box.setToolTip('Practice loops and starts over after a miss. Strict gives one try.')
        self.mode_box.activated.connect(lambda _: self._quick_setting(mode=self.mode_box.currentData()))
        self.show_box = W.QComboBox()
        for text, value in (('Next 1', 1), ('Next 3', 3), ('Next 5', 5), ('All steps', 0)):
            self.show_box.addItem(text, value)
        self.show_box.setToolTip('How many upcoming steps to list')
        self.show_box.activated.connect(lambda _: self._quick_setting(show_next=self.show_box.currentData()))
        for widget in (self.start_button, self.reset_button, self.mode_box, self.show_box):
            controls.addWidget(widget)
        layout.addLayout(controls)

        self.status = W.QLabel('Open a trial or make one from frames')
        self.status.setWordWrap(True)
        self.status.setAccessibleName('Trainer status')
        layout.addWidget(self.status)
        self.source_label = W.QLabel()
        self.source_label.setWordWrap(True)
        self.source_label.setAccessibleName('Where the trainer reads input from')
        layout.addWidget(self.source_label)

        self.steps_host = W.QWidget()
        self.steps_layout = W.QVBoxLayout(self.steps_host)
        self.steps_layout.setContentsMargins(0, 0, 0, 0)
        self.steps_layout.setSpacing(2)
        self.steps_layout.addStretch(1)
        self.scroll = W.QScrollArea()
        self.scroll.setWidget(self.steps_host)
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(W.QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(C.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setStyleSheet('QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; }')
        layout.addWidget(self.scroll)

        timing_title = W.QLabel('Last input')
        timing_title.setStyleSheet('font-weight:600;')
        self.timing_text = W.QLabel('—')
        self.timing_text.setWordWrap(True)
        self.timing_bar = WindowBar(self)
        layout.addWidget(timing_title)
        layout.addWidget(self.timing_bar)
        layout.addWidget(self.timing_text)

        self.stats_label = W.QLabel()
        self.stats_label.setWordWrap(True)
        self.stats_label.setAccessibleName('Trainer statistics')
        layout.addWidget(self.stats_label)

        history_title = W.QLabel('Input history')
        history_title.setStyleSheet('font-weight:600;')
        self.history_strip = HistoryStrip(self.history, self)
        self.history_text = W.QLabel()
        self.history_text.setWordWrap(True)
        layout.addWidget(history_title)
        layout.addWidget(self.history_strip)
        layout.addWidget(self.history_text)

    def _tool(self, text, tip, slot):
        button = W.QToolButton()
        button.setText(text)
        button.setToolTip(tip)
        button.setAccessibleName(tip)
        button.setAutoRaise(True)
        button.clicked.connect(slot)
        return button

    # -- settings -------------------------------------------------------
    def defaults(self):
        return self.settings.window_early, self.settings.window_late

    def reduced_motion(self):
        if self.run is not None and self.run.trial.motion == 'none':
            return True
        if self.settings.reduced_motion == 'reduce':
            return True
        if self.settings.reduced_motion == 'full':
            return False
        try:  # "system": follow the global Settings > Reduce motion switch first.
            from . import ui_settings
            if ui_settings.reduced_motion():
                return True
        except Exception:
            pass
        return self._system_motion

    def apply_settings(self, settings, *, save=True):
        self.settings = settings
        self._system_motion = _system_reduced_motion()
        colors = settings.colors
        background = G.QColor(colors['panel'])
        background.setAlphaF(settings.panel_opacity)
        rgba = f'rgba({background.red()},{background.green()},{background.blue()},{background.alpha()})'
        self.setStyleSheet(
            f'QFrame#inputTrainer {{ background:{rgba}; border:1px solid {colors["window"]}; border-radius:8px; }}'
            f'QFrame#inputTrainer QLabel {{ color:{colors["text"]}; background:transparent; }}')
        self.setFixedWidth(settings.panel_width)
        self.history.resize(settings.history_entries, settings.history_frames)
        self.history_strip.configure(settings)
        self.timing_bar.colors = settings
        self.mode_box.setCurrentIndex(max(0, self.mode_box.findData(settings.mode)))
        index = self.show_box.findData(settings.show_next)
        if index < 0:
            self.show_box.addItem(f'Next {settings.show_next}', settings.show_next)
            index = self.show_box.count() - 1
        self.show_box.setCurrentIndex(index)
        if self.run is not None:
            self.run.settings = settings
        self._install_hotkeys()
        self._sync_rows()
        self._refresh_stats()
        self._refresh_history()
        self._place()
        if save:
            self._save()

    def _quick_setting(self, **changes):
        try:
            self.apply_settings(T.replace_settings(self.settings, **changes))
        except T.TrainerError as exc:
            self.status.setText(str(exc))

    def _save(self):
        try:
            T.save_settings(self.settings)
        except OSError as exc:
            self.status.setText(f'Settings were not saved: {exc}')

    def open_settings(self):
        dialog = SettingsDialog(self.overlay.root, self.settings, self.hotkey_conflicts)
        if dialog.exec() == W.QDialog.DialogCode.Accepted and dialog.result_settings is not None:
            settings = dialog.result_settings
            if settings.panel_corner == 'custom' and self.settings.panel_corner != 'custom':
                settings = T.replace_settings(settings, panel_x=self.x(), panel_y=self.y())
            self.apply_settings(T.replace_settings(settings, panel_visible=self.isVisible()))

    # -- hotkeys --------------------------------------------------------
    def hotkey_callbacks(self):
        return {'trainer.start_stop': self.start_stop, 'trainer.reset': self.reset_trial,
                'trainer.previous': lambda: self.select_trial(self.position - 1),
                'trainer.next': lambda: self.select_trial(self.position + 1),
                'trainer.toggle_panel': lambda: self.set_panel_visible(not self.isVisible())}

    def _install_hotkeys(self):
        for shortcut in self._shortcuts:
            shortcut.setEnabled(False)
            shortcut.deleteLater()
        self._shortcuts = []
        self.hotkey_conflicts = set()
        callbacks = self.hotkey_callbacks()
        binding = self.settings.hotkeys.get('trainer.toggle_panel', '')
        self.menu_action.setToolTip('Show or hide the input trainer' + (f' · {binding}' if binding else ''))
        if getattr(self.overlay, '_shortcut_binder', None) is not None:
            # Central keymap: it binds the keys, applies keymap.toml overrides
            # and resolves conflicts. Trainer settings supply the defaults.
            try:
                from .actions import register_action
                for action, key in self.settings.hotkeys.items():
                    register_action(action, T.HOTKEY_LABELS[action], category='Input trainer', default=key,
                                    callback=callbacks[action], description='Works while the Stasis window has focus.')
                self.uses_keymap = True
                try:  # Show the resolved key next to the More menu item.
                    self.overlay._shortcut_binder.show_in_menu('trainer.toggle_panel', self.menu_action, 'Input trainer')
                except Exception:
                    pass
                return
            except Exception as exc:  # Fall back to local shortcuts below.
                self.uses_keymap = False
                try:
                    self.overlay.console.appendPlainText(f'Input trainer: central shortcuts unavailable. {exc}')
                except (AttributeError, RuntimeError):
                    pass
        self.uses_keymap = False
        portable = G.QKeySequence.SequenceFormat.PortableText
        taken = {}
        for action, text in getattr(self.overlay.ui_preferences, 'shortcuts', ()):
            if text:
                taken[G.QKeySequence.fromString(text, portable).toString(portable)] = action
        skipped = []
        for action, text in self.settings.hotkeys.items():
            if not text:
                continue
            sequence = G.QKeySequence.fromString(text, portable)
            normalized = sequence.toString(portable)
            if sequence.isEmpty():
                skipped.append(f'{text} is not a key sequence')
                continue
            if normalized in taken:
                self.hotkey_conflicts.add(action)
                skipped.append(f'{text} is already used by {taken[normalized]}')
                continue
            taken[normalized] = action
            shortcut = G.QShortcut(sequence, self.overlay.root)
            shortcut.setContext(C.Qt.ShortcutContext.WindowShortcut)
            shortcut.setAutoRepeat(False)
            shortcut.activated.connect(lambda action=action, callback=callbacks[action]: self._dispatch(action, callback))
            self._shortcuts.append(shortcut)
        if skipped:
            message = 'Trainer hotkeys turned off: ' + '; '.join(skipped) + '. Change them in trainer settings.'
            try:
                self.overlay.console.appendPlainText(message)
            except (AttributeError, RuntimeError):
                pass
            self.status.setText(message)

    def _dispatch(self, action, callback):
        controls = getattr(self.overlay, 'interaction_controls', None)
        if controls is not None:
            return controls.dispatch(action, callback, source='qt')
        callback()
        return True

    # -- live evidence --------------------------------------------------
    def attach_live(self, *, enable=None, note_at=None, selection=None, live_address=None):
        """Workspace session hook.

        enable(on) turns per-step receipts on or off. live_address() returns
        (host, command port); the owner's input log is read from port + 1.
        """
        self._live_enable = enable
        self._note_at = note_at
        self._selection = selection
        if live_address is not None and self.reader is None:
            self.reader = LiveInputReader(live_address, self)
            self.reader.frames.connect(self.accept_frames)
            self.reader.state.connect(self._live_state_changed)
            reader = self.reader
            self.destroyed.connect(lambda *_: reader.close())
        self._demand = None
        self._update_demand()

    def _update_demand(self):
        """Read inputs only while the panel is shown or a trial is running."""
        wanted = (not self.isHidden() or self._armed
                  or (self.run is not None and self.run.state == 'running'))
        # Per-step receipts cost an inspect payload; skip them while the live log flows.
        receipts = wanted and (self.live_state != 'live' or time.monotonic() - self._live_seen > 1.0)
        if (wanted, receipts) == self._demand:
            return
        changed = self._demand is None or wanted != self._demand[0]
        self._demand = (wanted, receipts)
        if self._live_enable is not None:
            try:
                self._live_enable(receipts)
            except Exception as exc:  # A display hook never fails the session.
                self.status.setText(f'Live checking is unavailable: {exc}')
        if self.reader is not None and changed:
            self.reader.set_active(wanted)
        self._refresh_source()

    def _live_state_changed(self, state):
        self.live_state = state
        self._update_demand()
        self._refresh_source()

    def _refresh_source(self):
        if self._live_enable is None and self.reader is None:
            text = 'Live input: needs the workspace session'
        elif not self._demand or not self._demand[0]:
            text = 'Live input: off while the trainer is hidden'
        elif self.live_state == 'live':
            text = 'Live input: every committed frame (Step, Play, scores and recording playback)'
        elif self.live_state == 'unsupported':
            text = 'Live input: Step and Apply only (this game session does not share live input)'
        elif self.live_state == 'offline':
            text = 'Live input: waiting for the game session'
        else:
            text = 'Live input: connecting'
        self.source_label.setText(text)

    def accept_frames(self, frames):
        events = []
        for frame in frames:
            events += self._feed_frame(frame, 'live')
        self._after_feed(events)

    def _frame_from_snapshot(self, snapshot):
        try:
            note = snapshot.last_inputs
            token = snapshot.owner_token or {}
            return T.frame_from_tokens(token['session_id'], snapshot.take_path, snapshot.tick,
                                       getattr(note, 'held', ()) or (), getattr(note, 'buttons', ()) or (),
                                       precision=snapshot.clock_precision)
        except (AttributeError, KeyError, TypeError, ValueError):
            return None

    def sequence_evidence(self, envelope):
        """Envelopes from the workspace: input, recorded, restore, unavailable."""
        try:
            kind, value = envelope
        except (TypeError, ValueError):
            return
        if kind == 'input':
            self._after_feed(self._feed_frame(value, 'receipt'))
        elif kind == 'recorded':
            frame = self._frame_from_snapshot(value)
            if frame is not None:
                self._after_feed(self._feed_frame(frame, 'recorded'))
        elif kind == 'restore':
            self.latest = None
            self._last_fed = None
            self._pending_restore = None
            try:
                # The next frame is the edited one after this restore. Use the
                # restored boundary as its baseline so that frame can land a step.
                note = getattr(value, 'last_inputs', None)
                held = (frozenset(k for token in (getattr(note, 'held', ()) or ()) for k in token_keys(token))
                        | frozenset(getattr(note, 'buttons', ()) or ()))
                token = getattr(value, 'owner_token', None) or {}
                self._pending_restore = (int(value.tick), held, (token.get('session_id'), getattr(value, 'take_path', None)))
            except (AttributeError, TypeError, ValueError):
                self._pending_restore = None
        else:
            # Multi-frame batches carry no per-step receipt. The live input log
            # or a recorded frame covers them; otherwise say so. Never a miss.
            recent = time.monotonic() - max(self._live_seen, self._recorded_seen) < .5
            self._update_demand()   # the live log went quiet: ask for receipts again
            if not recent and self.run is not None and self.run.state == 'running':
                self.status.setText('Paused: step one frame at a time to check inputs.')
                self.status.setToolTip(str(value))

    def _feed_frame(self, frame, source):
        """Merge the three sources; the owner's live log wins when it is flowing."""
        now = time.monotonic()
        if source == 'live':
            self._live_seen = now
        elif now - self._live_seen < .5:
            return []
        if source == 'recorded':
            self._recorded_seen = now
        lineage = (frame.session, frame.take)
        key = (lineage, frame.boundary)
        if self._last_fed is not None and self._last_fed[0] == key and self._last_fed[1] != source:
            return []             # the same frame from the other source
        self._last_fed = (key, source)
        self.latest = frame
        self.history.feed(frame.boundary, frame.held)
        if self.run is None:
            return []
        if frame.precision is not None:
            problem = self.run.check_precision(frame.precision)
            if problem:
                if self.run.state == 'running':
                    self.run.suspend(problem)
                return [T.Event('status', None, problem)]
        pending, self._pending_restore = self._pending_restore, None
        if self._armed:
            self._armed = False
            self.run.start(frame.boundary - 1, frozenset(), lineage)
        elif (pending is not None and self.run.state == 'running' and self.settings.mode == 'practice'
                and frame.boundary == pending[0] + 1 and pending[2] == lineage):
            self.run.start(pending[0], pending[1], lineage, text=f'Rewound to frame {pending[0]}. Trial restarted.')
        return self.run.feed(frame.boundary, frame.held, lineage, frame.identity)

    def _after_feed(self, events):
        if self.run is not None:
            self._present(events)
        self._refresh_history()
        self._update_demand()

    # -- trials ---------------------------------------------------------
    def add_trial(self, trial, *, select=True):
        for index, existing in enumerate(self.trials):
            if existing.source == trial.source and existing.id == trial.id:
                self.trials[index] = trial
                break
        else:
            self.trials.append(trial)
            index = len(self.trials) - 1
        self._refresh_trial_box()
        if select:
            self.select_trial(index)

    def _refresh_trial_box(self):
        self.trial_box.blockSignals(True)
        self.trial_box.clear()
        for trial in self.trials:
            self.trial_box.addItem(trial.title)
        self.trial_box.setCurrentIndex(self.position)
        self.trial_box.blockSignals(False)
        self.counter.setText(f'{self.position + 1}/{len(self.trials)}' if self.trials else '')
        self.prev_button.setEnabled(len(self.trials) > 1)
        self.next_button.setEnabled(len(self.trials) > 1)
        self.save_action.setEnabled(self.run is not None)
        self.remove_action.setEnabled(self.run is not None)
        self.clear_stats_action.setEnabled(self.run is not None)

    def select_trial(self, index):
        if not self.trials:
            self.status.setText('Open a trial or make one from frames')
            return
        index %= len(self.trials)
        self.position = index
        trial = self.trials[index]
        if self.run is not None and self.run.trial is trial:
            return self._refresh_trial_box()
        self.run = T.TrialRun(trial, self.settings)
        # Stats stay with each trial for this session; Trials > Clear stats resets them.
        self.run.stats = self._stats_by_trial.setdefault((trial.source, trial.id), T.Stats())
        self._armed = False
        if self.latest is not None:
            self.run.feed(self.latest.boundary, self.latest.held, (self.latest.session, self.latest.take))
        self._build_rows()
        self._refresh_trial_box()
        self.title.setText(trial.title)
        self.title.setToolTip(f'{trial.source}\nDrag to move')
        self.status.setText(self.run.status)
        self._refresh_all()
        self._update_demand()

    def clear_stats(self):
        if self.run is None:
            return
        trial = self.run.trial
        self.run.stats = self._stats_by_trial[(trial.source, trial.id)] = T.Stats()
        self.status.setText('Stats cleared for this trial')
        self._refresh_stats()

    def remove_trial(self):
        if self.run is None:
            return
        del self.trials[self.position]
        self.run = None
        self._build_rows()
        if self.trials:
            self.select_trial(min(self.position, len(self.trials) - 1))
        else:
            self.position = -1
            self._refresh_trial_box()
            self.title.setText('Input trainer')
            self._refresh_all()

    def open_trials(self):
        paths, _ = W.QFileDialog.getOpenFileNames(self.overlay.root, 'Open trial files', '',
                                                  'Trials (*.trial.toml *.sequence.toml);;TOML files (*.toml)')
        errors = []
        loaded = 0
        for path in paths:
            try:
                trial = T.load_trial(path)
            except (OSError, T.TrainerError) as exc:
                errors.append(f'{Path(path).name}: {exc}')
                continue
            self.add_trial(trial, select=loaded == 0)
            loaded += 1
        if errors:
            self.status.setText('Not loaded. ' + ' '.join(errors))

    def save_trial(self):
        if self.run is None:
            return
        trial = self.run.trial
        name = re.sub(r'[^\w.-]+', '-', trial.id).strip('-') or 'trial'
        path, _ = W.QFileDialog.getSaveFileName(self.overlay.root, 'Save trial', f'{name}.trial.toml', 'Trials (*.trial.toml)')
        if not path:
            return
        try:
            from .ui_profiles import _publish
            _publish(Path(path), T.trial_text(trial).encode('utf-8'))
            saved = T.load_trial(path)
        except (OSError, T.TrainerError) as exc:
            self.status.setText(f'Trial was not saved: {exc}')
            return
        self.trials[self.position] = saved
        self.run.trial = saved
        self._refresh_trial_box()
        self.status.setText(f'Saved {Path(path).name}')

    def make_from_frames(self):
        span = None
        if self._selection is not None:
            try:
                span = self._selection()
            except Exception:
                span = None
        if span is None and self.latest is not None:
            span = (max(1, self.latest.boundary - 59), self.latest.boundary)
        score_text = self._score_text()
        if self._note_at is None and not score_text.strip():
            self.status.setText('Load a score or open the workspace timeline first')
            return
        dialog = SpanDialog(self.overlay.root, span, has_timeline=self._note_at is not None, has_score=bool(score_text.strip()))
        if dialog.exec() != W.QDialog.DialogCode.Accepted:
            return
        first, last = sorted((dialog.first.value(), dialog.last.value()))
        title = dialog.title.text().strip() or f'Frames {first}–{last}'
        try:
            if last - first + 1 > T.MAX_SPAN:
                raise T.TrainerError(f'Pick at most {T.MAX_SPAN} frames')
            if dialog.source.currentData() == 'timeline':
                frames = [(tick, self._timeline_inputs(tick)) for tick in range(first, last + 1)]
            else:
                frames = T.score_frames(score_text, first, last, include_dirs=self._score_includes())
            trial = T.trial_from_frames(frames, title=title[:200], settings=self.settings)
        except T.TrainerError as exc:
            self.status.setText(str(exc))
            return
        self.add_trial(trial)
        self.status.setText(f'Made a trial with {len(trial.steps)} steps. Press Start, then play it.')

    def _timeline_inputs(self, tick):
        try:
            note = self._note_at(tick)
        except ValueError:
            return None
        return None if note is None else T.note_inputs(note)

    def _score_text(self):
        try:
            return self.overlay.score.toPlainText()
        except (AttributeError, RuntimeError):
            return ''

    def _score_includes(self):
        if getattr(self.overlay, 'control', None) is None:
            return ()
        try:
            return tuple(self.overlay._score_includes())
        except Exception:
            return ()

    # -- control --------------------------------------------------------
    def start_stop(self):
        if self.run is None:
            self.set_panel_visible(True)
            self.status.setText('Open a trial or make one from frames')
            return
        if self.run.state == 'running' or self._armed:
            self._armed = False
            self.run.stop()
        elif self._live_enable is None and self.reader is None:
            self.status.setText('Live checking needs the workspace session. You can still read the steps here.')
            return
        elif self.latest is not None:
            # If the game moved since (a seek), the run re-anchors at the next frame.
            self.run.start(self.latest.boundary, self.latest.held, (self.latest.session, self.latest.take))
        else:
            self._armed = True
            self.run.status = 'Ready. Play or step; the first frame starts the trial.'
        self._hold_rows_until = None
        self.row_timer.stop()
        self._refresh_all()
        self._update_demand()

    def reset_trial(self):
        if self.run is None:
            return
        self._hold_rows_until = None
        self.row_timer.stop()
        if self.latest is None:
            self._armed = self._live_enable is not None or self.reader is not None
            self.run.reset()
        else:
            self.run.start(self.latest.boundary, self.latest.held, (self.latest.session, self.latest.take))
        self._refresh_all()
        self._update_demand()

    def set_panel_visible(self, visible):
        visible = bool(visible)
        if self.isVisible() != visible:
            self.setVisible(visible)
            if visible:
                self._place()
                self.raise_()
        self.menu_action.blockSignals(True)
        self.menu_action.setChecked(visible)
        self.menu_action.blockSignals(False)
        if self.settings.panel_visible != visible:
            self.settings = T.replace_settings(self.settings, panel_visible=visible)
            self._save()
        self._update_demand()

    # -- presentation ---------------------------------------------------
    def _build_rows(self):
        for row in self.rows:
            row.hide()
            row.deleteLater()
        self.rows = []
        if self.run is None:
            return
        for number, step in enumerate(self.run.trial.steps, 1):
            row = StepRow(number, step, self)
            self.steps_layout.insertWidget(self.steps_layout.count() - 1, row)
            self.rows.append(row)

    def _present(self, events):
        if not events:
            if self._hold_rows_until is None:
                self._sync_rows(keep_animations=True)
            self._refresh_stats()
            self._refresh_controls()
            return
        if self._hold_rows_until is not None and any(e.result is not None for e in events):
            # The new attempt has started landing steps: show it now.
            self.row_timer.stop()
            self._release_rows()
        reduced = self.reduced_motion()
        miss = next((e for e in events if e.kind == 'miss'), None)
        for event in events:
            if event.index is not None and event.result is not None and event.index < len(self.rows):
                result = event.result
                if event.kind == 'miss':
                    self._sound('miss')
                elif event.kind == 'hit':
                    self._sound('perfect' if result.verdict == 'perfect' else 'hit')
                if self._hold_rows_until is None or event.kind == 'miss':
                    self.rows[event.index].apply(result, animate=event.kind != 'closed', reduced=reduced)
                self._show_timing(event.index, result)
            elif event.kind == 'complete':
                self._sound('complete')
                self._flash_panel(reduced)
        self.status.setText(miss.text if miss is not None else self.run.status)
        self._status_color('miss' if miss is not None else 'text')
        finished = miss is not None or any(e.kind == 'complete' for e in events)
        if finished and any(e.kind == 'reset' for e in events):
            # The run already started over. Keep the miss or the full list of
            # checks on screen for a moment before showing the fresh attempt.
            self._hold_rows_until = True
            self.row_timer.start(self.settings.reset_delay_ms)
        elif self._hold_rows_until is None:
            self._sync_rows(keep_animations=True)
        self._refresh_stats()
        self._refresh_controls()

    def _sound(self, name):
        if self.run is not None and self.run.trial.sound is False:
            return            # The trial file asks for silent feedback.
        self.sounds.play(name, self.settings, self)

    def _release_rows(self):
        self._hold_rows_until = None
        if self.run is not None:
            self.status.setText(self.run.status)
            self._status_color('text')
        self._sync_rows()

    def _flash_panel(self, reduced):
        if reduced or not self.rows:
            return
        for row in self.rows:
            if row.isVisible():
                row.flash_name = 'perfect'
                row.flasher.stop()
                row.flasher.setDuration(int(600 / self.settings.animation_speed))
                row.flasher.start()

    def _show_timing(self, index, result):
        step = self.run.trial.steps[index]
        low, high = self.run.window(step)
        rate = self.settings.frame_rate
        parts = [f'Step {index + 1} · {step.label}:']
        if result.state == 'miss' and result.reason:
            parts.append(result.reason)
        elif result.verdict == 'perfect':
            parts.append('Perfect, on the target frame.')
        elif result.verdict in ('early', 'late'):
            parts.append(f'{result.verdict.title()} by {T._frames(abs(result.offset))} '
                         f'({abs(result.offset) * 1000 / rate:.0f} ms).')
        elif result.elapsed is not None:
            parts.append(f'Landed {T._frames(result.elapsed)} after the previous step.')
        if high is not None:
            width = high - low + 1
            parts.append(f'Window {T._frames(width)} ({width * 1000 / rate:.0f} ms)'
                         + (f', target +{step.target}.' if step.timed else f', +{low}–{high}.'))
        if result.hold is not None:
            parts.append(f'Held {T._frames(result.hold)}.')
        self.timing_text.setText(' '.join(parts))
        if high is not None or result.elapsed is not None:
            kind = 'miss' if result.state == 'miss' else result.verdict
            self.timing_bar.set_window(self.settings, (low, high, step.target), result.elapsed, kind,
                                       animate=not self.reduced_motion(), duration=int(220 / self.settings.animation_speed))

    def _sync_rows(self, *, keep_animations=False):
        if self.run is None:
            return
        current = self.run.index
        show_next, show_done = self.settings.show_next, self.settings.show_done
        for index, row in enumerate(self.rows):
            if show_next == 0:
                visible = True
            else:
                visible = current - show_done <= index < current + show_next
            row.setVisible(visible)
            result = self.run.results[index]
            state = 'perfect' if result.state == 'hit' and result.verdict == 'perfect' else result.state
            if not keep_animations or row.state != state:
                row.apply(result, animate=False)
        if 0 <= current < len(self.rows):
            self.scroll.ensureWidgetVisible(self.rows[current], 0, 24)
        visible_rows = [row for row in self.rows if row.isVisible()]
        height = sum(row.sizeHint().height() + 2 for row in visible_rows)
        self.scroll.setFixedHeight(max(28, min(height + 4, 320)))
        self._fit()

    def _refresh_stats(self):
        if self.run is None:
            self.stats_label.setText('')
            return
        stats = self.run.stats
        parts = [f'This trial: hits {stats.hits}', f'Misses {stats.misses}', f'Perfect {stats.perfect}',
                 f'Best streak {stats.best_streak}', f'Clears {stats.completions}']
        average = stats.average_offset
        if average is not None:
            parts.append(f'Average {average:+.1f} f (±{stats.average_distance:.1f})')
        self.stats_label.setText(' · '.join(parts))
        self.stats_label.setToolTip('Kept per trial until you close Stasis. Trials > Clear stats for this trial resets them.')
        self.stats_label.setStyleSheet(f'color:{self.settings.colors["muted"]};')
        self.source_label.setStyleSheet(f'color:{self.settings.colors["muted"]};')

    def _refresh_history(self):
        self.history_strip.update_height()
        entries = list(self.history.entries)[::-1][:self.settings.history_entries]
        style = 'compact' if self.settings.glyph_style == 'compact' else 'text'
        text = '   '.join(f'{T.chord_label(e.held, style) if e.held else "·"} {e.frames}'
                           + (' ⋯' if e.gap else '') for e in entries)
        self.history_text.setText(text)
        self.history_text.setStyleSheet(f'color:{self.settings.colors["muted"]};')
        self._fit()

    def _status_color(self, name):
        self.status.setStyleSheet(f'color:{self.settings.colors[name]};font-size:14px;font-weight:600;')

    def _refresh_controls(self):
        running = self.run is not None and (self.run.state == 'running' or self._armed)
        self.start_button.setText('Stop' if running else 'Start')
        self.start_button.setEnabled(self.run is not None)
        self.reset_button.setEnabled(self.run is not None)

    def _refresh_all(self):
        if self.run is not None:
            self.status.setText(self.run.status)
            self._status_color('text')
        self._refresh_controls()
        self._sync_rows()
        self._refresh_stats()
        self._refresh_history()

    # -- placement ------------------------------------------------------
    def _fit(self):
        root = self.overlay.root
        self.setMaximumHeight(max(160, root.height() - 16))
        self.adjustSize()
        self._place()

    def _place(self):
        root = self.overlay.root
        s = self.settings
        width, height = self.width(), self.height()
        if s.panel_corner == 'custom':
            x, y = s.panel_x, s.panel_y
        else:
            x = s.panel_x if s.panel_corner.endswith('left') else root.width() - width - s.panel_x
            y = s.panel_y if s.panel_corner.startswith('top') else root.height() - height - s.panel_y
        x = min(max(0, x), max(0, root.width() - width))
        y = min(max(0, y), max(0, root.height() - height))
        if (x, y) != (self.x(), self.y()):
            self.move(x, y)

    def _remask(self):
        try:
            self.overlay.mask()
        except (AttributeError, RuntimeError):
            pass

    def eventFilter(self, watched, event):
        if watched is self.overlay.root and event.type() == C.QEvent.Type.Resize:
            C.QTimer.singleShot(0, self._place)
        return False

    def moveEvent(self, event):
        super().moveEvent(event)
        self._remask()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._remask()

    def showEvent(self, event):
        super().showEvent(event)
        self.raise_()
        self._remask()
        self._update_demand()

    def hideEvent(self, event):
        super().hideEvent(event)
        self._remask()
        self._update_demand()

    def mousePressEvent(self, event):
        if event.button() == C.Qt.MouseButton.LeftButton and event.position().y() <= self.title.geometry().bottom() + 4:
            self._drag = event.globalPosition().toPoint() - self.pos()
            self._drag_origin = event.globalPosition().toPoint()
            self._drag_moved = False
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag is not None:
            point = event.globalPosition().toPoint()
            if not self._drag_moved and (point - self._drag_origin).manhattanLength() < 4:
                return
            self._drag_moved = True
            self.move(point - self._drag)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._drag is not None:
            self._drag = None
            if self._drag_moved:
                self.settings = T.replace_settings(self.settings, panel_corner='custom', panel_x=self.x(), panel_y=self.y())
                self._place()
                self._save()
            event.accept()
            return
        super().mouseReleaseEvent(event)


def install_trainer(overlay):
    """Create the trainer panel, its menu entry and its hotkeys."""
    return TrainerPanel(overlay)
