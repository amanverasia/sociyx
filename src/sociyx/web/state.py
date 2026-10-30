"""Server-wide state of the local web UI: launch secret, sessions, background jobs.

Everything is in memory and single-user. Sessions hold the CSRF token and the per-account selection
(a BrowseModel each); background jobs (scan, deletion run, login) are registered here so pages can poll them.
"""

from __future__ import annotations

import secrets
import threading
import time
from typing import Any, Callable

from .. import paths
from ..adapters import ADAPTERS, Adapter, adapter_class, get_adapter
from ..service import BrowseModel
from ..store import Store

SESSION_LIFETIME_S = 12 * 3600


class Session:
    def __init__(self) -> None:
        self.sid = secrets.token_urlsafe(32)
        self.csrf = secrets.token_urlsafe(32)
        self.created = time.monotonic()
        self.lock = threading.RLock()
        self.models: dict[tuple[str, str], BrowseModel] = {}

    def expired(self) -> bool:
        return time.monotonic() - self.created > SESSION_LIFETIME_S


class WebState:
    def __init__(
        self,
        *,
        port: int,
        demo: bool = False,
        adapter_factory: Callable[..., Adapter] | None = None,
        executor_options: dict[str, Any] | None = None,
        scan_options: dict[str, Any] | None = None,
        launch_secret: str | None = None,
    ):
        self.port = port
        self.demo = demo
        self.adapter_factory = adapter_factory  # test seam: factory(platform, **options) -> Adapter
        self.executor_options = executor_options or {}
        self.scan_options = scan_options or {}
        self.progress_hook: Callable[[str, Any, str], None] | None = None  # test seam, runs in the worker
        self._launch_secret: str | None = launch_secret or secrets.token_urlsafe(32)
        self._lock = threading.Lock()
        self.sessions: dict[str, Session] = {}
        self.scans: dict[tuple[str, str], Any] = {}
        self.runs: dict[int, Any] = {}
        self.logins: dict[str, Any] = {}

    # ---------------- launch secret / sessions ----------------

    @property
    def launch_secret(self) -> str | None:
        """Only for the code that prints/opens the launch URL. None once it has been used."""
        return self._launch_secret

    def exchange_launch_secret(self, candidate: str) -> Session | None:
        """Single use: a correct secret creates a session and is invalidated immediately."""
        with self._lock:
            current = self._launch_secret
            if current is None or not candidate or not secrets.compare_digest(candidate.encode(), current.encode()):
                return None
            self._launch_secret = None
            sess = Session()
            self.sessions[sess.sid] = sess
            return sess

    def session_for(self, sid: str | None) -> Session | None:
        if not sid:
            return None
        with self._lock:
            sess = self.sessions.get(sid)
            if sess is not None and sess.expired():
                del self.sessions[sid]
                return None
            return sess

    def allowed_hosts(self) -> set[str]:
        return {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}

    # ---------------- services (mirror SociyxApp) ----------------

    @staticmethod
    def new_store() -> Store:
        return Store(paths.db_path())

    def adapter_cls(self, platform: str) -> type[Adapter]:
        if self.demo and platform == "fixture":
            from ..adapters.fixture import DemoFixtureAdapter

            return DemoFixtureAdapter
        return adapter_class(platform)

    def new_adapter(self, platform: str, **options: Any) -> Adapter:
        if self.adapter_factory is not None:
            return self.adapter_factory(platform, **options)
        if self.demo and platform == "fixture":
            from ..adapters.fixture import DemoFixtureAdapter

            return DemoFixtureAdapter()
        if platform == "fixture":
            return get_adapter(platform)
        return get_adapter(platform, **options)

    def platforms(self) -> list[str]:
        return [n for n in sorted(ADAPTERS) if n != "fixture" or self.demo]
