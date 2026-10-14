"""Presentation adapter for the existing native WorkspaceWindow, without Qt imports.

Call on the window's GUI thread, after its original controls are refreshed.
This module does not construct a window, session, transport, timer, or command queue.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


SCHEMA = 'kptas-ui-surface/1'


@dataclass(frozen=True)
class ActionView:
    id: str
    label: str
    enabled: bool
    disabled_reason: str
    shortcut: str | None
    checked: bool | None = None


@dataclass(frozen=True)
class SurfaceView:
    schema: str
    presentation: str
    expanded: bool
    visible: bool
    follow_playhead: bool
    unit: str
    clock_precision: str | None
    owner_capabilities: tuple[str, ...]
    unknown_state: bool | None
    committed_tick: int | None
    selected_tick: int | None
    preview_target: int | None
    seek_min: int
    seek_max: int
    durable_tick: int | None
    engaged: bool | None
    pending: bool
    awaiting_ack: bool
    status: str
    error: str
    actions: tuple[ActionView, ...]


@dataclass(frozen=True)
class DispatchResult:
    """Local dispatch only. Never a game ACK, completed seek, or saved recording."""
    dispatched: bool
    reason: str = ''


@dataclass(frozen=True)
class _Binding:
    path: str
    activation: str = 'click'


# Stable action names follow the existing native action registry. These point
# to its original widgets/actions, not duplicate callbacks or capability rules.
_BINDINGS = {
    'play_pause': _Binding('overlay.play'),
    'step': _Binding('overlay.step'),
    'rewind': _Binding('overlay.rewind'),
    'seek': _Binding('overlay.seek_go'),
    'apply': _Binding('apply'),
    'save': _Binding('overlay.save_button'),
    'toggle_editor': _Binding('overlay.expand'),
    'switch_layout': _Binding('overlay.ui_switch_action', 'trigger'),
    'toggle_visibility': _Binding('overlay.interaction_controls.visibility_action', 'trigger'),
    'toggle_engagement': _Binding('overlay.interaction_controls.engagement_action', 'trigger'),
    'follow_playhead': _Binding('follow_action', 'trigger'),
    'undo_input_edit': _Binding('undo_action', 'trigger'),
    'redo_input_edit': _Binding('redo_action', 'trigger'),
}


def _position(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


class NativeSurface:
    """Expose one existing native UI as data and guarded user activations.

    The native host stays authoritative for capability, shortcut focus, draft
    history, restore/replay, input ownership, and persistence. In particular,
    hiding the surface or changing layout never dispatches Play/Pause.
    """

    def __init__(self, window: Any):
        self.window = window

    def _require_gui_thread(self):
        if self.window.overlay.C.QThread.currentThread() != self.window.root.thread():
            raise RuntimeError('NativeSurface must be used on the workspace GUI thread')

    def _widget(self, action: str) -> Any:
        value = self.window
        for part in _BINDINGS[action].path.split('.'):
            value = getattr(value, part, None)
            if value is None:
                break
        return value

    def action(self, action: str) -> ActionView:
        self._require_gui_thread()
        if action not in _BINDINGS:
            raise ValueError(f'Unknown TAS action: {action}')
        widget = self._widget(action)
        if widget is None:
            return ActionView(action, action.replace('_', ' ').capitalize(), False,
                              'Unavailable in this native host', '')
        enabled = not self.window.closed and widget.isEnabled()
        if action == 'seek':
            enabled = enabled and self.window.overlay.slider.seek_enabled
        label = widget.text().strip()
        accessible = getattr(widget, 'accessibleName', None)
        # Accessible names are often static ("Play or pause"). Keep the current
        # button wording, which can instead be Pause, Stop run, or Confirm stop.
        if (not label or action == 'seek') and callable(accessible) and accessible():
            label = accessible()
        reason = ''
        if not enabled:
            # Most original tooltips describe an action, rather than explaining
            # its current refusal. Preserve them as context, not as a reason.
            reason = self.window.disabled_reason() or 'Unavailable in the current session'
            if widget.toolTip() and widget.toolTip() != reason:
                reason += '. ' + widget.toolTip()
        if self.window.closed:
            reason = 'The native workspace is closed'
        shortcuts = dict(self.window.overlay.ui_preferences.shortcuts)
        shortcut = shortcuts.get(action, '')
        # The host binds Follow through a separate registry QShortcut, not the
        # QAction. Its resolved key is not exposed here: None means unknown,
        # while an empty string means no key on the inspected binding.
        if action == 'follow_playhead':
            shortcut = None
        # Draft edit QActions carry the resolved registry shortcut keys.
        elif action not in shortcuts and hasattr(widget, 'shortcuts'):
            shortcut = ' / '.join(key.toString() for key in widget.shortcuts())
        checked = widget.isChecked() if widget.isCheckable() else None
        return ActionView(action, label or action, bool(enabled), reason, shortcut, checked)

    def view(self) -> SurfaceView:
        """Project acknowledged and presentation positions without conflating them."""
        self._require_gui_thread()
        w, o = self.window, self.window.overlay
        s = w.snapshot
        engaged = getattr(s, 'engaged', None)
        # These host flags are booleans, not message strings. Never replace a
        # useful owner/draft error with the literal text "True".
        error = w.error.text() or getattr(s, 'error', '') or ''
        if not error and (w.owner_error or w.draft_failure):
            error = w.disabled_reason() or 'Resolve the owner or input draft error'
        return SurfaceView(
            schema=SCHEMA,
            presentation=o.ui_preferences.layout,
            expanded=bool(o.expanded),
            visible=bool(o.interaction_controls.visible),
            follow_playhead=bool(w.follow_action.isChecked()),
            unit=o._unit(),
            clock_precision=getattr(s, 'clock_precision', None),
            owner_capabilities=tuple(sorted(getattr(s, 'capabilities', ()))),
            unknown_state=s.unknown_state if s is not None else None,
            committed_tick=_position(getattr(s, 'tick', None)),
            selected_tick=_position(w.selected_tick()) if s is not None else None,
            preview_target=_position(getattr(w.future_rail, 'target', None)),
            seek_min=o.slider.minimum(),
            seek_max=o.slider.maximum(),
            durable_tick=_position(getattr(s, 'durable_tick', None)),
            engaged=engaged if type(engaged) is bool else None,
            pending=bool(w.mutation_pending()),
            awaiting_ack=bool(w.awaiting_owner_ack()),
            status=o.state.text(),
            error=str(error),
            actions=tuple(self.action(action) for action in _BINDINGS),
        )

    def activate(self, action: str, *, tick: int | None = None,
                 keyboard: bool = False) -> DispatchResult:
        """Activate once; never retry or report a dispatched mutation as committed.

        Pointer/menu activations use the original Qt control. Keyboard callers
        use the original editing/modal focus guard, so typing cannot step the
        game. Invoke from an existing host shortcut callback after Qt resolves
        its scope and key; this method is not a raw/global key dispatcher.
        Exact seek accepts a target only on release/explicit submit;
        previewing/dragging a position is the existing rail's responsibility.
        """
        state = self.action(action)
        if not state.enabled:
            return DispatchResult(False, state.disabled_reason)
        if type(keyboard) is not bool:
            raise ValueError('keyboard must be a boolean')
        if action == 'seek':
            slider = self.window.overlay.slider
            if type(tick) is not int or not slider.minimum() <= tick <= slider.maximum():
                return DispatchResult(False, 'Choose a position in the recorded range')
        elif tick is not None:
            raise ValueError('Only seek accepts a tick')

        result = None

        def invoke():
            nonlocal result
            if action == 'seek':
                # submit retains the owner snapshot token and FutureRail path;
                # do not issue raw rewind or mutate the displayed tick.
                result = self.window.submit('seek', tick,
                                            expected_snapshot=self.window.snapshot)
            else:
                getattr(self._widget(action), _BINDINGS[action].activation)()

        if keyboard:
            # Passing the callback keeps the same QAction/widget behavior;
            # the existing dispatcher handles visibility/engagement itself.
            allowed = self.window.overlay.interaction_controls.dispatch(
                action, callback=invoke, source='qt')
            if allowed is False:
                return DispatchResult(False, 'The current focus or owner refuses this action')
        else:
            invoke()
        if result is False:
            return DispatchResult(False, self.window.error.text() or 'The owner refused the request')
        return DispatchResult(True)
