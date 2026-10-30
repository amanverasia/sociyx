"""Read-only live checks against mastodon.social. Run with UNPOST_LIVE_TESTS=1.

No app registration, no login, no writes. Only unauthenticated public GETs.
"""

from __future__ import annotations

import os

import httpx
import pytest

from unpost.adapters.mastodon import MastodonAdapter
from unpost.adapters.mastodon.client import next_max_id
from unpost.models import Account, AuthRequired, VerifyStatus

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.environ.get("UNPOST_LIVE_TESTS") != "1", reason="set UNPOST_LIVE_TESTS=1"),
]

HOST = "mastodon.social"
NOPE = "999999999999999999"  # far beyond any issued snowflake id; (id "1" is a real 2016 post)
SEEN: list[httpx.Response] = []


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("UNPOST_HOME", str(tmp_path))


@pytest.fixture
def http():
    with httpx.Client(timeout=30, trust_env=False, event_hooks={"response": [SEEN.append]}) as c:
        yield c


@pytest.fixture
def acct(http):
    r = http.get(f"https://{HOST}/api/v1/accounts/lookup", params={"acct": "Mastodon"})
    if r.status_code in (401, 403):
        pytest.xfail(f"account lookup requires auth on {HOST}: HTTP {r.status_code}")
    assert r.status_code == 200
    print("lookup headers:", {k: v for k, v in r.headers.items() if k.lower().startswith(("x-ratelimit", "link", "server"))})
    body = r.json()
    return Account("mastodon", f"{HOST}:{body['id']}", f"@{body['username']}@{HOST}")


def test_public_statuses_two_pages(http, acct):
    a = MastodonAdapter(http=http)
    try:
        p1 = a.scan_page(acct, "post", None)
    except AuthRequired:
        pytest.xfail(f"{HOST} requires auth for account statuses (HTTP 401)")
    last = SEEN[-1]
    print("statuses status:", last.status_code)
    print("Link:", last.headers.get("link"))
    print("rate-limit:", {k: v for k, v in last.headers.items() if k.lower().startswith("x-ratelimit")})
    assert p1.items and p1.next_cursor, "expected items and a next cursor"
    assert next_max_id(last) == p1.next_cursor
    p2 = a.scan_page(acct, "post", p1.next_cursor)
    assert p2.items
    ids = [i.item_id for i in p1.items + p2.items]
    assert len(ids) == len(set(ids))
    for i in p1.items + p2.items:
        assert i.account_id == acct.account_id and i.content_type == "post"
        assert i.created_at is not None and i.created_at.tzinfo is not None
        assert i.url.startswith("https://")
    assert any(i.text for i in p1.items)
    # newest-first ordering across pages
    assert p1.items[-1].created_at >= p2.items[0].created_at


def test_nonexistent_status_is_gone(http, acct):
    from unpost.models import ContentItem

    a = MastodonAdapter(http=http)
    it = ContentItem("mastodon", acct.account_id, NOPE, "post", None, raw={"status_id": NOPE})
    r = http.get(f"https://{HOST}/api/v1/statuses/{NOPE}")
    print("nonexistent status:", r.status_code, r.text[:100])
    assert r.status_code == 404
    assert a.verify_deleted(it) is VerifyStatus.GONE
