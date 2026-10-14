"""In-game tool hotkeys, using the existing stopped gate and public seek path.

No second game-input source: step plays the recorded note, rewind restores a
recorded point, and branch continues the same verified stopped process. This
controller is restricted to shim; hands-off has no native gate or hotkeys.
"""
from __future__ import annotations

from collections import OrderedDict, deque
from contextlib import ExitStack, nullcontext
from copy import deepcopy
import json
import hashlib
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from kptas.holo.seek import resume_cursor, seek
from kptas.score import Assert, Mark, Note, SaveMs, Segment, ScoreError, compile_score
from kptas.supervisor import BundleSink, RunError, Supervisor, TelosError
from kptas.types import BackendError, EngineEvent, SeekMode, VerifyLevel
from .session import TASSession
from kptas.config import unit_name as _unit_name

# Deprecated import name; there is no second controller implementation.
Controls = TASSession


def control_transaction(control, active=True):
    return control.operation() if active and isinstance(control, TASSession) else nullcontext()


def finish_branch(control, sink, *, seal=False):
    """Deprecated call shape; branch persistence is owned by TASSession."""
    return TASSession.finish_branch(control, sink, seal=seal)


class Recorder:
    """The player's view as a video, in wall-clock order.

    The controller calls show() at its stopped gate after every action and every played tick, with the
    native view and the actual visible workspace. Historical previews are explicitly labelled and
    separately sourced in the per-frame ledger. A writer thread feeds ffmpeg at a fixed rate and repeats
    the latest view, so a blocking
    seek, canary or relaunch is a hold of its real length. events.jsonl beside the video names what
    happened when. Output only: it reads the view, never the game.
    """

    def __init__(self, path: str | Path, fps: int = 60):
        self.path, self.fps = Path(path), fps
        self.path.parent.mkdir(parents=True, exist_ok=True)
        outputs = [self.path, *(self.path.with_suffix(suffix) for suffix in
                   (".events.jsonl", ".frames.jsonl", ".ffmpeg.log", ".latest.png"))]
        for output in outputs:
            if output.exists() or output.is_symlink():
                raise RunError(f"recording output already exists: {output}")
        self.view: bytes | None = None
        self.size: tuple[int, int] | None = None
        self.frames = 0
        self.t0 = time.monotonic()
        self.lock = threading.RLock()
        self.metadata = {}
        self.error = None
        self.publications = deque()
        self.started = None
        self.stop_time = None
        self.write_started = None
        self.write_timeout = 5.0
        self.closed = False
        self.watchdog = None
        self.done = threading.Event()
        self.proc: subprocess.Popen[bytes] | None = None
        self.thread: threading.Thread | None = None
        self.log = open(self.path.with_suffix(".events.jsonl"), "x")
        try:
            self.frame_log = open(self.path.with_suffix(".frames.jsonl"), "x")
        except BaseException:
            # Keep newly created bytes, but never leak an open ledger when a
            # competing writer claimed the second output after admission.
            self.log.close()
            raise

    def event(self, what: str, **fields: Any) -> None:
        line = {"t": round(time.monotonic() - self.t0, 3), "video_s": round(self.frames / self.fps, 3),
                "event": what, **fields}
        self.log.write(json.dumps(line) + "\n")
        self.log.flush()

    def _open(self, w: int, h: int, clock: bool = True) -> None:
        from kptas.frames import ffmpeg_argv
        argv = ffmpeg_argv("-hide_banner", "-loglevel", "error", "-n", "-f", "rawvideo", "-pix_fmt", "bgra",
                           "-s", f"{w}x{h}", "-framerate", str(self.fps), "-i", "pipe:0",
                           "-c:v", "libx264", "-preset", "veryfast", "-crf", "16", "-pix_fmt", "yuv420p",
                           "-movflags", "+faststart", str(self.path))
        try:
            self.err = open(self.path.with_suffix(".ffmpeg.log"), "xb")
            self.proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=self.err, bufsize=0)
        except BaseException as exc:
            self.error = exc
            if getattr(self, 'err', None) is not None:
                self.err.close()
            raise
        self.size = (w, h)
        if clock:
            self.thread = threading.Thread(target=self._run, name="control-video", daemon=True)
            self.thread.start()
            self.watchdog = threading.Thread(target=self._watch, name="control-video-watch", daemon=True)
            self.watchdog.start()

    def _fit(self, top_down: bytes, w: int, h: int) -> bytes:
        """The first view's size is the video's; another size is cropped or padded with black."""
        assert self.size is not None
        W, H = self.size
        if (w, h) == (W, H):
            return top_down
        out = bytearray(W * H * 4)
        for y in range(min(h, H)):
            n = min(w, W) * 4
            out[y * W * 4:y * W * 4 + n] = top_down[y * w * 4:y * w * 4 + n]
        return bytes(out)

    def show(self, bgra_bottom_up: bytes, w: int, h: int) -> None:
        stride = w * 4
        top = b"".join(bgra_bottom_up[(h - 1 - y) * stride:(h - y) * stride] for y in range(h))
        self.show_top_down(top, w, h)

    def show_top_down(self, top: bytes, w: int, h: int, *, metadata=None) -> None:
        with self.lock:
            if self.closed or self.stop_time is not None:
                raise RunError("recording is closed")
            if self.error is not None:
                raise RunError(f"video writer failed: {self.error}")
            if self.proc is None:
                self._open(w, h)
            self.view = bytes(self._fit(top, w, h))
            self.metadata = deepcopy(metadata or {})
            self.metadata["raw_bgra_sha256"] = hashlib.sha256(self.view).hexdigest()
            stamp = time.monotonic()
            if self.started is None:
                self.started = stamp
            if len(self.publications) >= 4096 or len(self.publications) * len(self.view) > 128 * 1024 * 1024:
                self.error = RunError("video publication backlog exceeded; recording is incomplete")
                raise self.error
            self.publications.append((stamp, self.view, self.metadata))

    def _write(self, view: bytes, metadata: dict) -> None:
        """Only the writer owns the pipe and ledger; publication never waits on I/O."""
        assert self.proc is not None and self.proc.stdin is not None
        self.write_started = time.monotonic()
        try:
            remaining = memoryview(view)
            while remaining:
                written = self.proc.stdin.write(remaining)
                if not written:
                    raise BrokenPipeError("ffmpeg accepted no video bytes")
                remaining = remaining[written:]
            self.frame_log.write(json.dumps({"frame": self.frames, "video_s": self.frames / self.fps,
                                             **metadata}) + "\n")
            self.frames += 1
            if self.frames % self.fps == 0:
                self.frame_log.flush()
        finally:
            self.write_started = None

    def _watch(self) -> None:
        while not self.done.wait(.05):
            started = self.write_started
            if started is not None and time.monotonic() - started > self.write_timeout:
                self.error = TimeoutError("ffmpeg video write timed out; recording is incomplete")
                if self.proc is not None:
                    self.proc.kill()
                self.done.set()
                return

    def _run(self) -> None:
        try:
            while self.error is None:
                with self.lock:
                    now = time.monotonic()
                    deadline = None if self.started is None else self.started + self.frames / self.fps
                    if self.stop_time is not None and (deadline is None or deadline > self.stop_time):
                        break
                    snapshot = None
                    if deadline is not None and deadline <= now:
                        # Retain publication times during encoder stalls: a new view must
                        # never be backdated into frames belonging to an earlier view.
                        while len(self.publications) > 1 and self.publications[1][0] <= deadline:
                            self.publications.popleft()
                        _, pixels, metadata = self.publications[0]
                        snapshot = pixels, metadata
                if snapshot is not None:
                    self._write(*snapshot)
                else:
                    self.done.wait(.5 / self.fps)
        except Exception as exc:
            self.error = self.error or exc
        finally:
            self.done.set()

    def close(self) -> None:
        if self.closed:
            return
        with self.lock:
            self.stop_time = time.monotonic()
        if self.thread is not None:
            self.thread.join(self.write_timeout + 1)
            if self.thread.is_alive():
                self.error = self.error or TimeoutError("video writer did not stop; recording is incomplete")
                if self.proc is not None:
                    self.proc.kill()
                self.thread.join(2)
            if self.thread.is_alive():
                # Do not close a ledger still owned by its writer.
                raise RunError(str(self.error))
        self.done.set()
        if self.watchdog is not None:
            self.watchdog.join(1)
        rc = 0
        if self.proc is not None:
            assert self.proc.stdin is not None
            try:
                self.proc.stdin.close()
            except OSError as exc:
                self.error = self.error or exc
            try:
                rc = self.proc.wait(timeout=self.write_timeout)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.error = self.error or TimeoutError("ffmpeg finalization timed out; recording is incomplete")
                try:
                    rc = self.proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    rc = -1
            self.err.close()
        self.closed = True
        self.event("close", video_frames=self.frames, complete=rc == 0 and self.error is None)
        self.log.close()
        self.frame_log.flush()
        self.frame_log.close()
        if rc != 0 or self.error is not None:
            raise RunError(f"video writer failed (ffmpeg {rc}): {self.error}")


class PreviewCache:
    """Bound decoded pixels as well as entry count; large one-off frames are not retained."""
    def __init__(self, max_bytes=32 * 1024 * 1024, max_entries=16):
        self.max_bytes, self.max_entries = max_bytes, max_entries
        self.frames = OrderedDict()
        self.bytes = 0

    def clear(self):
        self.frames.clear()
        self.bytes = 0

    def get(self, key, loader):
        if key in self.frames:
            value = self.frames.pop(key)
            self.frames[key] = value
            return value
        value = loader()
        size = len(value[0]) if value is not None else 0
        if size <= self.max_bytes:
            while self.frames and (len(self.frames) >= self.max_entries or self.bytes + size > self.max_bytes):
                _, old = self.frames.popitem(last=False)
                self.bytes -= len(old[0]) if old is not None else 0
            self.frames[key] = value
            self.bytes += size
        return value


def run(ctx: Any, args: Any) -> int:
    from kptas.telos.cli import bundle_cfg, err, open_bundle, out
    video = getattr(args, "video", None)
    rec = Recorder(video) if video else None
    host_keys = None
    overlay = None
    protocol = None
    command_id = None
    last_error = None
    allow_native_poll = True
    last_view = None
    last_ui = 0.0

    preview_cache = PreviewCache()

    def status():
        return {"state": "playing" if control.playing else "paused", "tick": control.sup.tick,
                "bundle": str(control.bundle.root), "error": last_error,
                "view": deepcopy(getattr(control, "view_info", {})),
                "input": deepcopy(getattr(control, "live_input", None)),
                "notices": list(getattr(control, "notices", ())) if isinstance(getattr(control, "notices", None), deque) else []}

    def frame_metadata():
        from kptas.telos.input_view import recorded_input
        return {"tick": control.sup.tick,
                "clip_tick": control.sup.tick - control.clip_origin_tick,
                "input": recorded_input(control.bundle, control.sup.tick),
                "device_state": control.live_input,
                "view": control.view_info,
                "playing": control.playing,
                "workspace_expanded": bool(overlay and overlay.expanded),
                "overlay_layout": overlay.layout_metadata() if overlay is not None else None,
                "status": overlay.state.text() if overlay else "",
                "bundle": str(control.bundle.root)}

    def publish(pixels, w, h, *, snapshot=False):
        if rec is None:
            return
        stride = w * 4
        top = (overlay.compose(pixels, w, h) if overlay is not None else
               b"".join(pixels[(h-1-y)*stride:(h-y)*stride] for y in range(h)))
        rec.show_top_down(top, w, h, metadata=frame_metadata())
        if snapshot and overlay is not None:
            img = overlay.G.QImage(top, w, h, w*4, overlay.G.QImage.Format.Format_ARGB32)
            img.save(str(rec.path.with_suffix(".latest.png")))

    def show(what: str = "", **fields: Any) -> None:
        nonlocal last_view, last_ui
        if rec is None and overlay is None:
            return
        # Sample output and device state at the SAME stopped gate before painting labels.
        if control.sup.backend is None or not getattr(control.sup, "alive", True):
            control.live_input = None
            control.view_info = {"source": "unavailable"}
            last_view = None
            if overlay is not None:
                overlay.busy = False
                overlay.refresh(what, **fields)
            if rec is not None:
                rec.event(what or "tick", tick=control.sup.tick, **fields)
            return
        pixels, w, h = control.sup.backend.view_raw()
        native = dict(getattr(control.sup.backend, "last_view_metadata", {}))
        control.view_info = native or {"source": "unknown"}
        if "gate_tick" in native:
            control.view_info["gate_tick"] += control.sup.offset
            if type(native.get("captured_tick")) is int:
                control.view_info["captured_tick"] += control.sup.offset
            control.live_input = control.sup.backend.input_state()
            control.live_input["tick"] += control.sup.offset
            if control.live_input["tick"] != control.sup.tick:
                raise RunError("input sample does not match the displayed gate")
        else:
            control.live_input = None
        if native.get("blank"):
            from kptas.telos.frame_preview import recorded_preview
            key = (str(control.bundle.root), control.sup.tick)
            preview = preview_cache.get(key, lambda: recorded_preview(control.bundle, control.sup.tick))
            if preview is not None:
                pixels, w, h, control.view_info = preview
        last_view = (pixels, w, h)
        last_ui = time.monotonic()
        if overlay is not None:
            overlay.busy = False
            overlay.set_game_view(pixels, w, h, control.view_info)
            overlay.refresh(what, **fields)
        publish(pixels, w, h, snapshot=bool(what))
        if rec is not None:
            rec.event(what or "tick", tick=control.sup.tick, **fields)

    def pending(text: str) -> None:
        if overlay is not None:
            overlay.busy_state(text)
            if last_view is not None:
                publish(*last_view)

    try:
        with ExitStack() as opened:
            bundle = opened.enter_context(open_bundle(args.bundle))
            start_mark = getattr(args, "start_mark", None)
            if start_mark:
                matches = [m["tick"] for m in bundle.marks if m.get("name") == start_mark]
                if len(matches) != 1:
                    raise TelosError(f"bookmark {start_mark!r} must identify exactly one recorded tick")
                args.tick = matches[0]
            cfg = bundle_cfg(ctx, bundle, run__backend=args.backend)
            from kptas.telos.xhotkeys import XHotkeys
            host_keys = None
            control = Controls(bundle, cfg, attach=args.attach)
            control.sink = None
            control.branch_ok = False

            def finish_take():
                if control.sink is not None:
                    try:
                        control.finish_branch(seal=control.branch_ok)
                    except Exception as exc:
                        control.sup.sink = None
                        control.sink = None
                        raise RuntimeError("Could not save the branch before rewinding; stopping") from exc
                    preview_cache.clear()
                    control.sink = None
                    control.branch_ok = False

            try:
                control.rewind(args.tick)
                control.clip_origin_tick = args.tick
                control.clip_origin_name = start_mark or ("Recording start" if args.tick == 0 else "Selected checkpoint")
                control.live_input = None
                control.view_info = {}
                if getattr(args, "overlay", False):
                    from kptas.telos.overlay import Overlay
                    overlay = Overlay(control, args)
                show("open", bundle=str(args.bundle), requested_tick=args.tick, origin=control.clip_origin_name)
                from kptas.telos.workspace_protocol import WorkspaceProtocol
                protocol = WorkspaceProtocol(getattr(args, "control_port", 0))
                protocol.update(status())
                out(json.dumps({"workspace_url": protocol.url, "bearer_token": protocol.token}))
                out("Game tool shortcuts follow UI preferences when the native dispatcher is available; "
                    "older runtimes retain Ctrl+Alt function-key controls.")
                out("Mode labels are not a certification of base-game fidelity.")
                input_due = 0.0
                while True:
                    native_action = control.sup.backend.hud_poll() if allow_native_poll and control.sup.backend is not None and getattr(control.sup, "alive", True) else ""
                    supports_tools = getattr(control.sup.backend, 'supports_tool_bindings', lambda: False)()
                    if supports_tools:
                        if host_keys is not None: host_keys.close(); host_keys = None
                        action = native_action
                    else:
                        if host_keys is None: host_keys = XHotkeys.for_config(cfg)
                        action = host_keys.poll() if host_keys is not None else native_action
                    values = {}
                    if overlay is not None:
                        ui_action, values = overlay.poll()
                        has_ui_focus = getattr(overlay, "has_ui_focus", None)
                        # Both legacy sources were drained above. While Qt owns
                        # focus, only its configured shortcuts may dispatch.
                        if supports_tools:
                            # Native recognition already required game focus;
                            # Qt shortcuts required UI focus. Preserve both
                            # queued commands instead of dropping one edge.
                            if ui_action and action:
                                overlay.actions.appendleft((action, {}))
                            action = ui_action or action
                        else:
                            action = (ui_action if callable(has_ui_focus) and has_ui_focus() is True
                                      else ui_action or action)
                    command_id = None
                    if not action:
                        command_id, action, values = protocol.poll()
                    try:
                        scheduler_active = control.input_scheduler_running
                        with control_transaction(control, bool(action) or control.playing or scheduler_active):
                            if action:
                                last_error = None
                                protocol.update({**status(), "state": "busy", "action": action})
                            if action == 'configure_shortcuts':
                                if supports_tools:
                                    from .xhotkeys import native_bindings
                                    control.sup.backend.hud_bindings(native_bindings(values['shortcuts']))
                                action = ''
                            elif action in ('toggle_visibility','toggle_engagement','toggle_editor','switch_layout','reload_ui','apply'):
                                if overlay is not None:
                                    overlay.interaction_controls.dispatch(action, source='native')
                                action = ''
                            elif action == 'set_engaged':
                                if getattr(control, 'engagement_qualified', False) is not True:
                                    raise TelosError('normal input handoff is not qualified')
                                control.set_engaged(values['engaged'], values.get('pause_on_engage', True))
                                show('engagement')
                            if action == "stop":
                                if overlay is not None:
                                    overlay.preserve_draft()
                                protocol.update(status())
                                protocol.finish(command_id)
                                command_id = None
                                break
                            if action == "load_bundle":
                                if overlay is not None:
                                    overlay.preserve_draft()
                                pending("Opening…")
                                try:
                                    control.load_recording(values["path"], 0)
                                    cfg = control.cfg
                                except Exception as exc:
                                    if control.broken is True:
                                        raise RuntimeError("Could not open the recording. Stasis stopped this session.") from exc
                                    raise
                                preview_cache.clear()
                                last_view = None
                                control.clip_origin_tick = 0
                                control.clip_origin_name = "Start"
                                control.live_input = None
                                control.view_info = {}
                                if host_keys is not None:
                                    host_keys.close()
                                host_keys = XHotkeys.for_config(cfg)
                                if overlay is not None:
                                    overlay.set_control(control)
                                show("open", bundle=str(control.bundle.root), requested_tick=0, origin="Start")
                            elif action == "load_score":
                                if overlay is None:
                                    raise TelosError("score editing requires the workspace; pass score and out to branch")
                                overlay.load_score_path(values["path"])
                                show("score_loaded")
                            elif action == "workspace" and overlay is not None:
                                overlay.toggle()
                                show("workspace")
                            elif action == "play_pause":
                                control.toggle_play()
                                show("play" if control.playing else "pause")
                            elif action == "step":
                                control.playing = False
                                pending("Stepping…")
                                if control.step():
                                    show("step")
                                else:
                                    control.branch_ok = control.sink is not None
                                    show("complete")
                            elif action == "seek":
                                finish_take()
                                pending("Restoring…")
                                control.rewind(values["tick"])
                                show("seek")
                            elif action == "save_recording":
                                pending("Saving recording…")
                                control.save_recording()
                                show("saved")
                            elif action == "save":
                                if values.get("path"):
                                    if overlay is not None:
                                        overlay.save_score(values["path"])
                                    else:
                                        from kptas.telos.overlay import Overlay
                                        Overlay._save_text(Path(values["path"]), control.score.text())
                                elif control.sink is not None:
                                    control.save()
                                elif overlay is not None and command_id is None:
                                    if overlay.save_score(values.get("path")) is None:
                                        continue
                                else:
                                    raise TelosError("there is no branch to save; use the workspace to save a score")
                                show("save")
                            elif action == "rewind":
                                finish_take()
                                pending("Rewinding…")
                                if rec is not None:
                                    rec.event("rewind.begin", tick=control.sup.tick)
                                control.rewind(control.sup.tick - 1)
                                show("rewind")
                            elif action == "rewind_mark":
                                finish_take()
                                pending("Rewinding to mark…")
                                if rec is not None:
                                    rec.event("rewind_mark.begin", tick=control.sup.tick)
                                control.rewind_mark()
                                show("rewind_mark")
                            elif action == "branch":
                                destination = values.get("out") or (overlay.branch_destination() if overlay is not None else args.branch_out)
                                if overlay is not None and destination is None:
                                    continue
                                if not destination or (overlay is None and not (values.get("score") or args.branch_score)):
                                    raise TelosError("choose a branch score and output path")
                                score_path = values.get("score") or (overlay.branch_score() if overlay is not None else args.branch_score)
                                if score_path is None:
                                    continue
                                edited = ctx.compile(score_path)
                                pending("Starting branch…")
                                control.branch(edited, destination, on_durable=(
                                    (lambda r: overlay.saved.setText(f"Saved through {_unit_name(((control.cfg or {}).get('clock') or {}).get('precision'))} {r.get('tick', control.sup.tick)}"))
                                    if overlay is not None else None))
                                preview_cache.clear()
                                show("branch", score=str(score_path), out=str(destination))
                            elif action:
                                raise TelosError("unknown HUD action")
                            if control.input_scheduler_running and time.monotonic() >= input_due:
                                pending("Playing…")
                                # Budget recording work inside the frame period.
                                # Waiting a whole period after it finishes adds
                                # capture/flush latency to every physical frame.
                                frame_started = time.monotonic()
                                control.input_scheduler_step()
                                input_due = frame_started + 1 / max(1, control.sup.hz)
                                show()
                            elif control.playing:
                                pending("Playing…")
                                if control.play_step() is False:
                                    control.playing = False
                                    control.branch_ok = control.sink is not None
                                    control.label("BRANCH COMPLETE - CTRL ALT F11 EXIT")
                                    show("complete")
                                else:
                                    show()
                    except (BackendError, TelosError, RunError, ScoreError, OSError, ValueError) as exc:
                        control.playing = False
                        if scheduler_active:
                            control.park_input_scheduler(exc)
                        allow_native_poll = False
                        last_error = str(exc)
                        err(str(exc))
                        # A partial restore can change the gate before it fails.
                        # Do not republish an older observation under that new tick.
                        last_view = None
                        control.live_input = None
                        control.view_info = {"source": "unavailable"}
                        if overlay is not None:
                            overlay.busy = False
                            if getattr(overlay, "preview", None) is not None:
                                overlay.preview.hide()
                            overlay.refresh("refused", why=str(exc))
                            overlay.state.setText("Refused: " + str(exc))
                        if rec is not None:
                            rec.event("refused", tick=control.sup.tick, why=str(exc))
                    if action and last_error is None:
                        allow_native_poll = True
                    protocol.update(status())
                    if overlay is not None and isinstance(getattr(control, "notices", None), deque):
                        while control.notices:
                            overlay.console.appendPlainText(control.notices.popleft())
                    protocol.finish(command_id, last_error)
                    command_id = None
                    # This wall delay is at a stopped gate, never in a game tick.
                    if control.input_scheduler_running:
                        time.sleep(min(.025, max(0.0, input_due - time.monotonic())))
                    elif not control.playing:
                        if overlay is not None and rec is not None and last_view is not None and time.monotonic()-last_ui > .1:
                            pixels,w,h = last_view
                            publish(pixels,w,h)
                            last_ui = time.monotonic()
                        if control.sink is not None:
                            control.sink.checkpoint_if_due()
                        time.sleep(0.025)
            finally:
                try:
                    control.close()
                finally:
                    if control.sink is not None:
                        control.sink.close(seal=control.branch_ok, backend_ceiling=control.sup.caps.tier_ceiling)
        return 0
    finally:
        if protocol is not None:
            if command_id is not None:
                protocol.finish(command_id, "controller stopped before command completion")
            protocol.close()
        if overlay is not None:
            overlay.close()
        if host_keys is not None:
            host_keys.close()
        if rec is not None:
            rec.close()
