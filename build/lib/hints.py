"""Identity-hint loader + applier. In name mode, a previous Phantom JSON
report can be passed to filter out FOUND hits whose country/language
clearly contradicts the subject's known profile.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

from identity import _normalise_country
from models import CheckResult


def _load_identity_hint(path: Path) -> Optional[dict]:
    """Read a previous Phantom JSON report and pull the bits we can use as
    a sanity filter for a fresh name-mode scan: a country (from geo_hint
    first, falling back to a normalisable item in `locations`), a bio
    language (the most common per-FOUND `language`), and a display name.

    Returns None if the file can't be parsed or carries no usable signal.
    Display name is informational only — filtering uses country/language.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"error: --identity-hint {path}: {e}", file=sys.stderr)
        return None

    overall = data.get("overall_identity") or data.get("identity") or {}

    country = None
    geo = overall.get("geo_hint") or {}
    if isinstance(geo, dict) and geo.get("region"):
        country = _normalise_country(geo["region"]) or geo["region"].strip()
    if not country:
        for loc in overall.get("locations") or []:
            country = _normalise_country(loc) if isinstance(loc, str) else None
            if country:
                break

    lang_counts: dict[str, int] = {}
    for f in data.get("found") or []:
        p = (f.get("profile") or {}) if isinstance(f, dict) else {}
        lang = p.get("language")
        if isinstance(lang, str) and lang.strip():
            lang_counts[lang] = lang_counts.get(lang, 0) + 1
    language = max(lang_counts, key=lang_counts.get) if lang_counts else None

    display_name = overall.get("display_name") or None

    if not (country or language):
        print(
            f"warning: --identity-hint {path} has no usable country or "
            f"language signal; nothing to filter on.",
            file=sys.stderr,
        )
        return None

    return {
        "country": country,
        "language": language,
        "display_name": display_name,
        "source": str(path),
    }


def _filter_results_by_hint(
    grouped: list[tuple[str, list["CheckResult"]]],
    hint: dict,
) -> int:
    """Reclassify FOUND hits whose profile country or language clearly
    contradicts the hint. The hit isn't deleted — its `exists` flips from
    True to None (UNKNOWN) and `reason` records the mismatch, so the row
    survives in the report for auditing while staying out of FOUND and
    out of the identity-correlation pool.

    Missing data is never treated as a contradiction: a profile with no
    location and no language is left alone.
    """
    expected_country = hint.get("country")
    expected_lang = hint.get("language")
    n_filtered = 0

    for _, rs in grouped:
        for r in rs:
            if r.exists is not True:
                continue
            profile = r.profile or {}
            mismatches: list[str] = []

            if expected_country:
                loc = profile.get("location")
                if isinstance(loc, str) and loc.strip():
                    observed = _normalise_country(loc)
                    if observed and observed.lower() != expected_country.lower():
                        mismatches.append(f"country={observed}≠{expected_country}")

            if expected_lang:
                lang = profile.get("language")
                if isinstance(lang, str) and lang.strip() and lang != expected_lang:
                    mismatches.append(f"lang={lang}≠{expected_lang}")

            if mismatches:
                r.exists = None
                tag = "filter:" + ",".join(mismatches)
                r.reason = f"{r.reason}+{tag}" if r.reason else tag
                n_filtered += 1

    return n_filtered
