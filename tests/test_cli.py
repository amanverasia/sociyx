import csv
import json
import os
import stat

import pytest

from sociyx.adapters.fixture import FixtureAdapter
from sociyx.cli import main


def cli(capsys, *argv):
    code = main(list(argv))
    cap = capsys.readouterr()
    return code, cap.out, cap.err


def token_from(out):
    for line in out.splitlines():
        if line.startswith("Confirmation token: "):
            return line.split(": ", 1)[1].strip()
    raise AssertionError("no token in output")


@pytest.fixture
def scanned(capsys):
    assert cli(capsys, "login", "fixture", "--account", "alice")[0] == 0
    code, out, err = cli(capsys, "scan", "fixture")
    assert code == 0 and "post: complete" in out
    return capsys


def test_platforms_and_banner(capsys):
    code, out, _ = cli(capsys, "platforms")
    assert code == 0 and "fixture" in out and "FIXTURE" in out
    code, out, err = cli(capsys, "login", "fixture")
    assert "FIXTURE ADAPTER: fake data" in err and "alice.fixture" in out


def test_list_and_export(scanned, tmp_path):
    capsys = scanned
    code, out, _ = cli(capsys, "list", "fixture", "--keyword", "vacation", "--type", "post", "--until", "2021-12-31")
    assert code == 0 and "Vacation photos" in out and "3 matching items" in out
    jpath, cpath = tmp_path / "o.json", tmp_path / "o.csv"
    assert cli(capsys, "export", "fixture", "--format", "json", "--out", str(jpath), "--type", "like")[0] == 0
    assert cli(capsys, "export", "fixture", "--format", "csv", "--out", str(cpath), "--type", "like")[0] == 0
    for p in (jpath, cpath):
        assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    data = json.loads(jpath.read_text())
    assert data["meta"]["count"] == 10 == len(data["items"]) and data["meta"]["tool"] == "sociyx"
    rows = list(csv.reader(cpath.open()))
    assert rows[0] == ["platform", "account_id", "item_id", "content_type", "created_at", "url", "text"]
    assert len(rows) == 11


def test_bad_filter_is_a_friendly_error(scanned):
    code, out, err = cli(scanned, "list", "fixture", "--since", "2022-01-01", "--until", "2021-01-01")
    assert code == 2 and "error: --since is after --until" in err
    code, out, err = cli(scanned, "list", "fixture", "--type", "story")
    assert code == 2 and "unknown content type" in err


def test_plan_dry_run_live_and_report(scanned):
    capsys = scanned
    code, out, _ = cli(capsys, "plan", "create", "fixture", "--type", "post", "--keyword", "crypto")
    assert code == 0
    token = token_from(out)
    adapter = FixtureAdapter()
    before = adapter.present_ids("fixture:alice")

    # dry run: no --live
    code, out, _ = cli(capsys, "plan", "execute", "1")
    assert code == 0 and "dry run: nothing was deleted" in out and token in out
    assert adapter.present_ids("fixture:alice") == before and adapter.delete_calls() == []

    # --live without / with wrong token is refused
    assert cli(capsys, "plan", "execute", "1", "--live")[0] == 2
    code, _, err = cli(capsys, "plan", "execute", "1", "--live", "--confirm", "wrong")
    assert code == 2 and "error:" in err
    assert adapter.delete_calls() == []

    code, out, _ = cli(capsys, "plan", "execute", "1", "--live", "--confirm", token)
    assert code == 0
    deleted = before - adapter.present_ids("fixture:alice")
    assert deleted and all(i.startswith("post/") for i in deleted)
    assert sorted(adapter.delete_calls()) == sorted(deleted)
    assert "Deleted & verified" in out and "covers only the" in out

    # a finished job cannot run again
    assert cli(capsys, "plan", "execute", "1", "--live", "--confirm", token)[0] == 2

    code, out, _ = cli(capsys, "report", "1")
    assert code == 0
    for heading in ("Deleted & verified", "Deleted but UNVERIFIED", "Already deleted", "Inaccessible",
                    "Failed (request failed)", "Skipped", "Not attempted (pending)", "Interrupted / unknown"):
        assert heading in out
    assert f"This report covers only the {len(deleted)} items in plan 1." in out
    assert "not evidence that the account contains no other content" in out
    assert "wiped" not in out.lower()
    code, out, _ = cli(capsys, "report", "1", "--format", "json")
    assert json.loads(out)["counts"]["deleted_verified"] == len(deleted)

    code, out, _ = cli(capsys, "job", "status", "1")
    assert code == 0 and "completed" in out
    assert cli(capsys, "plan", "list")[0] == 0 and cli(capsys, "job", "list")[0] == 0


def test_auth_stop_exit_code_and_resume(scanned, monkeypatch):
    capsys = scanned
    _, out, _ = cli(capsys, "plan", "create", "fixture", "--type", "like")
    token = token_from(out)
    monkeypatch.setenv("SOCIYX_FIXTURE_FAULTS", json.dumps({"auth_after": 4}))
    code, out, _ = cli(capsys, "plan", "execute", "1", "--live", "--confirm", token)
    assert code == 3 and "stopped_auth" in out and "Not attempted (pending)" in out
    monkeypatch.delenv("SOCIYX_FIXTURE_FAULTS")
    assert cli(capsys, "login", "fixture")[0] == 0
    code, out, _ = cli(capsys, "job", "resume", "1", "--confirm", token)
    assert code == 0
    assert len(FixtureAdapter().delete_calls()) == 10


def test_platforms_shows_pacing_profile(capsys):
    code, out, _ = cli(capsys, "platforms")
    assert code == 0 and "pacing:" in out and "min delete interval" in out and "daily cap none" in out


def test_new_flags_in_help(capsys):
    for argv in (["plan", "execute", "--help"], ["job", "resume", "--help"]):
        with pytest.raises(SystemExit):
            main(argv)
        out = capsys.readouterr().out
        assert "--daily-cap" in out and "--ignore-cooldown" in out


def test_scan_blocked_exits_3_with_guidance(capsys, monkeypatch):
    assert cli(capsys, "login", "fixture", "--account", "alice")[0] == 0
    monkeypatch.setenv("SOCIYX_FIXTURE_FAULTS", json.dumps({"scan_blocked": True}))
    code, out, err = cli(capsys, "scan", "fixture")
    assert code == 3 and "Stop automation" in err


def test_blocked_job_exit_3_status_and_resume_refused(scanned, monkeypatch):
    capsys = scanned
    code, out, _ = cli(capsys, "plan", "create", "fixture", "--type", "post")
    token = token_from(out)
    monkeypatch.setenv("SOCIYX_FIXTURE_FAULTS", json.dumps({"action_blocked": ["post/alice/0019", "post/alice/0000"]}))
    code, out, err = cli(capsys, "plan", "execute", "1", "--live", "--confirm", token, "--delay", "0")
    assert code == 3 and "stopped_blocked" in out
    code, out, _ = cli(capsys, "job", "status", "1")
    assert "cool-down" in out and "--ignore-cooldown" in out
    code, out, err = cli(capsys, "job", "resume", "1", "--confirm", token, "--delay", "0")
    assert code == 2 and "do not resume before" in err
    monkeypatch.delenv("SOCIYX_FIXTURE_FAULTS")
    code, out, err = cli(capsys, "job", "resume", "1", "--confirm", token, "--delay", "0", "--ignore-cooldown")
    assert code == 0


def _py(code, **kw):
    import subprocess
    import sys

    env = {**os.environ, "SOCIYX_HOME": os.environ["SOCIYX_HOME"]}
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, stdin=subprocess.DEVNULL,
                          env=env, timeout=60, **kw)


def test_bare_sociyx_without_tty_prints_help_without_importing_textual():
    r = _py("import sys; from sociyx.cli import main; rc = main([]); print('RC', rc, 'textual' in sys.modules)")
    assert r.returncode == 0, r.stderr
    assert "usage: sociyx" in r.stdout and "tui" in r.stdout and "RC 0 False" in r.stdout


def test_other_commands_do_not_import_textual():
    r = _py("import sys; from sociyx.cli import main; main(['platforms']); print('TEXTUAL', 'textual' in sys.modules)")
    assert "TEXTUAL False" in r.stdout


def test_tui_help_and_non_tty_refusal(capsys):
    with pytest.raises(SystemExit) as e:
        main(["tui", "--help"])
    assert e.value.code == 0
    out = capsys.readouterr().out
    assert "--demo" in out
    code, _, err = cli(capsys, "tui")
    assert code == 2 and "interactive terminal" in err


def test_bare_sociyx_on_a_tty_launches_the_tui(monkeypatch):
    import sys

    launched = []
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)
    monkeypatch.setattr("sociyx.tui.run_tui", lambda demo=False: launched.append(demo) or 0)
    assert main([]) == 0 and launched == [False]
