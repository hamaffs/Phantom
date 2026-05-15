"""Same-site profile dedup + the flatten helper that turns a list of
(variant, [results]) into (found, unknown, missing_count).
"""
from __future__ import annotations

from typing import Optional

from models import CheckResult


# Sites whose URL semantics make multiple URL patterns reliably resolve
# to one profile, so a same-site display_name match alone is safe to
# dedupe even when the canonical URL or photo couldn't be extracted.
# Facebook is the canonical example: `/john.smith`, `/john-smith`, and
# `/johnsmith` all point at the same person, never different real users.
_NAME_ONLY_DEDUP_SITES = frozenset({"Facebook"})
def _profile_dedup_key_parts(profile: dict, url: str, final_url: Optional[str], site: Optional[str] = None) -> Optional[tuple]:
    """Return a key identifying *which profile* this result is for, or
    None when we can't tell. Two FOUND results with the same key on the
    same site are the same person reached via different URL aliases —
    a Facebook account exposed at `/john.smith`, `/john-smith`, and
    `/johnsmith` returns the same profile body for all three.

    Priority:
      1. Platform-shipped numeric profile ID (Facebook's
         `al:ios:url` / `al:android:url`) — bullet-proof.
      2. Canonical URL from `og:url` (`profile.canonical_url`).
      3. The post-redirect final URL — works for sites that 30x to the
         normalised path.
      4. (display_name, photo) — same person if both match exactly.
      5. display_name alone — only on a small set of sites whose URL
         aliasing makes this safe (Facebook today). Falls back here
         when the platform served no canonical URL and we couldn't
         extract a photo.
    """
    p = profile or {}
    fb_id = p.get("fb_profile_id")
    if fb_id:
        return ("fb_id", str(fb_id))
    canonical = p.get("canonical_url")
    if canonical:
        return ("canonical", canonical.lower())
    final = (final_url or "").lower().rstrip("/").split("?", 1)[0]
    own = (url or "").lower().rstrip("/").split("?", 1)[0]
    if final and final != own:
        return ("final", final)
    name = (p.get("display_name") or "").strip().lower()
    photo = (p.get("photo") or "").strip()
    if name and photo:
        return ("name+photo", name, photo)
    if name and site in _NAME_ONLY_DEDUP_SITES:
        return ("name", name)
    return None


def _dedupe_same_site_profiles(found: list[CheckResult]) -> list[CheckResult]:
    """Merge FOUND results that point at the same profile on the same
    site — see `_profile_dedup_key_parts` for the matching rule.

    Kept entry is the one with the richest profile dict; merged variants
    get stashed on `profile["aliases"]` so the report can show every
    handle pattern that resolved to this profile.
    """
    by_key: dict[tuple, list[CheckResult]] = {}
    untouched: list[CheckResult] = []
    for r in found:
        key = _profile_dedup_key_parts(r.profile, r.url, r.final_url, r.site)
        if key is None:
            untouched.append(r)
            continue
        by_key.setdefault((r.site, *key), []).append(r)

    merged: list[CheckResult] = []
    for group in by_key.values():
        if len(group) == 1:
            merged.append(group[0])
            continue
        primary = max(
            group,
            key=lambda r: (len(r.profile or {}), -len(r.url or "")),
        )
        others = [g for g in group if g is not primary]
        primary.profile = {**(primary.profile or {})}
        primary.profile["aliases"] = [
            {"variant": o.variant, "url": o.url} for o in others
        ]
        merged.append(primary)
    return untouched + merged


def _dedupe_same_site_dicts(found_dicts: list[dict]) -> list[dict]:
    """Dict-level twin of `_dedupe_same_site_profiles`. Used before
    identity correlation so the photo-match cluster doesn't inflate
    when a single profile is reached via several URL patterns."""
    by_key: dict[tuple, list[dict]] = {}
    untouched: list[dict] = []
    for d in found_dicts:
        key = _profile_dedup_key_parts(
            d.get("profile") or {}, d.get("url") or "", d.get("final_url"),
            d.get("site"),
        )
        if key is None:
            untouched.append(d)
            continue
        by_key.setdefault((d.get("site"), *key), []).append(d)

    merged: list[dict] = []
    for group in by_key.values():
        if len(group) == 1:
            merged.append(group[0])
            continue
        primary = max(
            group,
            key=lambda d: (len(d.get("profile") or {}), -len(d.get("url") or "")),
        )
        others = [g for g in group if g is not primary]
        primary["profile"] = {**(primary.get("profile") or {})}
        primary["profile"]["aliases"] = [
            {"variant": o.get("variant"), "url": o.get("url")} for o in others
        ]
        merged.append(primary)
    return untouched + merged
def _flatten(grouped: list[tuple[str, list[CheckResult]]]):
    found, unknown = [], []
    missing_count = 0
    for _, rs in grouped:
        for r in rs:
            if r.exists is True:
                found.append(r)
            elif r.exists is False:
                missing_count += 1
            else:
                unknown.append(r)
    found = _dedupe_same_site_profiles(found)
    sort_key = lambda r: (-r.reliability, r.site.lower(), r.variant or "")
    found.sort(key=sort_key)
    unknown.sort(key=sort_key)
    return found, unknown, missing_count
