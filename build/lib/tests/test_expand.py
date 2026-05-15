"""Tests for the cross-link expansion URL → handle parser."""
from __future__ import annotations

import sys
import unittest
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from expand import (
    SOURCE_BIO_LINK, SOURCE_GITHUB_HANDLE, SOURCE_KEYBASE_PROOF,
    SOURCE_LINKED_ACCOUNT, SOURCE_WEBSITE, SOURCE_WEIGHTS,
    _extract_one, discover_new_handles,
)


@dataclass
class _StubResult:
    """Minimal stub for CheckResult — discover_new_handles only reads .profile."""
    profile: dict = field(default_factory=dict)


class HandleExtraction(unittest.TestCase):
    def test_twitter(self):
        self.assertEqual(_extract_one("https://twitter.com/alice"), "alice")
        self.assertEqual(_extract_one("https://x.com/alice"), "alice")
        self.assertEqual(_extract_one("https://www.x.com/@alice"), "alice")

    def test_instagram(self):
        self.assertEqual(_extract_one("https://www.instagram.com/foo.bar/"), "foo.bar")

    def test_tiktok_requires_at_sign(self):
        self.assertEqual(_extract_one("https://www.tiktok.com/@foo"), "foo")
        # No @ prefix → not a profile URL.
        self.assertIsNone(_extract_one("https://www.tiktok.com/foo"))

    def test_github(self):
        self.assertEqual(_extract_one("https://github.com/torvalds"), "torvalds")
        # Multi-segment paths shouldn't return a handle.
        self.assertIsNone(_extract_one("https://github.com/torvalds/linux"))

    def test_reddit(self):
        self.assertEqual(_extract_one("https://reddit.com/u/alice"), "alice")
        self.assertEqual(_extract_one("https://www.reddit.com/user/alice/"), "alice")

    def test_bandcamp_subdomain(self):
        self.assertEqual(_extract_one("https://acme.bandcamp.com/"), "acme")
        self.assertIsNone(_extract_one("https://bandcamp.com/"))

    def test_unknown_host_returns_none(self):
        self.assertIsNone(_extract_one("https://example.com/foo"))

    def test_youtube_handle_form(self):
        self.assertEqual(_extract_one("https://www.youtube.com/@MrBeast"), "MrBeast")

    def test_handles_bare_url_without_scheme(self):
        self.assertEqual(_extract_one("twitter.com/alice"), "alice")


class SourceTagging(unittest.TestCase):
    """discover_new_handles must tag each discovered handle with the source
    that surfaced it. Stronger sources win when the same handle appears in
    multiple places."""

    def test_keybase_proof_tagged_correctly(self):
        r = _StubResult(profile={
            "proofs": ["https://twitter.com/alice"],
        })
        out = discover_new_handles([r], already_tested={"original_handle"})
        self.assertIn(("alice", SOURCE_KEYBASE_PROOF), out)

    def test_linktree_links_tagged_as_bio_link(self):
        r = _StubResult(profile={
            "links": [{"title": "Twitter", "url": "https://twitter.com/alice"}],
        })
        out = discover_new_handles([r], already_tested=set())
        self.assertIn(("alice", SOURCE_BIO_LINK), out)

    def test_already_tested_handles_are_filtered(self):
        r = _StubResult(profile={
            "linked_accounts": ["https://twitter.com/known"],
        })
        out = discover_new_handles([r], already_tested={"known"})
        self.assertEqual(out, [])

    def test_strongest_source_wins_on_duplicate(self):
        # Same handle appears in both a Keybase proof and a Linktree link.
        # The Keybase proof is stronger, so it should be the recorded source.
        r = _StubResult(profile={
            "proofs": ["https://twitter.com/alice"],
            "links": [{"url": "https://twitter.com/alice"}],
        })
        out = discover_new_handles([r], already_tested=set())
        self.assertEqual(out, [("alice", SOURCE_KEYBASE_PROOF)])

    def test_github_x_handle_field_recognised(self):
        r = _StubResult(profile={
            "x_handle": "alice",
        })
        out = discover_new_handles([r], already_tested=set())
        self.assertIn(("alice", SOURCE_GITHUB_HANDLE), out)

    def test_weights_ordered_correctly(self):
        # Sanity check on the weight table — Keybase > GitHub-explicit >
        # JSON-LD linked > website > bio-link. Reordering these without
        # thinking about it would change scan-time confidence boosts.
        self.assertGreater(SOURCE_WEIGHTS[SOURCE_KEYBASE_PROOF],
                           SOURCE_WEIGHTS[SOURCE_GITHUB_HANDLE])
        self.assertGreater(SOURCE_WEIGHTS[SOURCE_GITHUB_HANDLE],
                           SOURCE_WEIGHTS[SOURCE_LINKED_ACCOUNT])
        self.assertGreater(SOURCE_WEIGHTS[SOURCE_LINKED_ACCOUNT],
                           SOURCE_WEIGHTS[SOURCE_WEBSITE])
        self.assertGreater(SOURCE_WEIGHTS[SOURCE_WEBSITE],
                           SOURCE_WEIGHTS[SOURCE_BIO_LINK])


if __name__ == "__main__":
    unittest.main()
