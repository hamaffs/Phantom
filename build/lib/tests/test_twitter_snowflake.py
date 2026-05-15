"""Tests for the Twitter snowflake → creation-timestamp derivation."""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from enrich import extract_twitter


# Build a fake Twitter hydration blob with a known snowflake.
# 2023-01-01T12:00:00Z = 1672574400000 ms epoch
# Snowflake = (ms_since_twitter_epoch << 22) — we just need a
# representative value for the test.
_TWITTER_EPOCH_MS = 1288834974657
_TARGET_MS = 1672574400000   # 2023-01-01T12:00:00 UTC
_TARGET_SNOWFLAKE = (_TARGET_MS - _TWITTER_EPOCH_MS) << 22


def _fake_body(snowflake: int, screen_name: str = "alice") -> str:
    return (
        '{"data":{"user":{"result":{'
        '"legacy":{'
        f'"screen_name":"{screen_name}",'
        '"name":"Alice",'
        '"description":"hi",'
        f'"id_str":"{snowflake}",'
        '"followers_count":42,'
        '"friends_count":10,'
        '"statuses_count":100,'
        '"created_at":"2023-01-01T12:00:00.000Z",'
        '"verified":false'
        '}}}}}'
    )


class SnowflakeDerivation(unittest.TestCase):
    def test_modern_snowflake_decoded(self):
        body = _fake_body(_TARGET_SNOWFLAKE)
        out = extract_twitter(body, "alice")
        self.assertIn("created_precise", out)
        # The recovered timestamp should be within a second of target.
        recovered = datetime.fromisoformat(out["created_precise"])
        target = datetime(2023, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        delta = abs((recovered - target).total_seconds())
        self.assertLess(delta, 2.0, f"recovered={recovered} target={target}")

    def test_legacy_pre_snowflake_id_skipped(self):
        # Old Twitter IDs (pre Nov 2010) are sequential and below 100M.
        # We must NOT try to decode them as snowflakes — the resulting
        # timestamp would be nonsensical.
        body = _fake_body(44196397)  # elonmusk's real ID
        out = extract_twitter(body, "alice")
        self.assertNotIn("created_precise", out)
        # But user_id should still be stored.
        self.assertEqual(out.get("user_id"), "44196397")

    def test_default_profile_image_flag(self):
        body = (
            '{"data":{"user":{"result":{"legacy":{'
            '"screen_name":"alice",'
            '"name":"Alice",'
            '"default_profile_image":true'
            '}}}}}'
        )
        out = extract_twitter(body, "alice")
        self.assertTrue(out.get("default_avatar"))

    def test_withheld_in_countries(self):
        body = (
            '{"data":{"user":{"result":{"legacy":{'
            '"screen_name":"alice",'
            '"name":"Alice",'
            '"withheld_in_countries":["DE","FR"]'
            '}}}}}'
        )
        out = extract_twitter(body, "alice")
        self.assertEqual(out.get("withheld_in_countries"), ["DE", "FR"])


if __name__ == "__main__":
    unittest.main()
