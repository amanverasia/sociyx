import pytest

from unpost.adapters.base import ScanPage
from unpost.adapters.fixture import FixtureAdapter
from unpost.models import TransientError
from unpost.scanning import scan_account
from conftest import prepare


def count(store, **where):
    sql = "SELECT COUNT(*) FROM items"
    if where:
        sql += " WHERE " + " AND ".join(f"{k} = ?" for k in where)
    return store.conn.execute(sql, tuple(where.values())).fetchone()[0]


NOSLEEP = lambda s: None  # noqa: E731


def test_overlapping_pages_are_deduplicated(store):
    adapter = FixtureAdapter(faults={"page_overlap": 3})
    acct = adapter.login("alice")
    [s] = scan_account(store, adapter, acct, ["post"], sleep=NOSLEEP)
    assert s.items_seen > 20 and s.new_items == 20
    assert count(store, content_type="post") == 20
    scan_account(store, adapter, acct, ["post"], sleep=NOSLEEP)
    assert count(store, content_type="post") == 20  # rescan doesn't duplicate


class Wrapper:
    """Delegates to an adapter but lets a test intercept scan_page."""

    def __init__(self, inner, hook):
        self._inner, self._hook, self.cursors = inner, hook, []

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def scan_page(self, account, ctype, cursor):
        self.cursors.append(cursor)
        return self._hook(self, account, ctype, cursor)


def test_interrupted_scan_resumes_from_saved_cursor(store):
    inner = FixtureAdapter()
    acct = inner.login("alice")

    def boom(w, account, ctype, cursor):
        if len(w.cursors) == 3:
            raise RuntimeError("network died")
        return inner.scan_page(account, ctype, cursor)

    first = Wrapper(inner, boom)
    with pytest.raises(RuntimeError):
        scan_account(store, first, acct, ["post"], sleep=NOSLEEP)
    assert first.cursors == [None, "7", "14"]
    assert count(store, content_type="post") == 14  # pages 1-2 committed

    second = Wrapper(inner, lambda w, a, t, c: inner.scan_page(a, t, c))
    [s] = scan_account(store, second, acct, ["post"], sleep=NOSLEEP)
    assert second.cursors[0] == "14"  # did not refetch page 1
    assert s.status == "complete" and count(store, content_type="post") == 20


def test_stuck_cursor_is_detected(store):
    adapter = FixtureAdapter(faults={"stuck_cursor": True})
    acct = adapter.login("alice")
    [s] = scan_account(store, adapter, acct, ["post"], sleep=NOSLEEP)
    assert s.status == "failed" and "did not advance" in s.error
    assert s.pages <= 3
    assert store.latest_scans("fixture", "fixture:alice")[0]["status"] == "failed"


def test_deleted_items_marked_missing_only_after_complete_scan(store):
    p = prepare(store)
    gone1, gone2 = "post/alice/0010", "post/alice/0011"
    p.adapter.remove_item("fixture:alice", gone1)
    scan_account(store, p.adapter, p.account, ["post"], max_pages=1, fresh=True, sleep=NOSLEEP)
    state = lambda i: store.get_item_row("fixture", "fixture:alice", i)["state"]  # noqa: E731
    assert state(gone1) == "present"  # partial scan must not mark anything missing
    [s] = scan_account(store, p.adapter, p.account, ["post"], sleep=NOSLEEP)  # resumes the partial one
    assert s.status == "complete" and state(gone1) == "missing"
    p.adapter.remove_item("fixture:alice", gone2)
    scan_account(store, p.adapter, p.account, ["post"], sleep=NOSLEEP)
    assert state(gone2) == "missing"


def test_partial_scan_reports_partial_and_stays_resumable(store):
    adapter = FixtureAdapter()
    acct = adapter.login("alice")
    [s] = scan_account(store, adapter, acct, ["post"], max_pages=1, sleep=NOSLEEP)
    assert s.partial and s.status == "running" and s.pages == 1


def test_zero_item_complete_scan_warns_and_does_not_mark_missing(store):
    p = prepare(store)
    empty = Wrapper(p.adapter, lambda w, a, t, c: ScanPage([], None))
    [s] = scan_account(store, empty, p.account, ["post"], fresh=True, sleep=NOSLEEP)
    assert s.status == "complete" and "zero items" in s.warning
    assert count(store, content_type="post", state="present") == 20


def test_transient_errors_retry_with_backoff_then_fail(store):
    p = prepare(store, scan=False)
    sleeps = []

    def always(w, *a):
        raise TransientError("flaky")

    w = Wrapper(p.adapter, always)
    summaries = scan_account(store, w, p.account, ["post", "like"], max_retries=3, sleep=sleeps.append)
    assert [s.status for s in summaries] == ["failed", "failed"]  # moved on to the next type
    assert len(w.cursors) == 8  # (1 + 3 retries) per type
    assert sleeps[:3] == [1.0, 2.0, 4.0]


def test_unknown_content_type_rejected(store):
    adapter = FixtureAdapter()
    acct = adapter.login("alice")
    with pytest.raises(ValueError):
        scan_account(store, adapter, acct, ["nope"], sleep=NOSLEEP)


# ---------------- scan delay, soft blocks, rate-limit escalation ----------------

import random

from unpost.models import ActionBlocked, RateLimited


class DelayedFixture(FixtureAdapter):
    platform = "fixture"
    scan_delay = (2.0, 4.0)


def test_scan_delay_applied_between_pages_only(store):
    adapter = DelayedFixture(page_size=5)
    acct = adapter.login("alice")
    calls = []
    [s] = scan_account(store, adapter, acct, ["post"], sleep=calls.append, rng=random.Random(3))
    assert s.pages == 4  # 20 posts / 5
    assert len(calls) == 3  # none before the first page
    assert all(2.0 <= c <= 4.0 for c in calls) and len(set(calls)) > 1
    ref = random.Random(3)
    assert calls == [ref.uniform(2.0, 4.0) for _ in range(3)]


def test_no_scan_delay_by_default(store):
    adapter = FixtureAdapter(page_size=5)
    acct = adapter.login("alice")
    calls = []
    scan_account(store, adapter, acct, ["post"], sleep=calls.append)
    assert calls == []


def test_action_blocked_in_scan_stops_remaining_types(store):
    adapter = FixtureAdapter(faults={"scan_blocked": True})
    acct = adapter.login("alice")
    with pytest.raises(ActionBlocked):
        scan_account(store, adapter, acct, ["post", "like"], sleep=NOSLEEP)
    rows = store.conn.execute("SELECT content_type, status, error FROM scans").fetchall()
    assert [(r["content_type"], r["status"]) for r in rows] == [("post", "failed")]
    assert "restricted" in rows[0]["error"]


def test_scan_rate_limit_waits_escalate(store):
    inner = FixtureAdapter()
    acct = inner.login("alice")
    state = {"n": 0}

    def hook(w, account, ctype, cursor):
        if state["n"] < 3:
            state["n"] += 1
            raise RateLimited("slow", retry_after=5)
        return w._inner.scan_page(account, ctype, cursor)

    w = Wrapper(inner, hook)
    w.platform, w.content_types = inner.platform, inner.content_types
    calls = []
    [s] = scan_account(store, w, acct, ["like"], sleep=calls.append)
    assert s.status == "complete" and calls == [5, 10, 20]
