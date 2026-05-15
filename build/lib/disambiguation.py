"""
Identity Disambiguation — clusters FOUND accounts into distinct identity groups.

After confidence scoring, this module answers: "are all these found accounts
the same person, or several different people sharing the username?"

Algorithm
---------
1. Build a weighted similarity graph over all found accounts.
2. Two accounts get an edge when their signal-weight sum >= EDGE_THRESHOLD (3).
3. Connected components of that graph become clusters.
4. Each cluster is labelled: primary_identity / secondary_cluster / low_confidence_cluster.

Signal weights
--------------
Strong (+5): photo perceptual match — same photo = same person, period.
Strong (+3): cross-link in bio/website, both verified + same name,
             separator-variant of same input root (hamaffs ↔ hama.ffs ↔ hama_ffs).
Medium (+2): fuzzy display-name match (>0.85), same location, same website, same follower tier.
Weak   (+1): same exact variant, same bio language, account creation dates within 12 months.
Score-proximity (+2): same variant, both score ≥ 25, gap ≤ 45 pts (data-sparse bridge).
Negative (−2): different verified status on verifiable platforms, follower count 6+ OOM apart,
               contradicting locations, one default avatar vs real face photo.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from checker import CheckResult

LABEL_PRIMARY   = "primary_identity"
LABEL_SECONDARY = "secondary_cluster"
LABEL_LOW       = "low_confidence_cluster"

EDGE_THRESHOLD  = 3.0          # minimum pairwise weight for "same person" edge
PRIMARY_MIN_SCORE  = 55        # aligns with TIER_VERIFIED; verified badges push above this
SECONDARY_MIN_SCORE = 40       # cluster must contain an account this high for secondary

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FOLLOWER_TIERS = [100, 1_000, 10_000, 100_000, 1_000_000]

def _follower_tier(n) -> Optional[int]:
    if n is None:
        return None
    try:
        n = int(n)
    except (TypeError, ValueError):
        return None
    for i, t in enumerate(_FOLLOWER_TIERS):
        if n < t:
            return i
    return len(_FOLLOWER_TIERS)


def _norm(s: str) -> str:
    return re.sub(r'[^a-z0-9]', '', (s or '').lower())


def _locations_overlap(a: str, b: str) -> bool:
    if not a or not b:
        return False
    na, nb = _norm(a), _norm(b)
    return na == nb or (len(na) > 3 and na in nb) or (len(nb) > 3 and nb in na)


def _locations_contradict(a: str, b: str) -> bool:
    if not a or not b:
        return False
    na, nb = _norm(a), _norm(b)
    return (len(na) > 3 and len(nb) > 3
            and na != nb and na not in nb and nb not in na)


_DEFAULT_PHOTO_HINTS = (
    'default_profile', 'default_avatar', 'default-th', 'defaults-',
    'ghost.png', 'anonymous.png', 'placeholder', 'identicon',
    'wavatar', 'monsterid', '/img/f-default',
)

def _is_default_photo(url: str) -> bool:
    u = (url or '').lower()
    return not u or any(h in u for h in _DEFAULT_PHOTO_HINTS)


def _sep_root(s: str) -> str:
    """Strip separator characters (. _ -) and lowercase — the 'root' of a variant.

    hamaffs, hama.ffs, hama_ffs, hama-ffs all return 'hamaffs'.
    pewdiepie123 returns 'pewdiepie123' (digits kept; it's not a sep variant).
    """
    return re.sub(r'[.\-_]', '', (s or '').lower())


def _parse_date(s: str) -> Optional[datetime]:
    for fmt in ('%Y-%m-%dT%H:%M:%S', '%Y-%m-%dT%H:%M', '%Y-%m-%d', '%b %Y', '%B %Y'):
        try:
            return datetime.strptime(s.strip()[:len(fmt)], fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return None


# ---------------------------------------------------------------------------
# Cluster dataclass
# ---------------------------------------------------------------------------

@dataclass
class DisambiguationCluster:
    cluster_id: int
    member_indices: list[int] = field(default_factory=list)
    display_name: Optional[str] = None
    location:     Optional[str] = None
    max_score:    int            = 0
    total_internal_weight: float = 0.0
    label:        str            = LABEL_LOW
    size:         int            = 0
    verified_on:  list[str]      = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "cluster_id":   self.cluster_id,
            "members":      self.member_indices,
            "display_name": self.display_name,
            "location":     self.location,
            "max_score":    self.max_score,
            "label":        self.label,
            "size":         self.size,
            "verified_on":  self.verified_on,
        }


# ---------------------------------------------------------------------------
# Pairwise signal computation
# ---------------------------------------------------------------------------

_VERIFIABLE_SITES = frozenset({
    'Twitter', 'Instagram', 'YouTube', 'TikTok', 'Facebook', 'Threads',
    'LinkedIn', 'Mastodon',
})


def _pair_weight(
    a: 'CheckResult',
    b: 'CheckResult',
    photo_same_cluster: bool,
) -> float:
    pa = a.profile or {}
    pb = b.profile or {}
    w = 0.0

    # ---- Strong signals ----

    # Same perceptual photo cluster (+5): same profile photo = same person.
    # Weight raised to 5 so a single photo match is always decisive even when
    # negative signals (avatar-type mismatch, location conflict) push back.
    if photo_same_cluster:
        w += 5

    # Cross-link: one profile's bio/website references the other site's domain
    def _domain(url: str) -> str:
        m = re.search(r'https?://([^/\s]+)', url or '')
        return m.group(1).lower() if m else ''

    dom_a = _domain(a.url)
    dom_b = _domain(b.url)
    text_a = ((pa.get('bio') or '') + ' ' + (pa.get('website') or '')).lower()
    text_b = ((pb.get('bio') or '') + ' ' + (pb.get('website') or '')).lower()
    if (dom_a and len(dom_a) > 5 and dom_a in text_b) or \
       (dom_b and len(dom_b) > 5 and dom_b in text_a):
        w += 3

    # Both verified on their platforms AND display names match
    ver_a = pa.get('verified') is True
    ver_b = pb.get('verified') is True
    na = _norm(pa.get('display_name') or '')
    nb = _norm(pb.get('display_name') or '')
    if ver_a and ver_b and na and nb and (na == nb or na in nb or nb in na):
        w += 3

    # ---- Medium signals (+2) ----

    # Fuzzy display-name match > 0.85
    dn_a = (pa.get('display_name') or '').strip()
    dn_b = (pb.get('display_name') or '').strip()
    if dn_a and dn_b:
        ratio = SequenceMatcher(None, dn_a.lower(), dn_b.lower()).ratio()
        if ratio > 0.85:
            w += 2

    # Same location
    loc_a = pa.get('location') or ''
    loc_b = pb.get('location') or ''
    if _locations_overlap(loc_a, loc_b):
        w += 2

    # Same website
    ws_a = (pa.get('website') or '').lower().rstrip('/')
    ws_b = (pb.get('website') or '').lower().rstrip('/')
    if ws_a and ws_b and len(ws_a) > 8 and ws_a == ws_b:
        w += 2

    # Same follower-count tier
    tier_a = _follower_tier(pa.get('followers'))
    tier_b = _follower_tier(pb.get('followers'))
    if tier_a is not None and tier_b is not None and tier_a == tier_b:
        w += 2

    # ---- Weak signals (+1) ----

    # Same exact username variant
    var_a = (a.variant or '').lower()
    var_b = (b.variant or '').lower()
    if var_a and var_b and var_a == var_b:
        w += 1

    # Same variant AND similar confidence scores (within 45 points):
    # bridges accounts where platform-specific data is sparse but both are
    # credible. The minimum-score guard (≥ 25) prevents squatter/empty accounts
    # (score 0-20) from chaining up to the real person's cluster.
    score_a = a.score if a.score is not None else 0
    score_b = b.score if b.score is not None else 0
    if (var_a and var_b and var_a == var_b
            and score_a >= 25 and score_b >= 25
            and abs(score_a - score_b) <= 45):
        w += 2

    # Same bio language
    lang_a = pa.get('language') or ''
    lang_b = pb.get('language') or ''
    if lang_a and lang_b and lang_a == lang_b:
        w += 1

    # Stylometric bio match: punctuation / capitalization / lexical
    # fingerprint similarity above the (intentionally strict) threshold.
    # Conservative weight (+2) — bios are short and stylometry alone is
    # never enough to merge two accounts, but it tips the scale on
    # otherwise ambiguous edges. This is the signal that catches an
    # impostor reusing a display-name but writing in a clearly different
    # voice (lowercase-only vs ALL CAPS, em-dash vs straight hyphen,
    # heavy emoji vs none).
    try:
        from stylometry import STYLE_MATCH_WEIGHT, is_style_match
        if is_style_match(pa.get('bio') or '', pb.get('bio') or ''):
            w += STYLE_MATCH_WEIGHT
    except ImportError:
        pass  # stylometry is in-tree; this guard is defensive only

    # Account creation dates within 12 months
    joined_a = pa.get('joined') or ''
    joined_b = pb.get('joined') or ''
    if joined_a and joined_b:
        dt_a = _parse_date(joined_a)
        dt_b = _parse_date(joined_b)
        if dt_a and dt_b and abs((dt_a - dt_b).days) <= 365:
            w += 1

    # ---- Negative signals (−2) ----

    # Different verified status on platforms that support verification
    if a.site in _VERIFIABLE_SITES and b.site in _VERIFIABLE_SITES:
        if ver_a != ver_b:
            w -= 2

    # Follower counts 6+ orders of magnitude apart
    fc_a = pa.get('followers')
    fc_b = pb.get('followers')
    if fc_a and fc_b:
        try:
            fa, fb = int(fc_a), int(fc_b)
            if fa > 0 and fb > 0:
                ratio = max(fa, fb) / min(fa, fb)
                if ratio >= 1_000_000:
                    w -= 2
        except (TypeError, ValueError):
            pass

    # Contradicting locations
    if _locations_contradict(loc_a, loc_b):
        w -= 2

    # One has a real photo, the other has a default/placeholder avatar
    ph_a = (pa.get('photo') or '')
    ph_b = (pb.get('photo') or '')
    if ph_a or ph_b:
        if _is_default_photo(ph_a) != _is_default_photo(ph_b):
            w -= 2

    return w


# ---------------------------------------------------------------------------
# Connected-components BFS
# ---------------------------------------------------------------------------

def _connected_components(n: int, adj: list[list[int]]) -> list[list[int]]:
    visited = [False] * n
    components: list[list[int]] = []
    for start in range(n):
        if visited[start]:
            continue
        component: list[int] = []
        queue = [start]
        visited[start] = True
        while queue:
            node = queue.pop(0)
            component.append(node)
            for nbr in adj[node]:
                if not visited[nbr]:
                    visited[nbr] = True
                    queue.append(nbr)
        components.append(component)
    return components


# ---------------------------------------------------------------------------
# Cluster aggregation helpers
# ---------------------------------------------------------------------------

def _agg_display_name(found: list['CheckResult'], indices: list[int]) -> Optional[str]:
    names = [
        (found[i].profile or {}).get('display_name') or ''
        for i in indices
    ]
    names = [n.strip() for n in names if n.strip()]
    if not names:
        return None
    # Most common normalized form, return original casing of first match.
    normed = Counter(n.lower() for n in names)
    best = normed.most_common(1)[0][0]
    return next((n for n in names if n.lower() == best), names[0])


def _agg_location(found: list['CheckResult'], indices: list[int]) -> Optional[str]:
    locs = [
        (found[i].profile or {}).get('location') or ''
        for i in indices
    ]
    locs = [l.strip() for l in locs if l.strip()]
    if not locs:
        return None
    normed = Counter(l.lower() for l in locs)
    best = normed.most_common(1)[0][0]
    return next((l for l in locs if l.lower() == best), locs[0])


def _agg_verified_on(found: list['CheckResult'], indices: list[int]) -> list[str]:
    return [
        found[i].site
        for i in indices
        if (found[i].profile or {}).get('verified') is True
    ]


# ---------------------------------------------------------------------------
# Main API
# ---------------------------------------------------------------------------

def disambiguate(
    found: list['CheckResult'],
    photo_clusters: list,
    input_username: str,
    edge_threshold: float = EDGE_THRESHOLD,
) -> list[DisambiguationCluster]:
    """Cluster *found* accounts into identity groups.

    Returns clusters sorted by max_score descending (primary first).
    """
    n = len(found)
    if n == 0:
        return []

    # Build photo-URL → cluster-index mapping so we can do URL-based lookups
    # instead of relying on index alignment with identity.py's found_dicts.
    photo_url_to_cid: dict[str, int] = {}
    for cid, pc in enumerate(photo_clusters or []):
        if len(getattr(pc, 'member_indexes', [])) >= 2:
            for photo in (getattr(pc, 'photos', []) or []):
                if photo:
                    photo_url_to_cid[photo.rstrip('/')] = cid

    def _photo_same_cluster(a: 'CheckResult', b: 'CheckResult') -> bool:
        ph_a = ((a.profile or {}).get('photo') or '').rstrip('/')
        ph_b = ((b.profile or {}).get('photo') or '').rstrip('/')
        if not ph_a or not ph_b:
            return False
        cid_a = photo_url_to_cid.get(ph_a)
        cid_b = photo_url_to_cid.get(ph_b)
        return cid_a is not None and cid_a == cid_b

    # Identify which accounts are separator-variants of the input username.
    # e.g. searching "hamaffs" also finds "hama.ffs" and "hama_ffs" — all have
    # the same normalized root and are almost certainly the same person.
    # This signal only fires when BOTH accounts share the input's root, so it
    # never incorrectly links "pewdiepie123" (root="pewdiepie123") to "pewdiepie".
    input_root = _sep_root(input_username)
    sep_variant_set: set[int] = set()
    if input_root:
        for i in range(n):
            if found[i].variant and _sep_root(found[i].variant) == input_root:
                sep_variant_set.add(i)

    # Pairwise weights
    weights = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1, n):
            w = _pair_weight(found[i], found[j], _photo_same_cluster(found[i], found[j]))
            # Separator-variant bonus (+3): both variants are separator forms of
            # the same input root (hamaffs ↔ hama.ffs ↔ hama_ffs).  Only fires
            # when the two variants are DIFFERENT from each other (same variant
            # pairs are already covered by the regular +1 same-variant signal).
            if (i in sep_variant_set and j in sep_variant_set
                    and (found[i].variant or '').lower()
                        != (found[j].variant or '').lower()):
                w += 3
            weights[i][j] = weights[j][i] = w

    # Build adjacency list (only edges meeting threshold)
    adj: list[list[int]] = [[] for _ in range(n)]
    for i in range(n):
        for j in range(i + 1, n):
            if weights[i][j] >= edge_threshold:
                adj[i].append(j)
                adj[j].append(i)

    # Connected components
    components = _connected_components(n, adj)

    # Assemble cluster objects
    clusters: list[DisambiguationCluster] = []
    for cid, members in enumerate(components, 1):
        max_score = max((found[i].score or 0) for i in members)
        total_w = sum(
            weights[i][j]
            for idx_i, i in enumerate(members)
            for j in members[idx_i + 1:]
        )
        c = DisambiguationCluster(
            cluster_id=cid,
            member_indices=sorted(members),
            display_name=_agg_display_name(found, members),
            location=_agg_location(found, members),
            max_score=max_score,
            total_internal_weight=total_w,
            size=len(members),
            verified_on=_agg_verified_on(found, members),
        )
        clusters.append(c)

    # Sort: highest max_score first, then larger clusters first
    clusters.sort(key=lambda c: (-c.max_score, -c.size, -c.total_internal_weight))

    # Reassign sequential IDs after sorting
    for i, c in enumerate(clusters, 1):
        c.cluster_id = i

    # Assign labels
    _label_clusters(clusters)

    return clusters


def _label_clusters(clusters: list[DisambiguationCluster]) -> None:
    """Assign PRIMARY / SECONDARY / LOW labels in-place (clusters already sorted)."""
    primary_assigned = False
    for c in clusters:
        if not primary_assigned and c.max_score >= PRIMARY_MIN_SCORE:
            c.label = LABEL_PRIMARY
            primary_assigned = True
        elif c.max_score >= SECONDARY_MIN_SCORE:
            c.label = LABEL_SECONDARY
        else:
            c.label = LABEL_LOW
    # If no cluster met the primary threshold, promote the top one to secondary
    # (at least one cluster always needs to surface the most-likely identity).
    if not primary_assigned and clusters:
        top = clusters[0]
        if top.max_score >= SECONDARY_MIN_SCORE:
            top.label = LABEL_SECONDARY
        # If everything is below 40 the labels stay LOW — expected for
        # searches that return only squatter accounts.


def attach_identity_fields(
    found: list['CheckResult'],
    clusters: list[DisambiguationCluster],
) -> None:
    """Mutate each CheckResult to set identity_id and is_primary_identity."""
    primary_id = next(
        (c.cluster_id for c in clusters if c.label == LABEL_PRIMARY),
        (clusters[0].cluster_id if clusters else None),
    )
    for c in clusters:
        for idx in c.member_indices:
            if 0 <= idx < len(found):
                found[idx].identity_id       = c.cluster_id
                found[idx].is_primary_identity = (c.cluster_id == primary_id)
