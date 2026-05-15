"""Snapshot + diff for --watch.

After each scan, write the FOUND set + per-site profile data to a
snapshot file keyed by the original input. On the next run, read the
previous snapshot and emit a diff: new accounts, removed accounts,
follower deltas, bio changes, new photos.

Designed for cron usage:
    0 9 * * *  /usr/local/bin/phantom <username> --watch --quiet --export html

The snapshot store is just JSON. Each input gets one file under
~/.cache/phantom/snapshots/<slug>.json. We keep a small history (the
last N snapshots) so the diff can reach back if the previous run was
empty or partial.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


_SNAPSHOT_DIR = Path(
    os.environ.get("PHANTOM_SNAPSHOT_DIR")
    or Path.home() / ".cache" / "phantom" / "snapshots"
)
_HISTORY_KEEP = 10
_FILESAFE = str.maketrans({c: "_" for c in r' /\:?"*<>|'})


def _slug(raw: str) -> str:
    return raw.strip().translate(_FILESAFE) or "phantom"


@dataclass
class Snapshot:
    input: str
    generated_at: str
    sites: dict[str, dict] = field(default_factory=dict)  # site -> {url, profile, variant}

    @classmethod
    def from_results(cls, raw: str, found: list[dict]) -> "Snapshot":
        sites: dict[str, dict] = {}
        for r in found:
            sites[r["site"]] = {
                "url": r.get("final_url") or r.get("url"),
                "variant": r.get("variant"),
                "profile": r.get("profile") or {},
            }
        return cls(
            input=raw,
            generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            sites=sites,
        )

    def to_dict(self) -> dict:
        return {
            "input": self.input,
            "generated_at": self.generated_at,
            "sites": self.sites,
        }


def _path_for(raw: str) -> Path:
    return _SNAPSHOT_DIR / f"{_slug(raw)}.json"


def load_history(raw: str) -> list[Snapshot]:
    """Return the saved snapshots for `raw`, oldest-first."""
    p = _path_for(raw)
    if not p.is_file():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []
    out = []
    for entry in data.get("history", []):
        out.append(Snapshot(
            input=entry.get("input", raw),
            generated_at=entry.get("generated_at", ""),
            sites=entry.get("sites", {}),
        ))
    return out


def save_snapshot(snap: Snapshot) -> None:
    p = _path_for(snap.input)
    history = load_history(snap.input)
    history.append(snap)
    history = history[-_HISTORY_KEEP:]
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(
            {"history": [s.to_dict() for s in history]}, indent=2,
        ),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Diffing
# ---------------------------------------------------------------------------

# Numeric stats we treat as "interesting if they changed".
_NUM_STATS = (
    "followers", "following", "posts", "hearts", "karma",
    "post_karma", "comment_karma", "views", "rating", "steam_level",
)
_TEXT_STATS = ("bio", "display_name", "location", "company", "website")
_LIST_STATS = ("pinned_repos",)


@dataclass
class SiteChange:
    site: str
    kind: str          # "new" | "removed" | "changed"
    summary: str       # short human-readable description
    details: dict = field(default_factory=dict)


@dataclass
class Diff:
    previous_at: Optional[str]
    current_at: str
    added: list[SiteChange] = field(default_factory=list)
    removed: list[SiteChange] = field(default_factory=list)
    changed: list[SiteChange] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not (self.added or self.removed or self.changed)

    def to_dict(self) -> dict:
        return {
            "previous_at": self.previous_at,
            "current_at": self.current_at,
            "added": [c.__dict__ for c in self.added],
            "removed": [c.__dict__ for c in self.removed],
            "changed": [c.__dict__ for c in self.changed],
        }


def _format_delta(label: str, old, new) -> str:
    """Pretty-print a numeric or text delta."""
    if isinstance(old, (int, float)) and isinstance(new, (int, float)):
        d = new - old
        sign = "+" if d > 0 else ""
        return f"{label}: {old:,} → {new:,} ({sign}{d:,})"
    return f"{label}: {old!r} → {new!r}"


def diff(prev: Optional[Snapshot], curr: Snapshot) -> Diff:
    """Compare a previous snapshot to the current one."""
    out = Diff(
        previous_at=prev.generated_at if prev else None,
        current_at=curr.generated_at,
    )
    prev_sites = prev.sites if prev else {}
    curr_sites = curr.sites

    for site, info in curr_sites.items():
        if site not in prev_sites:
            out.added.append(SiteChange(
                site=site, kind="new",
                summary=f"new account at {info['url']}",
                details={"url": info["url"], "profile": info.get("profile") or {}},
            ))

    for site, info in prev_sites.items():
        if site not in curr_sites:
            out.removed.append(SiteChange(
                site=site, kind="removed",
                summary=f"account no longer found ({info.get('url')})",
                details={"url": info.get("url")},
            ))

    for site, curr_info in curr_sites.items():
        if site not in prev_sites:
            continue
        prev_p = (prev_sites[site].get("profile") or {})
        curr_p = (curr_info.get("profile") or {})
        deltas: list[str] = []
        details: dict = {}
        for key in _NUM_STATS:
            if key in prev_p and key in curr_p and prev_p[key] != curr_p[key]:
                deltas.append(_format_delta(key, prev_p[key], curr_p[key]))
                details[key] = {"before": prev_p[key], "after": curr_p[key]}
        for key in _TEXT_STATS:
            if (
                prev_p.get(key) != curr_p.get(key)
                and (prev_p.get(key) or curr_p.get(key))
            ):
                deltas.append(_format_delta(key, prev_p.get(key), curr_p.get(key)))
                details[key] = {"before": prev_p.get(key), "after": curr_p.get(key)}
        for key in _LIST_STATS:
            old_list = list(prev_p.get(key) or [])
            new_list = list(curr_p.get(key) or [])
            if old_list != new_list:
                added = [x for x in new_list if x not in old_list]
                gone = [x for x in old_list if x not in new_list]
                if added or gone:
                    bits = []
                    if added:
                        bits.append(f"+{', '.join(added)}")
                    if gone:
                        bits.append(f"-{', '.join(gone)}")
                    deltas.append(f"{key}: " + " ".join(bits))
                    details[key] = {"added": added, "removed": gone}
        # New profile photo (URL change, even if hash didn't — phash isn't
        # stored in the snapshot to keep it cheap).
        if prev_p.get("photo") and curr_p.get("photo"):
            if prev_p["photo"] != curr_p["photo"]:
                deltas.append("photo updated")
                details["photo"] = {
                    "before": prev_p["photo"], "after": curr_p["photo"],
                }
        if deltas:
            out.changed.append(SiteChange(
                site=site, kind="changed",
                summary="; ".join(deltas), details=details,
            ))
    return out


# ---------------------------------------------------------------------------
# Pretty printer
# ---------------------------------------------------------------------------

def render_diff_terminal(d: Diff, color: bool) -> str:
    if d.is_empty():
        return "No changes since last run."
    lines: list[str] = []
    g = "\033[32m" if color else ""
    r = "\033[31m" if color else ""
    y = "\033[33m" if color else ""
    b = "\033[1m" if color else ""
    x = "\033[0m" if color else ""

    if d.added:
        lines.append(f"{b}{g}+ {len(d.added)} new account(s){x}")
        for c in d.added:
            lines.append(f"  + {b}{c.site}{x}  {c.summary}")
    if d.removed:
        lines.append(f"{b}{r}- {len(d.removed)} removed account(s){x}")
        for c in d.removed:
            lines.append(f"  - {b}{c.site}{x}  {c.summary}")
    if d.changed:
        lines.append(f"{b}{y}~ {len(d.changed)} changed account(s){x}")
        for c in d.changed:
            lines.append(f"  ~ {b}{c.site}{x}  {c.summary}")
    return "\n".join(lines)
