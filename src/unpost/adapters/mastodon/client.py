"""Mastodon REST helpers: request + uniform error mapping, Link parsing, HTML -> text."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx

from ...models import AuthRequired, PermanentError, RateLimited, TransientError, utc
from ...redact import scrub

USER_AGENT = "unpost/0.1 (+local CLI)"
TIMEOUT = 30.0


class ApiError(PermanentError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(scrub(f"HTTP {status}: {message}"))
        self.status = status


def _error_message(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return ""
    if isinstance(body, dict):
        return str(body.get("error_description") or body.get("error") or "")
    return ""


def retry_after(resp: httpx.Response) -> float | None:
    reset = resp.headers.get("X-RateLimit-Reset")  # ISO 8601 timestamp
    if reset:
        try:
            return max(0.0, (utc(datetime.fromisoformat(reset.replace("Z", "+00:00"))) - datetime.now(timezone.utc)).total_seconds())
        except ValueError:
            pass
    value = resp.headers.get("Retry-After")
    if value:
        try:
            return max(0.0, float(value))
        except ValueError:
            try:
                return max(0.0, parsedate_to_datetime(value).timestamp() - datetime.now(timezone.utc).timestamp())
            except (TypeError, ValueError):
                pass
    return None


def request(
    http: httpx.Client,
    method: str,
    base: str,
    path: str,
    token: str | None = None,
    params: dict[str, Any] | None = None,
    data: dict[str, str] | None = None,
) -> httpx.Response:
    """One request. 404 is returned to the caller (it is meaningful); every other
    failure is mapped to the shared error types."""
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    clean = {k: v for k, v in (params or {}).items() if v is not None}
    try:
        resp = http.request(method, base + path, params=clean or None, data=data, headers=headers, timeout=TIMEOUT)
    except httpx.TransportError as e:
        raise TransientError(f"network error calling {path}: {type(e).__name__}") from None
    status = resp.status_code
    if status < 400 or status == 404:
        return resp
    if status == 429:
        raise RateLimited("rate limited by the Mastodon server", retry_after=retry_after(resp))
    if status >= 500:
        raise TransientError(f"Mastodon server error (HTTP {status})")
    if status == 401:
        raise AuthRequired("authentication failed (HTTP 401); run `unpost login mastodon`")
    raise ApiError(status, _error_message(resp))


def json_body(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        raise PermanentError("server returned a non-JSON response") from None


_NEXT = re.compile(r'<([^>]+)>\s*;\s*rel="?next"?')


def next_max_id(resp: httpx.Response) -> str | None:
    """max_id of the Link rel="next" URL, or None if there is no next page."""
    for header in resp.headers.get_list("link"):
        m = _NEXT.search(header)
        if m:
            ids = parse_qs(urlsplit(m.group(1)).query).get("max_id")
            return ids[0] if ids else None
    return None


def has_link_header(resp: httpx.Response) -> bool:
    return "link" in resp.headers


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag == "br":
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "p":
            self.parts.append("\n\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def html_to_text(html: str) -> str:
    p = _Text()
    p.feed(html or "")
    p.close()
    text = "".join(p.parts)
    return re.sub(r"\n{3,}", "\n\n", unescape(text)).strip()
