"""Results: the report by outcome section, the mandatory disclaimer, and what to do next."""

from __future__ import annotations

from datetime import datetime, timezone

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Collapsible, Footer, Static

from ...export import write_private_file
from ...service import RESUMABLE, status_message  # noqa: F401
from ...planning import execute_command
from ...report import build_report, render_report
from ..app import FIXTURE_BANNER
from .account import AccountScreen
from .home import HomeScreen
from .resume import confirm_resume

SECTION_ROWS = 300  # per section; the saved report file has everything


class ResultsScreen(Screen):
    BINDINGS = [("escape", "home", "Home")]

    def __init__(self, plan_id: int):
        super().__init__()
        self.plan_id = plan_id

    def compose(self) -> ComposeResult:
        app = self.app
        self.rep = rep = build_report(app.store, self.plan_id)  # type: ignore[attr-defined]
        self.plan = app.store.get_plan(self.plan_id)  # type: ignore[attr-defined]
        job = rep["job"]
        msg, css = status_message(job)
        if rep["platform"] == "fixture":
            yield Static(FIXTURE_BANNER, classes="banner")
        yield Static(Text(f"Results for plan {self.plan_id}: {rep['handle']} ({rep['platform']})"), classes="title")
        yield Static(Text(f"Job status: {job['status'] if job else 'not started'}"), classes="status")
        yield Static(Text(msg), classes=css)
        with VerticalScroll(id="sections"):
            summary = "\n".join(f"{len(s['items']):>6}  {s['title']}" for s in rep["sections"].values())
            yield Static(Text(summary))
            for sec in rep["sections"].values():
                if not sec["items"]:
                    continue
                with Collapsible(title=f"{sec['title']} ({len(sec['items'])})"):
                    lines = []
                    for it in sec["items"][:SECTION_ROWS]:
                        lines.append(f"{it['item_id']} [{it['content_type']}]" + (f" - {it['detail']}" if it["detail"] else ""))
                    if len(sec["items"]) > SECTION_ROWS:
                        lines.append(f"... and {len(sec['items']) - SECTION_ROWS} more (save the report to see all)")
                    yield Static(Text("\n".join(lines)))
            if job and job["status"] in RESUMABLE:
                yield Static(Text("To resume: Home, 'Resume unfinished jobs', or run\n  " + execute_command(self.plan)),
                             classes="hint")
            yield Static(Text("Platform history notes: " + rep["discovery_notes"]), classes="hint")
            yield Static(Text(rep["disclaimer"]), id="disclaimer", classes="status")
        with Horizontal(classes="buttons"):
            if job and job["status"] in RESUMABLE:
                yield Button("Resume", id="resume", variant="warning")
            yield Button("Save report", id="save")
            yield Button("Back to account", id="account")
            yield Button("Home", id="home", variant="primary")
        yield Footer()

    def _save(self) -> None:
        from ... import paths

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = paths.exports_dir() / f"report-plan-{self.plan_id}-{stamp}.txt"
        write_private_file(path, render_report(self.rep, "text"))
        self.notify(f"Report saved to {path}", markup=False)

    def action_home(self) -> None:
        self.app.pop_to(HomeScreen)  # type: ignore[attr-defined]

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "home":
            self.action_home()
        elif bid == "account":
            if not self.app.pop_to(AccountScreen):  # type: ignore[attr-defined]
                self.action_home()  # reached via Resume: no account screen underneath
        elif bid == "save":
            self._save()
        elif bid == "resume":
            confirm_resume(self, self.plan_id)
