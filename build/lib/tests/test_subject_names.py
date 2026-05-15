"""Tests for the subject-level real-name / nickname classifier."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from exporters.html_export import _classify_name, _detect_subject_names


class ClassifyName(unittest.TestCase):
    def test_full_real_name(self):
        self.assertEqual(_classify_name("Alex Stevens", set()), "real_name")
        self.assertEqual(_classify_name("Jean-Pierre Dupont", set()), "real_name")
        self.assertEqual(_classify_name("Maria O'Brien", set()), "real_name")

    def test_single_word_nickname(self):
        self.assertEqual(_classify_name("Sasha", set()), "nickname")
        self.assertEqual(_classify_name("PewDiePie", set()), "nickname")

    def test_username_when_matches_variant(self):
        self.assertEqual(_classify_name("alice", {"alice"}), "username")
        self.assertEqual(_classify_name("ALICE", {"alice"}), "username")

    def test_platform_decorated_is_noise(self):
        # Pastebin's title pattern
        self.assertEqual(
            _classify_name("Alice's Pastebin - Pastebin.com", set()),
            "noise",
        )
        # Twitch tracker title
        self.assertEqual(
            _classify_name("alice - Streamer Overview & Stats", set()),
            "noise",
        )

    def test_digits_in_name_rejected(self):
        self.assertEqual(_classify_name("Cool User 123", set()), "noise")
        self.assertEqual(_classify_name("alice 99", set()), "noise")

    def test_lowercase_first_letter_rejected(self):
        # "theirhandle" is a styled handle, not a real name.
        self.assertEqual(_classify_name("theirhandle", set()), "noise")

    def test_empty_or_too_long(self):
        self.assertEqual(_classify_name("", set()), "noise")
        self.assertEqual(_classify_name("a" * 200, set()), "noise")


class StubResult:
    """Minimal CheckResult stub for _detect_subject_names."""
    def __init__(self, profile, tier=None, is_primary=True):
        self.exists = True
        self.profile = profile
        self.tier = tier
        self.is_primary_identity = is_primary


class DetectSubjectNames(unittest.TestCase):
    def test_youtube_bio_surfaces_real_name(self):
        # YouTube About-page case: display name is a nickname,
        # description carries the real name.
        results = [
            StubResult({"display_name": "Sasha", "bio": "Alex Stevens"}),
            StubResult({"display_name": "alice"}),
        ]
        real, nick = _detect_subject_names(results, {"alice"})
        self.assertEqual(real, "Alex Stevens")
        self.assertEqual(nick, "Sasha")

    def test_nickname_repeated_across_accounts(self):
        results = [
            StubResult({"display_name": "Sasha"}),
            StubResult({"display_name": "Sasha"}),
        ]
        real, nick = _detect_subject_names(results, {"alice"})
        self.assertEqual(real, "")
        self.assertEqual(nick, "Sasha")

    def test_impostor_tier_excluded(self):
        # Impostor accounts often have misleading names — should not
        # contribute to the subject-level real-name detection.
        results = [
            StubResult({"display_name": "Cool Fake Person"}, tier="possible_impostor"),
        ]
        real, nick = _detect_subject_names(results, {"alice"})
        self.assertEqual(real, "")

    def test_nickname_inside_real_name_dropped(self):
        # When the nickname is already a substring of the real name,
        # don't show both.
        results = [
            StubResult({"display_name": "Sasha"}),
            StubResult({"display_name": "Sasha Doe"}),
        ]
        real, nick = _detect_subject_names(results, set())
        self.assertEqual(real, "Sasha Doe")
        self.assertEqual(nick, "")


if __name__ == "__main__":
    unittest.main()
