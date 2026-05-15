"""Stylometric correlation — does the same person write all these bios?

A signal Maigret literally doesn't have. The disambiguation engine
already compares display names, photos, location, follower counts. None
of those help when an impostor copies the display name and uses a
different photo: clustering still bundles them in because the
*surface-level identity tokens* line up.

Writing style is much harder to fake. Capitalization habits (lowercase-
only, ALL CAPS shouting, sentence case), punctuation choices (em-dash
vs hyphen, oxford comma, ellipsis), emoji frequency and which emoji,
lexical signatures (favourite words, slang) — these stay consistent
across a person's accounts but vary wildly between different people
sharing a handle.

We extract a small feature vector per bio and compute cosine similarity
between every pair of FOUND accounts. Pairs above the threshold pick up
a +2 disambiguation weight; pairs below it add nothing (no penalty —
short bios just don't carry enough signal). Local-only, no models, no
external libraries.

This module exposes one function: `style_score(a, b) -> float in [0,1]`
plus a small helper `feature_vector(text)`. Callers wire the score into
their existing disambiguation edge calculation.
"""
from __future__ import annotations

import math
import re
import unicodedata


# Words common enough across all English bios that their frequency is
# noise rather than signal. Pruned aggressively — we only filter the
# absolute mainstays. Bigger stop-lists kill discriminating power.
_STOPWORDS = frozenset({
    "a", "an", "and", "the", "is", "in", "of", "on", "to", "for",
    "i", "me", "my", "we", "you", "your", "it", "this", "that",
    "be", "am", "are", "was", "were", "with", "at", "by", "as",
})

# Punctuation marks we count as separate stylometric features. The
# choice of `—` vs `-`, `'` vs `’`, ellipsis vs three dots is unusually
# stable per author.
_PUNCT_CATEGORIES = {
    "em_dash": "—",
    "en_dash": "–",
    "hyphen": "-",
    "ellipsis": "…",
    "ellipsis_dots": "...",
    "exclaim": "!",
    "question": "?",
    "ampersand": "&",
    "at_sign": "@",
    "smart_apos": "’",
    "smart_dquote": "“",
    "curly_close_dquote": "”",
    "asterisk": "*",
    "underscore": "_",
    "pipe": "|",
    "slash": "/",
    "backslash": "\\",
}


def _is_emoji(ch: str) -> bool:
    """Cheap emoji detector via Unicode category + named ranges.

    Emojis sit in a handful of high Unicode planes; we don't need a
    perfect set, just enough for frequency counting.
    """
    if not ch:
        return False
    cp = ord(ch)
    return (
        0x1F300 <= cp <= 0x1FAFF  # symbols & pictographs
        or 0x1F600 <= cp <= 0x1F64F  # emoticons
        or 0x1F900 <= cp <= 0x1F9FF  # supplemental symbols
        or 0x2600 <= cp <= 0x26FF    # misc symbols
        or 0x2700 <= cp <= 0x27BF    # dingbats
        or 0x1F1E6 <= cp <= 0x1F1FF  # regional indicators (flags)
    )


def _capitalization_class(text: str) -> str:
    """Return one of: 'allcaps', 'allower', 'sentence', 'mixed'.

    Strong signal — "lowercase only" bios cluster very tightly with
    other lowercase-only bios from the same author.
    """
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return "mixed"
    n_upper = sum(1 for c in letters if c.isupper())
    n_lower = sum(1 for c in letters if c.islower())
    if n_lower and not n_upper:
        return "allower"
    if n_upper and not n_lower:
        return "allcaps"
    # Sentence case: starts uppercase, mostly lowercase after.
    if n_upper / max(1, len(letters)) < 0.2:
        return "sentence"
    return "mixed"


def feature_vector(text: str) -> dict[str, float]:
    """Extract a stylometric feature vector from a bio.

    Returns a dict of feature_name → float. Empty bio → empty dict
    (caller should treat as "no signal"). Numeric features are
    normalised to per-100-character rates so length doesn't dominate
    the similarity score.
    """
    text = (text or "").strip()
    if not text:
        return {}
    out: dict[str, float] = {}
    n_chars = max(1, len(text))

    # 1. Punctuation rates (per 100 chars).
    for name, ch in _PUNCT_CATEGORIES.items():
        count = text.count(ch)
        if count:
            out[f"p_{name}"] = (count / n_chars) * 100

    # 2. Emoji rate + count of distinct emoji.
    emojis = [c for c in text if _is_emoji(c)]
    if emojis:
        out["emoji_rate"] = (len(emojis) / n_chars) * 100
        out["emoji_unique"] = len(set(emojis))

    # 3. Capitalization class as a one-hot.
    cap_class = _capitalization_class(text)
    for cls in ("allcaps", "allower", "sentence", "mixed"):
        out[f"cap_{cls}"] = 1.0 if cls == cap_class else 0.0

    # 4. Sentence-length distribution: mean + stddev, normalised to a
    #    0–1 range so the feature doesn't dominate the cosine score with
    #    its raw character magnitude. Buckets reflect "tweet-short",
    #    "one-line", "paragraph" — the bands that actually differ
    #    stylistically between authors.
    sentence_chunks = [s.strip() for s in re.split(r"[.!?]+\s+", text) if s.strip()]
    if sentence_chunks:
        lens = [len(s) for s in sentence_chunks]
        mean = sum(lens) / len(lens)
        # Three banded one-hots — tiny / medium / long sentences. Stable
        # under length normalisation, captures the meaningful distinction.
        if mean < 30:
            out["sent_short"] = 1.0
        elif mean < 80:
            out["sent_med"] = 1.0
        else:
            out["sent_long"] = 1.0
        if len(lens) > 1:
            var = sum((l - mean) ** 2 for l in lens) / len(lens)
            # Stddev normalised by mean → "how varied are sentence
            # lengths relative to the typical one?" — also bounded.
            cv = math.sqrt(var) / max(1, mean)
            out["sent_cv"] = min(1.5, cv)

    # 5. Lexical signature — frequency of meaningful words after
    #    stripping the stop-list. Top-15 most frequent only, to keep
    #    the vector dimensionality bounded.
    tokens = [
        unicodedata.normalize("NFKC", w).lower()
        for w in re.findall(r"[a-zA-Z]{3,}", text)
    ]
    tokens = [t for t in tokens if t not in _STOPWORDS]
    if tokens:
        counts: dict[str, int] = {}
        for t in tokens:
            counts[t] = counts.get(t, 0) + 1
        for word, c in sorted(counts.items(), key=lambda kv: -kv[1])[:15]:
            out[f"w_{word}"] = c / len(tokens)
        out["unique_word_rate"] = len(set(tokens)) / len(tokens)

    # 6. Whitespace habits — single vs double-space after periods,
    #    presence of newlines, trailing whitespace. Subtle but stable.
    if "  " in text:
        out["double_space"] = 1.0
    if "\n" in text:
        out["has_newline"] = 1.0

    return out


def _cosine(a: dict[str, float], b: dict[str, float]) -> float:
    """Cosine similarity between two sparse feature dicts.

    Returns 0 when either side is empty (insufficient signal). Range
    [0, 1] under the assumption all feature values are non-negative —
    which they are in `feature_vector`.
    """
    if not a or not b:
        return 0.0
    keys = set(a) | set(b)
    dot = sum(a.get(k, 0) * b.get(k, 0) for k in keys)
    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def style_score(text_a: str, text_b: str) -> float:
    """Cosine similarity of two bios' stylometric feature vectors.

    Convenience wrapper: `style_score(profile_a.bio, profile_b.bio)`.
    """
    return _cosine(feature_vector(text_a), feature_vector(text_b))


# Disambiguation weight to add for a "same style" edge in the identity
# clustering graph. Conservative — stylometry on bios is noisy because
# bios are short, so we only nudge clustering, never gate it.
STYLE_MATCH_WEIGHT = 2

# Similarity threshold above which two bios get treated as "same style".
# Tuned conservatively — most random bio pairs sit at 0.1–0.4; a real
# same-author match typically hits 0.55+. We require 0.7 to bias toward
# precision over recall (the rest of disambiguation already provides
# the recall side).
STYLE_MATCH_THRESHOLD = 0.7


def is_style_match(text_a: str, text_b: str) -> bool:
    """True iff style similarity exceeds the match threshold AND both
    bios are long enough to carry meaningful signal (≥ 40 chars each).
    """
    a = (text_a or "").strip()
    b = (text_b or "").strip()
    if len(a) < 40 or len(b) < 40:
        return False
    return style_score(a, b) >= STYLE_MATCH_THRESHOLD
