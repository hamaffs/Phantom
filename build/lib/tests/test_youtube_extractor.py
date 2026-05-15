"""Tests for the YouTube ytInitialData number parser.

Phantom now reads YouTube channel data from `ytInitialData` (the
embedded JSON blob), not the og:* meta tags. The blob's stat fields
arrive as localised text — "7.669 weergaven" (Dutch), "1,234,567
abonnees" (US), "1.2M Abonnenten" (German). The parser must recover
the integer regardless of which separator style the platform shipped.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from enrich import _yt_parse_count


class CountParsing(unittest.TestCase):
    def test_bare_integer(self):
        self.assertEqual(_yt_parse_count("18"), 18)

    def test_dutch_thousands_separator(self):
        # Dutch / German use `.` as thousands separator. The previous
        # parser treated 7.669 as 7 (float) — this is the regression
        # test for the live-test bug on hamaffs's YouTube.
        self.assertEqual(_yt_parse_count("7.669"), 7669)

    def test_english_thousands_separator(self):
        self.assertEqual(_yt_parse_count("1,234"), 1234)
        self.assertEqual(_yt_parse_count("1,234,567"), 1234567)

    def test_k_suffix_decimal(self):
        # With a K/M/B suffix the separator IS a decimal point.
        self.assertEqual(_yt_parse_count("1.2K"), 1200)
        self.assertEqual(_yt_parse_count("1.5M"), 1500000)

    def test_k_suffix_with_comma_decimal(self):
        # French / German often use comma as the decimal with K/M suffix.
        self.assertEqual(_yt_parse_count("1,2K"), 1200)

    def test_empty_or_garbage(self):
        self.assertIsNone(_yt_parse_count(""))
        self.assertIsNone(_yt_parse_count("abc"))


if __name__ == "__main__":
    unittest.main()
