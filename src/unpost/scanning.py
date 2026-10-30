"""Page through an account's content, committing progress page by page."""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import Callable

from .adapters.base import Adapter, ScanPage
from .models import Account, ActionBlocked, AuthRequired, PermanentError, RateLimited, TransientError
from .store import Store


@dataclass
class ScanSummary:
    content_type: str
    scan_id: int
    status: str  # complete | failed | running (partial)
    pages: int = 0  # pages fetched in this run
    items_seen: int = 0  # items returned in this run (duplicates included)
    new_items: int = 0
    marked_missing: int = 0
    partial: bool = False
    warning: str | None = None
    error: str | None = None
    stopped: bool = False  # should_stop() fired; the scan stays 'running' and resumes next time


class _ScanFailed(Exception):
    pass


class _StopRequested(Exception):
    """Internal: the caller's should_stop() returned True before a page request."""


def scan_account(
    store: Store,
    adapter: Adapter,
    account: Account,
    content_types: list[str] | None = None,
    *,
    fresh: bool = False,
    max_pages: int | None = None,
    sleep: Callable[[float], None] = time.sleep,
    max_retries: int = 3,
    max_rate_limit_waits: int = 5,
    max_wait: float = 300,
    progress: Callable[[str, int, int], None] | None = None,
    rng: random.Random | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> list[ScanSummary]:
    """Scan the given content types (all by default).

    ``should_stop`` is polled before every page request (and during waits). When it returns True the
    scan stops cleanly between pages: committed pages are kept, the scan row stays 'running' so the next
    scan resumes from the saved cursor, nothing is marked missing, and later content types are skipped.
    """
    types = list(content_types) if content_types else list(adapter.content_types)
    unknown = [t for t in types if t not in adapter.content_types]
    if unknown:
        raise ValueError(f"unknown content type(s) {unknown}; known: {', '.join(adapter.content_types)}")
    store.upsert_account(account, verified=False)  # FK target for scans/items
    out = []
    wait = _Waiter(sleep, should_stop)
    pacer = _Pacer(adapter.scan_delay, wait, rng or random.Random())
    for ctype in types:  # ActionBlocked/AuthRequired propagate, so remaining types are not scanned
        if should_stop and should_stop():
            break
        out.append(
            _scan_type(store, adapter, account, ctype, fresh, max_pages, wait, max_retries,
                       max_rate_limit_waits, max_wait, progress, pacer, should_stop)
        )
        if out[-1].stopped:
            break
    return out


class _Waiter:
    """sleep() that, when a stop callback exists, sleeps in <=1s slices so Stop stays responsive."""

    def __init__(self, sleep, should_stop):
        self.sleep, self.should_stop = sleep, should_stop

    def __call__(self, seconds: float) -> None:
        if self.should_stop is None:
            self.sleep(seconds)
            return
        remaining = seconds
        while remaining > 1e-9:
            if self.should_stop():
                raise _StopRequested
            chunk = min(1.0, remaining)
            self.sleep(chunk)
            remaining -= chunk
        if self.should_stop():
            raise _StopRequested


class _Pacer:
    """Jittered delay between consecutive scan page requests (never before the first)."""

    def __init__(self, delay: tuple[float, float], sleep, rng):
        self.delay, self.sleep, self.rng = delay, sleep, rng
        self.first = True

    def before_page(self) -> None:
        if not self.first and self.delay[1] > 0:
            self.sleep(self.rng.uniform(*self.delay))
        self.first = False


def _fetch(adapter, account, ctype, cursor, sleep, max_retries, max_rl, max_wait, pacer=None) -> ScanPage:
    """One page with bounded retries; raises _ScanFailed when budgets run out."""
    retries = rl_waits = 0
    if pacer:
        pacer.before_page()
    while True:
        try:
            return adapter.scan_page(account, ctype, cursor)
        except TransientError as e:
            if retries >= max_retries:
                raise _ScanFailed(f"giving up after {retries + 1} attempts: {e}") from e
            sleep(min(30.0, 2.0**retries))
            retries += 1
        except RateLimited as e:
            if rl_waits >= max_rl:
                raise _ScanFailed(f"rate limited too many times: {e}") from e
            rl_waits += 1
            base = 60 if e.retry_after is None else e.retry_after
            sleep(min(max_wait, base * 2 ** (rl_waits - 1)))  # escalate on consecutive limits


def _scan_type(store, adapter, account, ctype, fresh, max_pages, sleep, max_retries, max_rl, max_wait, progress,
               pacer=None, should_stop=None):
    scan = store.start_or_resume_scan(adapter.platform, account.account_id, ctype, fresh)
    summary = ScanSummary(ctype, scan.scan_id, "running")
    cursor = scan.cursor
    seen_cursors: set[str] = {cursor} if cursor is not None else set()
    try:
        while True:
            if should_stop and should_stop():
                raise _StopRequested
            page = _fetch(adapter, account, ctype, cursor, sleep, max_retries, max_rl, max_wait, pacer)
            for it in page.items:
                if (it.platform, it.account_id, it.content_type) != (adapter.platform, account.account_id, ctype):
                    msg = "adapter returned foreign item (wrong platform/account/type); scan aborted, nothing stored"
                    store.finish_scan(scan.scan_id, "failed", msg)
                    summary.status, summary.error = "failed", msg
                    raise PermanentError(msg)
            nxt = page.next_cursor
            stuck = nxt is not None and (nxt == cursor or nxt in seen_cursors)
            with store.tx():
                seen, new = store.upsert_items(page.items, scan.scan_id)
                store.record_scan_page(scan.scan_id, cursor if stuck else nxt, seen, new)
                if nxt is None:
                    store.finish_scan(scan.scan_id, "complete")
                    final = store.get_scan(scan.scan_id)
                    if final.items_seen > 0:
                        summary.marked_missing = store.mark_unseen_missing(final)
                elif stuck:
                    store.finish_scan(scan.scan_id, "failed", "pagination did not advance")
            summary.pages += 1
            summary.items_seen += seen
            summary.new_items += new
            if progress:
                progress(ctype, summary.pages, summary.items_seen)
            if nxt is None:
                summary.status = "complete"
                final = store.get_scan(scan.scan_id)
                if final.items_seen == 0:
                    summary.warning = (
                        "complete scan returned zero items — verify login/account; this may indicate "
                        "breakage (previously known items were NOT marked missing)"
                    )
                break
            if stuck:
                summary.status, summary.error = "failed", "pagination did not advance"
                break
            seen_cursors.add(nxt)
            cursor = nxt
            if max_pages is not None and summary.pages >= max_pages:
                summary.partial = True  # scan stays 'running' so the next scan resumes here
                break
    except _StopRequested:
        summary.partial = summary.stopped = True  # leave status 'running': resumable, nothing marked missing
    except _ScanFailed as e:
        store.finish_scan(scan.scan_id, "failed", str(e))
        summary.status, summary.error = "failed", str(e)
    except ActionBlocked as e:
        store.finish_scan(scan.scan_id, "failed", f"platform restricted the account: {e}")
        raise
    except AuthRequired as e:
        store.finish_scan(scan.scan_id, "failed", f"login required: {e}")
        raise
    except PermanentError as e:
        if summary.status != "failed":
            store.finish_scan(scan.scan_id, "failed", str(e))
        raise
    except Exception as e:
        store.finish_scan(scan.scan_id, "failed", f"{type(e).__name__}: {e}")
        raise
    return summary
