"""instagrapi objects / raw API dicts -> ContentItem. Raw fields are minimal and non-sensitive."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

from ...models import Account, ContentItem, utc

PLATFORM = "instagram"
TEXT_LIMIT = 500
DM_PLACEHOLDER = "[media/link/other]"


def full_media_id(m: Any) -> str:
    mid = str(getattr(m, "id", "") or "")
    if "_" in mid:
        return mid
    return f"{m.pk}_{m.user.pk}"


def media_version(m: Any) -> str:
    caption = getattr(m, "caption_text", "") or ""
    taken = utc(m.taken_at)
    return f"{int(taken.timestamp())}:{hashlib.sha1(caption.encode()).hexdigest()[:12]}"


def media_url(m: Any, reel: bool = False) -> str:
    code = getattr(m, "code", "") or ""
    reel = reel or getattr(m, "product_type", "") == "clips"
    return f"https://www.instagram.com/{'reel' if reel else 'p'}/{code}/" if code else ""


def media_item(m: Any, account: Account, ctype: str) -> ContentItem:
    mid = full_media_id(m)
    pk = str(m.pk)
    caption = getattr(m, "caption_text", "") or ""
    raw = {
        "media_pk": pk, "media_id": mid, "code": getattr(m, "code", "") or "",
        "owner_pk": str(m.user.pk), "owner_username": getattr(m.user, "username", "") or "",
    }
    if ctype in ("like", "saved"):
        # pk-only ids so API scans and data-export imports of the same item collapse into one.
        return ContentItem(
            PLATFORM, account.account_id, f"{ctype}:{pk}", ctype, utc(m.taken_at), caption[:TEXT_LIMIT],
            media_url(m), "", raw,
        )
    return ContentItem(
        PLATFORM, account.account_id, mid, ctype, utc(m.taken_at), caption, media_url(m, ctype == "reel"),
        media_version(m), raw,
    )


def story_item(s: Any, account: Account, archived: bool) -> ContentItem:
    sid = str(getattr(s, "id", "") or "")
    if "_" not in sid:
        sid = f"{s.pk}_{s.user.pk}"
    username = getattr(s.user, "username", "") or ""
    return ContentItem(
        PLATFORM, account.account_id, sid, "story", utc(s.taken_at), "",
        f"https://www.instagram.com/stories/{username}/{s.pk}/" if username else "", "",
        {"media_pk": str(s.pk), "media_id": sid, "owner_pk": str(s.user.pk), "archived": archived},
    )


def comment_item(cm: Any, media_id: str, code: str, account: Account) -> ContentItem:
    return ContentItem(
        PLATFORM, account.account_id, f"comment:{media_id}:{cm.pk}", "comment", utc(cm.created_at_utc),
        cm.text or "", f"https://www.instagram.com/p/{code}/" if code else "", "",
        {"media_id": media_id, "comment_pk": str(cm.pk), "code": code},
    )


def dm_timestamp(us: Any) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(us) / 1_000_000, tz=timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def dm_text(raw: dict[str, Any]) -> str:
    text = raw.get("text")
    if not text and isinstance(raw.get("link"), dict):
        text = raw["link"].get("text")
    return text or DM_PLACEHOLDER


def dm_item(raw: dict[str, Any], thread_id: str, account: Account, title: str = "") -> ContentItem:
    mid = str(raw["item_id"])
    return ContentItem(
        PLATFORM, account.account_id, f"dm:{thread_id}:{mid}", "dm", dm_timestamp(raw.get("timestamp")),
        dm_text(raw), f"https://www.instagram.com/direct/t/{thread_id}/", "",
        {"thread_id": thread_id, "message_id": mid, "ts_us": str(raw.get("timestamp", "")),
         "item_type": raw.get("item_type", ""), "thread_title": title, "sent_by_viewer": True},
    )
