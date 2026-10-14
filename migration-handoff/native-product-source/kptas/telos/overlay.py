"""The live Telos workspace. The same visible Qt surface is captured into video.

Runs on the controller thread; buttons enqueue actions for the existing stopped
 gate. No game input, extra game owner, or simulated state is introduced.
"""
from collections import deque
import os
import sys
from pathlib import Path
import uuid
import time
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from kptas.config import unit_name
from .ui_theme import qss

if TYPE_CHECKING:
    from .workspace_session import WorkspaceSnapshot

# Palette, proportions and typography follow the deployed Telos prototype.
# Intent: a route author rehearses, inspects a tick, rewinds and changes inputs.
# Hierarchy: transport and current tick lead; the editor expands only when needed.
# Surfaces: void/panel/raised, thin borders; teal selects, amber marks saves.
# Type: shared 13px sans, monospace tick and score; shared 4px/8px spacing.
STYLE = qss('''
QWidget { color:$text; font-family:$font_sans; font-size:$font_body; }
QFrame#panel, QFrame[uiRole="transportPanel"], QFrame[uiRole="editorPanel"], QFrame[uiRole="inputPanel"] { background:$panel; border:1px solid $line; }
QLabel#brand { color:$active; font-weight:600; }
QLabel#muted { color:$muted; font-size:11px; }
QLabel#tick { font-family:$font_mono; font-weight:600; color:$active; }
QPushButton, QToolButton { background:$raise; border:1px solid $line; padding:$space_small $space_medium; min-height:$control_content; }
QPushButton:hover, QToolButton:hover { background:$raise_2; border-color:$focus; }
QPushButton:pressed, QToolButton:pressed { background:$selected; border-color:$active; }
QPushButton:focus, QToolButton:focus, QComboBox:focus { border-color:$focus; }
QSlider:focus {border:1px solid $focus;}
QSpinBox:focus {border:1px solid $focus;}
QPushButton#play { color:$active; border-color:$active; }
QPushButton:disabled, QPushButton#play:disabled { color:#596975; border-color:#202b32; background:#10171d; }
QLineEdit:disabled, QTableWidget:disabled, QListWidget:disabled { color:#596975; }
QPlainTextEdit, QLineEdit, QSpinBox, QTableWidget, QTableView, QListWidget { background:$void; border:1px solid $line; selection-background-color:#126659; padding:2px; }
QPlainTextEdit { font-family:$font_mono; font-size:11px; }
QComboBox { background:$void; border:1px solid $line; padding:2px; }
QComboBox QAbstractItemView { background:$panel; color:$text; selection-background-color:#126659; }
QTableCornerButton::section { background:#151e27; border:0; }
QCheckBox::indicator { width:12px; height:12px; background:$void; border:1px solid $line; }
QCheckBox::indicator:checked { background:$active; border-color:$active; }
QHeaderView::section { background:#151e27; color:$muted; border:0; padding:2px; }
QSlider::groove:horizontal { height:4px; background:$line; }
QSlider::sub-page:horizontal { background:$line; }
QSlider::handle:horizontal { width:8px; margin:-4px 0; background:$active; }
QScrollBar:horizontal {height:14px;background:#111b23;}
QScrollBar:vertical {width:14px;background:#111b23;}
QScrollBar::handle {background:#425763;min-width:28px;min-height:28px;}
QScrollBar::add-line, QScrollBar::sub-line {width:0;height:0;}
QScrollBar::add-page, QScrollBar::sub-page {background:none;}
QTableView#pianoRoll {background:$panel;alternate-background-color:#111b23;border:1px solid $line;padding:0;gridline-color:#263744;}
QWidget#workspaceEditor, QWidget#pianoEditor, QScrollArea#editorScroll {background:$panel;}
QFrame#panelControlsTitle {background:$chrome;border-bottom:1px solid $line;}
QFrame#panelControlsTitle QLabel {color:$text;font-size:$font_small;font-weight:600;}
QFrame#panelControlsTitle QToolButton {padding:2px 4px;min-height:12px;font-size:11px;}
QLabel[uiRole="keycap"] {background:#111c24;color:#647b89;border:1px solid #334651;border-radius:3px;font-size:11px;}
QLabel[uiRole="keycap"][held="true"] {background:#12594f;color:$text;border-color:$active;}
QSlider::sub-page:horizontal:disabled, QSlider::handle:horizontal:disabled { background:#596975; }
''')


# Owner messages stay unchanged on the wire and in logs (tests match them).
# The visible status line shows a plain reading instead.
PLAIN_ERRORS = (
    ('closed or poisoned', 'The game stopped responding, so Stasis stopped playback. Restart Stasis to continue.'),
    ('health is unknown', 'Stasis cannot tell whether the game is still running, so playback stopped. Try More > Reconnect. If it is greyed out, restart Stasis.'),
    ('ownership needs recovery', 'Stasis stopped after a failed rewind or save. Your recording is saved. Restart Stasis to continue.'),
)
RESTART_GUIDANCE = 'Close this window and run ./stasis play again. Your recording is saved.'


def plain_error(text):
    text = str(text or '')
    for needle, plain in PLAIN_ERRORS:
        if needle in text:
            return plain
    return text


@dataclass(frozen=True)
class RemotePresentation:
    """Read-only display data; never a supervisor, bundle or restore owner."""
    snapshot: "WorkspaceSnapshot | None" = None
    playing: bool = False
    pending: bool = False
    error: str = ''
    awaiting_ack: bool = False
    can_stop_run: bool = False
    stopping_run: bool = False


class Overlay:
    def __init__(self, control, args, *, presentation=None):
        from .ui_preferences import UIPreferences
        self.ui_preferences = UIPreferences()
        self.ui_config_path = getattr(args, "ui_config", None)
        self.ui_layout_override = getattr(args, "layout", None)
        self.ui_shortcuts = []
        self._preset_sizes = {}
        self._session_preferences_ready = False
        self._preference_error = ''
        self._reload_error = ''
        self._compact_operation_error = ''
        self._workspace_pixmap = None
        self._workspace_frame_tick = None
        self._owner_frame_metadata = {}
        self.presentation = presentation
        self.remote_action = None
        display = getattr(args, 'display', None) if presentation is not None else control.cfg['game']['display']
        if display and display != 'auto':
            os.environ['DISPLAY'] = display
        if sys.platform == 'linux' and display and display != 'auto':
            os.environ.setdefault('QT_QPA_PLATFORM', 'xcb')
        from PySide6 import QtCore as C, QtGui as G, QtWidgets as W
        self.C,self.G,self.W=C,G,W
        self.app=W.QApplication.instance() or W.QApplication([])
        if self.app.style().objectName().lower()!='fusion':self.app.setStyle('Fusion')
        self.control,self.args=control,args
        self.preferences_applied=None
        self._metric_tick=None;self._metric_time=None
        self.actions=deque();self.expanded=False;self.busy=False
        self._state_controls=[];self._pending_operation=''
        self.score_path=None;self._branch_destination=None;self._key_signature=None
        self.preview=None;self._last_game_view=None
        self.root=W.QWidget(None,C.Qt.WindowType.FramelessWindowHint|C.Qt.WindowType.WindowStaysOnTopHint|C.Qt.WindowType.Tool)
        self.root.setObjectName('telosRoot')
        self.root.setWindowTitle('Stasis')
        # Status must stay outside the transport: compact density hides that
        # entire parent, including its ordinary hint label.
        self.compact_status = W.QToolButton(self.root)
        self.compact_status.setObjectName('compactSessionStatus')
        self.compact_status.setAccessibleName('Status and errors')
        self.compact_status.setStyleSheet(qss('background:$panel;color:$warning;border:1px solid $warning;padding:2px 6px;'))
        self.compact_status.clicked.connect(self._show_compact_status)
        self.compact_status.setCursor(C.Qt.CursorShape.PointingHandCursor)
        self.compact_status.hide()
        # Next to an error chip: Reconnect when the owner allows it, otherwise restart guidance.
        self.compact_action = W.QToolButton(self.root)
        self.compact_action.setObjectName('compactSessionAction')
        self.compact_action.setStyleSheet(qss('background:$panel;color:$text;border:1px solid $active;padding:2px 6px;'))
        self.compact_action.setCursor(C.Qt.CursorShape.PointingHandCursor)
        self.compact_action.clicked.connect(self._compact_action_clicked)
        self.compact_action.hide()
        self.root.setAttribute(C.Qt.WidgetAttribute.WA_TranslucentBackground)
        self.root.setAttribute(C.Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.root.setGeometry(0,0,800,600);self.root.setStyleSheet(STYLE)
        self.panel=W.QFrame(self.root);self.panel.setObjectName('panel');self.panel.setGeometry(144,8,644,132)
        layout=W.QVBoxLayout(self.panel);layout.setContentsMargins(4,4,4,4);layout.setSpacing(4)
        self.panel.setStyleSheet('QPushButton,QToolButton {padding:2px 6px;min-height:16px;}')
        self.hud=W.QFrame(self.root);self.hud.setObjectName('panel')
        hud_layout=W.QHBoxLayout(self.hud);hud_layout.setContentsMargins(6,0,6,0);hud_layout.setSpacing(8)
        self.hud_brand=W.QLabel('Stasis');self.hud_brand.setToolTip('Stasis by Kolto Labs');self.hud_brand.setStyleSheet('color:#39A9DB;font-weight:600;')
        hud_layout.addWidget(self.hud_brand)
        self.hud_metrics=W.QLabel('Waiting for the game');hud_layout.addWidget(self.hud_metrics)
        self.hud_metrics.setMinimumWidth(0)
        self.hud_metrics.setSizePolicy(W.QSizePolicy.Ignored,W.QSizePolicy.Preferred)
        self.hud_metrics.setAccessibleName('Playback progress')
        self.hud_toggle=W.QToolButton();self.hud_toggle.setText('▾');self.hud_toggle.setToolTip('Show timeline')
        self.hud_toggle.setStyleSheet('padding:0 4px;min-height:0;max-height:18px;')
        self.hud_toggle.setFixedHeight(18)
        self.hud_toggle.clicked.connect(lambda:self.set_density('timeline'));hud_layout.addWidget(self.hud_toggle)
        self.hud.setFixedHeight(24)
        self.metric_timer=C.QTimer(self.root);self.metric_timer.setInterval(250)
        self.metric_timer.timeout.connect(self.refresh_hud);self.metric_timer.start()
        self.transport_layout=layout
        class TransportLabel(W.QLabel):
            # Keep the painted counter/status inside its own layout item even
            # when a narrow panel leaves no room for the expanding spacer.
            def minimumSizeHint(label):
                hint=label.sizeHint()
                hint.setWidth(max(hint.width(),label.fontMetrics().horizontalAdvance(label.text())+12))
                return hint
        head=W.QHBoxLayout();self.head_layout=head;layout.addLayout(head)
        self.brand=W.QLabel('Stasis');self.brand.setObjectName('brand');head.addWidget(self.brand)
        self.state=TransportLabel('Paused');self.state.setObjectName('muted');head.addWidget(self.state)
        self.origin=W.QLabel();self.origin.setObjectName('muted');self.origin.setMaximumWidth(170);head.addWidget(self.origin)
        head.addStretch();self.tick=TransportLabel();self.tick.setObjectName('tick');head.addWidget(self.tick)
        self.expand=self.button('Edit inputs ▾',self.toggle,head,state_change=False)
        row=W.QHBoxLayout();self.toolbar_layout=row;layout.addLayout(row)
        self.play=self.button('▶ Play',lambda:self.queue('play_pause'),row);self.play.setObjectName('play')
        self.rewind=self.button(f'{self._unit(title=True)} back',lambda:self.queue('rewind'),row)
        self.step=self.button(f'{self._unit(title=True)} advance',lambda:self.queue('step'),row)
        self.rewind_mark=self.button('↶ Mark',lambda:self.queue('rewind_mark'),row)
        self.branch=self.button('Branch here',lambda:self.queue('branch'),row)
        self.branch.setEnabled(presentation is None and hasattr(control,'sup'))
        self.open_button=self.button('Open',self.load_bundle,row)
        self.save_button=self.button('Save inputs',lambda:self.queue('save'),row)
        # Secondary operations remain available without crowding playback.
        self.more=W.QPushButton('More');self.more.setObjectName('moreActions')
        self.more_menu=W.QMenu(self.more)
        self.more_menu.setToolTipsVisible(True)  # Disabled items explain themselves.
        for widget,name,description in (
            (self.play,'Play or pause','Start or pause playback.'),
            (self.rewind,f'{self._unit(title=True)} back',f'Go back one {self._unit()}.'),
            (self.step,f'{self._unit(title=True)} advance',f'Advance one {self._unit()}.'),
            (self.expand,'Show or hide the input editor','The game position does not change.'),
            (self.more,'More actions','Editing, layout and recovery actions.'),
            (self.hud_toggle,'Show timeline','Show the collapsed seek rail.')):
            widget.setAccessibleName(name);widget.setAccessibleDescription(description)
        exact=W.QWidget();exact_layout=W.QHBoxLayout(exact);exact_layout.setContentsMargins(6,4,6,4)
        label=self.exact_label=W.QLabel(self._unit(title=True));exact_layout.addWidget(label)
        self.seek_input=W.QSpinBox();self.seek_input.setRange(0,0);label.setBuddy(self.seek_input)
        self.seek_input.setAccessibleName(f'Exact seek {self._unit()}')
        self.seek_input.setAccessibleDescription(f'{self._unit(title=True)} to request within the currently admitted seek range.')
        exact_layout.addWidget(self.seek_input)
        self.seek_go=W.QPushButton('Go');self.seek_go.setAccessibleName(f'Seek to exact {self._unit()}')
        self.seek_go.clicked.connect(self.seek_exact_tick);exact_layout.addWidget(self.seek_go)
        exact_action=W.QWidgetAction(self.more_menu);exact_action.setDefaultWidget(exact);self.more_menu.addAction(exact_action)
        self.more_menu.aboutToShow.connect(lambda:self.sync_seek_input(initialize=True))
        self.ui_switch_action=self.more_menu.addAction('Switch layout',self.switch_layout)
        self.ui_reload_action=self.more_menu.addAction('Reload layout file',self.reload_ui_preferences)
        overflow=W.QWidget();overflow_layout=W.QVBoxLayout(overflow);overflow_layout.setContentsMargins(5,5,5,5)
        self.more_contents=overflow_layout
        for button in (self.rewind_mark,self.branch,self.open_button):
            row.removeWidget(button);overflow_layout.addWidget(button)
        action=W.QWidgetAction(self.more_menu);action.setDefaultWidget(overflow);self.more_menu.addAction(action)
        self.reconnect_action=self.more_menu.addAction('Reconnect')
        self.reconnect_action.setToolTip('Ask the game what happened to the last command, then continue')
        self.reconnect_action.setEnabled(False)
        self.reconnect_action.triggered.connect(lambda:self.queue('reconcile'))
        # The chip's Reconnect/Restart button follows this action's enabled state.
        self.reconnect_action.changed.connect(lambda:hasattr(self,'_compact_status_signature') and self._refresh_compact_status())
        self.more.setMenu(self.more_menu);row.addWidget(self.more)
        row.addStretch()
        from .timeline_markers import InputSeekSlider as TickSlider
        self.slider=TickSlider(C.Qt.Orientation.Horizontal);self.slider.setFixedHeight(24);self.slider.setRange(0,0 if presentation is not None else control.bundle.last_tick)
        self.slider.seekRequested.connect(lambda tick:self.queue('seek',tick=tick));layout.addWidget(self.slider)
        self.seek_rail=W.QFrame(self.root);self.seek_rail.setObjectName('panel')
        self.seek_rail_effect=W.QGraphicsOpacityEffect(self.seek_rail)
        self.seek_rail_effect.setOpacity(.9)
        self.seek_rail.setGraphicsEffect(self.seek_rail_effect)
        self.rail_layout=W.QVBoxLayout(self.seek_rail);self.rail_layout.setContentsMargins(0,0,0,0);self.rail_layout.setSpacing(0)
        self.rail_expand=W.QToolButton(self.seek_rail)
        self.rail_expand.setObjectName('timelineExpand')
        self.rail_expand.setFixedSize(22,22)
        self.rail_expand.setStyleSheet('padding:0;min-height:0;')
        self.rail_expand.setFocusPolicy(C.Qt.StrongFocus)
        self.rail_expand.setAccessibleName('Show the input editor')
        self.rail_expand.setAccessibleDescription('Open the piano roll without changing the game position.')
        self.rail_expand.setToolTip('Show the input editor')
        self.rail_expand.clicked.connect(lambda:self.set_density('editor'))
        self.rail_layout.addWidget(self.rail_expand)
        self.run_progress=W.QLabel(self.seek_rail)
        self.run_progress.setObjectName('muted')
        self.run_progress.setMinimumWidth(0)
        self.run_progress.setAccessibleName('Run progress')
        self.run_progress.hide()
        self.rail_layout.insertWidget(0,self.run_progress)
        # Compact timeline keeps the basic controls on the bar: Play, one
        # frame back, one frame forward and More. They mirror the full row.
        self.rail_controls=W.QWidget(self.seek_rail)
        self.rail_controls_layout=W.QBoxLayout(W.QBoxLayout.LeftToRight,self.rail_controls)
        self.rail_controls_layout.setContentsMargins(0,0,0,0);self.rail_controls_layout.setSpacing(2)
        self.rail_buttons={}
        for key,text,callback in (('play','▶ Play',self.play.click),
                                  ('rewind','◀ Back',self.rewind.click),
                                  ('step','Advance ▶',self.step.click)):
            rail_button=W.QToolButton(self.rail_controls);rail_button.setText(text)
            rail_button.setStyleSheet('padding:0 6px;min-height:0;');rail_button.setFixedHeight(22)
            rail_button.clicked.connect(callback);self.rail_controls_layout.addWidget(rail_button)
            self.rail_buttons[key]=rail_button
        self.rail_more=W.QToolButton(self.rail_controls);self.rail_more.setText('More')
        self.rail_more.setStyleSheet('padding:0 6px;min-height:0;');self.rail_more.setFixedHeight(22)
        self.rail_more.setPopupMode(W.QToolButton.ToolButtonPopupMode.InstantPopup)
        self.rail_more.setMenu(self.more_menu);self.rail_more.setAccessibleName('More actions')
        self.rail_controls_layout.addWidget(self.rail_more)
        # Small and quiet, one click away: never nested inside More.
        self.rail_about=W.QToolButton(self.rail_controls);self.rail_about.setText('About this tool')
        self.rail_about.setAutoRaise(True);self.rail_about.setFixedHeight(22)
        self.rail_about.setStyleSheet('padding:0 5px;min-height:0;font-size:10px;color:#9aa4b2;')
        self.rail_about.setToolTip('About this tool');self.rail_about.setAccessibleName('About this tool')
        self.rail_about.clicked.connect(self.show_about);self.rail_controls_layout.addWidget(self.rail_about)
        self.rail_layout.insertWidget(0,self.rail_controls)
        self.slider.expandRequested.connect(lambda:self.set_density('editor'))
        foot=W.QHBoxLayout();self.footer_layout=foot;layout.addLayout(foot)
        # Saving/input status precedes the seek strip; seek stays at the bottom.
        layout.removeItem(foot);layout.insertLayout(layout.indexOf(self.slider),foot)
        self.saved=W.QLabel('');self.saved.setObjectName('muted');foot.addWidget(self.saved)
        foot.addStretch();self.view_source=W.QLabel('');self.view_source.setObjectName('muted');foot.addWidget(self.view_source)
        self.hint=W.QLabel('');self.hint.setObjectName('muted');self.hint.setMaximumWidth(300);foot.addWidget(self.hint)
        self.hotkey_hint=''
        self.play.setToolTip('Play or pause')
        self.branch.setToolTip(f'Create a branch at this {self._unit()}')
        self.action_timer=C.QTimer(self.root);self.action_timer.setSingleShot(True)
        self.action_timer.timeout.connect(lambda:self.hint.setText(self.hotkey_hint))
        self.editor_panel=W.QFrame(self.root);self.editor_panel.setObjectName('panel');self.editor_panel.setGeometry(144,144,644,306)
        el=W.QVBoxLayout(self.editor_panel);el.setContentsMargins(3,3,3,3)
        # A single expanding editor: secondary tools are menu destinations,
        # never a row of competing navigation tabs.
        self.editor_pages=W.QStackedWidget();el.addWidget(self.editor_pages)
        self.editor_names=[];self.editor_actions={}
        self.roll=W.QTableWidget(0,4);self.roll.setHorizontalHeaderLabels([self._unit(title=True),'Input','Mark','State'])
        self.roll.horizontalHeader().setSectionResizeMode(W.QHeaderView.ResizeMode.Stretch)
        self.roll.verticalHeader().hide();self.roll.setEditTriggers(W.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.roll.cellDoubleClicked.connect(lambda r,c:self.queue('seek',tick=int(self.roll.item(r,0).text())))
        self.add_editor_page(self.roll,'Piano roll')
        score_page=W.QWidget();sl=W.QVBoxLayout(score_page);sl.setContentsMargins(6,6,6,6)
        self.score=W.QPlainTextEdit('' if presentation is not None else Path(args.branch_score).read_text() if args.branch_score else control.bundle.score)
        if args.branch_score:self.score_path=Path(args.branch_score)
        sl.addWidget(self.score)
        sr=W.QHBoxLayout();sl.addLayout(sr);self.button('Undo',self.score.undo,sr,state_change=False);self.button('Redo',self.score.redo,sr,state_change=False)
        sr.addStretch();self.load_score_button=self.button('Load score',self.load_score,sr);self.button('Save score',self.save_score_clicked,sr);self.button('Branch with edits',lambda:self.queue('branch'),sr)
        self.add_editor_page(score_page,'Score')
        # Live score control is its own More > Live score menu; this page is the text.
        self.editor_actions['Score'].setText('Score text')
        self.branches=W.QListWidget();self.branches.setToolTip('Double-click a branch to open it.');self.branches.itemDoubleClicked.connect(self.open_branch);self.add_editor_page(self.branches,'Branches')
        history_page=W.QWidget();hl=W.QVBoxLayout(history_page);hl.setContentsMargins(4,4,4,4)
        self.history=W.QListWidget();hl.addWidget(self.history)
        hr=W.QHBoxLayout();hl.addLayout(hr)
        self.button('Previous action',lambda:self.history_action(-1),hr,state_change=False)
        self.button('Next action',lambda:self.history_action(1),hr,state_change=False)
        self.history_seek=self.button(f'Go to selected {self._unit()}',self.seek_history,hr)
        self.history.itemDoubleClicked.connect(lambda _:self.seek_history())
        self.add_editor_page(history_page,'History')
        self.memory=W.QPlainTextEdit();self.memory.setReadOnly(True);self.add_editor_page(self.memory,'Journal')
        # Keep command entry and history together while allowing the timeline
        # and console to remain visible independently, with one command owner.
        self.console_panel=W.QFrame(self.root);self.console_panel.setObjectName('panel')
        self.console_panel.setGeometry(16,48,480,220)
        cl=W.QVBoxLayout(self.console_panel)
        self.console=W.QPlainTextEdit();self.console.setReadOnly(True);cl.addWidget(self.console)
        self.entry=W.QLineEdit();self.entry.setPlaceholderText(f'Enter a command, such as seek {self._unit().upper()}');cl.addWidget(self.entry)
        self.entry.returnPressed.connect(self.command)
        self.editor_actions['Console']=self.more_menu.addAction('Console',self.show_console_panel)
        self.console_panel.hide()
        self._state_controls.extend((self.slider,self.roll,self.branches,self.history,self.entry))
        self.editor_pages.currentChanged.connect(self.inspect_tab)
        self.badge=W.QLabel('TAS');self.badge.setStyleSheet('background:#0b1016;color:#19e3c1;padding:2px 6px;border:1px solid #33434e;font-weight:600;');self.badge.adjustSize();self.head_layout.insertWidget(1,self.badge)
        self.input_panel=W.QFrame(self.panel);self.input_panel.setObjectName('panel')
        # Keep seek/transport at the lower edge when the piano unfolds upward.
        self.transport_layout.insertWidget(0,self.input_panel,1)
        il=W.QVBoxLayout(self.input_panel);il.setContentsMargins(8,6,8,6);il.setSpacing(4)
        self.keyboard_panel=W.QWidget(self.input_panel)
        kl=W.QVBoxLayout(self.keyboard_panel);kl.setContentsMargins(0,0,0,0);kl.setSpacing(2)
        il.addWidget(self.keyboard_panel)
        self.input_title=W.QLabel('Inputs');self.input_title.setObjectName('muted');kl.addWidget(self.input_title)
        self.key_grid=W.QGridLayout();self.key_grid.setSpacing(3);self.key_grid.setAlignment(C.Qt.AlignmentFlag.AlignLeft);kl.addLayout(self.key_grid)
        self.input_keys={}
        self.live_keys=W.QLabel();self.live_keys.setObjectName('muted');kl.addWidget(self.live_keys)
        self.input_event=W.QLabel();self.input_event.setObjectName('muted');kl.addWidget(self.input_event)
        # The seek strip expands into the same input document, above its bottom
        # anchor. Playback and editor commands still share one remote owner.
        self.head_layout.removeWidget(self.expand)
        self.toolbar_layout.insertWidget(3,self.expand)
        self.current_input_summary=W.QLabel('Inputs unavailable');self.current_input_summary.setObjectName('muted')
        self.current_input_summary.setSizePolicy(W.QSizePolicy.Policy.Ignored,W.QSizePolicy.Policy.Preferred)
        self.current_input_summary.setMaximumWidth(240);self.footer_layout.insertWidget(1,self.current_input_summary)
        # One aligned transport/status row. Secondary controls stay in More.
        for widget in (self.badge,self.state,self.tick,self.origin):self.head_layout.removeWidget(widget)
        self.toolbar_layout.insertWidget(0,self.badge)
        self.toolbar_layout.insertWidget(4,self.tick)
        self.toolbar_layout.insertWidget(5,self.state)
        self.tick.setSizePolicy(W.QSizePolicy.Policy.Minimum,W.QSizePolicy.Policy.Preferred)
        self.state.setSizePolicy(W.QSizePolicy.Policy.Minimum,W.QSizePolicy.Policy.Preferred)
        self.toolbar_layout.takeAt(self.toolbar_layout.count()-1)
        self.toolbar_layout.insertStretch(6,1)
        self.footer_layout.insertWidget(0,self.origin)
        self.transport_layout.removeItem(self.head_layout)
        self.transport_layout.removeItem(self.footer_layout)
        self.footer_container=W.QWidget(self.panel)
        self.footer_layout.setParent(None)
        self.footer_container.setLayout(self.footer_layout)
        self.footer_container.hide()
        for index in range(self.footer_layout.count()):
            widget=self.footer_layout.itemAt(index).widget()
            if widget is not None:widget.hide()
        self.head_layout.setSpacing(8);self.toolbar_layout.setSpacing(8);self.footer_layout.setSpacing(8)
        self.more_menu.addAction('Show minimal bar',lambda:self.set_density('minimal'))
        self.more_menu.addAction('Show compact timeline',lambda:self.set_density('timeline'))
        self.keyboard_details=False
        self.keyboard_action=self.more_menu.addAction('Show keyboard details')
        self.keyboard_action.setCheckable(True)
        self.keyboard_action.toggled.connect(self.toggle_keyboard_details)
        self.keyboard_panel.hide()
        self.editor_scroll=W.QScrollArea(self.input_panel)
        self.editor_scroll.setObjectName('editorScroll')
        self.editor_scroll.setWidgetResizable(True)
        self.editor_scroll.setFrameShape(W.QFrame.Shape.NoFrame)
        self.editor_scroll.setWidget(self.editor_panel)
        il.addWidget(self.editor_scroll,1)
        self.editor_scroll.hide()
        self.game_canvas=W.QLabel('Frame unavailable',self.root)
        self.game_canvas.setObjectName('ownerFrame')
        self.game_canvas.setAlignment(C.Qt.AlignmentFlag.AlignCenter)
        self.game_canvas.setSizePolicy(W.QSizePolicy.Policy.Ignored, W.QSizePolicy.Policy.Ignored)
        self.game_canvas.setStyleSheet('background:#05070a;color:#91a5b2;')
        self.game_canvas.setAttribute(C.Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.game_canvas.setGeometry(self.root.rect());self.game_canvas.lower();self.game_canvas.hide()
        # Opaque backing only behind editing content keeps game HUD text from
        # bleeding through the grid, while the surrounding strip stays mild.
        self.editor_backplate=W.QFrame(self.root)
        self.editor_backplate.setStyleSheet('background:#0b1016;border:0;')
        self.editor_backplate.setAttribute(C.Qt.WidgetAttribute.WA_StyledBackground)
        self.editor_backplate.setAttribute(C.Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.editor_backplate_effect=W.QGraphicsOpacityEffect(self.editor_backplate)
        self.editor_backplate.setGraphicsEffect(self.editor_backplate_effect)
        self.editor_backplate.hide()
        self.workspace_preview=W.QLabel('Frame unavailable',self.root)
        self.workspace_preview.setObjectName('workspacePreview')
        self.workspace_preview.setAlignment(C.Qt.AlignmentFlag.AlignCenter)
        self.workspace_preview.setSizePolicy(W.QSizePolicy.Policy.Ignored, W.QSizePolicy.Policy.Ignored)
        self.workspace_preview.setStyleSheet('background:#05070a;border:1px solid #33434e;color:#91a5b2;')
        self.workspace_preview.hide()
        self.workspace_preview.setToolTip('Game')
        for widget,name in ((self.panel,'transportPanel'),(self.editor_panel,'editorPanel'),
                            (self.input_panel,'inputPanel'),(self.editor_pages,'workspaceEditor'),
                            (self.slider,'seekSlider'),(self.roll,'pianoRoll'),
                            (self.step,'stepButton'),(self.rewind,'rewindButton'),
                            (self.score,'scoreEditor'),(self.entry,'commandEntry'),
                            (self.console,'commandLog'),(self.badge,'tasBadge')):
            # Keep original #panel styling while exposing stable semantic selectors.
            widget.setProperty('uiRole',name)
            widget.setObjectName(name)
        self.root.setProperty('layoutPreset','overlay')
        class ResizeFilter(C.QObject):
            def eventFilter(observer,watched,event):
                if event.type()==C.QEvent.Type.Resize:
                    self._layout_surface(event.size().width(),event.size().height())
                return False
        self._resize_filter=ResizeFilter(self.root)
        self.root.installEventFilter(self._resize_filter)
        self.editor_panel.hide();self.input_panel.hide();self.root.show();self.root.raise_()
        if presentation is not None:
            self.score.setReadOnly(True)
            for button in score_page.findChildren(W.QPushButton):button.setEnabled(False)
            # A live owner plays a loaded file as a score from the current position.
            self.load_score_button.setEnabled(True)
            self.load_score_button.setToolTip('Play a score file from the current position')
        self.prepared_text=self.score.toPlainText();self._preserved_text=self.prepared_text;self.last_draft_path=None;self.refresh('Ready');self.mask()
        self.reload_ui_preferences()
        self._build_file_controls()
        if presentation is None:self.initialize_panel_controls()
        self._initialize_session_preferences()
        self._initialize_preference_reload()
        from .interaction_controls import InteractionControls
        self.interaction_controls = InteractionControls(self)
        self.interaction_controls.apply_preferences(self.ui_preferences)
        from .shortcut_binder import attach_overlay
        attach_overlay(self)  # Settings menu item and registry shortcuts; never raises.
        self._install_shortcut_list_action()
        self._install_about_action()
        tray=getattr(self.interaction_controls,'tray',None)
        if tray is not None:tray.setToolTip('Stasis by Kolto Labs')
        try:self._install_first_run_hint()
        except Exception as exc:self.console.appendPlainText(f'The first-run tip is unavailable: {exc}')
        try:  # Input trainer panel, menu item and hotkeys; optional, never fails the overlay.
            from .trainer_widget import install_trainer
            self.trainer_panel = install_trainer(self)
        except Exception as exc:
            self.trainer_panel = None
            self.console.appendPlainText(f'Input trainer is unavailable: {exc}')

    @staticmethod
    def stasis_version():
        """Version from the VERSION file next to the package, else 'development build'."""
        try:
            line=(Path(__file__).resolve().parents[3]/'VERSION').read_text(encoding='utf-8').splitlines()[0].split()
            if line and line[0]=='version' and len(line)>1:line=line[1:]
            if line and line[0][:1].isdigit():return line[0]
        except (OSError,IndexError,UnicodeError):pass
        return 'development build'

    def _install_about_action(self):
        """More > About Stasis… shows the version, publisher and trademark notice."""
        try:
            self.about_action=self.more_menu.addAction('About Stasis…')
            self.about_action.triggered.connect(self.show_about)
            from .actions import register_action
            register_action('show_about','About Stasis',category='Help',callback=self.show_about,
                            description='Show the version and the trademark notice.')
        except Exception as exc:
            self.console.appendPlainText(f'About Stasis is unavailable: {exc}')

    def show_about(self):
        W,C=self.W,self.C
        dialog=getattr(self,'_about_dialog',None)
        try:
            if dialog is not None and dialog.isVisible():
                dialog.raise_();dialog.activateWindow();return
        except RuntimeError:pass
        dialog=W.QDialog(self.root);dialog.setWindowTitle('About this tool');dialog.setMinimumWidth(420)
        dialog.setAttribute(C.Qt.WidgetAttribute.WA_DeleteOnClose,True)
        layout=W.QVBoxLayout(dialog);layout.setContentsMargins(16,16,16,12);layout.setSpacing(8)
        version=self.stasis_version()
        title=W.QLabel(f'Stasis {version}' if version[:1].isdigit() else f'Stasis ({version})')
        title.setObjectName('brand');layout.addWidget(title)
        layout.addWidget(W.QLabel('by Kolto Labs'))
        url='https://koltolabs.bocloud.workers.dev/'
        link=W.QLabel(f'<a href="{url}">{url}</a>');link.setTextFormat(C.Qt.TextFormat.RichText)
        link.setTextInteractionFlags(C.Qt.TextInteractionFlag.TextBrowserInteraction)
        link.setOpenExternalLinks(False)
        link.linkActivated.connect(lambda href:self.G.QDesktopServices.openUrl(C.QUrl(href)))
        layout.addWidget(link)
        story=W.QLabel('Stasis started as a quiet bet with myself: one game, a short window of days, and '
                       'the question of how close a frame-exact TAS could get before time ran out. Most of '
                       'it went into learning how KotOR keeps its own clock, and then politely asking it to '
                       'wait. It was a good few days.')
        story.setWordWrap(True);layout.addWidget(story)
        layout.addWidget(W.QLabel('Copyright © 2026 Kolto Labs. All rights reserved.'))
        notice=W.QLabel('Stasis is a fan-made tool. It is not affiliated with or endorsed by Lucasfilm, Disney, '
                        'BioWare, Aspyr, or EA. Star Wars and Knights of the Old Republic are trademarks of '
                        'their respective owners.')
        notice.setWordWrap(True);notice.setObjectName('muted');layout.addWidget(notice)
        buttons=W.QDialogButtonBox(W.QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dialog.close);layout.addWidget(buttons)
        dialog.setMinimumWidth(420)
        self._about_dialog=dialog
        dialog.show();dialog.raise_();dialog.activateWindow()

    def _install_shortcut_list_action(self):
        """More > Keyboard shortcuts… and F1 open Settings on the Shortcut list tab."""
        try:
            from .actions import register_action
            from .settings_dialog import open_settings
            show=lambda:open_settings(self,tab=3)
            register_action('show_shortcuts','Show the shortcut list',category='View',default='F1',
                            callback=show,description='Opens Settings on the Shortcut list. Works while Stasis has focus.')
            self.shortcuts_action=self.more_menu.addAction('Keyboard shortcuts…')
            self.shortcuts_action.triggered.connect(show)
            binder=getattr(self,'_shortcut_binder',None)
            if binder is not None:binder.show_in_menu('show_shortcuts',self.shortcuts_action,'Keyboard shortcuts…')
        except Exception as exc:
            self.console.appendPlainText(f'Keyboard shortcuts: the shortcut list is unavailable. {exc}')

    def _initialize_preference_reload(self):
        # The automatic last-session channel must never watch itself. Explicit
        # external designs are the only live sources; named presets stay saved.
        if self.ui_config_path is None:return
        source = Path(self.ui_config_path).expanduser().absolute()
        try:
            if source.resolve() == self._session_writer.path.resolve():return
        except (OSError,ValueError) as exc:
            self._reload_error = 'Stasis cannot watch the layout file for changes: '+str(exc)
            self._refresh_compact_status();return
        self._ui_watcher = self.C.QFileSystemWatcher(self.root)
        self._ui_reload_timer = self.C.QTimer(self.root)
        self._ui_reload_timer.setSingleShot(True)
        self._ui_reload_timer.setInterval(400)
        self._ui_reload_timer.timeout.connect(self._reload_external_preferences)
        self._ui_watcher.fileChanged.connect(lambda path:self._ui_reload_timer.start())
        self._ui_watcher.directoryChanged.connect(lambda path:self._ui_reload_timer.start())
        self._ui_dependency_signature = None
        self._refresh_preference_watches()
        self._ui_dependency_signature = self._preference_dependency_signature()
        self._ui_accepted_config_identity = (dict(self._ui_dependency_signature).get(str(source))
                                            if not self._reload_error else None)

    def _refresh_preference_watches(self):
        if not hasattr(self,'_ui_watcher'):return
        from .ui_preferences import preference_watch_paths
        paths = preference_watch_paths(self.ui_config_path,self.ui_preferences.theme_source)
        # If a new nested theme directory does not exist yet, watch its nearest
        # existing ancestor so creation/replacement can repair the live design.
        wanted = set()
        physical_paths = set(paths)
        for path in paths:
            try:physical_paths.add(path.resolve())
            except (OSError,ValueError):pass
        for path in physical_paths:
            try:
                if path.is_file():wanted.add(str(path))
                parent = path.parent
                for _ in range(32):
                    if parent.is_dir():
                        wanted.add(str(parent));break
                    if parent == parent.parent:break
                    parent = parent.parent
            except (OSError,ValueError):
                continue
        watched = set(self._ui_watcher.files()+self._ui_watcher.directories())
        removed = watched-wanted
        if removed:self._ui_watcher.removePaths(list(removed))
        if wanted-watched:
            failed = self._ui_watcher.addPaths(list(wanted-watched))
            if failed:
                self._reload_error = 'Stasis cannot watch these layout files for changes: '+', '.join(failed)
                self._refresh_compact_status()
        self._ui_dependency_paths = paths

    def _preference_dependency_signature(self):
        signature = []
        for path in sorted(self._ui_dependency_paths,key=str):
            try:
                info = path.stat()
                identity = (info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns)
            except (OSError,ValueError):identity = None
            signature.append((str(path),identity))
        return tuple(signature)

    def _reload_external_preferences(self):
        # QFileSystemWatcher drops a file watch after atomic replacement; the
        # parent directory survives and re-arms that file after every event.
        self._refresh_preference_watches()
        signature = self._preference_dependency_signature()
        if signature == self._ui_dependency_signature:return
        source = str(Path(self.ui_config_path).expanduser().absolute())
        # Compare against the last accepted config, not the last failed edit.
        # If a config changes layout and references a missing theme, creation
        # of that theme must apply the entire waiting config once it is valid.
        theme_only = (self._ui_accepted_config_identity is not None and
                      dict(signature).get(source) == self._ui_accepted_config_identity)
        self._ui_dependency_signature = signature
        self.reload_ui_preferences(preserve_layout=theme_only)
        self._refresh_preference_watches()
        # Do not acknowledge bytes changed during parsing/application as if
        # they were applied. A queued filesystem event will see this baseline,
        # and an explicit retry also covers events coalesced by the watcher.
        if self._preference_dependency_signature() != self._ui_dependency_signature:
            self._ui_reload_timer.start()

    def _initialize_session_preferences(self):
        from .ui_preferences import SessionPreferencesWriter, load_preferences, PreferenceError
        self._session_writer = SessionPreferencesWriter()
        if self.ui_config_path is None and self.ui_layout_override is None:
            try:
                candidate = load_preferences(self._session_writer.path)
                self.apply_ui_preferences(candidate)
                self.active_profile = candidate.selected_layout
                self.refresh_profiles(self.active_profile)
            except (PreferenceError,ValueError) as exc:
                if not isinstance(exc.__cause__, FileNotFoundError):
                    self._preference_error = 'Layout could not be restored: ' + str(exc)
        self._session_preferences_ready = True
        self._session_last_snapshot = None
        self._session_debounce = self.C.QTimer(self.root)
        self._session_debounce.setSingleShot(True)
        self._session_debounce.setInterval(750)
        self._session_debounce.timeout.connect(self._publish_session_preferences)
        # Observe presentation-only changes such as Follow whose callbacks are
        # owned by the client. This never polls the owner or touches game state.
        self._session_watch = self.C.QTimer(self.root)
        self._session_watch.setInterval(250)
        self._session_watch.timeout.connect(self._observe_session_preferences)
        self._session_watch.start()
        self.app.aboutToQuit.connect(self._finish_session_preferences)
        self._observe_session_preferences()

    def _session_preferences_snapshot(self):
        size = replace(self.ui_preferences.sizing,
                       window_width=max(640,min(3840,self.root.width())),
                       window_height=max(600 if self.ui_preferences.layout=='workspace' else 480,
                                         min(2160,self.root.height())))
        states = self.panel_manager.get_states() if hasattr(self,'panel_manager') else self.ui_preferences.panel_states
        interactions = getattr(self, 'interaction_controls', None)
        if interactions is not None and interactions.saved_panel_states is not None:
            states = interactions.saved_panel_states
        return replace(self.ui_preferences, sizing=size, panel_states=states,
                       window_position=(self.root.x(),self.root.y()),
                       selected_layout=getattr(self,'active_profile',None))

    def _observe_session_preferences(self):
        if not self._session_preferences_ready or getattr(self,'_applying_panels',False):return
        snapshot = self._session_preferences_snapshot()
        if snapshot != self._session_last_snapshot:
            self._session_last_snapshot = snapshot
            self._session_debounce.start()
        result = self._session_writer.take_result()
        if result is not None:
            _, error = result
            self._preference_error = ('Layout settings are not saved: ' + error) if error else ''
            if error:self.console.appendPlainText(self._preference_error)
        self._refresh_compact_status()

    def _publish_session_preferences(self):
        if self._session_preferences_ready:
            self._session_writer.submit(self._session_preferences_snapshot())

    def _finish_session_preferences(self):
        if not self._session_preferences_ready:return
        self._session_watch.stop();self._session_debounce.stop()
        self._publish_session_preferences()
        self._session_preferences_ready = False
        if not self._session_writer.finish():
            print('Stasis: the layout is still being saved. The previous layout is kept if this does not finish.',file=sys.stderr)
        result = self._session_writer.take_result()
        if result is not None and result[1]:print('Stasis: layout settings were not saved: '+result[1],file=sys.stderr)

    def _refresh_compact_status(self):
        presentation = self.presentation
        error = (getattr(presentation,'error','') if presentation is not None else '') or self._compact_operation_error
        snapshot = getattr(presentation,'snapshot',None) if presentation is not None else None
        if snapshot is not None and getattr(snapshot,'unknown_state',False):
            error = error or ('Stasis lost track of the last command, so playback stopped. '
                              'Choose More > Reconnect to check the game and continue.')
        if snapshot is not None and getattr(snapshot,'error',''):
            error = error or str(snapshot.error)
        error = plain_error(error)
        # Raw or already-plain (overlay.hint is rewritten each refresh): name the saved frame.
        if error == plain_error('ownership needs recovery') and getattr(snapshot,'durable_tick',None) is not None:
            error = (f'Stasis stopped after a failed rewind or save. Your recording is saved up to '
                     f'{self._unit()} {snapshot.durable_tick}. Restart Stasis to continue.')
        notice = getattr(self,'_notice',None)
        notice_error = bool(notice and notice[2])
        if notice is not None and not error and notice_error:error = notice[1]
        busy = bool(getattr(presentation,'pending',False) or getattr(presentation,'awaiting_ack',False) or self.busy)
        detail = '\n\n'.join(message for message in (error,self._reload_error,self._preference_error) if message)
        if not detail and notice is not None:detail = notice[1]
        if not detail and busy:detail = 'Waiting for the game to confirm.'
        session_error = bool(error) and not (notice_error and error == notice[1])
        # A session error names its reason inline (first sentence, elided to fit).
        reason = (error.split('. ')[0].rstrip('.')+'.') if session_error else ''
        if reason:
            room = max(160,self.root.width()-240)
            reason = self.compact_status.fontMetrics().elidedText(reason,self.C.Qt.TextElideMode.ElideRight,room)
        text = (notice[0] if notice_error and error == notice[1] else reason if error
                else 'Design unchanged' if self._reload_error else 'Layout unsaved' if self._preference_error
                else notice[0] if notice is not None else 'Working…')
        can_reconnect = bool(session_error and self.reconnect_action.isEnabled())
        action = 'Reconnect' if can_reconnect else 'Restart Stasis' if session_error else ''
        if action == 'Restart Stasis' and RESTART_GUIDANCE not in detail:detail += '\n\n'+RESTART_GUIDANCE
        signature = (text,detail,action)
        if signature == getattr(self,'_compact_status_signature',None):return
        self._compact_status_signature = signature
        self._compact_status_detail = detail
        self.compact_status.setText(text)
        self.compact_status.setToolTip((detail+'\n\nClick for details.') if detail else '')
        self.compact_status.setAccessibleDescription(detail)
        self.compact_status.setVisible(bool(detail));self.compact_status.adjustSize()
        self.compact_action.setText(action)
        self.compact_action.setToolTip('Ask the game what happened to the last command, then continue.'
                                       if can_reconnect else RESTART_GUIDANCE)
        self.compact_action.setAccessibleName(action or 'Session action')
        self.compact_action.setVisible(bool(action and detail));self.compact_action.adjustSize()
        self._position_compact_status();self.mask()

    def _compact_action_clicked(self):
        if self.compact_action.text() == 'Reconnect' and self.reconnect_action.isEnabled():
            self.reconnect_action.trigger()
            return
        dialog = self.W.QMessageBox(self.root)
        dialog.setWindowTitle('Restart Stasis')
        dialog.setTextFormat(self.C.Qt.TextFormat.PlainText)
        dialog.setText(RESTART_GUIDANCE)
        dialog.setAttribute(self.C.Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.setStandardButtons(self.W.QMessageBox.StandardButton.Close)
        self._status_dialog = dialog
        dialog.open()

    def set_notice(self,label,detail='',*,error=False,ms=6000,console=True):
        """Short-lived result of a local action (for example a draft save).

        Successes never read as a session error. A failed local action shows
        its own label and detail until it expires or the next notice replaces it.
        """
        self._notice=(label,detail or label,error)
        self._notice_serial=getattr(self,'_notice_serial',0)+1
        serial=self._notice_serial
        def expire():
            if getattr(self,'_notice_serial',None)==serial:
                self._notice=None;self._refresh_compact_status()
        self.C.QTimer.singleShot(ms,self.root,expire)
        if console and hasattr(self,'console'):self.console.appendPlainText(detail or label)
        self._refresh_compact_status()

    def _position_compact_status(self):
        self.compact_status.move(max(0,self.root.width()-self.compact_status.width()-8),8)
        self.compact_status.raise_()
        action=getattr(self,'compact_action',None)
        if action is not None:
            action.move(max(0,self.compact_status.x()-action.width()-4),8);action.raise_()
        hint=getattr(self,'first_run_hint',None)
        if hint is not None and not hint.isHidden():
            hint.adjustSize();hint.move(8,36 if self.hud.isVisible() else 8);hint.raise_()

    def _install_first_run_hint(self):
        """One line that names the show/hide key and F1 until the user closes it."""
        from .ui_settings import current_settings
        if current_settings().first_run_hint_dismissed:return
        W=self.W
        self.first_run_hint=W.QFrame(self.root);self.first_run_hint.setObjectName('panel')
        row=W.QHBoxLayout(self.first_run_hint);row.setContentsMargins(8,2,2,2);row.setSpacing(6)
        self.first_run_label=W.QLabel();row.addWidget(self.first_run_label)
        close=W.QToolButton(self.first_run_hint);close.setText('✕');close.setToolTip('Close this tip')
        close.setAccessibleName('Close this tip');close.setStyleSheet('padding:0 4px;min-height:0;')
        close.clicked.connect(self._dismiss_first_run_hint);row.addWidget(close)
        self._update_first_run_hint()
        self.first_run_hint.show();self._position_compact_status();self.mask()

    def _update_first_run_hint(self):
        label=getattr(self,'first_run_label',None)
        if label is None:return
        key=dict(self.ui_preferences.shortcuts).get('toggle_visibility','') or 'the tray icon'
        label.setText(f'{key} shows or hides Stasis. F1 lists all keys.')

    def _dismiss_first_run_hint(self):
        from .ui_settings import dismiss_first_run_hint
        self.first_run_hint.hide();self.mask()
        error=dismiss_first_run_hint()
        if error:self.console.appendPlainText('The tip will show again next time. It could not be saved: '+error)

    def _show_compact_status(self):
        message = getattr(self,'_compact_status_detail','') or self.compact_status.toolTip()
        if not message:return
        dialog = self.W.QMessageBox(self.root)
        dialog.setWindowTitle('Session status')
        dialog.setTextFormat(self.C.Qt.TextFormat.PlainText)
        dialog.setText(message)
        dialog.setAttribute(self.C.Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.setStandardButtons(self.W.QMessageBox.StandardButton.Close)
        self._status_dialog = dialog
        dialog.open()

    def add_editor_page(self,widget,name):
        index=self.editor_pages.addWidget(widget)
        self.editor_names.append(name)
        action=self.more_menu.addAction(name)
        action.triggered.connect(lambda checked=False,i=index:self.show_editor_page(i))
        self.editor_actions[name]=action

    def show_editor_page(self,index):
        if not self.expanded:self.toggle()
        self.editor_pages.setCurrentIndex(index)
        self.inspect_tab()

    def show_console_panel(self):
        if not self.ui_preferences.panels.console:return
        if hasattr(self,'panel_manager'):
            self.panel_manager.set_state('console',{'visible':True,'collapsed':False})
            self.console_panel.raise_()
        else:
            self.console_panel.show();self.console_panel.raise_()
        self.entry.setFocus()
        self.mask()

    def toggle_keyboard_details(self,visible):
        self.keyboard_details=visible
        self.keyboard_panel.setVisible(visible and self.ui_preferences.panels.inputs)
        self._layout_surface(self.root.width(),self.root.height())

    def _build_file_controls(self):
        from .ui_profiles import LayoutProfiles
        self.layout_profiles=LayoutProfiles();self.active_profile=None
        W=self.W
        row=W.QHBoxLayout();row.setSpacing(4)
        row.addWidget(W.QLabel('Layout'))
        self.layout_combo=W.QComboBox();self.layout_combo.setObjectName('layoutProfiles');self.layout_combo.setMinimumWidth(100)
        self.layout_combo.setAccessibleName('Saved UI layout')
        row.addWidget(self.layout_combo,1)
        self.profile_load=self.button('Load layout',lambda:self.profile_action('load'),row,False)
        self.profile_save=self.button('Save layout',lambda:self.profile_action('save'),row,False)
        self.profile_more=W.QPushButton('Manage');menu=W.QMenu(self.profile_more)
        for label,action in (('Save as…','save_as'),('Rename…','rename'),('Delete','delete')):
            menu.addAction(label,lambda checked=False,a=action:self.profile_action(a))
        menu.addSeparator()
        menu.addAction('Export shared presentation…',lambda:self.shared_profile_action('export'))
        menu.addAction('Import shared presentation…',lambda:self.shared_profile_action('import'))
        # Inside the More popup, setMenu() can place this submenu far away; anchor it under the button.
        self.profile_menu=menu
        self.profile_more.clicked.connect(lambda:menu.popup(self.profile_more.mapToGlobal(self.profile_more.rect().bottomLeft())))
        row.addWidget(self.profile_more)
        self.layout_combo.currentIndexChanged.connect(self._profile_selection_changed)
        self.profile_row=row
        recording=W.QHBoxLayout();recording.setSpacing(4);recording.addWidget(W.QLabel('Recording'))
        self.recording_combo=W.QComboBox();self.recording_combo.setObjectName('recordingLibrary');self.recording_combo.setAccessibleName('Holorecord recording');recording.addWidget(self.recording_combo,1)
        self.recording_browse=self.button('Browse…',self.browse_recording,recording,False)
        self.recording_load=self.button('Load recording',lambda:self.queue('load_recording',path=self.recording_combo.currentData()),recording)
        self.recording_save=self.button('Save recording',lambda:self.queue('save_recording'),recording)
        self.file_controls=W.QWidget()
        files=W.QVBoxLayout(self.file_controls);files.setContentsMargins(6,4,6,6)
        files.addLayout(recording);files.addLayout(row)
        self.file_controls.setMinimumWidth(460)
        self.file_action=W.QWidgetAction(self.more_menu)
        self.file_action.setDefaultWidget(self.file_controls);self.more_menu.addAction(self.file_action)
        if self.presentation is None:
            self.recording_load.clicked.disconnect();self.recording_load.clicked.connect(lambda:self.queue('load_bundle',path=self.recording_combo.currentData()) if self.recording_combo.currentData() else None)
            self.recording_save.clicked.disconnect();self.recording_save.clicked.connect(lambda:self.queue('save_recording'))
            self.refresh_local_recordings()
        self.refresh_profiles()
        self._layout_surface(self.root.width(),self.root.height())

    def refresh_profiles(self,selected=None):
        self.layout_combo.clear();self.layout_combo.addItem('Current layout',None)
        for name in self.layout_profiles.names():self.layout_combo.addItem(name,name)
        if selected:self.layout_combo.setCurrentIndex(self.layout_combo.findData(selected))
        self._profile_selection_changed()

    def _profile_selection_changed(self,*_):
        self.profile_load.setEnabled(bool(self.layout_combo.currentData()))
        self.profile_save.setEnabled(True)
        self.profile_save.setToolTip('Save current layout' if self.active_profile else 'Name and save current layout')

    def profile_action(self,action):
        try:
            name=self.layout_combo.currentData()
            if action=='load':
                if not name:return
                candidate=self.layout_profiles.load(name)
                self.apply_ui_preferences(candidate);self.active_profile=name
            elif action in ('save','save_as'):
                if action=='save':name=self.active_profile
                if action=='save_as' or not name:
                    name,ok=self.W.QInputDialog.getText(self.root,'Save layout','Layout name')
                    if not ok:return
                if not name:return
                self.layout_profiles.save(name,self.ui_preferences,panel_states=self.panel_manager.get_states(),overwrite=action=='save' and bool(self.active_profile))
                self.active_profile=name
            elif action=='rename':
                if not name:return
                new,ok=self.W.QInputDialog.getText(self.root,'Rename layout','New name',text=name)
                if not ok:return
                self.layout_profiles.rename(name,new)
                if self.active_profile==name:self.active_profile=new
                name=new
            elif action=='delete':
                if not name:return
                self.layout_profiles.delete(name)
                if self.active_profile==name:self.active_profile=None
                name=None
            self.refresh_profiles(name);self.hint.setText('Layout '+action.replace('_',' ')+' complete')
            self._preference_error = ''
        except Exception as exc:
            self.hint.setText(str(exc));self._preference_error = str(exc)
        self._refresh_compact_status()

    def shared_profile_action(self, action):
        import json
        from .shared_presentation import LIMIT, validate_profile, export_profile, apply_profile
        settings = self.C.QSettings('KPTAS', 'TelosSharedPresentation')
        try:
            if action == 'export':
                try:
                    previous = validate_profile(json.loads(settings.value('profile', '{}')))
                    hosts = previous['hostProfiles']
                except (ValueError, TypeError):
                    hosts = {}
                document = export_profile(self, hosts)
                path,_ = self.W.QFileDialog.getSaveFileName(self.root, 'Export shared presentation', 'telos-presentation.json', 'Presentation profiles (*.json)')
                if not path:return
                output = self.C.QSaveFile(path)
                if not output.open(self.C.QIODevice.OpenModeFlag.WriteOnly):raise OSError(output.errorString())
                data = (json.dumps(document,ensure_ascii=False,allow_nan=False,indent=2)+'\n').encode('utf-8')
                if output.write(data) != len(data):
                    output.cancelWriting();raise OSError(output.errorString())
                if not output.commit():raise OSError(output.errorString())
                self.hint.setText('Shared presentation exported')
            else:
                path,_ = self.W.QFileDialog.getOpenFileName(self.root, 'Import shared presentation', '', 'Presentation profiles (*.json)')
                if not path:return
                with open(path,'rb') as source:data = source.read(LIMIT+1)
                if len(data)>LIMIT:raise ValueError('Shared profiles must be 64 KiB or smaller')
                document = validate_profile(json.loads(data))
                apply_profile(self,document)
                settings.setValue('profile',json.dumps(document,ensure_ascii=False,allow_nan=False))
                self.hint.setText('Shared presentation imported; panel geometry retained')
        except (ValueError,TypeError,OSError,RuntimeError) as exc:
            self.hint.setText('Presentation profile: '+str(exc))

    def browse_recording(self):
        path=self.W.QFileDialog.getExistingDirectory(self.root,'Choose a recording folder (.holo)')
        if path:
            if self.presentation is not None:self.queue('add_recording',path=path)
            else:
                try:
                    from .recording_library import RecordingLibrary
                    entry=RecordingLibrary().add(path);self.refresh_local_recordings(entry.path)
                except Exception as exc:self.hint.setText(str(exc))

    def refresh_local_recordings(self,selected=None):
        from .recording_library import RecordingLibrary
        try:
            self.recording_combo.clear()
            for entry in RecordingLibrary().list(self.control.bundle.root):
                self.recording_combo.addItem(entry.title+(' · current' if entry.current else ''),entry.path)
            if selected:self.recording_combo.setCurrentIndex(self.recording_combo.findData(selected))
        except Exception as exc:self.hint.setText(str(exc))

    def initialize_panel_controls(self):
        from .panel_controls import PanelManager
        if hasattr(self,'panel_manager'):return
        self._applying_panels=True
        self.panel_manager=PanelManager(self.root,on_change=self._panels_changed,
                                        menu_anchor=(8,8) if self.ui_preferences.layout=='workspace' else (8,136))
        self.panel_manager.menu.addAction('Reset layout',self.panel_manager.reset_layout)
        for name,widget,title,minimum in (
            ('transport',self.panel,'Timeline',(240,96)),
            ('preview',self.workspace_preview,'Game',(240,160)),
            ('console',self.console_panel,'Console',(280,120))):
            self.panel_manager.register(name,widget,title=title,min_size=minimum)
        self.panel_manager.attach_menu_to_panel('transport')
        self._layout_surface(self.root.width(),self.root.height())
        self.panel_manager.reset_defaults_from_widgets(opacity=.9 if self.ui_preferences.layout=='overlay' else 1.0)
        self.panel_manager.set_states(self._merged_panel_states(self.ui_preferences.panel_states))
        self._applying_panels=False
        self._panels_changed(self.panel_manager.get_states())

    def _panels_changed(self,states):
        if getattr(self,'_applying_panels',False):return
        if self.ui_preferences.density!='editor':
            self.panel.hide();self.panel_manager.menu_button.hide()
        self.expand.setText('Close editor ▴' if self.expanded else 'Edit inputs ▾')
        self.ui_preferences=replace(self.ui_preferences,panel_states=states,
                                   panels=replace(self.ui_preferences.panels,editor=self.expanded))
        self._sync_editor_backplate();self.mask();self._render_workspace_frame()

    def _merged_panel_states(self,states):
        # Old profiles retain their expansion choice, but never create a
        # second floating editor or apply its obsolete root coordinates.
        states=dict(states)
        # Inputs now belong to the timeline strip; obsolete sidebar geometry
        # must never recreate a second editor.
        states.pop('inputs',None)
        editor=states.pop('editor',None)
        if editor is not None and 'visible' in editor:
            self.expanded=editor['visible'] and not editor.get('collapsed',False)
        self.editor_panel.setVisible(self.expanded)
        self.editor_scroll.setVisible(self.expanded)
        self.expand.setText('Close editor ▴' if self.expanded else 'Edit inputs ▾')
        self.input_panel.setVisible(self.expanded)
        if not self.ui_preferences.panels.console:
            states['console']={**states.get('console',{}),'visible':False}
        return states

    def _finish_panel_layout(self):
        if hasattr(self,'panel_manager') and not getattr(self,'_applying_panels',False):
            self.panel_manager.reapply(adopt_geometry=self.ui_preferences.layout=='workspace')
        if self.ui_preferences.density!='editor':
            self.panel.hide()
            if hasattr(self,'panel_manager'):self.panel_manager.menu_button.hide()
        self.hud.setVisible(self.ui_preferences.density=='minimal')
        self.hud.raise_()
        self._layout_seek_rail()
        self._sync_editor_backplate()
        self.mask()

    def _layout_seek_rail(self):
        if not hasattr(self,'seek_rail'):return
        rail=self.ui_preferences.density=='timeline'
        self.run_progress.setVisible(rail and self.ui_preferences.timeline_edge!='left'
                                     and bool(getattr(self,'_run_progress_text','')))
        self.seek_rail.setVisible(rail)
        manager=getattr(self,'panel_manager',None)
        transport=manager.panels.get('transport') if manager is not None else None
        if transport is not None:self.seek_rail_effect.setOpacity(transport.effect.opacity())
        if rail:
            if self.slider.parentWidget() is not self.seek_rail:
                self.transport_layout.removeWidget(self.slider);self.slider.setParent(self.seek_rail);self.rail_layout.insertWidget(1,self.slider,1)
            left=self.ui_preferences.timeline_edge=='left'
            self.rail_layout.setDirection(self.W.QBoxLayout.TopToBottom if left else self.W.QBoxLayout.LeftToRight)
            # A 24 px left rail has no room for labelled buttons; More stays in the editor row.
            self.rail_controls.setVisible(not left)
            self._sync_rail_controls()
            self.rail_expand.setArrowType(self.C.Qt.RightArrow if left else self.C.Qt.UpArrow)
            self.slider.setMinimumSize(0,0);self.slider.setMaximumSize(16777215,16777215)
            self.slider.setOrientation(self.C.Qt.Orientation.Vertical if left else self.C.Qt.Orientation.Horizontal)
            self.seek_rail.setGeometry(0,32,24,max(24,self.root.height()-40)) if left else self.seek_rail.setGeometry(8,self.root.height()-32,max(24,self.root.width()-16),24)
            self.slider.show();self.seek_rail.raise_()
        elif self.slider.parentWidget() is self.seek_rail:
            self.rail_layout.removeWidget(self.slider);self.slider.setParent(self.panel);self.transport_layout.addWidget(self.slider)
            self.slider.setOrientation(self.C.Qt.Orientation.Horizontal);self.slider.setFixedHeight(24);self.slider.show()

    def _sync_rail_controls(self):
        """Mirror the full row's Play/Back/Advance labels and enablement on the rail."""
        if not hasattr(self,'rail_buttons'):return
        unit=self._unit()
        for key,source,text,tip in (('play',self.play,None,'Play or pause'),
                                    ('rewind',self.rewind,'◀ Back',f'Go back one {unit}'),
                                    ('step',self.step,'Advance ▶',f'Advance one {unit}')):
            button=self.rail_buttons[key]
            label=source.text() if text is None else text
            if button.text()!=label:button.setText(label)
            button.setEnabled(source.isEnabled())
            button.setToolTip(source.toolTip() or tip)
            # Play also represents Resume, Stop and Confirm stop. Keep that
            # meaning on both presentations, including assistive technology.
            button.setAccessibleName(label if text is None else tip)
            if text is None:
                source.setAccessibleName(label)
                source.setAccessibleDescription(source.toolTip() or tip)
                button.setAccessibleDescription(source.accessibleDescription())

    def _sync_editor_backplate(self):
        if not hasattr(self,'editor_backplate'):return
        controller=getattr(self,'panel_manager',None)
        transport=controller.panels.get('transport') if controller is not None else None
        if transport is not None:
            self.editor_backplate_effect.setOpacity(transport.effect.opacity())
        visible=(self.expanded and not self.panel.isHidden() and not self.input_panel.isHidden()
                 and not (transport is not None and transport.collapsed))
        self.editor_backplate.setVisible(visible)
        if visible:
            self.transport_layout.activate()
            origin=self.input_panel.mapTo(self.root,self.C.QPoint(0,0))
            self.editor_backplate.setGeometry(self.C.QRect(origin,self.input_panel.size()))
            self.editor_backplate.stackUnder(self.panel)

    def _transport_height(self):
        # QScrollArea's own minimum hint does not include its editor content.
        # Without explicit allocation an expanded strip can expose only the
        # table header, even though the table requests visible piano rows.
        if self.expanded:
            if isinstance(self.roll,self.W.QTableView):
                header=self.roll.horizontalHeader().sizeHint().height()
                # The piano's horizontal bar is hidden (the seek rail drives it).
                scrollbar=(0 if self.roll.horizontalScrollBarPolicy()==self.C.Qt.ScrollBarPolicy.ScrollBarAlwaysOff
                           else self.roll.horizontalScrollBar().sizeHint().height())
                model=self.roll.model()
                lanes=model.rowCount() if model is not None else 0
                lane_height=min(180,
                                max(min(18,self.ui_preferences.sizing.row_height),lanes*min(18,self.ui_preferences.sizing.row_height)))
                self.roll.setMinimumHeight(lane_height+
                                           header+scrollbar+2*self.roll.frameWidth())
            page=self.editor_pages.currentWidget()
            page_layout=page.layout() if page is not None else None
            if page_layout is not None:page_layout.activate()
            content=max(0,page.minimumSizeHint().height() if page is not None else 0,
                        page.minimumHeight() if page is not None else 0,
                        page_layout.minimumSize().height() if page_layout is not None else 0)
            margins=self.editor_panel.layout().contentsMargins()
            editor_height=content+margins.top()+margins.bottom()
            self.editor_panel.setMinimumHeight(editor_height)
            transport_margins=self.transport_layout.contentsMargins()
            base=(self.head_layout.minimumSize().height()+self.toolbar_layout.minimumSize().height()+
                  self.slider.minimumHeight()+self.footer_layout.minimumSize().height()+
                  transport_margins.top()+transport_margins.bottom()+4*self.transport_layout.spacing())
            input_margins=self.input_panel.layout().contentsMargins()
            keyboard=(self.keyboard_panel.minimumSizeHint().height() if not self.keyboard_panel.isHidden() else 0)
            prefix=keyboard+input_margins.top()+input_margins.bottom()+self.input_panel.layout().spacing()
            available=max(60,self.root.height()-16-base-prefix)
            # Small windows retain a scrollable full editor rather than clipping
            # its content or pushing the bottom transport off-screen.
            self.editor_scroll.setMaximumHeight(16777215)
            self.editor_scroll.setMinimumHeight(min(editor_height+2*self.editor_scroll.frameWidth(),available))
        else:
            self.editor_scroll.setMaximumHeight(16777215)
            self.editor_scroll.setMinimumHeight(0)
            self.editor_panel.setMinimumHeight(0)
        self.transport_layout.activate()
        return max(80,self.transport_layout.minimumSize().height())

    def button(self,text,fn,layout,state_change=True):
        b=self.W.QPushButton(text);b.clicked.connect(fn);layout.addWidget(b)
        if state_change:self._state_controls.append(b)
        return b

    def explain_disabled(self,reason):
        """Give each disabled transport/file control the shared reason as its tooltip.

        Callers reset the normal tooltips on every refresh, so enabled controls
        keep theirs and this only overrides the ones the reason applies to."""
        if not reason:return
        for name in ('play','rewind','step','rewind_mark','branch','open_button',
                     'recording_load','save_button','recording_save'):
            widget=getattr(self,name,None)
            if widget is not None and not widget.isEnabled():widget.setToolTip(reason)

    def _set_busy(self,busy):
        if self.presentation is not None:
            self.busy=busy
            self._remote_capabilities()
            return
        self.busy=busy
        for widget in self._state_controls:widget.setEnabled(not busy)
        self.branch.setEnabled(not busy and hasattr(self.control,'sup'))
        if not busy:self._pending_operation=''

    def _busy_refusal(self):
        if not self.busy:return False
        message=f'Wait for {self._pending_operation.rstrip("….")} to finish'
        self.hint.setText(message);self.console.appendPlainText(message)
        return True

    def mask(self):
        interactions=getattr(self,'interaction_controls',None)
        if interactions is not None and not interactions.visible:
            if self.ui_preferences.layout=='workspace' or self.ui_preferences.game_surface=='owner_frame':
                self.root.setMask(self.G.QRegion(self.root.rect()))
            return
        if self.ui_preferences.layout=='workspace' or self.ui_preferences.game_surface=='owner_frame':
            self.root.setMask(self.G.QRegion(self.root.rect()))
            return
        region=self.G.QRegion()
        if self.hud.isVisible():region|=self.G.QRegion(self.hud.geometry())
        if self.seek_rail.isVisible():region|=self.G.QRegion(self.seek_rail.geometry())
        if self.compact_status.isVisible():region|=self.G.QRegion(self.compact_status.geometry())
        if self.compact_action.isVisible():region|=self.G.QRegion(self.compact_action.geometry())
        hint=getattr(self,'first_run_hint',None)
        if hint is not None and hint.isVisible():region|=self.G.QRegion(hint.geometry())
        for widget in (self.panel,self.workspace_preview,self.console_panel):
            if not widget.isHidden():region|=self.G.QRegion(widget.geometry())
        if hasattr(self,'panel_manager') and self.panel_manager.menu_button.parentWidget() is self.root:
            region|=self.G.QRegion(self.panel_manager.menu_button.geometry())
        trainer=getattr(self,'trainer_panel',None)
        if trainer is not None and not trainer.isHidden():region|=self.G.QRegion(trainer.geometry())
        self.root.setMask(region)

    def set_density(self,density):
        focus=self.app.focusWidget()
        restore_focus=focus is not None and focus.window() is self.root
        self.ui_preferences=replace(self.ui_preferences,density=density,
            panels=replace(self.ui_preferences.panels,editor=density=='editor'))
        self.expanded=density=='editor'
        if self.expanded:self.editor_pages.setCurrentIndex(0)
        for widget in (self.editor_panel,self.input_panel,self.editor_scroll):widget.setVisible(self.expanded)
        self.panel.setVisible(density=='editor');self.hud.setVisible(density=='minimal')
        self.expand.setText('Close editor ▴' if self.expanded else 'Edit inputs ▾')
        self._layout_seek_rail()
        if hasattr(self,'panel_manager'):
            height=self._transport_height()
            self.panel_manager.set_state('transport',{'visible':density=='editor','collapsed':False,
                'y':max(8,self.root.height()-height-8),'height':height})
        self._layout_seek_rail();self._layout_surface(self.root.width(),self.root.height());self.refresh_hud()
        # Density changes hide/reparent controls and can hide the focused
        # button/editor. Recover inside this window only, leaving
        # game, dialog and popup-menu focus with their existing owners.
        if restore_focus:
            target=focus if focus.isVisible() and focus.isEnabled() else (
                self.hud_toggle if density=='minimal' else
                self.slider if density=='timeline' else self.expand)
            if target.isVisible() and target.isEnabled():target.setFocus(self.C.Qt.OtherFocusReason)

    def sync_seek_input(self, *, initialize=False):
        self.seek_input.setRange(self.slider.minimum(),self.slider.maximum())
        # A refresh may arrive between editing the target and pressing Go.
        # Only a newly opened popup initializes it; range changes still clamp it.
        if initialize:self.seek_input.setValue(self.slider.value())
        self.seek_go.setEnabled(self.slider.seek_enabled)

    def seek_exact_tick(self):
        tick=self.seek_input.value()
        if self.slider.seek_enabled and self.slider.minimum()<=tick<=self.slider.maximum():
            self.queue('seek',tick=tick)

    def shortcut_tooltip(self,action,text):
        key=dict(self.ui_preferences.shortcuts).get(action,'')
        suffix=' · '+key if key else ' · Shortcut unbound'
        if key:
            sequence=self.G.QKeySequence.fromString(key,self.G.QKeySequence.SequenceFormat.PortableText)
            if self.timeline_scoped_shortcut(sequence):suffix+=' (when timeline focused)'
        return text+suffix

    def timeline_scoped_shortcut(self,sequence):
        key=sequence[0]
        modifiers=self.C.Qt.KeyboardModifier
        chord=key.keyboardModifiers() & (modifiers.ControlModifier|modifiers.AltModifier|modifiers.MetaModifier)
        function=self.C.Qt.Key.Key_F1<=key.key()<=self.C.Qt.Key.Key_F35
        return not chord and not function

    def _unit(self,plural=False,title=False):
        """Time-unit word for labels: frame unless the owner or config says clock.precision = "tick"."""
        p=getattr(self,'presentation',None);s=getattr(p,'snapshot',None) if p is not None else None
        precision=getattr(s,'clock_precision',None)
        if precision is None:
            cfg=getattr(getattr(self,'control',None),'cfg',None)
            precision=cfg.get('clock',{}).get('precision') if isinstance(cfg,dict) else None
        return unit_name(precision,plural,title)

    def apply_unit_wording(self):
        """Relabel the constructed controls once the owner reports its clock precision."""
        if not hasattr(self,'history_seek'):return
        U,u=self._unit(title=True),self._unit()
        self.rewind.setText(f'{U} back');self.rewind.setAccessibleName(f'{U} back')
        self.rewind.setAccessibleDescription(f'Go back one {u}.')
        self.step.setText(f'{U} advance');self.step.setAccessibleName(f'{U} advance')
        self.step.setAccessibleDescription(f'Advance one {u}.')
        self.exact_label.setText(U)
        if hasattr(self.slider,'position_unit'):self.slider.position_unit=U
        self.seek_input.setAccessibleName(f'Exact seek {u}')
        self.seek_go.setAccessibleName(f'Seek to exact {u}')
        # The overlay layout swaps the table for a model-based PianoView whose
        # model labels its own columns; only the plain table takes labels here.
        if hasattr(self.roll,'setHorizontalHeaderLabels'):self.roll.setHorizontalHeaderLabels([U,'Input','Mark','State'])
        self.history_seek.setText(f'Go to selected {u}')
        self.entry.setPlaceholderText(f'Enter a command, such as seek {u.upper()}')

    def update_shortcut_tooltips(self):
        for widget,action,text in (
            (self.play,'play_pause','Play or pause'),
            (self.rewind,'rewind',f'Go back one {self._unit()}. Stasis refuses if no restore point exists'),
            (self.step,'step',f'{self._unit(title=True)} advance'),
            (self.save_button,'save','Save current input drafts'),
            (self.expand,'toggle_editor','Show or hide the input editor'),
            (self.rail_expand,'toggle_editor','Show the input editor'),
            (self.ui_switch_action,'switch_layout','Switch between overlay and window layout'),
            (self.ui_reload_action,'reload_ui','Reload the layout file and your keys')):
            widget.setToolTip(self.shortcut_tooltip(action,text))
        self.slider.setAccessibleDescription(f'Authored inputs and recorded {self._unit(plural=True)}. Enter or Space opens the editor. Arrows, Home and End move between {self._unit(plural=True)}. Wheel seeking requires keyboard focus on this rail. '+self.shortcut_tooltip('toggle_editor','Editor shortcut'))

    def toggle(self):
        self.set_density('timeline' if self.expanded else 'editor')
        self.inspect_tab()

    def refresh_hud(self):
        if not hasattr(self,'hud_metrics'):return
        self._sync_rail_controls()
        p=self.presentation
        s=p.snapshot if p is not None else None
        run=getattr(s,'run',None)
        state=self.state.text() if hasattr(self,'state') else 'Paused'
        if s is not None:
            durable=str(s.durable_tick) if s.durable_tick is not None else '—'
            text=f'{self._unit(title=True)} {s.tick} · Saved {durable}'
            detail=f'The game confirmed {self._unit()} {s.tick}.\nThe recording is saved through {self._unit()} {durable}.'
        else:
            text='Waiting for the game' if p is not None else f'{self._unit(title=True)} {self._metric_tick if self._metric_tick is not None else "—"}'
            detail='Speed shows once a run starts.'
        self._run_progress_text=''
        if run is not None:
            phase={'queued':'Queued','running':'Running','paused':'Paused','completed':'Completed',
                   'cancelled':'Stopped' if getattr(run,'end_reason',None)=='stopped' else 'Cancelled',
                   'failed':'Failed'}.get(run.status,str(run.status).title())
            phase=phase if run.active else 'Last run '+phase.lower()
            rate=(f'{run.executed_frames/run.elapsed_seconds:.1f} {self._unit(plural=True)}/s avg'
                  if run.executed_frames and run.elapsed_seconds>0 else 'Rate —')
            score_name=getattr(run,'name',None)
            progress=f'{score_name+" · " if score_name else ""}{phase} {run.executed_frames:,}/{run.total_frames:,}'
            if not run.active:text+=' · '+state
            text+=f' · {progress} · {rate}'
            self._run_progress_text=f'{progress} · Saved {run.durable_tick} · {rate}'
            detail+=(f'\nRun: {run.run_id}\n{run.executed_frames:,} of {run.total_frames:,} {self._unit(plural=True)} done.'
                     f'\nThe game confirmed the run through {self._unit()} {run.acknowledged_tick}.'
                     f'\nThe run is saved through {self._unit()} {run.durable_tick}.'
                     f'\n{rate} over {run.elapsed_seconds:.2f} seconds.'
                     '\nThe average includes playing and saving.'
                     '\nProgress updates when the game confirms each step.')
            if run.error:detail+='\n'+run.error
            if score_name:detail+=f'\nScore: {score_name}'
            detail+=(f'\nScore {self._unit()} {run.executed_frames:,} of {run.total_frames:,}'
                     f' at game {self._unit()} {s.tick if s is not None else "—"}.')
        else:
            text+=' · '+state+' · Rate —'
            if s is not None and 'run_control' in s.capabilities:
                self._run_progress_text='No score loaded'
        history=getattr(s,'run_history',()) if s is not None else ()
        if history:
            from .score_control import history_lines
            detail+='\nScore starts in this recording:\n'+'\n'.join(history_lines(history,self._unit())[-12:])
        if p is not None and (p.error or p.awaiting_ack or (s is not None and (s.error or s.unknown_state))):
            text='Last confirmed · '+text
            if self._run_progress_text:self._run_progress_text='Last confirmed · '+self._run_progress_text
            detail+='\nWaiting for the game to confirm.'
        if self._metric_tick is not None:
            detail+=f'\nDisplayed image: {self._unit()} {self._metric_tick}.'
        if self._metric_time is not None:detail+=f'\nImage game time: {self._metric_time:.2f} seconds.'
        if run is not None:self.state.setToolTip(detail)
        # Bound both presentations to the current window. Full evidence stays
        # available to tooltips and assistive technology after visual elision.
        available=max(0,self.root.width()-16)
        reserved=self.hud_brand.sizeHint().width()+self.hud_toggle.sizeHint().width()+28
        width=max(0,available-reserved)
        full=text
        self.hud_metrics.setMaximumWidth(width)
        self.hud_metrics.setText(self.hud_metrics.fontMetrics().elidedText(full,self.C.Qt.ElideRight,width))
        self.hud_metrics.setToolTip(detail)
        self.hud_metrics.setAccessibleDescription(full+'\n'+detail)
        self.hud.resize(min(available,reserved+self.hud_metrics.fontMetrics().horizontalAdvance(full)),24)
        self.hud.move(8,8);self.hud.raise_()
        width=min(320,max(0,self.root.width()//2-40))
        self.run_progress.setMaximumWidth(width)
        self.run_progress.setText(self.run_progress.fontMetrics().elidedText(self._run_progress_text,self.C.Qt.ElideRight,width))
        self.run_progress.setToolTip(detail)
        self.run_progress.setAccessibleDescription(self._run_progress_text+'\n'+detail)
        self.run_progress.setVisible(bool(self._run_progress_text) and self.ui_preferences.density=='timeline'
                                     and self.ui_preferences.timeline_edge!='left')
        self.rail_expand.setToolTip('Show the input editor\n'+detail)

    def queue(self,action,**args):
        if self.presentation is not None:
            return self.remote_action(action, **args) if self.remote_action else False
        if self._busy_refusal():return False
        self.actions.append((action,args))
        return True

    def _restore_platform_mask(self):
        # A recreated xcb window can retain an old shape even when Qt's cached
        # mask is empty. Force a platform update after show, then use this preset.
        if not self.root.isVisible():return
        self.root.clearMask()
        self.mask()

    def has_ui_focus(self):
        """Let UI-local shortcuts own focused input; legacy polls still drain."""
        return self.root.isActiveWindow()

    def poll(self):
        self.app.processEvents()
        return self.actions.popleft() if self.actions else ('',{})

    def set_control(self,control):
        """Reuse the workspace after a successful controller-owned bundle change."""
        self.preserve_draft()
        self._last_game_view=None
        self.control=control;self.actions.clear();self._set_busy(False)
        self.score_path=None;self._branch_destination=None;self._key_signature=None
        self.score.setPlainText(control.bundle.score)
        self._preserved_text=self.score.toPlainText()
        self.history.clear();self.branches.clear()
        self.slider.setRange(0,max(0,control.bundle.last_tick))
        if self.preview is not None:self.preview.hide()
        self.refresh()
        if hasattr(self,'recording_combo'):self.refresh_local_recordings()

    def input_metadata(self):
        """Selected record and separately sourced native input, suitable for event logs."""
        return dict(self._input_metadata)

    def history_action(self, direction):
        row=self.history.currentRow()+direction
        if 0<=row<self.history.count():self.history.setCurrentRow(row)

    def seek_history(self):
        item=self.history.currentItem()
        if item is not None:self.queue('seek',tick=item.data(self.C.Qt.ItemDataRole.UserRole))

    def load_bundle(self):
        if self.presentation is not None:
            self.queue('load_bundle')
            return
        if self._busy_refusal():return
        path=self.W.QFileDialog.getExistingDirectory(self.root,'Open recording',str(self.control.bundle.root.parent))
        if not path:return
        if not (Path(path)/'journal').is_file():
            self.saved.setText('Choose a recording folder')
            return
        self.queue('load_bundle',path=path)

    def open_branch(self,item):
        if self.presentation is not None:return
        path=item.data(self.C.Qt.ItemDataRole.UserRole)
        if path and Path(path).resolve()!=self.control.bundle.root.resolve():self.queue('load_bundle',path=path)

    def load_score(self):
        # A live owner plays the file as a score at the current frame; the
        # client controller has no local bundle or include configuration.
        if self.presentation is not None:return self.queue('score_load_here')
        if self._busy_refusal():return
        from kptas.score import ScoreError
        path,_=self.W.QFileDialog.getOpenFileName(self.root,'Load score',str(Path.home()),'Scores (*.score);;All files (*)')
        if not path:return
        try:
            self.load_score_path(path)
        except (OSError,ValueError,ScoreError) as exc:
            self.console.appendPlainText(f'Load failed: {exc}')

    def load_score_path(self,path):
        # Same include search as `score check`: the score's folder, configured
        # include_dirs, then bundled kptas/data; a missing @include lists them.
        from .score_control import compile_score_file
        compiled=compile_score_file(path,self.control.cfg['score']['include_dirs'])
        self.preserve_draft()
        self.score_path=Path(path)
        self.score.setPlainText(compiled.text())
        self._preserved_text=self.score.toPlainText()
        self.saved.setText('Score loaded · branch to apply edits')
        self.console.appendPlainText(f'Loaded score: {path}')

    def save_score_clicked(self):
        if self._busy_refusal():return
        from kptas.score import ScoreError
        try:self.save_score()
        except (OSError,ValueError,ScoreError) as exc:
            self.saved.setText('Score save failed')
            self.console.appendPlainText(f'Save failed: {exc}')

    def _score_includes(self):
        from .score_control import score_include_dirs
        dirs=score_include_dirs(self.control.cfg['score']['include_dirs'])
        if self.score_path is not None:dirs.insert(0,self.score_path.parent)
        return dirs

    def preserve_draft(self):
        """Save changed text before replacement, including text that cannot compile."""
        text=self.score.toPlainText()
        if text==self._preserved_text:return self.last_draft_path
        path=self.control.bundle.root.parent/('telos-draft-'+uuid.uuid4().hex+'.score')
        self._save_text(path,text)
        self._preserved_text=text;self.last_draft_path=str(path)
        self.saved.setText('Draft saved');self.console.appendPlainText(f'Draft saved: {path}')
        return str(path)

    def save_score(self, path=None):
        from kptas.score import compile_score,ScoreError
        text=self.score.toPlainText()
        try:compile_score(text,include_dirs=self._score_includes());is_draft=False
        except ScoreError:is_draft=True
        if path is None:
            suggested=self.score_path or (self.control.bundle.root.parent/'inputs.score')
            chosen,_=self.W.QFileDialog.getSaveFileName(self.root,'Save score',str(suggested),'Scores (*.score);;All files (*)')
            if not chosen:return None
            path=Path(chosen)
        else:path=Path(path)
        self._save_text(path,text)
        self.score_path=path
        self._preserved_text=text
        self.saved.setText('Draft saved' if is_draft else 'Score saved');self.console.appendPlainText(f'Saved score: {path}')
        return str(path)

    @staticmethod
    def _save_text(path,text):
        from kptas.platform_sync import require_directory_barrier,sync_dir
        require_directory_barrier()
        tmp=path.with_name(path.name+'.tmp-'+uuid.uuid4().hex)
        with tmp.open('x',encoding='utf-8') as f:f.write(text);f.flush();os.fsync(f.fileno())
        os.replace(tmp,path);sync_dir(path.parent)

    def branch_destination(self):
        configured=getattr(self.args,'branch_out',None)
        if configured and not Path(configured).exists():
            path=Path(configured)
        else:
            chosen,_=self.W.QFileDialog.getSaveFileName(self.root,'Create branch',
                str((Path(self.presentation.snapshot.take_path).parent if self.presentation is not None and self.presentation.snapshot is not None else self.control.bundle.root.parent)/'branch.holo'),'Recording (*.holo)')
            if not chosen:return None
            path=Path(chosen)
            if path.suffix!='.holo':path=path.with_name(path.name+'.holo')
        if path.exists():
            self.saved.setText('Choose a new branch name')
            return None
        self._branch_destination=path
        return str(path)

    def branch_score(self):
        from kptas.score import compile_score
        text=self.score.toPlainText()
        expanded=compile_score(text,include_dirs=self._score_includes()).text()
        directory=self._branch_destination.parent if self._branch_destination else self.control.bundle.root.parent
        path=directory/('.telos-inputs-'+uuid.uuid4().hex+'.score')
        self._save_text(path,expanded)
        return str(path)

    def command(self):
        if self.presentation is not None:
            text=self.entry.text().strip();self.entry.clear()
            self.console.appendPlainText('> '+text)
            self.queue('command', text=text)
            return
        if self._busy_refusal():return
        text=self.entry.text().strip();self.entry.clear();self.console.appendPlainText('> '+text)
        parts=text.split()
        if not parts:return
        cmd=parts[0]
        if cmd in ('play','pause'):
            if (cmd=='play') != self.control.playing:self.queue('play_pause')
        elif cmd=='seek' and len(parts)==2 and parts[1].isdigit():self.queue('seek',tick=int(parts[1]))
        elif cmd=='save':self.queue('save')
        elif cmd in ('step','rewind','rewind_mark','branch','stop'):self.queue(cmd)
        else:self.console.appendPlainText(f'Unknown command. Use play, pause, step, rewind, seek {self._unit().upper()}, branch or save.')

    def inspect_tab(self,*_):
        if self.presentation is not None:return
        if not self.expanded or self.busy:return
        c=self.control;b=c.bundle;tick=c.sup.tick
        name=self.editor_names[self.editor_pages.currentIndex()]
        if name=='Piano roll':
            from kptas.telos.input_view import recorded_input, input_summary
            marks={m['tick']:m['name'] for m in b.marks if not m['name'].startswith('event:')}
            rows=list(range(max(0,tick-4),min(b.last_tick,tick+7)+1));self.roll.setRowCount(len(rows))
            for r,t in enumerate(rows):
                view=recorded_input(b,t)
                for col,value in enumerate((str(t),input_summary(view),marks.get(t,''),'Current' if t==tick else '')):
                    item=self.W.QTableWidgetItem(value)
                    if t==tick:item.setBackground(self.G.QColor('#12473f'))
                    self.roll.setItem(r,col,item)
        elif name=='Journal':
            try:
                from kptas.holo.verify import journal_of
                import json
                rec=b.record(tick);self.memory.setPlainText(json.dumps({'tick':tick,'journal':journal_of(rec),
                    'input_sample':getattr(c,'live_input',None),'image':getattr(c,'view_info',None)},indent=2))
            except Exception as exc:self.memory.setPlainText(str(exc))
        elif name=='Branches':
            from kptas.holo.bundle import read_meta
            self.branches.clear();seen=set()
            candidates=[(b.root,'Current')]
            if b.parent:candidates.append((b.parent.root,'Parent'))
            for owner in (b,b.parent):
                if owner is not None:
                    candidates.extend((Path(row['path']),'Branch') for row in read_meta(owner.root).get('branches',[]) if row.get('path'))
            for path,label in candidates:
                key=str(path.resolve())
                if key in seen or not (path/'journal').is_file():continue
                seen.add(key);item=self.W.QListWidgetItem(f'{path.name} · {label}')
                item.setData(self.C.Qt.ItemDataRole.UserRole,key);item.setToolTip(key);self.branches.addItem(item)

    def refresh(self,event='',**fields):
        if self.presentation is not None:
            self.refresh_remote(self.presentation)
            return
        if event in ('refused','error'):
            self._compact_operation_error = str(fields.get('why','Action refused' if event=='refused' else 'Action failed'))
            self._set_busy(False);self._last_game_view=None
            if self.preview is not None:self.preview.hide()
        elif self.busy:
            self.app.processEvents()
            return
        elif event:
            self._compact_operation_error = ''
        self._set_busy(False)
        c=self.control;t=c.sup.tick
        self.apply_unit_wording()
        self.tick.setText(f'{self._unit(title=True)} {t}')
        self.tick.setToolTip(f'Current recording {self._unit()}: {t}')
        origin_tick=getattr(c,'clip_origin_tick',None);origin_name=getattr(c,'clip_origin_name','')
        if type(origin_tick) is int and origin_name:
            context=f'{origin_name} · {t-origin_tick:+d}'
            self.origin.setText(self.origin.fontMetrics().elidedText(context,self.C.Qt.TextElideMode.ElideMiddle,170))
            self.origin.setToolTip(f'{origin_name}: {self._unit()} {origin_tick}; offset {t-origin_tick:+d}')
        else:self.origin.setText('');self.origin.setToolTip('')
        self.state.setText('Playing' if c.playing else 'Paused');self.play.setText('Ⅱ Pause' if c.playing else '▶ Play')
        self.slider.setMaximum(max(c.bundle.last_tick,t))
        if not self.slider.isSliderDown():self.slider.setValue(t)
        self.sync_seek_input()
        from kptas.telos.input_view import recorded_input,sampled_input,key_label
        view=recorded_input(c.bundle,t)
        live=getattr(c,'live_input',None);current=sampled_input(live,t)
        self._input_metadata={**view,'live_input':dict(live) if isinstance(live,dict) else None,
                              'highlight_source':'native' if current['available'] else 'unavailable',
                              'highlight_keys':current['keys'],'highlight_buttons':current['buttons']}
        held=tuple(dict.fromkeys(current['keys']+current['buttons'])) if current['available'] else ()
        self._render_input_keys(current['available'],held)
        self.input_title.setToolTip(f'Game keyboard and mouse state sampled at this {self._unit()}. Saved inputs are in the piano roll.')
        self.input_panel.setToolTip(f'Input source: native virtual devices at {self._unit()} {t}' if current['available'] else f'No native input sample for this {self._unit()}. Saved inputs are not substituted.')
        cursor=current['cursor']
        self.live_keys.setText('Mouse '+', '.join(map(str,cursor)) if cursor is not None else '')
        self.show_input_motion(self.live_keys.text())
        self.input_event.setText('Close on save is armed' if current['armed'] else '')
        self.input_event.setVisible(current['armed'])
        self._input_height=self._keycap_height+(18 if current['armed'] else 0)
        self._layout_surface(self.root.width(),self.root.height())
        view_info=getattr(c,'view_info',{}) or {};source=view_info.get('source','unavailable')
        self._input_metadata['view_info']=dict(view_info)
        if source=='recorded_preview':
            self.view_source.setText('Checkpoint image')
            self.view_source.setToolTip(f"Image saved at {self._unit()} {view_info.get('archive_tick',view_info.get('captured_tick','?'))}. The game's framebuffer has not been rebuilt by restore.")
            self.view_source.setStyleSheet('color:#ffc66d;')
        elif source in ('captured_back_buffer','restored_framebuffer'):
            self.view_source.setText('');self.view_source.setToolTip(f"Game image captured at {self._unit()} {view_info.get('captured_tick','?')}")
        else:
            self.view_source.setText('Image unavailable');self.view_source.setToolTip(f'The game has not supplied an image for this {self._unit()}.')
        tier=str(c.bundle.header.tier).upper()
        self.badge.setText('TAS' if tier=='TAS' else 'TAS · '+tier);self.badge.adjustSize()
        self._layout_surface(self.root.width(),self.root.height())
        if event:
            action_names={'step':f'Step one {self._unit()}','rewind':f'Restore previous {self._unit()}','rewind_mark':'Restore bookmark',
                          'seek':f'Restore selected {self._unit()}','branch':'Start branch','play':'Play recording','pause':'Pause playback'}
            if event in action_names:
                self.hint.setText(action_names[event]);self.action_timer.start(1500)
            text=f'{event} · {self._unit()} {t}'
            item=self.W.QListWidgetItem(text);item.setData(self.C.Qt.ItemDataRole.UserRole,t)
            self.history.addItem(item);self.history.setCurrentItem(item);self.console.appendPlainText(text)
            if event in ('rewind_mark','rewind','seek'):self.saved.setText(f'Restored {self._unit()} {t}')
            elif event=='branch':self.saved.setText('Branch created')
            elif event in ('refused','error'):self.saved.setText(fields.get('why','Action refused' if event=='refused' else 'Action failed'));self.state.setText('Stopped')
            elif event=='open':self.saved.setText(c.bundle.root.name)
        self.inspect_tab()
        self._refresh_compact_status()
        self.app.processEvents()

    @staticmethod
    def seek_extent(snapshot):
        if snapshot is None:return 0
        end=getattr(snapshot,'recorded_last',None)
        if (getattr(snapshot,'mode','live')=='archive' and 'seek_recorded' in snapshot.capabilities
                and type(end) is int and end>=snapshot.tick):return end
        return snapshot.tick

    def _remote_capabilities(self):
        p=self.presentation;s=p.snapshot
        running=bool(getattr(s,'run',None) and s.run.active)
        ready=s is not None and not running and not p.pending and not p.awaiting_ack and not s.unknown_state and not s.error
        authored_ready=ready and getattr(s,'engaged',None) is not False
        self.play.setEnabled(authored_ready or p.playing or p.can_stop_run)
        self.step.setEnabled(authored_ready)
        self.rewind.setEnabled(authored_ready and s.tick>0)
        self.slider.set_seek_enabled(authored_ready and self.seek_extent(s)>0)
        self.slider.setToolTip(f'Go to a recorded {self._unit()}. Stasis refuses if it cannot restore that point exactly.'
                               if s is not None and self.seek_extent(s)>s.tick else
                               f'Go back to an earlier {self._unit()}. Forward restore is unavailable in live play.')
        for widget in (self.branch,self.open_button,self.rewind_mark):
            widget.setEnabled(False)
            widget.setToolTip('Unavailable in live play')
        self.save_button.setEnabled(ready)
        self.update_shortcut_tooltips()
        self.sync_seek_input()
        self.entry.setEnabled(not running and not p.pending and not p.awaiting_ack)
        self.branches.setEnabled(False)

    def refresh_remote(self, presentation):
        """Render acknowledged owner data on the original overlay widgets."""
        self.presentation=presentation
        self.apply_unit_wording()
        if hasattr(self,'interaction_controls'):self.interaction_controls.refresh_engagement()
        # An operation is not a new captured frame. Keep the last genuine image
        # with its original provenance until a coherent replacement arrives.
        s=presentation.snapshot
        if s is not None:self.slider.set_rows(s.rows)
        if hasattr(self.slider,'set_splices'):self.slider.set_splices(getattr(s,'run_history',()) if s is not None else ())
        self.busy=presentation.pending
        self._remote_capabilities()
        # Play keeps one batch in flight; it is still Playing, not a busy command.
        self.state.setText('Playing' if presentation.playing else 'Working…' if presentation.pending else 'Paused')
        run=getattr(s,'run',None)
        if run is not None and run.active:
            self.state.setText('Run queued' if run.status=='queued' else 'Score paused' if run.status=='paused' else 'Running')
        if presentation.awaiting_ack:self.state.setText('Waiting…')
        if presentation.stopping_run:self.state.setText('Stopping…')
        if presentation.error:self.state.setText('Unavailable' if s is None else 'Error')
        self.state.setToolTip(plain_error(presentation.error) or ('Waiting for the game to confirm.'
                                                    if presentation.awaiting_ack else ''))
        self.play.setText('Stopping…' if presentation.stopping_run else
                          'Confirm stop' if presentation.can_stop_run and s is not None and s.unknown_state else
                          '▶ Resume score' if run is not None and run.status=='paused' else
                          '■ Stop run' if run is not None and run.active else
                          'Confirm stop' if presentation.can_stop_run else
                          'Ⅱ Pause' if presentation.playing else '▶ Play')
        if (run is not None and run.active) or presentation.can_stop_run or presentation.stopping_run:
            self.play.setToolTip(self.shortcut_tooltip('play_pause',
                f'Resume the score from {self._unit()} {s.tick}.' if run is not None and run.status=='paused' else
                f'Stop this run at the end of the current {self._unit()}.'))
        self.hint.setText(plain_error(presentation.error))
        self._compact_operation_error = ''
        self._refresh_compact_status()
        self.refresh_hud()
        self.hint.setVisible(self.ui_preferences.layout=='workspace' or bool(presentation.error))
        if s is None or s.unknown_state or s.error:
            self.clear_workspace_frame('not connected to the game' if s is None else
                                       'Stasis lost track of the last command; choose More > Reconnect' if s.unknown_state else str(s.error))
        if s is None:
            self.tick.setText(f'{self._unit(title=True)} —')
            self._input_metadata={'highlight_source':'unavailable','highlight_keys':[], 'highlight_buttons':[], 'live_input':None}
            return
        position_unit=self._unit(title=True)
        self.tick.setText(f'{position_unit} {s.tick}')
        self.tick.setToolTip(f'The game confirmed {position_unit.lower()} {s.tick}')
        self.seek_input.setAccessibleDescription(
            f'{position_unit} to request within the currently admitted seek range.')
        self.slider.setRange(0,self.seek_extent(s))
        if not self.slider.isSliderDown():self.slider.setValue(s.tick)
        self.sync_seek_input()
        self.origin.setText('')
        self.origin.setToolTip(s.take_path)
        self.saved.setText(f'Saved through {position_unit.lower()} {s.durable_tick}' if s.durable_tick is not None else '')
        self.view_source.setText('')
        self.view_source.setToolTip('The game remains visible beneath the transparent overlay.')
        score_identity=(s.take_path,s.score_sha256,s.score_text)
        if score_identity!=getattr(self,'_remote_score_identity',None):
            self.score.setPlainText(s.score_text)
            self._remote_score_identity=score_identity
        note=s.last_inputs
        available=note is not None and not s.unknown_state and not s.error
        keys=list(note.held) if available else []
        buttons=list(note.buttons) if available else []
        self._input_metadata={'tick':s.tick,'highlight_source':'owner_applied' if available else 'unavailable',
                              'highlight_keys':keys,'highlight_buttons':buttons,'live_input':None,
                              'view_info':dict(self._owner_frame_metadata) if self._workspace_pixmap is not None else {'surface':'unavailable'}}
        held=tuple(dict.fromkeys(keys+buttons))
        input_layout_changed=self._render_input_keys(available,held)
        self.input_title.setToolTip(f'Keyboard and mouse inputs the game applied at this {self._unit()}.')
        self.input_panel.setToolTip(f'Inputs the game applied at {self._unit()} {s.tick}')
        mouse=''
        if available:
            if note.mouse_abs is not None:mouse='Mouse '+', '.join(map(str,note.mouse_abs))
            elif note.mouse_rel is not None:mouse='Mouse Δ '+', '.join(map(str,note.mouse_rel))
            if note.wheel:mouse+=f' · Wheel {note.wheel:+d}'
        self.live_keys.setText(mouse)
        self.show_input_motion(mouse)
        self.input_event.setVisible(False)
        self._input_height=self._keycap_height
        if input_layout_changed:self._layout_surface(self.root.width(),self.root.height())

    def reload_ui_preferences(self, *, preserve_layout=False):
        from .ui_preferences import load_preferences, PreferenceError
        from .keymap import STORE
        STORE.reload()  # Pick up hand edits to keymap.toml; never raises.
        try:
            signature = None
            if hasattr(self,'_ui_watcher'):
                self._refresh_preference_watches()
                signature = self._preference_dependency_signature()
                source = str(Path(self.ui_config_path).expanduser().absolute())
                if preserve_layout:
                    preserve_layout = (self._ui_accepted_config_identity is not None and
                                       dict(signature).get(source) == self._ui_accepted_config_identity)
            candidate=load_preferences(self.ui_config_path,layout=self.ui_layout_override)
            if signature is not None and signature != self._preference_dependency_signature():
                self._ui_reload_timer.start()
                return False
            if self.presentation is None and self.ui_config_path is None:
                candidate=replace(candidate,game_surface='native')
            if preserve_layout:
                # Editing only the QSS must not reset a manually moved/collapsed
                # layout or remap the root to the config's initial dimensions.
                self._validate_theme(candidate.stylesheet)
                current=self.ui_preferences
                opaque=current.layout=='workspace' or current.game_surface=='owner_frame'
                self.root.setStyleSheet(STYLE+('\nQWidget#telosRoot { background:#202329; }' if opaque else '')+'\n'+candidate.stylesheet)
                self.ui_preferences=replace(current,stylesheet=candidate.stylesheet,
                                            theme_source=candidate.theme_source,source=candidate.source)
            else:self.apply_ui_preferences(candidate)
            self._reload_error = ''
            self._refresh_preference_watches()
            if hasattr(self,'_ui_watcher'):
                self._ui_dependency_signature = signature
                self._ui_accepted_config_identity = dict(signature).get(source)
                if signature != self._preference_dependency_signature():
                    self._ui_reload_timer.start()
            self._refresh_compact_status()
            return True
        except (PreferenceError,ValueError) as exc:
            self.hint.setText('UI preferences unchanged')
            self._reload_error = 'UI preferences unchanged: ' + str(exc)
            self._refresh_compact_status()
            self.hint.setToolTip(str(exc));self.console.appendPlainText('UI preferences: '+str(exc))
            return False

    def switch_layout(self):
        preset='workspace' if self.ui_preferences.layout=='overlay' else 'overlay'
        size=self._preset_sizes.get(preset)
        if size is None:
            size=self.ui_preferences.sizing
            size=replace(size,window_width=max(1024,size.window_width),window_height=max(768,size.window_height)) if preset=='workspace' else replace(size,window_width=800,window_height=600)
        self.apply_ui_preferences(replace(self.ui_preferences,layout=preset,sizing=size,panel_states={}))

    def _validate_theme(self,stylesheet):
        """Ask Qt to parse the candidate on a hidden probe before visible changes."""
        from .ui_preferences import PreferenceError
        messages=[]
        previous=self.C.qInstallMessageHandler(lambda kind,context,text:messages.append(text))
        probe=None
        try:
            probe=self.W.QFrame()
            probe.setStyleSheet(stylesheet);probe.ensurePolished()
        finally:
            self.C.qInstallMessageHandler(previous)
            if probe is not None:probe.deleteLater()
        errors=[m for m in messages if 'parse' in m.lower() or 'unknown property' in m.lower()]
        if errors:raise PreferenceError('invalid theme: '+errors[0])

    def apply_ui_preferences(self,candidate):
        from .panel_controls import validate_panel_states
        from .keymap import merge_layout_shortcuts
        candidate=merge_layout_shortcuts(candidate)  # keymap.toml overrides; never raises.
        validate_panel_states(candidate.panel_states)
        self._validate_theme(candidate.stylesheet)
        if set(candidate.panel_states)-{'transport','editor','inputs','preview','console'}:raise ValueError('Unknown panel in layout')
        prepared=[]
        callbacks={'play_pause':lambda:self.queue('play_pause'),'step':lambda:self.queue('step'),
                   'rewind':lambda:self.queue('rewind'),'save':lambda:self.queue('save'),
                   'apply':lambda:self.queue('apply_selected'),'toggle_editor':self.toggle,
                   'switch_layout':self.switch_layout,'reload_ui':self.reload_ui_preferences,
                   'toggle_engagement':lambda:self.interaction_controls.toggle_engagement(),
                   'toggle_visibility':lambda:self.interaction_controls.toggle_visibility()}
        from .interaction_controls import activate_shortcut
        for action,text in candidate.shortcuts:
            if not text or (action=='apply' and self.presentation is None):continue
            sequence=self.G.QKeySequence.fromString(text,self.G.QKeySequence.SequenceFormat.PortableText)
            scoped=self.timeline_scoped_shortcut(sequence)
            shortcut=self.G.QShortcut(sequence,self.slider if scoped else self.root)
            shortcut.setContext(self.C.Qt.ShortcutContext.WidgetShortcut if scoped else self.C.Qt.ShortcutContext.WindowShortcut)
            shortcut.setAutoRepeat(False);shortcut.setEnabled(False)
            shortcut.activated.connect(lambda action=action:activate_shortcut(self,action,callbacks[action]));prepared.append(shortcut)
        visible=self.root.isVisible()
        self.root.setUpdatesEnabled(False)
        interactions=getattr(self,'interaction_controls',None)
        try:
            if interactions is not None:interactions.begin_design_change()
            for shortcut in self.ui_shortcuts:shortcut.setEnabled(False);shortcut.deleteLater()
            self.ui_shortcuts=prepared
            self._applying_panels=True
            if hasattr(self,'panel_manager'):self.panel_manager.prepare_layout()
            self.ui_preferences=candidate
            self._preset_sizes[candidate.layout]=candidate.sizing
            compact=candidate.layout=='overlay'
            self.brand.hide();self.origin.setVisible(not compact)
            self.input_title.setVisible(not compact)
            self.saved.setVisible(not compact);self.hint.setVisible(not compact)
            self.expand.setText('Editor' if compact and not self.expanded else 'Close editor ▴' if self.expanded else 'Edit inputs ▾')
            self.clear_workspace_frame()
            if self.preview is not None:self.preview.hide()
            workspace=candidate.layout=='workspace'
            if hasattr(self,'panel_manager'):self.panel_manager.set_menu_anchor((8,8) if workspace else (8,136))
            opaque=workspace or candidate.game_surface=='owner_frame'
            flags=self.C.Qt.WindowType.Window if workspace else (self.C.Qt.WindowType.FramelessWindowHint|
                self.C.Qt.WindowType.WindowStaysOnTopHint|self.C.Qt.WindowType.Tool)
            self.root.setWindowFlags(flags)
            self.root.setAttribute(self.C.Qt.WidgetAttribute.WA_TranslucentBackground,not opaque)
            self.root.setAttribute(self.C.Qt.WidgetAttribute.WA_NoSystemBackground,not opaque)
            self.root.setAttribute(self.C.Qt.WidgetAttribute.WA_StyledBackground,opaque)
            # Showing or remapping either preset must leave the current native
            # input owner alone. Users can still click and type in the editor.
            self.root.setAttribute(self.C.Qt.WidgetAttribute.WA_ShowWithoutActivating,True)
            self.root.setProperty('layoutPreset',candidate.layout)
            self.root.setStyleSheet(STYLE+('\nQWidget#telosRoot { background:#202329; }' if opaque else '')+'\n'+candidate.stylesheet)
            self.panel.show()
            panels=candidate.panels
            self.expanded=candidate.density=='editor'
            self.input_panel.setVisible(self.expanded)
            self.keyboard_panel.setVisible(panels.inputs and self.keyboard_details)
            self.keyboard_action.setEnabled(panels.inputs)
            self.editor_panel.setVisible(self.expanded)
            self.editor_scroll.setVisible(self.expanded)
            self.expand.setText('Close editor ▴' if self.expanded else 'Edit inputs ▾')
            for name,action in self.editor_actions.items():
                key=name.lower()
                if key in ('console','score','history','journal','branches'):
                    action.setVisible(getattr(panels,key))
                    if not getattr(panels,key) and self.editor_names[self.editor_pages.currentIndex()]==name:
                        self.editor_pages.setCurrentIndex(0)
            if not panels.console:self.console_panel.hide()
            self.workspace_preview.setVisible(workspace and panels.preview)
            self.game_canvas.setVisible(not workspace and candidate.game_surface=='owner_frame')
            size=candidate.sizing
            width,height=size.window_width,size.window_height
            if workspace:
                available=self.root.screen().availableGeometry()
                width=min(width,available.width())
                height=min(height,max(1,available.height()-48))
                self.root.setMinimumSize(min(640,width),min(600,height))
                self.root.move(available.x()+max(0,(available.width()-width)//2),available.y()+24)
            else:
                self.root.setMinimumSize(0,0);self.root.move(0,0)
            self.root.resize(width,height)
            if candidate.window_position is not None:
                screens = self.app.screens()
                x,y = candidate.window_position
                target = next((screen.availableGeometry() for screen in screens
                               if screen.availableGeometry().contains(x,y)),self.root.screen().availableGeometry())
                self.root.move(min(max(target.left(),x),max(target.left(),target.right()-width+1)),
                               min(max(target.top(),y),max(target.top(),target.bottom()-height+1)))
            if isinstance(self.roll,self.W.QTableView):
                self.fit_piano_headers()
                self.roll.verticalHeader().setDefaultSectionSize(min(18,size.row_height))
                # The local four-column inspection table keeps its legacy fill.
                if not isinstance(self.roll,self.W.QTableWidget):self.roll.setMaximumHeight(16777215)
            self._layout_surface(width,height)
            if hasattr(self,'panel_manager'):
                self.panel_manager.reset_defaults_from_widgets(opacity=.9 if self.ui_preferences.layout=='overlay' else 1.0)
                self.panel_manager.set_states(self._merged_panel_states(candidate.panel_states))
            self.panel.setVisible(candidate.density=='editor');self.hud.setVisible(candidate.density=='minimal')
            if hasattr(self,'panel_manager'):
                self.panel_manager.set_state('transport',{'visible':candidate.density=='editor'})
            self._applying_panels=False
            if hasattr(self,'panel_manager'):self._panels_changed(self.panel_manager.get_states())
            for shortcut in self.ui_shortcuts:shortcut.setEnabled(True)
            self.update_shortcut_tooltips()
            if hasattr(self,'interaction_controls'):self.interaction_controls.apply_preferences(candidate)
            self.queue('configure_shortcuts',shortcuts=list(candidate.shortcuts))
            if self.preferences_applied is not None:self.preferences_applied(candidate)
        finally:
            self._applying_panels=False
            try:
                if visible:
                    self.root.show()
                    self._restore_platform_mask()
                    self.C.QTimer.singleShot(0,self.root,self._restore_platform_mask)
            finally:
                try:
                    if interactions is not None:interactions.finish_design_change()
                finally:self.root.setUpdatesEnabled(True)

    def fit_piano_headers(self):
        """Configured cell width is a minimum; tick labels must stay readable."""
        if not isinstance(self.roll,self.W.QTableView):return
        width=self.ui_preferences.sizing.column_width
        model=self.roll.model()
        if model is not None and model.columnCount():
            metrics=self.roll.horizontalHeader().fontMetrics()
            for column in (0,model.columnCount()-1):
                text=model.headerData(column,self.C.Qt.Orientation.Horizontal,self.C.Qt.ItemDataRole.DisplayRole)
                width=max(width,metrics.horizontalAdvance(str(text or ''))+12)
        self.roll.horizontalHeader().setDefaultSectionSize(width)

    def _layout_workspace(self,w,h):
        if self.root.width()!=w or self.root.height()!=h:self.root.resize(w,h)
        gap=8;width=min(self.ui_preferences.sizing.panel_width,max(240,w-2*gap))
        transport_height=self._transport_height()
        # Stable full-width game framing; the piano unfolds over its lower edge.
        self.workspace_preview.setGeometry(gap,32,max(1,w-2*gap),max(1,h-40))
        self.workspace_preview.lower()
        self.panel.setGeometry(max(gap,(w-width)//2),max(gap,h-transport_height-gap),width,transport_height)
        self.input_panel.setVisible(self.expanded)
        self.panel.raise_()

    def _render_input_keys(self,available,held):
        signature=(available,held)
        if signature==self._key_signature:return False
        from kptas.telos.input_view import key_label
        self._key_signature=signature
        if hasattr(self,'current_input_summary'):
            self._compact_input_text=(' + '.join(held) if available and held else 'Inputs idle' if available else 'Inputs unavailable')
            self.current_input_summary.setText(self._compact_input_text)
            self.current_input_summary.setToolTip(self.current_input_summary.text())
        positions={'w':(0,1,1),'a':(1,0,1),'s':(1,1,1),'d':(1,2,1),
                   'lshift':(2,0,2),'space':(2,2,5),
                   'lmb':(1,4,1),'mmb':(1,5,1),'rmb':(1,6,1)}
        names={'lshift':'Shift','space':'Space','lmb':'LMB','mmb':'MMB','rmb':'RMB'}
        extras=[key for key in held if key not in positions]
        layout_signature=(tuple(extras),bool(available))
        if layout_signature==getattr(self,'_keycap_layout_signature',None):
            self.input_keys={}
            for key,label in self.keycaps.items():
                pressed=available and key in held
                if label.property('held')!=pressed:
                    label.setProperty('held',pressed)
                    label.style().unpolish(label);label.style().polish(label);label.update()
                if pressed:self.input_keys[key]=label
            return False
        self._keycap_layout_signature=layout_signature
        while self.key_grid.count():
            item=self.key_grid.takeAt(0)
            if item.widget():item.widget().hide();item.widget().deleteLater()
        self.input_keys={};self.keycaps={}
        mouse=self.W.QLabel('Mouse');mouse.setObjectName('muted');self.key_grid.addWidget(mouse,0,4,1,3)
        for index,key in enumerate(extras):positions[key]=(3+index//3,(index%3)*2,2)
        for key,(row,column,span) in positions.items():
            label=self.W.QLabel(names.get(key,key_label(key)))
            label.setObjectName('keycap_'+key);label.setProperty('uiRole','keycap')
            label.setProperty('held',available and key in held)
            label.setAlignment(self.C.Qt.AlignmentFlag.AlignCenter)
            label.setFixedSize(23*span+3*(span-1),20)
            label.setToolTip(key_label(key))
            self.key_grid.addWidget(label,row,column,1,span,self.C.Qt.AlignmentFlag.AlignLeft)
            self.keycaps[key]=label
            if available and key in held:self.input_keys[key]=label
        self._keycap_height=118+25*((len(extras)+2)//3)
        if not available:
            unavailable=self.W.QLabel('Input unavailable');unavailable.setObjectName('muted')
            self.key_grid.addWidget(unavailable,4+(len(extras)+2)//3,0,1,7)
            self._keycap_height+=18
        return True

    def show_input_motion(self,text):
        base=getattr(self,'_compact_input_text','Inputs unavailable')
        summary=base+(' · '+text if text else '')
        self.current_input_summary.setText(summary)
        self.current_input_summary.setToolTip(summary)

    def clear_workspace_frame(self,reason=''):
        if reason and self._workspace_pixmap is not None:
            # Say why the game image went away; once per distinct reason a few seconds apart.
            import time
            now=time.monotonic();last=getattr(self,'_frame_clear_log',('',0.0))
            if reason!=last[0] or now-last[1]>5:
                print('Stasis: game image cleared: '+reason,file=sys.stderr,flush=True)
                self._frame_clear_log=(reason,now)
        self._workspace_pixmap=None;self._workspace_frame_tick=None
        self._metric_tick=None;self._metric_time=None
        self.refresh_hud()
        if hasattr(self,'workspace_preview'):
            self.workspace_preview.clear();self.workspace_preview.setText('Frame unavailable')
            self.workspace_preview.setToolTip('Game unavailable'+(': '+reason if reason else ''))
            self.game_canvas.clear();self.game_canvas.setText('Frame unavailable')
            self.game_canvas.setToolTip(reason)
            self.workspace_preview._telos_frame_identity=None
            self.game_canvas._telos_frame_identity=None
            self._owner_frame_metadata={}
            if hasattr(self,'_input_metadata'):self._input_metadata['view_info']={'surface':'unavailable'}

    @staticmethod
    def frame_provenance_matches(tick,metadata):
        if not isinstance(metadata,dict) or type(tick) is not int:return False
        coherent=(type(metadata.get('gate_tick')) is int and type(metadata.get('captured_tick')) is int
                  and metadata['gate_tick']==metadata['captured_tick']==tick)
        source=metadata.get('source')
        if source in ('captured_back_buffer','restored_framebuffer'):return coherent
        return (source=='recorded_preview' and coherent and type(metadata.get('archive_tick')) is int and metadata.get('archive_tick')==tick
                and metadata.get('archive_digest_verified') is True
                and metadata.get('simulation_state_source')=='verified_full_root_restore')

    def set_workspace_frame(self,pixmap,tick,metadata=None):
        if self.ui_preferences.layout=='workspace' and not self.ui_preferences.panels.preview:return False
        if self.ui_preferences.layout=='overlay' and self.ui_preferences.game_surface!='owner_frame':return False
        # Never substitute a stale restore buffer or re-label the previous frame
        # as the new gate. Invalid replacements leave its pixels/tick untouched.
        if pixmap is None or pixmap.isNull() or not self.frame_provenance_matches(tick,metadata):return False
        self._metric_tick=tick
        virtual=metadata.get('virtual_us')
        self._metric_time=virtual/1_000_000 if type(virtual) is int and virtual>=0 else None
        self.refresh_hud()
        self._workspace_pixmap=pixmap;self._workspace_frame_tick=tick
        capture=metadata.get('captured_tick')
        self._owner_frame_metadata={'surface':'owner_frame','owner_gate_tick':tick,
                                    'capture_tick':capture if type(capture) is int else None,
                                    'gate_tick':metadata['gate_tick'],'captured_tick':capture,
                                    'epoch':metadata.get('epoch'),
                                    'source':metadata.get('source','unknown')}
        if metadata.get('source') == 'recorded_preview':
            archive_tick = metadata.get('archive_tick')
            self._owner_frame_metadata['archive_tick'] = archive_tick
            self._owner_frame_metadata['archive_digest_verified'] = metadata.get('archive_digest_verified')
            self._owner_frame_metadata['simulation_state_source'] = metadata.get('simulation_state_source')
            tooltip=f'Recorded image · {self._unit()} {archive_tick}; game restored to {self._unit()} {tick}'
        else:
            tooltip=f'Game · {self._unit()} {tick}'
        self.workspace_preview.setToolTip(tooltip);self.game_canvas.setToolTip(tooltip)
        if hasattr(self,'_input_metadata'):self._input_metadata['view_info']=dict(self._owner_frame_metadata)
        self._render_workspace_frame()
        if self.ui_preferences.layout=='workspace' and self.root.isVisible():
            # The focused native renderer can restack itself after a load or
            # restore. Keep the surrounding owner-frame surface visible;
            # raising this nonactivating widget does not take keyboard focus.
            self.root.raise_()
        return True

    def _render_workspace_frame(self):
        # Keep scaling on the GUI thread without Qt's smooth-scale worker pool.
        # A live GUI stack showed QLatch/qSmoothScaleImage waiting for those
        # workers under heavy host load. This cheaper path preserves pixel edges.
        if self._workspace_pixmap is None:return
        surfaces=[]
        if self.ui_preferences.layout=='overlay' and self.ui_preferences.game_surface=='owner_frame':
            surfaces.append(self.game_canvas)
            # The floating "Game" panel (Panels > Game) shows the same frame;
            # without this it only ever said "Frame unavailable" in overlay layout.
            if self.workspace_preview.isVisible():surfaces.append(self.workspace_preview)
        elif self.ui_preferences.layout=='workspace' and self.ui_preferences.panels.preview:
            surfaces.append(self.workspace_preview)
        for surface in surfaces:
            size=surface.contentsRect().size()
            if size.width()<=0 or size.height()<=0:continue
            identity=(self._workspace_pixmap.cacheKey(),size.width(),size.height())
            if identity==getattr(surface,'_telos_frame_identity',None):continue
            surface.setPixmap(self._workspace_pixmap.scaled(size,
                self.C.Qt.AspectRatioMode.KeepAspectRatio,self.C.Qt.TransformationMode.FastTransformation))
            surface._telos_frame_identity=identity

    def busy_state(self,text):
        if self.ui_preferences.layout=='workspace':self.clear_workspace_frame()
        # Verification may briefly play native frames. Keep the last accepted
        # image and its existing tick/input labels until the next gate is accepted.
        self._pending_operation=text
        self._set_busy(True);self.state.setText(text)
        if self._last_game_view is not None and self.ui_preferences.layout=='overlay':
            self._show_game_image(*self._last_game_view)
        self.app.processEvents()

    def _layout_surface(self,w,h):
        self._position_compact_status()
        self._layout_seek_rail()
        self.hud.move(8,8);self.hud.setVisible(self.ui_preferences.density=='minimal');self.hud.raise_()
        if hasattr(self,"game_canvas"):
            self.game_canvas.setGeometry(0,0,w,h);self.game_canvas.lower()
        if self.ui_preferences.layout=='workspace':
            self._layout_workspace(w,h)
            self._finish_panel_layout()
            # Saved panel geometry can override the preset above. Scale once
            # against the final viewport, rather than alternating both sizes.
            self._render_workspace_frame()
            return
        self._render_workspace_frame()
        if self.root.width()!=w or self.root.height()!=h:self.root.resize(w,h)
        width=min(self.ui_preferences.sizing.panel_width,max(240,w-16));left=max(8,(w-width)//2)
        height=self._transport_height()
        self.panel.setGeometry(left,max(8,h-height-8),width,height)
        self.panel.setVisible(self.ui_preferences.density=='editor')
        self.input_panel.setVisible(self.expanded)
        self._finish_panel_layout()

    def set_game_view(self,pixels,w,h,view_info):
        """Show a verified checkpoint image on the actual display when requested.

        Its separate input-transparent window leaves game mouse input alone.
        The same pixels are the recorder base; controls are the actual widgets.
        """
        if self.ui_preferences.layout=='workspace' or self.ui_preferences.game_surface=='owner_frame':
            self._set_busy(False)
            source=view_info.get('source')
            acknowledged=(self.presentation.snapshot.tick if self.presentation is not None and self.presentation.snapshot is not None
                          else self.control.sup.tick if self.control is not None else None)
            coherent=(type(acknowledged) is int and type(view_info.get('gate_tick')) is int
                      and type(view_info.get('captured_tick')) is int
                      and view_info['gate_tick']==view_info['captured_tick']==acknowledged)
            recorded = (source == 'recorded_preview' and coherent
                        and view_info.get('archive_tick') == acknowledged
                        and view_info.get('archive_digest_verified') is True
                        and view_info.get('simulation_state_source') == 'verified_full_root_restore')
            if (source in ('captured_back_buffer','restored_framebuffer') and coherent) or recorded:
                image=self.G.QImage(pixels,w,h,w*4,self.G.QImage.Format.Format_RGB32).mirrored(False,True).copy()
                self.set_workspace_frame(self.G.QPixmap.fromImage(image),acknowledged,view_info)
            # An invalid incoming view never changes the old image's identity.
            return
        self._layout_surface(w,h)
        self._set_busy(False)
        source=view_info.get('source')
        self._last_game_view=(bytes(pixels),w,h) if source in (
            'captured_back_buffer','restored_framebuffer','recorded_preview') else None
        if source!='recorded_preview':
            if self.preview is not None:self.preview.hide()
            return
        self._show_game_image(pixels,w,h)

    def _show_game_image(self,pixels,w,h):
        if self.preview is None:
            flags=(self.C.Qt.WindowType.FramelessWindowHint|self.C.Qt.WindowType.WindowStaysOnTopHint
                   |self.C.Qt.WindowType.Tool|self.C.Qt.WindowType.WindowTransparentForInput)
            self.preview=self.W.QLabel(None,flags)
            self.preview.setAttribute(self.C.Qt.WidgetAttribute.WA_ShowWithoutActivating)
            self.preview.setAttribute(self.C.Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            self.preview.setFocusPolicy(self.C.Qt.FocusPolicy.NoFocus)
            self.preview.setWindowTitle('Stasis game image')
        # Native framebuffer alpha is not window opacity. Interpret its BGR
        # bytes as opaque for display only; keep the verified source unchanged.
        image=self.G.QImage(pixels,w,h,w*4,self.G.QImage.Format.Format_RGB32).mirrored(False,True).copy()
        self.preview.setGeometry(0,0,w,h);self.preview.setPixmap(self.G.QPixmap.fromImage(image))
        self.preview.show();self.preview.raise_();self.root.raise_();self.app.processEvents()

    def layout_metadata(self):
        """Visible widget regions in composed-video pixels, for independent audits."""
        def region(widget):
            if not widget.isVisible():return None
            point=widget.mapTo(self.root,self.C.QPoint(0,0))
            return {'bbox':[point.x(),point.y(),widget.width(),widget.height()],
                    'text':widget.text() if isinstance(widget,self.W.QLabel) else ''}
        return {'size':[self.root.width(),self.root.height()], 'tick':region(self.tick),
                'origin':region(self.origin), 'input_panel':region(self.input_panel),
                'keys':{key:region(widget) for key,widget in self.input_keys.items()},
                'input_labels':[region(self.key_grid.itemAt(i).widget()) for i in range(self.key_grid.count())],
                'mouse':region(self.live_keys), 'event':region(self.input_event),
                'image_source':region(self.view_source), 'hud':region(self.hud_metrics), 'density':self.ui_preferences.density}

    def snapshot(self):
        # Render the actual visible widgets at their live positions. Rendering
        # the masked top-level window directly can clip to only its dirty region.
        pixmap=self.G.QPixmap(self.root.size());pixmap.fill(self.C.Qt.GlobalColor.transparent)
        flags=self.W.QWidget.RenderFlag.DrawWindowBackground|self.W.QWidget.RenderFlag.DrawChildren
        for widget in (self.game_canvas,self.panel,self.workspace_preview,self.input_panel):
            if widget.isVisible():
                widget.render(pixmap,widget.pos(),self.G.QRegion(widget.rect()),flags)
        return pixmap

    def compose(self,pixels,w,h):
        # This is the live widget, not a separate video-only drawing.
        image=self.G.QImage(pixels,w,h,w*4,self.G.QImage.Format.Format_ARGB32).mirrored(False,True).copy()
        painter=self.G.QPainter(image);painter.drawPixmap(0,0,self.snapshot());painter.end()
        return bytes(image.constBits()[:image.sizeInBytes()])

    def close(self):
        if self.preview is not None:self.preview.close()
        if hasattr(self,'interaction_controls'):self.interaction_controls.close()
        self.root.close();self.app.processEvents()
