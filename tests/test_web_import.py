"""Import page (Instagram data export) and server shutdown behaviour. Synthetic export, no network."""

import json
import threading

import pytest

from sociyx import paths
from sociyx.store import Store
from sociyx.web.app import shutdown_jobs
from webhelpers import Env
from test_web_flow import blocking_env, start_run

IG = "1111"


@pytest.fixture
def export_dir(tmp_path):
    d = tmp_path / "export"
    d.mkdir()
    (d / "saved_posts.json").write_text(json.dumps(
        {"saved_saved_media": [{"href": "https://www.instagram.com/p/ABCdef12345/", "timestamp": 1700000001}]}))
    (d / "comments").mkdir()
    (d / "comments" / "post_comments.json").write_text(json.dumps({"comments_media_comments": [{"string_map_data": {
        "Comment": {"value": "nice!"}, "Media Owner": {"value": "otheruser"}, "Time": {"timestamp": 1700000002}}}]}))
    return d


def test_import_page_imports_a_synthetic_export_and_makes_items_browsable(export_dir):
    e = Env()
    page = e.get("/import").text
    assert "Import an Instagram data export" in page and "unzip" in page
    r = e.post("/import", {"path": str(export_dir), "account": IG}, htmx=False)
    assert r.status_code == 200 and "Imported 2 items (2 new)" in r.text
    store = Store(paths.db_path())
    try:
        types = sorted(row["content_type"] for row in store.query_items("instagram", IG))
        assert types == ["comment_export", "saved"]
        assert store.get_account("instagram", IG) is not None
    finally:
        store.close()
    # the account is now known, so it can be opened and its imported items browsed
    again = e.post("/import", {"path": str(export_dir), "account": IG}, htmx=False)
    assert "Imported 2 items (0 new)" in again.text
    assert IG in e.get("/import").text
    browse = e.get(f"/a/instagram/{e.key(IG)}/browse?type=comment_export").text
    assert "nice!" in browse and "comment_export" in browse
    assert "<td>saved</td>" in e.get(f"/a/instagram/{e.key(IG)}/browse").text


def test_import_errors_are_clear(export_dir, tmp_path):
    e = Env()
    r = e.post("/import", {"path": str(tmp_path / "nope"), "account": IG}, htmx=False)
    assert r.status_code == 422 and "does not exist" in r.text
    f = tmp_path / "export.zip"
    f.write_bytes(b"PK")
    r = e.post("/import", {"path": str(f), "account": IG}, htmx=False)
    assert r.status_code == 422 and "unzipped export directory" in r.text
    r = e.post("/import", {"path": "", "account": IG}, htmx=False)
    assert r.status_code == 422 and "Enter the path" in r.text
    r = e.post("/import", {"path": str(export_dir)}, htmx=False)  # no stored Instagram session, no account given
    assert r.status_code == 422 and "pass the account explicitly" in r.text
    empty = tmp_path / "empty"
    empty.mkdir()
    r = e.post("/import", {"path": str(empty), "account": IG}, htmx=False)
    assert r.status_code == 200 and "Nothing importable" in r.text


def test_import_never_leaks_paths_as_html(export_dir, tmp_path):
    e = Env()
    evil = tmp_path / "<script>alert(1)</script>"
    r = e.post("/import", {"path": str(evil)}, htmx=False)
    assert "<script>alert(1)</script>" not in r.text


def test_server_shutdown_pauses_a_running_deletion_instead_of_abandoning_it():
    e, gate, reached = blocking_env()
    plan_id, n = start_run(e)
    assert reached.wait(10)
    threading.Timer(0.3, gate.set).start()
    shutdown_jobs(e.state, timeout=10)
    snap = e.state.runs[plan_id].snapshot()
    assert not snap["running"] and snap["payload"] == "paused"
    store = Store(paths.db_path())
    try:
        assert store.get_job(e.state.runs[plan_id].job_id).status == "paused"
    finally:
        store.close()
