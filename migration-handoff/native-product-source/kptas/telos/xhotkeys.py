"""Read tool hotkeys on Wine's X display while its game thread is gated.

This queries the server's physical key state. It never sends an input or calls
into the game. The native poll still drains and drops host input messages.
"""
import ctypes as C
import ctypes.util
import os
import sys
import re


TOOL_ACTIONS = ('stop', 'branch', 'rewind_mark', 'rewind', 'step', 'play_pause',
                'toggle_editor', 'save', 'apply', 'switch_layout', 'reload_ui',
                'toggle_engagement', 'toggle_visibility')


_NATIVE_ALIASES = {'Return': 13, 'Enter': 13, 'Space': 32, 'Escape': 27,
                   'Esc': 27, 'Tab': 9, 'Backspace': 8, 'Delete': 46, 'Del': 46,
                   'Home': 36, 'End': 35, 'Left': 37, 'Up': 38, 'Right': 39,
                   'Down': 40, 'PgUp': 33, 'PgDown': 34, 'Insert': 45, 'Ins': 45}
_NATIVE_MODIFIERS = {'Ctrl': 1, 'Alt': 2, 'Shift': 4, 'Meta': 8}


def native_chord(text):
    """One PortableText key -> (vk, modifier mask, is function key).

    Raises ValueError for a key the native hook cannot read. Qt names such as
    Del and Ins map to the same virtual keys as Delete and Insert.
    """
    parts = text.split('+')
    key, mask = parts[-1], 0
    for part in parts[:-1]:
        if part not in _NATIVE_MODIFIERS or mask & _NATIVE_MODIFIERS[part]:
            raise ValueError('unsupported native shortcut: ' + text)
        mask |= _NATIVE_MODIFIERS[part]
    function = re.fullmatch(r'F([1-9]|1[0-9]|2[0-4])', key)
    if function:
        return 111 + int(function[1]), mask, True
    if len(key) == 1 and key.isascii() and key.isalnum():
        return ord(key.upper()), mask, False
    if key in _NATIVE_ALIASES:
        return _NATIVE_ALIASES[key], mask, False
    raise ValueError('unsupported native shortcut key: ' + text)


def native_bindings(shortcuts, skipped=None):
    """PortableText preferences -> native VK chords, shared by every surface.

    Plain text/navigation shortcuts remain local to the focused Qt widget.
    A key the native hook cannot read, or a second key for the same chord
    (Ctrl+Return and Ctrl+Enter), is left out rather than guessed. It still
    works as a Qt shortcut while Stasis has focus. Reasons go to `skipped`.
    """
    result = []
    seen = set()
    for action, text in shortcuts:
        if action not in TOOL_ACTIONS or not text:
            continue
        try:
            vk, mask, function = native_chord(text)
        except ValueError as exc:
            if skipped is not None:
                skipped.append(str(exc))
            continue
        if not mask and not function:
            continue
        if (vk, mask) in seen:
            if skipped is not None:
                skipped.append('native shortcut collision: ' + text)
            continue
        seen.add((vk, mask))
        result.append({'vk': vk, 'modifiers': mask, 'action': TOOL_ACTIONS.index(action)})
    return result


class XHotkeys:
    def __init__(self, display):
        self.x = C.CDLL(ctypes.util.find_library('X11'))
        self.x.XOpenDisplay.argtypes = [C.c_char_p]
        self.x.XOpenDisplay.restype = C.c_void_p
        self.x.XCloseDisplay.argtypes = [C.c_void_p]
        self.x.XQueryKeymap.argtypes = [C.c_void_p, C.c_void_p]
        self.x.XGetInputFocus.argtypes = [C.c_void_p, C.POINTER(C.c_ulong), C.POINTER(C.c_int)]
        self.x.XFetchName.argtypes = [C.c_void_p, C.c_ulong, C.POINTER(C.c_void_p)]
        self.x.XFree.argtypes = [C.c_void_p]
        self.x.XStringToKeysym.argtypes = [C.c_char_p]
        self.x.XStringToKeysym.restype = C.c_ulong
        self.x.XKeysymToKeycode.argtypes = [C.c_void_p, C.c_ulong]
        self.x.XKeysymToKeycode.restype = C.c_ubyte
        self.d = self.x.XOpenDisplay(display.encode() if display and display != 'auto' else None)
        if not self.d:
            raise RuntimeError('cannot open the game X display for tool hotkeys')
        self.codes = {k: self.x.XKeysymToKeycode(self.d, self.x.XStringToKeysym(k.encode()))
                      for k in ('Control_L','Control_R','Alt_L','Alt_R','Shift_L','Shift_R',
                                'F6','F7','F8','F9','F10','F11','F12')}
        self.held = set()

    @classmethod
    def for_config(cls, cfg):
        if sys.platform != 'linux' or not cfg.get('game', {}).get('display'):
            return None
        return cls(cfg['game']['display'])

    def poll(self):
        win, revert, name = C.c_ulong(), C.c_int(), C.c_void_p()
        self.x.XGetInputFocus(self.d, C.byref(win), C.byref(revert))
        focused = False
        if win.value > 1 and self.x.XFetchName(self.d, win, C.byref(name)) and name.value:
            try:
                focused = C.string_at(name).decode(errors='replace') in ('Star Wars: Knights of the Old Republic', 'Stasis', 'InputInterfaceTelos')
            finally:
                self.x.XFree(name)
        keys = (C.c_ubyte * 32)()
        self.x.XQueryKeymap(self.d, keys)
        def down(k):
            code = self.codes[k]
            return bool(code and keys[code // 8] & (1 << (code % 8)))
        now = set()
        ctrl, alt = down('Control_L') or down('Control_R'), down('Alt_L') or down('Alt_R')
        shift = down('Shift_L') or down('Shift_R')
        # Fallback for the default toggle_visibility chord when the native
        # dispatcher is absent. KotOR binds F4/F5 and Pause, not F12.
        if focused and ctrl and shift and not alt and down('F12'):
            now.add('toggle_visibility')
        if focused and ctrl and alt:
            for key, action in (('F6','workspace'),('F7','play_pause'),('F8','step'),('F9','rewind'),('F10','branch'),('F11','stop')):
                if down(key):
                    now.add('rewind_mark' if key == 'F9' and (down('Shift_L') or down('Shift_R')) else action)
        edges, self.held = now - self.held, now
        return next((a for a in ('stop','workspace','branch','rewind_mark','rewind','step','play_pause','toggle_visibility') if a in edges), '')

    def close(self):
        if self.d:
            self.x.XCloseDisplay(self.d)
            self.d = None
