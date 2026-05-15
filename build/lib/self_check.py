"""--self-check: probe a curated canary handle per site and report drift.

For each site in sites.json that has an entry in tests/canaries.json,
Phantom scans the canary handle with `--exact` and `--no-cache` and
classifies the outcome:

  ok            FOUND verdict + at least one profile field extracted
  missing       MISSING — the canary disappeared (rare; flag for review)
  unknown       UNKNOWN — site went bot-walled or changed shape
  not_listed    no canary handle configured for this site

A site that flips from `ok` to `unknown` or `missing` is a strong
signal the extractor or detection rule broke. Exit code is non-zero
when any drift is detected, so this can be wired into CI.

Auto-disable: after N consecutive non-ok results for the same site,
we *suggest* (never auto-write) marking it `disabled: true` in
sites.json. The user reviews and applies. Streak state lives in
`~/.cache/phantom/self_check_history.json`.

The canary list lives in `tests/canaries.json` so it travels with the
repo and gets versioned alongside the sites.json it references.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Optional

from cache import ResponseCache
from models import load_sites
from scanner import Phantom


# Streak threshold — flag a site after this many consecutive failures.
# 3 is conservative: transient rate-limits don't trigger a false alarm
# but a real schema change is caught quickly.
_DISABLE_STREAK = 3


def _canaries_path() -> Path:
    return Path(__file__).parent / "tests" / "canaries.json"


def _history_path() -> Path:
    base = Path(
        os.environ.get("PHANTOM_CACHE_PATH")
        or Path.home() / ".cache" / "phantom"
    )
    if base.suffix == ".json":  # PHANTOM_CACHE_PATH points at a file
        base = base.parent
    return base / "self_check_history.json"


def _load_history() -> dict:
    """Per-site `{streak: N, last_status: '...', last_seen_ok: ISO}` map."""
    p = _history_path()
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_history(hist: dict) -> None:
    p = _history_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps(hist, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except OSError:
        pass  # history is best-effort, never fatal


def load_canaries() -> dict[str, str]:
    """Load the canary handle mapping (site_name → handle). The
    underscore-prefixed `_README` key is filtered out."""
    p = _canaries_path()
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if not k.startswith("_") and isinstance(v, str)}


async def run_self_check(
    sites_path: Path,
    *,
    concurrency: int = 25,
    timeout: float = 15.0,
    impersonate: bool = True,
    verbose: bool = False,
) -> int:
    """Run the canary probe. Returns the count of drifted sites
    (FOUND-expected → not-FOUND)."""
    canaries = load_canaries()
    if not canaries:
        print("self-check: no canaries.json — nothing to probe", file=sys.stderr)
        return 0
    sites = load_sites(sites_path)
    # Index sites by name for quick lookup, and skip ones without a canary.
    by_name = {s.name: s for s in sites}
    targets = [
        (by_name[name], handle)
        for name, handle in canaries.items()
        if name in by_name
    ]
    if not targets:
        print(
            "self-check: no canaries matched any active site",
            file=sys.stderr,
        )
        return 0

    print(
        f"self-check: probing {len(targets)} canaries...",
        file=sys.stderr,
    )

    # Use a single Phantom instance so the per-host limiter + caching
    # apply naturally. Cache disabled so we always hit the live site.
    phantom = Phantom(
        [s for s, _ in targets],
        concurrency=concurrency,
        timeout=timeout,
        impersonate=impersonate,
        cache=ResponseCache(enabled=False),
    )

    # Each canary needs its own variant — `run_many` expects ONE
    # variant list checked against ALL sites. We run targets
    # one-at-a-time so each site gets its own handle.
    rows: list[tuple[str, str, str, str, str]] = []
    # ^ (site, handle, status, reason, profile-summary)

    for site, handle in targets:
        try:
            results = await phantom.run_many([handle])
        except Exception as e:
            rows.append((
                site.name, handle, "error", type(e).__name__, "",
            ))
            continue
        # Find the result for this specific site (run_many returns all
        # configured sites; we filter).
        result = None
        for _, rs in results:
            for r in rs:
                if r.site == site.name and r.variant == handle:
                    result = r
                    break
            if result:
                break
        if result is None:
            rows.append((site.name, handle, "no_result", "", ""))
            continue
        if result.exists is True:
            p = result.profile or {}
            keys = [k for k in ("display_name", "followers", "photo") if p.get(k)]
            summary = ", ".join(keys) or "no fields extracted"
            status = "ok" if keys else "ok-thin"
            rows.append((
                site.name, handle, status, result.reason or "", summary,
            ))
        elif result.exists is False:
            rows.append((
                site.name, handle, "missing", result.reason or "", "",
            ))
        else:
            rows.append((
                site.name, handle, "unknown", result.reason or "",
                result.error or "",
            ))

    # Report.
    counts: dict[str, int] = {}
    for _, _, status, *_ in rows:
        counts[status] = counts.get(status, 0) + 1
    print(file=sys.stderr)
    print("self-check results:", file=sys.stderr)
    for status, n in sorted(counts.items()):
        print(f"  {status:12} {n}", file=sys.stderr)
    print(file=sys.stderr)

    # Detail rows — sort drifted first so they're obvious.
    sort_key = {"missing": 0, "error": 1, "unknown": 2, "ok-thin": 3,
                "no_result": 4, "ok": 5}
    rows.sort(key=lambda r: sort_key.get(r[2], 99))
    for site_name, handle, status, reason, summary in rows:
        if verbose or status != "ok":
            print(
                f"  [{status:9}] {site_name:14} @{handle:24} "
                f"{reason:20} {summary}",
                file=sys.stderr,
            )

    drifted = counts.get("missing", 0) + counts.get("unknown", 0) + counts.get("error", 0)
    if drifted:
        print(
            f"\nself-check: {drifted} site"
            f"{'s' if drifted != 1 else ''} drifted",
            file=sys.stderr,
        )
    else:
        print("\nself-check: all canaries cleanly FOUND", file=sys.stderr)

    # --- Streak tracking + auto-disable suggestion -------------------
    # Update per-site history. ok / ok-thin = reset streak; anything
    # else = increment. After N consecutive failures we print a
    # suggestion line: "consider disabling X". We never auto-write
    # sites.json — that's the user's call.
    hist = _load_history()
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
    to_flag: list[tuple[str, int]] = []
    for site_name, _handle, status, _reason, _summary in rows:
        slot = hist.get(site_name) or {}
        if status in ("ok", "ok-thin"):
            slot["streak"] = 0
            slot["last_seen_ok"] = now_iso
        else:
            slot["streak"] = int(slot.get("streak") or 0) + 1
        slot["last_status"] = status
        slot["last_run"] = now_iso
        hist[site_name] = slot
        if slot["streak"] >= _DISABLE_STREAK:
            to_flag.append((site_name, slot["streak"]))
    _save_history(hist)

    if to_flag:
        print(
            f"\nself-check: {len(to_flag)} site"
            f"{'s' if len(to_flag) != 1 else ''} have failed "
            f"{_DISABLE_STREAK}+ consecutive checks — consider disabling:",
            file=sys.stderr,
        )
        for site_name, streak in to_flag:
            print(
                f"  {site_name:14} (streak: {streak})  — add "
                f"`\"disabled\": true` to sites.json",
                file=sys.stderr,
            )

    return drifted
