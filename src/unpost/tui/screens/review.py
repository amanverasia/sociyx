"""Review: the exact items, what will happen, and the typed confirmation. The plan is created only when
the user leaves this screen to proceed (or to save it), never earlier."""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.screen import Screen
from textual.widgets import Button, DataTable, Footer, Input, Static

from ... import paths
from ...models import Account, ContentItem
from ...planning import create_plan_from_items, describe_pacing, execute_command
from ...redact import scrub
from ..app import FIXTURE_BANNER
from ..dialogs import InfoDialog
from ..model import group_of, one_line
from .home import HomeScreen

TYPE_WARNINGS = {
    "dm": "Unsending a direct message removes it for everyone in the conversation, not just for you.",
    "like": "Removing a like cannot be undone by this tool; you would have to like the post again by hand.",
    "repost": "Removing a repost cannot be undone by this tool.",
    "post": "Deleted posts disappear from your profile and cannot be restored by this tool.",
}


def describe_selection(items: list[ContentItem]) -> tuple[str, list[str]]:
    """(headline fragment like '12 items in 2 conversations', per-type count lines)."""
    by_type: dict[str, int] = {}
    convos: set[str] = set()
    for i in items:
        by_type[i.content_type] = by_type.get(i.content_type, 0) + 1
        g = group_of(i)
        if g:
            convos.add(g[0])
    if convos:
        groups = f"{len(convos)} conversation{'s' if len(convos) != 1 else ''}"
    else:
        groups = f"{len(by_type)} content type{'s' if len(by_type) != 1 else ''}"
    return f"{len(items)} item{'s' if len(items) != 1 else ''} in {groups}", [f"{n} {t}" for t, n in sorted(by_type.items())]


class ReviewScreen(Screen):
    BINDINGS = [("escape", "back", "Back")]

    def __init__(self, account: Account, items: list[ContentItem], filters_description: dict):
        super().__init__()
        self.account = account
        self.items = items
        self.desc = filters_description
        self._busy = False

    @property
    def phrase(self) -> str:
        return f"delete {len(self.items)}"

    def compose(self) -> ComposeResult:
        cls = self.app.adapter_cls(self.account.platform)  # type: ignore[attr-defined]
        headline, per_type = describe_selection(self.items)
        dates = sorted(i.created_at for i in self.items if i.created_at)
        undated = sum(1 for i in self.items if not i.created_at)
        span = f"{dates[0].date()} to {dates[-1].date()}" if dates else "no dates"
        who = f"{self.account.handle or self.account.account_id} ({self.account.platform})"
        if self.account.platform == "fixture":
            yield Static(FIXTURE_BANNER, classes="banner")
        yield Static(Text(f"Unsend/delete {headline} from {who}"), classes="title")
        yield Static(Text(f"{', '.join(per_type)}.  Dates: {span}" + (f" (+{undated} undated)" if undated else "")),
                     classes="hint")
        yield Static(Text(describe_pacing(cls, len(self.items))), classes="hint")
        warn = ["This cannot be undone. unpost cannot restore what it deletes, and the platform may not either."]
        warn += [TYPE_WARNINGS[t] for t in sorted({i.content_type for i in self.items}) if t in TYPE_WARNINGS]
        yield Static(Text("\n".join(warn)), classes="danger")
        yield DataTable(id="items", cursor_type="row")
        yield Static(Text(f"To start, type exactly:  {self.phrase}   then press Enter. Nothing has been deleted yet."),
                     classes="hint")
        yield Input(placeholder=self.phrase, id="phrase")
        yield Static("", id="msg", classes="error")
        with Horizontal(classes="buttons"):
            yield Button("Start deleting", id="go", variant="error")
            yield Button("Save plan and exit without deleting", id="save")
            yield Button("Back", id="back")
        yield Footer()

    def on_mount(self) -> None:
        t = self.query_one("#items", DataTable)
        t.add_columns("#", "Date (UTC)", "Type", "Item id", "Text")
        for n, i in enumerate(self.items, 1):
            t.add_row(Text(str(n)), Text(i.created_at.strftime("%Y-%m-%d %H:%M") if i.created_at else "(unknown)"),
                      Text(i.content_type), Text(i.item_id), Text(one_line(i.text, 80)))
        self.query_one("#phrase", Input).focus()

    def _message(self, text: str) -> None:
        self.query_one("#msg", Static).update(Text(text))

    def _create_plan(self):
        """Freeze exactly the selected items into a plan (writes the export first). None on failure."""
        app = self.app
        try:
            plan = create_plan_from_items(app.store, app.adapter_cls(self.account.platform),  # type: ignore[attr-defined]
                                          self.account, self.items, self.desc)
        except Exception as e:
            self._message(scrub(f"Could not create the plan: {e}"))
            return None
        if plan.item_count != len(self.items):
            self._message(f"Plan {plan.plan_id} holds {plan.item_count} items, not the {len(self.items)} you "
                          "selected, so nothing was started. Check it with `unpost plan show`.")
            return None
        return plan

    def _go(self) -> None:
        from .run import RunScreen

        if self._busy:
            return
        typed = self.query_one("#phrase", Input).value.strip()
        if typed != self.phrase:
            self._message(f"That is not the confirmation phrase. Type exactly: {self.phrase}")
            return
        self._busy = True
        plan = self._create_plan()
        if plan is None:
            self._busy = False
            return
        self.app.switch_screen(RunScreen(plan.plan_id))

    def _save(self) -> None:
        if self._busy:
            return
        plan = self._create_plan()
        if plan is None:
            return
        body = (f"Plan {plan.plan_id} saved with {plan.item_count} items. Nothing was deleted.\n"
                f"Export of exactly these items: {plan.export_path}\n"
                f"Review later: unpost plan show {plan.plan_id} --all\nTo delete: {execute_command(plan)}\n"
                f"(data folder: {paths.home()})")
        self.app.push_screen(InfoDialog("Plan saved", body), lambda _: self.app.pop_to(HomeScreen))  # type: ignore[attr-defined]

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._go()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        {"go": self._go, "save": self._save, "back": self.app.pop_screen}[event.button.id or ""]()

    def action_back(self) -> None:
        self.app.pop_screen()
