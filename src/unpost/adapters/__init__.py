"""Adapter registry. Adapters are imported lazily so optional heavy deps only load when used."""

from __future__ import annotations

import importlib
from typing import Any

from .base import Adapter, ScanPage

ADAPTERS: dict[str, str] = {
    "fixture": "unpost.adapters.fixture:FixtureAdapter",
    "bluesky": "unpost.adapters.bluesky:BlueskyAdapter",
    "mastodon": "unpost.adapters.mastodon:MastodonAdapter",
    "instagram": "unpost.adapters.instagram:InstagramAdapter",
}


def adapter_class(name: str) -> type[Adapter]:
    try:
        target = ADAPTERS[name]
    except KeyError:
        raise LookupError(f"unknown platform {name!r}; known: {', '.join(sorted(ADAPTERS))}") from None
    module, _, attr = target.partition(":")
    return getattr(importlib.import_module(module), attr)


def get_adapter(name: str, **options: Any) -> Adapter:
    return adapter_class(name)(**options)


__all__ = ["ADAPTERS", "Adapter", "ScanPage", "adapter_class", "get_adapter"]
