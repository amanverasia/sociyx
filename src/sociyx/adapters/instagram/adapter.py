"""Instagram adapter (UNOFFICIAL private mobile API via the optional `instagrapi` package)."""

from __future__ import annotations

import getpass
import json
import logging
import random
import shutil
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import unquote

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
from ...paths import secrets_dir
from ...redact import scrub
from ..base import Adapter, ScanPage
from . import mapping
from .browser import LOGIN_URL, TIMEOUT, BrowserRunner, playwright_runner
from .errors import Gone, map_exceptions
from .export_import import iter_export_items
from .session import delete_settings, dump_settings, load_settings, save_settings, settings_path

log = logging.getLogger(__name__)

PAGE = 33
EXPORT_TOLERANCE_S = 120
INSTALL_HINT = "the Instagram adapter needs instagrapi: install it with `uv sync --extra instagram`"
PASSWORD_GUIDANCE = (
    "Instagram rejected the browser session (login_required). Browser sessions are sometimes refused by the "
    "mobile API; log in with `--method password` instead."
)


def default_client_factory(**kwargs: Any) -> Any:
    try:
        from instagrapi import Client
    except ImportError as e:
        raise PermanentError(INSTALL_HINT) from e
    return Client(**kwargs)


def _dump(state: dict[str, Any]) -> str:
    return json.dumps(state, separators=(",", ":"), sort_keys=True)


def _load(cursor: str | None) -> dict[str, Any]:
    if not cursor:
        return {}
    try:
        st = json.loads(cursor)
    except ValueError:
        raise PermanentError("corrupt scan cursor") from None
    if not isinstance(st, dict):
        raise PermanentError("corrupt scan cursor")
    return st


def _next(cursor: Any, previous: Any) -> str | None:
    c = str(cursor) if cursor else ""
    return c if c and c != str(previous or "") else None


class InstagramAdapter(Adapter):
    platform: ClassVar[str] = "instagram"
    label: ClassVar[str] = "Instagram (private mobile API via instagrapi; unofficial)"
    content_types: ClassVar[dict[str, str]] = {
        "post": "your feed posts (including carousels; not reels)",
        "reel": "your reels",
        "story": "your stories (live and archived)",
        "like": "posts you liked (via feed/liked; depth cap unverified)",
        "saved": "posts you saved (all collections; unsaved from every collection)",
        "dm": "direct messages YOU sent (unsent for everyone), across inbox threads",
        "comment": "your comments on YOUR OWN posts",
        "comment_export": "your comments on OTHER people's posts; only from a data export "
        "(`sociyx import instagram <export_dir>`); resolved best-effort at delete time",
    }
    deletable_types: ClassVar[frozenset[str]] = frozenset(content_types)
    # Very slow on purpose: 30-45 s between writes, a 1-3 minute pause every 25, 200/day maximum.
    # DM unsends are riskier; run DM jobs with a lower cap, e.g. `--daily-cap 100`.
    min_delete_interval: ClassVar[float] = 30.0
    pacing_jitter: ClassVar[float] = 0.5
    think_pause_every: ClassVar[int] = 25
    think_pause_seconds: ClassVar[tuple[float, float]] = (60.0, 180.0)
    daily_write_cap: ClassVar[int | None] = 200
    scan_delay: ClassVar[tuple[float, float]] = (8.0, 20.0)
    discovery_notes: ClassVar[str] = (
        "UNOFFICIAL: uses Instagram's private mobile API through the community `instagrapi` library.\n"
        "This conflicts with Instagram's Terms of Use and can trigger challenges or action blocks.\n"
        "You accepted that risk for your own data; sociyx mitigates it with very slow pacing (30-45 s\n"
        "per write, long pauses, a 200/day cap), stops on any challenge/login prompt, and never tries\n"
        "to bypass one. For DM jobs use a lower cap, e.g. `--daily-cap 100`.\n"
        "Login: a visible browser where YOU log in (default), or `password`, or an existing instagrapi\n"
        "settings file. Whether the browser's sessionid is accepted by the mobile API is UNVERIFIED;\n"
        "if it is refused, use `--method password`.\n"
        "Listed via the API: posts, reels, stories (live + archive), likes, saved posts, DMs you sent,\n"
        "and comments on your own posts (top-level comments only).\n"
        "  - likes: feed/liked depth cap is UNVERIFIED; older likes may need the data export\n"
        "  - saved: all saved posts are listed; unsaving removes the post from ALL collections\n"
        "  - DMs: inbox threads reachable via direct_threads (message requests/pending not covered);\n"
        "    only YOUR sent messages are listed and unsent. Unsending removes them for the other\n"
        "    party, but they may already have seen them, got notifications, or exported them. Instagram's\n"
        "    unsend can report success without effect, so every unsend is re-read and verified.\n"
        "  - comments on other people's posts: NO API lists them. Request your data export (Settings ->\n"
        "    Accounts Center -> Your information and permissions -> Download your information, JSON) and\n"
        "    import it. Resolving each export comment is fuzzy: sociyx searches the owner's recent posts\n"
        "    for an exact text match within +/-120 s; if not found it reports it so you can delete it\n"
        "    manually (Settings -> Your activity -> Comments).\n"
        "NOT covered: likes on comments, story replies, tags, replies to comments, follows/followers.\n"
        "Instagram may retain backups or copies after deletion; deletion is not instantly visible\n"
        "everywhere."
    )

    def __init__(
        self,
        client_factory: Callable[..., Any] | None = None,
        browser_runner: BrowserRunner | None = None,
        prompt_text: Callable[[str], str] = input,
        prompt_secret: Callable[[str], str] = getpass.getpass,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
        settings_dir: Path | None = None,
        say: Callable[[str], None] = print,
        session_file: Path | str | None = None,
        export_search_pages: int = 10,
        export_search_posts: int = 15,
        dm_lookup_pages: int = 3,
        browser_timeout: float = TIMEOUT,
    ) -> None:
        self._factory = client_factory or default_client_factory
        self._browser_runner = browser_runner or playwright_runner
        self._prompt_text = prompt_text
        self._prompt_secret = prompt_secret
        self._sleep = sleep
        self._rng = rng or random.Random()
        self._settings_dir = Path(settings_dir) if settings_dir else None
        self._say = say
        self._session_file = session_file
        self.export_search_pages = export_search_pages
        self.export_search_posts = export_search_posts
        self.dm_lookup_pages = dm_lookup_pages
        self._browser_timeout = browser_timeout
        self._clients: dict[str, Any] = {}
        self._saved: dict[str, str] = {}
        self._titles: dict[str, str] = {}
        self._resolved: dict[str, tuple[str, str]] = {}

    def __repr__(self) -> str:
        return "InstagramAdapter(sessions=<hidden>)"

    # ---- clients and sessions ----

    def _base(self) -> Path:
        return self._settings_dir or secrets_dir("instagram")

    def _new_client(self) -> Any:
        # with_challenge_flow=False: never let the library auto-resolve challenges (hidden
        # /challenge/ requests, blocking input()). Challenges surface as AuthRequired instead.
        return self._factory(delay_range=[1, 3], with_challenge_flow=False)

    def _client(self, account_id: str) -> Any:
        c = self._clients.get(account_id)
        if c is not None:
            return c
        settings = load_settings(account_id, self._base())
        if settings is None:
            raise AuthRequired("no Instagram session for this account; run `sociyx login instagram`")
        c = self._new_client()
        c.set_settings(settings)
        uid = getattr(c, "user_id", None)
        if uid is not None and str(uid) != account_id:
            raise AuthRequired("stored session does not belong to this account; log in again")
        self._clients[account_id] = c
        self._saved[account_id] = dump_settings(settings)
        return c

    def _persist(self, account_id: str) -> None:
        c = self._clients.get(account_id)
        if c is None:
            return
        text = dump_settings(c.get_settings())
        if text != self._saved.get(account_id):
            save_settings(account_id, json.loads(text), self._base())
            self._saved[account_id] = text

    def _finish_login(self, client: Any, expect_pk: str | None = None) -> Account:
        info = client.account_info()
        pk = str(info.pk)
        if expect_pk is not None and pk != str(expect_pk):
            raise PermanentError("the session belongs to a different account than the browser login; rejected")
        self._clients[pk] = client
        self._persist(pk)
        return Account(self.platform, pk, getattr(info, "username", "") or "")

    # ---- login ----

    def login(self, account_hint: str | None = None, method: str | None = None) -> Account:
        method = method or "browser"
        if method not in ("browser", "password", "sessionfile"):
            raise PermanentError("login method must be 'browser', 'password' or 'sessionfile'")
        if method == "browser":
            return self._login_browser()
        if method == "password":
            return self._login_password(account_hint)
        return self._login_sessionfile(account_hint)

    def _login_browser(self) -> Account:
        from instagrapi import exceptions as ig

        profile = self._base() / "browser-profile"
        self._say("Opening a browser. Log in to Instagram yourself (2FA/challenges included).")
        cookies = self._browser_runner(profile, LOGIN_URL, self._browser_timeout)
        sessionid = unquote(cookies.get("sessionid", ""))
        ds_user_id = cookies.get("ds_user_id") or (sessionid.split(":", 1)[0] if sessionid else "")
        client = self._new_client()
        try:
            client.login_by_sessionid(sessionid)
            return self._finish_login(client, ds_user_id or None)
        except (ig.LoginRequired, ig.ClientLoginRequired) as e:
            raise AuthRequired(PASSWORD_GUIDANCE) from e
        except AssertionError as e:
            raise PermanentError(f"the browser did not provide a usable sessionid: {scrub(str(e))[:100]}") from None
        except AdapterError:
            raise
        except Exception as e:  # noqa: BLE001
            with map_exceptions():
                raise e

    def _login_password(self, hint: str | None) -> Account:
        from instagrapi import exceptions as ig

        username = hint or self._prompt_text("Instagram username: ")
        password = self._prompt_secret("Instagram password (hidden): ")
        client = self._new_client()
        try:
            try:
                client.login(username, password)
            except ig.TwoFactorRequired:
                code = self._prompt_secret("2FA code (hidden): ")
                client.login(username, password, verification_code=code.strip())
            return self._finish_login(client)
        except ig.ChallengeError as e:
            raise AuthRequired(
                "Instagram wants a challenge/verification. Complete it in the official app or website, "
                "then try logging in again. sociyx will not attempt to solve it."
            ) from e
        except (ig.BadPassword, ig.BadCredentials) as e:
            raise AuthRequired("Instagram rejected the username or password") from e
        except AdapterError:
            raise
        except Exception as e:  # noqa: BLE001
            with map_exceptions():
                raise e
        finally:
            password = ""  # noqa: F841

    def _login_sessionfile(self, hint: str | None) -> Account:
        path = hint or self._session_file
        if not path:
            raise PermanentError("provide the instagrapi settings JSON path (--session-file PATH)")
        client = self._new_client()
        try:
            client.load_settings(path)
        except (OSError, ValueError) as e:
            raise PermanentError(f"could not read the session file ({type(e).__name__})") from None
        with map_exceptions():
            return self._finish_login(client)

    def current_account(self, account_id: str) -> Account:
        c = self._client(account_id)
        with map_exceptions():
            info = c.account_info()
        if str(info.pk) != account_id:
            raise AuthRequired("stored session does not belong to this account; log in again")
        self._persist(account_id)
        return Account(self.platform, account_id, getattr(info, "username", "") or "")

    def logout(self, account_id: str) -> None:
        self._clients.pop(account_id, None)
        self._saved.pop(account_id, None)
        delete_settings(account_id, self._base())
        shutil.rmtree(self._base() / "browser-profile", ignore_errors=True)

    def close(self) -> None:
        for aid in list(self._clients):
            try:
                self._persist(aid)
            except Exception:  # noqa: BLE001
                log.warning("could not save Instagram settings")

    # ---- scanning ----

    def scan_page(self, account: Account, content_type: str, cursor: str | None) -> ScanPage:
        if content_type not in self.content_types:
            raise PermanentError(f"unknown Instagram content type {content_type!r}")
        if content_type == "comment_export":
            return ScanPage([], None)  # cannot be listed through the API; use `sociyx import instagram`
        c = self._client(account.account_id)
        st = _load(cursor)
        with map_exceptions():
            page = getattr(self, f"_scan_{content_type}")(c, account, st)
        self._persist(account.account_id)
        return page

    def _own(self, m: Any, account: Account) -> bool:
        ok = str(m.user.pk) == account.account_id
        if not ok:
            log.warning("skipping a media item not owned by this account")
        return ok

    def _scan_post(self, c: Any, account: Account, st: dict[str, Any]) -> ScanPage:
        medias, nxt = c.user_medias_paginated(account.account_id, PAGE, st.get("c", ""))
        items = [
            mapping.media_item(m, account, "post")
            for m in medias
            if self._own(m, account) and getattr(m, "product_type", "") != "clips"
        ]
        n = _next(nxt, st.get("c"))
        return ScanPage(items, _dump({"c": n}) if n else None)

    def _scan_reel(self, c: Any, account: Account, st: dict[str, Any]) -> ScanPage:
        medias, nxt = c.user_clips_paginated_v1(account.account_id, 50, st.get("c", ""))
        items = [mapping.media_item(m, account, "reel") for m in medias if self._own(m, account)]
        n = _next(nxt, st.get("c"))
        return ScanPage(items, _dump({"c": n}) if n else None)

    def _scan_story(self, c: Any, account: Account, st: dict[str, Any]) -> ScanPage:
        if st.get("ph") != "arch":
            live = [s for s in c.user_stories(account.account_id) if self._own(s, account)]
            items = [mapping.story_item(s, account, False) for s in live]
            return ScanPage(items, _dump({"ph": "arch", "live": [str(s.pk) for s in live]}))
        seen = set(st.get("live", []))
        # archive_stories_v1 fetches all archive days (50 per request) in this one scan page
        stories = [s for s in c.archive_stories_v1(0) if self._own(s, account) and str(s.pk) not in seen]
        return ScanPage([mapping.story_item(s, account, True) for s in stories], None)

    def _scan_collection(self, c: Any, account: Account, st: dict[str, Any], which: str) -> ScanPage:
        medias, nxt = c.collection_medias_v1_chunk(which, st.get("c", ""))
        items = [mapping.media_item(m, account, which if which == "liked" and False else self._ctype(which))
                 for m in medias]
        n = _next(nxt, st.get("c"))
        return ScanPage(items, _dump({"c": n}) if n else None)

    @staticmethod
    def _ctype(which: str) -> str:
        return "like" if which == "liked" else "saved"

    def _scan_like(self, c: Any, account: Account, st: dict[str, Any]) -> ScanPage:
        return self._scan_collection(c, account, st, "liked")

    def _scan_saved(self, c: Any, account: Account, st: dict[str, Any]) -> ScanPage:
        return self._scan_collection(c, account, st, "saved")

    # DM crawl: cursor = {"q": [thread ids of the current inbox page], "mc": message cursor of q[0],
    # "tc": cursor for the next inbox page, "more": bool}. Each call is ONE request: one inbox page
    # or one message page of one thread. The cursor only ever holds one inbox page of ids (<= ~20).
    def _thread_page(self, c: Any, thread_id: str, cursor: str | None) -> tuple[list[dict[str, Any]], str | None]:
        from instagrapi import exceptions as ig

        params = {"visual_message_return_type": "unseen", "direction": "older", "seq_id": "40065", "limit": "20"}
        if cursor:
            params["cursor"] = cursor
        try:
            result = c.private_request(f"direct_v2/threads/{thread_id}/", params=params)
        except ig.ClientNotFoundError as e:
            raise ig.DirectThreadNotFound(e, thread_id=thread_id) from None
        thread = result["thread"]
        return list(thread.get("items", [])), thread.get("oldest_cursor") or None

    def _scan_dm(self, c: Any, account: Account, st: dict[str, Any]) -> ScanPage:
        st = st or {"q": [], "mc": None, "tc": None, "more": True}
        items: list[ContentItem] = []
        if st["q"]:
            tid = str(st["q"][0])
            try:
                raws, nxt = self._thread_page(c, tid, st["mc"])
            except Gone:
                raws, nxt = [], None
            for r in raws:
                if str(r.get("user_id")) == account.account_id and r.get("item_type") != "action_log" and r.get("item_id"):
                    items.append(mapping.dm_item(r, tid, account, self._titles.get(tid, "")))
            if nxt and nxt != st["mc"]:
                st["mc"] = nxt
            else:
                st["q"], st["mc"] = st["q"][1:], None
        elif st["more"]:
            threads, nxt = c.direct_threads_chunk(cursor=st["tc"])
            for t in threads:
                self._titles[str(t.id)] = (getattr(t, "thread_title", "") or "")[:80]
            st["q"] = [str(t.id) for t in threads]
            new = _next(nxt, st["tc"])
            st["tc"], st["more"] = new, bool(new)
        if not st["q"] and not st["more"]:
            return ScanPage(items, None)
        return ScanPage(items, _dump(st))

    # Comments on your own posts: cursor = {"q": [post media ids from the current page of posts],
    # "cc": comment cursor of q[0], "pc": next post-page cursor, "more": bool}. One request per call.
    def _scan_comment(self, c: Any, account: Account, st: dict[str, Any]) -> ScanPage:
        st = st or {"q": [], "cc": None, "pc": "", "more": True}
        items: list[ContentItem] = []
        if st["q"]:
            mid = str(st["q"][0])
            try:
                comments, nxt = c.media_comments_chunk(mid, 1, st["cc"] or None)
            except Gone:
                comments, nxt = [], None
            code = st.get("codes", {}).get(mid, "")
            for cm in comments:
                if str(cm.user.pk) == account.account_id:
                    items.append(mapping.comment_item(cm, mid, code, account))
            n = _next(nxt, st["cc"])
            if n:
                st["cc"] = n
            else:
                st["q"], st["cc"] = st["q"][1:], None
                st.get("codes", {}).pop(mid, None)
        elif st["more"]:
            medias, nxt = c.user_medias_paginated(account.account_id, PAGE, st["pc"])
            own = [m for m in medias if self._own(m, account) and (getattr(m, "comment_count", 1) or 0) > 0]
            st["q"] = [mapping.full_media_id(m) for m in own]
            st["codes"] = {mapping.full_media_id(m): getattr(m, "code", "") or "" for m in own}
            new = _next(nxt, st["pc"])
            st["pc"], st["more"] = new or "", bool(new)
        if not st["q"] and not st["more"]:
            return ScanPage(items, None)
        return ScanPage(items, _dump(st))

    # ---- deleting ----

    def pause(self) -> None:
        self._sleep(self._rng.uniform(*self.scan_delay))

    def delete(self, item: ContentItem) -> DeleteResult:
        if item.content_type not in self.deletable_types:
            raise PermanentError(f"unsupported content type {item.content_type!r}")
        c = self._client(item.account_id)
        try:
            with map_exceptions():
                result = getattr(self, f"_delete_{item.content_type}")(c, item)
        except Gone:
            return DeleteResult(DeleteStatus.ALREADY_DELETED, "no longer exists on Instagram")
        self._persist(item.account_id)
        return result

    @staticmethod
    def _ok(did: Any, what: str) -> None:
        if not did:
            raise PermanentError(f"Instagram did not confirm {what}")

    def _media_id(self, item: ContentItem) -> str:
        return str(item.raw.get("media_id") or item.item_id)

    def _delete_media(self, c: Any, item: ContentItem, check_version: bool = True) -> DeleteResult:
        mid = self._media_id(item)
        info = c.media_info_v1(mid.split("_")[0])  # Gone -> ALREADY_DELETED
        if str(info.user.pk) != item.account_id:
            return DeleteResult(DeleteStatus.INACCESSIBLE, "this media is not owned by the logged-in account")
        if check_version and item.version and mapping.media_version(info) != item.version:
            return DeleteResult(DeleteStatus.CHANGED, "the caption changed since the plan was made")
        self._ok(c.media_delete(mid), "the deletion")
        return DeleteResult(DeleteStatus.DELETED, "media deleted")

    def _delete_post(self, c: Any, item: ContentItem) -> DeleteResult:
        return self._delete_media(c, item)

    def _delete_reel(self, c: Any, item: ContentItem) -> DeleteResult:
        return self._delete_media(c, item)

    def _delete_story(self, c: Any, item: ContentItem) -> DeleteResult:
        self._ok(c.story_delete(self._media_id(item)), "the story deletion")
        return DeleteResult(DeleteStatus.DELETED, "story deleted")

    def _full_id(self, c: Any, item: ContentItem) -> str:
        mid = str(item.raw.get("media_id") or "")
        if "_" in mid:
            return mid
        pk = str(item.raw.get("media_pk") or item.item_id.split(":", 1)[1])
        info = c.media_info_v1(pk)  # Gone -> ALREADY_DELETED; one extra request to learn the owner
        return f"{pk}_{info.user.pk}"

    def _delete_like(self, c: Any, item: ContentItem) -> DeleteResult:
        self._ok(c.media_unlike(self._full_id(c, item)), "the unlike")
        return DeleteResult(DeleteStatus.DELETED, "unliked")

    def _delete_saved(self, c: Any, item: ContentItem) -> DeleteResult:
        self._ok(c.media_unsave(self._full_id(c, item)), "the unsave")
        return DeleteResult(DeleteStatus.DELETED, "unsaved (all collections)")

    def _delete_comment(self, c: Any, item: ContentItem) -> DeleteResult:
        mid, pk = str(item.raw["media_id"]), str(item.raw["comment_pk"])
        self._ok(c.comment_bulk_delete(mid, [pk]), "the comment deletion")
        return DeleteResult(DeleteStatus.DELETED, "comment deleted")

    def _dm_ids(self, item: ContentItem) -> tuple[str, str]:
        if item.raw.get("thread_id") and item.raw.get("message_id"):
            return str(item.raw["thread_id"]), str(item.raw["message_id"])
        _, tid, mid = item.item_id.split(":", 2)
        return tid, mid

    def _locate_dm(self, c: Any, tid: str, mid: str, ts_us: int | None) -> tuple[str, dict[str, Any] | None]:
        """Walk the thread newest-first. Returns ('found', raw) | ('absent', None) | ('unknown', None)."""
        cursor: str | None = None
        for page in range(max(1, self.dm_lookup_pages)):
            if page:
                self.pause()
            raws, nxt = self._thread_page(c, tid, cursor)
            for r in raws:
                if str(r.get("item_id")) == mid:
                    return "found", r
            oldest = min((int(r["timestamp"]) for r in raws if str(r.get("timestamp", "")).isdigit()), default=None)
            if not nxt or nxt == cursor or (ts_us and oldest is not None and oldest < ts_us):
                return "absent", None
            cursor = nxt
        return "unknown", None

    def _delete_dm(self, c: Any, item: ContentItem) -> DeleteResult:
        tid, mid = self._dm_ids(item)
        ts = str(item.raw.get("ts_us", ""))
        state, raw = self._locate_dm(c, tid, mid, int(ts) if ts.isdigit() else None)  # Gone -> ALREADY_DELETED
        if state == "absent":
            return DeleteResult(DeleteStatus.ALREADY_DELETED, "message no longer in the thread")
        if state != "found" or raw is None:
            return DeleteResult(DeleteStatus.INACCESSIBLE, "could not locate the message to verify its sender; no unsend attempted")
        if raw is not None and str(raw.get("user_id")) != item.account_id:
            return DeleteResult(DeleteStatus.INACCESSIBLE, "this message was not sent by you")
        self._ok(c.direct_message_delete(tid, mid), "the unsend")
        return DeleteResult(DeleteStatus.DELETED, "unsend requested (verify re-reads the thread)")

    def _delete_comment_export(self, c: Any, item: ContentItem) -> DeleteResult:
        from instagrapi import exceptions as ig

        manual = (
            "could not locate this comment via the API; delete it manually in "
            "Settings -> Your activity -> Comments"
        )
        owner, ts = str(item.raw.get("owner", "")), int(item.raw.get("timestamp", 0))
        if not owner or not ts:
            return DeleteResult(DeleteStatus.INACCESSIBLE, manual)
        try:
            owner_pk = c.user_id_from_username(owner)
        except ig.UserNotFound:
            return DeleteResult(DeleteStatus.INACCESSIBLE, manual + " (the account no longer exists or was renamed)")
        cursor, checked, requests = "", 0, 0
        for _ in range(max(1, self.export_search_pages)):
            if requests:
                self.pause()
            medias, nxt = c.user_medias_paginated(owner_pk, PAGE, cursor)
            requests += 1
            for m in medias:
                if utc(m.taken_at).timestamp() > ts + EXPORT_TOLERANCE_S:
                    continue  # posted after the comment: cannot hold it
                if checked >= self.export_search_posts:
                    return DeleteResult(DeleteStatus.INACCESSIBLE, manual)
                self.pause()
                checked += 1
                requests += 1
                comments, _ = c.media_comments_chunk(m.id, 50)
                for cm in comments:
                    if (
                        str(cm.user.pk) == item.account_id
                        and (cm.text or "").strip() == item.text.strip()
                        and abs(utc(cm.created_at_utc).timestamp() - ts) <= EXPORT_TOLERANCE_S
                    ):
                        self._ok(c.comment_bulk_delete(m.id, [cm.pk]), "the comment deletion")
                        self._resolved[item.item_id] = (str(m.id), str(cm.pk))
                        return DeleteResult(DeleteStatus.DELETED, "comment found and deleted")
            n = _next(nxt, cursor)
            if not n:
                break
            cursor = n
        return DeleteResult(DeleteStatus.INACCESSIBLE, manual)

    # ---- verifying ----

    def verify_deleted(self, item: ContentItem) -> VerifyStatus:
        c = self._client(item.account_id)
        try:
            with map_exceptions():
                return getattr(self, f"_verify_{item.content_type}")(c, item)
        except Gone:
            return VerifyStatus.GONE

    def _pk(self, item: ContentItem) -> str:
        return str(item.raw.get("media_pk") or self._media_id(item).split(":")[-1].split("_")[0])

    def _verify_post(self, c: Any, item: ContentItem) -> VerifyStatus:
        c.media_info_v1(self._pk(item))
        return VerifyStatus.PRESENT

    _verify_reel = _verify_post

    def _verify_story(self, c: Any, item: ContentItem) -> VerifyStatus:
        c.media_info_v1(self._pk(item))  # Gone -> GONE
        if any(str(s.pk) == self._pk(item) for s in c.user_stories(item.account_id)):
            return VerifyStatus.PRESENT
        return VerifyStatus.UNKNOWN  # not live any more but still resolvable (archived?)

    def _verify_like(self, c: Any, item: ContentItem) -> VerifyStatus:
        liked = getattr(c.media_info_v1(self._pk(item)), "has_liked", None)
        if liked is None:
            return VerifyStatus.UNKNOWN
        return VerifyStatus.PRESENT if liked else VerifyStatus.GONE

    def _verify_saved(self, c: Any, item: ContentItem) -> VerifyStatus:
        # instagrapi's Media model never populates the viewer-saved flag (it reads
        # `viewer_has_saved`, the API sends `has_viewer_saved`), so saved state is not checkable.
        return VerifyStatus.UNKNOWN

    def _verify_dm(self, c: Any, item: ContentItem) -> VerifyStatus:
        tid, mid = self._dm_ids(item)
        ts = str(item.raw.get("ts_us", ""))
        state, _ = self._locate_dm(c, tid, mid, int(ts) if ts.isdigit() else None)
        return {"found": VerifyStatus.PRESENT, "absent": VerifyStatus.GONE}.get(state, VerifyStatus.UNKNOWN)

    def _comment_present(self, c: Any, media_id: str, pk: str) -> VerifyStatus:
        cursor: str | None = None
        for page in range(3):
            if page:
                self.pause()
            comments, nxt = c.media_comments_chunk(media_id, 50, cursor)
            if any(str(cm.pk) == pk for cm in comments):
                return VerifyStatus.PRESENT
            n = _next(nxt, cursor)
            if not n:
                return VerifyStatus.GONE
            cursor = n
        return VerifyStatus.UNKNOWN

    def _verify_comment(self, c: Any, item: ContentItem) -> VerifyStatus:
        return self._comment_present(c, str(item.raw["media_id"]), str(item.raw["comment_pk"]))

    def _verify_comment_export(self, c: Any, item: ContentItem) -> VerifyStatus:
        resolved = self._resolved.get(item.item_id)
        return self._comment_present(c, *resolved) if resolved else VerifyStatus.UNKNOWN

    # ---- data export ----

    def import_export(self, path: Path, account: Account | None = None) -> list[ContentItem]:
        """Parse an unzipped "Download your information" JSON export into ContentItems."""
        if account is None:
            ids = sorted(p.stem for p in self._base().glob("*.json"))
            if len(ids) != 1:
                raise PermanentError("pass the account explicitly: expected exactly one stored Instagram session")
            account = Account(self.platform, ids[0], "")
        return list(iter_export_items(Path(path), account))
