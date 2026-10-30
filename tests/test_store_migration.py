from __future__ import annotations

import re
import sqlite3

from unpost import store as store_mod
from unpost.store import Store


def _v1_schema() -> str:
    s = store_mod.SCHEMA
    return re.sub(r"\n\s*request_may_have_landed[^\n]*", "", s)


def test_v1_db_is_migrated_with_data_preserved(tmp_path):
    path = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript(_v1_schema())
    conn.execute("INSERT INTO meta VALUES ('schema_version', '1')")
    cols = [r[1] for r in conn.execute("PRAGMA table_info(plan_items)")]
    assert "request_may_have_landed" not in cols
    conn.commit()
    conn.close()

    s = Store(path)
    try:
        cols = [r["name"] for r in s.conn.execute("PRAGMA table_info(plan_items)")]
        assert "request_may_have_landed" in cols
        v = s.conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
        assert v == str(store_mod.SCHEMA_VERSION) == "2"
    finally:
        s.close()
    Store(path).close()  # idempotent


def test_v1_rows_survive_and_attempted_rows_flagged(tmp_path):
    path = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.executescript(_v1_schema())
    conn.executemany(
        "INSERT INTO plan_items (plan_id, platform, account_id, item_id, position, fingerprint, attempts, detail)"
        " VALUES (1,'p','a',?,?,'f',?,'keep me')",
        [("x", 0, 0), ("y", 1, 2)],
    )
    conn.commit()
    conn.close()
    s = Store(path)
    try:
        rows = {r["item_id"]: r for r in s.conn.execute("SELECT * FROM plan_items")}
        assert rows["x"]["detail"] == "keep me" and rows["x"]["request_may_have_landed"] == 0
        assert rows["y"]["attempts"] == 2 and rows["y"]["request_may_have_landed"] == 1
    finally:
        s.close()
