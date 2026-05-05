"""Profile enrichment.

For sites where we got a FOUND, try to pull whatever public profile data the
SSR'd page hands us — display name, bio, photo, follower counts, location,
joined date, verified flag — without making any extra HTTP requests.

Three layers:

1. **Generic OpenGraph** — `og:title`, `og:image`, `og:description`,
   `twitter:image`. Covers most of the non-SPA platforms (GitHub, Pastebin,
   YouTube, Threads, Letterboxd, Mastodon, etc.).
2. **Per-site extractors** — for the SPA platforms (Twitter, TikTok,
   Instagram) that omit OpenGraph or hide it behind login walls, parse the
   embedded JSON state directly.
3. **Skip empty values** — Instagram literally serves
   `<meta property="og:image" content="">` for incomplete profiles, so any
   extracted string is checked for non-emptiness before being returned.

Public-only data: this is pure HTML scraping, no auth, no API keys, no
session cookies. Everything we surface here is already visible to anyone
who opens the URL in a browser — the tool just collects it consistently
across platforms.
"""

from __future__ import annotations

import html
import json
import re
from html import unescape
from typing import Optional
from urllib.parse import urljoin

# ---------------------------------------------------------------------------
# Generic meta extraction
# ---------------------------------------------------------------------------

# Match property/name first, content first. Both attribute orders exist.
_META_RE = re.compile(
    r'<meta\b'
    r'(?=[^>]*\b(?:property|name)\s*=\s*["\'](?P<key>[^"\']+)["\'])'
    r'(?=[^>]*\bcontent\s*=\s*["\'](?P<val>[^"\']*)["\'])'
    r'[^>]*>',
    re.IGNORECASE,
)


def _meta_map(body: str) -> dict[str, str]:
    """Return a {og:key -> value} map of meta tags, lowercase keys."""
    out: dict[str, str] = {}
    for m in _META_RE.finditer(body):
        k = m.group("key").strip().lower()
        v = unescape(m.group("val")).strip()
        if v and k not in out:
            out[k] = v
    return out


def _normalise_query(url: str) -> str:
    """Fix double `?` patterns we see in the wild (notably GitHub's
    `og:image` content).

    `https://x/u/N?v=4?s=400` -> `https://x/u/N?v=4&s=400`
    """
    if "?" not in url:
        return url
    head, _, tail = url.partition("?")
    if "?" in tail:
        tail = tail.replace("?", "&")
    return f"{head}?{tail}"


def _abs_url(url: Optional[str], base: str) -> Optional[str]:
    if not url:
        return None
    if url.startswith("//"):
        url = "https:" + url
    elif not url.startswith(("http://", "https://")):
        url = urljoin(base, url)
    return _normalise_query(url)


def extract_meta(body: str, base_url: str) -> dict:
    """OpenGraph / twitter-card tags. Empty values are dropped."""
    meta = _meta_map(body)
    info: dict = {}
    photo = meta.get("og:image") or meta.get("twitter:image")
    if photo:
        info["photo"] = _abs_url(photo, base_url)
    if meta.get("og:title"):
        info["display_name"] = meta["og:title"]
    if meta.get("og:description"):
        info["bio"] = meta["og:description"]
    return info


# ---------------------------------------------------------------------------
# Twitter / X
# ---------------------------------------------------------------------------

def _grab_str(body: str, key: str) -> Optional[str]:
    m = re.search(rf'"{re.escape(key)}":"((?:\\.|[^"\\])*)"', body)
    if not m:
        return None
    raw = m.group(1)
    try:
        return json.loads(f'"{raw}"')
    except Exception:
        return raw


def _grab_int(body: str, key: str) -> Optional[int]:
    m = re.search(rf'"{re.escape(key)}":(\d+)', body)
    return int(m.group(1)) if m else None


def _grab_bool(body: str, key: str) -> Optional[bool]:
    m = re.search(rf'"{re.escape(key)}":(true|false)\b', body)
    return m.group(1) == "true" if m else None


def extract_twitter(body: str, username: str) -> dict:
    """Twitter/X: locate the user's legacy v1.1 user object and pull fields.

    The page embeds a hydration blob with `"screen_name":"<user>"`. We anchor
    on that and parse a window around it so fields belonging to *other* users
    in the response (recommendations, etc.) don't bleed into the result.
    """
    needle = f'"screen_name":"{username}"'
    idx = body.find(needle)
    if idx < 0:
        return {}
    section = body[max(0, idx - 4000): idx + 6000]

    info: dict = {}
    name = _grab_str(section, "name")
    if name:
        info["display_name"] = name
    desc = _grab_str(section, "description")
    if desc:
        info["bio"] = desc
    loc = _grab_str(section, "location")
    if loc:
        info["location"] = loc
    photo = _grab_str(section, "profile_image_url_https")
    if photo:
        # twimg gives _normal (48px); upgrade to _400x400 for the report
        info["photo"] = photo.replace("_normal.", "_400x400.")
    fc = _grab_int(section, "followers_count")
    if fc is not None:
        info["followers"] = fc
    fr = _grab_int(section, "friends_count")
    if fr is not None:
        info["following"] = fr
    sc = _grab_int(section, "statuses_count")
    if sc is not None:
        info["posts"] = sc
    created = _grab_str(section, "created_at")
    if created:
        info["joined"] = created
    ver = _grab_bool(section, "verified")
    if ver is not None:
        info["verified"] = ver
    return info


# ---------------------------------------------------------------------------
# TikTok
# ---------------------------------------------------------------------------

_TIKTOK_SCRIPT_RE = re.compile(
    r'<script[^>]*\bid=["\']__UNIVERSAL_DATA_FOR_REHYDRATION__["\'][^>]*>(.*?)</script>',
    re.DOTALL,
)


def extract_tiktok(body: str, username: str) -> dict:
    """TikTok: parse the universal-data script JSON properly."""
    m = _TIKTOK_SCRIPT_RE.search(body)
    if not m:
        return {}
    try:
        data = json.loads(m.group(1))
    except Exception:
        return {}
    scope = data.get("__DEFAULT_SCOPE__", {})
    detail = scope.get("webapp.user-detail", {})
    user_info = detail.get("userInfo", {})
    user = user_info.get("user", {}) or {}
    stats = user_info.get("stats", {}) or {}

    if user.get("uniqueId") and user["uniqueId"] != username:
        return {}

    info: dict = {}
    if user.get("nickname"):
        info["display_name"] = user["nickname"]
    if user.get("signature"):
        info["bio"] = user["signature"]
    photo = user.get("avatarLarger") or user.get("avatarMedium") or user.get("avatarThumb")
    if photo:
        info["photo"] = photo
    if "verified" in user:
        info["verified"] = bool(user["verified"])
    if "privateAccount" in user:
        info["private"] = bool(user["privateAccount"])
    if user.get("region"):
        info["location"] = user["region"]
    for src, dst in (
        ("followerCount", "followers"),
        ("followingCount", "following"),
        ("videoCount", "posts"),
        ("heartCount", "hearts"),
    ):
        if src in stats:
            info[dst] = int(stats[src])
    return info


# ---------------------------------------------------------------------------
# Instagram
# ---------------------------------------------------------------------------

# The og:description on a public Instagram profile follows the format:
#   "1,234 Followers, 567 Following, 89 Posts - See Instagram photos and..."
_IG_STATS_RE = re.compile(
    r"([\d,.]+)\s+Followers?,\s+([\d,.]+)\s+Following,\s+([\d,.]+)\s+Posts?",
    re.IGNORECASE,
)
# og:title pattern: "Display Name (@username) • Instagram photos and videos"
_IG_TITLE_RE = re.compile(
    r"^(.*?)\s*\(@[^)]+\)\s*[•·]\s*Instagram",
)


def _parse_ig_count(s: str) -> int:
    return int(s.replace(",", "").replace(".", ""))


def extract_instagram(body: str, username: str) -> dict:
    meta = _meta_map(body)
    info: dict = {}
    title = meta.get("og:title", "")
    desc = meta.get("og:description", "")
    if title:
        m = _IG_TITLE_RE.match(title)
        if m and m.group(1).strip():
            info["display_name"] = m.group(1).strip()
    if desc:
        m = _IG_STATS_RE.search(desc)
        if m:
            info["followers"] = _parse_ig_count(m.group(1))
            info["following"] = _parse_ig_count(m.group(2))
            info["posts"] = _parse_ig_count(m.group(3))
    # The og:image is sometimes empty; only keep non-empty.
    if meta.get("og:image"):
        info["photo"] = meta["og:image"]
    # Bio falls back to the description if it doesn't look like a stats line.
    if desc and "Followers" not in desc:
        info["bio"] = desc
    return info


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

_HUMAN_NUMBER_RE = re.compile(r"^([\d.,]+)\s*([KMB])?", re.IGNORECASE)


def _parse_human_number(s: Optional[str]) -> Optional[int]:
    """Turn '29.5K', '12,345', '1.2M', '301k followers' into an int."""
    if not s:
        return None
    s = str(s).replace(",", ".").strip()
    m = _HUMAN_NUMBER_RE.match(s)
    if not m:
        return None
    try:
        n = float(m.group(1))
    except ValueError:
        return None
    unit = (m.group(2) or "").upper()
    mult = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}.get(unit, 1)
    return int(n * mult)


# ---------------------------------------------------------------------------
# GitHub
# ---------------------------------------------------------------------------

def extract_github(body: str, username: str) -> dict:
    """GitHub /<username> profile page.

    GitHub serves a *fully* SSR'd profile so we can get bio, location,
    company, blog, X handle, follower / following counts, and the names
    of the user's pinned repositories — all without an API token.
    """
    info = extract_meta(body, f"https://github.com/{username}")
    # GitHub's display name lives in `<span itemprop="name">` (separate
    # from the @username in `<span itemprop="additionalName">`). Replace
    # the og:title (which is "<user> - Overview") with the real name.
    m = re.search(
        r'<span[^>]+itemprop=["\']name["\'][^>]*>\s*([^<]+?)\s*</span>',
        body, re.IGNORECASE,
    )
    if m and m.group(1).strip():
        info["display_name"] = unescape(m.group(1).strip())
    else:
        # Strip the "<user> - Overview" suffix.
        title = info.get("display_name", "")
        if title.endswith(" - Overview"):
            info["display_name"] = title[: -len(" - Overview")]

    # Drop the og:description boilerplate ("X has N repositories available.
    # Follow their code on GitHub.") — it's not a real bio.
    if info.get("bio", "").startswith(("Follow ", username)) and "repositor" in info.get("bio", ""):
        info.pop("bio", None)

    # Real bio from the profile div (may be empty / hidden).
    m = re.search(
        r'<div[^>]+user-profile-bio[^>]*>(?P<inner>.*?)</div>',
        body, re.S,
    )
    if m:
        text = re.sub(r"<[^>]+>", "", m.group("inner")).strip()
        if text:
            info["bio"] = unescape(text)

    # Followers / following — each lives in a span next to a labelled link.
    for label, key in (("followers", "followers"), ("following", "following")):
        m = re.search(
            rf'tab={label}"[\s\S]+?text-bold[^>]*>([^<]+)<',
            body,
        )
        if m:
            n = _parse_human_number(m.group(1))
            if n is not None:
                info[key] = n

    # Optional vcard details: company, location, blog/site, X handle.
    for itemprop, key in (
        ("worksFor", "company"),
        ("homeLocation", "location"),
        ("url", "website"),
    ):
        m = re.search(
            rf'<li[^>]+itemprop="{itemprop}"[^>]*aria-label="[^:]+:\s*([^"]+)"',
            body,
        )
        if m:
            info[key] = unescape(m.group(1).strip())
    m = re.search(
        r'<li[^>]+itemprop="twitter"[^>]*>\s*(?:<svg[\s\S]+?</svg>)?\s*'
        r'<a[^>]+>\s*<div[^>]*>\s*@?([A-Za-z0-9_]+)',
        body,
    )
    if m:
        info["twitter_handle"] = m.group(1)

    # Pinned repos — we want the names, not counts.
    pinned: list[str] = []
    for m in re.finditer(
        r'<a[^>]+href="/' + re.escape(username) + r'/([A-Za-z0-9_.-]+)"'
        r'[^>]+data-hovercard-type="repository"',
        body,
    ):
        name = m.group(1)
        if name not in pinned:
            pinned.append(name)
        if len(pinned) >= 6:
            break
    if pinned:
        info["pinned_repos"] = pinned

    # Public repository count (sidebar Counter).
    m = re.search(
        r'Repositories\s*<span[^>]+Counter[^>]*>\s*([\d,]+)\s*</span>',
        body,
    )
    if m:
        info["posts"] = _parse_human_number(m.group(1))  # reuse "posts" slot

    return info


# ---------------------------------------------------------------------------
# YouTube
# ---------------------------------------------------------------------------

_YT_SUBS_RE = re.compile(
    r'"subscriberCountText":\s*\{[^}]*?'
    r'"(?:simpleText|accessibility)"[^}]*?"(?:simpleText|label)":\s*"([^"]+)"',
    re.IGNORECASE,
)
_YT_VIDEOS_RE = re.compile(
    r'"videosCountText":\s*\{[^}]*?"text":\s*"([\d,. ]+)"'
)
_YT_VIEWS_RE = re.compile(r'"viewCountText":\s*"([\d,. ]+)\s*views?"')
_YT_COUNTRY_RE = re.compile(r'"country":\s*"([^"]+)"')
_YT_JOINED_RE = re.compile(
    r'"joinedDateText":\s*\{[^}]*?"text":\s*"Joined\s+([^"]+)"'
)
_YT_DESC_RE = re.compile(r'"description":\s*"((?:\\.|[^"\\])*)"')


def extract_youtube(body: str, username: str) -> dict:
    info = extract_meta(body, f"https://www.youtube.com/@{username}")
    m = _YT_SUBS_RE.search(body)
    if m:
        # The string is something like "29.5M subscribers" or "1,234 subscribers".
        raw = m.group(1)
        # Strip trailing 'subscribers'
        clean = re.sub(r"\s*subscribers?\s*", "", raw, flags=re.I).strip()
        n = _parse_human_number(clean)
        if n is not None:
            info["followers"] = n  # subs are followers
    m = _YT_VIDEOS_RE.search(body)
    if m:
        n = _parse_human_number(m.group(1))
        if n is not None:
            info["posts"] = n
    m = _YT_VIEWS_RE.search(body)
    if m:
        info["views"] = _parse_human_number(m.group(1))
    m = _YT_COUNTRY_RE.search(body)
    if m:
        info["location"] = m.group(1)
    m = _YT_JOINED_RE.search(body)
    if m:
        info["joined"] = m.group(1).strip()
    m = _YT_DESC_RE.search(body)
    if m and m.group(1):
        try:
            info["bio"] = json.loads(f'"{m.group(1)}"')
        except Exception:
            info["bio"] = m.group(1)
    return info


# ---------------------------------------------------------------------------
# Reddit (old.reddit.com profile page — not the API; we already used the API
# for detection but old.reddit ships more public stats in plain HTML).
# ---------------------------------------------------------------------------

def extract_reddit(body: str, username: str) -> dict:
    info: dict = {}
    # `<span class="age">a redditor for <time ...>X years</time></span>`
    m = re.search(
        r'<span class="age">([^<]+<time[^>]*>[^<]+</time>[^<]*)</span>',
        body,
    )
    if m:
        text = re.sub(r"<[^>]+>", "", m.group(1)).strip()
        if text:
            info["joined"] = text
    # Karma counters.
    for label, key in (("post karma", "post_karma"), ("comment karma", "comment_karma")):
        m = re.search(
            rf'<span class="karma[^"]*">([\d,]+)</span>\s*<span[^>]*>{label}',
            body, re.IGNORECASE,
        )
        if m:
            info[key] = _parse_human_number(m.group(1))
    # Aggregate karma if either side present.
    if "post_karma" in info or "comment_karma" in info:
        info["karma"] = (info.get("post_karma") or 0) + (info.get("comment_karma") or 0)
    return info


# ---------------------------------------------------------------------------
# Steam
# ---------------------------------------------------------------------------

def extract_steam(body: str, username: str) -> dict:
    info = extract_meta(body, f"https://steamcommunity.com/id/{username}/")
    # Real name
    m = re.search(
        r'<bdi>\s*([^<]+?)\s*</bdi>\s*<img[^>]+countryflag',
        body,
    )
    if m:
        info["display_name"] = unescape(m.group(1).strip())
    # Country (alt= on the flag)
    m = re.search(r'class="header_real_name[^"]*"[\s\S]*?>([^<]+)</span>\s*<img[^>]+alt="([^"]+)"', body)
    if m:
        info["location"] = m.group(2).strip()
    # Profile level
    m = re.search(r'<span class="friendPlayerLevelNum">(\d+)</span>', body)
    if m:
        info["steam_level"] = int(m.group(1))
    # Games count appears on the inventory link.
    m = re.search(r'data-tooltip-html="[^"]*?(\d[\d,]*)\s+games', body)
    if m:
        info["games"] = _parse_human_number(m.group(1))
    return info


# ---------------------------------------------------------------------------
# Lichess
# ---------------------------------------------------------------------------

def extract_lichess(body: str, username: str) -> dict:
    info = extract_meta(body, f"https://lichess.org/@/{username}")
    # Title / display name from a header span.
    m = re.search(
        r'<span class="title"[^>]*>([^<]+)</span>\s*<span[^>]*>([^<]+)</span>',
        body,
    )
    # Lichess shows ratings as "<rating>" next to perf icons. There's a
    # primary rating in `data-icon`-decorated table rows.
    ratings: dict = {}
    for m in re.finditer(
        r'<a[^>]+href="/@/[^/]+/perf/(\w+)"[^>]*>[\s\S]{0,200}?'
        r'<rating>\s*([\d?]+)',
        body,
    ):
        perf, rating = m.group(1), m.group(2)
        if rating != "?":
            ratings[perf] = int(rating)
    if ratings:
        info["lichess_ratings"] = ratings
        # Pick "best" rating across blitz/rapid/classical for the headline.
        best = max(ratings.values())
        info["rating"] = best
    # Total games played.
    m = re.search(r'>\s*([\d,]+)\s+games\s*played\b', body, re.IGNORECASE)
    if m:
        info["posts"] = _parse_human_number(m.group(1))
    return info


# ---------------------------------------------------------------------------
# Threads (Meta product, similar to Instagram)
# ---------------------------------------------------------------------------

_THREADS_STATS_RE = re.compile(
    r"([\d,.]+)\s+Followers?\s*[•·]\s*([\d,.]+)\s+Threads?",
    re.IGNORECASE,
)


def extract_threads(body: str, username: str) -> dict:
    info = extract_meta(body, f"https://www.threads.com/@{username}")
    # og:description carries the public stats.
    desc = info.get("bio") or ""
    m = _THREADS_STATS_RE.search(desc)
    if m:
        info["followers"] = _parse_human_number(m.group(1))
        info["posts"] = _parse_human_number(m.group(2))
        # The "real" bio appears after the stats line on Threads' meta tags.
        tail = desc.split("•", 1)[-1] if "•" in desc else ""
        if tail and "See" in tail:
            info["bio"] = re.split(r"\s+See ", tail, 1)[0].strip()
    # Strip the title decoration "@user • Threads, Say more"
    title = info.get("display_name") or ""
    m = re.match(r"@(\S+)\s*[•·]\s*Threads", title)
    if m:
        info["display_name"] = m.group(1)
    return info


_PER_SITE = {
    "Twitter": extract_twitter,
    "TikTok": extract_tiktok,
    "Instagram": extract_instagram,
    "GitHub": extract_github,
    "YouTube": extract_youtube,
    "Reddit": extract_reddit,
    "Steam": extract_steam,
    "Lichess": extract_lichess,
    "Threads": extract_threads,
}


def extract_profile(site_name: str, body: str, base_url: str, username: str) -> dict:
    """Return what we can pull from `body` about the user.

    Sites listed in `_PER_SITE` use ONLY their custom extractor — they
    typically have layered metadata (a misleading SSR shell, embedded JSON)
    where blindly mixing in OpenGraph would produce wrong fields.
    Everything else uses the generic OpenGraph reader.
    """
    site_fn = _PER_SITE.get(site_name)
    info = site_fn(body, username) if site_fn else extract_meta(body, base_url)
    # Final scrub: drop empty strings, keep zeros and False.
    return {k: v for k, v in info.items() if v not in (None, "", [])}
