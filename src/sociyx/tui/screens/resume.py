"""Unfinished jobs and the lighter confirmation used to resume one."""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.screen import Screen
from textual.widgets import Button, DataTable, Footer, Static

from ...executor import job_is_running
from ...models import Outcome
from ...store import Store
from ..dialogs import ConfirmDialog

RESUMABLE = ("paused", "stopped_auth", "stopped_blocked", "running")


def unfinished_jobs(store: Store) -> list[dict]:
    """Jobs that can be resumed; a DB status of 'running' only counts if no process holds its lock."""
    out = []
    for j in store.list_jobs():
        if j["status"] not in RESUMABLE or (j["status"] == "running" and job_is_running(j["job_id"])):
            continue
        plan = store.get_plan(j["plan_id"])
        acct = store.get_account(plan.platform, plan.account_id)
        counts = store.outcome_counts(plan.plan_id)
        remaining = counts.get(Outcome.PENDING.value, 0) + counts.get(Outcome.IN_PROGRESS.value, 0)
        out.append({"job": j, "plan": plan, "handle": acct.handle if acct else plan.account_id,
                    "counts": counts, "remaining": remaining})
    return out


def counts_text(counts: dict[str, int]) -> str:
    return ", ".join(f"{n} {k.replace('_', ' ')}" for k, n in sorted(counts.items()) if n) or "nothing yet"


def confirm_resume(screen: Screen, plan_id: int) -> None:
    """Ask for a lighter confirmation (the plan was already confirmed when created), then re-enter Run."""
    from .run import RunScreen

    store = screen.app.store  # type: ignore[attr-defined]
    plan = store.get_plan(plan_id)
    acct = store.get_account(plan.platform, plan.account_id)
    counts = store.outcome_counts(plan_id)
    remaining = counts.get(Outcome.PENDING.value, 0) + counts.get(Outcome.IN_PROGRESS.value, 0)
    job = next((j for j in store.list_jobs() if j["plan_id"] == plan_id), None)
    body = (f"Plan {plan_id}: {plan.item_count} items from {acct.handle if acct else plan.account_id} "
            f"({plan.platform}).\nSo far: {counts_text(counts)}.\nRemaining to delete: {remaining}.\n"
            + (f"Last status: {job['status']}" + (f" - {job['error']}" if job["error"] else "") if job else ""))

    def answered(yes: bool | None) -> None:
        if yes:
            screen.app.push_screen(RunScreen(plan_id))

    screen.app.push_screen(
        ConfirmDialog("Resume this deletion job?", body, yes="Resume deleting", no="Not now", danger=True), answered)


class ResumeScreen(Screen):
    BINDINGS = [("escape", "app.pop_screen", "Back")]

    def compose(self) -> ComposeResult:
        yield Static("Unfinished jobs", classes="title")
        yield Static("Paused, stopped, or interrupted deletion jobs. If a job stopped because the login expired, "
                     "open the account and log in again first.", classes="hint")
        yield DataTable(id="jobs", cursor_type="row")
        with Horizontal(classes="buttons"):
            yield Button("Resume selected", id="resume", variant="primary")
            yield Button("Back", id="back")
        yield Footer()

    def on_mount(self) -> None:
        t = self.query_one("#jobs", DataTable)
        t.add_columns("Job", "Account", "Status", "Items", "Remaining", "Outcomes so far", "Message")
        self.rows = unfinished_jobs(self.app.store)  # type: ignore[attr-defined]
        for r in self.rows:
            j = r["job"]
            t.add_row(Text(str(j["job_id"])), Text(f"{r['handle']} ({r['plan'].platform})"), Text(j["status"]),
                      Text(str(r["plan"].item_count)), Text(str(r["remaining"])), Text(counts_text(r["counts"])),
                      Text((j["error"] or "")[:60]), key=str(r["plan"].plan_id))
        if not self.rows:
            self.notify("No unfinished jobs.", markup=False)
        t.focus()

    def _resume(self) -> None:
        t = self.query_one("#jobs", DataTable)
        if t.row_count:
            confirm_resume(self, int(t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value or 0))

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        self._resume()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "resume":
            self._resume()
        else:
            self.app.pop_screen()
