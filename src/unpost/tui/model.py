"""Pure (UI-free) state for the Browse screen: filtering, grouping and the selection set."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from ..filters import Filter
from ..models import ContentItem

# (raw key identifying a group, raw key holding its display title). Generic on purpose: any adapter that
# records these keys in ContentItem.raw (Instagram DMs do) gets conversation grouping for free.
GROUP_KEYS = (("thread_id", "thread_title"),)


@dataclass(frozen=True)
class Group:
    key: str
    title: str
    count: int


def group_of(item: ContentItem) -> tuple[str, str] | None:
    """(group key, title) when the item's raw metadata says it belongs to a conversation."""
    for id_key, title_key in GROUP_KEYS:
        gid = item.raw.get(id_key)
        if gid:
            return str(gid), str(item.raw.get(title_key) or gid)
    return None


def one_line(text: str, n: int) -> str:
    t = " ".join(text.split())
    return t if len(t) <= n else t[: n - 1] + "…"


def plan_order(items: list[ContentItem]) -> list[ContentItem]:
    """Same order `filters.select` gives the CLI: oldest first, undated last."""
    return sorted(items, key=lambda i: (i.created_at is None, i.created_at.timestamp() if i.created_at else 0, i.item_id))


class BrowseModel:
    def __init__(self, items: list[ContentItem], deletable: frozenset[str] | set[str]):
        self.deletable = frozenset(deletable)
        self.items = sorted(items, key=lambda i: (i.created_at is None, -(i.created_at.timestamp() if i.created_at else 0), i.item_id))
        self.by_id = {i.item_id: i for i in self.items}
        self.selected: set[str] = set()
        self.ctype: str | None = None  # None = all types
        self.query = ""
        self.since: datetime | None = None
        self.until: datetime | None = None
        self.group: str | None = None
        self._visible: list[ContentItem] | None = None

    # ---------------- filtering ----------------

    def _invalidate(self) -> None:
        self._visible = None

    def set_type(self, ctype: str | None) -> None:
        self.ctype, self.group = ctype, None
        self._invalidate()

    def set_query(self, query: str) -> None:
        self.query = query.strip()
        self._invalidate()

    def set_dates(self, since: datetime | None, until: datetime | None) -> None:
        Filter(since=since, until=until)  # validates since <= until (raises ValueError)
        self.since, self.until = since, until
        self._invalidate()

    def set_group(self, key: str | None) -> None:
        self.group = key
        self._invalidate()

    def visible(self) -> list[ContentItem]:
        """Items passing the type tab, conversation, search and date filters, newest first."""
        if self._visible is None:
            flt = Filter(since=self.since, until=self.until, keywords=[self.query] if self.query else [])
            out = []
            for i in self.items:
                if self.ctype and i.content_type != self.ctype:
                    continue
                if self.group is not None and (group_of(i) or ("",))[0] != self.group:
                    continue
                if flt.matches(i):
                    out.append(i)
            self._visible = out
        return self._visible

    def groups(self) -> list[Group]:
        """Conversations present in the current type tab, biggest first (empty if nothing is grouped)."""
        counts: dict[str, list] = {}
        for i in self.items:
            if self.ctype and i.content_type != self.ctype:
                continue
            g = group_of(i)
            if g:
                entry = counts.setdefault(g[0], [g[1], 0])
                entry[1] += 1
        return sorted((Group(k, t, n) for k, (t, n) in counts.items()), key=lambda g: (-g.count, g.title))

    # ---------------- selection ----------------

    def can_select(self, item: ContentItem) -> bool:
        return item.content_type in self.deletable

    def toggle(self, item_id: str) -> bool:
        """Toggle one item; False if its type cannot be deleted on this platform."""
        item = self.by_id[item_id]
        if not self.can_select(item):
            return False
        self.selected ^= {item_id}
        return True

    def _apply(self, items: list[ContentItem], mode: str) -> tuple[set[str], int]:
        """Change selection for `items`; returns (changed ids, number skipped as not deletable)."""
        ok = [i.item_id for i in items if self.can_select(i)]
        skipped = len(items) - len(ok)
        if mode == "select":
            changed = {i for i in ok if i not in self.selected}
            self.selected |= changed
        elif mode == "deselect":
            changed = {i for i in ok if i in self.selected}
            self.selected -= changed
        else:  # invert
            changed = set(ok)
            self.selected ^= changed
        return changed, skipped

    def select_visible(self) -> tuple[set[str], int]:
        return self._apply(self.visible(), "select")

    def deselect_visible(self) -> tuple[set[str], int]:
        return self._apply(self.visible(), "deselect")

    def invert_visible(self) -> tuple[set[str], int]:
        return self._apply(self.visible(), "invert")

    def select_group(self, key: str) -> tuple[set[str], int]:
        """Every message of one conversation (within the type tab), ignoring search and dates."""
        members = [i for i in self.items if (group_of(i) or ("",))[0] == key
                   and (not self.ctype or i.content_type == self.ctype)]
        return self._apply(members, "select")

    def clear(self) -> None:
        self.selected.clear()

    def keep_existing(self, ids: set[str]) -> None:
        self.selected &= ids

    # ---------------- counts ----------------

    @property
    def hidden_selected(self) -> int:
        shown = {i.item_id for i in self.visible()}
        return len(self.selected - shown)

    def selected_items(self) -> list[ContentItem]:
        """Exactly the selected items in plan order (oldest first)."""
        return plan_order([self.by_id[i] for i in self.selected if i in self.by_id])
