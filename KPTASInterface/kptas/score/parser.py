"""Parse one score file into a ScoreAST. Sugar (+k -k ~k) becomes full notes here."""

import re

from . import keys as K
from .ast import (Alias, Assert, Expect, Include, Line, Mark, NoteLine, Relaunch,
                  Repeat, ScoreAST, ScoreError, Segment, Sync)
from .lexer import Token, split_comment, tokenize, unquote

MAGIC = "#!score"
HEADER_ORDER = ("game", "build", "tier", "hz", "start", "render", "keymap")
TIERS = ("lab", "tas", "hands-off")
GAMES = ("k1", "k2")
SYNC_EVENTS = ("main_menu", "area.ready", "load.done", "save.begin", "save.done",
               "dialog.open", "dialog.close", "gui.open", "proc.down", "proc.up")
SYNC_WITH_ARG = ("gui.open",)
OUTCOMES = {"exit": "exit", "alive": "alive", "exit|alive": "exit|alive", "alive|exit": "exit|alive"}
OSEVENTS = ("altf4", "focus", "blur")
PACES = ("gate", "realtime")
DIRECTIVES = ("include", "alias", "segment", "sync", "expect", "relaunch",
              "mark", "assert", "repeat", "end")

_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_LABEL = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.\-]*\Z")
_INT = re.compile(r"[0-9]+\Z")
_HEADKEY = re.compile(r"[a-z_][a-z0-9_.\-]*\Z")
_MABS = re.compile(r"m@([0-9]+),([0-9]+)\Z")
_MREL = re.compile(r"md(-?[0-9]+),(-?[0-9]+)\Z")
_WHEEL = re.compile(r"wh([+-][0-9]+)\Z")


def parse(text: str, *, source: str = "<string>") -> ScoreAST:
    return _Parser(source).parse(text)


class _Parser:
    def __init__(self, source: str):
        self.source = source
        # Held set of the previous note, for sugar. None after @include: unknown here.
        self.held: frozenset[str] | None = frozenset()

    def err(self, msg: str, line: int, col: int = 1) -> ScoreError:
        return ScoreError(msg, self.source, line, col)

    def parse(self, text: str) -> ScoreAST:
        raw = text.removeprefix("\ufeff").splitlines()
        header = hcomment = None
        first = 0
        if raw and raw[0].lstrip().startswith(MAGIC):
            header, hcomment = self.header(raw[0])
            first = 1
        root: list[Line] = []
        stack: list[tuple[Repeat | None, list[Line]]] = [(None, root)]
        for idx in range(first, len(raw)):
            lineno = idx + 1
            if raw[idx].lstrip().startswith(MAGIC):
                raise self.err("the '#!score' header must be the first line", lineno)
            code, comment = split_comment(raw[idx], self.source, lineno)
            toks = tokenize(code, self.source, lineno)
            body = stack[-1][1]
            if not toks:
                body.append(Line(None, comment, lineno))
                continue
            head = toks[0]
            if head.text == "@end":
                if len(toks) > 1:
                    raise self.err("@end takes no arguments", lineno, toks[1].col)
                if len(stack) == 1:
                    raise self.err("@end without a matching @repeat", lineno, head.col)
                rep = stack.pop()[0]
                rep.end_comment, rep.end_line = comment, lineno
                continue
            node = self.statement(toks, code, lineno)
            body.append(Line(node, comment, lineno))
            if isinstance(node, Repeat):
                stack.append((node, node.body))
        if len(stack) > 1:
            raise self.err("@repeat without a matching @end", stack[-1][0].line)
        return ScoreAST(self.source, header, hcomment, root)

    def header(self, line: str) -> tuple[dict[str, str], str | None]:
        off = line.index(MAGIC) + len(MAGIC)
        code, comment = split_comment(line[off:], self.source, 1)
        toks = tokenize(code, self.source, 1)
        if not toks or toks[0].text != "v1":
            raise self.err("expected '#!score v1'", 1, toks[0].col + off if toks else off + 1)
        hdr: dict[str, str] = {}
        for t in toks[1:]:
            col = t.col + off
            k, eq, v = t.text.partition("=")
            if not eq or not v or not _HEADKEY.match(k):
                raise self.err(f"header entries are key=value, got '{t.text}'", 1, col)
            if k in hdr:
                raise self.err(f"header key '{k}' is given twice", 1, col)
            if k == "hz":
                v = str(self.int_arg(v, "hz", 1, col))
            elif k == "tier" and v not in TIERS:
                raise self.err(f"tier must be one of {', '.join(TIERS)}, got '{v}'", 1, col)
            elif k == "game" and v not in GAMES:
                raise self.err(f"game must be one of {', '.join(GAMES)}, got '{v}'", 1, col)
            hdr[k] = v
        return hdr, comment

    def int_arg(self, text: str, what: str, line: int, col: int, minimum: int = 1) -> int:
        if not _INT.match(text) or int(text) < minimum:
            raise self.err(f"{what} must be a whole number >= {minimum}, got '{text}'", line, col)
        return int(text)

    def statement(self, toks: list[Token], code: str, lineno: int):
        head = toks[0]
        if head.text.startswith("@"):
            return self.directive(toks, code, lineno)
        if head.text[0].isdigit():
            return self.note(toks, lineno)
        raise self.err(f"expected a note (tick count first) or an @directive, got '{head.text}'",
                       lineno, head.col)

    # Directives

    def options(self, toks: list[Token], allowed: tuple[str, ...], lineno: int) -> dict[str, tuple[str, int]]:
        out: dict[str, tuple[str, int]] = {}
        for t in toks:
            k, eq, v = t.text.partition("=")
            if not eq:
                raise self.err(f"expected key=value, got '{t.text}'", lineno, t.col)
            if k not in allowed:
                raise self.err(f"unknown option '{k}'{K.suggest(k, allowed)}", lineno, t.col)
            if k in out:
                raise self.err(f"option '{k}' is given twice", lineno, t.col)
            out[k] = (v, t.col)
        return out

    def timeout(self, opts: dict, lineno: int) -> int | None:
        if "timeout" not in opts:
            return None
        v, col = opts["timeout"]
        return self.int_arg(v, "timeout", lineno, col)

    def need(self, toks: list[Token], what: str, lineno: int) -> Token:
        if len(toks) < 2:
            raise self.err(f"{toks[0].text} needs {what}", lineno, toks[0].col + len(toks[0].text))
        return toks[1]

    def only(self, toks: list[Token], n: int, lineno: int) -> None:
        if len(toks) > n:
            raise self.err(f"unexpected '{toks[n].text}' after {toks[0].text}", lineno, toks[n].col)

    def directive(self, toks: list[Token], code: str, lineno: int):
        head = toks[0]
        name = head.text[1:]
        if name not in DIRECTIVES:
            raise self.err(f"unknown directive '{head.text}'{K.suggest(name, DIRECTIVES)}", lineno, head.col)
        loc = {"source": self.source, "line": lineno}
        rest = code[head.col - 1 + len(head.text):].strip()

        if name == "include":
            path = self.need(toks, "a path", lineno)
            self.only(toks, 2, lineno)
            self.held = None
            return Include(path.text, **loc)

        if name == "alias":
            return self.alias(rest, code, lineno, loc)

        if name == "segment":
            label = self.need(toks, "a name", lineno)
            if not _LABEL.match(label.text):
                raise self.err(f"bad segment name '{label.text}'", lineno, label.col)
            opts = self.options(toks[2:], ("hz", "scale", "pace"), lineno)
            hz = scale = pace = None
            if "hz" in opts:
                hz = self.int_arg(opts["hz"][0], "hz", lineno, opts["hz"][1])
            if "scale" in opts:
                scale = self.int_arg(opts["scale"][0], "scale", lineno, opts["scale"][1])
            if "pace" in opts:
                pace, col = opts["pace"]
                if pace not in PACES:
                    raise self.err(f"pace must be gate or realtime, got '{pace}'", lineno, col)
            return Segment(label.text, hz, scale, pace, **loc)

        if name == "sync":
            ev = self.need(toks, "an event", lineno)
            if ev.text not in SYNC_EVENTS:
                raise self.err(f"unknown sync event '{ev.text}'{K.suggest(ev.text, SYNC_EVENTS)}",
                               lineno, ev.col)
            more, arg = toks[2:], None
            if ev.text in SYNC_WITH_ARG:
                if not more or "=" in more[0].text:
                    raise self.err(f"{ev.text} needs a panel name", lineno, ev.col + len(ev.text))
                if not _LABEL.match(more[0].text):
                    raise self.err(f"bad panel name '{more[0].text}'", lineno, more[0].col)
                arg, more = more[0].text, more[1:]
            elif more and "=" not in more[0].text:
                raise self.err(f"{ev.text} takes no argument", lineno, more[0].col)
            opts = self.options(more, ("timeout",), lineno)
            return Sync(ev.text, arg, self.timeout(opts, lineno), **loc)

        if name == "expect":
            out = self.need(toks, "exit, alive or exit|alive", lineno)
            if out.text not in OUTCOMES:
                raise self.err(f"@expect takes exit, alive or exit|alive, got '{out.text}'", lineno, out.col)
            opts = self.options(toks[2:], ("timeout",), lineno)
            return Expect(OUTCOMES[out.text], self.timeout(opts, lineno), **loc)

        if name == "relaunch":
            opts = self.options(toks[1:], ("if",), lineno)
            cond = None
            if "if" in opts:
                cond, col = opts["if"]
                if cond not in ("exit", "always"):
                    raise self.err(f"if= takes exit or always, got '{cond}'", lineno, col)
            return Relaunch(cond, **loc)

        if name == "mark":
            label = self.need(toks, "a name", lineno)
            self.only(toks, 2, lineno)
            if not _LABEL.match(label.text):
                raise self.err(f"bad mark name '{label.text}'", lineno, label.col)
            return Mark(label.text, **loc)

        if name == "assert":
            if not rest:
                raise self.err("@assert needs an expression", lineno, head.col + len(head.text))
            return Assert(rest, **loc)

        # repeat; @end is handled by the caller
        n = self.need(toks, "a count", lineno)
        self.only(toks, 2, lineno)
        return Repeat(self.int_arg(n.text, "repeat count", lineno, n.col, minimum=0), [], **loc)

    def alias(self, rest: str, code: str, lineno: int, loc: dict) -> Alias:
        name, eq, value = rest.partition("=")
        name, value = name.strip().removeprefix("$"), value.strip()
        col = code.find("@alias") + 1
        if not eq or not name or not value:
            raise self.err("expected '@alias NAME = KEY'", lineno, col)
        if not _NAME.match(name):
            raise self.err(f"bad alias name '{name}'", lineno, col)
        vcol = code.rfind(value) + 1
        if any(ch.isspace() for ch in value):
            raise self.err("an alias names a single key or chord", lineno, vcol)
        if value.startswith("$"):
            if not _NAME.match(value[1:]):
                raise self.err(f"bad alias reference '{value}'", lineno, vcol)
        else:
            value = self.key(value, lineno, vcol)
        return Alias(name, value, **loc)

    # Notes

    def key(self, text: str, lineno: int, col: int) -> str:
        try:
            return K.canonical_token(text)
        except ValueError as e:
            raise self.err(str(e), lineno, col) from None

    def target(self, text: str, lineno: int, col: int) -> str:
        """Canonical key token, $alias or mouse button."""
        if not text:
            raise self.err("missing key after sugar prefix", lineno, col)
        if text.startswith("$"):
            if not _NAME.match(text[1:]):
                raise self.err(f"bad alias reference '{text}'", lineno, col)
            return text
        if text.lower() in K.BUTTONS:
            return text.lower()
        if text.startswith("m@"):
            raise self.err(f"mouse position is written m@X,Y, got '{text}'", lineno, col)
        if text.startswith("md"):
            raise self.err(f"mouse delta is written mdX,Y, got '{text}'", lineno, col)
        if text[:3] in ("wh+", "wh-"):
            raise self.err(f"wheel is written wh+N or wh-N, got '{text}'", lineno, col)
        return self.key(text, lineno, col)

    def note(self, toks: list[Token], lineno: int) -> NoteLine:
        ctok = toks[0]
        if not _INT.match(ctok.text):
            raise self.err(f"tick count must be a whole number, got '{ctok.text}'", lineno, ctok.col)
        count = int(ctok.text)
        rest = toks[1:]
        if not rest:
            raise self.err("a note needs inputs after the count ('.' for nothing held)",
                           lineno, ctok.col + len(ctok.text))
        plain: list[tuple[str, Token]] = []
        sugar: list[tuple[str, str, Token]] = []
        once: dict[str, object] = {}
        text = dot = None

        def set_once(field: str, value: object, t: Token) -> None:
            if field in once:
                raise self.err(f"only one {field.replace('_', ' ')} per note", lineno, t.col)
            once[field] = value

        for t in rest:
            s = t.text
            if s == ".":
                dot = t
            elif s[0] in "+-~":
                sugar.append((s[0], self.target(s[1:], lineno, t.col + 1), t))
            elif s[0] == '"':
                if text is not None:
                    raise self.err("only one text per note", lineno, t.col)
                text = (unquote(t, self.source, lineno), t)
            elif s[0] == "!":
                if s[1:] not in OSEVENTS:
                    raise self.err(f"unknown OS event '{s}'{K.suggest(s[1:], OSEVENTS)}", lineno, t.col)
                set_once("osev", s[1:], t)
            elif m := _MABS.match(s):
                set_once("mouse_abs", (int(m[1]), int(m[2])), t)
            elif m := _MREL.match(s):
                set_once("mouse_rel", (int(m[1]), int(m[2])), t)
            elif m := _WHEEL.match(s):
                if int(m[1]) == 0:
                    raise self.err("wheel impulse must not be zero", lineno, t.col)
                set_once("wheel", int(m[1]), t)
            else:
                tok = self.target(s, lineno, t.col)
                if any(tok == p for p, _ in plain):
                    raise self.err(f"'{tok}' is listed twice", lineno, t.col)
                plain.append((tok, t))

        loc = {"source": self.source, "line": lineno}
        if dot is not None and len(rest) > 1:
            raise self.err("'.' means nothing held and stands alone", lineno, dot.col)
        if text is not None:
            txt, t = text
            if len(rest) > 1:
                raise self.err("text must be the only input in its note", lineno, t.col)
            self.check_text(txt, count, lineno, t.col)
            self.held = frozenset()
            return NoteLine(count, text=txt, **loc)

        if sugar:
            if plain:
                raise self.err("a note with +k, -k or ~k cannot also list plain keys; "
                               "use a full note or sugar only", lineno, plain[0][1].col)
            if self.held is None:
                raise self.err("the held set is unknown after @include; write a full note before using sugar",
                               lineno, sugar[0][2].col)
            running, transient, seen = set(self.held), set(), set()
            for op, tok, t in sugar:
                if tok in seen:
                    raise self.err(f"'{tok}' appears twice in one note", lineno, t.col)
                seen.add(tok)
                if op == "-":
                    if tok not in running:
                        raise self.err(f"cannot release '{tok}': it is not held", lineno, t.col)
                    running.discard(tok)
                elif tok in running:
                    raise self.err(f"'{tok}' is already held", lineno, t.col)
                elif op == "+":
                    running.add(tok)
                else:
                    transient.add(tok)
            self.held = frozenset(running)
            held = running | transient
        else:
            held = {p for p, _ in plain}
            self.held = frozenset(held)

        return NoteLine(
            count,
            keys=tuple(sorted(h for h in held if h[0] != "$" and h not in K.BUTTONS)),
            aliases=tuple(sorted(h for h in held if h[0] == "$")),
            buttons=tuple(b for b in K.BUTTONS if b in held),
            **once, **loc)

    def check_text(self, txt: str, count: int, lineno: int, col: int) -> None:
        if count < 1:
            raise self.err("text needs a tick count of at least 1", lineno, 1)
        if not txt:
            raise self.err("empty text", lineno, col)
        prev = None
        for ch in txt:
            try:
                base = K.char_token(ch).split("+")[-1]
            except ValueError as e:
                raise self.err(str(e), lineno, col) from None
            # Notes are stateless: the same key on two ticks in a row is one press.
            if base == prev:
                raise self.err(f"text types {ch!r} on a key that is still down from the previous "
                               "character; split the text around a '1 .' note", lineno, col)
            prev = base
