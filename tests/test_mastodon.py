from __future__ import annotations

import json
import logging
import os
import stat
import threading
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from unpost.adapters import adapter_class
from unpost.adapters.mastodon import MastodonAdapter
from unpost.adapters.mastodon import oauth as moauth
from unpost.adapters.mastodon.client import html_to_text
from unpost.adapters.mastodon.session import Session, load_session, save_session, session_path
from unpost.models import (
    Account,
    AuthRequired,
    ContentItem,
    DeleteStatus,
    PermanentError,
    RateLimited,
    TransientError,
    VerifyStatus,
)

HOST = "mastodon.test"
ACCT = Account("mastodon", f"{HOST}:111", f"@me@{HOST}")
TOKEN = "tok-SECRET-123"


def status(sid, *, acct="111", content="<p>hi</p>", reblog=None, **kw):
    return {
        "id": sid, "created_at": "2024-03-01T10:00:00.000Z", "content": content, "spoiler_text": "",
        "url": f"https://{HOST}/@me/{sid}", "edited_at": None, "account": {"id": acct},
        "reblog": reblog, **kw,
    }


class Server:
    """Tiny programmable Mastodon. routes: (METHOD, path) -> callable(request) | response."""

    def __init__(self):
        self.routes = {}
        self.calls = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        h = self.routes.get((request.method, request.url.path))
        if h is None:
            return httpx.Response(404, json={"error": "Record not found"})
        return h(request) if callable(h) else h

    def paths(self):
        return [(c.method, c.url.path) for c in self.calls]


@pytest.fixture
def srv():
    return Server()


@pytest.fixture
def http(srv):
    with httpx.Client(transport=httpx.MockTransport(srv)) as c:
        yield c


def adapter(http, **kw):
    kw.setdefault("browser_launcher", lambda url: None)
    kw.setdefault("say", lambda s: None)
    return MastodonAdapter(http=http, **kw)


@pytest.fixture
def logged(srv, http):
    save_session(Session(HOST, ACCT.account_id, "me", "cid", "csecret", TOKEN))
    return adapter(http)


def item(ctype="post", sid="5", version="", **raw):
    iid = {"post": sid, "boost": sid, "favourite": f"fav:{sid}", "bookmark": f"bm:{sid}"}[ctype]
    return ContentItem("mastodon", ACCT.account_id, iid, ctype, None, version=version,
                       raw={"status_id": sid, **raw})


def link(next_id):
    return {"Link": f'<https://{HOST}/api/v1/x?max_id={next_id}>; rel="next", <https://{HOST}/api/v1/x?min_id=9>; rel="prev"'}


# ---------- registry / basics ----------

def test_registry_and_contract():
    assert adapter_class("mastodon") is MastodonAdapter
    assert MastodonAdapter.min_delete_interval == 60.0
    assert set(MastodonAdapter.content_types) == {"post", "boost", "favourite", "bookmark"}
    assert MastodonAdapter.deletable_types == frozenset(MastodonAdapter.content_types)


def test_html_to_text():
    html = '<p>Hello <a href="x">@bob</a> &amp; co<br>line2</p><p>second &lt;3</p>'
    assert html_to_text(html) == "Hello @bob & co\nline2\n\nsecond <3"
    assert html_to_text("") == ""


# ---------- login ----------

def oauth_server(srv, *, username="me", reject_loopback=False, reg_log=None):
    def apps(req):
        form = parse_qs(req.content.decode())
        uris = form["redirect_uris"][0].split("\n")
        if reg_log is not None:
            reg_log.append(uris)
        if reject_loopback and len(uris) > 1:
            return httpx.Response(422, json={"error": "Validation failed: Redirect URI must be an absolute URI"})
        assert form["scopes"][0].startswith("read:accounts")
        return httpx.Response(200, json={"client_id": "cid", "client_secret": "csecret"})

    def token(req):
        form = parse_qs(req.content.decode())
        assert form["grant_type"] == ["authorization_code"] and form["code_verifier"][0]
        assert form["code"] == ["the-code"]
        return httpx.Response(200, json={"access_token": TOKEN, "token_type": "Bearer"})

    srv.routes[("POST", "/api/v1/apps")] = apps
    srv.routes[("POST", "/oauth/token")] = token
    srv.routes[("POST", "/oauth/revoke")] = httpx.Response(200, json={})
    srv.routes[("GET", "/api/v1/accounts/verify_credentials")] = httpx.Response(
        200, json={"id": "111", "username": username, "acct": username, "url": "x"})


def loopback_launcher(state_override=None, code="the-code", capture=None):
    def launch(url):
        q = parse_qs(urlsplit(url).query)
        assert q["code_challenge_method"] == ["S256"] and q["response_type"] == ["code"]
        if capture is not None:
            capture.update(q)
        redirect = q["redirect_uri"][0]
        assert redirect.startswith("http://127.0.0.1:")
        state = state_override or q["state"][0]

        def go():
            httpx.get(redirect, params={"code": code, "state": state}, trust_env=False)

        threading.Thread(target=go).start()
        return None

    return launch


def test_loopback_login_and_app_cache(srv, http, caplog):
    regs = []
    oauth_server(srv, reg_log=regs)
    cap = {}
    a = adapter(http, browser_launcher=loopback_launcher(capture=cap))
    with caplog.at_level(logging.DEBUG):
        acct = a.login("@me@Mastodon.Test")
    assert acct == Account("mastodon", f"{HOST}:111", f"@me@{HOST}")
    assert len(regs) == 1 and len(regs[0]) == 2 and regs[0][1] == moauth.OOB
    # (the simulated browser's own httpx request log legitimately contains the code; check unpost's logs)
    ours = "\n".join(r.getMessage() for r in caplog.records if r.name.startswith("unpost"))
    assert "the-code" not in ours and TOKEN not in ours
    assert "the-code" not in repr(a.__dict__) and TOKEN not in str(srv.calls[-1].url)
    p = session_path(acct.account_id)
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(p.parent).st_mode) == 0o700
    assert load_session(acct.account_id).access_token == TOKEN
    assert TOKEN not in repr(load_session(acct.account_id))
    # app registration cached 0600; second login does not re-register
    apps = list((p.parent.parent / "apps").glob("*.json"))
    assert len(apps) == 1 and stat.S_IMODE(os.stat(apps[0]).st_mode) == 0o600
    adapter(http, browser_launcher=loopback_launcher()).login("mastodon.test")
    assert len(regs) == 1


def test_state_mismatch_rejected(srv, http):
    oauth_server(srv)
    a = adapter(http, browser_launcher=loopback_launcher(state_override="forged"))
    with pytest.raises(moauth.OAuthError, match="state mismatch"):
        a.login("@me@mastodon.test")
    assert ("POST", "/oauth/token") not in srv.paths()
    assert load_session(ACCT.account_id) is None


def test_oob_fallback_when_loopback_rejected(srv, http):
    regs = []
    oauth_server(srv, reject_loopback=True, reg_log=regs)
    prompts = []

    def secret(p):
        prompts.append(p)
        return " the-code \n"

    a = adapter(http, prompt_secret=secret)
    acct = a.login("@me@mastodon.test")
    assert acct.account_id == ACCT.account_id
    assert regs[-1] == [moauth.OOB] and len(regs[0]) == 2
    assert len(prompts) == 1
    tok = [c for c in srv.calls if c.url.path == "/oauth/token"][0]
    assert parse_qs(tok.content.decode())["redirect_uri"] == [moauth.OOB]


def test_oob_method_explicit(srv, http):
    oauth_server(srv)
    a = adapter(http, prompt_secret=lambda p: "the-code")
    assert a.login("mastodon.test", method="oob").account_id == ACCT.account_id


def test_username_mismatch_refused_and_token_revoked(srv, http):
    oauth_server(srv, username="someoneelse")
    a = adapter(http, browser_launcher=loopback_launcher())
    with pytest.raises(PermanentError, match="login rejected"):
        a.login("@me@mastodon.test")
    assert ("POST", "/oauth/revoke") in srv.paths()
    assert load_session("mastodon.test:111") is None


def test_login_prompts_for_instance(srv, http):
    oauth_server(srv)
    a = adapter(http, browser_launcher=loopback_launcher(), prompt_text=lambda p: "https://mastodon.test/")
    assert a.login().handle == f"@me@{HOST}"


def test_bad_method_and_hint(http):
    a = adapter(http)
    with pytest.raises(PermanentError):
        a.login("mastodon.test", method="password")
    with pytest.raises(PermanentError):
        a.login("not a host!")


def test_current_account(srv, logged):
    srv.routes[("GET", "/api/v1/accounts/verify_credentials")] = httpx.Response(200, json={"id": "111", "username": "me"})
    assert logged.current_account(ACCT.account_id).handle == f"@me@{HOST}"
    assert srv.calls[-1].headers["Authorization"] == f"Bearer {TOKEN}"
    srv.routes[("GET", "/api/v1/accounts/verify_credentials")] = httpx.Response(401, json={"error": "The access token is invalid"})
    with pytest.raises(AuthRequired):
        logged.current_account(ACCT.account_id)


def test_current_account_without_session(http):
    with pytest.raises(AuthRequired):
        adapter(http).current_account("nowhere.test:1")


def test_logout_revokes_and_deletes(srv, logged):
    srv.routes[("POST", "/oauth/revoke")] = httpx.Response(200, json={})
    logged.logout(ACCT.account_id)
    assert ("POST", "/oauth/revoke") in srv.paths()
    assert not session_path(ACCT.account_id).exists()


def test_logout_survives_revoke_failure(srv, logged):
    srv.routes[("POST", "/oauth/revoke")] = httpx.Response(500)
    logged.logout(ACCT.account_id)
    assert not session_path(ACCT.account_id).exists()


# ---------- scanning ----------

def test_statuses_pagination_three_pages_and_post_boost_split(srv, logged):
    boosted = status("900", acct="777")
    pages = {
        None: ([status("30"), status("29", reblog=boosted, content="")], link("29")),
        "29": ([status("28"), status("27")], link("27")),
        "27": ([status("26")], {"Link": f'<https://{HOST}/api/v1/x?min_id=26>; rel="prev"'}),  # no rel=next: done
    }

    def h(req):
        assert req.url.params["limit"] == "40"
        body, hdr = pages[req.url.params.get("max_id")]
        return httpx.Response(200, json=body, headers=hdr)

    srv.routes[("GET", "/api/v1/accounts/111/statuses")] = h
    posts, boosts, cur = [], [], None
    while True:
        p = logged.scan_page(ACCT, "post", cur)
        posts += p.items
        cur = p.next_cursor
        if cur is None:
            break
    assert [i.item_id for i in posts] == ["30", "28", "27", "26"]
    cur = None
    p = logged.scan_page(ACCT, "boost", None)
    boosts = p.items
    assert [(i.item_id, i.raw["original_id"]) for i in boosts] == [("29", "900")]
    assert boosts[0].content_type == "boost"
    assert posts[0].created_at == datetime(2024, 3, 1, 10, tzinfo=timezone.utc)
    assert posts[0].text == "hi" and posts[0].url.endswith("/30")


def test_statuses_empty_page_terminates(srv, logged):
    srv.routes[("GET", "/api/v1/accounts/111/statuses")] = httpx.Response(200, json=[])
    p = logged.scan_page(ACCT, "post", "26")
    assert p.items == [] and p.next_cursor is None


def test_statuses_fallback_cursor_without_link(srv, logged):
    srv.routes[("GET", "/api/v1/accounts/111/statuses")] = httpx.Response(200, json=[status("9"), status("8")])
    assert logged.scan_page(ACCT, "post", None).next_cursor == "8"


def test_favourites_link_only_cursor(srv, logged):
    # internal pagination ids (999, 888) differ from status ids
    def h(req):
        mid = req.url.params.get("max_id")
        if mid is None:
            return httpx.Response(200, json=[status("5", acct="222"), status("4", acct="333")], headers=link("999"))
        assert mid == "999"
        return httpx.Response(200, json=[status("3", acct="222")])

    srv.routes[("GET", "/api/v1/favourites")] = h
    p1 = logged.scan_page(ACCT, "favourite", None)
    assert p1.next_cursor == "999"
    assert [i.item_id for i in p1.items] == ["fav:5", "fav:4"]  # foreign authors are fine for favourites
    p2 = logged.scan_page(ACCT, "favourite", "999")
    assert p2.next_cursor is None and p2.items[0].item_id == "fav:3"
    assert srv.calls[-1].headers["Authorization"] == f"Bearer {TOKEN}"


def test_bookmarks_ids_distinct_from_posts(srv, logged):
    srv.routes[("GET", "/api/v1/bookmarks")] = httpx.Response(200, json=[status("5", acct="2")], headers=link("77"))
    p = logged.scan_page(ACCT, "bookmark", None)
    assert p.items[0].item_id == "bm:5" and p.items[0].raw["status_id"] == "5"


def test_foreign_status_in_own_listing_rejected(srv, logged):
    srv.routes[("GET", "/api/v1/accounts/111/statuses")] = httpx.Response(200, json=[status("1", acct="999")])
    with pytest.raises(PermanentError, match="different account"):
        logged.scan_page(ACCT, "post", None)


def test_spoiler_text_included(srv, logged):
    st = status("1", spoiler_text="CW")
    srv.routes[("GET", "/api/v1/accounts/111/statuses")] = httpx.Response(200, json=[st])
    assert logged.scan_page(ACCT, "post", None).items[0].text == "CW\n\nhi"


def test_version_is_edited_at(srv, logged):
    st = status("1", edited_at="2024-03-02T00:00:00.000Z")
    srv.routes[("GET", "/api/v1/accounts/111/statuses")] = httpx.Response(200, json=[st])
    assert logged.scan_page(ACCT, "post", None).items[0].version == "2024-03-02T00:00:00.000Z"


def test_public_posts_scan_without_session(srv, http):
    srv.routes[("GET", "/api/v1/accounts/111/statuses")] = httpx.Response(200, json=[status("1")])
    a = adapter(http)
    assert len(a.scan_page(ACCT, "post", None).items) == 1
    assert "Authorization" not in srv.calls[-1].headers
    with pytest.raises(AuthRequired):
        a.scan_page(ACCT, "favourite", None)


# ---------- errors ----------

def test_429_uses_ratelimit_reset(srv, logged):
    reset = (datetime.now(timezone.utc) + timedelta(seconds=120)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    srv.routes[("GET", "/api/v1/favourites")] = httpx.Response(
        429, headers={"X-RateLimit-Limit": "300", "X-RateLimit-Remaining": "0", "X-RateLimit-Reset": reset})
    with pytest.raises(RateLimited) as e:
        logged.scan_page(ACCT, "favourite", None)
    assert 100 < e.value.retry_after <= 120


def test_429_retry_after_header(srv, logged):
    srv.routes[("GET", "/api/v1/favourites")] = httpx.Response(429, headers={"Retry-After": "17"})
    with pytest.raises(RateLimited) as e:
        logged.scan_page(ACCT, "favourite", None)
    assert e.value.retry_after == 17


def test_5xx_transient_and_network_error(srv, logged):
    srv.routes[("GET", "/api/v1/favourites")] = httpx.Response(503)
    with pytest.raises(TransientError):
        logged.scan_page(ACCT, "favourite", None)

    def boom(req):
        raise httpx.ConnectTimeout("t")

    srv.routes[("GET", "/api/v1/favourites")] = boom
    with pytest.raises(TransientError):
        logged.scan_page(ACCT, "favourite", None)


def test_401_auth_required(srv, logged):
    srv.routes[("GET", "/api/v1/favourites")] = httpx.Response(401, json={"error": "invalid token"})
    with pytest.raises(AuthRequired):
        logged.scan_page(ACCT, "favourite", None)


# ---------- delete ----------

def test_delete_post_deleted(srv, logged):
    srv.routes[("GET", "/api/v1/statuses/5")] = httpx.Response(200, json=status("5"))
    srv.routes[("DELETE", "/api/v1/statuses/5")] = httpx.Response(200, json=status("5"))
    assert logged.delete(item()).status is DeleteStatus.DELETED
    assert srv.paths() == [("GET", "/api/v1/statuses/5"), ("DELETE", "/api/v1/statuses/5")]


def test_delete_post_already_deleted_via_get(srv, logged):
    assert logged.delete(item()).status is DeleteStatus.ALREADY_DELETED
    assert ("DELETE", "/api/v1/statuses/5") not in srv.paths()


def test_delete_post_404_race_then_gone(srv, logged):
    gets = iter([httpx.Response(200, json=status("5")), httpx.Response(404, json={"error": "x"})])
    srv.routes[("GET", "/api/v1/statuses/5")] = lambda r: next(gets)
    assert logged.delete(item()).status is DeleteStatus.ALREADY_DELETED


def test_delete_post_404_but_exists_is_inaccessible(srv, logged):
    srv.routes[("GET", "/api/v1/statuses/5")] = httpx.Response(200, json=status("5"))
    assert logged.delete(item()).status is DeleteStatus.INACCESSIBLE  # DELETE -> 404


def test_delete_post_foreign_is_inaccessible_without_delete(srv, logged):
    srv.routes[("GET", "/api/v1/statuses/5")] = httpx.Response(200, json=status("5", acct="999"))
    assert logged.delete(item()).status is DeleteStatus.INACCESSIBLE
    assert ("DELETE", "/api/v1/statuses/5") not in srv.paths()


def test_delete_post_changed(srv, logged):
    srv.routes[("GET", "/api/v1/statuses/5")] = httpx.Response(200, json=status("5", edited_at="2024-04-01T00:00:00.000Z"))
    assert logged.delete(item(version="")).status is DeleteStatus.CHANGED
    assert logged.delete(item(version="2024-04-01T00:00:00.000Z")).status is DeleteStatus.INACCESSIBLE  # passes check, DELETE 404s
    assert [m for m, _ in srv.paths()].count("DELETE") == 1


def test_delete_requires_session(http):
    with pytest.raises(AuthRequired):
        adapter(http).delete(item())


@pytest.mark.parametrize(
    "ctype,endpoint,flag,sid",
    [("boost", "unreblog", "reblogged", "900"), ("favourite", "unfavourite", "favourited", "5"),
     ("bookmark", "unbookmark", "bookmarked", "5")],
)
def test_undo_endpoints_and_ids(srv, logged, ctype, endpoint, flag, sid):
    srv.routes[("POST", f"/api/v1/statuses/{sid}/{endpoint}")] = httpx.Response(200, json=status(sid, **{flag: False}))
    it = item(ctype, "29" if ctype == "boost" else "5", original_id="900")
    assert logged.delete(it).status is DeleteStatus.DELETED
    assert srv.paths() == [("POST", f"/api/v1/statuses/{sid}/{endpoint}")]  # boost: ORIGINAL id, no GET


@pytest.mark.parametrize("ctype,endpoint", [("boost", "unreblog"), ("favourite", "unfavourite"), ("bookmark", "unbookmark")])
def test_undo_404_already_deleted(srv, logged, ctype, endpoint):
    r = logged.delete(item(ctype, "5", original_id="5"))
    assert r.status is DeleteStatus.ALREADY_DELETED and "original status" in r.detail


def test_undo_flag_still_true_is_error(srv, logged):
    srv.routes[("POST", "/api/v1/statuses/5/unfavourite")] = httpx.Response(200, json=status("5", favourited=True))
    with pytest.raises(PermanentError):
        logged.delete(item("favourite"))


def test_delete_429(srv, logged):
    srv.routes[("GET", "/api/v1/statuses/5")] = httpx.Response(200, json=status("5"))
    srv.routes[("DELETE", "/api/v1/statuses/5")] = httpx.Response(429, headers={"Retry-After": "30"})
    with pytest.raises(RateLimited):
        logged.delete(item())


# ---------- verify ----------

def test_verify_post(srv, logged):
    assert logged.verify_deleted(item()) is VerifyStatus.GONE  # 404
    srv.routes[("GET", "/api/v1/statuses/5")] = httpx.Response(200, json=status("5"))
    assert logged.verify_deleted(item()) is VerifyStatus.PRESENT


@pytest.mark.parametrize("ctype,flag", [("boost", "reblogged"), ("favourite", "favourited"), ("bookmark", "bookmarked")])
def test_verify_flags(srv, logged, ctype, flag):
    sid = "900" if ctype == "boost" else "5"
    it = item(ctype, "29" if ctype == "boost" else "5", original_id="900")
    assert logged.verify_deleted(it) is VerifyStatus.GONE  # status itself 404
    srv.routes[("GET", f"/api/v1/statuses/{sid}")] = httpx.Response(200, json=status(sid, **{flag: False}))
    assert logged.verify_deleted(it) is VerifyStatus.GONE
    srv.routes[("GET", f"/api/v1/statuses/{sid}")] = httpx.Response(200, json=status(sid, **{flag: True}))
    assert logged.verify_deleted(it) is VerifyStatus.PRESENT


@pytest.mark.parametrize("resp", [httpx.Response(500), httpx.Response(429), httpx.Response(401)])
def test_verify_errors_unknown(srv, logged, resp):
    srv.routes[("GET", "/api/v1/statuses/5")] = resp
    assert logged.verify_deleted(item()) is VerifyStatus.UNKNOWN
    assert logged.verify_deleted(item("favourite")) is VerifyStatus.UNKNOWN


def test_verify_missing_flag_unknown(srv, logged):
    srv.routes[("GET", "/api/v1/statuses/5")] = httpx.Response(200, json=status("5"))  # no flag (unauthenticated view)
    assert logged.verify_deleted(item("favourite")) is VerifyStatus.UNKNOWN
