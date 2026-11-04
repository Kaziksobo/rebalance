import contextlib
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from rebalance.collect import Settings

TEST_KEY = "TESTKEY123"


@pytest.fixture
def write_env(tmp_path: Path) -> Callable[..., Path]:
    def _write(content: str, folder: Path | None = None) -> Path:
        path = (folder or tmp_path) / ".env"
        path.write_text(content, encoding="utf-8")
        return path

    return _write


@pytest.fixture
def make_settings(tmp_path: Path) -> Callable[..., Settings]:
    def _make(**overrides: Any) -> Settings:
        base = Settings(api_key=TEST_KEY, data_dir=tmp_path / "data")
        return replace(base, **overrides)

    return _make


@dataclass
class FakeServer:
    base_url: str
    seen: list[dict[str, str]]

    def url(self, path: str) -> str:
        return f"{self.base_url}{path}"


@pytest.fixture
def fake_server() -> Iterator[FakeServer]:
    seen: list[dict[str, str]] = []

    class Handler(BaseHTTPRequestHandler):
        def _reply(
            self, status: int, body: bytes, content_type: str = "text/plain"
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            # Suppress logging to stderr during tests
            pass

        def do_GET(self) -> None:
            seen.append(
                {"path": self.path, "user_agent": self.headers.get("User-Agent", "")}
            )
            route = self.path.split("?")[0]
            if route == "/ok":
                self._reply(200, b'[{"a": 1}]', "application/json")
            elif route == "/forbidden":
                self._reply(403, b"<html>Forbidden</html>", "text/html")
            elif route == "/empty":
                self._reply(200, b"", "application/json")
            elif route == "/baddata":
                self._reply(200, b"[\xff\xfe]", "application/json")
            elif route == "/large":
                self._reply(200, b"[" + b"1," * 100_000 + b"1]", "application/json")
            elif route == "/hang":
                time.sleep(2)
                with contextlib.suppress(OSError):
                    self._reply(200, b"[]", "application/json")
            elif route == "/truncated":
                self.send_response(200)
                self.send_header("Content-Length", "1000")
                self.end_headers()
                self.wfile.write(b"[1,2")  # promise 1000 bytes, send 4, then close
            elif route == "/echo":
                self._reply(403, self.path.encode(), "text/plain")
            else:
                self._reply(404, b"not found")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    try:
        yield FakeServer(f"http://127.0.0.1:{server.server_port}", seen)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
