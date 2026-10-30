"""Scan screen: options, live progress and a Stop button that stops cleanly between pages."""

from __future__ import annotations

import threading
import time

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.screen import Screen
from textual.widgets import Button, Checkbox, Footer, Input, Select, Static
from textual.worker import Worker, WorkerState

from ...models import Account, ActionBlocked, AuthRequired
from ...redact import scrub
from ...scanning import ScanSummary, scan_account
from ..app import FIXTURE_BANNER


def summary_line(s: ScanSummary) -> str:
    state = "stopped (resumable)" if s.stopped else "partial (resumable)" if s.partial else s.status
    line = f"{s.content_type}: {state}; pages={s.pages} seen={s.items_seen} new={s.new_items} missing={s.marked_missing}"
    return line + (f"; error: {s.error}" if s.error else "") + (f"; warning: {s.warning}" if s.warning else "")


class ScanScreen(Screen):
    BINDINGS = [("escape", "back", "Back")]

    def __init__(self, account: Account):
        super().__init__()
        self.account = account
        self.stop_event = threading.Event()
        self.running = False
        self._quit_after = False

    def compose(self) -> ComposeResult:
        cls = self.app.adapter_cls(self.account.platform)  # type: ignore[attr-defined]
        lo, hi = cls.scan_delay
        if self.account.platform == "fixture":
            yield Static(FIXTURE_BANNER, classes="banner")
        yield Static(Text(f"Scan {self.account.handle or self.account.account_id}"), classes="title")
        yield Static("Scanning only READS your history into the local database. It never deletes anything.",
                     classes="hint")
        if hi > 0:
            yield Static(f"The scanner waits {lo:g}-{hi:g} s between pages on purpose, to look like normal use "
                         "and avoid being rate limited. Large accounts take a while.", classes="hint")
        yield Select([("All content types", "")] + [(f"{t}: {d}", t) for t, d in cls.content_types.items()],
                     value="", allow_blank=False, id="type")
        yield Input(placeholder="Pages to scan this time (empty = scan everything)", id="pages", type="integer")
        yield Checkbox("Start over instead of resuming an unfinished scan (--fresh)", id="fresh")
        yield Static("", id="progress", classes="status")
        yield Static("", id="result")
        with Horizontal(classes="buttons"):
            yield Button("Start scan", id="start", variant="primary")
            yield Button("Stop", id="stop", disabled=True)
            yield Button("Back", id="back")
        yield Footer()

    def _start(self) -> None:
        pages_text = self.query_one("#pages", Input).value.strip()
        max_pages = int(pages_text) if pages_text else None
        if max_pages is not None and max_pages < 1:
            self.notify("Pages must be 1 or more.", severity="warning", markup=False)
            return
        ctype = self.query_one("#type", Select).value
        types = [str(ctype)] if ctype else None
        fresh = self.query_one("#fresh", Checkbox).value
        self.stop_event.clear()
        self.running = True
        self.query_one("#start", Button).disabled = True
        self.query_one("#stop", Button).disabled = False
        self.query_one("#result", Static).update("")
        self.query_one("#progress", Static).update("Starting...")
        self.run_worker(lambda: self._scan(types, fresh, max_pages), thread=True, name="scan", exclusive=True)

    def _scan(self, types: list[str] | None, fresh: bool, max_pages: int | None) -> tuple:
        """Worker thread: own Store and adapter; all UI updates go through call_from_thread."""
        app = self.app
        store = app.new_store()  # type: ignore[attr-defined]
        adapter = app.new_adapter(self.account.platform)  # type: ignore[attr-defined]

        def progress(ctype: str, pages: int, n: int) -> None:
            try:
                app.call_from_thread(self._on_progress, ctype, pages, n)
            except RuntimeError:
                pass  # app is shutting down

        try:
            opts = {"sleep": time.sleep, **app.scan_options}  # type: ignore[attr-defined]
            done = scan_account(store, adapter, self.account, types, fresh=fresh, max_pages=max_pages,
                                progress=progress, should_stop=self.stop_event.is_set, **opts)
            return "done", done
        except ActionBlocked as e:
            return "error", scrub(f"The platform is restricting the account. {e.guidance()}")
        except AuthRequired as e:
            return "error", scrub(f"Login required ({e}). Go back and use 'Log in again'.")
        except Exception as e:
            return "error", scrub(f"{type(e).__name__}: {e}")
        finally:
            adapter.close()
            store.close()

    def _on_progress(self, ctype: str, pages: int, n: int) -> None:
        self.query_one("#progress", Static).update(Text(f"{ctype}: {pages} pages, {n} items found"))

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.worker.name != "scan" or event.state not in (WorkerState.SUCCESS, WorkerState.ERROR):
            return
        self.running = False
        self.query_one("#start", Button).disabled = False
        self.query_one("#stop", Button).disabled = True
        if event.state == WorkerState.ERROR:
            kind, payload = "error", scrub(str(event.worker.error))
        else:
            kind, payload = event.worker.result
        result = self.query_one("#result", Static)
        if kind == "error":
            result.update(Text(payload))
            result.set_classes("error")
        else:
            lines = [summary_line(s) for s in payload]
            if any(s.stopped for s in payload) or self.stop_event.is_set():
                lines.append("Stopped. Progress is saved; scan again to continue where it left off.")
            result.update(Text("\n".join(lines)))
            result.set_classes("ok")
        self.query_one("#progress", Static).update("")
        if self._quit_after:
            self.app.exit()

    def request_quit(self) -> bool:
        if not self.running:
            return False
        self._quit_after = True
        self.stop_event.set()
        self.notify("Stopping the scan after the current page, then quitting.", markup=False)
        return True

    def action_back(self) -> None:
        if self.running:
            self.notify("Press Stop first; the scan finishes its current page.", markup=False)
        else:
            self.app.pop_screen()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "start":
            self._start()
        elif event.button.id == "stop":
            self.stop_event.set()
            event.button.disabled = True
            self.query_one("#progress", Static).update("Stopping after the current page...")
        else:
            self.action_back()
