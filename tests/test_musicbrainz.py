"""Offline tests for the public MusicBrainz fallback mapping."""

from __future__ import annotations

import os
import unittest
from unittest.mock import Mock


os.environ.setdefault("DISCORD_TOKEN", "test-token")

from musicbrainz import (  # noqa: E402
    MusicBrainzClient,
    MusicBrainzUnavailable,
    _pick_exact_release,
    release_to_details,
)


class MusicBrainzFallbackTests(unittest.TestCase):
    def test_503_opens_meaningful_global_retry_window(self):
        client = MusicBrainzClient()
        response = Mock(status_code=503, headers={"Retry-After": "120"})
        client.session.get = Mock(return_value=response)

        with self.assertRaises(MusicBrainzUnavailable) as raised:
            client._json("/release/", params={"query": "test", "fmt": "json"})

        self.assertGreaterEqual(raised.exception.retry_after, 15 * 60)
        response.raise_for_status.assert_not_called()

    def test_exact_matching_refuses_similar_but_wrong_release(self):
        candidates = [
            {
                "id": "wrong",
                "title": "Uncut Gems (Deluxe)",
                "artist-credit": [{"name": "KiiiKiii"}],
            },
            {
                "id": "right",
                "title": "Uncut Gem",
                "artist-credit": [{"name": "KiiiKiii"}],
            },
        ]
        result = _pick_exact_release(candidates, "KiiiKiii", "Uncut Gem")
        self.assertEqual(result["id"], "right")

    def test_release_mapping_keeps_aoty_only_stats_empty(self):
        details = release_to_details(
            {
                "id": "release-id",
                "title": "Uncut Gem",
                "date": "2025-03-24",
                "artist-credit": [{"name": "KiiiKiii"}],
                "release-group": {
                    "id": "group-id",
                    "primary-type": "EP",
                    "genres": [{"name": "K-Pop", "count": 4}],
                },
                "label-info": [{"label": {"name": "Starship Entertainment"}}],
                "media": [
                    {
                        "position": 1,
                        "tracks": [
                            {"title": "Debut Song", "length": 185000},
                            {"title": "Second Song", "length": 245000},
                        ],
                    }
                ],
            },
            requested_format="EP",
        )

        self.assertEqual(details["artist"], "KiiiKiii")
        self.assertEqual(details["year"], "2025")
        self.assertEqual(details["album_format"], "EP")
        self.assertEqual(details["genres"], ["K-Pop"])
        self.assertEqual(details["tracklist"][0]["duration"], "3:05")
        self.assertIn("coverartarchive.org/release-group/group-id", details["cover"])
        self.assertFalse(details["_section_complete"]["score"])
        self.assertFalse(details["_section_complete"]["ranking"])
