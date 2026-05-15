"""Tests for the generic bio-URL harvester.

This is the post-extractor pass that catches handles users write
directly in their bio rather than in a structured Links field.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from enrich import _harvest_bio_links


class BioURLHarvest(unittest.TestCase):
    def test_full_https_url_caught(self):
        out = _harvest_bio_links("find me at https://twitter.com/alice")
        self.assertIn("https://twitter.com/alice", out)

    def test_bare_domain_form_caught(self):
        out = _harvest_bio_links("instagram.com/foo · tiktok.com/@bar")
        self.assertIn("https://instagram.com/foo", out)
        self.assertIn("https://tiktok.com/@bar", out)

    def test_unknown_domain_ignored(self):
        out = _harvest_bio_links("check example.com/me out")
        self.assertEqual(out, [])

    def test_trailing_punctuation_stripped(self):
        out = _harvest_bio_links("see twitter.com/alice.")
        self.assertIn("https://twitter.com/alice", out)
        self.assertNotIn("https://twitter.com/alice.", out)

    def test_dedup_case_insensitive(self):
        out = _harvest_bio_links("TWITTER.COM/alice and twitter.com/alice")
        # Same URL surfaced via two casings — should keep only one.
        self.assertEqual(len(out), 1)

    def test_empty_input_returns_empty(self):
        self.assertEqual(_harvest_bio_links(""), [])
        self.assertEqual(_harvest_bio_links(None), [])

    def test_multiple_platforms_in_one_bio(self):
        bio = "Find me on twitter.com/foo, github.com/foo, and bsky.app/profile/foo.bar"
        out = _harvest_bio_links(bio)
        self.assertIn("https://twitter.com/foo", out)
        self.assertIn("https://github.com/foo", out)
        self.assertIn("https://bsky.app/profile/foo.bar", out)


if __name__ == "__main__":
    unittest.main()
