"""Tests for photo_ocr — handle extraction logic (no Tesseract needed)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from photo_ocr import _extract_candidate_handles


class HandleExtraction(unittest.TestCase):
    def test_basic_handle_extracted(self):
        # cool_user123 has both a digit and an underscore → passes the
        # "deliberately username-shaped" gate.
        out = _extract_candidate_handles("follow me @cool_user123", set())
        self.assertIn("cool_user123", out)

    def test_plain_word_rejected_by_default(self):
        # OCR noise on non-text avatars often surfaces capitalized English
        # words. We reject them unless they look username-shaped.
        out = _extract_candidate_handles("ARUN something", set())
        self.assertNotIn("ARUN", out)
        out2 = _extract_candidate_handles("cam something", set())
        self.assertNotIn("cam", out2)

    def test_plain_word_accepted_when_anchor_matches(self):
        # If the OCR finds a word that looks like the user's handle,
        # accept it even though it's "just letters".
        out = _extract_candidate_handles("Hamaffs is the handle", {"hama"})
        # "Hamaffs" contains "hama" anchor → kept.
        self.assertIn("Hamaffs", out)

    def test_domain_suffix_rejected(self):
        out = _extract_candidate_handles("huggingface.co/hamaffs", set())
        # `huggingface.co` is a domain not a handle.
        self.assertNotIn("huggingface.co", out)

    def test_already_tested_filtered(self):
        out = _extract_candidate_handles(
            "find me @alice_doe online", {"alice_doe"},
        )
        self.assertNotIn("alice_doe", out)

    def test_blocklist_filters_platform_words(self):
        out = _extract_candidate_handles(
            "follow me on instagram twitter youtube", set(),
        )
        self.assertEqual(out, [])

    def test_too_short_ignored(self):
        out = _extract_candidate_handles("hi me ab tt", set())
        self.assertEqual(out, [])

    def test_handle_must_start_with_letter(self):
        out = _extract_candidate_handles("user 123abc _underscore", set())
        for h in out:
            self.assertTrue(h[0].isalpha(), f"bad leading char in {h!r}")

    def test_dedup_within_text(self):
        # Use tokens that pass the shape gate (digit + underscore).
        out = _extract_candidate_handles(
            "alice_1 alice_1 alice_1 bob_2", set(),
        )
        self.assertEqual(sorted(out), ["alice_1", "bob_2"])

    def test_case_insensitive_dedup(self):
        out = _extract_candidate_handles("Alice_1 ALICE_1 alice_1", set())
        self.assertEqual(len(out), 1)


if __name__ == "__main__":
    unittest.main()
