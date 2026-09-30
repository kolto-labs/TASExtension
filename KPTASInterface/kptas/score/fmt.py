"""Canonical score text."""

from .ast import (Alias, Assert, Expect, Include, Line, Mark, Note, NoteLine, Relaunch,
                  Repeat, Segment, Sync)
from .lexer import quote
from .parser import HEADER_ORDER, parse

COMMENT_COL = 36  # trailing comments start one column after this


def fmt(text: str, *, source: str = "<string>") -> str:
    """Expand sugar, normalize spacing and keep comments. fmt(fmt(x)) == fmt(x)."""
    ast = parse(text, source=source)
    out: list[str] = []
    if ast.header is not None:
        out.append(_with_comment(header_line(ast.header), ast.header_comment))
    _lines(ast.lines, 0, out)
    res: list[str] = []
    for s in out:
        if s == "" and (not res or res[-1] == ""):
            continue
        res.append(s)
    while res and res[-1] == "":
        res.pop()
    return "\n".join(res) + "\n" if res else ""


def header_line(header: dict) -> str:
    order = [k for k in HEADER_ORDER if k in header] + [k for k in header if k not in HEADER_ORDER]
    return " ".join(["#!score v1", *(f"{k}={header[k]}" for k in order)])


def render(node) -> str:
    """One line of score text for a node or compiled item."""
    match node:
        case Note():
            toks = [*node.held, *_mouse(node), *node.buttons, *_wheel(node), *_osev(node)]
            return _note(node.count, toks)
        case NoteLine():
            text = [quote(node.text)] if node.text is not None else []
            toks = [*node.keys, *_mouse(node), *node.buttons, *node.aliases,
                    *_wheel(node), *text, *_osev(node)]
            return _note(node.count, toks)
        case Include():
            return f"@include {node.path}"
        case Alias():
            return f"@alias {node.name} = {node.value}"
        case Segment():
            return _join("@segment", node.name, _opt("hz", node.hz), _opt("scale", node.scale),
                         _opt("pace", node.pace))
        case Sync():
            return _join("@sync", node.event, node.arg, _opt("timeout", node.timeout))
        case Expect():
            return _join("@expect", node.outcome, _opt("timeout", node.timeout))
        case Relaunch():
            return _join("@relaunch", _opt("if", node.cond))
        case Mark():
            return f"@mark {node.name}"
        case Assert():
            return f"@assert {node.expr}"
        case Repeat():
            return f"@repeat {node.count}"
    raise TypeError(f"cannot render {node!r}")


def _note(count: int, toks: list[str]) -> str:
    return f"{count:<3} {' '.join(toks) or '.'}"


def _mouse(n) -> list[str]:
    out = []
    if n.mouse_abs is not None:
        out.append(f"m@{n.mouse_abs[0]},{n.mouse_abs[1]}")
    if n.mouse_rel is not None:
        out.append(f"md{n.mouse_rel[0]},{n.mouse_rel[1]}")
    return out


def _wheel(n) -> list[str]:
    return [f"wh{n.wheel:+d}"] if n.wheel else []


def _osev(n) -> list[str]:
    return [f"!{n.osev}"] if n.osev else []


def _opt(key: str, value) -> str | None:
    return None if value is None else f"{key}={value}"


def _join(*parts) -> str:
    return " ".join(p for p in parts if p is not None)


def _with_comment(code: str, comment: str | None) -> str:
    if comment is None:
        return code
    if not code.strip():
        return code + "#" + comment
    return code.ljust(COMMENT_COL) + " #" + comment


def _lines(lines: list[Line], depth: int, out: list[str]) -> None:
    indent = "  " * depth
    for ln in lines:
        if ln.node is None:
            out.append("" if ln.comment is None else indent + "#" + ln.comment)
            continue
        out.append(_with_comment(indent + render(ln.node), ln.comment))
        if isinstance(ln.node, Repeat):
            _lines(ln.node.body, depth + 1, out)
            out.append(_with_comment(indent + "@end", ln.node.end_comment))
