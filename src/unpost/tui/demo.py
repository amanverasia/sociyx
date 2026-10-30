"""Demo mode: a throw-away UNPOST_HOME pre-seeded with fake accounts, so the whole flow is safe to try."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from .. import paths
from ..adapters.fixture import DemoFixtureAdapter
from ..scanning import scan_account
from ..store import Store


def prepare_demo_home() -> Path:
    """Create a scratch data directory, point UNPOST_HOME at it and seed it. Never touches real data."""
    home = Path(tempfile.mkdtemp(prefix="unpost-demo-"))
    os.environ["UNPOST_HOME"] = str(home)
    os.environ["UNPOST_TUI_DEMO"] = "1"
    os.environ.pop("UNPOST_FIXTURE_FAULTS", None)
    seed_demo(Store(paths.db_path()))
    return home


def seed_demo(store: Store) -> None:
    """Log in both fake accounts; scan alice so there is something to browse right away (bob is empty)."""
    adapter = DemoFixtureAdapter()
    try:
        alice = adapter.login("alice")
        store.upsert_account(alice)
        store.upsert_account(adapter.login("bob"))
        scan_account(store, adapter, alice, sleep=lambda s: None)
    finally:
        adapter.close()
        store.close()
