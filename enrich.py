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

from identity import detect_lang


LANG_LABELS = {
    "en": "English", "fr": "French", "es": "Spanish", "de": "German",
    "pt": "Portuguese", "it": "Italian", "nl": "Dutch", "ar": "Arabic",
    "ja": "Japanese", "zh": "Chinese", "ko": "Korean", "ru": "Russian",
    "tr": "Turkish",
}

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
    lc = _grab_int(section, "listed_count")
    if lc is not None:
        info["lists"] = lc
    # `entities.url.urls[0].expanded_url` is the real http(s) URL the
    # user pinned to their profile (the top-level `url` field is just a
    # t.co shortener).
    m = re.search(
        r'"url":\s*\{\s*"urls":\s*\[\s*\{[^}]*?"expanded_url":\s*"((?:\\.|[^"\\])*)"',
        section,
    )
    if m:
        try:
            info["website"] = json.loads(f'"{m.group(1)}"')
        except Exception:
            info["website"] = m.group(1)
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
    bio_link = user.get("bioLink") or {}
    if isinstance(bio_link, dict) and bio_link.get("link"):
        info["website"] = bio_link["link"]
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

def extract_instagram(body: str, username: str) -> dict:
    """Instagram: parse the web_profile_info API JSON response.

    The body is the raw JSON from
    https://i.instagram.com/api/v1/users/web_profile_info/?username={username}
    which requires the x-ig-app-id header but no login cookie.
    """
    info: dict = {}
    try:
        data = json.loads(body)
    except Exception:
        return info
    user = (data.get("data") or {}).get("user")
    if not isinstance(user, dict) or not user:
        return info
    if user.get("full_name"):
        info["display_name"] = user["full_name"]
    if user.get("biography"):
        info["bio"] = user["biography"]
    photo = user.get("profile_pic_url_hd") or user.get("profile_pic_url") or ""
    if photo:
        info["photo"] = photo
    fc = (user.get("edge_followed_by") or {}).get("count")
    if fc is not None:
        info["followers"] = int(fc)
    fw = (user.get("edge_follow") or {}).get("count")
    if fw is not None:
        info["following"] = int(fw)
    posts = (user.get("edge_owner_to_timeline_media") or {}).get("count")
    if posts is not None:
        info["posts"] = int(posts)
    if user.get("is_verified"):
        info["verified"] = True
    if user.get("is_private"):
        info["private"] = True
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

    # Drop the og:description boilerplate — it's not a real bio. GitHub
    # serves a few variants depending on profile type/state:
    #   "<name> has N repositories available. Follow their code on GitHub."
    #   "<name> has one repository available. Follow their code on GitHub."
    #   "GitHub is where <name> builds software."
    # The leading name uses display capitalization (e.g. "Node.js"), not
    # the URL slug, so a username-based startswith check misses orgs.
    bio = info.get("bio", "") or ""
    if (
        re.search(r"\bhas\s+\S+\s+(?:public\s+)?repositor(?:y|ies)\s+available\b", bio, re.IGNORECASE)
        or re.search(r"\bGitHub is where .+ builds software\b", bio, re.IGNORECASE)
        or re.search(r"\bFollow their code on GitHub\b", bio, re.IGNORECASE)
    ):
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

# Plain-string form (About section): "subscriberCountText":"110M subscribers"
# This is the canonical channel-level count. Prefer it over the structured form,
# which appears in video-card contexts and may belong to a sub-channel.
_YT_SUBS_PLAIN_RE = re.compile(
    r'"subscriberCountText"\s*:\s*"([^"]+)"',
    re.IGNORECASE,
)
# Structured fallback: {"simpleText":"1.2M subscribers"} or accessibility label.
_YT_SUBS_STRUCT_RE = re.compile(
    r'"subscriberCountText"\s*:\s*\{[^{]*?"(?:simpleText|label)"\s*:\s*"([^"]+)"',
    re.IGNORECASE,
)
_YT_VIDEOS_RE = re.compile(
    r'"videosCountText":\s*\{[^}]*?"text":\s*"([\d,. ]+)"'
)
_YT_VIEWS_RE = re.compile(r'"viewCountText":\s*"([\d,. ]+)\s*views?"')
_YT_COUNTRY_RE = re.compile(r'"country":\s*"([^"]+)"')
# joinedDateText uses "content" in newer layouts, "text" in older ones.
_YT_JOINED_RE = re.compile(
    r'"joinedDateText"\s*:\s*\{[^{]*?"(?:text|content)"\s*:\s*"Joined\s+([^"]+)"'
)
_YT_DESC_RE = re.compile(r'"description":\s*"((?:\\\\.|[^"\\\\])*)"')
# Verified channels have an accessibility label ending with ", Verified".
_YT_VERIFIED_RE = re.compile(
    r'"accessibilityContext"\s*:\s*\{"label"\s*:\s*"[^"]+,\s*Verified"\}',
    re.IGNORECASE,
)


def extract_youtube(body: str, username: str) -> dict:
    info = extract_meta(body, f"https://www.youtube.com/@{username}")

    # --- Subscriber count ---
    # Prefer the plain-string form; it appears in the About section and always
    # reflects the full channel count. The structured form appears earlier in
    # video-card contexts and may belong to a sub-channel with a far lower count.
    subs_raw: Optional[str] = None
    m = _YT_SUBS_PLAIN_RE.search(body)
    if m:
        subs_raw = m.group(1)
    else:
        m = _YT_SUBS_STRUCT_RE.search(body)
        if m:
            subs_raw = m.group(1)
    if subs_raw:
        clean = re.sub(r"\s*subscribers?\s*", "", subs_raw, flags=re.I).strip()
        n = _parse_human_number(clean)
        if n is not None:
            info["followers"] = n  # subs are followers

    # --- Video count, views, country, joined date, description ---
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

    # --- Verified badge ---
    if _YT_VERIFIED_RE.search(body):
        info["verified"] = True

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


# ---------------------------------------------------------------------------
# Facebook
#
# Facebook accepts several URL patterns for the *same* profile (no dot,
# dotted, hyphenated) and returns 200 for each. To dedupe, we read
# `og:url`, which is the user's canonical profile URL — identical across
# the alias paths. We also normalise the display name (strip the "| ...
# | Facebook" suffix) and look for the public profile facts that
# sometimes appear in the SSR'd HTML for the legacy /m/-shaped renders
# we get with our older Chrome User-Agent: "Lives in", "From", "Works
# at", "Studied at".
# ---------------------------------------------------------------------------

_FB_LIVES_RE = re.compile(
    r'(?:Lives in|Habite à|Vit à)\s+([^<\n]{2,80}?)(?:\s*<|\s*\|\s*|$)',
    re.IGNORECASE,
)
_FB_FROM_RE = re.compile(
    r'(?:From|De|Originaire de)\s+([A-Z][^<\n]{2,80}?)(?:\s*<|\s*\|\s*|$)',
)
_FB_WORKS_RE = re.compile(
    r'(?:Works at|Travaille (?:à|chez|au))\s+([^<\n]{2,80}?)(?:\s*<|\s*\|\s*|$)',
    re.IGNORECASE,
)
_FB_STUDIED_RE = re.compile(
    r'(?:Studied at|A étudié à)\s+([^<\n]{2,80}?)(?:\s*<|\s*\|\s*|$)',
    re.IGNORECASE,
)


def _norm_fb_url(u: str) -> str:
    """Normalise a Facebook profile URL for dedup: lower-case, strip
    query/fragment, drop trailing slash, ensure www host."""
    if not u:
        return u
    u = u.strip().lower()
    u = re.sub(r'^https?://(?:m\.|web\.|mbasic\.)?facebook\.com', 'https://www.facebook.com', u)
    u = u.split('?', 1)[0].split('#', 1)[0].rstrip('/')
    return u


def extract_facebook(body: str, username: str) -> dict:
    info = extract_meta(body, f"https://www.facebook.com/{username}")
    # Boilerplate og:description ("X est sur Facebook. Inscrivez-vous…") —
    # not a real bio, drop it.
    bio = info.get("bio") or ""
    if re.search(r"(?:is on Facebook|est sur Facebook|en Facebook|على فيسبوك)", bio):
        info.pop("bio", None)
    # Display name: og:title is "Mohamed Mahemli" or "Mohamed Mahemli | Facebook".
    title = info.get("display_name") or ""
    title = re.sub(r"\s*\|\s*Facebook\s*$", "", title).strip()
    if title:
        info["display_name"] = title
    # Canonical profile URL — the same across alias paths, so we use it
    # to dedupe `mohamedmahemli` / `mohamed.mahemli` / `mohamed-mahemli`.
    meta = _meta_map(body)
    og_url = meta.get("og:url")
    if og_url:
        info["canonical_url"] = _norm_fb_url(og_url)

    # Facebook's app-deep-link tags expose the numeric profile ID, which
    # is the bullet-proof identity key — same value regardless of how
    # many vanity URL aliases the profile uses. Pull from `al:ios:url`
    # (`fb://profile/<id>`) or `al:android:url`.
    for key in ("al:ios:url", "al:android:url"):
        v = meta.get(key) or ""
        m = re.search(r'fb://(?:profile|page|profile_tabs|page_tabs)/(\d+)', v)
        if m:
            info["fb_profile_id"] = m.group(1)
            # Compose a stable canonical_url if og:url didn't fire so the
            # generic dedup picks up the same ID across alias paths.
            info.setdefault("canonical_url", f"fb://profile/{m.group(1)}")
            break

    # Public profile facts. Facebook only ships these on the legacy SSR
    # render — best-effort, no-op if missing.
    m = _FB_LIVES_RE.search(body)
    if m:
        info["location"] = unescape(m.group(1).strip())
    m = _FB_FROM_RE.search(body)
    if m and not info.get("hometown"):
        info["hometown"] = unescape(m.group(1).strip())
    m = _FB_WORKS_RE.search(body)
    if m:
        info["company"] = unescape(m.group(1).strip())
    m = _FB_STUDIED_RE.search(body)
    if m:
        info["education"] = unescape(m.group(1).strip())
    return info


# ---------------------------------------------------------------------------
# Behance
#
# Behance ships a SSR'd profile JSON in `<script id="beconfig-store_state">`
# (older) or in inline `window.__INITIAL_STATE__` / `<script
# id="__NEXT_DATA__">` (newer). All three have the same
# `profile.owner.user` shape with location, occupation, company, social
# links — fully public, no auth.
# ---------------------------------------------------------------------------

_BEHANCE_BLOB_RES = [
    re.compile(
        r'<script[^>]*\bid=["\']beconfig-store_state["\'][^>]*>(.*?)</script>',
        re.DOTALL,
    ),
    re.compile(
        r'<script[^>]*\bid=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
        re.DOTALL,
    ),
]


def _walk_for_user(node, want_username: str):
    """Depth-first walk a parsed JSON blob looking for a Behance user
    object whose username matches. Returns the dict or None."""
    if isinstance(node, dict):
        if (node.get("username") or "").lower() == want_username.lower():
            return node
        # Behance has multiple shapes; check the most common keys first.
        for key in ("owner", "user", "profile", "data"):
            child = node.get(key)
            if isinstance(child, (dict, list)):
                hit = _walk_for_user(child, want_username)
                if hit:
                    return hit
        for v in node.values():
            if isinstance(v, (dict, list)):
                hit = _walk_for_user(v, want_username)
                if hit:
                    return hit
    elif isinstance(node, list):
        for v in node:
            hit = _walk_for_user(v, want_username)
            if hit:
                return hit
    return None


def extract_behance(body: str, username: str) -> dict:
    info = extract_meta(body, f"https://www.behance.net/{username}")
    user = None
    for rx in _BEHANCE_BLOB_RES:
        m = rx.search(body)
        if not m:
            continue
        try:
            data = json.loads(m.group(1))
        except Exception:
            continue
        user = _walk_for_user(data, username)
        if user:
            break
    if not user:
        return info

    name = " ".join(
        x for x in (user.get("first_name"), user.get("last_name")) if x
    ).strip() or user.get("display_name")
    if name:
        info["display_name"] = name
    if user.get("occupation"):
        info["bio"] = user["occupation"]
    # Location: Behance ships `city` + `state` + `country` separately
    # AND a pre-formatted `location` string. The pre-formatted one is
    # what shows on the profile, so prefer it.
    loc = user.get("location")
    if loc:
        info["location"] = str(loc).strip()
    elif user.get("city") or user.get("country"):
        info["location"] = ", ".join(
            x for x in (user.get("city"), user.get("state"), user.get("country")) if x
        )
    if user.get("country"):
        info["country"] = str(user["country"]).strip()
    if user.get("company"):
        info["company"] = str(user["company"]).strip()
    stats = user.get("stats") or {}
    if "followers" in stats:
        info["followers"] = int(stats["followers"])
    if "following" in stats:
        info["following"] = int(stats["following"])
    if "appreciations" in stats:
        info["hearts"] = int(stats["appreciations"])
    if "views" in stats:
        info["views"] = int(stats["views"])
    if "project_views" in stats:
        info["views"] = int(stats["project_views"])
    # Social links — pull a website if present.
    for link in user.get("social_links") or []:
        if isinstance(link, dict) and link.get("url"):
            kind = (link.get("service_name") or "").lower()
            if kind in ("website", "personal", "portfolio") and "website" not in info:
                info["website"] = link["url"]
            elif kind == "twitter" and not info.get("twitter_handle"):
                handle = link["url"].rstrip("/").rsplit("/", 1)[-1].lstrip("@")
                if handle:
                    info["twitter_handle"] = handle
    return info


# ---------------------------------------------------------------------------
# Linktree
# ---------------------------------------------------------------------------

_LINKTREE_NEXT_RE = re.compile(
    r'<script[^>]*\bid=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
    re.DOTALL,
)


def extract_linktree(body: str, username: str) -> dict:
    """Linktree: parse __NEXT_DATA__ for links list, bio, and avatar.

    The og:image is a generated OG preview card, not a real avatar — skip it
    and use account.profilePictureUrl instead.
    """
    info: dict = {}
    m = _LINKTREE_NEXT_RE.search(body)
    if not m:
        return info
    try:
        data = json.loads(m.group(1))
    except Exception:
        return info
    pp = data.get("props", {}).get("pageProps", {}) or {}
    acct = pp.get("account", {}) or {}

    page_title = (pp.get("pageTitle") or "").strip().lstrip("@")
    if page_title:
        info["display_name"] = page_title

    desc = (pp.get("description") or "").strip()
    if desc:
        info["bio"] = desc

    photo = acct.get("profilePictureUrl") or ""
    if photo:
        info["photo"] = photo

    links_raw = pp.get("links") or []
    links = []
    for lnk in links_raw:
        if not isinstance(lnk, dict):
            continue
        url = (lnk.get("url") or "").strip()
        title = (lnk.get("title") or "").strip()
        if url.startswith("http"):
            entry: dict = {"url": url}
            if title:
                entry["title"] = title
            links.append(entry)
    if links:
        info["links"] = links
        info["link_count"] = len(links)

    social_raw = pp.get("socialLinks") or []
    socials = []
    for s in social_raw:
        if not isinstance(s, dict):
            continue
        url = s.get("url") or ""
        platform = s.get("type") or s.get("platform") or ""
        if url:
            entry = {"url": url}
            if platform:
                entry["platform"] = platform
            socials.append(entry)
    if socials:
        info["social_links"] = socials

    return info


# ---------------------------------------------------------------------------
# Beacons
# ---------------------------------------------------------------------------

_BEACONS_TITLE_RE = re.compile(
    r'^(.*?)\s*\(@[^)]+\)\s*\|(.*?)\|\s*Beacons\s*$',
    re.IGNORECASE,
)


def extract_beacons(body: str, username: str) -> dict:
    """Beacons: og:title encodes the display name and linked platforms.

    Format: "displayname (@handle) | Platform1, Platform2 | Beacons"
    """
    info = extract_meta(body, f"https://beacons.ai/{username}")
    title = (info.get("display_name") or "").strip()
    m = _BEACONS_TITLE_RE.match(title)
    if m:
        display = m.group(1).strip()
        if display:
            info["display_name"] = display
        platforms_str = m.group(2).strip()
        if platforms_str:
            platforms = [p.strip() for p in platforms_str.split(",") if p.strip()]
            if platforms:
                info["linked_platforms"] = platforms
    # og:description on Beacons is generic marketing copy — not a real bio.
    bio = info.get("bio") or ""
    if re.search(r"(?:mobile website|link in bio|monetization|Beacons is)", bio, re.IGNORECASE):
        info.pop("bio", None)
    return info


def extract_bio_link(body: str, username: str) -> dict:
    """Bio.link: og:description is always generic marketing copy — drop it."""
    info = extract_meta(body, f"https://bio.link/{username}")
    bio = info.get("bio") or ""
    if re.search(r"Link to everywhere from your bio link", bio, re.IGNORECASE):
        info.pop("bio", None)
    return info


# ---------------------------------------------------------------------------
# Dev.to
# ---------------------------------------------------------------------------

_DEVTO_LD_RE = re.compile(
    r'application/ld\+json[^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)
_DEVTO_JOINED_RE = re.compile(
    r'Joined</[^>]+>[\s\S]{0,300}?<time[^>]+datetime="([^"]+)"',
    re.IGNORECASE,
)
_DEVTO_LOC_RE = re.compile(
    r'Location</[^>]+>[\s\S]{0,300}?<span[^>]*>\s*([^<\n]{1,60}?)\s*</span>',
    re.IGNORECASE,
)


def extract_devto(body: str, username: str) -> dict:
    """Dev.to: JSON-LD Person block carries name, bio, sameAs links, and photo."""
    info = extract_meta(body, f"https://dev.to/{username}")
    m = _DEVTO_LD_RE.search(body)
    if m:
        try:
            ld = json.loads(m.group(1))
        except Exception:
            ld = {}
        name = ld.get("name", "")
        if name:
            info["display_name"] = name
        desc = ld.get("description", "")
        if desc:
            info["bio"] = unescape(desc)
        img = ld.get("image", "")
        if img and isinstance(img, str):
            info["photo"] = img
        same_as = ld.get("sameAs", [])
        if isinstance(same_as, list) and same_as:
            info["linked_accounts"] = same_as
    joined = _DEVTO_JOINED_RE.search(body)
    if joined:
        info["joined"] = joined.group(1).strip()
    loc = _DEVTO_LOC_RE.search(body)
    if loc:
        text = loc.group(1).strip()
        if text:
            info["location"] = unescape(text)
    return info


# ---------------------------------------------------------------------------
# Medium
# ---------------------------------------------------------------------------

_MEDIUM_TITLE_RE = re.compile(r'–\s*Medium</title>', re.IGNORECASE)


def extract_medium(body: str, username: str) -> dict:
    """Medium: og:title carries display name; JSON embeds follower counts."""
    info = extract_meta(body, f"https://medium.com/@{username}")
    # Strip " – Medium" suffix from display name if extract_meta left it.
    title = (info.get("display_name") or "").strip()
    clean = re.sub(r'\s*–\s*Medium\s*$', '', title).strip()
    if clean:
        info["display_name"] = clean
    # Follower / following counts live in the hydration JSON.
    fc = re.search(r'"followerCount"\s*:\s*(\d+)', body)
    fw = re.search(r'"followingCount"\s*:\s*(\d+)', body)
    if fc:
        info["followers"] = int(fc.group(1))
    if fw:
        info["following"] = int(fw.group(1))
    # Recent article titles (skip very short strings that are UI labels).
    articles: list[str] = []
    seen: set[str] = set()
    for m in re.finditer(r'"title"\s*:\s*"((?:[^"\\]|\\.){10,120})"', body):
        try:
            t = json.loads(f'"{m.group(1)}"')
        except Exception:
            t = m.group(1)
        if t and t not in seen and len(t) >= 10:
            seen.add(t)
            articles.append(t)
        if len(articles) >= 5:
            break
    if articles:
        info["recent_articles"] = articles
    return info


# ---------------------------------------------------------------------------
# About.me
# ---------------------------------------------------------------------------

_ABOUTME_LD_RE = re.compile(
    r'application/ld\+json[^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)


def extract_about_me(body: str, username: str) -> dict:
    """About.me: JSON-LD Person block has name, bio, jobTitle, address, sameAs."""
    info: dict = {}
    m = _ABOUTME_LD_RE.search(body)
    if not m:
        return extract_meta(body, f"https://about.me/{username}")
    try:
        ld = json.loads(m.group(1))
    except Exception:
        return extract_meta(body, f"https://about.me/{username}")
    name = ld.get("name", "")
    if name:
        info["display_name"] = name
    bio = ld.get("description", "")
    if bio:
        info["bio"] = unescape(bio)
    job = ld.get("jobTitle", "")
    if job:
        info["job_title"] = job
    addr = ld.get("address", "")
    if addr and isinstance(addr, str):
        info["location"] = addr
    # Profile photo — image is a dict with a "url" key.
    img = ld.get("image", {})
    if isinstance(img, dict):
        photo_url = img.get("url", "")
    else:
        photo_url = str(img) if img else ""
    if photo_url:
        info["photo"] = photo_url
    # sameAs — verified linked profiles on other platforms.
    same_as = ld.get("sameAs", [])
    if isinstance(same_as, list) and same_as:
        info["linked_accounts"] = same_as
    return info


# ---------------------------------------------------------------------------
# Keybase
# ---------------------------------------------------------------------------

def extract_keybase(body: str, username: str) -> dict:
    """Keybase: the 'body' is a JSON API response, not HTML.

    Extracts profile fields and the cryptographic proofs list — the most
    valuable output, since each proof is a verified link to another account.
    """
    info: dict = {}
    try:
        data = json.loads(body)
    except Exception:
        return info
    if data.get("status", {}).get("code") != 0:
        return info
    them = data.get("them", {})
    if not isinstance(them, dict) or not them:
        return info
    profile = them.get("profile") or {}
    if profile.get("full_name"):
        info["display_name"] = profile["full_name"]
    if profile.get("bio"):
        info["bio"] = profile["bio"]
    if profile.get("location"):
        info["location"] = profile["location"]
    pics = them.get("pictures") or {}
    primary = pics.get("primary") or {}
    photo_url = primary.get("url", "")
    if photo_url:
        info["photo"] = photo_url
    # Cryptographic proofs — each is a verified link to another account.
    proofs_raw = (them.get("proofs_summary") or {}).get("all", [])
    proofs: list[dict] = []
    for p in proofs_raw:
        if not isinstance(p, dict) or p.get("state") != 1:
            continue  # only include verified proofs
        entry: dict = {"type": p.get("proof_type", ""), "handle": p.get("nametag", "")}
        url = p.get("service_url", "")
        if url:
            entry["url"] = url
        proofs.append(entry)
    if proofs:
        info["proofs"] = proofs
        info["proof_count"] = len(proofs)
    return info


def extract_pastebin(body: str, username: str) -> dict:
    info = extract_meta(body, f"https://pastebin.com/u/{username}")
    # Pastebin's og:image is a site-wide social card (e.g.
    # /i/facebook.png), never the user's avatar — drop anything served
    # under the /i/ icon path.
    photo = info.get("photo") or ""
    if re.search(r"/i/[^/?#]+\.(?:png|jpe?g|gif|webp|svg)\b", photo, re.IGNORECASE):
        info.pop("photo", None)
    # The real avatar lives in <div class="user-icon"><img src="...">.
    m = re.search(
        r'<div[^>]+class=["\'][^"\']*\buser-icon\b[^"\']*["\'][^>]*>\s*'
        r'<img[^>]+src=["\']([^"\']+)["\']',
        body, re.IGNORECASE,
    )
    if m:
        src = m.group(1)
        # Skip the default placeholder shown for users without an avatar.
        if "guest.png" not in src:
            info["photo"] = urljoin(f"https://pastebin.com/u/{username}", src)
    return info


# ---------------------------------------------------------------------------
# Vimeo
# ---------------------------------------------------------------------------

_VIMEO_LD_RE = re.compile(
    r'application/ld\+json[^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)


def extract_vimeo(body: str, username: str) -> dict:
    """Vimeo: JSON-LD Person block carries name, description, follower/video counts.

    og:description is the generic "X is a member of Vimeo" template — drop it
    and prefer the actual description field from JSON-LD instead.
    """
    info = extract_meta(body, f"https://vimeo.com/{username}")
    # Strip Vimeo's generic profile description — it is never a real bio.
    if "is a member of Vimeo" in (info.get("bio") or ""):
        info.pop("bio", None)
    # Skip the default portrait placeholder.
    if "defaults-blue" in (info.get("photo") or "") or "defaults-" in (info.get("photo") or ""):
        info.pop("photo", None)

    m = _VIMEO_LD_RE.search(body)
    if not m:
        return info
    try:
        blocks = json.loads(m.group(1))
    except Exception:
        return info
    if isinstance(blocks, dict):
        blocks = [blocks]
    for block in (blocks if isinstance(blocks, list) else [blocks]):
        if not isinstance(block, dict):
            continue
        entity = block.get("mainEntity")
        if not isinstance(entity, dict) or entity.get("@type") != "Person":
            continue
        if entity.get("name"):
            info["display_name"] = entity["name"]
        desc = entity.get("description") or ""
        if desc.strip():
            info["bio"] = desc
        photo = entity.get("image") or ""
        if (isinstance(photo, str)
                and photo.startswith("http")
                and "defaults-" not in photo):
            info["photo"] = photo
        # interactionStatistic may be a single dict or a list.
        stats = entity.get("interactionStatistic") or []
        if isinstance(stats, dict):
            stats = [stats]
        for stat in stats:
            if not isinstance(stat, dict):
                continue
            itype = stat.get("interactionType", "")
            count = stat.get("userInteractionCount")
            if count is None:
                continue
            if "FollowAction" in itype:
                info["followers"] = int(count)
            elif "WriteAction" in itype:
                info["posts"] = int(count)
        joined = block.get("dateCreated") or ""
        if joined:
            info["joined"] = joined[:10]
        break
    return info


# ---------------------------------------------------------------------------
# SoundCloud
# ---------------------------------------------------------------------------

_SC_USER_RE = re.compile(
    r'\{"hydratable"\s*:\s*"user"\s*,\s*"data"\s*:\s*(\{)',
    re.DOTALL,
)


def extract_soundcloud(body: str, username: str) -> dict:
    """SoundCloud: user data lives in the hydration blob embedded in the page."""
    info: dict = {}
    m = _SC_USER_RE.search(body)
    if not m:
        return extract_meta(body, f"https://soundcloud.com/{username}")
    # Walk forward to find the matching closing brace.
    start = m.start(1)
    depth, i = 0, start
    limit = min(start + 30_000, len(body))
    while i < limit:
        c = body[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                break
        i += 1
    try:
        data = json.loads(body[start : i + 1])
    except Exception:
        return extract_meta(body, f"https://soundcloud.com/{username}")

    if data.get("full_name"):
        info["display_name"] = data["full_name"]
    elif data.get("username"):
        info["display_name"] = data["username"]
    if data.get("description"):
        info["bio"] = data["description"]
    avatar = data.get("avatar_url", "")
    if avatar:
        info["photo"] = avatar.replace("-large.", "-t500x500.")
    if data.get("followers_count") is not None:
        info["followers"] = int(data["followers_count"])
    if data.get("followings_count") is not None:
        info["following"] = int(data["followings_count"])
    if data.get("track_count") is not None:
        info["posts"] = int(data["track_count"])
    city = data.get("city") or ""
    country = data.get("country_code") or ""
    if city or country:
        info["location"] = ", ".join(x for x in [city, country] if x)
    if data.get("verified"):
        info["verified"] = bool(data["verified"])
    if data.get("created_at"):
        info["joined"] = data["created_at"][:10]
    return info


# ---------------------------------------------------------------------------
# Bandcamp
# ---------------------------------------------------------------------------

_BC_GENRE_RE = re.compile(
    r'"genre"\s*:\s*"https?://bandcamp\.com/discover/([^"]+)"',
    re.IGNORECASE,
)
_BC_LD_RE = re.compile(
    r'application/ld\+json[^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)


def extract_bandcamp(body: str, username: str) -> dict:
    """Bandcamp: artist name from page title; genre from JSON-LD."""
    info: dict = {}
    # Artist name is the last segment of the page title: "Album | Artist"
    title_m = re.search(r'<title[^>]*>([^<]+)</title>', body, re.IGNORECASE)
    if title_m:
        parts = title_m.group(1).split(" | ")
        artist = parts[-1].strip() if len(parts) > 1 else parts[0].strip()
        if artist:
            info["display_name"] = unescape(artist)
    # Genre from the JSON-LD genre URL.
    gm = _BC_GENRE_RE.search(body)
    if gm:
        info["genre"] = gm.group(1).replace("-", " ")
    # Photo from og:image.
    og = _meta_map(body)
    photo = og.get("og:image")
    if photo:
        info["photo"] = _abs_url(photo, f"https://{username}.bandcamp.com/")
    return info


# ---------------------------------------------------------------------------
# Mixcloud  (body is the raw API JSON response, not HTML)
# ---------------------------------------------------------------------------

def extract_mixcloud(body: str, username: str) -> dict:
    """Mixcloud: parse the public API JSON response directly."""
    info: dict = {}
    try:
        data = json.loads(body)
    except Exception:
        return info
    if "error" in data:
        return info
    name = data.get("name") or data.get("username") or ""
    if name:
        info["display_name"] = name
    bio = data.get("biog") or ""
    if bio.strip():
        info["bio"] = bio.strip()
    pics = data.get("pictures") or {}
    photo = pics.get("640x640") or pics.get("large") or pics.get("medium") or ""
    if photo:
        info["photo"] = photo
    if data.get("follower_count") is not None:
        info["followers"] = int(data["follower_count"])
    if data.get("following_count") is not None:
        info["following"] = int(data["following_count"])
    if data.get("cloudcast_count") is not None:
        info["posts"] = int(data["cloudcast_count"])  # mixes = posts
    if data.get("listen_count") is not None:
        info["views"] = int(data["listen_count"])
    city = data.get("city") or ""
    country = data.get("country") or ""
    if city or country:
        info["location"] = ", ".join(x for x in [city, country] if x)
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
    "Facebook": extract_facebook,
    "Behance": extract_behance,
    "Pastebin": extract_pastebin,
    "Linktree": extract_linktree,
    "Beacons": extract_beacons,
    "Bio.link": extract_bio_link,
    "Dev.to": extract_devto,
    "Medium": extract_medium,
    "About.me": extract_about_me,
    "Keybase": extract_keybase,
    "Vimeo": extract_vimeo,
    "SoundCloud": extract_soundcloud,
    "Bandcamp": extract_bandcamp,
    "Mixcloud": extract_mixcloud,
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
    # Tag the bio language so the report can show a language chip and
    # the cluster-level geo inference has a per-account hint to weigh.
    bio = info.get("bio")
    if isinstance(bio, str) and bio.strip():
        lang = detect_lang(bio)
        if lang:
            info["language"] = lang
            info["language_label"] = LANG_LABELS.get(lang, lang)
    # Final scrub: drop empty strings, keep zeros and False.
    return {k: v for k, v in info.items() if v not in (None, "", [])}
