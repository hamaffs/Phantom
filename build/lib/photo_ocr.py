"""Profile-photo OCR — extract handle-shaped text from avatars.

A surprisingly large number of users (especially on gaming, streamer,
and creator platforms) put their handle as text on top of their profile
picture. The cross-link expander can't see that text — it parses URLs
out of bio fields. OCR over the avatar bytes recovers it.

Workflow:

  1. For each FOUND account that has photo bytes downloaded, run
     Tesseract over them.
  2. Filter the OCR output to substrings that look like usernames
     (alphanumeric + underscores / periods, 3–32 chars).
  3. Drop anything that's just the variant we already tested.
  4. Return new candidate handles for the next --expand round.

Why it's a win over Maigret: Maigret doesn't look at avatars at all.
Phantom already downloads them for the photo-cluster step; running OCR
on the same bytes is free in terms of network and almost free in CPU
(~50ms per image for a 400×400 PFP).

Dependencies are optional: tesseract binary + pytesseract Python wrapper.
When either is missing, the module degrades to a no-op — `discover_handles`
returns an empty list, no warning. Users who want this feature install:

    apt install tesseract-ocr
    pip install pytesseract

(Pillow is already a Phantom dependency.)
"""
from __future__ import annotations

import re
import shutil
import sys
from typing import Optional

try:
    import pytesseract  # type: ignore
    _HAS_PYTESSERACT = True
except ImportError:
    _HAS_PYTESSERACT = False


# A token looks like a handle when:
#   - It's 3–32 characters long.
#   - Composed of alphanumeric / underscore / period / hyphen only.
#   - Has at least one letter (filters out OCR garbage like "12345").
#   - Doesn't start with a digit or punctuation (rare in real handles).
_HANDLE_RE = re.compile(r"\b([A-Za-z][A-Za-z0-9_.\-]{2,31})\b")

# Words that look like handles but never are — common OCR-extracted
# nouns and platform watermarks. Tight list to avoid dropping legit
# usernames; extend cautiously.
_HANDLE_BLOCKLIST = frozenset({
    "official", "verified", "private", "follow", "following", "followers",
    "official", "profile", "username", "subscribe", "channel", "youtube",
    "twitter", "instagram", "tiktok", "twitch", "facebook", "linkedin",
    "discord", "reddit", "github", "snapchat", "pinterest", "threads",
    "the", "and", "for", "with", "from",
})

# OCR on profile photos is *noisy*. A 64×64 avatar with a few decorative
# characters routinely produces "ARUN", "cam", "JOSH" etc. — random
# OCR garbage that would FOUND on dozens of platforms (real strangers
# with those handles), polluting the scan.
#
# To stay within Phantom's "accuracy first" promise, OCR-derived handles
# need to *look* like deliberate username text:
#   - at least one non-letter character (digit, underscore, dot, dash)
#     OR begin/end with an `@` marker that the user explicitly typed,
#   - OR be a substring of, OR contain, the originating handle we're
#     already investigating (the user wrote a variant of their own name
#     on their avatar)
#
# Generic capitalized words and English nouns are rejected. This means
# we lose some legitimate OCR-recovered handles (the rare user whose
# handle is just an English word) — but we trade recall for precision
# because the false-positive cost downstream is much higher.


def available() -> bool:
    """True iff Tesseract is installable and importable. The CLI checks
    this before announcing the feature to avoid printing spurious
    "OCR found nothing" messages on systems without it."""
    if not _HAS_PYTESSERACT:
        return False
    return shutil.which("tesseract") is not None


def _ocr_bytes(image_bytes: bytes) -> str:
    """Run Tesseract on raw image bytes. Returns the OCR text, or "" on
    any failure (corrupt image, unsupported format, Tesseract error)."""
    try:
        from PIL import Image
        import io
        img = Image.open(io.BytesIO(image_bytes))
        # Upscale small avatars — Tesseract reads 300+dpi best, and
        # Twitter/IG often serve 200px square avatars. Bicubic to a
        # 600px square doubles legibility without artefacts.
        if min(img.size) < 400:
            scale = 600 / min(img.size)
            new_size = (int(img.size[0] * scale), int(img.size[1] * scale))
            img = img.resize(new_size, Image.BICUBIC)
        # Grayscale + light auto-contrast helps OCR on stylised avatars.
        img = img.convert("L")
        return pytesseract.image_to_string(img, config="--psm 11")
    except Exception:
        return ""


def _looks_handle_shaped(token: str, anchor_handles: set[str]) -> bool:
    """Stricter precision filter for OCR-derived candidates.

    A token is accepted only when it looks deliberately username-shaped:
      1. it contains a non-letter (digit / underscore / dot / dash), OR
      2. it's a substring of one of the anchor handles we're already
         investigating, OR an anchor is a substring of it (the user
         wrote a variant of their own handle on their avatar), OR
      3. it's at least 5 chars long AND contains both upper and lower
         case characters (deliberate casing pattern — `JohnDoe` ok,
         `JOSH` rejected).

    Bare all-caps or all-lowercase English-word-shaped tokens (`ARUN`,
    `cam`, `JOSH`) are rejected because they're the dominant OCR noise
    pattern on non-text avatars.
    """
    if any(not c.isalpha() for c in token):
        return True
    lower = token.lower()
    for anchor in anchor_handles:
        a = anchor.lower()
        if len(a) >= 4 and (a in lower or lower in a):
            return True
    if len(token) >= 5 and any(c.isupper() for c in token) and any(c.islower() for c in token):
        return True
    return False


def _extract_candidate_handles(text: str, already_tested: set[str]) -> list[str]:
    """Filter OCR text down to plausible new handles."""
    seen: set[str] = set()
    out: list[str] = []
    tested_lower = {h.lower() for h in already_tested}
    for m in _HANDLE_RE.finditer(text):
        h = m.group(1)
        lower = h.lower()
        if lower in tested_lower or lower in seen:
            continue
        if lower in _HANDLE_BLOCKLIST:
            continue
        if not any(c.isalpha() for c in h):
            continue
        # Reject tokens that look like domains rather than handles. A
        # `.com`, `.io`, `.co`, etc. suffix is a giveaway.
        if re.search(r"\.(com|io|co|net|org|app|me|tv|gg|ai)$", lower):
            continue
        # Stricter "looks like a deliberate username" gate.
        if not _looks_handle_shaped(h, already_tested):
            continue
        seen.add(lower)
        out.append(h)
    return out


def discover_handles(
    photo_bytes_map: dict[str, bytes],
    already_tested: set[str],
) -> list[str]:
    """OCR every avatar in `photo_bytes_map` and return new handle
    candidates the variant queue should test.

    The caller is responsible for passing the bytes (Phantom already
    fetches them for the photo-cluster step), so this module never
    touches the network.

    Returns an empty list when Tesseract isn't installed.
    """
    if not available() or not photo_bytes_map:
        return []
    accumulated: list[str] = []
    seen: set[str] = set()
    for _url, data in photo_bytes_map.items():
        if not data:
            continue
        text = _ocr_bytes(data)
        if not text or not text.strip():
            continue
        for h in _extract_candidate_handles(text, already_tested):
            if h.lower() in seen:
                continue
            seen.add(h.lower())
            accumulated.append(h)
    return accumulated
