"""Read-only live checks against real Bluesky servers. Run with UNPOST_LIVE_TESTS=1.

No login, no writes, no deletes. The PAR test creates a single-use request_uri that
expires unused; it touches no account.
"""

from __future__ import annotations

import os

import httpx
import pytest

from unpost.adapters.bluesky import BlueskyAdapter, dpop
from unpost.adapters.bluesky.identity import resolve_identity
from unpost.adapters.bluesky.oauth import SCOPE, build_client_id, discover, push_authorization_request
from unpost.models import Account, ContentItem, VerifyStatus

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.environ.get("UNPOST_LIVE_TESTS") != "1", reason="set UNPOST_LIVE_TESTS=1"),
]


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("UNPOST_HOME", str(tmp_path))


@pytest.fixture
def http():
    with httpx.Client(timeout=30, trust_env=False) as c:
        yield c


@pytest.fixture
def ident(http):
    return resolve_identity("bsky.app", http)


def test_resolve_bsky_app(ident):
    assert ident.did.startswith("did:plc:")
    assert ident.pds_url.startswith("https://")
    assert ident.handle == "bsky.app"


def test_scan_public_account_two_pages(http, ident):
    a = BlueskyAdapter(http=http)
    acct = Account("bluesky", ident.did, ident.handle)
    p1 = a.scan_page(acct, "post", None)
    assert len(p1.items) > 0 and p1.next_cursor
    p2 = a.scan_page(acct, "post", p1.next_cursor)
    assert p2.items and p2.next_cursor != p1.next_cursor
    ids = [i.item_id for i in p1.items + p2.items]
    assert len(set(ids)) == len(ids)
    for i in p1.items[:5]:
        assert i.item_id.startswith(f"at://{ident.did}/app.bsky.feed.post/")
        assert i.version and i.url.startswith("https://bsky.app/profile/")
        assert i.created_at is None or i.created_at.tzinfo is not None
        assert i.raw["value"].get("text", "") == i.text


def test_verify_nonexistent_record_is_gone(http, ident):
    a = BlueskyAdapter(http=http)
    ghost = ContentItem("bluesky", ident.did, f"at://{ident.did}/app.bsky.feed.post/3zzzzzzzzzzzz",
                        "post", None, version="x")
    assert a.verify_deleted(ghost) == VerifyStatus.GONE


def test_oauth_discovery(http, ident):
    meta = discover(ident.pds_url, http)
    assert meta.par_endpoint.startswith("https://") and meta.token_endpoint.startswith("https://")
    assert "ES256" in meta.dpop_algs


def test_par_request_returns_request_uri(http, ident):
    meta = discover(ident.pds_url, http)
    redirect = "http://127.0.0.1:53682/callback"
    key = dpop.generate_key()
    nonces: dict[str, str] = {}
    uri = push_authorization_request(
        http, meta, client_id=build_client_id(redirect), redirect_uri=redirect, scope=SCOPE,
        state=dpop.new_state(), challenge=dpop.code_challenge(dpop.new_code_verifier()),
        login_hint=None, key=key, nonces=nonces)
    assert uri.startswith("urn:ietf:params:oauth:request_uri:")
    print("nonces learned for:", list(nonces))
