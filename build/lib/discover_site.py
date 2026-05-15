#!/usr/bin/env python3
"""Auto-propose a sites.json entry for a new platform.

Probes a URL pattern twice — once with a known-real username, once with a
known-fake one — and diffs the two responses to suggest:

  - valid_status / invalid_status (from the HTTP codes)
  - presence_text (substrings unique to the real-user response, anchored
    on the username so they substitute cleanly)
  - absence_text (well-known "not found" phrases that appear in the fake
    response but not the real one)
  - method ("status" if the codes differ cleanly, "message" otherwise)

Output is JSON printed to stdout, ready to paste into sites.json. The tool
suggests — the user verifies. Phantom's two-sided detection rule means
weak proposals get caught downstream by `validate_sites.py`.

Usage:
    python3 discover_site.py 'https://example.com/u/{username}' real_user_handle
    python3 discover_site.py 'https://example.com/u/{username}' real_user \\
        --name Example --category social --reliability 85

When the platform has TLS-fingerprint protection, add --impersonate to
route through curl_cffi (the same flag the scanner uses).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import re
import string
import sys
from typing import Optional
from urllib.parse import urlparse

import aiohttp

try:
    from curl_cffi.requests import AsyncSession as CurlSession  # type: ignore
    HAS_CURL_CFFI = True
except ImportError:
    CurlSession = None  # type: ignore
    HAS_CURL_CFFI = False


# Phrases that strongly suggest "user does not exist". Long-form matches
# win over short ones — "404" alone is too noisy.
_ABSENCE_PHRASES = (
    "user not found",
    "page not found",
    "doesn't exist",
    "does not exist",
    "no longer available",
    "this page isn't available",
    "this page isn’t available",   # curly apostrophe variant Instagram uses
    "sorry, this page",
    "user does not exist",
    "account not found",
    "profile not found",
    "channel not found",
    "no such user",
    "404 not found",
)

# Bot-wall hints — if either response looks like a WAF challenge, we can't
# auto-discover. Same list the scanner uses.
_BOT_TITLE_HINTS = (
    "just a moment",
    "verify you are human",
    "checking your browser",
    "attention required",
    "ddos protection",
    "client challenge",
)

_TITLE_RE = re.compile(r"<title[^>]*>([^<]*)</title>", re.IGNORECASE)


def _fake_username(real: str) -> str:
    """Generate a high-entropy handle that's vanishingly unlikely to exist.

    Mirrors the real handle's length when possible so length-based gating
    on the site doesn't trip differently between the two probes.
    """
    base = "".join(random.choices(string.ascii_lowercase, k=8))
    target_len = max(8, min(20, len(real)))
    suffix = "".join(random.choices(string.digits, k=max(0, target_len - len(base) - 4)))
    return f"zzz{base}{suffix}_x"[:target_len]


def _looks_like_bot_wall(body: str) -> bool:
    m = _TITLE_RE.search(body or "")
    if not m:
        return False
    title = m.group(1).strip().lower()
    return any(h in title for h in _BOT_TITLE_HINTS)


async def _fetch_aiohttp(url: str, timeout: float) -> tuple[int, str, str]:
    """(status, body, final_url) via aiohttp with browser-ish headers."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    async with aiohttp.ClientSession() as s:
        async with s.get(
            url,
            headers=headers,
            allow_redirects=True,
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as r:
            body = await r.text(errors="replace")
            return r.status, body, str(r.url)


async def _fetch_curl(url: str, timeout: float) -> tuple[int, str, str]:
    if CurlSession is None:
        raise RuntimeError("curl_cffi not installed — install it or skip --impersonate")
    async with CurlSession(impersonate="chrome") as s:
        r = await s.get(url, allow_redirects=True, timeout=timeout)
        body = r.text or ""
        return r.status_code, body, str(r.url)


async def fetch(url: str, *, impersonate: bool, timeout: float) -> tuple[int, str, str]:
    if impersonate and HAS_CURL_CFFI:
        return await _fetch_curl(url, timeout)
    return await _fetch_aiohttp(url, timeout)


def _looks_anchored(pattern: str) -> bool:
    """A presence_text pattern is 'anchored' if it carries at least one
    HTML/markup character (`<`, `>`, `"`, `'`, ` `, `:`) that distinguishes
    it from a pure URL fragment. Pure URL fragments echo the requested
    URL back and false-positive on every input. We require anchored
    patterns when status codes don't discriminate.
    """
    literal = pattern.replace("{username}", "")
    return any(c in literal for c in '<>"\' :>')


def _propose_presence(
    real_body: str,
    fake_body: str,
    username: str,
    fake_username: str = "",
) -> list[str]:
    """Suggest presence_text patterns: short substrings containing the
    username that appear in the real-user response but NOT in the fake
    response when the fake's handle is substituted.

    The critical check is the second one. A naive "does this literal
    appear in fake_body?" test fails on platforms that echo the requested
    URL back in their response — `/p/torvalds` won't be in the fake_body
    (which has `/p/zzzfake_x` instead), but `/p/{username}` substituted
    with anything will match the platform's echoed URL. Those patterns
    look "anchored" (have quotes / colons) but false-positive on every
    handle at scan time. So we substitute the FAKE handle into each
    candidate pattern and reject if THAT appears in fake_body — which
    proves the pattern is URL-echo.
    """
    if not username or username not in real_body:
        return []

    candidates: list[tuple[int, str]] = []  # (length, pattern)
    seen: set[str] = set()

    # Anchor markers we like: things that almost always wrap a username
    # in HTML — quotes, slashes, `@`, tag boundaries.
    pre_windows = (5, 12, 25)
    post_windows = (0, 3, 8)

    pos = 0
    while True:
        i = real_body.find(username, pos)
        if i < 0:
            break
        for pre in pre_windows:
            for post in post_windows:
                lo = max(0, i - pre)
                hi = min(len(real_body), i + len(username) + post)
                raw = real_body[lo:hi]
                if raw in fake_body:
                    continue  # raw form already in fake — not discriminating
                # Strip a leading partial entity (avoid matching mid-utf8/html).
                raw = raw.lstrip()
                if not raw or username not in raw:
                    continue
                pattern = raw.replace(username, "{username}")
                # Skip patterns that contain template-y braces or newlines.
                if "\n" in pattern or "\r" in pattern:
                    continue
                # URL-echo guard: substitute the fake handle and verify the
                # pattern doesn't match the fake body. If it does, the
                # site echoes the URL back and this pattern would
                # false-positive on every scan.
                if fake_username:
                    fake_substituted = pattern.replace("{username}", fake_username)
                    if fake_substituted in fake_body:
                        continue
                if pattern in seen:
                    continue
                seen.add(pattern)
                candidates.append((len(pattern), pattern))
        pos = i + len(username)

    # Prefer shortest patterns first, but require a strong anchor:
    #   - at least 3 chars of literal text outside the {username} placeholder
    #     (so `{username}` alone — which appears in every URL — is rejected)
    #   - total length ≥ 5
    # This stops the suggester from proposing patterns that match anywhere
    # the username happens to appear, e.g. in the requested URL itself.
    candidates.sort()
    out: list[str] = []
    for _, p in candidates:
        if len(p) < 5:
            continue
        literal = p.replace("{username}", "")
        if len(literal) < 3:
            continue
        out.append(p)
        if len(out) >= 3:
            break
    return out


def _propose_absence(fake_body: str, real_body: str) -> list[str]:
    """Suggest absence_text patterns: well-known not-found phrases that
    appear in the fake response but not the real one."""
    out: list[str] = []
    low_fake = fake_body.lower()
    low_real = real_body.lower()
    for phrase in _ABSENCE_PHRASES:
        if phrase in low_fake and phrase not in low_real:
            # Recover the case from the body (so we don't normalise the
            # smart-quote variants the platform actually ships).
            idx = low_fake.find(phrase)
            out.append(fake_body[idx : idx + len(phrase)])
        if len(out) >= 3:
            break
    return out


def build_entry(
    *,
    url_template: str,
    real_status: int,
    real_body: str,
    fake_status: int,
    fake_body: str,
    username: str,
    name: str,
    category: str,
    reliability: int,
    impersonate: bool,
    notes: list[str],
    fake_username: str = "",
) -> dict:
    presence = _propose_presence(real_body, fake_body, username, fake_username)
    absence = _propose_absence(fake_body, real_body)

    entry: dict = {
        "name": name,
        "category": category,
        "url": url_template,
        "method": "status" if real_status != fake_status else "message",
        "reliability": reliability,
    }
    if real_status:
        entry["valid_status"] = [real_status]
    if fake_status and fake_status != real_status:
        entry["invalid_status"] = [fake_status]
    if presence:
        entry["presence_text"] = presence
    if absence:
        entry["absence_text"] = absence
    if impersonate:
        entry["protection"] = ["tls_fingerprint"]
    return entry


def _human_diff_summary(
    real_status: int, fake_status: int,
    real_body: str, fake_body: str,
    username: str,
) -> list[str]:
    notes = [
        f"real-user response: status={real_status}, body={len(real_body)} bytes",
        f"fake-user response: status={fake_status}, body={len(fake_body)} bytes",
    ]
    if _looks_like_bot_wall(real_body) or _looks_like_bot_wall(fake_body):
        notes.append(
            "WARNING: one response looks like a bot-wall (Cloudflare / WAF). "
            "Auto-discovered patterns will be unreliable. Consider "
            "--impersonate, or hand-craft the entry."
        )
    if real_status == fake_status:
        notes.append(
            "same HTTP status for both probes — method=message required "
            "and presence_text MUST be strong."
        )
    if username not in real_body:
        notes.append(
            "WARNING: username not echoed in the real response. presence_text "
            "cannot be anchored on the username; this site may need a "
            "hand-written pattern."
        )
    real_title_m = _TITLE_RE.search(real_body)
    fake_title_m = _TITLE_RE.search(fake_body)
    if real_title_m and fake_title_m:
        notes.append(
            f"titles — real: {real_title_m.group(1).strip()[:80]!r}, "
            f"fake: {fake_title_m.group(1).strip()[:80]!r}"
        )
    return notes


async def discover(args: argparse.Namespace) -> int:
    if "{username}" not in args.url_template:
        print("error: URL template must contain {username}", file=sys.stderr)
        return 2

    real = args.real_user
    fake = args.fake_user or _fake_username(real)

    real_url = args.url_template.replace("{username}", real)
    fake_url = args.url_template.replace("{username}", fake)

    try:
        real_status, real_body, real_final = await fetch(
            real_url, impersonate=args.impersonate, timeout=args.timeout
        )
        fake_status, fake_body, fake_final = await fetch(
            fake_url, impersonate=args.impersonate, timeout=args.timeout
        )
    except Exception as e:
        print(f"error: probe failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    # Derive a default site name from the hostname if none was provided.
    if not args.name:
        host = urlparse(real_url).hostname or ""
        host = host.replace("www.", "").split(".")[0]
        args.name = host.capitalize() if host else "Site"

    notes = _human_diff_summary(
        real_status, fake_status, real_body, fake_body, real
    )
    entry = build_entry(
        url_template=args.url_template,
        real_status=real_status, real_body=real_body,
        fake_status=fake_status, fake_body=fake_body,
        username=real, fake_username=fake,
        name=args.name,
        category=args.category,
        reliability=args.reliability,
        impersonate=args.impersonate,
        notes=notes,
    )

    print("// diff notes:", file=sys.stderr)
    for n in notes:
        print(f"//   - {n}", file=sys.stderr)
    print("// proposed entry — paste into sites.json after review:",
          file=sys.stderr)
    print(json.dumps(entry, indent=2, ensure_ascii=False))

    # Heuristic for whether this looks usable. Hard rules:
    #   1. The real user MUST NOT have returned a 4xx. If they did, either
    #      our "known-real" handle is wrong or the site is gating
    #      everything (login wall, country block). Either way, presence
    #      patterns built from such a body will match URL artifacts and
    #      false-positive at scan time.
    #   2. If real and fake share a status, we need at least an
    #      absence_text pattern OR a presence_text pattern that isn't a
    #      pure URL fragment. Without either, presence alone (e.g.
    #      `et/u/{username}`) just echoes back the requested URL.
    if 400 <= real_status < 500:
        looks_usable = False
    else:
        looks_usable = (
            real_status != fake_status
            or entry.get("absence_text")
            or any(_looks_anchored(p) for p in entry.get("presence_text", []))
        )
    if not looks_usable:
        print(
            "WARNING: no clear discriminator found between real and fake "
            "responses. This site likely needs a hand-written entry or "
            "isn't suitable for username detection.",
            file=sys.stderr,
        )
        return 1
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(
        prog="discover_site",
        description="Probe a URL pattern and propose a sites.json entry.",
    )
    p.add_argument(
        "url_template",
        help="URL with {username} placeholder, e.g. https://example.com/u/{username}",
    )
    p.add_argument("real_user", help="a known-existing username on the platform")
    p.add_argument(
        "--fake-user", default=None,
        help="known-nonexistent username (default: a random high-entropy handle)",
    )
    p.add_argument("--name", default=None, help="display name for sites.json entry")
    p.add_argument(
        "--category", default="other",
        choices=("dev", "social", "gaming", "media", "forum", "other"),
    )
    p.add_argument("--reliability", type=int, default=80)
    p.add_argument(
        "--impersonate", action="store_true",
        help="route through curl_cffi with Chrome TLS impersonation (for "
             "Cloudflare-protected sites)",
    )
    p.add_argument("--timeout", type=float, default=20.0)
    args = p.parse_args(argv)
    return asyncio.run(discover(args))


if __name__ == "__main__":
    raise SystemExit(main())
