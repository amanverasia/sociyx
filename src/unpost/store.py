"""SQLite persistence: discovered items, scans, deletion plans and jobs.

Account isolation is enforced by the schema, not just by queries: plan items
reference their plan *and* the plan's account through composite foreign keys,
so an item belonging to another account cannot be attached to a plan.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

from .models import Account, ContentItem, Outcome, from_iso, to_iso

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS accounts (
    platform        TEXT NOT NULL,
    account_id      TEXT NOT NULL,
    handle          TEXT NOT NULL DEFAULT '',
    last_verified_at TEXT,
    PRIMARY KEY (platform, account_id)
);

CREATE TABLE IF NOT EXISTS items (
    platform      TEXT NOT NULL,
    account_id    TEXT NOT NULL,
    item_id       TEXT NOT NULL,
    content_type  TEXT NOT NULL,
    created_at    TEXT,              -- fixed-width UTC ISO, NULL if unknown
    text          TEXT NOT NULL DEFAULT '',
    url           TEXT NOT NULL DEFAULT '',
    version       TEXT NOT NULL DEFAULT '',
    raw_json      TEXT NOT NULL DEFAULT '{}',
    fingerprint   TEXT NOT NULL,
    source        TEXT NOT NULL DEFAULT 'scan',
    state         TEXT NOT NULL DEFAULT 'present',  -- present | missing | deleted
    first_seen_at TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL,
    last_scan_id  INTEGER,
    PRIMARY KEY (platform, account_id, item_id),
    FOREIGN KEY (platform, account_id) REFERENCES accounts (platform, account_id)
);
CREATE INDEX IF NOT EXISTS items_by_type_time ON items (platform, account_id, content_type, created_at);

CREATE TABLE IF NOT EXISTS scans (
    scan_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    platform     TEXT NOT NULL,
    account_id   TEXT NOT NULL,
    content_type TEXT NOT NULL,
    status       TEXT NOT NULL,      -- running | complete | failed
    cursor       TEXT,               -- resume point (opaque, adapter-defined)
    pages        INTEGER NOT NULL DEFAULT 0,
    items_seen   INTEGER NOT NULL DEFAULT 0,
    new_items    INTEGER NOT NULL DEFAULT 0,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    error        TEXT,
    FOREIGN KEY (platform, account_id) REFERENCES accounts (platform, account_id)
);

CREATE TABLE IF NOT EXISTS plans (
    plan_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    platform     TEXT NOT NULL,
    account_id   TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    filters_json TEXT NOT NULL,
    item_count   INTEGER NOT NULL,
    digest       TEXT NOT NULL,
    export_path  TEXT,
    export_sha256 TEXT,
    UNIQUE (plan_id, platform, account_id),
    FOREIGN KEY (platform, account_id) REFERENCES accounts (platform, account_id)
);

CREATE TABLE IF NOT EXISTS plan_items (
    plan_id     INTEGER NOT NULL,
    platform    TEXT NOT NULL,
    account_id  TEXT NOT NULL,
    item_id     TEXT NOT NULL,
    position    INTEGER NOT NULL,
    fingerprint TEXT NOT NULL,       -- what the user previewed
    outcome     TEXT NOT NULL DEFAULT 'pending',
    attempts    INTEGER NOT NULL DEFAULT 0,
    request_may_have_landed INTEGER NOT NULL DEFAULT 0,  -- a request with unknown outcome was sent
    detail      TEXT NOT NULL DEFAULT '',
    updated_at  TEXT,
    PRIMARY KEY (plan_id, item_id),
    FOREIGN KEY (plan_id, platform, account_id) REFERENCES plans (plan_id, platform, account_id),
    FOREIGN KEY (platform, account_id, item_id) REFERENCES items (platform, account_id, item_id)
);

CREATE TABLE IF NOT EXISTS jobs (
    job_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id     INTEGER NOT NULL UNIQUE REFERENCES plans (plan_id),
    status      TEXT NOT NULL,       -- running | paused | stopped_auth | stopped_blocked | cancelled | completed
    control     TEXT NOT NULL DEFAULT '',  -- '' | pause | cancel  (set by another process)
    rate_limited_until TEXT,
    started_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    finished_at TEXT,
    error       TEXT
);

CREATE TABLE IF NOT EXISTS request_log (
    platform   TEXT NOT NULL,
    account_id TEXT NOT NULL,
    ts         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS request_log_idx ON request_log (platform, account_id, ts);

CREATE TABLE IF NOT EXISTS events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id   INTEGER REFERENCES jobs (job_id),
    ts       TEXT NOT NULL,
    level    TEXT NOT NULL,
    message  TEXT NOT NULL
);
"""


def now_iso() -> str:
    return to_iso(datetime.now(timezone.utc))  # type: ignore[return-value]


@dataclass
class Plan:
    plan_id: int
    platform: str
    account_id: str
    created_at: str
    filters: dict[str, Any]
    item_count: int
    digest: str
    export_path: str | None
    export_sha256: str | None

    @property
    def confirmation_token(self) -> str:
        """What the user must type to authorize this exact plan."""
        return f"{self.plan_id}:{self.account_id}:{self.item_count}:{self.digest[:8]}"


@dataclass
class Job:
    job_id: int
    plan_id: int
    status: str
    control: str
    rate_limited_until: str | None
    started_at: str
    updated_at: str
    finished_at: str | None
    error: str | None


@dataclass
class ScanState:
    scan_id: int
    platform: str
    account_id: str
    content_type: str
    status: str
    cursor: str | None
    pages: int
    items_seen: int
    new_items: int
    started_at: str
    finished_at: str | None
    error: str | None


class Store:
    def __init__(self, path: Path | str):
        self.path = str(path)
        self.conn = sqlite3.connect(self.path, isolation_level=None, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA busy_timeout = 30000")
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(plan_items)")}
        if "request_may_have_landed" not in cols:  # v1 -> v2
            self.conn.execute(
                "ALTER TABLE plan_items ADD COLUMN request_may_have_landed INTEGER NOT NULL DEFAULT 0"
            )
            self.conn.execute("UPDATE plan_items SET request_may_have_landed = 1 WHERE attempts > 0")
        self.conn.execute(
            "INSERT INTO meta (key, value) VALUES ('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(SCHEMA_VERSION),),
        )

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    # ---------------- accounts ----------------

    def upsert_account(self, account: Account, verified: bool = True) -> None:
        self.conn.execute(
            """INSERT INTO accounts (platform, account_id, handle, last_verified_at) VALUES (?, ?, ?, ?)
               ON CONFLICT (platform, account_id) DO UPDATE SET
                 handle = excluded.handle,
                 last_verified_at = COALESCE(excluded.last_verified_at, accounts.last_verified_at)""",
            (account.platform, account.account_id, account.handle, now_iso() if verified else None),
        )

    def get_account(self, platform: str, account_id: str) -> Account | None:
        row = self.conn.execute(
            "SELECT * FROM accounts WHERE platform = ? AND account_id = ?", (platform, account_id)
        ).fetchone()
        return Account(row["platform"], row["account_id"], row["handle"]) if row else None

    def list_accounts(self, platform: str | None = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM accounts" + (" WHERE platform = ?" if platform else "") + " ORDER BY platform, handle"
        return self.conn.execute(sql, (platform,) if platform else ()).fetchall()

    def resolve_account(self, platform: str, ref: str | None) -> Account:
        """Find a known account by id or handle. With ref=None, require exactly one."""
        rows = self.list_accounts(platform)
        if ref is not None:
            rows = [r for r in rows if ref in (r["account_id"], r["handle"])]
        if not rows:
            raise LookupError(f"no known {platform} account matching {ref!r}; run `unpost login {platform}` first")
        if len(rows) > 1:
            names = ", ".join(f"{r['handle']} ({r['account_id']})" for r in rows)
            raise LookupError(f"several {platform} accounts known ({names}); pass --account")
        r = rows[0]
        return Account(r["platform"], r["account_id"], r["handle"])

    # ---------------- items ----------------

    def upsert_items(self, items: Iterable[ContentItem], scan_id: int | None, source: str = "scan") -> tuple[int, int]:
        """Insert or refresh items. Returns (seen, new). Dedup key: (platform, account, item_id)."""
        seen = new = 0
        ts = now_iso()
        for it in items:
            seen += 1
            cur = self.conn.execute(
                """INSERT INTO items (platform, account_id, item_id, content_type, created_at, text, url,
                                      version, raw_json, fingerprint, source, state, first_seen_at,
                                      last_seen_at, last_scan_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'present', ?, ?, ?)
                   ON CONFLICT (platform, account_id, item_id) DO NOTHING""",
                (
                    it.platform, it.account_id, it.item_id, it.content_type, to_iso(it.created_at), it.text,
                    it.url, it.version, json.dumps(it.raw, ensure_ascii=False, default=str), it.fingerprint(),
                    source, ts, ts, scan_id,
                ),
            )
            if cur.rowcount:
                new += 1
            else:
                self.conn.execute(
                    """UPDATE items SET content_type = ?, created_at = ?, text = ?, url = ?, version = ?,
                              raw_json = ?, fingerprint = ?, state = 'present', last_seen_at = ?,
                              last_scan_id = COALESCE(?, last_scan_id)
                       WHERE platform = ? AND account_id = ? AND item_id = ?""",
                    (
                        it.content_type, to_iso(it.created_at), it.text, it.url, it.version,
                        json.dumps(it.raw, ensure_ascii=False, default=str), it.fingerprint(), ts, scan_id,
                        it.platform, it.account_id, it.item_id,
                    ),
                )
        return seen, new

    @staticmethod
    def row_to_item(row: sqlite3.Row) -> ContentItem:
        return ContentItem(
            platform=row["platform"],
            account_id=row["account_id"],
            item_id=row["item_id"],
            content_type=row["content_type"],
            created_at=from_iso(row["created_at"]),
            text=row["text"],
            url=row["url"],
            version=row["version"],
            raw=json.loads(row["raw_json"]),
        )

    def query_items(
        self,
        platform: str,
        account_id: str,
        content_types: Iterable[str] | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        states: Iterable[str] = ("present",),
    ) -> list[sqlite3.Row]:
        """Coarse SQL pre-filter; keyword matching happens in filters.py."""
        sql = ["SELECT * FROM items WHERE platform = ? AND account_id = ?"]
        args: list[Any] = [platform, account_id]
        states = list(states)
        if states:
            sql.append(f"AND state IN ({','.join('?' * len(states))})")
            args += states
        types = list(content_types or [])
        if types:
            sql.append(f"AND content_type IN ({','.join('?' * len(types))})")
            args += types
        if since is not None:
            sql.append("AND created_at IS NOT NULL AND created_at >= ?")
            args.append(to_iso(since))
        if until is not None:
            sql.append("AND created_at IS NOT NULL AND created_at <= ?")
            args.append(to_iso(until))
        sql.append("ORDER BY created_at IS NULL, created_at, item_id")
        return self.conn.execute(" ".join(sql), args).fetchall()

    def get_item_row(self, platform: str, account_id: str, item_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM items WHERE platform = ? AND account_id = ? AND item_id = ?",
            (platform, account_id, item_id),
        ).fetchone()

    def set_item_state(self, platform: str, account_id: str, item_id: str, state: str) -> None:
        self.conn.execute(
            "UPDATE items SET state = ? WHERE platform = ? AND account_id = ? AND item_id = ?",
            (state, platform, account_id, item_id),
        )

    def item_counts(self, platform: str, account_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT content_type, state, COUNT(*) AS n, MIN(created_at) AS oldest, MAX(created_at) AS newest
               FROM items WHERE platform = ? AND account_id = ?
               GROUP BY content_type, state ORDER BY content_type, state""",
            (platform, account_id),
        ).fetchall()

    # ---------------- scans ----------------

    def _scan(self, row: sqlite3.Row | None) -> ScanState | None:
        return ScanState(**dict(row)) if row else None

    def start_or_resume_scan(self, platform: str, account_id: str, content_type: str, fresh: bool) -> ScanState:
        """Resume the latest unfinished scan for this content type, unless fresh=True."""
        if not fresh:
            row = self.conn.execute(
                """SELECT * FROM scans WHERE platform = ? AND account_id = ? AND content_type = ?
                   AND status IN ('running', 'failed') ORDER BY scan_id DESC LIMIT 1""",
                (platform, account_id, content_type),
            ).fetchone()
            if row:
                self.conn.execute("UPDATE scans SET status = 'running', error = NULL WHERE scan_id = ?", (row["scan_id"],))
                return self.get_scan(row["scan_id"])  # type: ignore[return-value]
        # A fresh scan supersedes any unfinished ones.
        self.conn.execute(
            """UPDATE scans SET status = 'abandoned', finished_at = ?
               WHERE platform = ? AND account_id = ? AND content_type = ? AND status IN ('running', 'failed')""",
            (now_iso(), platform, account_id, content_type),
        )
        cur = self.conn.execute(
            "INSERT INTO scans (platform, account_id, content_type, status, started_at) VALUES (?, ?, ?, 'running', ?)",
            (platform, account_id, content_type, now_iso()),
        )
        return self.get_scan(cur.lastrowid)  # type: ignore[arg-type,return-value]

    def get_scan(self, scan_id: int) -> ScanState | None:
        return self._scan(self.conn.execute("SELECT * FROM scans WHERE scan_id = ?", (scan_id,)).fetchone())

    def record_scan_page(self, scan_id: int, cursor: str | None, seen: int, new: int) -> None:
        self.conn.execute(
            """UPDATE scans SET cursor = ?, pages = pages + 1, items_seen = items_seen + ?, new_items = new_items + ?
               WHERE scan_id = ?""",
            (cursor, seen, new, scan_id),
        )

    def finish_scan(self, scan_id: int, status: str, error: str | None = None) -> None:
        self.conn.execute(
            "UPDATE scans SET status = ?, error = ?, finished_at = ? WHERE scan_id = ?",
            (status, error, now_iso(), scan_id),
        )

    def mark_unseen_missing(self, scan: ScanState) -> int:
        """After a *complete* scan, items of that type not seen by it are no longer listed."""
        cur = self.conn.execute(
            """UPDATE items SET state = 'missing'
               WHERE platform = ? AND account_id = ? AND content_type = ? AND state = 'present'
                 AND source = 'scan' AND (last_scan_id IS NULL OR last_scan_id <> ?)""",
            (scan.platform, scan.account_id, scan.content_type, scan.scan_id),
        )
        return cur.rowcount

    def latest_scans(self, platform: str, account_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT s.* FROM scans s JOIN (
                   SELECT content_type, MAX(scan_id) AS id FROM scans
                   WHERE platform = ? AND account_id = ? GROUP BY content_type) m ON s.scan_id = m.id
               ORDER BY s.content_type""",
            (platform, account_id),
        ).fetchall()

    # ---------------- plans ----------------

    def create_plan(
        self, account: Account, filters: dict[str, Any], items: list[ContentItem], digest: str
    ) -> Plan:
        with self.tx():
            cur = self.conn.execute(
                """INSERT INTO plans (platform, account_id, created_at, filters_json, item_count, digest)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (account.platform, account.account_id, now_iso(), json.dumps(filters), len(items), digest),
            )
            plan_id = cur.lastrowid
            self.conn.executemany(
                """INSERT INTO plan_items (plan_id, platform, account_id, item_id, position, fingerprint)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                [
                    (plan_id, it.platform, it.account_id, it.item_id, i, it.fingerprint())
                    for i, it in enumerate(items)
                ],
            )
        return self.get_plan(plan_id)  # type: ignore[arg-type]

    def set_plan_export(self, plan_id: int, path: str, sha256: str) -> None:
        self.conn.execute("UPDATE plans SET export_path = ?, export_sha256 = ? WHERE plan_id = ?", (path, sha256, plan_id))

    def get_plan(self, plan_id: int) -> Plan:
        row = self.conn.execute("SELECT * FROM plans WHERE plan_id = ?", (plan_id,)).fetchone()
        if not row:
            raise LookupError(f"no plan {plan_id}")
        d = dict(row)
        d["filters"] = json.loads(d.pop("filters_json"))
        return Plan(**d)

    def list_plans(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT p.*, a.handle, j.job_id, j.status AS job_status FROM plans p
               JOIN accounts a USING (platform, account_id)
               LEFT JOIN jobs j ON j.plan_id = p.plan_id ORDER BY p.plan_id"""
        ).fetchall()

    def plan_items(self, plan_id: int, outcomes: Iterable[str] | None = None) -> list[sqlite3.Row]:
        sql = """SELECT pi.*, i.content_type, i.created_at, i.text, i.url, i.version, i.raw_json,
                        i.fingerprint AS current_fingerprint, i.state AS item_state
                 FROM plan_items pi
                 JOIN items i ON i.platform = pi.platform AND i.account_id = pi.account_id AND i.item_id = pi.item_id
                 WHERE pi.plan_id = ?"""
        args: list[Any] = [plan_id]
        outcomes = list(outcomes or [])
        if outcomes:
            sql += f" AND pi.outcome IN ({','.join('?' * len(outcomes))})"
            args += outcomes
        return self.conn.execute(sql + " ORDER BY pi.position", args).fetchall()

    def set_outcome(self, plan_id: int, item_id: str, outcome: Outcome, detail: str = "", bump_attempt: bool = False) -> None:
        """bump_attempt marks that a request with an unknown outcome is about to be sent."""
        self.conn.execute(
            """UPDATE plan_items SET outcome = ?, detail = ?, updated_at = ?, attempts = attempts + ?,
                      request_may_have_landed = request_may_have_landed OR ?
               WHERE plan_id = ? AND item_id = ?""",
            (outcome.value, detail, now_iso(), int(bump_attempt), int(bump_attempt), plan_id, item_id),
        )

    def outcome_counts(self, plan_id: int) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT outcome, COUNT(*) AS n FROM plan_items WHERE plan_id = ? GROUP BY outcome", (plan_id,)
        ).fetchall()
        return {r["outcome"]: r["n"] for r in rows}

    # ---------------- jobs ----------------

    def get_or_create_job(self, plan_id: int) -> Job:
        row = self.conn.execute("SELECT * FROM jobs WHERE plan_id = ?", (plan_id,)).fetchone()
        if row:
            return Job(**dict(row))
        ts = now_iso()
        cur = self.conn.execute(
            "INSERT INTO jobs (plan_id, status, started_at, updated_at) VALUES (?, 'paused', ?, ?)", (plan_id, ts, ts)
        )
        return self.get_job(cur.lastrowid)  # type: ignore[arg-type]

    def get_job(self, job_id: int) -> Job:
        row = self.conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        if not row:
            raise LookupError(f"no job {job_id}")
        return Job(**dict(row))

    def list_jobs(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM jobs ORDER BY job_id").fetchall()

    def update_job(self, job_id: int, **fields: Any) -> None:
        fields["updated_at"] = now_iso()
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(f"UPDATE jobs SET {cols} WHERE job_id = ?", (*fields.values(), job_id))

    def request_control(self, job_id: int, control: str) -> None:
        assert control in ("pause", "cancel", "")
        self.update_job(job_id, control=control)

    # ---------------- request log (daily caps) ----------------

    def log_request(self, platform: str, account_id: str, ts: str | None = None) -> None:
        """Record one write request for rolling-window caps (shared by all plans of the account)."""
        self.conn.execute(
            "INSERT INTO request_log (platform, account_id, ts) VALUES (?, ?, ?)",
            (platform, account_id, ts or now_iso()),
        )
        # housekeeping: nothing older than 7 days is ever consulted
        old = to_iso(datetime.now(timezone.utc) - timedelta(days=7))
        self.conn.execute("DELETE FROM request_log WHERE ts < ?", (old,))

    def count_requests_since(self, platform: str, account_id: str, since_iso: str) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM request_log WHERE platform = ? AND account_id = ? AND ts >= ?",
            (platform, account_id, since_iso),
        ).fetchone()[0]

    def request_times_since(self, platform: str, account_id: str, since_iso: str) -> list[str]:
        return [r[0] for r in self.conn.execute(
            "SELECT ts FROM request_log WHERE platform = ? AND account_id = ? AND ts >= ? ORDER BY ts",
            (platform, account_id, since_iso),
        )]

    def log_event(self, job_id: int | None, level: str, message: str) -> None:
        from .redact import scrub

        self.conn.execute(
            "INSERT INTO events (job_id, ts, level, message) VALUES (?, ?, ?, ?)",
            (job_id, now_iso(), level, scrub(message)),
        )

    def events(self, job_id: int, limit: int = 50) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM events WHERE job_id = ? ORDER BY event_id DESC LIMIT ?", (job_id, limit)
        ).fetchall()[::-1]
