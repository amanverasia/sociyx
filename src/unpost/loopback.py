"""Shared loopback login helpers: a single-use 127.0.0.1 callback server and a visible browser."""

from __future__ import annotations

import sys
import threading
import webbrowser
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from .paths import private_dir


class _Server(HTTPServer):
    params: dict[str, str] | None = None
    done: threading.Event


class _Handler(BaseHTTPRequestHandler):
    server: _Server

    def log_message(self, format: str, *args: Any) -> None:  # silence: URLs contain the code
        pass

    def _reply(self, status: int, text: str) -> None:
        body = f"<!doctype html><title>unpost</title><p>{text}</p>".encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parts = urlsplit(self.path)
        if parts.path != "/callback":
            self._reply(404, "Not found.")
        elif self.server.params is not None:
            self._reply(410, "This login link was already used.")  # single-use
        else:
            self.server.params = dict(parse_qsl(parts.query))
            self._reply(200, "You can close this window and return to the terminal.")
            self.server.done.set()


class CallbackServer:
    """Loopback server on 127.0.0.1: `preferred_port` when free (else ephemeral)."""

    def __init__(self, preferred_port: int | None = None) -> None:
        server: _Server | None = None
        if preferred_port:
            try:
                server = _Server(("127.0.0.1", preferred_port), _Handler)
            except OSError:
                server = None
        self._server = server or _Server(("127.0.0.1", 0), _Handler)
        self._server.done = threading.Event()
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    @property
    def redirect_uri(self) -> str:
        return f"http://127.0.0.1:{self.port}/callback"

    def start(self) -> None:
        self._thread.start()

    def wait(self, timeout: float) -> dict[str, str] | None:
        return self._server.params if self._server.done.wait(timeout) else None

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()


def open_visible_browser(url: str, profile_dir: Path | None = None) -> Callable[[], None] | None:
    """Visible Playwright Chromium (persistent profile in `profile_dir` when given, which is
    treated as a credential), else the system browser. Returns a cleanup callable, or None
    when the system browser was used. The URL must not be secret."""
    try:
        from playwright.sync_api import sync_playwright

        pw = sync_playwright().start()
        try:
            if profile_dir is not None:
                ctx = pw.chromium.launch_persistent_context(str(private_dir(profile_dir)), headless=False)
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                closers = (ctx.close, pw.stop)
            else:
                browser = pw.chromium.launch(headless=False)
                page = browser.new_page()
                closers = (browser.close, pw.stop)
            page.goto(url)
        except Exception:
            pw.stop()
            raise

        def close() -> None:
            for fn in closers:
                try:
                    fn()
                except Exception:
                    pass

        return close
    except Exception:
        print("Could not start the Playwright browser; opening your default browser instead.", file=sys.stderr)
        print(f"If nothing opens, visit this URL:\n{url}", file=sys.stderr)
        webbrowser.open(url)
        return None
