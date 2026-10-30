"""UI-neutral logic shared by the terminal UI (sociyx.tui) and the local web UI (sociyx.web).

Nothing here imports Textual or Starlette. It holds the browse/selection model, the plain-words wording for
statuses and warnings, and small helpers that used to live inside TUI screens, so both front ends show the
same facts and apply the same safety rules. The engine itself (scanning, planning, executor, report) is
reused as is and is not wrapped here.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .executor import job_is_running
from .filters import Filter
from .models import Account, ActionBlocked, AuthRequired, ContentItem, Outcome, from_iso
from .redact import scrub
from .scanning import ScanSummary
from .store import Store

# (raw key identifying a group, raw key holding its display title). Generic on purpose: any adapter that
# records these keys in ContentItem.raw (Instagram DMs do) gets conversation grouping for free.
GROUP_KEYS = (("thread_id", "thread_title"),)


@dataclass(frozen=True)
class Group:
    key: str
    title: str
    count: int


def group_of(item: ContentItem) -> tuple[str, str] | None:
    """(group key, title) when the item's raw metadata says it belongs to a conversation."""
    for id_key, title_key in GROUP_KEYS:
        gid = item.raw.get(id_key)
        if gid:
            return str(gid), str(item.raw.get(title_key) or gid)
    return None


def one_line(text: str, n: int) -> str:
    t = " ".join(text.split())
    return t if len(t) <= n else t[: n - 1] + "…"


def plan_order(items: list[ContentItem]) -> list[ContentItem]:
    """Same order `filters.select` gives the CLI: oldest first, undated last."""
    return sorted(items, key=lambda i: (i.created_at is None, i.created_at.timestamp() if i.created_at else 0, i.item_id))


class BrowseModel:
    def __init__(self, items: list[ContentItem], deletable: frozenset[str] | set[str]):
        self.deletable = frozenset(deletable)
        self.items = sorted(items, key=lambda i: (i.created_at is None, -(i.created_at.timestamp() if i.created_at else 0), i.item_id))
        self.by_id = {i.item_id: i for i in self.items}
        self.selected: set[str] = set()
        self.ctype: str | None = None  # None = all types
        self.query = ""
        self.since: datetime | None = None
        self.until: datetime | None = None
        self.group: str | None = None
        self._visible: list[ContentItem] | None = None

    # ---------------- filtering ----------------

    def _invalidate(self) -> None:
        self._visible = None

    def set_type(self, ctype: str | None) -> None:
        self.ctype, self.group = ctype, None
        self._invalidate()

    def set_query(self, query: str) -> None:
        self.query = query.strip()
        self._invalidate()

    def set_dates(self, since: datetime | None, until: datetime | None) -> None:
        Filter(since=since, until=until)  # validates since <= until (raises ValueError)
        self.since, self.until = since, until
        self._invalidate()

    def set_group(self, key: str | None) -> None:
        self.group = key
        self._invalidate()

    def visible(self) -> list[ContentItem]:
        """Items passing the type tab, conversation, search and date filters, newest first."""
        if self._visible is None:
            flt = Filter(since=self.since, until=self.until, keywords=[self.query] if self.query else [])
            out = []
            for i in self.items:
                if self.ctype and i.content_type != self.ctype:
                    continue
                if self.group is not None and (group_of(i) or ("",))[0] != self.group:
                    continue
                if flt.matches(i):
                    out.append(i)
            self._visible = out
        return self._visible

    def groups(self) -> list[Group]:
        """Conversations present in the current type tab, biggest first (empty if nothing is grouped)."""
        counts: dict[str, list] = {}
        for i in self.items:
            if self.ctype and i.content_type != self.ctype:
                continue
            g = group_of(i)
            if g:
                entry = counts.setdefault(g[0], [g[1], 0])
                entry[1] += 1
        return sorted((Group(k, t, n) for k, (t, n) in counts.items()), key=lambda g: (-g.count, g.title))

    # ---------------- selection ----------------

    def can_select(self, item: ContentItem) -> bool:
        return item.content_type in self.deletable

    def toggle(self, item_id: str) -> bool:
        """Toggle one item; False if its type cannot be deleted on this platform."""
        item = self.by_id[item_id]
        if not self.can_select(item):
            return False
        self.selected ^= {item_id}
        return True

    def _apply(self, items: list[ContentItem], mode: str) -> tuple[set[str], int]:
        """Change selection for `items`; returns (changed ids, number skipped as not deletable)."""
        ok = [i.item_id for i in items if self.can_select(i)]
        skipped = len(items) - len(ok)
        if mode == "select":
            changed = {i for i in ok if i not in self.selected}
            self.selected |= changed
        elif mode == "deselect":
            changed = {i for i in ok if i in self.selected}
            self.selected -= changed
        else:  # invert
            changed = set(ok)
            self.selected ^= changed
        return changed, skipped

    def select_visible(self) -> tuple[set[str], int]:
        return self._apply(self.visible(), "select")

    def deselect_visible(self) -> tuple[set[str], int]:
        return self._apply(self.visible(), "deselect")

    def invert_visible(self) -> tuple[set[str], int]:
        return self._apply(self.visible(), "invert")

    def select_group(self, key: str) -> tuple[set[str], int]:
        """Every message of one conversation (within the type tab), ignoring search and dates."""
        members = [i for i in self.items if (group_of(i) or ("",))[0] == key
                   and (not self.ctype or i.content_type == self.ctype)]
        return self._apply(members, "select")

    def clear(self) -> None:
        self.selected.clear()

    def keep_existing(self, ids: set[str]) -> None:
        self.selected &= ids

    # ---------------- counts ----------------

    @property
    def hidden_selected(self) -> int:
        shown = {i.item_id for i in self.visible()}
        return len(self.selected - shown)

    def selected_items(self) -> list[ContentItem]:
        """Exactly the selected items in plan order (oldest first)."""
        return plan_order([self.by_id[i] for i in self.selected if i in self.by_id])


def selection_digest(items: list[ContentItem]) -> str:
    """Stable fingerprint of a selection; the web review form carries it so a changed selection is noticed."""
    h = hashlib.sha256()
    for i in sorted(items, key=lambda i: i.item_id):
        h.update(i.item_id.encode() + b"\0")
    return h.hexdigest()[:16]


# ---------------- account dashboard ----------------


def scan_label(row) -> str:
    if row is None:
        return "never scanned"
    status = {"complete": "complete", "failed": "FAILED"}.get(row["status"], "partial (resumable)")
    return f"{status}, {row['items_seen']} seen" + (f" ({row['error']})" if row["error"] else "")


def type_rows(store, cls, account: Account) -> list[tuple]:
    """(type, description, stored, date range, scan status) per content type, including unexpected stored types."""
    counts: dict[str, dict] = {}
    for r in store.item_counts(account.platform, account.account_id):
        c = counts.setdefault(r["content_type"], {"present": 0, "oldest": None, "newest": None})
        if r["state"] == "present":
            c["present"] += r["n"]
            c["oldest"] = min(filter(None, [c["oldest"], r["oldest"]]), default=None)
            c["newest"] = max(filter(None, [c["newest"], r["newest"]]), default=None)
    scans = {s["content_type"]: s for s in store.latest_scans(account.platform, account.account_id)}
    types = list(cls.content_types) + [t for t in counts if t not in cls.content_types]
    out = []
    for t in types:
        c = counts.get(t, {"present": 0, "oldest": None, "newest": None})
        span = f"{c['oldest'][:10]} .. {c['newest'][:10]}" if c["oldest"] and c["newest"] else "-"
        out.append((t, cls.content_types.get(t, ""), c["present"], span, scan_label(scans.get(t))))
    return out


def verify_session(adapter, account: Account) -> tuple[str, str, Account | None]:
    """Ask the platform whether the saved session still belongs to `account`.

    Returns (kind, message, live account) with kind in ok | mismatch | auth | blocked | error. The caller owns
    (and closes) the adapter. Messages are scrubbed."""
    try:
        live = adapter.current_account(account.account_id)
        if live.account_id != account.account_id:
            return "mismatch", "the saved session belongs to a different account; refusing to use it", None
        return "ok", "", live
    except AuthRequired as e:
        return "auth", scrub(str(e)), None
    except ActionBlocked as e:
        return "blocked", scrub(f"{e}. {e.guidance()}"), None
    except Exception as e:
        return "error", scrub(f"{type(e).__name__}: {e}"), None


# ---------------- scanning ----------------


def summary_line(s: ScanSummary) -> str:
    state = "stopped (resumable)" if s.stopped else "partial (resumable)" if s.partial else s.status
    line = f"{s.content_type}: {state}; pages={s.pages} seen={s.items_seen} new={s.new_items} missing={s.marked_missing}"
    return line + (f"; error: {s.error}" if s.error else "") + (f"; warning: {s.warning}" if s.warning else "")


# ---------------- review ----------------

TYPE_WARNINGS = {
    "dm": "Unsending a direct message removes it for everyone in the conversation, not just for you.",
    "like": "Removing a like cannot be undone by this tool; you would have to like the post again by hand.",
    "repost": "Removing a repost cannot be undone by this tool.",
    "post": "Deleted posts disappear from your profile and cannot be restored by this tool.",
}


def describe_selection(items: list[ContentItem]) -> tuple[str, list[str]]:
    """(headline fragment like '12 items in 2 conversations', per-type count lines)."""
    by_type: dict[str, int] = {}
    convos: set[str] = set()
    for i in items:
        by_type[i.content_type] = by_type.get(i.content_type, 0) + 1
        g = group_of(i)
        if g:
            convos.add(g[0])
    if convos:
        groups = f"{len(convos)} conversation{'s' if len(convos) != 1 else ''}"
    else:
        groups = f"{len(by_type)} content type{'s' if len(by_type) != 1 else ''}"
    return f"{len(items)} item{'s' if len(items) != 1 else ''} in {groups}", [f"{n} {t}" for t, n in sorted(by_type.items())]


def confirmation_phrase(n: int) -> str:
    """The exact text the user must type before anything is deleted (both front ends)."""
    return f"delete {n}"


# ---------------- run / results ----------------

LABELS = [("deleted_verified", "verified"), ("deleted_unverified", "unverified"), ("already_deleted", "already gone"),
          ("inaccessible", "inaccessible"), ("failed", "failed"), ("skipped", "skipped"),
          ("in_progress", "in flight"), ("pending", "pending")]

RESUMABLE = ("paused", "stopped_auth", "stopped_blocked", "running")


def status_message(job: dict | None) -> tuple[str, str]:
    """(plain-words explanation of the job status, CSS class)."""
    if not job:
        return "This plan has not been started.", "hint"
    status, err = job["status"], job.get("error") or ""
    if status == "completed":
        return "Job completed: every item reached a final outcome.", "ok"
    if status == "cancelled":
        return "Job cancelled. Remaining items were skipped; items already deleted stay deleted.", "hint"
    if status == "stopped_auth":
        return ("Your login expired or the platform asked for a challenge. Log in again (Home, open the account, "
                f"'Log in again'), then resume this job. {err}"), "error"
    if status == "stopped_blocked":
        until = from_iso(job.get("rate_limited_until"))
        when = f" Do not resume before {until.astimezone().strftime('%Y-%m-%d %H:%M %Z')}." if until else ""
        return ("The platform is restricting this account. Stop for now, resolve it in the official app or site, "
                f"and wait out the cool-down.{when} {err}"), "error"
    if "daily cap" in err:
        return (f"Paused on purpose: the daily limit on deletions was reached. Nothing is wrong. {err}"), "hint"
    return (f"Job {status}. {err}".strip() + " Nothing is lost; resume when ready."), "hint"


def unfinished_jobs(store: Store) -> list[dict]:
    """Jobs that can be resumed; a DB status of 'running' only counts if no process holds its lock."""
    out = []
    for j in store.list_jobs():
        if j["status"] not in RESUMABLE or (j["status"] == "running" and job_is_running(j["job_id"])):
            continue
        plan = store.get_plan(j["plan_id"])
        acct = store.get_account(plan.platform, plan.account_id)
        counts = store.outcome_counts(plan.plan_id)
        remaining = counts.get(Outcome.PENDING.value, 0) + counts.get(Outcome.IN_PROGRESS.value, 0)
        out.append({"job": j, "plan": plan, "handle": acct.handle if acct else plan.account_id,
                    "counts": counts, "remaining": remaining})
    return out


def counts_text(counts: dict[str, int]) -> str:
    return ", ".join(f"{n} {k.replace('_', ' ')}" for k, n in sorted(counts.items()) if n) or "nothing yet"


# ---------------- data export import ----------------


@dataclass
class ImportResult:
    found: int
    seen: int
    new: int


def import_export(store: Store, adapter, platform: str, path_text: str, account_id: str | None = None) -> ImportResult:
    """Import an unzipped platform data export into the local database (offline, adds records only).

    Shared by `sociyx import` and the web Import page. Raises ValueError with a user-facing message."""
    fn = getattr(adapter, "import_export", None)
    if fn is None:
        raise ValueError(f"platform {platform!r} does not support importing a data export")
    path = Path(path_text)
    if not path.exists():
        raise ValueError(f"export path {path_text!r} does not exist")
    if not path.is_dir():
        raise ValueError("export path must be an unzipped export directory")
    acct = None
    if account_id:
        known = store.get_account(platform, account_id)
        acct = known or Account(platform, account_id, "")
    items = list(fn(path, acct) if acct is not None else fn(path))
    if not items:
        return ImportResult(0, 0, 0)
    by_acct: dict[str, list] = {}
    for it in items:
        by_acct.setdefault(it.account_id, []).append(it)
    total_seen = total_new = 0
    for aid, subset in sorted(by_acct.items()):
        known = store.get_account(platform, aid)
        store.upsert_account(known or Account(platform, aid, ""), verified=False)  # keep a known handle
        seen, new = store.upsert_items(subset, None, source="export")
        total_seen += seen
        total_new += new
    return ImportResult(len(items), total_seen, total_new)
