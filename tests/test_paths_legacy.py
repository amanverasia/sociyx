from __future__ import annotations

import stat

from sociyx import paths


def _clean(monkeypatch, tmp_path):
    monkeypatch.delenv("SOCIYX_HOME", raising=False)
    monkeypatch.delenv("UNPOST_HOME", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))


def test_legacy_env_var_honored(monkeypatch, tmp_path):
    _clean(monkeypatch, tmp_path)
    monkeypatch.setenv("UNPOST_HOME", str(tmp_path / "old"))
    assert paths.home() == tmp_path / "old"
    monkeypatch.setenv("SOCIYX_HOME", str(tmp_path / "new"))
    assert paths.home() == tmp_path / "new"


def test_legacy_default_dir_used_only_if_new_missing(monkeypatch, tmp_path):
    _clean(monkeypatch, tmp_path)
    legacy = tmp_path / "xdg" / "unpost"
    legacy.mkdir(parents=True)
    (legacy / "unpost.sqlite3").write_text("")
    assert paths.home() == legacy
    assert paths.db_path() == legacy / "unpost.sqlite3"
    assert stat.S_IMODE(legacy.stat().st_mode) == 0o700
    assert not (tmp_path / "xdg" / "sociyx").exists()  # nothing created or copied
    (tmp_path / "xdg" / "sociyx").mkdir()
    assert paths.home() == tmp_path / "xdg" / "sociyx"


def test_new_default_when_no_legacy(monkeypatch, tmp_path):
    _clean(monkeypatch, tmp_path)
    assert paths.home() == tmp_path / "xdg" / "sociyx"
    assert paths.db_path().name == "sociyx.sqlite3"
