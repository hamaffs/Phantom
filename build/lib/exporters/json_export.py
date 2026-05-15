"""JSON exporter + the shared `_build_json_payload` helper used by both
file and stdout JSON output.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from dedupe import _flatten
from models import CheckResult


def _deep_evidence_to_dict(deep) -> Optional[dict]:
    if deep is None:
        return None
    return {
        "notes": list(getattr(deep, "notes", []) or []),
        "extra_edges": [
            {"i": i, "j": j, "rationale": why}
            for (i, j, why) in (getattr(deep, "extra_edges", []) or [])
        ],
    }


def _build_json_payload(grouped, raw, elapsed, overall, clusters, emails=None, deep_evidence=None, dis_clusters=None):
    found, unknown, missing_count = _flatten(grouped)
    payload = {
        "input": raw,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "elapsed_seconds": round(elapsed, 2),
        "variants": [v for v, _ in grouped],
        "summary": {
            "found": len(found),
            "unknown": len(unknown),
            "missing": missing_count,
            "photo_matches": len(
                [c for c in (clusters or []) if len(c.member_indexes) > 1]
            ),
        },
        "overall_identity": overall.to_dict() if overall else None,
        "photo_matched_clusters": [
            c.to_dict() for c in (clusters or []) if len(c.member_indexes) > 1
        ],
        "found": [asdict(r) for r in found],
    }
    if emails:
        payload["emails"] = emails
    if deep_evidence is not None:
        payload["photo_deep"] = _deep_evidence_to_dict(deep_evidence)
    if dis_clusters is not None:
        payload["identity_clusters"] = [c.to_dict() for c in dis_clusters]
    return payload


def export_json(grouped, raw, elapsed, path: Path, overall=None, clusters=None, emails=None, deep_evidence=None, dis_clusters=None) -> None:
    payload = _build_json_payload(grouped, raw, elapsed, overall, clusters or [], emails, deep_evidence, dis_clusters=dis_clusters)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
