"""Movable Qt panel chrome and plain presentation state; no TAS or file ownership."""
from __future__ import annotations

import math
import re
from collections.abc import Mapping
from PySide6 import QtCore as C, QtGui as G, QtWidgets as W

_FIELDS = {'x', 'y', 'width', 'height', 'visible', 'collapsed', 'opacity'}


def validate_panel_states(states):
    """Validate an entire partial state update before any widget is changed."""
    if not isinstance(states, Mapping) or len(states) > 64:
        raise ValueError('panel states must be a mapping with at most 64 panels')
    result = {}
    for name, state in states.items():
        if not isinstance(name, str) or re.fullmatch(r'[a-z][a-z0-9_-]{0,47}', name) is None:
            raise ValueError('invalid panel identifier')
        if not isinstance(state, Mapping) or set(state) - _FIELDS:
            raise ValueError(f'{name}: invalid panel state fields')
        clean = {}
        for key, value in state.items():
            if key in {'visible', 'collapsed'}:
                if type(value) is not bool:
                    raise ValueError(f'{name}.{key}: expected a boolean')
            elif key == 'opacity':
                if type(value) not in (int, float) or not math.isfinite(value) or not .1 <= value <= 1:
                    raise ValueError(f'{name}.opacity: expected 0.1 through 1.0')
                value = float(value)
            elif type(value) is not int or not (-100000 <= value <= 100000):
                raise ValueError(f'{name}.{key}: expected a bounded integer')
            elif key in {'width', 'height'} and value < 1:
                raise ValueError(f'{name}.{key}: expected a positive size')
            clean[key] = value
        result[name] = clean
    return result


class _Grip(W.QLabel):
    def __init__(self, panel, *, resize=False):
        super().__init__('◢' if resize else panel.title, panel.widget)
        self.panel, self.resize_panel = panel, resize
        self.setCursor(C.Qt.SizeFDiagCursor if resize else C.Qt.SizeAllCursor)
        self.setToolTip('Drag to resize panel' if resize else 'Drag to move panel')
        self.setAccessibleName((('Resize ' if resize else 'Move ') + panel.title))
        self.origin = None

    def mousePressEvent(self, event):
        if event.button() == C.Qt.LeftButton:
            self.origin = (event.globalPosition().toPoint(), self.panel.state())
            self.panel.widget.raise_()
            self.panel.manager.menu_button.raise_()
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self.origin is None:
            return super().mouseMoveEvent(event)
        point, state = self.origin
        delta = event.globalPosition().toPoint() - point
        update = ({'width': state['width'] + delta.x(), 'height': state['height'] + delta.y()}
                  if self.resize_panel else {'x': state['x'] + delta.x(), 'y': state['y'] + delta.y()})
        if self.resize_panel:
            update = {key: max(1, value) for key, value in update.items()}
        self.panel.apply(update)
        self.panel.manager.changed()
        event.accept()

    def mouseReleaseEvent(self, event):
        if event.button() == C.Qt.LeftButton and self.origin is not None:
            self.origin = None
            self.panel.manager.changed()
            event.accept()
        else:
            super().mouseReleaseEvent(event)


class PanelController(C.QObject):
    HEADER = 28

    def __init__(self, manager, name, widget, title, min_size):
        super().__init__(widget)
        self.manager, self.name, self.widget, self.title = manager, name, widget, title
        self.requested_min_size = min_size
        self.min_size = min_size
        self.collapsed = False
        self._expanded_height = max(widget.height(), min_size[1])
        self._hidden_children = []
        layout = widget.layout()
        if layout is not None:
            margins = layout.contentsMargins()
            layout.setContentsMargins(margins.left(), margins.top() + self.HEADER,
                                      margins.right() + 8, margins.bottom() + 6)
            layout.setSizeConstraint(W.QLayout.SetNoConstraint)
            minimum = layout.minimumSize()
            self.min_size = (max(min_size[0], minimum.width()), max(min_size[1], minimum.height()))
        if layout is None:
            widget.setContentsMargins(0,self.HEADER,0,0)
        self.title_bar = W.QFrame(widget)
        self.title_bar.setObjectName('panelControlsTitle')
        bar = W.QHBoxLayout(self.title_bar)
        bar.setContentsMargins(5, 2, 3, 2)
        bar.setSpacing(8)
        self.drag_grip = _Grip(self)
        bar.addWidget(self.drag_grip, 1)
        self.opacity_slider = W.QSlider(C.Qt.Horizontal, self.title_bar)
        self.opacity_slider.setRange(10, 100)
        self.opacity_slider.setValue(100)
        self.opacity_slider.setFixedWidth(46)
        self.opacity_slider.setToolTip('Panel opacity')
        self.opacity_slider.setAccessibleName(title + ' opacity')
        bar.addWidget(self.opacity_slider)
        self.collapse_button = W.QToolButton(self.title_bar)
        self.collapse_button.setText('−')
        self.collapse_button.setToolTip('Minimize panel')
        self.collapse_button.setAccessibleName('Minimize ' + title)
        bar.addWidget(self.collapse_button)
        self.hide_button = W.QToolButton(self.title_bar)
        self.hide_button.setText('×')
        self.hide_button.setToolTip('Hide panel; restore it from Panels')
        self.hide_button.setAccessibleName('Hide ' + title)
        bar.addWidget(self.hide_button)
        self.resize_grip = _Grip(self, resize=True)
        self.resize_grip.setAlignment(C.Qt.AlignRight | C.Qt.AlignBottom)
        self.effect = W.QGraphicsOpacityEffect(widget)
        self.effect.setOpacity(1)
        widget.setGraphicsEffect(self.effect)
        self.opacity_slider.valueChanged.connect(self._opacity_changed)
        self.collapse_button.clicked.connect(self.toggle_collapsed)
        self.hide_button.clicked.connect(lambda: self.set_visible(False))
        widget.installEventFilter(self)
        self._layout_chrome()

    def eventFilter(self, watched, event):
        if watched is self.widget and event.type() == C.QEvent.Resize:
            self._layout_chrome()
        elif watched is self.widget and event.type() == C.QEvent.LayoutRequest and self.collapsed:
            self._hide_collapsed_contents()
        return False

    def _contents(self):
        return [child for child in self.widget.findChildren(
            W.QWidget, options=C.Qt.FindDirectChildrenOnly)
            if child not in (self.title_bar, self.resize_grip)]

    def _hide_collapsed_contents(self):
        # Live input labels can be replaced while minimized. Keep only current
        # children, remembering new content before hiding it for the first time.
        remembered = {id(child): visible for child, visible in self._hidden_children}
        self._hidden_children = [(child, remembered.get(id(child), not child.isHidden()))
                                 for child in self._contents()]
        for child, _ in self._hidden_children:
            child.hide()

    def _layout_chrome(self):
        self.title_bar.setGeometry(0, 0, self.widget.width(), self.HEADER)
        self.title_bar.show()
        # Fit the grip inside the reserved right margin (+8): an 18px grip sat
        # over the seek rail's right end and turned rail presses into resizes.
        grip = 12
        self.resize_grip.setGeometry(max(0, self.widget.width() - grip),
                                     max(0, self.widget.height() - grip), grip, grip)
        self.resize_grip.setVisible(not self.collapsed)
        self.title_bar.raise_()
        self.resize_grip.raise_()

    def _opacity_changed(self, value):
        self.effect.setOpacity(value / 100)
        self.manager._states[self.name] = self.state()
        self.manager.changed()

    def state(self):
        return {'x': self.widget.x(), 'y': self.widget.y(), 'width': self.widget.width(),
                'height': self._expanded_height if self.collapsed else self.widget.height(),
                'visible': not self.widget.isHidden(), 'collapsed': self.collapsed,
                'opacity': self.effect.opacity()}

    def apply(self, update):
        state = {**self.manager._states.get(self.name, self.state()), **update}
        root = self.manager.root
        collapsed = state['collapsed']
        if collapsed != self.collapsed:
            if collapsed:
                self._hide_collapsed_contents()
            else:
                live_children = {id(child) for child in self._contents()}
                for child, was_visible in self._hidden_children:
                    if id(child) in live_children:
                        child.setVisible(was_visible)
                self._hidden_children.clear()
            self.collapsed = collapsed
        elif collapsed:
            self._hide_collapsed_contents()
        # Remeasure after revealing content. Hidden layout children contribute
        # no minimum, and live content can change while the panel is minimized.
        if not collapsed and self.widget.layout() is not None:
            self.widget.layout().activate()
            minimum = self.widget.layout().minimumSize()
            self.min_size = (max(self.requested_min_size[0], minimum.width()),
                             max(self.requested_min_size[1], minimum.height()))
        width = min(max(self.min_size[0], state['width']), max(1, root.width()))
        height = min(max(self.min_size[1], state['height']), max(1, root.height()))
        self._expanded_height = height
        actual_height = min(root.height(), self.HEADER) if collapsed else height
        x = min(max(0, state['x']), max(0, root.width() - width))
        y = min(max(0, state['y']), max(0, root.height() - actual_height))
        self.widget.setMinimumSize(min(width, self.min_size[0]),
                                   actual_height if collapsed else min(height, self.min_size[1]))
        self.widget.setMaximumHeight(self.HEADER if collapsed else 16777215)
        self.widget.setGeometry(x, y, width, actual_height)
        self.collapse_button.setText('+' if collapsed else '−')
        self.collapse_button.setToolTip('Restore panel' if collapsed else 'Minimize panel')
        self.collapse_button.setAccessibleName(('Restore ' if collapsed else 'Minimize ') + self.title)
        value = round(state['opacity'] * 100)
        blocked = self.opacity_slider.blockSignals(True)
        self.opacity_slider.setValue(value)
        self.opacity_slider.blockSignals(blocked)
        self.effect.setOpacity(value / 100)
        self.widget.setVisible(state['visible'])
        self._layout_chrome()
        self.manager._states[self.name] = self.state()
        self.manager._sync_action(self.name)

    def toggle_collapsed(self):
        self.manager.set_state(self.name, {'collapsed': not self.collapsed})

    def set_visible(self, visible):
        self.manager.set_state(self.name, {'visible': visible})
        if visible:
            self.widget.raise_()
            self.manager.menu_button.raise_()


class PanelManager(C.QObject):
    """Attach controls to existing panels; on_change can debounce external saving.

    The host includes the root-level menu_button in any custom window mask.
    Attached menus live in panel chrome, with a root-level recovery fallback
    when that panel is hidden.
    """
    def __init__(self, root, on_change=None, *, menu_anchor=(8, 8)):
        super().__init__(root)
        self.root, self.on_change = root, on_change
        self.panels = {}
        self._states = {}
        self._defaults = {}
        self._actions = {}
        self._applying = False
        self._last_published = None
        self.menu_button = W.QToolButton(root)
        self.menu_button.setText('Panels')
        self.menu_button.setAccessibleName('Show or restore panels')
        self.menu_button.setPopupMode(W.QToolButton.InstantPopup)
        self.menu = W.QMenu(self.menu_button)
        self.menu_button.setMenu(self.menu)
        self.set_menu_anchor(menu_anchor)
        self.menu_button.show()
        root.installEventFilter(self)

    def set_menu_anchor(self, anchor):
        if (not isinstance(anchor, (tuple, list)) or len(anchor) != 2
                or any(type(n) is not int or not 0 <= n <= 100000 for n in anchor)):
            raise ValueError('menu_anchor must contain two bounded nonnegative integers')
        self.menu_anchor=tuple(anchor)
        self._place_menu_button()

    def _place_menu_button(self):
        name=getattr(self,'_menu_panel',None)
        panel=self.panels.get(name) if name is not None else None
        if panel is not None and not panel.widget.isHidden():
            if self.menu_button.parentWidget() is not panel.title_bar:
                self.menu_button.setParent(panel.title_bar)
                panel.title_bar.layout().insertWidget(1,self.menu_button)
            self.menu_button.setFixedHeight(22)
            self.menu_button.show()
            return
        if self.menu_button.parentWidget() is not self.root:
            parent=self.menu_button.parentWidget()
            if parent is not None and parent.layout() is not None:parent.layout().removeWidget(self.menu_button)
            self.menu_button.setParent(self.root)
        width,height=min(76,max(1,self.root.width())),min(28,max(1,self.root.height()))
        x,y=((max(0,self.root.width()-width-8),max(0,self.root.height()-height-8))
             if name is not None else self.menu_anchor)
        self.menu_button.setFixedHeight(height)
        self.menu_button.setGeometry(min(x,max(0,self.root.width()-width)),
                                     min(y,max(0,self.root.height()-height)),width,height)
        self.menu_button.show()
        self.menu_button.raise_()

    def attach_menu_to_panel(self,name):
        if name not in self.panels:raise ValueError('unknown menu panel')
        self._menu_panel=name
        self._place_menu_button()

    def register(self, name, widget, title=None, min_size=(160, 72)):
        validate_panel_states({name: {}})
        if name in self.panels:
            raise ValueError('panel is already registered: ' + name)
        if widget.parentWidget() is not self.root:
            raise ValueError('managed panel must be a direct child of the UI root')
        if widget.graphicsEffect() is not None:
            raise ValueError('managed panel already has a graphics effect')
        if len(min_size) != 2 or any(type(n) is not int or n < 1 for n in min_size):
            raise ValueError('min_size must contain two positive integers')
        panel = PanelController(self, name, widget, title or name.replace('_', ' ').title(), min_size)
        self.panels[name] = panel
        action = self.menu.addAction(panel.title)
        action.setCheckable(True)
        action.triggered.connect(lambda visible, key=name: self.panels[key].set_visible(visible))
        self._actions[name] = action
        restore = self.menu.addAction('Restore ' + panel.title)
        restore.triggered.connect(lambda checked=False, key=name: self.restore_panel(key))
        panel.apply({})
        self._defaults[name] = dict(self._states[name])
        self.menu_button.raise_()
        return panel

    def panel(self, name):
        return self.panels[name]

    def _sync_action(self, name):
        if name in self._actions:
            self._actions[name].setChecked(not self.panels[name].widget.isHidden())

    def get_states(self):
        return {name: dict(state) for name, state in self._states.items()}

    def set_state(self, name, state):
        self.set_states({name: state})

    def set_states(self, states):
        validated = validate_panel_states(states)
        unknown = validated.keys() - self.panels.keys()
        if unknown:
            raise ValueError('unknown panels: ' + ', '.join(sorted(unknown)))
        self._applying = True
        try:
            for name, state in validated.items():
                self.panels[name].apply(state)
        finally:
            self._applying = False
        self.changed()

    def restore_panel(self, name):
        self.set_state(name, {'visible': True, 'collapsed': False, 'opacity': 1.0})
        self.panels[name].widget.raise_()
        self.menu_button.raise_()

    def changed(self):
        self._place_menu_button()
        self.menu_button.raise_()
        states = self.get_states()
        if not self._applying and states != self._last_published:
            self._last_published = states
            if self.on_change is not None:
                self.on_change(self.get_states())

    def reapply(self, *, adopt_geometry=False):
        """Reflow untouched preset panels; keep custom geometry and other state."""
        keys=('x','y','width','height');automatic=[]
        if adopt_geometry:
            for name,panel in self.panels.items():
                state=self._states[name];default=self._defaults.get(name,{})
                if not panel.collapsed and all(state[key]==default.get(key) for key in keys):
                    geometry=panel.widget.geometry()
                    values=dict(zip(keys,(geometry.x(),geometry.y(),geometry.width(),geometry.height())))
                    state.update(values);automatic.append(name)
        self.reclamp()
        for name in automatic:
            self._defaults[name].update({key:self._states[name][key] for key in keys})

    def capture_defaults(self):
        """Adopt host geometry after an explicit layout preset change."""
        self.reset_defaults_from_widgets()

    def prepare_layout(self):
        """Release collapsed height constraints before the host lays out a preset."""
        self._applying = True
        try:
            for panel in self.panels.values():
                panel.apply({'collapsed': False, 'opacity': 1.0})
        finally:
            self._applying = False

    def reset_defaults_from_widgets(self, *, opacity=1.0):
        """Capture a preset after prepare_layout() and the host's default layout."""
        self._states = {name: panel.state() for name, panel in self.panels.items()}
        for name,state in self._states.items():
            state.update(collapsed=False, opacity=1.0 if name=='preview' else opacity)
        self.reclamp()
        self._defaults = self.get_states()

    def reset_layout(self):
        self.set_states(self._defaults)

    def reclamp(self):
        self._applying = True
        try:
            for panel in self.panels.values():
                panel.apply({})
        finally:
            self._applying = False
        self.changed()

    def eventFilter(self, watched, event):
        if watched is self.root and event.type() == C.QEvent.Resize:
            self._place_menu_button()
            if self.panels:self.reclamp()
        return False
