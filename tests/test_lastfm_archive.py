"""Offline coverage for the independent newest-to-oldest Last.fm archive."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path


os.environ.setdefault("DISCORD_TOKEN", "test-token")

from lastfm_database import LastFMDatabase  # noqa: E402


class LastFMArchiveDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="kotone-lastfm-test-")
        self.db = LastFMDatabase(str(Path(self.temp.name) / "lastfm.sqlite3"))

    def tearDown(self):
        self.db.connection.close()
        self.temp.cleanup()

    def test_latest_page_is_saved_before_older_pages(self):
        self.db.save_profile(
            "enso",
            {
                "lastfm_username": "desinitesse",
                "total_scrobbles": "1234",
                "artist_count": "90",
                "album_count": "200",
            },
        )
        self.db.import_page(
            "enso",
            {
                "page": 1,
                "total_pages": 2,
                "total": 3,
                "tracks": [
                    {
                        "played_at": 300,
                        "artist": "New Artist",
                        "album": "New Album",
                        "track": "New Track",
                    },
                    {
                        "played_at": 200,
                        "artist": "Older Artist",
                        "album": "Older Album",
                        "track": "Older Track",
                    },
                ],
            },
        )

        self.assertEqual(self.db.latest_scrobble("enso")["track"], "New Track")
        self.assertEqual(self.db.state("enso")["next_page"], 2)
        self.assertFalse(self.db.state("enso")["complete"])
        self.assertEqual(self.db.get_profile("enso")["total_scrobbles"], 1234)
        self.assertEqual(
            self.db.archive_statistics("enso"),
            {"scrobbles": 2, "artists": 2, "albums": 2, "tracks": 2},
        )

        self.db.import_page(
            "enso",
            {
                "page": 2,
                "total_pages": 2,
                "total": 3,
                "tracks": [
                    {
                        "played_at": 100,
                        "artist": "Oldest Artist",
                        "album": None,
                        "track": "Oldest Track",
                    }
                ],
            },
        )
        self.assertTrue(self.db.state("enso")["complete"])

        self.db.refresh_newest_page(
            "enso",
            {
                "tracks": [
                    {
                        "played_at": 400,
                        "artist": "Newest Artist",
                        "album": "Newest Album",
                        "track": "Newest Track",
                    }
                ]
            },
        )
        self.assertEqual(self.db.latest_scrobble("enso")["track"], "Newest Track")
        self.assertTrue(self.db.state("enso")["complete"])
