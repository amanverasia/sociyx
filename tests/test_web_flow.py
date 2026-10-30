"""End-to-end flows of the web UI over the demo fixture accounts (scan, browse, review, run, results)."""

import json
import re
import threading

import pytest

from sociyx import paths
from sociyx.adapters.fixture import DemoFixtureAdapter
from sociyx.store import Store
from webhelpers import Env, ids_of

ALICE = "fixture:alice"


@pytest.fixture
def env():
    return Env()


def adapter():
    return DemoFixtureAdapter()


def plans():
    s = Store(paths.db_path())
    try:
        return s.list_plans()
    finally:
        s.close()


def selected_count(html):
    return int(re.search(r"<strong>(\d+) selected</strong>", html).group(1))


# ---------------- home / account ----------------


def test_home_lists_accounts_and_demo_banner(env):
    html = env.get("/").text
    assert "alice.fixture" in html and "bob.fixture" in html and "DEMO MODE" in html
    assert "/login" in html and "/import" in html and "/help" in html


def test_account_dashboard_and_verified_session(env):
    html = env.get(env.base()).text
    assert "FIXTURE: fake data" in html
    for t in ("post", "like", "repost", "dm"):
        assert f"<td>{t}</td>" in html
    v = env.get(f"{env.base()}/verify", htmx=True).text
    assert "Session verified" in v and "Start scan" in v


def test_auth_required_is_a_clear_message_not_a_scan_form():
    e = Env(faults={"auth_challenge": True})
    v = e.get(f"{e.base()}/verify", htmx=True).text
    assert "Not logged in" in v and "simulated login challenge" in v
    assert "Start scan" not in v and "Log in again" in v


def test_scan_with_page_limit_then_stop_flow():
    e = Env()
    base = e.base("fixture:bob")
    r = e.post(f"{base}/scan/start", {"type": "post", "pages": "1"})
    assert r.status_code == 200
    e.wait(lambda: "hx-trigger" not in e.get(f"{base}/scan/status", htmx=True).text)
    html = e.get(f"{base}/scan/status", htmx=True).text
    assert "post: partial (resumable)" in html and "pages=1" in html
    assert e.post(f"{base}/scan/start", {"pages": "0"}).status_code == 422
    assert e.post(f"{base}/scan/start", {"type": "bogus"}).status_code == 422


def test_scan_stop_button_stops_between_pages():
    e = Env()
    base = e.base("fixture:bob")
    gate = threading.Event()
    e.state.scan_options = {"sleep": lambda s: gate.wait(5)}  # scan_delay is 0 for fixture, so poke via hook below
    # the fixture has no scan delay, so hold the scan inside its first page via a slow adapter instead
    orig = e.factory

    class Slow(DemoFixtureAdapter):
        def scan_page(self, account, ctype, cursor):
            gate.wait(5)
            return super().scan_page(account, ctype, cursor)

    e.state.adapter_factory = lambda platform, **kw: Slow(page_size=3)
    assert e.post(f"{base}/scan/start", {"type": "post"}).status_code == 200
    r = e.post(f"{base}/scan/stop")
    assert "Stopping" in r.text
    gate.set()
    e.wait(lambda: "hx-trigger" not in e.get(f"{base}/scan/status", htmx=True).text)
    assert "stopped (resumable)" in e.get(f"{base}/scan/status", htmx=True).text
    e.state.adapter_factory = orig


# ---------------- browse and select ----------------


def test_browse_tabs_search_dates_and_selection_survives_filters(env):
    base = env.base()
    page = env.get(f"{base}/browse").text
    assert "<title>Browse" in page and "Select all" in page
    posts = env.get(f"{base}/browse?type=post", htmx=True).text
    assert len(ids_of(posts)) == 20 and "post/alice/0000" in posts
    assert "<title>" not in posts  # htmx request gets the partial only
    hit = env.get(f"{base}/browse?type=post&q=crypto", htmx=True).text
    assert 0 < len(ids_of(hit)) < 20
    dated = env.get(f"{base}/browse?type=post&since=2021-01-01&until=2021-12-31", htmx=True).text
    assert 0 < len(ids_of(dated)) < 20
    bad = env.get(f"{base}/browse?since=nonsense", htmx=True).text
    assert "Cannot use that date range" in bad
    backwards = env.get(f"{base}/browse?since=2022-01-01&until=2021-01-01", htmx=True).text
    assert "Cannot use that date range" in backwards

    one = ids_of(posts)[0]
    r = env.post(f"{base}/select/toggle", {"item_id": one, "on": "1"})
    assert selected_count(r.text) == 1
    other_tab = env.get(f"{base}/browse?type=like", htmx=True).text
    assert selected_count(other_tab) == 1 and "selected but hidden by filters" in other_tab
    r = env.post(f"{base}/select/toggle", {"item_id": one})  # unchecked: no "on"
    assert selected_count(r.text) == 0
    assert env.post(f"{base}/select/toggle", {"item_id": "nope", "on": "1"}).status_code == 404


def test_select_deselect_invert_clear_apply_to_the_filtered_set(env):
    base = env.base()
    r = env.post(f"{base}/select/visible", {"type": "like"})
    assert selected_count(r.text) == 10 and "Selected 10 item(s)" in r.text
    r = env.post(f"{base}/select/none", {"type": "like", "q": "vacation"})
    n_removed = int(re.search(r"Deselected (\d+)", r.text).group(1))
    assert selected_count(r.text) == 10 - n_removed
    r = env.post(f"{base}/select/invert", {"type": "like"})
    assert selected_count(r.text) == n_removed
    r = env.post(f"{base}/select/clear", {})
    assert selected_count(r.text) == 0 and "Cleared the whole selection" in r.text


def test_select_all_filtered_covers_every_page_not_only_the_shown_one(env, monkeypatch):
    monkeypatch.setattr("sociyx.web.app.PAGE_SIZE", 5)
    base = env.base()
    page1 = env.get(f"{base}/browse?type=post", htmx=True).text
    assert len(ids_of(page1)) == 5 and "Page 1 of 4" in page1 and "Next" in page1
    page4 = env.get(f"{base}/browse?type=post&page=4", htmx=True).text
    assert len(ids_of(page4)) == 5 and "Page 4 of 4" in page4 and "Next" not in page4
    assert len(ids_of(env.get(f"{base}/browse?type=post&page=99", htmx=True).text)) == 5  # clamped
    assert len(ids_of(env.get(f"{base}/browse?type=post&page=x", htmx=True).text)) == 5
    r = env.post(f"{base}/select/visible", {"type": "post"})
    assert selected_count(r.text) == 20


def test_whole_conversation_selection(env):
    base = env.base()
    html = env.get(f"{base}/browse?type=dm", htmx=True).text
    assert "Book club (6)" in html and "Sam (4)" in html and "thread2 (3)" in html
    key = re.search(r'hx-vals=\'\{"select_group": "([^"]+)"\}\'', html).group(1)
    assert key == "thread0"  # biggest first
    r = env.post(f"{base}/select/group", {"type": "dm", "select_group": key})
    assert selected_count(r.text) == 6 and "whole conversation" in r.text
    # conversation selection ignores a search that hides some of its messages
    r = env.post(f"{base}/select/group", {"type": "dm", "q": "zzz", "select_group": "thread1"})
    assert selected_count(r.text) == 10
    assert env.post(f"{base}/select/group", {"type": "dm", "select_group": "nope"}).status_code == 404
    one = env.get(f"{base}/browse?type=dm&group=thread1", htmx=True).text
    assert len(ids_of(one)) == 4


def test_unknown_select_action_is_404(env):
    assert env.post(f"{env.base()}/select/frobnicate", {}).status_code == 404


# ---------------- review: typed confirmation, exact items ----------------


def review_page(env, ctype="post", q=""):
    env.post(f"{env.base()}/select/clear", {})
    env.post(f"{env.base()}/select/visible", {"type": ctype, "q": q})
    page = env.get(f"{env.base()}/review")
    assert page.status_code == 200, page.text
    return page.text


def digest_of(html):
    return re.search(r'name="digest" value="([^"]+)"', html).group(1)


def test_review_without_selection_redirects_back(env):
    r = env.get(f"{env.base()}/review")
    assert r.status_code == 303 and r.headers["location"].endswith("/browse")


def test_wrong_phrase_runs_nothing_and_creates_no_plan(env):
    html = review_page(env, "post", "crypto")
    n = len(re.findall(r"<td>post/alice/", html))
    assert n >= 1 and f"<code>delete {n}</code>" in html
    for typed in ("", "delete", f"delete {n + 1}", f"DELETE {n}", f"delete  {n}", "delete 0", "yes"):
        r = env.post(f"{env.base()}/review/start", {"digest": digest_of(html), "phrase": typed}, htmx=False)
        assert r.status_code == 422, typed
        assert "not the confirmation phrase" in r.text
    r = env.post(f"{env.base()}/review/start", {"digest": digest_of(html)}, htmx=False)  # no phrase at all
    assert r.status_code == 422
    assert plans() == []
    assert adapter().delete_calls() == []
    assert len(adapter().present_ids(ALICE)) == 53
    assert not env.state.runs


def test_selection_changed_since_review_is_refused(env):
    html = review_page(env, "post", "crypto")
    n = len(re.findall(r"<td>post/alice/", html))
    env.post(f"{env.base()}/select/visible", {"type": "like"})  # selection grows in "another tab"
    r = env.post(f"{env.base()}/review/start", {"digest": digest_of(html), "phrase": f"delete {n}"}, htmx=False)
    assert r.status_code == 422 and "selection changed" in r.text
    assert plans() == [] and adapter().delete_calls() == []


def test_right_phrase_deletes_exactly_the_selected_items_and_results_report_it(env):
    html = review_page(env, "post", "crypto")
    chosen = set(re.findall(r"<td>(post/alice/\d+)</td>", html))
    n = len(chosen)
    before = adapter().present_ids(ALICE)
    assert chosen < before
    r = env.post(f"{env.base()}/review/start", {"digest": digest_of(html), "phrase": f"delete {n}"}, htmx=False)
    assert r.status_code == 303
    run_url = r.headers["location"]
    plan_id = int(run_url.rsplit("/", 1)[1])
    assert "Deleting" in env.get(run_url).text
    done = env.wait_run(plan_id)
    assert done.headers["hx-redirect"] == f"/results/{plan_id}"

    assert adapter().present_ids(ALICE) == before - chosen
    assert set(adapter().delete_calls()) == chosen
    store = Store(paths.db_path())
    try:
        assert {row["item_id"] for row in store.plan_items(plan_id)} == chosen
        plan = store.get_plan(plan_id)
        assert plan.item_count == n
        token = plan.confirmation_token
    finally:
        store.close()

    res = env.get(f"/results/{plan_id}").text
    assert "Deleted &amp; verified" in res and f"<strong>{n}</strong> Deleted &amp; verified" in res
    assert "Job completed" in res and "not evidence that the account contains no other content" in res
    assert "FIXTURE: fake data" in res
    for item in chosen:
        assert item in res
    # the selection was consumed by the plan: no accidental second plan
    assert selected_count(env.get(f"{env.base()}/browse").text) == 0
    assert env.get(f"{env.base()}/review").status_code == 303
    # the confirmation token is never sent to the browser
    for page in ("/", run_url, f"/results/{plan_id}", f"/run/{plan_id}/status", f"/resume/{plan_id}", f"{env.base()}/browse"):
        assert token not in env.get(page).text
    assert token not in json.dumps(dict(env.client.cookies))


def test_downloads_resolve_from_the_store(env):
    html = review_page(env, "repost")
    n = len(re.findall(r"<td>repost/alice/", html))
    plan_id = int(env.post(f"{env.base()}/review/start", {"digest": digest_of(html), "phrase": f"delete {n}"},
                           htmx=False).headers["location"].rsplit("/", 1)[1])
    env.wait_run(plan_id)
    exp = env.get(f"/plan/{plan_id}/export")
    assert exp.status_code == 200 and "attachment" in exp.headers["content-disposition"]
    assert len(json.loads(exp.text)["items"]) == n
    rep = env.get(f"/plan/{plan_id}/report/text")
    assert "not evidence" in rep.text and "attachment" in rep.headers["content-disposition"]
    assert env.get(f"/plan/{plan_id}/report/csv").text.startswith("section,item_id")
    assert env.get(f"/plan/{plan_id}/report/json").status_code == 200
    assert env.get(f"/plan/{plan_id}/report/../../etc/passwd").status_code == 404
    assert env.get(f"/plan/{plan_id}/report/exe").status_code == 404


def test_export_download_refuses_files_outside_the_exports_folder(env, tmp_path):
    html = review_page(env, "repost")
    n = len(re.findall(r"<td>repost/alice/", html))
    plan_id = int(env.post(f"{env.base()}/review/start", {"digest": digest_of(html), "phrase": f"delete {n}"},
                           htmx=False).headers["location"].rsplit("/", 1)[1])
    env.wait_run(plan_id)
    outside = tmp_path / "secret.txt"
    outside.write_text("top secret")
    store = Store(paths.db_path())
    store.set_plan_export(plan_id, str(outside), "x")
    store.close()
    assert env.get(f"/plan/{plan_id}/export").status_code == 404


def test_save_plan_without_deleting(env):
    html = review_page(env, "like")
    r = env.post(f"{env.base()}/review/save", {"digest": digest_of(html)}, htmx=False)
    assert r.status_code == 200 and "Nothing was deleted" in r.text
    assert len(plans()) == 1
    assert adapter().delete_calls() == [] and not env.state.runs


# ---------------- run controls ----------------


def blocking_env(hold_after=1):
    e = Env()
    gate = threading.Event()
    reached = threading.Event()
    seen = []

    def hook(item_id, outcome, detail):
        seen.append(item_id)
        if len(seen) == hold_after:
            reached.set()
            gate.wait(10)

    e.state.progress_hook = hook
    return e, gate, reached


def start_run(e, ctype="repost"):
    html = review_page(e, ctype)
    n = len(re.findall(rf"<td>{ctype}/alice/", html))
    r = e.post(f"{e.base()}/review/start", {"digest": digest_of(html), "phrase": f"delete {n}"}, htmx=False)
    return int(r.headers["location"].rsplit("/", 1)[1]), n


def test_pause_then_resume_with_confirmation_page():
    e, gate, reached = blocking_env()
    plan_id, n = start_run(e)
    assert reached.wait(10)
    assert "pause requested" in e.post(f"/run/{plan_id}/pause").text
    assert "Pause requested" in e.get(f"/run/{plan_id}/status").text
    gate.set()
    snap = e.wait_run_stopped(plan_id)
    assert snap["kind"] == "result" and snap["payload"] == "paused"
    res = e.get(f"/results/{plan_id}").text
    assert "Job paused" in res and "Resume..." in res
    assert "Unfinished jobs" in e.get("/").text
    confirm = e.get(f"/resume/{plan_id}").text
    assert "Resume this deletion job?" in confirm and "Remaining to delete" in confirm
    assert e.post(f"/resume/{plan_id}", htmx=False).status_code == 303
    e.wait_run(plan_id)
    assert "Job completed" in e.get(f"/results/{plan_id}").text
    assert len(adapter().present_ids(ALICE)) == 53 - n


def test_cancel_needs_confirmation_and_skips_the_rest():
    e, gate, reached = blocking_env()
    plan_id, n = start_run(e)
    assert reached.wait(10)
    assert "Cancel this job?" in e.get(f"/run/{plan_id}/cancel").text
    assert e.get(f"/run/{plan_id}/cancel?close=1").text == ""
    assert e.post(f"/run/{plan_id}/cancel", {}).status_code == 422
    assert e.post(f"/run/{plan_id}/cancel", {"confirm": "no"}).status_code == 422
    assert "cancel requested" in e.post(f"/run/{plan_id}/cancel", {"confirm": "yes"}).text
    gate.set()
    snap = e.wait_run_stopped(plan_id)
    assert snap["payload"] == "cancelled"
    res = e.get(f"/results/{plan_id}").text
    assert "Job cancelled" in res and "Skipped" in res
    assert "Resume..." not in res
    assert e.post(f"/resume/{plan_id}", htmx=False).status_code == 409


def test_run_page_marks_running_state_for_the_leave_guard():
    e, gate, reached = blocking_env()
    plan_id, n = start_run(e)
    assert reached.wait(10)
    assert 'data-running="true"' in e.get(f"/run/{plan_id}/status").text
    assert "app.js" in e.get(f"/run/{plan_id}").text
    gate.set()
    e.wait_run(plan_id)


def test_results_error_guidance_for_action_blocked():
    e = Env(faults={"action_blocked": ["repost/alice/0003"]})
    plan_id, n = start_run(e)
    e.wait(lambda: not e.state.runs[plan_id].running)
    res = e.get(f"/results/{plan_id}").text
    assert "Job status: stopped_blocked" in res and "restricting this account" in res
    assert "Do not resume before" in res


def test_results_error_guidance_for_expired_login():
    e = Env(faults={"auth_after": 2})
    plan_id, n = start_run(e)
    e.wait(lambda: not e.state.runs[plan_id].running)
    res = e.get(f"/results/{plan_id}").text
    assert "stopped_auth" in res and "Log in again" in res
    assert "Resume..." in res


def test_unfinished_job_in_a_new_server_is_resumable_from_home(env):
    e, gate, reached = blocking_env()
    plan_id, n = start_run(e)
    assert reached.wait(10)
    e.post(f"/run/{plan_id}/pause")
    gate.set()
    e.wait_run_stopped(plan_id)
    fresh = Env(seed=False)  # a new server process over the same data folder
    assert f"/resume/{plan_id}" in fresh.get("/").text
    assert fresh.get(f"/run/{plan_id}/status").headers.get("hx-redirect") == f"/results/{plan_id}"


# ---------------- login ----------------


def test_fixture_login_flow(env):
    page = env.get("/login").text
    assert "Fake account (demo)" in page and "csrf_token" in page
    r = env.post("/login/start", {"platform": "fixture", "method": "", "hint": "bob"}, htmx=False)
    assert r.status_code == 303
    token = r.headers["location"].rsplit("/", 1)[1]
    status = env.wait(lambda: (lambda t: t if "Logged in as" in t else None)(env.get(f"/login/{token}/status").text))
    assert "bob.fixture" in status and "Open the account" in status
    bad = env.post("/login/start", {"platform": "fixture", "hint": "mallory"}, htmx=False).headers["location"]
    t2 = bad.rsplit("/", 1)[1]
    msg = env.wait(lambda: (lambda t: t if "Login failed" in t else None)(env.get(f"/login/{t2}/status").text))
    assert "unknown fixture account" in msg


def test_login_input_validation(env):
    assert env.post("/login/start", {"platform": "nope"}, htmx=False).status_code == 422
    assert env.post("/login/start", {"platform": "instagram", "method": "evil"}, htmx=False).status_code == 422
    assert env.post("/login/start", {"platform": "bluesky", "hint": ""}, htmx=False).status_code == 422
    r = env.post("/login/start", {"platform": "instagram", "method": "password", "hint": "me"}, htmx=False)
    assert r.status_code == 422 and "username and password" in r.text
    assert env.get("/login/doesnotexist").status_code == 404


def test_instagram_login_lists_browser_method_first_with_preferred_note(env):
    html = env.get("/login?platform=instagram").text
    assert html.index('value="browser"') < html.index('value="password"')
    assert "preferred" in html and 'type="password"' in html and 'autocomplete="off"' in html
    assert 'value="browser" checked' in html


def test_instagram_password_login_never_stored_or_logged(caplog):
    from sociyx.adapters.base import Adapter

    seen = {}

    class PwStub(DemoFixtureAdapter):
        platform = "instagram"

        def __init__(self, prompt_secret=None, prompt_text=None, **kw):
            super().__init__()
            self.ask = prompt_secret

        def login(self, hint=None, method=None):
            seen["method"], seen["hint"] = method, hint
            seen["password"] = self.ask("Instagram password (hidden): ")
            try:
                seen["code"] = self.ask("2FA code (hidden): ")
            except Exception as e:
                seen["code_error"] = str(e)
                from sociyx.models import AuthRequired

                raise AuthRequired("Instagram wants a verification code") from None
            return super().login("alice")

    caplog.set_level(logging_debug())
    e = Env()
    e.state.adapter_factory = lambda platform, **kw: PwStub(**kw) if platform == "instagram" else DemoFixtureAdapter()
    secret_pw = "Sup3r-Secret-PW-9"
    r = e.post("/login/start", {"platform": "instagram", "method": "password", "hint": "alice",
                                "password": secret_pw}, htmx=False)
    assert r.status_code == 303 and secret_pw not in r.text
    token = r.headers["location"].rsplit("/", 1)[1]
    msg = e.wait(lambda: (lambda t: t if "not completed" in t else None)(e.get(f"/login/{token}/status").text))
    assert "verification code" in msg and "never bypasses a challenge" in msg
    assert seen["method"] == "password" and seen["hint"] == "alice" and seen["password"] == secret_pw
    assert "verification (2FA) code" in seen["code_error"]
    # with a code it succeeds
    r = e.post("/login/start", {"platform": "instagram", "method": "password", "hint": "alice", "password": secret_pw,
                                "code": "123456"}, htmx=False)
    t2 = r.headers["location"].rsplit("/", 1)[1]
    e.wait(lambda: "Logged in as" in e.get(f"/login/{t2}/status").text)
    assert seen["code"] == "123456"
    # nowhere: not in logs, pages, the job objects, or the database/state files
    server_logs = "\n".join(rec.getMessage() for rec in caplog.records if not rec.name.startswith("httpx"))
    assert secret_pw not in server_logs and "123456" not in server_logs
    for page in ("/", "/login", f"/login/{token}", f"/login/{token}/status", f"/login/{t2}/status"):
        assert secret_pw not in e.get(page).text
    for job in e.state.logins.values():
        assert not job._secrets and secret_pw not in repr(vars(job))
    for f in paths.home().rglob("*"):
        if f.is_file():
            assert secret_pw.encode() not in f.read_bytes(), f


def logging_debug():
    import logging

    return logging.DEBUG


# ---------------- help ----------------


def test_help_explains_limits_and_never_claims_a_complete_wipe(env):
    html = env.get("/help").text
    assert "does not guarantee a complete wipe" in html
    assert "cannot be undone" in html
    assert "FIXTURE" in html and "Platform" not in "" and "Instagram" in html
    for claim in ("completely erase", "guaranteed to remove everything", "wipes everything"):
        assert claim not in html
