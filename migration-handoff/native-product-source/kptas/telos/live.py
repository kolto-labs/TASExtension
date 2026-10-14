"""Live NDJSON frontend and video export for the authoritative TASSession."""
from __future__ import annotations

import json
import os
import select
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from kptas.supervisor import TelosError, listen_socket
from .session import TASSession, Take, DEFAULT_PORT, MANIFEST

# Compatibility name: the command frontend owns the same session type as controls.
LiveSession = TASSession

# ---------------------------------------------------------------- video

def find_manifest(path: str | os.PathLike[str] | None, out_dir: str) -> Path:
    """A session manifest: the path itself, the manifest beside a take, or the newest in out_dir."""
    if path:
        p = Path(path)
        if p.is_file():
            return p
        # A branch take (live-1@b2@120.holo) shares its session's manifest (live-1.session.json).
        m = p.with_name(p.name.split("@", 1)[0].removesuffix(".holo") + MANIFEST) if p.name.endswith(".holo") else None
        if m is not None and m.is_file():
            return m
        raise TelosError(f"{path}: not a live session manifest or a root take with one")
    d = Path(os.path.expanduser(out_dir))
    found = sorted(d.glob(f"live-*{MANIFEST}"), key=lambda q: q.stat().st_mtime)
    if not found:
        raise TelosError(f"no live session in {d}")
    return found[-1]


def stitch(manifest: str | os.PathLike[str], out: str | os.PathLike[str], *, fps: int = 60, crf: int = 18,
           font: str = "monospace") -> str:
    """Encode each take's own ticks (a branch from its branch tick on) and join them in wall order."""
    from kptas.frames import ffmpeg_argv, video
    from kptas.types import BackendError
    takes = json.loads(Path(manifest).read_text())["takes"]
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    parts: list[Path] = []
    lines: list[str] = []
    try:
        for i, t in enumerate(takes):
            if t["last"] < t["first"]:
                continue
            part = out.with_name(f".{out.stem}.part{i}.mp4")
            try:
                lines.append(video(t["path"], part, start=t["first"], end=t["last"], fps=fps, crf=crf, font=font))
            except BackendError as e:
                lines.append(f"{t['path']}: skipped ({e})")
                continue
            parts.append(part)
        if not parts:
            raise TelosError("no take has frames to encode")
        lst = out.with_name(f".{out.stem}.parts.ffconcat")
        lst.write_text("ffconcat version 1.0\n" + "".join(f"file '{p.resolve()}'\n" for p in parts))
        try:
            rc = subprocess.run(ffmpeg_argv("-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0",
                                            "-i", str(lst), "-c", "copy", "-movflags", "+faststart", str(out)),
                                stdin=subprocess.DEVNULL, check=False).returncode
        finally:
            lst.unlink(missing_ok=True)
        if rc != 0:
            raise TelosError(f"ffmpeg concat ended with status {rc}")
    finally:
        for p in parts:
            p.unlink(missing_ok=True)
    return "\n".join([*lines, f"{out}: {len(parts)} take(s) joined in wall order"])


# ---------------------------------------------------------------- server

def handle(s: LiveSession, req: Mapping[str, Any]) -> dict[str, Any]:
    # Analysis jobs (window finder, input search) run in slices on this owner; while one
    # runs only reads and analysis ops are admitted (kptas.analysis.jobs).
    op = req.get("op")
    if isinstance(op, str) and op.startswith("analysis_"):
        from kptas.analysis.jobs import handle_op
        return handle_op(s, req)
    if getattr(s, "_analysis_jobs", None) is not None:
        from kptas.analysis.jobs import guard
        guard(s, op)
    return s.dispatch(req)


def serve(s: LiveSession, host: str = "127.0.0.1", port: int = DEFAULT_PORT, *,
          ready: Callable[[int], None] | None = None) -> None:
    """Serve one session over NDJSON, one client at a time, until a close op. Same framing as gym serve."""
    from kptas.gym import _jsonable
    # This protocol has no authentication. Resolve no arbitrary hostnames and
    # reject wildcard/LAN binds before opening a socket or changing the session.
    import ipaddress
    if host == "localhost":host = "127.0.0.1"
    try:
        address = ipaddress.IPv4Address(host)
    except (ValueError, TypeError):
        raise TelosError("telos live requires localhost or a numeric IPv4 loopback address; "
                         "remote and IPv6 listeners are not supported") from None
    if not address.is_loopback:
        raise TelosError("telos live has no authentication and only accepts IPv4 loopback listeners")
    srv = listen_socket(str(address), port)
    from .preview_channel import PreviewChannel
    preview = None
    try:
        # Same session, separate read-only socket: long Notes cannot starve pixels.
        try:
            preview = PreviewChannel(s, str(address), srv.getsockname()[1] + 1)
        except (OSError, OverflowError) as exc:
            print(f'telos preview unavailable: {exc}', file=sys.stderr)
        if preview is not None:
            s._presentation_channel = preview
            preview.attach()
        if ready:ready(srv.getsockname()[1])
        input_due = 0.0
        scheduler_identity = None
        command_served = False
        from .run_queue import BatchSizer
        # Authored runs batch frames of one Note only while behind schedule;
        # at or ahead of real time every frame keeps its own due time.
        run_batches = BatchSizer(max_wall_s=0.5)
        import inspect
        batched_runs = 'max_frames' in inspect.signature(s.run_scheduler_step).parameters
        # Capture cadence: pump frames always capture their gate (capture_last=True, the
        # default), so wherever a pause lands it is a restore point. Only Play's step(k)
        # batches leave their inner ticks off the capture.every grid.

        def scheduler():
            jobs = getattr(s, '_analysis_jobs', None)
            if jobs is not None and jobs['active'] is not None and jobs['active'].status == 'running':
                return ('analysis', jobs['active'].id)
            if s.run_scheduler_running:
                return ('run', s._run_queue.current.run_id)
            if s.input_scheduler_running:
                return ('input', s.engaged, id(s._engagement_resume_note))
            return None

        def advance(identity):
            """Run one scheduler turn; return the frames it acknowledged (min 1)."""
            try:
                if identity[0] == 'analysis':
                    from kptas.analysis.jobs import advance as analysis_advance
                    analysis_advance(s)   # one owner operation per slice; never raises
                    return 1
                if identity[0] == 'run':
                    run = s._run_queue.current
                    before = run.executed_frames
                    behind = time.monotonic() - input_due > 1 / max(1, s.sup.hz)
                    started = time.perf_counter()
                    if batched_runs:
                        s.run_scheduler_step(run_batches.ticks if behind else 1)
                    else:
                        s.run_scheduler_step()
                    frames = run.executed_frames - before
                    if frames > 0:
                        run_batches.record(frames, time.perf_counter() - started)
                    return max(1, frames)
                s.input_scheduler_step()
            except Exception as exc:
                print(f'telos {identity[0]} input parked: {exc}', file=sys.stderr, flush=True)
            return 1

        def active_line(conn):
            # An accepted partial client gets a bounded transport window, not
            # a way to stop the frame pump by dripping bytes indefinitely.
            deadline = time.monotonic() + 0.05
            data = bytearray()
            while len(data) <= 8 * 1024 * 1024:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not select.select([conn], [], [], remaining)[0]:
                    raise TimeoutError('incomplete command at scheduler boundary')
                chunk = conn.recv(65536)
                if not chunk:
                    return bytes(data)
                data.extend(chunk)
                if b'\n' in chunk:
                    return bytes(data).split(b'\n', 1)[0]
            raise TelosError('command exceeds 8 MiB')

        while not s.closed:
            identity = scheduler()
            running = identity is not None
            if identity != scheduler_identity:
                scheduler_identity = identity
                input_due = time.monotonic()
                command_served = False
            # Give at most one command priority per due frame. Even an endless
            # sequence of status clients therefore cannot starve execution.
            if running and command_served and time.monotonic() >= input_due:
                input_due += advance(identity) / max(1, s.sup.hz)
                command_served = False
                continue
            srv.settimeout(max(0.0, input_due - time.monotonic()) if running else None)
            try:
                conn, _ = srv.accept()
            except (TimeoutError, BlockingIOError):
                # Absolute deadlines do not add a frame interval after costly
                # captures. Behind schedule, keep every frame and its barrier;
                # never skip inputs or reduce capture work to catch up.
                input_due += advance(identity) / max(1, s.sup.hz)
                command_served = False
                continue
            try:
                # An idle/partial client must not indefinitely freeze normal
                # play. An active owner operation itself retains its full limit.
                if running:
                    conn.settimeout(0.05)
                with conn, conn.makefile("rb") as rf:
                    lines = [active_line(conn)] if running else iter(lambda: rf.readline(8 * 1024 * 1024 + 1), b'')
                    for line in lines:
                        if not line.strip():
                            continue
                        rid = None
                        try:
                            if len(line) > 8 * 1024 * 1024:
                                raise TelosError('command exceeds 8 MiB')
                            req = json.loads(line)
                            rid = req.get("id")
                            out = {"id": rid, "ok": True, "result": _jsonable(handle(s, req))}
                        except Exception as e:
                            out = {"id": rid, "ok": False, "error": {"code": type(e).__name__, "message": str(e)}}
                        conn.sendall(json.dumps(out, separators=(",", ":")).encode() + b"\n")
                        if s.closed:
                            break
                        if scheduler() is not None:
                            break  # Resume queued/physical frames after this acknowledgement.
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError, TelosError):
                # A lost acknowledgement must not close the owner. The caller
                # must reconcile explicitly before more input. Command errors
                # above still produce refusals; no command is retried here.
                continue
            finally:
                command_served = True
    finally:
        if preview is not None:
            preview.close()
        srv.close()
        try:
            s.close()
        except Exception as e:
            print(f"telos live: close: {e}", file=sys.stderr)


__all__ = ["DEFAULT_PORT", "LiveSession", "Take", "find_manifest", "handle", "serve", "stitch"]
