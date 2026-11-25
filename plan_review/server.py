"""Local HTTP server for one review round (imperative shell).

Serves the page on 127.0.0.1 under a random token, and blocks until the reviewer submits,
closes the page (heartbeats stop), or a timeout passes.
"""

from __future__ import annotations

import json
import mimetypes
import secrets
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

STATIC_DIR = Path(__file__).parent / "static"
MAX_BODY = 2_000_000


class ReviewServer:
    """Serve one payload and collect one submission."""

    def __init__(self, payload: dict, save_draft: Callable[[dict], None], static_dir: Path = STATIC_DIR) -> None:
        self.payload = payload
        self.save_draft = save_draft
        self.static_dir = static_dir.resolve()
        self.token = secrets.token_urlsafe(16)
        self.result: dict | None = None
        self.last_seen: float | None = None
        self._done = threading.Event()
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), self._handler_class())
        self.port = self._httpd.server_address[1]

    @property
    def url(self) -> str:
        """Page URL, including the token."""
        return f"http://127.0.0.1:{self.port}/{self.token}/"

    def wait(self, total_timeout: float, load_timeout: float, heartbeat_timeout: float) -> dict | None:
        """Serve until a submission arrives; None when the page never loads, goes quiet, or time runs out."""
        thread = threading.Thread(target=self._httpd.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True)
        thread.start()
        start = time.monotonic()
        try:
            while not self._done.wait(0.25):
                now = time.monotonic()
                if now - start > total_timeout:
                    return None
                if self.last_seen is None and now - start > load_timeout:
                    return None
                if self.last_seen is not None and now - self.last_seen > heartbeat_timeout:
                    return None
            return self.result
        finally:
            self._httpd.shutdown()
            self._httpd.server_close()

    def _handler_class(self) -> type[BaseHTTPRequestHandler]:
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - stdlib signature
                pass

            def _route(self) -> str | None:
                """Path after the token, or None when the request must be rejected."""
                if self.headers.get("Host") not in (f"127.0.0.1:{server.port}", f"localhost:{server.port}"):
                    return None
                prefix = f"/{server.token}/"
                path = self.path.split("?", 1)[0]
                if not path.startswith(prefix):
                    return None
                return path[len(prefix) :]

            def _send(self, status: int, body: bytes, content_type: str) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def _json(self, status: int, data: object) -> None:
                self._send(status, json.dumps(data).encode(), "application/json")

            def do_GET(self) -> None:  # noqa: N802 - stdlib name
                route = self._route()
                if route is None:
                    return self._send(404, b"not found", "text/plain")
                server.last_seen = time.monotonic()
                if route == "api/plan":
                    return self._json(200, server.payload)
                file = (server.static_dir / (route or "index.html")).resolve()
                if not file.is_file() or server.static_dir not in file.parents:
                    return self._send(404, b"not found", "text/plain")
                content_type = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
                if content_type.startswith("text/") or content_type.endswith("javascript"):
                    content_type += "; charset=utf-8"
                return self._send(200, file.read_bytes(), content_type)

            def do_POST(self) -> None:  # noqa: N802 - stdlib name
                route = self._route()
                length = int(self.headers.get("Content-Length") or 0)
                if route is None or length > MAX_BODY:
                    return self._send(404, b"not found", "text/plain")
                try:
                    data = json.loads(self.rfile.read(length) or b"{}")
                except json.JSONDecodeError:
                    return self._json(400, {"error": "invalid JSON"})
                if not isinstance(data, dict):
                    return self._json(400, {"error": "expected an object"})
                server.last_seen = time.monotonic()
                if route == "api/heartbeat":
                    return self._json(200, {"ok": True})
                if route == "api/draft":
                    server.save_draft(data)
                    return self._json(200, {"ok": True})
                if route == "api/submit":
                    if data.get("decision") not in ("revise", "approve", "terminal"):
                        return self._json(400, {"error": "unknown decision"})
                    server.result = data
                    self._json(200, {"ok": True})
                    server._done.set()
                    return None
                return self._send(404, b"not found", "text/plain")

        return Handler
