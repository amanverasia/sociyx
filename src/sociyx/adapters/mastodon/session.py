"""Per-account session files and per-instance app registrations (all mode 0600).

Contents are credentials: never log them. Reprs hide secret fields.
"""

from __future__ import annotations

import dataclasses
import json
import re
from dataclasses import dataclass
from pathlib import Path

from ...paths import secrets_dir, write_private


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", name)


@dataclass(repr=False)
class Session:
    instance: str  # host[:port]
    account_id: str  # "<instance>:<status-api account id>"
    username: str
    client_id: str
    client_secret: str
    access_token: str

    def __repr__(self) -> str:
        return f"Session(account_id={self.account_id!r}, secrets=<hidden>)"


@dataclass(repr=False)
class AppRegistration:
    client_id: str
    client_secret: str

    def __repr__(self) -> str:
        return f"AppRegistration(client_id={self.client_id!r}, secret=<hidden>)"


def session_path(account_id: str) -> Path:
    return secrets_dir("mastodon") / "sessions" / (_safe(account_id) + ".json")


def save_session(s: Session) -> None:
    write_private(session_path(s.account_id), json.dumps(dataclasses.asdict(s), indent=2))


def load_session(account_id: str) -> Session | None:
    path = session_path(account_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        names = {f.name for f in dataclasses.fields(Session)}
        return Session(**{k: v for k, v in data.items() if k in names})
    except (ValueError, TypeError):
        return None  # corrupt: treated as no session; never echo contents


def delete_session(account_id: str) -> None:
    session_path(account_id).unlink(missing_ok=True)


def _app_path(instance: str) -> Path:
    return secrets_dir("mastodon") / "apps" / (_safe(instance) + ".json")


def load_app(instance: str, redirect_uri: str) -> AppRegistration | None:
    path = _app_path(instance)
    if not path.exists():
        return None
    try:
        entry = json.loads(path.read_text(encoding="utf-8")).get(redirect_uri)
        return AppRegistration(entry["client_id"], entry["client_secret"]) if entry else None
    except (ValueError, KeyError, TypeError, AttributeError):
        return None


def save_app(instance: str, redirect_uris: list[str], app: AppRegistration) -> None:
    path = _app_path(instance)
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if not isinstance(data, dict):
            data = {}
    except ValueError:
        data = {}
    for uri in redirect_uris:
        data[uri] = {"client_id": app.client_id, "client_secret": app.client_secret}
    write_private(path, json.dumps(data, indent=2))
