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

    def test_known_script_and_romanized_aliases_share_statistics_bucket(self):
        self.db.import_tracks(
            "enso",
            [
                {
                    "played_at": 100,
                    "artist": "椎名林檎",
                    "album": "加爾基 精液 栗ノ花",
                    "track": "Track A",
                },
                {
                    "played_at": 200,
                    "artist": "Sheena Ringo",
                    "album": "Kalk Samen Chestnut Flower",
                    "track": "Track B",
                },
                {
                    "played_at": 300,
                    "artist": "Ringo Sheena",
                    "album": "加爾基 精液 栗ノ花 Kalk Samen Chestnut Flower",
                    "track": "Track C",
                },
            ],
        )
        self.assertEqual(
            self.db.archive_statistics("enso"),
            {"scrobbles": 3, "artists": 1, "albums": 1, "tracks": 3},
        )
        self.assertEqual(
            self.db.artist_scrobble_count("enso", "Sheena Ringo"),
            3,
        )
        self.assertEqual(
            self.db.album_scrobble_count(
                "enso", "Kalk Samen Chestnut Flower", artist="Sheena Ringo"
            ),
            3,
        )

    def test_album_counter_falls_back_to_canonical_names_when_mbid_differs(self):
        """A Last.fm release-group ID must not hide imported scrobbles."""

        self.db.import_tracks(
            "enso",
            [
                {
                    "played_at": 100,
                    "artist": "Cocteau Twins",
                    "album": "Heaven or Las Vegas",
                    "track": "Cherry-Coloured Funk",
                    "album_mbid": "lastfm-release-group-id",
                }
            ],
        )

        self.assertEqual(
            self.db.album_scrobble_count(
                "enso",
                "Heaven or Las Vegas",
                artist="Cocteau Twins",
                album_mbid="kotone-release-group-id",
            ),
            1,
        )

    def test_best_archive_key_prefers_complete_kotone_import_over_login_stub(self):
        self.db.save_profile(
            "desinitesse",
            {"lastfm_username": "desinitesse", "total_scrobbles": "100"},
        )
        self.db.import_tracks(
            "desinitesse",
            [{"played_at": 1, "artist": "A", "album": "X", "track": "One"}],
        )
        self.db.import_tracks(
            "enso",
            [
                {"played_at": 2, "artist": "A", "album": "X", "track": "Two"},
                {"played_at": 3, "artist": "A", "album": "X", "track": "Three"},
                {"played_at": 4, "artist": "A", "album": "X", "track": "Four"},
            ],
        )

        self.assertEqual(self.db.best_archive_key("enso", "desinitesse"), "enso")
        self.assertEqual(
            self.db.album_scrobble_count(
                self.db.best_archive_key("enso", "desinitesse"),
                "X",
                artist="A",
            ),
            3,
        )

    def test_archive_progress_uses_fresh_profile_total_over_stale_cursor_total(self):
        self.db.import_page(
            "enso",
            {
                "page": 1,
                "total_pages": 1,
                "total": 2,
                "tracks": [
                    {"played_at": 1, "artist": "A", "album": "X", "track": "One"}
                ],
            },
        )
        self.db.save_profile(
            "enso",
            {"lastfm_username": "desinitesse", "total_scrobbles": "5"},
        )

        progress = self.db.archive_progress("enso")

        self.assertEqual(progress["scrobbles"], 1)
        self.assertEqual(progress["total_scrobbles"], 5)

    def test_offline_csv_marks_history_complete_without_a_row_source_column(self):
        self.db.import_tracks(
            "enso",
            [
                {
                    "played_at": 100,
                    "artist": "Offline Artist",
                    "album": "Offline Album",
                    "track": "Offline Track",
                }
            ],
        )
        self.db.mark_imported_complete("enso")

        state = self.db.state("enso")
        self.assertTrue(state["complete"])
        self.assertEqual(state["next_page"], 1)
        self.assertEqual(state["total_scrobbles"], 1)
        self.assertFalse(self.db.newest_due("enso", 60 * 60))
        columns = {
            row["name"]
            for row in self.db.connection.execute("PRAGMA table_info(scrobbles)")
        }
        self.assertNotIn("source", columns)
