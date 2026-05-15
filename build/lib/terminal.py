"""Terminal rendering — ANSI helpers, three-tier output, clustered
output, and the per-row formatter. No I/O beyond stdout/stderr.
"""
from __future__ import annotations

import re
import sys
from typing import Optional

from confidence import TIER_IMPOSTOR, TIER_LIKELY, TIER_VERIFIED
from disambiguation import LABEL_LOW, LABEL_PRIMARY, LABEL_SECONDARY
from dedupe import _flatten
from models import CheckResult


def _format_count(n) -> str:
    """Human-friendly counts: 12345 -> '12.3K'."""
    try:
        n = int(n)
    except (TypeError, ValueError):
        return str(n)
    if abs(n) >= 1_000_000_000:
        return f"{n/1_000_000_000:.1f}B".rstrip("0").rstrip(".")
    if abs(n) >= 1_000_000:
        return f"{n/1_000_000:.1f}M".rstrip("0").rstrip(".")
    if abs(n) >= 1_000:
        return f"{n/1_000:.1f}K".rstrip("0").rstrip(".")
    return str(n)


ANSI = {
    "green": "\033[32m",
    "red":   "\033[31m",
    "yellow":"\033[33m",
    "dim":   "\033[2m",
    "bold":  "\033[1m",
    "reset": "\033[0m",
}

def _c(color: bool, key: str) -> str:
    return ANSI[key] if color else ""
def _format_row(r: CheckResult, color: bool, show_variant: bool) -> str:
    """One line in the FOUND or UNKNOWN section.

    The variant tag is only printed when more than one variant ran — with
    `--exact`, it's just noise.
    """
    site = f"{_c(color,'bold')}{r.site:<14}{_c(color,'reset')}"
    # Show the canonical URL (the one we requested) — that's what reliably
    # opens the profile when clicked. Some sites (Instagram) drop the www
    # subdomain on redirect, and the redirected form trips their bot
    # detection when opened cold from a browser.
    target = r.url
    url_part = f"{_c(color,'dim')}{target}{_c(color,'reset')}"
    note_parts = []
    if r.status is not None:
        note_parts.append(f"http={r.status}")
    if r.error and r.error != r.reason:
        note_parts.append(r.error)
    if r.reason and r.reason != f"{r.status}":
        note_parts.append(r.reason)
    note = f" {_c(color,'dim')}({', '.join(note_parts)}){_c(color,'reset')}" if note_parts else ""
    score_tag = (
        f"  {_c(color,'dim')}(score {r.score}){_c(color,'reset')}"
        if r.score is not None else ""
    )
    tag = ""
    if show_variant and r.variant:
        tag = f"  {_c(color,'yellow')}[{r.variant}]{_c(color,'reset')}"
    return f"  {site} {url_part}{note}{score_tag}{tag}"

def _print_identity_summary(overall, clusters, color: bool) -> None:
    """Print the overall identity summary + any photo-matched groups.

    The overall summary always prints when there's at least one FOUND;
    that's the bit that surfaces a region for users whose accounts
    don't share a photo. Photo-matched clusters print below as a
    secondary "definitely the same person on these sites" view.
    """
    g, b, x, dim, accent = (
        _c(color, "green"), _c(color, "bold"), _c(color, "reset"),
        _c(color, "dim"), _c(color, "yellow"),
    )

    if overall and len(overall.member_indexes) >= 1:
        print(f"\n{b}[ IDENTITY ]{x}  {dim}(aggregated from {len(overall.member_indexes)} account(s)){x}")
        if overall.display_name:
            print(f"  {b}Name{x}    {overall.display_name}")
        sites = ", ".join(overall.sites)
        print(f"  {b}Sites{x}   {sites}")
        loc_bits: list[str] = []
        if overall.locations:
            loc_bits.append(", ".join(overall.locations))
        if (
            overall.geo_hint and overall.geo_hint.region
            and overall.geo_hint.region not in (overall.locations or [])
        ):
            loc_bits.append(f"likely {overall.geo_hint.region} ({overall.geo_hint.confidence})")
        if loc_bits:
            print(f"  {b}Region{x}  " + " · ".join(loc_bits))
        stat_bits = []
        if overall.total_followers is not None:
            stat_bits.append(f"{_format_count(overall.total_followers)} followers")
        if overall.total_following is not None:
            stat_bits.append(f"{_format_count(overall.total_following)} following")
        if overall.total_posts is not None:
            stat_bits.append(f"{_format_count(overall.total_posts)} posts")
        if stat_bits:
            print(f"  {b}Stats{x}   " + " · ".join(stat_bits))
        if overall.verified_on:
            print(f"  {b}✓{x}       Verified on " + ", ".join(overall.verified_on))

    multi = [c for c in (clusters or []) if len(c.member_indexes) > 1]
    if multi:
        print(f"\n{b}[ PHOTO MATCH ]{x} {b}{len(multi)}{x}  "
              f"{dim}(same profile photo across multiple sites){x}")
        for c in multi:
            name = c.display_name or "(no name)"
            sites = ", ".join(c.sites)
            conf_part = f"{accent}({c.confidence:.2f}){x}"
            print(f"  {b}{name}{x} {conf_part} → {sites}")
def _format_site_url(r: CheckResult) -> str:
    """Compact URL for display: strip protocol, trailing slash."""
    return re.sub(r'^https?://', '', r.url).rstrip('/')
def print_clustered(
    found: list[CheckResult],
    dis_clusters: list,
    elapsed: float,
    color: bool,
    found_only: bool,
    show_all: bool,
    unknown_count: int,
    missing_count: int,
    n_variants: int,
) -> None:
    """Identity-grouped terminal output (default mode when disambiguation runs)."""
    g, y, b, x, dim = (
        _c(color, "green"), _c(color, "yellow"),
        _c(color, "bold"), _c(color, "reset"), _c(color, "dim"),
    )
    r_ = _c(color, "red")
    accent = _c(color, "yellow")

    # Group clusters by label
    primary   = [c for c in dis_clusters if c.label == LABEL_PRIMARY]
    secondary = [c for c in dis_clusters if c.label == LABEL_SECONDARY]
    low       = [c for c in dis_clusters if c.label == LABEL_LOW]

    def _cluster_rows(cluster, limit: int = 5):
        """Print member rows for a cluster, capped at *limit*."""
        show_variant = len(set(r.variant for r in found)) > 1
        members = sorted(
            [found[i] for i in cluster.member_indices if i < len(found)],
            key=lambda r: -(r.score or 0),
        )
        for r in members[:limit]:
            ver = f", {_c(color,'bold')}verified{x}" if (r.profile or {}).get('verified') else ""
            score_txt = f"(score {r.score}{ver})" if r.score is not None else ""
            tag = f"  {accent}[{r.variant}]{x}" if show_variant and r.variant else ""
            print(f"  {dim}▸{x} {_format_site_url(r)}  {dim}{score_txt}{x}{tag}")
        extra = len(members) - limit
        if extra > 0:
            print(f"  {dim}… and {extra} more — see --export for full report{x}")

    # Primary cluster(s)
    for c in primary:
        name_part = f" {b}— {c.display_name}{x}" if c.display_name else ""
        print(f"\n{b}{g}[ PRIMARY IDENTITY ]{x}{name_part}")
        meta_parts = [f"{c.size} account{'s' if c.size != 1 else ''}"]
        if c.location:
            meta_parts.append(f"region: {c.location}")
        meta_parts.append(f"max confidence: {c.max_score}")
        print(f"  {dim}" + " · ".join(meta_parts) + x)
        if c.verified_on:
            print(f"  {b}✓{x} Verified on " + ", ".join(c.verified_on))
        sites = sorted({found[i].site for i in c.member_indices if i < len(found)})
        print(f"  {dim}Sites: {', '.join(sites)}{x}")
        _cluster_rows(c)

    # Secondary clusters
    for c in secondary:
        name_part = f" {b}— {c.display_name}{x}" if c.display_name else ""
        cnum = f" #{c.cluster_id}" if c.cluster_id > 1 else ""
        print(f"\n{b}{y}[ SECONDARY CLUSTER{cnum} ]{x}{name_part}")
        meta_parts = [f"{c.size} account{'s' if c.size != 1 else ''}"]
        meta_parts.append(f"max confidence: {c.max_score}")
        print(f"  {dim}" + " · ".join(meta_parts) + x)
        if c.verified_on:
            print(f"  {b}✓{x} Verified on " + ", ".join(c.verified_on))
        _cluster_rows(c, limit=3)

    # Low-confidence / unrelated
    total_low = sum(c.size for c in low)
    if total_low:
        if show_all:
            for c in low:
                name_part = f" {b}— {c.display_name}{x}" if c.display_name else ""
                print(f"\n{b}[ UNRELATED ]{x}{name_part}")
                _cluster_rows(c, limit=3)
        else:
            print(f"\n{b}{y}[ UNRELATED MATCHES ]{x}{b} {total_low}{x}  "
                  f"{dim}(use --show-all to display){x}")

    if not primary and not secondary and not low:
        print(f"\n{b}{g}[ FOUND ]{x}{b} 0{x}")

    if not found_only:
        print(f"\n{b}{y}[   ?   ]{x}{b} {unknown_count}{x}  "
              f"{dim}(use --export to see details){x}")
        print(f"{b}{r_}[MISSING]{x}{b} {missing_count}{x}")

    sys.stdout.flush()
    suffix = f"across {n_variants} variant{'s' if n_variants != 1 else ''}"
    total = len(found) + unknown_count + missing_count
    print(f"\n{dim}{total} checks {suffix} in {elapsed:.1f}s{x}", file=sys.stderr)
def print_compact(
    grouped: list[tuple[str, list[CheckResult]]],
    elapsed: float,
    color: bool,
    found_only: bool,
    show_all: bool = False,
    dis_clusters: Optional[list] = None,
) -> None:
    """Clustered or three-tier FOUND output depending on whether disambiguation ran.

    When *dis_clusters* is provided, uses the identity-grouped format.
    When None (--no-cluster or no results), falls back to the three-tier display.
    """
    found, unknown, missing_count = _flatten(grouped)
    n_variants = len(grouped)

    # --- Clustered mode ---
    if dis_clusters is not None:
        print_clustered(
            found, dis_clusters, elapsed, color, found_only, show_all,
            len(unknown), missing_count, n_variants,
        )
        return

    # --- Legacy three-tier mode (--no-cluster) ---
    show_variant = n_variants > 1
    g, r_, y, b, x = (
        _c(color, "green"), _c(color, "red"), _c(color, "yellow"),
        _c(color, "bold"), _c(color, "reset"),
    )
    dim = _c(color, "dim")

    scored = any(r.tier is not None for r in found)
    if scored:
        verified  = sorted([r for r in found if r.tier == TIER_VERIFIED],
                           key=lambda r: -(r.score or 0))
        likely    = sorted([r for r in found if r.tier == TIER_LIKELY],
                           key=lambda r: -(r.score or 0))
        impostors = sorted([r for r in found if r.tier == TIER_IMPOSTOR],
                           key=lambda r: -(r.score or 0))
        unscored  = [r for r in found if r.tier is None]
        likely    = likely + unscored
    else:
        verified, likely, impostors = [], found, []

    if verified:
        print(f"\n{b}{g}[ VERIFIED IDENTITY ]{x}{b} {len(verified)}{x}")
        for r in verified:
            print(_format_row(r, color, show_variant))
    if likely:
        print(f"\n{b}{g}[ LIKELY MATCH ]{x}{b} {len(likely)}{x}")
        for r in likely:
            print(_format_row(r, color, show_variant))
    if not verified and not likely and not impostors:
        print(f"\n{b}{g}[ FOUND ]{x}{b} 0{x}")
    if impostors:
        if show_all:
            print(f"\n{b}{y}[ POSSIBLE IMPOSTOR ]{x}{b} {len(impostors)}{x}")
            for r in impostors:
                print(_format_row(r, color, show_variant))
        else:
            print(f"\n{b}{y}[ POSSIBLE IMPOSTOR ]{x}{b} {len(impostors)}{x}  "
                  f"{dim}(use --show-all to display){x}")
    if not found_only:
        print(f"\n{b}{y}[   ?   ]{x}{b} {len(unknown)}{x}  "
              f"{dim}(use --export to see details){x}")
        print(f"{b}{r_}[MISSING]{x}{b} {missing_count}{x}")

    sys.stdout.flush()
    suffix = f"across {n_variants} variant{'s' if n_variants != 1 else ''}"
    total = len(found) + len(unknown) + missing_count
    print(f"\n{dim}{total} checks {suffix} in {elapsed:.1f}s{x}", file=sys.stderr)
