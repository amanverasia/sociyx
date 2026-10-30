"""Visible-browser login: the USER logs in (and does 2FA / challenges) themselves.

We never type into the page and never touch CAPTCHAs; we only poll the browser's cookie jar
until a `sessionid` appears. (loopback.open_visible_browser exposes no cookie access, so this
module drives Playwright directly.)
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from ...models import AuthRequired, PermanentError
from ...paths import private_dir

LOGIN_URL = "https://www.instagram.com/accounts/login/"
TIMEOUT = 600.0
POLL_SECONDS = 2.0


class BrowserRunner(Protocol):
    def __call__(self, profile_dir: Path, url: str, timeout: float) -> dict[str, str]: ...


def cookies_from(jar: list[dict[str, Any]]) -> dict[str, str]:
    """Reduce a Playwright cookie list to the instagram.com cookies we care about."""
    out: dict[str, str] = {}
    for c in jar:
        domain = str(c.get("domain", ""))
        if domain.lstrip(".").endswith("instagram.com") and c.get("name") in ("sessionid", "ds_user_id"):
            if c.get("value"):
                out[c["name"]] = str(c["value"])
    return out


def wait_for_session(
    get_cookies: Callable[[], list[dict[str, Any]]],
    timeout: float = TIMEOUT,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
    poll: float = POLL_SECONDS,
) -> dict[str, str]:
    deadline = now() + timeout
    while True:
        found = cookies_from(get_cookies())
        if "sessionid" in found:
            return found
        if now() >= deadline:
            raise AuthRequired("timed out waiting for you to finish logging in to Instagram in the browser")
        sleep(poll)


def playwright_runner(profile_dir: Path, url: str, timeout: float) -> dict[str, str]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:  # pragma: no cover
        raise PermanentError("Playwright is not installed") from e
    pw = sync_playwright().start()
    ctx = None
    try:
        ctx = pw.chromium.launch_persistent_context(str(private_dir(profile_dir)), headless=False)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(url)
        print("Log in to Instagram in the browser window (including any 2FA or challenge).")
        print("This window is yours: sociyx will not type anything. Press Ctrl+C to cancel.")
        return wait_for_session(lambda: ctx.cookies(["https://www.instagram.com"]), timeout)
    finally:
        for fn in ((ctx.close if ctx else None), pw.stop):
            try:
                if fn:
                    fn()
            except Exception:  # noqa: BLE001
                pass
