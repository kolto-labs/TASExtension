#!/usr/bin/env python3
"""Serve the demo and its isolated public-video player on separate local origins."""
from __future__ import annotations

import argparse
import hashlib
import fnmatch
import ipaddress
import json
import os
import re
import socket
import threading
import time
from collections import deque
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from game_installation import game_installation, make_manifest, open_indexed_file, validate_manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=47651)
    parser.add_argument("--kotor-install", type=Path, help="Serve one local K1 installation through same-origin lazy range reads.")
    parser.add_argument("--kotor-executable", type=Path, help="Use a matching DRM-free swkotor.exe while reading game resources from --kotor-install.")
    parser.add_argument("--kotor-startup-overlay", type=Path, help="Use matching K1 startup files from this directory while streaming shared resources from --kotor-install.")
    args = parser.parse_args()
    if not 1024 <= args.port < 65535:
        parser.error("port must leave room for the adjacent video-player origin")
    public = Path(__file__).resolve().parents[1] / "public"
    public_root = public.resolve()
    source_digest_cache: dict[str, tuple[tuple[int, int, int], str]] = {}
    source_digest_lock = threading.Lock()
    player_origin = f"http://127.0.0.1:{args.port + 1}"
    parent_origin = f"http://127.0.0.1:{args.port}"
    runtime_origin = f"http://localhost:{args.port}"
    game_files, game_manifest = ([], None)
    game_library_snapshots: dict[int, bytes] = {}
    game_root: Path | None = None
    game_index_ready = threading.Event()
    game_index_error: str | None = None
    if args.kotor_executable and not args.kotor_install:
        parser.error("--kotor-executable requires --kotor-install.")
    if args.kotor_startup_overlay and not args.kotor_install:
        parser.error("--kotor-startup-overlay requires --kotor-install.")

    def build_game_index() -> None:
        nonlocal game_files, game_manifest, game_index_error, game_root, game_library_snapshots
        try:
            game_root = args.kotor_install.resolve(strict=True)
            game_files, _ = game_installation(args.kotor_install, require_executable=not args.kotor_executable)
            if args.kotor_executable:
                executable = args.kotor_executable.resolve(strict=True)
                if executable.name.casefold() != "swkotor.exe" or not executable.is_file():
                    raise RuntimeError("--kotor-executable must name an existing swkotor.exe file.")
                executable_size = executable.stat().st_size
                if executable_size <= 0:
                    raise RuntimeError("--kotor-executable must not be empty.")
                game_files = [
                    (executable, name, executable_size) if name.casefold() == "swkotor.exe" else (path, name, size)
                    for path, name, size in game_files
                ]
            if args.kotor_startup_overlay:
                overlay_root = args.kotor_startup_overlay.resolve(strict=True)
                if not overlay_root.is_dir():
                    raise RuntimeError("--kotor-startup-overlay must name a directory.")
                # K1 releases can share their large resource archives while
                # differing in the small files that define the executable's
                # startup environment. Keep those files from one edition
                # together so the guest never mixes its EXE with another
                # edition's dialog table, Bink runtime, or startup settings.
                # When an explicit executable is supplied, it is the launch
                # authority and must stay paired with the selected startup
                # resources.  The overlay may provide the matching Bink,
                # dialog table and INI, but must not silently replace the
                # executable with the copy from the resource directory.
                overlay_names = {"binkw32.dll", "swkotor.ini", "dialog.tlk"}
                if not args.kotor_executable:
                    overlay_names.add("swkotor.exe")
                indexed = {name.casefold(): index for index, (_, name, _) in enumerate(game_files)}
                for name in sorted(overlay_names):
                    candidate = overlay_root / name
                    if not candidate.is_file() or candidate.is_symlink():
                        raise RuntimeError(f"K1 startup overlay is missing a regular {name} file.")
                    size = candidate.stat().st_size
                    if size <= 0:
                        raise RuntimeError(f"K1 startup overlay contains an empty {name} file.")
                    index = indexed.get(name.casefold())
                    if index is None:
                        raise RuntimeError(f"K1 installation index is missing {name} for the startup overlay.")
                    _, indexed_name, _ = game_files[index]
                    game_files[index] = (candidate, indexed_name, size)
            game_library_snapshots = {}
            game_manifest = make_manifest(game_files, immutable_library_bytes=game_library_snapshots)
            print(f"K1 installation index ready: {len(game_files)} files.", flush=True)
        except Exception as error:
            game_index_error = f"{type(error).__name__}: {error}"
            print(f"K1 installation index failed: {game_index_error}", flush=True)
        finally:
            game_index_ready.set()
    rules: list[tuple[str, list[tuple[str, str]]]] = []
    range_rejections_logged: set[str] = set()
    # The selected local runtime can emit compact, changed-only guest
    # fingerprints. Keep these receipts in memory for local diagnosis; never
    # turn them into stdout logs or files. Both storage and request rate are
    # bounded so a stale runtime cannot grow the server indefinitely.
    guest_trace_lock = threading.Lock()
    guest_trace_receipts: deque[dict[str, str]] = deque(maxlen=256)
    guest_trace_request_times: deque[float] = deque()
    guest_trace_state = {"received_total": 0}
    guest_trace_rate_per_second = 8
    guest_trace_max_query_chars = 16_384
    guest_trace_max_fingerprint_chars = 12_000
    for line in (public / "_headers").read_text().splitlines():
        if not line.strip():
            continue
        if line.startswith(" "):
            key, value = line.strip().split(":", 1)
            rules[-1][1].append((key, value.strip()))
        else:
            rules.append((line.strip(), []))

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *handler_args, **handler_kwargs):
            super().__init__(*handler_args, directory=str(public), **handler_kwargs)

        def log_message(self, format: str, *args) -> None:
            # A stale runtime can flood stdout with one access log per byte.
            # Keep ordinary lazy-range traffic quiet and report a bounded
            # number of HTTP failures while preserving other server logs.
            request_path = self.path.split("?", 1)[0]
            if request_path == "/__dev/ui-revisions":
                return
            # The worker liveness endpoint encodes a bounded but verbose guest
            # snapshot in this query. Logging every poll expands that payload
            # into a multi-megabyte access-log stream and can starve the local
            # server's managed stdout during long startup investigations.
            if "__telosGuestTrace=" in self.path:
                return
            if request_path.startswith("/private/install/"):
                is_rejection = format.startswith("code %d, message %s") and args and int(args[0]) >= 400
                if (is_rejection and request_path not in range_rejections_logged
                        and len(range_rejections_logged) < 8):
                    range_rejections_logged.add(request_path)
                    super().log_message(format, *args)
                return
            super().log_message(format, *args)

        def is_loopback_request(self) -> bool:
            request_host = urlsplit(f"//{self.headers.get('Host', '')}").hostname
            try:
                remote_is_loopback = ipaddress.ip_address(self.client_address[0]).is_loopback
            except (ValueError, IndexError):
                return False
            return remote_is_loopback and request_host in {"127.0.0.1", "localhost", "::1"}

        def send_empty(self, status: int) -> None:
            self.send_response(status)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def receive_guest_trace(self, raw_query: str) -> None:
            """Accept one opt-in, bounded fingerprint without serving the page."""
            if not self.is_loopback_request():
                self.send_empty(404)
                return
            if len(raw_query) > guest_trace_max_query_chars:
                self.send_empty(413)
                return
            try:
                fields = parse_qs(raw_query, keep_blank_values=True, strict_parsing=True, max_num_fields=1)
                values = fields.get("__telosGuestTrace", [])
                if set(fields) != {"__telosGuestTrace"} or len(values) != 1:
                    raise ValueError
                fingerprint = values[0]
                if (not fingerprint or len(fingerprint) > guest_trace_max_fingerprint_chars
                        or any(ord(char) < 0x20 or ord(char) > 0x7e for char in fingerprint)):
                    raise ValueError
            except (ValueError, UnicodeError):
                self.send_empty(400)
                return

            now = time.monotonic()
            rate_limited = False
            with guest_trace_lock:
                while guest_trace_request_times and now - guest_trace_request_times[0] >= 1.0:
                    guest_trace_request_times.popleft()
                if len(guest_trace_request_times) >= guest_trace_rate_per_second:
                    rate_limited = True
                else:
                    guest_trace_request_times.append(now)
                    guest_trace_state["received_total"] += 1
                    guest_trace_receipts.append({
                        "receivedAt": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                        "fingerprint": fingerprint,
                    })
            if rate_limited:
                self.send_empty(429)
                return
            self.send_empty(204)

        def read_guest_traces(self) -> None:
            if not self.is_loopback_request():
                self.send_empty(404)
                return
            with guest_trace_lock:
                payload = {
                    "receivedTotal": guest_trace_state["received_total"],
                    "retained": len(guest_trace_receipts),
                    "capacity": guest_trace_receipts.maxlen,
                    "records": list(guest_trace_receipts),
                }
            data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            request = urlsplit(self.path)
            path = request.path
            host = self.headers.get("Host", "").split(":", 1)[0].lower()
            if path == "/" and "__telosGuestTrace" in request.query:
                self.receive_guest_trace(request.query)
                return
            if path == "/__dev/guest-traces":
                self.read_guest_traces()
                return
            if path == "/__dev/ui-revisions":
                request_host = urlsplit(f"//{self.headers.get('Host', '')}").hostname
                if request_host not in {"127.0.0.1", "localhost", "::1"}:
                    self.send_error(404)
                    return
                requested = parse_qs(request.query, keep_blank_values=True).get("path", [])
                if not requested or len(requested) > 32:
                    self.send_error(400, "Provide between one and 32 local UI source paths.")
                    return
                revisions: dict[str, str] = {}
                try:
                    for relative in dict.fromkeys(requested):
                        if not relative or relative.startswith("/") or "\\" in relative or ".." in relative.split("/"):
                            raise ValueError("Invalid UI source path.")
                        source = (public_root / relative).resolve(strict=True)
                        source.relative_to(public_root)
                        if source.suffix not in {".css", ".js"} or not source.is_file():
                            raise ValueError("Only local CSS and JavaScript sources are eligible.")
                        metadata = source.stat()
                        fingerprint = (metadata.st_mtime_ns, metadata.st_size, metadata.st_ino)
                        with source_digest_lock:
                            cached = source_digest_cache.get(relative)
                            if cached is not None and cached[0] == fingerprint:
                                digest = cached[1]
                            else:
                                digest = hashlib.sha256(source.read_bytes()).hexdigest()
                                source_digest_cache[relative] = (fingerprint, digest)
                        revisions[relative] = digest
                except (OSError, ValueError) as error:
                    self.send_error(400, str(error))
                    return
                data = json.dumps(revisions, separators=(",", ":")).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)
                return
            isolated_runtime = host == "localhost"
            if isolated_runtime and path.endswith("/game-runtime.html"):
                runtime_path = public / path.lstrip("/")
                if runtime_path.is_file():
                    html = runtime_path.read_text(encoding="utf-8")
                    # Run the CPU-heavy emulator on localhost so Chromium can
                    # place it in a separate renderer from the prototype at
                    # 127.0.0.1. The exact parent origin is fixed by this local
                    # server, never accepted from an untrusted query string.
                    parent_script = f'<script>window.__telosParentOrigin={json.dumps(parent_origin)};</script>'
                    # The selected runtime's owned-install.js is the single
                    # owner of bridge loading. Injecting another bridge here
                    # creates duplicate listeners and can race the runtime's
                    # control handshake.
                    html = html.replace('<script src=owned-install.js></script>', parent_script + '<script src=owned-install.js></script>')
                    data = html.encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(data)
                    return
            if isolated_runtime and (path.endswith("/owned-install.js") or path == "/original-game/telos-tas-bridge.js"):
                script_path = public / path.lstrip("/")
                if script_path.is_file():
                    # Keep script URLs relative to this isolated localhost
                    # origin so the runtime's script-src 'self' policy allows
                    # them. __telosParentOrigin is for the validated message
                    # target only; rewriting location.origin here breaks CSP.
                    script = script_path.read_text(encoding="utf-8")
                    data = script.encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/javascript; charset=utf-8")
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(data)
                    return
            if args.kotor_install and path in {"/private/install.json", "/private/install/status"}:
                if path == "/private/install/status":
                    data = json.dumps({"state": "ready" if game_index_ready.is_set() and game_index_error is None else "error" if game_index_error else "indexing",
                                       "error": game_index_error}, separators=(",", ":")).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(data)
                    return
                game_index_ready.wait()
                if game_index_error:
                    self.send_error(503, "K1 installation index failed")
                    return
                try:
                    validate_manifest(game_manifest)
                except RuntimeError:
                    self.send_error(503, "K1 installation manifest failed validation")
                    return
                data = json.dumps(game_manifest, separators=(",", ":")).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)
                return
            if args.kotor_install and path.startswith("/private/install/"):
                game_index_ready.wait()
                if game_index_error:
                    self.send_error(503, "K1 installation index failed")
                    return
                try:
                    index = int(path.rsplit("/", 1)[1])
                    if index < 0:
                        raise ValueError
                    file_path, _, size = game_files[index]
                    snapshot = game_library_snapshots.get(index)
                    range_header = self.headers.get("Range")
                    if size == 0:
                        if range_header is not None:
                            self.send_response(416)
                            self.send_header("Content-Range", "bytes */0")
                            self.send_header("Content-Length", "0")
                            self.send_header("Cache-Control", "no-store")
                            self.end_headers()
                            return
                        try:
                            descriptor = open_indexed_file(file_path, game_root, size)
                        except (OSError, ValueError):
                            self.send_error(409, "Indexed game asset changed after it was validated.")
                            return
                        os.close(descriptor)
                        self.send_response(200)
                        self.send_header("Content-Type", "application/octet-stream")
                        self.send_header("Content-Length", "0")
                        self.send_header("Accept-Ranges", "bytes")
                        self.send_header("Cache-Control", "no-store")
                        self.end_headers()
                        return
                    start, end = 0, size - 1
                    if not range_header or not range_header.startswith("bytes=") or "," in range_header:
                        raise ValueError
                    left, right = range_header[6:].split("-", 1)
                    start = int(left)
                    end = min(int(right), size - 1)
                    if start < 0 or end < start or start >= size:
                        raise ValueError
                except (ValueError, IndexError):
                    self.send_error(416)
                    return
                if snapshot is None:
                    try:
                        descriptor = open_indexed_file(file_path, game_root, size)
                    except (OSError, ValueError):
                        self.send_error(409, "Indexed game asset changed after it was validated.")
                        return
                self.send_response(206)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(end - start + 1))
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                if snapshot is not None:
                    self.wfile.write(snapshot[start:end + 1])
                else:
                    with os.fdopen(descriptor, "rb") as source:
                        source.seek(start)
                        remaining = end - start + 1
                        while remaining:
                            block = source.read(min(256 * 1024, remaining))
                            if not block:
                                break
                            self.wfile.write(block)
                            remaining -= len(block)
                return
            if self.path.split("?", 1)[0] == "/media/player-config.json":
                data = json.dumps({"origin": player_origin}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)
                return
            # Player implementation is served exclusively on the separate origin.
            if self.path.split("?", 1)[0].startswith("/player/"):
                self.send_error(404)
                return
            super().do_GET()

        def do_POST(self) -> None:
            # BoxedWine guest-worker failures are otherwise reduced to an
            # opaque ErrorEvent in the embedding page. Keep this diagnostic
            # sink loopback-only (the server is bound to 127.0.0.1), bounded,
            # and separate from the visible application UI.
            if self.path.split("?", 1)[0] != "/private/diagnostics":
                self.send_error(404)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 8192:
                    raise ValueError
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict) or payload.get("kind") != "worker-fatal":
                    raise ValueError
                detail = str(payload.get("detail", ""))[:6000].replace("\r", " ")
            except (ValueError, json.JSONDecodeError):
                self.send_error(400)
                return
            print(f"[BoxedWine worker fatal] {detail}", flush=True)
            self.send_response(204)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def end_headers(self) -> None:
            path = self.path.split("?", 1)[0]
            host = self.headers.get("Host", "").split(":", 1)[0].lower()
            isolated_runtime = host == "localhost"
            release_parts = path.split("/")
            immutable_release_asset = (
                isolated_runtime
                and len(release_parts) == 5
                and release_parts[1:2] == ["original-game"]
                and release_parts[2:3] == ["runtime-releases"]
                and re.fullmatch(r"[0-9a-f]{64}", release_parts[3]) is not None
                and release_parts[4] != "game-runtime.html"
            )
            selected: dict[str, str] = {}
            for pattern, headers in rules:
                if fnmatch.fnmatchcase(path, pattern):
                    selected.update(headers)
            for key, value in selected.items():
                if host == "127.0.0.1" and (path == "/style.css" or path.startswith("/js/")) and key.lower() == "cache-control":
                    value = "no-store"
                if immutable_release_asset and key.lower() == "cache-control":
                    value = "public, max-age=31536000, immutable"
                if key.lower() == "content-security-policy":
                    value = value.replace("frame-src ", f"frame-src {player_origin} ")
                    if path in {"/", "/index.html"}:
                        value = value.replace("frame-src ", f"frame-src {runtime_origin} ", 1)
                    if isolated_runtime and path.startswith("/original-game/"):
                        value = value.replace("frame-ancestors 'self'", f"frame-ancestors {parent_origin}")
                if isolated_runtime and path.startswith("/original-game/"):
                    if key.lower() == "x-frame-options":
                        continue
                    if key.lower() == "cross-origin-resource-policy":
                        value = "cross-origin"
                if path in {"/", "/index.html"} and key.lower() == "permissions-policy":
                    value += f', cross-origin-isolated=(self "{runtime_origin}")'
                self.send_header(key, value)
            if immutable_release_asset and not any(key.lower() == "cache-control" for key in selected):
                self.send_header("Cache-Control", "public, max-age=31536000, immutable")
            super().end_headers()

    class PlayerHandler(SimpleHTTPRequestHandler):
        def __init__(self, *handler_args, **handler_kwargs):
            super().__init__(*handler_args, directory=str(public / "player"), **handler_kwargs)

        def end_headers(self) -> None:
            self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self' https://player.twitch.tv; style-src 'self' 'unsafe-inline'; frame-src https://player.twitch.tv; connect-src 'none'; img-src 'self' data:; object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors " + parent_origin)
            self.send_header("Cross-Origin-Embedder-Policy", "credentialless")
            self.send_header("Cross-Origin-Resource-Policy", "cross-origin")
            self.send_header("Cache-Control", "no-store")
            super().end_headers()

    class IPv6LoopbackHTTPServer(ThreadingHTTPServer):
        """Serve the isolated localhost alias on IPv6 without opening it externally."""
        address_family = socket.AF_INET6

        def server_bind(self) -> None:
            # Keep the IPv4 127.0.0.1 listener and the IPv6 ::1 listener as
            # separate loopback-only sockets on the same port. Chromium may
            # resolve `localhost` to ::1 first and does not reliably retry the
            # IPv4 address after a failed connection; the runtime intentionally
            # uses localhost as its isolated origin.
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            super().server_bind()

    # Bind every required socket before starting any serve loop: a port
    # conflict must not leave only one of the demo's origins available.
    with ThreadingHTTPServer(("127.0.0.1", args.port), Handler) as server, \
         IPv6LoopbackHTTPServer(("::1", args.port), Handler) as runtime_alias, \
         ThreadingHTTPServer(("127.0.0.1", args.port + 1), PlayerHandler) as player:
        if args.kotor_install:
            print(f"Indexing the configured K1 installation in the background: {args.kotor_install}", flush=True)
            threading.Thread(target=build_game_index, name="k1-install-index", daemon=True).start()
        worker = threading.Thread(target=player.serve_forever, daemon=True)
        worker.start()
        runtime_worker = threading.Thread(target=runtime_alias.serve_forever, daemon=True)
        runtime_worker.start()
        print(f"Serving the local demo at {parent_origin}/; isolated public-video player at {player_origin}/", flush=True)
        try:
            server.serve_forever()
        finally:
            runtime_alias.shutdown()
            runtime_worker.join()
            player.shutdown()
            worker.join()


if __name__ == "__main__":
    main()
