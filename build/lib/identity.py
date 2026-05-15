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

try:
    import cv2  # type: ignore
    import numpy as _np  # type: ignore
    HAS_CV2 = True
except ImportError:  # pragma: no cover — opencv-python is in requirements
    cv2 = None  # type: ignore
    _np = None  # type: ignore
    HAS_CV2 = False

# Per-process cache for face-detection results, keyed by photo URL.
# OpenCV's Haar cascade load + per-image scan is ~30–80ms; cache so a
# rerun of `_pick_subject_photo` (or any future caller) is instant.
_FACE_CACHE: dict[str, bool] = {}
_FACE_CASCADE = None  # lazily loaded on first call


def _load_face_cascade():
    """Lazy-load the bundled Haar frontal-face cascade once per process."""
    global _FACE_CASCADE
    if _FACE_CASCADE is not None or not HAS_CV2:
        return _FACE_CASCADE
    try:
        path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        _FACE_CASCADE = cv2.CascadeClassifier(path)
        if _FACE_CASCADE.empty():
            _FACE_CASCADE = None
    except Exception:
        _FACE_CASCADE = None
    return _FACE_CASCADE


def has_human_face(image_bytes: Optional[bytes], cache_key: Optional[str] = None) -> bool:
    """True if OpenCV's frontal-face Haar detector finds at least one
    face in `image_bytes`. False on any failure (missing cv2, undecodable
    image, no detection). When `cache_key` is provided (typically the
    photo URL), the result is memoised for the rest of the process so
    repeat calls are free.
    """
    if cache_key is not None and cache_key in _FACE_CACHE:
        return _FACE_CACHE[cache_key]
    result = False
    if HAS_CV2 and image_bytes and len(image_bytes) >= 200:
        cascade = _load_face_cascade()
        if cascade is not None:
            try:
                arr = _np.frombuffer(image_bytes, dtype=_np.uint8)
                gray = cv2.imdecode(arr, cv2.IMREAD_GRAYSCALE)
                if gray is not None:
                    faces = cascade.detectMultiScale(
                        gray,
                        scaleFactor=1.05,
                        minNeighbors=5,
                        minSize=(20, 20),
                    )
                    result = len(faces) > 0
            except Exception:
                result = False
    if cache_key is not None:
        _FACE_CACHE[cache_key] = result
    return result


# Hamming distance threshold below which two phashes are considered
# "the same image". 64-bit phash → distances 0–10 mean "identical or
# only minor variation" (resize, JPEG re-compression, subtle crop).
# Bumped from 8 to 12 to capture the same-logo-different-bg/color case
# where a creator uses one logo across platforms but each upload has a
# distinct background tint or palette swap. Risk is higher false-merge
# on similar-but-different selfies; the rationale string in each cluster
# still surfaces the exact distance for inspection.
_PHASH_MATCH_DISTANCE = 16
# Companion hashes computed alongside phash. Each catches variations the
# others miss: phash (DCT) is robust to scaling but sensitive to crop;
# dhash (gradient) catches re-encoded photos with shifted edges; whash
# (wavelet) handles soft blurs and tonal shifts. Two photos merge when
# ANY of the three is below its threshold. Thresholds are tuned so a
# single hit isn't enough to false-merge unrelated photos (each is
# ~8 bits out of 64).
_DHASH_MATCH_DISTANCE = 16
_WHASH_MATCH_DISTANCE = 12

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

# Common country names — used to normalise location strings like
# "Tunis, Tunisia" or "Paris, France" into a clean country tag. We only
# match against words at end-of-string after a comma so that city/region
# names that *contain* a country word (e.g. "USA Today") don't trigger.
# Casing-insensitive; multi-word entries first so e.g. "United States"
# is preferred over a stray "States".
_COUNTRY_NAMES = (
    # Multi-word first so the longest match wins.
    "United Kingdom", "United States", "United Arab Emirates",
    "Saudi Arabia", "South Africa", "South Korea", "North Korea",
    "New Zealand", "Czech Republic", "Dominican Republic",
    "Sri Lanka", "Costa Rica", "Puerto Rico", "Hong Kong",
    "Ivory Coast", "El Salvador", "Trinidad and Tobago",
    "Bosnia and Herzegovina", "North Macedonia",
    "Papua New Guinea",
    # Single word.
    "Tunisia", "Algeria", "Morocco", "Egypt", "Libya", "Sudan", "Mauritania",
    "Senegal", "Nigeria", "Ghana", "Kenya", "Ethiopia", "Tanzania", "Uganda",
    "Cameroon", "Angola", "Mozambique", "Zimbabwe", "Zambia", "Rwanda",
    "France", "Germany", "Spain", "Italy", "Portugal", "Belgium",
    "Netherlands", "Switzerland", "Austria", "Sweden", "Norway", "Denmark",
    "Finland", "Iceland", "Ireland", "Poland", "Czechia", "Slovakia",
    "Hungary", "Romania", "Bulgaria", "Greece", "Turkey", "Russia",
    "Ukraine", "Belarus", "Lithuania", "Latvia", "Estonia",
    "Serbia", "Croatia", "Slovenia", "Albania", "Kosovo", "Montenegro",
    "USA", "America", "Canada", "Mexico", "Brazil", "Argentina", "Chile",
    "Colombia", "Peru", "Venezuela", "Uruguay", "Paraguay", "Bolivia",
    "Ecuador", "Cuba", "Jamaica", "Haiti",
    "Iran", "Iraq", "Syria", "Lebanon", "Jordan", "Israel", "Palestine",
    "Qatar", "Kuwait", "Bahrain", "Oman", "Yemen", "Afghanistan",
    "Pakistan", "India", "Bangladesh", "Nepal", "Bhutan", "Maldives",
    "China", "Taiwan", "Japan", "Korea", "Mongolia", "Vietnam", "Laos",
    "Cambodia", "Thailand", "Malaysia", "Singapore", "Indonesia",
    "Philippines", "Myanmar",
    "Australia", "UK", "Britain", "England", "Scotland", "Wales",
)

_COUNTRY_RE = re.compile(
    r'(?:^|[,/])\s*(' + "|".join(re.escape(c) for c in _COUNTRY_NAMES) + r')\s*$',
    re.IGNORECASE,
)
# Looser variant: country word appearing anywhere in the string,
# bounded by start/whitespace/punctuation. Used for soft hints (work
# location, school name, free-form bio location) where the country
# isn't necessarily at the end of the string. Not used for explicit
# location fields — a 200-char bio mentioning "I love France" should
# not be treated as the user living there.
_COUNTRY_LOOSE_RE = re.compile(
    r'(?<![A-Za-z])(' + "|".join(re.escape(c) for c in _COUNTRY_NAMES) + r')(?![A-Za-z])',
    re.IGNORECASE,
)
_COUNTRY_CANONICAL = {
    "uk": "United Kingdom", "britain": "United Kingdom",
    "england": "United Kingdom", "scotland": "United Kingdom",
    "wales": "United Kingdom", "usa": "United States",
    "america": "United States", "korea": "South Korea",
    "czechia": "Czech Republic",
}

# City → country lookup for the major cities most likely to appear in a
# work/school name without an explicit country. Conservative list —
# only cities whose name is unambiguous (no two cities share it). This
# keeps "Lives in Springfield" or "Works at Cambridge" from being
# silently mapped to a country.
_CITY_TO_COUNTRY = {
    "tunis": "Tunisia", "casablanca": "Morocco", "rabat": "Morocco",
    "marrakech": "Morocco", "fes": "Morocco", "fès": "Morocco",
    "algiers": "Algeria", "alger": "Algeria", "oran": "Algeria",
    "cairo": "Egypt", "alexandria": "Egypt",
    "tripoli": "Libya", "lagos": "Nigeria", "abuja": "Nigeria",
    "nairobi": "Kenya", "addis ababa": "Ethiopia",
    "dakar": "Senegal", "abidjan": "Ivory Coast",
    "johannesburg": "South Africa", "cape town": "South Africa",
    "paris": "France", "lyon": "France", "marseille": "France",
    "toulouse": "France", "nice": "France", "bordeaux": "France",
    "berlin": "Germany", "munich": "Germany", "hamburg": "Germany",
    "frankfurt": "Germany", "köln": "Germany", "cologne": "Germany",
    "madrid": "Spain", "barcelona": "Spain", "valencia": "Spain",
    "rome": "Italy", "milan": "Italy", "naples": "Italy",
    "lisbon": "Portugal", "porto": "Portugal",
    "amsterdam": "Netherlands", "rotterdam": "Netherlands",
    "brussels": "Belgium", "antwerp": "Belgium",
    "vienna": "Austria", "zurich": "Switzerland", "geneva": "Switzerland",
    "stockholm": "Sweden", "oslo": "Norway", "copenhagen": "Denmark",
    "helsinki": "Finland", "warsaw": "Poland", "kraków": "Poland",
    "prague": "Czech Republic", "budapest": "Hungary", "athens": "Greece",
    "istanbul": "Turkey", "ankara": "Turkey",
    "moscow": "Russia", "saint petersburg": "Russia", "kiev": "Ukraine",
    "kyiv": "Ukraine",
    "london": "United Kingdom", "manchester": "United Kingdom",
    "edinburgh": "United Kingdom", "glasgow": "United Kingdom",
    "dublin": "Ireland",
    "new york": "United States", "los angeles": "United States",
    "chicago": "United States", "san francisco": "United States",
    "boston": "United States", "seattle": "United States",
    "miami": "United States", "houston": "United States",
    "toronto": "Canada", "vancouver": "Canada", "montreal": "Canada",
    "mexico city": "Mexico",
    "são paulo": "Brazil", "rio de janeiro": "Brazil",
    "buenos aires": "Argentina", "santiago": "Chile",
    "tokyo": "Japan", "osaka": "Japan", "kyoto": "Japan",
    "seoul": "South Korea", "beijing": "China", "shanghai": "China",
    "hong kong": "Hong Kong",
    "bangkok": "Thailand", "singapore": "Singapore", "kuala lumpur": "Malaysia",
    "jakarta": "Indonesia", "manila": "Philippines",
    "delhi": "India", "mumbai": "India", "bangalore": "India",
    "karachi": "Pakistan", "islamabad": "Pakistan", "lahore": "Pakistan",
    "dhaka": "Bangladesh",
    "tehran": "Iran", "baghdad": "Iraq", "damascus": "Syria",
    "beirut": "Lebanon", "amman": "Jordan",
    "tel aviv": "Israel", "jerusalem": "Israel",
    "doha": "Qatar", "dubai": "United Arab Emirates",
    "abu dhabi": "United Arab Emirates", "kuwait city": "Kuwait",
    "sydney": "Australia", "melbourne": "Australia",
    "auckland": "New Zealand",
}


def _normalise_country(loc: str) -> Optional[str]:
    """Pull a clean country name out of a free-form location string.

    "Tunis, Tunisia" → "Tunisia"
    "Paris, France"  → "France"
    "London, UK"     → "United Kingdom"
    "France"         → "France"
    "Lyon"           → "France"   (city → country lookup)
    "Springfield"    → None       (ambiguous, no lookup)
    """
    if not loc:
        return None
    loc = loc.strip()
    m = _COUNTRY_RE.search(loc)
    if m:
        raw = m.group(1).strip()
        return _COUNTRY_CANONICAL.get(raw.lower(), raw)
    # City fallback: look at the first comma-separated token.
    head = loc.split(",", 1)[0].strip().lower()
    if head in _CITY_TO_COUNTRY:
        return _CITY_TO_COUNTRY[head]
    return None


def _country_from_soft(s: str) -> Optional[str]:
    """Extract a country from a soft hint (work title, school name,
    free-form text). Tries a full country word first, then any city
    word from the curated lookup. Returns None when nothing matches.
    """
    if not s:
        return None
    m = _COUNTRY_LOOSE_RE.search(s)
    if m:
        raw = m.group(1).strip()
        return _COUNTRY_CANONICAL.get(raw.lower(), raw)
    low = s.lower()
    for city, country in _CITY_TO_COUNTRY.items():
        if re.search(rf'(?<![A-Za-z]){re.escape(city)}(?![A-Za-z])', low):
            return country
    return None


def detect_lang(text: str) -> Optional[str]:
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
    """Best-guess region from real, attributable signals only.

    Priority (only the *highest* available tier is reported):

      1. Explicit `location` / `country` / `hometown` fields from one or
         more profiles. These are the user's own self-reported location.
         When present, this alone decides the region — language and
         timezone hints add nothing here, so we don't pollute the
         output with them.
      2. Bio language detection. Reported as an explicit language hint
         ("Arabic-speaking content"), never as a country — French content
         could come from France, Belgium, Quebec, Tunisia, Morocco, …
         Confidence is intentionally low.

    Joined-date timezone offsets are deliberately **not** used. UTC+0
    spans the UK, Iceland, Tunisia, Morocco, Senegal — too coarse to be
    a useful signal and too easy to get badly wrong.

    Each signal is annotated with its source site(s) so the report can
    show *why* the region was picked.
    """
    location_strings: list[tuple[str, str]] = []  # (location, source site)
    country_strings: list[tuple[str, str]] = []
    hometowns: list[tuple[str, str]] = []
    # Soft hints — strings where a country name *might* appear but the
    # whole string isn't itself a location (e.g. Facebook's "Works at
    # Google Tunis" → a country word inside an employer name). Used as
    # a country *fallback* only, never as a raw location label.
    soft_hints: list[tuple[str, str]] = []
    bios: list[str] = []
    for m in members:
        prof = m.get("profile") or {}
        site = m.get("site") or "?"
        if prof.get("location"):
            location_strings.append((str(prof["location"]).strip(), site))
        if prof.get("country"):
            country_strings.append((str(prof["country"]).strip(), site))
        if prof.get("hometown"):
            hometowns.append((str(prof["hometown"]).strip(), site))
        for k in ("company", "education"):
            v = prof.get(k)
            if v:
                soft_hints.append((str(v).strip(), f"{site} {k}"))
        # Some extractors (extract_facebook) ship a curated list of
        # geo-relevant strings under `geo_strings`; treat those the
        # same as soft hints.
        for v in prof.get("geo_strings") or []:
            soft_hints.append((str(v).strip(), site))
        if prof.get("bio"):
            bios.append(str(prof["bio"]))

    signals: list[str] = []
    country_votes: Counter = Counter()
    country_sources: dict[str, list[str]] = {}

    # ---- Tier 1: explicit location fields ----
    # Build a vote of country names (extracted from each location).
    # Explicit fields are weighted strongly: the city→country mapping
    # in `_normalise_country` only kicks in for a clean head token, so
    # we trust whatever it returns.
    all_locations = location_strings + hometowns + [
        (c, s) for (c, s) in country_strings
    ]
    for loc, site in all_locations:
        country = _normalise_country(loc)
        if country:
            country_votes[country] += 1
            country_sources.setdefault(country, []).append(site)

    # ---- Tier 1b: soft hints (Facebook "Works at <X>" / "Studied at
    # <Y>" — country names buried inside an employer or school name).
    # Each hint counts as half a vote so an explicit location always
    # wins over a soft hint when both are present, but a soft hint
    # alone can still surface a country.
    soft_country_sources: dict[str, list[str]] = {}
    for hint, src in soft_hints:
        country = _country_from_soft(hint)
        if country:
            soft_country_sources.setdefault(country, []).append(src)

    if country_votes:
        top, n = country_votes.most_common(1)[0]
        srcs = ", ".join(sorted(set(country_sources[top])))
        signals.append(f"profile country: {top} (via {srcs})")
        # Co-mention boosts confidence: "Tunisia" appears as both an
        # explicit location and inside a work/school name.
        if top in soft_country_sources:
            extra = ", ".join(sorted(set(soft_country_sources[top])))
            signals.append(f"corroborated by work/school ({extra})")
        unique_sources = len(set(country_sources[top]))
        confidence = min(0.7 + 0.1 * unique_sources, 0.95)
        # Surface OTHER countries that also appeared (e.g. user lived
        # in two places). Lower-vote candidates are listed but don't
        # change the headline region.
        other_countries = [c for c, _ in country_votes.most_common()[1:]]
        if other_countries:
            signals.append(
                "also mentioned: " + ", ".join(other_countries)
            )
        return GeoHint(region=top, confidence=round(confidence, 2),
                       signals=signals)

    # No explicit location with a clean country. Try soft hints alone.
    if soft_country_sources:
        # Pick the country with the most independent sources.
        ranked = sorted(
            soft_country_sources.items(),
            key=lambda kv: -len(set(kv[1])),
        )
        top, srcs = ranked[0]
        signals.append(
            f"work/school country: {top} (via {', '.join(sorted(set(srcs)))})"
        )
        if len(ranked) > 1:
            signals.append(
                "also mentioned: " + ", ".join(c for c, _ in ranked[1:])
            )
        return GeoHint(region=top, confidence=0.55, signals=signals)

    # No country word matched — fall back to the most-frequent raw
    # location string. This handles inputs like "Lyon" that didn't
    # match either the country list or the city map.
    if all_locations:
        loc_counts: Counter = Counter()
        loc_sources: dict[str, list[str]] = {}
        for loc, site in all_locations:
            loc_counts[loc] += 1
            loc_sources.setdefault(loc, []).append(site)
        top, _ = loc_counts.most_common(1)[0]
        srcs = ", ".join(sorted(set(loc_sources[top])))
        signals.append(f"profile location: {top} (via {srcs})")
        confidence = min(0.55 + 0.1 * len(set(loc_sources[top])), 0.85)
        return GeoHint(region=top, confidence=round(confidence, 2),
                       signals=signals)

    # ---- Tier 2: bio language (presented as a language hint, NOT a
    # country) ----
    lang = detect_lang(" ".join(bios)) if bios else None
    if lang:
        label = {
            "en": "English-speaking content",
            "fr": "French-speaking content",
            "es": "Spanish-speaking content",
            "de": "German-speaking content",
            "pt": "Portuguese-speaking content",
            "it": "Italian-speaking content",
            "nl": "Dutch-speaking content",
            "ar": "Arabic-speaking content",
            "ja": "Japanese-speaking content",
            "zh": "Chinese-speaking content",
            "ko": "Korean-speaking content",
            "ru": "Russian-speaking content",
            "tr": "Turkish-speaking content",
        }.get(lang, f"{lang}-language content")
        signals.append(f"bio language ({lang}) — language only, not a country")
        return GeoHint(region=label, confidence=0.30, signals=signals)

    return None


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

_IMAGE_REFERERS = (
    ("cdninstagram.com", "https://www.instagram.com/"),
    ("fbcdn.net", "https://www.facebook.com/"),
    ("tiktokcdn", "https://www.tiktok.com/"),
    ("twimg.com", "https://x.com/"),
    ("ggpht.com", "https://www.youtube.com/"),
    ("googleusercontent.com", "https://www.youtube.com/"),
    ("jtvnw.net", "https://www.twitch.tv/"),
)


def _referer_for(url: str) -> Optional[str]:
    for host_frag, ref in _IMAGE_REFERERS:
        if host_frag in url:
            return ref
    return None


async def _fetch_image(session: aiohttp.ClientSession, url: str) -> Optional[bytes]:
    """Best-effort image download with a tight budget.

    IG/TikTok/Facebook/Twitter image CDNs often 403 plain GETs without a
    Referer matching their parent site. We attach one based on host match,
    plus retry once with a slightly slower deadline — the vast majority of
    transient failures are TLS handshake stalls under high concurrency.
    """
    headers = {
        "User-Agent": _IMAGE_USER_AGENT,
        "Accept": "image/avif,image/webp,image/png,image/jpeg,image/*;q=0.8,*/*;q=0.5",
        "Accept-Language": "en-US,en;q=0.9",
    }
    ref = _referer_for(url)
    if ref:
        headers["Referer"] = ref

    for attempt in (0, 1):
        try:
            async with session.get(
                url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=_IMAGE_FETCH_TIMEOUT),
                allow_redirects=True,
            ) as resp:
                if resp.status != 200:
                    if attempt == 0 and resp.status in (403, 429, 503):
                        continue
                    return None
                # aiohttp's `read(N)` returns "at most N bytes" — it
                # does NOT block until N bytes accumulate. Calling it
                # with our 2MB budget therefore returns whatever's in
                # the buffer at that moment (often just 5–8KB), which
                # PIL then fails to decode with "image file is
                # truncated". Loop on `readany()` instead so we drain
                # the full body, with the budget enforced manually.
                chunks: list[bytes] = []
                total = 0
                while True:
                    chunk = await resp.content.readany()
                    if not chunk:
                        break
                    chunks.append(chunk)
                    total += len(chunk)
                    if total >= _IMAGE_MAX_BYTES:
                        break
                data = b"".join(chunks)
                if not data or len(data) < 200:  # tiny → tracking pixel, skip
                    return None
                return data
        except Exception:
            if attempt == 0:
                continue
            return None
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


def _multihash_bytes(data: bytes) -> tuple[Optional[Any], Optional[Any], Optional[Any]]:
    """Compute (phash, dhash, whash) in one image-decode. Returns
    (None, None, None) on any failure. Used by clustering to merge on
    *any* hash agreement — orthogonal feature axes catch variations a
    single hash misses."""
    if not HAS_IMAGES:
        return None, None, None
    try:
        with Image.open(io.BytesIO(data)) as img:
            img.load()
            rgb = img.convert("RGB")
            return (
                imagehash.phash(rgb),
                imagehash.dhash(rgb),
                imagehash.whash(rgb),
            )
    except Exception:
        return None, None, None


async def fetch_photo_data(
    photo_urls: Iterable[Optional[str]],
) -> tuple[list[Optional[bytes]], list[Optional[Any]]]:
    """Fetch raw image bytes AND their phashes in one pass.

    Returns (bytes_list, phash_list) parallel to `photo_urls`. The bytes
    are kept around so deep-matching providers (CLIP/DINO embeddings,
    Face++) can reuse them without re-downloading.

    Back-compat shim: callers that need dhash / whash should use
    fetch_photo_hashes_multi() instead.
    """
    urls = list(photo_urls)
    n = len(urls)
    if not any(urls):
        return [None] * n, [None] * n

    sem = asyncio.Semaphore(8)
    bytes_out: list[Optional[bytes]] = [None] * n
    hashes_out: list[Optional[Any]] = [None] * n

    async with aiohttp.ClientSession() as session:
        async def one(idx: int):
            url = urls[idx]
            if not url:
                return
            async with sem:
                data = await _fetch_image(session, url)
            if not data:
                return
            bytes_out[idx] = data
            if HAS_IMAGES:
                hashes_out[idx] = _phash_bytes(data)

        await asyncio.gather(*(one(i) for i in range(n)))

    return bytes_out, hashes_out


async def fetch_photo_data_multi(
    photo_urls: Iterable[Optional[str]],
) -> tuple[
    list[Optional[bytes]],
    list[Optional[Any]],
    list[Optional[Any]],
    list[Optional[Any]],
]:
    """Same as fetch_photo_data but returns (bytes, phash, dhash, whash).

    Three orthogonal hashes per image. Each one decodes the image
    once via _multihash_bytes; failures collapse all three to None for
    that slot. Default-avatar URLs (per confidence._is_default_avatar)
    get their hashes nulled so they can never merge into a real
    person's cluster.
    """
    from confidence import _is_default_avatar
    urls = list(photo_urls)
    n = len(urls)
    if not any(urls):
        return [None] * n, [None] * n, [None] * n, [None] * n

    sem = asyncio.Semaphore(8)
    bytes_out: list[Optional[bytes]] = [None] * n
    phash_out: list[Optional[Any]] = [None] * n
    dhash_out: list[Optional[Any]] = [None] * n
    whash_out: list[Optional[Any]] = [None] * n

    async with aiohttp.ClientSession() as session:
        async def one(idx: int):
            url = urls[idx]
            if not url:
                return
            async with sem:
                data = await _fetch_image(session, url)
            if not data:
                return
            bytes_out[idx] = data
            if not HAS_IMAGES:
                return
            ph, dh, wh = _multihash_bytes(data)
            # Default-avatar URLs are real images (e.g. Twitch's
            # silhouette CDN) but they're shared across every user
            # with no PFP — clustering them would group strangers.
            # Drop their hashes so they're never merge candidates.
            if _is_default_avatar(url):
                ph, dh, wh = None, None, None
            phash_out[idx] = ph
            dhash_out[idx] = dh
            whash_out[idx] = wh

        await asyncio.gather(*(one(i) for i in range(n)))

    return bytes_out, phash_out, dhash_out, whash_out


async def fetch_photo_hashes(
    photo_urls: Iterable[Optional[str]],
) -> list[Optional[Any]]:
    """Back-compat wrapper around fetch_photo_data — phashes only."""
    _, hashes = await fetch_photo_data(photo_urls)
    return hashes


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
    dhash_a=None,
    dhash_b=None,
    whash_a=None,
    whash_b=None,
) -> tuple[bool, list[str]]:
    """Decide if two FOUND results look like the same person.

    Strongest signal: matching profile photo. Three orthogonal hashes
    are computed per image (phash/dhash/whash); merge when ANY of
    them is below its threshold. Each catches a class of variation
    the others miss — phash is robust to scaling but sensitive to
    crop, dhash catches re-encoded photos with shifted gradients,
    whash handles soft blurs and tonal shifts. A single hit is enough
    to merge but the rationale records exactly which hash agreed and
    its distance, so over-merges stay debuggable.

    Otherwise: matching normalised display name PLUS strong bio-token
    overlap. Either alone is too weak — common names collide and short
    bios share filler words.
    """
    rationale: list[str] = []

    # Try every hash that's available on both sides. First one under
    # its threshold wins and we return immediately — the rationale
    # records the kind + distance for inspection.
    for kind, ha, hb, threshold in (
        ("phash", phash_a, phash_b, _PHASH_MATCH_DISTANCE),
        ("dhash", dhash_a, dhash_b, _DHASH_MATCH_DISTANCE),
        ("whash", whash_a, whash_b, _WHASH_MATCH_DISTANCE),
    ):
        if ha is None or hb is None:
            continue
        d = ha - hb
        if d <= threshold:
            rationale.append(
                f"matching profile photo ({kind}={d})"
            )
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
    has_photo_match = any(
        "matching profile photo" in r
        or "matching image content" in r
        or "matching face" in r
        for r in rationales
    )
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
    extra_edges: Optional[list[tuple[int, int, str]]] = None,
    dhashes: Optional[list[Optional[Any]]] = None,
    whashes: Optional[list[Optional[Any]]] = None,
) -> list[IdentityCluster]:
    """Cluster FOUND results into identity groups.

    `found_dicts` is the list of already-asdict()'d CheckResult dicts —
    we work on dicts to keep this module decoupled from checker.py types.
    `phashes` is the parallel-fetched hash for each result's profile
    photo (None if not available). `dhashes` and `whashes` are optional
    companion hashes (difference + wavelet); when supplied, two photos
    merge if ANY of the three hashes is below its threshold.
    `extra_edges` is an optional list of (i, j, rationale) triples from
    deep-matching providers (DINO/Face++); each one merges its two
    indices into the same cluster with the given rationale string.
    """
    n = len(found_dicts)
    parent = list(range(n))
    dhashes = dhashes or [None] * n
    whashes = whashes or [None] * n

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
            same, why = _are_same_person(
                prof_i, prof_j,
                phashes[i], phashes[j],
                dhashes[i], dhashes[j],
                whashes[i], whashes[j],
            )
            if same:
                union(i, j)
                ra, rb = find(i), find(j)
                cluster_rationale.setdefault(ra, []).extend(why)
                cluster_rationale.setdefault(rb, []).extend(why)

    for i, j, why in (extra_edges or []):
        if 0 <= i < n and 0 <= j < n:
            union(i, j)
            root = find(i)
            cluster_rationale.setdefault(root, []).append(why)

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

def aggregate_all(found: list[dict]) -> Optional[IdentityCluster]:
    """One identity summary built from *every* FOUND result.

    The per-cluster view (`build_identities`) only fires when photos
    match across platforms, which leaves single-platform users with
    nothing to show. This function instead asks: what can we say about
    *the person whose accounts these are*, treating every FOUND result
    as a contribution?

    Behaviour:
    - Photos are deduped (kept as-is — no phash needed; the URL set is
      the union of all profile pics).
    - Display name is the most common normalized name across results.
    - Locations are vote-counted across every result's `location` field.
    - Bios from every result feed the language/geo inference.
    - Followers/following/posts are summed (best-effort: a Twitter
      account with 1k followers and an Instagram with 50 followers
      shows 1050 — meaningful as "reach").

    Returns None if `found` is empty, otherwise one cluster with
    member_indexes = range(len(found)).
    """
    if not found:
        return None
    indexes = list(range(len(found)))
    photos_by_index = {
        i: (r.get("profile") or {}).get("photo")
        for i, r in enumerate(found)
        if (r.get("profile") or {}).get("photo")
    }
    cluster = _aggregate(indexes, found, photos_by_index, [])
    # Confidence here means "how confident are we that we know who this
    # person is", not "how confident are we that these are the same
    # person" — the latter is what the per-cluster confidence tracks.
    # We use number of contributing platforms as a rough signal: more
    # platforms → more agreement → higher confidence.
    n = len(indexes)
    if n >= 5:
        cluster.confidence = 0.85
    elif n >= 3:
        cluster.confidence = 0.7
    elif n == 2:
        cluster.confidence = 0.55
    else:
        cluster.confidence = 0.4
    cluster.rationale = [f"aggregated from {n} FOUND account(s)"]
    return cluster


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


async def build_overall_and_clusters(
    found: list[dict],
    deep_options: Optional[Any] = None,
) -> tuple[Optional[IdentityCluster], list[IdentityCluster], Any, dict[str, bool], dict[str, bytes]]:
    """Run both: an overall aggregate AND per-photo clusters.

    Returns (overall, clusters, deep_evidence, face_map). `deep_evidence`
    is a `photo_deep.DeepEvidence` instance when `deep_options` was
    provided and at least one provider was wired up; `None` otherwise.
    `face_map` is a `{photo_url: has_face}` dict computed via OpenCV's
    Haar cascade — empty if cv2 isn't installed or no photos resolved.

    Deep providers (DINOv2, Face++) augment the phash-only clustering:
    they add edges where phash misses (logos recoloured, selfies at
    different angles).
    """
    if not found:
        return None, [], None, {}, {}
    photo_urls = [(r.get("profile") or {}).get("photo") for r in found]
    photo_bytes, phashes, dhashes, whashes = await fetch_photo_data_multi(
        photo_urls,
    )

    photo_bytes_map: dict[str, bytes] = {
        url: data
        for url, data in zip(photo_urls, photo_bytes)
        if url and data
    }

    # Detect faces once per URL; cached so any caller (e.g.
    # _pick_subject_photo) can call has_human_face(url) cheaply later.
    face_map: dict[str, bool] = {}
    if HAS_CV2:
        for url, data in zip(photo_urls, photo_bytes):
            if not url:
                continue
            face_map[url] = has_human_face(data, cache_key=url)

    overall = aggregate_all(found)

    # Phase 1: multi-hash clustering, then capture which pairs got merged.
    phash_clusters = correlate(found, phashes, dhashes=dhashes, whashes=whashes)
    existing_edges: set[tuple[int, int]] = set()
    for c in phash_clusters:
        idx = sorted(c.member_indexes)
        for i in range(len(idx)):
            for j in range(i + 1, len(idx)):
                existing_edges.add((idx[i], idx[j]))

    deep_evidence = None
    if deep_options is not None and getattr(deep_options, "enabled", False):
        try:
            import photo_deep  # local import — avoid circular at top
            deep_evidence = await photo_deep.run_deep(
                found, photo_urls, photo_bytes, deep_options,
                existing_edges=existing_edges,
            )
        except Exception as e:  # never let deep failure kill the run
            print(f"photo-deep: skipped due to error: {e}", file=__import__("sys").stderr)
            deep_evidence = None

    # Phase 2: re-cluster with extra edges from deep providers.
    if deep_evidence and deep_evidence.extra_edges:
        clusters = correlate(
            found, phashes,
            extra_edges=deep_evidence.extra_edges,
            dhashes=dhashes, whashes=whashes,
        )
    else:
        clusters = phash_clusters

    return overall, clusters, deep_evidence, face_map, photo_bytes_map
