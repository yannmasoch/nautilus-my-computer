#!/usr/bin/python3
"""Serve one local video to WebKit over a private loopback URL.

WebKitGTK's sandbox rejects file:// media and its media backend does not accept
custom URI schemes. A standards-compliant HTTP source is therefore the narrow
bridge between the local file and WebKit's crash-isolated decoder process.
This helper exposes exactly one tokenized path on 127.0.0.1, supports byte
ranges for seeking/container indexes, and exits when its parent closes stdin.
It never parses or decodes the media itself.
"""

from __future__ import annotations

import json
import mimetypes
import os
import secrets
import select
import signal
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def parse_byte_range(value: str | None, total: int) -> tuple[int, int] | None:
    """Parse one RFC 7233 bytes range, returning inclusive bounds."""
    if not value:
        return None
    if not value.startswith("bytes=") or "," in value:
        raise ValueError("unsupported range")
    spec = value[6:].strip()
    if "-" not in spec:
        raise ValueError("malformed range")
    start_text, end_text = spec.split("-", 1)
    if not start_text:
        suffix = int(end_text)
        if suffix <= 0:
            raise ValueError("empty suffix range")
        start = max(0, total - suffix)
        end = total - 1
    else:
        start = int(start_text)
        end = int(end_text) if end_text else total - 1
        if start < 0 or start >= total or end < start:
            raise ValueError("range outside file")
        end = min(end, total - 1)
    return start, end


class _VideoServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, path: str, token: str) -> None:
        self.video_fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
        self.video_size = os.fstat(self.video_fd).st_size
        self.token_path = f"/{token}"
        self.content_type = mimetypes.guess_type(path)[0] or "application/octet-stream"
        try:
            super().__init__(("127.0.0.1", 0), _VideoHandler)
        except Exception:
            os.close(self.video_fd)
            raise

    def server_close(self) -> None:
        super().server_close()
        if self.video_fd >= 0:
            os.close(self.video_fd)
            self.video_fd = -1


class _VideoHandler(BaseHTTPRequestHandler):
    server: _VideoServer

    def log_message(self, _format: str, *_args) -> None:
        return

    def do_HEAD(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._serve(send_body=False)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._serve(send_body=True)

    def _serve(self, *, send_body: bool) -> None:
        if self.path != self.server.token_path:
            self.send_error(404)
            return
        try:
            total = self.server.video_size
            requested = parse_byte_range(self.headers.get("Range"), total)
        except ValueError:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{max(0, locals().get('total', 0))}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        start, end = requested or (0, total - 1)
        length = end - start + 1
        self.send_response(206 if requested is not None else 200)
        self.send_header("Content-Type", self.server.content_type)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        if requested is not None:
            self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
        self.end_headers()
        if not send_body:
            return

        try:
            with os.fdopen(os.dup(self.server.video_fd), "rb", buffering=0) as stream:
                stream.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = stream.read(min(256 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError, OSError):
            # Seeking/stopping a video normally cancels an in-flight range.
            return


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: video_stream_helper.py LOCAL_FILE", file=sys.stderr)
        return 2
    path = os.path.abspath(argv[1])
    if not os.path.isfile(path):
        print(json.dumps({"event": "error", "message": "video is not a local file"}), flush=True)
        return 1

    token = secrets.token_urlsafe(32)
    server = _VideoServer(path, token)
    stopping = False

    def stop(_signum, _frame) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    port = server.server_address[1]
    print(
        json.dumps(
            {"event": "ready", "url": f"http://127.0.0.1:{port}/{token}"},
            separators=(",", ":"),
        ),
        flush=True,
    )

    stdin_fd = sys.stdin.fileno()
    try:
        while not stopping:
            readable, _writable, _exceptional = select.select(
                [stdin_fd, server.fileno()], [], [], 0.5
            )
            if stdin_fd in readable and not os.read(stdin_fd, 1):
                break
            if server.fileno() in readable:
                server._handle_request_noblock()
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
