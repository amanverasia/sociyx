"""Write items to JSON / JSONL / CSV files readable only by the current user."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import paths
from .models import ContentItem, to_iso

CSV_FIELDS = ["platform", "account_id", "item_id", "content_type", "created_at", "url", "text"]


def _item_dict(it: ContentItem) -> dict[str, Any]:
    d = asdict(it)
    d["created_at"] = to_iso(it.created_at)
    return d


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def render_export(items: list[ContentItem], fmt: str, meta: dict | None = None) -> str:
    if fmt == "json":
        full_meta = {
            **(meta or {}),
            "generated_at": to_iso(datetime.now(timezone.utc)),
            "tool": "unpost",
            "count": len(items),
        }
        return json.dumps({"meta": full_meta, "items": [_item_dict(i) for i in items]},
                          indent=2, ensure_ascii=False, default=str) + "\n"
    if fmt == "jsonl":
        return "".join(json.dumps(_item_dict(i), ensure_ascii=False, default=str) + "\n" for i in items)
    if fmt == "csv":
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(CSV_FIELDS)
        for i in items:
            d = _item_dict(i)
            w.writerow([d[f] if d[f] is not None else "" for f in CSV_FIELDS])
        return buf.getvalue()
    raise ValueError(f"unknown export format {fmt!r} (use json, jsonl or csv)")


def write_private_file(path: Path, text: str) -> str:
    """Write text with mode 0600 (even if the file pre-existed); returns its sha256."""
    path = Path(path)
    secrets = (paths.home() / "secrets").resolve()
    if secrets in path.resolve().parents:
        raise ValueError("refusing to write output inside the secrets directory")
    path.parent.mkdir(parents=True, exist_ok=True)
    data = text.encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.chmod(path, 0o600)
    return hashlib.sha256(data).hexdigest()


def write_export(items: list[ContentItem], path: Path, fmt: str, meta: dict | None = None) -> str:
    """Write items to path; returns the sha256 hex digest of the written file."""
    return write_private_file(path, render_export(items, fmt, meta))
