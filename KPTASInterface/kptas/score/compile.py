"""Resolve a score into canonical items: includes, repeats, text, aliases."""

import dataclasses
import hashlib
from pathlib import Path
from typing import Iterator, Sequence

from . import keys as K
from .ast import (Alias, Include, Item, Line, Mark, Note, NoteLine, Relaunch, Repeat,
                  ScoreError, Segment, Sync)
from .fmt import header_line, render
from .include import IncludeStack, read, resolve
from .parser import HEADER_ORDER, parse

# tier and hz stay unset unless the score says so; the run config fills them in.
DEFAULTS = {"game": "k1", "build": "auto", "tier": None, "hz": None,
            "start": None, "render": None, "keymap": None}
DEFAULT_SCALE = 100
DEFAULT_PACE = "gate"
MAX_ITEMS = 5_000_000


class CompiledScore:
    def __init__(self, header: dict, items: list[Item], aliases: dict[str, str], files: list[str]):
        self.header = header
        self.items = items
        self.aliases = aliases
        self.files = files  # every file read, in include order

    def text(self) -> str:
        """Canonical score text. It is itself a valid score that compiles to the same items."""
        known = [k for k in HEADER_ORDER if self.header.get(k) is not None]
        extra = sorted(k for k in self.header if k not in HEADER_ORDER)
        lines = [header_line({k: self.header[k] for k in known + extra})]
        lines += [render(item) for item in self.items]
        return "\n".join(lines) + "\n"

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.text().encode("utf-8")).hexdigest()

    def timeline(self) -> Iterator[tuple[str | None, int, Item]]:
        """Yield (segment name, ticks since the last @sync, item) at each item's start.

        The segment name is None before the first @segment. A @sync is yielded
        with the count it waits after, and the count restarts at 0 after it.
        """
        seg, ticks = None, 0
        for item in self.items:
            if isinstance(item, Segment):
                seg = item.name
            yield seg, ticks, item
            if isinstance(item, Note):
                ticks += item.count
            elif isinstance(item, Sync):
                ticks = 0

    def __repr__(self) -> str:
        return f"<CompiledScore {len(self.items)} items sha256={self.sha256[:12]}>"


def compile_score(src: str | Path, *, include_dirs: Sequence[Path] = ()) -> CompiledScore:
    """Compile a score file. A str that starts with '#!score' or holds a newline is score text."""
    if isinstance(src, str) and (src.lstrip().startswith("#!score") or "\n" in src):
        text, source, path = src, "<string>", None
    else:
        path = Path(src)
        source = str(path)
        text = read(path, source, 0)
    ast = parse(text, source=source)
    if ast.header is None:
        raise ScoreError("missing '#!score v1' header on line 1", source, 1, 1)
    c = _Compiler(ast.header, [Path(d) for d in include_dirs])
    if path is not None:
        c.stack.push(path, source, 0)
        c.files.append(source)
    c.walk(ast.lines, path.parent if path is not None else Path.cwd())
    c.check_marks()
    return CompiledScore(c.header, c.items, c.aliases, c.files)


class _Compiler:
    def __init__(self, header: dict[str, str], include_dirs: list[Path]):
        self.header = dict(DEFAULTS)
        for k, v in header.items():
            self.header[k] = int(v) if k == "hz" else v
        self.include_dirs = include_dirs
        self.items: list[Item] = []
        self.aliases: dict[str, str] = {}
        self.stack = IncludeStack()
        self.files: list[str] = []

    def add(self, item: Item) -> None:
        if len(self.items) >= MAX_ITEMS:
            raise ScoreError(f"score expands past {MAX_ITEMS} items", item.source, item.line, 1)
        self.items.append(item)

    def walk(self, lines: list[Line], base: Path) -> None:
        for ln in lines:
            node = ln.node
            if node is None:
                continue
            if isinstance(node, NoteLine):
                self.note(node)
            elif isinstance(node, Include):
                self.include(node, base)
            elif isinstance(node, Alias):
                self.alias(node)
            elif isinstance(node, Repeat):
                for _ in range(node.count):
                    self.walk(node.body, base)
            elif isinstance(node, Segment):
                self.segment(node)
            elif isinstance(node, Relaunch):
                self.add(dataclasses.replace(node, cond=node.cond or "always"))
            else:
                self.add(node)

    def note(self, n: NoteLine) -> None:
        if n.text is not None:
            for ch in n.text:
                self.add(Note(n.count, (K.char_token(ch),), source=n.source, line=n.line))
            return
        held = set(n.keys)
        for a in n.aliases:
            key = self.lookup(a, n)
            if key in held:
                raise ScoreError(f"'{key}' is held twice (once through {a})", n.source, n.line, 1)
            held.add(key)
        self.add(Note(n.count, tuple(sorted(held)), n.mouse_abs, n.mouse_rel, n.buttons,
                      n.wheel, n.osev, source=n.source, line=n.line))

    def lookup(self, ref: str, node) -> str:
        name = ref[1:]
        if name not in self.aliases:
            hint = K.suggest(ref, ["$" + a for a in self.aliases])
            raise ScoreError(f"undefined alias '{ref}'{hint}", node.source, node.line, 1)
        return self.aliases[name]

    def alias(self, a: Alias) -> None:
        value = self.lookup(a.value, a) if a.value.startswith("$") else a.value
        old = self.aliases.get(a.name)
        if old is not None and old != value:
            raise ScoreError(f"alias ${a.name} is already {old} and cannot become {value}",
                             a.source, a.line, 1)
        self.aliases[a.name] = value

    def segment(self, s: Segment) -> None:
        # Options left out take the header or default value, not the previous segment's.
        hz = s.hz or self.header["hz"]
        scale = s.scale or DEFAULT_SCALE
        if hz is not None and scale * 1_000_000 >= hz * 2**31:
            raise ScoreError(f"scale={scale} at hz={hz} overflows the engine timer "
                             "(scale*1e6/hz must stay below 2^31)", s.source, s.line, 1)
        self.add(Segment(s.name, hz, scale, s.pace or DEFAULT_PACE, source=s.source, line=s.line))

    def include(self, inc: Include, base: Path) -> None:
        path = resolve(inc.path, base, self.include_dirs, inc.source, inc.line)
        self.stack.push(path, inc.source, inc.line)
        try:
            child = parse(read(path, inc.source, inc.line), source=str(path))
            self.files.append(str(path))
            if child.header:
                self.check_header(child.header, child.source)
            self.walk(child.lines, path.parent)
        finally:
            self.stack.pop()

    def check_header(self, h: dict[str, str], source: str) -> None:
        for k in HEADER_ORDER:
            if k in h:
                got = int(h[k]) if k == "hz" else h[k]
                if got != self.header[k]:
                    raise ScoreError(f"included file has {k}={h[k]} but the score has "
                                     f"{k}={self.header[k]}", source, 1, 1)

    def check_marks(self) -> None:
        seen: dict[str, Mark] = {}
        for item in self.items:
            if isinstance(item, Mark):
                if item.name in seen:
                    first = seen[item.name]
                    where = ("again by @repeat or @include" if (first.source, first.line) == (item.source, item.line)
                             else f"at {first.source}:{first.line}")
                    raise ScoreError(f"mark '{item.name}' is already set {where}", item.source, item.line, 1)
                seen[item.name] = item
