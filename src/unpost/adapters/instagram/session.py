"""Stored instagrapi settings (a credential: tokens, cookies, device ids). Mode 0600; never logged."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ...paths import secrets_dir, write_private


def settings_path(account_id: str, base: Path | None = None) -> Path:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", account_id)
    return (base or secrets_dir("instagram")) / f"{safe}.json"


def dump_settings(settings: dict[str, Any]) -> str:
    return json.dumps(settings, indent=2, sort_keys=True, default=str)


def save_settings(account_id: str, settings: dict[str, Any], base: Path | None = None) -> str:
    text = dump_settings(settings)
    write_private(settings_path(account_id, base), text)
    return text


def load_settings(account_id: str, base: Path | None = None) -> dict[str, Any] | None:
    path = settings_path(account_id, base)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return None  # corrupt: treated as no session; never echo contents
    return data if isinstance(data, dict) else None


def delete_settings(account_id: str, base: Path | None = None) -> None:
    settings_path(account_id, base).unlink(missing_ok=True)
