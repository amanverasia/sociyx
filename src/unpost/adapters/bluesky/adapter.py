"""Bluesky (AT Protocol) adapter: records live in the user's own repo on their PDS."""

from __future__ import annotations

import getpass
import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any, ClassVar

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
    utc,
)
from ..base import Adapter, ScanPage
from ._http import PUBLIC_APPVIEW, TIMEOUT, USER_AGENT
from .client import AuthFactorRequired, OAuthAuth, PasswordAuth, XrpcClient, XrpcError
from .identity import Identity, resolve_identity
from .oauth import BrowserLauncher, OAuthLogin, dpop_form_post
from .session import (
    OAuthSession,
    PasswordSession,
    Session,
    browser_profile_dir,
    delete_session,
    load_session,
    save_session,
)
from . import dpop

log = logging.getLogger(__name__)

COLLECTIONS = {
    "post": "app.bsky.feed.post",
    "like": "app.bsky.feed.like",
    "repost": "app.bsky.feed.repost",
    "follow": "app.bsky.graph.follow",
    "block": "app.bsky.graph.block",
    "list": "app.bsky.graph.list",
    "listitem": "app.bsky.graph.listitem",
}
_DESCRIPTIONS = {
    "post": "posts, including replies and quote posts",
    "like": "likes",
    "repost": "reposts",
    "follow": "accounts you follow",
    "block": "accounts you block",
    "list": "lists you created",
    "listitem": "entries in your lists",
}
POST_COLLECTION = COLLECTIONS["post"]


def parse_at_uri(uri: str) -> tuple[str, str, str]:
    """at://did/collection/rkey -> (did, collection, rkey)."""
    if not uri.startswith("at://"):
        raise PermanentError("not an at:// record URI")
    parts = uri[len("at://") :].split("/")
    if len(parts) != 3 or not all(parts):
        raise PermanentError("malformed at:// record URI")
    return parts[0], parts[1], parts[2]


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


class BlueskyAdapter(Adapter):
    platform: ClassVar[str] = "bluesky"
    label: ClassVar[str] = "Bluesky (AT Protocol)"
    content_types: ClassVar[dict[str, str]] = dict(_DESCRIPTIONS)
    deletable_types: ClassVar[frozenset[str]] = frozenset(COLLECTIONS)
    # Writes are budgeted at 5000 points/hour and 35000/day per account; a DELETE costs
    # 1 point. One delete per second is ~3600/hour, safely under the hourly budget.
    min_delete_interval: ClassVar[float] = 1.0
    discovery_notes: ClassVar[str] = (
        "Scans the records in your own repository on your PDS (com.atproto.repo.listRecords),\n"
        "one collection per content type. The docs state no cap on history depth; whether\n"
        "very old records are all returned in practice is unverified until tested.\n"
        "NOT covered:\n"
        "  - direct messages (chat.bsky is a separate service)\n"
        "  - your profile record, threadgates/postgates, starter packs and other collections\n"
        "    (not scanned in v0.1)\n"
        "  - media blobs: they become unreferenced after deletion; when the PDS garbage-collects\n"
        "    them is PDS-dependent and unverified\n"
        "  - copies on relays, AppViews and third-party archives/indexes\n"
        "  - other people's quotes, replies, reposts and screenshots of your posts\n"
        "Deletions propagate through the firehose; per the sync spec, mirrors are expected to\n"
        "honour them within seconds or minutes, but third parties may not.\n"
        "Public repos can be scanned read-only without logging in; deleting needs a login."
    )

    def __init__(
        self,
        http: httpx.Client | None = None,
        browser_launcher: BrowserLauncher | None = None,
        prompt_secret: Callable[[str], str] = getpass.getpass,
        prompt_text: Callable[[str], str] = input,
        public_appview: str = PUBLIC_APPVIEW,
    ) -> None:
        self._owns_http = http is None
        self._http = http or httpx.Client(timeout=TIMEOUT, headers={"User-Agent": USER_AGENT})
        self._browser_launcher = browser_launcher
        self._prompt_secret = prompt_secret
        self._prompt_text = prompt_text
        self._appview = public_appview
        self._clients: dict[str, XrpcClient] = {}
        self._pds_cache: dict[str, str] = {}

    # ---- login / session ----

    def login(self, account_hint: str | None = None, method: str | None = None) -> Account:
        method = method or "oauth"
        if method not in ("oauth", "app-password"):
            raise PermanentError("login method must be 'oauth' or 'app-password'")
        hint = account_hint or self._prompt_text("Bluesky handle: ")
        identity = resolve_identity(hint, self._http, self._appview)
        if not identity.handle_verified:
            log.warning("handle %s is not bidirectionally verified against the DID document", identity.handle)
        session: Session
        if method == "oauth":
            session = OAuthLogin(self._http, identity, self._browser_launcher).run()
        else:
            session = self._app_password_login(identity)
        save_session(session)
        self._clients.pop(identity.did, None)
        self._pds_cache[identity.did] = identity.pds_url
        return Account(self.platform, identity.did, identity.handle)

    def _app_password_login(self, identity: Identity) -> PasswordSession:
        print(
            "Create an app password in Bluesky: Settings -> Privacy and security -> App passwords.\n"
            "Type it at the hidden prompt below. Never paste it into a chat or ticket."
        )
        password = self._prompt_secret("App password: ")
        client = XrpcClient(identity.pds_url, http=self._http)
        body: dict[str, Any] = {"identifier": identity.handle or identity.did, "password": password}
        try:
            try:
                data = client.post("com.atproto.server.createSession", body)
            except AuthFactorRequired:
                body["authFactorToken"] = self._prompt_secret("Emailed sign-in code: ")
                data = client.post("com.atproto.server.createSession", body)
        finally:
            del body, password
        if data.get("did") != identity.did:
            raise PermanentError("server returned a session for a different DID; login rejected")
        return PasswordSession(
            did=identity.did,
            handle=data.get("handle", identity.handle),
            pds_url=identity.pds_url,
            access_jwt=data["accessJwt"],
            refresh_jwt=data["refreshJwt"],
        )

    def _authed_client(self, did: str) -> XrpcClient:
        if did in self._clients:
            return self._clients[did]
        session = load_session(did)
        if session is None:
            raise AuthRequired("no Bluesky session for this account; run `unpost login bluesky`")
        auth = (
            OAuthAuth(session, self._http)
            if isinstance(session, OAuthSession)
            else PasswordAuth(session, self._http)
        )
        client = XrpcClient(session.pds_url, auth, self._http)
        self._clients[did] = client
        return client

    def current_account(self, account_id: str) -> Account:
        data = self._authed_client(account_id).get("com.atproto.server.getSession")
        if data.get("did") != account_id:
            raise AuthRequired("stored session does not belong to this account; log in again")
        return Account(self.platform, account_id, data.get("handle", ""))

    # ---- scanning ----

    def _pds_for(self, did: str) -> str:
        if did not in self._pds_cache:
            session = load_session(did)
            self._pds_cache[did] = (
                session.pds_url if session else resolve_identity(did, self._http, self._appview).pds_url
            )
        return self._pds_cache[did]

    def _reader(self, did: str) -> XrpcClient:
        """Authenticated client if we have a session, else a public one on the PDS
        (listRecords/getRecord need no auth, so public repos scan read-only)."""
        if load_session(did) is not None:
            return self._authed_client(did)
        return XrpcClient(self._pds_for(did), None, self._http)

    def scan_page(self, account: Account, content_type: str, cursor: str | None) -> ScanPage:
        if content_type not in COLLECTIONS:
            raise PermanentError(f"unknown Bluesky content type {content_type!r}")
        data = self._reader(account.account_id).get(
            "com.atproto.repo.listRecords",
            repo=account.account_id,
            collection=COLLECTIONS[content_type],
            limit=100,
            cursor=cursor,
        )
        items = [self._to_item(account, content_type, rec) for rec in data.get("records", [])]
        return ScanPage(items, (data.get("cursor") or None) if items else None)

    def _to_item(self, account: Account, content_type: str, rec: dict[str, Any]) -> ContentItem:
        uri = rec["uri"]
        did, _collection, rkey = parse_at_uri(uri)
        if did != account.account_id:  # isolation: never surface another repo's records
            raise PermanentError("listRecords returned a record from a different repo")
        value = rec.get("value") or {}
        text, url = "", ""
        subject = value.get("subject")
        if content_type == "post":
            text = value.get("text", "")
            url = f"https://bsky.app/profile/{did}/post/{rkey}"
        elif content_type == "list":
            text = "\n".join(p for p in (value.get("name", ""), value.get("description", "")) if p)
            url = f"https://bsky.app/profile/{did}/lists/{rkey}"
        elif content_type in ("like", "repost") and isinstance(subject, dict):
            try:
                s_did, s_coll, s_rkey = parse_at_uri(str(subject.get("uri", "")))
                if s_coll == POST_COLLECTION:
                    url = f"https://bsky.app/profile/{s_did}/post/{s_rkey}"
            except PermanentError:
                pass
        elif content_type in ("follow", "block") and isinstance(subject, str):
            url = f"https://bsky.app/profile/{subject}"
        return ContentItem(
            platform=self.platform,
            account_id=account.account_id,
            item_id=uri,
            content_type=content_type,
            created_at=_parse_time(value.get("createdAt")),
            text=text,
            url=url,
            version=rec.get("cid", ""),
            raw={"uri": uri, "cid": rec.get("cid", ""), "value": value},
        )

    # ---- deletion ----

    def _get_record(self, client: XrpcClient, did: str, collection: str, rkey: str) -> dict[str, Any] | None:
        try:
            return client.get("com.atproto.repo.getRecord", repo=did, collection=collection, rkey=rkey)
        except XrpcError as e:
            if e.error == "RecordNotFound":
                return None
            raise

    def delete(self, item: ContentItem) -> DeleteResult:
        did, collection, rkey = parse_at_uri(item.item_id)
        if did != item.account_id:
            return DeleteResult(DeleteStatus.INACCESSIBLE, "record belongs to another repo")
        client = self._authed_client(did)
        try:
            current = self._get_record(client, did, collection, rkey)
            if current is None:
                return DeleteResult(DeleteStatus.ALREADY_DELETED, "record not found")
            if current.get("cid") != item.version:
                return DeleteResult(DeleteStatus.CHANGED, "record was replaced since the scan")
            try:
                client.post(
                    "com.atproto.repo.deleteRecord",
                    {"repo": did, "collection": collection, "rkey": rkey, "swapRecord": item.version},
                )
            except XrpcError as e:
                if e.error != "InvalidSwap":
                    raise
                # Lost a race: someone deleted or replaced it between getRecord and delete.
                again = self._get_record(client, did, collection, rkey)
                if again is None:
                    return DeleteResult(DeleteStatus.ALREADY_DELETED, "record vanished during delete")
                return DeleteResult(DeleteStatus.CHANGED, "record changed during delete")
        except XrpcError as e:
            if e.status == 403 or e.error in ("Forbidden", "NotAuthorized"):
                return DeleteResult(DeleteStatus.INACCESSIBLE, e.message or f"HTTP {e.status} {e.error}")
            raise
        return DeleteResult(DeleteStatus.DELETED)

    def verify_deleted(self, item: ContentItem) -> VerifyStatus:
        try:
            did, collection, rkey = parse_at_uri(item.item_id)
            record = self._get_record(self._reader(did), did, collection, rkey)
        except AdapterError:
            return VerifyStatus.UNKNOWN
        return VerifyStatus.GONE if record is None else VerifyStatus.PRESENT

    # ---- teardown ----

    def logout(self, account_id: str) -> None:
        session = load_session(account_id)
        self._clients.pop(account_id, None)
        if session is None:
            return
        try:  # best effort: never block a local logout on the network
            if isinstance(session, OAuthSession):
                if session.revocation_endpoint:
                    dpop_form_post(
                        self._http,
                        session.revocation_endpoint,
                        {
                            "token": session.refresh_token,
                            "token_type_hint": "refresh_token",
                            "client_id": session.client_id,
                        },
                        dpop.key_from_pem(session.dpop_private_key_pem),
                        session.nonces,
                    )
            else:
                self._http.post(
                    f"{session.pds_url}/xrpc/com.atproto.server.deleteSession",
                    headers={"Authorization": f"Bearer {session.refresh_jwt}", "User-Agent": USER_AGENT},
                    timeout=TIMEOUT,
                )
        except Exception:  # noqa: BLE001
            log.info("remote token revocation failed; deleting local session anyway")
        delete_session(account_id)
        log.info(
            "Bluesky session removed. The browser profile (holds your Bluesky web login) stays at %s; "
            "delete it with rm -rf if you want it gone.",
            browser_profile_dir(),
        )

    def close(self) -> None:
        if self._owns_http:
            self._http.close()
