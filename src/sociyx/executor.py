"""Deletion executor — the safety-critical part.

Guarantees, in order of enforcement:
  * nothing happens without the exact confirmation token and an untampered export;
  * only items in the plan are ever touched, and only if they still match what was previewed;
  * the account behind the session is re-checked before the first delete;
  * an item is marked IN_PROGRESS (committed) *before* the request is sent, so a crash
    leaves "unknown" rather than a false "pending"; recovery verifies before retrying;
  * auth problems and challenges stop the run — we never try to get around them;
  * outcomes are never overstated (verified / unverified / failed are distinct).
"""

from __future__ import annotations

import fcntl
import os
import random
import re
import signal
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable

from . import paths
from .adapters.base import Adapter
from .models import (
    ActionBlocked,
    AdapterError,
    AuthRequired,
    DeleteStatus,
    Outcome,
    PermanentError,
    RateLimited,
    TransientError,
    VerifyStatus,
    from_iso,
    to_iso,
)
from .planning import PlanRefused, check_plan_ready
from .redact import scrub
from .store import Plan, Store, now_iso

__all__ = ["Executor", "PlanRefused", "RunResult", "pause_job", "cancel_job", "job_is_running"]


@dataclass
class RunResult:
    job_id: int
    status: str
    counts: dict[str, int] = field(default_factory=dict)
    message: str = ""


def _job_lock_name(job_id: int) -> str:
    return f"job-{job_id}.lock"


def _account_lock_name(platform: str, account_id: str) -> str:
    safe_account_id = re.sub(r"[^A-Za-z0-9._-]", "_", account_id)
    return f"account-{platform}-{safe_account_id}.lock"


def _try_lock(name: str) -> int | None:
    """Exclusive non-blocking flock on locks/<name>; returns an fd to keep open, or None if held elsewhere."""
    d = paths.private_dir(paths.home() / "locks")
    fd = os.open(d / name, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def _unlock(fd: int) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


class Executor:
    def __init__(
        self,
        store: Store,
        adapter: Adapter,
        *,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        min_interval: float | None = None,
        max_attempts: int = 3,
        backoff_base: float = 2.0,
        backoff_cap: float = 60.0,
        max_rate_limit_waits: int = 5,
        max_wait: float = 900,
        verify_retries: int = 2,
        verify_delay: float = 3.0,
        progress: Callable[[str, Outcome, str], None] | None = None,
        rng: random.Random | None = None,
        daily_cap: int | None = None,
        ignore_cooldown: bool = False,
        now: Callable[[], datetime] | None = None,
        on_wait: Callable[[float, str], None] | None = None,
        on_state: Callable[[str], None] | None = None,
    ):
        self.store = store
        self.adapter = adapter
        self.sleep = sleep
        self.monotonic = monotonic
        self.min_interval = adapter.min_delete_interval if min_interval is None else min_interval
        self.max_attempts = max_attempts
        self.backoff_base = backoff_base
        self.backoff_cap = backoff_cap
        self.max_rate_limit_waits = max_rate_limit_waits
        self.max_wait = max_wait
        self.verify_retries = verify_retries
        self.verify_delay = verify_delay
        self.progress = progress
        # UI hooks (never needed for safety): on_wait(seconds, reason) fires at the start of every
        # non-zero wait with reason "pacing" | "think pause" | "rate limit" | "retrying";
        # on_state(text) reports what the executor is doing ("checking session", "deleting <id>", ...).
        self.on_wait = on_wait
        self.on_state = on_state
        self.rng = rng if rng is not None else random.Random()
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.ignore_cooldown = ignore_cooldown
        # A user-supplied cap may lower the adapter's cap, never raise it.
        caps = [c for c in (adapter.daily_write_cap, daily_cap) if c is not None]
        self.daily_cap: int | None = min(caps) if caps else None
        self._interval = self.min_interval
        self._run_requests = 0
        self._think_done_at = 0
        self._last_request: float | None = None
        self._stop_requested = False
        self._job_id: int | None = None

    # ---------------- public ----------------

    def run(self, plan_id: int, confirm: str) -> RunResult:
        store = self.store
        plan = store.get_plan(plan_id)
        if confirm != plan.confirmation_token:
            raise PlanRefused("confirmation token missing or wrong; nothing was deleted")
        check_plan_ready(store, plan)
        job = store.get_or_create_job(plan_id)
        self._refuse_finished(job.status, job.job_id)
        fd = _try_lock(_job_lock_name(job.job_id))
        if fd is None:
            raise PlanRefused("job already running in another process")
        # Per-account lock: two different plans for one account must never delete concurrently,
        # or they would together exceed the platform's per-account rate limits.
        acct_fd = _try_lock(_account_lock_name(plan.platform, plan.account_id))
        if acct_fd is None:
            _unlock(fd)
            raise PlanRefused("another deletion job for this account is already running")
        self._job_id = job.job_id
        self._stop_requested = False
        self._run_requests = 0
        self._think_done_at = 0
        self._last_request = None
        old_handler = self._install_sigint()
        try:
            job = store.get_job(job.job_id)
            self._refuse_finished(job.status, job.job_id)
            if job.status == "stopped_blocked":
                until = from_iso(job.rate_limited_until)
                if until and until > self.now() and not self.ignore_cooldown:
                    raise PlanRefused(
                        f"job {job.job_id} was stopped because the platform restricted this account; "
                        f"do not resume before {until.astimezone().strftime('%Y-%m-%d %H:%M %Z')} "
                        "(pass --ignore-cooldown only if you know the block was cleared)"
                    )
                store.update_job(job.job_id, rate_limited_until=None)
            if job.status == "running":
                # DB says running but we hold the lock: the previous process died.
                self._event("warn", "previous run did not finish cleanly; recovering")
            try:
                return self._run_locked(plan, job.job_id)
            except KeyboardInterrupt:
                store.update_job(job.job_id, status="paused", error="interrupted by user; resume to continue")
                self._event("warn", "interrupted; the current item (if any) is left as in_progress")
                raise
            except PlanRefused:
                raise
            except BaseException as e:
                store.update_job(job.job_id, status="paused", error=f"unexpected {type(e).__name__}; resume to continue")
                self._event("error", f"unexpected {type(e).__name__}")
                raise
        finally:
            self._restore_sigint(old_handler)
            try:
                _unlock(acct_fd)
            finally:
                _unlock(fd)

    # ---------------- internals ----------------

    @staticmethod
    def _refuse_finished(status: str, job_id: int) -> None:
        if status in ("completed", "cancelled"):
            raise PlanRefused(f"job {job_id} is already {status}; create a new plan to delete more")

    def _event(self, level: str, msg: str) -> None:
        self.store.log_event(self._job_id, level, msg)

    def _install_sigint(self):
        if threading.current_thread() is not threading.main_thread():
            return None

        def handler(signum, frame):
            if self._stop_requested:
                raise KeyboardInterrupt
            self._stop_requested = True

        return signal.signal(signal.SIGINT, handler)

    @staticmethod
    def _restore_sigint(old) -> None:
        if old is not None and threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGINT, old)

    def _stop(self, job_id: int, status: str, message: str, counts_plan: int) -> RunResult:
        message = scrub(message)
        self.store.update_job(job_id, status=status, error=message)
        self._event("info", f"{status}: {message}")
        return RunResult(job_id, status, self.store.outcome_counts(counts_plan), message)

    def _run_locked(self, plan: Plan, job_id: int) -> RunResult:
        store, adapter = self.store, self.adapter
        self._state("checking the logged-in account")
        try:
            live = adapter.current_account(plan.account_id)
        except ActionBlocked as e:
            return self._stop(job_id, "stopped_blocked", self._block(job_id, e), plan.plan_id)
        except AuthRequired as e:
            return self._stop(job_id, "stopped_auth",
                              f"not logged in ({e}); log in again with `sociyx login {plan.platform}` and resume",
                              plan.plan_id)
        if live.platform != plan.platform or live.account_id != plan.account_id:
            store.update_job(job_id, status="paused", error="session belongs to a different account")
            self._event("error", "session account differs from plan account; refusing")
            raise PlanRefused(
                f"logged-in account {live.account_id!r} is not the plan's account {plan.account_id!r}; nothing deleted"
            )

        job = store.get_job(job_id)
        store.update_job(job_id, control="", status="running", error=None, finished_at=None)
        self._event("info", f"run started for plan {plan.plan_id} ({plan.item_count} items)")
        if job.rate_limited_until:
            until = from_iso(job.rate_limited_until)
            delta = (until - self.now()).total_seconds() if until else 0
            if delta > 0:
                self._event("info", f"waiting {min(delta, self.max_wait):.0f}s for earlier rate limit")
                intr = self._wait(min(delta, self.max_wait), job_id, "rate limit")
                if intr:
                    return self._apply_stop(plan, job_id, self._interrupt_stop(job_id, intr))
            store.update_job(job_id, rate_limited_until=None)

        rows = store.plan_items(plan.plan_id, [Outcome.PENDING, Outcome.IN_PROGRESS])  # plan items only
        for row in rows:
            if self._stop_requested:
                return self._stop(job_id, "paused", "paused by Ctrl+C", plan.plan_id)
            control = store.get_job(job_id).control
            if control == "pause":
                store.update_job(job_id, control="")
                return self._stop(job_id, "paused", "paused on request", plan.plan_id)
            if control == "cancel":
                return self._cancel(plan, job_id)
            stop = self._handle_item(plan, job_id, row)
            if stop:
                return self._apply_stop(plan, job_id, stop)

        counts = store.outcome_counts(plan.plan_id)
        if counts.get("pending") or counts.get("in_progress"):
            return self._stop(job_id, "paused", "some items are still unfinished", plan.plan_id)
        store.update_job(job_id, status="completed", finished_at=now_iso(), error=None)
        self._event("info", "completed")
        return RunResult(job_id, "completed", counts, "all items reached a final outcome")

    def _apply_stop(self, plan: Plan, job_id: int, stop: tuple[str, str]) -> RunResult:
        if stop[0] == "cancel":
            return self._cancel(plan, job_id)
        return self._stop(job_id, stop[0], stop[1], plan.plan_id)

    def _interrupted(self, job_id: int) -> str | None:
        if self._stop_requested:
            return "stop"
        control = self.store.get_job(job_id).control
        return control if control in ("pause", "cancel") else None

    def _state(self, text: str) -> None:
        if self.on_state:
            self.on_state(text)

    def _wait(self, seconds: float, job_id: int, reason: str = "") -> str | None:
        """Sleep `seconds` in chunks of <= 1s; return "stop"/"pause"/"cancel" if interrupted."""
        if seconds > 0 and self.on_wait:
            self.on_wait(seconds, reason)
        remaining = seconds
        while True:
            intr = self._interrupted(job_id)
            if intr:
                return intr
            if remaining <= 1e-9:
                return None
            chunk = min(1.0, remaining)
            self.sleep(chunk)
            remaining -= chunk

    def _interrupt_stop(self, job_id: int, intr: str) -> tuple[str, str]:
        if intr == "cancel":
            return ("cancel", "cancelled on request")
        if intr == "pause":
            self.store.update_job(job_id, control="")
            return ("paused", "paused on request")
        return ("paused", "paused by Ctrl+C")

    def _requeue(self, plan: Plan, item_id: str) -> None:
        # request_may_have_landed stays set in the DB, so a later run still knows.
        self.store.set_outcome(plan.plan_id, item_id, Outcome.PENDING, "interrupted while waiting; not deleted")

    def _cancel(self, plan: Plan, job_id: int) -> RunResult:
        _cancel_items(self.store, plan.plan_id)
        self.store.update_job(job_id, status="cancelled", control="", finished_at=now_iso())
        self._event("info", "cancelled on request")
        return RunResult(job_id, "cancelled", self.store.outcome_counts(plan.plan_id), "cancelled on request")

    def _finish_item(self, plan: Plan, row, outcome: Outcome, detail: str, item_state: str | None = None) -> None:
        store = self.store
        with store.tx():
            store.set_outcome(plan.plan_id, row["item_id"], outcome, scrub(detail))
            if item_state:
                store.set_item_state(plan.platform, plan.account_id, row["item_id"], item_state)
        self._event("info", f"{row['item_id']}: {outcome.value}" + (f" ({detail})" if detail else ""))
        if self.progress:
            self.progress(row["item_id"], outcome, detail)

    def _block(self, job_id: int, e: ActionBlocked) -> str:
        """Persist the cool-down for a soft block and return the user-facing guidance."""
        self.store.update_job(job_id, rate_limited_until=to_iso(self.now() + timedelta(seconds=e.cool_down_seconds())))
        self._event("error", f"platform restricted the account/action: {e}")
        return e.guidance()

    def _mark_request(self) -> None:
        self._last_request = self.monotonic()
        j = self.adapter.pacing_jitter
        self._interval = self.min_interval * (self.rng.uniform(1.0, 1.0 + j) if j > 0 else 1.0)

    def _pace_seconds(self) -> float:
        if self._last_request is None:
            return 0.0
        return max(0.0, self._interval - (self.monotonic() - self._last_request))

    def _cap_message(self, plan: Plan) -> str | None:
        """Non-None when the rolling-24h request cap for this account is exhausted."""
        cap = self.daily_cap
        if cap is None:
            return None
        since = to_iso(self.now() - timedelta(hours=24))
        times = self.store.request_times_since(plan.platform, plan.account_id, since)
        if len(times) < cap:
            return None
        free_at = from_iso(times[len(times) - cap]) + timedelta(hours=24)
        return (f"daily cap of {cap} reached; resume after "
                f"{free_at.astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')}")

    def _think_pause_seconds(self) -> float:
        every = self.adapter.think_pause_every
        n = self._run_requests
        if every > 0 and n > 0 and n % every == 0 and self._think_done_at != n:
            self._think_done_at = n
            lo, hi = self.adapter.think_pause_seconds
            return self.rng.uniform(lo, hi)
        return 0.0

    def _verify(self, item) -> tuple[VerifyStatus | None, str]:
        """(status, why-unverified). status None means the check itself failed.
        ActionBlocked / AuthRequired propagate so the caller can stop the job."""
        tries = 0
        while True:
            try:
                v = self.adapter.verify_deleted(item)
            except (ActionBlocked, AuthRequired):
                raise
            except AdapterError as e:
                return None, f"verification failed: {e}"
            if v == VerifyStatus.PRESENT and tries < self.verify_retries:
                tries += 1
                self.sleep(self.verify_delay)
                continue
            return v, ""

    def _handle_item(self, plan: Plan, job_id: int, row) -> tuple[str, str] | None:
        """Process one plan item. Returns (job_status, message) if the run must stop."""
        store = self.store
        item_id = row["item_id"]
        cur = store.get_item_row(plan.platform, plan.account_id, item_id)
        if cur is None or (cur["platform"], cur["account_id"]) != (plan.platform, plan.account_id):
            self._finish_item(plan, row, Outcome.SKIPPED, "item record no longer available locally")
            return None
        if cur["fingerprint"] != row["fingerprint"]:
            self._finish_item(plan, row, Outcome.SKIPPED,
                              "changed since plan was created (re-scan and make a new plan)")
            return None
        item = store.row_to_item(cur)

        # Could an earlier request of ours already have reached the platform?
        maybe_sent = bool(row["request_may_have_landed"])
        if row["outcome"] == Outcome.IN_PROGRESS:
            try:
                v = self.adapter.verify_deleted(item)
            except ActionBlocked as e:
                return "stopped_blocked", self._block(self._job_id, e)
            except AuthRequired as e:
                return ("stopped_auth", f"login expired or challenged ({e}); log in again with "
                                        f"`sociyx login {plan.platform}` and resume the job")
            except AdapterError:
                v = VerifyStatus.UNKNOWN
            if v == VerifyStatus.GONE:
                self._finish_item(plan, row, Outcome.DELETED_VERIFIED,
                                  "confirmed gone after an interrupted attempt", "deleted")
                return None
            maybe_sent = True

        attempts = 0
        rl_waits = 0
        need_mark = True
        while True:
            capped = self._cap_message(plan)
            if capped:
                if attempts:
                    self._requeue(plan, item_id)
                return "paused", capped
            # Think pause, then pace (both interruptible), then commit "attempt started" before the request leaves.
            intr = self._wait(self._think_pause_seconds(), job_id, "think pause")
            if not intr:
                intr = self._wait(self._pace_seconds(), job_id, "pacing")
            if intr:
                if attempts:
                    self._requeue(plan, item_id)
                return self._interrupt_stop(job_id, intr)
            if need_mark:
                attempts += 1
                store.set_outcome(plan.plan_id, item_id, Outcome.IN_PROGRESS,
                                  "request in flight" if attempts == 1 else "retrying", bump_attempt=True)
                need_mark = False
            store.log_request(plan.platform, plan.account_id, to_iso(self.now()))
            self._run_requests += 1
            self._state(f"deleting {item_id}")
            try:
                result = self.adapter.delete(item)
                self._mark_request()
                break
            except RateLimited as e:
                self._mark_request()
                rl_waits += 1
                if rl_waits > self.max_rate_limit_waits:
                    store.set_outcome(plan.plan_id, item_id, Outcome.PENDING, "rate limited; not attempted")
                    return "paused", "repeatedly rate limited; resume later"
                base = 60.0 if e.retry_after is None else e.retry_after
                wait = min(self.max_wait, base * 2 ** (rl_waits - 1))  # escalate on consecutive limits
                until = self.now() + timedelta(seconds=wait)
                store.update_job(job_id, rate_limited_until=to_iso(until))
                self._event("warn", f"rate limited; waiting {wait:.0f}s")
                intr = self._wait(wait, job_id, "rate limit")
                if intr:
                    self._requeue(plan, item_id)
                    return self._interrupt_stop(job_id, intr)
            except TransientError as e:
                self._mark_request()
                maybe_sent = True
                if attempts >= self.max_attempts:
                    self._finish_item(plan, row, Outcome.FAILED, f"request failed: {e} (after {attempts} attempts)")
                    return None
                intr = self._wait(min(self.backoff_cap, self.backoff_base * 2 ** (attempts - 1)), job_id, "retrying")
                if intr:
                    self._requeue(plan, item_id)
                    return self._interrupt_stop(job_id, intr)
                need_mark = True
            except ActionBlocked as e:
                self._mark_request()
                store.set_outcome(plan.plan_id, item_id, Outcome.PENDING, "platform restricted the account; not retried")
                return "stopped_blocked", self._block(job_id, e)
            except AuthRequired as e:
                store.set_outcome(plan.plan_id, item_id, Outcome.PENDING, "login required; not attempted")
                return ("stopped_auth",
                        f"login expired or challenged ({e}); log in again with `sociyx login {plan.platform}` "
                        f"and resume the job")
            except (PermanentError, AdapterError) as e:
                self._mark_request()
                self._finish_item(plan, row, Outcome.FAILED, f"request failed: {e}")
                return None

        st = result.status
        if st == DeleteStatus.ALREADY_DELETED:
            if maybe_sent:
                self._finish_item(plan, row, Outcome.DELETED_VERIFIED,
                                  "gone; most likely removed by an earlier attempt whose response was lost", "deleted")
            else:
                self._finish_item(plan, row, Outcome.ALREADY_DELETED,
                                  result.detail or "already gone before our request", "deleted")
        elif st == DeleteStatus.INACCESSIBLE:
            self._finish_item(plan, row, Outcome.INACCESSIBLE, result.detail or "platform does not allow deleting this item")
        elif st == DeleteStatus.CHANGED:
            self._finish_item(plan, row, Outcome.SKIPPED, "changed on the platform since the plan was made")
        else:
            self._state(f"verifying {item_id}")
            try:
                v, why = self._verify(item)
            except ActionBlocked as e:
                self._finish_item(plan, row, Outcome.DELETED_UNVERIFIED,
                                  "platform restricted the account during verification")
                return "stopped_blocked", self._block(job_id, e)
            except AuthRequired as e:
                self._finish_item(plan, row, Outcome.DELETED_UNVERIFIED, f"login expired during verification: {e}")
                return ("stopped_auth", f"login expired or challenged ({e}); log in again with "
                                        f"`sociyx login {plan.platform}` and resume the job")
            if v == VerifyStatus.GONE:
                self._finish_item(plan, row, Outcome.DELETED_VERIFIED, "", "deleted")
            elif v == VerifyStatus.PRESENT:
                self._finish_item(plan, row, Outcome.FAILED, "platform accepted the delete but the item is still present")
            else:
                self._finish_item(plan, row, Outcome.DELETED_UNVERIFIED,
                                  why or "platform gives no reliable way to confirm the deletion", "deleted")
        return None


def _cancel_items(store: Store, plan_id: int) -> None:
    with store.tx():
        for r in store.plan_items(plan_id, [Outcome.PENDING]):
            store.set_outcome(plan_id, r["item_id"], Outcome.SKIPPED, "cancelled before attempt")
        for r in store.plan_items(plan_id, [Outcome.IN_PROGRESS]):
            store.set_outcome(plan_id, r["item_id"], Outcome.IN_PROGRESS,
                              "cancelled while a previous attempt's result is unknown; check the platform")


def job_is_running(job_id: int) -> bool:
    """True while some process holds the job's lock (a DB status of 'running' alone can be stale)."""
    fd = _try_lock(_job_lock_name(job_id))
    if fd is None:
        return True
    _unlock(fd)
    return False


def pause_job(store: Store, job_id: int) -> str:
    job = store.get_job(job_id)
    if job.status in ("completed", "cancelled"):
        return f"job {job_id} is already {job.status}"
    fd = _try_lock(_job_lock_name(job_id))
    if fd is not None:  # nobody is running it
        try:
            if job.status == "running":
                store.update_job(job_id, status="paused", error="found stale 'running' state; marked paused")
            return f"job {job_id} is not running (status {store.get_job(job_id).status})"
        finally:
            _unlock(fd)
    store.request_control(job_id, "pause")
    return "pause requested; the job stops after the current item"


def cancel_job(store: Store, job_id: int) -> str:
    job = store.get_job(job_id)
    if job.status in ("completed", "cancelled"):
        return f"job {job_id} is already {job.status}"
    fd = _try_lock(_job_lock_name(job_id))
    if fd is not None:
        try:
            _cancel_items(store, job.plan_id)
            store.update_job(job_id, status="cancelled", control="", finished_at=now_iso())
            store.log_event(job_id, "info", "cancelled while not running")
            return f"job {job_id} cancelled; remaining items marked skipped"
        finally:
            _unlock(fd)
    store.request_control(job_id, "cancel")
    return "cancel requested; the job stops before the next item"
