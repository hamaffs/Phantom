"""Phantom data model + detection logic.

Site, CheckResult, evaluate(), bot-wall detection, body streaming. Pure
functions and dataclasses — no I/O beyond load_sites() reading sites.json.
"""
from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Cache-Control": "no-cache",
    "DNT": "1",
}

USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")

# Title fragments that indicate a Cloudflare / WAF / captcha challenge —
# the body cannot be trusted as a real response.
BOT_TITLE_HINTS = (
    "just a moment",
    "please wait",
    "attention required",
    "verify you are human",
    "checking your browser",
    "robot check",
    "access denied",
    "ddos protection",
    "client challenge",
    "ng guard",
    "prove your humanity",
)

_TITLE_RE = re.compile(r"<title[^>]*>([^<]*)</title>", re.IGNORECASE)

# CDN domains that send `cross-origin-resource-policy: same-origin`, blocking
# browsers from loading the image in cross-origin contexts (file:// reports).
# For these we embed the bytes as a base64 data URI instead of a URL.
_CORP_RESTRICTED_CDNS = (
    "fbcdn.net/", "cdninstagram.com/",
    # TikTok's CDNs Referer-gate every image — clicking the URL in a
    # browser tab returns "Access Denied" because the browser sends no
    # Referer. We already fetched the bytes via _fetch_image (which
    # adds the right Referer), so inline them as a data URI instead.
    "tiktokcdn.com/", "tiktokcdn-us.com/",
)

def _photo_to_data_uri(url: str, photo_bytes_map: Optional[dict]) -> Optional[str]:
    """Return a base64 data URI for `url` when it comes from a CORP-restricted CDN."""
    if not url or not photo_bytes_map:
        return None
    if not any(p in url for p in _CORP_RESTRICTED_CDNS):
        return None
    data = photo_bytes_map.get(url)
    if not data:
        return None
    ct = "image/png" if data[:4] == b"\x89PNG" else "image/jpeg"
    return f"data:{ct};base64,{base64.b64encode(data).decode('ascii')}"


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Site:
    name: str
    category: str
    url: str
    method: str                              # "status" | "message"
    reliability: int
    valid_status: list[int] = field(default_factory=list)
    invalid_status: list[int] = field(default_factory=list)
    presence_text: list[str] = field(default_factory=list)
    absence_text: list[str] = field(default_factory=list)
    protection: list[str] = field(default_factory=list)
    headers: dict = field(default_factory=dict)
    request_method: str = "GET"              # "GET" | "POST"
    request_body: Optional[str] = None       # raw body template (with {username})
    profile_url: Optional[str] = None        # public profile URL when probe URL is an API endpoint
    # Optional richer metadata for filtering. None = "any / unspecified".
    country: Optional[str] = None            # ISO 3166-1 alpha-2 (lowercase), or "global"
    language: Optional[str] = None           # ISO 639-1 (lowercase), or "global"
    content_type: Optional[str] = None       # photo | text | code | audio | video | links | mixed
    disabled: bool = False                   # auto-disable flag for known-broken sites

    def url_for(self, username: str) -> str:
        return self.url.replace("{username}", username)

    def display_url_for(self, username: str) -> str:
        """Public-facing profile URL used in reports. Falls back to the probe URL."""
        base = self.profile_url or self.url
        return base.replace("{username}", username)

    def body_for(self, username: str) -> Optional[str]:
        return self.request_body.replace("{username}", username) if self.request_body else None

    @property
    def needs_impersonation(self) -> bool:
        return "tls_fingerprint" in self.protection

    @property
    def needs_js_render(self) -> bool:
        return "js_challenge" in self.protection


@dataclass
class CheckResult:
    site: str
    category: str
    url: str
    exists: Optional[bool]      # True = found, False = absent, None = unknown
    reliability: int
    status: Optional[int] = None
    error: Optional[str] = None
    elapsed_ms: int = 0
    reason: Optional[str] = None    # human label for why we decided as we did
    final_url: Optional[str] = None # set when redirects landed elsewhere
    backend: str = "aiohttp"        # which HTTP client handled the request
    profile: dict = field(default_factory=dict)  # display name, bio, photo, follower counts… if FOUND
    variant: Optional[str] = None   # which generated variant produced this
    score: Optional[int] = None     # confidence score 0–100 (set by confidence.py after scan)
    tier: Optional[str] = None      # 'verified_identity' | 'likely_match' | 'possible_impostor'
    identity_id: Optional[int] = None        # cluster ID assigned by disambiguation.py
    is_primary_identity: Optional[bool] = None  # True iff this account's cluster is the primary
    signals: list = field(default_factory=list)  # confidence trace: [{"label": str, "weight": int}, ...]


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def load_sites(path: Path) -> list[Site]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    sites = []
    for entry in raw:
        if entry.get("disabled"):
            continue
        sites.append(Site(
            name=entry["name"],
            category=entry["category"],
            url=entry["url"],
            method=entry["method"],
            reliability=int(entry["reliability"]),
            valid_status=entry.get("valid_status", []),
            invalid_status=entry.get("invalid_status", []),
            presence_text=entry.get("presence_text", []),
            absence_text=entry.get("absence_text", []),
            protection=entry.get("protection", []),
            headers=entry.get("headers", {}),
            request_method=entry.get("request_method", "GET"),
            request_body=entry.get("request_body"),
            profile_url=entry.get("profile_url"),
            country=entry.get("country"),
            language=entry.get("language"),
            content_type=entry.get("content_type"),
            disabled=False,  # filtered above, never propagates
        ))
    return sites
# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

def _expand(patterns: list[str], username: str) -> list[str]:
    """Substitute {username} in detection patterns."""
    return [p.replace("{username}", username) for p in patterns]


def _is_bot_wall(body: str) -> bool:
    if not body:
        return False
    m = _TITLE_RE.search(body)
    if not m:
        return False
    title = m.group(1).strip().lower()
    return any(h in title for h in BOT_TITLE_HINTS)


def evaluate(site: Site, status: int, body: str, username: str) -> tuple[Optional[bool], str]:
    """Return (exists, reason).

    `exists` is True/False (decision), or None for inconclusive.
    `reason` is a short tag suitable for output: e.g. "200", "404",
    "presence", "absence", "no-presence", "bot-wall".

    Decision order (hard rules first, soft signals last):

    1. If the status is in `invalid_status` → MISSING. Status codes are the
       single most reliable signal when the site has clean ones.
    2. If the body matches an `absence_text` pattern → MISSING. A site
       saying "user not found" beats a misleading 200.
    3. Bot-wall detection (Cloudflare/captcha title) → UNKNOWN. Body is
       untrustworthy.
    4. Method-specific positive checks (status_code or presence_text).
    5. Anything else → UNKNOWN.
    """
    presence = _expand(site.presence_text, username)
    absence = _expand(site.absence_text, username)

    body_has_presence = any(p in body for p in presence) if presence else False
    body_has_absence = any(p in body for p in absence) if absence else False

    # 1. Hard MISSING signals.
    if status in site.invalid_status:
        return False, f"{status}"
    if body_has_absence:
        return False, "absence"

    # 2. Bot-wall — body unreliable, status was likely 200 from the WAF.
    if _is_bot_wall(body):
        return None, "bot-wall"

    # 3. Method-specific positive decision.
    if site.method == "status":
        if status in site.valid_status:
            if presence and not body_has_presence:
                return None, "no-presence"
            return True, f"{status}"
        return None, f"unexpected-{status}"

    if site.method == "message":
        if presence:
            if body_has_presence:
                return True, "presence"
            return None, "no-presence"
        # No presence patterns defined — last resort: trust the status.
        if status >= 400:
            return None, f"{status}"
        return True, "no-absence"

    return None, "unknown-method"
# ---------------------------------------------------------------------------
# HTTP backends
# ---------------------------------------------------------------------------

async def _drain(stream, max_body: int) -> bytes:
    """Read until EOF or max_body, in chunks."""
    chunks: list[bytes] = []
    total = 0
    while total < max_body:
        piece = await stream.read(min(64 * 1024, max_body - total))
        if not piece:
            break
        chunks.append(piece)
        total += len(piece)
    return b"".join(chunks)
