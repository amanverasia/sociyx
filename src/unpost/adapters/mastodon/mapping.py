"""Status JSON -> ContentItem."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ...models import Account, ContentItem, PermanentError, utc
from .client import html_to_text


def parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


def status_text(status: dict[str, Any]) -> str:
    body = html_to_text(str(status.get("content") or ""))
    spoiler = str(status.get("spoiler_text") or "").strip()
    return f"{spoiler}\n\n{body}".strip() if spoiler else body


def local_id(account_id: str) -> str:
    """'host:123' -> '123' (host may itself contain a port)."""
    return account_id.rsplit(":", 1)[-1]


def to_item(status: dict[str, Any], account: Account, content_type: str) -> ContentItem:
    try:
        sid = str(status["id"])
        if content_type in ("post", "boost"):
            if str((status.get("account") or {}).get("id")) != local_id(account.account_id):
                raise PermanentError("server returned a status from a different account; refusing")
        if content_type == "boost":
            original = status["reblog"]
            return ContentItem(
                platform=account.platform,
                account_id=account.account_id,
                item_id=sid,
                content_type="boost",
                created_at=parse_time(status.get("created_at")),
                text=status_text(original),
                url=str(original.get("url") or original.get("uri") or ""),
                version="",
                raw={"status_id": sid, "original_id": str(original["id"])},
            )
        item_id = {"post": sid, "favourite": f"fav:{sid}", "bookmark": f"bm:{sid}"}[content_type]
        return ContentItem(
            platform=account.platform,
            account_id=account.account_id,
            item_id=item_id,
            content_type=content_type,
            created_at=parse_time(status.get("created_at")),
            text=status_text(status),
            url=str(status.get("url") or status.get("uri") or ""),
            version=str(status.get("edited_at") or "") if content_type == "post" else "",
            raw={"status_id": sid},
        )
    except (KeyError, TypeError):
        raise PermanentError("unexpected status shape from server") from None
