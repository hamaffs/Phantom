"""CSV exporter — one row per FOUND account, flattened profile fields.

Spreadsheet-friendly output for analysts who want to filter/sort with
their own tooling. Inconclusive rows are omitted — the same policy as
the other export formats.
"""
from __future__ import annotations

import csv
from pathlib import Path
from typing import Optional

from dedupe import _flatten
from models import CheckResult


# The columns we emit in order. Profile fields chosen for OSINT utility:
# the ones an analyst typically wants in a sortable column.
CSV_COLUMNS = [
    "site",
    "category",
    "url",
    "variant",
    "reliability",
    "score",
    "tier",
    "identity_id",
    "is_primary_identity",
    "display_name",
    "bio",
    "followers",
    "following",
    "posts",
    "location",
    "language",
    "joined",
    "verified",
    "private",
    "website",
    "email",
    "photo",
]


def _row(r: CheckResult) -> dict:
    p = r.profile or {}
    return {
        "site": r.site,
        "category": r.category,
        "url": r.url,
        "variant": r.variant or "",
        "reliability": r.reliability,
        "score": r.score if r.score is not None else "",
        "tier": r.tier or "",
        "identity_id": r.identity_id if r.identity_id is not None else "",
        "is_primary_identity": (
            "" if r.is_primary_identity is None
            else ("yes" if r.is_primary_identity else "no")
        ),
        "display_name": p.get("display_name") or "",
        "bio": (p.get("bio") or "").replace("\n", " ").strip(),
        "followers": p.get("followers") if p.get("followers") is not None else "",
        "following": p.get("following") if p.get("following") is not None else "",
        "posts": p.get("posts") if p.get("posts") is not None else "",
        "location": p.get("location") or "",
        "language": p.get("language") or "",
        "joined": p.get("joined") or "",
        "verified": "" if p.get("verified") is None else ("yes" if p["verified"] else "no"),
        "private": "" if p.get("private") is None else ("yes" if p["private"] else "no"),
        "website": p.get("website") or "",
        "email": p.get("email") or "",
        "photo": p.get("photo") or "",
    }


def export_csv(grouped, raw, elapsed, path: Path, **_ignored) -> None:
    """Write one row per FOUND result. Extra kwargs are accepted and
    ignored so the dispatcher can pass the same shape to every exporter.
    """
    found, _, _ = _flatten(grouped)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, quoting=csv.QUOTE_MINIMAL)
        writer.writeheader()
        for r in found:
            writer.writerow(_row(r))
