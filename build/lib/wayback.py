"""Wayback Machine — historical-snapshot lookup for FOUND and UNKNOWN URLs.

A free public API that strengthens Phantom in two directions:

  1. **"This account existed historically"** — when a site cleanly
     returns MISSING or UNKNOWN today but the Wayback Machine has
     snapshots, that's evidence the username was used and then
     deleted/renamed. Useful for OSINT on accounts that have been
     scrubbed.

  2. **"How old is this account?"** — for current FOUND accounts, the
     earliest Wayback snapshot is often more reliable than the
     platform's self-reported `joined` date (Instagram's join date can
     be backdated; Wayback's first snapshot can't be).

We use the CDX API endpoint:

    https://web.archive.org/cdx/search/cdx?url=<u>&output=json&limit=1&filter=statuscode:200

…which returns a tiny JSON array. No auth, generous rate limits.

Failure mode: any network error → empty result, no crash. Wayback is a
nice-to-have signal; Phantom must never break if it's down.
"""
from __future__ import annotations

import asyncio
from typing import Optional
from urllib.parse import quote_plus

import aiohttp
from aiohttp import ClientTimeout


# Conservative concurrency cap — Wayback is public but a polite tool
# doesn't hammer it. ~5 concurrent requests across a 20-account scan is
# well under their stated rate limits.
_CDX_CONCURRENCY = 5
# CDX can be slow under load (we've seen 8-20s in the wild). Bump the
# timeout but keep the global scan latency bounded by the concurrency
# cap above.
_CDX_TIMEOUT = 25.0
_CDX_BASE = "https://web.archive.org/cdx/search/cdx"


async def _cdx_first_snapshot(
    session: aiohttp.ClientSession, url: str
) -> Optional[dict]:
    """Query CDX for the earliest 200-status snapshot of `url`.

    Returns a dict with `first_snapshot_date` (YYYY-MM-DD) and
    `snapshot_count` (≥1) on success, or None when no snapshots exist
    or the request fails.
    """
    params = {
        "url": url,
        "output": "json",
        "limit": "1",
        "filter": "statuscode:200",
        # Sort by timestamp ascending so limit=1 gives us the OLDEST.
        # The CDX API doesn't expose a count without a separate call,
        # so we make a tiny second call to get the count cheaply.
    }
    try:
        async with session.get(
            _CDX_BASE, params=params,
            timeout=ClientTimeout(total=_CDX_TIMEOUT),
        ) as r:
            if r.status != 200:
                return None
            data = await r.json(content_type=None)
    except Exception:
        return None
    if not isinstance(data, list) or len(data) < 2:
        return None
    # data[0] is the header row; data[1] is the oldest snapshot.
    row = data[1]
    if not row or len(row) < 2:
        return None
    timestamp = row[1]  # YYYYMMDDhhmmss
    if not isinstance(timestamp, str) or len(timestamp) < 8:
        return None
    iso_date = f"{timestamp[0:4]}-{timestamp[4:6]}-{timestamp[6:8]}"

    # Second call to count snapshots — same endpoint, no limit, fetch
    # just the timestamp column to keep payload small.
    count_params = {
        "url": url,
        "output": "json",
        "fl": "timestamp",
        "filter": "statuscode:200",
    }
    snapshot_count: Optional[int] = None
    try:
        async with session.get(
            _CDX_BASE, params=count_params,
            timeout=ClientTimeout(total=_CDX_TIMEOUT),
        ) as r2:
            if r2.status == 200:
                count_data = await r2.json(content_type=None)
                if isinstance(count_data, list):
                    # Subtract 1 for the header row.
                    snapshot_count = max(0, len(count_data) - 1)
    except Exception:
        pass

    return {
        "first_snapshot_date": iso_date,
        "snapshot_count": snapshot_count,
        "first_snapshot_url": (
            f"https://web.archive.org/web/{timestamp}/{url}"
        ),
    }


async def lookup_many(urls: list[str]) -> dict[str, dict]:
    """Look up Wayback snapshots for many URLs concurrently.

    Returns `{url: {first_snapshot_date, snapshot_count,
    first_snapshot_url}}` for the URLs that have at least one snapshot.
    URLs with no snapshots, or that failed to query, are absent from
    the result.
    """
    if not urls:
        return {}
    sem = asyncio.Semaphore(_CDX_CONCURRENCY)
    results: dict[str, dict] = {}

    async with aiohttp.ClientSession() as session:
        async def one(u: str):
            async with sem:
                info = await _cdx_first_snapshot(session, u)
                if info:
                    results[u] = info

        await asyncio.gather(*(one(u) for u in urls))

    return results


def attach_wayback_to_found(
    grouped: list,
    wayback_data: dict[str, dict],
) -> int:
    """Stamp wayback info onto each FOUND profile so JSON / HTML exports
    pick it up uniformly. Returns the count of profiles that got data.
    """
    n = 0
    for _, rs in grouped:
        for r in rs:
            if r.exists is not True:
                continue
            info = wayback_data.get(r.url) or wayback_data.get(
                getattr(r, "final_url", None) or ""
            )
            if not info:
                continue
            if r.profile is None:
                r.profile = {}
            r.profile["wayback_first_snapshot"] = info["first_snapshot_date"]
            if info.get("snapshot_count") is not None:
                r.profile["wayback_snapshot_count"] = info["snapshot_count"]
            r.profile["wayback_archive_url"] = info["first_snapshot_url"]
            n += 1
    return n
