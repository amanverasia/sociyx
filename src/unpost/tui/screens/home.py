"""Home: known accounts, add account, resume unfinished jobs."""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.screen import Screen
from textual.widgets import Button, DataTable, Footer, Static

from ...models import Account
from ..dialogs import PlatformPicker


class HomeScreen(Screen):
    BINDINGS = [
        ("a", "add", "Add account"),
        ("r", "resume", "Resume jobs"),
        ("q", "app.quit", "Quit"),
    ]

    def compose(self) -> ComposeResult:
        if self.app.demo:  # type: ignore[attr-defined]
            yield Static("DEMO MODE: fake accounts in a scratch folder. Real data is not used.", classes="banner")
        yield Static("Your accounts", classes="title")
        yield Static("Pick an account and press Enter. Nothing is deleted until you review a selection and type "
                     "the confirmation phrase.", classes="hint")
        yield DataTable(id="accounts", cursor_type="row")
        with Horizontal(classes="buttons"):
            yield Button("Open account", id="open", variant="primary")
            yield Button("Add account", id="add")
            yield Button("Resume unfinished jobs", id="resume")
            yield Button("Quit", id="quit")
        yield Footer()

    def on_mount(self) -> None:
        t = self.query_one("#accounts", DataTable)
        t.add_columns("Platform", "Handle", "Last verified")
        t.focus()
        self.refresh_accounts()

    def on_screen_resume(self) -> None:
        self.refresh_accounts()

    def refresh_accounts(self) -> None:
        t = self.query_one("#accounts", DataTable)
        t.clear()
        rows = self.app.store.list_accounts()  # type: ignore[attr-defined]
        for r in rows:
            t.add_row(Text(r["platform"]), Text(r["handle"] or r["account_id"]),
                      Text(r["last_verified_at"] or "never"), key=f"{r['platform']}\n{r['account_id']}")
        if not rows:
            self.notify("No accounts yet. Choose 'Add account' to log in.", markup=False)

    def _selected_account(self) -> Account | None:
        t = self.query_one("#accounts", DataTable)
        if t.row_count == 0:
            return None
        key = t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value or ""
        platform, _, account_id = key.partition("\n")
        return self.app.store.get_account(platform, account_id)  # type: ignore[attr-defined]

    def action_open(self) -> None:
        from .account import AccountScreen

        acct = self._selected_account()
        if acct is None:
            self.notify("Add an account first.", markup=False)
            return
        self.app.push_screen(AccountScreen(acct))

    def action_add(self) -> None:
        self.app.push_screen(PlatformPicker(self.app.platforms()), self._picked)  # type: ignore[attr-defined]

    def _picked(self, choice: tuple[str, str | None] | None) -> None:
        if choice is None:
            return
        platform, hint = choice
        if self.app.login_flow(platform, hint):  # type: ignore[attr-defined]
            self.refresh_accounts()

    def action_resume(self) -> None:
        from .resume import ResumeScreen

        self.app.push_screen(ResumeScreen())

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        self.action_open()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        {"open": self.action_open, "add": self.action_add, "resume": self.action_resume,
         "quit": lambda: self.app.run_action("quit")}[event.button.id or ""]()
