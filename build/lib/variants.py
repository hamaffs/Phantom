"""Username variation engine.

`generate(raw)` returns a deduplicated, validated list of plausible variants.

Two modes, picked from the input:

- Single token (no whitespace): produce separator / smart-split, number-suffix,
  prefix, and suffix variants.
- Two-or-more tokens (whitespace): treat as first + last name, generate the
  common first/last permutations.

Every variant is filtered through Phantom's username regex
(`^[A-Za-z0-9_.\\-]{1,64}$`), so callers never have to re-validate.
"""

from __future__ import annotations

import re

USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")

NUMBER_SUFFIXES = ("1", "2", "99", "123")
PREFIXES = ("the", "its", "real", "official")
SUFFIXES = ("_", "official", "_official")
SEPARATORS = ("", ".", "_", "-")

# Leetspeak substitutions. Only the safest mappings: characters that are
# universally recognised as visual substitutes for letters. Each substitution
# is applied to *every* occurrence (so "jose" → "j0se" not "j0se" and "jos3"
# variants — that combinatorial expansion produces too much noise for the
# extra coverage). Most common substitution forms first.
_LEET_MAP = {
    "o": "0",
    "i": "1",
    "e": "3",
    "a": "4",
    "s": "5",
    "t": "7",
}
# Cap on how many leet variants we add per input. Even with the bounded
# substitution above, applying all six to a long input creates a lot of
# rarely-used forms — most leetspeak handles use one or two substitutions.
_LEET_MAX_VARIANTS = 8

_EXISTING_SEP_RE = re.compile(r"[._\-]")
_CAMEL_SPLIT_RE = re.compile(r"(?<=[a-z])(?=[A-Z])")
_DIGIT_BOUNDARY_RE = re.compile(r"(?<=[A-Za-z])(?=\d)|(?<=\d)(?=[A-Za-z])")

# Maximum input length for blind position-insertion. Above this, the variant
# count would explode (a 19-char input × 3 separators × 18 positions = 54
# mostly-nonsense variants). Short condensed handles like `<word1word2>` ↔
# `<word1>.<word2>` benefit; long ones don't.
_BLIND_SPLIT_MAX_LEN = 14
_BLIND_SPLIT_MIN_PIECE = 2  # avoid 1-char prefixes/suffixes ("h.amaffs", "hamaff.s")

# Name-mode floor: short permutations like "kmalay" / "malayk" collide with
# huge numbers of unrelated accounts on every platform, drowning the signal.
# Below 8 chars there's not enough entropy for a name-derived handle to
# uniquely identify the target — drop them.
_NAME_MIN_LEN = 8


def _split_parts(token: str) -> list[str]:
    """Detect word boundaries in `token`.

    Priority:
    1. If the token already contains a separator (`._-`), split on it.
       Lets us re-skin "word1_word2" → ["word1", "word2"] and try other separators.
    2. Otherwise camelCase boundaries ("JohnDoe" → ["John", "Doe"]).
    3. Then digit/letter boundaries inside each piece ("john2024" → ["john", "2024"]).
    """
    if _EXISTING_SEP_RE.search(token):
        return [p for p in re.split(r"[._\-]", token) if p]
    pieces = _CAMEL_SPLIT_RE.split(token)
    parts: list[str] = []
    for p in pieces:
        parts.extend(x for x in _DIGIT_BOUNDARY_RE.split(p) if x)
    return parts


def _dedup(seq):
    """Order-preserving dedup."""
    seen: set = set()
    out: list[str] = []
    for x in seq:
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _valid(s: str) -> bool:
    return bool(USERNAME_PATTERN.match(s))


def _leet_variants(token: str) -> list[str]:
    """Single-substitution leetspeak forms.

    Each output is the input with exactly one letter→digit substitution
    applied to all occurrences of that letter. Skips substitutions that
    produce a string identical to the input (no eligible letter). Capped
    at `_LEET_MAX_VARIANTS` total to keep the run sensible.
    """
    base = token.lower()
    out: list[str] = []
    for letter, digit in _LEET_MAP.items():
        if letter not in base:
            continue
        candidate = base.replace(letter, digit)
        if candidate != base:
            out.append(candidate)
        if len(out) >= _LEET_MAX_VARIANTS:
            break
    return out


def _blind_position_splits(token: str) -> list[str]:
    """For tokens with no obvious word boundary (no separator, no camelCase,
    no digit run), try inserting each separator at each internal position.

    This is the only way to reach split forms like `word1.word2` from a
    condensed input — there's no way to *infer* the split point, so we
    enumerate all of them.
    """
    if len(token) > _BLIND_SPLIT_MAX_LEN:
        return []
    if _EXISTING_SEP_RE.search(token):
        return []
    if _CAMEL_SPLIT_RE.search(token):
        return []
    if _DIGIT_BOUNDARY_RE.search(token):
        return []
    base = token.lower()
    out: list[str] = []
    lo, hi = _BLIND_SPLIT_MIN_PIECE, len(base) - _BLIND_SPLIT_MIN_PIECE + 1
    for i in range(lo, hi):
        for sep in (".", "_", "-"):
            out.append(f"{base[:i]}{sep}{base[i:]}")
    return out


def _variants_single(token: str) -> list[str]:
    base = token.lower()
    out: list[str] = [token]
    if base != token:
        out.append(base)

    parts = [p.lower() for p in _split_parts(token)]
    if len(parts) > 1:
        for sep in SEPARATORS:
            out.append(sep.join(parts))

    out.extend(_blind_position_splits(token))
    out.extend(_leet_variants(base))

    for n in NUMBER_SUFFIXES:
        out.append(f"{base}{n}")

    for p in PREFIXES:
        out.append(f"{p}{base}")
        out.append(f"{p}_{base}")

    for s in SUFFIXES:
        out.append(f"{base}{s}")

    return out


def _variants_name(first: str, last: str) -> list[str]:
    """Generate plausible username variants from a first + last name.

    Skipped intentionally:
    - `firstname + last_letter` (e.g. "johns", "marym") — first names are
      common, so adding one trailing letter produces strings that collide
      with countless unrelated accounts on every platform. Net effect was a
      flood of unrelated FOUND results that drowned the real signal.

    Kept: `first_letter + lastname` and `lastname + first_letter`. These
    anchor on the lastname, which is usually rare enough to be specific.
    """
    f, l = first.lower(), last.lower()
    if not f or not l:
        single = f or l
        return [single] if len(single) >= _NAME_MIN_LEN else []
    candidates = [
        f"{f}{l}",
        f"{f}.{l}",
        f"{f}_{l}",
        f"{f}-{l}",
        f"{f[0]}{l}",
        f"{l}{f}",
        f"{l}.{f}",
        f"{l}_{f}",
        f"{l}{f[0]}",
    ]
    return [v for v in candidates if len(v) >= _NAME_MIN_LEN]


_EMAIL_RE = re.compile(r"^([A-Za-z0-9._\-+]+)@([A-Za-z0-9.\-]+\.[A-Za-z]{2,})$")


def _strip_email(raw: str) -> str:
    """If raw looks like a complete email, return the local-part (before @).

    Strips any `+tag` suffix common in plus-addressing — `alice+gh@x.com`
    becomes `alice`, which is the handle the user almost certainly carries
    on platforms.
    """
    m = _EMAIL_RE.match(raw)
    if not m:
        return raw
    local = m.group(1)
    plus = local.find("+")
    if plus > 0:
        local = local[:plus]
    return local


def generate(raw: str) -> list[str]:
    """Return a deduplicated, validated list of variants for `raw`.

    Whitespace in `raw` switches to first/last-name mode. Tokens beyond the
    second are ignored (parts[0] + parts[-1]) so middle names don't explode
    the variant set.

    Email inputs (`foo@bar.com`) collapse to the local-part before variant
    generation — that's the handle the user is statistically most likely
    to reuse on social platforms.
    """
    raw = raw.strip()
    if not raw:
        return []
    raw = _strip_email(raw)
    parts = raw.split()
    if len(parts) >= 2:
        candidates = _variants_name(parts[0], parts[-1])
    else:
        candidates = _variants_single(raw)
    return [v for v in _dedup(candidates) if _valid(v)]


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("usage: variants.py <username | first last>", file=sys.stderr)
        raise SystemExit(2)
    for v in generate(" ".join(sys.argv[1:])):
        print(v)
