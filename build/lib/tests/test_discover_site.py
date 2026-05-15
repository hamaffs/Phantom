"""Tests for the auto-discovery heuristics — pure functions, no network."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from discover_site import (
    _propose_absence,
    _propose_presence,
    build_entry,
)


class PresenceProposal(unittest.TestCase):
    def test_extracts_anchored_pattern(self):
        real = '<html><title>alice — Example</title><a href="/u/alice">profile</a></html>'
        fake = '<html><title>Not found — Example</title></html>'
        out = _propose_presence(real, fake, "alice")
        # Every proposed pattern must contain {username}.
        self.assertTrue(all("{username}" in p for p in out))
        # Every pattern must have literal anchor text outside {username}.
        for p in out:
            self.assertGreaterEqual(len(p.replace("{username}", "")), 3, p)

    def test_rejects_patterns_present_in_fake(self):
        # The contract: every proposed pattern, when {username} is
        # re-substituted, must NOT appear in the fake body. (Otherwise it
        # wouldn't discriminate between FOUND and MISSING.)
        real = "uniquemarker alice"
        fake = "shared text alice"   # literal "alice" exists in fake but no shared HTML context
        out = _propose_presence(real, fake, "alice")
        self.assertTrue(out, "expected at least one pattern from a unique-marker real body")
        for p in out:
            substituted = p.replace("{username}", "alice")
            self.assertNotIn(substituted, fake, f"pattern {p!r} leaks into fake body")

    def test_empty_when_username_not_in_real(self):
        out = _propose_presence("<html>no handle here</html>", "<html>missing</html>", "alice")
        self.assertEqual(out, [])

    def test_short_patterns_filtered(self):
        # `<alice>` alone, with no literal context, must not be proposed.
        real = "<alice>"
        fake = "<bob>"
        out = _propose_presence(real, fake, "alice")
        # `<` is only 1 char of literal — should be filtered.
        for p in out:
            self.assertGreaterEqual(len(p.replace("{username}", "")), 3, p)

    def test_url_echo_pattern_rejected(self):
        # Regression: TryHackMe / KhanAcademy false-positive bug. A site
        # that echoes the requested URL back in its response produces
        # presence patterns like `":/p/{username}` that LOOK anchored
        # (contain quotes/colons) but match anything at scan time because
        # the URL itself always contains `/p/<whatever>`.
        #
        # The defence: substitute the FAKE handle into each candidate
        # and verify it doesn't appear in the fake body. If it does,
        # the pattern is URL-echo and must be rejected.
        real = '<meta property="og:url" content="https://ex.com/p/alice"> alice profile'
        fake = '<meta property="og:url" content="https://ex.com/p/zzzfake_x"> Not found'
        out = _propose_presence(real, fake, "alice", "zzzfake_x")
        # Verify the URL-echo pattern is NOT in the output.
        for p in out:
            # The substituted fake form must not appear in fake body.
            self.assertNotIn(
                p.replace("{username}", "zzzfake_x"), fake,
                f"pattern {p!r} is a URL-echo false-positive trap",
            )


class AbsenceProposal(unittest.TestCase):
    def test_known_not_found_phrase_picked_up(self):
        real = "<html>alice's profile</html>"
        fake = "<html><h1>Page not found</h1></html>"
        out = _propose_absence(fake, real)
        self.assertIn("Page not found", out)

    def test_phrase_in_both_is_not_proposed(self):
        # If "page not found" appears in BOTH responses (e.g. a footer
        # link to a 404 helper), it's not a discriminator.
        real = '<a href="/help">"page not found"</a> alice'
        fake = '<a href="/help">"page not found"</a> nope'
        out = _propose_absence(fake, real)
        self.assertNotIn("page not found", [s.lower() for s in out])


class BuildEntry(unittest.TestCase):
    def test_status_method_when_codes_differ(self):
        entry = build_entry(
            url_template="https://ex.com/{username}",
            real_status=200, real_body="alice profile",
            fake_status=404, fake_body="not found",
            username="alice",
            name="Ex", category="social", reliability=85,
            impersonate=False, notes=[],
        )
        self.assertEqual(entry["method"], "status")
        self.assertEqual(entry["valid_status"], [200])
        self.assertEqual(entry["invalid_status"], [404])

    def test_message_method_when_codes_match(self):
        entry = build_entry(
            url_template="https://ex.com/{username}",
            real_status=200, real_body="<title>alice</title>",
            fake_status=200, fake_body="<title>Page not found</title>",
            username="alice",
            name="Ex", category="social", reliability=85,
            impersonate=False, notes=[],
        )
        self.assertEqual(entry["method"], "message")

    def test_impersonate_flag_added(self):
        entry = build_entry(
            url_template="https://ex.com/{username}",
            real_status=200, real_body="alice", fake_status=404, fake_body="nope",
            username="alice",
            name="Ex", category="social", reliability=85,
            impersonate=True, notes=[],
        )
        self.assertEqual(entry["protection"], ["tls_fingerprint"])


if __name__ == "__main__":
    unittest.main()
