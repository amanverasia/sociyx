"""Local web UI: a third front end (next to the CLI and the Textual TUI) over the same engine.

Imported lazily by `sociyx ui`; other commands never pay for Starlette/uvicorn/Jinja2.
"""

from __future__ import annotations

import socket
import threading
import webbrowser


def run_web(demo: bool = False, port: int = 0, open_browser: bool = True) -> int:
    """Serve on 127.0.0.1 only. port=0 picks a random free port."""
    import logging

    import uvicorn

    from .app import create_app
    from .state import WebState

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("127.0.0.1", port))
    except OSError as e:
        print(f"error: cannot listen on 127.0.0.1:{port} ({e.strerror}); pick another --port.")
        return 2
    real_port = sock.getsockname()[1]
    state = WebState(port=real_port, demo=demo)
    app = create_app(state)
    url = f"http://127.0.0.1:{real_port}/?launch={state.launch_secret}"

    config = uvicorn.Config(app, log_level="warning", access_log=False, server_header=False, date_header=False,
                            proxy_headers=False, ws="none", lifespan="on")
    server = uvicorn.Server(config)
    logging.getLogger("uvicorn.access").disabled = True

    def opener() -> None:
        # Give the server a moment to start accepting, then hand the one-time URL to the browser.
        for _ in range(100):
            if server.started:
                break
            threading.Event().wait(0.05)
        if not webbrowser.open(url):
            print(f"Could not open a browser. Open this one-time link yourself:\n  {url}", flush=True)

    if open_browser:
        print(f"sociyx web UI on http://127.0.0.1:{real_port}/ (opening your browser; the link works once). "
              "Press Ctrl+C to stop.", flush=True)
        threading.Thread(target=opener, daemon=True).start()
    else:
        print(f"Open this one-time link in your browser (it works once, only on this computer):\n  {url}\n"
              "Press Ctrl+C to stop.", flush=True)
    try:
        server.run(sockets=[sock])
    finally:
        sock.close()
    return 0
