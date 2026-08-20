"""Offline tests for the public MusicBrainz fallback mapping."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

import requests


os.environ.setdefault("DISCORD_TOKEN", "test-token")

from musicbrainz import (  # noqa: E402
    MusicBrainzClient,
    MusicBrainzUnavailable,
    _pick_exact_release,
    release_to_details,
)
from shared import country_flag_emoji  # noqa: E402


class MusicBrainzFallbackTests(unittest.TestCase):
    def test_release_country_uses_flags_and_global_code_uses_globe(self):
        self.assertEqual(country_flag_emoji("PL"), "🇵🇱")
        self.assertEqual(country_flag_emoji("XW"), "🌐")
        self.assertEqual(country_flag_emoji("invalid"), "")

    def test_outage_cooldown_survives_client_restart_without_another_request(self):
        with tempfile.TemporaryDirectory(prefix="kotone-musicbrainz-test-") as tmp:
            state_file = Path(tmp) / "musicbrainz_state.json"
            client = MusicBrainzClient(state_file=str(state_file))
            response = Mock(status_code=503, headers={})
            client.session.get = Mock(return_value=response)

            with self.assertRaises(MusicBrainzUnavailable):
                client._json("/release/", params={"query": "test", "fmt": "json"})

            self.assertTrue(state_file.exists())
            self.assertEqual(client.session.get.call_count, 1)

            restarted = MusicBrainzClient(state_file=str(state_file))
            restarted.session.get = Mock()
            with self.assertRaises(MusicBrainzUnavailable) as raised:
                restarted._json("/release/", params={"query": "test", "fmt": "json"})

            self.assertGreater(raised.exception.retry_after, 0)
            restarted.session.get.assert_not_called()

    def test_not_found_does_not_open_global_cooldown(self):
        with tempfile.TemporaryDirectory(prefix="kotone-musicbrainz-test-") as tmp:
            client = MusicBrainzClient(
                state_file=str(Path(tmp) / "musicbrainz_state.json")
            )
            response = Mock(status_code=404, headers={})
            error = requests.HTTPError("404 Client Error")
            error.response = response
            response.raise_for_status.side_effect = error
            client.session.get = Mock(return_value=response)

            with self.assertRaises(MusicBrainzUnavailable) as raised:
                client._json("/release/", params={"query": "missing", "fmt": "json"})

            self.assertEqual(raised.exception.retry_after, 0)
            self.assertFalse(client.status()["blocked"])

    def test_503_opens_meaningful_global_retry_window(self):
        with tempfile.TemporaryDirectory(prefix="kotone-musicbrainz-test-") as tmp:
            client = MusicBrainzClient(
                state_file=str(Path(tmp) / "musicbrainz_state.json")
            )
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

    def test_artist_lookup_accepts_aoty_romanization_as_musicbrainz_alias(self):
        client = MusicBrainzClient()
        client._json = Mock(
            side_effect=[
                {"artists": [{"id": "shiina-id", "name": "椎名林檎"}]},
                {
                    "id": "shiina-id",
                    "name": "椎名林檎",
                    "aliases": [
                        {"name": "Sheena Ringo"},
                        {"name": "Yumiko Shiina"},
                    ],
                    "area": {"name": "Japan", "iso-3166-1-code": "JP"},
                    "life-span": {"begin": "1978-11-25"},
                },
            ]
        )

        result = client.lookup_artist("Sheena Ringo")

        self.assertEqual(result["musicbrainz_artist_id"], "shiina-id")
        self.assertIn("椎名林檎", result["aliases"])
        self.assertIn("Yumiko Shiina", result["aliases"])
