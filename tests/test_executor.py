import fcntl
import os
import signal
from datetime import datetime, timezone

import pytest

from sociyx import paths
from sociyx.executor import Executor, PlanRefused, cancel_job, pause_job
from sociyx.filters import Filter
from sociyx.planning import create_plan
from sociyx.scanning import scan_account
from conftest import make_executor, prepare

POSTS = Filter(content_types=["post"])  # 20 alice posts
IDS = [f"post/alice/{i:04d}" for i in range(20)]


def outcomes(store, plan):
    return {r["item_id"]: r for r in store.plan_items(plan.plan_id)}


def run(p, sleeper, **kw):
    return make_executor(p, sleeper, **kw).run(p.plan.plan_id, p.plan.confirmation_token)


def test_wrong_token_refused_and_nothing_deleted(prep, sleeper):
    p = prep(POSTS)
    ex = make_executor(p, sleeper)
    for bad in ("", "nope", p.plan.confirmation_token + " "):
        with pytest.raises(PlanRefused):
            ex.run(p.plan.plan_id, bad)
    assert p.adapter.delete_calls() == []
    assert p.store.list_jobs() == []


def test_happy_path_deletes_exactly_the_plan_once(prep, sleeper):
    p = prep(POSTS)
    r = run(p, sleeper)
    assert r.status == "completed" and r.counts == {"deleted_verified": 20}
    assert sorted(p.adapter.delete_calls()) == IDS  # each exactly once
    assert not [i for i in p.adapter.present_ids("fixture:alice") if i.startswith("post/")]
    assert len([i for i in p.adapter.present_ids("fixture:alice") if i.startswith("like/")]) == 10
    row = p.store.get_item_row("fixture", "fixture:alice", IDS[0])
    assert row["state"] == "deleted"


def test_only_plan_items_deleted_even_if_new_matches_appear(prep, store, sleeper):
    p = prep(Filter(keywords=["vacation"]))
    planned = [r["item_id"] for r in store.plan_items(p.plan.plan_id)]
    new_id = p.adapter.add_item("fixture:alice", "post", "Vacation again!", datetime(2023, 1, 1, tzinfo=timezone.utc))
    scan_account(store, p.adapter, p.account, sleep=lambda s: None)  # new item is now stored locally
    run(p, sleeper)
    assert sorted(p.adapter.delete_calls()) == sorted(planned)
    assert new_id in p.adapter.present_ids("fixture:alice")


def test_item_edited_on_platform_after_plan_is_skipped(prep, store, sleeper):
    p = prep(POSTS)
    p.adapter.edit_item("fixture:alice", IDS[3], "edited after the preview")
    run(p, sleeper)
    row = outcomes(store, p.plan)[IDS[3]]
    assert row["outcome"] == "skipped" and "changed on the platform" in row["detail"]
    assert IDS[3] in p.adapter.present_ids("fixture:alice")


def test_item_changed_in_local_db_after_plan_is_skipped_without_request(prep, store, sleeper):
    p = prep(POSTS)
    p.adapter.edit_item("fixture:alice", IDS[4], "edited")
    scan_account(store, p.adapter, p.account, ["post"], sleep=lambda s: None)  # re-scan sees new version
    run(p, sleeper)
    row = outcomes(store, p.plan)[IDS[4]]
    assert row["outcome"] == "skipped" and "changed since plan was created" in row["detail"]
    assert IDS[4] not in p.adapter.delete_calls()


def test_tampered_or_missing_export_refused(prep, sleeper):
    p = prep(POSTS)
    path = p.store.get_plan(p.plan.plan_id).export_path
    with open(path, "a") as f:
        f.write(" ")
    with pytest.raises(PlanRefused, match="export missing or modified"):
        run(p, sleeper)
    os.unlink(path)
    with pytest.raises(PlanRefused, match="export missing or modified"):
        run(p, sleeper)
    assert p.adapter.delete_calls() == []


def test_distinct_outcomes(prep, store, sleeper):
    faults = {
        "inaccessible": [IDS[1]],
        "permanent": [IDS[2]],
        "accept_but_keep": [IDS[3]],
        "unverifiable": [IDS[4]],
    }
    p = prep(POSTS, faults=faults)
    p.adapter.remove_item("fixture:alice", IDS[5])  # already gone on the platform
    r = run(p, sleeper)
    o = outcomes(store, p.plan)
    assert r.status == "completed"
    assert o[IDS[1]]["outcome"] == "inaccessible"
    assert o[IDS[2]]["outcome"] == "failed" and "request failed" in o[IDS[2]]["detail"]
    assert o[IDS[3]]["outcome"] == "failed" and "still present" in o[IDS[3]]["detail"]
    assert o[IDS[4]]["outcome"] == "deleted_unverified"
    assert o[IDS[5]]["outcome"] == "already_deleted"
    assert r.counts == {"deleted_verified": 15, "inaccessible": 1, "failed": 2, "deleted_unverified": 1, "already_deleted": 1}
    assert store.get_item_row("fixture", "fixture:alice", IDS[3])["state"] == "present"
    # accept_but_keep re-checked verify_retries times before giving up
    assert sleeper.calls.count(0) == 2


def test_transient_below_limit_retries_then_verifies(prep, store, sleeper):
    p = prep(POSTS, faults={"transient": {IDS[0]: 1}})
    run(p, sleeper)
    row = outcomes(store, p.plan)[IDS[0]]
    assert row["outcome"] == "deleted_verified" and row["attempts"] == 2
    assert sum(sleeper.calls) == 2.0 and max(sleeper.calls) <= 1.0  # backoff_base * 2**0, chunked
    assert p.adapter.delete_calls().count(IDS[0]) == 2


def test_transient_at_limit_fails_with_exactly_max_attempts_calls(prep, store, sleeper):
    p = prep(POSTS, faults={"transient": {IDS[0]: 99}})
    run(p, sleeper, max_attempts=3)
    row = outcomes(store, p.plan)[IDS[0]]
    assert row["outcome"] == "failed" and "after 3 attempts" in row["detail"]
    assert p.adapter.delete_calls().count(IDS[0]) == 3
    assert sum(sleeper.calls) == 6.0 and max(sleeper.calls) <= 1.0


def test_rate_limit_waits_recorded_then_paused_then_resume_completes(prep, store, sleeper):
    p = prep(POSTS)
    order = [r["item_id"] for r in store.plan_items(p.plan.plan_id)]  # plan runs oldest-first
    p = prep(POSTS, faults={"rate_limit": {order[2]: 3}})
    r = run(p, sleeper, max_rate_limit_waits=2)
    assert r.status == "paused" and "rate limited" in r.message
    assert sum(sleeper.calls) == 15 and max(sleeper.calls) <= 1.0  # bounded; escalation 5s then 10s
    o = outcomes(store, p.plan)
    assert o[order[2]]["outcome"] == "pending" and o[order[1]]["outcome"] == "deleted_verified"
    assert store.get_job(r.job_id).rate_limited_until is not None
    r2 = run(p, sleeper, max_rate_limit_waits=2)
    assert r2.status == "completed" and r2.counts == {"deleted_verified": 20}
    assert all(c <= 5 for c in sleeper.calls)  # earlier rate-limit window honoured, never above max


def test_single_rate_limit_does_not_consume_an_attempt(prep, store, sleeper):
    p = prep(POSTS, faults={"rate_limit": {IDS[0]: 1}})
    run(p, sleeper)
    row = outcomes(store, p.plan)[IDS[0]]
    assert row["outcome"] == "deleted_verified" and row["attempts"] == 1
    assert sum(sleeper.calls) == 5 and max(sleeper.calls) <= 1.0


def test_auth_expiry_stops_then_resume_after_relogin(prep, store, sleeper):
    p = prep(POSTS, faults={"auth_after": 3})
    r = run(p, sleeper)
    assert r.status == "stopped_auth" and "log in again" in r.message
    assert r.counts == {"deleted_verified": 3, "pending": 17}
    calls_before = len(p.adapter.delete_calls())
    r_again = run(p, sleeper)  # still logged out: stops before any request
    assert r_again.status == "stopped_auth" and len(p.adapter.delete_calls()) == calls_before
    p.adapter.login("alice")
    r2 = run(p, sleeper)
    assert r2.status == "completed" and r2.counts == {"deleted_verified": 20}
    assert sorted(p.adapter.delete_calls()) == IDS


def test_auth_challenge_stops_before_any_delete(prep, sleeper):
    p = prep(POSTS, faults={"auth_challenge": True})
    r = run(p, sleeper)
    assert r.status == "stopped_auth" and p.adapter.delete_calls() == []


def test_crash_after_request_sent_then_resume_never_double_deletes(prep, store, sleeper):
    victim = IDS[6]
    p = prep(POSTS, faults={"crash_after_delete": [victim]})
    before = signal.getsignal(signal.SIGINT)
    with pytest.raises(KeyboardInterrupt):
        run(p, sleeper)
    assert signal.getsignal(signal.SIGINT) is before  # handler restored
    row = outcomes(store, p.plan)[victim]
    assert row["outcome"] == "in_progress"
    job = store.list_jobs()[0]
    assert job["status"] == "paused"
    r = run(p, sleeper)
    assert r.status == "completed"
    row = outcomes(store, p.plan)[victim]
    assert row["outcome"] == "deleted_verified" and "interrupted attempt" in row["detail"]
    assert p.adapter.delete_calls().count(victim) == 1
    assert sorted(p.adapter.delete_calls()) == IDS


def test_stale_running_status_without_lock_is_recovered(prep, store, sleeper):
    p = prep(POSTS)
    job = store.get_or_create_job(p.plan.plan_id)
    store.update_job(job.job_id, status="running")  # a process died while running
    r = run(p, sleeper)
    assert r.status == "completed"


def test_pause_mid_run_then_resume(prep, store, sleeper):
    p = prep(POSTS)
    job = store.get_or_create_job(p.plan.plan_id)
    done = []

    def progress(item_id, outcome, detail):
        done.append(item_id)
        if len(done) == 3:
            store.request_control(job.job_id, "pause")

    r = run(p, sleeper, progress=progress)
    assert r.status == "paused" and r.counts == {"deleted_verified": 3, "pending": 17}
    r2 = run(p, sleeper)
    assert r2.status == "completed" and sorted(p.adapter.delete_calls()) == IDS


def test_cancel_mid_run_marks_remaining_skipped(prep, store, sleeper):
    p = prep(POSTS)
    job = store.get_or_create_job(p.plan.plan_id)
    done = []

    def progress(item_id, outcome, detail):
        done.append(item_id)
        if len(done) == 2:
            store.request_control(job.job_id, "cancel")

    r = run(p, sleeper, progress=progress)
    assert r.status == "cancelled" and r.counts == {"deleted_verified": 2, "skipped": 18}
    assert outcomes(store, p.plan)[IDS[5]]["detail"] == "cancelled before attempt"
    with pytest.raises(PlanRefused, match="cancelled"):
        run(p, sleeper)
    assert len(p.adapter.delete_calls()) == 2


def test_cancel_and_pause_when_not_running(prep, store, sleeper):
    p = prep(POSTS)
    job = store.get_or_create_job(p.plan.plan_id)
    assert "not running" in pause_job(store, job.job_id)
    cancel_job(store, job.job_id)
    assert store.get_job(job.job_id).status == "cancelled"
    assert store.outcome_counts(p.plan.plan_id) == {"skipped": 20}
    assert p.adapter.delete_calls() == []


def test_completed_job_cannot_rerun(prep, sleeper):
    p = prep(POSTS)
    run(p, sleeper)
    calls = len(p.adapter.delete_calls())
    with pytest.raises(PlanRefused, match="completed"):
        run(p, sleeper)
    assert len(p.adapter.delete_calls()) == calls


def test_job_lock_held_by_another_process_refuses(prep, store, sleeper):
    p = prep(POSTS)
    job = store.get_or_create_job(p.plan.plan_id)
    lock = paths.home() / "locks"
    lock.mkdir(exist_ok=True)
    fd = os.open(lock / f"job-{job.job_id}.lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with pytest.raises(PlanRefused, match="already running"):
            run(p, sleeper)
        assert "requested" in pause_job(store, job.job_id)  # held lock => treated as running
        assert store.get_job(job.job_id).control == "pause"
    finally:
        os.close(fd)
    assert p.adapter.delete_calls() == []


def test_account_lock_refuses_second_plan_for_same_account_only(prep, store, sleeper):
    p = prep(POSTS)  # alice, first plan
    second = create_plan(store, "fixture", p.account, Filter(content_types=["like"]))
    before = list(p.adapter.delete_calls())
    lock = paths.home() / "locks"
    lock.mkdir(exist_ok=True)
    fd = os.open(lock / "account-fixture-fixture_alice.lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        ex = make_executor(p, sleeper)
        with pytest.raises(PlanRefused, match="another deletion job for this account"):
            ex.run(second.plan_id, second.confirmation_token)
        assert p.adapter.delete_calls() == before == []
        # a different account is not blocked by alice's account lock
        b = prep(Filter(content_types=["post"]), who="bob")
        rb = make_executor(b, sleeper).run(b.plan.plan_id, b.plan.confirmation_token)
        assert rb.status == "completed"
        assert b.adapter.delete_calls()
    finally:
        os.close(fd)
    # refused run released its job lock, so the same plan can run once the account is free
    r = make_executor(p, sleeper).run(second.plan_id, second.confirmation_token)
    assert r.status == "completed"
    assert len([i for i in p.adapter.delete_calls() if i.startswith("like/alice/")]) == 10  # each once


def test_pacing_between_delete_requests(prep, sleeper):
    p = prep(Filter(content_types=["like"]))
    now = [100.0]
    r = Executor(p.store, p.adapter, sleep=sleeper, monotonic=lambda: now[0], min_interval=1.5, verify_delay=0).run(
        p.plan.plan_id, p.plan.confirmation_token
    )
    assert r.status == "completed"
    assert sum(sleeper.calls) == 13.5 and max(sleeper.calls) <= 1.0  # clock never advances, so each request after the first waits


# ---------------- interruptible waits ----------------

def _order(p, store):
    return [r["item_id"] for r in store.plan_items(p.plan.plan_id)]  # plan runs oldest-first


def test_rate_limit_wait_is_chunked_and_totals_requested(prep, store, sleeper):
    p = prep(POSTS)
    first = _order(p, store)[0]
    ex = make_executor(p, sleeper)
    # emulate a long Retry-After by patching the adapter's raised exception
    from sociyx.models import RateLimited
    orig = p.adapter.delete
    state = {"n": 0}

    def delete(item):
        if item.item_id == first and state["n"] == 0:
            state["n"] += 1
            raise RateLimited("slow down", retry_after=300)
        return orig(item)

    p.adapter.delete = delete
    r = ex.run(p.plan.plan_id, p.plan.confirmation_token)
    assert r.status == "completed"
    assert sum(sleeper.calls) == 300 and max(sleeper.calls) <= 1.0 and len(sleeper.calls) == 300


def _long_rl_setup(prep, store, interrupt):
    """First item raises RateLimited(300); `interrupt(ex, calls)` runs inside each fake sleep."""
    from sociyx.models import RateLimited
    p = prep(POSTS)
    order = _order(p, store)
    orig = p.adapter.delete
    state = {"limited": False}

    def delete(item):
        if item.item_id == order[0] and not state["limited"]:
            state["limited"] = True
            raise RateLimited("slow down", retry_after=300)
        return orig(item)

    p.adapter.delete = delete
    calls: list[float] = []
    holder = {}

    def sleep(sec):
        calls.append(sec)
        interrupt(holder["ex"], len(calls), p)

    ex = Executor(store, p.adapter, sleep=sleep, min_interval=0, verify_delay=0)
    holder["ex"] = ex
    return p, ex, order, calls


def test_pause_during_long_rate_limit_wait_requeues_item_and_resume_completes(prep, store):
    def interrupt(ex, n, p):
        if n == 3:
            store.request_control(ex._job_id, "pause")

    p, ex, order, calls = _long_rl_setup(prep, store, interrupt)
    r = ex.run(p.plan.plan_id, p.plan.confirmation_token)
    assert r.status == "paused" and len(calls) == 3  # stopped within one chunk of the request
    row = outcomes(store, p.plan)[order[0]]
    assert row["outcome"] == "pending" and "interrupted while waiting; not deleted" in row["detail"]
    assert p.adapter.delete_calls() == []  # the limited call never reached the fixture; nothing extra sent
    assert store.get_job(r.job_id).control == ""
    r2 = Executor(store, p.adapter, sleep=lambda s: None, min_interval=0, verify_delay=0).run(
        p.plan.plan_id, p.plan.confirmation_token)
    assert r2.status == "completed" and r2.counts == {"deleted_verified": 20}


def test_cancel_during_long_rate_limit_wait_skips_remaining(prep, store):
    def interrupt(ex, n, p):
        if n == 2:
            store.request_control(ex._job_id, "cancel")

    p, ex, order, calls = _long_rl_setup(prep, store, interrupt)
    r = ex.run(p.plan.plan_id, p.plan.confirmation_token)
    assert r.status == "cancelled" and len(calls) == 2
    assert r.counts == {"skipped": 20}
    assert p.adapter.delete_calls() == []


def test_ctrl_c_flag_during_wait_pauses_with_item_pending(prep, store):
    def interrupt(ex, n, p):
        if n == 4:
            ex._stop_requested = True  # what the first SIGINT does

    p, ex, order, calls = _long_rl_setup(prep, store, interrupt)
    r = ex.run(p.plan.plan_id, p.plan.confirmation_token)
    assert r.status == "paused" and "Ctrl+C" in r.message and len(calls) == 4
    assert outcomes(store, p.plan)[order[0]]["outcome"] == "pending"


def test_pacing_interrupted_leaves_item_pending_not_in_progress(prep, store):
    p = prep(Filter(content_types=["like"]))
    order = _order(p, store)
    calls: list[float] = []

    def sleep(sec):
        calls.append(sec)
        if len(calls) == 5:
            store.request_control(ex._job_id, "pause")

    ex = Executor(store, p.adapter, sleep=sleep, monotonic=lambda: 100.0, min_interval=60, verify_delay=0)
    r = ex.run(p.plan.plan_id, p.plan.confirmation_token)
    assert r.status == "paused" and len(calls) == 5
    o = outcomes(store, p.plan)
    assert o[order[0]]["outcome"] == "deleted_verified"
    # The second item was never marked in_progress: pacing happens before the commit.
    assert o[order[1]]["outcome"] == "pending" and o[order[1]]["attempts"] == 0
    assert p.adapter.delete_calls() == [order[0]]
    r2 = Executor(store, p.adapter, sleep=lambda s: None, monotonic=lambda: 100.0, min_interval=60,
                  verify_delay=0).run(p.plan.plan_id, p.plan.confirmation_token)
    assert r2.status == "completed"


# ---------------- honest "already deleted" ----------------

def test_lost_response_then_gone_is_verified_not_already_deleted(prep, store, sleeper):
    p = prep(POSTS, faults={"transient_then_gone": [IDS[0]]})
    p.adapter.remove_item("fixture:alice", IDS[5])  # genuinely gone before any request of ours
    r = run(p, sleeper)
    o = outcomes(store, p.plan)
    assert r.status == "completed"
    assert o[IDS[0]]["outcome"] == "deleted_verified"
    assert "earlier attempt" in o[IDS[0]]["detail"]
    assert store.get_item_row("fixture", "fixture:alice", IDS[0])["state"] == "deleted"
    assert o[IDS[5]]["outcome"] == "already_deleted"
    assert store.get_item_row("fixture", "fixture:alice", IDS[5])["state"] == "deleted"


def test_interrupted_backoff_then_gone_on_resume_is_verified(prep, store):
    p = prep(POSTS)
    first = _order(p, store)[0]
    p.adapter.faults["transient_then_gone"] = [first]
    calls = []

    def sleep(sec):
        calls.append(sec)
        store.request_control(ex._job_id, "pause")

    ex = Executor(store, p.adapter, sleep=sleep, min_interval=0, verify_delay=0)
    r = ex.run(p.plan.plan_id, p.plan.confirmation_token)
    assert r.status == "paused"
    assert outcomes(store, p.plan)[first]["outcome"] == "pending"
    Executor(store, p.adapter, sleep=lambda s: None, min_interval=0, verify_delay=0).run(
        p.plan.plan_id, p.plan.confirmation_token)
    row = outcomes(store, p.plan)[first]
    assert row["outcome"] == "deleted_verified" and "earlier attempt" in row["detail"]


# ---------------- secret hygiene ----------------

def test_error_details_are_scrubbed(prep, store, sleeper):
    p = prep(POSTS)
    first = _order(p, store)[0]
    from sociyx.models import PermanentError
    orig = p.adapter.delete

    def delete(item):
        if item.item_id == first:
            raise PermanentError("401 for Authorization: Bearer abc.def.ghi")
        return orig(item)

    p.adapter.delete = delete
    run(p, sleeper)
    row = outcomes(store, p.plan)[first]
    assert row["outcome"] == "failed"
    assert "abc.def.ghi" not in row["detail"] and "[REDACTED]" in row["detail"]


# ---------------- slow-platform pacing: jitter, caps, think pauses, soft blocks ----------------

import random
from datetime import timedelta

from sociyx.adapters.fixture import FixtureAdapter
from sociyx.models import ActionBlocked, RateLimited, to_iso
from sociyx.store import now_iso


class Slow(FixtureAdapter):
    platform = "fixture"
    min_delete_interval = 10.0
    pacing_jitter = 0.5


class Capped(FixtureAdapter):
    platform = "fixture"
    daily_write_cap = 3


class Thinker(FixtureAdapter):
    platform = "fixture"
    think_pause_every = 3
    think_pause_seconds = (100.0, 200.0)


def _swap(p, cls, **kw):
    p.adapter = cls(**kw)
    return p


def test_pacing_jitter_bounds_with_seeded_rng(prep, sleeper):
    p = _swap(prep(Filter(content_types=["like"])), Slow)
    now = [0.0]
    ex = Executor(p.store, p.adapter, sleep=lambda s: (sleeper(s), now.__setitem__(0, now[0] + s)),
                  monotonic=lambda: now[0], verify_delay=0, rng=random.Random(7))
    r = ex.run(p.plan.plan_id, p.plan.confirmation_token)
    assert r.status == "completed"
    # requests are 10 intervals apart per item; each total gap lies in [10, 15]
    total = sum(sleeper.calls)
    n_gaps = 9
    assert 10 * n_gaps <= total <= 15 * n_gaps
    assert total != 10 * n_gaps  # jitter actually applied
    ref = random.Random(7)
    assert abs(total - sum(10 * ref.uniform(1, 1.5) for _ in range(n_gaps))) < 1e-6


def test_daily_cap_across_two_plans_and_other_account_unaffected(prep, store, sleeper):
    p = _swap(prep(Filter(content_types=["post"])), Capped)
    ids = _order(p, store)
    p2 = create_plan(store, "fixture", p.account, Filter(content_types=["like"]))
    r1 = Executor(store, p.adapter, sleep=sleeper, min_interval=0, verify_delay=0).run(
        p.plan.plan_id, p.plan.confirmation_token)
    assert r1.status == "paused" and "daily cap of 3 reached; resume after" in r1.message
    assert r1.counts.get("deleted_verified") == 3 and r1.counts.get("pending") == 17
    # a second plan of the same account pauses immediately without any request
    before = len(p.adapter.delete_calls())
    r2 = Executor(store, p.adapter, sleep=sleeper, min_interval=0, verify_delay=0).run(
        p2.plan_id, p2.confirmation_token)
    assert r2.status == "paused" and "daily cap of 3" in r2.message
    assert len(p.adapter.delete_calls()) == before
    # a different account is not affected
    other = prepare(store, Filter(content_types=["post"]), who="bob")
    other.adapter = Capped()
    r3 = Executor(store, other.adapter, sleep=sleeper, min_interval=0, verify_delay=0).run(
        other.plan.plan_id, other.plan.confirmation_token)
    assert r3.counts.get("deleted_verified") == 3
    # age the log beyond 24h: the second plan proceeds again (up to the cap)
    old = to_iso(datetime.now(timezone.utc) - timedelta(hours=25))
    store.conn.execute("UPDATE request_log SET ts = ? WHERE account_id = ?", (old, p.account.account_id))
    r4 = Executor(store, p.adapter, sleep=sleeper, min_interval=0, verify_delay=0).run(
        p2.plan_id, p2.confirmation_token)
    assert r4.counts.get("deleted_verified") == 3 and r4.status == "paused"
    assert ids  # sanity


def test_daily_cap_resume_time_is_when_oldest_counted_request_ages_out(prep, store, sleeper):
    p = _swap(prep(Filter(content_types=["post"])), Capped)
    base = datetime(2030, 1, 1, 12, 0, tzinfo=timezone.utc)
    for i, mins in enumerate((30, 20, 10)):  # three requests already in the window
        store.log_request("fixture", p.account.account_id, to_iso(base - timedelta(minutes=mins)))
    ex = Executor(store, p.adapter, sleep=sleeper, min_interval=0, now=lambda: base)
    r = ex.run(p.plan.plan_id, p.plan.confirmation_token)
    expect = (base - timedelta(minutes=30) + timedelta(hours=24)).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    assert r.status == "paused" and r.message.endswith(expect)
    assert p.adapter.delete_calls() == []
    assert r.counts == {"pending": 20}


def test_executor_daily_cap_can_lower_but_not_raise(prep, store, sleeper):
    p = _swap(prep(Filter(content_types=["post"])), Capped)
    assert Executor(store, p.adapter, daily_cap=2).daily_cap == 2
    assert Executor(store, p.adapter, daily_cap=50).daily_cap == 3
    assert Executor(store, FixtureAdapter(), daily_cap=5).daily_cap == 5
    assert Executor(store, FixtureAdapter()).daily_cap is None
    r = Executor(store, p.adapter, sleep=sleeper, min_interval=0, verify_delay=0, daily_cap=2).run(
        p.plan.plan_id, p.plan.confirmation_token)
    assert r.counts.get("deleted_verified") == 2 and "daily cap of 2" in r.message


def test_every_attempt_counts_toward_cap(prep, store, sleeper):
    p = prep(POSTS, faults={"transient": {IDS[0]: 1}})
    p.adapter = Capped(faults=p.adapter.faults)
    first = _order(p, store)[0]
    p.adapter.faults["transient"] = {first: 1}
    r = Executor(store, p.adapter, sleep=sleeper, min_interval=0, verify_delay=0).run(
        p.plan.plan_id, p.plan.confirmation_token)
    assert r.counts.get("deleted_verified") == 2  # 1 failed attempt + 2 successes = 3 requests


def test_think_pause_cadence(prep, store):
    p = _swap(prep(Filter(content_types=["like"])), Thinker)
    calls: list[float] = []
    ex = Executor(store, p.adapter, sleep=calls.append, min_interval=0, verify_delay=0, rng=random.Random(1))
    ex.run(p.plan.plan_id, p.plan.confirmation_token)
    ref = random.Random(1)
    expected = [ref.uniform(100, 200) for _ in range(3)]  # before the 4th, 7th and 10th request
    assert sum(calls) == pytest.approx(sum(expected))
    assert max(calls) <= 1.0  # chunked, interruptible


def test_think_pause_interruptible_leaves_item_pending(prep, store):
    p = _swap(prep(Filter(content_types=["like"])), Thinker)
    calls: list[float] = []
    holder = {}

    def sleep(s):
        calls.append(s)
        holder["ex"]._stop_requested = True

    ex = holder["ex"] = Executor(store, p.adapter, sleep=sleep, min_interval=0, verify_delay=0)
    r = ex.run(p.plan.plan_id, p.plan.confirmation_token)
    assert r.status == "paused" and r.counts == {"deleted_verified": 3, "pending": 7}


def test_action_blocked_stops_job_resume_refused_until_cooldown(prep, store, sleeper):
    p = prep(POSTS)
    order = _order(p, store)
    p = prep(POSTS, faults={"action_blocked": [order[2]], "action_blocked_cooldown": 7200})
    r = run(p, sleeper)
    assert r.status == "stopped_blocked"
    assert "Stop automation" in r.message and "2h" in r.message
    o = outcomes(store, p.plan)
    assert o[order[2]]["outcome"] == "pending" and "not retried" in o[order[2]]["detail"]
    assert order[2] not in p.adapter.delete_calls()
    assert store.get_job(r.job_id).rate_limited_until is not None
    with pytest.raises(PlanRefused, match="do not resume before"):
        run(p, sleeper)
    p.adapter.faults.clear()
    r2 = make_executor(p, sleeper, ignore_cooldown=True).run(p.plan.plan_id, p.plan.confirmation_token)
    assert r2.status == "completed" and store.get_job(r.job_id).rate_limited_until is None


def test_action_blocked_resume_allowed_after_cooldown_passes(prep, store, sleeper):
    p = prep(POSTS, faults={"action_blocked": [IDS[0]]})
    p.adapter.faults["action_blocked"] = [_order(p, store)[0]]
    r = run(p, sleeper)
    assert r.status == "stopped_blocked" and "1h" in r.message
    p.adapter.faults.clear()
    later = datetime.now(timezone.utc) + timedelta(hours=2)
    r2 = make_executor(p, sleeper, now=lambda: later).run(p.plan.plan_id, p.plan.confirmation_token)
    assert r2.status == "completed"


def test_action_blocked_default_cooldown_is_24h(prep, store, sleeper):
    p = prep(POSTS)
    first = _order(p, store)[0]
    orig = p.adapter.delete

    def delete(item):
        raise ActionBlocked("feedback_required")

    p.adapter.delete = delete
    r = run(p, sleeper)
    assert r.status == "stopped_blocked" and "24h" in r.message
    until = datetime.fromisoformat(store.get_job(r.job_id).rate_limited_until.replace("Z", "+00:00"))
    assert timedelta(hours=23) < until - datetime.now(timezone.utc) <= timedelta(hours=24)
    assert outcomes(store, p.plan)[first]["outcome"] == "pending"


def test_action_blocked_during_verify_is_unverified_and_stops(prep, store, sleeper):
    p = prep(POSTS)
    order = _order(p, store)
    p = prep(POSTS, faults={"verify_blocked": [order[1]]})
    r = run(p, sleeper)
    assert r.status == "stopped_blocked"
    o = outcomes(store, p.plan)
    assert o[order[1]]["outcome"] == "deleted_unverified" and o[order[0]]["outcome"] == "deleted_verified"
    assert o[order[2]]["outcome"] == "pending"


def test_consecutive_rate_limits_double_the_wait(prep, store, sleeper):
    p = prep(POSTS, faults={"rate_limit": {IDS[0]: 0}})
    first = _order(p, store)[0]
    p.adapter.faults["rate_limit"] = {first: 4}  # retry_after=5 -> waits 5,10,20,40
    r = run(p, sleeper, max_rate_limit_waits=5)
    assert r.status == "completed" and sum(sleeper.calls) == 75


def test_rate_limit_escalation_capped_by_max_wait(prep, store, sleeper):
    p = prep(POSTS)
    first = _order(p, store)[0]
    orig, n = p.adapter.delete, {"n": 0}

    def delete(item):
        if item.item_id == first and n["n"] < 4:
            n["n"] += 1
            raise RateLimited("slow", retry_after=None)
        return orig(item)

    p.adapter.delete = delete
    r = run(p, sleeper, max_rate_limit_waits=5, max_wait=100)
    assert r.status == "completed" and sum(sleeper.calls) == 60 + 100 + 100 + 100
