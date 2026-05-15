"""Markdown exporter."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from confidence import TIER_IMPOSTOR, TIER_LIKELY, TIER_VERIFIED
from dedupe import _flatten
from disambiguation import LABEL_LOW, LABEL_PRIMARY, LABEL_SECONDARY
from models import CheckResult


def _md_identity_block(c, header: str) -> list[str]:
    """Render an identity cluster as a markdown block. Used for both the
    overall identity and photo-matched clusters — same shape, different
    headers."""
    lines = [header, ""]
    if c.display_name:
        lines.append(f"- **Display name**: {c.display_name}")
    lines.append(f"- **Sites** ({len(c.sites)}): {', '.join(c.sites)}")
    if c.locations:
        lines.append(f"- **Locations**: {', '.join(c.locations)}")
    if c.geo_hint and c.geo_hint.region and c.geo_hint.region not in c.locations:
        lines.append(
            f"- **Likely region**: {c.geo_hint.region} "
            f"_(conf {c.geo_hint.confidence}, {'; '.join(c.geo_hint.signals)})_"
        )
    if c.joined_oldest:
        lines.append(f"- **Active since**: {c.joined_oldest}")
    if c.total_followers is not None:
        lines.append(f"- **Followers (total)**: {c.total_followers:,}")
    if c.total_following is not None:
        lines.append(f"- **Following (total)**: {c.total_following:,}")
    if c.total_posts is not None:
        lines.append(f"- **Posts (total)**: {c.total_posts:,}")
    if c.verified_on:
        lines.append(f"- **Verified on**: {', '.join(c.verified_on)}")
    if c.private_on:
        lines.append(f"- **Private on**: {', '.join(c.private_on)}")
    if c.rationale:
        lines.append(f"- **Reason**: {'; '.join(c.rationale)}")
    lines.append(f"- **Confidence**: {c.confidence}")
    lines.append("")
    return lines


def export_markdown(grouped, raw, elapsed, path: Path, overall=None, clusters=None, dis_clusters=None) -> None:
    found, unknown, missing_count = _flatten(grouped)
    clusters = clusters or []
    multi = [c for c in clusters if len(c.member_indexes) > 1]
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        f"# Phantom report — `{raw}`",
        "",
        f"_Generated {ts} — {len(grouped)} variant(s) in {elapsed:.1f}s_",
        "",
        f"- **Found**: {len(found)}",
        f"- **Unknown**: {len(unknown)}",
        f"- **Missing**: {missing_count}",
        "",
    ]
    if overall and len(found) >= 2:
        lines += _md_identity_block(overall, "## Overall identity")

    def _md_rows(rs: list) -> list[str]:
        out = []
        for r in rs:
            tag = f" — `{r.variant}`" if r.variant else ""
            score_tag = f" (score {r.score})" if r.score is not None else ""
            cid_tag = f" [cluster {r.identity_id}]" if r.identity_id is not None else ""
            out.append(f"- [{r.site}]({r.url}){tag}{score_tag}{cid_tag}")
        return out

    if dis_clusters:
        # Group by identity cluster.
        for c in dis_clusters:
            label_map = {LABEL_PRIMARY: "Primary identity",
                         LABEL_SECONDARY: "Secondary cluster",
                         LABEL_LOW: "Unrelated matches"}
            heading = label_map.get(c.label, c.label)
            name_part = f" — {c.display_name}" if c.display_name else ""
            lines += [f"## {heading}{name_part} ({c.size} accounts)", ""]
            members = sorted(
                [found[i] for i in c.member_indices if i < len(found)],
                key=lambda r: -(r.score or 0),
            )
            lines += _md_rows(members) + [""]
    else:
        scored = any(r.tier is not None for r in found)
        if scored and found:
            v = sorted([r for r in found if r.tier == TIER_VERIFIED], key=lambda r: -(r.score or 0))
            l = sorted([r for r in found if r.tier == TIER_LIKELY],   key=lambda r: -(r.score or 0))
            imp = sorted([r for r in found if r.tier == TIER_IMPOSTOR], key=lambda r: -(r.score or 0))
            l += [r for r in found if r.tier is None]
            if v:
                lines += [f"## Verified identity ({len(v)})", ""] + _md_rows(v) + [""]
            if l:
                lines += [f"## Likely match ({len(l)})", ""] + _md_rows(l) + [""]
            if imp:
                lines += [f"## Possible impostor ({len(imp)})", ""] + _md_rows(imp) + [""]
        else:
            lines += [f"## Found ({len(found)})", ""]
            if found:
                lines += _md_rows(found)
            else:
                lines.append("_None._")
            lines.append("")

    lines += ["## Missing", "", f"{missing_count} sites cleanly returned not-found."]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
