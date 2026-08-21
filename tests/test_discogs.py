"""Offline checks for Discogs' narrow tracklist/duration fallback."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path


os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="kotone-discogs-data-"))

from discogs import DiscogsClient, release_to_details  # noqa: E402


class DiscogsFallbackTests(unittest.TestCase):
    def test_release_tracks_and_total_duration_are_mapped(self):
        details = release_to_details(
            {
                "id": 123,
                "master_id": 456,
                "released": "1981-11-13",
                "tracklist": [
                    {"position": "1", "title": "First", "duration": "3:13"},
                    {"position": "2", "title": "Second", "duration": "3:53"},
                ],
            }
        )
        self.assertIsNotNone(details)
        assert details is not None
        self.assertEqual(details["duration"], "7:06")
        self.assertEqual(
            details["tracklist"],
            [
                {"number": 1, "title": "First", "duration": "3:13", "disc": 1},
                {"number": 2, "title": "Second", "duration": "3:53", "disc": 1},
            ],
        )
        self.assertEqual(
            details["external_metadata"]["discogs_release_id"], "123"
        )
        self.assertEqual(details["release_date"], "1981-11-13")
        self.assertEqual(details["year"], "1981")
        self.assertEqual(
            details["external_metadata"]["discogs_master_url"],
            "https://www.discogs.com/master/456",
        )

    def test_client_without_token_is_not_treated_as_ready(self):
        with tempfile.TemporaryDirectory(prefix="kotone-discogs-test-") as tmp:
            client = DiscogsClient(
                token="",
                state_file=str(Path(tmp) / "discogs_state.json"),
            )
            self.assertFalse(client.status()["configured"])


if __name__ == "__main__":
    unittest.main()
