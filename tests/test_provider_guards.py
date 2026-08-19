"""Offline regression tests for the public metadata-provider safety gates."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


os.environ.setdefault("DISCORD_TOKEN", "test-token")

import lastfm  # noqa: E402
from source_switches import SourceSwitches  # noqa: E402


class ProviderSwitchTests(unittest.TestCase):
    def test_optional_provider_switches_are_independent_and_persistent(self):
        path = Path(tempfile.mkdtemp(prefix="kotone-provider-switch-")) / "state.json"
        switches = SourceSwitches(str(path))

        self.assertEqual(
            switches.status(),
            {"musicbrainz": True, "lastfm": True},
        )
        self.assertFalse(switches.set_enabled("lastfm", False, actor="enso"))
        self.assertTrue(switches.enabled("musicbrainz"))
        self.assertFalse(SourceSwitches(str(path)).enabled("lastfm"))


class LastFMGuardTests(unittest.TestCase):
    def test_rate_limit_opens_shared_cooldown_without_a_retry_burst(self):
        client = lastfm.LastFMClient()
        response = Mock(status_code=429, headers={"Retry-After": "120"})
        client.session.get = Mock(return_value=response)

        with patch.object(lastfm, "LASTFM_API_ENABLED", True):
            with self.assertRaises(lastfm.LastFMUnavailable) as raised:
                client._json("artist.getInfo", artist="Artist")
            with self.assertRaises(lastfm.LastFMUnavailable):
                client._json("artist.getInfo", artist="Artist")

        self.assertGreaterEqual(
            raised.exception.retry_after,
            lastfm.LASTFM_OUTAGE_COOLDOWN,
        )
        client.session.get.assert_called_once()

    def test_album_mapping_keeps_provider_counts_separate(self):
        client = lastfm.LastFMClient()
        payload = {
            "album": {
                "artist": "Artist",
                "name": "Album",
                "url": "https://www.last.fm/music/Artist/Album",
                "listeners": "12",
                "playcount": "34",
                "userplaycount": "5",
                "tracks": {
                    "track": {
                        "name": "Song",
                        "duration": "180",
                        "mbid": "recording-id",
                        "@attr": {"rank": "1"},
                    }
                },
            }
        }
        with patch.object(client, "_json", return_value=payload):
            result = client.album_info("Artist", "Album", username="desinitesse")

        self.assertEqual(result["tracklist"][0]["duration"], "3:00")
        self.assertEqual(result["external_metadata"]["user_playcount"], "5")
        self.assertEqual(
            result["external_metadata"]["tracks"][0]["musicbrainz_recording_id"],
            "recording-id",
        )
