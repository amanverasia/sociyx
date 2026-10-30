import sqlite3

import pytest

from sociyx.adapters.base import ScanPage
from sociyx.adapters.fixture import FixtureAdapter
from sociyx.filters import Filter, select
from sociyx.models import Account, PermanentError
from sociyx.scanning import scan_account
from conftest import make_executor, prepare
from sociyx.planning import PlanRefused


def test_select_never_returns_other_accounts_items(store):
    a = prepare(store)
    b = prepare(store, who="bob")
    mine = select(store, "fixture", a.account.account_id, Filter())
    theirs = select(store, "fixture", b.account.account_id, Filter())
    assert mine and theirs
    assert {i.account_id for i in mine} == {"fixture:alice"}
    assert {i.account_id for i in theirs} == {"fixture:bob"}
    assert not {i.item_id for i in mine} & {i.item_id for i in theirs}


def test_cross_account_plan_item_rejected_by_foreign_keys(store):
    a = prepare(store, Filter(content_types=["like"]))
    b = prepare(store, who="bob")
    bob_item = select(store, "fixture", "fixture:bob", Filter())[0]
    # bob's item claiming bob's account but attached to alice's plan
    with pytest.raises(sqlite3.IntegrityError):
        store.conn.execute(
            "INSERT INTO plan_items (plan_id, platform, account_id, item_id, position, fingerprint) VALUES (?,?,?,?,?,?)",
            (a.plan.plan_id, "fixture", "fixture:bob", bob_item.item_id, 99, "x"),
        )
    # bob's item id under alice's account columns: no such item row
    with pytest.raises(sqlite3.IntegrityError):
        store.conn.execute(
            "INSERT INTO plan_items (plan_id, platform, account_id, item_id, position, fingerprint) VALUES (?,?,?,?,?,?)",
            (a.plan.plan_id, "fixture", "fixture:alice", bob_item.item_id, 99, "x"),
        )


class ForeignItemAdapter(FixtureAdapter):
    """Misbehaving adapter that leaks another account's item into alice's listing."""

    def scan_page(self, account, content_type, cursor):
        page = super().scan_page(account, content_type, cursor)
        other = super().scan_page(Account("fixture", "fixture:bob", "bob.fixture"), content_type, cursor)
        return ScanPage(page.items + other.items[:1], page.next_cursor)


def test_scanner_rejects_foreign_item_and_stores_nothing(store):
    adapter = ForeignItemAdapter()
    adapter.login("bob")
    acct = adapter.login("alice")
    with pytest.raises(PermanentError, match="foreign"):
        scan_account(store, adapter, acct, ["post"], sleep=lambda s: None)
    assert store.conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0
    assert store.latest_scans("fixture", "fixture:alice")[0]["status"] == "failed"


class LyingAdapter(FixtureAdapter):
    def current_account(self, account_id):
        return Account("fixture", "fixture:bob", "bob.fixture")


def test_executor_refuses_when_session_is_a_different_account(store, sleeper):
    p = prepare(store, Filter(content_types=["like"]))
    liar = LyingAdapter()
    ex = make_executor(p, sleeper)
    ex.adapter = liar
    with pytest.raises(PlanRefused, match="not the plan's account"):
        ex.run(p.plan.plan_id, p.plan.confirmation_token)
    assert p.adapter.delete_calls() == []
    assert store.outcome_counts(p.plan.plan_id) == {"pending": p.plan.item_count}
