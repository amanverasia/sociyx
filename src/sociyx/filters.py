"""Selection filters shared by every platform.

Date semantics (all comparisons in UTC unless the input carries an offset):
  * ``since`` is inclusive.
  * ``until`` is inclusive. A bare date (``2021-03-31``) means the *end* of that
    day, so ``--since 2021-01-01 --until 2021-03-31`` covers all of Q1.
  * Items whose timestamp is unknown never match when a date bound is set.
Keywords are case-insensitive substring matches on the item text.
``keywords`` match if ANY is present; ``exclude_keywords`` drop an item if ANY is present.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta, timezone

from .models import ContentItem, utc
from .store import Store

_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def parse_bound(value: str, *, end: bool) -> datetime:
    """Parse a CLI date/datetime. Date-only values snap to start/end of day (UTC)."""
    value = value.strip()
    if _DATE_ONLY.match(value):
        d = date.fromisoformat(value)
        if end:
            return datetime.combine(d, time.max, tzinfo=timezone.utc)
        return datetime.combine(d, time.min, tzinfo=timezone.utc)
    return utc(datetime.fromisoformat(value.replace("Z", "+00:00")))


@dataclass
class Filter:
    content_types: list[str] = field(default_factory=list)
    since: datetime | None = None
    until: datetime | None = None
    keywords: list[str] = field(default_factory=list)
    exclude_keywords: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.since and self.until and self.since > self.until:
            raise ValueError("--since is after --until")

    def matches(self, item: ContentItem) -> bool:
        if self.content_types and item.content_type not in self.content_types:
            return False
        if self.since or self.until:
            if item.created_at is None:
                return False
            ts = utc(item.created_at)
            if self.since and ts < self.since:
                return False
            if self.until and ts > self.until:
                return False
        text = item.text.casefold()
        if self.keywords and not any(k.casefold() in text for k in self.keywords):
            return False
        if any(k.casefold() in text for k in self.exclude_keywords):
            return False
        return True

    def to_dict(self) -> dict:
        d = asdict(self)
        d["since"] = self.since.isoformat() if self.since else None
        d["until"] = self.until.isoformat() if self.until else None
        return d

    def describe(self) -> str:
        parts = []
        parts.append("types=" + (",".join(self.content_types) if self.content_types else "all"))
        if self.since:
            parts.append(f"since={self.since.isoformat()}")
        if self.until:
            parts.append(f"until={self.until.isoformat()}")
        if self.keywords:
            parts.append("keywords(any)=" + ",".join(repr(k) for k in self.keywords))
        if self.exclude_keywords:
            parts.append("exclude=" + ",".join(repr(k) for k in self.exclude_keywords))
        return " ".join(parts)


def select(store: Store, platform: str, account_id: str, flt: Filter) -> list[ContentItem]:
    """Items for exactly one account that match the filter, oldest first."""
    rows = store.query_items(platform, account_id, flt.content_types or None, flt.since, flt.until)
    items = [Store.row_to_item(r) for r in rows]
    return [it for it in items if it.account_id == account_id and flt.matches(it)]
