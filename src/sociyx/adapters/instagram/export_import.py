"""Parse Instagram's official "Download your information" JSON export (offline, no network).

Gives access to what the API cannot list: older likes and comments you wrote on OTHER people's
posts. Parsing is deliberately tolerant: unknown layouts are logged and skipped, never fatal.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ...models import Account, ContentItem

log = logging.getLogger(__name__)

_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
_URL = re.compile(r"instagram\.com/(?:[A-Za-z0-9._]+/)?(?:p|reel|reels|tv)/([A-Za-z0-9_-]{11,})")


def fix_text(s: Any) -> str:
    """Instagram exports UTF-8 bytes as latin-1 escapes ("mojibake"); undo that when possible."""
    if not isinstance(s, str):
        return ""
    try:
        return s.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return s


def pk_from_code(code: str) -> str:
    n = 0
    for ch in code[:11]:
        n = n * 64 + _ALPHABET.index(ch)
    return str(n)


def pk_from_url(url: str) -> str | None:
    m = _URL.search(url or "")
    if not m:
        return None
    try:
        return pk_from_code(m.group(1))
    except ValueError:
        return None


def _ts(value: Any) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _entries(data: Any, *keys: str) -> list[Any]:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in keys:
            if isinstance(data.get(k), list):
                return data[k]
        for v in data.values():
            if isinstance(v, list):
                return v
    return []


def _walk(node: Any) -> Iterator[tuple[str, Any]]:
    if isinstance(node, dict):
        for k, v in node.items():
            yield k, v
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def _first_url_and_ts(entry: Any) -> tuple[str | None, str, datetime | None]:
    url = None
    ts = None
    for k, v in _walk(entry):
        if url is None and k in ("href", "value") and isinstance(v, str) and pk_from_url(v):
            url = v
        if ts is None and k == "timestamp" and v:
            ts = _ts(v)
    owner = fix_text(entry.get("title", "")) if isinstance(entry, dict) else ""
    return url, owner, ts


def _load(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        log.warning("could not read %s (%s); skipping", path.name, type(e).__name__)
        return None


def _media_items(path: Path, ctype: str, keys: tuple[str, ...], account: Account) -> Iterator[ContentItem]:
    data = _load(path)
    if data is None:
        return
    entries = _entries(data, *keys)
    if not entries:
        log.warning("unrecognized layout in %s; skipped", path.name)
    for e in entries:
        url, owner, ts = _first_url_and_ts(e)
        pk = pk_from_url(url) if url else None
        if not pk:
            continue  # no resolvable post (e.g. share links need network); never crash
        yield ContentItem(
            platform="instagram", account_id=account.account_id, item_id=f"{ctype}:{pk}",
            content_type=ctype, created_at=ts, text="", url=url or "",
            raw={"media_pk": pk, "owner_username": owner, "source": "export"},
        )


def _fields(entry: dict[str, Any]) -> dict[str, dict[str, Any]]:
    smd = entry.get("string_map_data")
    if isinstance(smd, dict):
        return {k: v for k, v in smd.items() if isinstance(v, dict)}
    out = {}
    for lv in entry.get("label_values") or []:  # newer layout, minimal support
        if isinstance(lv, dict) and lv.get("label"):
            out[str(lv["label"])] = lv
    return out


def _comment_items(path: Path, account: Account) -> Iterator[ContentItem]:
    data = _load(path)
    if data is None:
        return
    entries = _entries(data, "comments_reels_comments", "comments_media_comments")
    if not entries:
        log.warning("unrecognized layout in %s; skipped", path.name)
    for e in entries:
        if not isinstance(e, dict):
            continue
        f = _fields(e)
        text = fix_text((f.get("Comment") or {}).get("value", ""))
        owner = fix_text((f.get("Media Owner") or {}).get("value", ""))
        tsraw = (f.get("Time") or {}).get("timestamp") or e.get("timestamp")
        ts = _ts(tsraw)
        if not text or ts is None:
            continue
        digest = hashlib.sha1(f"{owner}|{int(ts.timestamp())}|{text}".encode()).hexdigest()[:16]
        yield ContentItem(
            platform="instagram", account_id=account.account_id, item_id=f"cexp:{digest}",
            content_type="comment_export", created_at=ts, text=text,
            url=f"https://www.instagram.com/{owner}/" if owner else "",
            raw={"owner": owner, "timestamp": int(ts.timestamp()), "source": "export"},
        )


def iter_export_items(export_dir: Path, account: Account) -> Iterator[ContentItem]:
    """Yield likes, saved posts and comment_export items found anywhere under `export_dir`."""
    root = Path(export_dir)
    for p in sorted(root.rglob("liked_posts.json")):
        yield from _media_items(p, "like", ("likes_media_likes",), account)
    for p in sorted(root.rglob("saved_posts.json")):
        yield from _media_items(p, "saved", ("saved_saved_media",), account)
    for p in sorted(root.rglob("post_comments*.json")):
        yield from _comment_items(p, account)
    for p in sorted(root.rglob("reels_comments*.json")):
        yield from _comment_items(p, account)
