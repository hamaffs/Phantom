"""Mermaid mindmap exporter (`.mmd` / `.mermaid`).

Mermaid is the markdown-native diagram language — `.mmd` files render
natively in GitHub, GitLab, Obsidian, VS Code, Notion, and any HTML
preview tool that loads the Mermaid library. No external ecosystem
dependency like XMind requires, no proprietary format.

Output shape — a `mindmap` rooted at the subject's handle, branching
into identity clusters (primary, secondary, unrelated), each cluster
listing its member accounts with site name + score. When no
disambiguation clusters exist (--no-cluster), falls back to the three
confidence tiers.

Example output for `hamaffs`:

    mindmap
      root((hamaffs))
        Primary identity (8 accounts)
          Threads · score 65
          YouTube · score 55
          ...
        Secondary cluster
          Pastebin · score 60

This is the visual-export answer to Maigret's XMind file, but
text-native and zero-install.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from confidence import TIER_IMPOSTOR, TIER_LIKELY, TIER_VERIFIED
from dedupe import _flatten
from disambiguation import LABEL_LOW, LABEL_PRIMARY, LABEL_SECONDARY
from models import CheckResult


def _sanitize(text: str) -> str:
    """Strip characters that would break the Mermaid parser.

    Mermaid is picky about parentheses and braces inside node labels —
    they get treated as syntax. Replace them with neutral equivalents
    so the rendered tree still reads naturally.
    """
    if not text:
        return ""
    out = text.strip()
    # Strip newlines and tabs (would terminate the line)
    out = out.replace("\n", " ").replace("\r", " ").replace("\t", " ")
    # Replace parens/braces that confuse the parser
    out = (
        out.replace("(", "[")
           .replace(")", "]")
           .replace("{", "[")
           .replace("}", "]")
    )
    # Collapse whitespace
    while "  " in out:
        out = out.replace("  ", " ")
    return out[:60]  # cap to keep the diagram legible


def _node_line(r: CheckResult) -> str:
    p = r.profile or {}
    parts = [r.site]
    if r.score is not None:
        parts.append(f"score {r.score}")
    if p.get("verified"):
        parts.append("verified ✓")
    if p.get("display_name"):
        parts.append(_sanitize(p["display_name"]))
    return " · ".join(parts)


def export_mermaid(
    grouped,
    raw,
    elapsed,
    path: Path,
    *,
    overall=None,
    clusters=None,
    dis_clusters=None,
    **_ignored,
) -> None:
    """Write a Mermaid `mindmap` file for `grouped`."""
    found, _, _ = _flatten(grouped)
    safe_root = _sanitize(raw) or "subject"

    lines: list[str] = ["mindmap", f"  root(({safe_root}))"]

    if dis_clusters and found:
        # Cluster-grouped rendering — preferred when disambiguation ran.
        label_map = {
            LABEL_PRIMARY: "Primary identity",
            LABEL_SECONDARY: "Secondary cluster",
            LABEL_LOW: "Unrelated matches",
        }
        # Order: primary first, then secondary by ID, then unrelated last.
        ordered = sorted(
            dis_clusters,
            key=lambda c: (
                {LABEL_PRIMARY: 0, LABEL_SECONDARY: 1, LABEL_LOW: 2}.get(c.label, 3),
                getattr(c, "cluster_id", 0),
            ),
        )
        for c in ordered:
            header = label_map.get(c.label, c.label)
            count = f"{c.size} account{'s' if c.size != 1 else ''}"
            name = f" — {_sanitize(c.display_name)}" if c.display_name else ""
            lines.append(f"    {header}{name} [{count}]")
            members = sorted(
                [found[i] for i in c.member_indices if i < len(found)],
                key=lambda r: -(r.score or 0),
            )
            for r in members:
                lines.append(f"      {_sanitize(_node_line(r))}")
    elif found:
        # Tier-grouped fallback (--no-cluster).
        tier_map = [
            (TIER_VERIFIED, "Verified identity"),
            (TIER_LIKELY, "Likely match"),
            (TIER_IMPOSTOR, "Possible impostor"),
        ]
        for tier, label in tier_map:
            members = sorted(
                [r for r in found if r.tier == tier],
                key=lambda r: -(r.score or 0),
            )
            if not members:
                continue
            lines.append(f"    {label} [{len(members)}]")
            for r in members:
                lines.append(f"      {_sanitize(_node_line(r))}")
        untiered = [r for r in found if r.tier is None]
        if untiered:
            lines.append(f"    Other found [{len(untiered)}]")
            for r in untiered:
                lines.append(f"      {_sanitize(_node_line(r))}")
    else:
        lines.append("    No accounts found")

    # Optional overall-identity summary as a sibling root branch.
    if overall and getattr(overall, "display_name", None):
        lines.append(f"    Identity summary")
        if overall.display_name:
            lines.append(f"      Name · {_sanitize(overall.display_name)}")
        locs = getattr(overall, "locations", None) or []
        if locs:
            lines.append(f"      Locations · {_sanitize(', '.join(locs))}")
        geo = getattr(overall, "geo_hint", None)
        if geo and getattr(geo, "region", None):
            lines.append(f"      Region · {_sanitize(geo.region)}")
        if getattr(overall, "verified_on", None):
            lines.append(
                f"      Verified on · {_sanitize(', '.join(overall.verified_on))}"
            )

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
