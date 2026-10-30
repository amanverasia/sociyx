"""Small modal dialogs shared by several screens."""

from __future__ import annotations

from datetime import datetime

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, OptionList, Static
from textual.widgets.option_list import Option

from ..filters import parse_bound


class ConfirmDialog(ModalScreen[bool]):
    """Yes/No question. Escape means No, so a stray key never confirms anything."""

    BINDINGS = [("escape", "no", "No")]

    def __init__(self, title: str, body: str, yes: str = "Yes", no: str = "No", danger: bool = False):
        super().__init__()
        self._title, self._body, self._yes, self._no, self._danger = title, body, yes, no, danger

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Static(Text(self._title, style="bold"))
            yield Static(Text(self._body), classes="danger" if self._danger else "")
            with Horizontal(classes="buttons"):
                yield Button(self._yes, id="yes", variant="error" if self._danger else "primary")
                yield Button(self._no, id="no")

    def on_mount(self) -> None:
        self.query_one("#no", Button).focus()  # the safe answer is the default

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")

    def action_no(self) -> None:
        self.dismiss(False)


class InfoDialog(ModalScreen[None]):
    """Scrollable read-only text (item detail, platform notes)."""

    BINDINGS = [("escape", "close", "Close"), ("enter", "close", "Close")]

    def __init__(self, title: str, lines: list[tuple[str, str]] | str):
        super().__init__()
        self._title, self._lines = title, lines

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Static(Text(self._title, style="bold"))
            with VerticalScroll():
                if isinstance(self._lines, str):
                    yield Static(Text(self._lines))
                else:
                    for label, value in self._lines:
                        yield Static(Text.assemble((f"{label}: ", "bold"), value))
            yield Button("Close", id="close", variant="primary")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(None)

    def action_close(self) -> None:
        self.dismiss(None)


class DateRangeDialog(ModalScreen["tuple[datetime | None, datetime | None] | None"]):
    """since/until, parsed exactly like the CLI's --since/--until (bare date = start / end of day, UTC)."""

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, since: str = "", until: str = ""):
        super().__init__()
        self._since, self._until = since, until

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Static(Text("Date range (UTC)", style="bold"))
            yield Static("Since (inclusive), YYYY-MM-DD or ISO datetime; empty = no limit")
            yield Input(self._since, id="since", placeholder="2021-01-01")
            yield Static("Until (inclusive; a bare date means the end of that day)")
            yield Input(self._until, id="until", placeholder="2021-12-31")
            yield Static("", id="err", classes="error")
            with Horizontal(classes="buttons"):
                yield Button("Apply", id="apply", variant="primary")
                yield Button("Clear", id="clear")
                yield Button("Cancel", id="cancel")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._apply()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "apply":
            self._apply()
        elif event.button.id == "clear":
            self.dismiss((None, None))
        else:
            self.dismiss(None)

    def _apply(self) -> None:
        try:
            since = self.query_one("#since", Input).value.strip()
            until = self.query_one("#until", Input).value.strip()
            lo = parse_bound(since, end=False) if since else None
            hi = parse_bound(until, end=True) if until else None
            if lo and hi and lo > hi:
                raise ValueError("since is after until")
        except ValueError as e:
            self.query_one("#err", Static).update(Text(f"Cannot use that: {e}"))
            return
        self.dismiss((lo, hi))

    def action_cancel(self) -> None:
        self.dismiss(None)


class PlatformPicker(ModalScreen["tuple[str, str | None] | None"]):
    """Choose a platform (by label) and optionally an account hint passed to adapter.login."""

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, platforms: list[tuple[str, str]]):
        super().__init__()
        self._platforms = platforms

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Static(Text("Add account: choose a platform", style="bold"))
            yield OptionList(*[Option(Text(label), id=name) for name, label in self._platforms], id="platforms")
            yield Static("Account handle (optional, some platforms ask in the terminal anyway)")
            yield Input(id="hint", placeholder="leave empty to be asked")
            yield Static("Login happens in your terminal (password prompt or a visible browser window).", classes="hint")
            with Horizontal(classes="buttons"):
                yield Button("Cancel", id="cancel")

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        hint = self.query_one("#hint", Input).value.strip() or None
        self.dismiss((event.option.id or "", hint))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)
