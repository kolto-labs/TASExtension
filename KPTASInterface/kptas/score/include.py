"""Find @include targets and catch include cycles."""

from pathlib import Path
from typing import Sequence

from .ast import ScoreError


def resolve(path: str, base: Path | None, include_dirs: Sequence[Path],
            source: str, line: int) -> Path:
    """Look next to the including file first, then in each include dir."""
    p = Path(path)
    if p.is_absolute():
        candidates = [p]
    else:
        candidates = ([base / p] if base is not None else []) + [Path(d) / p for d in include_dirs]
    for c in candidates:
        if c.is_file():
            return c
    looked = ", ".join(str(c) for c in candidates)
    raise ScoreError(f"cannot find include '{path}' (looked for {looked})", source, line, 1)


def read(path: Path, source: str, line: int) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise ScoreError(f"cannot read '{path}': {e}", source, line, 1) from None


class IncludeStack:
    """Files currently being expanded. A file may appear twice in a score, but not inside itself."""

    def __init__(self) -> None:
        self._keys: list[Path] = []
        self._names: list[str] = []

    def push(self, path: Path, source: str, line: int) -> None:
        key = path.resolve()
        if key in self._keys:
            chain = [*self._names[self._keys.index(key):], str(path)]
            raise ScoreError("include cycle: " + " -> ".join(chain), source, line, 1)
        self._keys.append(key)
        self._names.append(str(path))

    def pop(self) -> None:
        self._keys.pop()
        self._names.pop()
