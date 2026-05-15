"""Golden-file regression tests for per-site profile extractors.

Why this exists
---------------

Phantom's most fragile dependency is the *shape* of each platform's
HTML / JSON response. When YouTube re-shuffles `ytInitialData` keys, or
Instagram renames `biography_with_entities` to something else, the
extractor silently returns less data and the dossier quietly degrades
— no exception, no test failure. By the time anyone notices, weeks of
scans have run without that field.

These tests use **minimal synthetic fixtures** — hand-authored
response shapes that exercise each extractor's specific parse path.
They aren't saved real bodies because:

  1. Real bodies are huge (YouTube ≈ 800KB each) — repo bloat.
  2. Real bodies drift weekly as users update their profiles, so
     replay tests fail spuriously.
  3. Real bodies contain other users' private data fields even on
     public profiles — privacy hazard to commit.

The fixtures are intentionally tiny — just enough JSON / HTML to
trigger the parse path. If a parser stops finding a field that the
fixture clearly contains, the test fails immediately and points at
the regression.

Add a new fixture when you rewrite a parser. Update an existing
fixture only when you intentionally change the contract.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from enrich import (
    extract_github, extract_instagram, extract_threads,
    extract_twitter, extract_youtube,
)


# ---------------------------------------------------------------------------
# YouTube — ytInitialData parsing
# ---------------------------------------------------------------------------

_YT_FIXTURE = """
<!DOCTYPE html><html><head></head><body>
<script>
var ytInitialData = {
  "metadata": {
    "channelMetadataRenderer": {
      "title": "Alice Test",
      "description": "Alice Real-Name",
      "avatar": {"thumbnails": [{"url": "https://yt3/avatar.jpg", "width": 900}]}
    }
  },
  "onResponseReceivedEndpoints": [{
    "showEngagementPanelEndpoint": {
      "engagementPanel": {
        "engagementPanelSectionListRenderer": {
          "content": {
            "sectionListRenderer": {
              "contents": [{
                "itemSectionRenderer": {
                  "contents": [{
                    "aboutChannelRenderer": {
                      "metadata": {
                        "aboutChannelViewModel": {
                          "description": "Alice Real-Name",
                          "country": "France",
                          "subscriberCountText": "1,234 subscribers",
                          "viewCountText": "7,669 views",
                          "videoCountText": "14 videos",
                          "joinedDateText": {"content": "Joined Sep 21, 2020"},
                          "links": [{
                            "channelExternalLinkViewModel": {
                              "title": {"content": "instagram"},
                              "link": {"content": "instagram.com/alice"}
                            }
                          }]
                        }
                      }
                    }
                  }]
                }
              }]
            }
          }
        }
      }
    }
  }]
};</script>
</body></html>
""".strip()


class YouTubeFixture(unittest.TestCase):
    """Locks the ytInitialData parser against schema drift."""

    def test_real_bio_replaces_og_description(self):
        out = extract_youtube(_YT_FIXTURE, "alice")
        self.assertEqual(out.get("bio"), "Alice Real-Name")

    def test_display_name_from_channel_metadata(self):
        out = extract_youtube(_YT_FIXTURE, "alice")
        self.assertEqual(out.get("display_name"), "Alice Test")

    def test_links_panel_feeds_linked_accounts(self):
        out = extract_youtube(_YT_FIXTURE, "alice")
        self.assertIn("linked_accounts", out)
        self.assertIn("https://instagram.com/alice", out["linked_accounts"])

    def test_location_from_about_panel(self):
        out = extract_youtube(_YT_FIXTURE, "alice")
        self.assertEqual(out.get("location"), "France")

    def test_stats_parsed_as_numbers(self):
        out = extract_youtube(_YT_FIXTURE, "alice")
        self.assertEqual(out.get("followers"), 1234)
        self.assertEqual(out.get("views"), 7669)
        self.assertEqual(out.get("posts"), 14)

    def test_joined_date_normalised(self):
        out = extract_youtube(_YT_FIXTURE, "alice")
        # The "Joined " prefix is stripped.
        self.assertEqual(out.get("joined"), "Sep 21, 2020")

    def test_hd_avatar_url(self):
        out = extract_youtube(_YT_FIXTURE, "alice")
        self.assertEqual(out.get("photo"), "https://yt3/avatar.jpg")


# ---------------------------------------------------------------------------
# Instagram — web_profile_info JSON API
# ---------------------------------------------------------------------------

_IG_FIXTURE_BIO_LINKS = """
{"data":{"user":{
  "full_name":"Alice Test",
  "biography":"hi @bob check https://twitter.com/charlie",
  "biography_with_entities":{
    "entities":[
      {"user":{"username":"bob"}},
      {"url":"https://twitter.com/charlie"}
    ]
  },
  "profile_pic_url_hd":"https://ig/hd.jpg",
  "edge_followed_by":{"count":50},
  "edge_follow":{"count":159},
  "edge_owner_to_timeline_media":{"count":1},
  "is_verified":false,
  "is_private":true,
  "external_url":"https://linktr.ee/alice",
  "category_name":"Personal Blog",
  "business_email":"alice@example.com"
}}}
""".strip()


class InstagramFixture(unittest.TestCase):
    def test_basic_fields(self):
        out = extract_instagram(_IG_FIXTURE_BIO_LINKS, "alice")
        self.assertEqual(out["display_name"], "Alice Test")
        self.assertEqual(out["followers"], 50)
        self.assertEqual(out["following"], 159)
        self.assertEqual(out["posts"], 1)
        self.assertTrue(out["private"])
        self.assertEqual(out["photo"], "https://ig/hd.jpg")

    def test_external_url_becomes_website(self):
        out = extract_instagram(_IG_FIXTURE_BIO_LINKS, "alice")
        self.assertEqual(out["website"], "https://linktr.ee/alice")

    def test_category_and_business_email(self):
        out = extract_instagram(_IG_FIXTURE_BIO_LINKS, "alice")
        self.assertEqual(out["category"], "Personal Blog")
        self.assertEqual(out["email"], "alice@example.com")

    def test_bio_entities_feed_linked_accounts(self):
        out = extract_instagram(_IG_FIXTURE_BIO_LINKS, "alice")
        linked = out.get("linked_accounts") or []
        # The @bob mention becomes an Instagram URL
        self.assertIn("https://instagram.com/bob", linked)
        # The inline URL is preserved
        self.assertIn("https://twitter.com/charlie", linked)
        # Self-mention (alice in own bio entities) shouldn't appear.
        self.assertNotIn("https://instagram.com/alice", linked)


# ---------------------------------------------------------------------------
# Threads — SSR JSON parsing
# ---------------------------------------------------------------------------

_THREADS_FIXTURE = """
<html><body>
<script>
{"user":{
  "full_name":"Alice Threads",
  "biography":"hello world",
  "follower_count":42,
  "profile_pic_url":"https://t.cdn/avatar.jpg",
  "is_verified":false,
  "is_private":false,
  "user_id":"123456789",
  "bio_links":[{"url":"https://example.com/alice"}],
  "text_app_biography":{"text_fragments":{"fragments":[
    {"mention_fragment":{"username":"bob"}},
    {"plaintext":"some text"}
  ]}}
}}
</script>
</body></html>
""".strip()


class ThreadsFixture(unittest.TestCase):
    def test_basic_fields_from_ssr(self):
        out = extract_threads(_THREADS_FIXTURE, "alice")
        self.assertEqual(out.get("display_name"), "Alice Threads")
        self.assertEqual(out.get("bio"), "hello world")
        self.assertEqual(out.get("followers"), 42)
        self.assertEqual(out.get("photo"), "https://t.cdn/avatar.jpg")
        self.assertEqual(out.get("user_id"), "123456789")

    def test_bio_links_surfaced(self):
        out = extract_threads(_THREADS_FIXTURE, "alice")
        linked = out.get("linked_accounts") or []
        self.assertIn("https://example.com/alice", linked)

    def test_mention_becomes_threads_link(self):
        out = extract_threads(_THREADS_FIXTURE, "alice")
        linked = out.get("linked_accounts") or []
        self.assertIn("https://www.threads.com/@bob", linked)


# ---------------------------------------------------------------------------
# GitHub — SSR'd profile page
# ---------------------------------------------------------------------------

_GH_FIXTURE = """
<!DOCTYPE html><html><head>
<meta property="og:title" content="alice - Overview">
<meta property="og:image" content="https://avatars.githubusercontent.com/u/12345?v=4">
</head><body>
<span itemprop="name">Alice Doe</span>
<div class="user-profile-bio"><div>OSINT enthusiast</div></div>
<a rel="nofollow me" href="https://alice.example/">alice.example</a>
<span class="p-org">Acme Inc.</span>
<span class="p-label">Paris, France</span>
</body></html>
""".strip()


class GitHubFixture(unittest.TestCase):
    def test_display_name_overrides_og_title(self):
        out = extract_github(_GH_FIXTURE, "alice")
        # The og:title is "alice - Overview" — the itemprop=name wins.
        self.assertEqual(out.get("display_name"), "Alice Doe")


# ---------------------------------------------------------------------------
# Twitter — user-object regex parse
# ---------------------------------------------------------------------------

_TW_FIXTURE_MODERN = """
{"user":{"result":{"legacy":{
  "screen_name":"alice",
  "name":"Alice X",
  "description":"hi",
  "id_str":"1621953612988440581",
  "created_at":"2023-02-04T19:27:41.000Z",
  "followers_count":1,
  "friends_count":143,
  "statuses_count":2706,
  "verified":false,
  "profile_image_url_https":"https://pbs/avatar_normal.jpg",
  "profile_banner_url":"https://pbs/banner.jpg",
  "default_profile_image":false
}}}}
""".strip()


class TwitterFixture(unittest.TestCase):
    def test_basics(self):
        out = extract_twitter(_TW_FIXTURE_MODERN, "alice")
        self.assertEqual(out["display_name"], "Alice X")
        self.assertEqual(out["followers"], 1)
        self.assertEqual(out["following"], 143)
        self.assertEqual(out["posts"], 2706)

    def test_avatar_upgraded_to_400(self):
        out = extract_twitter(_TW_FIXTURE_MODERN, "alice")
        # _normal → _400x400 substitution
        self.assertIn("_400x400", out["photo"])

    def test_snowflake_decoded(self):
        out = extract_twitter(_TW_FIXTURE_MODERN, "alice")
        self.assertIn("created_precise", out)
        # Should match the ISO created_at to within a few seconds.
        self.assertTrue(out["created_precise"].startswith("2023-02-04T19:27"))

    def test_banner_surfaced(self):
        out = extract_twitter(_TW_FIXTURE_MODERN, "alice")
        self.assertEqual(out["banner"], "https://pbs/banner.jpg")


if __name__ == "__main__":
    unittest.main()
