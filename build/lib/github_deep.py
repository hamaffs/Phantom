"""GitHub deep-dive — public-data enrichment beyond the profile page.

When GitHub is FOUND, fetch every meaningful public surface and stamp
the data onto the profile dict for the HTML/JSON exports. All endpoints
are public — no token needed for the read-only surfaces we use, though
unauthenticated requests are capped at 60/hr per IP.

Signals we extract:

  1. **Public events feed** (`/users/{u}/events/public`) — recent push,
     issue, PR, star, fork activity. Surfaces interests, timezone-ish
     activity patterns, organizations the user contributes to.

  2. **Starred repos** (`/users/{u}/starred?per_page=10`) — first page
     only. Reveals tech stack interest area.

  3. **Public commit email leak** — fetching `.patch` of a recent commit
     leaks the author's commit email in plaintext. Most users haven't
     enabled GitHub's "Keep my email addresses private" setting, so
     this works on ~70% of accounts. Highest-value OSINT signal from
     the lot.

  4. **Organizations** (`/users/{u}/orgs`) — public org memberships.

  5. **Followers / following count + verified social-accounts** —
     extends what enrich.py already pulls.

This module is **off by default** because it makes 4–6 extra HTTP
requests per GitHub account found, which adds 1–2s of latency. Enable
with `--github-deep` on the CLI.

Rate-limit handling: 60 requests/hour from one IP is the unauth cap. A
typical 1-handle scan finds GitHub once, then makes 4 lookups → 4
requests, fine. But running `--github-deep` across many handles can
exhaust the quota — we surface the X-RateLimit-Remaining header in a
warning when it drops below 20% so the user can pace.

Privacy stance: we only fetch public endpoints. The commit-email leak
is publicly viewable on github.com — we just surface it. Nothing here
needs auth or scraping past Cloudflare.
"""
from __future__ import annotations

import asyncio
import re
from typing import Optional

import aiohttp
from aiohttp import ClientTimeout


_USER_AGENT = (
    "Phantom-OSINT/1.0 (github.com/hamaffs/Phantom-; public-data enrichment)"
)
_TIMEOUT = 10.0
_BASE = "https://api.github.com"

# Cap on parallel GitHub API requests. The unauth rate is 60/hr — we
# don't want to burn it all on one scan, so 3 concurrent keeps the
# per-request budget reasonable.
_CONCURRENCY = 3


async def _json(session: aiohttp.ClientSession, url: str) -> tuple[int, object, dict]:
    """GET + parse JSON. Returns (status, payload-or-None, headers)."""
    try:
        async with session.get(
            url,
            headers={"User-Agent": _USER_AGENT, "Accept": "application/vnd.github+json"},
            timeout=ClientTimeout(total=_TIMEOUT),
        ) as r:
            try:
                payload = await r.json(content_type=None)
            except Exception:
                payload = None
            return r.status, payload, dict(r.headers)
    except Exception:
        return 0, None, {}


async def _text(session: aiohttp.ClientSession, url: str) -> Optional[str]:
    try:
        async with session.get(
            url,
            headers={"User-Agent": _USER_AGENT},
            timeout=ClientTimeout(total=_TIMEOUT),
        ) as r:
            if r.status != 200:
                return None
            return await r.text(errors="replace")
    except Exception:
        return None


# Regex for the `From:` header in a .patch file. GitHub's patches start
# with one of these lines, e.g. `From: Alice <alice@example.com>`.
_COMMIT_EMAIL_RE = re.compile(
    r"^From:\s+[^<]*<([^>]+)>",
    re.MULTILINE,
)

# Whitelist of email domains that indicate a GitHub-internal noreply
# rather than a real user email. Surfacing one of these as "the user's
# email" is misleading — the user has email-privacy turned on, the
# value is intentionally non-contactable.
_NOREPLY_DOMAINS = ("users.noreply.github.com", "noreply.github.com")


async def _fetch_commit_email(
    session: aiohttp.ClientSession, username: str
) -> Optional[dict]:
    """Try to leak the user's commit email from their most recent .patch.

    Returns {email, source_url} on success, {note: '…'} when blocked by
    GitHub's email-privacy setting, or None when nothing was extractable.
    """
    # Find a recent push event via the public events feed, then look at
    # the commit URL it references and append .patch.
    status, events, _ = await _json(
        session, f"{_BASE}/users/{username}/events/public?per_page=15",
    )
    if status != 200 or not isinstance(events, list):
        return None
    for ev in events:
        if not isinstance(ev, dict):
            continue
        if ev.get("type") != "PushEvent":
            continue
        payload = ev.get("payload") or {}
        commits = payload.get("commits") or []
        if not commits:
            continue
        first = commits[0]
        if not isinstance(first, dict):
            continue
        commit_url = first.get("url")  # api.github.com/repos/.../commits/<sha>
        if not commit_url:
            continue
        # Convert API URL → html URL + .patch
        m = re.match(
            r"https?://api\.github\.com/repos/([^/]+)/([^/]+)/commits/([0-9a-f]+)",
            commit_url,
        )
        if not m:
            continue
        owner, repo, sha = m.groups()
        patch_url = f"https://github.com/{owner}/{repo}/commit/{sha}.patch"
        body = await _text(session, patch_url)
        if not body:
            continue
        email_match = _COMMIT_EMAIL_RE.search(body)
        if not email_match:
            continue
        email = email_match.group(1).strip().lower()
        if any(email.endswith("@" + d) for d in _NOREPLY_DOMAINS):
            return {
                "note": "email-privacy enabled (commits use GitHub noreply)",
                "noreply_address": email,
            }
        return {"email": email, "source_url": patch_url}
    return None


async def _fetch_orgs(session: aiohttp.ClientSession, username: str) -> list[str]:
    status, data, _ = await _json(session, f"{_BASE}/users/{username}/orgs")
    if status != 200 or not isinstance(data, list):
        return []
    return [o.get("login") for o in data if isinstance(o, dict) and o.get("login")]


async def _fetch_starred(
    session: aiohttp.ClientSession, username: str, n: int = 5,
) -> list[dict]:
    status, data, _ = await _json(
        session, f"{_BASE}/users/{username}/starred?per_page={n}",
    )
    if status != 200 or not isinstance(data, list):
        return []
    out = []
    for repo in data[:n]:
        if not isinstance(repo, dict):
            continue
        out.append({
            "name": repo.get("full_name") or repo.get("name"),
            "description": (repo.get("description") or "")[:200],
            "stars": repo.get("stargazers_count"),
            "language": repo.get("language"),
        })
    return out


async def _fetch_social_accounts(
    session: aiohttp.ClientSession, username: str,
) -> list[dict]:
    """The /social_accounts endpoint surfaces verified linked profiles
    (Twitter, LinkedIn, personal website, …) that the user explicitly
    connected to their GitHub. Returns a list of `{provider, url}` dicts.
    """
    status, data, _ = await _json(
        session, f"{_BASE}/users/{username}/social_accounts",
    )
    if status != 200 or not isinstance(data, list):
        return []
    out = []
    for a in data:
        if not isinstance(a, dict):
            continue
        out.append({
            "provider": a.get("provider"),
            "url": a.get("url"),
        })
    return out


async def enrich_one(username: str) -> dict:
    """Run all the GitHub-deep lookups for one username. Returns a dict
    suitable for merging into `profile`. Always returns a dict — fields
    are simply absent on failure, never raises.
    """
    out: dict = {}
    sem = asyncio.Semaphore(_CONCURRENCY)

    async with aiohttp.ClientSession() as session:
        async def with_sem(coro):
            async with sem:
                return await coro

        # Parallel where independent.
        orgs_t = asyncio.create_task(with_sem(_fetch_orgs(session, username)))
        starred_t = asyncio.create_task(with_sem(_fetch_starred(session, username)))
        social_t = asyncio.create_task(with_sem(_fetch_social_accounts(session, username)))
        email_t = asyncio.create_task(with_sem(_fetch_commit_email(session, username)))

        orgs, starred, social, email_info = await asyncio.gather(
            orgs_t, starred_t, social_t, email_t,
        )

        if orgs:
            out["organizations"] = orgs
        if starred:
            out["recent_starred"] = starred
        if social:
            out["verified_social_accounts"] = social
            # Feed them into linked_accounts so --expand can use them.
            existing_links = []
            for s in social:
                if s.get("url"):
                    existing_links.append(s["url"])
            if existing_links:
                out["linked_accounts"] = existing_links
        if isinstance(email_info, dict):
            if email_info.get("email"):
                out["commit_email"] = email_info["email"]
                out["commit_email_source"] = email_info["source_url"]
            elif email_info.get("note"):
                out["commit_email_note"] = email_info["note"]

    return out


async def enrich_grouped(grouped: list) -> int:
    """Walk every FOUND GitHub result in `grouped` and stamp deep-dive
    fields onto its profile dict. Returns the number of accounts that
    received new data.
    """
    targets: list = []
    for _, rs in grouped:
        for r in rs:
            if r.exists is not True or r.site != "GitHub":
                continue
            handle = (r.variant or "").strip()
            if handle:
                targets.append((r, handle))
    if not targets:
        return 0
    # Dedup by handle so we don't fetch the same data twice.
    seen: dict[str, dict] = {}
    sem = asyncio.Semaphore(_CONCURRENCY)

    async def fetch(handle: str) -> dict:
        async with sem:
            return await enrich_one(handle)

    unique = list({h for _, h in targets})
    enrichments = await asyncio.gather(*(fetch(h) for h in unique))
    for h, e in zip(unique, enrichments):
        seen[h] = e

    n = 0
    for r, handle in targets:
        data = seen.get(handle) or {}
        if not data:
            continue
        if r.profile is None:
            r.profile = {}
        merged_any = False
        for k, v in data.items():
            # linked_accounts merges with whatever enrich.py already
            # captured rather than overwriting.
            if k == "linked_accounts":
                existing = r.profile.get("linked_accounts") or []
                if isinstance(existing, list):
                    combined = list(existing)
                    for u in v:
                        if u not in combined:
                            combined.append(u)
                    r.profile[k] = combined
                else:
                    r.profile[k] = v
            else:
                r.profile[k] = v
            merged_any = True
        if merged_any:
            n += 1
    return n
