"""Shared-module additions behind the TUI: create_plan_from_items, scan should_stop, Executor on_wait."""

import json
from datetime import datetime, timezone

import pytest

from unpost.adapters.fixture import DemoFixtureAdapter, FixtureAdapter
from unpost.filters import Filter, select
from unpost.models import ContentItem
from unpost.planning import create_plan, create_plan_from_items, describe_pacing, preview
from unpost.scanning import scan_account
from conftest import make_executor, prepare

NOSLEEP = lambda s: None  # noqa: E731


def items_of(store, ids):
    return [store.row_to_item(store.get_item_row("fixture", "fixture:alice", i)) for i in ids]


# ---------------- create_plan_from_items ----------------

def test_plan_contains_exactly_the_given_items(prep, store):
    p = prep()
    ids = ["post/alice/0003", "like/alice/0002", "repost/alice/0001"]
    plan = create_plan_from_items(store, "fixture", p.account, items_of(store, ids), {"search": "x"})
    assert [r["item_id"] for r in store.plan_items(plan.plan_id)] == ids  # caller's order is kept
    assert plan.filters["selection"] == "manual" and plan.filters["count"] == 3 and plan.filters["search"] == "x"
    data = json.loads(open(plan.export_path).read())
    assert sorted(i["item_id"] for i in data["items"]) == sorted(ids)
    assert plan.export_sha256 and "manual selection of 3 items" in preview(store, plan)


def test_create_plan_still_works_and_has_no_manual_marker(prep, store):
    p = prep(Filter(content_types=["like"]))
    assert "selection" not in p.plan.filters and p.plan.item_count == 10


def test_plan_from_items_rejects_empty_foreign_and_unknown(prep, store):
    p = prep()
    with pytest.raises(ValueError, match="nothing selected"):
        create_plan_from_items(store, "fixture", p.account, [], {})
    bob = FixtureAdapter().login("bob")
    store.upsert_account(bob)
    scan_account(store, FixtureAdapter(), bob, sleep=NOSLEEP)
    foreign = store.row_to_item(store.get_item_row("fixture", bob.account_id, "post/bob/0001"))
    with pytest.raises(ValueError, match="another account"):
        create_plan_from_items(store, "fixture", p.account, [foreign], {})
    ghost = ContentItem("fixture", p.account.account_id, "post/alice/9999", "post", None, "x")
    with pytest.raises(ValueError, match="not in the local database"):
        create_plan_from_items(store, "fixture", p.account, [ghost], {})
    assert store.list_plans() == []


def test_plan_from_items_non_deletable_types_excluded_like_create_plan(prep, store):
    p = prep()

    class NoLikes(FixtureAdapter):
        deletable_types = frozenset({"post"})

    ids = ["post/alice/0001", "like/alice/0001"]
    plan = create_plan_from_items(store, NoLikes, p.account, items_of(store, ids), {})
    assert [r["item_id"] for r in store.plan_items(plan.plan_id)] == ["post/alice/0001"]
    assert plan.filters["excluded_non_deletable"] == 1
    with pytest.raises(ValueError, match="can be deleted"):
        create_plan_from_items(store, NoLikes, p.account, items_of(store, ["like/alice/0001"]), {})


def test_plan_from_items_dedupes(prep, store):
    p = prep()
    one = items_of(store, ["post/alice/0001"])
    plan = create_plan_from_items(store, "fixture", p.account, one + one, {})
    assert plan.item_count == 1


def test_manual_plan_runs_only_selected(prep, store, sleeper):
    p = prep()
    ids = ["post/alice/0001", "post/alice/0002"]
    p.plan = create_plan_from_items(store, "fixture", p.account, items_of(store, ids), {})
    res = make_executor(p, sleeper).run(p.plan.plan_id, p.plan.confirmation_token)
    assert res.status == "completed" and sorted(p.adapter.delete_calls()) == ids


def test_describe_pacing_plain_words():
    from unpost.adapters.instagram import InstagramAdapter

    text = describe_pacing(InstagramAdapter, 50)
    assert "30-45 s" in text and "200 per day" in text and "pause and resume" in text
    assert "days" in describe_pacing(InstagramAdapter, 450)
    assert "quick" in describe_pacing(FixtureAdapter, 5)


# ---------------- scan should_stop ----------------

def test_scan_stops_between_pages_and_stays_resumable(prep, store):
    p = prep(scan=False, page_size=3)
    scan_account(store, p.adapter, p.account, ["post"], sleep=NOSLEEP)  # 20 posts, complete
    calls = []
    [s] = scan_account(store, p.adapter, p.account, ["post"], fresh=True, sleep=NOSLEEP,
                       progress=lambda t, pages, n: calls.append(pages), should_stop=lambda: len(calls) >= 1)
    assert s.stopped and s.partial and s.status == "running" and s.pages == 1
    scan = store.latest_scans("fixture", p.account.account_id)[0]
    assert scan["status"] == "running" and scan["pages"] == 1 and scan["cursor"] == "3"
    assert not [r for r in store.item_counts("fixture", p.account.account_id) if r["state"] == "missing"]
    [done] = scan_account(store, p.adapter, p.account, ["post"], sleep=NOSLEEP)  # resumes from the cursor
    assert done.status == "complete" and done.pages == 6  # pages 2..7 of 7


def test_scan_stop_skips_remaining_types_and_checks_before_first_page(prep, store):
    p = prep(scan=False, page_size=3)
    assert scan_account(store, p.adapter, p.account, sleep=NOSLEEP, should_stop=lambda: True) == []
    out = scan_account(store, p.adapter, p.account, sleep=NOSLEEP, max_pages=None,
                       should_stop=lambda: bool(store.item_counts("fixture", p.account.account_id)))
    assert len(out) == 1 and out[0].stopped  # 'like' and 'repost' were never started


def test_scan_stop_during_a_long_wait_is_prompt(prep, store):
    p = prep(scan=False, page_size=3)
    p.adapter.scan_delay = (30.0, 30.0)
    slept = []
    pages = []
    [s] = scan_account(store, p.adapter, p.account, ["post"], sleep=slept.append,
                       progress=lambda t, n, m: pages.append(n), should_stop=lambda: bool(pages) and len(slept) >= 2)
    assert s.stopped and sum(slept) < 5  # gave up after ~2 one-second slices instead of waiting 30 s


# ---------------- Executor on_wait / on_state ----------------

def test_on_wait_reports_pacing_with_duration(prep, sleeper):
    p = prep(Filter(content_types=["post"]))
    waits, states = [], []
    ex = make_executor(p, sleeper, min_interval=2.0, monotonic=lambda: 0.0,
                       on_wait=lambda s, r: waits.append((s, r)), on_state=states.append)
    ex.run(p.plan.plan_id, p.plan.confirmation_token)
    assert waits == [(2.0, "pacing")] * 19  # none before the first of 20 items
    assert states[0] == "checking the logged-in account" and "deleting post/alice/0000" in states


def test_on_wait_think_pause_retry_and_rate_limit(prep, sleeper):
    p = prep(Filter(content_types=["post"]), faults={"transient": {"post/alice/0000": 1}, "rate_limit": {"post/alice/0001": 1}})
    p.adapter.think_pause_every = 2
    p.adapter.think_pause_seconds = (7.0, 7.0)
    waits = []
    ex = make_executor(p, sleeper, on_wait=lambda s, r: waits.append((s, r)))
    ex.run(p.plan.plan_id, p.plan.confirmation_token)
    assert (2.0, "retrying") in waits and (5.0, "rate limit") in waits and (7.0, "think pause") in waits
    assert all(s > 0 for s, _ in waits)  # zero-length waits are not reported


def test_on_wait_not_called_without_waits(prep, sleeper):
    p = prep(Filter(content_types=["like"]))
    waits = []
    make_executor(p, sleeper, on_wait=lambda s, r: waits.append(s)).run(p.plan.plan_id, p.plan.confirmation_token)
    assert waits == []


def test_demo_fixture_has_dms_with_thread_metadata(store):
    a = DemoFixtureAdapter()
    acct = a.login("alice")
    store.upsert_account(acct)
    scan_account(store, a, acct, sleep=NOSLEEP)
    dms = select(store, "fixture", acct.account_id, Filter(content_types=["dm"]))
    assert len(dms) == 13 and {d.raw["thread_id"] for d in dms} == {"thread0", "thread1", "thread2"}
