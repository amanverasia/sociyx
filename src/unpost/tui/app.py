"""The Textual application: shared services (store, adapters, quitting, login) used by every screen.

Threading model: the UI thread owns ``app.store``. Anything slow (scan, delete, session check) runs in a
worker *thread* that opens its own ``Store`` and its own adapter instance (SQLite connections and
adapter clients must not cross threads) and reports back only through ``call_from_thread``.
"""

from __future__ import annotations

import os
import threading
from typing import Any, Callable

from textual.app import App, SuspendNotSupported
from textual.binding import Binding
from textual.screen import Screen

from .. import paths
from ..adapters import ADAPTERS, Adapter, adapter_class, get_adapter
from ..models import Account
from ..redact import scrub
from ..store import Store
from .styles import CSS

FIXTURE_BANNER = "FIXTURE: fake data. Nothing real is touched."


class UnpostApp(App[None]):
    TITLE = "unpost"
    CSS = CSS
    BINDINGS = [
        Binding("ctrl+q", "quit", "Quit", priority=True),
        Binding("ctrl+c", "quit", "Quit", show=False),
        Binding("question_mark", "help", "Help"),
    ]

    def __init__(
        self,
        *,
        demo: bool = False,
        adapter_factory: Callable[..., Adapter] | None = None,
        executor_options: dict[str, Any] | None = None,
        scan_options: dict[str, Any] | None = None,
    ):
        super().__init__()
        self.demo = demo or os.environ.get("UNPOST_TUI_DEMO") == "1"
        self._adapter_factory = adapter_factory
        self.executor_options = executor_options or {}  # e.g. sleep/min_interval, for tests
        self.scan_options = scan_options or {}  # e.g. sleep, for tests
        self.progress_hook: Callable[[str, Any, str], None] | None = None  # test seam, runs in the worker
        self._store: Store | None = None

    # ---------------- services ----------------

    @property
    def store(self) -> Store:
        """The UI thread's connection. Workers must open their own (``new_store``)."""
        if self._store is None:
            self._store = Store(paths.db_path())
        return self._store

    @staticmethod
    def new_store() -> Store:
        return Store(paths.db_path())

    def adapter_cls(self, platform: str) -> type[Adapter]:
        if self.demo and platform == "fixture":
            from ..adapters.fixture import DemoFixtureAdapter

            return DemoFixtureAdapter
        return adapter_class(platform)

    def new_adapter(self, platform: str) -> Adapter:
        if self._adapter_factory is not None:
            return self._adapter_factory(platform)
        if self.demo and platform == "fixture":
            from ..adapters.fixture import DemoFixtureAdapter

            return DemoFixtureAdapter()
        return get_adapter(platform)

    def platforms(self) -> list[tuple[str, str]]:
        """(registry name, label) for platforms the user may add; fixture only in demo mode."""
        out = []
        for name in sorted(ADAPTERS):
            if name == "fixture" and not self.demo:
                continue
            try:
                out.append((name, self.adapter_cls(name).label))
            except Exception as e:  # optional dependency missing, etc.
                out.append((name, f"{name} (unavailable: {type(e).__name__})"))
        return out

    def notify_error(self, prefix: str, exc: BaseException | str) -> None:
        """Show an error without ever leaking credentials from exception text."""
        self.notify(scrub(f"{prefix}: {exc}"), severity="error", timeout=15, markup=False)

    # ---------------- interactive login ----------------

    def run_interactive(self, fn: Callable[[], Any]) -> Any:
        """Run `fn` with the real terminal (getpass prompts, visible browser).

        The UI is suspended and `fn` runs in a plain thread: Playwright's sync API refuses to run inside
        a thread that already has an asyncio loop, which the Textual UI thread does."""
        box: dict[str, Any] = {}

        def target() -> None:
            try:
                box["value"] = fn()
            except BaseException as e:  # re-raised in the caller
                box["error"] = e

        def run() -> None:
            t = threading.Thread(target=target, daemon=True)
            t.start()
            t.join()

        try:
            with self.suspend():
                run()
        except SuspendNotSupported:
            if not self.is_headless:
                raise
            run()  # headless (tests): nothing to suspend
        if "error" in box:
            raise box["error"]
        return box.get("value")

    def login_flow(self, platform: str, hint: str | None = None) -> Account | None:
        """Log in via the adapter (own terminal), store the verified account, notify on failure."""

        def work() -> Account:
            adapter = self.new_adapter(platform)
            try:
                return adapter.login(hint, None)
            finally:
                adapter.close()

        try:
            acct = self.run_interactive(work)
        except KeyboardInterrupt:
            self.notify("Login cancelled.")
            return None
        except Exception as e:
            self.notify_error("Login failed", e)
            return None
        self.store.upsert_account(acct)
        self.notify(f"Logged in as {acct.handle or acct.account_id}", markup=False)
        return acct

    # ---------------- navigation / quitting ----------------

    def on_mount(self) -> None:
        from .screens.home import HomeScreen

        self.push_screen(HomeScreen())

    def pop_to(self, screen_type: type[Screen]) -> bool:
        """Pop screens until one of `screen_type` is on top; False (and no change) if none is in the stack."""
        if not any(isinstance(s, screen_type) for s in self.screen_stack):
            return False
        while not isinstance(self.screen, screen_type):
            self.pop_screen()
        return True

    def action_help(self) -> None:
        from .screens.help import HelpScreen

        if not isinstance(self.screen, HelpScreen):
            self.push_screen(HelpScreen())

    async def action_quit(self) -> None:
        """Quit, but never abandon a running scan or deletion silently: busy screens get to intercept."""
        for screen in reversed(self.screen_stack):
            handler = getattr(screen, "request_quit", None)
            if handler is not None and handler():
                return
        self.exit()

    def on_unmount(self) -> None:
        if self._store is not None:
            self._store.close()
