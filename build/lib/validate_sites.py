#!/usr/bin/env python3
"""Schema + sanity checker for sites.json.

Phantom's accuracy depends entirely on site definitions being well-formed.
A malformed entry can silently degrade a site to UNKNOWN, or worse, leak
false positives — for example a `method=message` entry with no
`presence_text` falls through to "trust the status" and accepts any 200.

Run before committing changes to sites.json:

    python3 validate_sites.py
    python3 validate_sites.py --strict   # exit 1 on warnings too

Exit codes:
  0  — clean
  1  — at least one error (or warning under --strict)
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

VALID_CATEGORIES = {"dev", "social", "gaming", "media", "forum", "other"}
VALID_METHODS = {"status", "message"}
VALID_HTTP_METHODS = {"GET", "POST"}
VALID_PROTECTION_FLAGS = {"tls_fingerprint", "js_challenge"}
VALID_CONTENT_TYPES = {
    "photo", "text", "code", "audio", "video", "links", "mixed",
}

REQUIRED_FIELDS = ("name", "category", "url", "method", "reliability")

# ISO 3166-1 alpha-2 — we don't pin to a closed set since new TLDs and
# platforms appear, but we do enforce the shape. "global" is a special
# value meaning "no geographic affinity".
_ISO_COUNTRY_RE = re.compile(r"^[a-z]{2}$")
_ISO_LANGUAGE_RE = re.compile(r"^[a-z]{2}(-[a-z]{2})?$")


def validate(sites_path: Path) -> tuple[list[str], list[str]]:
    """Return (errors, warnings) — lists of human-readable strings."""
    errors: list[str] = []
    warnings: list[str] = []

    try:
        raw = json.loads(sites_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return [f"JSON parse error: {e}"], []
    except OSError as e:
        return [f"cannot read {sites_path}: {e}"], []

    if not isinstance(raw, list):
        return [f"top-level must be a JSON array, got {type(raw).__name__}"], []

    seen_names: dict[str, int] = {}

    for i, entry in enumerate(raw):
        prefix = f"[{i}]"
        if not isinstance(entry, dict):
            errors.append(f"{prefix} not an object")
            continue
        name = entry.get("name") or f"<index {i}>"
        prefix = f"[{i} {name!r}]"

        # Required fields
        for f in REQUIRED_FIELDS:
            if f not in entry:
                errors.append(f"{prefix} missing required field: {f}")

        # Duplicate-name check
        nm = entry.get("name")
        if isinstance(nm, str):
            if nm in seen_names:
                errors.append(f"{prefix} duplicate name (first seen at index {seen_names[nm]})")
            else:
                seen_names[nm] = i

        # Category
        cat = entry.get("category")
        if cat is not None and cat not in VALID_CATEGORIES:
            errors.append(
                f"{prefix} bad category {cat!r} — must be one of "
                f"{sorted(VALID_CATEGORIES)}"
            )

        # URL or request_body — at least one must carry {username}. POST
        # endpoints like AniList's GraphQL legitimately put the username
        # in the body and the URL is a fixed endpoint.
        url = entry.get("url")
        body = entry.get("request_body")
        url_has = isinstance(url, str) and "{username}" in url
        body_has = isinstance(body, str) and "{username}" in body
        if isinstance(url, str) and not url_has and not body_has:
            errors.append(
                f"{prefix} no {{username}} placeholder in url or request_body"
            )

        # Method
        method = entry.get("method")
        if method is not None and method not in VALID_METHODS:
            errors.append(
                f"{prefix} bad method {method!r} — must be one of "
                f"{sorted(VALID_METHODS)}"
            )

        # Reliability
        rel = entry.get("reliability")
        if rel is not None:
            try:
                ri = int(rel)
                if not (0 <= ri <= 100):
                    errors.append(f"{prefix} reliability out of range [0,100]: {ri}")
            except (TypeError, ValueError):
                errors.append(f"{prefix} reliability not an integer: {rel!r}")

        # Status lists
        for field_name in ("valid_status", "invalid_status"):
            v = entry.get(field_name)
            if v is None:
                continue
            if not isinstance(v, list):
                errors.append(f"{prefix} {field_name} must be a list, got {type(v).__name__}")
                continue
            for j, item in enumerate(v):
                if not isinstance(item, int):
                    errors.append(
                        f"{prefix} {field_name}[{j}] not an integer: {item!r}"
                    )
                elif not (100 <= item < 1000):
                    # Some platforms ship non-standard 3-digit codes
                    # (LinkedIn's 999 = "auth required" is the famous one,
                    # Cloudflare uses 521/526 too). We warn only when the
                    # number is outside the 3-digit range entirely.
                    warnings.append(
                        f"{prefix} {field_name}[{j}] {item} is not a typical "
                        f"HTTP code"
                    )

        # Presence / absence
        for field_name in ("presence_text", "absence_text"):
            v = entry.get(field_name)
            if v is None:
                continue
            if not isinstance(v, list):
                errors.append(f"{prefix} {field_name} must be a list, got {type(v).__name__}")
                continue
            for j, item in enumerate(v):
                if not isinstance(item, str):
                    errors.append(
                        f"{prefix} {field_name}[{j}] not a string: {item!r}"
                    )

        # Method-specific requirements
        if method == "status":
            if not entry.get("valid_status") and not entry.get("invalid_status"):
                errors.append(
                    f"{prefix} method=status requires at least one of "
                    f"valid_status / invalid_status"
                )
        elif method == "message":
            if not entry.get("presence_text"):
                # Allowed but suspicious — falls through to "trust the status",
                # which can produce false positives on generic 200 pages.
                warnings.append(
                    f"{prefix} method=message without presence_text falls "
                    f"through to trusting the status code — easy false positive"
                )

        # Protection flags
        prot = entry.get("protection")
        if prot is not None:
            if not isinstance(prot, list):
                errors.append(f"{prefix} protection must be a list, got {type(prot).__name__}")
            else:
                for p in prot:
                    if p not in VALID_PROTECTION_FLAGS:
                        warnings.append(
                            f"{prefix} unknown protection flag {p!r} — "
                            f"recognised: {sorted(VALID_PROTECTION_FLAGS)}"
                        )

        # Headers
        headers = entry.get("headers")
        if headers is not None and not isinstance(headers, dict):
            errors.append(f"{prefix} headers must be an object, got {type(headers).__name__}")

        # Request method / body
        rm = entry.get("request_method")
        if rm is not None and rm.upper() not in VALID_HTTP_METHODS:
            errors.append(
                f"{prefix} request_method {rm!r} — must be GET or POST"
            )
        rb = entry.get("request_body")
        if rb is not None and not isinstance(rb, str):
            errors.append(
                f"{prefix} request_body must be a string template, got {type(rb).__name__}"
            )
        if rb and (not rm or rm.upper() != "POST"):
            warnings.append(
                f"{prefix} request_body set but request_method isn't POST"
            )

        # profile_url sanity
        purl = entry.get("profile_url")
        if purl is not None:
            if not isinstance(purl, str):
                errors.append(f"{prefix} profile_url must be a string")
            elif "{username}" not in purl:
                errors.append(f"{prefix} profile_url missing {{username}} placeholder")

        # country — ISO 3166-1 alpha-2 lowercase, or "global"
        country = entry.get("country")
        if country is not None:
            if not isinstance(country, str):
                errors.append(f"{prefix} country must be a string")
            elif country != "global" and not _ISO_COUNTRY_RE.match(country):
                errors.append(
                    f"{prefix} country {country!r} — expected ISO 3166-1 "
                    f"alpha-2 lowercase (e.g. 'us', 'fr') or 'global'"
                )

        # language — ISO 639-1 (optionally with a region tag), or "global"
        language = entry.get("language")
        if language is not None:
            if not isinstance(language, str):
                errors.append(f"{prefix} language must be a string")
            elif language != "global" and not _ISO_LANGUAGE_RE.match(language):
                errors.append(
                    f"{prefix} language {language!r} — expected ISO 639-1 "
                    f"lowercase (e.g. 'en', 'fr', 'zh-cn') or 'global'"
                )

        # content_type — closed set
        ct = entry.get("content_type")
        if ct is not None:
            if ct not in VALID_CONTENT_TYPES:
                errors.append(
                    f"{prefix} content_type {ct!r} — must be one of "
                    f"{sorted(VALID_CONTENT_TYPES)}"
                )

        # disabled — boolean flag
        dis = entry.get("disabled")
        if dis is not None and not isinstance(dis, bool):
            errors.append(f"{prefix} disabled must be a boolean")

    return errors, warnings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate Phantom's sites.json against the expected schema."
    )
    parser.add_argument(
        "path",
        nargs="?",
        default=str(Path(__file__).with_name("sites.json")),
        help="path to sites.json (default: alongside this script)",
    )
    parser.add_argument(
        "--strict", action="store_true",
        help="exit non-zero on warnings too (default: only errors fail)",
    )
    args = parser.parse_args(argv)

    sites_path = Path(args.path)
    if not sites_path.is_file():
        print(f"error: not a file: {sites_path}", file=sys.stderr)
        return 2

    errors, warnings = validate(sites_path)

    for e in errors:
        print(f"ERROR: {e}", file=sys.stderr)
    for w in warnings:
        print(f"warn:  {w}", file=sys.stderr)

    n_sites = len(json.loads(sites_path.read_text(encoding="utf-8")))
    print(
        f"\n{n_sites} sites · {len(errors)} error(s) · {len(warnings)} warning(s)",
        file=sys.stderr,
    )

    if errors:
        return 1
    if warnings and args.strict:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
