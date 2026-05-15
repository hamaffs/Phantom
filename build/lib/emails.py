"""Hunter.io email-finder integration. Skipped silently when no key is
configured; per-site results are stamped onto each FOUND profile dict.
"""
from __future__ import annotations

import asyncio
import re
from typing import Optional

import aiohttp
from aiohttp import ClientTimeout

from models import CheckResult
from terminal import _c


# Domains whose accounts don't issue user emails (social/streaming/forum
# platforms). Hunter.io will happily search them and return spurious
# corporate or generic addresses, so skip the call entirely.
_HUNTER_DOMAIN_BLOCKLIST = frozenset({
    "instagram.com", "twitter.com", "x.com", "tiktok.com",
    "threads.net", "facebook.com", "youtube.com", "twitch.tv",
    "twitchtracker.com",
    "reddit.com", "pastebin.com", "pinterest.com", "tumblr.com",
    "soundcloud.com", "telegram.org", "discord.com", "linkedin.com",
})

# Minimum Hunter.io score to surface as a real match. Below this the
# result is recorded as low-confidence and the address is dropped.
_HUNTER_MIN_SCORE = 70


_NAME_TITLE_SEPARATOR_RE = re.compile(r"\s[-|·–—]\s|&")

def _looks_like_real_name(s: str) -> bool:
    """Cheap filter for Hunter.io: a real full name has at least two
    whitespace-separated parts with the first and last each ≥2 chars,
    AND no page-title-style separators (" - ", " | ", " · ", "&"). The
    last check catches og:title strings that platforms ship as display
    names — e.g. twitchtracker emits "<user> - Streamer Overview & Stats"
    which passes the word/length test but Hunter rejects with "Full name
    contains invalid characters".
    """
    text = s.strip()
    if _NAME_TITLE_SEPARATOR_RE.search(text):
        return False
    parts = text.split()
    if len(parts) < 2:
        return False
    return len(parts[0]) >= 2 and len(parts[-1]) >= 2

async def discover_emails(
    found: list["CheckResult"],
    api_key: str,
    timeout: float = 15.0,
) -> dict[str, dict]:
    """Query Hunter.io email-finder for each FOUND profile that has a
    display name. Uses the site's hostname as the company domain — Hunter
    expects an org domain, but we ship what the user has on hand and let
    the score speak for itself.

    Per-site outcome shapes:
      success     {email, score, domain}
      low score   {low_confidence: True, score, domain}  (email dropped)
      api error   {error, domain}
      pre-skip    {skipped: <reason>, domain?}

    Identical (full_name, domain) pairs are de-duplicated to one API
    call. Domains in _HUNTER_DOMAIN_BLOCKLIST (social/streaming
    platforms that don't issue user emails) are skipped before any
    network call. Successful results below _HUNTER_MIN_SCORE are
    discarded as low-confidence rather than surfaced as a match.
    """
    from urllib.parse import urlparse

    queue: list[tuple["CheckResult", str, str]] = []
    skipped: dict[str, dict] = {}
    for r in found:
        display = ((r.profile or {}).get("display_name") or "").strip()
        if not display:
            skipped[r.site] = {"skipped": "no display_name"}
            continue
        if not _looks_like_real_name(display):
            skipped[r.site] = {"skipped": "no real name detected"}
            continue
        host = (urlparse(r.url).hostname or "").lower()
        if host.startswith("www."):
            host = host[4:]
        if not host:
            skipped[r.site] = {"skipped": "no domain"}
            continue
        if host in _HUNTER_DOMAIN_BLOCKLIST:
            skipped[r.site] = {"skipped": "social platform", "domain": host}
            continue
        queue.append((r, display, host))

    if not queue:
        return skipped

    cache: dict[tuple[str, str], dict] = {}
    out: dict[str, dict] = dict(skipped)
    sem = asyncio.Semaphore(5)

    async with aiohttp.ClientSession() as session:
        async def lookup(r, full_name, domain):
            cache_key = (full_name.lower(), domain)
            if cache_key in cache:
                return r.site, dict(cache[cache_key])
            params = {
                "domain": domain,
                "full_name": full_name,
                "api_key": api_key,
            }
            async with sem:
                try:
                    async with session.get(
                        "https://api.hunter.io/v2/email-finder",
                        params=params,
                        timeout=ClientTimeout(total=timeout),
                    ) as resp:
                        try:
                            payload = await resp.json(content_type=None)
                        except Exception:
                            payload = {}
                        if resp.status == 401:
                            info = {"error": "invalid Hunter.io API key (401)", "domain": domain}
                        elif resp.status == 429:
                            info = {"error": "Hunter.io rate-limited (429)", "domain": domain}
                        elif resp.status != 200:
                            err = "http " + str(resp.status)
                            errs = payload.get("errors") if isinstance(payload, dict) else None
                            if isinstance(errs, list) and errs and isinstance(errs[0], dict):
                                err = errs[0].get("details") or errs[0].get("id") or err
                            info = {"error": err, "domain": domain}
                        else:
                            data = (payload.get("data") if isinstance(payload, dict) else None) or {}
                            email = data.get("email") or None
                            score = data.get("score")
                            if (
                                email
                                and isinstance(score, (int, float))
                                and score < _HUNTER_MIN_SCORE
                            ):
                                info = {
                                    "low_confidence": True,
                                    "score": score,
                                    "domain": domain,
                                }
                            else:
                                info = {
                                    "email": email,
                                    "score": score,
                                    "domain": domain,
                                }
                except asyncio.TimeoutError:
                    info = {"error": "timeout", "domain": domain}
                except Exception as e:
                    info = {"error": type(e).__name__, "domain": domain}
            cache[cache_key] = info
            return r.site, dict(info)

        results = await asyncio.gather(*(lookup(r, n, d) for r, n, d in queue))

    for site, info in results:
        out[site] = info
    return out


def _attach_emails_to_found(
    grouped: list[tuple[str, list["CheckResult"]]],
    emails: dict[str, dict],
) -> int:
    """Stamp the email-finder result onto each FOUND result's profile
    dict so JSON export and HTML render pick it up uniformly. Returns
    the count of profiles with an actual email address attached."""
    n = 0
    for _, rs in grouped:
        for r in rs:
            if r.exists is not True:
                continue
            info = emails.get(r.site)
            if not info:
                continue
            if r.profile is None:
                r.profile = {}
            if info.get("email"):
                r.profile["email"] = info["email"]
                if info.get("score") is not None:
                    r.profile["email_score"] = info["score"]
                if info.get("domain"):
                    r.profile["email_domain"] = info["domain"]
                n += 1
            elif info.get("low_confidence"):
                r.profile["email_low_confidence"] = True
                if info.get("score") is not None:
                    r.profile["email_score"] = info["score"]
                if info.get("domain"):
                    r.profile["email_domain"] = info["domain"]
            elif info.get("error"):
                r.profile["email_error"] = info["error"]
    return n

def _print_emails_section(
    found: list["CheckResult"],
    emails: dict[str, dict],
    color: bool,
) -> None:
    """Print a [ EMAILS ] block under the FOUND list with one line per
    site that produced an email or a per-site error/skip note."""
    if not emails:
        return
    rows = []
    n_emails = 0
    for r in found:
        info = emails.get(r.site)
        if not info:
            continue
        if info.get("email"):
            n_emails += 1
            score = info.get("score")
            tail = f" (score {score})" if score is not None else ""
            rows.append((r.site, info["email"] + tail, "ok"))
        elif info.get("low_confidence"):
            score = info.get("score")
            tail = f" (score {score})" if score is not None else ""
            rows.append((r.site, f"low confidence{tail}", "dim"))
        elif info.get("error"):
            rows.append((r.site, f"error: {info['error']}", "err"))
        elif info.get("skipped"):
            rows.append((r.site, f"skipped: {info['skipped']}", "dim"))
        else:
            rows.append((r.site, "no match", "dim"))

    if not rows:
        return
    b, x, dim, g, r_ = (
        _c(color, "bold"), _c(color, "reset"), _c(color, "dim"),
        _c(color, "green"), _c(color, "red"),
    )
    print(f"\n{b}[ EMAILS ]{x}{b} {n_emails}{x}  {dim}(via Hunter.io){x}")
    for site, msg, kind in rows:
        col = g if kind == "ok" else (r_ if kind == "err" else dim)
        print(f"  {b}{site:<14}{x} {col}{msg}{x}")
