"""Key names: canonical name -> (Windows VK code, DirectInput DIK scan code)."""

import difflib

KEYS: dict[str, tuple[int, int]] = {}

_LETTER_DIK = {
    "a": 0x1E, "b": 0x30, "c": 0x2E, "d": 0x20, "e": 0x12, "f": 0x21, "g": 0x22,
    "h": 0x23, "i": 0x17, "j": 0x24, "k": 0x25, "l": 0x26, "m": 0x32, "n": 0x31,
    "o": 0x18, "p": 0x19, "q": 0x10, "r": 0x13, "s": 0x1F, "t": 0x14, "u": 0x16,
    "v": 0x2F, "w": 0x11, "x": 0x2D, "y": 0x15, "z": 0x2C,
}
for _ch, _dik in _LETTER_DIK.items():
    KEYS[_ch] = (ord(_ch.upper()), _dik)

for _d in range(10):
    KEYS[str(_d)] = (0x30 + _d, 0x0B if _d == 0 else 0x01 + _d)

for _n in range(1, 11):
    KEYS[f"f{_n}"] = (0x6F + _n, 0x3A + _n)
KEYS["f11"] = (0x7A, 0x57)
KEYS["f12"] = (0x7B, 0x58)

_NUMPAD_DIK = [0x52, 0x4F, 0x50, 0x51, 0x4B, 0x4C, 0x4D, 0x47, 0x48, 0x49]
for _d, _dik in enumerate(_NUMPAD_DIK):
    KEYS[f"num{_d}"] = (0x60 + _d, _dik)

KEYS.update({
    "up": (0x26, 0xC8), "down": (0x28, 0xD0), "left": (0x25, 0xCB), "right": (0x27, 0xCD),
    "lshift": (0xA0, 0x2A), "rshift": (0xA1, 0x36),
    "lctrl": (0xA2, 0x1D), "rctrl": (0xA3, 0x9D),
    "lalt": (0xA4, 0x38), "ralt": (0xA5, 0xB8),
    "esc": (0x1B, 0x01), "tab": (0x09, 0x0F), "space": (0x20, 0x39),
    "enter": (0x0D, 0x1C), "backspace": (0x08, 0x0E),
    "nummul": (0x6A, 0x37), "numadd": (0x6B, 0x4E), "numsub": (0x6D, 0x4A),
    "numdec": (0x6E, 0x53), "numdiv": (0x6F, 0xB5),
    "numenter": (0x0D, 0x9C),  # same VK as enter; Windows tells them apart by the extended flag
    "numlock": (0x90, 0x45),
    "minus": (0xBD, 0x0C), "equals": (0xBB, 0x0D),
    "lbracket": (0xDB, 0x1A), "rbracket": (0xDD, 0x1B), "backslash": (0xDC, 0x2B),
    "semicolon": (0xBA, 0x27), "apostrophe": (0xDE, 0x28), "grave": (0xC0, 0x29),
    "comma": (0xBC, 0x33), "period": (0xBE, 0x34), "slash": (0xBF, 0x35),
    "home": (0x24, 0xC7), "end": (0x23, 0xCF), "pgup": (0x21, 0xC9), "pgdn": (0x22, 0xD1),
    "ins": (0x2D, 0xD2), "del": (0x2E, 0xD3),
    "capslock": (0x14, 0x3A), "scrolllock": (0x91, 0x46),
    "pause": (0x13, 0xC5), "prtsc": (0x2C, 0xB7),
})

# Accepted on input, rewritten to the canonical name.
SYNONYMS = {
    "shift": "lshift", "ctrl": "lctrl", "control": "lctrl", "alt": "lalt",
    "escape": "esc", "return": "enter", "bksp": "backspace",
    "pageup": "pgup", "pagedown": "pgdn", "insert": "ins", "delete": "del",
    "capslk": "capslock", "printscreen": "prtsc",
}

BUTTONS = ("lmb", "rmb", "mmb")

_TEXT_PLAIN = {
    " ": "space", "-": "minus", "=": "equals", "[": "lbracket", "]": "rbracket",
    "\\": "backslash", ";": "semicolon", "'": "apostrophe", "`": "grave",
    ",": "comma", ".": "period", "/": "slash", "\n": "enter", "\t": "tab",
}
_TEXT_SHIFTED = {
    "!": "1", "@": "2", "#": "3", "$": "4", "%": "5", "^": "6", "&": "7", "*": "8",
    "(": "9", ")": "0", "_": "minus", "+": "equals", "{": "lbracket", "}": "rbracket",
    "|": "backslash", ":": "semicolon", '"': "apostrophe", "~": "grave",
    "<": "comma", ">": "period", "?": "slash",
}


def suggest(name: str, choices) -> str:
    """Return ' (did you mean 'x'?)' for the nearest choice, or ''."""
    near = difflib.get_close_matches(name, list(choices), n=1, cutoff=0.6)
    return f" (did you mean '{near[0]}'?)" if near else ""


def canonical_key(name: str) -> str:
    """Canonical name for one key; raises ValueError for unknown names."""
    low = name.lower()
    low = SYNONYMS.get(low, low)
    if low in KEYS:
        return low
    raise ValueError(f"unknown key '{name}'{suggest(low, [*KEYS, *SYNONYMS])}")


def canonical_token(token: str) -> str:
    """Canonical key token; chords like alt+f4 keep their press order."""
    parts = token.split("+")
    if any(not p for p in parts):
        raise ValueError(f"bad chord '{token}'")
    names = [canonical_key(p) for p in parts]
    if len(set(names)) != len(names):
        raise ValueError(f"chord '{token}' repeats a key")
    return "+".join(names)


def token_keys(token: str) -> list[str]:
    """Key names in a canonical token."""
    return token.split("+")


def vk(name: str) -> int:
    return KEYS[canonical_key(name)][0]


def dik(name: str) -> int:
    return KEYS[canonical_key(name)][1]


def char_token(ch: str) -> str:
    """Key token that types one character on a US layout."""
    if "a" <= ch <= "z" or "0" <= ch <= "9":
        return ch
    if "A" <= ch <= "Z":
        return "lshift+" + ch.lower()
    if ch in _TEXT_PLAIN:
        return _TEXT_PLAIN[ch]
    if ch in _TEXT_SHIFTED:
        return "lshift+" + _TEXT_SHIFTED[ch]
    raise ValueError(f"cannot type {ch!r} with a US keyboard layout")
