"""Small shared HTTP constants/helpers for the Bluesky adapter."""

from __future__ import annotations

from urllib.parse import urlsplit

USER_AGENT = "sociyx/0.1 (+local CLI)"
TIMEOUT = 30.0
PUBLIC_APPVIEW = "https://public.api.bsky.app"


def origin(url: str) -> str:
    """scheme://host[:port]; DPoP nonces are tracked per server origin."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"
