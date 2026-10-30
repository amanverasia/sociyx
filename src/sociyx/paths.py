"""Local data locations. Everything lives under one private directory (mode 0700).

Default: $XDG_DATA_HOME/sociyx (usually ~/.local/share/sociyx).
Override with SOCIYX_HOME (used by tests and for portable setups).

Legacy compatibility (nothing is ever moved or copied automatically, since the folder holds
credentials): UNPOST_HOME is honored when SOCIYX_HOME is unset, and an existing legacy
$XDG_DATA_HOME/unpost directory keeps being used while the new default does not exist.
"""

from __future__ import annotations

import os
from pathlib import Path


def _data_root() -> Path:
    xdg = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(xdg)


def home() -> Path:
    override = os.environ.get("SOCIYX_HOME") or os.environ.get("UNPOST_HOME")
    if override:
        base = Path(override)
    else:
        root = _data_root()
        base = root / "sociyx"
        legacy = root / "unpost"
        if not base.exists() and legacy.is_dir():
            base = legacy
    return private_dir(base)


def private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def db_path() -> Path:
    h = home()
    db = h / "sociyx.sqlite3"
    legacy = h / "unpost.sqlite3"  # database name used before the rename
    return legacy if legacy.exists() and not db.exists() else db


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
