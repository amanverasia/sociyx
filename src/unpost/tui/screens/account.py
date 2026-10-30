"""Account dashboard: what is stored locally per content type, session check, scan and browse."""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.screen import Screen
from textual.widgets import Button, DataTable, Footer, Static
from textual.worker import Worker, WorkerState

from ...models import Account, ActionBlocked, AuthRequired
from ...redact import scrub
from ..app import FIXTURE_BANNER
from ..dialogs import InfoDialog


def scan_label(row) -> str:
    if row is None:
        return "never scanned"
    status = {"complete": "complete", "failed": "FAILED"}.get(row["status"], "partial (resumable)")
    return f"{status}, {row['items_seen']} seen" + (f" ({row['error']})" if row["error"] else "")


def type_rows(store, cls, account: Account) -> list[tuple]:
    """(type, description, stored, date range, scan status) per content type, including unexpected stored types."""
    counts: dict[str, dict] = {}
    for r in store.item_counts(account.platform, account.account_id):
        c = counts.setdefault(r["content_type"], {"present": 0, "oldest": None, "newest": None})
        if r["state"] == "present":
            c["present"] += r["n"]
            c["oldest"] = min(filter(None, [c["oldest"], r["oldest"]]), default=None)
            c["newest"] = max(filter(None, [c["newest"], r["newest"]]), default=None)
    scans = {s["content_type"]: s for s in store.latest_scans(account.platform, account.account_id)}
    types = list(cls.content_types) + [t for t in counts if t not in cls.content_types]
    out = []
    for t in types:
        c = counts.get(t, {"present": 0, "oldest": None, "newest": None})
        span = f"{c['oldest'][:10]} .. {c['newest'][:10]}" if c["oldest"] and c["newest"] else "-"
        out.append((t, cls.content_types.get(t, ""), c["present"], span, scan_label(scans.get(t))))
    return out


class AccountScreen(Screen):
    BINDINGS = [("escape", "app.pop_screen", "Back"), ("s", "scan", "Scan"), ("b", "browse", "Browse & select"),
                ("p", "notes", "Platform notes")]

    def __init__(self, account: Account):
        super().__init__()
        self.account = account
        self.session_ok = False

    def compose(self) -> ComposeResult:
        if self.account.platform == "fixture":
            yield Static(FIXTURE_BANNER, classes="banner")
        yield Static(Text(f"{self.account.handle or self.account.account_id}  ({self.account.platform})"), classes="title")
        yield Static("Checking the saved session...", id="session", classes="hint")
        yield DataTable(id="types", cursor_type="row")
        with Horizontal(classes="buttons"):
            yield Button("Scan", id="scan", variant="primary", disabled=True)
            yield Button("Browse & select", id="browse", variant="success")
            yield Button("Platform notes", id="notes")
            login = Button("Log in again", id="login")
            login.display = False
            yield login
            yield Button("Back", id="back")
        yield Footer()

    def on_mount(self) -> None:
        t = self.query_one("#types", DataTable)
        t.add_columns("Type", "What", "Stored here", "Dates", "Latest scan")
        self.refresh_types()
        self.run_worker(self._verify, thread=True, name="verify", exclusive=True)

    def on_screen_resume(self) -> None:
        self.refresh_types()

    def refresh_types(self) -> None:
        t = self.query_one("#types", DataTable)
        t.clear()
        cls = self.app.adapter_cls(self.account.platform)  # type: ignore[attr-defined]
        for typ, what, n, span, scan in type_rows(self.app.store, cls, self.account):  # type: ignore[attr-defined]
            t.add_row(Text(typ), Text(what), Text(str(n)), Text(span), Text(scan))

    # ---- session re-verification (worker thread: own adapter, no shared state) ----

    def _verify(self) -> tuple[str, str, Account | None]:
        adapter = self.app.new_adapter(self.account.platform)  # type: ignore[attr-defined]
        try:
            live = adapter.current_account(self.account.account_id)
            if live.account_id != self.account.account_id:
                return "mismatch", "the saved session belongs to a different account; refusing to use it", None
            return "ok", "", live
        except AuthRequired as e:
            return "auth", scrub(str(e)), None
        except ActionBlocked as e:
            return "blocked", scrub(f"{e}. {e.guidance()}"), None
        except Exception as e:
            return "error", scrub(f"{type(e).__name__}: {e}"), None
        finally:
            adapter.close()

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.worker.name != "verify" or event.state != WorkerState.SUCCESS:
            return
        kind, msg, live = event.worker.result
        status = self.query_one("#session", Static)
        if kind == "ok" and live is not None:
            self.session_ok = True
            self.app.store.upsert_account(live)  # type: ignore[attr-defined]
            status.update(Text("Session verified with the platform.", style="green"))
            status.set_classes("ok")
            self.query_one("#scan", Button).disabled = False
            return
        status.set_classes("error")
        if kind == "auth":
            status.update(Text(f"Not logged in ({msg}). Use 'Log in again'; browsing saved items still works."))
            self.query_one("#login", Button).display = True
        else:
            status.update(Text(f"Could not verify the session: {msg}"))
            self.query_one("#login", Button).display = kind == "error"

    # ---- actions ----

    def action_scan(self) -> None:
        from .scan import ScanScreen

        if not self.session_ok:
            self.notify("Session not verified; log in again first.", severity="warning", markup=False)
            return
        self.app.push_screen(ScanScreen(self.account))

    def action_browse(self) -> None:
        from .browse import BrowseScreen

        self.app.push_screen(BrowseScreen(self.account))

    def action_notes(self) -> None:
        cls = self.app.adapter_cls(self.account.platform)  # type: ignore[attr-defined]
        self.app.push_screen(InfoDialog(f"{cls.label}: what history is available", cls.discovery_notes))

    def _login_again(self) -> None:
        if self.app.login_flow(self.account.platform, self.account.handle or None):  # type: ignore[attr-defined]
            self.query_one("#login", Button).display = False
            self.query_one("#session", Static).update("Checking the saved session...")
            self.run_worker(self._verify, thread=True, name="verify", exclusive=True)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        {"scan": self.action_scan, "browse": self.action_browse, "notes": self.action_notes,
         "login": self._login_again, "back": self.app.pop_screen}[event.button.id or ""]()
