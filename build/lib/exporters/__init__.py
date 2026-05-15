"""Export format dispatcher. Resolves an --export argument to a path and
delegates to the matching format-specific exporter.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from models import CheckResult
from exporters.csv_export import export_csv
from exporters.html_export import export_html
from exporters.json_export import _build_json_payload, export_json
from exporters.markdown_export import export_markdown
from exporters.mermaid_export import export_mermaid
from exporters.pdf_export import export_pdf


_FORMAT_ALIASES = {
    "html": ".html", "htm": ".html",
    "json": ".json",
    "md": ".md", "markdown": ".md", "txt": ".md",
    "pdf": ".pdf",
    "csv": ".csv",
    "mmd": ".mmd", "mermaid": ".mmd",
}

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_filename(raw: str) -> str:
    """Turn the raw input into a filesystem-safe slug for default export names.

    Multi-word inputs are joined with `_`; anything that isn't word-char,
    `.`, or `-` becomes `_`. Empty result falls back to "phantom".
    """
    slug = _SAFE_NAME_RE.sub("_", raw.strip()).strip("._-")
    return slug or "phantom"


def resolve_export_path(spec: str, raw: str) -> Path:
    """Decide where the export goes.

    Behaviour:
      - "html" / "json" / "md"   → `<input>_report.<ext>` in the cwd
      - "report.html"            → use as-is (has an extension)
      - "/tmp/out.html"          → use as-is (full path)
      - "reports/"               → directory, becomes
                                    "reports/<input>_report.json"
    """
    p = Path(spec).expanduser()
    if spec.endswith("/") or p.is_dir():
        return p / f"{_safe_filename(raw)}_report.json"
    if spec.lower() in _FORMAT_ALIASES:
        ext = _FORMAT_ALIASES[spec.lower()]
        return Path(f"{_safe_filename(raw)}_report{ext}")
    if p.suffix:
        return p
    # No extension and not a known format alias → treat as a basename and
    # default to JSON so the user gets a usable file.
    return p.with_suffix(".json")


def export_report(
    grouped: list[tuple[str, list[CheckResult]]],
    raw: str,
    elapsed: float,
    path: Path,
    overall=None,
    clusters=None,
    emails=None,
    deep_evidence=None,
    face_map=None,
    dark: bool = False,
    dis_clusters=None,
    photo_bytes_map=None,
) -> None:
    """Dispatch by extension. Defaults to JSON if the suffix is unrecognised."""
    suffix = path.suffix.lower()
    if suffix == ".html" or suffix == ".htm":
        export_html(grouped, raw, elapsed, path, overall, clusters, emails, deep_evidence, face_map,
                    dark=dark, dis_clusters=dis_clusters, photo_bytes_map=photo_bytes_map)
    elif suffix == ".pdf":
        export_pdf(grouped, raw, elapsed, path, overall, clusters, emails, deep_evidence, face_map,
                   dark=dark, dis_clusters=dis_clusters, photo_bytes_map=photo_bytes_map)
    elif suffix == ".md" or suffix == ".markdown" or suffix == ".txt":
        export_markdown(grouped, raw, elapsed, path, overall, clusters, dis_clusters=dis_clusters)
    elif suffix == ".csv":
        export_csv(grouped, raw, elapsed, path)
    elif suffix == ".mmd":
        export_mermaid(
            grouped, raw, elapsed, path,
            overall=overall, clusters=clusters, dis_clusters=dis_clusters,
        )
    else:
        export_json(grouped, raw, elapsed, path, overall, clusters, emails, deep_evidence,
                    dis_clusters=dis_clusters)
