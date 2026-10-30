"""Post-run report. Every outcome class is reported separately and never overstated."""

from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timezone

from .adapters import adapter_class
from .models import Outcome, to_iso
from .store import Store

# (key, outcome value, heading) in display order
SECTIONS = [
    ("deleted_verified", Outcome.DELETED_VERIFIED, "Deleted & verified"),
    ("deleted_unverified", Outcome.DELETED_UNVERIFIED, "Deleted but UNVERIFIED (platform accepted the request; not confirmed gone)"),
    ("already_deleted", Outcome.ALREADY_DELETED, "Already deleted"),
    ("inaccessible", Outcome.INACCESSIBLE, "Inaccessible (could not be deleted)"),
    ("failed", Outcome.FAILED, "Failed (request failed)"),
    ("skipped", Outcome.SKIPPED, "Skipped"),
    ("pending", Outcome.PENDING, "Not attempted (pending)"),
    ("in_progress", Outcome.IN_PROGRESS, "Interrupted / unknown (attempt started, result not known)"),
]


def disclaimer(n: int, plan_id: int) -> str:
    return (
        f"This report covers only the {n} items in plan {plan_id}. It is not evidence that the account "
        "contains no other content: platforms may not expose all history, copies may exist elsewhere "
        "(caches, archives, other users' reposts/quotes, platform backups)."
    )


def build_report(store: Store, plan_id: int) -> dict:
    plan = store.get_plan(plan_id)
    acct = store.get_account(plan.platform, plan.account_id)
    rows = store.plan_items(plan_id)
    job = None
    for j in store.list_jobs():
        if j["plan_id"] == plan_id:
            job = {k: j[k] for k in j.keys() if k != "control"}
    try:
        notes = adapter_class(plan.platform).discovery_notes
    except Exception as e:  # adapter may fail to import (optional deps)
        notes = f"(adapter notes unavailable: {type(e).__name__})"
    scans = []
    for s in store.latest_scans(plan.platform, plan.account_id):
        status = {"complete": "complete", "failed": "failed"}.get(s["status"], "partial")
        scans.append({"content_type": s["content_type"], "status": status, "items_seen": s["items_seen"],
                      "finished_at": s["finished_at"], "error": s["error"]})
    sections = {}
    for key, outcome, title in SECTIONS:
        sections[key] = {
            "title": title,
            "items": [
                {"item_id": r["item_id"], "content_type": r["content_type"], "outcome": r["outcome"],
                 "attempts": r["attempts"], "detail": r["detail"], "url": r["url"]}
                for r in rows if r["outcome"] == outcome.value
            ],
        }
    return {
        "plan_id": plan_id,
        "platform": plan.platform,
        "account_id": plan.account_id,
        "handle": acct.handle if acct else "",
        "generated_at": to_iso(datetime.now(timezone.utc)),
        "job": job,
        "item_count": len(rows),
        "counts": {k: len(v["items"]) for k, v in sections.items()},
        "sections": sections,
        "scans": scans,
        "discovery_notes": notes,
        "export_path": plan.export_path,
        "disclaimer": disclaimer(len(rows), plan_id),
    }


def format_report(rep: dict) -> str:
    lines = [
        f"Report for plan {rep['plan_id']} — {rep['platform']} account {rep['handle']} ({rep['account_id']})",
    ]
    if rep["platform"] == "fixture":
        lines.append("FIXTURE ADAPTER: fake data, nothing real was touched.")
    job = rep["job"]
    lines.append(f"Job status: {job['status']}" + (f" — {job['error']}" if job and job.get("error") else "")
                 if job else "Job status: not started")
    lines.append("")
    lines.append("Summary")
    for key, sec in rep["sections"].items():
        lines.append(f"  {len(sec['items']):>5}  {sec['title']}")
    for key, sec in rep["sections"].items():
        if not sec["items"]:
            continue
        lines += ["", f"== {sec['title']} ({len(sec['items'])}) =="]
        for it in sec["items"]:
            detail = f" — {it['detail']}" if it["detail"] else ""
            lines.append(f"  {it['item_id']} [{it['content_type']}] attempts={it['attempts']}{detail}")
            if it["url"]:
                lines.append(f"      {it['url']}")
    lines += ["", "Latest scan per content type"]
    for s in rep["scans"] or []:
        lines.append(f"  {s['content_type']}: {s['status']} ({s['items_seen']} items seen)"
                     + (f" — {s['error']}" if s["error"] else ""))
    if not rep["scans"]:
        lines.append("  (none recorded)")
    lines += ["", "Platform history notes: " + rep["discovery_notes"], "", rep["disclaimer"]]
    return "\n".join(lines)


def render_report(rep: dict, fmt: str) -> str:
    if fmt == "text":
        return format_report(rep) + "\n"
    if fmt == "json":
        return json.dumps(rep, indent=2, ensure_ascii=False) + "\n"
    if fmt == "csv":
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["section", "item_id", "content_type", "outcome", "attempts", "detail", "url"])
        for key, sec in rep["sections"].items():
            for it in sec["items"]:
                w.writerow([key, it["item_id"], it["content_type"], it["outcome"], it["attempts"], it["detail"], it["url"]])
        return buf.getvalue()
    raise ValueError(f"unknown report format {fmt!r}")
