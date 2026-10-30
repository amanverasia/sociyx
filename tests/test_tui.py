"""Guided TUI driven through Textual's pilot. Fixture adapter only: no real platform is ever contacted."""

import asyncio
import threading
from datetime import datetime, timezone

import pytest
from textual.widgets import Button, DataTable, Input, OptionList, Select, Static, Tabs

from unpost import paths
from unpost.adapters.fixture import DemoFixtureAdapter
from unpost.models import ContentItem
from unpost.scanning import scan_account
from unpost.store import Store
from unpost.tui.app import UnpostApp
from unpost.tui.dialogs import ConfirmDialog, DateRangeDialog, PlatformPicker
from unpost.tui.model import BrowseModel
from unpost.tui.screens.account import AccountScreen
from unpost.tui.screens.browse import BrowseScreen
from unpost.tui.screens.home import HomeScreen
from unpost.tui.screens.resume import ResumeScreen
from unpost.tui.screens.results import ResultsScreen
from unpost.tui.screens.review import ReviewScreen
from unpost.tui.screens.run import RunScreen
from unpost.tui.screens.scan import ScanScreen

ALICE = "fixture:alice"
NOSLEEP = lambda s: None  # noqa: E731


def make_app(faults=None, page_size=7, adapter_cls=DemoFixtureAdapter, **kw):
    return UnpostApp(demo=True, adapter_factory=lambda platform: adapter_cls(faults=faults or {}, page_size=page_size),
                     executor_options={"sleep": NOSLEEP, "min_interval": 0, "verify_delay": 0},
                     scan_options={"sleep": NOSLEEP}, **kw)


def drive(scenario, app=None, size=(150, 50)):
    async def go():
        a = app or make_app()
        async with a.run_test(size=size) as pilot:
            await pilot.pause()
            await scenario(a, pilot)

    asyncio.run(go())


async def until(pilot, cond, timeout=15.0):
    for _ in range(int(timeout / 0.05)):
        if cond():
            return
        await pilot.pause(0.05)
    raise AssertionError("condition not reached in time")


def seed(scan=True, who=("alice",)):
    """Accounts known + logged in (+ alice scanned) in the test home; returns a Store for assertions."""
    store = Store(paths.db_path())
    adapter = DemoFixtureAdapter()
    for w in who:
        acct = adapter.login(w)
        store.upsert_account(acct)
        if scan and w == "alice":
            scan_account(store, adapter, acct, sleep=NOSLEEP)
    return store


def adapter_state():
    return DemoFixtureAdapter()


async def open_browse(app, pilot):
    await until(pilot, lambda: isinstance(app.screen, HomeScreen))
    await pilot.press("enter")
    await until(pilot, lambda: isinstance(app.screen, AccountScreen))
    await pilot.press("b")
    await until(pilot, lambda: isinstance(app.screen, BrowseScreen) and app.screen.model is not None)
    return app.screen


async def click(pilot, selector):
    """Click after letting the layout settle (a click right after a screen switch can miss)."""
    await pilot.pause(0.1)
    await pilot.click(selector)


async def pick(pilot, screen, n):
    """Tick the first n rows of the table (space, down)."""
    screen.query_one("#items", DataTable).focus()
    for _ in range(n):
        await pilot.press("space", "down")


def status(screen):
    return str(screen.query_one("#status", Static).render())


# ---------------- model (pure) ----------------

def _item(i, ctype="post", text="hello", day=1, raw=None):
    return ContentItem("fixture", ALICE, i, ctype, datetime(2021, 1, day, tzinfo=timezone.utc), text, raw=raw or {})


def test_model_select_all_respects_search_dates_and_type_and_selection_persists():
    items = [_item("a", text="Crypto one", day=1), _item("b", text="crypto two", day=10), _item("c", text="lunch", day=10),
             _item("d", "like", "crypto like", 10), _item("e", "story", "crypto story", 10)]
    m = BrowseModel(items, {"post", "like"})
    m.set_type("post")
    m.set_query("CRYPTO")
    m.set_dates(datetime(2021, 1, 5, tzinfo=timezone.utc), None)
    assert [i.item_id for i in m.visible()] == ["b"]
    changed, skipped = m.select_visible()
    assert changed == {"b"} and skipped == 0
    m.set_query("")
    m.set_dates(None, None)
    m.set_query("lunch")
    assert m.selected == {"b"} and m.hidden_selected == 1  # persists while hidden
    m.select_visible()
    assert m.selected == {"b", "c"}
    m.set_type(None)
    m.set_query("crypto")
    changed, skipped = m.select_visible()
    assert skipped == 1 and "e" not in m.selected  # 'story' cannot be deleted
    m.invert_visible()
    assert m.selected == {"c"}
    assert [i.item_id for i in m.selected_items()] == ["c"]
    with pytest.raises(ValueError):
        m.set_dates(datetime(2022, 1, 1, tzinfo=timezone.utc), datetime(2021, 1, 1, tzinfo=timezone.utc))


def test_model_groups_generic_from_raw_keys():
    raw = lambda t, title: {"thread_id": t, "thread_title": title}  # noqa: E731
    items = [_item("1", "dm", "x", raw=raw("t1", "Sam")), _item("2", "dm", "y", raw=raw("t1", "Sam")),
             _item("3", "dm", "z", raw=raw("t2", "")), _item("4", "post")]
    m = BrowseModel(items, {"dm", "post"})
    assert [(g.title, g.count) for g in m.groups()] == [("Sam", 2), ("t2", 1)]
    m.set_group("t1")
    assert {i.item_id for i in m.visible()} == {"1", "2"}
    m.set_query("y")
    changed, _ = m.select_group("t1")  # whole conversation ignores the search
    assert changed == {"1", "2"}


# ---------------- home / login ----------------

def test_home_lists_accounts():
    seed(scan=False, who=("alice", "bob"))

    async def scenario(app, pilot):
        await until(pilot, lambda: isinstance(app.screen, HomeScreen))
        t = app.screen.query_one("#accounts", DataTable)
        assert t.row_count == 2
        assert "DEMO MODE" in str(app.screen.query(".banner").first().render())

    drive(scenario)


def test_add_account_flow_with_fixture():
    async def scenario(app, pilot):
        await until(pilot, lambda: isinstance(app.screen, HomeScreen))
        assert app.screen.query_one("#accounts", DataTable).row_count == 0
        await pilot.press("a")
        await until(pilot, lambda: isinstance(app.screen, PlatformPicker))
        names = [n for n, _ in app.platforms()]
        assert "fixture" in names  # shown because of demo mode
        app.screen.query_one("#hint", Input).value = "alice"
        ol = app.screen.query_one("#platforms", OptionList)
        ol.focus()
        ol.highlighted = names.index("fixture")
        await pilot.press("enter")
        await until(pilot, lambda: isinstance(app.screen, HomeScreen) and app.screen.query_one("#accounts", DataTable).row_count == 1)
        assert [r["handle"] for r in app.store.list_accounts()] == ["alice.fixture"]

    drive(scenario)


def test_fixture_hidden_outside_demo(monkeypatch):
    monkeypatch.delenv("UNPOST_TUI_DEMO", raising=False)
    assert "fixture" not in [n for n, _ in UnpostApp().platforms()]
    monkeypatch.setenv("UNPOST_TUI_DEMO", "1")
    assert "fixture" in [n for n, _ in UnpostApp().platforms()]


def test_login_errors_are_scrubbed(monkeypatch):
    seen = []

    async def scenario(app, pilot):
        monkeypatch.setattr(app, "notify", lambda msg, **kw: seen.append(msg))

        def boom(self, hint=None, method=None):
            raise RuntimeError('failed password="hunter2secret" access_token: abc123')

        monkeypatch.setattr(DemoFixtureAdapter, "login", boom)
        assert app.login_flow("fixture", "alice") is None

    drive(scenario)
    assert seen and "hunter2secret" not in seen[0] and "abc123" not in seen[0] and "REDACTED" in seen[0]


# ---------------- dashboard / scan ----------------

def test_scan_from_dashboard_populates_store():
    seed(scan=False)

    async def scenario(app, pilot):
        await until(pilot, lambda: isinstance(app.screen, HomeScreen))
        await pilot.press("enter")
        await until(pilot, lambda: isinstance(app.screen, AccountScreen) and not app.screen.query_one("#scan", Button).disabled)
        assert "Session verified" in str(app.screen.query_one("#session", Static).render())
        await pilot.press("s")
        await until(pilot, lambda: isinstance(app.screen, ScanScreen))
        await click(pilot, "#start")
        await until(pilot, lambda: "complete" in str(app.screen.query_one("#result", Static).render()))
        counts = {r["content_type"]: r["n"] for r in app.store.item_counts("fixture", ALICE) if r["state"] == "present"}
        assert counts == {"post": 20, "like": 10, "repost": 10, "dm": 13}
        await pilot.press("escape")
        await until(pilot, lambda: isinstance(app.screen, AccountScreen))
        rows = app.screen.query_one("#types", DataTable)
        assert rows.row_count == 4

    drive(scenario)


def test_expired_session_offers_login_again():
    seed(scan=False)
    DemoFixtureAdapter().logout(ALICE)

    async def scenario(app, pilot):
        await until(pilot, lambda: isinstance(app.screen, HomeScreen))
        await pilot.press("enter")
        await until(pilot, lambda: app.screen.query_one("#login", Button).display)
        assert app.screen.query_one("#scan", Button).disabled
        await click(pilot, "#login")
        await until(pilot, lambda: "Session verified" in str(app.screen.query_one("#session", Static).render()))
        assert not app.screen.query_one("#scan", Button).disabled

    drive(scenario)


def test_stop_button_leaves_scan_resumable_and_marks_nothing_missing():
    store = seed(scan=True)  # a complete earlier scan: 20 present posts
    gate = {"reached": threading.Event(), "release": threading.Event()}

    class Gated(DemoFixtureAdapter):
        def scan_page(self, account, content_type, cursor):
            page = super().scan_page(account, content_type, cursor)
            if cursor is None:  # first page of the run: hold the worker until the test pressed Stop
                gate["reached"].set()
                gate["release"].wait(10)
            return page

    async def scenario(app, pilot):
        await until(pilot, lambda: isinstance(app.screen, HomeScreen))
        await pilot.press("enter")
        await until(pilot, lambda: isinstance(app.screen, AccountScreen) and not app.screen.query_one("#scan", Button).disabled)
        await pilot.press("s")
        await until(pilot, lambda: isinstance(app.screen, ScanScreen))
        app.screen.query_one("#type", Select).value = "post"
        app.screen.query_one("#fresh").value = True
        await click(pilot, "#start")
        await until(pilot, gate["reached"].is_set)
        await click(pilot, "#stop")
        gate["release"].set()
        await until(pilot, lambda: "Stopped" in str(app.screen.query_one("#result", Static).render()))

    drive(scenario, make_app(page_size=3, adapter_cls=Gated))
    scan = store.latest_scans("fixture", ALICE)
    post = next(s for s in scan if s["content_type"] == "post")
    assert post["status"] == "running" and post["pages"] == 1
    states = {r["state"] for r in store.item_counts("fixture", ALICE)}
    assert states == {"present"}  # nothing marked missing by the interrupted fresh scan


# ---------------- browse ----------------

def test_browse_toggle_search_dates_tabs_and_persistence():
    seed()

    async def scenario(app, pilot):
        s = await open_browse(app, pilot)
        assert "0 selected / 53 shown / 53 total" in status(s)
        assert s.query_one("#review", Button).disabled
        s.query_one("#items", DataTable).focus()
        await pilot.press("space")
        assert "1 selected" in status(s) and not s.query_one("#review", Button).disabled
        await pilot.press("space")
        assert "0 selected" in status(s)
        # search is live and case-insensitive; 'a' selects only what is shown
        await pilot.press("slash")
        await pilot.press(*"CRYPTO")
        shown = s.model.visible()
        assert shown and all("crypto" in i.text.casefold() for i in shown)
        await pilot.press("enter")  # leave the search box
        await pilot.press("a")
        assert s.model.selected == {i.item_id for i in shown}
        # clearing the search keeps the selection and counts hidden ones
        s.query_one("#search", Input).value = ""
        await pilot.pause()
        assert len(s.model.selected) == len(shown) and "0 selected but hidden" not in status(s)
        s.query_one("#search", Input).value = "zzzz-no-match"
        await pilot.pause()
        assert f"{len(shown)} selected but hidden by filters" in status(s)
        s.query_one("#search", Input).value = ""
        await pilot.pause()
        # date range through the dialog
        await pilot.press("x")
        assert not s.model.selected
        await pilot.press("d")
        await until(pilot, lambda: isinstance(app.screen, DateRangeDialog))
        app.screen.query_one("#since", Input).value = "not a date"
        await click(pilot, "#apply")
        assert "Cannot use that" in str(app.screen.query_one("#err", Static).render())
        app.screen.query_one("#since", Input).value = "2021-01-01"
        app.screen.query_one("#until", Input).value = "2021-12-31"
        app.screen.query_one("#until", Input).focus()
        await pilot.press("enter")  # submitting the form is the same as Apply
        await until(pilot, lambda: isinstance(app.screen, BrowseScreen))
        in_2021 = s.model.visible()
        assert in_2021 and all(i.created_at.year == 2021 for i in in_2021)
        # type tab + date + 'a'
        tabs = s.query_one("#tabs", Tabs)
        tabs.active = [t.id for t in tabs.query("Tab")][1]  # first real type tab: post
        await pilot.pause()
        s.query_one("#items", DataTable).focus()
        await pilot.press("a")
        assert s.model.selected and all(s.model.by_id[i].content_type == "post" and s.model.by_id[i].created_at.year == 2021
                                        for i in s.model.selected)
        await pilot.press("n")
        assert not s.model.selected
        await pilot.press("i")
        assert s.model.selected == {i.item_id for i in s.model.visible()}

    drive(scenario)


def test_browse_detail_and_conversations():
    seed()

    async def scenario(app, pilot):
        s = await open_browse(app, pilot)
        tabs = s.query_one("#tabs", Tabs)
        assert s.query_one("#groups").display  # conversations exist among the 'All' items too
        dm_tab = [t for t in tabs.query("Tab") if "dm" in str(t.label)][0]
        tabs.active = dm_tab.id
        await until(pilot, lambda: s.query_one("#groups").display)
        groups = s.query_one("#groups", OptionList)
        assert groups.option_count == 4  # all + 3 conversations
        s.query_one("#items", DataTable).focus()
        await pilot.press("c")  # no conversation picked yet: nothing selected
        assert not s.model.selected
        groups.focus()
        groups.highlighted = 1  # the biggest: 'Book club' with 6 messages
        await pilot.press("enter")
        await pilot.pause()
        assert len(s.model.visible()) == 6
        s.query_one("#items", DataTable).focus()
        await pilot.press("c")
        assert len(s.model.selected) == 6 and {s.model.by_id[i].raw["thread_id"] for i in s.model.selected} == {"thread0"}
        await pilot.press("enter")  # item detail
        await until(pilot, lambda: not isinstance(app.screen, BrowseScreen))
        texts = " ".join(str(w.render()) for w in app.screen.query(Static))
        assert "Conversation: Book club" in texts and "fake message" in texts
        await pilot.press("escape")

    drive(scenario)


# ---------------- review -> plan -> run -> results ----------------

async def to_review(app, pilot, n):
    s = await open_browse(app, pilot)
    await pick(pilot, s, n)
    ids = set(s.model.selected)
    assert len(ids) == n
    await pilot.press("r")
    await until(pilot, lambda: isinstance(app.screen, ReviewScreen))
    return ids


async def type_phrase(pilot, app, phrase):
    box = app.screen.query_one("#phrase", Input)
    box.value = phrase
    box.focus()
    await pilot.press("enter")
    await pilot.pause()


def test_review_wrong_phrases_do_nothing_then_exact_phrase_deletes_only_selected():
    store = seed()

    async def scenario(app, pilot):
        ids = await to_review(app, pilot, 3)
        review = app.screen
        text = " ".join(str(w.render()) for w in review.query(Static))
        assert "Unsend/delete 3 items" in text and "cannot be undone" in text and "FIXTURE" in text
        assert review.query_one("#items", DataTable).row_count == 3
        assert store.list_plans() == []  # no plan before the user proceeds
        for bad in ("", "delete", "delete 4", "DELETE 3", "delete 30", "yes"):
            await type_phrase(pilot, app, bad)
            assert isinstance(app.screen, ReviewScreen) and "not the confirmation phrase" in str(app.screen.query_one("#msg").render())
        assert store.list_plans() == [] and adapter_state().delete_calls() == []
        await type_phrase(pilot, app, "delete 3")
        await until(pilot, lambda: isinstance(app.screen, ResultsScreen))
        [plan] = store.list_plans()
        assert {r["item_id"] for r in store.plan_items(plan["plan_id"])} == ids
        assert plan["item_count"] == 3
        import os
        assert os.path.isfile(store.get_plan(plan["plan_id"]).export_path)
        assert set(adapter_state().delete_calls()) == ids and len(adapter_state().delete_calls()) == 3
        text = " ".join(str(w.render()) for w in app.screen.query(Static))
        assert "Job completed" in text and "Deleted & verified" in text and "This report covers only the 3 items" in text
        assert "FIXTURE" in text
        await click(pilot, "#home")
        await until(pilot, lambda: isinstance(app.screen, HomeScreen))

    drive(scenario)


def test_save_plan_without_deleting():
    store = seed()

    async def scenario(app, pilot):
        ids = await to_review(app, pilot, 2)
        await click(pilot, "#save")
        await pilot.pause()
        await pilot.pause()
        [plan] = store.list_plans()
        assert {r["item_id"] for r in store.plan_items(plan["plan_id"])} == ids
        assert adapter_state().delete_calls() == [] and store.list_jobs() == []
        await pilot.press("enter")
        await until(pilot, lambda: isinstance(app.screen, HomeScreen))

    drive(scenario)


def test_pause_mid_run_then_resume_from_home_without_double_deletes():
    store = seed()
    gate = {"reached": threading.Event(), "release": threading.Event()}
    count = {"n": 0}

    def hook(item_id, outcome, detail):
        count["n"] += 1
        if count["n"] == 2:
            gate["reached"].set()
            gate["release"].wait(10)

    async def scenario(app, pilot):
        app.progress_hook = hook
        ids = await to_review(app, pilot, 5)
        await type_phrase(pilot, app, "delete 5")
        await until(pilot, lambda: isinstance(app.screen, RunScreen))
        await until(pilot, gate["reached"].is_set)
        assert "2/5 done" in str(app.screen.query_one("#counts", Static).render())
        await click(pilot, "#pause")
        gate["release"].set()
        await until(pilot, lambda: isinstance(app.screen, ResultsScreen))
        plan_id = store.list_plans()[0]["plan_id"]
        assert store.get_job(store.list_jobs()[0]["job_id"]).status == "paused"
        assert store.outcome_counts(plan_id) == {"deleted_verified": 2, "pending": 3}
        text = " ".join(str(w.render()) for w in app.screen.query(Static))
        assert "paused" in text and "Not attempted (pending)" in text
        await click(pilot, "#home")
        await until(pilot, lambda: isinstance(app.screen, HomeScreen))
        await pilot.press("r")
        await until(pilot, lambda: isinstance(app.screen, ResumeScreen))
        await until(pilot, lambda: app.screen.query_one("#jobs", DataTable).row_count == 1)
        await pilot.press("enter")
        await until(pilot, lambda: isinstance(app.screen, ConfirmDialog))
        await click(pilot, "#yes")
        await until(pilot, lambda: isinstance(app.screen, ResultsScreen))
        assert store.outcome_counts(plan_id) == {"deleted_verified": 5}
        calls = adapter_state().delete_calls()
        assert sorted(calls) == sorted(ids) and len(calls) == 5  # no double deletes

    drive(scenario)


def test_quit_while_running_asks_to_pause_first():
    store = seed()
    gate = {"reached": threading.Event(), "release": threading.Event()}

    def hook(item_id, outcome, detail):
        gate["reached"].set()
        gate["release"].wait(10)

    async def scenario(app, pilot):
        app.progress_hook = hook
        await to_review(app, pilot, 4)
        await type_phrase(pilot, app, "delete 4")
        await until(pilot, gate["reached"].is_set)
        await pilot.press("ctrl+q")
        await until(pilot, lambda: isinstance(app.screen, ConfirmDialog))
        await click(pilot, "#no")  # keep running: the app must still be alive
        await until(pilot, lambda: isinstance(app.screen, RunScreen))
        await pilot.press("ctrl+q")
        await until(pilot, lambda: isinstance(app.screen, ConfirmDialog))
        await click(pilot, "#yes")
        gate["release"].set()
        await pilot.pause(0.5)

    drive(scenario)
    [job] = store.list_jobs()
    assert job["status"] == "paused" and len(adapter_state().delete_calls()) == 1


def test_stopped_auth_results_messaging():
    store = seed()

    async def scenario(app, pilot):
        await to_review(app, pilot, 3)
        await type_phrase(pilot, app, "delete 3")
        await until(pilot, lambda: isinstance(app.screen, ResultsScreen))
        text = " ".join(str(w.render()) for w in app.screen.query(Static))
        assert "Job status: stopped_auth" in text and "Log in again" in text and "expired" in text
        assert app.screen.query("#resume")

    drive(scenario, make_app(faults={"auth_after": 1}))
    assert len(adapter_state().delete_calls()) == 1


def test_stopped_blocked_results_messaging():
    store = seed()

    async def scenario(app, pilot):
        s = await open_browse(app, pilot)
        await pick(pilot, s, 2)
        await pilot.press("r")
        await until(pilot, lambda: isinstance(app.screen, ReviewScreen))
        await type_phrase(pilot, app, "delete 2")
        await until(pilot, lambda: isinstance(app.screen, ResultsScreen))
        text = " ".join(str(w.render()) for w in app.screen.query(Static))
        assert "Job status: stopped_blocked" in text and "restricting this account" in text
        assert "Do not resume before" in text
        # resuming inside the cool-down is refused with an explanation, nothing is deleted
        await click(pilot, "#resume")
        await until(pilot, lambda: isinstance(app.screen, ConfirmDialog))
        await click(pilot, "#yes")
        await until(pilot, lambda: isinstance(app.screen, RunScreen) and not app.screen.running)
        assert "Not started" in str(app.screen.query_one("#state", Static).render())

    drive(scenario, make_app(faults={"action_blocked": sorted(adapter_state().present_ids(ALICE))}))
    assert adapter_state().delete_calls() == []


def test_cancel_confirmation_and_cancel():
    store = seed()
    gate = {"reached": threading.Event(), "release": threading.Event()}

    def hook(item_id, outcome, detail):
        gate["reached"].set()
        gate["release"].wait(10)

    async def scenario(app, pilot):
        app.progress_hook = hook
        await to_review(app, pilot, 4)
        await type_phrase(pilot, app, "delete 4")
        await until(pilot, gate["reached"].is_set)
        await click(pilot, "#cancel")
        await until(pilot, lambda: isinstance(app.screen, ConfirmDialog))
        await click(pilot, "#yes")
        gate["release"].set()
        await until(pilot, lambda: isinstance(app.screen, ResultsScreen))
        assert store.get_job(store.list_jobs()[0]["job_id"]).status == "cancelled"
        assert "Job cancelled" in " ".join(str(w.render()) for w in app.screen.query(Static))

    drive(scenario)
    assert len(adapter_state().delete_calls()) == 1


def test_demo_home_is_isolated_and_seeded(monkeypatch, tmp_path):
    from unpost.tui.demo import prepare_demo_home

    real = tmp_path / "real"
    monkeypatch.setenv("UNPOST_HOME", str(real))
    monkeypatch.setenv("UNPOST_TUI_DEMO", "0")
    demo = prepare_demo_home()
    assert demo != real and not real.exists() and str(paths.home()) == str(demo)
    s = Store(paths.db_path())
    assert [r["handle"] for r in s.list_accounts()] == ["alice.fixture", "bob.fixture"]
    assert sum(r["n"] for r in s.item_counts("fixture", ALICE)) == 53
    s.close()
