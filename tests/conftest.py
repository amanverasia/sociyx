from __future__ import annotations

from types import SimpleNamespace

import pytest

from sociyx import paths
from sociyx.adapters.fixture import FixtureAdapter
from sociyx.executor import Executor
from sociyx.filters import Filter
from sociyx.planning import create_plan
from sociyx.scanning import scan_account
from sociyx.store import Store


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("SOCIYX_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("SOCIYX_FIXTURE_FAULTS", raising=False)
    return tmp_path / "home"


@pytest.fixture
def store(home):
    s = Store(paths.db_path())
    yield s
    s.close()


class Sleeper:
    """Records requested sleeps instead of sleeping."""

    def __init__(self):
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


@pytest.fixture
def sleeper():
    return Sleeper()


def prepare(store, flt=None, faults=None, who="alice", scan=True, **adapter_kw):
    """Login, scan and (if flt given) plan, on the fixture adapter."""
    adapter = FixtureAdapter(faults=faults or {}, **adapter_kw)
    account = adapter.login(who)
    store.upsert_account(account)
    if scan:
        scan_account(store, adapter, account, sleep=lambda s: None)
    plan = create_plan(store, "fixture", account, flt) if flt is not None else None
    return SimpleNamespace(adapter=adapter, account=account, plan=plan, store=store)


def make_executor(prep, sleeper, **kw):
    kw.setdefault("min_interval", 0)
    kw.setdefault("verify_delay", 0)
    return Executor(prep.store, prep.adapter, sleep=sleeper, **kw)


@pytest.fixture
def prep(store):
    return lambda flt=None, **kw: prepare(store, flt, **kw)
