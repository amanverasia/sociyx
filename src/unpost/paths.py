"""Local data locations. Everything lives under one private directory (mode 0700).

Default: $XDG_DATA_HOME/unpost (usually ~/.local/share/unpost).
Override with UNPOST_HOME (used by tests and for portable setups).
"""

from __future__ import annotations

import os
from pathlib import Path


def home() -> Path:
    override = os.environ.get("UNPOST_HOME")
    if override:
        base = Path(override)
    else:
        xdg = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
        base = Path(xdg) / "unpost"
    return private_dir(base)


def private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def db_path() -> Path:
    return home() / "unpost.sqlite3"


def exports_dir() -> Path:
    return private_dir(home() / "exports")


def secrets_dir(platform: str) -> Path:
    """Session tokens, keys and browser profiles. Treat as credentials."""
    return private_dir(home() / "secrets" / platform)


def write_private(path: Path, data: str) -> None:
    """Write a file readable only by the current user, atomically."""
    private_dir(path.parent)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(data)
    os.replace(tmp, path)
