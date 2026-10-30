"""The Starlette application: routes and views of the local web UI.

All handlers are plain (sync) functions, which Starlette runs in its thread pool, so each request opens its
own short-lived Store. Long work (scan, deletion, login) runs in the background threads of ``jobs``.
Access control (Host, session, Origin, CSRF, headers) lives in ``security.SecurityMiddleware``, not here.
"""

from __future__ import annotations

import base64
import re
import urllib.parse
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from jinja2 import Environment, FileSystemLoader, select_autoescape
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import Route

from .. import paths
from ..adapters import ADAPTERS
from ..executor import cancel_job, job_is_running, pause_job
from ..filters import parse_bound
from ..models import Account, AdapterError, Outcome, format_duration, to_iso
from ..planning import create_plan_from_items, describe_pacing
from ..redact import scrub
from ..report import build_report, render_report
from ..service import (
    LABELS,
    RESUMABLE,
    TYPE_WARNINGS,
    BrowseModel,
    confirmation_phrase,
    counts_text,
    describe_selection,
    group_of,
    import_export,
    one_line,
    selection_digest,
    status_message,
    type_rows,
    unfinished_jobs,
    verify_session,
)
from ..store import Store
from .jobs import LoginJob, RunJob, ScanJob
from .security import SecurityMiddleware
from .state import Session, WebState

HERE = Path(__file__).parent
STATIC = HERE / "static"
PAGE_SIZE = 100
SECTION_ROWS = 300
FIXTURE_BANNER = "FIXTURE: fake data. Nothing real is touched."

# Login choices offered per platform: (method value, label, preferred). The first entry is the default.
LOGIN_METHODS = {
    "instagram": [("browser", "Browser session (preferred): a browser window opens on this computer and you log in yourself", True),
                  ("password", "Username and password (use this if the browser session is rejected)", False)],
    "bluesky": [("oauth", "Browser (OAuth): a browser window opens on this computer", True)],
    "mastodon": [("loopback", "Browser (OAuth): a browser window opens on this computer", True)],
    "fixture": [(None, "Fake account (demo)", True)],
}
HINT_LABELS = {"instagram": "Instagram username (needed for the password method)",
               "bluesky": "Bluesky handle (for example name.bsky.social)",
               "mastodon": "Mastodon account (@user@instance) or instance",
               "fixture": "Fake account name: alice or bob"}


def b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def unb64(key: str) -> str:
    try:
        return base64.urlsafe_b64decode(key + "=" * (-len(key) % 4)).decode()
    except Exception:  # noqa: BLE001
        raise HTTPException(404) from None


def fmt_date(dt: datetime | None) -> str:
    return dt.strftime("%Y-%m-%d %H:%M") if dt else "(unknown)"


class Web:
    """Per-application helpers bound to the WebState."""

    def __init__(self, state: WebState):
        self.state = state
        self.env = Environment(loader=FileSystemLoader(HERE / "templates"), autoescape=select_autoescape(["html"]),
                               trim_blocks=True, lstrip_blocks=True)
        self.env.globals.update(b64=b64, fmt_date=fmt_date, one_line=one_line,
                                format_duration=format_duration, FIXTURE_BANNER=FIXTURE_BANNER,
                                urlencode=urllib.parse.urlencode)

    @contextmanager
    def store(self) -> Iterator[Store]:
        s = self.state.new_store()
        try:
            yield s
        finally:
            s.close()

    def render(self, request: Request, name: str, status: int = 200, headers: dict | None = None, **ctx: Any) -> HTMLResponse:
        sess: Session = request.scope["sociyx.session"]
        html = self.env.get_template(name).render(csrf=sess.csrf, demo=self.state.demo, request_path=request.url.path, **ctx)
        return HTMLResponse(html, status_code=status, headers=headers)

    def account(self, store: Store, platform: str, key: str) -> Account:
        if platform not in ADAPTERS:
            raise HTTPException(404)
        acct = store.get_account(platform, unb64(key))
        if acct is None:
            raise HTTPException(404)
        return acct

    def plan_or_404(self, store: Store, plan_id: int):
        try:
            return store.get_plan(plan_id)
        except LookupError:
            raise HTTPException(404) from None

    def job_for_plan(self, store: Store, plan_id: int):
        return next((j for j in store.list_jobs() if j["plan_id"] == plan_id), None)

    def start_run(self, store: Store, plan_id: int) -> RunJob:
        plan = store.get_plan(plan_id)
        job = store.get_or_create_job(plan_id)
        with self.state._lock:
            existing = self.state.runs.get(plan_id)
            if existing is not None and existing.running:
                return existing
            run = RunJob(self.state, plan_id, job.job_id, plan.platform)
            self.state.runs[plan_id] = run
        run.start()
        return run


def create_app(state: WebState) -> Starlette:
    web = Web(state)

    def form(request: Request) -> dict[str, str]:
        return request.scope["sociyx.form"]

    def session(request: Request) -> Session:
        return request.scope["sociyx.session"]

    def see_other(url: str) -> Response:
        return RedirectResponse(url, status_code=303)

    def hx_redirect(url: str) -> Response:
        return Response(status_code=200, headers={"HX-Redirect": url})

    def base(acct: Account) -> str:
        return f"/a/{acct.platform}/{b64(acct.account_id)}"

    # ================= home / help =================

    def home(request: Request) -> Response:
        with web.store() as store:
            accounts = store.list_accounts()
            jobs = unfinished_jobs(store)
            plans = store.list_plans()
        running = [{"plan_id": p, "job": r.snapshot()} for p, r in state.runs.items() if r.running]
        return web.render(request, "home.html", accounts=accounts, jobs=jobs, plans=plans, running=running,
                          counts_text=counts_text)

    def help_page(request: Request) -> Response:
        notes = []
        for name in sorted(ADAPTERS):
            if name == "fixture" and not state.demo:
                continue
            try:
                cls = state.adapter_cls(name)
                notes.append({"name": name, "label": cls.label, "notes": cls.discovery_notes,
                              "pacing": describe_pacing(cls, 100)})
            except Exception as e:  # noqa: BLE001
                notes.append({"name": name, "label": name, "notes": f"unavailable: {type(e).__name__}", "pacing": ""})
        return web.render(request, "help.html", platforms=notes)

    # ================= login =================

    def login_page(request: Request) -> Response:
        platforms = [p for p in state.platforms() if p in LOGIN_METHODS]
        return web.render(request, "login.html", platforms=platforms, methods=LOGIN_METHODS, hints=HINT_LABELS,
                          prefill=request.query_params.get("hint", ""), only=request.query_params.get("platform", ""),
                          error="")

    def login_start(request: Request) -> Response:
        f = form(request)
        platform = f.get("platform", "")
        if platform not in LOGIN_METHODS or platform not in state.platforms():
            return PlainTextResponse("Unknown platform.", status_code=422)
        methods = [m for m, _, _ in LOGIN_METHODS[platform]]
        method = f.get("method") or methods[0]
        if method not in methods:
            return PlainTextResponse("Unknown login method.", status_code=422)
        hint = f.get("hint", "").strip()[:200] or None
        if platform in ("bluesky", "mastodon") and not hint:
            return web.render(request, "login.html", status=422, platforms=[p for p in state.platforms() if p in LOGIN_METHODS],
                              methods=LOGIN_METHODS, hints=HINT_LABELS, prefill="", only=platform,
                              error="Enter your handle or instance first.")
        password, code = f.get("password", ""), f.get("code", "").strip()
        if platform == "instagram" and method == "password" and not (hint and password):
            return web.render(request, "login.html", status=422, platforms=[p for p in state.platforms() if p in LOGIN_METHODS],
                              methods=LOGIN_METHODS, hints=HINT_LABELS, prefill=hint or "", only=platform,
                              error="The password method needs your username and password.")
        job = LoginJob(state, platform, method, hint, password if method == "password" else "", code)
        password = ""  # the only other reference is the job's private dict, cleared when the login call returns
        state.logins[job.token] = job
        job.start()
        return see_other(f"/login/{job.token}")

    def login_wait(request: Request) -> Response:
        token = request.path_params["token"]
        if token not in state.logins:
            raise HTTPException(404)
        return web.render(request, "login_wait.html", token=token, platform=state.logins[token].platform)

    def login_status(request: Request) -> Response:
        job = state.logins.get(request.path_params["token"])
        if job is None:
            raise HTTPException(404)
        snap = job.snapshot()
        ref = snap["ref"]
        url = f"/a/{ref[0]}/{b64(ref[1])}" if ref else "/"
        return web.render(request, "_login_status.html", snap=snap, token=request.path_params["token"], account_url=url)

    # ================= account dashboard / scan =================

    def account_page(request: Request) -> Response:
        with web.store() as store:
            acct = web.account(store, request.path_params["platform"], request.path_params["key"])
            cls = state.adapter_cls(acct.platform)
            rows = type_rows(store, cls, acct)
        scan = state.scans.get((acct.platform, acct.account_id))
        return web.render(request, "account.html", acct=acct, base=base(acct), rows=rows, cls=cls,
                          scan=scan.snapshot() if scan else None, scan_options=list(cls.content_types.items()),
                          scan_delay=cls.scan_delay)

    def verify(request: Request) -> Response:
        with web.store() as store:
            acct = web.account(store, request.path_params["platform"], request.path_params["key"])
            adapter = state.new_adapter(acct.platform)
            try:
                kind, msg, live = verify_session(adapter, acct)
            finally:
                adapter.close()
            if kind == "ok" and live is not None:
                store.upsert_account(live)  # refresh the handle, like the TUI
        cls = state.adapter_cls(acct.platform)
        return web.render(request, "_verify.html", acct=acct, base=base(acct), kind=kind, msg=msg, cls=cls,
                          scan=_scan_ctx(acct), scan_options=list(cls.content_types.items()),
                          scan_delay=cls.scan_delay, hint=urllib.parse.quote(acct.handle or ""))

    def _scan_ctx(acct: Account):
        job = state.scans.get((acct.platform, acct.account_id))
        return job.snapshot() if job else None

    def _scan_render(request: Request, acct: Account, status: int = 200, error: str = "") -> Response:
        cls = state.adapter_cls(acct.platform)
        return web.render(request, "_scan_status.html", status=status, acct=acct, base=base(acct), scan=_scan_ctx(acct),
                          scan_options=list(cls.content_types.items()), error=error)

    def scan_status(request: Request) -> Response:
        with web.store() as store:
            acct = web.account(store, request.path_params["platform"], request.path_params["key"])
        return _scan_render(request, acct)

    def scan_start(request: Request) -> Response:
        f = form(request)
        with web.store() as store:
            acct = web.account(store, request.path_params["platform"], request.path_params["key"])
        cls = state.adapter_cls(acct.platform)
        ctype = f.get("type", "")
        if ctype and ctype not in cls.content_types:
            return PlainTextResponse("Unknown content type.", status_code=422)
        pages_text = f.get("pages", "").strip()
        max_pages = None
        if pages_text:
            if not pages_text.isdigit() or int(pages_text) < 1 or int(pages_text) > 100000:
                return _scan_render(request, acct, 422, "Pages must be a whole number of 1 or more.")
            max_pages = int(pages_text)
        key = (acct.platform, acct.account_id)
        with state._lock:
            existing = state.scans.get(key)
            if existing is None or not existing.running:
                job = ScanJob(state, acct, [ctype] if ctype else None, f.get("fresh") == "1", max_pages)
                state.scans[key] = job
                job.start()
        return _scan_render(request, acct)

    def scan_stop(request: Request) -> Response:
        with web.store() as store:
            acct = web.account(store, request.path_params["platform"], request.path_params["key"])
        job = state.scans.get((acct.platform, acct.account_id))
        if job is not None:
            job.stop()
        return _scan_render(request, acct)

    # ================= browse & select =================

    def get_model(sess: Session, store: Store, acct: Account, cls, fresh: bool) -> BrowseModel:
        key = (acct.platform, acct.account_id)
        old = sess.models.get(key)
        if old is not None and not fresh:
            return old
        items = [Store.row_to_item(r) for r in store.query_items(acct.platform, acct.account_id)]
        model = BrowseModel(items, cls.deletable_types)
        if old is not None:  # keep what was picked, minus items that no longer exist
            model.selected = old.selected & set(model.by_id)
            model.query, model.since, model.until, model.ctype, model.group = (
                old.query, old.since, old.until, old.ctype, old.group)
            model.since_text, model.until_text = getattr(old, "since_text", ""), getattr(old, "until_text", "")
        sess.models[key] = model
        return model

    def tab_types(model: BrowseModel, cls) -> list[str]:
        present = {i.content_type for i in model.items}
        return [t for t in cls.content_types if t in present] + sorted(present - set(cls.content_types))

    def apply_filters(model: BrowseModel, cls, params) -> str:
        """Apply type/q/since/until/group from a query string or form. Returns an error text ('' if fine)."""
        types = tab_types(model, cls)
        ctype = params.get("type") or None
        model.set_type(ctype if ctype in types else None)
        model.set_query(params.get("q", ""))
        error = ""
        since_t, until_t = params.get("since", "").strip(), params.get("until", "").strip()
        try:
            lo = parse_bound(since_t, end=False) if since_t else None
            hi = parse_bound(until_t, end=True) if until_t else None
            model.set_dates(lo, hi)
            model.since_text, model.until_text = since_t, until_t
        except ValueError as e:
            error = f"Cannot use that date range: {e}"
        gk = params.get("group") or None
        model.set_group(gk if gk and any(g.key == gk for g in model.groups()) else None)
        return error

    def browse_ctx(acct: Account, cls, model: BrowseModel, params, notice: str = "", error: str = "") -> dict:
        visible = model.visible()
        try:
            page = max(1, int(params.get("page", "1")))
        except ValueError:
            page = 1
        pages = max(1, -(-len(visible) // PAGE_SIZE))
        page = min(page, pages)
        shown = visible[(page - 1) * PAGE_SIZE: page * PAGE_SIZE]
        rows = []
        for i in shown:
            g = group_of(i)
            rows.append({"id": i.item_id, "date": fmt_date(i.created_at), "type": i.content_type,
                         "text": one_line(i.text, 160), "full": i.text, "url": i.url,
                         "selectable": model.can_select(i), "selected": i.item_id in model.selected,
                         "convo": g[1] if g else ""})
        filt = {"type": model.ctype or "", "q": model.query, "since": getattr(model, "since_text", ""),
                "until": getattr(model, "until_text", ""), "group": model.group or ""}
        return {"acct": acct, "base": base(acct), "rows": rows, "filt": filt, "page": page, "pages": pages,
                "total": len(visible), "all_total": len(model.items), "nsel": len(model.selected),
                "hidden_sel": model.hidden_selected, "tabs": tab_types(model, cls), "groups": model.groups(),
                "notice": notice, "error": error, "page_size": PAGE_SIZE}

    def browse(request: Request) -> Response:
        sess = session(request)
        with web.store() as store:
            acct = web.account(store, request.path_params["platform"], request.path_params["key"])
            cls = state.adapter_cls(acct.platform)
            htmx = request.headers.get("hx-request") == "true"
            with sess.lock:
                model = get_model(sess, store, acct, cls, fresh=not htmx)
                error = apply_filters(model, cls, request.query_params)
                ctx = browse_ctx(acct, cls, model, request.query_params, error=error)
        return web.render(request, "_browse_body.html" if htmx else "browse.html", **ctx)

    SELECT_ACTIONS = {"visible", "none", "invert", "clear", "group", "toggle"}

    def select(request: Request) -> Response:
        action = request.path_params["action"]
        if action not in SELECT_ACTIONS:
            raise HTTPException(404)
        sess, f = session(request), form(request)
        with web.store() as store:
            acct = web.account(store, request.path_params["platform"], request.path_params["key"])
            cls = state.adapter_cls(acct.platform)
            with sess.lock:
                model = get_model(sess, store, acct, cls, fresh=False)
                if action == "toggle":
                    iid = f.get("item_id", "")
                    if iid not in model.by_id:
                        raise HTTPException(404)
                    want = f.get("on") == "1"
                    if not model.can_select(model.by_id[iid]):
                        want = False
                    if want:
                        model.selected.add(iid)
                    else:
                        model.selected.discard(iid)
                    ctx = browse_ctx(acct, cls, model, f)
                    return web.render(request, "_selstatus.html", **ctx)
                error = apply_filters(model, cls, f)
                notice = ""
                if action == "visible":
                    changed, skipped = model.select_visible()
                    notice = f"Selected {len(changed)} item(s)."
                elif action == "none":
                    changed, skipped = model.deselect_visible()
                    notice = f"Deselected {len(changed)} item(s)."
                elif action == "invert":
                    changed, skipped = model.invert_visible()
                    notice = f"Inverted {len(changed)} item(s)."
                elif action == "clear":
                    ids = len(model.selected)
                    model.clear()
                    changed, skipped = set(range(ids)), 0
                    notice = f"Cleared the whole selection ({ids} item(s))."
                else:
                    gk = f.get("select_group", "")
                    if not any(g.key == gk for g in model.groups()):
                        raise HTTPException(404)
                    changed, skipped = model.select_group(gk)
                    notice = f"Selected the whole conversation: {len(changed)} new item(s)."
                if skipped:
                    notice += f" {skipped} of a type that cannot be deleted here were left alone."
                ctx = browse_ctx(acct, cls, model, f, notice=notice, error=error)
        return web.render(request, "_browse_body.html", **ctx)

    # ================= review =================

    def _selection(sess: Session, acct: Account):
        with sess.lock:
            model = sess.models.get((acct.platform, acct.account_id))
            if model is None:
                return None, []
            return model, model.selected_items()

    def _review_ctx(acct: Account, cls, items, model, error: str = "") -> dict:
        headline, per_type = describe_selection(items)
        dates = sorted(i.created_at for i in items if i.created_at)
        undated = sum(1 for i in items if not i.created_at)
        span = f"{dates[0].date()} to {dates[-1].date()}" if dates else "no dates"
        warn = ["This cannot be undone. sociyx cannot restore what it deletes, and the platform may not either."]
        warn += [TYPE_WARNINGS[t] for t in sorted({i.content_type for i in items}) if t in TYPE_WARNINGS]
        return {"acct": acct, "base": base(acct), "headline": headline, "per_type": per_type, "span": span,
                "undated": undated, "pacing": describe_pacing(cls, len(items)), "warnings": warn,
                "phrase": confirmation_phrase(len(items)), "digest": selection_digest(items), "error": error,
                "items": [{"n": n, "date": fmt_date(i.created_at), "type": i.content_type, "id": i.item_id,
                           "text": one_line(i.text, 120)} for n, i in enumerate(items, 1)]}

    def review(request: Request) -> Response:
        sess = session(request)
        with web.store() as store:
            acct = web.account(store, request.path_params["platform"], request.path_params["key"])
        cls = state.adapter_cls(acct.platform)
        model, items = _selection(sess, acct)
        if not items:
            return see_other(f"{base(acct)}/browse")
        return web.render(request, "review.html", **_review_ctx(acct, cls, items, model))

    def _desc(model: BrowseModel, items) -> dict:
        return {"content_types": sorted({i.content_type for i in items}), "search": model.query,
                "since": to_iso(model.since), "until": to_iso(model.until),
                "conversation_filter": model.group is not None}

    def _freeze(request: Request, save_only: bool) -> Response:
        """Common to 'start' and 'save': validate, then freeze exactly the selected items into a plan."""
        sess, f = session(request), form(request)
        with web.store() as store:
            acct = web.account(store, request.path_params["platform"], request.path_params["key"])
            cls = state.adapter_cls(acct.platform)
            with sess.lock:
                model, items = _selection(sess, acct)
                if not items:
                    return see_other(f"{base(acct)}/browse")

                def refuse(msg: str) -> Response:
                    return web.render(request, "review.html", status=422, **_review_ctx(acct, cls, items, model, msg))

                if f.get("digest") != selection_digest(items):
                    return refuse("Your selection changed since this page was shown (maybe in another tab). "
                                  "Nothing was started. Check the list below and confirm again.")
                if not save_only:
                    phrase = confirmation_phrase(len(items))
                    if f.get("phrase", "").strip() != phrase:
                        return refuse(f"That is not the confirmation phrase. Type exactly: {phrase}")
                try:
                    plan = create_plan_from_items(store, cls, acct, items, _desc(model, items))
                except Exception as e:  # noqa: BLE001
                    return refuse(scrub(f"Could not create the plan: {e}"))
                if plan.item_count != len(items):
                    return refuse(f"Plan {plan.plan_id} holds {plan.item_count} items, not the {len(items)} you "
                                  "selected, so nothing was started. Check it with `sociyx plan show`.")
                model.selected -= {i.item_id for i in items}  # the plan is now the record; no accidental second plan
            if save_only:
                return web.render(request, "plan_saved.html", acct=acct, base=base(acct), plan=plan,
                                  export_dir=str(paths.home()))
            web.start_run(store, plan.plan_id)
        return see_other(f"/run/{plan.plan_id}")

    def review_start(request: Request) -> Response:
        return _freeze(request, save_only=False)

    def review_save(request: Request) -> Response:
        return _freeze(request, save_only=True)

    # ================= run =================

    def _run_context(store: Store, plan_id: int):
        plan = web.plan_or_404(store, plan_id)
        acct = store.get_account(plan.platform, plan.account_id)
        return plan, acct, web.job_for_plan(store, plan_id)

    def run_page(request: Request) -> Response:
        plan_id = int(request.path_params["plan_id"])
        with web.store() as store:
            plan, acct, job = _run_context(store, plan_id)
        return web.render(request, "run.html", plan=plan, acct=acct, job=job, plan_id=plan_id)

    def run_status(request: Request) -> Response:
        plan_id = int(request.path_params["plan_id"])
        with web.store() as store:
            plan, acct, job = _run_context(store, plan_id)
            counts = store.outcome_counts(plan_id)
            control = store.get_job(job["job_id"]).control if job else ""
        run = state.runs.get(plan_id)
        snap = run.snapshot() if run else None
        total = plan.item_count
        done = total - counts.get("pending", 0) - counts.get("in_progress", 0)
        running = bool(snap and snap["running"]) or bool(job and job["status"] == "running" and job_is_running(job["job_id"]))
        if not running and (snap is None or snap["kind"] == "result"):
            return hx_redirect(f"/results/{plan_id}")
        stopped_msg = ""
        if not running and snap:
            stopped_msg = (("Not started: " if snap["kind"] == "refused" else "Stopped unexpectedly: ") + snap["payload"]
                           + " Nothing further was deleted. You can resume from Home.")
        return web.render(request, "_run_status.html", plan_id=plan_id, total=total, done=done,
                          counts=[(label, counts.get(key, 0)) for key, label in LABELS], running=running,
                          snap=snap, stopped_msg=stopped_msg, pause_pending=control == "pause",
                          cancel_pending=control == "cancel")

    def run_pause(request: Request) -> Response:
        plan_id = int(request.path_params["plan_id"])
        with web.store() as store:
            _, _, job = _run_context(store, plan_id)
            if job is None:
                raise HTTPException(404)
            msg = pause_job(store, job["job_id"])
        return PlainTextResponse(msg)

    def run_cancel_confirm(request: Request) -> Response:
        plan_id = int(request.path_params["plan_id"])
        if request.query_params.get("close"):
            return HTMLResponse("")
        return web.render(request, "_cancel_confirm.html", plan_id=plan_id)

    def run_cancel(request: Request) -> Response:
        plan_id = int(request.path_params["plan_id"])
        if form(request).get("confirm") != "yes":
            return PlainTextResponse("Cancelling needs explicit confirmation.", status_code=422)
        with web.store() as store:
            _, _, job = _run_context(store, plan_id)
            if job is None:
                raise HTTPException(404)
            msg = cancel_job(store, job["job_id"])
        return web.render(request, "_cancel_done.html", msg=msg)

    def resume_page(request: Request) -> Response:
        plan_id = int(request.path_params["plan_id"])
        with web.store() as store:
            plan, acct, job = _run_context(store, plan_id)
            counts = store.outcome_counts(plan_id)
        remaining = counts.get(Outcome.PENDING.value, 0) + counts.get(Outcome.IN_PROGRESS.value, 0)
        resumable = bool(job and job["status"] in RESUMABLE)
        return web.render(request, "resume.html", plan=plan, acct=acct, job=job, remaining=remaining,
                          counts=counts_text(counts), resumable=resumable)

    def resume_start(request: Request) -> Response:
        plan_id = int(request.path_params["plan_id"])
        with web.store() as store:
            plan, acct, job = _run_context(store, plan_id)
            if not job or job["status"] not in RESUMABLE:
                return PlainTextResponse("This job cannot be resumed.", status_code=409)
            web.start_run(store, plan_id)
        return see_other(f"/run/{plan_id}")

    # ================= results / downloads =================

    def results(request: Request) -> Response:
        plan_id = int(request.path_params["plan_id"])
        with web.store() as store:
            web.plan_or_404(store, plan_id)
            rep = build_report(store, plan_id)
        job = rep["job"]
        msg, css = status_message(job)
        run = state.runs.get(plan_id)
        sections = [{"key": k, "title": s["title"], "n": len(s["items"]), "rows": s["items"][:SECTION_ROWS],
                     "more": max(0, len(s["items"]) - SECTION_ROWS)} for k, s in rep["sections"].items()]
        return web.render(request, "results.html", rep=rep, plan_id=plan_id, msg=msg, css=css, sections=sections,
                          resumable=bool(job and job["status"] in RESUMABLE and not (run and run.running)),
                          running=bool(run and run.running))

    def export_download(request: Request) -> Response:
        plan_id = int(request.path_params["plan_id"])
        with web.store() as store:
            plan = web.plan_or_404(store, plan_id)
        if not plan.export_path:
            raise HTTPException(404)
        p = Path(plan.export_path).resolve()
        root = paths.exports_dir().resolve()
        if root not in p.parents or not p.is_file():
            raise HTTPException(404)
        return FileResponse(p, media_type="application/json", filename=f"sociyx-plan-{plan_id}-export.json",
                            content_disposition_type="attachment")

    def report_download(request: Request) -> Response:
        plan_id, fmt = int(request.path_params["plan_id"]), request.path_params["fmt"]
        if fmt not in ("text", "json", "csv"):
            raise HTTPException(404)
        with web.store() as store:
            web.plan_or_404(store, plan_id)
            text = render_report(build_report(store, plan_id), fmt)
        ext = {"text": "txt", "json": "json", "csv": "csv"}[fmt]
        return Response(text, media_type="text/plain; charset=utf-8", headers={
            "Content-Disposition": f'attachment; filename="sociyx-report-plan-{plan_id}.{ext}"'})

    # ================= import =================

    def import_page(request: Request) -> Response:
        with web.store() as store:
            accounts = store.list_accounts("instagram")
        return web.render(request, "import.html", accounts=accounts, result=None, error="", path="")

    def import_run(request: Request) -> Response:
        f = form(request)
        path_text, account_id = f.get("path", "").strip(), f.get("account", "").strip() or None
        result = None
        error = ""
        with web.store() as store:
            accounts = store.list_accounts("instagram")
            if not path_text or "\0" in path_text:
                error = "Enter the path of the unzipped export folder."
            else:
                adapter = state.new_adapter("instagram")
                try:
                    result = import_export(store, adapter, "instagram", path_text, account_id)
                except (ValueError, LookupError, AdapterError) as e:
                    error = scrub(str(e))
                finally:
                    adapter.close()
        return web.render(request, "import.html", status=422 if error else 200, accounts=accounts, result=result,
                          error=error, path=path_text)

    # ================= static =================

    static_files = {p.name for p in STATIC.iterdir() if p.is_file()}
    SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]+$")

    def static(request: Request) -> Response:
        name = request.path_params["name"]
        if not SAFE_NAME.match(name) or name not in static_files:
            raise HTTPException(404)
        return FileResponse(STATIC / name)

    async def not_found(request: Request, exc: HTTPException) -> Response:
        return PlainTextResponse("Not found." if exc.status_code == 404 else str(exc.detail), status_code=exc.status_code)

    A = "/a/{platform}/{key}"
    routes = [
        Route("/", home), Route("/help", help_page),
        Route("/login", login_page), Route("/login/start", login_start, methods=["POST"]),
        Route("/login/{token}", login_wait), Route("/login/{token}/status", login_status),
        Route(A, account_page), Route(A + "/verify", verify),
        Route(A + "/scan/status", scan_status), Route(A + "/scan/start", scan_start, methods=["POST"]),
        Route(A + "/scan/stop", scan_stop, methods=["POST"]),
        Route(A + "/browse", browse), Route(A + "/select/{action}", select, methods=["POST"]),
        Route(A + "/review", review), Route(A + "/review/start", review_start, methods=["POST"]),
        Route(A + "/review/save", review_save, methods=["POST"]),
        Route("/run/{plan_id:int}", run_page), Route("/run/{plan_id:int}/status", run_status),
        Route("/run/{plan_id:int}/pause", run_pause, methods=["POST"]),
        Route("/run/{plan_id:int}/cancel", run_cancel_confirm), Route("/run/{plan_id:int}/cancel", run_cancel, methods=["POST"]),
        Route("/resume/{plan_id:int}", resume_page), Route("/resume/{plan_id:int}", resume_start, methods=["POST"]),
        Route("/results/{plan_id:int}", results),
        Route("/plan/{plan_id:int}/export", export_download),
        Route("/plan/{plan_id:int}/report/{fmt}", report_download),
        Route("/import", import_page), Route("/import", import_run, methods=["POST"]),
        Route("/static/{name}", static),
    ]

    @asynccontextmanager
    async def lifespan(app):
        yield
        shutdown_jobs(state)

    app = Starlette(routes=routes, lifespan=lifespan, exception_handlers={HTTPException: not_found})
    app.state.web = state
    return SecurityMiddleware(app, state)  # type: ignore[return-value]


def shutdown_jobs(state: WebState, timeout: float = 60.0) -> None:
    """Never abandon work silently: pause running deletions after their current item, stop scans between pages."""
    import time

    store = state.new_store()
    try:
        for plan_id, run in list(state.runs.items()):
            if run.running:
                try:
                    pause_job(store, run.job_id)
                except Exception:  # noqa: BLE001
                    pass
    finally:
        store.close()
    for scan in list(state.scans.values()):
        if scan.running:
            scan.stop()
    deadline = time.monotonic() + timeout
    for job in [*state.runs.values(), *state.scans.values()]:
        job.thread.join(max(0.0, deadline - time.monotonic()))
