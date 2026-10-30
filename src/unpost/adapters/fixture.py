"""FIXTURE adapter: synthetic data for development, demos and tests.

Nothing here talks to a network or a real platform. State lives in a plain JSON
file (not a secret) so CLI demos survive restarts. Failure modes are injected
through ``faults`` (constructor argument or the UNPOST_FIXTURE_FAULTS env var)
so the executor's safety behaviour can be tested deterministically.

Supported faults (all optional):
  page_overlap: int            next page repeats the last N items of the previous page
  stuck_cursor: true           scan never advances its cursor
  auth_challenge: true         current_account() raises AuthRequired
  transient: {item_id: n}      delete raises TransientError for the first n calls
  rate_limit: {item_id: n}     delete raises RateLimited(retry_after=5) for the first n calls
  transient_then_gone: [ids]   delete really happens, but raises TransientError (response lost), first call only
  inaccessible: [item_id]      delete returns INACCESSIBLE
  permanent: [item_id]         delete raises PermanentError
  auth_after: k                session expires (AuthRequired) once, after k successful deletes
  accept_but_keep: [item_id]   delete reports DELETED but the item stays (verify -> PRESENT)
  crash_after_delete: [id]     really delete, then raise KeyboardInterrupt (killed mid-request)
  unverifiable: [item_id]      verify_deleted returns UNKNOWN
  action_blocked: [item_id]    delete raises ActionBlocked(cool_down=action_blocked_cooldown, default 3600)
  verify_blocked: [item_id]    verify_deleted raises ActionBlocked
  scan_blocked: true           scan_page raises ActionBlocked
Counters are persisted in the state file so they work across executor runs.
"""

from __future__ import annotations

import json
import os
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, ClassVar

from .. import paths
from ..models import (
    Account,
    ActionBlocked,
    AuthRequired,
    ContentItem,
    DeleteResult,
    DeleteStatus,
    PermanentError,
    RateLimited,
    TransientError,
    VerifyStatus,
    from_iso,
    to_iso,
)
from .base import Adapter, ScanPage

_TEXTS = [
    "Vacation photos from the coast",
    "just got back from vacation",
    "Crypto is going to the moon",
    "Meeting notes for Monday",
    "Café au lait on the terrace",
    "Lunch was fine",
    "CRYPTO winter again",
    "Happy new year everyone",
    "Reading a good book about gardens",
    "Thoughts on the new release",
]


class FixtureAdapter(Adapter):
    platform: ClassVar[str] = "fixture"
    label: ClassVar[str] = "FIXTURE — fake data for development/tests, not a real platform"
    content_types: ClassVar[dict[str, str]] = {
        "post": "fake posts",
        "like": "fake likes",
        "repost": "fake reposts",
    }
    deletable_types: ClassVar[frozenset[str]] = frozenset({"post", "like", "repost"})
    discovery_notes: ClassVar[str] = (
        "FIXTURE data is entirely synthetic and generated locally; nothing real is read or "
        "deleted. It exists to exercise scanning, planning and deletion safely."
    )
    min_delete_interval: ClassVar[float] = 0.0

    def __init__(
        self,
        state_path: Path | None = None,
        faults: dict | None = None,
        page_size: int = 7,
        seed_accounts: tuple[str, ...] = ("alice", "bob"),
    ):
        self.state_path = Path(state_path) if state_path else paths.home() / "fixture" / "state.json"
        if faults is None:
            faults = json.loads(os.environ.get("UNPOST_FIXTURE_FAULTS") or "{}")
        self.faults: dict[str, Any] = faults
        self.page_size = page_size
        self.seed_accounts = tuple(seed_accounts)
        self._load()  # creates + seeds the state file if needed

    # ---------------- state ----------------

    def _load(self) -> dict[str, Any]:
        if self.state_path.exists():
            st = json.loads(self.state_path.read_text(encoding="utf-8"))
        else:
            st = {"logged_in": [], "accounts": {}, "counters": {}, "ok_deletes": 0,
                  "auth_fired": False, "delete_calls": []}
        changed = False
        for name in self.seed_accounts:
            aid = f"fixture:{name}"
            if aid not in st["accounts"]:
                st["accounts"][aid] = {"handle": f"{name}.fixture", "items": self._seed_items(name)}
                changed = True
        if changed or not self.state_path.exists():
            self._save(st)
        return st

    def _save(self, st: dict[str, Any]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_name(self.state_path.name + ".tmp")
        tmp.write_text(json.dumps(st, indent=1, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.state_path)

    @staticmethod
    def _seed_items(name: str) -> dict[str, dict[str, Any]]:
        """Deterministic content: 20 posts, 10 likes, 10 reposts, with awkward timestamps."""
        rng = random.Random(f"unpost-fixture:{name}")
        start = datetime(2019, 1, 1, tzinfo=timezone.utc)
        span = int((datetime(2024, 12, 31, 23, 59, 59, tzinfo=timezone.utc) - start).total_seconds())
        special: dict[tuple[str, int], datetime | None] = {
            ("post", 0): datetime(2021, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
            ("post", 1): datetime(2021, 12, 31, 23, 59, 59, 999999, tzinfo=timezone.utc),
            ("post", 2): datetime(2020, 12, 31, 23, 59, 59, tzinfo=timezone.utc),
            ("post", 3): datetime(2022, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
            ("post", 4): None,
            ("like", 0): None,
        }
        items: dict[str, dict[str, Any]] = {}
        for ctype, count in (("post", 20), ("like", 10), ("repost", 10)):
            for i in range(count):
                if (ctype, i) in special:
                    ts = special[(ctype, i)]
                else:
                    ts = start + timedelta(seconds=rng.randrange(span))
                item_id = f"{ctype}/{name}/{i:04d}"
                items[item_id] = {
                    "content_type": ctype,
                    "created_at": to_iso(ts),
                    "text": _TEXTS[(i + len(items)) % len(_TEXTS)],
                    "version": "v1",
                }
        return items

    # ---------------- test helpers ----------------

    def delete_calls(self) -> list[str]:
        return list(self._load()["delete_calls"])

    def present_ids(self, account_id: str) -> set[str]:
        return set(self._load()["accounts"][account_id]["items"])

    def edit_item(self, account_id: str, item_id: str, text: str) -> None:
        """Simulate the content changing on the platform after a plan was made."""
        st = self._load()
        rec = st["accounts"][account_id]["items"][item_id]
        rec["text"] = text
        rec["version"] = f"v{int(rec['version'][1:]) + 1}"
        self._save(st)

    def remove_item(self, account_id: str, item_id: str) -> None:
        """Simulate the user deleting the item elsewhere (not recorded in delete_calls)."""
        st = self._load()
        del st["accounts"][account_id]["items"][item_id]
        self._save(st)

    def add_item(self, account_id: str, content_type: str, text: str, created_at: datetime | None) -> str:
        """Simulate a new item appearing on the platform."""
        st = self._load()
        name = account_id.split(":", 1)[1]
        items = st["accounts"][account_id]["items"]
        n = 1000 + sum(1 for k in items if k.startswith(f"{content_type}/{name}/"))
        item_id = f"{content_type}/{name}/{n:04d}"
        items[item_id] = {"content_type": content_type, "created_at": to_iso(created_at), "text": text, "version": "v1"}
        self._save(st)
        return item_id

    # ---------------- Adapter API ----------------

    def _account(self, st: dict[str, Any], account_id: str) -> Account:
        return Account(self.platform, account_id, st["accounts"][account_id]["handle"])

    def login(self, account_hint: str | None = None, method: str | None = None) -> Account:
        st = self._load()
        hint = (account_hint or "alice").strip()
        for aid, acct in st["accounts"].items():
            if hint in (aid, acct["handle"], aid.split(":", 1)[1]):
                if aid not in st["logged_in"]:
                    st["logged_in"].append(aid)
                    self._save(st)
                return self._account(st, aid)
        raise ValueError(f"unknown fixture account {hint!r}; known: {', '.join(st['accounts'])}")

    def logout(self, account_id: str) -> None:
        st = self._load()
        if account_id in st["logged_in"]:
            st["logged_in"].remove(account_id)
            self._save(st)

    def current_account(self, account_id: str) -> Account:
        st = self._load()
        if self.faults.get("auth_challenge"):
            raise AuthRequired("fixture: simulated login challenge")
        if account_id not in st["logged_in"] or account_id not in st["accounts"]:
            raise AuthRequired(f"fixture: {account_id} is not logged in")
        return self._account(st, account_id)

    def scan_page(self, account: Account, content_type: str, cursor: str | None) -> ScanPage:
        if self.faults.get("scan_blocked"):
            raise ActionBlocked("fixture: simulated soft block", cool_down=self.faults.get("action_blocked_cooldown", 3600))
        st = self._load()
        if account.account_id not in st["logged_in"]:
            raise AuthRequired(f"fixture: {account.account_id} is not logged in")
        recs = [(iid, r) for iid, r in st["accounts"][account.account_id]["items"].items()
                if r["content_type"] == content_type]
        # newest first; unknown timestamps last
        recs.sort(key=lambda x: (x[1]["created_at"] is None, _neg(x[1]["created_at"]), x[0]))
        offset = int(cursor) if cursor else 0
        start = max(0, offset - int(self.faults.get("page_overlap", 0))) if offset else 0
        chunk = recs[start : offset + self.page_size]
        items = [self._to_item(account.account_id, iid, r) for iid, r in chunk]
        nxt = offset + self.page_size
        if self.faults.get("stuck_cursor"):
            next_cursor: str | None = cursor or str(self.page_size)
        else:
            next_cursor = str(nxt) if nxt < len(recs) else None
        return ScanPage(items, next_cursor)

    def _to_item(self, account_id: str, item_id: str, rec: dict[str, Any]) -> ContentItem:
        return ContentItem(
            platform=self.platform,
            account_id=account_id,
            item_id=item_id,
            content_type=rec["content_type"],
            created_at=from_iso(rec["created_at"]),
            text=rec["text"],
            url=f"https://fixture.invalid/{item_id}",
            version=rec["version"],
            raw={"synthetic": True, **rec.get("raw", {})},
        )

    @staticmethod
    def _consume(st: dict[str, Any], kind: str, item_id: str, limit: int) -> bool:
        """True while fewer than `limit` calls have been counted for (kind, item)."""
        key = f"{kind}:{item_id}"
        n = st["counters"].get(key, 0)
        if n < limit:
            st["counters"][key] = n + 1
            return True
        return False

    def delete(self, item: ContentItem) -> DeleteResult:
        st = self._load()
        f = self.faults
        if item.item_id in f.get("action_blocked", []):
            raise ActionBlocked("fixture: simulated soft block", cool_down=f.get("action_blocked_cooldown", 3600))
        if item.account_id not in st["logged_in"]:
            raise AuthRequired(f"fixture: {item.account_id} is not logged in")
        k = f.get("auth_after")
        if k is not None and not st["auth_fired"] and st["ok_deletes"] >= k:
            st["auth_fired"] = True
            st["logged_in"].remove(item.account_id)  # the "session" expired
            self._save(st)
            raise AuthRequired("fixture: simulated session expiry")
        st["delete_calls"].append(item.item_id)
        iid = item.item_id
        if self._consume(st, "transient", iid, f.get("transient", {}).get(iid, 0)):
            self._save(st)
            raise TransientError("fixture: simulated network error")
        if self._consume(st, "rate_limit", iid, f.get("rate_limit", {}).get(iid, 0)):
            self._save(st)
            raise RateLimited("fixture: simulated rate limit", retry_after=5)
        if iid in f.get("transient_then_gone", []) and self._consume(st, "transient_then_gone", iid, 1):
            st["accounts"][item.account_id]["items"].pop(iid, None)
            self._save(st)
            raise TransientError("fixture: simulated lost response (delete was applied)")
        if iid in f.get("inaccessible", []):
            self._save(st)
            return DeleteResult(DeleteStatus.INACCESSIBLE, "fixture: not permitted")
        if iid in f.get("permanent", []):
            self._save(st)
            raise PermanentError("fixture: simulated permanent failure")
        items = st["accounts"][item.account_id]["items"]
        rec = items.get(iid)
        if rec is None:
            self._save(st)
            return DeleteResult(DeleteStatus.ALREADY_DELETED, "fixture: not found")
        if rec["version"] != item.version:
            self._save(st)
            return DeleteResult(DeleteStatus.CHANGED, "fixture: version differs")
        if iid in f.get("accept_but_keep", []):
            self._save(st)
            return DeleteResult(DeleteStatus.DELETED, "fixture: accepted (but kept)")
        del items[iid]
        st["ok_deletes"] += 1
        self._save(st)
        if iid in f.get("crash_after_delete", []):
            raise KeyboardInterrupt("fixture: simulated kill after request was sent")
        return DeleteResult(DeleteStatus.DELETED)

    def verify_deleted(self, item: ContentItem) -> VerifyStatus:
        if item.item_id in self.faults.get("verify_blocked", []):
            raise ActionBlocked("fixture: simulated soft block", cool_down=self.faults.get("action_blocked_cooldown", 3600))
        if item.item_id in self.faults.get("unverifiable", []):
            return VerifyStatus.UNKNOWN
        st = self._load()
        present = item.item_id in st["accounts"].get(item.account_id, {}).get("items", {})
        return VerifyStatus.PRESENT if present else VerifyStatus.GONE


def _neg(iso: str | None) -> str:
    """Sort key giving descending order for fixed-width ISO strings."""
    if iso is None:
        return ""
    return "".join(chr(0x10FFFF - ord(c)) for c in iso)


class DemoFixtureAdapter(FixtureAdapter):
    """Fixture plus a fake "dm" content type with conversation metadata (raw thread_id/thread_title).

    Only used by the TUI demo mode and its tests, so the plain fixture (and the CLI tests that count
    its items) is unchanged."""

    content_types: ClassVar[dict[str, str]] = {**FixtureAdapter.content_types, "dm": "fake direct messages you sent"}
    deletable_types: ClassVar[frozenset[str]] = FixtureAdapter.deletable_types | {"dm"}

    @staticmethod
    def _seed_items(name: str) -> dict[str, dict[str, Any]]:
        items = FixtureAdapter._seed_items(name)
        rng = random.Random(f"unpost-fixture-dm:{name}")
        start = datetime(2023, 1, 1, tzinfo=timezone.utc)
        for t, (title, n) in enumerate((("Book club", 6), ("Sam", 4), ("", 3))):
            tid = f"thread{t}"
            for i in range(n):
                ts = start + timedelta(days=30 * t + i, seconds=rng.randrange(3600))
                items[f"dm:{tid}:{i:03d}"] = {
                    "content_type": "dm", "created_at": to_iso(ts), "version": "v1",
                    "text": f"fake message {i} in {title or 'an untitled chat'}",
                    "raw": {"thread_id": tid, "thread_title": title, "sent_by_viewer": True},
                }
        return items
