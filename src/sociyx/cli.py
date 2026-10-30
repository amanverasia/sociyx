"""Command-line interface (argparse only)."""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timezone

from . import paths
from .adapters import ADAPTERS, adapter_class, get_adapter
from .executor import Executor, PlanRefused, cancel_job, pause_job
from .export import write_export, write_private_file
from .filters import Filter, parse_bound, select
from .models import ActionBlocked, AdapterError, AuthRequired, Outcome, from_iso
from .planning import create_plan, execute_command, preview
from .redact import configure_logging, scrub
from .report import build_report, format_report, render_report
from .scanning import scan_account
from .store import Store

BANNER = "*** FIXTURE ADAPTER: fake data, nothing real is touched ***"


def _banner(platform: str) -> None:
    if platform == "fixture":
        print(BANNER, file=sys.stderr)


def _table(headers: list[str], rows: list[list]) -> str:
    cells = [[str(c) for c in r] for r in rows]
    widths = [max(len(h), *(len(r[i]) for r in cells)) if cells else len(h) for i, h in enumerate(headers)]
    out = ["  ".join(h.ljust(w) for h, w in zip(headers, widths)).rstrip()]
    out += ["  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip() for r in cells]
    return "\n".join(out)


def _one_line(text: str, n: int) -> str:
    t = " ".join(text.split())
    return t if len(t) <= n else t[: n - 1] + "…"


def _filter(args, platform: str) -> Filter:
    known = adapter_class(platform).content_types
    for t in args.types or []:
        if t not in known:
            raise ValueError(f"unknown content type {t!r} for {platform}; known: {', '.join(known)}")
    return Filter(
        content_types=list(args.types or []),
        since=parse_bound(args.since, end=False) if args.since else None,
        until=parse_bound(args.until, end=True) if args.until else None,
        keywords=list(args.keywords or []),
        exclude_keywords=list(args.exclude_keywords or []),
    )


def _verified_account(store: Store, adapter, platform: str, ref: str | None):
    """Resolve a known account and re-confirm with the platform that the session matches."""
    acct = store.resolve_account(platform, ref)
    try:
        live = adapter.current_account(acct.account_id)
    except AuthRequired as e:
        raise ValueError(f"not logged in as {acct.handle or acct.account_id} ({e}); run `sociyx login {platform}`")
    if live.account_id != acct.account_id:
        raise ValueError(f"session belongs to {live.account_id}, not {acct.account_id}; refusing")
    store.upsert_account(live)
    return live


# ---------------- commands ----------------


def _pacing_profile(cls) -> str:
    cap = cls.daily_write_cap
    parts = [
        f"min delete interval {cls.min_delete_interval:g}s",
        f"jitter +{cls.pacing_jitter * 100:.0f}%",
        f"daily cap {cap if cap is not None else 'none'}",
    ]
    if cls.think_pause_every:
        lo, hi = cls.think_pause_seconds
        parts.append(f"think pause every {cls.think_pause_every} deletes ({lo:g}-{hi:g}s)")
    lo, hi = cls.scan_delay
    parts.append(f"scan delay {lo:g}-{hi:g}s" if hi > 0 else "scan delay none")
    return ", ".join(parts)


def cmd_platforms(args, store) -> int:
    for name in sorted(ADAPTERS):
        try:
            cls = adapter_class(name)
        except Exception as e:  # missing optional deps etc.
            print(f"{name}: unavailable ({type(e).__name__}: {e})\n")
            continue
        print(f"{name}: {cls.label}")
        print(f"  content types: {', '.join(f'{k} ({v})' for k, v in cls.content_types.items())}")
        print(f"  deletable:     {', '.join(sorted(cls.deletable_types))}")
        print(f"  history notes: {cls.discovery_notes}")
        print(f"  pacing:        {_pacing_profile(cls)}\n")
    return 0


def cmd_login(args, store) -> int:
    _banner(args.platform)
    options = {}
    if args.session_file:
        if args.platform != "instagram" or args.method != "sessionfile":
            raise ValueError("--session-file requires instagram --method sessionfile")
        options["session_file"] = args.session_file
    adapter = get_adapter(args.platform, **options)
    try:
        acct = adapter.login(args.account, args.method)
    finally:
        adapter.close()
    store.upsert_account(acct)
    print(f"logged in and verified: {acct.handle} ({acct.account_id}) on {acct.platform}")
    return 0


def cmd_logout(args, store) -> int:
    _banner(args.platform)
    acct = store.resolve_account(args.platform, args.account)
    adapter = get_adapter(args.platform)
    try:
        adapter.logout(acct.account_id)
    finally:
        adapter.close()
    print(f"removed local session for {acct.handle} ({acct.account_id})")
    return 0


def cmd_whoami(args, store) -> int:
    _banner(args.platform)
    adapter = get_adapter(args.platform)
    try:
        acct = _verified_account(store, adapter, args.platform, args.account)
    finally:
        adapter.close()
    print(f"{acct.handle} ({acct.account_id}) on {acct.platform}")
    return 0


def cmd_accounts(args, store) -> int:
    rows = store.list_accounts()
    if not rows:
        print("no accounts known yet; run `sociyx login PLATFORM`")
        return 0
    print(_table(["platform", "handle", "account_id", "last verified"],
                 [[r["platform"], r["handle"], r["account_id"], r["last_verified_at"] or "never"] for r in rows]))
    return 0


def cmd_scan(args, store) -> int:
    _banner(args.platform)
    adapter = get_adapter(args.platform)
    try:
        acct = _verified_account(store, adapter, args.platform, args.account)
        print(f"scanning {acct.handle} ({acct.account_id}) ...")
        summaries = scan_account(
            store, adapter, acct, args.types or None, fresh=args.fresh, max_pages=args.max_pages,
            progress=lambda t, pages, n: print(f"  {t}: page {pages}, {n} items", file=sys.stderr),
        )
    except ActionBlocked as e:
        print(f"scan stopped: {scrub(str(e))}\n{e.guidance()}", file=sys.stderr)
        return 3
    except AuthRequired as e:
        print(f"scan stopped: login required ({scrub(str(e))}); run `sociyx login {args.platform}`", file=sys.stderr)
        return 3
    finally:
        adapter.close()
    bad = False
    for s in summaries:
        state = "PARTIAL (resume with another scan)" if s.partial else s.status
        print(f"{s.content_type}: {state}; pages={s.pages} seen={s.items_seen} new={s.new_items} "
              f"missing={s.marked_missing}" + (f" error={s.error}" if s.error else ""))
        if s.warning:
            print(f"  warning: {s.warning}")
        bad |= s.status == "failed"
    print(f"\nhistory notes: {adapter.discovery_notes}")
    return 1 if bad else 0


def cmd_status(args, store) -> int:
    _banner(args.platform)
    acct = store.resolve_account(args.platform, args.account)
    print(f"{acct.handle} ({acct.account_id})")
    counts = store.item_counts(acct.platform, acct.account_id)
    if counts:
        print(_table(["type", "state", "count", "oldest", "newest"],
                     [[r["content_type"], r["state"], r["n"], r["oldest"] or "-", r["newest"] or "-"] for r in counts]))
    else:
        print("no items stored; run `sociyx scan`")
    print("\nlatest scans:")
    for s in store.latest_scans(acct.platform, acct.account_id):
        label = {"complete": "complete", "failed": "failed"}.get(s["status"], "partial")
        print(f"  {s['content_type']}: {label}, {s['items_seen']} seen, finished {s['finished_at'] or '-'}"
              + (f" ({s['error']})" if s["error"] else ""))
    print(f"\nhistory notes: {adapter_class(args.platform).discovery_notes}")
    return 0


def _selected(args, store):
    acct = store.resolve_account(args.platform, args.account)
    flt = _filter(args, args.platform)
    return acct, flt, select(store, acct.platform, acct.account_id, flt)


def cmd_list(args, store) -> int:
    _banner(args.platform)
    acct, flt, items = _selected(args, store)
    shown = items[: args.limit]
    print(_table(["created_at", "type", "item_id", "text"],
                 [[i.created_at.isoformat() if i.created_at else "-", i.content_type, i.item_id, _one_line(i.text, 60)]
                  for i in shown]))
    print(f"\n{len(items)} matching items" + (f" (showing first {len(shown)}; raise --limit)" if len(shown) < len(items) else ""))
    return 0


def cmd_export(args, store) -> int:
    _banner(args.platform)
    acct, flt, items = _selected(args, store)
    ext = args.format
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", acct.account_id)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    from pathlib import Path

    path = Path(args.out) if args.out else paths.exports_dir() / f"export-{args.platform}-{safe}-{stamp}.{ext}"
    meta = {"platform": acct.platform, "account_id": acct.account_id, "handle": acct.handle, "filters": flt.to_dict()}
    sha = write_export(items, path, args.format, meta)
    print(f"wrote {len(items)} items to {path} (mode 0600)\nsha256 {sha}")
    return 0


def cmd_plan_create(args, store) -> int:
    _banner(args.platform)
    acct = store.resolve_account(args.platform, args.account)
    plan = create_plan(store, args.platform, acct, _filter(args, args.platform))
    print(preview(store, plan, limit=50))
    print(f"\nNothing has been deleted. Review with `sociyx plan show {plan.plan_id} --all`.")
    return 0


def cmd_plan_show(args, store) -> int:
    plan = store.get_plan(args.plan_id)
    _banner(plan.platform)
    print(preview(store, plan, limit=None if args.all else 50))
    return 0


def cmd_plan_list(args, store) -> int:
    rows = store.list_plans()
    if not rows:
        print("no plans")
        return 0
    print(_table(["plan", "platform", "account", "items", "created", "job", "job status"],
                 [[r["plan_id"], r["platform"], r["handle"], r["item_count"], r["created_at"],
                   r["job_id"] or "-", r["job_status"] or "-"] for r in rows]))
    return 0


_EXIT = {"completed": 0, "paused": 3, "stopped_auth": 3, "stopped_blocked": 3, "cancelled": 3}


def _run_live(store: Store, plan, confirm: str, delay: float | None, daily_cap: int | None = None,
              ignore_cooldown: bool = False) -> int:
    adapter = get_adapter(plan.platform)

    def show(item_id: str, outcome: Outcome, detail: str) -> None:
        print(f"  {item_id}: {outcome.value}", flush=True)

    ex = Executor(store, adapter, min_interval=delay, progress=show, daily_cap=daily_cap,
                  ignore_cooldown=ignore_cooldown)
    try:
        print(f"deleting {plan.item_count} planned items from {plan.account_id} ...")
        result = ex.run(plan.plan_id, confirm)
    except KeyboardInterrupt:
        print("\ninterrupted. The in-flight item (if any) is marked unknown; resume with "
              f"`sociyx plan execute {plan.plan_id} --live --confirm '{plan.confirmation_token}'`.")
        return 130
    finally:
        adapter.close()
    print(f"\njob {result.job_id}: {result.status} — {result.message}\n")
    print(format_report(build_report(store, plan.plan_id)))
    if result.status != "completed":
        print(f"\nTo resume: {execute_command(plan)}")
    return _EXIT.get(result.status, 3)


def cmd_plan_execute(args, store) -> int:
    plan = store.get_plan(args.plan_id)
    _banner(plan.platform)
    if not args.live:
        print(preview(store, plan, limit=50))
        acct = store.get_account(plan.platform, plan.account_id)
        print(f"\ndry run: nothing was deleted. To delete these {plan.item_count} items from "
              f"{acct.handle if acct else plan.account_id} run: {execute_command(plan)}")
        return 0
    if not args.confirm or args.confirm != plan.confirmation_token:
        raise PlanRefused("--live requires --confirm with the exact token printed by `plan create`/`plan show`")
    return _run_live(store, plan, args.confirm, args.delay, args.daily_cap, args.ignore_cooldown)


def cmd_job_list(args, store) -> int:
    rows = store.list_jobs()
    if not rows:
        print("no jobs")
        return 0
    print(_table(["job", "plan", "status", "updated", "error"],
                 [[r["job_id"], r["plan_id"], r["status"], r["updated_at"], _one_line(r["error"] or "", 60)] for r in rows]))
    return 0


def cmd_job_status(args, store) -> int:
    job = store.get_job(args.job_id)
    plan = store.get_plan(job.plan_id)
    _banner(plan.platform)
    print(f"job {job.job_id} (plan {job.plan_id}): {job.status}" + (f" — {job.error}" if job.error else ""))
    if job.rate_limited_until:
        until = from_iso(job.rate_limited_until)
        if until and until > datetime.now(timezone.utc):
            kind = "cool-down (platform restriction)" if job.status == "stopped_blocked" else "rate-limit wait"
            print(f"{kind} until {until.astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')}"
                  + ("; resume is refused before then unless --ignore-cooldown" if job.status == "stopped_blocked" else ""))
    for k, v in sorted(store.outcome_counts(plan.plan_id).items()):
        print(f"  {v:>5}  {k}")
    print("\nrecent events:")
    for e in store.events(job.job_id, 15):
        print(f"  {e['ts']} {e['level']:<5} {e['message']}")
    return 0


def cmd_job_pause(args, store) -> int:
    print(pause_job(store, args.job_id))
    return 0


def cmd_job_cancel(args, store) -> int:
    print(cancel_job(store, args.job_id))
    return 0


def cmd_job_resume(args, store) -> int:
    job = store.get_job(args.job_id)
    plan = store.get_plan(job.plan_id)
    _banner(plan.platform)
    if not args.confirm or args.confirm != plan.confirmation_token:
        raise PlanRefused("resume requires --confirm with the plan's exact token")
    return _run_live(store, plan, args.confirm, args.delay, args.daily_cap, args.ignore_cooldown)


def cmd_report(args, store) -> int:
    plan = store.get_plan(args.plan_id)
    _banner(plan.platform)
    text = render_report(build_report(store, args.plan_id), args.format)
    if args.out:
        from pathlib import Path

        write_private_file(Path(args.out), text)
        print(f"wrote report to {args.out}")
    else:
        sys.stdout.write(text)
    return 0


def cmd_import(args, store) -> int:
    from pathlib import Path

    from .models import Account as _Account

    _banner(args.platform)
    adapter = get_adapter(args.platform)
    try:
        fn = getattr(adapter, "import_export", None)
        if fn is None:
            raise ValueError(f"platform {args.platform!r} does not support importing a data export")
        path = Path(args.path)
        if not path.exists():
            raise ValueError(f"export path {args.path!r} does not exist")
        if not path.is_dir():
            raise ValueError("export path must be an unzipped export directory")
        acct = None
        if args.account:
            known = store.get_account(args.platform, args.account)
            acct = known or _Account(args.platform, args.account, "")
        items = list(fn(path, acct) if acct is not None else fn(path))
        if not items:
            print("nothing importable found in the export")
            return 0
        by_acct: dict[str, list] = {}
        for it in items:
            by_acct.setdefault(it.account_id, []).append(it)
        total_seen = total_new = 0
        for aid, subset in sorted(by_acct.items()):
            handle = subset[0].account_id
            # Preserve a known handle when we already have this account.
            known = store.get_account(args.platform, aid)
            store.upsert_account(known or _Account(args.platform, aid, ""), verified=False)
            seen, new = store.upsert_items(subset, None, source="export")
            total_seen += seen
            total_new += new
        print(f"imported {total_seen} items ({total_new} new) from {args.path}")
    finally:
        adapter.close()
    return 0


# ---------------- parser ----------------


def _add_filters(p: argparse.ArgumentParser) -> None:
    p.add_argument("--account", help="account id or handle (needed if several are known)")
    p.add_argument("--type", dest="types", action="append", help="content type (repeatable)")
    p.add_argument("--since", help="inclusive start, YYYY-MM-DD or ISO datetime (UTC unless offset given)")
    p.add_argument("--until", help="inclusive end; a bare date means end of that day")
    p.add_argument("--keyword", dest="keywords", action="append", help="match any of these (case-insensitive)")
    p.add_argument("--exclude-keyword", dest="exclude_keywords", action="append", help="drop items containing this")


def _add_safety_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("--daily-cap", type=int, metavar="N",
                   help="max delete requests per rolling 24h for this account (can only lower the platform's cap)")
    p.add_argument("--ignore-cooldown", action="store_true",
                   help="resume even though the platform restricted the account recently "
                        "(only if you know the block was cleared)")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="sociyx", description="Local-first bulk cleanup of your OWN social media history. "
        "Run with no arguments in a terminal for the guided UI.")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="command", required=True)

    def add(name, func, help_, parent=sub):
        p = parent.add_parser(name, help=help_)
        p.set_defaults(func=func)
        return p

    p = add("tui", cmd_tui, "open the guided terminal UI (same as running `sociyx` with no arguments)")
    p.add_argument("--demo", action="store_true",
                   help="try everything with fake data in a throw-away folder; your real data is not touched")
    add("platforms", cmd_platforms, "list supported platforms")
    p = add("login", cmd_login, "log in to a platform")
    p.add_argument("platform")
    p.add_argument("--account")
    p.add_argument("--method")
    p.add_argument("--session-file", help="instagrapi settings JSON (instagram --method sessionfile)")
    p = add("logout", cmd_logout, "remove the local session")
    p.add_argument("platform")
    p.add_argument("--account")
    p = add("whoami", cmd_whoami, "show the verified logged-in account")
    p.add_argument("platform")
    p.add_argument("--account")
    add("accounts", cmd_accounts, "list known accounts")
    p = add("scan", cmd_scan, "fetch your content into the local database")
    p.add_argument("platform")
    p.add_argument("--account")
    p.add_argument("--type", dest="types", action="append")
    p.add_argument("--fresh", action="store_true", help="start over instead of resuming")
    p.add_argument("--max-pages", type=int)
    p = add("status", cmd_status, "what is stored locally")
    p.add_argument("platform")
    p.add_argument("--account")
    p = add("list", cmd_list, "list matching stored items")
    p.add_argument("platform")
    _add_filters(p)
    p.add_argument("--limit", type=int, default=50)
    p = add("export", cmd_export, "export matching items")
    p.add_argument("platform")
    _add_filters(p)
    p.add_argument("--format", choices=["json", "jsonl", "csv"], default="json")
    p.add_argument("--out")

    plan = sub.add_parser("plan", help="create, review and execute deletion plans")
    ps = plan.add_subparsers(dest="plan_command", required=True)
    p = add("create", cmd_plan_create, "freeze a selection into a plan", ps)
    p.add_argument("platform")
    _add_filters(p)
    p = add("show", cmd_plan_show, "show a plan", ps)
    p.add_argument("plan_id", type=int)
    p.add_argument("--all", action="store_true")
    add("list", cmd_plan_list, "list plans", ps)
    p = add("execute", cmd_plan_execute, "dry-run (default) or delete with --live", ps)
    p.add_argument("plan_id", type=int)
    p.add_argument("--live", action="store_true", help="really delete (needs --confirm)")
    p.add_argument("--confirm", help="the plan's confirmation token")
    p.add_argument("--delay", type=float, help="minimum seconds between delete requests")
    _add_safety_flags(p)

    job = sub.add_parser("job", help="inspect and control deletion jobs")
    js = job.add_subparsers(dest="job_command", required=True)
    add("list", cmd_job_list, "list jobs", js)
    for name, func, h in (("status", cmd_job_status, "job details"), ("pause", cmd_job_pause, "pause after current item"),
                          ("cancel", cmd_job_cancel, "cancel remaining items"), ("resume", cmd_job_resume, "resume a job")):
        p = add(name, func, h, js)
        p.add_argument("job_id", type=int)
        if name == "resume":
            p.add_argument("--confirm")
            p.add_argument("--delay", type=float)
            _add_safety_flags(p)

    p = add("report", cmd_report, "report on a plan's outcomes")
    p.add_argument("plan_id", type=int)
    p.add_argument("--format", choices=["text", "json", "csv"], default="text")
    p.add_argument("--out")
    p = add("import", cmd_import, "import a data export (e.g. Instagram download)")
    p.add_argument("platform")
    p.add_argument("path", help="export directory or zip contents directory")
    p.add_argument("--account", help="account id (uses the stored session when omitted, if unambiguous)")
    return ap


def cmd_tui(args, store=None) -> int:
    """Launch the guided UI. Textual is imported only here, so other commands never pay for it."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print("error: the guided UI needs an interactive terminal; use the regular subcommands instead "
              "(see `sociyx --help`).", file=sys.stderr)
        return 2
    if args.demo:
        from .tui.demo import prepare_demo_home

        home = prepare_demo_home()
        print(f"Demo mode: fake data lives in {home} (safe to delete afterwards). Your real data is untouched.",
              file=sys.stderr)
    try:
        from .tui import run_tui
    except ImportError as e:  # textual missing
        print(f"error: the guided UI needs the 'textual' package ({e}); run `uv sync`.", file=sys.stderr)
        return 2
    return run_tui(demo=args.demo)


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if not argv:  # bare `sociyx`: guided UI on a terminal, plain help otherwise (pipes, scripts, CI)
        if sys.stdin.isatty() and sys.stdout.isatty():
            return cmd_tui(argparse.Namespace(demo=False))
        build_parser().print_help()
        return 0
    args = build_parser().parse_args(argv)
    if args.command == "tui":
        return cmd_tui(args)
    configure_logging(args.verbose)
    store = Store(paths.db_path())
    try:
        return args.func(args, store)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    except (LookupError, ValueError, PlanRefused, AdapterError) as e:
        print(f"error: {scrub(str(e))}", file=sys.stderr)
        return 2
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
