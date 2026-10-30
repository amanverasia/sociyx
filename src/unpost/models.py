"""Platform-neutral data types shared by the store, filters, executor and adapters."""

from __future__ import annotations

import enum
import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass(frozen=True)
class Account:
    """An authenticated account as reported by the platform itself."""

    platform: str
    account_id: str  # stable identifier (e.g. a DID), never a display name
    handle: str = ""  # human-readable, may change over time


@dataclass
class ContentItem:
    """One normalized piece of the user's content."""

    platform: str
    account_id: str
    item_id: str  # unique within (platform, account_id)
    content_type: str  # adapter-defined, e.g. "post", "like", "repost"
    created_at: datetime | None  # timezone-aware UTC, None if the platform doesn't say
    text: str = ""
    url: str = ""
    # Platform-specific opaque version marker (e.g. a record CID). Used to make
    # sure we delete exactly the version the user previewed.
    version: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    def fingerprint(self) -> str:
        """Hash of the identity and content the user reviewed in a preview."""
        payload = json.dumps(
            [self.platform, self.account_id, self.item_id, self.content_type, self.version, self.text],
            ensure_ascii=False,
        )
        return hashlib.sha256(payload.encode()).hexdigest()


class DeleteStatus(enum.StrEnum):
    DELETED = "deleted"  # platform accepted the delete request
    ALREADY_DELETED = "already_deleted"  # item was already gone before we asked
    INACCESSIBLE = "inaccessible"  # exists (or might) but we may not delete it
    CHANGED = "changed"  # item changed since the plan was made; not deleted


@dataclass
class DeleteResult:
    status: DeleteStatus
    detail: str = ""


class VerifyStatus(enum.StrEnum):
    GONE = "gone"
    PRESENT = "present"
    UNKNOWN = "unknown"  # platform gives us no reliable way to tell


class Outcome(enum.StrEnum):
    """Final per-item outcome of a deletion job, as reported to the user."""

    PENDING = "pending"  # not attempted yet
    IN_PROGRESS = "in_progress"  # attempt started; result unknown (crash recovery)
    DELETED_VERIFIED = "deleted_verified"
    DELETED_UNVERIFIED = "deleted_unverified"
    ALREADY_DELETED = "already_deleted"
    INACCESSIBLE = "inaccessible"
    FAILED = "failed"
    SKIPPED = "skipped"


TERMINAL_OUTCOMES = frozenset(Outcome) - {Outcome.PENDING, Outcome.IN_PROGRESS}


# ---- errors adapters raise so the executor can react uniformly ----


class AdapterError(Exception):
    """Base class for adapter failures."""


class AuthRequired(AdapterError):
    """Session missing/expired, or the platform presented a challenge (CAPTCHA,
    re-login, MFA). The executor stops; the user must resolve it manually."""


class RateLimited(AdapterError):
    def __init__(self, message: str = "rate limited", retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


DEFAULT_BLOCK_COOL_DOWN = 24 * 3600.0


def format_duration(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    if seconds >= 3600 and seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds >= 3600:
        return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"
    if seconds >= 60:
        return f"{seconds // 60}m"
    return f"{seconds}s"


class ActionBlocked(AdapterError):
    """The platform is restricting this account/action (e.g. Instagram FeedbackRequired or a
    challenge-like soft block). Stop now, wait a long time, and do NOT retry."""

    def __init__(self, message: str = "action blocked", cool_down: float | None = None):
        super().__init__(message)
        self.cool_down = cool_down

    def cool_down_seconds(self) -> float:
        return DEFAULT_BLOCK_COOL_DOWN if self.cool_down is None else self.cool_down

    def guidance(self) -> str:
        return (
            "Platform is restricting this account/action. Stop automation, resolve it manually in the "
            f"official app/site, and wait at least {format_duration(self.cool_down_seconds())} before resuming."
        )


class TransientError(AdapterError):
    """Network error, 5xx, timeout: worth a bounded retry."""


class PermanentError(AdapterError):
    """The request failed in a way retrying won't fix."""


def utc(dt: datetime) -> datetime:
    """Normalize to an aware UTC datetime (naive input is assumed to be UTC)."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def to_iso(dt: datetime | None) -> str | None:
    """Fixed-width UTC ISO format so lexical order == chronological order in SQLite."""
    if dt is None:
        return None
    return utc(dt).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def from_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    return utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
