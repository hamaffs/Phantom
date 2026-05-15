"""PDF exporter — renders the dossier HTML to PDF via Playwright."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from typing import Optional

from exporters.html_export import export_html


def export_pdf(grouped, raw, elapsed, path: Path, overall=None, clusters=None, emails=None, deep_evidence=None, face_map=None, dark=False, dis_clusters=None, photo_bytes_map=None) -> None:
    """Render the HTML report to PDF via playwright (Chromium)."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(
            "error: PDF export requires playwright.\n"
            "  Install with: pip install playwright && playwright install chromium",
            file=sys.stderr,
        )
        return
    import tempfile
    html_path = Path(tempfile.mktemp(suffix=".html"))
    try:
        export_html(
            grouped, raw, elapsed, html_path,
            overall, clusters, emails, deep_evidence, face_map,
            dark=dark, include_toggle=False, dis_clusters=dis_clusters,
            photo_bytes_map=photo_bytes_map,
        )
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page()
            page.set_viewport_size({"width": 1100, "height": 768})
            page.goto(f"file://{html_path.resolve()}", wait_until="networkidle")
            page.pdf(
                path=str(path),
                width="1100px",
                print_background=True,
                margin={"top": "0px", "right": "0px", "bottom": "0px", "left": "0px"},
            )
            browser.close()
    finally:
        if html_path.exists():
            html_path.unlink()
