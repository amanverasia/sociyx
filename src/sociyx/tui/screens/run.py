"""Run: the executor in a worker thread (own Store + adapter), live progress, pause/cancel, countdown."""

from __future__ import annotations

import time
from typing import Any, Callable

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.screen import Screen
from textual.widgets import Button, Footer, ProgressBar, RichLog, Static
from textual.worker import Worker, WorkerState

from ...executor import Executor, PlanRefused, cancel_job, pause_job
from ...models import Outcome, format_duration
from ...redact import scrub
from ..app import FIXTURE_BANNER
from ..dialogs import ConfirmDialog

LABELS = [("deleted_verified", "verified"), ("deleted_unverified", "unverified"), ("already_deleted", "already gone"),
          ("inaccessible", "inaccessible"), ("failed", "failed"), ("skipped", "skipped"),
          ("in_progress", "in flight"), ("pending", "pending")]


class RunScreen(Screen):
    BINDINGS = [("p", "pause", "Pause")]

    def __init__(self, plan_id: int):
        super().__init__()
        self.plan_id = plan_id
        self.job_id = 0
        self.running = False
        self._quit_after = False
        self._deadline = 0.0
        self._reason = ""

    def compose(self) -> ComposeResult:
        plan = self.app.store.get_plan(self.plan_id)  # type: ignore[attr-defined]
        self.plan = plan
        acct = self.app.store.get_account(plan.platform, plan.account_id)  # type: ignore[attr-defined]
        if plan.platform == "fixture":
            yield Static(FIXTURE_BANNER, classes="banner")
        yield Static(Text(f"Deleting {plan.item_count} items from {acct.handle if acct else plan.account_id} "
                          f"({plan.platform}), plan {plan.plan_id}"), classes="title")
        yield Static(Text(f"Export of exactly these items: {plan.export_path}"), classes="hint")
        yield ProgressBar(total=plan.item_count, show_eta=False, id="bar")
        yield Static("", id="counts", classes="status")
        yield Static("Starting...", id="state")
        yield Static("", id="countdown", classes="hint")
        yield RichLog(id="log", wrap=True, markup=False, max_lines=500)
        with Horizontal(classes="buttons"):
            yield Button("Pause", id="pause", variant="warning")
            yield Button("Cancel job", id="cancel", variant="error")
            back = Button("Back", id="back")
            back.display = False
            yield back
        yield Footer()

    def on_mount(self) -> None:
        store = self.app.store  # type: ignore[attr-defined]
        self.job_id = store.get_or_create_job(self.plan_id).job_id
        self._show_counts(store.outcome_counts(self.plan_id))
        self.running = True
        self.run_worker(self._work, thread=True, name="executor", exclusive=True)
        self.set_interval(0.5, self._tick)

    # ---------------- worker thread ----------------

    def _post(self, fn: Callable[..., None], *args: Any) -> None:
        try:
            self.app.call_from_thread(fn, *args)
        except Exception:
            pass  # UI already gone; the executor itself keeps going and persists everything

    def _work(self) -> tuple[str, Any]:
        """Executor thread. The confirmation token is passed here, internally, only after the user typed the
        phrase (new run) or confirmed the resume dialog. Own Store/adapter: nothing crosses threads."""
        app = self.app
        store = app.new_store()  # type: ignore[attr-defined]
        adapter = app.new_adapter(self.plan.platform)  # type: ignore[attr-defined]

        def progress(item_id: str, outcome: Outcome, detail: str) -> None:
            self._post(self._on_item, item_id, outcome, detail, store.outcome_counts(self.plan_id))
            hook = app.progress_hook  # type: ignore[attr-defined]
            if hook:
                hook(item_id, outcome, detail)

        try:
            ex = Executor(store, adapter, progress=progress, **{
                "on_wait": lambda s, why: self._post(self._on_wait, s, why),
                "on_state": lambda text: self._post(self._on_state, text),
                **app.executor_options})  # type: ignore[attr-defined]
            return "result", ex.run(self.plan_id, self.plan.confirmation_token)
        except PlanRefused as e:
            return "refused", scrub(str(e))
        except (Exception, KeyboardInterrupt) as e:
            return "error", scrub(f"{type(e).__name__}: {e}")
        finally:
            adapter.close()
            store.close()

    # ---------------- UI updates (UI thread) ----------------

    def _show_counts(self, counts: dict[str, int]) -> None:
        total = self.plan.item_count
        done = total - counts.get("pending", 0) - counts.get("in_progress", 0)
        self.query_one("#bar", ProgressBar).update(total=total, progress=done)
        text = f"{done}/{total} done   " + "  ".join(f"{label}: {counts.get(key, 0)}" for key, label in LABELS)
        self.query_one("#counts", Static).update(Text(text))

    def _on_item(self, item_id: str, outcome: Outcome, detail: str, counts: dict[str, int]) -> None:
        self._show_counts(counts)
        self.query_one("#log", RichLog).write(Text(f"{item_id}  {outcome.value}" + (f"  {detail}" if detail else "")))

    def _on_wait(self, seconds: float, reason: str) -> None:
        self._deadline, self._reason = time.monotonic() + seconds, reason

    def _on_state(self, text: str) -> None:
        self.query_one("#state", Static).update(Text(text))

    def _tick(self) -> None:
        left = self._deadline - time.monotonic()
        self.query_one("#countdown", Static).update(
            Text(f"Next action in {format_duration(left)} ({self._reason})") if self.running and left > 0 else "")

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.worker.name != "executor" or event.state not in (WorkerState.SUCCESS, WorkerState.ERROR):
            return
        self.running = False
        kind, payload = ("error", scrub(str(event.worker.error))) if event.state == WorkerState.ERROR \
            else event.worker.result
        if self._quit_after:
            self.app.exit()
            return
        if kind == "result":
            from .results import ResultsScreen

            self.app.switch_screen(ResultsScreen(self.plan_id))
            return
        self.query_one("#state", Static).update(Text(
            ("Not started: " if kind == "refused" else "Stopped unexpectedly: ") + payload
            + "\nNothing further was deleted. You can resume from Home > Resume unfinished jobs."))
        self.query_one("#state", Static).set_classes("error")
        for bid in ("pause", "cancel"):
            self.query_one(f"#{bid}", Button).display = False
        self.query_one("#back", Button).display = True

    # ---------------- controls ----------------

    def _control(self, fn: Callable[[Any, int], str]) -> str:
        """Use a separate short-lived connection: the executor's own connection lives in its thread."""
        store = self.app.new_store()  # type: ignore[attr-defined]
        try:
            return fn(store, self.job_id)
        finally:
            store.close()

    def action_pause(self) -> None:
        if not self.running:
            return
        self.notify(self._control(pause_job), markup=False)
        btn = self.query_one("#pause", Button)
        btn.disabled = True
        btn.label = "Pausing..."
        self.query_one("#state", Static).update("Pause requested: finishing the current item, then stopping.")

    def action_cancel(self) -> None:
        def answered(yes: bool | None) -> None:
            if yes and self.running:
                self.notify(self._control(cancel_job), markup=False)

        self.app.push_screen(ConfirmDialog(
            "Cancel this job?", "Remaining items will be marked skipped and the job cannot be resumed. "
            "Items already deleted stay deleted.", yes="Cancel the job", no="Keep going", danger=True), answered)

    def request_quit(self) -> bool:
        """Quit while deleting: never silently. Offer to pause first, then exit once the executor has stopped."""
        if not self.running:
            return False

        def answered(yes: bool | None) -> None:
            if yes:
                self._quit_after = True
                self.action_pause()

        self.app.push_screen(ConfirmDialog(
            "Pause the job first?", "A deletion job is running. sociyx will pause it after the current item "
            "(nothing is cut off mid-request) and then quit. Resume any time from Home.",
            yes="Pause and quit", no="Keep running"), answered)
        return True

    def on_button_pressed(self, event: Button.Pressed) -> None:
        {"pause": self.action_pause, "cancel": self.action_cancel, "back": self.app.pop_screen}[event.button.id or ""]()
