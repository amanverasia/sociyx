"""Mastodon (Mastodon API) adapter: posts, boosts, favourites and bookmarks."""

from __future__ import annotations

import getpass
import logging
import re
from collections.abc import Callable
from typing import Any, ClassVar
from urllib.parse import urlsplit

import httpx

from ...models import (
    Account,
    AdapterError,
    AuthRequired,
    ContentItem,
    DeleteResult,
    DeleteStatus,
    PermanentError,
    VerifyStatus,
)
from ..base import Adapter, ScanPage
from . import client, oauth
from .mapping import local_id, to_item
from .session import Session, delete_session, load_session, save_session

log = logging.getLogger(__name__)

PAGE_LIMIT = 40
_HOST = re.compile(r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?(:\d{1,5})?$")
# content type -> (endpoint action, status flag that must become false)
_UNDO = {
    "boost": ("unreblog", "reblogged"),
    "favourite": ("unfavourite", "favourited"),
    "bookmark": ("unbookmark", "bookmarked"),
}


def parse_hint(hint: str) -> tuple[str | None, str]:
    """'@user@host' | 'user@host' | 'host' | 'https://host/...' -> (username|None, host)."""
    h = hint.strip()
    if "://" in h:
        user = None
        host = urlsplit(h).netloc
    else:
        h = h.lstrip("@")
        user, _, host = h.rpartition("@")
        user = user or None
        host = host.split("/")[0]
    host = host.lower()
    if not _HOST.match(host):
        raise PermanentError("could not understand the Mastodon instance; use @user@instance.example")
    return user, host


def _host_of(account_id: str) -> str:
    return account_id.rsplit(":", 1)[0]


def _base(host: str) -> str:
    return f"https://{host}"


class MastodonAdapter(Adapter):
    platform: ClassVar[str] = "mastodon"
    label: ClassVar[str] = "Mastodon (and compatible ActivityPub servers implementing the Mastodon API)"
    content_types: ClassVar[dict[str, str]] = {
        "post": "your own posts, including replies (not boosts)",
        "boost": "your boosts of other people's posts",
        "favourite": "posts you favourited",
        "bookmark": "posts you bookmarked",
    }
    deletable_types: ClassVar[frozenset[str]] = frozenset(content_types)
    # Default limit: DELETE status and unreblog are 30 per 30 minutes per account, i.e. about
    # 60/hour. One per 60 s stays under it; 10,000 posts therefore take roughly a week.
    # Unfavourite/unbookmark fall under the general 300 requests / 5 minutes budget, so
    # they are paced more slowly than strictly necessary.
    min_delete_interval: ClassVar[float] = 60.0
    discovery_notes: ClassVar[str] = (
        "Pages your statuses on your HOME instance (GET /api/v1/accounts/:id/statuses, 40 per\n"
        "page, Link-header pagination). The docs state no history cap; whether every old post is\n"
        "returned in practice is UNVERIFIED, and instances may customize limits.\n"
        "Favourites and bookmarks are listed via Link-header pagination only. The API gives the\n"
        "date of the STATUS, not of when you favourited/bookmarked it, so date filters apply to\n"
        "the status date for those types.\n"
        "Deleting is slow by design: ~60 deletes/hour (server limit 30 per 30 minutes), so 10,000\n"
        "posts take about a week.\n"
        "NOT covered:\n"
        "  - direct messages: they may or may not be listed depending on server version; treat as\n"
        "    not covered\n"
        "  - media attachments: the delete_media option (Mastodon 4.4+) is not used\n"
        "  - poll votes, follows, lists, filters, profile fields\n"
        "  - federated copies: other servers receive a Delete activity, but they may keep copies;\n"
        "    this cannot be verified from your instance\n"
        "Unreblog on a post you did not boost succeeds server-side, so such an item may be\n"
        "reported as deleted instead of already deleted."
    )

    def __init__(
        self,
        http: httpx.Client | None = None,
        browser_launcher: oauth.BrowserLauncher | None = None,
        prompt_text: Callable[[str], str] = input,
        prompt_secret: Callable[[str], str] = getpass.getpass,
        say: Callable[[str], None] = print,
    ) -> None:
        self._owns_http = http is None
        self._http = http or httpx.Client(timeout=client.TIMEOUT, headers={"User-Agent": client.USER_AGENT})
        self._browser_launcher = browser_launcher or oauth.default_browser_launcher
        self._prompt_text = prompt_text
        self._prompt_secret = prompt_secret
        self._say = say
        self._sessions: dict[str, Session] = {}

    # ---- low-level ----

    def _call(self, method: str, account_id: str, path: str, token: str | None, **kw: Any) -> httpx.Response:
        return client.request(self._http, method, _base(_host_of(account_id)), path, token, **kw)

    def _session(self, account_id: str) -> Session:
        s = self._sessions.get(account_id) or load_session(account_id)
        if s is None:
            raise AuthRequired("no Mastodon session for this account; run `sociyx login mastodon`")
        self._sessions[account_id] = s
        return s

    def _token(self, account_id: str, required: bool) -> str | None:
        if required:
            return self._session(account_id).access_token
        s = self._sessions.get(account_id) or load_session(account_id)
        return s.access_token if s else None

    def _get_status(self, account_id: str, sid: str, token: str | None) -> dict[str, Any] | None:
        resp = self._call("GET", account_id, f"/api/v1/statuses/{sid}", token)
        if resp.status_code == 404:
            return None
        body = client.json_body(resp)
        return body if isinstance(body, dict) else None

    # ---- login ----

    def login(self, account_hint: str | None = None, method: str | None = None) -> Account:
        method = method or "loopback"
        if method not in ("loopback", "oob"):
            raise PermanentError("login method must be 'loopback' or 'oob'")
        hint = account_hint or self._prompt_text("Mastodon account (@user@instance) or instance: ")
        want_user, host = parse_hint(hint)
        base = _base(host)
        launcher = self._browser_launcher
        if method == "loopback":
            try:
                app, token = oauth.run_loopback(self._http, base, host, launcher, self._say)
            except oauth.RedirectRejected:
                self._say("This instance rejected the loopback redirect; falling back to the manual code flow.")
                method = "oob"
        if method == "oob":
            app, token = oauth.run_oob(self._http, base, host, launcher, self._prompt_secret, self._say)

        try:
            resp = client.request(self._http, "GET", base, "/api/v1/accounts/verify_credentials", token)
            body = client.json_body(resp)
            if resp.status_code != 200 or not body.get("id") or not body.get("username"):
                raise PermanentError("could not verify the new login")
            username = str(body["username"])
            if want_user and want_user.lower() != username.lower():
                raise PermanentError(
                    f"logged in as @{username}@{host}, not the requested @{want_user}@{host}; login rejected"
                )
        except Exception:
            self._revoke(base, app.client_id, app.client_secret, token)
            raise
        account_id = f"{host}:{body['id']}"
        session = Session(host, account_id, username, app.client_id, app.client_secret, token)
        save_session(session)
        self._sessions[account_id] = session
        return Account(self.platform, account_id, f"@{username}@{host}")

    def _revoke(self, base: str, client_id: str, client_secret: str, token: str) -> None:
        try:
            client.request(
                self._http, "POST", base, "/oauth/revoke", None,
                data={"client_id": client_id, "client_secret": client_secret, "token": token},
            )
        except Exception:
            log.warning("could not revoke token at %s (best effort)", base)

    def current_account(self, account_id: str) -> Account:
        s = self._session(account_id)
        resp = self._call("GET", account_id, "/api/v1/accounts/verify_credentials", s.access_token)
        body = client.json_body(resp) if resp.status_code == 200 else {}
        if not body.get("id"):
            raise AuthRequired("session is not valid; run `sociyx login mastodon`")
        if f"{s.instance}:{body['id']}" != account_id:
            raise AuthRequired("stored session does not belong to this account; log in again")
        return Account(self.platform, account_id, f"@{body.get('username', s.username)}@{s.instance}")

    # ---- scanning ----

    def scan_page(self, account: Account, content_type: str, cursor: str | None) -> ScanPage:
        if content_type not in self.content_types:
            raise PermanentError(f"unknown content type {content_type!r}")
        params: dict[str, Any] = {"limit": PAGE_LIMIT, "max_id": cursor}
        if content_type in ("post", "boost"):
            # Public read works without a session on most instances; a 401 means the
            # instance requires auth and surfaces as AuthRequired.
            token = self._token(account.account_id, required=False)
            path = f"/api/v1/accounts/{local_id(account.account_id)}/statuses"
        else:
            token = self._token(account.account_id, required=True)
            path = "/api/v1/favourites" if content_type == "favourite" else "/api/v1/bookmarks"
        resp = self._call("GET", account.account_id, path, token, params=params)
        if resp.status_code == 404:
            raise PermanentError("listing endpoint not found on this server")
        statuses = client.json_body(resp)
        if not isinstance(statuses, list):
            raise PermanentError("unexpected listing response")
        items: list[ContentItem] = []
        for st in statuses:
            if content_type == "post" and st.get("reblog"):
                continue
            if content_type == "boost" and not st.get("reblog"):
                continue
            items.append(to_item(st, account, content_type))
        if not statuses:
            return ScanPage(items, None)
        nxt = client.next_max_id(resp)
        if nxt is None and content_type in ("post", "boost") and not client.has_link_header(resp):
            nxt = str(statuses[-1]["id"])  # no Link header at all: fall back to the last id
        return ScanPage(items, nxt)

    # ---- deleting ----

    def _status_id(self, item: ContentItem) -> str:
        if item.content_type == "boost":
            return str(item.raw.get("original_id") or "")
        sid = item.raw.get("status_id")
        return str(sid or item.item_id.split(":", 1)[-1])

    def delete(self, item: ContentItem) -> DeleteResult:
        if item.content_type not in self.deletable_types:
            raise PermanentError(f"unsupported content type {item.content_type!r}")
        token = self._token(item.account_id, required=True)
        sid = self._status_id(item)
        if not sid:
            raise PermanentError("item lacks a status id")
        if item.content_type == "post":
            return self._delete_post(item, sid, token)
        action, flag = _UNDO[item.content_type]
        resp = self._call("POST", item.account_id, f"/api/v1/statuses/{sid}/{action}", token)
        if resp.status_code == 404:
            return DeleteResult(
                DeleteStatus.ALREADY_DELETED,
                "the original status no longer exists, so your boost/favourite/bookmark of it is gone too",
            )
        body = client.json_body(resp)
        if isinstance(body, dict) and body.get(flag) is False:
            return DeleteResult(DeleteStatus.DELETED, action)
        raise PermanentError(f"server still reports {flag} after {action}")

    def _delete_post(self, item: ContentItem, sid: str, token: str | None) -> DeleteResult:
        current = self._get_status(item.account_id, sid, token)
        if current is None:
            return DeleteResult(DeleteStatus.ALREADY_DELETED, "status not found")
        return self._delete_existing(item, sid, token, current)

    def _delete_existing(self, item: ContentItem, sid: str, token: str | None, current: dict[str, Any]) -> DeleteResult:
        if str((current.get("account") or {}).get("id")) != local_id(item.account_id):
            return DeleteResult(DeleteStatus.INACCESSIBLE, "status belongs to a different account")
        if str(current.get("edited_at") or "") != item.version:
            return DeleteResult(DeleteStatus.CHANGED, "status was edited after the plan was made")
        resp = self._call("DELETE", item.account_id, f"/api/v1/statuses/{sid}", token)
        if resp.status_code == 200:
            return DeleteResult(DeleteStatus.DELETED)
        # 404: gone, or not ours. Look again to tell which.
        again = self._get_status(item.account_id, sid, token)
        if again is None:
            return DeleteResult(DeleteStatus.ALREADY_DELETED, "status not found")
        return DeleteResult(DeleteStatus.INACCESSIBLE, "server says the status cannot be deleted by this account")

    def verify_deleted(self, item: ContentItem) -> VerifyStatus:
        try:
            token = self._token(item.account_id, required=False)
            sid = self._status_id(item)
            if not sid:
                return VerifyStatus.UNKNOWN
            st = self._get_status(item.account_id, sid, token)
            if st is None:
                return VerifyStatus.GONE
            if item.content_type == "post":
                return VerifyStatus.PRESENT
            flag = _UNDO[item.content_type][1]
            value = st.get(flag)
            if value is False:
                return VerifyStatus.GONE
            return VerifyStatus.PRESENT if value is True else VerifyStatus.UNKNOWN
        except AdapterError:
            return VerifyStatus.UNKNOWN

    # ---- housekeeping ----

    def logout(self, account_id: str) -> None:
        s = self._sessions.pop(account_id, None) or load_session(account_id)
        if s:
            self._revoke(_base(s.instance), s.client_id, s.client_secret, s.access_token)
        delete_session(account_id)

    def close(self) -> None:
        if self._owns_http:
            self._http.close()
