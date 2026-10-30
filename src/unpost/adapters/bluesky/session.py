"""Per-account session files (mode 0600) under secrets_dir("bluesky").

Contents are credentials: never log them. Reprs hide token fields.
"""

from __future__ import annotations

import dataclasses
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

from ...paths import secrets_dir, write_private


@dataclass(repr=False)
class OAuthSession:
    kind: ClassVar[str] = "oauth"
    did: str
    handle: str
    pds_url: str
    issuer: str
    token_endpoint: str
    client_id: str
    redirect_uri: str
    access_token: str
    refresh_token: str
    expires_at: float  # epoch seconds
    dpop_private_key_pem: str
    nonces: dict[str, str] = field(default_factory=dict)  # server origin -> DPoP nonce
    revocation_endpoint: str = ""  # extra field: needed for best-effort logout

    def __repr__(self) -> str:
        return f"OAuthSession(did={self.did!r}, handle={self.handle!r}, secrets=<hidden>)"


@dataclass(repr=False)
class PasswordSession:
    kind: ClassVar[str] = "app_password"
    did: str
    handle: str
    pds_url: str
    access_jwt: str
    refresh_jwt: str

    def __repr__(self) -> str:
        return f"PasswordSession(did={self.did!r}, handle={self.handle!r}, secrets=<hidden>)"


Session = OAuthSession | PasswordSession


def session_path(did: str) -> Path:
    # Keep the name filesystem-safe even for odd did:web identifiers.
    return secrets_dir("bluesky") / (re.sub(r"[^A-Za-z0-9._-]", "_", did) + ".json")


def browser_profile_dir() -> Path:
    return secrets_dir("bluesky") / "browser-profile"


def save_session(session: Session) -> None:
    data = dataclasses.asdict(session)
    data["kind"] = session.kind
    write_private(session_path(session.did), json.dumps(data, indent=2))


def load_session(did: str) -> Session | None:
    path = session_path(did)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        kind = data.pop("kind")
        cls = {"oauth": OAuthSession, "app_password": PasswordSession}[kind]
        names = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in names})
    except (ValueError, KeyError, TypeError):
        return None  # corrupt file: treated as "no session"; never echo contents


def delete_session(did: str) -> None:
    session_path(did).unlink(missing_ok=True)
