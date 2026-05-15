"""Test that expand-discovered handles get the right confidence boost.

The boost is what makes recursive expansion smarter than Maigret's: a
handle discovered via a cryptographically-signed Keybase proof gets a
much higher starting confidence than one found in a Linktree link.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from confidence import score_all
from expand import (
    SOURCE_BIO_LINK, SOURCE_KEYBASE_PROOF, SOURCE_WEIGHTS,
)
from models import CheckResult


def _result(site: str, variant: str, profile: dict | None = None) -> CheckResult:
    return CheckResult(
        site=site, category="social",
        url=f"https://example.com/{variant}",
        exists=True, reliability=80, variant=variant,
        profile=profile or {"display_name": variant},
    )


class ConfidenceBoost(unittest.TestCase):
    def test_keybase_boost_applied(self):
        r = _result("Twitter", "alice")
        score_all(
            [r], clusters=[], subject_name="", input_username="alice",
            expand_source_map={"alice": SOURCE_WEIGHTS[SOURCE_KEYBASE_PROOF]},
        )
        # Save the boosted score, then re-run without the boost to compare.
        boosted = r.score
        r2 = _result("Twitter", "alice")
        score_all([r2], clusters=[], subject_name="", input_username="alice",
                  expand_source_map=None)
        unboosted = r2.score
        self.assertGreater(boosted, unboosted,
                           f"keybase boost should raise score; got {boosted} vs {unboosted}")
        # Exact-handle base + keybase boost should be approximately 30 apart.
        self.assertEqual(boosted - unboosted, SOURCE_WEIGHTS[SOURCE_KEYBASE_PROOF])

    def test_bio_link_boost_smaller_than_keybase(self):
        r_keybase = _result("Twitter", "alice")
        r_biolink = _result("Twitter", "bob")
        score_all(
            [r_keybase, r_biolink], clusters=[],
            subject_name="", input_username="x",
            expand_source_map={
                "alice": SOURCE_WEIGHTS[SOURCE_KEYBASE_PROOF],
                "bob": SOURCE_WEIGHTS[SOURCE_BIO_LINK],
            },
        )
        self.assertGreater(r_keybase.score, r_biolink.score)

    def test_score_clamped_to_100(self):
        # A high base score + max boost must clamp at 100, not overflow.
        r = _result("Twitter", "alice", profile={
            "display_name": "alice",
            "verified": True,    # +50
            "followers": 1000,
        })
        score_all([r], clusters=[], subject_name="alice", input_username="alice",
                  expand_source_map={"alice": 30})
        self.assertLessEqual(r.score, 100)
        self.assertGreaterEqual(r.score, 0)

    def test_no_boost_when_map_empty(self):
        r = _result("Twitter", "alice")
        score_all([r], clusters=[], subject_name="", input_username="alice",
                  expand_source_map=None)
        r2 = _result("Twitter", "alice")
        score_all([r2], clusters=[], subject_name="", input_username="alice",
                  expand_source_map={})
        self.assertEqual(r.score, r2.score)


if __name__ == "__main__":
    unittest.main()
