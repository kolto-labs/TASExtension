"""Split score lines into code, comment and tokens."""

from dataclasses import dataclass

from .ast import ScoreError


@dataclass(frozen=True, slots=True)
class Token:
    text: str
    col: int  # 1-based


def split_comment(line: str, source: str, lineno: int) -> tuple[str, str | None]:
    """Return (code, comment text after '#'). A '#' inside quotes is text."""
    quoted = False
    i = 0
    while i < len(line):
        ch = line[i]
        if quoted and ch == "\\":
            i += 2
            continue
        if ch == '"':
            quoted = not quoted
        elif ch == "#" and not quoted:
            return line[:i], line[i + 1:].rstrip()
        i += 1
    if quoted:
        raise ScoreError("unterminated text: missing closing '\"'", source, lineno, line.index('"') + 1)
    return line, None


def tokenize(code: str, source: str, lineno: int) -> list[Token]:
    tokens = []
    i, n = 0, len(code)
    while i < n:
        if code[i].isspace():
            i += 1
            continue
        start = i
        if code[i] == '"':
            i += 1
            while i < n and code[i] != '"':
                i += 2 if code[i] == "\\" else 1
            i += 1
            if i < n and not code[i].isspace():
                raise ScoreError("text must be followed by a space", source, lineno, i + 1)
        else:
            while i < n and not code[i].isspace():
                if code[i] == '"':
                    raise ScoreError("'\"' inside a token", source, lineno, i + 1)
                i += 1
        tokens.append(Token(code[start:i], start + 1))
    return tokens


_ESCAPES = {'"': '"', "\\": "\\", "n": "\n", "t": "\t"}


def unquote(tok: Token, source: str, lineno: int) -> str:
    body, out, i = tok.text[1:-1], [], 0
    while i < len(body):
        ch = body[i]
        if ch == "\\":
            nxt = body[i + 1] if i + 1 < len(body) else ""
            if nxt not in _ESCAPES:
                raise ScoreError(f"unknown escape '\\{nxt}' in text", source, lineno, tok.col + i + 1)
            out.append(_ESCAPES[nxt])
            i += 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def quote(text: str) -> str:
    rev = {v: k for k, v in _ESCAPES.items()}
    return '"' + "".join("\\" + rev[c] if c in rev else c for c in text) + '"'
