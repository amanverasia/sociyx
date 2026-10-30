"""Single place that maps instagrapi exceptions onto sociyx's error contract."""

from __future__ import annotations

import contextlib
from collections.abc import Iterator

from ...models import (
    ActionBlocked,
    AdapterError,
    AuthRequired,
    PermanentError,
    RateLimited,
    TransientError,
)
from ...redact import scrub

RESOLVE_MANUALLY = "resolve it in the official Instagram app or website, then log in again; do not retry automatically"
THROTTLE_RETRY_AFTER = 900.0
BLOCK_COOL_DOWN = 86400.0


class Gone(PermanentError):
    """The target (media, comment, thread, ...) no longer exists on Instagram."""


def _msg(exc: BaseException, limit: int = 200) -> str:
    return scrub(str(getattr(exc, "message", "") or exc))[:limit]


def _feedback(exc: BaseException) -> str:
    for key in ("feedback_message", "feedback_title", "error_title"):
        val = getattr(exc, key, None)
        if val:
            return scrub(str(val))[:200]
    return _msg(exc)


def translate(exc: BaseException) -> AdapterError | None:
    """Return the sociyx error for an instagrapi exception, or None if it is not one."""
    from instagrapi import exceptions as ig

    if isinstance(exc, AdapterError):
        return exc
    gone = (
        ig.MediaNotFound, ig.StoryNotFound, ig.InvalidMediaId, ig.MediaUnavailable, ig.CommentNotFound,
        ig.DirectThreadNotFound, ig.DirectMessageNotFound, ig.CollectionNotFound,
    )
    if isinstance(exc, gone):
        return Gone(f"no longer exists on Instagram ({type(exc).__name__})")
    if isinstance(exc, (ig.FeedbackRequired, ig.SentryBlock, ig.ProxyAddressIsBlocked)):
        return ActionBlocked(
            f"Instagram restricted this action ({type(exc).__name__}): {_feedback(exc)}", cool_down=BLOCK_COOL_DOWN
        )
    auth = (
        ig.LoginRequired, ig.ClientLoginRequired, ig.ChallengeError, ig.AccountSuspended,
        ig.CaptchaChallengeRequired, ig.ReloginAttemptExceeded, ig.TwoFactorRequired,
        ig.BadPassword, ig.BadCredentials, ig.RecaptchaChallengeForm,
    )
    if isinstance(exc, auth):
        return AuthRequired(f"Instagram needs you to sign in or verify ({type(exc).__name__}): {RESOLVE_MANUALLY}")
    if isinstance(exc, (ig.PleaseWaitFewMinutes, ig.RateLimitError, ig.ClientThrottledError)):
        return RateLimited(f"Instagram asked us to slow down ({type(exc).__name__})", retry_after=THROTTLE_RETRY_AFTER)
    transient = (ig.ClientConnectionError, ig.ClientRequestTimeout, ig.ClientIncompleteReadError, ig.ClientJSONDecodeError)
    if isinstance(exc, transient):
        return TransientError(f"network problem talking to Instagram ({type(exc).__name__})")
    if isinstance(exc, ig.ClientError):
        return PermanentError(f"Instagram request failed ({type(exc).__name__}): {_msg(exc)}")
    if isinstance(exc, ig.ValidationError):
        return PermanentError(f"unexpected Instagram response shape: {_msg(exc)}")
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return TransientError(f"network problem talking to Instagram ({type(exc).__name__})")
    if isinstance(exc, AssertionError) and "Login required" in str(exc):
        return AuthRequired(f"no valid Instagram session: {RESOLVE_MANUALLY}")
    return None


@contextlib.contextmanager
def map_exceptions() -> Iterator[None]:
    try:
        yield
    except Exception as exc:  # noqa: BLE001
        mapped = translate(exc)
        if mapped is None:
            raise
        if mapped is exc:
            raise
        raise mapped from None
