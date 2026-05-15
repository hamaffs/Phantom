"""Tests for stylometric correlation — feature extraction + similarity."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from stylometry import (
    STYLE_MATCH_THRESHOLD, feature_vector, is_style_match, style_score,
)


class FeatureExtraction(unittest.TestCase):
    def test_empty_text_returns_empty(self):
        self.assertEqual(feature_vector(""), {})
        self.assertEqual(feature_vector("   "), {})

    def test_punctuation_rates_extracted(self):
        v = feature_vector("hello — world — goodbye!")
        self.assertIn("p_em_dash", v)
        self.assertIn("p_exclaim", v)
        self.assertGreater(v["p_em_dash"], 0)

    def test_capitalization_class(self):
        self.assertEqual(feature_vector("hi there friend")["cap_allower"], 1.0)
        self.assertEqual(feature_vector("HI THERE FRIEND")["cap_allcaps"], 1.0)
        self.assertEqual(
            feature_vector("Hello. How are you today?")["cap_sentence"], 1.0,
        )

    def test_emoji_features(self):
        v = feature_vector("hello 👋 world 🌍 cool 😎")
        self.assertIn("emoji_rate", v)
        self.assertEqual(v["emoji_unique"], 3)

    def test_no_emoji_no_emoji_features(self):
        v = feature_vector("hello world goodbye")
        self.assertNotIn("emoji_rate", v)
        self.assertNotIn("emoji_unique", v)

    def test_lexical_signature_excludes_stopwords(self):
        v = feature_vector("the quick brown fox jumps over the lazy dog")
        # 'the' is a stop-word.
        self.assertNotIn("w_the", v)
        # Content words should be present.
        self.assertTrue(any(k.startswith("w_quick") for k in v))


class Similarity(unittest.TestCase):
    def test_identical_bios_score_1(self):
        s = "lowercase only · paris · web dev"
        self.assertAlmostEqual(style_score(s, s), 1.0, places=4)

    def test_very_different_bios_score_low(self):
        a = "lowercase only ✨ paris ✨ web dev"
        b = "PROFESSIONAL ATHLETE. TEAM CAPTAIN. MULTIPLE TITLES."
        s = style_score(a, b)
        self.assertLess(s, 0.3)

    def test_same_author_two_bios_score_high(self):
        # Both bios share: lowercase-only, em-dash, no exclamation.
        a = "writer — based in lisbon — into film and books"
        b = "designer — based in lisbon — into film and tea"
        s = style_score(a, b)
        self.assertGreater(s, STYLE_MATCH_THRESHOLD - 0.1)

    def test_empty_returns_zero(self):
        self.assertEqual(style_score("", "anything here"), 0.0)
        self.assertEqual(style_score("anything here", ""), 0.0)


class MatchGate(unittest.TestCase):
    def test_short_bios_never_match(self):
        a = "lowercase only"
        b = "lowercase only"
        self.assertFalse(is_style_match(a, b))

    def test_long_similar_bios_match(self):
        a = (
            "writer — based in lisbon — into film, books, and slow afternoons "
            "with espresso"
        )
        b = (
            "designer — based in lisbon — into film, tea, and slow afternoons "
            "with espresso"
        )
        self.assertTrue(is_style_match(a, b))

    def test_long_dissimilar_bios_no_match(self):
        a = "writer — based in lisbon — into film and slow afternoons here"
        b = "PROFESSIONAL ATHLETE!!! MULTIPLE TITLES!!! ALL CAPS ALL THE TIME!!!"
        self.assertFalse(is_style_match(a, b))


if __name__ == "__main__":
    unittest.main()
