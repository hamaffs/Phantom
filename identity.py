"""Identity correlation.

Phantom's `[ FOUND ]` list says *some* account exists at each URL — it
doesn't say they're the same person. This module builds that bridge:
fetch each FOUND profile photo, perceptually hash it, and cluster
results whose photo + display name + bio agree.

The output is one or more "identity clusters". A cluster is a group of
results we believe belong to the same person, plus an aggregated view
(best display name, total followers, oldest joined date, all locations,
all photos). Each cluster also gets a confidence score.

Why hash photos rather than compare URLs?
  Same photo on Instagram and Twitter ships from completely different
  CDNs with different sizes / signatures / cache-busting query strings.
  Byte-equal comparison fails. Perceptual hashing (`imagehash.phash`)
  gives a 64-bit fingerprint of *what the picture looks like*, so the
  same headshot scaled down for a 48px avatar still matches the original
  400px upload.

Why not pull a session and grab the JSON API instead?
  Goal is zero-auth, public-only. We hash whatever public photo URL the
  enrichment step already pulled out of the SSR'd HTML. No tokens, no
  cookies, nothing personal of yours sent to the platform.

This module is best-effort — if a host blocks the image fetch, or the
file isn't a recognisable image, we just skip that result. The original
FOUND row stays in the report; only the *correlation* loses one signal.
"""

from __future__ import annotations

import asyncio
import io
import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Optional

import aiohttp

try:
    from PIL import Image  # type: ignore
    import imagehash  # type: ignore
    HAS_IMAGES = True
except ImportError:  # pragma: no cover — deps optional but in requirements
    Image = None  # type: ignore
    imagehash = None  # type: ignore
    HAS_IMAGES = False


# Hamming distance threshold below which two phashes are considered
# "the same image". 64-bit phash → distances 0–10 mean "identical or
# only minor variation" (resize, JPEG re-compression, subtle crop). 8 is
# the sweet spot in practice — tighter misses real matches across CDNs,
# looser starts merging different-but-similar selfies.
_PHASH_MATCH_DISTANCE = 8

# Profile photo fetch budget. Stays small because we don't actually need
# the whole image — phash works on a 32×32 downscale.
_IMAGE_FETCH_TIMEOUT = 8.0
_IMAGE_MAX_BYTES = 2 * 1024 * 1024
_IMAGE_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Words we don't want polluting the bio-overlap signal — boilerplate that
# every Instagram / Threads / Pinterest profile says.
_BIO_STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "i", "im", "i'm", "me", "my",
    "is", "of", "to", "in", "on", "at", "for", "with", "by",
    "see", "instagram", "photos", "videos", "threads", "pinterest",
    "tiktok", "facebook", "twitter", "youtube", "follow",
    "followers", "following", "posts", "post",
}

_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")

# Tiny no-API language hints. Each language has a bag of cheap-to-spot
# common words; we count matches per language and attribute the bio to
# the highest-scoring one. This is **rough** — we lean towards "no
# guess" rather than pretending we know more than we do. Anyone wanting
# real language ID should bolt on langdetect / fasttext.
_LANG_WORDS = {
    "fr": {"je", "tu", "moi", "mais", "avec", "dans", "pour", "sans", "très", "merci", "amour", "vie"},
    "es": {"yo", "tu", "pero", "para", "con", "sin", "muy", "vida", "amor", "gracias", "hola", "soy"},
    "de": {"ich", "und", "aber", "mit", "ohne", "sehr", "leben", "liebe", "danke", "wie", "noch"},
    "pt": {"eu", "mas", "para", "com", "sem", "muito", "vida", "amor", "obrigado", "obrigada", "ola"},
    "it": {"io", "ma", "con", "senza", "molto", "vita", "amore", "grazie", "ciao", "sono"},
    "nl": {"ik", "maar", "met", "zonder", "leven", "liefde", "dank", "hallo"},
    "ar": set(),  # detected via script
    "ja": set(),  # detected via script
    "zh": set(),  # detected via script
    "ko": set(),  # detected via script
    "ru": set(),  # detected via script
    "tr": {"ben", "ama", "ile", "için", "çok", "hayat", "merhaba"},
}

# Script-based detection — Unicode block presence beats any wordlist for
# CJK / RTL / Cyrillic. Codepoint ranges from the canonical blocks.
_SCRIPT_BLOCKS = [
    ("ar", 0x0600, 0x06FF),
    ("ar", 0x0750, 0x077F),
    ("ja", 0x3040, 0x30FF),  # hiragana + katakana
    ("ja", 0x4E00, 0x9FFF),  # also chinese, but combined with kana implies ja
    ("zh", 0x4E00, 0x9FFF),
    ("ko", 0xAC00, 0xD7AF),
    ("ru", 0x0400, 0x04FF),
]

# When location strings are missing but joined dates ship a timezone, we
# can at least narrow to a UTC offset. This isn't a country, but with
# language signal it's enough to pick a region.
_TZ_HINT_FROM_OFFSET = {
    -5: "US East Coast / Eastern Caribbean",
    -8: "US West Coast",
    0: "UK / West Africa",
    1: "Western Europe",
    2: "Central / Eastern Europe",
    3: "Eastern Europe / Middle East",
    5: "Central Asia",
    8: "China / SE Asia",
    9: "Japan / Korea",
    10: "Eastern Australia",
}


def _detect_lang(text: str) -> Optional[str]:
    if not text:
        return None
    # Script first.
    script_counts: Counter = Counter()
    for ch in text:
        cp = ord(ch)
        for lang, lo, hi in _SCRIPT_BLOCKS:
            if lo <= cp <= hi:
                script_counts[lang] += 1
                break
    if script_counts:
        # If both ja-only kana and zh-shared CJK appear, ja wins.
        return script_counts.most_common(1)[0][0]
    # Word lookup for Latin-script languages.
    tokens = {t.lower() for t in _TOKEN_RE.findall(text) if len(t) > 1}
    word_counts = Counter()
    for lang, words in _LANG_WORDS.items():
        if not words:
            continue
        word_counts[lang] = len(tokens & words)
    if word_counts and word_counts.most_common(1)[0][1] >= 2:
        return word_counts.most_common(1)[0][0]
    return None


def _infer_geo(members: list[dict]) -> Optional[GeoHint]:
    """Aggregate signals from a cluster and produce a region hint.

    Inputs (priority order):
      1. Explicit location strings ("Paris, France", "Tokyo").
      2. Country fields (YouTube ships a clean 2-letter or full name).
      3. Bio language detection.
      4. Joined-date timezone offset (last resort).

    Output is intentionally vague — "France" or "Brazil" is fine,
    "12 rue Lafayette" is not. We also track which signals fired so the
    confidence can be defended in the report.
    """
    locations: list[str] = []
    bios: list[str] = []
    tz_offsets: Counter = Counter()
    for m in members:
        prof = m.get("profile") or {}
        if prof.get("location"):
            locations.append(str(prof["location"]).strip())
        if prof.get("bio"):
            bios.append(str(prof["bio"]))
        joined = prof.get("joined")
        if isinstance(joined, str) and "+" in joined or (
            isinstance(joined, str) and joined.endswith("Z")
        ):
            # ISO offset.
            mo = re.search(r"([+-])(\d{2}):?(\d{2})$", joined)
            if mo:
                hrs = int(mo.group(2))
                if mo.group(1) == "-":
                    hrs = -hrs
                tz_offsets[hrs] += 1
            elif joined.endswith("Z"):
                tz_offsets[0] += 1

    signals: list[str] = []
    region: Optional[str] = None
    confidence = 0.0

    if locations:
        # Pick the most common location — if all members report it the
        # same we can be confident.
        loc_counts = Counter(locations)
        top, n = loc_counts.most_common(1)[0]
        region = top
        confidence = min(0.6 + 0.1 * n, 0.95)
        signals.append(f"location string ({n}× '{top}')")

    if not region:
        lang = _detect_lang(" ".join(bios))
        if lang:
            label = {
                "en": "English-speaking", "fr": "France / Francophone",
                "es": "Spanish-speaking", "de": "German-speaking",
                "pt": "Portuguese-speaking", "it": "Italy / Italian-speaking",
                "nl": "Dutch-speaking", "ar": "Arabic-speaking",
                "ja": "Japan", "zh": "China / Chinese-speaking",
                "ko": "Korea", "ru": "Russia / Russian-speaking",
                "tr": "Turkey",
            }.get(lang, lang)
            region = label
            confidence = 0.5
            signals.append(f"bio language ({lang})")

    if tz_offsets:
        offset, _ = tz_offsets.most_common(1)[0]
        tz_label = _TZ_HINT_FROM_OFFSET.get(offset)
        if tz_label:
            signals.append(f"joined-date tz UTC{offset:+d} → {tz_label}")
            if not region:
                region = tz_label
                confidence = 0.35

    if not signals:
        return None
    return GeoHint(region=region, confidence=round(confidence, 2), signals=signals)


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

@dataclass
class GeoHint:
    """Best-guess regional inference based on no-API signals."""
    region: Optional[str] = None       # human label ("France", "Brazil", "EN-speaking")
    confidence: float = 0.0
    signals: list[str] = field(default_factory=list)


@dataclass
class IdentityCluster:
    """Group of FOUND results we believe belong to the same person."""

    # Indexes back into the original results list. Keeps the cluster
    # serializable without duplicating CheckResult data on disk.
    member_indexes: list[int] = field(default_factory=list)

    display_name: Optional[str] = None
    photos: list[str] = field(default_factory=list)
    locations: list[str] = field(default_factory=list)
    bios: list[str] = field(default_factory=list)
    joined_oldest: Optional[str] = None
    total_followers: Optional[int] = None
    total_following: Optional[int] = None
    total_posts: Optional[int] = None
    verified_on: list[str] = field(default_factory=list)
    private_on: list[str] = field(default_factory=list)
    sites: list[str] = field(default_factory=list)
    variants: list[str] = field(default_factory=list)
    geo_hint: Optional[GeoHint] = None
    # 0.0 — 1.0. 1.0 = absolute lock (multiple sites with matching photo).
    # ~0.5 = single result, can't verify — show as a candidate.
    confidence: float = 0.0
    # Brief human-readable explanation of why these results were grouped.
    rationale: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


# ---------------------------------------------------------------------------
# Photo fetching + hashing
# ---------------------------------------------------------------------------

async def _fetch_image(session: aiohttp.ClientSession, url: str) -> Optional[bytes]:
    """Best-effort image download with a tight budget."""
    try:
        async with session.get(
            url,
            headers={"User-Agent": _IMAGE_USER_AGENT, "Accept": "image/*,*/*;q=0.8"},
            timeout=aiohttp.ClientTimeout(total=_IMAGE_FETCH_TIMEOUT),
            allow_redirects=True,
        ) as resp:
            if resp.status != 200:
                return None
            data = await resp.content.read(_IMAGE_MAX_BYTES)
            if not data or len(data) < 200:  # tiny → tracking pixel, skip
                return None
            return data
    except Exception:
        return None


def _phash_bytes(data: bytes):
    """Perceptual hash of raw image bytes. Returns the imagehash or None."""
    if not HAS_IMAGES:
        return None
    try:
        with Image.open(io.BytesIO(data)) as img:
            img.load()
            return imagehash.phash(img.convert("RGB"))
    except Exception:
        return None


async def fetch_photo_hashes(
    photo_urls: Iterable[Optional[str]],
) -> list[Optional[Any]]:
    """Fetch + hash a list of profile photo URLs in parallel.

    Order of the returned list matches the input. None entries indicate
    "couldn't get a hash" — could be a missing URL, a 403, a non-image
    response, or an unsupported format.
    """
    urls = list(photo_urls)
    if not HAS_IMAGES or not any(urls):
        return [None] * len(urls)

    sem = asyncio.Semaphore(8)

    async with aiohttp.ClientSession() as session:
        async def one(u: Optional[str]):
            if not u:
                return None
            async with sem:
                data = await _fetch_image(session, u)
            if not data:
                return None
            return _phash_bytes(data)

        return await asyncio.gather(*(one(u) for u in urls))


# ---------------------------------------------------------------------------
# Tokenisation helpers
# ---------------------------------------------------------------------------

def _tokens(text: Optional[str]) -> set[str]:
    if not text:
        return set()
    out: set[str] = set()
    for tok in _TOKEN_RE.findall(text.lower()):
        if len(tok) <= 2 or tok in _BIO_STOPWORDS or tok.isdigit():
            continue
        out.add(tok)
    return out


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def _normalise_name(name: Optional[str]) -> str:
    if not name:
        return ""
    # Strip the common "X | Site name" suffix patterns the SSR adds.
    head = re.split(r"\s+[•·|—-]\s+", name)[0]
    head = re.sub(r"\s*\([^)]*\)\s*$", "", head)
    return head.strip().lower()


# ---------------------------------------------------------------------------
# Clustering
# ---------------------------------------------------------------------------

def _are_same_person(
    a: dict,
    b: dict,
    phash_a,
    phash_b,
) -> tuple[bool, list[str]]:
    """Decide if two FOUND results look like the same person.

    Strongest signal: matching profile photo (perceptual hash within
    threshold). That alone is enough to merge.

    Otherwise: matching normalised display name PLUS strong bio-token
    overlap. Either alone is too weak — common names collide and short
    bios share filler words.
    """
    rationale: list[str] = []

    if phash_a is not None and phash_b is not None:
        d = phash_a - phash_b
        if d <= _PHASH_MATCH_DISTANCE:
            rationale.append(f"matching profile photo (hamming={d})")
            return True, rationale

    name_a = _normalise_name(a.get("display_name"))
    name_b = _normalise_name(b.get("display_name"))
    name_match = name_a and name_b and name_a == name_b

    bio_overlap = _jaccard(_tokens(a.get("bio")), _tokens(b.get("bio")))

    if name_match and bio_overlap >= 0.4:
        rationale.append(
            f"identical display name ({name_a!r}) + bio overlap "
            f"{bio_overlap:.2f}"
        )
        return True, rationale

    return False, rationale


def _aggregate(
    indexes: list[int],
    results: list[dict],
    photos_by_index: dict[int, str],
    rationales: list[str],
) -> IdentityCluster:
    """Roll up the per-platform data into one cluster summary."""
    members = [results[i] for i in indexes]

    name_counts: Counter = Counter()
    for m in members:
        n = (m.get("profile") or {}).get("display_name")
        if n:
            name_counts[_normalise_name(n)] += 1
    best_name = name_counts.most_common(1)[0][0].title() if name_counts else None

    photos = list({photos_by_index[i] for i in indexes if i in photos_by_index})

    locations = list({
        (m.get("profile") or {}).get("location")
        for m in members
        if (m.get("profile") or {}).get("location")
    })

    bios = [
        (m.get("profile") or {}).get("bio")
        for m in members
        if (m.get("profile") or {}).get("bio")
    ]

    joined = sorted([
        (m.get("profile") or {}).get("joined")
        for m in members
        if (m.get("profile") or {}).get("joined")
    ])
    joined_oldest = joined[0] if joined else None

    def _sum(field: str) -> Optional[int]:
        vals = [
            (m.get("profile") or {}).get(field)
            for m in members
        ]
        nums = [v for v in vals if isinstance(v, (int, float))]
        return int(sum(nums)) if nums else None

    verified_on = [
        m["site"] for m in members
        if (m.get("profile") or {}).get("verified") is True
    ]
    private_on = [
        m["site"] for m in members
        if (m.get("profile") or {}).get("private") is True
    ]

    sites = sorted({m["site"] for m in members})
    variants = sorted({m.get("variant") for m in members if m.get("variant")})

    # Confidence: photo-matched groups of 2+ platforms = lock. Single
    # result = candidate. Name+bio matches start at 0.7 and rise with
    # member count.
    has_photo_match = any("matching profile photo" in r for r in rationales)
    if has_photo_match and len(members) >= 2:
        confidence = min(0.9 + 0.05 * (len(members) - 2), 1.0)
    elif has_photo_match:
        confidence = 0.85
    elif len(members) >= 3:
        confidence = 0.8
    elif len(members) == 2:
        confidence = 0.6
    else:
        confidence = 0.4

    return IdentityCluster(
        member_indexes=sorted(indexes),
        display_name=best_name,
        photos=photos,
        locations=locations,
        bios=bios,
        joined_oldest=joined_oldest,
        total_followers=_sum("followers"),
        total_following=_sum("following"),
        total_posts=_sum("posts"),
        verified_on=verified_on,
        private_on=private_on,
        sites=sites,
        variants=variants,
        geo_hint=_infer_geo(members),
        confidence=round(confidence, 2),
        rationale=sorted(set(rationales)),
    )


def correlate(
    found_dicts: list[dict],
    phashes: list[Optional[Any]],
) -> list[IdentityCluster]:
    """Cluster FOUND results into identity groups.

    `found_dicts` is the list of already-asdict()'d CheckResult dicts —
    we work on dicts to keep this module decoupled from checker.py types.
    `phashes` is the parallel-fetched hash for each result's profile
    photo (None if not available).
    """
    n = len(found_dicts)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    cluster_rationale: dict[int, list[str]] = {i: [] for i in range(n)}

    for i in range(n):
        prof_i = found_dicts[i].get("profile") or {}
        for j in range(i + 1, n):
            prof_j = found_dicts[j].get("profile") or {}
            same, why = _are_same_person(prof_i, prof_j, phashes[i], phashes[j])
            if same:
                union(i, j)
                ra, rb = find(i), find(j)
                cluster_rationale.setdefault(ra, []).extend(why)
                cluster_rationale.setdefault(rb, []).extend(why)

    photos_by_index: dict[int, str] = {}
    for i, r in enumerate(found_dicts):
        photo = (r.get("profile") or {}).get("photo")
        if photo:
            photos_by_index[i] = photo

    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)

    clusters: list[IdentityCluster] = []
    for root, members in groups.items():
        clusters.append(
            _aggregate(
                members, found_dicts, photos_by_index,
                cluster_rationale.get(root, []),
            )
        )

    # Biggest, most-confident clusters first.
    clusters.sort(
        key=lambda c: (-c.confidence, -len(c.member_indexes))
    )
    return clusters


# ---------------------------------------------------------------------------
# Top-level entry
# ---------------------------------------------------------------------------

async def build_identities(found: list[dict]) -> list[IdentityCluster]:
    """High-level: hash each FOUND profile photo and produce clusters.

    Pass in the list of result dicts (asdict on the CheckResult). Returns
    a sorted list of IdentityCluster instances. Always returns at least
    one cluster per FOUND result (a singleton cluster if nothing matches).
    """
    if not found:
        return []
    photo_urls = [(r.get("profile") or {}).get("photo") for r in found]
    phashes = await fetch_photo_hashes(photo_urls)
    return correlate(found, phashes)
