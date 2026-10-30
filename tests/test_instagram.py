"""Instagram adapter tests. No network: fake instagrapi clients are injected."""

from __future__ import annotations

import json
import os
import stat
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from unpost.adapters import adapter_class
from unpost.adapters.instagram import InstagramAdapter, iter_export_items
from unpost.adapters.instagram import errors as ierrors
from unpost.adapters.instagram import mapping as imapping
from unpost.adapters.instagram.session import (
    delete_settings,
    load_settings,
    save_settings,
)
from unpost.models import (
    Account,
    AuthRequired,
    ContentItem,
    DeleteStatus,
    PermanentError,
    RateLimited,
    VerifyStatus,
)

ACCT = Account("instagram", "111", "me")


def user(pk="111", username="me"):
    return SimpleNamespace(pk=pk, username=username)


def media(pk="100", owner="111", caption="hello", code="ABCdef12345", product="photo"):
    m = SimpleNamespace(
        pk=pk,
        id=f"{pk}_{owner}",
        user=user(owner, "me" if owner == "111" else "other"),
        caption_text=caption,
        taken_at=datetime(2024, 3, 1, 10, 0, 0, tzinfo=timezone.utc),
        code=code,
        product_type=product,
        comment_count=0,
    )
    return m


def make_adapter(client=None, **kw):
    kw.setdefault("prompt_text", lambda p: "me")
    kw.setdefault("prompt_secret", lambda p: "pw")
    kw.setdefault("say", lambda s: None)
    kw.setdefault("sleep", lambda s: None)
    if client is not None:
        kw["client_factory"] = lambda **_: client
    return InstagramAdapter(**kw)


def test_registry_and_metadata():
    cls = adapter_class("instagram")
    assert cls is InstagramAdapter
    assert cls.platform == "instagram"
    assert set(cls.deletable_types) <= set(cls.content_types)
    assert "post" in cls.content_types and "dm" in cls.content_types
    assert "comment_export" in cls.content_types
    assert cls.min_delete_interval >= 30.0
    assert cls.daily_write_cap == 200
    assert "UNOFFICIAL" in cls.discovery_notes or "unofficial" in cls.discovery_notes.lower()


def test_mapping_media_item_ids():
    m = media(pk="200", owner="111")
    item = imapping.media_item(m, ACCT, "post")
    assert item.platform == "instagram" and item.account_id == "111"
    assert item.item_id == "200_111"
    assert item.version != ""
    # likes/saved use pk-only ids so API scans and export imports collapse
    like = imapping.media_item(m, ACCT, "like")
    assert like.item_id == "like:200"
    saved = imapping.media_item(m, ACCT, "saved")
    assert saved.item_id == "saved:200"


def test_scan_post_filters_clips_and_foreign():
    own_photo = media(pk="1", owner="111", product="photo")
    own_clip = media(pk="2", owner="111", product="clips")
    foreign = media(pk="3", owner="999", product="photo")
    client = SimpleNamespace(
        user_medias_paginated=lambda uid, amount, cur="": ([own_photo, own_clip, foreign], "next1")
    )
    ad = make_adapter()
    page = ad._scan_post(client, ACCT, {})
    ids = [i.item_id for i in page.items]
    assert "1_111" in ids
    assert all("2_111" not in i for i in ids)  # clips excluded from post scan
    assert all("3_999" not in i for i in ids)  # foreign skipped


def test_scan_like_and_saved():
    m = media(pk="50", owner="999")
    client = SimpleNamespace(
        collection_medias_v1_chunk=lambda which, cur="": ([m], None)
    )
    ad = make_adapter()
    like_page = ad._scan_like(client, ACCT, {})
    assert like_page.items[0].item_id == "like:50"
    assert like_page.next_cursor is None
    saved_page = ad._scan_saved(client, ACCT, {})
    assert saved_page.items[0].item_id == "saved:50"


def test_scan_page_unknown_type_raises():
    ad = make_adapter(client=SimpleNamespace())
    with pytest.raises(PermanentError):
        ad.scan_page(ACCT, "nope", None)


def test_scan_comment_export_returns_empty():
    ad = make_adapter(client=SimpleNamespace())
    page = ad.scan_page(ACCT, "comment_export", None)
    assert page.items == [] and page.next_cursor is None


def test_delete_media_success_changed_and_inaccessible():
    m = media(pk="100", owner="111", caption="hi")
    client = SimpleNamespace(
        media_info_v1=lambda pk: m,
        media_delete=lambda mid: True,
        get_settings=lambda: {"a": 1},
    )
    ad = make_adapter()
    ad._clients["111"] = client
    ad._saved["111"] = "{}"
    item = imapping.media_item(m, ACCT, "post")
    res = ad.delete(item)
    assert res.status == DeleteStatus.DELETED

    # changed caption -> CHANGED
    m2 = media(pk="100", owner="111", caption="edited!")
    client.media_info_v1 = lambda pk: m2
    res2 = ad.delete(item)
    assert res2.status == DeleteStatus.CHANGED

    # foreign owner -> INACCESSIBLE
    m3 = media(pk="100", owner="999")
    client.media_info_v1 = lambda pk: m3
    res3 = ad.delete(item)
    assert res3.status == DeleteStatus.INACCESSIBLE


def test_delete_gone_maps_to_already_deleted():
    from instagrapi import exceptions as ig

    def gone(pk):
        raise ig.MediaNotFound("gone")

    client = SimpleNamespace(media_info_v1=gone, get_settings=lambda: {"a": 1})
    ad = make_adapter()
    ad._clients["111"] = client
    ad._saved["111"] = "{}"
    item = ContentItem("instagram", "111", "100_111", "post", None, "", "", "", {"media_id": "100_111"})
    res = ad.delete(item)
    assert res.status == DeleteStatus.ALREADY_DELETED


def test_verify_like_gone_present_unknown():
    m_liked = SimpleNamespace(has_liked=True)
    m_unliked = SimpleNamespace(has_liked=False)
    ad = make_adapter()
    item = ContentItem("instagram", "111", "like:1", "like", None, "", "", "", {"media_pk": "1"})

    ad._clients["111"] = SimpleNamespace(media_info_v1=lambda pk: m_liked, get_settings=lambda: {"a": 1})
    assert ad.verify_deleted(item) == VerifyStatus.PRESENT
    ad._clients["111"] = SimpleNamespace(media_info_v1=lambda pk: m_unliked, get_settings=lambda: {"a": 1})
    assert ad.verify_deleted(item) == VerifyStatus.GONE


def test_error_mapping():
    from instagrapi import exceptions as ig

    assert isinstance(ierrors.translate(ig.MediaNotFound("x")), ierrors.Gone)
    assert isinstance(ierrors.translate(ig.FeedbackRequired("spam")), type(__import__("unpost.models", fromlist=["ActionBlocked"]).ActionBlocked("x")))
    auth = ierrors.translate(ig.LoginRequired("out"))
    assert isinstance(auth, AuthRequired)
    rl = ierrors.translate(ig.RateLimitError("slow"))
    assert isinstance(rl, RateLimited)
    assert rl.retry_after == 900.0
    trans = ierrors.translate(ig.ClientConnectionError("net"))
    from unpost.models import TransientError

    assert isinstance(trans, TransientError)


def test_session_roundtrip_mode_0600(tmp_path):
    base = tmp_path / "secrets"
    base.mkdir()
    save_settings("111", {"a": 1}, base)
    p = base / "111.json"
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    assert load_settings("111", base) == {"a": 1}
    delete_settings("111", base)
    assert not p.exists()
    assert load_settings("111", base) is None


def test_no_secrets_in_error_messages():
    ad = make_adapter()
    try:
        ad.scan_page(ACCT, "bogus_type_xyz", None)
    except PermanentError as e:
        assert "pw" not in str(e) and "SECRET" not in str(e)


def test_export_import_likes_saved_comments(tmp_path):
    # minimal export layout: liked post url with shortcode, saved, comment file
    # shortcode must decode: use known 11-char code
    code = "ABCdef12345"
    like_dir = tmp_path / "likes"
    like_dir.mkdir()
    (like_dir / "liked_posts.json").write_text(
        json.dumps({"likes_media_likes": [{"href": f"https://www.instagram.com/p/{code}/", "timestamp": 1700000000}]})
    )
    saved_dir = tmp_path
    (saved_dir / "saved_posts.json").write_text(
        json.dumps({"saved_saved_media": [{"href": f"https://www.instagram.com/reel/{code}/", "timestamp": 1700000001}]})
    )
    comments_dir = tmp_path / "comments"
    comments_dir.mkdir()
    (comments_dir / "post_comments.json").write_text(
        json.dumps(
            {
                "comments_media_comments": [
                    {
                        "string_map_data": {
                            "Comment": {"value": "nice!"},
                            "Media Owner": {"value": "otheruser"},
                            "Time": {"timestamp": 1700000002},
                        }
                    }
                ]
            }
        )
    )
    items = list(iter_export_items(tmp_path, ACCT))
    kinds = sorted(i.content_type for i in items)
    assert kinds == ["comment_export", "like", "saved"]
    cexp = [i for i in items if i.content_type == "comment_export"][0]
    assert cexp.text == "nice!" and cexp.raw["owner"] == "otheruser"


def test_dm_scan_lists_only_own_messages():
    raw_own = {"item_id": "m1", "user_id": "111", "item_type": "text", "text": "hi", "timestamp": "1700000000000000"}
    raw_other = {"item_id": "m2", "user_id": "999", "item_type": "text", "text": "yo", "timestamp": "1700000000000000"}
    thread = SimpleNamespace(id="T1", thread_title="chat")
    client = SimpleNamespace(
        direct_threads_chunk=lambda cursor=None: ([thread], None),
    )
    ad = make_adapter(client=client)
    # stub _thread_page to return our raws
    ad._thread_page = lambda c, tid, cur: ([raw_own, raw_other], None)
    # first call loads inbox page, second call reads thread
    p1 = ad._scan_dm(client, ACCT, {})
    assert p1.items == []  # first call only loads thread ids
    import json as _json

    st = _json.loads(p1.next_cursor)
    p2 = ad._scan_dm(client, ACCT, st)
    assert len(p2.items) == 1 and p2.items[0].item_id == "dm:T1:m1"


def test_cli_import_export_offline(capsys, tmp_path):
    from unpost.cli import main

    exp = tmp_path / "export"
    exp.mkdir()
    (exp / "saved_posts.json").write_text(
        json.dumps({"saved_saved_media": [{"href": "https://www.instagram.com/p/ABCdef12345/", "timestamp": 1700000001}]})
    )

    def run(*argv):
        code = main(list(argv))
        cap = capsys.readouterr()
        return code, cap.out, cap.err

    code, out, err = run("import", "instagram", "--account", "111", str(exp))
    assert code == 0 and "imported 1 items (1 new)" in out

    code, out, _ = run("list", "instagram", "--account", "111", "--type", "saved")
    assert code == 0 and "1 matching items" in out

    # unsupported platform refuses cleanly
    code, out, err = run("import", "fixture", str(exp))
    assert code == 2 and "does not support importing" in err

    # missing path refuses cleanly
    code, out, err = run("import", "instagram", "--account", "111", str(exp / "nope"))
    assert code == 2 and "does not exist" in err


@pytest.mark.parametrize("state,sender,expected", [
    ("unknown", None, DeleteStatus.INACCESSIBLE),
    ("found", "999", DeleteStatus.INACCESSIBLE),
    ("absent", None, DeleteStatus.ALREADY_DELETED),
    ("found", "111", DeleteStatus.DELETED),
])
def test_dm_unsend_requires_verified_sender(state, sender, expected):
    writes = []
    client = SimpleNamespace(direct_message_delete=lambda *ids: writes.append(ids) or True)
    ad = make_adapter()
    ad._locate_dm = lambda *args: (state, {"user_id": sender} if sender else None)
    item = imapping.dm_item({"item_id": "m1", "timestamp": "1700000000000000"}, "T1", ACCT)
    result = ad._delete_dm(client, item)
    assert result.status == expected
    assert writes == ([("T1", "m1")] if expected == DeleteStatus.DELETED else [])


def test_cli_session_file_reaches_adapter(monkeypatch, tmp_path, capsys):
    from unpost import cli

    received = {}

    class FakeAdapter:
        def login(self, hint, method):
            received.update(hint=hint, method=method)
            return ACCT

        def close(self):
            pass

    def factory(platform, **options):
        received.update(platform=platform, **options)
        return FakeAdapter()

    monkeypatch.setattr(cli, "get_adapter", factory)
    path = str(tmp_path / "settings.json")
    assert cli.main(["login", "instagram", "--method", "sessionfile", "--session-file", path]) == 0
    assert received == {"platform": "instagram", "session_file": path, "hint": None, "method": "sessionfile"}
    assert cli.main(["login", "instagram", "--session-file", path]) == 2
    assert "requires instagram --method sessionfile" in capsys.readouterr().err


def test_cli_import_rejects_file(tmp_path, capsys):
    from unpost.cli import main

    path = tmp_path / "export.zip"
    path.write_bytes(b"not an extracted directory")
    assert main(["import", "instagram", "--account", "111", str(path)]) == 2
    assert "unzipped export directory" in capsys.readouterr().err
