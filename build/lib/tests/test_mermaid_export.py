"""Tests for the Mermaid mindmap exporter — output shape and sanitization."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from exporters.mermaid_export import _node_line, _sanitize, export_mermaid
from models import CheckResult


def _result(site: str, score: int = 50, verified: bool = False, display_name: str = "") -> CheckResult:
    profile = {}
    if verified:
        profile["verified"] = True
    if display_name:
        profile["display_name"] = display_name
    return CheckResult(
        site=site, category="social",
        url=f"https://example.com/{site}",
        exists=True, reliability=80, score=score,
        variant=site.lower(), profile=profile,
    )


class Sanitize(unittest.TestCase):
    def test_parens_replaced(self):
        self.assertEqual(_sanitize("Hello (world)"), "Hello [world]")

    def test_braces_replaced(self):
        self.assertEqual(_sanitize("{foo}"), "[foo]")

    def test_newlines_collapsed(self):
        self.assertEqual(_sanitize("line1\nline2"), "line1 line2")

    def test_length_capped(self):
        long = "a" * 200
        self.assertLessEqual(len(_sanitize(long)), 60)


class NodeLine(unittest.TestCase):
    def test_includes_score_and_verified(self):
        r = _result("Twitter", score=80, verified=True, display_name="Alice")
        line = _node_line(r)
        self.assertIn("Twitter", line)
        self.assertIn("score 80", line)
        self.assertIn("verified", line)
        self.assertIn("Alice", line)

    def test_skips_missing_fields(self):
        r = _result("Pastebin", score=30)
        line = _node_line(r)
        self.assertIn("Pastebin", line)
        self.assertIn("score 30", line)
        self.assertNotIn("verified", line)


class ExportShape(unittest.TestCase):
    def test_writes_mindmap_with_root(self):
        with tempfile.NamedTemporaryFile(mode="r", suffix=".mmd", delete=False) as tmp:
            path = Path(tmp.name)
        grouped = [("hamaffs", [_result("Twitter"), _result("GitHub")])]
        export_mermaid(grouped, "hamaffs", 1.0, path)
        body = path.read_text(encoding="utf-8")
        path.unlink()
        self.assertTrue(body.startswith("mindmap\n"))
        self.assertIn("root((hamaffs))", body)
        self.assertIn("Twitter", body)
        self.assertIn("GitHub", body)


if __name__ == "__main__":
    unittest.main()
