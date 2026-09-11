"""The double-click launcher. Uses only localhost sockets, never the internet."""

from __future__ import annotations

import socket
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from app import launcher


class _AppHandler(BaseHTTPRequestHandler):
    """Answers /api/health the way the real app does."""

    body = b'{"status":"ok","version":"test"}'

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        if self.path == "/api/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(self.body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *_args: object) -> None:
        pass


class _OtherProgramHandler(_AppHandler):
    body = b"<html>some other web server</html>"


def _serve(handler: type[BaseHTTPRequestHandler]) -> Iterator[int]:
    server = HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture()
def running_app() -> Iterator[int]:
    yield from _serve(_AppHandler)


@pytest.fixture()
def other_program() -> Iterator[int]:
    yield from _serve(_OtherProgramHandler)


@pytest.fixture()
def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture()
def recorder(monkeypatch: pytest.MonkeyPatch) -> dict[str, list]:
    calls: dict[str, list] = {"browser": [], "server": []}
    monkeypatch.setattr(launcher.webbrowser, "open", calls["browser"].append)
    monkeypatch.setattr(launcher, "_run_server", calls["server"].append)
    return calls


class TestDetection:
    def test_nothing_on_the_port(self, free_port: int) -> None:
        assert not launcher.health_ok(free_port)
        assert not launcher.port_in_use(free_port)

    def test_recognises_the_running_app(self, running_app: int) -> None:
        assert launcher.health_ok(running_app)
        assert launcher.port_in_use(running_app)

    def test_another_web_server_is_not_mistaken_for_the_app(self, other_program: int) -> None:
        assert not launcher.health_ok(other_program)
        assert launcher.port_in_use(other_program)


class TestMain:
    def test_reuses_a_running_app_instead_of_starting_twice(
        self, running_app: int, recorder: dict[str, list]
    ) -> None:
        assert launcher.main(["--port", str(running_app)]) == 0
        assert recorder["server"] == []
        assert recorder["browser"] == [f"http://localhost:{running_app}"]

    def test_refuses_a_port_held_by_another_program(
        self, other_program: int, recorder: dict[str, list], capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert launcher.main(["--port", str(other_program)]) == 1
        assert recorder["server"] == []
        assert "annet program" in capsys.readouterr().out

    def test_starts_the_server_on_a_free_port(
        self, free_port: int, recorder: dict[str, list]
    ) -> None:
        assert launcher.main(["--port", str(free_port), "--no-browser"]) == 0
        assert recorder["server"] == [free_port]
        assert recorder["browser"] == []
