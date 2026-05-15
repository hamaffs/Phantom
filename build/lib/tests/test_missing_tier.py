"""Tests for the missing-result tier classifier."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from confidence import (
    TIER_MISSING_CONFIRMED, TIER_MISSING_UNCERTAIN,
    annotate_missing, missing_tier,
)
from models import CheckResult


def _missing(site="X", reason="404", reliability=90) -> CheckResult:
    return CheckResult(
        site=site, category="dev",
        url=f"https://example.com/{site}",
        exists=False, reliability=reliability,
        reason=reason,
    )


class MissingTier(unittest.TestCase):
    def test_clean_404_on_reliable_site_is_confirmed(self):
        r = _missing(reliability=90, reason="404")
        self.assertEqual(missing_tier(r), TIER_MISSING_CONFIRMED)

    def test_absence_pattern_on_reliable_site_is_confirmed(self):
        r = _missing(reliability=90, reason="absence")
        self.assertEqual(missing_tier(r), TIER_MISSING_CONFIRMED)

    def test_low_reliability_site_is_uncertain(self):
        r = _missing(reliability=60, reason="404")
        self.assertEqual(missing_tier(r), TIER_MISSING_UNCERTAIN)

    def test_retried_verdict_is_uncertain(self):
        # Even on a reliable site, a verdict that took a retry shouldn't
        # carry the "confirmed missing" label — the network was flaky.
        r = _missing(reliability=95, reason="404+retry")
        self.assertEqual(missing_tier(r), TIER_MISSING_UNCERTAIN)

    def test_cached_verdict_is_uncertain(self):
        r = _missing(reliability=95, reason="404+cached")
        self.assertEqual(missing_tier(r), TIER_MISSING_UNCERTAIN)

    def test_unexpected_status_is_uncertain(self):
        # `unexpected-NNN` reasons mean the site returned something
        # outside its known valid/invalid set — not a clean signal.
        r = _missing(reliability=90, reason="unexpected-503")
        self.assertEqual(missing_tier(r), TIER_MISSING_UNCERTAIN)

    def test_5xx_status_is_uncertain(self):
        r = _missing(reliability=90, reason="503")
        self.assertEqual(missing_tier(r), TIER_MISSING_UNCERTAIN)

    def test_non_missing_result_returns_empty(self):
        r = CheckResult(
            site="X", category="dev", url="https://x", exists=True,
            reliability=90, reason="200",
        )
        self.assertEqual(missing_tier(r), "")


class AnnotateMissing(unittest.TestCase):
    def test_stamps_tier_on_missing_in_place(self):
        results = [
            _missing(reliability=90, reason="404"),
            _missing(reliability=60, reason="404"),
        ]
        annotate_missing(results)
        self.assertEqual(results[0].tier, TIER_MISSING_CONFIRMED)
        self.assertEqual(results[1].tier, TIER_MISSING_UNCERTAIN)

    def test_does_not_overwrite_existing_tier(self):
        r = _missing(reliability=90, reason="404")
        r.tier = "custom_tier"
        annotate_missing([r])
        self.assertEqual(r.tier, "custom_tier")


if __name__ == "__main__":
    unittest.main()
