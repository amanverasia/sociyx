"""Plans: a frozen, reviewable selection of items plus the export that proves what was previewed."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from . import paths
from .adapters import adapter_class
from .adapters.base import Adapter
from .export import sha256_file, write_export
from .filters import Filter, select
from .models import Account, ContentItem
from .store import Plan, Store


class PlanRefused(Exception):
    """A safety check failed; nothing was (or will be) deleted by this attempt."""


def plan_digest(account: Account, items: list[ContentItem]) -> str:
    pairs = sorted((i.item_id, i.fingerprint()) for i in items)
    payload = json.dumps([account.platform, account.account_id, pairs], ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()


def _adapter_cls(adapter_cls_or_platform):
    cls = adapter_class(adapter_cls_or_platform) if isinstance(adapter_cls_or_platform, str) else adapter_cls_or_platform
    return type(cls) if isinstance(cls, Adapter) else cls


def create_plan(store: Store, adapter_cls_or_platform, account: Account, flt: Filter) -> Plan:
    cls = _adapter_cls(adapter_cls_or_platform)
    selected = select(store, account.platform, account.account_id, flt)
    items = [i for i in selected if i.content_type in cls.deletable_types]
    excluded = len(selected) - len(items)
    if not items:
        extra = f" ({excluded} matching items are of non-deletable types)" if excluded else ""
        raise ValueError(f"no items match the filter{extra}; nothing to plan")
    return create_plan_from_items(store, cls, account, items, flt.to_dict(), _excluded=excluded)


def create_plan_from_items(
    store: Store,
    adapter_cls_or_platform,
    account: Account,
    items: list[ContentItem],
    filters_description: dict,
    *,
    _excluded: int | None = None,
) -> Plan:
    """Freeze exactly `items` (e.g. a hand-picked selection) into a plan and write its export first.

    Rejects an empty selection and any item that is not this account's own stored item (wrong platform
    or account, or unknown to the store). Items of non-deletable types are dropped and counted in
    ``excluded_non_deletable``, exactly like ``create_plan``; if nothing deletable remains it refuses.
    `filters_description` is recorded in plan.filters next to {"selection": "manual", "count": n}
    (``create_plan`` passes its filter and records no "selection" key).
    """
    cls = _adapter_cls(adapter_cls_or_platform)
    if not items:
        raise ValueError("nothing selected; nothing to plan")
    seen: set[str] = set()
    unique: list[ContentItem] = []
    for it in items:
        if (it.platform, it.account_id) != (account.platform, account.account_id):
            raise ValueError(f"item {it.item_id!r} belongs to another account; refusing to plan it")
        if store.get_item_row(it.platform, it.account_id, it.item_id) is None:
            raise ValueError(f"item {it.item_id!r} is not in the local database; scan first")
        if it.item_id not in seen:
            seen.add(it.item_id)
            unique.append(it)
    chosen = [i for i in unique if i.content_type in cls.deletable_types]
    excluded = len(unique) - len(chosen) if _excluded is None else _excluded
    if not chosen:
        raise ValueError(f"none of the {len(unique)} selected items can be deleted on this platform; nothing to plan")
    items = chosen
    filters = dict(filters_description)
    if _excluded is None:
        filters = {"selection": "manual", "count": len(items), **filters}
    filters["excluded_non_deletable"] = excluded
    digest = plan_digest(account, items)
    plan = store.create_plan(account, filters, items, digest)

    safe = re.sub(r"[^A-Za-z0-9._-]", "_", account.account_id)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = paths.exports_dir() / f"plan-{plan.plan_id}-{safe}-{stamp}.json"
    meta = {
        "plan_id": plan.plan_id,
        "platform": account.platform,
        "account_id": account.account_id,
        "handle": account.handle,
        "filters": filters,
        "item_count": len(items),
        "digest": digest,
    }
    sha = write_export(items, path, "json", meta)
    store.set_plan_export(plan.plan_id, str(path), sha)
    return store.get_plan(plan.plan_id)


def check_plan_ready(store: Store, plan: Plan) -> None:
    """Refuse unless the exported copy of the plan still matches what was previewed."""
    if not plan.export_path or not plan.export_sha256:
        raise PlanRefused("plan has no export; deletion refused")
    path = Path(plan.export_path)
    if not path.is_file() or sha256_file(path) != plan.export_sha256:
        raise PlanRefused("export missing or modified; deletion refused")
    rows = store.plan_items(plan.plan_id)
    pairs = sorted((r["item_id"], r["fingerprint"]) for r in rows)
    payload = json.dumps([plan.platform, plan.account_id, pairs], ensure_ascii=False)
    if len(rows) != plan.item_count or hashlib.sha256(payload.encode()).hexdigest() != plan.digest:
        raise PlanRefused("plan items do not match the plan digest; deletion refused")


def _one_line(text: str, n: int) -> str:
    t = " ".join(text.split())
    return t if len(t) <= n else t[: n - 1] + "…"


def execute_command(plan: Plan) -> str:
    return f"sociyx plan execute {plan.plan_id} --live --confirm '{plan.confirmation_token}'"


def preview(store: Store, plan: Plan, limit: int | None = None) -> str:
    rows = store.plan_items(plan.plan_id)
    acct = store.get_account(plan.platform, plan.account_id)
    f = plan.filters
    desc = []
    if f.get("selection") == "manual":
        desc.append(f"manual selection of {f.get('count', len(rows))} items;")
    desc.append("types=" + (",".join(f.get("content_types") or []) or "all"))
    for key in ("since", "until"):
        if f.get(key):
            desc.append(f"{key}={f[key]}")
    if f.get("keywords"):
        desc.append("keywords(any)=" + ",".join(repr(k) for k in f["keywords"]))
    if f.get("exclude_keywords"):
        desc.append("exclude=" + ",".join(repr(k) for k in f["exclude_keywords"]))
    by_type: dict[str, int] = {}
    for r in rows:
        by_type[r["content_type"]] = by_type.get(r["content_type"], 0) + 1
    dates = sorted(r["created_at"] for r in rows if r["created_at"])
    undated = sum(1 for r in rows if not r["created_at"])
    lines = [
        f"Plan {plan.plan_id}",
        f"Account:  {acct.handle if acct else '?'} ({plan.account_id})",
        f"Platform: {plan.platform}",
        f"Filters:  {' '.join(desc)}",
        f"Items:    {len(rows)} total — " + ", ".join(f"{n} {t}" for t, n in sorted(by_type.items())),
    ]
    if dates:
        lines.append(f"Dates:    {dates[0]} .. {dates[-1]}" + (f" (+{undated} undated)" if undated else ""))
    elif undated:
        lines.append(f"Dates:    {undated} undated")
    if f.get("excluded_non_deletable"):
        lines.append(f"Excluded: {f['excluded_non_deletable']} matching items of non-deletable types")
    lines.append("")
    shown = rows if limit is None else rows[:limit]
    for n, r in enumerate(shown, 1):
        lines.append(
            f"{n:>4}  {r['created_at'] or '(unknown date)':<27} {r['content_type']:<7} "
            f"{r['item_id']}  {r['url']}  {_one_line(r['text'], 80)}"
        )
    if len(shown) < len(rows):
        lines.append(f"      ... {len(rows) - len(shown)} more not shown (use `sociyx plan show {plan.plan_id} --all`)")
    lines += [
        "",
        f"Export of exactly these items: {plan.export_path}",
        f"Confirmation token: {plan.confirmation_token}",
        f"To delete: {execute_command(plan)}",
    ]
    return "\n".join(lines)


def describe_pacing(cls, n: int) -> str:
    """Plain-words pacing profile and time estimate for deleting `n` items on adapter class `cls`."""
    from .models import format_duration

    lo = cls.min_delete_interval
    hi = lo * (1 + cls.pacing_jitter)
    avg = (lo + hi) / 2
    if hi > lo:
        every = f"about one item every {lo:g}-{hi:g} s"
    elif lo > 0:
        every = f"about one item every {lo:g} s"
    else:
        every = "items are processed back to back"
    parts = [every]
    total = n * avg
    if cls.think_pause_every:
        p_lo, p_hi = cls.think_pause_seconds
        pauses = n // cls.think_pause_every
        total += pauses * (p_lo + p_hi) / 2
        parts.append(f"a longer {p_lo:g}-{p_hi:g} s pause after every {cls.think_pause_every} items")
    cap = cls.daily_write_cap
    if cap is not None:
        parts.append(f"{cap} per day at most")
    text = ", ".join(parts) + ". "
    if cap is not None and n > cap:
        days = -(-n // cap)
        text += (f"With {n} items the daily cap means this takes about {days} days of running "
                 f"(each day about {format_duration(cap * avg)} of active work); ")
    else:
        text += f"This will take about {format_duration(total)}; " if total >= 1 else "This should be quick; "
    return text + "you can pause and resume anytime."
