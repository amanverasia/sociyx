"""Browse & select: the core screen. Tabs per content type, live search, date range, conversation grouping,
and a selection set that survives filter changes. Selecting never changes anything on the platform."""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Button, DataTable, Footer, Input, OptionList, Static, Tab, Tabs
from textual.widgets.option_list import Option

from ...models import Account, ContentItem, to_iso
from ...store import Store
from ..app import FIXTURE_BANNER
from ..dialogs import DateRangeDialog, InfoDialog
from ..model import BrowseModel, group_of, one_line

ROW_CAP = 5000  # rendering more rows makes the table sluggish; filters narrow things down instead


def fmt_date(item: ContentItem) -> str:
    return item.created_at.strftime("%Y-%m-%d %H:%M") if item.created_at else "(unknown)"


class BrowseScreen(Screen):
    BINDINGS = [
        ("escape", "back", "Back"),
        ("space", "toggle", "Tick row"),
        ("a", "select_all", "Select shown"),
        ("n", "select_none", "Deselect shown"),
        ("i", "invert", "Invert shown"),
        ("x", "clear", "Clear all"),
        ("slash", "search", "Search"),
        ("d", "dates", "Dates"),
        ("c", "conversation", "Whole conversation"),
        ("r", "review", "Review"),
    ]

    def __init__(self, account: Account):
        super().__init__()
        self.account = account
        self.model: BrowseModel | None = None
        self._ids: list[str] = []  # item ids of the rendered rows, in order
        self._tab_types: list[str | None] = [None]
        self._group_keys: list[str | None] = [None]
        self._since_text = self._until_text = ""
        self._seen_resume = False
        self._tab_gen = 0

    # ---------------- layout ----------------

    def compose(self) -> ComposeResult:
        if self.account.platform == "fixture":
            yield Static(FIXTURE_BANNER, classes="banner")
        yield Static(Text(f"Browse {self.account.handle or self.account.account_id} ({self.account.platform})"),
                     classes="title")
        yield Tabs(id="tabs")
        with Horizontal():
            yield OptionList(id="groups")
            with Vertical():
                yield Input(placeholder="/ search text (case-insensitive)", id="search")
                yield DataTable(id="items", cursor_type="row")
        yield Static("", id="note", classes="hint")
        yield Static("", id="status", classes="status")
        with Horizontal(classes="buttons"):
            yield Button("Review →", id="review", variant="success", disabled=True)
            yield Button("Back", id="back")
        yield Footer()

    def on_mount(self) -> None:
        t = self.query_one("#items", DataTable)
        t.add_column("", key="mark", width=3)
        t.add_column("Date (UTC)", key="date", width=16)
        t.add_column("Type", key="type", width=8)
        t.add_column("Text", key="text")
        self.query_one("#groups").display = False
        self._load()
        t.focus()

    def on_screen_resume(self) -> None:
        if self._seen_resume:  # coming back from a run: stored items changed
            self._load()
        self._seen_resume = True

    def _load(self) -> None:
        store: Store = self.app.store  # type: ignore[attr-defined]
        cls = self.app.adapter_cls(self.account.platform)  # type: ignore[attr-defined]
        rows = store.query_items(self.account.platform, self.account.account_id)
        items = [Store.row_to_item(r) for r in rows]
        old = self.model
        self.model = BrowseModel(items, cls.deletable_types)
        if old:  # keep what the user picked, minus items that no longer exist
            self.model.selected = old.selected & set(self.model.by_id)
            self.model.query, self.model.since, self.model.until = old.query, old.since, old.until
        present = {i.content_type for i in items}
        self._tab_types = [None] + [t for t in cls.content_types if t in present] + sorted(present - set(cls.content_types))
        tabs = self.query_one("#tabs", Tabs)
        tabs.clear()
        self._tab_gen += 1  # tab removal is asynchronous, so a reload must not reuse ids
        for n, t in enumerate(self._tab_types):
            tabs.add_tab(Tab(t or "All", id=f"tab-{self._tab_gen}-{n}"))
        tabs.active = f"tab-{self._tab_gen}-0"
        self._refresh_groups()
        self.rebuild()

    # ---------------- rendering ----------------

    def _mark(self, item: ContentItem) -> Text:
        m = self.model
        assert m is not None
        if not m.can_select(item):
            return Text(" - ", style="dim")
        return Text("[x]", style="bold green") if item.item_id in m.selected else Text("[ ]")

    def rebuild(self) -> None:
        """Re-render the table from the model (called after any filter change)."""
        m = self.model
        assert m is not None
        t = self.query_one("#items", DataTable)
        keep = self._current_id()
        t.clear()
        shown = m.visible()[:ROW_CAP]
        self._ids = [i.item_id for i in shown]
        for i in shown:
            t.add_row(self._mark(i), Text(fmt_date(i)), Text(i.content_type), Text(one_line(i.text, 200)), key=i.item_id)
        if keep in self._ids:
            t.move_cursor(row=self._ids.index(keep))
        total = len(m.visible())
        self.query_one("#note", Static).update(
            Text(f"Showing the newest {ROW_CAP} of {total} matching items. Narrow with / or d. "
                 f"'a' still selects all {total}.") if total > ROW_CAP else "")
        self._update_status()

    def _update_marks(self, ids: set[str]) -> None:
        t = self.query_one("#items", DataTable)
        m = self.model
        assert m is not None
        for iid in ids & set(self._ids):
            t.update_cell(iid, "mark", self._mark(m.by_id[iid]))
        self._update_status()

    def _update_status(self) -> None:
        m = self.model
        assert m is not None
        parts = [f"{len(m.selected)} selected / {len(m.visible())} shown / {len(m.items)} total"]
        if m.hidden_selected:
            parts.append(f"{m.hidden_selected} selected but hidden by filters")
        if m.query:
            parts.append(f"search: {m.query!r}")
        if m.since or m.until:
            parts.append(f"dates: {self._since_text or '...'} to {self._until_text or '...'}")
        if m.group is not None:
            parts.append("one conversation")
        parts.append(f"{self.account.handle or self.account.account_id} ({self.account.platform})")
        self.query_one("#status", Static).update(Text("  |  ".join(parts)))
        self.query_one("#review", Button).disabled = not m.selected

    def _refresh_groups(self) -> None:
        m = self.model
        assert m is not None
        groups = m.groups()
        panel = self.query_one("#groups", OptionList)
        panel.clear_options()
        self._group_keys = [None]
        if groups:
            panel.add_option(Option(Text("All conversations"), id="g-all"))
            for n, g in enumerate(groups):
                self._group_keys.append(g.key)
                panel.add_option(Option(Text(f"{g.title} ({g.count})"), id=f"g-{n}"))
        panel.display = bool(groups)

    def _current_id(self) -> str | None:
        t = self.query_one("#items", DataTable)
        if not self._ids or not (0 <= t.cursor_row < len(self._ids)):
            return None
        return self._ids[t.cursor_row]

    # ---------------- filter events ----------------

    def on_tabs_tab_activated(self, event: Tabs.TabActivated) -> None:
        if self.model is None or event.tab.id is None:
            return
        self.model.set_type(self._tab_types[int(event.tab.id.rsplit("-", 1)[1])])
        self._refresh_groups()
        self.rebuild()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if self.model is not None:
            self.model.set_group(self._group_keys[event.option_index])
            self.rebuild()
            self.query_one("#items", DataTable).focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        if self.model is not None and event.input.id == "search":
            self.model.set_query(event.value)
            self.rebuild()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.query_one("#items", DataTable).focus()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        m = self.model
        iid = str(event.row_key.value)
        if m is None or iid not in m.by_id:
            return
        item = m.by_id[iid]
        g = group_of(item)
        fields = [("Type", item.content_type), ("Date (UTC)", fmt_date(item)), ("URL", item.url or "-"),
                  ("Item id", item.item_id)]
        if g:
            fields.append(("Conversation", g[1]))
        self.app.push_screen(InfoDialog("Item detail", fields + [("Text", item.text or "(no text)")]))

    # ---------------- actions ----------------

    def _report(self, what: str, changed: set[str], skipped: int) -> None:
        self._update_marks(changed)
        msg = f"{what}: {len(changed)} item(s)."
        if skipped:
            msg += f" {skipped} of a type that cannot be deleted here were left alone."
        self.notify(msg, markup=False)

    def action_toggle(self) -> None:
        iid = self._current_id()
        if iid is None or self.model is None:
            return
        if not self.model.toggle(iid):
            self.notify("This type of item cannot be deleted on this platform.", severity="warning", markup=False)
            return
        self._update_marks({iid})

    def action_select_all(self) -> None:
        if self.model:
            changed, skipped = self.model.select_visible()
            self._report("Selected", changed, skipped)

    def action_select_none(self) -> None:
        if self.model:
            changed, skipped = self.model.deselect_visible()
            self._report("Deselected", changed, skipped)

    def action_invert(self) -> None:
        if self.model:
            changed, skipped = self.model.invert_visible()
            self._report("Inverted", changed, skipped)

    def action_clear(self) -> None:
        if self.model:
            ids = set(self.model.selected)
            self.model.clear()
            self._update_marks(ids)

    def action_conversation(self) -> None:
        m = self.model
        if m is None or m.group is None:
            self.notify("Pick a conversation in the left list first.", severity="warning", markup=False)
            return
        changed, skipped = m.select_group(m.group)
        self._report("Selected whole conversation", changed, skipped)

    def action_search(self) -> None:
        self.query_one("#search", Input).focus()

    def action_dates(self) -> None:
        self.app.push_screen(DateRangeDialog(self._since_text, self._until_text), self._dates_chosen)

    def _dates_chosen(self, result) -> None:
        if result is None or self.model is None:
            return
        since, until = result
        self.model.set_dates(since, until)
        self._since_text = since.date().isoformat() if since else ""
        self._until_text = until.date().isoformat() if until else ""
        self.rebuild()

    def action_review(self) -> None:
        from .review import ReviewScreen

        m = self.model
        if m is None or not m.selected:
            self.notify("Select at least one item first.", severity="warning", markup=False)
            return
        items = m.selected_items()
        desc = {"content_types": sorted({i.content_type for i in items}), "search": m.query,
                "since": to_iso(m.since), "until": to_iso(m.until), "conversation_filter": m.group is not None}
        self.app.push_screen(ReviewScreen(self.account, items, desc))

    def action_back(self) -> None:
        if isinstance(self.focused, Input):
            self.query_one("#items", DataTable).focus()
        else:
            self.app.pop_screen()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "review":
            self.action_review()
        else:
            self.app.pop_screen()
