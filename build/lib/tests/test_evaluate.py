"""Unit tests for the core detection function in models.evaluate.

These tests are the guardrail for Phantom's "accuracy first / zero false
positives" promise. Any regression in the decision order — invalid_status,
absence_text, bot-wall, presence_text — flips real users to UNKNOWN or
worse, false positives. We test every branch.

Run with:
    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from models import Site, evaluate


def _site(**kw) -> Site:
    """Build a Site with sensible defaults — only override what each test
    actually cares about."""
    defaults = dict(
        name="Example",
        category="dev",
        url="https://example.com/{username}",
        method="status",
        reliability=80,
        valid_status=[200],
        invalid_status=[404],
        presence_text=[],
        absence_text=[],
    )
    defaults.update(kw)
    return Site(**defaults)


class HardMissingSignals(unittest.TestCase):
    """invalid_status and absence_text are the two MISSING hard rules; both
    must win even when other signals would otherwise say FOUND."""

    def test_invalid_status_wins(self):
        site = _site(invalid_status=[404])
        exists, reason = evaluate(site, 404, "<html>hi</html>", "alice")
        self.assertIs(exists, False)
        self.assertEqual(reason, "404")

    def test_invalid_status_beats_a_200_body(self):
        site = _site(valid_status=[200], invalid_status=[404])
        exists, _ = evaluate(site, 404, "<title>alice</title>", "alice")
        self.assertIs(exists, False)

    def test_absence_text_beats_valid_status(self):
        site = _site(
            method="status",
            valid_status=[200],
            absence_text=["User not found"],
        )
        exists, reason = evaluate(site, 200, "User not found", "alice")
        self.assertIs(exists, False)
        self.assertEqual(reason, "absence")

    def test_absence_text_with_username_substitution(self):
        site = _site(absence_text=["{username} doesn't exist"])
        exists, _ = evaluate(site, 200, "alice doesn't exist on our site", "alice")
        self.assertIs(exists, False)


class BotWallDetection(unittest.TestCase):
    """A bot-wall title means the body is untrustworthy — must return
    UNKNOWN even when the status code is in valid_status."""

    def test_cloudflare_just_a_moment(self):
        site = _site(valid_status=[200])
        body = "<title>Just a moment...</title><body>challenge</body>"
        exists, reason = evaluate(site, 200, body, "alice")
        self.assertIsNone(exists)
        self.assertEqual(reason, "bot-wall")

    def test_verify_you_are_human(self):
        site = _site(valid_status=[200])
        body = "<title>Verify you are human</title>"
        exists, reason = evaluate(site, 200, body, "alice")
        self.assertIsNone(exists)
        self.assertEqual(reason, "bot-wall")

    def test_bot_wall_does_not_fire_on_unrelated_title(self):
        site = _site(valid_status=[200])
        body = "<title>alice's profile</title>"
        exists, _ = evaluate(site, 200, body, "alice")
        self.assertIs(exists, True)


class MethodStatus(unittest.TestCase):
    """method=status: 200 in valid_status → FOUND; anything else → UNKNOWN
    (NOT False, because we don't have a clean signal it's actually MISSING)."""

    def test_status_200_in_valid_status_means_found(self):
        site = _site(method="status", valid_status=[200])
        exists, reason = evaluate(site, 200, "<html></html>", "alice")
        self.assertIs(exists, True)
        self.assertEqual(reason, "200")

    def test_status_in_neither_list_is_unknown(self):
        site = _site(method="status", valid_status=[200], invalid_status=[404])
        exists, reason = evaluate(site, 999, "", "alice")
        self.assertIsNone(exists)
        self.assertEqual(reason, "unexpected-999")

    def test_status_method_with_presence_required(self):
        # status=200 + valid_status=[200] but presence_text defined and not in body → UNKNOWN
        site = _site(
            method="status",
            valid_status=[200],
            presence_text=["@{username}"],
        )
        exists, reason = evaluate(site, 200, "<html>random page</html>", "alice")
        self.assertIsNone(exists)
        self.assertEqual(reason, "no-presence")

    def test_status_method_with_presence_satisfied(self):
        site = _site(
            method="status",
            valid_status=[200],
            presence_text=["@{username}"],
        )
        exists, _ = evaluate(site, 200, "profile @alice here", "alice")
        self.assertIs(exists, True)


class MethodMessage(unittest.TestCase):
    """method=message: requires presence_text to fire for FOUND; no
    presence pattern in body → UNKNOWN; matched absence_text → MISSING."""

    def test_presence_match_means_found(self):
        site = _site(
            method="message",
            presence_text=["@{username}"],
        )
        exists, reason = evaluate(site, 200, "page about @alice", "alice")
        self.assertIs(exists, True)
        self.assertEqual(reason, "presence")

    def test_no_presence_in_body_is_unknown_not_missing(self):
        site = _site(
            method="message",
            presence_text=["@{username}"],
        )
        exists, reason = evaluate(site, 200, "<html>generic</html>", "alice")
        self.assertIsNone(exists)
        self.assertEqual(reason, "no-presence")

    def test_message_method_absence_still_wins(self):
        site = _site(
            method="message",
            presence_text=["@{username}"],
            absence_text=["user not found"],
        )
        body = "user not found — but also @alice somewhere"
        exists, reason = evaluate(site, 200, body, "alice")
        # absence runs before presence: a page saying both is missing.
        self.assertIs(exists, False)
        self.assertEqual(reason, "absence")

    def test_message_method_no_presence_patterns_falls_back_to_status(self):
        # method=message with no presence_text: trust the status.
        site = _site(method="message", valid_status=[200])
        exists, _ = evaluate(site, 200, "", "alice")
        self.assertIs(exists, True)


class TitleSubstitution(unittest.TestCase):
    """{username} substitution must happen for both presence and absence."""

    def test_presence_substitution(self):
        site = _site(
            method="message",
            presence_text=["<title>{username} ·"],
        )
        exists, _ = evaluate(site, 200, "<title>alice · GitHub</title>", "alice")
        self.assertIs(exists, True)

    def test_username_case_matters_in_match(self):
        # Substitution is literal, not case-insensitive: substring match is exact.
        site = _site(
            method="message",
            presence_text=["@{username}"],
        )
        exists, _ = evaluate(site, 200, "page about @Alice", "alice")
        # Body has @Alice (capital A), pattern is @alice → no match → UNKNOWN.
        self.assertIsNone(exists)


if __name__ == "__main__":
    unittest.main()
