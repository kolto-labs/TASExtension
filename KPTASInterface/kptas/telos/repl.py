"""telos REPL: author a score against a live backend, one line at a time.

Score lines (notes such as '4 w lmb', and @directives) run immediately and are
kept in the authored score when they succeed. @include and @alias lines join a
prelude that every later line compiles against. Anything else is a telos
command, parsed by the same argparse tree as the CLI.
"""

from __future__ import annotations

import shlex
import sys
from pathlib import Path
from typing import Any, TextIO

from kptas.score import ScoreError, compile_score
from kptas.supervisor import RunError, Supervisor, TelosError
from kptas.telos.cli import Ctx, dispatch, err, out, stream

HELP = """\
score lines   4 w lmb | . | @sync area.ready | @mark name | @segment s hz=30 | @relaunch ...
prelude       @include ../lib/k1-keymap.score | @alias name = keys
start         start the backend (first score line starts it too)
stop          end the session (bye)
score         print the authored score
save PATH     write the authored score
undo          drop the last authored line from the buffer (ticks already ran)
status        tick, epoch, backend
help, quit    this text; leave
other lines   telos commands, e.g. show run.holo --to 50, score check x.score"""


class Repl:
    def __init__(self, ctx: Ctx, *, backend: str | None = None, attach: bool = False,
                 stdin: TextIO | None = None):
        self.ctx, self.backend, self.attach = ctx, backend, attach
        self.stdin = stdin or sys.stdin
        self.sup: Supervisor | None = None
        self.prelude: list[str] = []
        self.lines: list[str] = []
        self.header = "#!score v1"
        self.zero: list[str] = []      # count-0 notes waiting for the next note
        self.show = stream()

    # session
    def _sup(self) -> Supervisor:
        if self.sup is None:
            cfg = self.ctx.cfg(run__backend=self.backend)
            self.sup = Supervisor(cfg, attach=self.attach, run_name="repl", log=err)
        return self.sup

    def start(self) -> None:
        sup = self._sup()
        if sup.backend is None:
            self.show(sup.open(), None)

    def stop(self) -> None:
        if self.sup is not None:
            self.sup.close()
            self.sup = None
            out("session closed")

    def compiled(self, extra: list[str]) -> Any:
        dirs = self.ctx.include_dirs(self.ctx.cfg())
        return compile_score("\n".join([self.header, *self.prelude, *extra]) + "\n", include_dirs=dirs)

    def score_line(self, line: str) -> None:
        head = line.split(None, 1)[0]
        if head in ("@include", "@alias"):
            before = len(self.compiled([]).items)
            self.prelude.append(line)
            try:
                items = self.compiled([]).items[before:]
            except ScoreError:
                self.prelude.pop()
                raise
            if items:
                self.prelude.pop()
                raise TelosError(f"{head} brought in {len(items)} runnable items; "
                                 "include only alias files in the prelude")
            self.lines.append(line)
            return
        base = len(self.compiled([]).items)
        c = self.compiled([*self.zero, line])
        c.items = c.items[base:]
        if [type(i).__name__ for i in c.items] == ["Note"] * len(c.items) and c.items[-1].count == 0:
            self.zero.append(line)
            out("count-0 note held for the next note")
            return
        sup = self._sup()
        if sup.backend is None:
            self.start()
        sup.run_score(c, on_report=self.show)
        self.lines += [*self.zero, line]
        self.zero = []

    def authored(self) -> str:
        return "\n".join([self.header, *self.lines]) + "\n"

    # loop
    def handle(self, line: str) -> bool:
        """Run one line. False means quit."""
        line = line.strip()
        if not line or line.startswith("#"):
            return True
        word = line.split(None, 1)[0]
        rest = line[len(word):].strip()
        if word in ("quit", "exit"):
            return False
        if word == "help":
            out(HELP)
        elif word == "start":
            self.start()
        elif word == "stop":
            self.stop()
        elif word == "score" and not rest:
            sys.stdout.write(self.authored())
        elif word == "save":
            if not rest:
                raise TelosError("save needs a path")
            Path(rest).write_text(self.authored(), encoding="utf-8")
            out(f"wrote {rest}")
        elif word == "undo":
            out(f"dropped: {self.lines.pop()}" if self.lines else "nothing to drop")
        elif word == "status":
            s = self.sup
            out("no session" if s is None or s.backend is None else
                f"tick {s.tick} epoch {s.epoch} backend {s.name} tier {s.tier} "
                f"{'alive' if s.alive else 'exited'}")
        elif word[0].isdigit() or word == "." or word.startswith("@"):
            self.score_line(line)
        else:
            try:
                argv = shlex.split(line)
            except ValueError as e:
                raise TelosError(str(e)) from None
            dispatch(argv, self.ctx)
        return True

    def loop(self) -> int:
        tty = self.stdin.isatty()
        if tty:
            out("telos repl; 'help' lists commands")
        try:
            while True:
                if tty:
                    sys.stdout.write("telos> ")
                    sys.stdout.flush()
                line = self.stdin.readline()
                if not line:
                    break
                try:
                    if not self.handle(line):
                        break
                except (RunError, TelosError, ScoreError) as e:
                    err(str(e))
                except (ValueError, OSError) as e:
                    err(f"{type(e).__name__}: {e}")
                except KeyboardInterrupt:
                    err("interrupted")
        finally:
            self.stop()
        return 0


__all__ = ["HELP", "Repl"]
