"""
Confidence scoring for Phantom FOUND results.

Each found profile receives a score from 0–100 based on cross-platform
signals that distinguish the genuine subject from impostors and squatters.

Tiers
-----
  verified_identity  (70+)  — highly likely the real person
  likely_match       (40–69) — probably the same person, some uncertainty
  possible_impostor  (0–39)  — likely a different person or squatter
"""

from __future__ import annotations

import re
import statistics
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from checker import CheckResult

TIER_VERIFIED = "verified_identity"
TIER_LIKELY   = "likely_match"
TIER_IMPOSTOR = "possible_impostor"

# Tiers for MISSING results — the asymmetry the previous version had
# where FOUND came with judgment but MISSING was an unlabelled mass.
# Surfacing "confident MISSING" matters for OSINT: "this handle does NOT
# exist on this platform" is itself an investigation finding.
TIER_MISSING_CONFIRMED = "confirmed_missing"
TIER_MISSING_UNCERTAIN = "uncertain_missing"

# ---------------------------------------------------------------------------
# Default / placeholder avatar heuristics
# ---------------------------------------------------------------------------

_DEFAULT_AVATAR_FRAGMENTS = (
    "default_profile",
    "default_avatar",
    "default-th",
    "ghost.png",
    "anonymous.png",
    "placeholder",
    "/i/default_profile",
    "identicon",
    "wavatar",
    "monsterid",
    "/img/f-default",
    "gravatar.com/avatar/00000000",
    "s.gravatar.com/avatar",          # bare gravatar (no hash after /avatar/)
    "cdn.jsdelivr.net/gh/primer",     # GitHub default user / org avatar
    "avatars.githubusercontent.com/u/0",
    "user-default-pictures-uv",       # Twitch "no PFP" silhouette CDN path
    "pf-default-user",                # Pillowfort placeholder
)


def _is_default_avatar(url: str) -> bool:
    if not url:
        return False
    u = url.lower()
    return any(f in u for f in _DEFAULT_AVATAR_FRAGMENTS)


# ---------------------------------------------------------------------------
# Impostor-affix detection
# ---------------------------------------------------------------------------

_IMPOSTOR_PREFIXES = frozenset({
    "official", "real", "the", "its", "iam", "i_am", "iamthe",
    "thisis", "this_is",
})
_IMPOSTOR_SUFFIXES = frozenset({
    "official", "real",
})


def _has_impostor_affix(variant: str) -> bool:
    """Return True if the variant starts/ends with a known impostor affix."""
    v = variant.lower().strip("-_.")
    for pfx in _IMPOSTOR_PREFIXES:
        if v.startswith(pfx) and len(v) > len(pfx):
            return True
    for sfx in _IMPOSTOR_SUFFIXES:
        clean = v.rstrip("_")
        if clean.endswith(sfx) and len(clean) > len(sfx):
            return True
    return False


# ---------------------------------------------------------------------------
# Joined-date parser (no third-party deps)
# ---------------------------------------------------------------------------

def _joined_days_ago(joined_raw: str) -> Optional[int]:
    """Return approximate days since the account joined, or None."""
    if not joined_raw:
        return None
    s = joined_raw.strip()
    now = datetime.now(timezone.utc)
    # ISO-ish prefixes: "2015-12-27T04:02:17Z", "2015-12-27T04:02:17", "2015-12-27"
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(s[:len(fmt)], fmt).replace(tzinfo=timezone.utc)
            return max(0, (now - dt).days)
        except (ValueError, IndexError):
            pass
    # "Jan 2020", "January 2020"
    for fmt in ("%b %Y", "%B %Y"):
        try:
            dt = datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
            return max(0, (now - dt).days)
        except ValueError:
            pass
    # "2019" (year only)
    m = re.match(r'^(20\d\d)$', s)
    if m:
        try:
            dt = datetime(int(m.group(1)), 1, 1, tzinfo=timezone.utc)
            return max(0, (now - dt).days)
        except (ValueError, OverflowError):
            pass
    return None


# ---------------------------------------------------------------------------
# Core scorer
# ---------------------------------------------------------------------------

def tier_from_score(score: int) -> str:
    if score >= 55:
        return TIER_VERIFIED
    if score >= 20:
        return TIER_LIKELY
    return TIER_IMPOSTOR


def missing_tier(r: "CheckResult") -> str:
    """Classify a MISSING result by confidence.

    A "confirmed missing" verdict requires:
      - the site's reliability score ≥ 80, AND
      - the reason wasn't a retried or cached verdict (we want the
        cleanest possible signal — a fresh 4xx from a reliable site
        is the strongest "this user doesn't exist" we can produce).

    Everything else is "uncertain missing". Low-reliability sites,
    `unexpected-NNN` cases, and reasons containing a retry/cache tag
    fall here.

    The signal: when a single high-reliability platform cleanly returns
    MISSING, that's near-proof the username is unused there. Surfacing
    this in reports lets OSINT analysts say "definitely not on Twitter"
    with the same confidence they'd say "found on Instagram".
    """
    if r.exists is not False:
        return ""
    reason = r.reason or ""
    if "+retry" in reason or "+cached" in reason:
        return TIER_MISSING_UNCERTAIN
    if r.reliability < 80:
        return TIER_MISSING_UNCERTAIN
    # `absence` (matched an absence_text pattern) and clean numeric
    # status reasons (e.g. "404") are the gold-standard missing signals.
    if reason == "absence" or (reason.isdigit() and 400 <= int(reason) < 500):
        return TIER_MISSING_CONFIRMED
    return TIER_MISSING_UNCERTAIN


def annotate_missing(found_or_unknown_or_missing: list["CheckResult"]) -> None:
    """Stamp `missing_tier` onto every MISSING result's `.tier` field
    in-place. Idempotent — calling twice produces no change.

    The FOUND tier is set by score_all; this is its mirror image for
    the MISSING side. After this runs, every CheckResult that wasn't
    UNKNOWN has a meaningful .tier value.
    """
    for r in found_or_unknown_or_missing:
        if r.exists is False and not r.tier:
            r.tier = missing_tier(r)


def score_result(
    r: "CheckResult",
    all_found: list["CheckResult"],
    clusters: list,
    subject_name: str,
    input_username: str,
    trace: list[dict] | None = None,
) -> int:
    """Return a 0–100 confidence score for one FOUND result.

    When `trace` is provided, every signal that fires appends a
    ``{"label": str, "weight": int}`` entry to it. The HTML dossier uses
    this to render a per-account "Why this score" breakdown — the kind
    of judgment Phantom does that Maigret simply doesn't.
    """
    p = r.profile or {}
    score = 0

    def fire(label: str, weight: int) -> None:
        """Helper: apply a signal's weight and record it in the trace."""
        nonlocal score
        score += weight
        if trace is not None:
            trace.append({"label": label, "weight": weight})

    # ------------------------------------------------------------------
    # Pre-compute shared data that multiple signals need
    # ------------------------------------------------------------------

    # All photo URLs that appear in clusters with 2+ members.
    multi_cluster_photo_urls: set[str] = set()
    has_any_multi_cluster = False
    for c in (clusters or []):
        if len(c.member_indexes) > 1:
            has_any_multi_cluster = True
            for photo in (c.photos or []):
                if photo:
                    multi_cluster_photo_urls.add(photo.rstrip("/"))

    this_photo = (p.get("photo") or "").rstrip("/")

    # Domains / bare hostnames of all found profile URLs.
    found_domains: set[str] = set()
    for other in all_found:
        m = re.search(r'https?://([^/]+)', other.url or "")
        if m:
            found_domains.add(m.group(1).lower())

    # All follower counts > 0 across found results.
    all_fc: list[float] = [
        float((rr.profile or {}).get("followers"))
        for rr in all_found
        if (rr.profile or {}).get("followers") is not None
        and float((rr.profile or {}).get("followers")) > 0
    ]
    this_fc = p.get("followers")

    # ------------------------------------------------------------------
    # Positive signals
    # ------------------------------------------------------------------

    if p.get("verified") is True:
        fire("verified badge on platform", 50)

    if this_photo and this_photo in multi_cluster_photo_urls:
        fire("profile photo matches another account", 30)

    bio_website = (
        (p.get("bio") or "") + " " + (p.get("website") or "")
    ).lower()
    if found_domains and any(d in bio_website for d in found_domains):
        fire("bio / website links to another found account", 25)

    if all_fc and this_fc is not None and this_fc > 0 and len(all_fc) >= 2:
        try:
            med = statistics.median(all_fc)
            if med > 0:
                ratio = max(float(this_fc), med) / min(float(this_fc), med)
                if ratio <= 100:
                    fire("follower count consistent with other accounts", 20)
        except statistics.StatisticsError:
            pass

    if subject_name:
        def _norm(s: str) -> str:
            return re.sub(r"[^a-z0-9]", "", s.lower())
        sn = _norm(subject_name)
        dn = _norm(p.get("display_name") or "")
        if sn and dn and (sn in dn or dn in sn):
            fire("display name matches subject", 15)

    has_posts = (p.get("posts") or 0) > 0
    days_ago = _joined_days_ago(p.get("joined") or "")
    has_old_join = days_ago is not None and days_ago > 180
    if has_posts or has_old_join:
        fire("account is active (posts or aged > 6 months)", 10)

    if r.variant and r.variant.lower() == input_username.lower():
        fire("exact handle match (no suffix, no separator)", 20)

    # ------------------------------------------------------------------
    # Negative signals
    # ------------------------------------------------------------------

    fc_val = p.get("followers")
    posts_val = p.get("posts")
    if (fc_val is not None and int(fc_val) == 0
            and posts_val is not None and int(posts_val) == 0):
        fire("parked / placeholder account (0 posts, 0 followers)", -25)

    if _is_default_avatar(this_photo):
        fire("default or placeholder profile photo", -20)

    if (this_photo
            and not _is_default_avatar(this_photo)
            and has_any_multi_cluster
            and this_photo not in multi_cluster_photo_urls):
        fire("photo does not match any photo cluster", -15)

    if _has_impostor_affix(r.variant or ""):
        plain_on_same_site = any(
            other.site == r.site
            and other is not r
            and not _has_impostor_affix(other.variant or "")
            for other in all_found
        )
        if plain_on_same_site:
            fire("impostor affix (real/official/the…) coexists with plain handle", -15)

    max_fc_elsewhere = 0
    if len(all_found) > 1:
        others = [
            int((rr.profile or {}).get("followers") or 0)
            for rr in all_found
            if rr is not r
        ]
        if others:
            max_fc_elsewhere = max(others)
    if (fc_val is not None
            and int(fc_val) < 10
            and max_fc_elsewhere > 100_000):
        fire("audience size mismatch (<10 here, >100k elsewhere)", -10)

    if r.variant and re.search(r'\d+$', r.variant):
        bare = re.sub(r'\d+$', '', r.variant).rstrip('-_.')
        if bare and any(
            other.site == r.site
            and other is not r
            and (other.variant or "").lower() == bare.lower()
            for other in all_found
        ):
            fire("numbered variant coexists with bare on same platform", -20)

    return max(0, min(100, score))


# ---------------------------------------------------------------------------
# Batch scorer — mutates CheckResult objects in-place
# ---------------------------------------------------------------------------

def score_all(
    found: list["CheckResult"],
    clusters: list,
    subject_name: str,
    input_username: str,
    expand_source_map: dict[str, int] | None = None,
) -> None:
    """Attach .score and .tier to every result in *found* (in-place).

    Idempotent: safe to call multiple times; rescores every time.

    `expand_source_map`: optional `{lower(variant) -> boost_int}` from the
    cross-link expansion. Handles discovered via a strong source (Keybase
    proof, JSON-LD sameAs, GitHub x_handle) get a starting boost added
    after the base score is computed. The boost reflects how confident we
    are the discovered handle belongs to the same person, independent of
    the scan's own signals.
    """
    expand_source_map = expand_source_map or {}
    for r in found:
        trace: list[dict] = []
        s = score_result(
            r, found, clusters, subject_name, input_username, trace=trace,
        )
        boost = expand_source_map.get((r.variant or "").lower(), 0)
        if boost:
            s = max(0, min(100, s + boost))
            trace.append({
                "label": "discovered via cross-link expansion (source bonus)",
                "weight": boost,
            })
        r.score = s
        r.tier = tier_from_score(s)
        r.signals = trace
