"""Score syntax tree and compiled items.

Sync, Segment, Expect, Relaunch, Mark and Assert serve both as parsed nodes
and as compiled items. In a parsed tree, an option the author left out is
None. Compilation fills it in.
"""

from dataclasses import dataclass, field


class ScoreError(Exception):
    def __init__(self, message: str, source: str = "<string>", line: int = 0, col: int = 0):
        super().__init__(f"{source}:{line}:{col}: {message}")
        self.message = message
        self.source = source
        self.line = line
        self.col = col


def _loc():
    return field(default="<string>", compare=False, kw_only=True)


def _line():
    return field(default=0, compare=False, kw_only=True)


@dataclass(frozen=True, slots=True)
class Note:
    """Hold exactly this input set for `count` ticks.

    held and buttons are down on every tick. mouse_abs holds the cursor there
    and mouse_rel moves it by that delta, on every tick. wheel and osev fire on
    the first tick only. A count of 0 puts its one-shots on the same tick as
    the next note.
    """
    count: int
    held: tuple[str, ...] = ()
    mouse_abs: tuple[int, int] | None = None
    mouse_rel: tuple[int, int] | None = None
    buttons: tuple[str, ...] = ()
    wheel: int = 0
    osev: str | None = None
    source: str = _loc()
    line: int = _line()


@dataclass(frozen=True, slots=True)
class Sync:
    event: str
    arg: str | None = None
    timeout: int | None = None
    source: str = _loc()
    line: int = _line()


@dataclass(frozen=True, slots=True)
class Segment:
    name: str
    hz: int | None = None
    scale: int | None = None
    pace: str | None = None
    source: str = _loc()
    line: int = _line()


@dataclass(frozen=True, slots=True)
class Expect:
    outcome: str  # exit, alive or exit|alive
    timeout: int | None = None
    source: str = _loc()
    line: int = _line()


@dataclass(frozen=True, slots=True)
class Relaunch:
    cond: str | None = None  # exit or always
    source: str = _loc()
    line: int = _line()


@dataclass(frozen=True, slots=True)
class Mark:
    name: str
    source: str = _loc()
    line: int = _line()


@dataclass(frozen=True, slots=True)
class Assert:
    expr: str
    source: str = _loc()
    line: int = _line()


# Parse-only nodes.

@dataclass(frozen=True, slots=True)
class NoteLine:
    """A canonical note as written: keys may include $aliases, and text is unexpanded."""
    count: int
    keys: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    buttons: tuple[str, ...] = ()
    mouse_abs: tuple[int, int] | None = None
    mouse_rel: tuple[int, int] | None = None
    wheel: int = 0
    osev: str | None = None
    text: str | None = None
    source: str = _loc()
    line: int = _line()


@dataclass(frozen=True, slots=True)
class Include:
    path: str
    source: str = _loc()
    line: int = _line()


@dataclass(frozen=True, slots=True)
class Alias:
    name: str
    value: str  # canonical key token or $name
    source: str = _loc()
    line: int = _line()


@dataclass(slots=True)
class Line:
    """One source line: a node, a comment, both, or neither (blank)."""
    node: object | None
    comment: str | None
    lineno: int


@dataclass(slots=True)
class Repeat:
    count: int
    body: list[Line]
    end_comment: str | None = None
    end_line: int = 0
    source: str = _loc()
    line: int = _line()


@dataclass(slots=True)
class ScoreAST:
    source: str
    header: dict[str, str] | None  # None when the file has no #!score line
    header_comment: str | None
    lines: list[Line]


Item = Note | Sync | Segment | Expect | Relaunch | Mark | Assert
