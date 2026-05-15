"""Tests for the username variant engine.

Covers the email-to-handle shortcut, leetspeak substitution, and the
existing separator / name-mode logic to lock in the current contract.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from variants import generate


class EmailHandling(unittest.TestCase):
    def test_email_collapses_to_local_part(self):
        out = generate("alice@example.com")
        self.assertIn("alice", out)
        # Make sure the @ form itself didn't sneak through the validator.
        self.assertFalse(any("@" in v for v in out))

    def test_plus_tag_is_stripped(self):
        out = generate("alice+gh@example.com")
        self.assertIn("alice", out)
        self.assertNotIn("alice+gh", out)

    def test_non_email_input_unchanged(self):
        # Looks email-ish but isn't (no TLD) → treated as a plain token.
        out = generate("alice@example")
        self.assertNotIn("alice", [v for v in out if "@" not in v and v == "alice"])


class Leetspeak(unittest.TestCase):
    def test_leet_produces_known_substitutions(self):
        out = generate("jose")
        self.assertIn("j0se", out)   # o → 0
        self.assertIn("jos3", out)   # e → 3
        self.assertIn("jo5e", out)   # s → 5

    def test_leet_skips_inputs_with_no_eligible_letter(self):
        # All-digit + uncommon-letter input → no leet variants generated.
        out = generate("xyz")
        leet_chars = set("0134578")
        leet_in_out = [v for v in out if any(c in leet_chars for c in v)]
        # Only the number-suffix variants (xyz1, xyz2, xyz99, xyz123) — not
        # leet substitutions.
        self.assertTrue(all(v.startswith("xyz") for v in leet_in_out))


class NameMode(unittest.TestCase):
    def test_two_words_produce_name_perms(self):
        out = generate("John Smith")
        self.assertIn("johnsmith", out)
        self.assertIn("john.smith", out)
        self.assertIn("smithjohn", out)

    def test_first_letter_lastname_emitted_when_long_enough(self):
        # "john" + "anderson" → "janderson" (9 chars) passes the 8-char floor.
        out = generate("John Anderson")
        self.assertIn("janderson", out)
        self.assertIn("andersonj", out)

    def test_short_perms_below_minimum_are_dropped(self):
        # "Al Bo" → "albo" is 4 chars, below the 8-char name-mode floor.
        out = generate("Al Bo")
        self.assertNotIn("albo", out)


class Validation(unittest.TestCase):
    def test_all_outputs_match_username_pattern(self):
        import re
        rx = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")
        for inp in ("alice", "John Smith", "user@example.com", "foo_bar"):
            for v in generate(inp):
                self.assertRegex(v, rx, f"{inp!r} produced bad variant {v!r}")


if __name__ == "__main__":
    unittest.main()
