"""Authored input intervals painted on the existing timeline slider."""
from math import ceil
from PySide6 import QtCore as C, QtGui as G, QtWidgets as W
from kptas.config import unit_name


def action_style(kind,key):
    """Stable semantic colors; exact input identity stays in the tooltip."""
    if kind=='mouse':
        return ('#F2A65A','circle') if key in ('lmb','rmb','mmb') else ('#D18DF0','diamond')
    token=key.lower()
    if token in ('w','a','s','d','up','down','left','right','lshift','rshift'):
        return '#39A9DB','triangle'
    if token in ('enter','return','esc','escape','space','tab'):
        return '#65D6AD','square'
    if token.startswith('f') and token[1:].isdigit():
        return '#E5C15B','diamond'
    return '#A9B8C8','square'


class InputSeekSlider(W.QSlider):
    expandRequested=C.Signal()
    seekRequested=C.Signal(int)
    # A pressed or dragged cursor tick. Presentation only; never a seek.
    cursorMoved=C.Signal(int)
    # Global position for a visible refusal when seek is not admitted.
    seekRefused=C.Signal(C.QPoint)
    CLUSTER_WIDTH=24
    MAX_MARKER_CACHE_PIXELS=4*1024*1024
    NAVIGATION_KEYS=(C.Qt.Key_Left,C.Qt.Key_Right,C.Qt.Key_Up,C.Qt.Key_Down,
                     C.Qt.Key_Home,C.Qt.Key_End,C.Qt.Key_PageUp,C.Qt.Key_PageDown)
    def __init__(self, *args):
        super().__init__(*args)
        self.markers=()
        self.splices=()  # (tick, score name or None, 'load'|'splice') score starts
        self.buckets={}
        self._rows=None
        self._marker_cache=None
        self.rangeChanged.connect(self._invalidate_marker_cache)
        self.setMouseTracking(True)
        self.setFocusPolicy(C.Qt.StrongFocus)
        self.setAccessibleName('TAS timeline seek rail')
        # The one unit source for rail text. The overlay sets it from the
        # owner's clock precision (unit_name); unknown precision reads as frame.
        self.position_unit=unit_name(None,title=True)
        self.setAccessibleDescription('Recorded positions and authored inputs. Enter or Space opens the editor. Arrow keys seek when admitted.')
        self._press_position=None
        self._press_value=None
        self._seek_enabled=True
        self._view_window=None
        # Optional hooks (timeline_rail.FutureRail): paint(slider,painter,bounds)
        # for recorded-future marks, and value -> drag tooltip text or None.
        self.timeline_marks=None
        self.cursor_hint=None

    def set_seek_enabled(self,enabled):
        enabled=bool(enabled)
        interacting=self._press_position is not None
        changed=enabled!=self._seek_enabled
        self._seek_enabled=enabled
        # Presentation expansion remains available after an owner refusal.
        super().setEnabled(True)
        if not self._seek_enabled:
            self._cancel_pointer_seek()
            # An interrupted drag or click says why instead of vanishing.
            if interacting:self.seekRefused.emit(self._handle_global_center())
        if changed:self.update()

    @property
    def seek_enabled(self):
        return self._seek_enabled

    def set_view_window(self,start,end=None):
        """Visible piano ticks, painted as a band; None hides it."""
        window=None if start is None else (min(start,end),max(start,end))
        if window!=self._view_window:
            self._view_window=window
            self.update()

    def _handle_rect(self):
        option=W.QStyleOptionSlider();self.initStyleOption(option)
        return self.style().subControlRect(W.QStyle.CC_Slider,option,W.QStyle.SC_SliderHandle,self)

    def _handle_global_center(self):
        return self.mapToGlobal(self._handle_rect().center())

    def _cancel_pointer_seek(self, *, restore=False):
        value=self._press_value
        self._press_position=None;self._press_value=None
        if restore and value is not None:self.setValue(value)
        self.setSliderDown(False)
        if value is not None:W.QToolTip.hideText()

    def _move_cursor(self,position,global_position):
        self.setValue(self._value_at_position(position))
        value=self.value()
        self.cursorMoved.emit(value)
        hint=self.cursor_hint(value) if self.cursor_hint is not None else None
        W.QToolTip.showText(global_position,hint or f'{self.position_unit} {value} · release to seek',self)

    def keyPressEvent(self,event):
        if event.key()==C.Qt.Key_Escape and self._press_position is not None:
            # Cancel a drag: restore the handle and ignore the coming release.
            self._cancel_pointer_seek(restore=True)
            event.accept();return
        if event.key() in (C.Qt.Key_Return,C.Qt.Key_Enter,C.Qt.Key_Space):
            self._cancel_pointer_seek(restore=True)
            self.expandRequested.emit();event.accept();return
        if not self._seek_enabled:
            if event.key() in self.NAVIGATION_KEYS:self.seekRefused.emit(self._handle_global_center())
            event.accept();return
        if event.key() in self.NAVIGATION_KEYS:self._cancel_pointer_seek(restore=True)
        before=self.value()
        super().keyPressEvent(event)
        if self.value()!=before:self.seekRequested.emit(self.value())

    def wheelEvent(self,event):
        # Passing over the overlay while scrolling must not restore the game.
        # StrongFocus admits keyboard/click focus, never wheel-acquired focus.
        if not self.hasFocus():event.ignore();return
        if not self._seek_enabled:event.accept();return
        self._cancel_pointer_seek(restore=True)
        before=self.value()
        super().wheelEvent(event)
        if self.value()!=before:self.seekRequested.emit(self.value())

    def _value_at_position(self,position):
        # QSlider's default groove click advances one page; a TAS rail needs
        # the tick under the pointer. Use the style's handle travel and direction.
        option=W.QStyleOptionSlider();self.initStyleOption(option)
        groove=self.style().subControlRect(W.QStyle.CC_Slider,option,W.QStyle.SC_SliderGroove,self)
        handle=self.style().subControlRect(W.QStyle.CC_Slider,option,W.QStyle.SC_SliderHandle,self)
        if self.orientation()==C.Qt.Vertical:
            start=groove.top();length=groove.height()-handle.height()
            offset=round(position.y()-handle.height()/2)-start
        else:
            start=groove.left();length=groove.width()-handle.width()
            offset=round(position.x()-handle.width()/2)-start
        return W.QStyle.sliderValueFromPosition(self.minimum(),self.maximum(),
            max(0,min(length,offset)),max(0,length),option.upsideDown)

    def mousePressEvent(self,event):
        if not self._seek_enabled:
            if event.button()==C.Qt.LeftButton:self.seekRefused.emit(event.globalPosition().toPoint())
            event.accept();return
        if event.button()==C.Qt.LeftButton:
            self._press_value=self.value()
            self._press_position=event.position()
            self.setSliderDown(True)
            self._move_cursor(event.position(),event.globalPosition().toPoint())
            event.accept();return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self,event):
        if not self._seek_enabled:event.accept();return
        if event.button()!=C.Qt.LeftButton:
            super().mouseReleaseEvent(event);return
        event.accept()
        position=self._press_position
        self._press_position=None;self._press_value=None
        # Cancellation and the second release of a double-click have no
        # tracked press. Never turn an unmatched release into a new seek.
        if position is None:return
        self.setValue(self._value_at_position(event.position()))
        value=self.value()  # Bind the exact released tick, never a later value.
        W.QToolTip.hideText()
        # Clicks and drags commit once on release without a double-click delay.
        # Keep the handle down until the existing receiver admits/refuses it.
        self.seekRequested.emit(value)
        self.setSliderDown(False)

    def set_rows(self, rows):
        if rows is self._rows or rows==self._rows:return
        self._rows=rows
        markers=[]
        for row in rows:
            if row.start is None or row.end is None:continue
            note=row.item
            tokens=[('key',str(key)) for key in getattr(note,'held',())]
            tokens += [('mouse',str(button)) for button in getattr(note,'buttons',())]
            if getattr(note,'mouse_rel',None) or getattr(note,'mouse_abs',None):tokens.append(('mouse','move'))
            if getattr(note,'wheel',0):tokens.append(('mouse','wheel'))
            for kind,key in tokens:
                markers.append((row.start,row.start if kind=='mouse' and key=='wheel' else row.end,kind,key))
        self.markers=tuple(markers)
        self._invalidate_marker_cache()
        self.update()

    def _invalidate_marker_cache(self,*_):
        self._marker_cache=None
        self.buckets={}

    def changeEvent(self,event):
        # Font, palette and style changes can arrive through a parent QSS reload.
        self._invalidate_marker_cache()
        super().changeEvent(event)

    def resizeEvent(self,event):
        self._invalidate_marker_cache()
        super().resizeEvent(event)

    def _bounds(self):
        option=W.QStyleOptionSlider();self.initStyleOption(option)
        groove=self.style().subControlRect(W.QStyle.CC_Slider,option,W.QStyle.SC_SliderGroove,self)
        handle=self.style().subControlRect(W.QStyle.CC_Slider,option,W.QStyle.SC_SliderHandle,self)
        if self.orientation()==C.Qt.Vertical:
            return self.height()-groove.bottom()+handle.height()//2,self.height()-groove.top()-handle.height()//2
        return groove.left()+handle.width()//2,groove.right()-handle.width()//2

    def paintEvent(self,event):
        # The native groove, handle, focus and hover state remain live. Only
        # authored markers are cached; moving the playhead cannot rebuild them.
        super().paintEvent(event)
        font=self.font();font.setPointSize(8)
        bounds=self._bounds()
        ratio=self.devicePixelRatioF()
        key=(self.width(),self.height(),ratio,self.orientation(),self.minimum(),self.maximum(),
             bounds,font.toString(),self.palette().cacheKey(),self.layoutDirection(),self.invertedAppearance())
        cached=self._marker_cache
        if cached is not None and cached[0]!=key:
            self._invalidate_marker_cache()
            cached=None
        if cached is None:
            width,height=ceil(self.width()*ratio),ceil(self.height()*ratio)
            # One transparent layer covering the actual widget, never a bitmap
            # whose dimensions grow with recording length or tick range.
            if 0<width*height<=self.MAX_MARKER_CACHE_PIXELS:
                layer=G.QPixmap(width,height)
                layer.setDevicePixelRatio(ratio)
                layer.fill(C.Qt.transparent)
                if not layer.isNull():
                    painter=G.QPainter(layer)
                    try:self._paint_markers(painter,font,bounds)
                    finally:painter.end()
                    self._marker_cache=cached=(key,layer)
        painter=G.QPainter(self)
        try:
            if cached is not None:painter.drawPixmap(0,0,cached[1])
            else:self._paint_markers(painter,font,bounds)
            if self._view_window is not None:self._paint_view_window(painter,bounds)
            if self.splices:self._paint_splices(painter,bounds)
            if self.timeline_marks is not None:self.timeline_marks(self,painter,bounds)
            if not self._seek_enabled:
                # The widget stays Qt-enabled for expansion; dim the handle so
                # a refused seek does not look like an active control.
                painter.fillRect(self._handle_rect(),G.QColor(11,16,22,160))
        finally:painter.end()

    def _paint_view_window(self,painter,bounds):
        """The piano viewport's tick span; clamped to an edge sliver when outside."""
        if self.maximum()<=self.minimum():return
        painter.save()
        try:
            vertical=self.orientation()==C.Qt.Vertical
            if vertical:
                painter.translate(0,self.height());painter.rotate(-90)
            thickness=self.width() if vertical else self.height()
            left,right=bounds;low,high=self.minimum(),self.maximum();span=high-low
            start,end=self._view_window
            start,end=max(low,min(high,start)),max(low,min(high,end))
            x0=left+round((start-low)/span*(right-left))
            x1=left+round((end-low)/span*(right-left))
            if x1-x0<3:x0=max(left-1,min(x0,right-2));x1=x0+3
            painter.setPen(G.QPen(G.QColor(25,227,193,150),1))
            painter.setBrush(G.QColor(25,227,193,34))
            painter.drawRect(C.QRectF(x0+.5,.5,x1-x0-1,thickness-1))
        finally:painter.restore()

    def _paint_markers(self,painter,font,bounds):
        painter.setFont(font)
        if self.orientation()==C.Qt.Vertical:
            painter.translate(0,self.height());painter.rotate(-90)
        left,right=bounds;span=max(1,self.maximum()-self.minimum())
        buckets={}
        for marker in self.markers:
            start,end,kind,key=marker
            if end<self.minimum() or start>self.maximum():continue
            x=left+round((max(start,self.minimum())-self.minimum())/span*(right-left))
            finish=left+round((min(end,self.maximum())-self.minimum())/span*(right-left))
            color=G.QColor(action_style(kind,key)[0])
            color.setAlpha(105)
            painter.fillRect(C.QRect(x,21,max(2,finish-x+1),2),color)
            buckets.setdefault(x//self.CLUSTER_WIDTH,[]).append(marker)
        self.buckets=buckets
        for bucket,items in buckets.items():
            x=max(left+6,min(right-6,bucket*self.CLUSTER_WIDTH))
            styles=list(dict.fromkeys(action_style(kind,key) for _,_,kind,key in items))
            # A cluster represents all categories here, never one arbitrary key.
            for index,(shade,shape) in enumerate(styles[:3]):
                center=x+(index-(min(3,len(styles))-1)/2)*4
                painter.setPen(G.QColor(shade));painter.setBrush(G.QColor(shade))
                rect=C.QRectF(center-2,1,4,5)
                if shape=='circle':painter.drawEllipse(rect)
                elif shape=='triangle':painter.drawPolygon(G.QPolygonF([C.QPointF(center,1),C.QPointF(center-2,6),C.QPointF(center+2,6)]))
                elif shape=='diamond':painter.drawPolygon(G.QPolygonF([C.QPointF(center,1),C.QPointF(center-2,3.5),C.QPointF(center,6),C.QPointF(center+2,3.5)]))
                else:painter.drawRect(rect)
            painter.setPen(G.QColor('#D7E3EA'))
            if len(items)==1:
                _,_,kind,key=items[0]
                glyph=key.upper() if kind=='key' else {'lmb':'L','rmb':'R','mmb':'M','move':'↔','wheel':'↕'}.get(key,key.upper())
            else:glyph=str(len(items))
            # Full function-key identities (F10/F11/F12) fit this cluster. Longer
            # names visibly elide rather than masquerading as another exact key.
            glyph=painter.fontMetrics().elidedText(glyph,C.Qt.ElideRight,self.CLUSTER_WIDTH-2)
            width=painter.fontMetrics().horizontalAdvance(glyph)
            painter.drawText(max(left,min(right-width,x-width//2)),18,glyph)

    def set_splices(self,splices):
        """Score starts kept in this recording: one line where each score begins."""
        splices=tuple(splices or ())
        if splices!=self.splices:
            self.splices=splices
            self.update()

    def _splice_x(self,tick,bounds):
        left,right=bounds;low,high=self.minimum(),self.maximum()
        if high<=low or not low<=tick<=high:return None
        return left+round((tick-low)/(high-low)*(right-left))

    def _paint_splices(self,painter,bounds):
        painter.save()
        try:
            vertical=self.orientation()==C.Qt.Vertical
            if vertical:
                painter.translate(0,self.height());painter.rotate(-90)
            thickness=self.width() if vertical else self.height()
            for tick,_name,kind in self.splices:
                x=self._splice_x(tick,bounds)
                if x is None:continue
                color=G.QColor('#FF7AB6' if kind=='splice' else '#7AE0FF')
                painter.fillRect(C.QRect(x-1,0,2,thickness),color)
                painter.setPen(color);painter.setBrush(color)
                painter.drawPolygon(G.QPolygonF([C.QPointF(x,0),C.QPointF(x+5,2.5),C.QPointF(x,5)]))
        finally:painter.restore()

    def mouseMoveEvent(self,event):
        x=self.height()-event.position().y() if self.orientation()==C.Qt.Vertical else event.position().x()
        near=min(self.buckets,key=lambda bucket:abs(bucket*self.CLUSTER_WIDTH-x),default=None)
        items=self.buckets.get(near,()) if near is not None and abs(near*self.CLUSTER_WIDTH-x)<self.CLUSTER_WIDTH else ()
        bounds=self._bounds()
        starts=[(tick,name,kind) for tick,name,kind in self.splices
                if (sx:=self._splice_x(tick,bounds)) is not None and abs(sx-x)<=6]
        self.setToolTip('\n'.join([f'{self.position_unit} {tick}: '+(f'replaced with {name or "a score"}' if kind=='splice'
                                    else f'loaded {name or "a score"}') for tick,name,kind in starts[:8]]
                                  +[f'{self.position_unit} {start}–{end}: {kind} {key}' for start,end,kind,key in items[:24]])+
                        (f'\n+{len(items)-24} more inputs' if len(items)>24 else ''))
        if not self._seek_enabled:
            self.setToolTip('Seek unavailable · Enter, Space or right-click to open controls\n'+self.toolTip())
            event.accept();return
        if self._press_position is not None and event.buttons() & C.Qt.LeftButton:
            self._move_cursor(event.position(),event.globalPosition().toPoint())
            event.accept();return
        super().mouseMoveEvent(event)

    def mouseDoubleClickEvent(self,event):
        # The first release already sought. Consume the continuation instead
        # of mutating twice or also expanding the editor. Expansion is explicit.
        self._cancel_pointer_seek(restore=True)
        event.accept()

    def contextMenuEvent(self,event):
        self._cancel_pointer_seek(restore=True)
        self.expandRequested.emit();event.accept()
