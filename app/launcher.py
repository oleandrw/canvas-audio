"""Double-click launcher for Canvas til lyd.

``start.bat`` runs this. It reuses an instance that is already running instead
of starting a second one, refuses a port another program holds, and otherwise
starts the server and opens the browser as soon as the app answers.

    python -m app.launcher [--port 8000] [--no-browser]
"""

from __future__ import annotations

import argparse
import socket
import sys
import threading
import time
import urllib.request
import webbrowser

HOST = "127.0.0.1"
DEFAULT_PORT = 8000
STARTUP_TIMEOUT = 60.0

# Talk to localhost directly, even if a system-wide proxy is configured.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def health_ok(port: int, timeout: float = 1.0) -> bool:
    """True when *this* app answers on ``port``, not just any web server."""
    try:
        with _OPENER.open(f"http://{HOST}:{port}/api/health", timeout=timeout) as response:
            return response.status == 200 and b'"status":"ok"' in response.read(512)
    except (OSError, ValueError):
        return False


def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        return probe.connect_ex((HOST, port)) == 0


def open_when_ready(url: str, port: int, timeout: float = STARTUP_TIMEOUT) -> None:
    """Open the browser once the app answers, so it never shows a connection error."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if health_ok(port):
            webbrowser.open(url)
            return
        time.sleep(0.5)


def _run_server(port: int) -> None:
    import uvicorn

    uvicorn.run("app.main:app", host=HOST, port=port)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Start Canvas til lyd og åpne nettleseren.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--no-browser", action="store_true", help="Ikke åpne nettleseren.")
    args = parser.parse_args(argv)

    url = f"http://localhost:{args.port}"

    if health_ok(args.port):
        print(f"Canvas til lyd kjører allerede på {url}.")
        if not args.no_browser:
            webbrowser.open(url)
        return 0

    if port_in_use(args.port):
        print(f"Port {args.port} brukes allerede av et annet program.")
        print(f"Start på en annen port, for eksempel:  start.bat --port {args.port + 1}")
        return 1

    print(f"Starter Canvas til lyd på {url}")
    print("Lukk dette vinduet, eller trykk Ctrl+C, for å stoppe.")
    print()
    if not args.no_browser:
        threading.Thread(target=open_when_ready, args=(url, args.port), daemon=True).start()
    _run_server(args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
