"""Cross-link expansion.

Phantom extracts public profile data during its initial scan (display name,
photos, bios — and crucially, *links to other accounts*). This module
harvests those linked accounts and produces a list of fresh handles to
test, turning a single-username scan into a two-hop graph walk.

Data sources we mine, in order of reliability:

  1. Keybase `proofs` — cryptographic identity proofs. Bullet-proof: the
     handle is verified to belong to the same person.
  2. JSON-LD `sameAs` (Dev.to, About.me, Linktree-style profile JSON) —
     the platform explicitly declares "this user is also @x on Twitter".
  3. Linktree `links` — bio-link URLs the user manually curated.
  4. Site-specific fields (GitHub `x_handle`, GitHub `blog`, Twitter
     `website`) — the user pointed at it themselves.

Each candidate URL is parsed against a known-platform regex to recover
the bare handle. URLs that don't match any pattern are dropped — we
don't try to scrape arbitrary websites for handles, that's a different
tool entirely.
"""
from __future__ import annotations

import re
from typing import Iterable, Optional
from urllib.parse import urlparse

from models import CheckResult


# Per-platform handle extractors. Each entry maps a host (without "www.")
# to a regex applied against the URL path. The first capture group is
# the handle.
#
# Patterns are written tightly to avoid greedy matches over multi-segment
# paths like /users/123/posts/456 — we want the username segment, not a
# random path component.
_HANDLE_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("twitter.com",      re.compile(r"^/@?([A-Za-z0-9_]{1,30})/?$")),
    ("x.com",            re.compile(r"^/@?([A-Za-z0-9_]{1,30})/?$")),
    ("instagram.com",    re.compile(r"^/@?([A-Za-z0-9_.]{1,32})/?$")),
    ("tiktok.com",       re.compile(r"^/@([A-Za-z0-9_.]{1,32})/?$")),
    ("threads.net",      re.compile(r"^/@([A-Za-z0-9_.]{1,32})/?$")),
    ("threads.com",      re.compile(r"^/@([A-Za-z0-9_.]{1,32})/?$")),
    ("youtube.com",      re.compile(r"^/@([A-Za-z0-9_.-]{1,64})/?")),
    ("github.com",       re.compile(r"^/([A-Za-z0-9](?:[A-Za-z0-9-]{0,38})?)/?$")),
    ("gitlab.com",       re.compile(r"^/([A-Za-z0-9][A-Za-z0-9_.-]{0,38})/?$")),
    ("reddit.com",       re.compile(r"^/(?:u(?:ser)?|r)/([A-Za-z0-9_-]{1,20})/?")),
    ("old.reddit.com",   re.compile(r"^/(?:u(?:ser)?|r)/([A-Za-z0-9_-]{1,20})/?")),
    ("twitch.tv",        re.compile(r"^/([A-Za-z0-9_]{1,25})/?$")),
    ("m.twitch.tv",      re.compile(r"^/([A-Za-z0-9_]{1,25})/?$")),
    ("linkedin.com",     re.compile(r"^/in/([A-Za-z0-9_-]{1,100})/?")),
    ("facebook.com",     re.compile(r"^/([A-Za-z0-9.][A-Za-z0-9._-]{0,49})/?$")),
    ("pinterest.com",    re.compile(r"^/([A-Za-z0-9_]{1,30})/?$")),
    ("soundcloud.com",   re.compile(r"^/([A-Za-z0-9_-]{1,40})/?$")),
    ("bandcamp.com",     re.compile(r"^/?$")),  # subdomain form, handled below
    ("medium.com",       re.compile(r"^/@?([A-Za-z0-9_.-]{1,40})/?$")),
    ("dev.to",           re.compile(r"^/([A-Za-z0-9_]{1,40})/?$")),
    ("vimeo.com",        re.compile(r"^/([A-Za-z0-9_-]{1,40})/?$")),
    ("mixcloud.com",     re.compile(r"^/([A-Za-z0-9_-]{1,40})/?$")),
    ("dribbble.com",     re.compile(r"^/([A-Za-z0-9_-]{1,40})/?$")),
    ("behance.net",      re.compile(r"^/([A-Za-z0-9_-]{1,40})/?")),
    ("keybase.io",       re.compile(r"^/([A-Za-z0-9_]{1,16})/?$")),
    ("lichess.org",      re.compile(r"^/@/([A-Za-z0-9_-]{1,40})/?")),
    ("last.fm",          re.compile(r"^/user/([A-Za-z0-9_-]{1,40})/?")),
    ("about.me",         re.compile(r"^/([A-Za-z0-9_-]{1,40})/?$")),
    ("linktr.ee",        re.compile(r"^/([A-Za-z0-9_-]{1,40})/?$")),
    ("beacons.ai",       re.compile(r"^/([A-Za-z0-9_-]{1,40})/?$")),
    ("bio.link",         re.compile(r"^/([A-Za-z0-9_.-]{1,40})/?$")),
]

# Subdomain-style platforms (handle is the subdomain, not a path segment).
_SUBDOMAIN_PLATFORMS = {
    "bandcamp.com",
    "carrd.co",
}


def _normalize_host(host: str) -> str:
    """Drop leading 'www.' for matching."""
    host = host.lower().strip()
    if host.startswith("www."):
        host = host[4:]
    return host


def _extract_one(url: str) -> Optional[str]:
    """Return the bare handle for a URL, or None if no known platform
    matches it."""
    try:
        parsed = urlparse(url if "://" in url else f"https://{url}")
    except Exception:
        return None
    host = _normalize_host(parsed.netloc)
    if not host:
        return None

    # Subdomain-style platforms: handle is the leftmost subdomain.
    for base in _SUBDOMAIN_PLATFORMS:
        if host.endswith("." + base) and host != base:
            sub = host[: -(len(base) + 1)]
            if sub and sub != "www":
                return sub
        if host == base:
            return None  # bare bandcamp.com isn't a profile

    path = parsed.path or "/"
    for known_host, rx in _HANDLE_PATTERNS:
        if host == known_host or host.endswith("." + known_host):
            m = rx.search(path)
            if m and m.groups():
                return m.group(1)
            return None
    return None


# Source-of-handle confidence weights. Each tells us how much we should
# trust that a discovered handle actually belongs to the same person. The
# integer is added to the discovered account's starting confidence score
# when it's eventually scanned.
SOURCE_KEYBASE_PROOF = "keybase_proof"   # cryptographic, strongest
SOURCE_LINKED_ACCOUNT = "linked_account" # JSON-LD sameAs (about.me, dev.to)
SOURCE_GITHUB_HANDLE = "github_handle"   # GitHub's explicit x_handle field
SOURCE_WEBSITE = "website"               # the user's own website field
SOURCE_BIO_LINK = "bio_link"             # Linktree / Beacons curated link

SOURCE_WEIGHTS: dict[str, int] = {
    SOURCE_KEYBASE_PROOF: 30,
    SOURCE_GITHUB_HANDLE: 20,
    SOURCE_LINKED_ACCOUNT: 15,
    SOURCE_WEBSITE: 10,
    SOURCE_BIO_LINK: 5,
}


def _candidate_urls(profile: dict) -> list[tuple[str, str]]:
    """Pull every URL that might point at another social profile, paired
    with the source kind it came from. Stable order, deduplicated by URL
    (first source wins).
    """
    if not profile:
        return []
    out: list[tuple[str, str]] = []
    seen: set[str] = set()

    def push(value, source: str) -> None:
        if isinstance(value, str) and value:
            v = value.strip()
            if v and v not in seen:
                seen.add(v)
                out.append((v, source))
        elif isinstance(value, dict):
            for k in ("url", "link", "href"):
                push(value.get(k), source)
        elif isinstance(value, list):
            for item in value:
                push(item, source)

    # Each input field carries its own source-kind weighting.
    push(profile.get("proofs"), SOURCE_KEYBASE_PROOF)
    push(profile.get("linked_accounts"), SOURCE_LINKED_ACCOUNT)
    push(profile.get("links"), SOURCE_BIO_LINK)
    push(profile.get("website"), SOURCE_WEBSITE)
    push(profile.get("blog"), SOURCE_WEBSITE)
    gh_x = profile.get("x_handle") or profile.get("twitter")
    if isinstance(gh_x, str) and gh_x.strip():
        push(f"https://x.com/{gh_x.strip().lstrip('@')}", SOURCE_GITHUB_HANDLE)

    return out


def discover_new_handles(
    found: Iterable[CheckResult],
    already_tested: set[str],
) -> list[tuple[str, str]]:
    """Return [(handle, source_kind), ...] — fresh handles to scan, paired
    with the source that surfaced them so the caller can boost confidence
    on a strong-source discovery. Order is stable. When the same handle is
    discovered via multiple sources, the strongest source wins (rather
    than the first).
    """
    tested_lower = {h.lower() for h in already_tested}
    best: dict[str, tuple[str, str]] = {}  # lower(handle) -> (handle, source)
    order: list[str] = []
    for r in found:
        for url, source in _candidate_urls(r.profile or {}):
            handle = _extract_one(url)
            if not handle:
                continue
            key = handle.lower()
            if key in tested_lower:
                continue
            prev = best.get(key)
            if prev is None:
                best[key] = (handle, source)
                order.append(key)
            else:
                # Keep the strongest source if we see the same handle from
                # multiple places (e.g. someone has a Keybase proof AND a
                # Linktree link to the same Twitter — Keybase wins).
                if SOURCE_WEIGHTS.get(source, 0) > SOURCE_WEIGHTS.get(prev[1], 0):
                    best[key] = (handle, source)
    return [best[k] for k in order]
