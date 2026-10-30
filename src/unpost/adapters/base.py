"""The contract every platform adapter implements.

Adapters do platform I/O only. Filtering, export, plans, job state, retries,
pacing and reporting are shared code and must not be reimplemented here.

Error contract (see unpost.models):
  AuthRequired    session missing/expired or a challenge (CAPTCHA, re-login) -> executor stops
  RateLimited     platform throttled us; carries retry_after seconds if known
  TransientError  network/5xx/timeouts; executor retries a bounded number of times
  PermanentError  anything retrying won't fix
  ActionBlocked   platform is restricting the account/action (soft block); executor stops and
                  enforces a long cool-down; never retried
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import ClassVar

from ..models import Account, ContentItem, DeleteResult, VerifyStatus


@dataclass
class ScanPage:
    items: list[ContentItem]
    next_cursor: str | None  # None means the listing is exhausted


class Adapter(ABC):
    platform: ClassVar[str]  # registry key, e.g. "bluesky"
    label: ClassVar[str]  # human-readable name
    content_types: ClassVar[dict[str, str]]  # content type -> description; one scan unit each
    deletable_types: ClassVar[frozenset[str]]
    discovery_notes: ClassVar[str]  # what history the platform does/doesn't expose
    min_delete_interval: ClassVar[float] = 1.0  # default pacing between deletes, seconds
    # --- slow-platform pacing profile (defaults keep the plain behaviour) ---
    # Each pacing interval is min_delete_interval * uniform(1, 1 + pacing_jitter).
    pacing_jitter: ClassVar[float] = 0.0
    # Max delete requests (every attempt counts) per rolling 24h per account across ALL plans/jobs.
    daily_write_cap: ClassVar[int | None] = None
    # After every `think_pause_every` delete requests in a run, sleep uniform(*think_pause_seconds).
    think_pause_every: ClassVar[int] = 0
    think_pause_seconds: ClassVar[tuple[float, float]] = (0.0, 0.0)
    # Jittered seconds between scan page requests. scanning.py applies this generically between
    # consecutive scan_page calls (never before the first); adapters must NOT sleep for it themselves.
    scan_delay: ClassVar[tuple[float, float]] = (0.0, 0.0)

    @abstractmethod
    def login(self, account_hint: str | None = None, method: str | None = None) -> Account:
        """Interactive login (visible browser where possible). Persists the session
        locally under paths.secrets_dir(platform) and returns the verified account."""

    @abstractmethod
    def current_account(self, account_id: str) -> Account:
        """Ask the platform who the stored session for account_id belongs to.
        Raises AuthRequired if there is no valid session."""

    @abstractmethod
    def scan_page(self, account: Account, content_type: str, cursor: str | None) -> ScanPage:
        """Fetch one page of the account's own content of one type.
        Every returned item must have platform/account_id equal to the account's."""

    @abstractmethod
    def delete(self, item: ContentItem) -> DeleteResult:
        """Delete exactly this item (and only if it still matches item.version when the
        platform supports that). Must distinguish already-deleted and inaccessible."""

    def verify_deleted(self, item: ContentItem) -> VerifyStatus:
        """Check whether the item is gone. Return UNKNOWN when the platform offers no
        reliable check; the executor then labels the deletion unverified."""
        return VerifyStatus.UNKNOWN

    def logout(self, account_id: str) -> None:
        """Remove locally stored session material for account_id."""

    def close(self) -> None:
        """Release network clients / browsers."""
