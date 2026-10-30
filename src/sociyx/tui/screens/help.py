"""Key reference (press ?)."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Static

HELP = """\
Everywhere
  ?            this help
  Escape       go back / close
  Ctrl+Q       quit (asks to pause a running job or stop a running scan first)

Home
  Enter        open the highlighted account      a  add account      r  resume jobs      q  quit

Browse & select
  Space        tick / untick the row           Enter  full item detail
  a            select all rows currently shown (after search, dates, tab, conversation)
  n            deselect the rows currently shown          i  invert the rows shown
  x            clear the whole selection (including rows hidden by filters)
  /            search text (live, case-insensitive)       d  date range
  c            select the whole highlighted conversation (direct messages)
  r            review the selection

Review
  Type  delete N  (N = number of items) and press Enter to start. Nothing is deleted before that.

Running
  p            pause after the current item     Cancel  stop for good (asks first)
"""


class HelpScreen(ModalScreen[None]):
    BINDINGS = [("escape", "close", "Close"), ("question_mark", "close", "Close")]

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            with VerticalScroll():
                yield Static(HELP, markup=False)
            yield Button("Close", id="close", variant="primary")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(None)

    def action_close(self) -> None:
        self.dismiss(None)
