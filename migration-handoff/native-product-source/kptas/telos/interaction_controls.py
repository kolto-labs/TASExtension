"""Presentation visibility and engagement policy; no game input ownership.

Disable is deliberately unavailable until the canonical owner can acknowledge a
physical-input transition. Hiding widgets never substitutes for that operation.
"""
from dataclasses import replace
import json
import os
from pathlib import Path
import sys
from PySide6 import QtCore as C, QtGui as G, QtWidgets as W


ENGAGE_UNAVAILABLE = ('Engage is off: this game session cannot hand keyboard and mouse input '
                      'between you and Stasis yet. In this version Stasis plays all game input.')


def stasis_icon():
    """Small teal S on a dark square, so the tray entry is recognisable."""
    pixmap = G.QPixmap(32, 32)
    pixmap.fill(G.QColor('#0b1016'))
    painter = G.QPainter(pixmap)
    painter.setPen(G.QColor('#19e3c1'))
    font = painter.font()
    font.setBold(True)
    font.setPixelSize(24)
    painter.setFont(font)
    painter.drawText(pixmap.rect(), C.Qt.AlignmentFlag.AlignCenter, 'S')
    painter.end()
    return G.QIcon(pixmap)


class ClickActivation(C.QObject):
    """Give the overlay keyboard focus only when the user clicks it.

    The frameless Tool window shows without activating, so the game keeps focus
    during play. A deliberate click on Stasis activates it, so window-scoped keys
    such as F1 and Ctrl+, work. During playback a click never takes focus.
    Clicking the game gives focus back to the game.
    """
    def __init__(self, overlay):
        super().__init__(overlay.root)
        self.overlay = overlay
        self.root = overlay.root

    def _playing(self):
        # During play the game keeps keyboard focus even when Stasis is clicked
        # (for example Pause); window keys work again once playback stops.
        presentation = getattr(self.overlay, 'presentation', None)
        if presentation is not None:
            run = getattr(getattr(presentation, 'snapshot', None), 'run', None)
            return bool(presentation.playing or (run is not None and run.playing))  # a paused score is not playing
        return bool(getattr(self.overlay.control, 'playing', False))

    def eventFilter(self, watched, event):
        if event.type() == C.QEvent.Type.MouseButtonPress and isinstance(watched, W.QWidget):
            try:
                if (watched.window() is self.root and not self.root.isActiveWindow()
                        and not self._playing()):
                    self.root.activateWindow()
            except RuntimeError:
                pass
        return False


def visibility_state_path():
    """Interface visibility survives restarts, separate from layout TOML."""
    base = os.environ.get('XDG_STATE_HOME') or str(Path.home() / '.local/state')
    return Path(base) / 'kptas' / 'ui-interface.json'


def load_saved_visibility():
    """KPTAS_UI_INTERFACE=visible|hidden overrides the saved state."""
    forced = os.environ.get('KPTAS_UI_INTERFACE', 'saved')
    if forced in ('visible', 'hidden'):
        return forced == 'visible'
    try:
        value = json.loads(visibility_state_path().read_text(encoding='utf-8')).get('visible', True)
        return value if type(value) is bool else True
    except (OSError, ValueError, AttributeError):
        return True


def save_visibility(visible):
    path = visibility_state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + '.tmp')
        temporary.write_text(json.dumps({'visible': bool(visible)}) + '\n', encoding='utf-8')
        os.replace(temporary, path)
    except OSError as exc:
        print(f'Stasis: could not save whether the interface is shown: {exc}', file=sys.stderr)


class InteractionControls(C.QObject):
    def __init__(self, overlay):
        super().__init__(overlay.root)
        self.overlay = overlay
        self.visible = True
        self._changing = False
        self._hidden_widgets = {}
        self.saved_panel_states = None
        self._design_hidden = False
        menu = overlay.more_menu
        self.visibility_action = menu.addAction('Hide Stasis', self.toggle_visibility)
        self.visibility_binding = ''
        self.visibility_hint = ''  # Last show/hide notice, for the launcher console and tests.
        self.engagement_action = menu.addAction('Engage TAS', self.toggle_engagement)
        self.engagement_action.setEnabled(False)
        self.engagement_action.setToolTip(ENGAGE_UNAVAILABLE)
        self.pause_action = menu.addAction('Pause on engage')
        self.pause_action.setCheckable(True)
        self.pause_action.setToolTip('Pause playback when you engage TAS')
        self.pause_action.setChecked(overlay.ui_preferences.pause_on_engage)
        self.pause_action.toggled.connect(self._pause_policy_changed)
        self.tray = None
        if W.QSystemTrayIcon.isSystemTrayAvailable():
            self.tray = W.QSystemTrayIcon(stasis_icon(), self)
            self.tray.setToolTip('Stasis by Kolto Labs')
            tray_menu = W.QMenu(overlay.root)
            tray_menu.addAction('Show Stasis', lambda: self.set_visible(True))
            self.tray.setContextMenu(tray_menu)
            self.tray.activated.connect(self._tray_activated)
            self.tray.show()
        overlay.root.installEventFilter(self)
        self.click_activation = ClickActivation(overlay)
        overlay.app.installEventFilter(self.click_activation)
        if not load_saved_visibility():
            # Restore after the window is shown and laid out.
            C.QTimer.singleShot(0, self._restore_hidden)

    def _restore_hidden(self):
        if self.visible:
            self.hide_interface()
            if not self.visible:
                self._announce('Stasis started hidden', notify=True)

    def _announce(self, text, *, notify=False):
        key = self.visibility_binding or 'the tray icon'
        self.visibility_hint = f'{text}. Press {key} to show it.' if not self.visible else ''
        if self.visibility_hint:
            print('Stasis: ' + self.visibility_hint, file=sys.stderr, flush=True)
            if self.tray is not None:
                self.tray.setToolTip('Stasis (hidden) · ' + key)
                if notify:
                    self.tray.showMessage('Stasis is hidden', self.visibility_hint,
                                          W.QSystemTrayIcon.MessageIcon.Information, 3000)
        elif self.tray is not None:
            self.tray.setToolTip('Stasis by Kolto Labs')

    def set_visible(self, visible):
        """User-initiated show/hide: presentation only, persisted across restarts."""
        if visible:
            self.show_interface()
        else:
            self.hide_interface()
        if self.visible == bool(visible):
            save_visibility(self.visible)
            self._announce('Stasis is hidden')

    def _pause_policy_changed(self, enabled):
        # This persists future engagement policy, never a live playing flag.
        self.overlay.ui_preferences = replace(self.overlay.ui_preferences,
                                               pause_on_engage=bool(enabled))

    def apply_preferences(self, preferences):
        previous = self.pause_action.blockSignals(True)
        self.pause_action.setChecked(preferences.pause_on_engage)
        self.pause_action.blockSignals(previous)
        self.refresh_engagement()
        binding = dict(preferences.shortcuts).get('toggle_visibility', '')
        self.visibility_binding = binding
        self.visibility_action.setToolTip('Show or hide Stasis. The game keeps running.' + (' · '+binding if binding else ''))

    def refresh_engagement(self):
        """Display only acknowledged ownership; qualification is never inferred."""
        presentation = getattr(self.overlay, 'presentation', None)
        snapshot = getattr(presentation, 'snapshot', None)
        owner = snapshot if snapshot is not None else self.overlay.control
        qualified = getattr(owner, 'engagement_qualified', False) is True
        engaged = getattr(owner, 'engaged', None)
        unavailable = (getattr(owner, 'unknown_state', False) or getattr(owner, 'error', None)
                       or getattr(presentation, 'pending', False)
                       or getattr(presentation, 'awaiting_ack', False)
                       or bool(getattr(owner, 'run', None) and owner.run.active))
        admitted = qualified and type(engaged) is bool and not unavailable
        self.engagement_action.setEnabled(admitted)
        self.engagement_action.setText('Disable TAS' if engaged is True else 'Engage TAS')
        self.engagement_action.setToolTip(
            'Return input to the game' if admitted and engaged else
            'Take control of game input' if admitted else
            ENGAGE_UNAVAILABLE if not qualified else
            'Engage is off until the current command finishes.')

    def dispatch(self, action, callback=None, *, source='qt'):
        """One named-action dispatcher; native events already own game focus."""
        if source == 'qt':
            focus = self.overlay.app.focusWidget()
            editing = isinstance(focus, (W.QLineEdit, W.QTextEdit, W.QPlainTextEdit, W.QAbstractSpinBox))
            if self.overlay.app.activeModalWidget() is not None or editing:
                return False
        if action == 'toggle_visibility':
            self.toggle_visibility()
        elif action == 'toggle_engagement':
            return self.toggle_engagement()
        elif callback is not None:
            callback()
        elif action == 'toggle_editor':
            self.overlay.toggle()
        elif action == 'switch_layout':
            self.overlay.switch_layout()
        elif action == 'reload_ui':
            self.overlay.reload_ui_preferences()
        elif action == 'apply':
            return self.overlay.queue('apply_selected')
        else:
            return self.overlay.queue(action)
        return True

    def begin_design_change(self):
        # A design reload may swap the embedded game widget. Restore first,
        # while the caller has painting suspended; suppress using the new design.
        self._design_hidden = not self.visible
        if self._design_hidden:
            self.show_interface()

    def finish_design_change(self):
        if self._design_hidden:
            self._design_hidden = False
            self.hide_interface()

    def _tray_activated(self, reason):
        if reason in (W.QSystemTrayIcon.ActivationReason.Trigger,
                      W.QSystemTrayIcon.ActivationReason.DoubleClick):
            self.set_visible(True)

    def toggle_engagement(self):
        # Native capability is necessary; actual qualification is separately
        # required. Never infer ownership from visibility or Play/Pause.
        snapshot = getattr(getattr(self.overlay, 'presentation', None), 'snapshot', None)
        owner = self.overlay.control
        qualified = (getattr(snapshot, 'engagement_qualified', False) is True
                     if snapshot is not None else getattr(owner, 'engagement_qualified', False) is True)
        engaged = getattr(snapshot, 'engaged', None) if snapshot is not None else getattr(owner, 'engaged', None)
        if not qualified or type(engaged) is not bool:
            self.overlay.console.appendPlainText(ENGAGE_UNAVAILABLE)
            return False
        return self.overlay.queue('set_engaged', engaged=not engaged,
                                  pause_on_engage=self.pause_action.isChecked())

    def toggle_visibility(self):
        self.set_visible(not self.visible)

    def hide_interface(self):
        overlay = self.overlay
        if not self.visible:
            return
        embedded = (overlay.ui_preferences.layout == 'workspace'
                    or overlay.ui_preferences.game_surface == 'owner_frame')
        if not embedded and self.tray is None:
            # A hidden native overlay cannot receive its Qt window shortcut.
            # Refuse rather than strand the user without any recovery route.
            message = 'Stasis cannot hide here: this desktop has no system tray to bring it back. Use More > Show minimal bar instead.'
            overlay.console.appendPlainText(message)
            if hasattr(overlay, 'set_notice'):
                overlay.set_notice('Cannot hide', message, error=True, ms=10000)
            return
        self.saved_panel_states = (overlay.panel_manager.get_states()
                                   if hasattr(overlay, 'panel_manager') else None)
        self.visible = False
        self.visibility_action.setText('Show Stasis')
        self._changing = True
        try:
            if embedded:
                game = overlay.workspace_preview if overlay.ui_preferences.layout == 'workspace' else overlay.game_canvas
                self._hidden_widgets = {widget: not widget.isHidden()
                    for widget in overlay.root.findChildren(W.QWidget, options=C.Qt.FindChildOption.FindDirectChildrenOnly)
                    if widget is not game and not isinstance(widget, W.QMenu)}
                for widget in self._hidden_widgets:
                    widget.installEventFilter(self)
                    widget.hide()
                overlay.root.setMask(G.QRegion(overlay.root.rect()))
            else:
                overlay.root.hide()
        finally:
            self._changing = False

    def show_interface(self):
        if self.visible:
            return
        overlay = self.overlay
        self.visible = True
        self._changing = True
        try:
            for widget, wanted in self._hidden_widgets.items():
                widget.removeEventFilter(self)
                widget.setVisible(wanted)
            self._hidden_widgets.clear()
            self.saved_panel_states = None
            # WA_ShowWithoutActivating is already configured by the workspace.
            overlay.root.show()
            overlay.mask()
        finally:
            self._changing = False
        self.visibility_action.setText('Hide Stasis')

    def eventFilter(self, watched, event):
        if self.visible or self._changing:
            return False
        if event.type() == C.QEvent.Type.Show:
            if watched in self._hidden_widgets:
                self._hidden_widgets[watched] = True
                self._changing = True
                try:
                    watched.hide()
                finally:
                    self._changing = False
            elif watched is self.overlay.root and not self._hidden_widgets:
                self._changing = True
                try:
                    watched.hide()
                finally:
                    self._changing = False
        return False

    def close(self):
        try:
            self.overlay.app.removeEventFilter(self.click_activation)
        except (AttributeError, RuntimeError):
            pass
        if self.tray is not None:
            self.tray.hide()


def activate_shortcut(overlay, action, callback):
    """Editing and modal widgets own their text keys; commands are edge-only."""
    return overlay.interaction_controls.dispatch(action, callback, source='qt')
